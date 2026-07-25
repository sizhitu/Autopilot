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
    _detect_high_low, _calc_valuation, STOCK_ROLE, DEFAULT_ROLE,
)
from nine_turn import calc_nine_turn_display
import db
import auth
import mailer
import daily_store
import user_store
import ticket_store
import settings_store
import supabase_client
import symbols
import watchlist_store
import cache
import analysis_store
import ratelimit

app = FastAPI(title="藤本茂融合策略 Web 工具 API", version="3.0")
fetcher = DataFetcher()

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
    allow_methods=["*"],
    allow_headers=["*"],
)


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
        }
    }
    return d


def df_to_chart_json(df: pd.DataFrame, result, show_last=120) -> dict:
    """提取K线+均线数据供前端绘图"""
    recent = df.tail(show_last).copy().reset_index(drop=True)

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
    ma_periods = [5, 10, 20, 30, 50, 100, 150, 200, 250]
    ma_colors = {
        5: "#3498db", 10: "#9b59b6", 20: "#e67e22", 30: "#1abc9c",
        50: "#f39c12", 100: "#e74c3c", 150: "#34495e", 200: "#7f8c8d", 250: "#bdc3c7"
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
@app.get("/api/auth/me")
async def api_me(user: dict = Depends(auth.get_current_user)):
    """返回当前登录用户信息（并防御性同步 profiles 行）。"""
    profile = user_store.get_or_create_profile(user["id"], user.get("email", ""))
    return {
        "success": True,
        "user": {
            "id": user["id"],
            "email": user.get("email", ""),
            "display_name": profile.get("display_name") or (user.get("email", "").split("@")[0]),
            "verified": True,
            "is_admin": bool(profile.get("is_admin") or user.get("is_admin")),
        },
    }


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


@app.post("/api/contact")
async def api_contact(req: ContactRequest):
    """公开咨询入口：收集 姓名/邮箱/国家/问题，落库并立即转发到 support 邮箱（自动建单）。"""
    email = (req.email or "").strip().lower()
    message = (req.message or "").strip()
    if not email or "@" not in email:
        raise HTTPException(400, "请填写有效邮箱")
    if len(message) < 3:
        raise HTTPException(400, "请填写咨询内容")
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
    _rate_check(authorization, request, "search", 60, 60)
    results = fetcher.search(q)
    return {"success": True, "results": results, "count": len(results)}


class QuoteRequest(BaseModel):
    symbol: str
    days: int = 300


@app.post("/api/quote")
async def get_quote(req: QuoteRequest, request: Request = None,
                    authorization: Optional[str] = Header(None)):
    """
    获取真实行情并自动分析。
    数据分层：原始 K 线写入缓存层（不落业务库）；实时拉取失败时，
    优先回退行情缓存，再回退每日 K 线缓存，并标记 stale=True 告知前端数据可能延迟。
    """
    _rate_check(authorization, request, "quote", 20, 60)
    try:
        df = fetcher.fetch(req.symbol, req.days)
        source = "live"
        stale = False
    except Exception as e:
        # 第一层兜底：直接用此前缓存的完整行情分析结果
        cached = cache.get_quote_cache(req.symbol)
        if cached:
            cached["stale"] = True
            return JSONResponse(content=cached,
                                headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
        # 第二层兜底：用缓存的每日 K 线重建 DataFrame
        stored = cache.get_daily_cache(req.symbol)
        df = _df_from_stored(stored) if stored else None
        if df is None or len(df) < 5:
            raise HTTPException(400, f"获取数据失败且无缓存，请稍后重试: {str(e)}")
        source = "cache"
        stale = True

    if len(df) < 5:
        raise HTTPException(400, f"数据不足: 仅{len(df)}根K线，无法分析")

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

    payload = _to_jsonable({
        "success": True,
        "symbol": req.symbol,
        "stale": stale,
        "data": result_to_dict(result),
        "chart": df_to_chart_json(df, result),
        "nine_turn": nine_turn,
        "high_low": extra["high_low"],
        "valuation": extra["valuation"],
        "meta": {
            "rows": len(df),
            "last_close": round(float(df['close'].iloc[-1]), 2),
            "start_date": df['date'].iloc[0].strftime('%Y-%m-%d') if 'date' in df.columns else "",
            "end_date": df['date'].iloc[-1].strftime('%Y-%m-%d') if 'date' in df.columns else "",
        }
    })

    # 实时成功时，把完整行情分析结果写入缓存层（短时 TTL，供失败兜底）
    if source == "live":
        try:
            cache.set_quote_cache(req.symbol, payload)
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
@app.get("/api/watchlist")
async def get_watchlist(user: Optional[dict] = Depends(_optional_user)):
    """获取自选看板。已登录用其自选（按用户排序、附带备注），未登录回退默认看板。"""
    user_id = user["id"] if user else None
    try:
        data = get_watchlist_status(user_id)
        data["user_scoped"] = user_id is not None
        # 已登录：按用户排序顺序重排看板，并附带每只备注
        if user_id:
            try:
                items = watchlist_store.get_all(user_id)
                order = [i["symbol"] for i in items]
                notes = {i["symbol"]: (i.get("note") or "") for i in items}
                if order:
                    rank = {s: i for i, s in enumerate(order)}
                    data["stocks"].sort(key=lambda x: rank.get(x["code"], 1 << 30))
                data["notes"] = notes
            except Exception:
                data["notes"] = {}
        return JSONResponse(content=_to_jsonable(data),
                            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    except Exception as e:
        raise HTTPException(500, f"获取自选列表失败: {str(e)}")


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
    return {"success": ok, "symbol": norm["symbol"], "name": name, "market": norm["market"]}


@app.delete("/api/watchlist/remove")
async def watchlist_remove(symbol: str = Query(...), user: dict = Depends(auth.get_current_user),
                           authorization: Optional[str] = Header(None)):
    """删除自选（需登录）。"""
    symbol = (symbol or "").strip().upper()
    ok = watchlist_store.remove(user["id"], symbol, _bearer(authorization))
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
    """分析上传的 CSV 或模拟数据"""
    _rate_check(authorization, request, "analyze", 10, 60)
    try:
        if use_sample or file is None:
            df = generate_sample_data(300)
            sym_label = "模拟数据"
        else:
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
            "chart": df_to_chart_json(df, result),
            "nine_turn": nine_turn,
            "high_low": extra["high_low"],
            "valuation": extra["valuation"],
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
        import traceback
        raise HTTPException(500, f"分析失败: {str(e)}\n{traceback.format_exc()}")


class LadderRequest(BaseModel):
    price_change: float
    current_position: float = 0


@app.post("/api/ladder")
async def calc_ladder(req: LadderRequest):
    """藤本茂阶梯仓位计算器"""
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
async def ladder_table():
    """返回藤本茂完整阶梯表"""
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


@app.post("/api/backtest")
async def run_backtest(req: BacktestRequest, request: Request = None,
                       authorization: Optional[str] = Header(None)):
    """执行策略回测"""
    _rate_check(authorization, request, "backtest", 10, 60)
    try:
        if req.symbol:
            df = fetcher.fetch(req.symbol, req.days)
            # 回测同样落库每日数据
            try:
                daily_store.store_daily_bars(req.symbol, df, source="backtest")
            except Exception:
                pass
        else:
            df = generate_sample_data(req.days)

        if len(df) < req.warmup + 30:
            raise ValueError(f"数据不足: 需要{req.warmup+30}根，实际{len(df)}根")

        bt = Backtester(
            initial_capital=req.initial_capital,
            risk_per_trade=req.risk_per_trade,
            max_position=req.max_position,
            commission=req.commission,
            warmup=req.warmup
        )
        result = bt.run(df)

        if result.config.get("error"):
            raise ValueError(result.config["error"])

        return JSONResponse(content=_to_jsonable({
            "success": True,
            "symbol": req.symbol or "模拟数据",
            "result": bt_to_dict(result)
        }))
    except Exception as e:
        raise HTTPException(400, f"回测失败: {str(e)}")


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
