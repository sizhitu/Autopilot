"""
用户资料 / 后台统计存取层
=========================
Supabase 模式：profiles 表（受 RLS，经 service 客户端绕过 RLS 供后台读取）。
本地回退：未配置 Supabase 时返回空结果（本地开发主要验证看板/缓存，后台统计为次要）。
"""

from datetime import datetime, timedelta, timezone

import db
import supabase_client

# 同一用户在此窗口内多次 /api/auth/me 只计一次「登录」，只更新 last_seen
_LOGIN_SESSION_HOURS = 12


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
    return {"id": uid, "email": email, "display_name": display_name, "is_admin": False}


def _parse_ts(val):
    if not val:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    try:
        s = str(val).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def touch_profile_activity(uid: str, country: str = None) -> dict:
    """
    方案 A：更新 last_seen_at；会话窗口外再更新 last_login_at / login_count / country。
    不写入 IP 明文。列未迁移时静默失败，不影响登录主流程。
    """
    if not uid or not supabase_client.using_supabase():
        return {}
    try:
        client = supabase_client.get_service_client()
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()
        row = client.table("profiles").select(
            "id,last_login_at,last_seen_at,login_count,last_login_country"
        ).eq("id", uid).limit(1).execute()
        if not row.data:
            return {}
        cur = row.data[0]
        last_login = _parse_ts(cur.get("last_login_at"))
        count = int(cur.get("login_count") or 0)
        is_new_session = last_login is None or (now - last_login) > timedelta(hours=_LOGIN_SESSION_HOURS)
        updates = {"last_seen_at": now_iso}
        if is_new_session:
            updates["last_login_at"] = now_iso
            updates["login_count"] = count + 1
            if country:
                updates["last_login_country"] = str(country).upper()[:8]
        client.table("profiles").update(updates).eq("id", uid).execute()
        cur.update(updates)
        return cur
    except Exception:
        return {}


def list_profiles(limit: int = 100, offset: int = 0) -> tuple:
    """返回 (rows, total)。"""
    if supabase_client.using_supabase():
        client = supabase_client.get_service_client()
        try:
            rows = (client.table("profiles")
                    .select(
                        "id,email,display_name,is_admin,created_at,"
                        "last_login_at,last_seen_at,last_login_country,login_count"
                    )
                    .order("created_at", desc=True)
                    .limit(limit).offset(offset).execute()).data or []
        except Exception:
            rows = (client.table("profiles")
                    .select("id,email,display_name,is_admin,created_at")
                    .order("created_at", desc=True)
                    .limit(limit).offset(offset).execute()).data or []
        total = (client.table("profiles").select("id", count="exact").execute()).count or len(rows)
        out = [{
            "id": r["id"], "email": r.get("email"), "display_name": r.get("display_name"),
            "verified": True, "is_admin": bool(r.get("is_admin")),
            "created_at": r.get("created_at"),
            "last_login": r.get("last_login_at"),
            "last_seen": r.get("last_seen_at"),
            "last_login_country": r.get("last_login_country"),
            "login_count": int(r.get("login_count") or 0),
        } for r in rows]
        return out, total
    return [], 0


def user_stats() -> dict:
    """注册用户统计：总数 / 近7、30天注册 / 近7天活跃（last_seen）。"""
    if supabase_client.using_supabase():
        client = supabase_client.get_service_client()
        total = (client.table("profiles").select("id", count="exact").execute()).count or 0
        now = datetime.now(timezone.utc)
        d7 = (now - timedelta(days=7)).isoformat()
        d30 = (now - timedelta(days=30)).isoformat()
        recent_7 = (client.table("profiles").select("id", count="exact")
                     .gte("created_at", d7).execute()).count or 0
        recent_30 = (client.table("profiles").select("id", count="exact")
                      .gte("created_at", d30).execute()).count or 0
        active_7 = 0
        try:
            active_7 = (client.table("profiles").select("id", count="exact")
                        .gte("last_seen_at", d7).execute()).count or 0
        except Exception:
            pass
        return {
            "total_users": total,
            "verified_users": total,
            "recent_7d": recent_7,
            "recent_30d": recent_30,
            "active_7d": active_7,
        }
    return {"total_users": 0, "verified_users": 0, "recent_7d": 0, "recent_30d": 0, "active_7d": 0}


# ---------- 看板邮件推送偏好（存 settings/cache，无需改 profiles 表结构）----------
def _digest_key(uid: str) -> str:
    return f"digest.{uid}"


def get_digest_prefs(uid: str) -> dict:
    """返回 {enabled: bool, freq: 'weekly'|'biweekly', last_sent: str|None}。默认关闭、每周。"""
    import settings_store
    raw = settings_store.get_setting(_digest_key(uid), None)
    enabled = False
    freq = "weekly"
    last_sent = None
    if isinstance(raw, dict):
        enabled = bool(raw.get("enabled"))
        freq = raw.get("freq") if raw.get("freq") in ("weekly", "biweekly") else "weekly"
        last_sent = raw.get("last_sent") or None
    elif isinstance(raw, str) and raw:
        try:
            import json
            d = json.loads(raw)
            enabled = bool(d.get("enabled"))
            freq = d.get("freq") if d.get("freq") in ("weekly", "biweekly") else "weekly"
            last_sent = d.get("last_sent") or None
        except Exception:
            enabled = raw in ("1", "true", "True")
    return {"enabled": enabled, "freq": freq, "last_sent": last_sent}


