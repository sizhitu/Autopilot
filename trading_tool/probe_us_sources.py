#!/usr/bin/env python3
"""独立模块：美股日线源质量探测。

免 Key：Yahoo / Nasdaq / Stooq
有 Key 才打：Finnhub / Tiingo / EODHD / Twelve Data
  FINNHUB_API_KEY  TIINGO_API_KEY  EODHD_API_KEY  TWELVE_DATA_API_KEY

产品需求：看板现价+日涨幅；分析页约 300 根 OHLCV。不依赖线上 fetcher。
"""
from __future__ import annotations

import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Any

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)

# 产品侧需要的字段
NEEDED = ("date", "open", "high", "low", "close", "volume")

# 自选里常见 + 流动性对照
SYMBOLS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOG", "META", "TSLA", "AVGO",
    "JPM", "V", "UNH", "XOM", "JNJ", "WMT", "MA", "PG",
    "HD", "CVX", "KO", "PEP", "ABBV", "COST", "MRK", "BAC",
    "NFLX", "AMD", "ADBE", "CRM", "ORCL", "CSCO",
    "SPY", "QQQ", "VTI", "IWM", "SMH", "GLD", "SLV", "USO",
    "EQT", "CAT", "CEG", "CF", "CRDO", "ETN", "ICE", "JEPI",
    "PLTR", "OUST", "ASTS", "POWL", "CBT", "MU", "FCX", "BE",
    "BRK-B", "SPCX",
]

# 可接受度（针对本产品，不是交易席位）
SPEC = {
    "bars_ok": 250,          # 分析页/MA250
    "bars_min_board": 10,    # 看板现价+涨跌
    "latency_ok_s": 3.0,     # 单只可接受
    "latency_slow_s": 8.0,   # 超过则体验差
    "price_agree_pct": 1.0,  # 多源收盘价相对中位数偏差
    "fresh_grace_days": 1,   # 休市后允许落后 1 个交易日
}


def expected_us_session_date() -> str:
    now = datetime.now(timezone.utc)
    # 美东约 UTC-4；收盘 20:00 UTC 前后
    d = now.date()
    if now.hour < 21:
        d = (now - timedelta(days=1)).date()
    # 周末回退到周五
    while d.weekday() > 4:
        d = d - timedelta(days=1)
    return d.isoformat()


