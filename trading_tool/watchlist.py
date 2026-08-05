"""
自选看板后端数据聚合
=================================
批量计算所有关注股票的状态：
  - 操盘建议（三层信号）
  - 神奇九转状态
  - 历史新高 / 近N日新高 / 新低
  - 近5日涨跌幅
"""

import sys
import os
import time
import threading
import pandas as pd
import numpy as np
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data_fetcher import DataFetcher
from strategy_engine import FujimotoStrategy
from nine_turn import calc_nine_turn_display
from db import get_conn, db_lock
from daily_store import store_daily_bars
import symbols
import watchlist_store

fetcher = DataFetcher()

# ----------------------------------------------------------------------
#  默认看板（两档）
#  - USER：普通用户 / 未登录精简默认
#  - ADMIN：管理员默认全量（原完整关注列表 + TSLA）
#  - WATCHLIST：兼容旧代码，等同 USER
# ----------------------------------------------------------------------
WATCHLIST_USER_DEFAULT = {
    '000001': '上证指数',
    '159501': '纳指ETF嘉实',
    '399300': '沪深300',
    'TSLA': '特斯拉',
    'SPCX': 'SpaceX',
    'NVDA': '英伟达',
    'WTI': '原油WTI',
}

WATCHLIST_ADMIN_DEFAULT = {
    'OUST': 'Ouster', 'FLY': 'Firefly Aerospace', 'SPCX': 'SpaceX',
    'FIGR': 'Figure Technology', 'MU': '美光科技', 'VCX': 'Fundrise Innovation Fund',
    'ETN': '伊顿', 'GEV': 'GE Vernova', 'HIMS': 'Hims & Hers', 'APP': 'AppLovin',
    'ICE': '洲际交易所', 'SMH': '半导体指数ETF', 'VGT': '领航信息技术',
    'JEPI': '摩根JEPi', 'GOOG': '谷歌C', 'LITE': 'Lumentum',
    'ASTS': 'AST SpaceMobile', 'FCX': 'Freeport-McMoRan', 'ASM': 'ASM International',
    'EUV': 'EUV', 'WTI': '原油WTI', 'AVGO': '博通', 'NVDA': '英伟达', 'INTC': '英特尔',
    'TSLA': '特斯拉',
    '600887': '伊利股份', '600111': '北方稀土', '601899': '紫金矿业',
    '159880': '有色ETF鹏华', '518850': '黄金ETF华夏', '560710': '船舶ETF富国', '159985': '豆粕ETF',
    '159501': '纳指ETF嘉实', '562500': '机器人ETF华夏', '513310': '中韩半导体ETF华泰柏瑞',
    '000001': '上证指数', '399300': '沪深300',
}

WATCHLIST = dict(WATCHLIST_USER_DEFAULT)


# ----------------------------------------------------------------------
#  按用户的自选看板（sqlite / Supabase 持久化）
#  未登录 → 精简默认；管理员无自选 → 全量管理员默认；普通用户清空后不回退。
# ----------------------------------------------------------------------
def get_user_watchlist_symbols(user_id, access_token: str = None) -> list:
    """返回 [(symbol, name), ...]，按 sort_order, created_at 升序。须尽量带 access_token 以通过 RLS。"""
    try:
        items = watchlist_store.get_items(user_id, access_token)
        return items if items else []
    except Exception:
        # 读库失败时不要伪装成「用户删光了」，交由上层降级
        raise


def add_user_watchlist(user_id: int, symbol: str, name: str = "") -> bool:
    symbol = (symbol or "").strip()
    if not symbol:
        return False
    norm = symbols.normalize_symbol(symbol)
    ok = watchlist_store.add(user_id, norm["symbol"], name or "", norm["market"], "")
    # 让该用户看板缓存立即失效，下一次请求会即时重算（修复"添加后不刷新"）
    _invalidate_cache(user_id)
    return ok


