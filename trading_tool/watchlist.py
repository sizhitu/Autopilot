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
    if caches is None or user_id is None:
        return
    uid = str(user_id)
    for k in (
        user_id, uid,
        f"admin_default:{uid}", f"admin_default:{user_id}",
        f"user_fallback:{uid}", f"user_fallback:{user_id}",
    ):
        caches.pop(k, None)


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
    bar_date: str = ""  # 现价对应的K线交易日 YYYY-MM-DD
    bar_stale: bool = False  # 明显落后于应有交易日
    change_1d: float = 0          # 当日涨跌幅%（最近一根K线相对前一根）
    change_5d: float = 0          # 近5日涨跌幅%
    signal: str = "观望"          # 兼容旧字段：操盘动作（汇总用）
    signal_color: str = "gray"    # 操盘动作颜色
    trend: str = ""
    # 结构化三列：时机 / 趋势过滤 / 操盘动作
    timing: str = "—"             # 九转时机
    timing_color: str = "gray"
    trend_filter: str = "—"       # 趋势过滤（系统层）
    trend_filter_color: str = "gray"
    action: str = "观望"          # 操盘动作（综合）
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
    analyst_target: object = None  # 参考价：个股=去极值分析师均价；基金=NAV float | None
    analyst_upside_pct: object = None  # 相对现价涨幅空间% | None
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
        try:
            _ld = df['date'].iloc[-1]
            if hasattr(_ld, 'strftime'):
                status.bar_date = _ld.strftime('%Y-%m-%d')
            else:
                status.bar_date = str(_ld)[:10]
        except Exception:
            status.bar_date = ""
        try:
            from data_fetcher import _df_last_date, _bar_is_stale
            mkt = "cn" if str(code).isdigit() else "us"
            status.bar_stale = bool(_bar_is_stale(_df_last_date(df), market=mkt))
            # 若仍陈旧：清缓存再拉一次，取更新结果
            if status.bar_stale:
                try:
                    from data_fetcher import invalidate_kline_cache
                    invalidate_kline_cache(code)
                    df2 = fetcher.fetch(code, days)
                    if df2 is not None and len(df2) >= 5:
                        d2 = _df_last_date(df2)
                        d1 = _df_last_date(df)
                        if d2 and (d1 is None or d2 >= d1):
                            df = df2
                            last_close = float(df['close'].iloc[-1])
                            status.price = round(last_close, 2)
                            status.bar_date = d2.strftime('%Y-%m-%d') if d2 else status.bar_date
                            status.bar_stale = bool(_bar_is_stale(d2, market=mkt))
                except Exception:
                    pass
        except Exception:
            status.bar_stale = False

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
            status.timing = "九转背离"
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

        # ---------- 2) 趋势过滤（均线趋势标签；≠ 系统层是否通过）----------
        trend = status.trend or "震荡"
        sys_layer = (result.layers_consistent or {}).get("系统层（趋势+指标）") or {}
        sys_ok = bool(sys_layer.get("通过"))
        if trend == "多头趋势":
            if sys_ok:
                status.trend_filter = "多头趋势"
                status.trend_filter_color = "green"
            else:
                # 均线偏多但加权指标未过 → 与详情页系统层红叉一致
                status.trend_filter = "多·指标未齐"
                status.trend_filter_color = "orange"
        elif trend == "空头趋势":
            if sys_ok:
                status.trend_filter = "空头趋势"
                status.trend_filter_color = "red"
            else:
                status.trend_filter = "空·指标未齐"
                status.trend_filter_color = "orange"
        else:
            status.trend_filter = "震荡整理"
            status.trend_filter_color = "gray"

        # ---------- 3) 操盘动作（时机 × 趋势 × 阶梯，冲突则降权）----------
        timing_buy = status.timing_color == "orange" and "九转" in status.timing
        timing_sell = status.timing_color == "red" and "九转" in status.timing
        reasons = []

        if nt.get('conflict'):
            status.action = "观望"
            status.action_color = "gray"
            reasons.append("日/月九转方向冲突")
        elif timing_buy and trend == "空头趋势":
            # 背离：不单独标「轻仓观察」，统一观望
            status.action = "观望"
            status.action_color = "gray"
            reasons.append("九转买点与空头趋势背离，观望")
        elif timing_sell and trend == "多头趋势":
            # 背离：不单独标「减仓观察」，统一观望
            status.action = "观望"
            status.action_color = "gray"
            reasons.append("九转卖点与多头趋势背离，观望")
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
        else:
            status.action = "观望"
            status.action_color = "gray"
            if nt_signal in ('买入', '加仓', '卖出', '持有'):
                reasons.append(f"无明确时机共振（策略信号仅供参考：{nt_signal}）")
            else:
                reasons.append("无共振时机")

        status.action_reason = "；".join(reasons)
        # 汇总仅三类，与列表行一一对应
        if status.action in ("关注买入", "阶梯抄底关注"):
            status.signal = "即将上涨关注"
            status.signal_color = "orange"
        elif status.action in ("关注卖出", "阶梯止盈关注"):
            status.signal = "上涨见顶关注"
            status.signal_color = "red"
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

        # 参考价（个股：去极值分析师均价；基金/ETF：NAV）+ 相对现价涨幅空间
        try:
            tgt = fetcher.fetch_analyst_mean_target(code)
            if tgt and last_close and last_close > 0:
                status.analyst_target = round(float(tgt), 2)
                status.analyst_upside_pct = round((float(tgt) / float(last_close) - 1.0) * 100.0, 2)
            else:
                status.analyst_target = None
                status.analyst_upside_pct = None
        except Exception:
            status.analyst_target = None
            status.analyst_upside_pct = None

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
        'bar_date': getattr(st, 'bar_date', '') or '',
        'bar_stale': bool(getattr(st, 'bar_stale', False)),
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
        'analyst_target': st.analyst_target,
        'analyst_upside_pct': st.analyst_upside_pct,
        'error': st.error,
    }


