#!/usr/bin/env python3
"""Calculate, persist, and cold-archive daily CN/US market temperature snapshots."""
from __future__ import annotations
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/"trading_tool"))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--out",default="data")
    args=ap.parse_args()
    import supabase_client
    from data_fetcher import DataFetcher
    from market_temperature import build_market_temperature_snapshot
    if not supabase_client.using_supabase():
        raise SystemExit("Supabase is not configured; refusing to produce non-durable temperature history")
    client=supabase_client.get_service_client()
    fetcher=DataFetcher()
    for market in ("cn","us"):
        try:
            snap=build_market_temperature_snapshot(market,fetcher=fetcher)
            day=str(snap.get("as_of") or datetime.now().date().isoformat())[:10]
            client.table("market_temperature_daily").upsert({
                "market":market,"trade_date":day,"temperature":snap.get("temperature"),
                "regime":snap.get("regime") or "数据不足","coverage":snap.get("coverage") or 0,
                "method_version":snap.get("method_version") or "1.0.0",
                "components":snap.get("components") or {},"snapshot":snap,
                "calculated_at":snap.get("calculated_at"),"updated_at":datetime.now(timezone.utc).isoformat()
            },on_conflict="market,trade_date").execute()
            history=(client.table("market_temperature_daily")
                     .select("trade_date,temperature,regime,coverage,components,method_version")
                     .eq("market",market).order("trade_date",desc=True).limit(30).execute().data or [])
            snap["history"]=list(reversed(history))
            import time
            import cache
            snap["stored_at"]=time.time()
            cache.set_json(f"market_temperature_v3_{market}",snap,18*3600)
            # Small JSON sidecar is the API's last-resort display cache; Parquet
            # remains the historical cold archive. Write atomically to avoid
            # readers observing a partially written file.
            sidecar_dir=Path(args.out)/"market"/"temperature"
            sidecar_dir.mkdir(parents=True,exist_ok=True)
            parquet_file=f"market/temperature/{day[:4]}/{day[5:7]}/{day[8:10]}.parquet"
            sidecar={"market":market,"trade_date":day,"parquet_file":parquet_file,"snapshot":snap}
            fd,tmp_name=tempfile.mkstemp(prefix=f".latest_{market}.",suffix=".tmp",dir=sidecar_dir)
            try:
                with os.fdopen(fd,"w",encoding="utf-8") as fp:
                    json.dump(sidecar,fp,ensure_ascii=False,separators=(",",":"),default=str)
                    fp.flush(); os.fsync(fp.fileno())
                os.replace(tmp_name,sidecar_dir/f"latest_{market}.json")
            finally:
                try:
                    if os.path.exists(tmp_name):os.unlink(tmp_name)
                except OSError:pass
            print(f"{market}: {snap.get('temperature')} / {snap.get('regime')} / coverage={snap.get('coverage')}% / {day}; cold sidecar written")
        except Exception as exc:
            print(f"{market}: snapshot failed: {type(exc).__name__}: {exc}")
    rows=(client.table("market_temperature_daily").select("*")
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
