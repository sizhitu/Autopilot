# NYSE Group consolidated short-interest archive

## What is being collected

The free public FTP directory contains historical `.xls` workbooks with four exchange-level rows: NYSE, NYSE ARCA, NYSE AMERICAN and NYSE GROUP. These are consolidated aggregates for the settlement date, not per-ticker positions. The separate NYSE Group Short Interest File product is a paid per-security feed.

Official source: https://ftp.nyse.com/NYSEGroupConsolidatedShortInterest/

## Pipeline

- Workflow: `.github/workflows/nyse-short-interest-archive.yml`
- Parser/downloader: `scripts/archive/nyse_short_interest/fetch_and_parse.py`
- Raw XLS: `data/nyse_short_interest/raw/consolidated/YYYY/MM/`
- Parquet: `data/nyse_short_interest/processed/consolidated/YYYY/MM/`
- JSON history: `data/nyse_short_interest/json/consolidated/YYYY/MM/`
- Latest API payload: `data/nyse_short_interest/latest.json`
- Manifest + SHA-256: `data/nyse_short_interest/manifests/reports.json`

The workflow polls each US business day at 22:30 UTC (after the normal 4 p.m. ET dissemination window) because publication dates can shift around weekends and holidays. It downloads only reports found in the official directory and commits only if files changed.

## API

`GET /api/short-interest/nyse/aggregate`

The API reads `data/nyse_short_interest/latest.json` from the private `sizhitu/Autopilot-data` repository. Configure the existing `DATA_REPO_TOKEN` secret as an environment variable on the Render API service as well; it needs read access to that private repository. The endpoint caches a successful response for five minutes.

Possible responses:
- `200`: latest exchange aggregate is available.
- `404`: no report has been archived yet.
- `503`: Render does not have `DATA_REPO_TOKEN`.
- `502`: cold-backup read or schema validation failed.

The response carries `scope=exchange_aggregate`; it is not a ticker lookup endpoint. The API rejects unexpected ticker-level fields.

## Local run

```bash
pip install pandas pyarrow xlrd==2.0.1
python scripts/archive/nyse_short_interest/fetch_and_parse.py --out /path/to/Autopilot-data/data
```

Use `--force` to re-download and re-parse all discovered reports.

## Important

Daily short-sale volume is a different metric and is not used here. Do not populate individual NYSE-listed symbols from these aggregate rows. Free access to download does not establish redistribution or commercial-use rights; review NYSE/ICE terms before republishing the files.
