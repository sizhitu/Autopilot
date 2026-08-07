#!/usr/bin/env python3
"""季并：合并三个 monthly parquet → quarterly/YYYY-Qn.parquet"""
from __future__ import annotations

import argparse
from pathlib import Path
import pandas as pd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="data")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--quarter", type=int, required=True, choices=[1, 2, 3, 4])
    args = ap.parse_args()
    root = Path(args.data_root)
    months = {1: (1, 2, 3), 2: (4, 5, 6), 3: (7, 8, 9), 4: (10, 11, 12)}[args.quarter]
    frames = []
    for m in months:
        p = root / "monthly" / f"{args.year:04d}-{m:02d}.parquet"
        if p.is_file():
            frames.append(pd.read_parquet(p))
            print("load", p)
    if not frames:
        print("no monthly files")
        return 1
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["symbol", "date"], keep="last")
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    out_dir = root / "quarterly"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.year:04d}-Q{args.quarter}.parquet"
    df.to_parquet(out, index=False)
    print(f"wrote {out} rows={len(df)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
