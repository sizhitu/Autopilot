"""
成交量收敛三角形（日 / 周 / 月）
================================
对成交量序列做上沿/下沿包络拟合，判断是否形成收敛三角形，
并给出斜率、夹角、振幅收缩比，供详情页示意绘制。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


def _local_extrema(y: np.ndarray, order: int = 2) -> tuple:
    """简单局部峰/谷（不依赖 scipy）。"""
    n = len(y)
    peaks, troughs = [], []
    for i in range(order, n - order):
        window = y[i - order : i + order + 1]
        if y[i] >= window.max() and y[i] > 0:
            peaks.append(i)
        if y[i] <= window.min():
            troughs.append(i)
    # 端点参与包络，避免点太少
    if 0 not in peaks and 0 not in troughs:
        if n > 1 and y[0] >= y[1]:
            peaks.insert(0, 0)
        else:
            troughs.insert(0, 0)
    last = n - 1
    if last not in peaks and last not in troughs:
        if n > 1 and y[last] >= y[last - 1]:
            peaks.append(last)
        else:
            troughs.append(last)
    return peaks, troughs


def _fit_line(idxs: List[int], vals: np.ndarray) -> tuple:
    """最小二乘 y = a + b*x，返回 (intercept, slope)。点不足则用首尾连线。"""
    if len(idxs) < 2:
        return 0.0, 0.0
    x = np.array(idxs, dtype=float)
    y = np.array([vals[i] for i in idxs], dtype=float)
    if len(idxs) == 2 or np.allclose(x, x[0]):
        b = (y[-1] - y[0]) / (x[-1] - x[0] + 1e-12)
        a = y[0] - b * x[0]
        return float(a), float(b)
    b, a = np.polyfit(x, y, 1)  # slope, intercept
    return float(a), float(b)


def _angle_deg(slope_u: float, slope_l: float) -> float:
    """两直线夹角（度），用斜率差近似 atan。"""
    # 线方向向量 (1, slope)
    a1 = math.atan(slope_u)
    a2 = math.atan(slope_l)
    return abs(math.degrees(a1 - a2))


def analyze_volume_series(volumes: List[float], timeframe: str = "D", lookback: int = 40) -> Dict[str, Any]:
    """
    分析单周期成交量收敛。
    返回结构可供前端绘制金边阴影三角形。
    """
    empty = {
        "timeframe": timeframe,
        "label": {"D": "日线", "W": "周线", "M": "月线"}.get(timeframe, timeframe),
        "status": "数据不足",
        "converging": False,
        "upper_slope": 0.0,
        "lower_slope": 0.0,
        "angle_deg": 0.0,
        "amplitude_ratio": 1.0,
        "amplitude_shrink_pct": 0.0,
        "score": 0,
        "summary": "成交量样本不足，无法判断收敛。",
        "vols": [],
        "upper_line": [],
        "lower_line": [],
    }
    if not volumes or len(volumes) < 8:
        return empty

    lookback = min(lookback, len(volumes))
    raw = np.array(volumes[-lookback:], dtype=float)
    # 归一化到 0~1，便于斜率/示意比较
    vmax = float(raw.max()) if raw.max() > 0 else 1.0
    y = raw / vmax
    n = len(y)
    xs = list(range(n))

    order = 2 if n >= 20 else 1
    peaks, troughs = _local_extrema(y, order=order)
    if len(peaks) < 2:
        peaks = [0, n - 1] if y[0] >= y[-1] else [int(np.argmax(y[: n // 2])), int(n // 2 + np.argmax(y[n // 2 :]))]
    if len(troughs) < 2:
        troughs = [0, n - 1] if y[0] <= y[-1] else [int(np.argmin(y[: n // 2])), int(n // 2 + np.argmin(y[n // 2 :]))]

    a_u, b_u = _fit_line(peaks, y)
    a_l, b_l = _fit_line(troughs, y)

    upper_line = [max(0.0, a_u + b_u * i) for i in xs]
    lower_line = [max(0.0, a_l + b_l * i) for i in xs]
    # 保证上沿不低于下沿（示意用）
    for i in range(n):
        if upper_line[i] < lower_line[i]:
            mid = (upper_line[i] + lower_line[i]) / 2
            upper_line[i], lower_line[i] = mid + 0.02, mid - 0.02

    angle = _angle_deg(b_u, b_l)
    # 振幅：前半 vs 后半 的 (max-min)
    mid = n // 2
    amp_early = float(y[:mid].max() - y[:mid].min()) if mid > 1 else 1.0
    amp_late = float(y[mid:].max() - y[mid:].min()) if n - mid > 1 else amp_early
    ratio = amp_late / (amp_early + 1e-12)
    shrink_pct = max(0.0, (1.0 - ratio) * 100.0)

    # 收敛判定：上沿下行 + 下沿上行，或振幅明显收缩且夹角收窄
    converging = (b_u < -1e-4 and b_l > 1e-4) or (ratio < 0.72 and angle < 25)
    expanding = b_u > 1e-4 and b_l < -1e-4

    if converging:
        status = "收敛中"
        score = int(min(100, 40 + shrink_pct * 0.5 + max(0, 20 - angle)))
        summary = (
            f"{empty['label']}成交量呈收敛三角形倾向：上沿斜率 {b_u:.4f}、下沿斜率 {b_l:.4f}，"
            f"夹角约 {angle:.1f}°，近端振幅较前期收缩 {shrink_pct:.0f}%。"
            f"量能波动收窄，常对应方向选择前的整理阶段（需结合价格结构，非操作建议）。"
        )
    elif expanding:
        status = "扩张中"
        score = int(max(0, 30 - shrink_pct * 0.3))
        summary = (
            f"{empty['label']}成交量包络扩张：上下沿张开，夹角约 {angle:.1f}°，"
            f"振幅比 {ratio:.2f}。量能波动放大，整理收敛特征较弱。"
        )
    else:
        status = "中性"
        score = 35
        summary = (
            f"{empty['label']}成交量未形成清晰收敛三角：上沿斜率 {b_u:.4f}、下沿 {b_l:.4f}，"
            f"夹角 {angle:.1f}°，振幅比 {ratio:.2f}。"
        )

    return {
        "timeframe": timeframe,
        "label": empty["label"],
        "status": status,
        "converging": bool(converging),
        "upper_slope": round(b_u, 5),
        "lower_slope": round(b_l, 5),
        "angle_deg": round(angle, 2),
        "amplitude_ratio": round(ratio, 3),
        "amplitude_shrink_pct": round(shrink_pct, 1),
        "score": score,
        "summary": summary,
        "vols": [round(float(v), 4) for v in y.tolist()],
        "upper_line": [round(float(v), 4) for v in upper_line],
        "lower_line": [round(float(v), 4) for v in lower_line],
        "lookback": n,
    }


def _resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    if df is None or len(df) == 0 or "volume" not in df.columns:
        return pd.DataFrame()
    x = df.copy()
    if "date" not in x.columns:
        return pd.DataFrame()
    x["date"] = pd.to_datetime(x["date"])
    x = x.set_index("date").sort_index()
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    cols = {k: v for k, v in agg.items() if k in x.columns}
    try:
        out = x.resample(rule).agg(cols).dropna(subset=["close"] if "close" in cols else ["volume"])
    except Exception:
        return pd.DataFrame()
    return out.reset_index()


def compute_volume_convergence(df: pd.DataFrame) -> Dict[str, Any]:
    """从日线 OHLCV 计算日/周/月成交量收敛。"""
    if df is None or len(df) < 8 or "volume" not in df.columns:
        return {
            "daily": analyze_volume_series([], "D"),
            "weekly": analyze_volume_series([], "W"),
            "monthly": analyze_volume_series([], "M"),
            "overall": "数据不足",
        }

    daily_vols = [float(v) for v in df["volume"].tolist() if v is not None and not (isinstance(v, float) and math.isnan(v))]
    d = analyze_volume_series(daily_vols, "D", lookback=48)

    wdf = _resample_ohlcv(df, "W-FRI")
    w = analyze_volume_series(
        [float(v) for v in wdf["volume"].tolist()] if len(wdf) else [],
        "W",
        lookback=36,
    )

    # pandas 2.2+ 用 ME；旧版用 M
    mdf = pd.DataFrame()
    for rule in ("ME", "M", "MS"):
        try:
            mdf = _resample_ohlcv(df, rule)
            if len(mdf) >= 3:
                break
        except Exception:
            mdf = pd.DataFrame()
    m = analyze_volume_series(
        [float(v) for v in mdf["volume"].tolist()] if len(mdf) else [],
        "M",
        lookback=24,
    )

    flags = [x.get("converging") for x in (d, w, m)]
    n_c = sum(1 for f in flags if f)
    if n_c >= 2:
        overall = f"多周期共振：{n_c}/3 个周期量能收敛，整理特征更强"
    elif n_c == 1:
        overall = "仅单一周期出现量能收敛，需结合价格结构观察"
    else:
        overall = "日/周/月均未见明显量能收敛三角"

    return {"daily": d, "weekly": w, "monthly": m, "overall": overall}
