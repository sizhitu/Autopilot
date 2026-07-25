"""
邮件发送（SMTP）
================
配置优先级：数据库 settings 表（后台可配置） > 环境变量。
未配置 SMTP_HOST 时进入「开发模式」：仅把内容打印到后端日志（不真实发信）。

配置项（key 名）：
  smtp_host / smtp_port / smtp_user / smtp_pass / smtp_from / smtp_tls
  support_email   收件工单邮箱（默认 support@timebricks.bid）
  site_name       站点名（用于邮件标题）

能力：
  - send_verification_email  注册验证码
  - send_email               通用发信（EDM / 工单通知）
  - send_ticket_notification 把用户提交的咨询工单转发到 support 邮箱
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

import db

SITE_NAME = os.getenv("SITE_NAME", "Autopilot 投资分析")
SUPPORT_EMAIL = os.getenv("SUPPORT_EMAIL", "support@timebricks.bid")


def _config() -> dict:
    """合并 数据库设置 + 环境变量，得到最终 SMTP 配置。"""
    def g(key, env):
        v = db.get_setting(key)
        if v is None:
            v = os.getenv(env)
        return v

    host = g("smtp_host", "SMTP_HOST")
    port = g("smtp_port", "SMTP_PORT") or "465"
    user = g("smtp_user", "SMTP_USER")
    pw = g("smtp_pass", "SMTP_PASS")
    frm = g("smtp_from", "SMTP_FROM") or user
    tls = (g("smtp_tls", "SMTP_TLS") or "true").lower() == "true"
    return {
        "host": host, "port": int(port or 465),
        "user": user, "pass": pw, "from": frm, "tls": tls,
    }


def _send(to_email: str, subject: str, body: str, html: str = None) -> bool:
    """真正的发信实现。成功返回 True，失败返回 False（并回退到日志打印）。"""
    cfg = _config()
    host = cfg["host"]
    if not host:
        print(f"[DEV-MAIL] (未配置 SMTP) → {to_email} | 主题:{subject}\n{body}")
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
        if cfg["port"] == 465 or (cfg["tls"] and cfg["port"] == 465):
            with smtplib.SMTP_SSL(host, cfg["port"], context=ctx, timeout=15) as s:
                s.login(cfg["user"], cfg["pass"])
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, cfg["port"], timeout=15) as s:
                if cfg["tls"]:
                    s.starttls(context=ctx)
                s.login(cfg["user"], cfg["pass"])
                s.send_message(msg)
        return True
    except Exception as e:
        print(f"[MAIL-ERROR] 发送失败 to={to_email}: {e}")
        print(f"[DEV-MAIL] (发信失败回退) → {to_email} | 主题:{subject}\n{body}")
        return False


def send_email(to_email: str, subject: str, body: str, html: str = None) -> bool:
    """通用发信入口（EDM / 工单通知）。返回是否真实发出。"""
    return _send(to_email, subject, body, html)


def send_verification_email(to_email: str, code: str) -> bool:
    """发送注册验证码。返回 True 表示已真实发出，False 表示开发模式。"""
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
