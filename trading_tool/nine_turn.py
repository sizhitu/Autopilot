"""
神奇九转计数模块
=================================
实现类似腾讯自选股的"神奇九转"趋势计数。

规则（基于 TD Sequential 简化版）：
  - 下跌九转：连续下跌中，若当日收盘价 < 前第4根K线收盘价，则计数+1
  - 上涨九转：连续上涨中，若当日收盘价 > 前第4根K线收盘价，则计数+1
  - 计数范围 1-9，到9表示九转完成
  - 方向反转时计数重置

状态分类：
  - 第7,8天：即将完成（密切关注）
  - 第9天：九转完成（趋势可能反转）
"""

from dataclasses import dataclass
from typing import List, Optional
import pandas as pd
import numpy as np


@dataclass
class NineTurnResult:
    """九转计数结果"""
    direction: str = "none"     # "down" | "up" | "none"
    count: int = 0              # 当前计数 1-9
    status: str = "无"          # 状态描述
    is_completing: bool = False  # 即将完成(7-8)
    is_complete: bool = False    # 已完成(=9)
    days_to_complete: int = 0    # 距离完成还差几天


def calc_nine_turn(df: pd.DataFrame, unit: str = "天") -> NineTurnResult:
    """
    计算神奇九转计数

    规则（TD Sequential 简化版，与腾讯/同花顺"神奇九转"一致）：
      - 下跌九转：连续出现 K 线，其收盘价 < 前第 4 根 K 线收盘价，则计数+1
      - 上涨九转：连续出现 K 线，其收盘价 > 前第 4 根 K 线收盘价，则计数+1
      - 计数范围 1-9，到 9 表示九转完成；方向反转或平盘时计数重置
      - unit: 计数单位文案（"天"=日级 / "月"=月级），仅影响展示文本

    Args:
        df: 含 close 列的 DataFrame（按时间升序）
        unit: 展示单位（日级为"天"，月级为"月"）
    Returns:
        NineTurnResult
    """
    closes = df['close'].values
    n = len(closes)
    if n < 5:
        return NineTurnResult()

    # 逐根计算九转计数（与之前第4根比较）
    counts = [0] * n
    dirs = ["none"] * n
    current_dir = "none"
    current_count = 0

    for i in range(4, n):
        prev4_close = closes[i - 4]
        cur_close = closes[i]

        if cur_close < prev4_close:
            # 下跌计数
            if current_dir == "down":
                current_count += 1
            else:
                current_dir = "down"
                current_count = 1
        elif cur_close > prev4_close:
            # 上涨计数
            if current_dir == "up":
                current_count += 1
            else:
                current_dir = "up"
                current_count = 1
        else:
            # 平盘，重置
            current_dir = "none"
            current_count = 0

        # 限制最大9（超过9后保持9直到方向改变）
        if current_count > 9:
            current_count = 9

        counts[i] = current_count
        dirs[i] = current_dir

    # 取最近的有效状态：允许回溯最近 2 根，避免"刚完成第9即消失"造成误读
    best_i = n - 1
    for i in range(n - 2, max(n - 3, -1), -1):
        if counts[i] > counts[best_i]:
            best_i = i
    final_count = counts[best_i]
    final_dir = dirs[best_i]

    result = NineTurnResult()

    if final_dir == "down" and final_count > 0:
        result.direction = "down"
        result.count = final_count
        result.status = f"下跌九转第{final_count}{unit}"
        if final_count >= 7 and final_count < 9:
            result.is_completing = True
            result.status = f"下跌九转第{final_count}{unit}(即将完成)"
            result.days_to_complete = 9 - final_count
        elif final_count == 9:
            result.is_complete = True
            result.status = f"下跌九转完成(买点)"
            result.days_to_complete = 0

    elif final_dir == "up" and final_count > 0:
        result.direction = "up"
        result.count = final_count
        result.status = f"上涨九转第{final_count}{unit}"
        if final_count >= 7 and final_count < 9:
            result.is_completing = True
            result.status = f"上涨九转第{final_count}{unit}(即将完成)"
            result.days_to_complete = 9 - final_count
        elif final_count == 9:
            result.is_complete = True
            result.status = f"上涨九转完成(卖点)"
            result.days_to_complete = 0

    else:
        result.direction = "none"
        result.count = 0
        result.status = "无九转信号"

    return result


