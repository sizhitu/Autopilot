"""
Waffo Pancake 服务端集成（Python）。

官方 SDK 为 @waffo/pancake-ts（Node）。本模块按文档实现同等 RSA-SHA256 签名，
供 FastAPI 创建结账会话并校验 Webhook。

环境变量：
  WAFFO_MERCHANT_ID          商户 ID（必填）
  WAFFO_PRIVATE_KEY          PEM 私钥，可用字面 \\n
  WAFFO_PRIVATE_KEY_BASE64   整段 PEM 的 base64（推荐 CI）
  WAFFO_STORE_ID             店铺 ID（可选，记录用）
  WAFFO_PRODUCT_ID           订阅产品 PROD_xxx（结账必填）
  WAFFO_PRODUCT_TYPE         subscription | onetime，默认 subscription
  WAFFO_WEBHOOK_PUBLIC_KEY   Webhook 验签公钥 PEM
  WAFFO_API_BASE             默认 https://api.waffo.ai
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from typing import Any, Optional

import requests

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
except Exception:  # pragma: no cover
    hashes = serialization = padding = None  # type: ignore


def _normalize_pem(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if "BEGIN" not in s and all(c.isalnum() or c in "+/=\n\r" for c in s[:80]):
        try:
            s = base64.b64decode(s).decode("utf-8")
        except Exception:
            pass
    s = s.replace("\\n", "\n").replace("\r\n", "\n").strip()
    return s


def load_private_key_pem() -> str:
    b64 = os.getenv("WAFFO_PRIVATE_KEY_BASE64", "").strip()
    if b64:
        try:
            return base64.b64decode(b64).decode("utf-8").strip()
        except Exception:
            pass
    return _normalize_pem(os.getenv("WAFFO_PRIVATE_KEY", ""))


def load_webhook_public_key_pem() -> str:
    b64 = os.getenv("WAFFO_WEBHOOK_PUBLIC_KEY_BASE64", "").strip()
    if b64:
        try:
            return base64.b64decode(b64).decode("utf-8").strip()
        except Exception:
            pass
    return _normalize_pem(os.getenv("WAFFO_WEBHOOK_PUBLIC_KEY", ""))


def configured() -> bool:
    mid = os.getenv("WAFFO_MERCHANT_ID", "").strip()
    key = load_private_key_pem()
    prod = os.getenv("WAFFO_PRODUCT_ID", "").strip()
    return bool(mid and key and prod and serialization is not None)


def _sign(method: str, path: str, timestamp: str, body_str: str, private_key_pem: str) -> str:
    body_hash = base64.b64encode(hashlib.sha256(body_str.encode("utf-8")).digest()).decode("ascii")
    canonical = f"{method}\n{path}\n{timestamp}\n{body_hash}"
    private_key = serialization.load_pem_private_key(private_key_pem.encode("utf-8"), password=None)
    sig = private_key.sign(canonical.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(sig).decode("ascii")


def api_request(method: str, path: str, body: Optional[dict] = None, timeout: int = 25) -> dict:
    """带商户签名的 REST 调用。path 须以 / 开头，如 /v1/actions/checkout/create-session"""
    merchant_id = os.getenv("WAFFO_MERCHANT_ID", "").strip()
    private_key_pem = load_private_key_pem()
    if not merchant_id or not private_key_pem:
        raise RuntimeError("未配置 WAFFO_MERCHANT_ID / WAFFO_PRIVATE_KEY")
    if serialization is None:
        raise RuntimeError("缺少 cryptography 依赖")

    api_base = (os.getenv("WAFFO_API_BASE") or "https://api.waffo.ai").rstrip("/")
    body_str = json.dumps(body or {}, separators=(",", ":"), ensure_ascii=False)
    ts = str(int(time.time()))
    signature = _sign(method.upper(), path, ts, body_str, private_key_pem)
    headers = {
        "Content-Type": "application/json",
        "X-Merchant-Id": merchant_id,
        "X-Timestamp": ts,
        "X-Signature": signature,
    }
    url = f"{api_base}{path}"
    resp = requests.request(method.upper(), url, headers=headers, data=body_str.encode("utf-8"), timeout=timeout)
    try:
        data = resp.json()
    except Exception:
        data = {"raw": resp.text[:500]}
    if resp.status_code >= 400:
        raise RuntimeError(f"Waffo API {resp.status_code}: {data}")
    return data if isinstance(data, dict) else {"data": data}


def create_checkout_session(
    *,
    buyer_email: str = "",
    success_url: str = "",
    order_merchant_external_id: str = "",
    metadata: Optional[dict] = None,
) -> dict:
    """
    创建结账会话，返回 {sessionId, checkoutUrl, expiresAt, ...}。
    order_merchant_external_id 建议传本站 user id，便于 webhook 对账。
    """
    product_id = os.getenv("WAFFO_PRODUCT_ID", "").strip()
    if not product_id:
        raise RuntimeError("未配置 WAFFO_PRODUCT_ID")
    product_type = (os.getenv("WAFFO_PRODUCT_TYPE") or "subscription").strip().lower()
    body: dict[str, Any] = {
        "productId": product_id,
        "currency": (os.getenv("WAFFO_CURRENCY") or "USD").strip().upper(),
    }
    if product_type in ("subscription", "onetime"):
        body["productType"] = product_type
    if buyer_email:
        body["buyerEmail"] = buyer_email.strip().lower()
    if success_url:
        body["successUrl"] = success_url
    if order_merchant_external_id:
        body["orderMerchantExternalId"] = order_merchant_external_id
    if metadata:
        body["metadata"] = metadata
    # 语言可选
    lang = os.getenv("WAFFO_LANGUAGE", "").strip()
    if lang:
        body["language"] = lang

    path = "/v1/actions/checkout/create-session"
    data = api_request("POST", path, body)
    # 响应可能包在 data 里
    session = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(session, dict):
        session = data
    # 再剥一层常见嵌套
    for key in ("session", "checkoutSession", "result"):
        if isinstance(session.get(key), dict) and (session[key].get("checkoutUrl") or session[key].get("sessionId")):
            session = session[key]
            break
    return session


def verify_webhook_signature(raw_body: str, signature_header: str, public_key_pem: str = "") -> bool:
    """校验 X-Waffo-Signature: t=<ms>,v1=<base64>"""
    if serialization is None:
        return False
    pem = public_key_pem or load_webhook_public_key_pem()
    if not pem or not signature_header:
        return False
    try:
        parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
        t, v1 = parts.get("t"), parts.get("v1")
        if not t or not v1:
            return False
        if abs(int(time.time() * 1000) - int(t)) > 5 * 60 * 1000:
            return False
        signature_input = f"{t}.{raw_body}".encode("utf-8")
        public_key = serialization.load_pem_public_key(pem.encode("utf-8"))
        public_key.verify(
            base64.b64decode(v1),
            signature_input,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


def extract_uid_from_event(event: dict) -> str:
    """从 webhook 事件中尽量解析本站用户 id。"""
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    for k in ("orderMerchantExternalId", "merchantExternalId", "externalId"):
        v = data.get(k) or event.get(k)
        if v and isinstance(v, str) and len(v) >= 8:
            return v.strip()
    meta = data.get("orderMetadata") or data.get("metadata") or {}
    if isinstance(meta, dict):
        for k in ("userId", "uid", "user_id", "external_customer_id"):
            v = meta.get(k)
            if v and isinstance(v, str):
                return v.strip()
    return ""