# 缓存：按 user_id 分桶（0 = 全局默认看板），后台刷新，接口永远秒回
_CACHES = {}          # key -> {'data':..., 'ts':..., 'refreshing':...}
_WATCHLIST_SOFT_TTL = 45    # 秒内纯内存命中，毫秒级返回
_WATCHLIST_TTL = 600
_STATUS_CACHE = {}
_STATUS_TTL = 180
_STATUS_CACHE_MAX = 64
_CACHES_MAX = 12            # 最多保留多少套看板缓存
_refresh_lock = threading.Lock()


def _bar_date_str(row) -> str:
    """YYYY-MM-DD；无法解析则空串。"""
    if not row or not isinstance(row, dict):
        return ""
    d = str(row.get("bar_date") or "").strip()
    return d[:10] if d else ""


def _row_fresher(a: dict, b: dict) -> dict:
    """两者都可用时取 bar_date 更新的；否则取可用的那份。"""
    ua = _row_usable(a) if a else False
    ub = _row_usable(b) if b else False
    if ua and not ub:
        return a
    if ub and not ua:
        return b
    if not ua and not ub:
        return a if a else b
    da, db = _bar_date_str(a), _bar_date_str(b)
    if da and db:
        if db > da:
            return b
        if da > db:
            return a
    return a if a else b


def _status_cache_put(k: str, data: dict) -> None:
    now = time.time()
    # 禁止用更旧 bar_date 覆盖状态缓存（刷新回退前一天的根因）
    try:
        hit = _STATUS_CACHE.get(k)
        if hit and isinstance(hit.get("data"), dict) and isinstance(data, dict):
            old_d = _bar_date_str(hit["data"])
            new_d = _bar_date_str(data)
            if old_d and new_d and old_d > new_d:
                return
            if old_d and not new_d:
                return
    except Exception:
        pass
    _STATUS_CACHE[k] = {"ts": now, "data": data}
    # 淘汰过期 + 超上限
    dead = [ck for ck, v in list(_STATUS_CACHE.items()) if (now - v.get("ts", 0)) >= _STATUS_TTL]
    for ck in dead:
        _STATUS_CACHE.pop(ck, None)
    if len(_STATUS_CACHE) > _STATUS_CACHE_MAX:
        ordered = sorted(_STATUS_CACHE.items(), key=lambda x: x[1].get("ts", 0))
        for ck, _ in ordered[: len(_STATUS_CACHE) - _STATUS_CACHE_MAX]:
            _STATUS_CACHE.pop(ck, None)


