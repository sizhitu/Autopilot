#!/usr/bin/env python3
"""从默认/自选符号列表计算看板字段，写出 watchlist_latest.json（供静态降级）。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "trading_tool"))

from fetch_and_archive import load_symbols  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="输出 JSON 路径")
    ap.add_argument("--symbols-file", default="", help="符号列表文件")
    ap.add_argument("--limit", type=int, default=40, help="最多计算只数")
    ap.add_argument("--sleep", type=float, default=0.35, help="每只间隔秒")
    args = ap.parse_args()

    from watchlist import get_stock_status, _status_to_dict

    symbols = load_symbols(args.symbols_file or None)[: max(1, args.limit)]
    stocks = []
    errors = []
    t0 = time.time()
    for code, name in symbols:
        try:
            st = get_stock_status(code, name or code, days=300)
            d = _status_to_dict(st)
            d["pending"] = False
            d["data_source"] = "daily_snapshot"
            stocks.append(d)
            print(f"ok {code} action={d.get('action')} px={d.get('price')}", flush=True)
        except Exception as e:
            msg = str(e)[:120]
            errors.append({"code": code, "error": msg})
            print(f"fail {code}: {msg}", flush=True)
        time.sleep(max(0.0, args.sleep))

    payload = {
        "success": True,
        "snapshot": True,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bar_hint": "上一交易日收盘快照（GitHub Action 批算，非实时）",
        "count": len(stocks),
        "stocks": stocks,
        "errors": errors,
        "elapsed_sec": round(time.time() - t0, 1),
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out} stocks={len(stocks)} errors={len(errors)}", flush=True)
    return 0 if stocks else 1


if __name__ == "__main__":
    raise SystemExit(main())
