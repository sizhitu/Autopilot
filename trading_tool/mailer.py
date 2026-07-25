"""
邮件发送（SMTP）
================
通过环境变量配置，未配置时进入「开发模式」：仅把验证码打印到后端日志，
并在 register/resend 接口返回 dev_code，方便先把流程跑通。

环境变量：
  SMTP_HOST    SMTP 服务器地址（留空 = 开发模式）
  SMTP_PORT    端口，默认 465（SSL）
  SMTP_USER    登录账号
  SMTP_PASS    登录密码 / 授权码
  SMTP_FROM    发件人，默认同 SMTP_USER
  SMTP_TLS     是否用 TLS，默认 true（465 走 SSL，其它端口走 STARTTLS）

接入真实发信（如 QQ / 163 / SendGrid）：在 Render 控制台配置上述环境变量即可，
无需改代码。
"""

import os
import smtplib
import ssl
from email.message import EmailMessage

SITE_NAME = os.getenv("SITE_NAME", "Autopilot 投资分析")


def send_verification_email(to_email: str, code: str) -> bool:
    """
    发送验证码邮件。返回 True 表示已真实发出，False 表示处于开发模式（未发信）。
    """
    host = os.getenv("SMTP_HOST")
    if not host:
        print(f"[DEV-MAIL] 验证码（{to_email}）: {code}  （未配置 SMTP，未真实发信）")
        return False

    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.getenv("SMTP_USER")
    pw = os.getenv("SMTP_PASS")
    frm = os.getenv("SMTP_FROM", user)
    use_tls = os.getenv("SMTP_TLS", "true").lower() == "true"

    msg = EmailMessage()
    msg["Subject"] = f"【{SITE_NAME}】你的邮箱验证码"
    msg["From"] = frm
    msg["To"] = to_email
    msg.set_content(
        f"你好，\n\n你的邮箱验证码是：{code}\n"
        f"（5 分钟内有效）。\n\n如非本人操作，请忽略本邮件。"
    )

    ctx = ssl.create_default_context()
    try:
        if port == 465 or (use_tls and port == 465):
            with smtplib.SMTP_SSL(host, port, context=ctx, timeout=15) as s:
                s.login(user, pw)
                s.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as s:
                if use_tls:
                    s.starttls(context=ctx)
                s.login(user, pw)
                s.send_message(msg)
        return True
    except Exception as e:
        print(f"[MAIL-ERROR] 发送失败 to={to_email}: {e}")
        # 发信失败也回退到开发模式，保证用户仍能拿到 code 调试
        print(f"[DEV-MAIL] 验证码（{to_email}）: {code}")
        return False