def remove_user_watchlist(user_id: int, symbol: str) -> bool:
    symbol = (symbol or "").strip().upper()
    ok = watchlist_store.remove(user_id, symbol)
    # 同上：删除后让看板缓存立即失效
    _invalidate_cache(user_id)
    return ok


def _invalidate_cache(user_id) -> None:
    """使某用户的看板缓存失效（增删自选后调用）。"""
    caches = globals().get("_CACHES")
    if caches is not None and user_id is not None:
        caches.pop(user_id, None)
        # 字符串/UUID 与 int 两种 key 都清一遍，避免类型不一致导致清不掉
        caches.pop(str(user_id), None)


def invalidate_user_cache(user_id) -> None:
    """对外接口：添加/删除自选后调用，确保下次看板请求拿到最新列表。"""
    _invalidate_cache(user_id)


def resolve_watchlist_items(user_id=None, is_admin: bool = False, access_token: str = None) -> list:
    """有用户自选用自选；否则管理员全量默认 / 其他精简默认。"""
    if user_id:
        items = get_user_watchlist_symbols(user_id, access_token)
        if items:
            return items
        if is_admin:
            return list(WATCHLIST_ADMIN_DEFAULT.items())
        return list(WATCHLIST_USER_DEFAULT.items())
    return list(WATCHLIST_USER_DEFAULT.items())


# 新高新低检测窗口（天数，从大到小）
HIGH_LOW_WINDOWS = [
    (99999, '历史新高'),
    (250, '近一年新高'),
    (120, '近半年新高'),
    (60, '近60天新高'),
    (30, '近30天新高'),
    (20, '近20天新高'),
    (10, '近10天新高'),
    (5, '近5天新高'),
]

LOW_WINDOWS = [
    (250, '近一年新低'),
    (120, '近半年新低'),
    (60, '近60天新低'),
    (30, '近30天新低'),
    (20, '近20天新低'),
    (10, '近10天新低'),
    (5, '近5天新低'),
]

# 持仓定位：压舱石(稳健蓝筹/指数/宽基ETF) / 高赔率(高成长科技) / 周期弹性(有色/原油/矿业) / 卫星仓(投机小仓)
# 不同定位用不同估值算法（见 ROLE_VAL_CONFIG）。
STOCK_ROLE = {
    # 压舱石：稳健蓝筹、宽基/行业ETF、指数
    '000001': '压舱石', '399300': '压舱石', '600887': '压舱石', 'GOOG': '压舱石',
    'ICE': '压舱石', 'ETN': '压舱石', 'VCX': '压舱石', 'SMH': '压舱石', 'VGT': '压舱石',
    'JEPI': '压舱石', '159501': '压舱石', '518850': '压舱石',
    # 高赔率：高成长科技/半导体
    'NVDA': '高赔率', 'MU': '高赔率', 'AVGO': '高赔率', 'ASM': '高赔率', 'APP': '高赔率',
    'HIMS': '高赔率', 'GEV': '高赔率', 'LITE': '高赔率', '562500': '高赔率', '513310': '高赔率',
    # 周期弹性：有色/原油/矿业/周期品
    '600111': '周期弹性', '601899': '周期弹性', 'FCX': '周期弹性', 'WTI': '周期弹性',
    '159880': '周期弹性', '560710': '周期弹性', '159985': '周期弹性', 'INTC': '周期弹性',
    # 卫星仓：投机/高方差小仓
    'OUST': '卫星仓', 'FLY': '卫星仓', 'SPCX': '卫星仓', 'FIGR': '卫星仓',
    'ASTS': '卫星仓', 'EUV': '卫星仓',
}
DEFAULT_ROLE = '压舱石'

