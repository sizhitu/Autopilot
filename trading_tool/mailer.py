"""
邮件发送
========
生产环境：优先走 Resend REST API（RESEND_API_KEY），规避 Render 免费层 SMTP 端口限制。
未配置 Resend 时：回退到 SMTP（配置来源 = settings_store：后台可配 或 环境变量）。

能力：
  - send_verification_email  注册验证码（Supabase Auth 负责发，这里保留以便需要时调用）
  - send_email               通用发信（EDM / 工单通知）
  - send_ticket_notification 把用户提交的咨询工单转发到 support 邮箱（自动建单）
  - send_edm                 群发 EDM，返回成功数
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

import requests

import settings_store

SITE_NAME = os.getenv("SITE_NAME", "Autopilot 投资分析")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@timebricks.bid")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
RESEND_API_URL = "https://api.resend.com/emails"


# ----------------------------------------------------------------------
#  配置解析：SMTP（仅 Resend 未配置时作为回退）
# ----------------------------------------------------------------------
def _smtp_config() -> dict:
    def g(key, env):
        v = settings_store.get_setting(key)
        return v if v is not None else os.getenv(env)

    host = g("smtp_host", "SMTP_HOST")
    port = g("smtp_port", "SMTP_PORT") or "465"
    user = g("smtp_user", "SMTP_USER")
    pw = g("smtp_pass", "SMTP_PASS")
    frm = g("smtp_from", "SMTP_FROM") or user
    tls = (g("smtp_tls", "SMTP_TLS") or "true").lower() == "true"
    return {"host": host, "port": int(port or 465), "user": user, "pass": pw,
            "from": frm, "tls": tls}


def _send_resend(to_email: str, subject: str, body: str, html: str = None) -> bool:
    """通过 Resend API 发信。成功返回 True。"""
    if not RESEND_API_KEY:
        return False
    payload = {
        "from": f"{SITE_NAME} <{SUPPORT_EMAIL}>",
        "to": [to_email],
        "subject": subject,
        "text": body,
    }
    if html:
        payload["html"] = html
    try:
        r = requests.post(
            RESEND_API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {RESEND_API_KEY}",
                     "Content-Type": "application/json"},
            timeout=15,
        )
        if r.status_code in (200, 201):
            return True
        print(f"[RESEND-ERROR] {r.status_code} {r.text}")
        return False
    except Exception as e:
        print(f"[RESEND-ERROR] 发送失败 to={to_email}: {e}")
        return False


def _send_smtp(to_email: str, subject: str, body: str, html: str = None) -> bool:
    cfg = _smtp_config()
    if not cfg["host"]:
        print(f"[DEV-MAIL] (未配置 SMTP/Resend) → {to_email} | 主题:{subject}\n{body}")
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = to_email
    if html:
        msg.set_content(body)
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(body)
    ctx = ssl.create_default_context()
    try:
        if cfg["port"] == 465:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], context=ctx, timeout=15) as s:
                s.login(cfg["user"], cfg["pass"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=15) as s:
                if cfg["tls"]:
                    s.starttls(context=ctx)
                s.login(cfg["user"], cfg["pass"])
                s.send_message(msg)
        return True
    except Exception as e:
        print(f"[MAIL-ERROR] 发送失败 to={to_email}: {e}")
        return False


def _send(to_email: str, subject: str, body: str, html: str = None) -> bool:
    """发信总入口：Resend 优先，SMTP 回退。"""
    if _send_resend(to_email, subject, body, html):
        return True
    return _send_smtp(to_email, subject, body, html)


def send_email(to_email: str, subject: str, body: str, html: str = None) -> bool:
    """通用发信入口（EDM / 工单通知）。返回是否真实发出。"""
    return _send(to_email, subject, body, html)


def send_verification_email(to_email: str, code: str) -> bool:
    """发送注册验证码（通常交给 Supabase Auth，这里保留备用）。"""
    return _send(
        to_email,
        f"【{SITE_NAME}】你的邮箱验证码",
        f"你好，\n\n你的邮箱验证码是：{code}\n（5 分钟内有效）。\n\n如非本人操作，请忽略本邮件。",
    )


def send_ticket_notification(name: str, email: str, country: str, message: str, ticket_id: int) -> bool:
    """把用户提交的咨询工单转发到 support 邮箱（自动建单）。"""
    body = (
        f"收到一条新的客户咨询工单（#{ticket_id}）\n\n"
        f"客户姓名：{name or '（未填）'}\n"
        f"客户邮箱：{email}\n"
        f"国家/地区：{country or '（未填）'}\n"
        f"咨询内容：\n{message}\n\n"
        f"登录后台可在「工单」中查看与回复。"
    )
    return _send(SUPPORT_EMAIL, f"【{SITE_NAME}】新咨询工单 #{ticket_id}（来自 {email}）", body)


def send_edm(subject: str, body: str, recipients: list) -> int:
    """群发 EDM（新特性通知等）。返回成功发送数量。"""
    ok = 0
    for to in recipients:
        if _send(to, subject, body):
            ok += 1
    return ok
