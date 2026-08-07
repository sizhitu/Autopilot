#!/usr/bin/env python3
"""
日更归档：拉取标的日 K，写入 Parquet（增量：每标的最近若干交易日）。

用法（在仓库根目录）:
  PYTHONPATH=trading_tool python scripts/archive/fetch_and_archive.py --out ./data-out
  PYTHONPATH=trading_tool python scripts/archive/fetch_and_archive.py --symbols-file symbols.txt --days 5

输出:
  {out}/daily/YYYY/MM/DD.parquet
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# 保证可 import trading_tool 内模块
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "trading_tool"))

from data_fetcher import DataFetcher  # noqa: E402

DEFAULT_SYMBOLS = [
    "000001", "159501", "399300", "TSLA", "SPCX", "NVDA", "WTI",
    "AAPL", "MSFT", "GOOG", "AMZN", "META", "AVGO", "MU",
    "600887", "601899", "159880", "518850",
]


def load_symbols(path: str | None) -> list[tuple[str, str]]:
    """返回 [(code, name), ...]；name 可空。"""
    items: list[tuple[str, str]] = []
    seen = set()
    if path and Path(path).is_file():
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace(",", " ").split()
            code = parts[0].strip()
            name = parts[1].strip() if len(parts) > 1 else ""
            key = code.upper()
            if key in seen:
                continue
            seen.add(key)
            items.append((code, name))
    if not items:
        for c in DEFAULT_SYMBOLS:
            if c.upper() not in seen:
                items.append((c, ""))
                seen.add(c.upper())
    return items


def bars_to_rows(symbol: str, name: str, df: pd.DataFrame, source: str, fetched_at: str) -> list[dict]:
    rows = []
    if df is None or len(df) == 0:
        return rows
    for _, r in df.iterrows():
        d = r.get("date")
        if hasattr(d, "strftime"):
            ds = d.strftime("%Y-%m-%d")
        else:
            ds = str(d)[:10]
        rows.append({
            "symbol": str(symbol).upper() if not str(symbol).isdigit() else str(symbol),
            "name": name or "",
            "date": ds,
            "open": float(r["open"]) if pd.notna(r.get("open")) else None,
            "high": float(r["high"]) if pd.notna(r.get("high")) else None,
            "low": float(r["low"]) if pd.notna(r.get("low")) else None,
            "close": float(r["close"]) if pd.notna(r.get("close")) else None,
            "volume": float(r["volume"]) if pd.notna(r.get("volume")) else None,
            "source": source,
            "fetched_at": fetched_at,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="Archive daily OHLCV to Parquet")
    ap.add_argument("--out", default="data", help="output root (contains daily/)")
    ap.add_argument("--symbols-file", default="", help="symbols.txt: CODE [NAME] per line")
    ap.add_argument("--days", type=int, default=8, help="bars per symbol to keep in this daily file")
    ap.add_argument("--fetch-days", type=int, default=40, help="fetch window from data source")
    args = ap.parse_args()

    symbols = load_symbols(args.symbols_file or None)
    fetcher = DataFetcher()
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    all_rows: list[dict] = []
    ok, fail = 0, 0

    for code, name in symbols:
        try:
            df = fetcher.fetch(code, days=args.fetch_days)
            if df is None or len(df) == 0:
                fail += 1
                print(f"[skip] {code}: empty")
                continue
            # 只保留最近 args.days 根，减小日文件
            if len(df) > args.days:
                df = df.tail(args.days).reset_index(drop=True)
            source = "cn" if str(code).isdigit() or str(code).startswith(("sh", "sz", "bj")) else "us"
            all_rows.extend(bars_to_rows(code, name, df, source, fetched_at))
            ok += 1
            print(f"[ok] {code} rows={len(df)}")
        except Exception as e:
            fail += 1
            print(f"[fail] {code}: {e}")

    if not all_rows:
        print("no rows written")
        return 1

    out = pd.DataFrame(all_rows)
    out = out.drop_duplicates(subset=["symbol", "date"], keep="last")
    out = out.sort_values(["symbol", "date"]).reset_index(drop=True)

    # 文件名用 UTC 日期
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    y, m, d = day.split("-")
    dest_dir = Path(args.out) / "daily" / y / m
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{d}.parquet"
    out.to_parquet(dest, index=False)
    print(f"wrote {dest} rows={len(out)} symbols_ok={ok} fail={fail}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