# 各定位的估值算法配置
#   method='ma'  : 收盘价相对均线偏离度，over/under 为高估/低估阈值（小数）
#   method='pct' : 收盘价在近 window 日区间的分位数，over/under 为高估/低估分位阈值
ROLE_VAL_CONFIG = {
    '压舱石':   {'method': 'ma',  'ma': 250, 'over': 0.08,  'under': -0.08},
    '高赔率':   {'method': 'ma',  'ma': 250, 'over': 0.35,  'under': -0.35},
    '周期弹性': {'method': 'ma',  'ma': 120, 'over': 0.18,  'under': -0.18},
    '卫星仓':   {'method': 'pct', 'window': 250, 'over': 0.80, 'under': 0.20},
}


@dataclass
class StockStatus:
    """单只股票状态"""
    code: str
    name: str
    market: str
    price: float = 0
    change_1d: float = 0          # 当日涨跌幅%（最近一根K线相对前一根）
    change_5d: float = 0          # 近5日涨跌幅%
    signal: str = "观望"          # 兼容旧字段：建议动作（汇总用）
    signal_color: str = "gray"    # 建议动作颜色
    trend: str = ""
    # 结构化三列：时机 / 趋势过滤 / 建议动作
    timing: str = "—"             # 九转时机
    timing_color: str = "gray"
    trend_filter: str = "—"       # 趋势过滤（系统层）
    trend_filter_color: str = "gray"
    action: str = "观望"          # 建议动作（综合）
    action_color: str = "gray"
    action_reason: str = ""       # 一句话理由
    nine_turn: str = "无"         # 九转状态（日级|月级 合并文本）
    nine_turn_dir: str = "none"   # down/up/none（主级别方向）
    nine_turn_level: str = "日"   # 主级别：月/日
    nine_turn_daily: str = "日·无九转信号"
    nine_turn_monthly: str = "月·—"
    nine_turn_daily_state: str = "none"
    nine_turn_monthly_state: str = "none"
    nine_turn_complete: bool = False
    nine_turn_completing: bool = False
    high_low: str = "—"           # 新高新低状态（最大幅度）
    high_low_type: str = "none"   # high/low/none
    role: str = "压舱石"          # 持仓定位：压舱石/高赔率/周期弹性/卫星仓
    valuation: str = "合理"       # 估值状态：低估/高估/合理
    valuation_type: str = "fair"  # under/over/fair
    valuation_detail: str = ""    # 估值依据（如 "MA250 -8%" / "分位85%"）
    error: str = ""


def _detect_high_low(df: pd.DataFrame) -> tuple:
    """
    检测新高/新低状态，只返回最大幅度的那个

    Returns: (状态文本, 类型high/low/none)
    """
    if len(df) < 5:
        return ("—", "none")

    cur_close = df['close'].iloc[-1]
    n = len(df)

    # 检测新高（从大到小，取第一个满足的）
    for window, label in HIGH_LOW_WINDOWS:
        lookback = min(window, n - 1)  # 不含当前日
        if lookback < 1:
            continue
        window_data = df['close'].iloc[:-1].tail(lookback)
        if len(window_data) > 0 and cur_close > window_data.max():
            return (label, "high")

    # 检测新低
    for window, label in LOW_WINDOWS:
        lookback = min(window, n - 1)
        if lookback < 1:
            continue
        window_data = df['close'].iloc[:-1].tail(lookback)
        if len(window_data) > 0 and cur_close < window_data.min():
            return (label, "low")

    return ("—", "none")


