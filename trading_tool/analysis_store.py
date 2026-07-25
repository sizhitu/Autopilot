"""
分析历史存取层（Supabase analysis_history 表，开启 RLS）
=========================================================
用户每次成功的行情分析 / CSV 分析都会落一条记录，关联 user_id + symbol，
供「我的分析历史」查看与回溯。

用户态操作使用注入用户 access_token 的客户端，自动命中 RLS（auth.uid() = user_id）；
本地回退模式使用 SQLite analysis_history 表（user_id 为 TEXT，兼容 uuid 与 dev uid）。

对外提供：
  - add(uid, symbol, name, result, access_token)   -> bool（写入一条历史）
  - list_for_user(uid, symbol, limit, offset, access_token)
        -> [dict{id, symbol, name, result_json, created_at}, ...]（按时间倒序）
"""

import json
import logging
from datetime import datetime

import db
import supabase_client


_logger = logging.getLogger("analysis_store")


def _client(access_token: str = None):
    """用户态客户端（RLS）优先；无 token 时回退 service 客户端。"""
    if supabase_client.using_supabase():
        if access_token:
            return supabase_client.get_user_client(access_token)
        return supabase_client.get_service_client()
    return None  # 本地回退走 sqlite


# ---------------------------------------------------------------------------
#  Supabase 实现
# ---------------------------------------------------------------------------
def _sb_add(uid: str, symbol: str, name: str, result: dict, client) -> None:
    client.table("analysis_history").insert({
        "user_id": uid,
        "symbol": symbol,
        "name": name or None,
        "result_json": result,
    }).execute()


def _sb_list(uid: str, symbol: str, limit: int, offset: int, client) -> list:
    q = (client.table("analysis_history")
         .select("id,symbol,name,result_json,created_at")
         .eq("user_id", uid)
         .order("created_at", desc=True)
         .limit(limit).offset(offset))
    if symbol:
        q = q.eq("symbol", symbol.upper())
    return q.execute().data or []


# ---------------------------------------------------------------------------
#  SQLite 回退实现
# ---------------------------------------------------------------------------
def _sql_add(uid: str, symbol: str, name: str, result: dict) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = db.get_conn()
    with db.db_lock():
        conn.execute(
            "INSERT INTO analysis_history(user_id, symbol, name, result_json, created_at) "
            "VALUES(?,?,?,?,?)",
            (uid, symbol, name or None, json.dumps(result, ensure_ascii=False), now),
        )
        conn.commit()


def _sql_list(uid: str, symbol: str, limit: int, offset: int) -> list:
    conn = db.get_conn()
    with db.db_lock():
        if symbol:
            rows = conn.execute(
                "SELECT id, symbol, name, result_json, created_at FROM analysis_history "
                "WHERE user_id=? AND symbol=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (uid, symbol.upper(), limit, offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, symbol, name, result_json, created_at FROM analysis_history "
                "WHERE user_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (uid, limit, offset),
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["result_json"] = json.loads(d["result_json"]) if d["result_json"] else None
        except Exception:
            pass
        out.append(d)
    return out


# ---------------------------------------------------------------------------
#  统一对外接口
# ---------------------------------------------------------------------------
def add(uid: str, symbol: str, name: str, result: dict, access_token: str = None) -> bool:
    """写入一条分析历史。失败仅记录日志、不影响主流程。返回是否写入成功。"""
    symbol = (symbol or "").strip().upper()
    if not uid or not symbol or not result:
        return False
    try:
        if supabase_client.using_supabase():
            _sb_add(uid, symbol, name or "", result, _client(access_token))
        else:
            _sql_add(uid, symbol, name or "", result)
        return True
    except Exception as e:  # 历史写入失败绝不阻断主请求
        _logger.warning("分析历史写入失败: %s", e)
        return False


def list_for_user(uid: str, symbol: str = None, limit: int = 20, offset: int = 0,
                  access_token: str = None) -> list:
    """返回某用户的分析历史（按时间倒序）；symbol 给定时按标的过滤。"""
    limit = max(1, min(int(limit or 20), 100))
    offset = max(0, int(offset or 0))
    if supabase_client.using_supabase():
        return _sb_list(uid, symbol, limit, offset, _client(access_token))
    return _sql_list(uid, symbol, limit, offset)
