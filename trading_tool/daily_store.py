"""
每日行情存储（缓存层，不写业务库）
================================
原始 K 线属于「高频临时数据」，严格遵循数据分层：
  - 不再写入 Supabase 业务库（daily_data 已废弃）
  - 改为写入缓存层（Upstash Redis / Supabase cache 表 / 本地内存）

对外函数签名保持不变，调用方（watchlist / quote / backtest）无需改动：
  store_daily_bars(symbol, df, source) -> int
  get_stored_daily(symbol, limit=None) -> list[dict]
  get_last_stored_date(symbol) -> str|None
  count_stored(symbol) -> int
"""

from datetime import datetime
from typing import List, Optional

import pandas as pd

import cache


def _fmt_date(v) -> Optional[str]:
    try:
        return pd.to_datetime(v).strftime("%Y-%m-%d")
    except Exception:
        return None


def store_daily_bars(symbol: str, df: pd.DataFrame, source: str = "") -> int:
    """将 df 每行按天写入缓存层，返回写入行数。"""
    if df is None or len(df) == 0 or "date" not in df.columns:
        return 0
    bars = []
    for _, r in df.iterrows():
        d = _fmt_date(r["date"])
        if not d:
            continue
        bars.append({
            "trade_date": d,
            "open": float(r.get("open", 0) or 0),
            "high": float(r.get("high", 0) or 0),
            "low": float(r.get("low", 0) or 0),
            "close": float(r.get("close", 0) or 0),
            "volume": float(r.get("volume", 0) or 0),
            "source": source,
        })
    if not bars:
        return 0
    cache.set_daily_cache(symbol, bars)
    return len(bars)


def get_stored_daily(symbol: str, limit: Optional[int] = None) -> List[dict]:
    """取回已缓存的每日行情（升序）；limit>0 取最近 N 天。"""
    bars = cache.get_daily_cache(symbol) or []
    if limit:
        bars = bars[-limit:]
    return bars


def get_last_stored_date(symbol: str) -> Optional[str]:
    bars = cache.get_daily_cache(symbol) or []
    return bars[-1]["trade_date"] if bars else None


def count_stored(symbol: str) -> int:
    return len(cache.get_daily_cache(symbol) or [])
