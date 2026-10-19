#!/usr/bin/env python3
"""把市场仓位快照写入冷备 Parquet，供后续 DuckDB 分析。

输出:
  {out}/market/spread/YYYY/MM/DD.parquet
  {out}/market/hints/YYYY/MM/DD.parquet
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "trading_tool"))

from market_spread import build_market_spread  # noqa: E402


def _num(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def _rows(payload: dict, fetched_at: str) -> tuple[list[dict], list[dict]]:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    spreads = []
    for key, market in (("cn", "cn"), ("us", "us"), ("smh", "smh")):
        row = payload.get(key) or {}
        if not row.get("ok"):
            continue
        band = row.get("band") or {}
        crowd = ((payload.get("congestion") or {}).get(market)) or {}
        spreads.append({
            "snapshot_date": day,
            "market": market,
            "name": row.get("name") or "",
            "data_date": str(row.get("date") or "")[:10],
            "pe": _num(row.get("pe")),
            "earnings_yield": _num(row.get("earnings_yield")),
            "bond_yield": _num(row.get("bond_yield")),
            "spread": _num(row.get("spread")),
            "band_mean": _num(band.get("mean")),
            "band_std": _num(band.get("std")),
            "band_upper": _num(band.get("upper")),
            "band_lower": _num(band.get("lower")),
            "band_position": band.get("position") or "",
            "crowd_value": _num(crowd.get("value")),
            "crowd_position": crowd.get("position") or "",
            "crowd_note": crowd.get("note") or "",
            "fetched_at": fetched_at,
        })
    hints = []
    for market, key in (("cn", "hint_history"), ("us", "us_hint_history")):
        for h in payload.get(key) or []:
            hints.append({
                "snapshot_date": day,
                "market": market,
                "start": str(h.get("start") or "")[:10],
                "end": str(h.get("end") or "")[:10],
                "kind": h.get("kind") or "",
                "text": h.get("text") or "",
                "value": _num(h.get("crowd")),
                "fetched_at": fetched_at,
            })
    return spreads, hints


def _write(rows: list[dict], dest: Path) -> None:
    if not rows:
        print(f"skip empty {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(dest, index=False)
    print(f"wrote {dest} rows={len(rows)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data", help="cold archive root")
    args = ap.parse_args()
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = build_market_spread()
    spreads, hints = _rows(payload, fetched_at)
    now = datetime.now(timezone.utc)
    y, m, d = now.strftime("%Y"), now.strftime("%m"), now.strftime("%d")
    root = Path(args.out)
    _write(spreads, root / "market" / "spread" / y / m / f"{d}.parquet")
    _write(hints, root / "market" / "hints" / y / m / f"{d}.parquet")


if __name__ == "__main__":
    main()
