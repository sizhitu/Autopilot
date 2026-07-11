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

fetcher = DataFetcher()

# 关注列表
WATCHLIST = {
    # 美股
    'OUST': 'Ouster', 'FLY': 'Firefly Aerospace', 'SPCX': 'SpaceX',
    'FIGR': 'Figure Technology', 'MU': '美光科技', 'CF': 'Fundrise Innovation Fund',
    'ETN': '伊顿', 'GEV': 'GE Vernova', 'HIMS': 'Hims & Hers', 'APP': 'AppLovin',
    'ICE': '洲际交易所', 'SMH': '半导体指数ETF', 'VGT': '领航信息技术',
    'JEPI': '摩根JEPi', 'GOOG': '谷歌C', 'LITE': 'Lumentum',
    'ASTS': 'AST SpaceMobile', 'FCX': 'Freeport-McMoRan', 'ASM': 'ASM International',
    'EUV': 'EUV', 'WTI': '原油WTI', 'AVGO': '博通', 'NVDA': '英伟达', 'INTC': '英特尔',
    # A股
    '600887': '伊利股份', '600111': '北方稀土', '601899': '紫金矿业',
    '159880': '有色ETF鹏华', '518850': '黄金ETF华夏', '560710': '船舶ETF富国', '159985': '豆粕ETF',
    '516150': '纳指ETF嘉实', '562500': '机器人ETF华夏', '513310': '中韩半导体ETF华泰柏瑞',
    # 指数
    '000001': '上证指数', '399300': '沪深300',
}

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
    'ICE': '压舱石', 'ETN': '压舱石', 'CF': '压舱石', 'SMH': '压舱石', 'VGT': '压舱石',
    'JEPI': '压舱石', '516150': '压舱石', '518850': '压舱石',
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
    signal: str = "观望"          # 操盘建议
    signal_color: str = "gray"    # 信号颜色
    trend: str = ""
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

        # 当日涨跌幅（昨收口径：今收/昨收 - 1，即最近一根K线收盘价相对前一根）
        if len(df) >= 2:
            prev_close = float(df['close'].iloc[-2])
            status.change_1d = round((last_close - prev_close) / prev_close * 100, 2)

        # 近5日涨跌幅（同样用全精度）
        lookback_5 = min(5, len(df) - 1)
        if lookback_5 > 0:
            prev_5 = float(df['close'].iloc[-1 - lookback_5])
            status.change_5d = round((last_close - prev_5) / prev_5 * 100, 2)

        # 操盘建议（九转为主、策略信号为辅的分类建议）
        #   下跌九转临近/完成（1-9 买点）→ 即将上涨关注（橙）
        #   上涨九转临近/完成（1-9 卖点）→ 上涨见顶关注（红）
        #   其余（仍在调整/震荡）          → 下跌观望（灰）
        strategy = FujimotoStrategy(total_capital=100000)
        result = strategy.analyze(df)
        status.trend = result.trend.value

        nt_signal = result.signal.value  # 策略原始信号：买入/卖出/持有/加仓/观望

        # 九转状态（日级+月级，月级形成则展示月级；供下方分类使用）
        nt = calc_nine_turn_display(df)

        # 藤本茂阶梯抄底：近5日跌幅达买入档位(-15% 第一档)视为买点关注
        buy_ladder_hit = status.change_5d <= -15.0
        # 藤本茂阶梯止盈：近5日涨幅达卖出档位(+25% 第一档)视为见顶关注
        sell_ladder_hit = status.change_5d >= 25.0

        # 操盘建议分类（九转时机 + 藤本茂阶梯）
        #   下跌九转临近/完成 或 阶梯买点(暴跌) → 即将上涨关注（橙）
        #   上涨九转临近/完成 或 阶梯卖点(暴涨) → 上涨见顶关注（红）
        #   其余（仍在调整/震荡）                → 下跌观望（灰）
        if (nt['is_completing'] or nt['is_complete']) and nt['direction'] == 'down':
            status.signal = "即将上涨关注"
            status.signal_color = "orange"
        elif (nt['is_completing'] or nt['is_complete']) and nt['direction'] == 'up':
            status.signal = "上涨见顶关注"
            status.signal_color = "red"
        elif buy_ladder_hit:
            status.signal = "即将上涨关注"
            status.signal_color = "orange"
        elif sell_ladder_hit:
            status.signal = "上涨见顶关注"
            status.signal_color = "red"
        elif nt_signal in ('买入', '加仓'):
            status.signal = "即将上涨关注"
            status.signal_color = "red" if nt_signal == '买入' else "orange"
        elif nt_signal == '卖出':
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