def _caches_put(key, entry: dict) -> None:
    _CACHES[key] = entry
    if len(_CACHES) <= _CACHES_MAX:
        return
    # 优先淘汰非 refreshing、最旧 ts
    items = []
    for k, v in list(_CACHES.items()):
        if k == key:
            continue
        if v.get("refreshing"):
            continue
        items.append((v.get("ts") or 0, k))
    items.sort()
    need = len(_CACHES) - _CACHES_MAX
    for _, k in items[:need]:
        _CACHES.pop(k, None)


def _status_dict_cached(code: str, name: str, days: int = 200) -> dict:
    k = str(code).strip().upper()
    hit = _STATUS_CACHE.get(k)
    now = time.time()
    cached_row = None
    if hit and (now - hit["ts"]) < _STATUS_TTL and isinstance(hit.get("data"), dict):
        cached_row = dict(hit["data"])
        if name:
            cached_row["name"] = name
        cached_row["pending"] = False
        # 缓存仍新鲜且 bar_date 较新时直接返回，避免无意义重算变旧
        if _bar_date_str(cached_row):
            # 仍走一次轻量比较：仅当调用方刚 bust 后 hit 不存在
            return cached_row
    st = get_stock_status(code, name, days=days)
    d = _status_to_dict(st)
    d["pending"] = False
    if name:
        d["name"] = name
    if cached_row:
        d = _row_fresher(d, cached_row)
    _status_cache_put(k, dict(d))
    return d


def _has_usable_stocks(data: dict) -> bool:
    for s in (data or {}).get("stocks") or []:
        if not s or s.get("error"):
            continue
        px = s.get("price")
        if px not in (None, "", "-", "…") and s.get("signal") not in (None, "", "计算中"):
            return True
    return False


def _align_stocks_to_items(stocks, items: list) -> list:
    """按当前自选列表对齐行：去掉已删、为新增代码补占位。"""
    if not items:
        return []
    by = {}
    for s in stocks or []:
        if s and s.get('code') is not None:
            by[str(s['code']).upper()] = dict(s)
    out = []
    for c, n in items:
        cu = str(c).upper()
        if cu in by:
            row = dict(by[cu])
            if n:
                row['name'] = n
            out.append(row)
        else:
            out.append({
                'code': c, 'name': n or c,
                'market': '美股' if not str(c).isdigit() else 'A股',
                'price': '-', 'bar_date': '', 'change_1d': None, 'change_5d': None,
                'signal': '计算中', 'signal_color': 'gray',
                'action': '计算中', 'action_color': 'gray',
                'timing': '—', 'timing_color': 'gray',
                'trend_filter': '—', 'trend_filter_color': 'gray',
                'nine_turn': '—', 'high_low': '—',
                'pending': True, 'error': None,
            })
    return out


def _prev_updated_at(cache_entry) -> str:
    if not cache_entry or not isinstance(cache_entry.get("data"), dict):
        return ""
    return (cache_entry["data"].get("updated_at")
            or cache_entry["data"].get("prev_updated_at")
            or "")


def _placeholder_row(code: str, name: str) -> dict:
    return {
        'code': code, 'name': name or code,
        'market': '美股' if not str(code).isdigit() else 'A股',
        'price': '-', 'bar_date': '', 'change_1d': None, 'change_5d': None,
        'signal': '计算中', 'signal_color': 'gray',
        'action': '计算中', 'action_color': 'gray',
        'timing': '—', 'timing_color': 'gray',
        'trend_filter': '—', 'trend_filter_color': 'gray',
        'nine_turn': '—', 'high_low': '—',
        'analyst_target': None, 'analyst_upside_pct': None,
        'pending': True, 'error': None,
    }


