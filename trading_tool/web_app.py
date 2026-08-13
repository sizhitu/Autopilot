"""
藤本茂融合策略 - Web API 后端（前后端分离版）
============================================
FastAPI 纯 API 服务，前端部署在 Cloudflare Pages，后端部署在 Render。
新增能力：
  - 用户注册 / 邮箱验证 / 登录（sqlite 存储，SMTP 发验证码）
  - 按用户的自选看板（增删，sqlite 持久化）
  - 每日粒度行情落库（daily_data），支持实时失败时回退到本地存储
  - CORS，允许 Pages 前端跨域调用

原有分析能力（analyze / quote / backtest / search / ladder / profile）保留。
"""

import sys
import os
import io
import json
import math
import re
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Header, Depends, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 导入策略引擎和数据源
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strategy_engine import FujimotoStrategy, generate_sample_data, SignalType, TrendType
from data_fetcher import DataFetcher
from backtest import Backtester, result_to_dict as bt_to_dict
from watchlist import (
    get_watchlist_status, get_user_watchlist_symbols,
    add_user_watchlist, remove_user_watchlist,
    invalidate_user_cache,
    _detect_high_low, _calc_valuation, STOCK_ROLE, DEFAULT_ROLE,
)
from nine_turn import calc_nine_turn_display
import db
import auth


# 免费体验固定真实行情代码（与 /api/watchlist/free-preview 一致）
FREE_PREVIEW_SYMBOLS = frozenset({"AAPL", "TSLA"})


def _norm_sym(symbol: str) -> str:
    return str(symbol or "").strip().upper().replace(".US", "").replace(".SS", "").replace(".SZ", "")


def _is_free_preview_symbol(symbol: str) -> bool:
    return _norm_sym(symbol) in FREE_PREVIEW_SYMBOLS


def _require_pro(authorization: Optional[str] = None):

    """核心功能门禁：BILLING_REQUIRED 时必须登录且为 pro/lifetime/admin。"""
    try:
        auth.ensure_entitled_from_header(authorization)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=402, detail="需要订阅后使用此功能") from e

import mailer
import daily_store
import user_store
import waffo_client
import ticket_store
import settings_store
import supabase_client
import symbols
import watchlist_store
import cache
import analysis_store
import ratelimit
import reports
import volume_convergence

def _safe_volume_convergence(df):
    """量能收敛失败时不阻断行情接口。"""
    try:
        return volume_convergence.compute_volume_convergence(df)
    except Exception as e:
        return {
            "daily": {"timeframe": "D", "label": "日线", "status": "计算失败", "converging": False,
                      "summary": str(e)[:120], "vols": [], "upper_line": [], "lower_line": []},
            "weekly": {"timeframe": "W", "label": "周线", "status": "计算失败", "converging": False,
                       "summary": "", "vols": [], "upper_line": [], "lower_line": []},
            "monthly": {"timeframe": "M", "label": "月线", "status": "计算失败", "converging": False,
                        "summary": "", "vols": [], "upper_line": [], "lower_line": []},
            "overall": "量能收敛计算暂时不可用",
        }


app = FastAPI(title="藤本茂融合策略 Web 工具 API", version="3.0")
fetcher = DataFetcher()


def _is_api_debug() -> bool:
    return (os.getenv("API_DEBUG") or os.getenv("DEBUG") or "").strip().lower() in ("1", "true", "yes", "on")


def _log_exc(context: str, exc: BaseException) -> None:
    try:
        import logging
        logging.getLogger("autopilot").exception("%s: %s", context, exc)
    except Exception:
        pass


def _client_http_error(status: int, public_msg: str, exc: BaseException = None) -> HTTPException:
    """对客户端返回简短错误；完整异常仅写日志。API_DEBUG=1 时附带异常摘要。"""
    if exc is not None:
        _log_exc(public_msg, exc)
        if _is_api_debug():
            return HTTPException(status, f"{public_msg}: {type(exc).__name__}: {str(exc)[:200]}")
    return HTTPException(status, public_msg)


# ----------------------------------------------------------------------
#  CORS：允许 Cloudflare Pages 前端跨域调用。
#  通过 CORS_ORIGINS 环境变量配置（逗号分隔），默认放行所有来源。
# ----------------------------------------------------------------------
_cors_origins = os.getenv("CORS_ORIGINS", "*")
if _cors_origins == "*":
    allow_origins = ["*"]
else:
    allow_origins = [o.strip() for o in _cors_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "X-Requested-With"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """安全响应头：不引入 Google/国外验证码，不阻断国内网络。"""
    response = await call_next(request)
    # 防点击劫持
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # 权限收敛（相机/麦克风等默认禁用）
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), microphone=(), geolocation=(), payment=()",
    )
    # 温和 CSP：允许本站、Supabase、内联脚本（现有前端依赖），禁止随意嵌套
    # 不强制 upgrade-insecure；不引入 reCAPTCHA / 谷歌域名
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net https://*.supabase.co; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: blob: https:; "
        "font-src 'self' data:; "
        "connect-src 'self' https: wss:; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self' https:"
    )
    response.headers.setdefault("Content-Security-Policy", csp)
    return response


# 服务启动：初始化数据库
@app.on_event("startup")
def _on_startup():
    db.init_db()


