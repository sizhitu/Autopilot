"""
量价背离形态识别
=================================
在日线（或传入周期）上识别常见量价背离 / 背驰结构，供时机层与详情页使用。

识别类型：
  1. 底背离（bull）：价格创新低（或更低低点），但成交量萎缩 / OBV 未创新低
     → 抛压衰竭，偏多观察
  2. 顶背离（bear）：价格创新高（或更高高点），但成交量未放大 / OBV 未创新高
     → 上攻动能不足，偏空观察
  3. 价涨量缩（weak_rally）：近端上涨段量能明显低于前一段
  4. 价跌量缩（selling_dryup）：近端下跌段量能明显低于前一段（抛压减轻）

输出统一结构，便于策略与前端复用。仅供研究，不构成投资建议。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd


def _swing_indices(series: np.ndarray, order: int = 3) -> Tuple[List[int], List[int]]:
    """简单摆动高低点：两侧各 order 根内为极值。"""
    n = len(series)
    highs, lows = [], []
    if n < order * 2 + 1:
        return highs, lows
    for i in range(order, n - order):
        window = series[i - order : i + order + 1]
        if series[i] >= np.max(window) and series[i] == window.max():
            highs.append(i)
        if series[i] <= np.min(window) and series[i] == window.min():
            lows.append(i)
    return highs, lows


def _obv(close: np.ndarray, volume: np.ndarray) -> np.ndarray:
    obv = np.zeros(len(close), dtype=float)
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            obv[i] = obv[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            obv[i] = obv[i - 1] - volume[i]
        else:
            obv[i] = obv[i - 1]
    return obv


def detect_volume_price_divergence(
    df: pd.DataFrame,
    lookback: int = 60,
    swing_order: int = 3,
) -> Dict[str, Any]:
    """
    检测量价背离。

    Returns:
      divergence: 'bull' | 'bear' | 'none'
      patterns: list of pattern codes
      label: 短标签
      detail: 说明
      strength: 0~1 粗略强度
    """
    result: Dict[str, Any] = {
        "divergence": "none",
        "patterns": [],
        "label": "无明显量价背离",
        "detail": "近端价格与成交量结构未形成有效背离。",
        "strength": 0.0,
        "price_change_pct": None,
        "volume_ratio": None,
    }

    if df is None or len(df) < max(20, swing_order * 4 + 5):
        result["detail"] = "K 线不足，无法识别量价背离。"
        return result

    need = ["close", "volume"]
    for c in need:
        if c not in df.columns:
            result["detail"] = f"缺少列 {c}"
            return result

    tail = df.tail(min(lookback, len(df))).copy()
    close = tail["close"].astype(float).values
    volume = tail["volume"].astype(float).values
    n = len(close)
    if np.nanmean(volume) <= 0:
        result["detail"] = "成交量数据异常或全为 0。"
        return result

    obv = _obv(close, volume)
    highs, lows = _swing_indices(close, order=swing_order)

    patterns: List[str] = []
    details: List[str] = []
    bull_score = 0.0
    bear_score = 0.0

    # —— 摆动点背离（核心）——
    # 底背离：最近两个低点，价更低但量更小 或 OBV 更高
    if len(lows) >= 2:
        i1, i2 = lows[-2], lows[-1]
        # 第二低点应靠近右端
        if n - 1 - i2 <= max(12, n // 4):
            price_ll = close[i2] < close[i1] * 0.998
            vol_lower = volume[i2] < volume[i1] * 0.85
            obv_higher = obv[i2] > obv[i1]
            if price_ll and (vol_lower or obv_higher):
                patterns.append("swing_bull")
                bull_score += 0.55 if (vol_lower and obv_higher) else 0.4
                why = []
                if vol_lower:
                    why.append(f"低点量能 {volume[i2]:.0f}<前低量 {volume[i1]:.0f}")
                if obv_higher:
                    why.append("OBV 未随价格创新低")
                details.append("底背离：" + "，".join(why))

    # 顶背离：最近两个高点，价更高但量更小 或 OBV 更低
    if len(highs) >= 2:
        i1, i2 = highs[-2], highs[-1]
        if n - 1 - i2 <= max(12, n // 4):
            price_hh = close[i2] > close[i1] * 1.002
            vol_lower = volume[i2] < volume[i1] * 0.85
            obv_lower = obv[i2] < obv[i1]
            if price_hh and (vol_lower or obv_lower):
                patterns.append("swing_bear")
                bear_score += 0.55 if (vol_lower and obv_lower) else 0.4
                why = []
                if vol_lower:
                    why.append(f"高点量能 {volume[i2]:.0f}<前高量 {volume[i1]:.0f}")
                if obv_lower:
                    why.append("OBV 未随价格创新高")
                details.append("顶背离：" + "，".join(why))

    # —— 近端段量价关系（辅助）——
    seg = max(5, n // 6)
    if n >= seg * 2:
        p_prev = close[-2 * seg : -seg]
        p_curr = close[-seg:]
        v_prev = volume[-2 * seg : -seg]
        v_curr = volume[-seg:]
        p_prev_ch = (p_prev[-1] - p_prev[0]) / p_prev[0] if p_prev[0] else 0
        p_curr_ch = (p_curr[-1] - p_curr[0]) / p_curr[0] if p_curr[0] else 0
        v_ratio = float(np.mean(v_curr) / np.mean(v_prev)) if np.mean(v_prev) > 0 else 1.0
        result["price_change_pct"] = round(p_curr_ch * 100, 2)
        result["volume_ratio"] = round(v_ratio, 2)

        # 价涨量缩
        if p_curr_ch > 0.02 and v_ratio < 0.75:
            patterns.append("weak_rally")
            bear_score += 0.25
            details.append(f"价涨量缩：近段涨 {p_curr_ch*100:.1f}%，量比前段 {v_ratio:.2f}")
        # 价跌量缩（抛压减轻）
        if p_curr_ch < -0.02 and v_ratio < 0.75:
            patterns.append("selling_dryup")
            bull_score += 0.25
            details.append(f"价跌量缩：近段跌 {abs(p_curr_ch)*100:.1f}%，量比前段 {v_ratio:.2f}")
        # 价涨量增确认（非背离，记录为健康）
        if p_curr_ch > 0.02 and v_ratio > 1.2:
            patterns.append("healthy_up")
        if p_curr_ch < -0.02 and v_ratio > 1.2:
            patterns.append("heavy_down")

    # —— 汇总（阈值略低以纳入单段价量背离）——
    if bull_score > bear_score and bull_score >= 0.25:
        result["divergence"] = "bull"
        result["strength"] = round(min(1.0, bull_score), 2)
        result["label"] = "量价底背离" if "swing_bull" in patterns else "价跌量缩（抛压减轻）"
    elif bear_score > bull_score and bear_score >= 0.25:
        result["divergence"] = "bear"
        result["strength"] = round(min(1.0, bear_score), 2)
        result["label"] = "量价顶背离" if "swing_bear" in patterns else "价涨量缩（上攻偏弱）"
    else:
        result["divergence"] = "none"
        result["strength"] = round(max(bull_score, bear_score), 2)
        if "healthy_up" in patterns:
            result["label"] = "量价齐升（确认偏多）"
            result["detail"] = "近段价格上涨且量能放大，未见背离。"
            result["patterns"] = patterns
            return result
        if "heavy_down" in patterns:
            result["label"] = "放量下跌（确认偏空）"
            result["detail"] = "近段价格下跌且量能放大，未见底背离。"
            result["patterns"] = patterns
            return result
        result["label"] = "无明显量价背离"

    result["patterns"] = patterns
    if details:
        result["detail"] = "；".join(details)
    return result


def volume_supports_side(div: Dict[str, Any], side: str) -> bool:
    """
    side: 'buy' | 'sell'
    量价结构是否支持该侧（背离同向或无明显反向）。
    """
    d = (div or {}).get("divergence") or "none"
    patterns = (div or {}).get("patterns") or []
    if side == "buy":
        if d == "bull":
            return True
        if d == "bear":
            return False
        if "heavy_down" in patterns:
            return False
        return True
    if side == "sell":
        if d == "bear":
            return True
        if d == "bull":
            return False
        if "healthy_up" in patterns:
            return False
        return True
    return True
