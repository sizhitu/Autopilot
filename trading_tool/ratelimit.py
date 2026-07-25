"""
接口限流（固定窗口计数）
========================
对高频外部依赖接口（/quote /search /backtest /analyze）做限流，保护后端与外部
数据源（行情 / AI）不被单点刷爆。

后端选择：
  - 配置了 UPSTASH_REDIS_REST_URL / _TOKEN → 走 Upstash Redis（INCR + EXPIRE），
    多实例共享计数。
  - 否则 → 进程内内存字典（单实例、早期够用）。

容错原则：外部计数服务异常时「放行」（fail-open），绝不让限流把正常用户挡在门外。

对外提供：
  - limit(key, max_requests, window) -> {allowed, remaining, retry_after}
"""

import os
import time

import requests

UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "").strip()
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()

# 进程内回退存储：key -> [时间戳, ...]
_MEM: dict = {}


def _backend() -> str:
    if UPSTASH_URL and UPSTASH_TOKEN:
        return "upstash"
    return "memory"


def limit(key: str, max_requests: int = 20, window: int = 60) -> dict:
    """固定窗口计数。

    返回 dict：
      allowed     bool   是否放行
      remaining   int    本窗口剩余可用次数
      retry_after int    被限流时还需等待的秒数
    """
    if _backend() == "upstash":
        return _upstash(key, max_requests, window)
    return _memory(key, max_requests, window)


# ---------------------------------------------------------------------------
#  进程内实现
# ---------------------------------------------------------------------------
def _memory(key: str, max_requests: int, window: int) -> dict:
    now = time.time()
    lst = _MEM.get(key)
    if lst is None:
        lst = []
    # 丢弃窗口外的旧时间戳
    lst = [t for t in lst if now - t < window]
    if len(lst) >= max_requests:
        retry_after = int(window - (now - lst[0])) + 1
        return {"allowed": False, "remaining": 0, "retry_after": max(retry_after, 1)}
    lst.append(now)
    _MEM[key] = lst
    return {"allowed": True, "remaining": max(max_requests - len(lst), 0), "retry_after": 0}


# ---------------------------------------------------------------------------
#  Upstash Redis REST 实现
# ---------------------------------------------------------------------------
def _upstash(key: str, max_requests: int, window: int) -> dict:
    auth = {"Authorization": f"Bearer {UPSTASH_TOKEN}"}
    base = f"{UPSTASH_URL}"
    try:
        cnt = int(requests.post(f"{base}/incr/{key}", headers=auth, timeout=5)
                  .json().get("result", 0))
        # 仅当 key 尚无过期时间时设置窗口，避免每次请求重置 TTL
        ttl = requests.get(f"{base}/ttl/{key}", headers=auth, timeout=5).json().get("result")
        if ttl is None or int(ttl) <= 0:
            requests.post(f"{base}/expire/{key}/{window}", headers=auth, timeout=5)
        if cnt > max_requests:
            ttl_now = requests.get(f"{base}/ttl/{key}", headers=auth, timeout=5).json().get("result")
            retry = int(ttl_now) if isinstance(ttl_now, (int, float)) and int(ttl_now) > 0 else window
            return {"allowed": False, "remaining": 0, "retry_after": max(retry, 1)}
        return {"allowed": True, "remaining": max(max_requests - cnt, 0), "retry_after": 0}
    except Exception:
        # 计数服务异常 → 放行，保障可用性
        return {"allowed": True, "remaining": max_requests, "retry_after": 0}
