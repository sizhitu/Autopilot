#!/usr/bin/env python3
"""Archive persisted daily market-temperature snapshots to cold Parquet."""
from __future__ import annotations
import argparse
from pathlib import Path
import pandas as pd

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--out",default="data"); args=ap.parse_args()
    from trading_tool import supabase_client
    if not supabase_client.using_supabase(): raise SystemExit("Supabase is not configured; refusing empty archive")
    rows=(supabase_client.get_service_client().table("market_temperature_daily").select("*")
          .order("trade_date",desc=True).limit(1000).execute().data or [])
    grouped={}
    for row in rows:
        day=str(row.get("trade_date") or "")[:10]
        if day and row.get("market") in ("cn","us"): grouped.setdefault(day,[]).append(row)
    for day,items in grouped.items():
        y,m,d=day.split("-"); dest=Path(args.out)/"market"/"temperature"/y/m/f"{d}.parquet"
        dest.parent.mkdir(parents=True,exist_ok=True); pd.DataFrame(items).to_parquet(dest,index=False)
        print(f"wrote {dest} rows={len(items)}")
    if not rows: print("No market-temperature rows to archive")
if __name__=="__main__": main()