def _calc_valuation(df: pd.DataFrame, role: str = DEFAULT_ROLE) -> tuple:
    """
    估值状态（按持仓定位差异化算法）。返回 (文本, 类型, 依据明细)。

    - 压舱石  : 收盘价 vs MA250，偏离 ±8% 即判定（稳健股小幅错配即值得关注）
    - 高赔率  : 收盘价 vs MA250，偏离 ±35% 才判定（成长股常大幅超涨，只标极端）
    - 周期弹性: 收盘价 vs MA120，偏离 ±18% 判定（捕捉周期峰谷，用中期均线）
    - 卫星仓  : 收盘价在近250日区间的分位数，≥80% 高估 / ≤20% 低估
               （投机品均值回归意义弱，改用区间位置）

    均线周期自适应：取 ≤目标周期且数据足够的最长标准周期；数据不足时退化为全部均值。
    """
    cfg = ROLE_VAL_CONFIG.get(role, ROLE_VAL_CONFIG[DEFAULT_ROLE])
    closes = df['close']
    n = len(df)
    close = float(closes.iloc[-1])

    if cfg['method'] == 'pct':
        win = closes.tail(min(cfg['window'], n))
        rank = float((win.values <= close).sum()) / len(win) if len(win) else 0.5
        if rank >= cfg['over']:
            return ("高估", "over", f"分位{rank*100:.0f}%")
        if rank <= cfg['under']:
            return ("低估", "under", f"分位{rank*100:.0f}%")
        return ("合理", "fair", f"分位{rank*100:.0f}%")

    # method == 'ma'
    target = cfg['ma']
    ma = None
    used = None
    for p in (250, 200, 150, 120, 100, 50, 30, 20):
        if p <= target and n >= p:
            m = closes.rolling(p).mean().iloc[-1]
            if not pd.isna(m) and float(m) > 0:
                ma = float(m)
                used = f"MA{p}"
                break
    if ma is None:
        ma = float(closes.mean())
        used = f"均值{n}"
    if ma <= 0:
        return ("合理", "fair", "")
    dev = (close - ma) / ma
    if dev >= cfg['over']:
        return ("高估", "over", f"{used} {dev*100:+.0f}%")
    if dev <= cfg['under']:
        return ("低估", "under", f"{used} {dev*100:+.0f}%")
    return ("合理", "fair", f"{used} {dev*100:+.0f}%")


