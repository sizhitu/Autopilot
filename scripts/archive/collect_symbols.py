#!/usr/bin/env python3
"""汇总归档符号表：默认文件 + 数据仓 symbols.txt + 全站用户自选。

数据源优先级：
  1) Supabase service_role 直读 watchlists（需 SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY）
  2) 后端 Cron API /api/cron/universe-symbols（需 DIGEST_API_BASE + CRON_SECRET）
  3) 仅文件列表（会保持约 18 只默认）

GitHub Secrets 请配置至少一组：
  - SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY（推荐）
  - 或 DIGEST_API_BASE + CRON_SECRET
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _parse_file(path: Path) -> dict:
    out = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        code = parts[0].strip()
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


def _norm_sym(sym) -> str:
    sym = str(sym or "").strip()
    if not sym:
        return ""
    if sym.isdigit() or sym.lower().startswith(("sh", "sz", "bj")):
        return sym
    return sym.upper()


def _from_supabase(limit: int = 800) -> dict:
    out = {}
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not url or not key:
        print("[collect] SKIP supabase: 未设置 SUPABASE_URL 或 SUPABASE_SERVICE_ROLE_KEY", flush=True)
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
                sym = _norm_sym((r or {}).get("symbol") or "")
                if not sym:
                    continue
                nm = str((r or {}).get("name") or "").strip()
                if sym not in out:
                    out[sym] = nm
                elif nm and not out[sym]:
                    out[sym] = nm
            if len(rows) < page:
                break
            start += page
        print(f"[collect] supabase watchlists distinct={len(out)}", flush=True)
        if not out:
            print("[collect] WARN supabase 返回 0 行（表空或密钥不对）", flush=True)
    except Exception as e:
        print(f"[collect] supabase failed: {type(e).__name__}: {e}", flush=True)
    return out


def _from_api(limit: int = 800) -> dict:
    out = {}
    base = (os.getenv("DIGEST_API_BASE") or os.getenv("API_BASE") or "").strip().rstrip("/")
    secret = (os.getenv("CRON_SECRET") or "").strip()
    if not base or not secret:
        print("[collect] SKIP api: 未设置 DIGEST_API_BASE/API_BASE 或 CRON_SECRET", flush=True)
        return out
    url = f"{base}/api/cron/universe-symbols?limit={limit}"
    try:
        req = urllib.request.Request(
            url,
            headers={"X-Cron-Secret": secret, "User-Agent": "collect-symbols"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        for s in body.get("symbols") or []:
            sym = _norm_sym(s)
            if sym and sym not in out:
                out[sym] = ""
        print(f"[collect] api universe-symbols distinct={len(out)} from {base}", flush=True)
    except Exception as e:
        print(f"[collect] api failed: {type(e).__name__}: {e}", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--default", default="")
    ap.add_argument("--data-repo", default="")
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--write-data-repo", action="store_true")
    args = ap.parse_args()

    merged = {}
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

    file_only = len(merged)
    sb = _from_supabase(limit=max(50, args.limit))
    for k, v in sb.items():
        if k not in merged:
            merged[k] = v
        elif v and not merged[k]:
            merged[k] = v

    if len(merged) <= file_only + 2:
        api = _from_api(limit=max(50, args.limit))
        for k, v in api.items():
            if k not in merged:
                merged[k] = v

    def sort_key(code: str):
        if code.isdigit() or code.lower().startswith(("sh", "sz", "bj")):
            return (1, code)
        return (0, code.upper())

    items = sorted(merged.items(), key=lambda kv: sort_key(kv[0]))[: max(1, args.limit)]

    lines = [
        "# 自动汇总：默认列表 + 数据仓 + 全站用户自选",
        "# collect_symbols.py 生成",
        "",
    ]
    for code, name in items:
        lines.append(f"{code} {name}".rstrip())

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(lines) + "\n"
    out.write_text(text, encoding="utf-8")
    print(f"[collect] wrote {out} count={len(items)} (file_base={file_only})", flush=True)

    if len(items) <= file_only + 2:
        print(
            "[collect] WARN 合并结果几乎等于文件列表，未并入用户自选。"
            "请在 GitHub Secrets 配置 SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY，"
            "或 DIGEST_API_BASE + CRON_SECRET。",
            flush=True,
        )

    if args.write_data_repo and args.data_repo:
        dest = Path(args.data_repo) / "symbols.txt"
        dest.write_text(text, encoding="utf-8")
        print(f"[collect] updated {dest}", flush=True)

    return 0 if items else 1


if __name__ == "__main__":
    raise SystemExit(main())
