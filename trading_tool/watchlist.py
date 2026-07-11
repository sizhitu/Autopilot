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


def get_stock_status(code: str, name: str, days: int = 300) -> StockStatus:
    """获取单只股票完整状态"""
    market = '美股' if not code.isdigit() else 'A股'
    status = StockStatus(code=code, name=name, market=market)

    try:
        df = fetcher.fetch(code, days)
        if len(df) < 10:
            status.error = f"数据不足({len(df)}根)"
            return status

        status.price = round(float(df['close'].iloc[-1]), 2)

        # 当日涨跌幅（最近一根K线收盘价相对前一根）
        if len(df) >= 2:
            prev_close = float(df['close'].iloc[-2])
            status.change_1d = round((status.price - prev_close) / prev_close * 100, 2)

        # 近5日涨跌幅
        lookback_5 = min(5, len(df) - 1)
        if lookback_5 > 0:
            prev_5 = df['close'].iloc[-1 - lookback_5]
            status.change_5d = round((status.price - prev_5) / prev_5 * 100, 2)

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