def get_stock_status(code: str, name: str, days: int = 300) -> StockStatus:
    """获取单只股票完整状态"""
    market = '美股' if not code.isdigit() else 'A股'
    status = StockStatus(code=code, name=name, market=market)
    status.role = STOCK_ROLE.get(code, DEFAULT_ROLE)

    try:
        df = fetcher.fetch(code, days)
        if len(df) < 10:
            status.error = f"数据不足({len(df)}根)"
            return status

        # 用全精度收盘价计算涨跌幅，避免“先四舍五入价格再算”导致
        # 低价/微小波动股（如 8.626→8.63）涨跌幅符号翻转。
        last_close = float(df['close'].iloc[-1])
        status.price = round(last_close, 2)

        # 当日涨跌幅（昨收口径：今收/昨收 - 1，即最近一根K线收盘价相对前一根）。
        # A-share primary K-line is Eastmoney forward-adjusted; avoids false day-change after split/dividend.
        if len(df) >= 2:
            prev_close = float(df['close'].iloc[-2])
            status.change_1d = round((last_close - prev_close) / prev_close * 100, 2)

        # 近5日涨跌幅（同样用全精度）
        lookback_5 = min(5, len(df) - 1)
        if lookback_5 > 0:
            prev_5 = float(df['close'].iloc[-1 - lookback_5])
            status.change_5d = round((last_close - prev_5) / prev_5 * 100, 2)

        strategy = FujimotoStrategy(total_capital=100000)
        result = strategy.analyze(df)
        status.trend = result.trend.value
        nt_signal = result.signal.value  # 策略原始信号：买入/卖出/持有/加仓/观望
        nt = calc_nine_turn_display(df)

        # 藤本茂阶梯：近5日相对涨跌触及档位（无成本价时的粗筛）
        buy_ladder_hit = status.change_5d <= -15.0
        sell_ladder_hit = status.change_5d >= 25.0

        # ---------- 1) 九转时机 ----------
        if nt.get('conflict'):
            status.timing = "日/月九转背离"
            status.timing_color = "gray"
        elif nt.get('is_complete') and nt.get('direction') == 'down':
            status.timing = "下跌九转完成·买点"
            status.timing_color = "orange"
        elif nt.get('is_completing') and nt.get('direction') == 'down':
            status.timing = "下跌九转临近"
            status.timing_color = "orange"
        elif nt.get('is_complete') and nt.get('direction') == 'up':
            status.timing = "上涨九转完成·卖点"
            status.timing_color = "red"
        elif nt.get('is_completing') and nt.get('direction') == 'up':
            status.timing = "上涨九转临近"
            status.timing_color = "red"
        else:
            status.timing = "无明确九转"
            status.timing_color = "gray"

        # ---------- 2) 趋势过滤（系统层）----------
        trend = status.trend or "震荡"
        if trend == "多头趋势":
            status.trend_filter = "多头趋势"
            status.trend_filter_color = "green"
        elif trend == "空头趋势":
            status.trend_filter = "空头趋势"
            status.trend_filter_color = "red"
        else:
            status.trend_filter = "震荡整理"
            status.trend_filter_color = "gray"

        # ---------- 3) 建议动作（时机 × 趋势 × 阶梯，冲突则降权）----------
        timing_buy = status.timing_color == "orange" and "九转" in status.timing
        timing_sell = status.timing_color == "red" and "九转" in status.timing
        reasons = []

        if nt.get('conflict'):
            status.action = "观望"
            status.action_color = "gray"
            reasons.append("日/月九转方向冲突")
        elif timing_buy and trend == "空头趋势":
            status.action = "轻仓观察"
            status.action_color = "gray"
            reasons.append("九转买点与空头趋势背离，不宜重仓")
        elif timing_sell and trend == "多头趋势":
            status.action = "减仓观察"
            status.action_color = "orange"
            reasons.append("九转卖点与多头趋势背离，优先风控")
        elif timing_buy and trend == "多头趋势":
            status.action = "关注买入"
            status.action_color = "orange"
            reasons.append("九转买点与多头同向")
        elif timing_sell and trend == "空头趋势":
            status.action = "关注卖出"
            status.action_color = "red"
            reasons.append("九转卖点与空头同向")
        elif timing_buy:
            status.action = "关注买入"
            status.action_color = "orange"
            reasons.append(status.timing)
        elif timing_sell:
            status.action = "关注卖出"
            status.action_color = "red"
            reasons.append(status.timing)
        elif buy_ladder_hit:
            status.action = "阶梯抄底关注"
            status.action_color = "orange"
            reasons.append(f"近5日{status.change_5d:+.1f}%触及藤本茂买入档")
        elif sell_ladder_hit:
            status.action = "阶梯止盈关注"
            status.action_color = "red"
            reasons.append(f"近5日{status.change_5d:+.1f}%触及藤本茂卖出档")
        elif nt_signal in ('买入', '加仓'):
            status.action = "策略偏多"
            status.action_color = "orange"
            reasons.append(f"融合策略信号：{nt_signal}")
        elif nt_signal == '卖出':
            status.action = "策略偏空"
            status.action_color = "red"
            reasons.append("融合策略信号：卖出")
        else:
            status.action = "观望"
            status.action_color = "gray"
            reasons.append("无共振时机")

        status.action_reason = "；".join(reasons)
        # 兼容旧看板汇总字段
        if status.action in ("关注买入", "阶梯抄底关注", "策略偏多"):
            status.signal = "即将上涨关注"
            status.signal_color = "orange"
        elif status.action in ("关注卖出", "阶梯止盈关注", "策略偏空", "减仓观察"):
            status.signal = "上涨见顶关注"
            status.signal_color = "red"
        elif status.action == "轻仓观察":
            status.signal = "九转背离·观望"
            status.signal_color = "gray"
        else:
            status.signal = "下跌观望"
            status.signal_color = "gray"

        # 九转状态文本（日级与月级并列展示）
        status.nine_turn = nt['text']
        status.nine_turn_dir = nt['direction']
        status.nine_turn_level = nt['level']
        status.nine_turn_daily = nt['daily_text']
        status.nine_turn_monthly = nt['monthly_text']
        status.nine_turn_daily_state = nt['daily_state']
        status.nine_turn_monthly_state = nt['monthly_state']
        status.nine_turn_complete = nt['is_complete']
        status.nine_turn_completing = nt['is_completing']

        # 新高新低
        hl_text, hl_type = _detect_high_low(df)
        status.high_low = hl_text
        status.high_low_type = hl_type

        # 估值状态（按持仓定位差异化算法：压舱石/高赔率/周期弹性/卫星仓）
        val_text, val_type, val_detail = _calc_valuation(df, status.role)
        status.valuation = val_text
        status.valuation_type = val_type
        status.valuation_detail = val_detail

        # 顺手把当日粒度行情落库（daily_data），供回测 / 指标分析 / 容错使用
        try:
            store_daily_bars(code, df, source="watchlist")
        except Exception:
            pass

    except Exception as e:
        status.error = str(e)[:50]

    return status


