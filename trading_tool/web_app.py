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
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query, Header, Depends
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
import daily_store

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
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()
    return auth.get_user_by_token(token)


# ================================================================
#  健康检查
# ================================================================
@app.get("/api/health")
async def health():
    return {"success": True, "service": "autopilot-api", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ================================================================
#  认证 API
# ================================================================
class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str = ""


class VerifyRequest(BaseModel):
    email: str
    code: str


class LoginRequest(BaseModel):
    email: str
    password: str


class ResendRequest(BaseModel):
    email: str


@app.post("/api/auth/register")
async def api_register(req: RegisterRequest):
    """注册（发送邮箱验证码）。未配置 SMTP 时返回 dev_code 便于调试。"""
    r = auth.register(req.email, req.password, req.display_name)
    return JSONResponse(content=r)


@app.post("/api/auth/verify")
async def api_verify(req: VerifyRequest):
    """邮箱验证码校验。"""
    r = auth.verify_email(req.email, req.code)
    return JSONResponse(content=r)


@app.post("/api/auth/login")
async def api_login(req: LoginRequest):
    """登录（需已完成邮箱验证）。"""
    r = auth.login(req.email, req.password)
    return JSONResponse(content=r)


@app.post("/api/auth/resend")
async def api_resend(req: ResendRequest):
    """重新发送验证码。"""
    r = auth.resend_code(req.email)
    return JSONResponse(content=r)


@app.get("/api/auth/me")
async def api_me(user: dict = Depends(auth.get_current_user)):
    """返回当前登录用户信息。"""
    return {
        "success": True,
        "user": {
            "id": user["id"],
            "email": user["email"],
            "display_name": user["display_name"],
            "verified": bool(user["verified"]),
        },
    }


# ================================================================
#  真实数据源 API
# ================================================================
@app.get("/api/search")
async def search_stocks(q: str = Query(..., description="股票代码或名称关键词")):
    """搜索股票代码"""
    results = fetcher.search(q)
    return {"success": True, "results": results, "count": len(results)}


class QuoteRequest(BaseModel):
    symbol: str
    days: int = 300


@app.post("/api/quote")
async def get_quote(req: QuoteRequest):
    """
    获取真实行情并自动分析。
    容错：实时拉取失败时，回退到本地已存储的每日行情（daily_data），
    并在响应中标记 stale=True 告知前端数据为缓存。
    """
    try:
        df = fetcher.fetch(req.symbol, req.days)
        source = "live"
        stale = False
    except Exception as e:
        # 实时失败 → 尝试本地存储兜底
        stored = daily_store.get_stored_daily(req.symbol)
        df = _df_from_stored(stored)
        if df is None or len(df) < 5:
            raise HTTPException(400, f"获取数据失败且无本地缓存: {str(e)}")
        source = "cache"
        stale = True

    if len(df) < 5:
        raise HTTPException(400, f"数据不足: 仅{len(df)}根K线，无法分析")

    # 落库每日数据（live 时覆盖最新；cache 兜底时补全可能缺失的日期）
    try:
        daily_store.store_daily_bars(req.symbol, df, source=source)
    except Exception:
        pass

    strategy = FujimotoStrategy(total_capital=100000)
    result = strategy.analyze(df)
    nine_turn = calc_nine_turn_display(df)
    extra = _extra_metrics(df, req.symbol)

    return JSONResponse(content=_to_jsonable({
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
    }), headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


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
    """获取自选看板。已登录用其自选，未登录回退默认看板。"""
    user_id = user["id"] if user else None
    try:
        data = get_watchlist_status(user_id)
        data["user_scoped"] = user_id is not None
        return JSONResponse(content=_to_jsonable(data),
                            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})
    except Exception as e:
        raise HTTPException(500, f"获取自选列表失败: {str(e)}")


class WatchAddRequest(BaseModel):
    symbol: str
    name: str = ""


@app.post("/api/watchlist/add")
async def watchlist_add(req: WatchAddRequest, user: dict = Depends(auth.get_current_user)):
    """添加自选（需登录）。支持「代码」或「名称」输入：名称会先解析成代码再入库。"""
    raw = req.symbol.strip()
    if not raw:
        raise HTTPException(400, "代码/名称不能为空")
    # 代码型（不含中文）直接当作代码；否则视为名称，走搜索解析
    is_code = bool(re.fullmatch(r"[A-Za-z0-9.\-^]+", raw.upper()))
    symbol = raw.upper()
    name = (req.name or "").strip()
    if not is_code:
        try:
            hits = fetcher.search(raw)
            if hits:
                symbol = hits[0]["code"].upper()
                name = name or hits[0].get("name", "")
        except Exception:
            pass
    if not name:
        try:
            name = fetcher.lookup_name(symbol) or ""
        except Exception:
            name = ""
    ok = add_user_watchlist(user["id"], symbol, name)
    return {"success": ok, "symbol": symbol, "name": name}


@app.delete("/api/watchlist/remove")
async def watchlist_remove(symbol: str = Query(...), user: dict = Depends(auth.get_current_user)):
    """删除自选（需登录）。"""
    symbol = symbol.strip().upper()
    ok = remove_user_watchlist(user["id"], symbol)
    return {"success": ok, "symbol": symbol}


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
):
    """分析上传的 CSV 或模拟数据"""
    try:
        if use_sample or file is None:
            df = generate_sample_data(300)
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
async def run_backtest(req: BacktestRequest):
    """执行策略回测"""
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
