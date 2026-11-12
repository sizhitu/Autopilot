#!/usr/bin/env python3
"""Fetch provider-computed sentiment indexes and archive valid daily snapshots."""
from __future__ import annotations
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "trading_tool"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data")
    args = parser.parse_args()
    import supabase_client
    from market_temperature import fetch_market_sentiment
    from market_temperature_cold_backup import write_cold_snapshot

    output_root = Path(args.out)
    sidecar_dir = output_root / "market" / "temperature"
    client = None
    if supabase_client.using_supabase():
        try:
            client = supabase_client.get_service_client()
        except Exception as exc:
            print(f"Supabase unavailable; file archive only: {type(exc).__name__}: {exc}")
    else:
        print("Supabase not configured; file archive only")

    current_rows = []
    for market in ("cn", "us"):
        try:
            snapshot = fetch_market_sentiment(market)
            trade_date = str(snapshot["as_of"])[:10]
            now_iso = datetime.now(timezone.utc).isoformat()
            row = {
                "market": market, "trade_date": trade_date,
                "temperature": snapshot["temperature"], "regime": snapshot["regime"],
                "coverage": 100, "method_version": "provider-index-1.0",
                "components": {}, "snapshot": snapshot,
                "calculated_at": snapshot.get("fetched_at"), "updated_at": now_iso,
            }
            history = []
            if client is not None:
                try:
                    client.table("market_temperature_daily").upsert(row, on_conflict="market,trade_date").execute()
                except Exception as exc:
                    print(f"{market}: Supabase upsert failed: {type(exc).__name__}: {exc}")
                try:
                    history = (client.table("market_temperature_daily")
                        .select("trade_date,temperature,regime,coverage,components,method_version")
                        .eq("market", market).order("trade_date", desc=True).limit(30).execute().data or [])
                    history = list(reversed(history))
                except Exception as exc:
                    print(f"{market}: Supabase history unavailable: {type(exc).__name__}: {exc}")
            snapshot["history_db"] = history
            snapshot["stored_at"] = time.time()
            row["snapshot"] = snapshot
            try:
                import cache
                cache.set_json(f"market_temperature_v4_{market}", snapshot, 600)
            except Exception as exc:
                print(f"{market}: cache write skipped: {type(exc).__name__}: {exc}")
            parquet_file = f"market/temperature/{trade_date[:4]}/{trade_date[5:7]}/{trade_date[8:10]}.parquet"
            sidecar = write_cold_snapshot(snapshot, sidecar_dir, parquet_file)
            print(f"{market}: {snapshot['temperature']} / {snapshot['regime']} / {trade_date}; cold sidecar={sidecar}")
            current_rows.append(row)
        except Exception as exc:
            print(f"{market}: provider fetch/archive failed: {type(exc).__name__}: {exc}")

    rows = []
    if client is not None:
        try:
            rows = (client.table("market_temperature_daily").select("*")
                .order("trade_date", desc=True).limit(1000).execute().data or [])
        except Exception as exc:
            print(f"Supabase history export failed; current snapshots only: {type(exc).__name__}: {exc}")
    keyed = {}
    for row in rows + current_rows:
        market, day = row.get("market"), str(row.get("trade_date") or "")[:10]
        if market in ("cn", "us") and day:
            keyed[(market, day)] = row
    grouped = {}
    for row in keyed.values():
        grouped.setdefault(str(row["trade_date"])[:10], []).append(row)
    for day, items in grouped.items():
        year, month, day_num = day.split("-")
        destination = output_root / "market" / "temperature" / year / month / f"{day_num}.parquet"
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            import pandas as pd
            pd.DataFrame(items).to_parquet(destination, index=False)
            print(f"wrote {destination} rows={len(items)}")
        except Exception as exc:
            print(f"Parquet write skipped: {type(exc).__name__}: {exc}")
    if not current_rows:
        raise SystemExit("No provider sentiment snapshots were fetched successfully")


if __name__ == "__main__":
    main()