def _status_to_dict(st: StockStatus) -> dict:
    """StockStatus 转字典"""
    return {
        'code': st.code,
        'name': st.name,
        'market': st.market,
        'price': st.price,
        'change_1d': st.change_1d,
        'change_5d': st.change_5d,
        'signal': st.signal,
        'signal_color': st.signal_color,
        'trend': st.trend,
        'timing': st.timing,
        'timing_color': st.timing_color,
        'trend_filter': st.trend_filter,
        'trend_filter_color': st.trend_filter_color,
        'action': st.action,
        'action_color': st.action_color,
        'action_reason': st.action_reason,
        'nine_turn': st.nine_turn,
        'nine_turn_dir': st.nine_turn_dir,
        'nine_turn_level': st.nine_turn_level,
        'nine_turn_daily': st.nine_turn_daily,
        'nine_turn_monthly': st.nine_turn_monthly,
        'nine_turn_daily_state': st.nine_turn_daily_state,
        'nine_turn_monthly_state': st.nine_turn_monthly_state,
        'nine_turn_complete': st.nine_turn_complete,
        'nine_turn_completing': st.nine_turn_completing,
        'high_low': st.high_low,
        'high_low_type': st.high_low_type,
        'role': st.role,
        'valuation': st.valuation,
        'valuation_type': st.valuation_type,
        'valuation_detail': st.valuation_detail,
        'error': st.error,
    }


# 缓存：按 user_id 分桶（0 = 全局默认看板），后台刷新，接口永远秒回
_CACHES = {}          # key -> {'data':..., 'ts':..., 'refreshing':...}
_WATCHLIST_TTL = 300
_refresh_lock = threading.Lock()