def to_monthly(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """
    将日线重采样为月线（取每月最后一根收盘），用于月级神奇九转。
    返回含 'close' 列的 DataFrame；数据不足或无 date 列时返回 None。
    """
    if df is None or len(df) < 5 or 'date' not in df.columns:
        return None
    try:
        s = pd.Series(df['close'].values,
                      index=pd.to_datetime(df['date'].values))
        # pandas 3.x 使用 'ME'（Month End）；旧版用 'M'
        try:
            s = s.resample('ME').last()
        except (ValueError, KeyError):
            s = s.resample('M').last()
        s = s.dropna()
        if len(s) == 0:
            return None
        return pd.DataFrame({'close': s.values})
    except Exception:
        return None


def calc_nine_turn_monthly(df: pd.DataFrame) -> NineTurnResult:
    """日线 -> 月线 -> 神奇九转（单位"月"）"""
    monthly = to_monthly(df)
    if monthly is None or len(monthly) < 5:
        return NineTurnResult()
    return calc_nine_turn(monthly, unit="月")


def _nt_state(r: "NineTurnResult") -> str:
    """九转状态分类：complete / completing / none（用于前端配色）"""
    if r.is_complete:
        return "complete"
    if r.is_completing:
        return "completing"
    return "none"


def _compact(r: "NineTurnResult", level: str) -> str:
    """
    紧凑展示（带方向符号、缩短计数）：
      日▼2  /  日▼9买(下跌完成=买点)  /  日▲9卖(上涨完成=卖点)  /  月–(无信号)
    """
    if r.direction == "none" or r.count == 0:
        return f"{level}–"
    arrow = "▼" if r.direction == "down" else "▲"
    if r.is_complete:
        mark = "买" if r.direction == "down" else "卖"
        return f"{level}{arrow}9{mark}"
    return f"{level}{arrow}{r.count}"


def calc_nine_turn_display(df: pd.DataFrame) -> dict:
    """
    同时返回日级与月级神奇九转，供同一列展示。
      - daily_text / monthly_text：带"日·"/"月·"前缀的展示文本（月级无信号时为"月·—"）
      - daily_state / monthly_state：none / completing / complete（用于配色）
      - 主级别（level / direction / is_complete / is_completing）取月级（若已形成）否则日级，
        仅用于操盘建议分类；展示时日级与月级并列呈现。
    """
    daily = calc_nine_turn(df, unit="天")
    monthly = calc_nine_turn_monthly(df)

    daily_text = _compact(daily, "日")
    monthly_text = _compact(monthly, "月")

    # 主级别：月级形成(计数≥7)则取月级，否则日级（仅用于分类）
    monthly_formed = monthly.direction != "none" and monthly.count >= 7
    primary = monthly if monthly_formed else daily
    primary_level = "月" if monthly_formed else "日"

    return {
        "daily_text": daily_text,
        "monthly_text": monthly_text,
        "daily_state": _nt_state(daily),
        "monthly_state": _nt_state(monthly),
        "text": f"{daily_text}　|　{monthly_text}",
        "level": primary_level,
        "direction": primary.direction,
        "count": primary.count,
        "status": primary.status,
        "is_complete": primary.is_complete,
        "is_completing": primary.is_completing,
    }


# ================================================================
#  测试
# ================================================================
if __name__ == "__main__":
    # 构造测试数据
    # 下跌趋势
    dates = pd.date_range('2026-01-01', periods=30, freq='D')
    # 模拟连续下跌（满足九转条件：每日收盘 < 前4日收盘）
    prices_down = np.array([100 - i * 0.5 - (i // 4) * 0.3 for i in range(30)])
    df_down = pd.DataFrame({'date': dates, 'close': prices_down})
    r1 = calc_nine_turn(df_down)
    print(f"下跌趋势: dir={r1.direction} count={r1.count} status={r1.status} complete={r1.is_complete} completing={r1.is_completing}")

    # 上涨趋势
    prices_up = np.array([100 + i * 0.5 + (i // 4) * 0.3 for i in range(30)])
    df_up = pd.DataFrame({'date': dates, 'close': prices_up})
    r2 = calc_nine_turn(df_up)
    print(f"上涨趋势: dir={r2.direction} count={r2.count} status={r2.status} complete={r2.is_complete} completing={r2.is_completing}")

    # 震荡
    np.random.seed(42)
    prices_noise = 100 + np.random.randn(30) * 2
    df_noise = pd.DataFrame({'date': dates, 'close': prices_noise})
    r3 = calc_nine_turn(df_noise)
    print(f"震荡: dir={r3.direction} count={r3.count} status={r3.status}")
