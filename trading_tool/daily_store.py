"""
每日行情存储
============
把每个标的按「天」粒度的 K 线落库（daily_data 表），用于：
  - 数据自动回测
  - 指标分析（在无法实时拉取时回退到本地存储）
  - 接口容错：某次实时请求失败时，前端可读取最近一次成功存储的数据

存储粒度：以 df 的 date 列（YYYY-MM-DD）为 trade_date，按 (symbol, trade_date)
upsert。实时拉取一次就顺手写入，零额外成本。
"""

from datetime import datetime
from typing import List, Optional

import pandas as pd

import db


def _fmt_date(v) -> Optional[str]:
    try:
        return pd.to_datetime(v).strftime("%Y-%m-%d")
    except Exception:
        return None


def store_daily_bars(symbol: str, df: pd.DataFrame, source: str = "") -> int:
    """
    将 df 中每行（含 date/open/high/low/close/volume）按天 upsert 到 daily_data。
    返回成功写入的行数。
    """
    if df is None or len(df) == 0 or "date" not in df.columns:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for _, r in df.iterrows():
        d = _fmt_date(r["date"])
        if not d:
            continue
        rows.append((
            symbol, d,
            float(r.get("open", 0) or 0),
            float(r.get("high", 0) or 0),
            float(r.get("low", 0) or 0),
            float(r.get("close", 0) or 0),
            float(r.get("volume", 0) or 0),
            source, now,
        ))
    if not rows:
        return 0
    conn = db.get_conn()
    with db.db_lock():
        conn.executemany(
            "INSERT OR REPLACE INTO daily_data"
            "(symbol, trade_date, open, high, low, close, volume, source, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    return len(rows)


def get_stored_daily(symbol: str, limit: Optional[int] = None) -> List[dict]:
    """
    取回某标的已存储的每日行情（按日期升序），用于回测 / 指标分析 / 容错展示。
    """
    conn = db.get_conn()
    with db.db_lock():
        if limit:
            rows = conn.execute(
                "SELECT * FROM daily_data WHERE symbol=? ORDER BY trade_date DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
            rows = list(reversed(rows))
        else:
            rows = conn.execute(
                "SELECT * FROM daily_data WHERE symbol=? ORDER BY trade_date ASC",
                (symbol,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_last_stored_date(symbol: str) -> Optional[str]:
    conn = db.get_conn()
    with db.db_lock():
        row = conn.execute(
            "SELECT MAX(trade_date) AS d FROM daily_data WHERE symbol=?", (symbol,)
        ).fetchone()
    return row["d"] if row and row["d"] else None


def count_stored(symbol: str) -> int:
    conn = db.get_conn()
    with db.db_lock():
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM daily_data WHERE symbol=?", (symbol,)
        ).fetchone()
    return int(row["c"]) if row else 0