def _compute_watchlist(items: list = None, user_id: int = None, key: int = None) -> dict:
    """真正计算全部关注股票状态（并行抓取提速，应在后台线程执行）。

    当传入 key 时，会“边算边写”：每完成一只就把已就绪的股票增量写入
    _CACHES[key]['data']，使前端可以轮询到逐行就绪的看板，而不必傻等全部算完。
    """
    if items is None:
        items = resolve_watchlist_items(user_id)
    total = len(items)
    # 初始化增量缓存（key 给定时），让前端可立即轮询到“计算中（含部分结果）”
    if key is not None:
        seeded0 = [{
            'code': c, 'name': n or c,
            'market': '美股' if not str(c).isdigit() else 'A股',
            'price': '-', 'change_1d': None, 'change_5d': None,
            'signal': '计算中', 'signal_color': 'gray',
            'action': '计算中', 'action_color': 'gray',
            'timing': '—', 'timing_color': 'gray',
            'trend_filter': '—', 'trend_filter_color': 'gray',
            'nine_turn': '—', 'high_low': '—',
            'pending': True, 'error': None,
        } for c, n in items]
        _CACHES[key] = {
            'data': {'success': True, 'computing': True, 'updated_at': '',
                     'count': 0, 'total': total, 'summary': {}, 'stocks': seeded0,
                     'symbols': [{'code': c, 'name': n} for c, n in items]},
            'ts': time.time(), 'refreshing': True,
        }

    def _sort_key(x):
        return (0 if (x.get('market') or '美股') == '美股' else 1, x['code'])

    partial = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        fut_to_code = {ex.submit(get_stock_status, code, name): code for code, name in items}
        for fut in as_completed(fut_to_code):
            code = fut_to_code[fut]
            try:
                st = fut.result()
            except Exception:
                st = StockStatus(code=code, name=dict(items).get(code, code),
                                 market='美股' if not code.isdigit() else 'A股')
                st.error = "获取失败"
            partial.append(_status_to_dict(st))
            # 增量写回缓存：每完成一只就更新，前端即可逐行看到已就绪的股票
            if key is not None:
                sorted_partial = sorted(partial, key=_sort_key)
                _CACHES[key]['data'] = {
                    'success': True, 'computing': True, 'updated_at': '',
                    'count': len(sorted_partial), 'total': total,
                    'summary': {}, 'stocks': sorted_partial,
                }

    # 最终排序：美股在前，A股在后
    partial.sort(key=_sort_key)

    # 分类汇总（操盘建议）
    summary = {'即将上涨关注': 0, '上涨见顶关注': 0, '下跌观望': 0, 'error': 0}
    for s in partial:
        if s.get('error'):
            summary['error'] += 1
        else:
            summary[s['signal']] = summary.get(s['signal'], 0) + 1

    final = {
        'success': True,
        'computing': False,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'count': len(partial),
        'total': total,
        'summary': summary,
        'stocks': partial,
        'symbols': [{'code': c, 'name': n} for c, n in items],
    }
    if key is not None:
        _CACHES[key] = {'data': final, 'ts': time.time(), 'refreshing': False}
    return final


def _background_refresh(key, items, user_id):
    """后台刷新某个看板缓存（边算边增量写入 _CACHES[key]，带兜底熔断）"""
    try:
        _compute_watchlist(items=items, user_id=user_id, key=key)
    except Exception:
        # 兜底：确保刷新标记被清除，避免前端一直轮询却永远算不完
        entry = _CACHES.get(key)
        if entry:
            entry['refreshing'] = False
            if entry['data'] is None:
                entry['data'] = {'success': True, 'computing': False, 'updated_at': '',
                                 'count': 0, 'total': len(items) if items else 0,
                                 'summary': {}, 'stocks': []}
        else:
            _CACHES[key] = {'data': {'success': True, 'computing': False, 'updated_at': '',
                                     'count': 0, 'total': len(items) if items else 0,
                                     'summary': {}, 'stocks': []},
                            'ts': 0, 'refreshing': False}


