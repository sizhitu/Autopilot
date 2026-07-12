# Autopilot · 藤本茂融合策略智能分析工具

> **中文** | **English**

一套把**藤本茂交易心法 + 斐波那契实战 + 加强版神奇九转（TD Sequential）**落地为可运行 Web 系统的股票分析工具，支持**美股 / A股 / ETF / 指数真实数据**、**策略回测**与**自选看板**。零前端框架依赖（原生 JS + Canvas 绘图），可纯本地或容器部署。

A production-ready stock analysis tool that turns the **Fujimoto trading philosophy + Fibonacci practice + an enhanced Magic Nine Turns (TD Sequential)** model into a runnable Web system. It supports **real-time US / A-share / ETF / index data**, **strategy backtesting**, and a **watchlist dashboard**. Zero front-end framework dependency (vanilla JS + Canvas charts), deployable locally or via container.

---

## 核心功能 · Core Features

1. **三层一体融合策略 · Three-Layer Integrated Strategy**
   系统层（趋势判定 + 多均线/指标）+ 工具层（斐波那契回撤反应）+ 心法层（藤本茂阶梯仓位）一致性检验，输出统一操盘建议。
   *System layer (trend + multi-MA/indicators) + Tool layer (Fibonacci retracement reactions) + Mindset layer (Fujimoto ladder sizing), cross-validated to produce one unified trading suggestion.*

2. **藤本茂阶梯仓位计算器 · Fujimoto Ladder Position Calculator**
   基于涨跌幅的阶梯式加减仓：下跌 15% 第一档承接、25% 加重仓；上涨 25% 起逐步兑现、100% 清仓。把"恐慌中接筹码、让利润奔跑"心法量化。
   *Stepwise add/sell by price move: first buy at −15%, heavier at −25%; begin taking profit at +25%, full exit at +100%. Quantifies "buy the panic, let winners run".*

3. **神奇九转（TD Sequential）· Magic Nine Turns**
   日级 + 月级双周期计数，第 7-8 天预警"即将完成"，第 9 天为买/卖点。分析页提供**详细说明**（方向、进度、含义、月/日级共振）。
   *Dual time-frame (daily + monthly) counting; days 7-8 warn of "completion imminent", day 9 is the buy/sell trigger. The analysis page now shows a **detailed explanation** (direction, progress, implication, monthly/daily confluence).*

4. **个股详情分析页 · Per-Stock Detail / Analysis Page**
   K 线 + 9 条均线 + 斐波那契 + RSI(14) 图表；策略信号、三层一致性、指标明细、九转详解，以及**新高 / 新低 / 估值**专门卡片（估值按持仓定位差异化：压舱石 / 高赔率 / 周期弹性 / 卫星仓）。
   *Candles + 9 MAs + Fibonacci + RSI(14); strategy signal, three-layer check, indicator detail, Nine-Turns explainer, plus a dedicated **New High / New Low / Valuation** card (valuation is role-aware: Anchor / High-odds / Cyclical / Satellite).*

5. **自选看板 · Watchlist Dashboard**
   批量聚合关注股票状态（现价、当日/近5日涨跌、操盘建议、九转），后台刷新 + 缓存，接口**永远秒回**。
   *Bulk status aggregation (price, 1d/5d move, suggestion, Nine Turns) with background refresh + cache so the API **always responds instantly**.*

6. **策略回测引擎 · Backtest Engine**
   资金曲线、最大回撤、夏普比率、胜率、交易记录，并与"买入持有"对比给出**超额收益**。
   *Equity curve, max drawdown, Sharpe, win rate, trade log, and **excess return** vs. buy-and-hold.*

7. **真实数据源 + 限流熔断 · Real Data with Rate-Limit Circuit Breaker**
   美股 Yahoo Finance（Nasdaq 兜底）、A股新浪财经、指数；命中 429 即触发全局冷却并自动切换兜底源，避免逐只空等。
   *US (Yahoo Finance + Nasdaq fallback), A-share (Sina), indices; a 429 triggers a global cooldown and auto-fallback to avoid per-symbol stalls.*

8. **多入口 · Multiple Entry Points**
   Web（FastAPI + 原生 JS）、桌面 GUI（tkinter）、CLI，覆盖不同使用场景。
   *Web (FastAPI + vanilla JS), Desktop GUI (tkinter), and CLI for different workflows.*

