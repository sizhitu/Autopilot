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


def delete_quote_cache(symbol: str) -> None:
    """删除某标的 quote 分析结果缓存（短 K 线污染时强制清掉）"""
    try:
        delete(f"quote:{str(symbol).strip().upper()}")
    except Exception:
        pass


def set_quote_cache(symbol: str, payload: dict, ttl: int = None) -> None:
    """
    缓存完整分析结果（含 chart）。
    - 默认 TTL 按交易活跃度：活跃 15min / 休市与周末 24h
    - candles 保留约 300 根（可 CACHE_QUOTE_CANDLES_MAX），供 MA120/MA250 展示
    - 同时写入远端（Supabase/Redis）与进程 L1
    """
    if ttl is None:
        ttl = quote_ttl_seconds(symbol)
    data = dict(payload) if isinstance(payload, dict) else payload
    if isinstance(data, dict) and isinstance(data.get("chart"), dict):
        ch = dict(data["chart"])
        candles = ch.get("candles")
        # 过短 chart 不入库，避免分析页长期 15 根
        if isinstance(candles, list) and len(candles) < 250:
            return
        # 历史曾截断为 80/更短，导致分析页只见十几～几十根 K 线
        max_n = int(os.getenv("CACHE_QUOTE_CANDLES_MAX", "300") or "300")
        max_n = min(max(max_n, 250), 300)  # 全站固定最多 300 根
        if isinstance(candles, list) and len(candles) > max_n:
            ch = dict(ch)
            n0 = len(candles)
            for k, v in list(ch.items()):
                if isinstance(v, list) and len(v) == n0:
                    ch[k] = v[-max_n:]
            # mas 为 {period: {data, color}}
            if isinstance(ch.get("mas"), dict):
                mas2 = {}
                for pk, pv in ch["mas"].items():
                    if isinstance(pv, dict) and isinstance(pv.get("data"), list) and len(pv["data"]) == n0:
                        mas2[pk] = {**pv, "data": pv["data"][-max_n:]}
                    else:
                        mas2[pk] = pv
                ch["mas"] = mas2
            ch["candles"] = candles[-max_n:]
            ch["count"] = len(ch["candles"])
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


ACTION_LOG_TTL = int(os.getenv("CACHE_ACTION_LOG_TTL", str(400 * 86400)))
ACTION_LOG_MAX = 260


def _action_log_from_db(symbol: str) -> list:
    """从持久表 action_logs 加载（service_role）。失败返回空列表。"""
    if not supabase_client.using_supabase():
        return []
    try:
        client = supabase_client.get_service_client()
        res = (client.table("action_logs")
               .select("bar_date,action,strength,side,timing,trend,price,reason")
               .eq("symbol", str(symbol or "").upper())
               .order("bar_date", desc=False)
               .limit(ACTION_LOG_MAX)
               .execute())
        rows = []
        for r in (res.data or []):
            rows.append({
                "date": str(r.get("bar_date") or "")[:10],
                "action": r.get("action") or "",
                "strength": int(r.get("strength") or 0),
                "side": r.get("side") or "",
                "timing": r.get("timing") or "",
                "trend": r.get("trend") or "",
                "price": r.get("price"),
                "reason": r.get("reason") or "",
            })
        return rows
    except Exception:
        return []