def _row_usable(s: dict) -> bool:
    if not s or s.get('error') or s.get('pending'):
        return False
    px = s.get('price')
    return px not in (None, '', '-', '…') and s.get('signal') not in (None, '', '计算中')


def _compute_watchlist(items: list = None, user_id: int = None, key=None,
                       bust_status_cache: bool = False) -> dict:
    """并行计算看板；刷新时以旧行为底图逐只替换，并控制内存。"""
    if items is None:
        items = resolve_watchlist_items(user_id)
    total = len(items)
    name_of = {str(c): (n or c) for c, n in items}

    def _sort_key(x):
        return (0 if (x.get('market') or '美股') == '美股' else 1, str(x.get('code') or ''))

    base = {}
    prev_ts = ""
    existing = _CACHES.get(key) if key is not None else None
    if existing:
        prev_ts = _prev_updated_at(existing)
        if isinstance(existing.get('data'), dict):
            for s in existing['data'].get('stocks') or []:
                if s and s.get('code'):
                    base[str(s['code']).upper()] = dict(s)

    if bust_status_cache:
        try:
            from data_fetcher import invalidate_kline_cache
        except Exception:
            invalidate_kline_cache = None
        for c, _ in items:
            cu = str(c).strip().upper()
            _STATUS_CACHE.pop(cu, None)
            if invalidate_kline_cache:
                try:
                    invalidate_kline_cache(c)
                    invalidate_kline_cache(cu)
                except Exception:
                    pass

    def _assemble(done_map: dict) -> list:
        rows = []
        for c, n in items:
            cu = str(c).upper()
            new_r = dict(done_map[cu]) if cu in done_map else None
            old_r = dict(base[cu]) if cu in base else None
            if new_r and old_r:
                r = _row_fresher(new_r, old_r)
                r['name'] = n or r.get('name') or c
                r['pending'] = False
                rows.append(r)
            elif new_r:
                r = new_r
                r['name'] = n or r.get('name') or c
                r['pending'] = False
                rows.append(r)
            elif old_r and _row_usable(old_r):
                r = old_r
                r['name'] = n or r.get('name') or c
                rows.append(r)
            else:
                rows.append(_placeholder_row(c, n or c))
        rows.sort(key=_sort_key)
        return rows

    if key is not None:
        display0 = _assemble({})
        _caches_put(key, {
            'data': {
                'success': True, 'computing': True,
                'updated_at': prev_ts, 'prev_updated_at': prev_ts,
                'count': sum(1 for s in display0 if _row_usable(s)),
                'total': total,
                'summary': (existing.get('data') or {}).get('summary', {}) if existing else {},
                'stocks': display0,
                'symbols': [{'code': c, 'name': n} for c, n in items],
            },
            'ts': (existing.get('ts') if existing else time.time()),
            'refreshing': True,
        })

    done_map = {}
    # 并行计算；每完成一批（默认 5 个）才写一次进度缓存，供前端轮询渐进重绘
    _batch = 5
    _done_n = 0
    with ThreadPoolExecutor(max_workers=min(5, max(2, len(items)))) as ex:
        futs = {ex.submit(_status_dict_cached, code, name, 160): code for code, name in items}
        for fut in as_completed(futs):
            code = futs[fut]
            try:
                row = fut.result()
            except Exception:
                row = {
                    'code': code, 'name': name_of.get(str(code), code),
                    'market': '美股' if not str(code).isdigit() else 'A股',
                    'price': '-', 'signal': '观望', 'error': '获取失败', 'pending': False,
                }
            done_map[str(code).upper()] = row
            _done_n += 1
            # 每 5 个或全部完成时更新一次，减少缓存写抖动；轮询可见分批结果
            if key is not None and (_done_n % _batch == 0 or _done_n >= total):
                display = _assemble(done_map)
                entry = _CACHES.get(key) or {}
                entry['data'] = {
                    'success': True, 'computing': True,
                    'updated_at': prev_ts, 'prev_updated_at': prev_ts,
                    'count': sum(1 for s in display if _row_usable(s)),
                    'total': total, 'summary': {},
                    'stocks': display,
                    'symbols': [{'code': c, 'name': n} for c, n in items],
                    'progress': {'done': _done_n, 'total': total},
                }
                entry['refreshing'] = True
                _CACHES[key] = entry

    partial = _assemble(done_map)
    summary = {'即将上涨关注': 0, '上涨见顶关注': 0, '下跌观望': 0, 'error': 0}
    for s in partial:
        if s.get('error'):
            summary['error'] += 1
            # 错误行也计入观望侧，保证三类之和 ≈ 列表总数
            summary['下跌观望'] += 1
            continue
        if s.get('pending') or s.get('signal') == '计算中':
            summary['下跌观望'] += 1
            continue
        sig = (s.get('signal') or '').strip()
        act = (s.get('action') or '').strip()
        if sig == '即将上涨关注' or act in ('关注买入', '阶梯抄底关注', '轻仓试探', '轻仓加仓', '关注加仓'):
            summary['即将上涨关注'] += 1
        elif sig == '上涨见顶关注' or act in ('关注卖出', '阶梯止盈关注', '减仓观察'):
            summary['上涨见顶关注'] += 1
        else:
            # 观望 / 九转背离·观望 / 轻仓观察 / 持有 等一律进观望桶
            summary['下跌观望'] += 1

    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    final = {
        'success': True, 'computing': False,
        'updated_at': now_str,
        'count': len(partial), 'total': total,
        'summary': summary, 'stocks': partial,
        'symbols': [{'code': c, 'name': n} for c, n in items],
        'progress': {'done': total, 'total': total},
        'cache_flushed': True,
    }
    # 刷新完成：强制把最新行写回单标的状态缓存 + 看板缓存，避免下次仍命中旧 STATUS/看板
    try:
        for s in partial:
            if not s or not s.get('code'):
                continue
            if s.get('pending') or s.get('signal') == '计算中':
                continue
            cu = str(s['code']).strip().upper()
            row = dict(s)
            row['pending'] = False
            _status_cache_put(cu, row)
    except Exception:
        pass
    if key is not None:
        _caches_put(key, {'data': final, 'ts': time.time(), 'refreshing': False})
        # 再写一次，防止 put 过程中被并发覆盖回 computing 中态
        try:
            _CACHES[key] = {'data': dict(final), 'ts': time.time(), 'refreshing': False}
        except Exception:
            pass
    try:
        import gc
        gc.collect()
    except Exception:
        pass
    return final


