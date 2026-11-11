#!/usr/bin/env python3
"""Export per-user analysis_history snapshots to Parquet for cold backup.

The analysis result JSON already contains data.fib_levels, so this archives the
existing history table rather than maintaining a duplicate Fibonacci table.
"""
from __future__ import annotations

import argparse
import json
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


def _rows_to_frame(rows: list[dict]) -> pd.DataFrame:
    """Keep flexible JSONB result payloads losslessly as UTF-8 JSON strings."""
    normalized = []
    for row in rows:
        item = dict(row)
        value = item.get("result_json")
        if value is not None and not isinstance(value, str):
            item["result_json"] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        normalized.append(item)
    return pd.DataFrame(normalized)


def fetch_all_analysis_history(client, page_size: int = 1000) -> list[dict]:
    """Read every page; never silently truncate at the Supabase row limit."""
    rows: list[dict] = []
    start = 0
    while True:
        page = (
            client.table("analysis_history")
            .select("id,user_id,symbol,name,result_json,created_at")
            .order("created_at", desc=False)
            .order("id", desc=False)
            .range(start, start + page_size - 1)
            .execute()
            .data
            or []
        )
        if not page:
            break
        rows.extend(page)
        if len(page) < page_size:
            break
        start += page_size
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Archive Supabase analysis_history to Parquet")
    parser.add_argument("--out", required=True, help="data 根目录，例如 ../data-repo/data")
    args = parser.parse_args()

    if not supabase_client or not supabase_client.using_supabase():
        raise RuntimeError("Supabase 未配置，拒绝静默跳过 analysis_history 冷备")

    client = supabase_client.get_service_client()
    rows = fetch_all_analysis_history(client)
    if not rows:
        print("analysis_history 为空，暂无分析历史需要归档")
        return 0

    frame = _rows_to_frame(rows)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    destination = Path(args.out) / "analysis_history" / f"{day}.parquet"
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(destination, index=False)
    print(f"wrote {len(frame)} analysis_history rows → {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
