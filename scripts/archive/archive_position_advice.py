"""
将 Supabase position_advice_logs 全量（或近期）导出为 parquet，供冷备。

用法（GitHub Actions 内）：
  PYTHONPATH=trading_tool python scripts/archive/archive_position_advice_logs.py --out ../data-repo/data
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "trading_tool"))

try:
    import supabase_client
except Exception:
    supabase_client = None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="data 根目录，例如 ../data-repo/data")
    ap.add_argument("--days", type=int, default=0, help="只导出最近 N 天；0=全量")
    args = ap.parse_args()

    if not supabase_client or not supabase_client.using_supabase():
        print("Supabase 未配置，跳过 position_advice_logs 归档")
        return

    try:
        client = supabase_client.get_service_client()
        q = client.table("position_advice_logs").select("*").order("bar_date", desc=True)
        if args.days > 0:
            from datetime import timedelta
            cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
            q = q.gte("bar_date", cutoff)
        res = q.limit(50000).execute()
        rows = res.data or []
    except Exception as e:
        print(f"读取 position_advice_logs 失败: {e}")
        return

    if not rows:
        print("position_advice_logs 为空，跳过")
        return

    df = pd.DataFrame(rows)
    # 统一日期列
    if "bar_date" in df.columns:
        df["bar_date"] = pd.to_datetime(df["bar_date"]).dt.strftime("%Y-%m-%d")
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dest = Path(args.out) / "position_advice_logs" / f"{day}.parquet"
    dest.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(dest, index=False)
    print(f"wrote {len(df)} rows → {dest}")


if __name__ == "__main__":
    main()