def _background_refresh(key, items, user_id, bust_status_cache: bool = False):
    """后台刷新某个看板缓存（边算边增量写入 _CACHES[key]，带兜底熔断）"""
    try:
        _compute_watchlist(items=items, user_id=user_id, key=key,
                           bust_status_cache=bust_status_cache)
    except Exception:
        pass
    finally:
        # 无论成功失败：必须结束 refreshing / computing，避免「最后一只」一直刷新中
        entry = _CACHES.get(key)
        if not entry:
            _CACHES[key] = {
                'data': {
                    'success': True, 'computing': False,
                    'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    'count': 0, 'total': len(items) if items else 0,
                    'summary': {}, 'stocks': [],
                    'progress': {'done': len(items) if items else 0, 'total': len(items) if items else 0},
                },
                'ts': time.time(), 'refreshing': False,
            }
        else:
            entry['refreshing'] = False
            data = entry.get('data')
            if not isinstance(data, dict):
                data = {}
                entry['data'] = data
            data['computing'] = False
            data.setdefault('total', len(items) if items else 0)
            # 若计算已写入新 updated_at 则保留；否则补时间戳
            if not data.get('updated_at') or data.get('updated_at') == data.get('prev_updated_at'):
                # 有可用 stocks 时用当前时间，保证「刷新完成」可见
                stocks = data.get('stocks') or []
                usable = any(
                    s and not s.get('pending') and s.get('price') not in (None, '', '-', '…')
                    for s in stocks
                )
                if usable:
                    data['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                elif not data.get('updated_at'):
                    data['updated_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            tot = int(data.get('total') or (len(items) if items else 0) or 0)
            data['progress'] = {'done': tot, 'total': tot}
            entry['ts'] = time.time()
            # 强制固化看板缓存引用
            _CACHES[key] = entry


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

    # 软 TTL：完整可用数据 → 毫秒级返回；但必须与当前自选 codes 对齐
    if (not force and cache and isinstance(cache.get('data'), dict)
            and (now - cache.get('ts', 0)) < _WATCHLIST_SOFT_TTL
            and _has_usable_stocks(cache['data'])):
        out = dict(cache['data'])
        aligned = _align_stocks_to_items(out.get('stocks') or [], items)
        # 自选有增删时不能当纯命中，需后台补齐新代码
        cached_codes = {str(s.get('code')).upper() for s in (out.get('stocks') or []) if s}
        want_codes = {str(c).upper() for c, _ in items}
        if cached_codes == want_codes:
            out['stocks'] = aligned
            out['count'] = len(aligned)
            out['total'] = len(items)
            out['symbols'] = [{'code': c, 'name': n} for c, n in items]
            out['computing'] = False
            out['cache_hit'] = True
            out['stale'] = False
            return out
        # codes 不一致：走 SWR 刷新

    # SWR：有上次结果 → 立刻返回对齐后的列表（新增代码先占位），后台刷新
    if cache and isinstance(cache.get('data'), dict) and _has_usable_stocks(cache['data']):
        out = dict(cache['data'])
        out['stocks'] = _align_stocks_to_items(out.get('stocks') or [], items)
        out['count'] = len(out['stocks'])
        out['total'] = len(items)
        out['symbols'] = [{'code': c, 'name': n} for c, n in items]
        out['cache_hit'] = True
        out['stale'] = (now - cache.get('ts', 0)) >= _WATCHLIST_SOFT_TTL
        prev_ts = out.get('updated_at') or out.get('prev_updated_at') or ''
        out['prev_updated_at'] = prev_ts
        out['updated_at'] = prev_ts  # 刷新完成前不改展示时间
        need_refresh = force or out['stale'] or (
            {str(s.get('code')).upper() for s in out['stocks'] if s and s.get('pending')}
        )
        if need_refresh:
            with _refresh_lock:
                c2 = _CACHES.get(key)
                # force：即使标记在刷新中也允许重开（避免卡死在「最后1只」）
                stuck = bool(c2 and c2.get('refreshing') and force)
                if c2 and (not c2.get('refreshing') or stuck):
                    c2['refreshing'] = True
                    if isinstance(c2.get('data'), dict):
                        c2['data']['computing'] = True
                        c2['data']['prev_updated_at'] = prev_ts
                        c2['data']['updated_at'] = prev_ts
                        c2['data']['progress'] = {
                            'done': 0,
                            'total': len(items),
                        }
                    threading.Thread(
                        target=_background_refresh, args=(key, items, user_id, bool(force)), daemon=True
                    ).start()
            out['computing'] = True
        else:
            out['computing'] = bool(cache.get('refreshing'))
        return out

    # 无可用缓存：骨架 + 后台计算
    with _refresh_lock:
        cache = _CACHES.get(key)
        if not (cache and cache.get('refreshing')):
            item_codes = [c for c, _ in items]
            item_name = {c: n for c, n in items}
            prev = []
            prev_ts = _prev_updated_at(cache) if cache else ''
            if cache and isinstance(cache.get('data'), dict):
                prev = list(cache['data'].get('stocks') or [])
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
                'data': {'success': True, 'computing': True,
                         'updated_at': prev_ts, 'prev_updated_at': prev_ts,
                         'count': len(seeded), 'total': len(items),
                         'summary': {}, 'stocks': seeded,
                         'symbols': [{'code': c, 'name': n} for c, n in items]},
                'ts': time.time(), 'refreshing': True,
            }
            threading.Thread(
                target=_background_refresh, args=(key, items, user_id, force), daemon=True
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


def _warm_default_watchlist():
    try:
        time.sleep(1.5)
        get_watchlist_status(user_id=None, force=True, is_admin=False)
    except Exception:
        pass


threading.Thread(target=_warm_default_watchlist, daemon=True, name="warm-watchlist").start()
