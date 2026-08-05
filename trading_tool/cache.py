"""
缓存层
======
数据分层中的「高频临时数据」层：
  - 首选 Upstash Redis（REST API）：配置 UPSTASH_REDIS_REST_URL / _TOKEN 时启用
  - 未配 Upstash 但配了 Supabase：复用 service-only 的 `cache` 表（jsonb + expires_at）
  - 本地开发（两者皆无）：进程内内存字典

缓存策略：
  - 实时行情 get_quote_cache / set_quote_cache
      * 交易活跃时段：短 TTL（默认 15 分钟，可用 CACHE_QUOTE_TTL_ACTIVE 覆盖）
      * 休市/周末：长 TTL（默认 24 小时，可用 CACHE_QUOTE_TTL_IDLE 覆盖）
      * 可用 CACHE_QUOTE_TTL 强制固定秒数（调试用）
  - AI 报告  get_report_cache / set_report_cache    TTL 中长（默认 6h）

进程内另有极短 L1（默认 60s），减轻对 Supabase/Redis 的重复打点。

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

# 本地内存回退 + L1 热缓存（有上限，防止 Render 小实例 OOM）
_MEM: dict = {}
_MEM_TS: dict = {}
_MEM_MAX_KEYS = int(os.getenv("CACHE_MEM_MAX_KEYS", "40"))
_L1_TTL = int(os.getenv("CACHE_QUOTE_L1_TTL", "60"))  # 秒，进程内防抖

# 强制固定 TTL（若设置则忽略活跃/休市分流）
_QUOTE_TTL_FORCE = os.getenv("CACHE_QUOTE_TTL", "").strip()
QUOTE_TTL_ACTIVE = int(os.getenv("CACHE_QUOTE_TTL_ACTIVE", "900"))     # 15 分钟
QUOTE_TTL_IDLE = int(os.getenv("CACHE_QUOTE_TTL_IDLE", "86400"))       # 24 小时
QUOTE_TTL = QUOTE_TTL_ACTIVE  # 兼容旧引用
REPORT_TTL = int(os.getenv("CACHE_REPORT_TTL", "21600"))               # 6h


def quote_ttl_seconds(symbol: str = None) -> int:
    """
    按大致交易活跃度选择 quote 缓存时长。
    - 强制 CACHE_QUOTE_TTL：固定秒数
    - 周末 / UTC 深夜：IDLE（默认 24h）——收盘后复用同一份分析+chart
    - 工作日 UTC 01:00–21:00：ACTIVE（默认 15min）——覆盖 A 股与美股主要交易时段
    """
    if _QUOTE_TTL_FORCE.isdigit():
        return int(_QUOTE_TTL_FORCE)
    now = datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return QUOTE_TTL_IDLE
    # 粗粒度「有任一主要市场可能在交易」的窗口
    if 1 <= now.hour < 21:
        return QUOTE_TTL_ACTIVE
    return QUOTE_TTL_IDLE


def _mem_prune(now: float = None) -> None:
    """清理过期项；超上限时按过期时间淘汰最旧。"""
    now = now if now is not None else time.time()
    expired = [k for k, exp in list(_MEM_TS.items()) if exp <= now]
    for k in expired:
        _MEM.pop(k, None)
        _MEM_TS.pop(k, None)
    if len(_MEM) <= _MEM_MAX_KEYS:
        return
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


def cache_backend_name() -> str:
    return _backend()


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
                   .select("value,expires_at").eq("key", key).limit(1).execute())
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
            # Upstash REST: SET key value EX ttl
            requests.post(
                f"{UPSTASH_URL}/set/{key}/{json.dumps(value)}",
                headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
                params={"EX": ttl},
                timeout=5,
            )
        except Exception:
            try:
                # 兼容部分 Upstash 路径写法
                requests.post(
                    f"{UPSTASH_URL}/set/{key}",
                    headers={"Authorization": f"Bearer {UPSTASH_TOKEN}",
                             "Content-Type": "application/json"},
                    json={"value": json.dumps(value), "ex": ttl},
                    timeout=5,
                )
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


def _l1_get(key: str):
    _mem_prune()
    if key in _MEM and _MEM_TS.get(key, 0) > time.time():
        return _MEM[key]
    return None


def _l1_set(key: str, value: dict, ttl: int = _L1_TTL) -> None:
    _MEM[key] = value
    _MEM_TS[key] = time.time() + max(1, ttl)
    _mem_prune()


# ---------------------------------------------------------------------------
#  业务便捷方法
# ---------------------------------------------------------------------------
def get_quote_cache(symbol: str) -> Optional[dict]:
    key = f"quote:{symbol.upper()}"
    hit = _l1_get(key)
    if hit is not None:
        return hit
    val = get_json(key)
    if val is not None:
        _l1_set(key, val, _L1_TTL)
    return val


def set_quote_cache(symbol: str, payload: dict, ttl: int = None) -> None:
    """
    缓存完整分析结果（含 chart）。
    - 默认 TTL 按交易活跃度：活跃 15min / 休市与周末 24h
    - candles 最多保留 80 根以控制体积
    - 同时写入远端（Supabase/Redis）与进程 L1
    """
    if ttl is None:
        ttl = quote_ttl_seconds(symbol)
    data = dict(payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict) and isinstance(data.get("chart"), dict):
        ch = dict(data["chart"])
        candles = ch.get("candles")
        max_n = 80
        if isinstance(candles, list) and len(candles) > max_n:
            ch = dict(ch)
            for k, v in list(ch.items()):
                if isinstance(v, list) and len(v) == len(candles):
                    ch[k] = v[-max_n:]
            ch["candles"] = candles[-max_n:]
        data = dict(data)
        data["chart"] = ch
        data.pop("chart_omitted", None)
        data["cache_ttl_sec"] = ttl
        data["cache_backend"] = _backend()
    key = f"quote:{symbol.upper()}"
    set_json(key, data, ttl)
    _l1_set(key, data, min(_L1_TTL, ttl))


def get_report_cache(symbol: str) -> Optional[dict]:
    return get_json(f"report:{symbol.upper()}")


def set_report_cache(symbol: str, payload: dict, ttl: int = REPORT_TTL) -> None:
    set_json(f"report:{symbol.upper()}", payload, ttl)


def get_daily_cache(symbol: str) -> Optional[list]:
    return get_json(f"daily:{symbol.upper()}")


def set_daily_cache(symbol: str, bars: list, ttl: int = REPORT_TTL) -> None:
    set_json(f"daily:{symbol.upper()}", bars, ttl)
