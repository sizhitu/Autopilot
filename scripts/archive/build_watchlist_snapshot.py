#!/usr/bin/env python3
"""从冷备 parquet（优先）+ 必要时网络，批算看板 JSON（含 by_code）。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "trading_tool"))

from fetch_and_archive import load_symbols  # noqa: E402


def load_bars_from_parquet(parquet_root: Path) -> dict[str, pd.DataFrame]:
    """读取 data/daily 与 data/monthly 下所有 parquet，按 symbol 聚合成 OHLCV。"""
    frames: list[pd.DataFrame] = []
    if not parquet_root.is_dir():
        return {}
    for sub in ("monthly", "daily", "quarterly"):
        d = parquet_root / sub
        if not d.is_dir():
            continue
        for p in d.rglob("*.parquet"):
            try:
                df = pd.read_parquet(p)
                if df is None or df.empty:
                    continue
                frames.append(df)
            except Exception as e:
                print(f"[parquet skip] {p}: {e}", flush=True)
    if not frames:
        return {}
    all_df = pd.concat(frames, ignore_index=True)
    # 列名规范化
    cols = {c.lower(): c for c in all_df.columns}
    rename = {}
    for need in ("symbol", "date", "open", "high", "low", "close", "volume"):
        if need not in all_df.columns:
            for k, orig in cols.items():
                if k == need or k.replace(" ", "") == need:
                    rename[orig] = need
                    break
    if rename:
        all_df = all_df.rename(columns=rename)
    if "symbol" not in all_df.columns or "close" not in all_df.columns:
        print("parquet missing symbol/close columns", flush=True)
        return {}
    all_df["symbol"] = all_df["symbol"].astype(str).str.strip()
    if "date" in all_df.columns:
        all_df["date"] = pd.to_datetime(all_df["date"], errors="coerce")
        all_df = all_df.dropna(subset=["date", "close"])
    out: dict[str, pd.DataFrame] = {}
    for sym, g in all_df.groupby("symbol"):
        g = g.sort_values("date").drop_duplicates(subset=["date"], keep="last")
        # 保留最多 320 根供均线/九转
        if len(g) > 320:
            g = g.tail(320)
        out[str(sym)] = g.reset_index(drop=True)
        out[str(sym).upper()] = out[str(sym)]
    print(f"parquet symbols={len(out)//2} (with upper keys)", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--symbols-file", default="")
    ap.add_argument("--parquet-root", default="", help="冷备 data 根目录（含 daily/monthly）")
    ap.add_argument("--limit", type=int, default=120)
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--network-fallback", action="store_true", default=True)
    ap.add_argument("--no-network-fallback", action="store_true")
    args = ap.parse_args()
    network = not args.no_network_fallback

    from watchlist import (
        get_stock_status,
        compute_stock_status_from_df,
        _status_to_dict,
    )

    symbols = load_symbols(args.symbols_file or None)
    pq_map: dict[str, pd.DataFrame] = {}
    if args.parquet_root:
        pq_map = load_bars_from_parquet(Path(args.parquet_root))
        # 冷备里有的代码并入符号表
        extra = []
        seen = {c.upper() for c, _ in symbols}
        for k, df in pq_map.items():
            if not k or k != k.upper():
                continue
            if k.upper() in seen:
                continue
            # 跳过重复 upper 映射产生的
            if k.isdigit() or k.isalpha() or any(ch.isdigit() for ch in k):
                nm = ""
                if "name" in df.columns and len(df):
                    try:
                        nm = str(df["name"].iloc[-1] or "")
                    except Exception:
                        nm = ""
                extra.append((k, nm))
                seen.add(k.upper())
        if extra:
            print(f"add {len(extra)} symbols from parquet", flush=True)
            symbols = symbols + extra

    symbols = symbols[: max(1, args.limit)]
    stocks = []
    by_code: dict = {}
    errors = []
    src_counts = {"parquet": 0, "network": 0}
    t0 = time.time()

    for code, name in symbols:
        try:
            df = pq_map.get(str(code))
            if df is None:
                df = pq_map.get(str(code).upper())
            st = None
            if df is not None and len(df) >= 10:
                st = compute_stock_status_from_df(code, name or code, df)
                if not st.error:
                    src_counts["parquet"] += 1
                elif network:
                    st = get_stock_status(code, name or code, days=300)
                    src_counts["network"] += 1
                else:
                    errors.append({"code": code, "error": st.error})
                    print(f"fail {code}: {st.error}", flush=True)
                    continue
            elif network:
                st = get_stock_status(code, name or code, days=300)
                src_counts["network"] += 1
            else:
                errors.append({"code": code, "error": "no parquet"})
                print(f"fail {code}: no parquet", flush=True)
                continue

            d = _status_to_dict(st)
            d["pending"] = False
            d["data_source"] = "parquet_snapshot" if (df is not None) and (len(df) >= 10) and (not st.error) else "daily_snapshot"
            if st.error and not d.get("price"):
                errors.append({"code": code, "error": st.error})
                print(f"fail {code}: {st.error}", flush=True)
                continue
            stocks.append(d)
            key = str(d.get("code") or code).strip()
            by_code[key] = d
            by_code[key.upper()] = d
            print(f"ok {code} src={d['data_source']} action={d.get('action')} px={d.get('price')}", flush=True)
        except Exception as e:
            msg = str(e)[:120]
            errors.append({"code": code, "error": msg})
            print(f"fail {code}: {msg}", flush=True)
        if network:
            time.sleep(max(0.0, args.sleep))

    payload = {
        "success": True,
        "snapshot": True,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bar_hint": "冷备 parquet 还原指标 + 必要时网络补全",
        "count": len(stocks),
        "stocks": stocks,
        "by_code": by_code,
        "errors": errors,
        "source_counts": src_counts,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"wrote {out} stocks={len(stocks)} parquet={src_counts['parquet']} "
        f"network={src_counts['network']} errors={len(errors)}",
        flush=True,
    )
    return 0 if stocks else 1


if __name__ == "__main__":
    raise SystemExit(main())
