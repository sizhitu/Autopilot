"""
自选股存取层（Supabase watchlists 表，开启 RLS）
==============================================
用户态操作使用注入用户 access_token 的客户端，自动命中 RLS（auth.uid() = user_id）；
本地回退模式使用 SQLite user_watchlist 表。

对外提供：
  - get_items(uid, token)       -> [(symbol, name), ...]（按 sort_order, created_at）
  - get_all(uid, token)         -> [dict{id,symbol,name,market,note,sort_order}, ...]
  - add(uid, symbol, name, market, note, token)
  - remove(uid, symbol, token)
  - reorder(uid, ordered_symbols, token)
  - set_note(uid, symbol, note, token)
"""

import db
import supabase_client


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
def _sb_get_all(uid: str, client) -> list:
    rows = (client.table("watchlists")
            .select("id,symbol,name,market,note,sort_order")
            .eq("user_id", uid)
            .order("sort_order").order("created_at").execute()).data or []
    return rows


def _sb_add(uid: str, symbol: str, name: str, market: str, note: str, client) -> None:
    client.table("watchlists").upsert({
        "user_id": uid, "symbol": symbol, "name": name,
        "market": market, "note": note or None,
    }, on_conflict="user_id,symbol").execute()


def _sb_remove(uid: str, symbol: str, client) -> None:
    client.table("watchlists").delete().eq("user_id", uid).eq("symbol", symbol).execute()


def _sb_reorder(uid: str, ordered: list, client) -> None:
    for i, sym in enumerate(ordered):
        client.table("watchlists").update({"sort_order": i}).eq("user_id", uid).eq("symbol", sym).execute()


def _sb_set_note(uid: str, symbol: str, note: str, client) -> None:
    client.table("watchlists").update({"note": note or None}).eq("user_id", uid).eq("symbol", symbol).execute()


# ---------------------------------------------------------------------------
#  SQLite 回退实现
# ---------------------------------------------------------------------------
def _sql_get_all(uid: str) -> list:
    conn = db.get_conn()
    with db.db_lock():
        rows = conn.execute(
            "SELECT symbol, name, market, note, sort_order FROM user_watchlist "
            "WHERE user_id=? ORDER BY sort_order ASC, created_at ASC",
            (uid,),
        ).fetchall()
    out = [dict(r) for r in rows]
    for r in out:
        r.setdefault("id", None)
    return out


def _sql_add(uid: str, symbol: str, name: str, market: str, note: str) -> None:
    from datetime import datetime
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = db.get_conn()
    with db.db_lock():
        conn.execute(
            "INSERT INTO user_watchlist(user_id, symbol, name, market, note, created_at) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id, symbol) DO UPDATE SET "
            "name=excluded.name, market=excluded.market, note=excluded.note",
            (uid, symbol, name, market, note, now),
        )
        conn.commit()


def _sql_remove(uid: str, symbol: str) -> None:
    conn = db.get_conn()
    with db.db_lock():
        conn.execute("DELETE FROM user_watchlist WHERE user_id=? AND symbol=?", (uid, symbol))
        conn.commit()


def _sql_reorder(uid: str, ordered: list) -> None:
    conn = db.get_conn()
    with db.db_lock():
        for i, sym in enumerate(ordered):
            conn.execute("UPDATE user_watchlist SET sort_order=? WHERE user_id=? AND symbol=?",
                         (i, uid, sym))
        conn.commit()


def _sql_set_note(uid: str, symbol: str, note: str) -> None:
    conn = db.get_conn()
    with db.db_lock():
        conn.execute("UPDATE user_watchlist SET note=? WHERE user_id=? AND symbol=?",
                     (note, uid, symbol))
        conn.commit()


# ---------------------------------------------------------------------------
#  统一对外接口
# ---------------------------------------------------------------------------
def get_all(uid: str, access_token: str = None) -> list:
    if supabase_client.using_supabase():
        return _sb_get_all(uid, _client(access_token))
    return _sql_get_all(uid)


def get_items(uid: str, access_token: str = None) -> list:
    """兼容 watchlist.py：返回 [(symbol, name), ...]。"""
    rows = get_all(uid, access_token)
    return [(r["symbol"], r.get("name") or "") for r in rows]


def add(uid: str, symbol: str, name: str, market: str = "", note: str = "", access_token: str = None) -> bool:
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return False
    if supabase_client.using_supabase():
        _sb_add(uid, symbol, name or "", market or "", note or "", _client(access_token))
    else:
        _sql_add(uid, symbol, name or "", market or "", note or "")
    return True


def remove(uid: str, symbol: str, access_token: str = None) -> bool:
    symbol = (symbol or "").strip().upper()
    if supabase_client.using_supabase():
        _sb_remove(uid, symbol, _client(access_token))
    else:
        _sql_remove(uid, symbol)
    return True


def reorder(uid: str, ordered_symbols: list, access_token: str = None) -> bool:
    if supabase_client.using_supabase():
        _sb_reorder(uid, ordered_symbols, _client(access_token))
    else:
        _sql_reorder(uid, ordered_symbols)
    return True


def set_note(uid: str, symbol: str, note: str, access_token: str = None) -> bool:
    symbol = (symbol or "").strip().upper()
    if supabase_client.using_supabase():
        _sb_set_note(uid, symbol, note or "", _client(access_token))
    else:
        _sql_set_note(uid, symbol, note or "")
    return True


def list_all_distinct_symbols(limit: int = 500) -> list:
    """汇总全站用户自选中的去重代码（service 角色；供收盘后预热缓存）。"""
    limit = max(1, min(int(limit or 500), 2000))
    out = []
    seen = set()
    try:
        if supabase_client.using_supabase():
            client = supabase_client.get_service_client()
            # 分页拉取，避免一次过大
            start = 0
            page = 1000
            while start < 5000 and len(out) < limit:
                end = start + page - 1
                res = client.table("watchlists").select("symbol").range(start, end).execute()
                rows = getattr(res, "data", None) or []
                if not rows:
                    break
                for r in rows:
                    sym = str((r or {}).get("symbol") or "").strip().upper()
                    if not sym or sym in seen:
                        continue
                    seen.add(sym)
                    out.append(sym)
                    if len(out) >= limit:
                        break
                if len(rows) < page:
                    break
                start += page
    except Exception:
        pass
    if not out:
        try:
            conn = db.get_conn()
            with db.db_lock():
                rows = conn.execute(
                    "SELECT DISTINCT symbol FROM user_watchlist ORDER BY symbol LIMIT ?",
                    (limit,),
                ).fetchall()
            for r in rows:
                sym = str(r[0] if not isinstance(r, dict) else r.get("symbol") or "").strip().upper()
                if sym and sym not in seen:
                    seen.add(sym)
                    out.append(sym)
        except Exception:
            pass
    return out[:limit]
