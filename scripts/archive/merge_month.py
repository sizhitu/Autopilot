#!/usr/bin/env python3
"""
月并：将 data/daily/YYYY/MM/*.parquet 合并为 data/monthly/YYYY-MM.parquet

用法:
  python scripts/archive/merge_month.py --data-root ./data --year 2026 --month 8
  python scripts/archive/merge_month.py --data-root ./data --prev-month
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def merge_month(data_root: Path, year: int, month: int) -> Path | None:
    daily_dir = data_root / "daily" / f"{year:04d}" / f"{month:02d}"
    if not daily_dir.is_dir():
        print(f"no daily dir: {daily_dir}")
        return None
    files = sorted(daily_dir.glob("*.parquet"))
    if not files:
        print(f"no parquet in {daily_dir}")
        return None
    frames = []
    for f in files:
        try:
            frames.append(pd.read_parquet(f))
        except Exception as e:
            print(f"skip {f}: {e}")
    if not frames:
        return None
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["symbol", "date"], keep="last")
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    out_dir = data_root / "monthly"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{year:04d}-{month:02d}.parquet"
    df.to_parquet(out, index=False)
    print(f"wrote {out} rows={len(df)} from {len(files)} daily files")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--year", type=int, default=0)
    ap.add_argument("--month", type=int, default=0)
    ap.add_argument("--prev-month", action="store_true", help="merge previous calendar month")
    args = ap.parse_args()
    root = Path(args.data_root)

    if args.prev_month:
        now = datetime.now(timezone.utc)
        y, m = now.year, now.month - 1
        if m <= 0:
            y, m = y - 1, 12
    else:
        y = args.year or datetime.now(timezone.utc).year
        m = args.month or datetime.now(timezone.utc).month

    path = merge_month(root, y, m)
    return 0 if path else 1


if __name__ == "__main__":
    raise SystemExit(main())
