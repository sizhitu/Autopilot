"""
用户认证与会话管理
====================
  - 密码：pbkdf2_hmac 加盐哈希，不存明文
  - 会话：随机 token 写入 sessions 表（30 天有效期），前端以
          Authorization: Bearer <token> 携带
  - 邮箱验证：6 位验证码存 users 表（5 分钟有效），由 mailer 发送
  - FastAPI 依赖 get_current_user：从请求头取 token，校验后返回用户行

不引入额外依赖（stdlib 足以），便于免费部署。
"""

import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta
from typing import Optional

from fastapi import Header, HTTPException

import db
from mailer import send_verification_email

SESSION_TTL_DAYS = 30
CODE_TTL_SECONDS = 5 * 60  # 验证码 5 分钟有效


# ----------------------------------------------------------------------
#  密码哈希
# ----------------------------------------------------------------------
def hash_password(password: str) -> str:
    """返回 'salt$iterations$hash' 形式的存储串。"""
    salt = secrets.token_bytes(16)
    iterations = 100_000
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{iterations}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        iterations_s, salt_hex, hash_hex = stored.split("$")
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(dk.hex(), hash_hex)
    except Exception:
        return False


# ----------------------------------------------------------------------
#  验证码
# ----------------------------------------------------------------------
def _gen_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def set_verify_code(email: str, code: str) -> None:
    exp = int(time.time()) + CODE_TTL_SECONDS
    conn = db.get_conn()
    with db.db_lock():
        conn.execute(
            "UPDATE users SET verify_code=?, verify_exp=? WHERE email=?",
            (code, exp, email),
        )
        conn.commit()


def check_verify_code(email: str, code: str) -> bool:
    conn = db.get_conn()
    with db.db_lock():
        row = conn.execute(
            "SELECT verify_code, verify_exp FROM users WHERE email=?", (email,)
        ).fetchone()
    if not row:
        return False
    if row["verify_exp"] and int(time.time()) > row["verify_exp"]:
        return False
    return hmac.compare_digest(row["verify_code"] or "", code)


# ----------------------------------------------------------------------
#  用户 / 会话 CRUD
# ----------------------------------------------------------------------
def get_user_by_email(email: str) -> Optional[dict]:
    conn = db.get_conn()
    with db.db_lock():
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    return dict(row) if row else None


def create_user(email: str, password: str, display_name: str = "") -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    pw_hash = hash_password(password)
    conn = db.get_conn()
    with db.db_lock():
        cur = conn.execute(
            "INSERT INTO users(email, display_name, password_hash, verified, created_at) "
            "VALUES(?,?,?,0,?)",
            (email, display_name or email.split("@")[0], pw_hash, now),
        )
        conn.commit()
        uid = cur.lastrowid
    return get_user_by_email(email)


def mark_verified(email: str) -> None:
    conn = db.get_conn()
    with db.db_lock():
        conn.execute(
            "UPDATE users SET verified=1, verify_code=NULL, verify_exp=NULL WHERE email=?",
            (email,),
        )
        conn.commit()


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    expires_at = int(time.time()) + SESSION_TTL_DAYS * 86400
    conn = db.get_conn()
    with db.db_lock():
        conn.execute(
            "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES(?,?,?,?)",
            (token, user_id, now, expires_at),
        )
        conn.commit()
    return token


def get_user_by_token(token: str) -> Optional[dict]:
    if not token:
        return None
    conn = db.get_conn()
    with db.db_lock():
        srow = conn.execute(
            "SELECT user_id, expires_at FROM sessions WHERE token=?", (token,)
        ).fetchone()
        if not srow or int(time.time()) > srow["expires_at"]:
            if srow:
                conn.execute("DELETE FROM sessions WHERE token=?", (token,))
                conn.commit()
            return None
        urow = conn.execute(
            "SELECT * FROM users WHERE id=?", (srow["user_id"],)
        ).fetchone()
    return dict(urow) if urow else None


def touch_login(user_id: int) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = db.get_conn()
    with db.db_lock():
        conn.execute("UPDATE users SET last_login=? WHERE id=?", (now, user_id))
        conn.commit()


# ----------------------------------------------------------------------
#  FastAPI 依赖：当前登录用户
# ----------------------------------------------------------------------
def get_current_user(authorization: Optional[str] = Header(None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="未登录或缺少令牌")
    token = authorization.split(" ", 1)[1].strip()
    user = get_user_by_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="令牌无效或已过期")
    return user


# ----------------------------------------------------------------------
#  业务封装：注册 / 验证 / 登录 / 重发
# ----------------------------------------------------------------------
def register(email: str, password: str, display_name: str = ""):
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="邮箱格式不正确")
    if len(password or "") < 6:
        raise HTTPException(status_code=400, detail="密码至少 6 位")
    if get_user_by_email(email):
        raise HTTPException(status_code=409, detail="该邮箱已注册")

    create_user(email, password, display_name)
    code = _gen_code()
    set_verify_code(email, code)
    sent = send_verification_email(email, code)
    return {
        "success": True,
        "email": email,
        "email_sent": bool(sent),        # 是否真实发出（未配置 SMTP 时为 False）
        "dev_code": code if not sent else None,
    }


def verify_email(email: str, code: str):
    email = (email or "").strip().lower()
    if not check_verify_code(email, code):
        raise HTTPException(status_code=400, detail="验证码错误或已过期")
    mark_verified(email)
    return {"success": True, "email": email}


def login(email: str, password: str):
    email = (email or "").strip().lower()
    user = get_user_by_email(email)
    if not user or not verify_password(password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    if not user["verified"]:
        raise HTTPException(status_code=403, detail="请先完成邮箱验证再登录")
    token = create_session(user["id"])
    touch_login(user["id"])
    return {
        "success": True,
        "token": token,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "verified": bool(user["verified"]),
        },
    }


def resend_code(email: str):
    email = (email or "").strip().lower()
    user = get_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="账号不存在")
    code = _gen_code()
    set_verify_code(email, code)
    sent = send_verification_email(email, code)
    return {
        "success": True,
        "email": email,
        "email_sent": bool(sent),
        "dev_code": code if not sent else None,
    }
