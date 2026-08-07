"""
周线 MACD 顶/底背离
==================
日 K → 周 K 重采样后计算 MACD(12,26,9)，在价格与 DIF 上寻找最近两处摆动高低点：
  - 底背离（bull）：价格后低更低，DIF 后低抬高
  - 顶背离（bear）：价格后高更高，DIF 后高降低

用于与「日线九转完成」组合，形成研究向的最强信号提示（非投资建议）。
"""

from __future__ import annotations

from typing import Optional, Tuple, List
import numpy as np
import pandas as pd


def _to_weekly_close(df: pd.DataFrame) -> Optional[pd.Series]:
    if df is None or len(df) < 30 or "close" not in df.columns:
        return None
    try:
        if "date" in df.columns:
            idx = pd.to_datetime(df["date"].values)
        else:
            idx = pd.RangeIndex(len(df))
        s = pd.Series(pd.to_numeric(df["close"], errors="coerce").values, index=idx)
        s = s.dropna()
        if len(s) < 30:
            return None
        # 周线：取每周最后一个交易日收盘
        try:
            w = s.resample("W-FRI").last().dropna()
        except Exception:
            w = s.resample("W").last().dropna()
        if len(w) < 35:
            return None
        return w
    except Exception:
        return None


def _ema(arr: np.ndarray, span: int) -> np.ndarray:
    s = pd.Series(arr, dtype=float)
    return s.ewm(span=span, adjust=False).mean().values