---

## 项目亮点 · Highlights

| 亮点 · Highlight | 说明 · Description |
|---|---|
| **心法可量化 · Philosophy made quantitative** | 藤本茂"阶梯承接、让利润奔跑"被转化为明确的加减仓规则与仓位百分比。 *Fujimoto's mindset becomes explicit add/sell rules and position percentages.* |
| **三层一致性 · Three-layer confluence** | 系统/工具/心法三层同时验证才强化信号，降低单一指标误判。 *Signals are strengthened only when all three layers agree, cutting single-indicator noise.* |
| **估值定位差异化 · Role-aware valuation** | 不同持仓定位用不同算法（均线偏离 / 区间分位），而非一刀切 PE。 *Different holdings use different algorithms (MA deviation / range percentile) instead of a one-size-fits-all metric.* |
| **九转日/月双周期 · Dual-cycle Nine Turns** | 月级代表大级别趋势，与日级同向时信号共振、参考价值更高。 *Monthly captures the bigger trend; alignment with daily raises conviction.* |
| **看板秒回 · Instant dashboard** | 后台并行抓取 + 缓存 + 冷却兜底，前端永不转圈。 *Parallel fetch + cache + fallback keep the UI never spinning.* |
| **零前端依赖 · Zero front-end deps** | 纯原生 JS + Canvas 绘图，无 React/Vue，部署极简。 *Pure vanilla JS + Canvas, no React/Vue, trivial to deploy.* |
| **多市场覆盖 · Multi-market** | 美股、A股、ETF、指数统一接口，自动路由。 *Unified interface auto-routing US, A-share, ETF, indices.* |
| **移动端友好 · Mobile-friendly** | 响应式布局 + 横滑手势返回看板。 *Responsive layout + swipe gesture to return to the dashboard.* |
| **可回测验证 · Backtestable** | 内置回测引擎与超额收益对比，策略可用历史数据证伪/证实。 *Built-in backtester with excess-return comparison to validate the strategy.* |
| **容器化部署 · Container-ready** | 提供 Dockerfile / docker-compose，一行启动。 *Dockerfile / docker-compose for one-command launch.* |

---

## 技术架构 · Tech Stack

- **后端 · Backend**：Python 3.11 · FastAPI · uvicorn · pandas / numpy
- **前端 · Frontend**：原生 HTML/CSS/JS · Canvas 绘图（无第三方框架）
- **数据源 · Data**：Yahoo Finance v8（美股/指数）· 新浪财经（A股）· Nasdaq API（兜底）
- **部署 · Deploy**：Docker · docker-compose · 预览看门狗（自愈重启）

---

## 快速开始 · Quick Start

### 方式一：直接运行 · Run Directly

```bash
cd trading_tool
pip install -r requirements.txt
python3.11 web_app.py        # 或: uvicorn web_app:app --host 0.0.0.0 --port 8000
# 浏览器访问 http://localhost:8000
```

### 方式二：Docker · Container

```bash
cd trading_tool
docker compose up -d --build
# 访问 http://localhost:8000
```

### 主要功能入口 · Main Entry Points

| 文件 · File | 作用 · Purpose |
|---|---|
| `web_app.py` | Web 服务（FastAPI），提供所有 REST API 与页面 *Web service: all REST APIs + page* |
| `index.html` | Web 前端页面 *Front-end page* |
| `strategy_engine.py` | 三层策略引擎核心 *Three-layer strategy core* |
| `data_fetcher.py` | 真实数据源（美股/A股/指数）*Real data sources* |
| `backtest.py` | 回测引擎 *Backtest engine* |
| `watchlist.py` | 自选看板聚合 *Watchlist aggregator* |
| `nine_turn.py` | 神奇九转计数 *Magic Nine Turns counter* |
| `app.py` | 桌面 GUI（tkinter）*Desktop GUI* |

---

## 免责声明 · Disclaimer

本项目仅供**学习与研究**使用，所有信号、估值与建议均由策略引擎自动生成，**不构成任何投资建议**。市场有风险，投资需谨慎。

This project is for **education and research only**. All signals, valuations, and suggestions are generated automatically by the strategy engine and **do not constitute investment advice**. Markets carry risk; invest with caution.