# ================================================================
#  辅助：策略结果转 JSON
# ================================================================
def _to_jsonable(v):
    """递归将 numpy 类型转为原生 Python 类型"""
    if isinstance(v, (np.bool_, bool)):
        return bool(v)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, dict):
        return {str(k): _to_jsonable(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_to_jsonable(x) for x in v]
    return v


def result_to_dict(result) -> dict:
    """将 StrategyResult 转为可 JSON 序列化的字典"""
    d = {
        "trend": result.trend.value,
        "signal": result.signal.value,
        "action": result.action,
        "position_pct": round(result.position_pct * 100, 2),
        "entry_price": round(result.entry_price, 2) if result.entry_price else None,
        "stop_loss": round(result.stop_loss, 2) if result.stop_loss else None,
        "target_prices": [round(float(t), 2) for t in result.target_prices],
        "indicators": [
            {
                "name": ind.name,
                "value": round(float(ind.value), 4),
                "signal": ind.signal,
                "detail": ind.detail
            } for ind in result.indicators
        ],
        "fib_levels": [
            {
                "level": float(fl.level),
                "price": round(float(fl.price), 2),
                "tested": bool(fl.tested),
                "reacted": bool(fl.reacted),
                "reaction_signal": fl.reaction_signal
            } for fl in result.fib_levels
        ],
        "layers": _to_jsonable(result.layers_consistent),
        "risk_warning": result.risk_warning,
        "chart_data": {
            "swing_high": round(float(result.chart_data.get("swing_high", 0)), 2),
            "swing_low": round(float(result.chart_data.get("swing_low", 0)), 2),
            "target_prices": [round(float(t), 2) for t in result.chart_data.get("target_prices", [])],
            "fib_levels": [
                {"level": float(fl.level), "price": round(float(fl.price), 2),
                 "reacted": bool(fl.reacted)}
                for fl in result.fib_levels
            ],
            "vwma": round(float(result.chart_data.get("vwma")), 2) if result.chart_data.get("vwma") else None,
            "volume_price_divergence": _to_jsonable(
                result.chart_data.get("volume_price_divergence") or {}
            ),
            "price_triangle": _to_jsonable(
                result.chart_data.get("price_triangle") or {}
            ),
        },
        "volume_price_divergence": _to_jsonable(
            result.chart_data.get("volume_price_divergence") or {}
        ),
        "price_triangle": _to_jsonable(
            result.chart_data.get("price_triangle") or {}
        ),
    }
    return d


def df_to_chart_json(df: pd.DataFrame, result, show_last=300) -> dict:
    """提取K线+均线数据供前端绘图。全库统一默认 300 根，便于观察 MA120/MA250。"""
    show_last = max(int(show_last or 300), 300)
    # 有多少展示多少，上限 show_last
    recent = df.tail(min(show_last, len(df))).copy().reset_index(drop=True)

    candles = []
    for _, row in recent.iterrows():
        candles.append({
            "o": round(float(row['open']), 2),
            "h": round(float(row['high']), 2),
            "l": round(float(row['low']), 2),
            "c": round(float(row['close']), 2),
            "v": int(row['volume']),
        })

    # 均线
    ma_periods = [5, 10, 20, 30, 50, 100, 120, 150, 200, 250]
    ma_colors = {
        5: "#3498db", 10: "#9b59b6", 20: "#e67e22", 30: "#1abc9c",
        50: "#f39c12", 100: "#e74c3c", 120: "#f1c40f", 150: "#34495e",
        200: "#7f8c8d", 250: "#bdc3c7",
    }
    mas = {}
    for p in ma_periods:
        if len(df) >= p:
            ma_series = df['close'].rolling(p).mean().tail(show_last).values
            ma_clean = [None if pd.isna(v) else round(float(v), 2) for v in ma_series]
            mas[str(p)] = {"data": ma_clean, "color": ma_colors[p]}

    # RSI
    rsi_data = []
    if len(df) >= 15:
        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        rsi_tail = rsi.tail(show_last).values
        rsi_data = [None if pd.isna(v) else round(float(v), 2) for v in rsi_tail]

    # 斐波那契
    fib_lines = []
    if result:
        for fl in result.fib_levels:
            fib_lines.append({
                "level": float(fl.level),
                "price": round(float(fl.price), 2),
                "reacted": bool(fl.reacted),
                "tested": bool(fl.tested),
                "reaction_signal": fl.reaction_signal
            })

    target_lines = []
    if result:
        for t in result.target_prices:
            target_lines.append(round(float(t), 2))

    return {
        "candles": candles,
        "count": len(candles),
        "mas": mas,
        "rsi": rsi_data,
        "fib_lines": fib_lines,
        "target_lines": target_lines,
        "entry_price": round(float(result.entry_price), 2) if result and result.entry_price else None,
        "stop_loss": round(float(result.stop_loss), 2) if result and result.stop_loss else None,
        "swing_high": round(float(result.chart_data.get("swing_high", 0)), 2) if result else None,
        "swing_low": round(float(result.chart_data.get("swing_low", 0)), 2) if result else None,
    }


def _extra_metrics(df: pd.DataFrame, symbol: str = None) -> dict:
    """计算新高/新低 与 估值状态，供股票详情页（分析页）展示。"""
    role = STOCK_ROLE.get(symbol, DEFAULT_ROLE) if symbol else DEFAULT_ROLE
    try:
        hl_text, hl_type = _detect_high_low(df)
    except Exception:
        hl_text, hl_type = "—", "none"
    try:
        val_text, val_type, val_detail = _calc_valuation(df, role)
    except Exception:
        val_text, val_type, val_detail = "合理", "fair", ""
    return {
        "high_low": {"text": hl_text, "type": hl_type},
        "valuation": {
            "text": val_text,
            "type": val_type,
            "detail": val_detail,
            "role": role,
        },
    }


def _df_from_stored(rows: list) -> Optional[pd.DataFrame]:
    """将 daily_data 行转为分析用的 DataFrame。"""
    if not rows:
        return None
    df = pd.DataFrame(rows)
    df = df.rename(columns={"trade_date": "date"})
    df['date'] = pd.to_datetime(df['date'])
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.sort_values('date').reset_index(drop=True)
    return df


def _optional_user(authorization: Optional[str] = Header(None)) -> Optional[dict]:
    """可选登录：有合法令牌则返回用户，否则返回 None（用于看板回退到默认）。"""
    return auth.get_optional_user(authorization)


def _bearer(authorization: Optional[str] = Header(None)) -> str:
    """从 Authorization 头提取原始 Bearer token（用于用户态 RLS 客户端）。"""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return ""


# ----------------------------------------------------------------------
#  限流辅助：按「用户优先、否则客户端 IP」构造计数键，超限抛 429
# ----------------------------------------------------------------------
def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_check(authorization: Optional[str], request: Request, name: str,
                max_req: int, window: int) -> None:
    """对指定接口做固定窗口限流；超限时抛 429 友好提示。"""
    user = auth.get_optional_user(authorization)
    ident = user["id"] if user else _client_ip(request)
    key = f"rl:{name}:{ident}"
    res = ratelimit.limit(key, max_req, window)
    if not res["allowed"]:
        raise HTTPException(
            status_code=429,
            detail=f"请求过于频繁，请 {res['retry_after']} 秒后再试（接口限流保护）",
        )


# ================================================================
#  健康检查
# ================================================================
@app.get("/api/health")
async def health():
    return {"success": True, "service": "autopilot-api", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/api/config")
async def public_config():
    """公开配置：前端用其初始化 supabase-js（anon key / url 均为公开安全值）。"""
    return {
        "success": True,
        "supabase_url": supabase_client.SUPABASE_URL,
        "supabase_anon_key": supabase_client.SUPABASE_ANON_KEY,
        "support_email": mailer.SUPPORT_EMAIL,
        "site_name": mailer.SITE_NAME,
        "using_supabase": supabase_client.using_supabase(),
        # 多设备实时同步（Supabase Realtime 订阅 watchlists），默认关闭，需显式开启
        "realtime_enabled": os.getenv("ENABLE_REALTIME", "false").lower() in ("1", "true", "yes", "on"),
    }


# ================================================================
#  认证 API
#  认证本身由前端 supabase-js + Supabase Auth 完成（注册 / 邮箱 OTP / Magic Link）。
#  后端只校验前端传来的 JWT，并返回/同步用户资料。
# ================================================================
def _client_country(request: Request) -> str:
    """从边缘/代理头取国家码（如 AU、CN）；无则空串。不写 IP。"""
    for h in ("cf-ipcountry", "CF-IPCountry", "x-vercel-ip-country", "x-country-code"):
        v = (request.headers.get(h) or "").strip().upper()
        if v and v not in ("XX", "T1", "UNKNOWN", "ZZ"):
            return v[:8]
    return ""


@app.get("/api/auth/me")
async def api_me(request: Request, user: dict = Depends(auth.get_current_user)):
    """返回当前登录用户信息；同步 profiles，并更新 last_seen / 会话级 last_login。"""
    profile = user_store.get_or_create_profile(user["id"], user.get("email", ""))
    country = _client_country(request)
    try:
        touched = user_store.touch_profile_activity(user["id"], country=country or None)
        if touched:
            profile.update({k: touched[k] for k in touched if k != "id"})
    except Exception:
        pass
    digest = user_store.get_digest_prefs(user["id"])
    is_adm = bool(profile.get("is_admin") or user.get("is_admin"))
    ent = user_store.entitlement_from_profile(profile, is_admin_user=is_adm)
    return {
        "success": True,
        "user": {
            "id": user["id"],
            "email": user.get("email", ""),
            "display_name": profile.get("display_name") or (user.get("email", "").split("@")[0]),
            "verified": True,
            "is_admin": is_adm,
            "digest_enabled": digest.get("enabled", False),
            "digest_freq": digest.get("freq", "weekly"),
            "last_login_at": profile.get("last_login_at"),
            "last_seen_at": profile.get("last_seen_at"),
            "last_login_country": profile.get("last_login_country"),
            "login_count": int(profile.get("login_count") or 0),
            "plan": ent.get("plan"),
            "plan_source": ent.get("plan_source"),
            "entitled": ent.get("entitled"),
            "billing_required": ent.get("billing_required"),
            "price_usd": ent.get("price_usd"),
            "plan_expires_at": ent.get("plan_expires_at"),
        },
    }


class DigestPrefsRequest(BaseModel):
    enabled: bool = False
    freq: str = "weekly"  # weekly | biweekly


@app.get("/api/user/digest")
async def get_digest_prefs_api(user: dict = Depends(auth.get_current_user)):
    prefs = user_store.get_digest_prefs(user["id"])
    return {"success": True, **prefs}


@app.post("/api/user/digest")
async def set_digest_prefs_api(req: DigestPrefsRequest, user: dict = Depends(auth.get_current_user)):
    profile = user_store.get_or_create_profile(user["id"], user.get("email", ""))
    ent = user_store.entitlement_from_profile(
        profile, is_admin_user=bool(user.get("is_admin") or profile.get("is_admin"))
    )
    # 开启邮件推送需要 Plus / 终身 / 管理员
    if req.enabled and not ent.get("can_digest"):
        raise HTTPException(
            status_code=402,
            detail={
                "code": "plus_required",
                "message": "邮件推送需升级到 Plus 套餐",
                "price_usd": ent.get("price_plus_usd"),
            },
        )
    freq = req.freq if req.freq in ("weekly", "biweekly") else "weekly"
    prefs = user_store.set_digest_prefs(user["id"], enabled=req.enabled, freq=freq)
    return {"success": True, **prefs}


# ================================================================
#  用户管理 / EDM / 工单（管理员 + 公开咨询）
# ================================================================
class SmtpSettingsRequest(BaseModel):
    smtp_host: str = ""
    smtp_port: str = ""
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = ""
    smtp_tls: str = ""


class EdmRequest(BaseModel):
    subject: str
    body: str
    scope: str = "all"   # all | verified


class ContactRequest(BaseModel):
    name: str = ""
    email: str
    country: str = ""
    message: str


class TicketReplyRequest(BaseModel):
    reply: str


@app.get("/api/admin/stats")
async def admin_stats(admin: dict = Depends(auth.require_admin)):
    """注册用户统计：总数 / 已验证 / 近7天 / 近30天 / 未处理工单。"""
    s = user_store.user_stats()
    s["open_tickets"] = ticket_store.count_open()
    s["success"] = True
    return s


@app.get("/api/admin/users")
async def admin_users(limit: int = 100, offset: int = 0, admin: dict = Depends(auth.require_admin)):
    """注册用户列表（脱敏：不含密码哈希）。"""
    users, total = user_store.list_profiles(limit, offset)
    return {"success": True, "count": len(users), "total": total, "users": users}


@app.get("/api/admin/settings/smtp")
async def admin_get_smtp(admin: dict = Depends(auth.require_admin)):
    """查看当前 SMTP 配置（密码脱敏）。Resend 优先，SMTP 作为回退。"""
    def g(k, env):
        v = settings_store.get_setting(k)
        return v if v is not None else os.getenv(env, "")
    host = g("smtp_host", "SMTP_HOST")
    user = g("smtp_user", "SMTP_USER")
    frm = g("smtp_from", "SMTP_FROM") or user
    return {
        "success": True,
        "resend_configured": bool(mailer.RESEND_API_KEY),
        "configured": bool(host),
        "smtp_host": host,
        "smtp_port": g("smtp_port", "SMTP_PORT") or "465",
        "smtp_user": user,
        "smtp_from": frm,
        "smtp_tls": g("smtp_tls", "SMTP_TLS") or "true",
        "support_email": mailer.SUPPORT_EMAIL,
    }


@app.post("/api/admin/settings/smtp")
async def admin_set_smtp(req: SmtpSettingsRequest, admin: dict = Depends(auth.require_admin)):
    """后台配置 SMTP（保存到 settings 层，覆盖环境变量）。空字符串=保留/不覆盖。"""
    mapping = {
        "smtp_host": req.smtp_host, "smtp_port": req.smtp_port,
        "smtp_user": req.smtp_user, "smtp_pass": req.smtp_pass,
        "smtp_from": req.smtp_from, "smtp_tls": req.smtp_tls,
    }
    written = {}
    for k, v in mapping.items():
        if v not in (None, ""):
            settings_store.set_setting(k, v)
            written[k] = True
    # 测试连通性（发一封到管理员自己的邮箱）
    ok = mailer.send_email(
        to_email=req.smtp_user or admin["email"],
        subject=f"【{mailer.SITE_NAME}】SMTP 配置已生效",
        body="这是一封测试邮件，说明你的 SMTP 发信配置已成功生效。",
    )
    return {"success": True, "written": list(written.keys()), "test_sent": bool(ok)}


@app.post("/api/admin/edm/send")
async def admin_edm_send(req: EdmRequest, admin: dict = Depends(auth.require_admin)):
    """向注册用户群发 EDM（新特性通知等）。scope=all|verified（Supabase 模式均已验证）。"""
    if not req.subject or not req.body:
        raise HTTPException(400, "主题与正文不能为空")
    # 分页拉取全部用户邮箱
    recipients = []
    offset = 0
    while True:
        rows, _ = user_store.list_profiles(limit=500, offset=offset)
        if not rows:
            break
        recipients.extend([r["email"] for r in rows if r.get("email")])
        if len(rows) < 500:
            break
        offset += 500
    sent = mailer.send_edm(req.subject, req.body, recipients)
    return {"success": True, "targets": len(recipients), "sent": sent}


class ReportGenRequest(BaseModel):
    period: str = "weekly"          # weekly | monthly
    email: str = ""                 # 可选：仅给该邮箱生成一封（用于测试 / 预览）
    force: bool = False             # True=忽略每周/双周节流（测新模板）


@app.post("/api/admin/reports/generate")
async def admin_reports_generate(req: ReportGenRequest, admin: dict = Depends(auth.require_admin)):
    """手动触发生成并发送 AI 周报/月报。生产由定时任务（Render Cron / 外部调度）调用。

    - 不传 email：遍历全部用户生成并群发，返回 {sent, skipped}。
    - 传 email：仅给该邮箱生成一封（便于预览/测试），返回 {sent}。
    """
    if req.period not in ("weekly", "monthly"):
        raise HTTPException(400, "period 仅支持 weekly / monthly")
    if req.email:
        ok = reports.generate_for_user("", req.email, req.period)
        return {"success": True, "sent": 1 if ok else 0, "forced": True}
    stats = reports.run_reports(req.period, force=bool(req.force))
    return {"success": True, **stats}


@app.post("/api/cron/reports")
async def cron_reports_generate(
    period: str = Query("weekly"),
    force: bool = Query(False, description="true=忽略节流，用于测试新邮件模板"),
    x_cron_secret: str = Header(None, alias="X-Cron-Secret"),
):
    """供 GitHub Actions / 外部定时器调用（免费调度）。

    鉴权：请求头 X-Cron-Secret 必须等于环境变量 CRON_SECRET。
    默认只向开启推送且到达频率的用户发送；force=true 可立即重发（测试用）。
    """
    expected = (os.getenv("CRON_SECRET") or "").strip()
    if not expected:
        raise HTTPException(503, "服务端未配置 CRON_SECRET")
    if not x_cron_secret or x_cron_secret.strip() != expected:
        raise HTTPException(401, "无效的 Cron 密钥")
    if period not in ("weekly", "monthly"):
        raise HTTPException(400, "period 仅支持 weekly / monthly")
    stats = reports.run_reports(period, force=bool(force))
    return {"success": True, **stats}


@app.post("/api/cron/prewarm-cache")
async def cron_prewarm_cache(
    market: str = Query("all", description="us | cn | all"),
    limit: int = Query(200, description="最多预热标的数"),
    x_cron_secret: str = Header(None, alias="X-Cron-Secret"),
):
    """收盘后预热：拉取用户自选最新行情并写入 quote/状态缓存，降低用户访问等待。

    鉴权：X-Cron-Secret == CRON_SECRET
    建议：A股收盘后约 1h（UTC 08:00）、美股收盘后约 1h（UTC 22:00）由 GitHub Actions 触发。
    """
    expected = (os.getenv("CRON_SECRET") or "").strip()
    if not expected:
        raise HTTPException(503, "服务端未配置 CRON_SECRET")
    if not x_cron_secret or x_cron_secret.strip() != expected:
        raise HTTPException(401, "无效的 Cron 密钥")

    market = (market or "all").strip().lower()
    if market not in ("us", "cn", "all"):
        raise HTTPException(400, "market 仅支持 us / cn / all")
    limit = max(1, min(int(limit or 200), 500))

    # 1) 全站自选去重 + 默认看板代码
    symbols = []
    try:
        symbols.extend(watchlist_store.list_all_distinct_symbols(limit=limit * 2))
    except Exception:
        pass
    try:
        from watchlist import WATCHLIST_USER_DEFAULT, WATCHLIST_ADMIN_DEFAULT
        for d in (WATCHLIST_USER_DEFAULT, WATCHLIST_ADMIN_DEFAULT):
            for c in (d or {}).keys():
                symbols.append(str(c).strip().upper())
    except Exception:
        pass

    # 去重并按市场过滤
    seen = set()
    ordered = []
    for s in symbols:
        s = str(s or "").strip().upper()
        if not s or s in seen:
            continue
        is_cn = s.isdigit() or s.startswith(("SH", "SZ", "BJ")) or (len(s) == 6 and s[:1] in "036")
        # 粗分：6位数字/带市场前缀 → A股，其余美股/ETF
        if market == "cn" and not (s.isdigit() or s[:2] in ("SH", "SZ", "BJ") or (len(s) >= 6 and s[-6:].isdigit())):
            # 也接受纯 6 位
            if not (len(s) == 6 and s.isdigit()):
                continue
        if market == "us" and (s.isdigit() or (len(s) == 6 and s.isdigit()) or s[:2] in ("SH", "SZ", "BJ")):
            continue
        seen.add(s)
        ordered.append(s)
        if len(ordered) >= limit:
            break

    ok, fail, skipped, stale_n = 0, 0, 0, 0
    errors = []
    stale_syms = []
    t0 = time.time()

    def _mkt_of(code: str) -> str:
        c = str(code or "").strip().upper()
        if c.isdigit() or c[:2] in ("SH", "SZ", "BJ") or (len(c) == 6 and c.isdigit()):
            return "cn"
        return "us"

    def _fetch_fresh(sym: str):
        """强制清缓存多源拉取，返回尽量新的 df；过旧则 None。"""
        from data_fetcher import invalidate_kline_cache, _df_last_date, _bar_is_stale
        mkt = _mkt_of(sym)
        best = None
        for attempt, days in enumerate((300, 400, 500)):
            try:
                invalidate_kline_cache(sym)
            except Exception:
                pass
            try:
                df = fetcher.fetch(sym, days)
            except Exception:
                df = None
            if df is None or len(df) < 30:
                time.sleep(0.35 + attempt * 0.2)
                continue
            last_d = _df_last_date(df)
            is_stale = _bar_is_stale(last_d, market=mkt, grace_days=1)
            if best is None or len(df) > len(best):
                best = df
            if not is_stale:
                return df, False, last_d
            time.sleep(0.4 + attempt * 0.25)
        # 仍旧：返回最长的一版并标记 stale
        if best is not None:
            return best, True, _df_last_date(best)
        return None, True, None

    for sym in ordered:
        if time.time() - t0 > 260:
            skipped = len(ordered) - ok - fail - stale_n
            break
        try:
            df, is_stale, last_d = _fetch_fresh(sym)
            if df is None or len(df) < 30:
                fail += 1
                errors.append({"symbol": sym, "error": "fetch empty/short"})
                continue
            end_s = ""
            try:
                if last_d is not None:
                    end_s = last_d.strftime("%Y-%m-%d") if hasattr(last_d, "strftime") else str(last_d)[:10]
            except Exception:
                end_s = ""
            strategy = FujimotoStrategy(total_capital=100000)
            result = strategy.analyze(df)
            nine_turn = calc_nine_turn_display(df)
            extra = _extra_metrics(df, sym)
            payload = _to_jsonable({
                "success": True,
                "symbol": sym,
                "stale": bool(is_stale),
                "prewarm": True,
                "data": result_to_dict(result),
                "chart": df_to_chart_json(df, result, show_last=300),
                "nine_turn": nine_turn,
                "high_low": extra.get("high_low"),
                "valuation": extra.get("valuation"),
                "meta": {
                    "rows": len(df),
                    "last_close": round(float(df["close"].iloc[-1]), 2),
                    "end_date": end_s or (
                        df["date"].iloc[-1].strftime("%Y-%m-%d") if "date" in df.columns else ""
                    ),
                },
            })
            _nc = len(((payload.get("chart") or {}).get("candles")) or [])
            # 仅新鲜且足够长的写入长期 quote 缓存；陈旧不覆盖已有好缓存
            if _nc >= 250 and not is_stale:
                cache.set_quote_cache(sym, payload)
            elif is_stale:
                stale_n += 1
                if len(stale_syms) < 30:
                    stale_syms.append({"symbol": sym, "end_date": end_s})
                # 不 delete 已有缓存，避免把旧好数据清掉后又写更差的
            try:
                from watchlist import get_stock_status, _status_to_dict, _status_cache_put
                # 状态也尽量用新数据：仅非 stale 强刷 STATUS
                if not is_stale:
                    st = get_stock_status(sym, sym, days=200)
                    _status_cache_put(sym, _status_to_dict(st))
            except Exception:
                pass
            if not is_stale:
                try:
                    daily_store.store_daily_bars(sym, df, source="prewarm")
                except Exception:
                    pass
                ok += 1
            time.sleep(0.3)
        except Exception as e:
            fail += 1
            if len(errors) < 20:
                errors.append({"symbol": sym, "error": str(e)[:120]})

    # 对陈旧标的再快速重试一轮（数据源瞬时延迟常见）
    if stale_syms and time.time() - t0 < 250:
        time.sleep(1.0)
        for item in list(stale_syms)[:40]:
            if time.time() - t0 > 280:
                break
            sym = item.get("symbol")
            try:
                df, is_stale, last_d = _fetch_fresh(sym)
                if df is None or is_stale or len(df) < 30:
                    continue
                strategy = FujimotoStrategy(total_capital=100000)
                result = strategy.analyze(df)
                nine_turn = calc_nine_turn_display(df)
                extra = _extra_metrics(df, sym)
                end_s = last_d.strftime("%Y-%m-%d") if last_d is not None and hasattr(last_d, "strftime") else ""
                payload = _to_jsonable({
                    "success": True, "symbol": sym, "stale": False, "prewarm": True,
                    "data": result_to_dict(result),
                    "chart": df_to_chart_json(df, result, show_last=300),
                    "nine_turn": nine_turn,
                    "high_low": extra.get("high_low"),
                    "valuation": extra.get("valuation"),
                    "meta": {
                        "rows": len(df),
                        "last_close": round(float(df["close"].iloc[-1]), 2),
                        "end_date": end_s,
                    },
                })
                if len(((payload.get("chart") or {}).get("candles")) or []) >= 250:
                    cache.set_quote_cache(sym, payload)
                    ok += 1
                    stale_n = max(0, stale_n - 1)
                time.sleep(0.3)
            except Exception:
                pass

    return {
        "success": True,
        "market": market,
        "total": len(ordered),
        "ok": ok,
        "fail": fail,
        "stale": stale_n,
        "skipped": skipped,
        "seconds": round(time.time() - t0, 1),
        "stale_symbols": stale_syms[:20],
        "errors": errors,
    }



@app.post("/api/contact")
async def api_contact(req: ContactRequest, request: Request):
    """公开咨询入口：收集 姓名/邮箱/国家/问题，落库并立即转发到 support 邮箱（自动建单）。"""
    _rate_check(None, request, "contact", 8, 3600)  # 每小时最多 8 次，防灌水
    email = (req.email or "").strip().lower()
    message = (req.message or "").strip()
    if not email or "@" not in email:
        raise HTTPException(400, "请填写有效邮箱")
    if len(message) < 3:
        raise HTTPException(400, "请填写咨询内容")
    if len(message) > 4000:
        raise HTTPException(400, "内容过长，请精简后提交")
    # 落库（service 操作）+ 立即转发到客服邮箱
    tid = ticket_store.create_ticket(req.name.strip(), email, req.country.strip(), message)
    mailed = mailer.send_ticket_notification(req.name, email, req.country, message, tid)
    return {"success": True, "ticket_id": tid, "notified": bool(mailed)}


@app.get("/api/admin/tickets")
async def admin_tickets(limit: int = 50, admin: dict = Depends(auth.require_admin)):
    """工单列表（最新在前）。"""
    tickets = ticket_store.list_tickets(limit)
    return {"success": True, "count": len(tickets), "tickets": tickets}


@app.post("/api/admin/tickets/{ticket_id}/reply")
async def admin_ticket_reply(ticket_id: int, req: TicketReplyRequest, admin: dict = Depends(auth.require_admin)):
    """回复工单（保存回复文本，并邮件通知客户）。"""
    reply = (req.reply or "").strip()
    if not reply:
        raise HTTPException(400, "回复内容不能为空")
    to_email = ticket_store.get_ticket_email(ticket_id)
    if not to_email:
        raise HTTPException(404, "工单不存在")
    ticket_store.reply_ticket(ticket_id, reply)
    mailer.send_email(
        to_email=to_email,
        subject=f"【{mailer.SITE_NAME}】关于您的咨询（工单 #{ticket_id}）的回复",
        body=f"您好，\n\n我们对您提交的咨询回复如下：\n\n{reply}\n\n——{mailer.SITE_NAME} 客服团队",
    )
    return {"success": True, "ticket_id": ticket_id}


# ================================================================
#  真实数据源 API
# ================================================================
@app.get("/api/search")
async def search_stocks(q: str = Query(..., description="股票代码或名称关键词"),
                        request: Request = None,
                        authorization: Optional[str] = Header(None)):
    """搜索股票代码"""
    _require_pro(authorization)
    _rate_check(authorization, request, "search", 60, 60)
    results = fetcher.search(q)
    return {"success": True, "results": results, "count": len(results)}


class QuoteRequest(BaseModel):
    symbol: str
    days: int = 300
    force: bool = False


@app.post("/api/quote")
async def get_quote(req: QuoteRequest, request: Request = None,
                    authorization: Optional[str] = Header(None)):
    """
    获取真实行情并自动分析。
    数据分层：原始 K 线写入缓存层（不落业务库）；实时拉取失败时，
    优先回退行情缓存，再回退每日 K 线缓存，并标记 stale=True 告知前端数据可能延迟。
    """
    # 免费体验代码可免订阅拉真实行情；其余仍需 Pro
    if not _is_free_preview_symbol(getattr(req, "symbol", "") or ""):
        _require_pro(authorization)
    _rate_check(authorization, request, "quote", 20, 60)
    # 命中短时分析结果缓存：直接返回（同一标的重复打开详情页秒开）
    # 若缓存 K 线过少（旧逻辑/残缺数据），忽略缓存并重新拉取
    cached_hit = cache.get_quote_cache(req.symbol)
    _force = bool(getattr(req, "force", False))
    if (cached_hit and not _force
            and isinstance(cached_hit, dict)
            and isinstance(cached_hit.get("chart"), dict)
            and (cached_hit.get("chart") or {}).get("candles")):
        _nc = len((cached_hit.get("chart") or {}).get("candles") or [])
        # 低于 250 根视为旧截断缓存，强制重拉
        if _nc >= 300:
            cached_hit = dict(cached_hit)
            cached_hit["stale"] = False
            cached_hit["cache_hit"] = True
            return JSONResponse(content=_to_jsonable(cached_hit),
                                headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
        # 短 K 线缓存作废（含历史 15/80 根污染）
        try:
            cache.delete_quote_cache(req.symbol)
        except Exception:
            try:
                cache.set_quote_cache(req.symbol, None)
            except Exception:
                pass
    try:
        _days = max(int(req.days or 300), 300)
        df = fetcher.fetch(req.symbol, _days)
        if df is None or len(df) < 200:
            try:
                from data_fetcher import invalidate_kline_cache
                invalidate_kline_cache(req.symbol)
            except Exception:
                pass
            df = fetcher.fetch(req.symbol, max(_days, 400))
        source = "live"
        stale = False
    except Exception as e:
        # 第一层兜底：直接用此前缓存的完整行情分析结果
        cached = cache.get_quote_cache(req.symbol)
        if cached and isinstance(cached, dict):
            _nc2 = len(((cached.get("chart") or {}).get("candles")) or [])
            if _nc2 >= 300:
                cached = dict(cached)
                cached["stale"] = True
                return JSONResponse(content=_to_jsonable(cached),
                                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
            try:
                cache.delete_quote_cache(req.symbol)
            except Exception:
                pass
        # 第二层兜底：用缓存的每日 K 线重建 DataFrame
        stored = cache.get_daily_cache(req.symbol)
        df = _df_from_stored(stored) if stored else None
        if df is None or len(df) < 5:
            raise _client_http_error(400, "获取数据失败且无缓存，请稍后重试", e)
        source = "cache"
        stale = True

    if len(df) < 5:
        raise HTTPException(400, f"数据不足: 仅{len(df)}根K线，无法分析")
    # 统一目标 ≥300 根；不足再强拉一次
    if len(df) < 250:
        try:
            from data_fetcher import invalidate_kline_cache
            invalidate_kline_cache(req.symbol)
            cache.delete_quote_cache(req.symbol)
        except Exception:
            pass
        try:
            df2 = fetcher.fetch(req.symbol, 400)
            if df2 is not None and len(df2) > len(df):
                df = df2
        except Exception:
            pass

    # 实时成功时，把每日 K 线写入缓存层（供回测 / 指标分析 / 容错）
    if source == "live":
        try:
            daily_store.store_daily_bars(req.symbol, df, source=source)
        except Exception:
            pass

    strategy = FujimotoStrategy(total_capital=100000)
    result = strategy.analyze(df)
    nine_turn = calc_nine_turn_display(df)
    extra = _extra_metrics(df, req.symbol)

    # 本地映射行业/简介：随 quote 一并返回，避免前端再等一轮 /api/profile
    _ind = None
    _biz = None
    try:
        from data_fetcher import STOCK_META
        _m = STOCK_META.get(str(req.symbol).upper()) or STOCK_META.get(str(req.symbol))
        if _m:
            _ind = _m.get("industry")
            _biz = _m.get("desc")
        if not _ind:
            _ind = fetcher.fetch_industry(req.symbol)
        if not _biz:
            # 仅在映射缺失时才走网络；常见标的不额外耗时
            pass
    except Exception:
        pass

    payload = _to_jsonable({
        "success": True,
        "symbol": req.symbol,
        "stale": stale,
        "data": result_to_dict(result),
        "chart": df_to_chart_json(df, result, show_last=max(300, int(getattr(req, "days", 300) or 300))),
        "nine_turn": nine_turn,
        "high_low": extra["high_low"],
        "valuation": extra["valuation"],
        "volume_convergence": _safe_volume_convergence(df),
        "industry": _ind,
        "business_summary": _biz,
        "meta": {
            "rows": len(df),
            "last_close": round(float(df['close'].iloc[-1]), 2),
            "start_date": df['date'].iloc[0].strftime('%Y-%m-%d') if 'date' in df.columns else "",
            "end_date": df['date'].iloc[-1].strftime('%Y-%m-%d') if 'date' in df.columns else "",
        }
    })

    # 仅当 chart≥300 根才写入 quote 缓存，杜绝 15 根污染全站
    if source == "live":
        try:
            _cn = len(((payload.get("chart") or {}).get("candles")) or [])
            if _cn >= 300:
                cache.set_quote_cache(req.symbol, payload)
            else:
                cache.delete_quote_cache(req.symbol)
        except Exception:
            pass

    # 已登录用户：把本次分析结果写入分析历史（关联 user + symbol），失败不阻断主流程
    user = auth.get_optional_user(authorization)
    if user:
        try:
            record = {
                "symbol": req.symbol.upper(),
                "data": payload["data"],
                "nine_turn": payload["nine_turn"],
                "high_low": payload["high_low"],
                "valuation": payload["valuation"],
                "meta": payload["meta"],
            }
            analysis_store.add(user["id"], req.symbol, "", record, _bearer(authorization))
        except Exception:
            pass

    return JSONResponse(content=payload,
                        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


# ================================================================
#  每日行情存储 API（回测 / 指标分析）
# ================================================================
@app.get("/api/daily/{symbol}")
async def get_daily(symbol: str, limit: int = Query(0, description="0=全部，>0 取最近 N 天")):
    """取回某标的已存储的每日行情，用于数据回测与指标分析。"""
    rows = daily_store.get_stored_daily(symbol, limit=limit or None)
    return {
        "success": True,
        "symbol": symbol,
        "count": len(rows),
        "data": rows,
    }


# ================================================================
#  自选看板 API（按用户）
# ================================================================

@app.get("/api/watchlist/free-preview")
async def watchlist_free_preview(request: Request, refresh: bool = False):
    """免费/未登录固定两只真实行情；与登录看板同一套日期新鲜度与防回退逻辑。"""
    _rate_check(None, request, "free_preview", 30, 60)
    import time as _time
    from watchlist import (
        _status_dict_cached, _row_is_date_fresh, _row_fresher, _bar_date_str,
        verify_and_refresh_symbols,
    )

    items = [
        ("AAPL", "苹果"),
        ("TSLA", "特斯拉"),
    ]
    global _FREE_PREVIEW_CACHE
    try:
        _FREE_PREVIEW_CACHE
    except NameError:
        _FREE_PREVIEW_CACHE = {"ts": 0, "data": None}

    now = _time.time()
    # 短缓存命中：若两只均已最新则直接返回；否则当作未命中重算
    if (not refresh) and _FREE_PREVIEW_CACHE.get("data") and (now - float(_FREE_PREVIEW_CACHE.get("ts") or 0) < 6 * 3600):
        payload = dict(_FREE_PREVIEW_CACHE["data"])
        stocks0 = payload.get("stocks") or []
        all_fresh = bool(stocks0) and all(
            isinstance(s, dict) and _row_is_date_fresh(s, s.get("code") or "")
            for s in stocks0
        )
        if all_fresh:
            payload["cached"] = True
            payload["data_source"] = "free_preview_cache"
            return JSONResponse(content=_to_jsonable(payload),
                                headers={"Cache-Control": "no-store, max-age=0"})
        # 缓存里有旧日期 → 继续往下强制刷新

    # 逐只：未最新则 force_live；与旧缓存行比日期防回退
    prev_by = {}
    try:
        for s in ((_FREE_PREVIEW_CACHE.get("data") or {}).get("stocks") or []):
            if isinstance(s, dict) and s.get("code"):
                prev_by[str(s["code"]).upper()] = dict(s)
    except Exception:
        prev_by = {}

    stocks = []
    for code, name in items:
        try:
            need_live = refresh or not _row_is_date_fresh(prev_by.get(str(code).upper()) or {}, code)
            row = _status_dict_cached(code, name, 300, force_live=bool(need_live))
            if isinstance(row, dict):
                row = dict(row)
                prev = prev_by.get(str(code).upper())
                if prev:
                    row = _row_fresher(row, prev)
                row["pending"] = False
                row["demo"] = False
                row["free_fixed"] = True
                stocks.append(row)
        except Exception as e:
            prev = prev_by.get(str(code).upper())
            if prev and prev.get("price") not in (None, "", "-", "…"):
                p = dict(prev)
                p["free_fixed"] = True
                stocks.append(p)
            else:
                stocks.append({
                    "code": code, "name": name, "market": "美股",
                    "price": "-", "error": str(e)[:40], "pending": False, "free_fixed": True,
                })

    # 二次校验：仍旧则再实拉
    try:
        v = verify_and_refresh_symbols([(c, n) for c, n in items])
        by = {str(s.get("code")).upper(): s for s in (v.get("stocks") or []) if s and s.get("code")}
        fixed = []
        for s in stocks:
            cu = str((s or {}).get("code") or "").upper()
            n = by.get(cu)
            if n:
                fixed.append(dict(_row_fresher(n, s)))
            else:
                fixed.append(s)
        stocks = fixed
    except Exception:
        pass

    def _sum_stocks(stocks_list):
        summary = {"即将上涨关注": 0, "上涨见顶关注": 0, "下跌观望": 0, "error": 0}
        for s in stocks_list:
            if not isinstance(s, dict):
                continue
            if s.get("error"):
                summary["error"] += 1
                summary["下跌观望"] += 1
                continue
            sig = (s.get("signal") or "").strip()
            act = (s.get("action") or "").strip()
            if sig == "即将上涨关注" or act in ("关注买入", "阶梯抄底关注"):
                summary["即将上涨关注"] += 1
            elif sig == "上涨见顶关注" or act in ("关注卖出", "阶梯止盈关注"):
                summary["上涨见顶关注"] += 1
            else:
                summary["下跌观望"] += 1
        summary["count"] = len(stocks_list)
        summary["free_preview"] = True
        return summary

    # 仅当整体不比旧缓存更旧时才写入进程缓存
    data = {
        "success": True,
        "stocks": stocks,
        "count": len(stocks),
        "total": len(stocks),
        "computing": False,
        "free_preview": True,
        "updated_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "notes": {},
        "summary": _sum_stocks(stocks),
        "cached": False,
    }
    try:
        old_stocks = ((_FREE_PREVIEW_CACHE.get("data") or {}).get("stocks") or [])
        # 逐行防回退后再存
        merged_store = []
        old_map = {str(s.get("code")).upper(): s for s in old_stocks if isinstance(s, dict)}
        for s in stocks:
            cu = str((s or {}).get("code") or "").upper()
            merged_store.append(_row_fresher(s, old_map.get(cu)) if cu in old_map else s)
        data["stocks"] = merged_store
        data["summary"] = _sum_stocks(merged_store)
        _FREE_PREVIEW_CACHE = {"ts": now, "data": data}
    except Exception:
        _FREE_PREVIEW_CACHE = {"ts": now, "data": data}

    return JSONResponse(content=_to_jsonable(data),
                        headers={"Cache-Control": "no-store, max-age=0"})



class VerifyFreshRequest(BaseModel):
    symbols: list = []  # [{code,name}] 或 ["AAPL",...]


@app.post("/api/watchlist/verify-fresh")
async def watchlist_verify_fresh(req: VerifyFreshRequest, request: Request = None,
                                 authorization: Optional[str] = Header(None)):
    """刷新完成后二次校验：未达最新交易日的代码强制实拉，并返回 data_source 便于定位缓存/接口。"""
    if not _is_free_preview_symbol(""):  # always rate-limit lightly
        pass
    _rate_check(authorization, request, "verify_fresh", 30, 60)
    from watchlist import verify_and_refresh_symbols
    items = []
    for s in (req.symbols or []):
        if isinstance(s, dict):
            items.append((s.get("code") or s.get("symbol"), s.get("name") or ""))
        else:
            items.append((s, str(s)))
    # 未传则用当前用户自选
    if not items:
        user = auth.get_optional_user(authorization)
        if user:
            try:
                token = _bearer(authorization) if authorization else ""
                for it in watchlist_store.get_all(user["id"], token or None):
                    items.append((it.get("symbol"), it.get("name") or ""))
            except Exception:
                pass
    if not items:
        return {"success": True, "stocks": [], "details": [], "fresh_count": 0, "stale_count": 0}
    out = verify_and_refresh_symbols(items[:80])
    return out


@app.get("/api/watchlist")
async def get_watchlist(refresh: bool = False, user: Optional[dict] = Depends(_optional_user),
                        authorization: Optional[str] = Header(None)):
    """获取自选看板。已登录用其自选（按用户排序、附带备注），未登录回退默认看板。

    refresh=1 时强制后台重新计算（前端「刷新」按钮使用，支持逐行渐进返回）。
    """
    user_id = user["id"] if user else None
    try:
        is_admin = bool(user and (user.get("is_admin") or False))
        # 若 profile 里才有 is_admin，尽量补一次
        if user and not is_admin:
            try:
                import user_store
                p = user_store.get_or_create_profile(user["id"], user.get("email") or "")
                is_admin = bool(p.get("is_admin"))
            except Exception:
                pass
        token = _bearer(authorization) if authorization else ""
        data = get_watchlist_status(
            user_id, force=refresh, is_admin=is_admin, access_token=token or None
        )
        data["user_scoped"] = user_id is not None
        if not isinstance(data.get("stocks"), list):
            data["stocks"] = []
        if user_id:
            try:
                items = watchlist_store.get_all(user_id, token or None)
                order = [i["symbol"] for i in items]
                notes = {i["symbol"]: (i.get("note") or "") for i in items}
                if order and data["stocks"]:
                    rank = {s: i for i, s in enumerate(order)}
                    data["stocks"].sort(key=lambda x: rank.get(x.get("code"), 1 << 30))
                data["notes"] = notes
                if not order:
                    if is_admin or data.get("stocks"):
                        # 管理员默认池 / 读库降级精简默认：保留已计算结果
                        data["empty"] = False
                        data["default_board"] = True
                    else:
                        data["empty"] = True
                        data["stocks"] = []
                        data["count"] = 0
                        data["total"] = 0
                        data["computing"] = False
            except Exception:
                data.setdefault("notes", {})
        return JSONResponse(content=_to_jsonable(data),
                            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    except Exception as e:
        raise _client_http_error(500, "获取自选列表失败，请稍后重试", e)


class WatchAddRequest(BaseModel):
    symbol: str
    name: str = ""


@app.post("/api/watchlist/add")
async def watchlist_add(req: WatchAddRequest, user: dict = Depends(auth.get_current_user),
                        authorization: Optional[str] = Header(None)):
    """添加自选（需登录）。支持「代码」或「名称」输入；多市场自动归一化。"""
    raw = (req.symbol or "").strip()
    if not raw:
        raise HTTPException(400, "代码/名称不能为空")
    name = (req.name or "").strip()
    # 代码型（不含中文）直接当作代码；否则视为名称，走搜索解析
    if not symbols.is_code_like(raw):
        try:
            hits = fetcher.search(raw)
            if hits:
                raw = hits[0]["code"]
                name = name or hits[0].get("name", "")
        except Exception:
            pass
    norm = symbols.normalize_symbol(raw)
    if not name:
        try:
            name = fetcher.lookup_name(norm["symbol"]) or ""
        except Exception:
            name = ""
    ok = watchlist_store.add(user["id"], norm["symbol"], name, norm["market"], "",
                             _bearer(authorization))
    try:
        invalidate_user_cache(user["id"])
    except Exception:
        pass
    return {"success": ok, "symbol": norm["symbol"], "name": name, "market": norm["market"]}


@app.delete("/api/watchlist/remove")
async def watchlist_remove(symbol: str = Query(...), user: dict = Depends(auth.get_current_user),
                           authorization: Optional[str] = Header(None)):
    """删除自选（需登录）。"""
    symbol = (symbol or "").strip().upper()
    ok = watchlist_store.remove(user["id"], symbol, _bearer(authorization))
    try:
        invalidate_user_cache(user["id"])
    except Exception:
        pass
    return {"success": ok, "symbol": symbol}


class WatchReorderRequest(BaseModel):
    order: list = []   # 期望的完整 symbol 顺序


@app.post("/api/watchlist/reorder")
async def watchlist_reorder(req: WatchReorderRequest, user: dict = Depends(auth.get_current_user),
                            authorization: Optional[str] = Header(None)):
    """调整自选顺序（需登录）。传入期望的 symbol 顺序列表。"""
    ok = watchlist_store.reorder(user["id"], [str(s).upper() for s in req.order],
                                 _bearer(authorization))
    return {"success": ok}


class WatchNoteRequest(BaseModel):
    symbol: str
    note: str = ""


@app.post("/api/watchlist/note")
async def watchlist_note(req: WatchNoteRequest, user: dict = Depends(auth.get_current_user),
                         authorization: Optional[str] = Header(None)):
    """为某自选添加/修改备注（需登录）。"""
    symbol = (req.symbol or "").strip().upper()
    if not symbol:
        raise HTTPException(400, "代码不能为空")
    ok = watchlist_store.set_note(user["id"], symbol, req.note or "", _bearer(authorization))
    return {"success": ok, "symbol": symbol}


# ================================================================
#  分析历史（关联用户，可回溯查看）
# ================================================================
@app.get("/api/history")
async def get_history(user: dict = Depends(auth.get_current_user),
                      authorization: Optional[str] = Header(None),
                      symbol: str = Query(None, description="可选：按标的过滤"),
                      limit: int = Query(20, ge=1, le=100),
                      offset: int = Query(0, ge=0)):
    """返回当前用户的分析历史（按时间倒序）。需登录。"""
    rows = analysis_store.list_for_user(
        user["id"], symbol=symbol, limit=limit, offset=offset,
        access_token=_bearer(authorization),
    )
    return {"success": True, "items": rows, "count": len(rows)}


# ================================================================
#  分析 / 回测 / 阶梯 / 公司简介
# ================================================================
@app.post("/api/analyze")
async def analyze_csv(
    file: UploadFile = File(None),
    capital: float = Form(100000),
    position: float = Form(0),
    entry_price: float = Form(0),
    use_sample: bool = Form(False),
    request: Request = None,
    authorization: Optional[str] = Header(None),
):
    """分析上传的 CSV 或模拟数据。模拟数据对免费用户开放，真实分析需 Pro。"""
    _rate_check(authorization, request, "analyze", 10, 60)
    try:
        if use_sample or file is None:
            df = generate_sample_data(300)
            sym_label = "模拟数据"
        else:
            _require_pro(authorization)
            content = await file.read()
            df = pd.read_csv(io.BytesIO(content))

            col_map = {}
            for c in df.columns:
                cl = c.lower().strip()
                if cl in ['date', 'datetime', 'time', '日期', '时间']:
                    col_map[c] = 'date'
                elif cl in ['open', '开盘']:
                    col_map[c] = 'open'
                elif cl in ['high', '最高']:
                    col_map[c] = 'high'
                elif cl in ['low', '最低']:
                    col_map[c] = 'low'
                elif cl in ['close', '收盘']:
                    col_map[c] = 'close'
                elif cl in ['volume', 'vol', '成交量']:
                    col_map[c] = 'volume'
            df = df.rename(columns=col_map)

            required = ['open', 'high', 'low', 'close', 'volume']
            missing = [c for c in required if c not in df.columns]
            if missing:
                raise HTTPException(400, f"CSV缺少列: {missing}")

            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df = df.sort_values('date').reset_index(drop=True)
            sym_label = (file.filename or "CSV上传")

        entry = entry_price if entry_price > 0 else None
        strategy = FujimotoStrategy(total_capital=capital, entry_price=entry)
        result = strategy.analyze(df, current_position_pct=position / 100.0)

        nine_turn = calc_nine_turn_display(df)
        extra = _extra_metrics(df)

        response_data = {
            "success": True,
            "data": result_to_dict(result),
            "chart": df_to_chart_json(df, result, show_last=300),
            "nine_turn": nine_turn,
            "high_low": extra["high_low"],
            "valuation": extra["valuation"],
            "volume_convergence": _safe_volume_convergence(df),
            "meta": {
                "rows": len(df),
                "last_close": round(float(df['close'].iloc[-1]), 2),
                "capital": capital,
                "position": position,
                "entry_price": entry,
            }
        }

        # 已登录用户：把本次分析写入历史（CSV/模拟数据以标签作为 symbol）
        user = auth.get_optional_user(authorization)
        if user:
            try:
                record = {
                    "symbol": sym_label,
                    "data": response_data["data"],
                    "nine_turn": nine_turn,
                    "high_low": extra["high_low"],
                    "valuation": extra["valuation"],
                    "meta": response_data["meta"],
                }
                analysis_store.add(user["id"], sym_label, "", record, _bearer(authorization))
            except Exception:
                pass

        return JSONResponse(content=_to_jsonable(response_data))
    except HTTPException:
        raise
    except Exception as e:
        raise _client_http_error(500, "分析失败，请稍后重试", e)


class LadderRequest(BaseModel):
    price_change: float
    current_position: float = 0


@app.post("/api/ladder")
async def calc_ladder(req: LadderRequest, authorization: Optional[str] = Header(None)):
    """藤本茂阶梯仓位计算器"""
    _require_pro(authorization)
    strategy = FujimotoStrategy()
    change = req.price_change / 100.0
    desc, delta = strategy._fujimoto_action(change, req.current_position)

    action_type = "none"
    if delta > 0:
        action_type = "buy"
    elif delta < 0:
        action_type = "sell"

    return {
        "success": True,
        "desc": desc,
        "delta": round(delta * 100, 1),
        "action_type": action_type,
        "price_change": req.price_change,
    }


@app.get("/api/ladder_table")
async def ladder_table(authorization: Optional[str] = Header(None)):
    """返回藤本茂完整阶梯表"""
    _require_pro(authorization)
    return {
        "buy_ladder": [
            {"trigger": "-5%", "action": "不操作", "desc": "噪音区间，不动如山"},
            {"trigger": "-15%", "action": "+10%", "desc": "第一档承接，试探性入场"},
            {"trigger": "-25%", "action": "+25%", "desc": "加重仓，恐慌中接筹码"},
            {"trigger": "-35%+", "action": "止损评估", "desc": "设硬止损，避免深套"},
        ],
        "sell_ladder": [
            {"trigger": "+5%", "action": "持有", "desc": "趋势初期，让利润奔跑"},
            {"trigger": "+15%", "action": "持有", "desc": "趋势确认，不动如山"},
            {"trigger": "+25%", "action": "-10%", "desc": "开始兑现，落袋为安"},
            {"trigger": "+35%", "action": "-20%", "desc": "加速兑现"},
            {"trigger": "+45%", "action": "-30%", "desc": "大幅减仓"},
            {"trigger": "+60%", "action": "-40%", "desc": "接近清仓"},
            {"trigger": "+100%", "action": "清仓", "desc": "极端泡沫，离场观望"},
        ]
    }


class BacktestRequest(BaseModel):
    symbol: str = ""
    days: int = 300
    initial_capital: float = 100000
    risk_per_trade: float = 0.02
    max_position: float = 0.70
    commission: float = 0.0003
    warmup: int = 60
    entry_price: float = 0
    initial_position_pct: float = 0


@app.post("/api/backtest")
async def run_backtest(req: BacktestRequest, request: Request = None,
                       authorization: Optional[str] = Header(None)):
    """执行策略回测"""
    _require_pro(authorization)
    _rate_check(authorization, request, "backtest", 10, 60)
    try:
        if req.symbol:
            need = int(req.warmup or 60) + 30
            days = max(int(req.days or 300), need + 30, 200)
            try:
                from data_fetcher import invalidate_kline_cache
                invalidate_kline_cache(req.symbol)
            except Exception:
                pass
            df = fetcher.fetch(req.symbol, days)
            # 仍过短则再清缓存拉一次
            if df is None or len(df) < need:
                try:
                    invalidate_kline_cache(req.symbol)
                except Exception:
                    pass
                df = fetcher.fetch(req.symbol, max(days, 400))
            # 回测同样落库每日数据
            try:
                daily_store.store_daily_bars(req.symbol, df, source="backtest")
            except Exception:
                pass
        else:
            df = generate_sample_data(req.days)

        if df is None or len(df) < req.warmup + 30:
            raise ValueError(
                f"数据不足: 需要{req.warmup+30}根，实际{0 if df is None else len(df)}根。"
                f"可能是行情源临时截断或缓存过短，请稍后重试或更换代码。"
            )

        _mp = float(req.max_position or 0.7)
        if _mp > 1.0:
            _mp = min(_mp / 100.0, 1.0)
        _mp = max(0.05, min(_mp, 1.0))
        bt = Backtester(
            initial_capital=req.initial_capital,
            risk_per_trade=req.risk_per_trade,
            max_position=_mp,
            commission=req.commission,
            warmup=req.warmup
        )
        _ep = float(getattr(req, "entry_price", 0) or 0) or None
        _ip = float(getattr(req, "initial_position_pct", 0) or 0)
        if _ip > 1.0:
            _ip = _ip / 100.0
        result = bt.run(df, entry_price=_ep, initial_position_pct=_ip)

        if result.config.get("error"):
            raise ValueError(result.config["error"])

        return JSONResponse(content=_to_jsonable({
            "success": True,
            "symbol": req.symbol or "模拟数据",
            "result": bt_to_dict(result)
        }))
    except Exception as e:
        raise _client_http_error(400, "回测失败，请检查参数后重试", e)


@app.get("/api/profile")
async def get_profile(symbol: str = Query(..., description="股票代码")):
    """获取公司主营业务简介 + 行业分类（best-effort，失败返回 null）"""
    try:
        summary = fetcher.fetch_profile(symbol)
        industry = fetcher.fetch_industry(symbol)
        return {"success": True, "symbol": symbol,
                "business_summary": summary, "industry": industry}
    except Exception:
        return {"success": True, "symbol": symbol,
                "business_summary": None, "industry": None}


if __name__ == "__main__":
    import uvicorn
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


# ========== 订阅 / Waffo Pancake（首选）+ Polar/Stripe 遗留；统一 $9.9/月；lifetime 不受影响 ==========

def _billing_provider_configured() -> dict:
    waffo = False
    try:
        waffo = waffo_client.configured()
    except Exception:
        waffo = bool(
            os.getenv("WAFFO_MERCHANT_ID", "").strip()
            and os.getenv("WAFFO_PRIVATE_KEY", "").strip()
            and os.getenv("WAFFO_PRODUCT_ID", "").strip()
        )
    polar = bool(os.getenv("POLAR_ACCESS_TOKEN", "").strip() and os.getenv("POLAR_PRODUCT_ID", "").strip())
    polar_link = bool(os.getenv("POLAR_CHECKOUT_LINK", "").strip())
    stripe = bool(os.getenv("STRIPE_SECRET_KEY", "").strip() and os.getenv("STRIPE_PRICE_ID", "").strip())
    if waffo:
        provider = "waffo"
    elif polar or polar_link:
        provider = "polar"
    elif stripe:
        provider = "stripe"
    else:
        provider = None
    return {
        "waffo": waffo,
        "polar": polar or polar_link,
        "polar_api": polar,
        "polar_link": polar_link,
        "stripe": stripe,
        "any": bool(provider),
        "provider": provider,
    }


def _client_ip(request: Request) -> str:
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if xff:
        return xff
    return (request.headers.get("cf-connecting-ip")
            or request.headers.get("true-client-ip")
            or (request.client.host if request.client else "")
            or "")


def _verify_polar_webhook(body: bytes, headers, secret: str) -> bool:
    """Standard Webhooks (Polar): webhook-id / webhook-timestamp / webhook-signature."""
    import base64
    import hashlib
    import hmac
    import time

    if not secret:
        return False
    msg_id = headers.get("webhook-id") or headers.get("Webhook-Id")
    ts = headers.get("webhook-timestamp") or headers.get("Webhook-Timestamp")
    sig_header = headers.get("webhook-signature") or headers.get("Webhook-Signature")
    if not msg_id or not ts or not sig_header:
        return False
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except Exception:
        return False

    # secret 可能是 polar_whs_xxx / whsec_xxx / 原始串
    raw = secret
    for prefix in ("polar_whs_", "whsec_"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    key_candidates = []
    try:
        key_candidates.append(base64.b64decode(raw))
    except Exception:
        pass
    key_candidates.append(raw.encode("utf-8"))

    signed = f"{msg_id}.{ts}.".encode("utf-8") + body
    expected_set = set()
    for key in key_candidates:
        dig = hmac.new(key, signed, hashlib.sha256).digest()
        expected_set.add(base64.b64encode(dig).decode("ascii"))

    for part in sig_header.split(" "):
        part = part.strip()
        if not part:
            continue
        # v1,<base64>
        sig = part.split(",", 1)[-1] if "," in part else part
        if sig in expected_set:
            return True
    return False


def _uid_from_polar_data(data: dict) -> str:
    if not isinstance(data, dict):
        return ""
    # checkout / order / subscription 常见挂载点
    meta = data.get("metadata") or {}
    if isinstance(meta, dict):
        uid = meta.get("user_id") or meta.get("external_customer_id") or ""
        if uid:
            return str(uid)
    cust = data.get("customer") or {}
    if isinstance(cust, dict):
        uid = cust.get("external_id") or ""
        if uid:
            return str(uid)
    uid = data.get("external_customer_id") or ""
    return str(uid) if uid else ""


@app.get("/api/billing/status")
async def billing_status(user: dict = Depends(auth.get_current_user)):
    profile = user_store.get_or_create_profile(user["id"], user.get("email", ""))
    ent = user_store.entitlement_from_profile(
        profile, is_admin_user=bool(user.get("is_admin") or profile.get("is_admin"))
    )
    cfg = _billing_provider_configured()
    return {
        "success": True,
        **ent,
        "provider": cfg.get("provider"),
        "waffo_configured": bool(cfg.get("waffo")),
        "polar_configured": bool(cfg.get("polar")),
        "stripe_configured": bool(cfg.get("stripe")),
    }


@app.post("/api/billing/checkout")
async def billing_checkout(request: Request, user: dict = Depends(auth.get_current_user)):
    """创建结账会话：优先 Polar，其次静态 Checkout Link，最后 Stripe。"""
    profile = user_store.get_or_create_profile(user["id"], user.get("email", ""))
    ent = user_store.entitlement_from_profile(
        profile, is_admin_user=bool(user.get("is_admin") or profile.get("is_admin"))
    )
    # 已有付费权益：若请求升级到 plus 且当前仅 basic，仍允许结账
    _req_plan = "basic"
    try:
        _body_early = {}
    except Exception:
        pass
    if ent.get("plan") in ("lifetime",) and ent.get("entitled"):
        return {"success": True, "already_entitled": True, "plan": ent.get("plan")}
    if ent.get("plan") in ("plus",) and ent.get("entitled"):
        return {"success": True, "already_entitled": True, "plan": ent.get("plan")}

    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    success_url = (body.get("success_url") or os.getenv("BILLING_SUCCESS_URL") or "").strip()
    if not success_url:
        success_url = "https://timebricks.bid/?billing=success"
    # 防开放重定向：仅允许本站与 CORS 白名单域名
    try:
        from urllib.parse import urlparse
        _su = urlparse(success_url)
        _host = (_su.hostname or "").lower()
        _allowed_hosts = {"timebricks.bid", "www.timebricks.bid"}
        for o in allow_origins:
            if o and o != "*":
                try:
                    h = urlparse(o if "://" in o else f"https://{o}").hostname
                    if h:
                        _allowed_hosts.add(h.lower())
                except Exception:
                    pass
        _origin = (request.headers.get("origin") or "").strip()
        if _origin:
            try:
                oh = urlparse(_origin).hostname
                if oh:
                    _allowed_hosts.add(oh.lower())
            except Exception:
                pass
        if _su.scheme not in ("https", "http") or _host not in _allowed_hosts:
            success_url = "https://timebricks.bid/?billing=success"
    except Exception:
        success_url = "https://timebricks.bid/?billing=success"

    cfg = _billing_provider_configured()
    if not cfg.get("any"):
        raise HTTPException(
            status_code=503,
            detail="未配置收款：请设置 WAFFO_MERCHANT_ID / WAFFO_PRIVATE_KEY / WAFFO_PRODUCT_ID",
        )

    # ---- Waffo Pancake（首选）----
    if cfg.get("waffo"):
        if not success_url:
            origin = (request.headers.get("origin") or "").rstrip("/")
            if origin:
                success_url = origin + "/?billing=success"
        uid = user["id"]
        want = (body.get("plan") or "pro").strip().lower()
        if want in ("basic", "pro"):
            want = "pro"
        elif want != "plus":
            want = "pro"
        product_id = os.getenv("WAFFO_PRODUCT_ID", "").strip()
        price_pro = float(os.getenv("PRO_PRICE_USD", "9.9") or "9.9")
        price_plus = float(os.getenv("PLUS_PRICE_USD", str(round(price_pro + 3, 1))) or (price_pro + 3))
        extra = {}
        if want == "plus":
            plus_pid = os.getenv("WAFFO_PRODUCT_ID_PLUS", "").strip()
            if plus_pid:
                product_id = plus_pid
            else:
                # 无独立 Plus 产品时，用价格快照 +$3
                extra["priceSnapshot"] = {
                    "amount": f"{price_plus:.2f}",
                    "taxCategory": "saas",
                }
        try:
            session = waffo_client.create_checkout_session(
                product_id=product_id or None,
                buyer_email=(user.get("email") or "").strip().lower() or None,
                success_url=success_url or None,
                order_merchant_external_id=str(uid),
                metadata={"userId": str(uid), "user_id": str(uid), "plan": want},
                **{k: v for k, v in extra.items()},
            )
        except TypeError:
            # 旧版 create_checkout_session 无 priceSnapshot 参数
            try:
                session = waffo_client.create_checkout_session(
                    product_id=product_id or None,
                    buyer_email=(user.get("email") or "").strip().lower() or None,
                    success_url=success_url or None,
                    order_merchant_external_id=str(uid),
                    metadata={"userId": str(uid), "user_id": str(uid), "plan": want},
                )
            except Exception as e:
                raise _client_http_error(502, "创建结账失败，请稍后重试或联系客服", e)
        except Exception as e:
            raise _client_http_error(502, "创建结账失败，请稍后重试或联系客服", e)
        url = session.get("checkoutUrl") or session.get("url")
        if not url:
            raise HTTPException(status_code=502, detail=f"Waffo 未返回 checkoutUrl: {session}")
        # 记录待确认，支付回跳时可确认开通（防 webhook 延迟/丢单）
        try:
            import cache as _cache
            sid = session.get("sessionId") or session.get("id") or ""
            _cache.set_setting(
                f"billing.pending.{uid}",
                {"session_id": sid, "plan": want, "ts": __import__("time").time()},
            ) if hasattr(_cache, "set_setting") else None
            if hasattr(_cache, "set_cache"):
                _cache.set_cache(f"billing.pending.{uid}", {"session_id": sid, "plan": want}, ttl=7200)
        except Exception:
            try:
                import settings_store
                import json as _json, time as _time
                settings_store.set_setting(
                    f"billing.pending.{uid}",
                    _json.dumps({"session_id": session.get("sessionId"), "plan": want, "ts": _time.time()}),
                )
            except Exception:
                pass
        return {
            "success": True,
            "url": url,
            "provider": "waffo",
            "session_id": session.get("sessionId") or session.get("id"),
            "plan": want,
        }

    # ---- Polar API Checkout（遗留）----
    polar_token = os.getenv("POLAR_ACCESS_TOKEN", "").strip()
    polar_product = os.getenv("POLAR_PRODUCT_ID", "").strip()
    if polar_token and polar_product:
        import urllib.request
        import urllib.error

        payload = {
            "products": [polar_product],
            "success_url": success_url if "{CHECKOUT_ID}" in success_url else (
                success_url + ("&" if "?" in success_url else "?") + "checkout_id={CHECKOUT_ID}"
            ),
            "external_customer_id": user["id"],
            "customer_email": (user.get("email") or "") or None,
            "metadata": {
                "user_id": user["id"],
                "email": user.get("email") or "",
            },
        }
        ip = _client_ip(request)
        if ip:
            payload["customer_ip_address"] = ip
        # 去掉 None
        payload = {k: v for k, v in payload.items() if v is not None}

        api_base = os.getenv("POLAR_API_BASE", "https://api.polar.sh").rstrip("/")
        req = urllib.request.Request(
            f"{api_base}/v1/checkouts/",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {polar_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
            raise _client_http_error(502, "创建结账失败，请稍后重试或联系客服", e)
        except Exception as e:
            raise _client_http_error(502, "支付服务暂时不可用，请稍后重试", e)

        url = data.get("url")
        if not url:
            raise HTTPException(status_code=502, detail="Polar 未返回 checkout url")
        return {"success": True, "url": url, "provider": "polar", "session_id": data.get("id")}

    # ---- Polar 静态 Checkout Link（后台配置好的固定链接）----
    polar_link = os.getenv("POLAR_CHECKOUT_LINK", "").strip()
    if polar_link:
        import urllib.parse
        sep = "&" if "?" in polar_link else "?"
        q = f"customer_email={urllib.parse.quote(user.get('email') or '')}"
        q += f"&external_customer_id={urllib.parse.quote(user['id'])}"
        return {
            "success": True,
            "url": f"{polar_link}{sep}{q}",
            "provider": "polar_link",
        }

    # ---- Stripe 回退 ----
    secret = os.getenv("STRIPE_SECRET_KEY", "").strip()
    price_id = os.getenv("STRIPE_PRICE_ID", "").strip()
    if secret and price_id:
        try:
            import stripe
            stripe.api_key = secret
        except ImportError:
            raise HTTPException(status_code=503, detail="未安装 stripe 包")
        cancel_url = (body.get("cancel_url") or os.getenv("BILLING_CANCEL_URL") or success_url).strip()
        kwargs = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "client_reference_id": user["id"],
            "metadata": {"user_id": user["id"], "email": user.get("email") or ""},
            "customer_email": user.get("email") or None,
        }
        session = stripe.checkout.Session.create(**{k: v for k, v in kwargs.items() if v is not None})
        return {"success": True, "url": session.url, "provider": "stripe", "session_id": session.id}

    raise HTTPException(
        status_code=503,
        detail="未配置收款：请设置 POLAR_ACCESS_TOKEN + POLAR_PRODUCT_ID（或 POLAR_CHECKOUT_LINK）",
    )




@app.post("/api/billing/confirm")
async def billing_confirm(user: dict = Depends(auth.get_current_user)):
    """支付成功回跳后由前端调用：若存在 pending 结账记录则开通对应套餐（弥补 webhook 延迟）。"""
    uid = user["id"]
    profile = user_store.get_or_create_profile(uid, user.get("email", ""))
    cur = (profile.get("plan") or "free").lower()
    if cur in ("lifetime", "plus"):
        ent = user_store.entitlement_from_profile(profile, is_admin_user=bool(profile.get("is_admin")))
        return {"success": True, "already": True, **ent}
    pending = None
    try:
        import settings_store, json as _json
        raw = settings_store.get_setting(f"billing.pending.{uid}", None)
        if isinstance(raw, str) and raw:
            pending = _json.loads(raw)
        elif isinstance(raw, dict):
            pending = raw
    except Exception:
        pending = None
    if not pending:
        # 无 pending 时：若已是 basic/pro 直接返回；否则不擅自开通
        if cur in ("basic", "pro", "plus"):
            ent = user_store.entitlement_from_profile(profile, is_admin_user=bool(profile.get("is_admin")))
            return {"success": True, "already": True, **ent}
        raise HTTPException(status_code=400, detail="未找到待确认的支付会话，请等待 webhook 同步或联系客服")
    want = (pending.get("plan") or "pro").lower()
    if want in ("basic", "pro"):
        want = "pro"
    elif want != "plus":
        want = "pro"
    # 不可降级：已 plus 不写成 basic
    if cur == "plus":
        want = "plus"
    user_store.set_plan(uid, plan=want, plan_source="waffo")
    try:
        import settings_store
        settings_store.set_setting(f"billing.pending.{uid}", "")
    except Exception:
        pass
    profile = user_store.get_or_create_profile(uid, user.get("email", ""))
    ent = user_store.entitlement_from_profile(profile, is_admin_user=bool(profile.get("is_admin")))
    return {"success": True, "confirmed": True, **ent}


@app.post("/api/billing/cancel")
async def billing_cancel(user: dict = Depends(auth.get_current_user)):
    """取消自动续费（Waffo：账期结束才失效；lifetime/赠送不可取消）。"""
    import json as _json
    from datetime import datetime as _dt
    uid = user["id"]
    profile = user_store.get_or_create_profile(uid, user.get("email", ""))
    plan = (profile.get("plan") or "").lower()
    source = (profile.get("plan_source") or "").lower()
    if plan == "lifetime" or source in ("grandfather", "admin", "manual", "gift", "lifetime", "comp", "complimentary"):
        raise HTTPException(status_code=400, detail="赠送/老用户权益不支持自助取消")
    if plan not in ("pro", "basic", "plus"):
        return {"success": True, "message": "当前不是付费订阅", "status": "none"}
    order_id = (profile.get("stripe_subscription_id") or "").strip()
    if not order_id:
        raise HTTPException(status_code=400, detail="未找到订阅订单号，请联系客服处理")

    # 已提交过取消：幂等返回，避免重复调支付侧
    try:
        prev = settings_store.get_setting(f"billing.cancel.{uid}", None)
        if isinstance(prev, str) and prev:
            try:
                prev = _json.loads(prev)
            except Exception:
                prev = {"raw": prev}
        if isinstance(prev, dict) and (prev.get("status") or "").lower() in ("canceling", "canceled"):
            return {
                "success": True,
                "status": prev.get("status") or "canceling",
                "message": "已取消自动续费，权益保留至当前账期结束",
                "cancel_requested_at": prev.get("at"),
            }
    except Exception:
        pass

    if source == "waffo" or order_id.startswith("ORD_"):
        try:
            import waffo_client
            res = waffo_client.api_call(
                "POST",
                "/v1/actions/subscription-order/cancel-order",
                {"orderId": order_id},
            )
        except Exception as e:
            # 支付侧可能已取消：仍记本地状态，避免用户反复点
            err_s = str(e).lower()
            if any(k in err_s for k in ("already", "cancel", "not active", "已取消")):
                try:
                    settings_store.set_setting(
                        f"billing.cancel.{uid}",
                        _json.dumps({"status": "canceling", "at": _dt.utcnow().isoformat() + "Z", "order_id": order_id}, ensure_ascii=False),
                    )
                except Exception:
                    pass
                return {"success": True, "status": "canceling", "message": "已取消自动续费，权益保留至当前账期结束"}
            raise _client_http_error(502, "取消订阅失败，请稍后重试或联系客服", e)
        # 周期结束才降级；本地标记 canceling，plan 仍保持至 webhook
        try:
            settings_store.set_setting(
                f"billing.cancel.{uid}",
                _json.dumps({
                    "status": "canceling",
                    "at": _dt.utcnow().isoformat() + "Z",
                    "order_id": order_id,
                }, ensure_ascii=False),
            )
        except Exception:
            pass
        return {"success": True, "status": "canceling", "raw": res, "message": "已提交取消，权益保留至当前账期结束"}
    raise HTTPException(status_code=400, detail="当前订阅渠道暂不支持自助取消")


@app.get("/api/billing/portal")
async def billing_portal(user: dict = Depends(auth.get_current_user)):
    """返回订阅管理信息（状态、订单号、是否可取消/退费）。

    赠送/老用户（lifetime、grandfather、admin 等）不可自助取消或退费。
    """
    profile = user_store.get_or_create_profile(user["id"], user.get("email", ""))
    ent = user_store.entitlement_from_profile(
        profile, is_admin_user=bool(user.get("is_admin") or profile.get("is_admin"))
    )
    raw_plan = (profile.get("plan") or "").strip().lower()
    source = (profile.get("plan_source") or ent.get("plan_source") or "").strip().lower()
    order_id = (profile.get("stripe_subscription_id") or "").strip()
    # 赠送权益：老用户特殊处理、终身、管理员开通等
    complimentary = (
        raw_plan == "lifetime"
        or (ent.get("plan") or "").lower() == "lifetime"
        or source in ("grandfather", "admin", "manual", "gift", "lifetime", "comp", "complimentary")
        or (ent.get("reason") or "") in ("grandfather", "admin")
    )
    paid_plan = raw_plan in ("pro", "basic", "plus") or (ent.get("plan") or "").lower() in ("pro", "plus")
    can_manage = bool(paid_plan and order_id and not complimentary)
    cancel_pending = False
    cancel_requested_at = None
    try:
        import json as _json
        prev = settings_store.get_setting(f"billing.cancel.{user['id']}", None)
        if isinstance(prev, str) and prev:
            try:
                prev = _json.loads(prev)
            except Exception:
                prev = None
        if isinstance(prev, dict) and (prev.get("status") or "").lower() in ("canceling", "canceled"):
            cancel_pending = True
            cancel_requested_at = prev.get("at")
    except Exception:
        pass
    return {
        "success": True,
        **ent,
        "order_id": order_id,
        "plan_source": source or ent.get("plan_source") or "",
        "complimentary": complimentary,
        "can_cancel": can_manage and not cancel_pending,
        "can_refund": can_manage,
        "cancel_pending": cancel_pending,
        "cancel_requested_at": cancel_requested_at,
    }


@app.post("/api/billing/webhook/waffo")
@app.post("/api/billing/webhook/polar")
@app.post("/api/billing/webhook")  # 兼容旧路径；优先 Waffo，再 Polar / Stripe
async def billing_webhook(request: Request):
    """Waffo / Polar / Stripe Webhook → 更新 plan。lifetime / grandfather 永不降级。"""
    body = await request.body()
    polar_secret = os.getenv("POLAR_WEBHOOK_SECRET", "").strip()
    stripe_wh = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    stripe_key = os.getenv("STRIPE_SECRET_KEY", "").strip()

    # ---------- Waffo Pancake ----------
    waffo_sig = request.headers.get("X-Waffo-Signature") or request.headers.get("x-waffo-signature")
    try:
        _probe = json.loads(body.decode("utf-8") or "{}")
    except Exception:
        _probe = {}
    _etype_probe = (_probe.get("eventType") or _probe.get("type") or _probe.get("event") or "").strip()
    looks_waffo = bool(waffo_sig) or _etype_probe.startswith(("order.", "subscription.", "refund."))
    if looks_waffo and (waffo_sig or os.getenv("WAFFO_MERCHANT_ID", "").strip()):
        raw_text = body.decode("utf-8", errors="replace")
        pub = waffo_client.load_webhook_public_key_pem()
        if pub and waffo_sig:
            if not waffo_client.verify_webhook_signature(raw_text, waffo_sig):
                raise HTTPException(status_code=401, detail="Waffo webhook 签名校验失败")
        try:
            event = json.loads(raw_text)
        except Exception:
            raise HTTPException(status_code=400, detail="无效 JSON")
        etype = (event.get("eventType") or event.get("type") or event.get("event") or "").strip()
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        uid = waffo_client.extract_uid_from_event(event)
        if not uid:
            email = (data.get("buyerEmail") or data.get("buyer_email") or data.get("email") or "").strip().lower()
            if email and supabase_client.using_supabase():
                try:
                    rows = (supabase_client.get_service_client()
                            .table("profiles").select("id")
                            .eq("email", email).limit(1).execute())
                    if rows.data:
                        uid = rows.data[0]["id"]
                except Exception:
                    pass
        order_id = data.get("orderId") or data.get("id") or event.get("id")
        exp = data.get("currentPeriodEnd") or data.get("current_period_end")
        if isinstance(exp, (int, float)):
            exp = None

        activate = etype in (
            "subscription.activated",
            "subscription.uncanceled",
            "subscription.updated",
            "order.completed",
            "subscription.payment_succeeded",
        )
        if etype == "subscription.updated":
            st = (data.get("orderStatus") or data.get("status") or "").lower()
            if st and st not in ("active", "trialing"):
                activate = False
        if activate and uid:
            meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
            want = (meta.get("plan") or meta.get("user_plan") or "pro")
            want = str(want).lower()
            if want in ("basic", "pro"):
                want = "pro"
            elif want != "plus":
                want = "pro"
            # 不覆盖 lifetime；plus 不被 basic 降级
            prev = {}
            try:
                prev = user_store.get_or_create_profile(uid) or {}
                prev_plan = (prev.get("plan") or "").lower()
                if prev_plan == "lifetime" or (prev.get("plan_source") or "") == "grandfather":
                    want = "lifetime"
                elif prev_plan == "plus" and want == "pro":
                    want = "plus"
            except Exception:
                prev = {}
            if want == "lifetime":
                pass  # 不改动终身
            else:
                user_store.set_plan(
                    uid,
                    plan=want,
                    plan_source="waffo",
                    stripe_subscription_id=str(order_id) if order_id else None,
                    plan_expires_at=str(exp) if exp else None,
                )
            try:
                import settings_store
                settings_store.set_setting(f"billing.pending.{uid}", "")
            except Exception:
                pass
        elif etype in ("subscription.canceled", "subscription.past_due", "refund.succeeded"):
            if uid:
                try:
                    prof = user_store.get_or_create_profile(uid)
                    if (prof.get("plan") or "") == "lifetime" or (prof.get("plan_source") or "") == "grandfather":
                        pass
                    elif etype == "subscription.past_due":
                        pass
                    else:
                        user_store.set_plan(
                            uid, plan="free", plan_source="waffo",
                            stripe_subscription_id=str(order_id) if order_id else None,
                        )
                except Exception:
                    pass
        return {"received": True, "provider": "waffo", "eventType": etype}

    # ---------- Polar ----------
    if polar_secret and (
        request.headers.get("webhook-id") or request.headers.get("Webhook-Id")
    ):
        if not _verify_polar_webhook(body, request.headers, polar_secret):
            raise HTTPException(status_code=400, detail="Polar webhook 签名校验失败")
        try:
            event = json.loads(body.decode("utf-8"))
        except Exception:
            raise HTTPException(status_code=400, detail="无效 JSON")
        etype = event.get("type") or ""
        data = event.get("data") or {}
        uid = _uid_from_polar_data(data)
        sub_id = data.get("id") if etype.startswith("subscription.") else (data.get("subscription_id") or data.get("subscription"))
        if isinstance(sub_id, dict):
            sub_id = sub_id.get("id")
        cust = data.get("customer")
        cust_id = cust.get("id") if isinstance(cust, dict) else cust

        if etype in (
            "subscription.created",
            "subscription.active",
            "subscription.updated",
            "subscription.uncanceled",
            "order.paid",
            "checkout.updated",
        ):
            # checkout.updated 仅 status=confirmed/succeeded 时放行
            if etype == "checkout.updated":
                st = (data.get("status") or "").lower()
                if st not in ("confirmed", "succeeded", "complete", "completed"):
                    return {"received": True, "ignored": True}
            if not uid:
                # order.paid 可能只有 customer.external_id
                pass
            if uid:
                status = (data.get("status") or "").lower()
                # 取消态不升 pro
                if status in ("canceled", "revoked", "incomplete_expired", "unpaid"):
                    pass
                else:
                    exp = None
                    for k in ("current_period_end", "ends_at", "cancel_at"):
                        v = data.get(k)
                        if v:
                            exp = v if isinstance(v, str) else None
                            break
                    user_store.set_plan(
                        uid,
                        plan="pro",
                        plan_source="polar",
                        stripe_customer_id=str(cust_id) if cust_id else None,
                        stripe_subscription_id=str(sub_id) if sub_id else None,
                        plan_expires_at=exp,
                    )
        elif etype in ("subscription.canceled", "subscription.revoked"):
            if uid:
                try:
                    prof = user_store.get_or_create_profile(uid)
                    if (prof.get("plan") or "") == "lifetime" or (prof.get("plan_source") or "") == "grandfather":
                        pass
                    else:
                        user_store.set_plan(
                            uid, plan="free", plan_source="polar",
                            stripe_customer_id=str(cust_id) if cust_id else None,
                            stripe_subscription_id=str(sub_id) if sub_id else None,
                        )
                except Exception:
                    user_store.set_plan(uid, plan="free", plan_source="polar")
        return {"received": True, "provider": "polar", "type": etype}

    # ---------- Stripe ----------
    if stripe_key:
        try:
            import stripe
            stripe.api_key = stripe_key
        except ImportError:
            raise HTTPException(status_code=503, detail="未安装 stripe")
        if stripe_wh:
            try:
                event = stripe.Webhook.construct_event(
                    body, request.headers.get("stripe-signature", ""), stripe_wh
                )
            except Exception as e:
                raise HTTPException(status_code=400, detail=f"Stripe webhook 签名失败: {e}")
        else:
            event = stripe.Event.construct_from(json.loads(body), stripe_key)

        etype = event["type"] if not isinstance(event, dict) else event.get("type")
        data = event["data"]["object"] if not isinstance(event, dict) else event.get("data", {}).get("object", {})

        if etype == "checkout.session.completed":
            uid = data.get("client_reference_id") or (data.get("metadata") or {}).get("user_id") or ""
            if uid:
                user_store.set_plan(
                    uid, plan="pro", plan_source="stripe",
                    stripe_customer_id=data.get("customer"),
                    stripe_subscription_id=data.get("subscription"),
                )
        elif etype in ("customer.subscription.updated", "customer.subscription.deleted"):
            status = data.get("status")
            cust = data.get("customer")
            sub_id = data.get("id")
            uid = (data.get("metadata") or {}).get("user_id") or ""
            if not uid and cust and supabase_client.using_supabase():
                try:
                    rows = (supabase_client.get_service_client()
                            .table("profiles").select("id")
                            .eq("stripe_customer_id", cust).limit(1).execute())
                    if rows.data:
                        uid = rows.data[0]["id"]
                except Exception:
                    pass
            if uid:
                if status in ("active", "trialing"):
                    exp = None
                    try:
                        period_end = data.get("current_period_end")
                        if period_end:
                            from datetime import datetime, timezone
                            exp = datetime.fromtimestamp(int(period_end), tz=timezone.utc).isoformat()
                    except Exception:
                        exp = None
                    user_store.set_plan(
                        uid, plan="pro", plan_source="stripe",
                        stripe_customer_id=cust, stripe_subscription_id=sub_id,
                        plan_expires_at=exp,
                    )
                else:
                    try:
                        prof = user_store.get_or_create_profile(uid)
                        if (prof.get("plan") or "") == "lifetime" or (prof.get("plan_source") or "") == "grandfather":
                            pass
                        else:
                            user_store.set_plan(
                                uid, plan="free", plan_source="stripe",
                                stripe_customer_id=cust, stripe_subscription_id=sub_id,
                            )
                    except Exception:
                        user_store.set_plan(uid, plan="free", plan_source="stripe")
        return {"received": True, "provider": "stripe", "type": etype}

    raise HTTPException(status_code=503, detail="未配置 Polar 或 Stripe Webhook")