def get_watchlist_status(user_id=None, force: bool = False, is_admin: bool = False,
                         access_token: str = None) -> dict:
    """
    获取关注股票看板状态（接口永远秒回）。
      - 已登录且有自选 → 计算该用户看板
      - 已登录普通用户自选为空 → 空列表（允许删光，不回退）
      - 已登录管理员自选为空 → 管理员全量默认看板
      - 未登录 → 精简默认看板（约 7 只）
      - 读自选库失败 → 降级为对应默认列表，避免整页失败
    """
    if user_id is not None:
        cache_uid = str(user_id)
        user_items = None
        read_err = None
        try:
            user_items = get_user_watchlist_symbols(user_id, access_token)
        except Exception as e:
            read_err = str(e)[:120]
            user_items = None

        if user_items:
            key, items = cache_uid, user_items
        elif user_items is not None and len(user_items) == 0 and not is_admin:
            # 明确空列表（读成功且无行）
            empty = {
                'success': True,
                'computing': False,
                'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'count': 0,
                'total': 0,
                'summary': {},
                'stocks': [],
                'empty': True,
            }
            _CACHES[cache_uid] = {'data': empty, 'ts': time.time(), 'refreshing': False}
            return empty
        elif is_admin:
            key, items = f"admin_default:{cache_uid}", list(WATCHLIST_ADMIN_DEFAULT.items())
        else:
            # 读库失败：降级精简默认，标记 degraded，避免登录后整板失败
            key, items = f"user_fallback:{cache_uid}", list(WATCHLIST_USER_DEFAULT.items())
    else:
        key, items = 0, list(WATCHLIST_USER_DEFAULT.items())

    now = time.time()
    cache = _CACHES.get(key)
    # 命中且未过期且非强制刷新 → 直接返回
    if not force and cache and cache['data'] is not None and (now - cache['ts']) < _WATCHLIST_TTL:
        cache['data']["updated_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return cache['data']

    # 需要刷新：确保仅一个后台刷新在跑；保留已有行 + 为新增代码放占位，避免列表被清空
    with _refresh_lock:
        cache = _CACHES.get(key)
        if not (cache and cache.get('refreshing')):
            item_codes = [c for c, _ in items]
            item_name = {c: n for c, n in items}
            prev = []
            if cache and isinstance(cache.get('data'), dict):
                prev = list(cache['data'].get('stocks') or [])
            # 只保留仍在自选中的旧行
            code_set = set(item_codes)
            seeded = [s for s in prev if s.get('code') in code_set]
            have = {s.get('code') for s in seeded}
            for c in item_codes:
                if c not in have:
                    seeded.append({
                        'code': c, 'name': item_name.get(c) or c,
                        'market': '美股' if not str(c).isdigit() else 'A股',
                        'price': '-', 'change_1d': None, 'change_5d': None,
                        'signal': '计算中', 'signal_color': 'gray',
                        'action': '计算中', 'action_color': 'gray',
                        'timing': '—', 'timing_color': 'gray',
                        'trend_filter': '—', 'trend_filter_color': 'gray',
                        'nine_turn': '—', 'high_low': '—',
                        'pending': True, 'error': None,
                    })
            _CACHES[key] = {
                'data': {'success': True, 'computing': True, 'updated_at': '',
                         'count': len(seeded), 'total': len(items),
                         'summary': {}, 'stocks': seeded,
                         'symbols': [{'code': c, 'name': n} for c, n in items]},
                'ts': time.time(), 'refreshing': True,
            }
            threading.Thread(
                target=_background_refresh, args=(key, items, user_id), daemon=True
            ).start()

    cache = _CACHES.get(key)
    if cache and cache['data'] is not None:
        return cache['data']
    return {'success': True, 'computing': True, 'updated_at': '',
            'count': 0, 'total': len(items), 'summary': {}, 'stocks': [],
            'symbols': [{'code': c, 'name': n} for c, n in items]}


if __name__ == "__main__":
    # CLI 直接同步计算（不走后台增量缓存，便于本地排障）
    data = _compute_watchlist(items=list(WATCHLIST_ADMIN_DEFAULT.items()), key=None)
    print(f"关注股票: {data['count']}只  更新时间: {data['updated_at']}")
    print(f"{'代码':8s} {'名称':12s} {'现价':>10s} {'5日%':>7s} {'信号':6s} {'九转':20s} {'高低':12s}")
    print("-" * 90)
    for s in data['stocks']:
        if s['error']:
            print(f"{s['code']:8s} {s['name']:12s} ERROR: {s['error']}")
        else:
            print(f"{s['code']:8s} {s['name']:12s} {s['price']:>10.2f} {s['change_5d']:>6.1f}% {s['signal']:6s} {s['nine_turn']:20s} {s['high_low']:12s}")
