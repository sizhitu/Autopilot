"""
客服工单存取层
==============
公开联系表单写入、管理员读取/回复，均为后端 service 操作（tickets 表仅 service_role 可访问）。
本地回退：直接读写 SQLite 的 tickets 表。
"""

from datetime import datetime

import db
import supabase_client


def create_ticket(name: str, email: str, country: str, message: str) -> int:
    """新建工单，返回 id。"""
    now = datetime.now().isoformat()
    if supabase_client.using_supabase():
        row = (supabase_client.get_service_client().table("tickets").insert({
            "name": name, "email": email, "country": country,
            "message": message, "status": "open", "created_at": now,
        }).execute())
        return row.data[0]["id"] if row.data else None
    conn = db.get_conn()
    with db.db_lock():
        cur = conn.execute(
            "INSERT INTO tickets(name, email, country, message, status, created_at) "
            "VALUES(?,?,?,?,'open',?)",
            (name, email, country, message, now),
        )
        conn.commit()
        return cur.lastrowid


def list_tickets(limit: int = 50) -> list:
    if supabase_client.using_supabase():
        rows = (supabase_client.get_service_client().table("tickets")
                .select("id,name,email,country,message,status,reply,created_at")
                .order("created_at", desc=True).limit(limit).execute()).data or []
        return rows
    conn = db.get_conn()
    with db.db_lock():
        rows = conn.execute(
            "SELECT id, name, email, country, message, status, reply, created_at "
            "FROM tickets ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_ticket_email(ticket_id: int):
    if supabase_client.using_supabase():
        row = (supabase_client.get_service_client().table("tickets")
               .select("email").eq("id", ticket_id).execute())
        return row.data[0]["email"] if row.data else None
    conn = db.get_conn()
    with db.db_lock():
        row = conn.execute("SELECT email FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    return row["email"] if row else None


def reply_ticket(ticket_id: int, reply: str) -> bool:
    now = datetime.now().isoformat()
    if supabase_client.using_supabase():
        (supabase_client.get_service_client().table("tickets")
         .update({"status": "replied", "reply": reply, "replied_at": now})
         .eq("id", ticket_id).execute())
        return True
    conn = db.get_conn()
    with db.db_lock():
        conn.execute(
            "UPDATE tickets SET status='replied', reply=?, replied_at=? WHERE id=?",
            (reply, now, ticket_id),
        )
        conn.commit()
    return True


def count_open() -> int:
    if supabase_client.using_supabase():
        return (supabase_client.get_service_client().table("tickets")
                .select("id", count="exact").eq("status", "open").execute()).count or 0
    conn = db.get_conn()
    with db.db_lock():
        return conn.execute("SELECT COUNT(*) AS c FROM tickets WHERE status='open'").fetchone()["c"]