def _sess() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json,text/csv,*/*"})
    return s


def _rows_from_ohlc(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"ok": False, "bars": 0}
    rows = sorted(rows, key=lambda r: r["date"])
    last = rows[-1]
    prev = rows[-2] if len(rows) >= 2 else None
    chg = None
    if prev and prev["close"]:
        chg = round((last["close"] - prev["close"]) / prev["close"] * 100, 4)
    return {
        "ok": True,
        "bars": len(rows),
        "first": rows[0]["date"],
        "last": last["date"],
        "close": float(last["close"]),
        "open": last.get("open"),
        "high": last.get("high"),
        "low": last.get("low"),
        "volume": last.get("volume"),
        "chg_1d": chg,
        "fields": {k: last.get(k) is not None for k in NEEDED},
    }


def fetch_yahoo(symbol: str, timeout: float = 8.0) -> dict[str, Any]:
    t0 = time.time()
    ysym = symbol.replace(".", "-")
    end = int(time.time())
    start = end - 460 * 86400
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ysym}"
    try:
        r = _sess().get(
            url,
            params={"period1": start, "period2": end, "interval": "1d"},
            timeout=timeout,
        )
        ms = time.time() - t0
        if r.status_code != 200:
            return {"source": "yahoo", "symbol": symbol, "ok": False, "latency_s": ms, "error": f"HTTP {r.status_code}"}
        data = r.json().get("chart", {}).get("result") or []
        if not data:
            err = (r.json().get("chart") or {}).get("error") or {}
            return {"source": "yahoo", "symbol": symbol, "ok": False, "latency_s": ms, "error": err.get("description") or "no result"}
        block = data[0]
        ts = block.get("timestamp") or []
        q = (block.get("indicators") or {}).get("quote") or [{}]
        q0 = q[0] if q else {}
        adj = ((block.get("indicators") or {}).get("adjclose") or [{}])
        adjs = (adj[0].get("adjclose") if adj else None) or q0.get("close") or []
        meta = block.get("meta") or {}
        rmp = meta.get("regularMarketPrice")
        rows = []
        for i, t in enumerate(ts):
            c = None
            if i < len(adjs) and adjs[i] not in (None,):
                c = adjs[i]
            elif i < len(q0.get("close") or []) and q0["close"][i] not in (None,):
                c = q0["close"][i]
            if c is None and i == len(ts) - 1 and rmp:
                c = rmp
            if c is None:
                continue
            rows.append({
                "date": datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"),
                "open": (q0.get("open") or [None] * len(ts))[i] if i < len(q0.get("open") or []) else c,
                "high": (q0.get("high") or [None] * len(ts))[i] if i < len(q0.get("high") or []) else c,
                "low": (q0.get("low") or [None] * len(ts))[i] if i < len(q0.get("low") or []) else c,
                "close": float(c),
                "volume": (q0.get("volume") or [0] * len(ts))[i] if i < len(q0.get("volume") or []) else 0,
            })
        out = _rows_from_ohlc(rows)
        out.update({"source": "yahoo", "symbol": symbol, "latency_s": ms, "error": None})
        return out
    except Exception as e:
        return {"source": "yahoo", "symbol": symbol, "ok": False, "latency_s": time.time() - t0, "error": str(e)[:120]}


def fetch_nasdaq(symbol: str, timeout: float = 10.0) -> dict[str, Any]:
    t0 = time.time()
    ysym = symbol.replace("-", ".")
    end = datetime.utcnow().date()
    start = end - timedelta(days=400)
    headers = {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": "https://www.nasdaq.com",
        "Referer": f"https://www.nasdaq.com/market-activity/stocks/{ysym.lower()}/historical",
    }
    last_err = None
    for asset in ("stocks", "etf", "fund", "index"):
        url = f"https://api.nasdaq.com/api/quote/{ysym}/historical"
        try:
            r = _sess().get(
                url,
                params={"assetclass": asset, "fromdate": start.isoformat(), "todate": end.isoformat()},
                headers=headers,
                timeout=timeout,
            )
            ms = time.time() - t0
            if r.status_code != 200:
                last_err = f"HTTP {r.status_code}/{asset}"
                continue
            d = r.json()
            rows_raw = (((d.get("data") or {}).get("tradesTable") or {}).get("rows")) or []
            if not rows_raw:
                last_err = f"empty/{asset}"
                continue

            def num(x):
                if x is None:
                    return None
                return float(str(x).replace("$", "").replace(",", "").strip() or 0)

            rows = []
            for row in rows_raw:
                try:
                    dt = datetime.strptime(row["date"], "%m/%d/%Y").strftime("%Y-%m-%d")
                    rows.append({
                        "date": dt,
                        "open": num(row.get("open")),
                        "high": num(row.get("high")),
                        "low": num(row.get("low")),
                        "close": num(row.get("close")),
                        "volume": num(row.get("volume")),
                    })
                except Exception:
                    continue
            out = _rows_from_ohlc(rows)
            out.update({"source": "nasdaq", "symbol": symbol, "latency_s": ms, "error": None, "assetclass": asset})
            return out
        except Exception as e:
            last_err = str(e)[:80]
            continue
    return {"source": "nasdaq", "symbol": symbol, "ok": False, "latency_s": time.time() - t0, "error": last_err or "fail"}


def fetch_stooq(symbol: str, timeout: float = 8.0) -> dict[str, Any]:
    t0 = time.time()
    ysym = symbol.replace(".", "-").upper()
    st = ysym.lower()
    if not st.startswith("^"):
        st = st + ".us"
    url = f"https://stooq.com/q/d/l/?s={st}&i=d"
    try:
        r = _sess().get(url, timeout=timeout, headers={"User-Agent": UA})
        ms = time.time() - t0
        if r.status_code != 200 or r.text.lstrip().lower().startswith("<!doctype"):
            return {"source": "stooq", "symbol": symbol, "ok": False, "latency_s": ms, "error": f"HTTP {r.status_code}"}
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        if len(lines) < 2 or "Date" not in lines[0]:
            return {"source": "stooq", "symbol": symbol, "ok": False, "latency_s": ms, "error": "not csv"}
        rows = []
        for ln in lines[1:]:
            parts = ln.split(",")
            if len(parts) < 6:
                continue
            try:
                c = float(parts[4])
            except Exception:
                continue
            rows.append({
                "date": parts[0],
                "open": float(parts[1]) if parts[1] not in ("", "null") else c,
                "high": float(parts[2]) if parts[2] not in ("", "null") else c,
                "low": float(parts[3]) if parts[3] not in ("", "null") else c,
                "close": c,
                "volume": float(parts[5]) if parts[5] not in ("", "null") else 0,
            })
        # 只要最近约 400 根作对比
        rows = rows[-400:]
        out = _rows_from_ohlc(rows)
        out.update({"source": "stooq", "symbol": symbol, "latency_s": ms, "error": None})
        return out
    except Exception as e:
        return {"source": "stooq", "symbol": symbol, "ok": False, "latency_s": time.time() - t0, "error": str(e)[:120]}



def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def fetch_finnhub(symbol: str, timeout: float = 8.0) -> dict[str, Any]:
    token = _env("FINNHUB_API_KEY")
    if not token:
        return {"source": "finnhub", "symbol": symbol, "ok": False, "skipped": True, "error": "no FINNHUB_API_KEY"}
    t0 = time.time()
    ysym = symbol.replace(".", "-")
    now = int(time.time())
    try:
        r = _sess().get(
            "https://finnhub.io/api/v1/stock/candle",
            params={"symbol": ysym, "resolution": "D", "from": now - 500 * 86400, "to": now, "token": token},
            timeout=timeout,
        )
        ms = time.time() - t0
        if r.status_code != 200:
            return {"source": "finnhub", "symbol": symbol, "ok": False, "latency_s": ms, "error": f"HTTP {r.status_code} {r.text[:80]}"}
        d = r.json()
        if d.get("s") != "ok":
            return {"source": "finnhub", "symbol": symbol, "ok": False, "latency_s": ms, "error": d.get("s") or "no data"}
        ts, o, h, l, c, v = d.get("t") or [], d.get("o") or [], d.get("h") or [], d.get("l") or [], d.get("c") or [], d.get("v") or []
        rows = []
        for i, tsv in enumerate(ts):
            if i >= len(c) or c[i] is None:
                continue
            rows.append({
                "date": datetime.utcfromtimestamp(tsv).strftime("%Y-%m-%d"),
                "open": o[i] if i < len(o) else c[i],
                "high": h[i] if i < len(h) else c[i],
                "low": l[i] if i < len(l) else c[i],
                "close": float(c[i]),
                "volume": v[i] if i < len(v) else 0,
            })
        out = _rows_from_ohlc(rows)
        out.update({"source": "finnhub", "symbol": symbol, "latency_s": ms, "error": None})
        return out
    except Exception as e:
        return {"source": "finnhub", "symbol": symbol, "ok": False, "latency_s": time.time() - t0, "error": str(e)[:120]}


def fetch_tiingo(symbol: str, timeout: float = 8.0) -> dict[str, Any]:
    token = _env("TIINGO_API_KEY")
    if not token:
        return {"source": "tiingo", "symbol": symbol, "ok": False, "skipped": True, "error": "no TIINGO_API_KEY"}
    t0 = time.time()
    ysym = symbol.replace(".", "-")
    start = (datetime.utcnow().date() - timedelta(days=500)).isoformat()
    try:
        r = _sess().get(
            f"https://api.tiingo.com/tiingo/daily/{ysym}/prices",
            params={"startDate": start, "token": token},
            headers={"Content-Type": "application/json"},
            timeout=timeout,
        )
        ms = time.time() - t0
        if r.status_code != 200:
            return {"source": "tiingo", "symbol": symbol, "ok": False, "latency_s": ms, "error": f"HTTP {r.status_code} {r.text[:80]}"}
        arr = r.json()
        if not isinstance(arr, list):
            return {"source": "tiingo", "symbol": symbol, "ok": False, "latency_s": ms, "error": str(arr)[:80]}
        rows = []
        for it in arr:
            ds = str(it.get("date") or "")[:10]
            c = it.get("adjClose") if it.get("adjClose") is not None else it.get("close")
            if not ds or c is None:
                continue
            rows.append({
                "date": ds,
                "open": it.get("adjOpen") if it.get("adjOpen") is not None else it.get("open") or c,
                "high": it.get("adjHigh") if it.get("adjHigh") is not None else it.get("high") or c,
                "low": it.get("adjLow") if it.get("adjLow") is not None else it.get("low") or c,
                "close": float(c),
                "volume": it.get("volume") or 0,
            })
        out = _rows_from_ohlc(rows)
        out.update({"source": "tiingo", "symbol": symbol, "latency_s": ms, "error": None})
        return out
    except Exception as e:
        return {"source": "tiingo", "symbol": symbol, "ok": False, "latency_s": time.time() - t0, "error": str(e)[:120]}


def fetch_eodhd(symbol: str, timeout: float = 8.0) -> dict[str, Any]:
    token = _env("EODHD_API_KEY")
    if not token:
        return {"source": "eodhd", "symbol": symbol, "ok": False, "skipped": True, "error": "no EODHD_API_KEY"}
    t0 = time.time()
    ysym = symbol.replace(".", "-") + ".US"
    start = (datetime.utcnow().date() - timedelta(days=500)).isoformat()
    try:
        r = _sess().get(
            f"https://eodhd.com/api/eod/{ysym}",
            params={"api_token": token, "fmt": "json", "from": start, "period": "d"},
            timeout=timeout,
        )
        ms = time.time() - t0
        if r.status_code != 200:
            return {"source": "eodhd", "symbol": symbol, "ok": False, "latency_s": ms, "error": f"HTTP {r.status_code} {r.text[:80]}"}
        arr = r.json()
        if not isinstance(arr, list):
            return {"source": "eodhd", "symbol": symbol, "ok": False, "latency_s": ms, "error": str(arr)[:80]}
        rows = []
        for it in arr:
            ds = str(it.get("date") or "")[:10]
            c = it.get("adjusted_close") if it.get("adjusted_close") is not None else it.get("close")
            if not ds or c is None:
                continue
            rows.append({
                "date": ds,
                "open": it.get("open") or c,
                "high": it.get("high") or c,
                "low": it.get("low") or c,
                "close": float(c),
                "volume": it.get("volume") or 0,
            })
        out = _rows_from_ohlc(rows)
        out.update({"source": "eodhd", "symbol": symbol, "latency_s": ms, "error": None})
        return out
    except Exception as e:
        return {"source": "eodhd", "symbol": symbol, "ok": False, "latency_s": time.time() - t0, "error": str(e)[:120]}


def fetch_twelvedata(symbol: str, timeout: float = 8.0) -> dict[str, Any]:
    token = _env("TWELVE_DATA_API_KEY")
    if not token:
        return {"source": "twelvedata", "symbol": symbol, "ok": False, "skipped": True, "error": "no TWELVE_DATA_API_KEY"}
    t0 = time.time()
    ysym = symbol.replace(".", "-")
    try:
        r = _sess().get(
            "https://api.twelvedata.com/time_series",
            params={"symbol": ysym, "interval": "1day", "outputsize": 300, "apikey": token},
            timeout=timeout,
        )
        ms = time.time() - t0
        if r.status_code != 200:
            return {"source": "twelvedata", "symbol": symbol, "ok": False, "latency_s": ms, "error": f"HTTP {r.status_code} {r.text[:80]}"}
        d = r.json()
        if d.get("status") == "error":
            return {"source": "twelvedata", "symbol": symbol, "ok": False, "latency_s": ms, "error": d.get("message") or "error"}
        vals = d.get("values") or []
        rows = []
        for it in vals:
            ds = str(it.get("datetime") or "")[:10]
            try:
                c = float(it.get("close"))
            except Exception:
                continue
            rows.append({
                "date": ds,
                "open": float(it.get("open") or c),
                "high": float(it.get("high") or c),
                "low": float(it.get("low") or c),
                "close": c,
                "volume": float(it.get("volume") or 0),
            })
        out = _rows_from_ohlc(rows)
        out.update({"source": "twelvedata", "symbol": symbol, "latency_s": ms, "error": None})
        return out
    except Exception as e:
        return {"source": "twelvedata", "symbol": symbol, "ok": False, "latency_s": time.time() - t0, "error": str(e)[:120]}


SOURCE_META = {
    "yahoo": {"kind": "indirect", "key": None, "role": "长K（限流不稳定）", "rep": "用得最多，无SLA"},
    "nasdaq": {"kind": "indirect", "key": None, "role": "现价兜底", "rep": "官方页，历史常仅约15根"},
    "stooq": {"kind": "indirect", "key": None, "role": "冷备日线", "rep": "量化入门常用，复权口径偶发偏差"},
    "finnhub": {"kind": "direct", "key": "FINNHUB_API_KEY", "role": "现价/报价", "rep": "免费档口碑最好，历史K线弱"},
    "tiingo": {"kind": "direct", "key": "TIINGO_API_KEY", "role": "日线主库", "rep": "EOD干净，回测圈评价高"},
    "eodhd": {"kind": "direct", "key": "EODHD_API_KEY", "role": "全球日线", "rep": "覆盖广、入门付费便宜"},
    "twelvedata": {"kind": "direct", "key": "TWELVE_DATA_API_KEY", "role": "多资产时序", "rep": "接口整齐，免费超额可能返回旧数"},
}

FETCHERS = {
    "yahoo": fetch_yahoo,
    "nasdaq": fetch_nasdaq,
    "stooq": fetch_stooq,
    "finnhub": fetch_finnhub,
    "tiingo": fetch_tiingo,
    "eodhd": fetch_eodhd,
    "twelvedata": fetch_twelvedata,
}


def active_fetchers() -> dict:
    """无 Key 的直连源跳过，避免把 skipped 算进失败率。"""
    out = {}
    for name, fn in FETCHERS.items():
        meta = SOURCE_META.get(name) or {}
        key = meta.get("key")
        if key and not _env(key):
            continue
        out[name] = fn
    return out



def run_probe(symbols=None) -> dict[str, Any]:
    symbols = symbols or SYMBOLS
    exp = expected_us_session_date()
    results = []
    fetchers = active_fetchers()

    def one(src, sym):
        return fetchers[src](sym)

    jobs = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        for src in fetchers:
            for sym in symbols:
                jobs.append(ex.submit(one, src, sym))
        for fut in as_completed(jobs):
            results.append(fut.result())

    by = {}
    for row in results:
        by.setdefault(row["symbol"], {})[row["source"]] = row

    summary_rows = []
    src_stats = {s: {"ok": 0, "bars_ok": 0, "fresh": 0, "lat": []} for s in fetchers}
    agree_n = agree_ok = 0

    for sym in symbols:
        pack = by.get(sym, {})
        closes = []
        rec = {"symbol": sym}
        for src in fetchers:
            r = pack.get(src) or {"ok": False, "error": "missing"}
            rec[f"{src}_ok"] = bool(r.get("ok"))
            rec[f"{src}_bars"] = r.get("bars")
            rec[f"{src}_last"] = r.get("last")
            rec[f"{src}_close"] = r.get("close")
            rec[f"{src}_lat"] = round(r.get("latency_s") or 0, 3)
            rec[f"{src}_err"] = r.get("error")
            if r.get("ok"):
                src_stats[src]["ok"] += 1
                src_stats[src]["lat"].append(r.get("latency_s") or 0)
                if (r.get("bars") or 0) >= SPEC["bars_ok"]:
                    src_stats[src]["bars_ok"] += 1
                if r.get("last") and r["last"] >= exp:
                    src_stats[src]["fresh"] += 1
                if r.get("close"):
                    closes.append(r["close"])
        if len(closes) >= 2:
            agree_n += 1
            mid = sorted(closes)[len(closes) // 2]
            rec["agree"] = all(abs(c - mid) / mid * 100 <= SPEC["price_agree_pct"] for c in closes if mid)
            if rec["agree"]:
                agree_ok += 1
        else:
            rec["agree"] = None
        summary_rows.append(rec)

    n = len(symbols)
    report = {
        "expected_session": exp,
        "n_symbols": n,
        "spec": SPEC,
        "active_sources": list(fetchers),
        "skipped_sources": [k for k in FETCHERS if k not in fetchers],
        "source_meta": SOURCE_META,
        "sources": {},
        "price_agree_among_ok": {"compared": agree_n, "within_1pct": agree_ok},
        "rows": summary_rows,
    }
    for src, st in src_stats.items():
        lats = st["lat"] or [0]
        report["sources"][src] = {
            "success": st["ok"],
            "success_pct": round(st["ok"] / n * 100, 1),
            "bars_ge_250": st["bars_ok"],
            "bars_ge_250_pct": round(st["bars_ok"] / n * 100, 1),
            "fresh_last_ge_expected": st["fresh"],
            "fresh_pct": round(st["fresh"] / n * 100, 1),
            "latency_avg_s": round(sum(lats) / len(lats), 3) if lats else None,
            "latency_p95_s": round(sorted(lats)[max(0, int(len(lats) * 0.95) - 1)], 3) if lats else None,
        }
    return report


def main():
    print("probing", len(SYMBOLS), "symbols x", list(active_fetchers()), "skipped", [k for k in FETCHERS if k not in active_fetchers()], flush=True)
    report = run_probe()
    out_json = "/home/workdir/artifacts/us_source_probe.json"
    out_csv = "/home/workdir/artifacts/us_source_probe.csv"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    keys = ["symbol", "agree"]
    for src in report.get("active_sources") or FETCHERS:
        keys += [f"{src}_ok", f"{src}_bars", f"{src}_last", f"{src}_close", f"{src}_lat", f"{src}_err"]
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(report["rows"])
    print(json.dumps({k: report[k] for k in ("expected_session", "n_symbols", "sources", "price_agree_among_ok")}, indent=2))
    print("wrote", out_json, out_csv)


if __name__ == "__main__":
    main()
