"""
神奇九转计数模块
=================================
实现类似腾讯自选股的"神奇九转"趋势计数（TD Sequential 简化版）。

规则：
  - 下跌九转（买点序列）：连续出现 K 线，其收盘价 <= 前第 4 根 K 线收盘价，则计数+1
  - 上涨九转（卖点序列）：连续出现 K 线，其收盘价 >= 前第 4 根 K 线收盘价，则计数+1
  - 计数范围 1-9，到 9 表示九转完成；方向反转或平盘时计数重置
  - 平盘（相等）视为延续当前方向（TD 中等号同时满足买/卖两侧，故不重置）

状态分类：
  - 第7,8天：即将完成（密切关注）
  - 第9天：九转完成（趋势可能反转）

趋势门控（重要）：
  单纯「收盘价 vs 4 根前」在震荡下行中遇到反弹日也会被记为上涨九转，
  导致"下跌股误报上涨九转"。因此最终只展示与中短期趋势方向一致的九转序列，
  下跌趋势只看下跌九转、上涨趋势只看上涨九转（见 calc_nine_turn / _trend_direction）。
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


def _trend_direction(closes: "np.ndarray") -> str:
    """
    中短期趋势判定（用于九转「趋势门控」，避免下跌股误报上涨九转）。

    综合三个弱信号：
      - 现价 vs MA20（站上/跌破）
      - MA20 vs MA60（均线多空排列；不足 60 根时退化为全样本均值）
      - 近期斜率（现价 vs 约 5 根前）
    只有多数信号同向才给出 up/down，否则 flat。
    """
    n = len(closes)
    if n < 10:
        return "flat"
    price = float(closes[-1])
    ma20 = float(np.mean(closes[-20:])) if n >= 20 else float(np.mean(closes))
    ma60 = float(np.mean(closes[-60:])) if n >= 60 else float(np.mean(closes[-min(n, 20):]))
    recent = float(closes[-1]) > float(closes[-min(n, 5)])

    above_ma = price > ma20
    ma_up = ma20 >= ma60

    if above_ma and ma_up:
        return "up"
    if (not above_ma) and (not ma_up):
        return "down"
    # 混合：以「现价相对均线」+「近期斜率」再判一次
    if above_ma and recent:
        return "up"
    if (not above_ma) and (not recent):
        return "down"
    return "flat"


def _select_gated(counts, dirs, n, trend) -> tuple:
    """
    趋势门控下的最终状态选择。

    只采纳与趋势方向一致的九转序列（下跌趋势只看下跌九转、上涨趋势只看上涨九转），
    在最近 L 根内取计数最高者（优先接近完成的信号）。趋势为 flat 时不做门控。
    没有任何一致序列则返回 (0, 'none')。
    """
    L = min(n, 15)
    best = None  # (count, dir)
    for i in range(n - 1, n - 1 - L, -1):
        c = counts[i]
        d = dirs[i]
        if c <= 0:
            continue
        if trend == "down" and d != "down":
            continue
        if trend == "up" and d != "up":
            continue
        if best is None or c > best[0]:
            best = (c, d)
    if best is not None:
        return best
    return (0, "none")


def calc_nine_turn(df: pd.DataFrame, unit: str = "天") -> NineTurnResult:
    """
    计算神奇九转计数（TD Sequential 简化版 + 趋势门控）。

    计数规则（与腾讯/同花顺"神奇九转"一致）：
      - 下跌九转（买点序列）：连续出现 K 线，其收盘价 <= 前第 4 根收盘价，则计数+1
      - 上涨九转（卖点序列）：连续出现 K 线，其收盘价 >= 前第 4 根收盘价，则计数+1
      - 计数范围 1-9，到 9 表示九转完成；方向反转或平盘时计数重置
      - 平盘（相等）视为延续当前方向（TD 中等号同时满足买/卖两侧，故不重置）

    关键修正（修复"下跌股误报上涨九转"）：
      单纯「收盘价 vs 4 根前」在震荡下行中遇到反弹日也会被记为上涨九转。
      这里引入 *趋势门控*：最终只展示与中短期趋势同向的九转序列，
      下跌趋势只报"下跌九转"、上涨趋势只报"上涨九转"，趋势中性才放宽。
      这样 FLY / MU 这类近期下行的标的不会错误地显示"上涨/月上涨"。

    Args:
        df: 含 close 列的 DataFrame（按时间升序）
        unit: 展示单位（日级为"天"，月级为"月"）
    Returns:
        NineTurnResult
    """
    closes = df['close'].values.astype(float)
    n = len(closes)
    if n < 5:
        return NineTurnResult()

    # 逐根计算九转计数（与之前第4根比较）
    counts = [0] * n
    dirs = ["none"] * n
    current_dir = "none"
    current_count = 0

    for i in range(4, n):
        prev4 = closes[i - 4]
        cur = closes[i]

        if cur < prev4:
            ndir = "down"
        elif cur > prev4:
            ndir = "up"
        else:
            ndir = current_dir  # 平盘：延续当前方向（计入当前九转，不重置）

        if ndir == "none":
            current_dir = "none"
            current_count = 0
        elif ndir == current_dir:
            current_count += 1
        else:
            current_dir = ndir
            current_count = 1

        # 限制最大9（超过9后保持9直到方向改变）
        if current_count > 9:
            current_count = 9

        counts[i] = current_count
        dirs[i] = current_dir

    # 趋势门控：只展示与中短期趋势方向一致的九转
    trend = _trend_direction(closes)
    final_count, final_dir = _select_gated(counts, dirs, n, trend)

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

    # —— 日/月九转冲突检测与统一操作建议 ——
    # 仅当两边都是「有效信号」（计数≥4）且方向相反时才视为冲突，
    # 避免把「日线刚起步 vs 月线微弱」这类不对称误判为矛盾。
    daily_sig = daily.direction if daily.count >= 4 else "none"
    monthly_sig = monthly.direction if monthly.count >= 4 else "none"
    conflict = (daily_sig != "none" and monthly_sig != "none" and daily_sig != monthly_sig)

    if conflict:
        suggestion = "观望"
        suggestion_detail = "九转矛盾（日/月方向相反）"
    elif daily_sig == "down" or monthly_sig == "down":
        suggestion = "买"
        suggestion_detail = "下跌九转买点"
    elif daily_sig == "up" or monthly_sig == "up":
        suggestion = "卖"
        suggestion_detail = "上涨九转卖点"
    else:
        suggestion = "无"
        suggestion_detail = "无九转信号"

    text = f"{daily_text}　|　{monthly_text}"
    if conflict:
        text += "（矛盾·观望）"

    return {
        "daily_text": daily_text,
        "monthly_text": monthly_text,
        "daily_state": _nt_state(daily),
        "monthly_state": _nt_state(monthly),
        "daily_direction": daily.direction,
        "daily_count": daily.count,
        "daily_is_complete": daily.is_complete,
        "daily_is_completing": daily.is_completing,
        "monthly_direction": monthly.direction,
        "monthly_count": monthly.count,
        "monthly_is_complete": monthly.is_complete,
        "monthly_is_completing": monthly.is_completing,
        "text": text,
        "level": primary_level,
        "direction": primary.direction,
        "count": primary.count,
        "status": primary.status,
        "is_complete": primary.is_complete,
        "is_completing": primary.is_completing,
        "conflict": conflict,
        "suggestion": suggestion,
        "suggestion_detail": suggestion_detail,
    }


# ================================================================
#  测试
# ================================================================
if __name__ == "__main__":
    # 构造测试数据
    # 下跌趋势
    dates = pd.date_range('2026-01-01', periods=30, freq='D')
    prices_down = np.array([100 - i * 0.5 - (i // 4) * 0.3 for i in range(30)])
    df_down = pd.DataFrame({'date': dates, 'close': prices_down})
    r1 = calc_nine_turn(df_down)
    print(f"下跌趋势: dir={r1.direction} count={r1.count} status={r1.status}")

    # 上涨趋势
    prices_up = np.array([100 + i * 0.5 + (i // 4) * 0.3 for i in range(30)])
    df_up = pd.DataFrame({'date': dates, 'close': prices_up})
    r2 = calc_nine_turn(df_up)
    print(f"上涨趋势: dir={r2.direction} count={r2.count} status={r2.status}")

    # 震荡
    np.random.seed(42)
    prices_noise = 100 + np.random.randn(30) * 2
    df_noise = pd.DataFrame({'date': dates, 'close': prices_noise})
    r3 = calc_nine_turn(df_noise)
    print(f"震荡: dir={r3.direction} count={r3.count} status={r3.status}")

    # 关键回归：下行中夹带反弹（复现 FLY/MU 误报场景）
    # 整体从 25 跌到 19，但末段有 4 根反弹小阳线（每根略高于 4 根前）→ 旧算法会误报"上涨"
    prices_fl = np.array([25,24,22,21,20,19,19,19.03,19.27,19.67,21.11,20.24,20.7,19.69,19.5])
    df_fl = pd.DataFrame({'date': dates[:15], 'close': prices_fl})
    r4 = calc_nine_turn(df_fl)
    print(f"下行夹反弹(FLY类): dir={r4.direction} count={r4.count} status={r4.status}  (应为 down/下跌九转)")
