"""
价格收敛三角形（对称三角 / 收敛楔）
====================================
技术分析经典形态：高点连线下行 + 低点连线上行，波动收窄。

理论简要（Edwards & Magee、Bulkowski 等）：
  - 多空暂时平衡，波动率下降，量能常同步收缩
  - 突破方向需等收盘确认；统计上略偏延续原趋势，但左右开破都常见
  - 单独形态不构成买卖指令，宜与趋势、九转、量价、位置合用

本模块在日线 high/low 上找摆动高低点，拟合上下轨斜率，判断是否收敛。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


def _swing_points(high: np.ndarray, low: np.ndarray, order: int = 3) -> Tuple[List[int], List[int]]:
    n = len(high)
    peaks, troughs = [], []
    if n < order * 2 + 3:
        return peaks, troughs
    for i in range(order, n - order):
        if high[i] >= np.max(high[i - order : i + order + 1]):
            peaks.append(i)
        if low[i] <= np.min(low[i - order : i + order + 1]):
            troughs.append(i)
    return peaks, troughs


def _fit(idxs: List[int], vals: np.ndarray) -> Tuple[float, float]:
    if len(idxs) < 2:
        return 0.0, 0.0
    x = np.asarray(idxs, dtype=float)
    y = np.asarray([vals[i] for i in idxs], dtype=float)
    if len(idxs) == 2 or np.allclose(x, x[0]):
        b = (y[-1] - y[0]) / (x[-1] - x[0] + 1e-12)
        a = y[0] - b * x[0]
        return float(a), float(b)
    b, a = np.polyfit(x, y, 1)
    return float(a), float(b)


def detect_price_triangle(
    df: pd.DataFrame,
    lookback: int = 60,
    swing_order: int = 3,
    min_swings: int = 2,
) -> Dict[str, Any]:
    """
    检测近端价格是否形成收敛三角形。

    Returns:
      forming: 是否收敛中
      status: 收敛中 / 扩张 / 中性 / 数据不足
      label: 短标签
      detail: 说明
      upper_slope / lower_slope: 上轨/下轨斜率（价/根）
      angle_deg: 夹角近似
      width_shrink_pct: 振幅收缩百分比
      apex_bars: 粗略估计距交点的 K 线数（可空）
      breakout: none | up | down（近端是否已收盘突破上/下轨）
    """
    out: Dict[str, Any] = {
        "forming": False,
        "status": "数据不足",
        "label": "价格三角 —",
        "detail": "K 线不足，无法识别价格收敛三角。",
        "upper_slope": 0.0,
        "lower_slope": 0.0,
        "angle_deg": 0.0,
        "width_shrink_pct": 0.0,
        "apex_bars": None,
        "breakout": "none",
        "score": 0,
    }

    if df is None or len(df) < 20:
        return out
    for col in ("high", "low", "close"):
        if col not in df.columns:
            out["detail"] = f"缺少列 {col}"
            return out

    tail = df.tail(min(lookback, len(df))).copy().reset_index(drop=True)
    high = tail["high"].astype(float).values
    low = tail["low"].astype(float).values
    close = tail["close"].astype(float).values
    n = len(close)

    peaks, troughs = _swing_points(high, low, order=swing_order)
    # 至少各 2 个摆动点
    if len(peaks) < min_swings or len(troughs) < min_swings:
        out["status"] = "中性"
        out["label"] = "摆动点不足"
        out["detail"] = "近端高低点数量不足，难以拟合收敛上下轨（分析师画线至少需要两高两低）。"
        return out

    # 用最近若干摆动点拟合（最多 5 个）
    pk = peaks[-min(5, len(peaks)) :]
    tr = troughs[-min(5, len(troughs)) :]
    a_u, b_u = _fit(pk, high)
    a_l, b_l = _fit(tr, low)

    out["upper_slope"] = round(b_u, 6)
    out["lower_slope"] = round(b_l, 6)

    # 收敛：上轨下行(b_u<0) 且 下轨上行(b_l>0)
    converging = b_u < -1e-6 and b_l > 1e-6

    # 振幅收缩：前半 vs 后半 (high-low) 均值
    mid = n // 2
    w_early = float(np.mean(high[:mid] - low[:mid])) if mid > 2 else 1.0
    w_late = float(np.mean(high[mid:] - low[mid:])) if n - mid > 2 else w_early
    ratio = w_late / (w_early + 1e-12)
    shrink = max(0.0, (1.0 - ratio) * 100.0)
    out["width_shrink_pct"] = round(shrink, 1)

    ang = abs(math.degrees(math.atan(b_u) - math.atan(b_l)))
    out["angle_deg"] = round(ang, 2)

    # 交点（apex）：a_u + b_u*x = a_l + b_l*x
    denom = b_u - b_l
    apex_bars = None
    if abs(denom) > 1e-12:
        x_apex = (a_l - a_u) / denom
        apex_bars = int(round(x_apex - (n - 1)))
        # 仅当交点在未来一段内才有意义
        if apex_bars is not None and (apex_bars < 0 or apex_bars > lookback):
            apex_bars = None
    out["apex_bars"] = apex_bars

    # 突破：收盘相对近端上/下轨
    x_last = float(n - 1)
    upper_now = a_u + b_u * x_last
    lower_now = a_l + b_l * x_last
    br = "none"
    if close[-1] > upper_now * 1.002:
        br = "up"
    elif close[-1] < lower_now * 0.998:
        br = "down"
    out["breakout"] = br

    if converging and shrink >= 8:
        out["forming"] = True
        out["status"] = "收敛中"
        out["score"] = min(100, int(40 + shrink * 0.5 + max(0, 20 - ang)))
        out["label"] = "价格收敛三角形"
        out["detail"] = (
            f"高点连线下行、低点连线上行（上轨斜率 {b_u:.4f}，下轨 {b_l:.4f}），"
            f"波幅约收窄 {shrink:.0f}%，夹角约 {ang:.1f}°。"
            f"属经典整理形态：多空平衡、波动压缩；方向看收盘突破上轨或下轨后是否回补。"
        )
        if br == "up":
            out["detail"] += " 当前收盘已偏上破上轨，需确认是否站稳。"
        elif br == "down":
            out["detail"] += " 当前收盘已偏下破下轨，需确认是否站稳。"
        if apex_bars is not None and 0 < apex_bars <= 30:
            out["detail"] += f" 粗略交点约在 {apex_bars} 根K线内。"
    elif b_u > 0 and b_l < 0 and shrink < 5:
        out["status"] = "扩张"
        out["label"] = "波动扩张"
        out["detail"] = "高低点包络张开，更像扩散形态而非收敛三角。"
        out["score"] = 10
    else:
        out["status"] = "中性"
        out["label"] = "未形成清晰收敛三角"
        out["detail"] = (
            f"上轨斜率 {b_u:.4f}、下轨 {b_l:.4f}，波幅变化约 {shrink:.0f}%。"
            f"分析师手绘的「两高渐低、两低渐高」尚未被算法稳定拟合。"
        )
        out["score"] = int(min(40, shrink))

    return out
