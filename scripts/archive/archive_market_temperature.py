#!/usr/bin/env python3
"""Calculate, persist when possible, and cold-archive CN/US temperature snapshots.

The file archive is intentionally independent of Supabase availability: a valid
market snapshot always produces a JSON fallback sidecar and a daily Parquet file.
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "trading_tool"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data")
    args = parser.parse_args()

    import supabase_client
    from data_fetcher import DataFetcher
    from market_temperature import build_market_temperature_snapshot
    from market_temperature_cold_backup import write_cold_snapshot

    output_root = Path(args.out)
    sidecar_dir = output_root / "market" / "temperature"
    snapshots = {}
    current_rows = []
    client = None

    # Supabase is an additional durable index, not a prerequisite for cold files.
    if supabase_client.using_supabase():
        try:
            client = supabase_client.get_service_client()
        except Exception as exc:
            print(f"Supabase unavailable; continuing with file archive only: {type(exc).__name__}: {exc}")
    else:
        print("Supabase not configured; continuing with file archive only")

    # Separate fetcher from the live web service; this script runs in the archive job.
    fetcher = DataFetcher()
    for market in ("cn", "us"):
        try:
            snapshot = build_market_temperature_snapshot(market, fetcher=fetcher)
            trade_date = str(snapshot.get("as_of") or datetime.now().date().isoformat())[:10]
            now_iso = datetime.now(timezone.utc).isoformat()
            row = {
                "market": market,
                "trade_date": trade_date,
                "temperature": snapshot.get("temperature"),
                "regime": snapshot.get("regime") or "数据不足",
                "coverage": snapshot.get("coverage") or 0,
                "method_version": snapshot.get("method_version") or "1.0.0",
                "components": snapshot.get("components") or {},
                "snapshot": snapshot,
                "calculated_at": snapshot.get("calculated_at"),
                "updated_at": now_iso,
            }

            history = []
            if client is not None:
                try:
                    client.table("market_temperature_daily").upsert(
                        row, on_conflict="market,trade_date"
                    ).execute()
                except Exception as exc:
                    print(f"{market}: Supabase upsert failed; file backup continues: {type(exc).__name__}: {exc}")
                try:
                    history = (
                        client.table("market_temperature_daily")
                        .select("trade_date,temperature,regime,coverage,components,method_version")
                        .eq("market", market)
                        .order("trade_date", desc=True)
                        .limit(30)
                        .execute()
                        .data or []
                    )
                    history = list(reversed(history))
                except Exception as exc:
                    print(f"{market}: Supabase history unavailable; file backup continues: {type(exc).__name__}: {exc}")

            snapshot["history"] = history
            snapshot["stored_at"] = time.time()
            row["snapshot"] = snapshot
            try:
                import cache
                cache.set_json(f"market_temperature_v3_{market}", snapshot, 18 * 3600)
            except Exception as exc:
                print(f"{market}: cache write skipped: {type(exc).__name__}: {exc}")

            parquet_file = f"market/temperature/{trade_date[:4]}/{trade_date[5:7]}/{trade_date[8:10]}.parquet"
            sidecar_path = write_cold_snapshot(snapshot, sidecar_dir, parquet_file)
            print(f"{market}: JSON cold sidecar written: {sidecar_path}")
            snapshots[market] = snapshot
            current_rows.append(row)
            print(f"{market}: {snapshot.get('temperature')} / {snapshot.get('regime')} / coverage={snapshot.get('coverage')}% / {trade_date}")
        except Exception as exc:
            print(f"{market}: snapshot/archive failed: {type(exc).__name__}: {exc}")

    # Rebuild available history from Supabase when possible, then overlay today's
    # snapshots. If Supabase is down, at least today's Parquet cold backup is kept.
    rows = []
    if client is not None:
        try:
            rows = (
                client.table("market_temperature_daily")
                .select("*")
                .order("trade_date", desc=True)
                .limit(1000)
                .execute()
                .data or []
            )
        except Exception as exc:
            print(f"Supabase history export failed; writing current file snapshots only: {type(exc).__name__}: {exc}")

    keyed = {}
    for row in rows + current_rows:
        market = row.get("market")
        day = str(row.get("trade_date") or "")[:10]
        if market in ("cn", "us") and day:
            keyed[(market, day)] = row
    grouped = {}
    for row in keyed.values():
        day = str(row.get("trade_date") or "")[:10]
        grouped.setdefault(day, []).append(row)

    for day, items in grouped.items():
        year, month, day_num = day.split("-")
        destination = output_root / "market" / "temperature" / year / month / f"{day_num}.parquet"
        destination.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(items).to_parquet(destination, index=False)
        print(f"wrote {destination} rows={len(items)}")

    if not grouped:
        print("No valid market-temperature snapshots to archive")


if __name__ == "__main__":
    main()
