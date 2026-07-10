"""
藤本茂融合策略 - Web API 后端
=================================
FastAPI 后端，提供：
  - POST /api/analyze     上传CSV并分析
  - POST /api/quote       获取真实行情数据（美股/A股）
  - POST /api/backtest    策略回测
  - GET  /api/search      搜索股票代码
  - POST /api/ladder      藤本茂阶梯仓位计算器
  - GET  /               前端页面
"""

import sys
import os
import io
import json
import math
from datetime import datetime

import pandas as pd
import numpy as np
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

# 导入策略引擎和数据源
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strategy_engine import FujimotoStrategy, generate_sample_data, SignalType, TrendType
from data_fetcher import DataFetcher
from backtest import Backtester, result_to_dict as bt_to_dict
from watchlist import get_watchlist_status
from nine_turn import calc_nine_turn_display
import threading

app = FastAPI(title="藤本茂融合策略 Web 工具", version="2.0")
fetcher = DataFetcher()

# 服务启动后预热自选看板缓存，避免用户首次加载等待过久
def _warmup_watchlist():
    try:
        import time
        time.sleep(3)
        get_watchlist_status()
    except Exception:
        pass

@app.on_event("startup")
def _on_startup():
    threading.Thread(target=_warmup_watchlist, daemon=True).start()

# ================================================================
#  辅助：策略结果转 JSON
# ================================================================

def _to_jsonable(v):
    """递归将 numpy 类型转为原生 Python 类型"""
    import numpy as np
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


# ================================================================
#  API 路由
# ================================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    """返回前端页面（禁用缓存，避免移动端 WebView 加载旧版本）"""
    html_path = os.path.join(os.path.dirname(__file__), "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(
        content=html,
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )


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

            # 标准化列名
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

        response_data = {
            "success": True,
            "data": result_to_dict(result),
            "chart": df_to_chart_json(df, result),
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
    price_change: float       # 涨跌幅百分比，如 -15 表示下跌15%
    current_position: float = 0  # 当前持仓比例


@app.post("/api/ladder")
async def calc_ladder(req: LadderRequest):
    """藤本茂阶梯仓位计算器"""
    strategy = FujimotoStrategy()
    change = req.price_change / 100.0
    desc, delta = strategy._fujimoto_action(change, req.current_position)

    # 判断是买入还是卖出
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
    """获取真实行情数据并自动分析"""
    try:
        df = fetcher.fetch(req.symbol, req.days)
        if len(df) < 5:
            raise ValueError(f"数据不足: 仅{len(df)}根K线，无法分析")

        # 自动执行策略分析
        strategy = FujimotoStrategy(total_capital=100000)
        result = strategy.analyze(df)

        # 神奇九转（日级+月级，月级形成则展示月级）
        nine_turn = calc_nine_turn_display(df)

        return JSONResponse(content=_to_jsonable({
            "success": True,
            "symbol": req.symbol,
            "data": result_to_dict(result),
            "chart": df_to_chart_json(df, result),
            "nine_turn": nine_turn,
            "meta": {
                "rows": len(df),
                "last_close": round(float(df['close'].iloc[-1]), 2),
                "start_date": df['date'].iloc[0].strftime('%Y-%m-%d') if 'date' in df.columns else "",
                "end_date": df['date'].iloc[-1].strftime('%Y-%m-%d') if 'date' in df.columns else "",
            }
        }))
    except Exception as e:
        raise HTTPException(400, f"获取数据失败: {str(e)}")


# ================================================================
#  回测 API
# ================================================================

class BacktestRequest(BaseModel):
    symbol: str = ""           # 股票代码（留空则用模拟数据）
    days: int = 300            # 回测天数
    initial_capital: float = 100000
    risk_per_trade: float = 0.02
    max_position: float = 0.70
    commission: float = 0.0003
    warmup: int = 60


@app.post("/api/backtest")
async def run_backtest(req: BacktestRequest):
    """执行策略回测"""
    try:
        # 获取数据
        if req.symbol:
            df = fetcher.fetch(req.symbol, req.days)
        else:
            df = generate_sample_data(req.days)

        if len(df) < req.warmup + 30:
            raise ValueError(f"数据不足: 需要{req.warmup+30}根，实际{len(df)}根")

        # 执行回测
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


# ================================================================
#  自选看板 API
# ================================================================

@app.get("/api/watchlist")
async def get_watchlist():
    """获取所有关注股票状态看板"""
    try:
        data = get_watchlist_status()
        return JSONResponse(content=_to_jsonable(data))
    except Exception as e:
        raise HTTPException(500, f"获取自选列表失败: {str(e)}")


if __name__ == "__main__":
    import os
    import uvicorn
    # 支持通过环境变量配置监听地址/端口，方便本地与容器部署
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host=host, port=port)