def set_digest_prefs(uid: str, enabled: bool = None, freq: str = None, last_sent: str = None) -> dict:
    import settings_store, json
    cur = get_digest_prefs(uid)
    if enabled is not None:
        cur["enabled"] = bool(enabled)
    if freq in ("weekly", "biweekly"):
        cur["freq"] = freq
    if last_sent is not None:
        cur["last_sent"] = last_sent or None
    settings_store.set_setting(_digest_key(uid), json.dumps(cur, ensure_ascii=False))
    return cur


def list_digest_subscribers() -> list:
    """列出开启推送的用户 {id, email, freq, last_sent}（从 profiles + 偏好合并）。"""
    rows, _ = list_profiles(limit=10000, offset=0)
    out = []
    for r in rows:
        uid = r.get("id")
        email = (r.get("email") or "").strip()
        if not uid or not email:
            continue
        prefs = get_digest_prefs(uid)
        if not prefs.get("enabled"):
            continue
        out.append({
            "id": uid,
            "email": email,
            "freq": prefs.get("freq") or "weekly",
            "last_sent": prefs.get("last_sent"),
        })
    return out


# ---------- 订阅权益（统一 $9.9/月；已注册 grandfather = lifetime）----------
import os

# 订阅门禁：显式 0/false 关闭；1/true 开启；未设置时若已配置 Waffo 则自动开启
_br = os.getenv("BILLING_REQUIRED", "").strip().lower()
if _br in ("0", "false", "no", "off"):
    BILLING_REQUIRED = False
elif _br in ("1", "true", "yes", "on"):
    BILLING_REQUIRED = True
else:
    BILLING_REQUIRED = bool(
        os.getenv("WAFFO_MERCHANT_ID", "").strip()
        and os.getenv("WAFFO_PRIVATE_KEY", "").strip()
        and os.getenv("WAFFO_PRODUCT_ID", "").strip()
    )
PRO_PRICE_USD = float(os.getenv("PRO_PRICE_USD", "9.9") or "9.9")


def entitlement_from_profile(profile: dict = None, is_admin_user: bool = False) -> dict:
    """
    计算用户权益。
    plan: free | pro | lifetime
    entitled: 是否可使用完整功能
    """
    p = profile or {}
    plan = (p.get("plan") or "free").strip().lower()
    source = (p.get("plan_source") or "default").strip().lower()
    if is_admin_user or p.get("is_admin"):
        return {
            "plan": "lifetime" if plan == "lifetime" else plan or "pro",
            "plan_source": source or "admin",
            "entitled": True,
            "billing_required": BILLING_REQUIRED,
            "price_usd": PRO_PRICE_USD,
            "reason": "admin",
        }
    if plan == "lifetime" or source == "grandfather":
        return {
            "plan": "lifetime",
            "plan_source": source or "grandfather",
            "entitled": True,
            "billing_required": BILLING_REQUIRED,
            "price_usd": PRO_PRICE_USD,
            "reason": "grandfather",
        }
    if plan == "pro":
        exp = _parse_ts(p.get("plan_expires_at"))
        now = datetime.now(timezone.utc)
        if exp is None or exp > now:
            return {
                "plan": "pro",
                "plan_source": source or "stripe",
                "entitled": True,
                "billing_required": BILLING_REQUIRED,
                "price_usd": PRO_PRICE_USD,
                "plan_expires_at": p.get("plan_expires_at"),
                "reason": "subscription",
            }
        # 已过期
        return {
            "plan": "free",
            "plan_source": source,
            "entitled": not BILLING_REQUIRED,
            "billing_required": BILLING_REQUIRED,
            "price_usd": PRO_PRICE_USD,
            "plan_expires_at": p.get("plan_expires_at"),
            "reason": "expired",
        }
    # free：收费未开启时仍可用；开启后需订阅
    return {
        "plan": "free",
        "plan_source": source,
        "entitled": not BILLING_REQUIRED,
        "billing_required": BILLING_REQUIRED,
        "price_usd": PRO_PRICE_USD,
        "reason": "free",
    }


def is_entitled(profile: dict = None, is_admin_user: bool = False) -> bool:
    return bool(entitlement_from_profile(profile, is_admin_user).get("entitled"))


def set_plan(uid: str, plan: str, plan_source: str = "admin",
             stripe_customer_id: str = None, stripe_subscription_id: str = None,
             plan_expires_at: str = None) -> dict:
    """更新订阅字段（service role）。列不存在时静默失败。"""
    if not uid or not supabase_client.using_supabase():
        return {}
    try:
        client = supabase_client.get_service_client()
        updates = {
            "plan": plan,
            "plan_source": plan_source,
        }
        if stripe_customer_id is not None:
            updates["stripe_customer_id"] = stripe_customer_id
        if stripe_subscription_id is not None:
            updates["stripe_subscription_id"] = stripe_subscription_id
        if plan_expires_at is not None:
            updates["plan_expires_at"] = plan_expires_at
        client.table("profiles").update(updates).eq("id", uid).execute()
        row = client.table("profiles").select("*").eq("id", uid).limit(1).execute()
        return (row.data or [{}])[0]
    except Exception:
        return {}
