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
import numpy as np


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
    if "date" not in df.columns:
        return pd.DataFrame()
    # 避免无谓整表 copy：只取需要的列
    cols = [c for c in ("date", "open", "high", "low", "close", "volume") if c in df.columns]
    x = df.loc[:, cols]
    if not np.issubdtype(x["date"].dtype, np.datetime64):
        x = x.copy()
        x["date"] = pd.to_datetime(x["date"])
    else:
        x = x.set_index("date")
        if not x.index.is_monotonic_increasing:
            x = x.sort_index()
        # already indexed path below
        agg = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
        cols_agg = {k: v for k, v in agg.items() if k in x.columns}
        try:
            out = x.resample(rule).agg(cols_agg).dropna(subset=["close"] if "close" in cols_agg else ["volume"])
        except Exception:
            return pd.DataFrame()
        return out.reset_index()
    x = x.set_index("date")
    if not x.index.is_monotonic_increasing:
        x = x.sort_index()
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


def _classify_weekly_tape(wdf: pd.DataFrame) -> Dict[str, Any]:
    """
    周线量价状态：放量上涨 / 缩量止跌 / 放量下跌 等。
    用最近 1 周相对前 4 周均量、以及近 1～2 周涨跌判断。
    """
    empty = {
        "label": "数据不足",
        "code": "insufficient",
        "detail": "周线样本不足，暂无法判断量价状态。",
    }
    if wdf is None or len(wdf) < 5:
        return empty
    if "close" not in wdf.columns or "volume" not in wdf.columns:
        return empty

    closes = wdf["close"].astype(float).tolist()
    vols = wdf["volume"].astype(float).tolist()
    last_c, prev_c = closes[-1], closes[-2]
    last_v = vols[-1]
    base_vols = vols[-5:-1]
    avg_v = sum(base_vols) / max(len(base_vols), 1)
    if avg_v <= 0:
        return empty

    chg = (last_c - prev_c) / prev_c * 100.0 if prev_c else 0.0
    # 近两周方向（减弱单周噪声）
    if len(closes) >= 3:
        chg2 = (last_c - closes[-3]) / closes[-3] * 100.0
    else:
        chg2 = chg
    vol_ratio = last_v / avg_v

    up = chg > 0.3 or (chg > 0 and chg2 > 0.5)
    down = chg < -0.3 or (chg < 0 and chg2 < -0.5)
    flat = not up and not down
    heavy = vol_ratio >= 1.25
    light = vol_ratio <= 0.85

    if up and heavy:
        label, code = "放量上涨", "vol_up_rise"
        detail = (
            f"近周收涨约 {chg:+.1f}%，成交量为近四周均量的 {vol_ratio:.2f} 倍，偏放量推动。"
            f"若此前周线处于收敛，需看后续一两周是否放量延续、还是迅速缩量回补整理区。"
        )
    elif up and light:
        label, code = "缩量上涨", "vol_down_rise"
        detail = (
            f"近周收涨约 {chg:+.1f}%，但量能为均量的 {vol_ratio:.2f} 倍，上涨缺乏量能确认，"
            f"更像修复或弱反弹；收敛结束后若无放量，向上打开的可靠性偏低。"
        )
    elif down and heavy:
        label, code = "放量下跌", "vol_up_fall"
        detail = (
            f"近周收跌约 {chg:+.1f}%，量能为均量的 {vol_ratio:.2f} 倍，偏放量抛压。"
            f"周线收敛若被向下放量打开，优先观察是否回补失败、波动是否继续扩张。"
        )
    elif down and light:
        label, code = "缩量止跌/缩量阴跌", "vol_down_fall"
        detail = (
            f"近周收跌约 {chg:+.1f}%，量能仅均量的 {vol_ratio:.2f} 倍。"
            f"缩量下跌有时是抛压衰竭（止跌雏形），也可能是阴跌无人接；需等放量周确认方向。"
        )
    elif flat and light:
        label, code = "缩量整理", "vol_down_flat"
        detail = (
            f"近周涨跌约 {chg:+.1f}%，量能缩至均量的 {vol_ratio:.2f} 倍，典型缩量整理。"
            f"与周线收敛同向时，关键看收敛结束后第一根（或前几根）带量周K的方向与是否回补。"
        )
    elif flat and heavy:
        label, code = "放量震荡", "vol_up_flat"
        detail = (
            f"近周价格近乎走平（{chg:+.1f}%），但量能达均量的 {vol_ratio:.2f} 倍，多空交换激烈。"
            f"收敛末端若持续放量而不选择方向，突破信号需等收盘站稳再确认。"
        )
    else:
        label, code = "量价中性", "neutral"
        detail = (
            f"近周涨跌 {chg:+.1f}%，量能比 {vol_ratio:.2f}，未形成清晰的放量/缩量方向标签。"
        )

    return {
        "label": label,
        "code": code,
        "detail": detail,
        "week_change_pct": round(chg, 2),
        "vol_ratio": round(vol_ratio, 2),
    }


def compute_volume_convergence(df: pd.DataFrame) -> Dict[str, Any]:
    """
    从日线 OHLCV 计算**周 / 月**成交量收敛（日线噪声大，不再输出）。
    并给出周线量价状态一句话，便于理解关键观察点。
    """
    empty_w = analyze_volume_series([], "W")
    empty_m = analyze_volume_series([], "M")
    guide = (
        "关键逻辑：周线收敛结束后，看第一根（或前几根）带量K线的方向与是否回补；"
        "月线用来判断大级别是活跃段还是沉寂段。日线噪声大，已省略。"
    )
    if df is None or len(df) < 8 or "volume" not in df.columns:
        return {
            "weekly": empty_w,
            "monthly": empty_m,
            "weekly_tape": {"label": "数据不足", "code": "insufficient", "detail": "数据不足"},
            "overall": "数据不足",
            "guide": guide,
        }

    wdf = _resample_ohlcv(df, "W-FRI")
    w = analyze_volume_series(
        [float(v) for v in wdf["volume"].tolist()] if len(wdf) else [],
        "W",
        lookback=36,
    )
    tape = _classify_weekly_tape(wdf)

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

    # overall：围绕周+月
    parts = []
    if w.get("converging"):
        parts.append("周线量能收敛中")
    elif "扩张" in str(w.get("status") or ""):
        parts.append("周线量能扩张")
    else:
        parts.append(f"周线量能{w.get('status') or '中性'}")

    if m.get("converging"):
        parts.append("月线收敛（偏沉寂整理）")
    elif "扩张" in str(m.get("status") or ""):
        parts.append("月线扩张（大级别仍活跃）")
    else:
        parts.append(f"月线{m.get('status') or '中性'}")

    tape_label = tape.get("label") or ""
    overall = "；".join(parts)
    if tape_label and tape_label != "数据不足":
        overall = f"周线量价：{tape_label}。{overall}"

    return {
        "weekly": w,
        "monthly": m,
        "weekly_tape": tape,
        "overall": overall,
        "guide": guide,
    }
