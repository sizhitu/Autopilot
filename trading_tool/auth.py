"""
用户认证（Supabase Auth 模式）
==============================
认证完全交给 Supabase Auth（前端 supabase-js 完成注册 / 邮箱 OTP / Magic Link）。
本模块仅负责：
  - 用 SUPABASE_JWT_SECRET（HS256）校验前端传来的 JWT，解出 sub / email / role
  - 若本地 HS256 校验失败（密钥配置错误 / 新版非对称签名），回退到 Supabase Auth API 校验
  - 回查 profiles 表补 is_admin
  - 提供 FastAPI 依赖 get_current_user / require_admin

本地开发未配置 Supabase 时，自动进入「dev token」回退模式，便于沙箱自测：
  Authorization: Bearer dev:<任意uid>  会被当作本地开发用户（is_admin=True）。
该分支仅在 using_supabase()==False 时生效，绝不进入生产路径。
"""

import os
from typing import Optional

import jwt
from fastapi import Header, HTTPException

import supabase_client

# 通过环境变量额外授予管理员权限的邮箱（逗号分隔）
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}


def is_admin(user: dict = None, uid: str = None, email: str = None) -> bool:
    """判断用户是否为管理员（profiles.is_admin 或 ADMIN_EMAILS 命中）。"""
    if user:
        uid = uid or user.get("id")
        email = email or user.get("email")
    if email and email.strip().lower() in ADMIN_EMAILS:
        return True
    if supabase_client.using_supabase() and uid:
        try:
            row = (supabase_client.get_service_client()
                   .table("profiles").select("is_admin").eq("id", uid).execute())
            if row.data:
                return bool(row.data[0].get("is_admin"))
        except Exception:
            pass
    return False


def _decode_supabase_jwt(token: str) -> dict:
    """校验 Supabase 签发的 JWT（HS256），返回 payload。失败抛 401。"""
    secret = supabase_client.get_jwt_secret()
    if not secret:
        raise HTTPException(status_code=500, detail="服务端未配置 SUPABASE_JWT_SECRET")
    try:
        # Supabase JWT 的 aud 通常为 "authenticated" 或项目 ref，这里跳过 aud 校验
        return jwt.decode(token, secret, algorithms=["HS256"], options={"verify_aud": False})
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="令牌无效")


def _user_from_auth_api(token: str) -> dict:
    """
    用 Supabase Auth API 校验 access_token（不依赖本地 JWT Secret）。
    覆盖：JWT Secret 配置错误、新版非对称签名密钥等本地 HS256 无法验证的情况。
    """
    try:
        client = supabase_client.get_user_client(token)
        # supabase-py v2: auth.get_user(jwt)
        resp = client.auth.get_user(token)
        user = getattr(resp, "user", None) or (resp.get("user") if isinstance(resp, dict) else None)
        if not user:
            raise ValueError("empty user")
        uid = getattr(user, "id", None) or (user.get("id") if isinstance(user, dict) else None)
        email = getattr(user, "email", None) or (user.get("email") if isinstance(user, dict) else None) or ""
        if not uid:
            raise ValueError("missing uid")
        return {
            "id": str(uid),
            "email": str(email).lower(),
            "is_admin": is_admin(uid=str(uid), email=str(email).lower()),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail="令牌无效或已过期") from e


def _resolve_supabase_user(token: str) -> dict:
    """先本地 JWT 校验，失败再走 Auth API 兜底。"""
    try:
        payload = _decode_supabase_jwt(token)
        uid = payload.get("sub")
        if not uid:
            raise HTTPException(status_code=401, detail="令牌缺少用户标识")
        email = (payload.get("email") or "").lower()
        return {"id": uid, "email": email, "is_admin": is_admin(uid=uid, email=email)}
    except HTTPException as e:
        # 过期仍提示过期；其它校验失败尝试 Auth API（兼容密钥/算法差异）
        if e.status_code == 401 and "过期" in str(e.detail):
            raise
        return _user_from_auth_api(token)


def _dev_user(token: str) -> dict:
    """本地开发回退：把 Bearer 内容当作 uid（dev: 前缀可省略）。"""
    uid = token[4:] if token.startswith("dev:") else token
    if not uid:
        raise HTTPException(status_code=401, detail="缺少开发令牌")
    email = f"{uid}@dev.local"
    return {"id": uid, "email": email, "is_admin": is_admin(email=email) or True}


# ----------------------------------------------------------------------
#  FastAPI 依赖：当前登录用户
# ----------------------------------------------------------------------
def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录或缺少令牌")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="令牌为空")

    if supabase_client.using_supabase():
        return _resolve_supabase_user(token)
    # 本地开发回退
    return _dev_user(token)


def require_admin(authorization: Optional[str] = Header(None)) -> dict:
    """管理员依赖：非管理员返回 403。"""
    user = get_current_user(authorization)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def get_optional_user(authorization: Optional[str] = Header(None)) -> Optional[dict]:
    """与 get_current_user 类似，但失败/缺失时返回 None（用于看板回退默认）。"""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        if supabase_client.using_supabase():
            return _resolve_supabase_user(token)
        return _dev_user(token)
    except Exception:
        return None