def _macd_dif(closes: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    dif = _ema(closes, 12) - _ema(closes, 26)
    dea = _ema(dif, 9)
    hist = dif - dea
    return dif, dea, hist


def _local_extrema(arr: np.ndarray, order: int = 2) -> Tuple[List[int], List[int]]:
    """简易摆动高低点（两侧 order 根内为严格极值）。"""
    highs, lows = [], []
    n = len(arr)
    if n < order * 2 + 1:
        return highs, lows
    for i in range(order, n - order):
        window = arr[i - order : i + order + 1]
        v = arr[i]
        if np.isfinite(v) and v == np.nanmax(window) and np.sum(window == v) == 1:
            highs.append(i)
        if np.isfinite(v) and v == np.nanmin(window) and np.sum(window == v) == 1:
            lows.append(i)
    return highs, lows


def detect_weekly_macd_divergence(df: pd.DataFrame, lookback_weeks: int = 52) -> dict:
    """
    返回:
      divergence: 'bull' | 'bear' | 'none'
      label / detail / dif / dea / weeks
    """
    empty = {
        "divergence": "none",
        "label": "周线 MACD 无明显顶/底背离",
        "detail": "周线样本不足或未形成可比较的摆动高低点。",
        "dif": None,
        "dea": None,
        "hist": None,
        "weeks": 0,
    }
    w = _to_weekly_close(df)
    if w is None or len(w) < 35:
        return empty

    closes = w.values.astype(float)
    if len(closes) > lookback_weeks:
        closes = closes[-lookback_weeks:]
    dif, dea, hist = _macd_dif(closes)
    n = len(closes)
    highs_p, lows_p = _local_extrema(closes, order=2)
    highs_d, lows_d = _local_extrema(dif, order=2)

    result = dict(empty)
    result["dif"] = round(float(dif[-1]), 4) if np.isfinite(dif[-1]) else None
    result["dea"] = round(float(dea[-1]), 4) if np.isfinite(dea[-1]) else None
    result["hist"] = round(float(hist[-1]), 4) if np.isfinite(hist[-1]) else None
    result["weeks"] = int(n)

    # 只认「第二处极值落在近 look_recent 周内」的背离，避免过旧结构
    look_recent = min(16, n // 2)

    def _bull() -> Optional[str]:
        # 价格两低点：后低更低；DIF 两低点：后低抬高；时间大致对齐
        if len(lows_p) < 2 or len(lows_d) < 2:
            return None
        i1, i2 = lows_p[-2], lows_p[-1]
        if i2 < n - look_recent:
            return None
        if closes[i2] >= closes[i1]:
            return None
        # DIF 上取与价格低点邻近的低点（±3 周）
        d_near = [j for j in lows_d if abs(j - i1) <= 3]
        d_near2 = [j for j in lows_d if abs(j - i2) <= 3]
        if not d_near or not d_near2:
            # 退化为 DIF 最近两低
            j1, j2 = lows_d[-2], lows_d[-1]
        else:
            j1, j2 = d_near[-1], d_near2[-1]
        if j2 <= j1:
            return None
        if dif[j2] <= dif[j1]:
            return None
        return (
            f"周线价格近端低点低于前低，同时 MACD-DIF 低点抬高"
            f"（约第 {i1+1}→{i2+1} 周），形成底背离结构。"
        )

    def _bear() -> Optional[str]:
        if len(highs_p) < 2 or len(highs_d) < 2:
            return None
        i1, i2 = highs_p[-2], highs_p[-1]
        if i2 < n - look_recent:
            return None
        if closes[i2] <= closes[i1]:
            return None
        d_near = [j for j in highs_d if abs(j - i1) <= 3]
        d_near2 = [j for j in highs_d if abs(j - i2) <= 3]
        if not d_near or not d_near2:
            j1, j2 = highs_d[-2], highs_d[-1]
        else:
            j1, j2 = d_near[-1], d_near2[-1]
        if j2 <= j1:
            return None
        if dif[j2] >= dif[j1]:
            return None
        return (
            f"周线价格近端高点高于前高，同时 MACD-DIF 高点降低"
            f"（约第 {i1+1}→{i2+1} 周），形成顶背离结构。"
        )

    bull_detail = _bull()
    bear_detail = _bear()
    # 若同时触发，取更近的一端（按第二极值位置）
    if bull_detail and bear_detail:
        # 简单：看 hist 符号与近 4 周价格方向
        if float(closes[-1]) <= float(closes[-min(4, n)]):
            bear_detail = None
        else:
            bull_detail = None

    if bull_detail:
        result["divergence"] = "bull"
        result["label"] = "周线 MACD 底背离"
        result["detail"] = bull_detail
    elif bear_detail:
        result["divergence"] = "bear"
        result["label"] = "周线 MACD 顶背离"
        result["detail"] = bear_detail
    else:
        result["detail"] = (
            f"近 {n} 根周线未检测到有效的顶/底背离"
            f"（需价格与 DIF 摆动高低点反向背离，且近端极值在约 {look_recent} 周内）。"
        )
    return result


def combine_nine_turn_macd(daily_complete: bool, daily_direction: str, macd: dict) -> dict:
    """
    最强组合：日线九转完成 + 同向周线 MACD 背离。
      下跌九转完成 + 底背离 → bull
      上涨九转完成 + 顶背离 → bear
    """
    div = (macd or {}).get("divergence") or "none"
    if (
        daily_complete
        and daily_direction == "down"
        and div == "bull"
    ):
        return {
            "active": True,
            "type": "bull",
            "label": "最强组合：日线下跌九转完成 + 周线 MACD 底背离",
            "detail": "研究框架下偏多观察权重高于单一指标；仍需趋势过滤与风控，不构成投资建议。",
        }
    if (
        daily_complete
        and daily_direction == "up"
        and div == "bear"
    ):
        return {
            "active": True,
            "type": "bear",
            "label": "最强组合：日线上涨九转完成 + 周线 MACD 顶背离",
            "detail": "研究框架下偏空观察权重高于单一指标；仍需趋势过滤与风控，不构成投资建议。",
        }
    return {
        "active": False,
        "type": "none",
        "label": "未形成「日线九转完成 + 周线 MACD 同向背离」组合",
        "detail": "可单独参考九转或周线 MACD；组合同时满足时权重更高。",
    }
