#!/usr/bin/env python3
"""Archive NYSE Group's free exchange-level consolidated short-interest reports.

Important: these workbooks contain exchange aggregates, NOT ticker-level positions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import pandas as pd

BASE_URL = "https://ftp.nyse.com/NYSEGroupConsolidatedShortInterest/"
YEAR_RE = re.compile(r"NYSEGroupConsolidatedShortInterest(\d{4})/?$")
MONTH_RE = re.compile(r"NYSEGroupConsolidatedShortInterest(\d{6})/?$")
FILE_RE = re.compile(r"NYSE_Group_Consolidated_Short_Interest_(\d{8})\.xls$", re.I)
EXCHANGE_ALIASES = {
    "NYSE": "NYSE",
    "NYSE ARCA": "NYSE ARCA",
    "NYSE AMERICAN": "NYSE AMERICAN",
    "NYSE GROUP": "NYSE GROUP",
}
FIELDS = {
    "settlement_date": ("settlement date",),
    "exchange": ("exchange",),
    "current_short_interest": ("total current short interest",),
    "previous_short_interest": ("total previous short interest",),
    "securities_with_short_position": ("number of securities with a short position",),
    "securities_5000_plus": ("number of securities with a position", "5000"),
}


def fetch_text(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Autopilot-NYSE-Archive/1.0"})
    with urlopen(req, timeout=30) as response:
        return response.read().decode("utf-8", "replace")


def list_links(url: str) -> list[str]:
    html = fetch_text(url)
    return re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.I)


def discover_reports() -> list[tuple[str, str]]:
    """Return (publication-date string, absolute URL) for official XLS files."""
    found: list[tuple[str, str]] = []
    for year_link in list_links(BASE_URL):
        if not YEAR_RE.search(year_link):
            continue
        year_url = urljoin(BASE_URL, year_link)
        for month_link in list_links(year_url):
            if not MONTH_RE.search(month_link):
                continue
            month_url = urljoin(year_url, month_link)
            for file_link in list_links(month_url):
                match = FILE_RE.search(file_link.rsplit("/", 1)[-1])
                if match:
                    found.append((match.group(1), urljoin(month_url, file_link)))
    return sorted(set(found))


def download(url: str, destination: Path) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    req = Request(url, headers={"User-Agent": "Autopilot-NYSE-Archive/1.0"})
    with urlopen(req, timeout=60) as response:
        payload = response.read()
    if not payload or not payload.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        raise ValueError(f"Unexpected XLS payload from {url}")
    destination.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def _norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _as_int(value: object):
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "").replace("$", "")
    if not text or text in {"-", "—", "N/A"}:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def parse_workbook(path: Path, publication_date: str, source_url: str, sha256: str) -> list[dict]:
    # Header is below a title block; scan rather than assuming a fixed row number.
    book = pd.ExcelFile(path, engine="xlrd")
    output: list[dict] = []
    for sheet in book.sheet_names:
        frame = pd.read_excel(book, sheet_name=sheet, header=None, dtype=object)
        header_idx = None
        colmap: dict[str, int] = {}
        for ridx, row in frame.iterrows():
            cells = [_norm(v) for v in row.tolist()]
            found: dict[str, int] = {}
            for field, aliases in FIELDS.items():
                for cidx, cell in enumerate(cells):
                    if field == "securities_5000_plus":
                        if "number of securities" in cell and "5000" in cell.replace(",", "").replace(" ", ""):
                            found[field] = cidx
                            break
                    elif any(alias in cell for alias in aliases):
                        found[field] = cidx
                        break
            if {"settlement_date", "exchange", "current_short_interest",
                "previous_short_interest", "securities_with_short_position",
                "securities_5000_plus"}.issubset(found):
                header_idx, colmap = ridx, found
                break
        if header_idx is None:
            continue

        last_settlement = None
        for _, row in frame.iloc[header_idx + 1:].iterrows():
            exchange_raw = str(row.iloc[colmap["exchange"]]).strip().upper()
            exchange = EXCHANGE_ALIASES.get(exchange_raw)
            if not exchange:
                continue
            raw_date = row.iloc[colmap["settlement_date"]]
            settlement = pd.to_datetime(raw_date, errors="coerce")
            if pd.isna(settlement):
                # Some legacy workbooks place the date in a merged title cell.
                date_cells = [pd.to_datetime(v, errors="coerce") for v in row.tolist()]
                settlement = next((v for v in date_cells if not pd.isna(v)), pd.NaT)
            if pd.isna(settlement) and last_settlement is not None:
                settlement = last_settlement
            if pd.isna(settlement):
                continue
            last_settlement = settlement
            output.append({
                "settlement_date": settlement.date().isoformat(),
                "publication_date": datetime.strptime(publication_date, "%Y%m%d").date().isoformat(),
                "exchange": exchange,
                "current_short_interest": _as_int(row.iloc[colmap["current_short_interest"]]),
                "previous_short_interest": _as_int(row.iloc[colmap["previous_short_interest"]]),
                "securities_with_short_position": _as_int(row.iloc[colmap["securities_with_short_position"]]),
                "securities_5000_plus": _as_int(row.iloc[colmap["securities_5000_plus"]]),
                "source": "NYSE Group official consolidated report",
                "source_url": source_url,
                "source_scope": "exchange_aggregate",
                "metric_type": "short_interest_position",
                "raw_sha256": sha256,
            })
    if not output:
        raise ValueError(f"No exchange aggregate rows parsed from {path.name}; refusing to publish empty/unknown schema")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="data root inside the cold-backup repository")
    parser.add_argument("--force", action="store_true", help="re-download and re-parse existing reports")
    args = parser.parse_args()
    root = Path(args.out).resolve() / "nyse_short_interest"
    raw_root = root / "raw" / "consolidated"
    processed_root = root / "processed" / "consolidated"
    json_root = root / "json" / "consolidated"
    manifest_root = root / "manifests"
    for folder in (raw_root, processed_root, json_root, manifest_root):
        folder.mkdir(parents=True, exist_ok=True)

    reports = discover_reports()
    if not reports:
        print("No public consolidated XLS reports discovered; leaving existing archive unchanged.")
        return 0

    all_rows: list[dict] = []
    manifest = []
    for pub, url in reports:
        yyyy, mm = pub[:4], pub[4:6]
        filename = url.rsplit("/", 1)[-1]
        raw_path = raw_root / yyyy / mm / filename
        if raw_path.exists() and not args.force:
            sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        else:
            sha = download(url, raw_path)
        rows = parse_workbook(raw_path, pub, url, sha)
        all_rows.extend(rows)
        manifest.append({"publication_date": f"{yyyy}-{mm}-{pub[6:8]}",
                         "filename": filename, "source_url": url, "sha256": sha,
                         "rows": len(rows), "scope": "exchange_aggregate"})

        df = pd.DataFrame(rows).sort_values(["settlement_date", "exchange"])
        out_dir = processed_root / yyyy / mm
        out_dir.mkdir(parents=True, exist_ok=True)
        df.to_parquet(out_dir / f"{pub}.parquet", index=False)
        json_dir = json_root / yyyy / mm
        json_dir.mkdir(parents=True, exist_ok=True)
        (json_dir / f"{pub}.json").write_text(
            json.dumps({"success": True, "scope": "exchange_aggregate",
                        "publication_date": f"{yyyy}-{mm}-{pub[6:8]}",
                        "source_url": url, "rows": rows},
                       ensure_ascii=False, indent=2), encoding="utf-8")

    # The latest endpoint payload is deliberately a separate aggregate-only object.
    all_rows.sort(key=lambda x: (x["settlement_date"], x["exchange"]))
    latest_settlement = max(x["settlement_date"] for x in all_rows)
    latest_rows = [x for x in all_rows if x["settlement_date"] == latest_settlement]
    latest = {"success": True, "scope": "exchange_aggregate",
              "metric_type": "short_interest_position",
              "settlement_date": latest_settlement,
              "publication_date": max(x["publication_date"] for x in latest_rows),
              "rows": latest_rows,
              "individual_security_data": "unavailable_without_NYSE_subscription",
              "source": "NYSE Group official public consolidated reports"}
    (root / "latest.json").write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")
    (manifest_root / "reports.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Archived {len(manifest)} reports; latest settlement={latest_settlement}; rows={len(latest_rows)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"NYSE archive failed: {exc}", file=sys.stderr)
        raise
