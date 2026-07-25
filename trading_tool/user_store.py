"""
用户资料 / 后台统计存取层
=========================
Supabase 模式：profiles 表（受 RLS，经 service 客户端绕过 RLS 供后台读取）。
本地回退：未配置 Supabase 时返回空结果（本地开发主要验证看板/缓存，后台统计为次要）。
"""

from datetime import datetime, timedelta

import db
import supabase_client


def get_or_create_profile(uid: str, email: str = "", display_name: str = "") -> dict:
    """按 uid 取 profile；不存在则建一条（防御性）。返回 dict。"""
    if supabase_client.using_supabase():
        client = supabase_client.get_service_client()
        row = client.table("profiles").select("*").eq("id", uid).execute()
        if row.data:
            return row.data[0]
        ins = client.table("profiles").insert({
            "id": uid, "email": email,
            "display_name": display_name or (email.split("@")[0] if email else uid),
        }).execute()
        if ins.data:
            return ins.data[0]
        return {"id": uid, "email": email, "display_name": display_name, "is_admin": False}
    # 本地回退
    return {"id": uid, "email": email, "display_name": display_name, "is_admin": False}


def list_profiles(limit: int = 100, offset: int = 0) -> tuple:
    """返回 (rows, total)。"""
    if supabase_client.using_supabase():
        client = supabase_client.get_service_client()
        rows = (client.table("profiles")
                .select("id,email,display_name,is_admin,created_at")
                .order("created_at", desc=True)
                .limit(limit).offset(offset).execute()).data or []
        total = (client.table("profiles").select("id", count="exact").execute()).count or len(rows)
        out = [{
            "id": r["id"], "email": r.get("email"), "display_name": r.get("display_name"),
            "verified": True, "is_admin": bool(r.get("is_admin")),
            "created_at": r.get("created_at"), "last_login": None,
        } for r in rows]
        return out, total
    return [], 0


def user_stats() -> dict:
    """注册用户统计：总数 / 已验证 / 近7天 / 近30天。"""
    if supabase_client.using_supabase():
        client = supabase_client.get_service_client()
        total = (client.table("profiles").select("id", count="exact").execute()).count or 0
        now = datetime.now()
        d7 = (now - timedelta(days=7)).isoformat()
        d30 = (now - timedelta(days=30)).isoformat()
        recent_7 = (client.table("profiles").select("id", count="exact")
                     .gte("created_at", d7).execute()).count or 0
        recent_30 = (client.table("profiles").select("id", count="exact")
                      .gte("created_at", d30).execute()).count or 0
        return {
            "total_users": total,
            "verified_users": total,   # Supabase 需验证邮箱才能拿到会话，故已注册即视为已验证
            "recent_7d": recent_7,
            "recent_30d": recent_30,
        }
    return {"total_users": 0, "verified_users": 0, "recent_7d": 0, "recent_30d": 0}