def _action_log_upsert_db(symbol: str, row: dict) -> None:
    """单条非观望信号写入持久表（幂等 upsert）。"""
    if not supabase_client.using_supabase():
        return
    day = str(row.get("date") or "")[:10]
    if not day or _is_watch_action(row.get("action")):
        return
    try:
        client = supabase_client.get_service_client()
        payload = {
            "symbol": str(symbol or "").upper(),
            "bar_date": day,
            "action": row.get("action") or "",
            "strength": int(row.get("strength") or 0),
            "side": row.get("side") or "",
            "timing": row.get("timing") or "",
            "trend": row.get("trend") or "",
            "price": row.get("price"),
            "reason": (row.get("reason") or "")[:120],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        client.table("action_logs").upsert(payload, on_conflict="symbol,bar_date").execute()
    except Exception:
        pass


def get_action_log(symbol: str):
    key = f"actionlog:{str(symbol or '').upper()}"
    raw = get_json(key)
    if isinstance(raw, dict) and isinstance(raw.get("rows"), list) and raw["rows"]:
        return raw["rows"]
    if isinstance(raw, list) and raw:
        return raw
    # 缓存空或未命中时回落到持久表，并回填热缓存
    db_rows = _action_log_from_db(symbol)
    if db_rows:
        try:
            set_json(key, {"rows": db_rows}, ACTION_LOG_TTL)
        except Exception:
            pass
        return db_rows
    return []


def _is_watch_action(action) -> bool:
    a = str(action or "").strip()
    return (not a) or a == "观望" or a.startswith("观望")


def append_action_log(symbol: str, row: dict) -> None:
    """只持久化非观望动作。同一天后来变成观望时，保留已存买卖，不删历史。
    热缓存 + Supabase action_logs 双写。"""
    if not symbol or not isinstance(row, dict):
        return
    day = str(row.get("date") or "")[:10]
    if not day:
        return
    existing = [r for r in (get_action_log(symbol) or []) if isinstance(r, dict)]
    if _is_watch_action(row.get("action")):
        return
    new_row = {
        "date": day,
        "action": row.get("action") or "",
        "strength": int(row.get("strength") or 0),
        "side": row.get("side") or "",
        "timing": row.get("timing") or "",
        "trend": row.get("trend") or "",
        "price": row.get("price"),
        "reason": (row.get("reason") or "")[:120],
    }
    rows = [r for r in existing if str(r.get("date") or "")[:10] != day]
    rows.append(new_row)
    rows.sort(key=lambda r: str(r.get("date") or ""))
    rows = rows[-ACTION_LOG_MAX:]
    set_json(f"actionlog:{str(symbol).upper()}", {"rows": rows}, ACTION_LOG_TTL)
    # 持久化（不依赖缓存 TTL）
    _action_log_upsert_db(symbol, new_row)


# ---------------------------------------------------------------------------
#  个股仓位建议时间轴（拥挤度三维度触发）
# ---------------------------------------------------------------------------
POS_ADVICE_TTL = 400 * 86400

def _pos_advice_from_db(symbol: str) -> list:
    try:
        import supabase_client
        if not supabase_client.using_supabase():
            return []
        client = supabase_client.get_service_client()
        rows = (client.table("position_advice_logs")
                .select("*")
                .eq("symbol", str(symbol or "").upper())
                .order("bar_date", desc=True)
                .limit(200).execute()).data or []
        return rows
    except Exception:
        return []

def get_position_advice(symbol: str) -> list:
    key = f"posadvice:{str(symbol or '').upper()}"
    raw = get_json(key)
    cached = raw.get("rows", []) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    # Supabase 是持久主存储；不能让旧的/不完整热缓存遮住数据库中的完整历史。
    db_rows = _pos_advice_from_db(symbol)
    if db_rows:
        merged = {}
        for row in cached if isinstance(cached, list) else []:
            if isinstance(row, dict):
                day = str(row.get("bar_date") or row.get("date") or "")[:10]
                if day:
                    merged[day] = row
        for row in db_rows:
            if isinstance(row, dict):
                day = str(row.get("bar_date") or row.get("date") or "")[:10]
                if day:
                    merged[day] = {**merged.get(day, {}), **{k: v for k, v in row.items() if v is not None}}
        rows = sorted(merged.values(), key=lambda r: str(r.get("bar_date") or r.get("date") or ""), reverse=True)[:200]
        try:
            set_json(key, {"rows": rows}, POS_ADVICE_TTL)
        except Exception:
            pass
        return rows
    return cached[:200] if isinstance(cached, list) else []

def append_position_advice(symbol: str, row: dict) -> None:
    if not symbol or not isinstance(row, dict):
        return
    day = str(row.get("bar_date") or row.get("date") or "")[:10]
    if not day:
        return
    sym = str(symbol).upper()
    as_of = str(row.get("as_of") or day)[:10]
    if "/" in as_of:
        for fmt in ("%m/%d/%Y", "%Y/%m/%d"):
            try:
                as_of = datetime.strptime(as_of, fmt).date().isoformat()
                break
            except ValueError:
                continue
    # 写入路径只读热缓存，避免看板每只股票都先 SELECT 一次 Supabase；
    # 持久历史由 get_position_advice() 在读取时与数据库合并。
    raw_cached = get_json(f"posadvice:{sym}")
    cached_rows = raw_cached.get("rows", []) if isinstance(raw_cached, dict) else raw_cached if isinstance(raw_cached, list) else []
    existing = [r for r in cached_rows if isinstance(r, dict)]
    # 同一天覆盖
    existing = [r for r in existing if str(r.get("bar_date") or r.get("date") or "")[:10] != day]
    new_row = {
        "symbol": sym,
        "bar_date": day,
        "level": row.get("level") or "",
        "percentile": row.get("percentile"),
        "score": row.get("score"),
        "metric_type": row.get("metric_type") or "",
        "speed": row.get("speed") or "",
        "divergence": row.get("divergence") or "",
        "advice": row.get("advice") or "",
        "reason": row.get("reason") or "",
        "source": row.get("source") or "",
        "as_of": as_of,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "short_interest": row.get("short_interest"),
        "float_shares": row.get("float_shares"),
        "short_pct_float": row.get("short_pct_float"),
        "days_to_cover": row.get("days_to_cover"),
        "price": row.get("price") or row.get("close"),
        "volume": row.get("volume"),
        "avg_volume_20": row.get("avg_volume_20"),
        "history": row.get("history"),
        "short_history": row.get("short_history"),
        "short_behavior": row.get("short_behavior"),
        "published_date": row.get("published_date"),
        "float_source": row.get("float_source"),
        "speed_change_pct": row.get("speed_change_pct"),
        "price_change_pct": row.get("price_change_pct"),
        "provider": row.get("provider"),
        "listing_exchange": row.get("listing_exchange"),
        "quality_status": row.get("quality_status") or "valid",
    }
    existing.insert(0, new_row)
    existing = existing[:200]
    try:
        set_json(f"posadvice:{sym}", {"rows": existing}, POS_ADVICE_TTL)
    except Exception:
        pass
    # 双写 Supabase
    try:
        import supabase_client
        if supabase_client.using_supabase():
            client = supabase_client.get_service_client()
            db_row = {k: v for k, v in new_row.items() if k != "symbol" or v}
            client.table("position_advice_logs").upsert(db_row, on_conflict="symbol,bar_date").execute()
    except Exception:
        pass
