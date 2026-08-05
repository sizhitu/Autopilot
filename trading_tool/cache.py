"""
缓存层
======
数据分层中的「高频临时数据」层：
  - 首选 Upstash Redis（REST API）：配置 UPSTASH_REDIS_REST_URL / _TOKEN 时启用
  - 未配 Upstash 但配了 Supabase：复用 service-only 的 `cache` 表（jsonb + expires_at）
  - 本地开发（两者皆无）：进程内内存字典

缓存策略：
  - 实时行情 get_quote_cache / set_quote_cache      TTL 短（默认 300s）
  - AI 报告  get_report_cache / set_report_cache    TTL 中长（默认 6h）

注意：原始 K 线（每日行情）也走本缓存层，绝不大量写入业务库（严格分离）。
"""

import os
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import supabase_client

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").strip()
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()

# 本地内存回退（有上限，防止 Render 小实例 OOM）
_MEM: dict = {}
_MEM_TS: dict = {}
_MEM_MAX_KEYS = int(os.getenv("CACHE_MEM_MAX_KEYS", "40"))

QUOTE_TTL = int(os.getenv("CACHE_QUOTE_TTL", "180"))       # 实时行情缓存（秒）略缩短省内存
REPORT_TTL = int(os.getenv("CACHE_REPORT_TTL", "21600"))   # AI 报告缓存（秒，6h）


def _mem_prune(now: float = None) -> None:
    """清理过期项；超上限时按过期时间淘汰最旧。"""
    now = now if now is not None else time.time()
    expired = [k for k, exp in list(_MEM_TS.items()) if exp <= now]
    for k in expired:
        _MEM.pop(k, None)
        _MEM_TS.pop(k, None)
    if len(_MEM) <= _MEM_MAX_KEYS:
        return
    # 按过期时间升序淘汰（越早过期越先删）
    ordered = sorted(_MEM_TS.items(), key=lambda x: x[1])
    overflow = len(_MEM) - _MEM_MAX_KEYS
    for k, _ in ordered[:overflow]:
        _MEM.pop(k, None)
        _MEM_TS.pop(k, None)

_BACKEND = None


def _backend() -> str:
    global _BACKEND
    if _BACKEND:
        return _BACKEND
    if UPSTASH_URL and UPSTASH_TOKEN:
        _BACKEND = "upstash"
    elif supabase_client.using_supabase():
        _BACKEND = "supabase"
    else:
        _BACKEND = "memory"
    return _BACKEND


# ---------------------------------------------------------------------------
#  底层 get/set
# ---------------------------------------------------------------------------
def get_json(key: str) -> Optional[dict]:
    b = _backend()
    if b == "upstash":
        try:
            r = requests.get(f"{UPSTASH_URL}/get/{key}",
                             headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=5)
            res = r.json().get("result")
            return json.loads(res) if res else None
        except Exception:
            return None
    if b == "supabase":
        try:
            row = (supabase_client.get_service_client().table("cache")
                   .select("value,expires_at").eq("key", key).execute())
            if not row.data:
                return None
            expires = row.data[0].get("expires_at")
            if expires and datetime.fromisoformat(expires.replace("Z", "+00:00")) < datetime.now(timezone.utc):
                delete(key)
                return None
            return row.data[0]["value"]
        except Exception:
            return None
    # memory
    _mem_prune()
    if key in _MEM and (key not in _MEM_TS or _MEM_TS[key] > time.time()):
        return _MEM[key]
    _MEM.pop(key, None)
    _MEM_TS.pop(key, None)
    return None


def set_json(key: str, value: dict, ttl: int = QUOTE_TTL) -> None:
    b = _backend()
    if b == "upstash":
        try:
            requests.post(f"{UPSTASH_URL}/set",
                          json={"key": key, "value": json.dumps(value), "ttl": ttl},
                          headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=5)
        except Exception:
            pass
        return
    if b == "supabase":
        expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl)).isoformat()
        try:
            supabase_client.get_service_client().table("cache").upsert({
                "key": key, "value": value, "expires_at": expires,
            }).execute()
        except Exception:
            pass
        return
    # memory
    _MEM[key] = value
    _MEM_TS[key] = time.time() + ttl
    _mem_prune()


def delete(key: str) -> None:
    b = _backend()
    if b == "upstash":
        try:
            requests.delete(f"{UPSTASH_URL}/del/{key}",
                            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}, timeout=5)
        except Exception:
            pass
    elif b == "supabase":
        try:
            supabase_client.get_service_client().table("cache").delete().eq("key", key).execute()
        except Exception:
            pass
    else:
        _MEM.pop(key, None)
        _MEM_TS.pop(key, None)


# ---------------------------------------------------------------------------
#  业务便捷方法
# ---------------------------------------------------------------------------
def get_quote_cache(symbol: str) -> Optional[dict]:
    return get_json(f"quote:{symbol.upper()}")


def set_quote_cache(symbol: str, payload: dict, ttl: int = QUOTE_TTL) -> None:
    # 缓存去掉大体积 chart 序列，仅保留分析摘要，显著降低内存
    slim = dict(payload) if isinstance(payload, dict) else payload
    if isinstance(slim, dict) and "chart" in slim:
        slim = dict(slim)
        slim.pop("chart", None)
        slim["chart_omitted"] = True
    set_json(f"quote:{symbol.upper()}", slim, ttl)


def get_report_cache(symbol: str) -> Optional[dict]:
    return get_json(f"report:{symbol.upper()}")


def set_report_cache(symbol: str, payload: dict, ttl: int = REPORT_TTL) -> None:
    set_json(f"report:{symbol.upper()}", payload, ttl)


def get_daily_cache(symbol: str) -> Optional[list]:
    return get_json(f"daily:{symbol.upper()}")


def set_daily_cache(symbol: str, bars: list, ttl: int = REPORT_TTL) -> None:
    set_json(f"daily:{symbol.upper()}", bars, ttl)
