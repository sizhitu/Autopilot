"""
Waffo Pancake Python 客户端（RSA-SHA256 签名）
环境变量：
  WAFFO_MERCHANT_ID
  WAFFO_PRIVATE_KEY   PEM / 纯 base64 / 带 \\n
  WAFFO_PRODUCT_ID    PROD_xxx
  WAFFO_API_BASE      默认 https://api.waffo.ai
  WAFFO_WEBHOOK_PUBLIC_KEY  可选验签
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Optional


def configured() -> bool:
    return bool(
        os.getenv("WAFFO_MERCHANT_ID", "").strip()
        and os.getenv("WAFFO_PRIVATE_KEY", "").strip()
        and os.getenv("WAFFO_PRODUCT_ID", "").strip()
    )


def _normalize_private_key(raw: str) -> bytes:
    s = (raw or "").strip().strip('"').strip("'")
    if not s:
        raise ValueError("WAFFO_PRIVATE_KEY 为空")
    if "\\n" in s and "-----BEGIN" in s:
        s = s.replace("\\n", "\n")
    if "-----BEGIN" in s:
        return s.encode("utf-8")
    try:
        decoded = base64.b64decode(s)
        if b"-----BEGIN" in decoded:
            return decoded
        b64 = base64.b64encode(decoded).decode("ascii")
        lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
        pem = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----\n"
        return pem.encode("utf-8")
    except Exception:
        pass
    body = "".join(s.split())
    lines = [body[i : i + 64] for i in range(0, len(body), 64)]
    pem = "-----BEGIN PRIVATE KEY-----\n" + "\n".join(lines) + "\n-----END PRIVATE KEY-----\n"
    return pem.encode("utf-8")


def _sign(method: str, path: str, body_str: str, private_key_pem: bytes) -> tuple[str, str]:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    timestamp = str(int(time.time()))
    body_hash = base64.b64encode(hashlib.sha256(body_str.encode("utf-8")).digest()).decode("ascii")
    canonical = f"{method}\n{path}\n{timestamp}\n{body_hash}"
    private_key = serialization.load_pem_private_key(private_key_pem, password=None)
    signature = private_key.sign(canonical.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return timestamp, base64.b64encode(signature).decode("ascii")


def api_call(method: str, path: str, body: Optional[dict] = None) -> dict:
    merchant_id = os.getenv("WAFFO_MERCHANT_ID", "").strip()
    private_key_pem = _normalize_private_key(os.getenv("WAFFO_PRIVATE_KEY", ""))
    api_base = (os.getenv("WAFFO_API_BASE") or "https://api.waffo.ai").rstrip("/")
    body_str = json.dumps(body or {}, separators=(",", ":"), ensure_ascii=False)
    timestamp, signature = _sign(method.upper(), path, body_str, private_key_pem)
    req = urllib.request.Request(
        f"{api_base}{path}",
        data=body_str.encode("utf-8") if method.upper() != "GET" else None,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Merchant-Id": merchant_id,
            "X-Timestamp": timestamp,
            "X-Signature": signature,
        },
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"Waffo API {e.code}: {err}") from e


def create_checkout_session(
    *,
    product_id: str = None,
    currency: str = None,
    buyer_email: str = None,
    success_url: str = None,
    user_id: str = None,
    order_merchant_external_id: str = None,
    metadata: dict = None,
    product_type: str = "subscription",
) -> dict:
    pid = (product_id or os.getenv("WAFFO_PRODUCT_ID", "")).strip()
    if not pid:
        raise ValueError("缺少 WAFFO_PRODUCT_ID")
    cur = (currency or os.getenv("WAFFO_CURRENCY", "USD") or "USD").strip()
    ext_id = order_merchant_external_id or user_id
    payload: dict[str, Any] = {
        "productId": pid,
        "currency": cur,
        "productType": product_type,
    }
    if buyer_email:
        payload["buyerEmail"] = buyer_email
    if success_url:
        payload["successUrl"] = success_url
    if ext_id:
        payload["orderMerchantExternalId"] = str(ext_id)
    meta = dict(metadata or {})
    if ext_id and "user_id" not in meta and "userId" not in meta:
        meta["user_id"] = str(ext_id)
    if meta:
        payload["metadata"] = meta

    data = api_call("POST", "/v1/actions/checkout/create-session", payload)
    session = data.get("data") if isinstance(data.get("data"), dict) else data
    if isinstance(session, dict) and isinstance(session.get("session"), dict):
        session = session["session"]
    session = session or {}
    url = (
        session.get("checkoutUrl")
        or session.get("checkout_url")
        or session.get("url")
        or data.get("checkoutUrl")
        or data.get("url")
    )
    sid = session.get("sessionId") or session.get("session_id") or session.get("id") or data.get("sessionId")
    if not url:
        raise RuntimeError(f"Waffo 未返回 checkoutUrl: {json.dumps(data, ensure_ascii=False)[:500]}")
    return {"checkoutUrl": url, "sessionId": sid, "url": url, "id": sid, "raw": data}


def load_webhook_public_key_pem() -> str:
    pem = (os.getenv("WAFFO_WEBHOOK_PUBLIC_KEY") or "").strip()
    if not pem:
        return ""
    if "\\n" in pem:
        pem = pem.replace("\\n", "\n")
    return pem


def verify_webhook_signature(raw_text: str, signature_header: str) -> bool:
    pem = load_webhook_public_key_pem()
    if not pem or not signature_header:
        return False
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        parts = dict(p.split("=", 1) for p in signature_header.split(",") if "=" in p)
        t, v1 = parts.get("t"), parts.get("v1")
        if not t or not v1:
            return False
        if abs(int(time.time() * 1000) - int(t)) > 5 * 60 * 1000:
            return False
        public_key = serialization.load_pem_public_key(pem.encode("utf-8"))
        public_key.verify(
            base64.b64decode(v1),
            f"{t}.{raw_text}".encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


# 兼容别名
verify_webhook = lambda body, sig: verify_webhook_signature(
    body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else body, sig
)


def extract_uid_from_event(event: dict) -> str:
    if not isinstance(event, dict):
        return ""
    data = event.get("data") if isinstance(event.get("data"), dict) else event
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    for key in ("user_id", "userId", "external_customer_id"):
        if meta.get(key):
            return str(meta[key])
    for key in ("orderMerchantExternalId", "order_merchant_external_id", "buyerIdentity", "buyer_identity"):
        v = data.get(key)
        if v:
            return str(v)
    return ""


extract_user_id = extract_uid_from_event
