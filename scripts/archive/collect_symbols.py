#!/usr/bin/env python3
"""汇总归档符号表：默认文件 + 数据仓 symbols.txt + 全站用户自选（Supabase）。

优于「每次加自选就改 git」：以 watchlists 表为实时真相源，日更/快照前合并一次。
需要环境变量（GitHub Secrets）：
  SUPABASE_URL
  SUPABASE_SERVICE_ROLE_KEY
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "trading_tool"))


def _parse_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        code = parts[0].strip()
        # A 股数字代码保持原样；美股统一大写
        if code.isdigit() or code.lower().startswith(("sh", "sz", "bj")):
            key = code
        else:
            key = code.upper()
        name = parts[1].strip() if len(parts) > 1 else ""
        if key and key not in out:
            out[key] = name
        elif key and name and not out.get(key):
            out[key] = name
    return out


def _from_supabase(limit: int = 800) -> dict[str, str]:
    out: dict[str, str] = {}
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        print("[collect] 无 SUPABASE_URL/SERVICE_ROLE_KEY，跳过用户自选并集", flush=True)
        return out
    try:
        from supabase import create_client

        client = create_client(url, key)
        start, page = 0, 1000
        while start < 8000 and len(out) < limit:
            end = start + page - 1
            res = (
                client.table("watchlists")
                .select("symbol,name")
                .range(start, end)
                .execute()
            )
            rows = getattr(res, "data", None) or []
            if not rows:
                break
            for r in rows:
                sym = str((r or {}).get("symbol") or "").strip()
                if not sym:
                    continue
                if not (sym.isdigit() or sym.lower().startswith(("sh", "sz", "bj"))):
                    sym = sym.upper()
                nm = str((r or {}).get("name") or "").strip()
                if sym not in out:
                    out[sym] = nm
                elif nm and not out[sym]:
                    out[sym] = nm
            if len(rows) < page:
                break
            start += page
        print(f"[collect] supabase watchlists distinct={len(out)}", flush=True)
    except Exception as e:
        print(f"[collect] supabase failed: {e}", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--default", default="", help="默认 symbols 文件")
    ap.add_argument("--data-repo", default="", help="Autopilot-data 根目录")
    ap.add_argument("--out", required=True, help="输出 symbols.txt")
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--write-data-repo", action="store_true", help="回写 data-repo/symbols.txt")
    args = ap.parse_args()

    merged: dict[str, str] = {}
    default_path = Path(args.default) if args.default else ROOT / "scripts/archive/symbols.default.txt"
    for p in (
        default_path,
        Path(args.data_repo) / "symbols.txt" if args.data_repo else None,
    ):
        if p is None:
            continue
        part = _parse_file(Path(p))
        for k, v in part.items():
            if k not in merged:
                merged[k] = v
            elif v and not merged[k]:
                merged[k] = v
        if part:
            print(f"[collect] file {p}: {len(part)}", flush=True)

    sb = _from_supabase(limit=max(50, args.limit))
    for k, v in sb.items():
        if k not in merged:
            merged[k] = v
        elif v and not merged[k]:
            merged[k] = v

    # 稳定排序：美股字母在前，A 股数字在后
    def sort_key(code: str):
        if code.isdigit() or code.lower().startswith(("sh", "sz", "bj")):
            return (1, code)
        return (0, code.upper())

    items = sorted(merged.items(), key=lambda kv: sort_key(kv[0]))[: max(1, args.limit)]

    lines = [
        "# 自动汇总：默认列表 + 数据仓 + 全站用户自选（Supabase watchlists）",
        "# 由 scripts/archive/collect_symbols.py 生成，勿手改后指望持久——会在日更时重写",
        "",
    ]
    for code, name in items:
        lines.append(f"{code} {name}".rstrip())

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    out.write_text(text, encoding="utf-8")
    print(f"[collect] wrote {out} count={len(items)}", flush=True)

    if args.write_data_repo and args.data_repo:
        dest = Path(args.data_repo) / "symbols.txt"
        dest.write_text(text, encoding="utf-8")
        print(f"[collect] updated {dest}", flush=True)

    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