# 缓存：避免每次刷新都重新拉取全部股票；后台刷新，接口永远秒回
_watchlist_cache = None
_watchlist_cache_ts = 0.0
_WATCHLIST_TTL = 300
_refreshing = False
_refresh_lock = threading.Lock()


def _compute_watchlist() -> dict:
    """真正计算全部关注股票状态（较重，应在后台线程执行）"""
    results = []
    # 串行抓取：对 Yahoo 更友好，避免并发触发限流
    for i, (code, name) in enumerate(WATCHLIST.items()):
        if i > 0:
            time.sleep(0.3)  # 请求间隔，降低被限流概率
        st = get_stock_status(code, name)
        results.append(_status_to_dict(st))

    # 排序：美股在前，A股在后
    results.sort(key=lambda x: (0 if x['market'] == '美股' else 1, x['code']))

    # 分类汇总（操盘建议）
    summary = {'即将上涨关注': 0, '上涨见顶关注': 0, '下跌观望': 0, 'error': 0}
    for s in results:
        if s.get('error'):
            summary['error'] += 1
        else:
            summary[s['signal']] = summary.get(s['signal'], 0) + 1

    return {
        'success': True,
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'count': len(results),
        'summary': summary,
        'stocks': results,
    }


def _background_refresh():
    """后台刷新缓存（带熔断/兜底，已在 data_fetcher 内系统性处理限流）"""
    global _watchlist_cache, _watchlist_cache_ts, _refreshing
    try:
        data = _compute_watchlist()
        _watchlist_cache = data
        _watchlist_cache_ts = time.time()
    finally:
        _refreshing = False


def get_watchlist_status() -> dict:
    """
    获取所有关注股票状态。
    设计目标：接口永远秒回——
      - 缓存命中（TTL 内）→ 直接返回；
      - 缓存缺失/过期 → 触发一次后台刷新，立即返回旧缓存（或“计算中”占位），
        不阻塞当前请求；后续请求自然拿到新数据。
    """
    global _refreshing
    now = time.time()
    if _watchlist_cache is not None and (now - _watchlist_cache_ts) < _WATCHLIST_TTL:
        _watchlist_cache["updated_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return _watchlist_cache

    # 需要刷新：确保仅一个后台刷新在跑（避免并发重复计算）
    with _refresh_lock:
        if not _refreshing:
            _refreshing = True
            threading.Thread(target=_background_refresh, daemon=True).start()

    if _watchlist_cache is not None:
        # 返回稍旧的缓存，前端下次轮询即拿到新数据
        _watchlist_cache["updated_at"] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return _watchlist_cache

    # 首次加载、尚无任何缓存：返回“计算中”占位，前端轮询重试
    return {
        'success': True,
        'computing': True,
        'updated_at': '',
        'count': 0,
        'summary': {},
        'stocks': [],
    }


if __name__ == "__main__":
    data = get_watchlist_status()
    print(f"关注股票: {data['count']}只  更新时间: {data['updated_at']}")
    print(f"{'代码':8s} {'名称':12s} {'现价':>10s} {'5日%':>7s} {'信号':6s} {'九转':20s} {'高低':12s}")
    print("-" * 90)
    for s in data['stocks']:
        if s['error']:
            print(f"{s['code']:8s} {s['name']:12s} ERROR: {s['error']}")
        else:
            print(f"{s['code']:8s} {s['name']:12s} {s['price']:>10.2f} {s['change_5d']:>6.1f}% {s['signal']:6s} {s['nine_turn']:20s} {s['high_low']:12s}")
