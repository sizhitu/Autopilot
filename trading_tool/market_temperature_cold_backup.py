"""Bounded cold-snapshot restore for the market-temperature module.

The archive workflow writes latest_<market>.json beside daily Parquet files.
The JSON sidecar is deliberately small enough for a fast fallback response;
Parquet remains the durable historical archive.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Optional


def _valid_snapshot(payload: object, market: str) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    snap = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else payload
    if snap.get("market") != market or not snap.get("as_of"):
        return None
    if snap.get("temperature") is None and snap.get("status") != "insufficient_data":
        return None
    try:
        age = (date.today() - date.fromisoformat(str(snap["as_of"])[:10])).days
    except (TypeError, ValueError):
        return None
    # Keep outage fallback useful, but never silently display months-old values.
    if age < -1 or age > int(os.getenv("MARKET_TEMPERATURE_COLD_MAX_AGE_DAYS", "14")):
        return None
    result = dict(snap)
    result["cold_backup_trade_date"] = str(snap["as_of"])[:10]
    result["cold_backup_age_days"] = max(0, age)
    result["cold_backup_file"] = payload.get("parquet_file") if isinstance(payload, dict) else None
    return result


def read_cold_snapshot(market: str) -> Optional[dict]:
    """Read a local archive sidecar first, then a bounded remote archive sidecar."""
    if market not in ("cn", "us"):
        return None
    filename = f"latest_{market}.json"
    root = (os.getenv("MARKET_TEMPERATURE_COLD_BACKUP_DIR") or "").strip()
    if root:
        base = Path(root)
        candidates = (base / filename, base / "market" / "temperature" / filename)
        for path in candidates:
            try:
                if path.is_file() and path.stat().st_size <= 2_000_000:
                    result = _valid_snapshot(json.loads(path.read_text(encoding="utf-8")), market)
                    if result:
                        result["cold_backup_source"] = "local_file"
                        return result
            except (OSError, ValueError, TypeError):
                continue

    base_url = (os.getenv("MARKET_TEMPERATURE_COLD_BACKUP_BASE_URL") or
                "https://raw.githubusercontent.com/sizhitu/Autopilot-data/main/data/market/temperature").rstrip("/")
    token = (os.getenv("MARKET_TEMPERATURE_COLD_BACKUP_TOKEN") or os.getenv("DATA_REPO_TOKEN") or "").strip()
    try:
        import requests
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = requests.get(f"{base_url}/{filename}", headers=headers, timeout=(0.6, 1.5))
        response.raise_for_status()
        if len(response.content) > 2_000_000:
            return None
        result = _valid_snapshot(response.json(), market)
        if result:
            result["cold_backup_source"] = "remote_file"
            return result
    except Exception:
        return None
    return None
