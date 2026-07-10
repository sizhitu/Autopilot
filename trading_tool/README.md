# 藤本茂融合交易策略 - 智能分析工具 v2.0

一套完整的 Python 工具，把**藤本茂交易心法 + 斐波那契实战 + 加强版九转模型**落地为可运行的 Web 分析系统，支持**美股/A股真实数据**和**策略回测**。

---

## 工具组成

| 文件 | 功能 | 说明 |
|------|------|------|
| `web_app.py` | Web 服务后端（FastAPI） | 提供 RESTful API |
| `index.html` | Web 前端页面 | Canvas 绘图 + 交互界面 |
| `strategy_engine.py` | 策略引擎核心 | 三层分析逻辑 |
| `data_fetcher.py` | **真实数据源模块** | 美股 Yahoo Finance + A股新浪财经 |
| `backtest.py` | **回测引擎** | 策略历史回测 + 统计指标 |
| `watchlist.py` | **自选看板** | 批量聚合关注股票状态 |
| `nine_turn.py` | 神奇九转模块 | TD Sequential 简化版计数 |
| `run_preview.sh` | 手动启动器（可选） | 本地手动拉起 web_app 的自愈包装，生产由 supervisord 直管 |
| `preview_watchdog.sh` | 健康看门狗 | 独立进程，监测 HTTP + 进程一致性，异常时重启服务 |

---

## 快速开始

### 启动 Web 服务

```bash
python3.11 web_app.py
# 浏览器访问 http://localhost:8000
```

## 本地部署

本项目支持在本机 / 内网 / 容器独立部署，不依赖沙箱环境（沙箱里的 supervisord 守护仅用于云端常驻，本地无需）。

### 方式一：直接运行（最简单）

**前置条件**：Python 3.10+（推荐 3.11），且机器能访问外网（美股 Yahoo / A股新浪 数据源需要联网）。

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 启动（默认 0.0.0.0:8000）
python web_app.py
# 或使用 uvicorn 生产方式（推荐，支持多 worker）：
uvicorn web_app:app --host 0.0.0.0 --port 8000

# 3. 浏览器打开 http://localhost:8000
```

也可用一键脚本：Linux / macOS 执行 `./start.sh`，Windows 双击 `start.bat`。

**环境变量（可选）**：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8000` | 监听端口 |

例如：`PORT=8080 python web_app.py`

### 方式二：Docker / Compose（推荐用于服务器或内网）

```bash
# 构建并后台启动
docker compose up -d --build
# 浏览器访问 http://<服务器IP>:8000
```

仅用 Docker 也可：

```bash
docker build -t trading-tool .
docker run -d --name trading-tool -p 8000:8000 trading-tool
```

`docker-compose.yml` 已配置 `restart: unless-stopped` 与健康检查，进程退出会自动拉起（等价于沙箱 supervisord 的 autorestart）。

### 依赖说明

- **Web 服务核心依赖**：`fastapi / uvicorn / pandas / numpy / requests`（见 `requirements.txt`）。
- **桌面版 `app.py` 与独立图表脚本 `gen_chart.py`** 额外需要 `matplotlib`，纯 Web 部署无需安装。
- **数据源**：美股走 Yahoo Finance v8 API，A股走新浪财经接口，均需联网；遇限流（429）已内置重试。

### 排错

| 现象 | 排查 |
|------|------|
| `pip install` 报版本冲突 | 使用虚拟环境：`python -m venv .venv && source .venv/bin/activate`（Windows: `.venv\Scripts\activate`） |
| 启动后打不开页面 | 确认防火墙放行端口；检查是否绑定到 `127.0.0.1`（应改为 `0.0.0.0`） |
| 行情获取失败 | 检查机器是否能访问外网；Yahoo / Sina 接口偶发限流，稍后重试 |
| `ModuleNotFoundError` | 确认在 `trading_tool/` 目录内执行，且已 `pip install -r requirements.txt` |

---

### 功能一览

| 功能 | 操作 | 数据源 |
|------|------|--------|
| **股票搜索** | 输入代码/名称/拼音 | 内置常用列表 + 自定义代码 |
| **美股行情** | AAPL, MSFT, NVDA, TSLA... | Yahoo Finance API |
| **A股行情** | 600519, 000001, 300750... | 新浪财经接口 |
| **指数** | ^GSPC(标普), sh000001(上证) | Yahoo / 新浪 |
| **策略分析** | 自动三层分析 + 信号 | 引擎实时计算 |
| **策略回测** | 输入代码→执行回测 | 历史数据模拟 |
| **CSV上传** | 导入自有数据 | 用户文件 |
| **阶梯计算器** | 输入涨跌幅→建议操作 | 藤本茂规则 |
| **自选看板** | 关注列表一览 | 美股 + A股 |

### API 接口

| 方法 | 路径 | 功能 |
|------|------|------|
| `GET` | `/` | 前端页面 |
| `GET` | `/api/search?q=keyword` | 搜索股票 |
| `POST` | `/api/quote` | 获取真实行情+分析 |
| `POST` | `/api/analyze` | CSV上传/模拟分析 |
| `POST` | `/api/backtest` | 策略回测 |
| `POST` | `/api/ladder` | 阶梯计算器 |
| `GET` | `/api/ladder_table` | 完整阶梯表 |
| `GET` | `/api/watchlist` | 自选股票看板 |

### API 示例

```bash
# 搜索股票
curl "http://localhost:8000/api/search?q=AAPL"
curl "http://localhost:8000/api/search?q=茅台"

# 获取行情并分析
curl -X POST http://localhost:8000/api/quote \
  -H "Content-Type: application/json" \
  -d '{"symbol":"600519","days":300}'

# 执行回测
curl -X POST http://localhost:8000/api/backtest \
  -H "Content-Type: application/json" \
  -d '{"symbol":"AAPL","days":300,"initial_capital":100000}'

# 获取自选看板
curl http://localhost:8000/api/watchlist
```

---

## 进程守护与高可用（生产部署）

生产环境通过 supervisord（PID 1）托管两个独立程序，专门解决"休眠恢复后状态不一致"问题：

| 程序 | 命令 | 职责 |
|------|------|------|
| `preview-8000` | `python3.11 web_app.py` | **直接托管真实服务进程**，autorestart 可靠自愈 |
| `preview-watchdog` | `bash preview_watchdog.sh` | 独立健康看门狗，每 30s 探测 HTTP + 进程一致性 |

> 两份 supervisord 配置已随项目附带于 **`deploy/`** 目录（`preview-8000.conf` / `preview-watchdog.conf`），可直接复制到 `/usr/local/share/supervisor/` 使用；注意其中的二进制路径（`/.PlnPyKFp4CRfFtgC1/...`）与项目路径（`/workspace/trading_tool/...`）是沙箱专属，换环境需按实际情况调整。

**关键设计（为什么这样稳）：**

1. **supervisord 直接托管 `web_app.py`**，而不是"带长驻子进程的包装脚本"。经验证：当被外部 kill 时，supervisord 对"带有长驻子进程的程序"会出现死亡检测失效、状态永久 stale（显示 Running 但实际已死）的 desync；而对直接托管的服务进程则能 3 秒内可靠 autorestart。
2. **看门狗是简单循环进程**（不后台任何长驻子进程），故 supervisord 能可靠地 autorestart 它；它只负责"补位"，不抢 supervisord 的活。
3. **看门狗监测 HTTP 健康 + 进程一致性**（受管 pid 必须真实存活、且等于真实 `web_app.py` 的 pid，无孤儿/错配）；发现异常才 `restart preview-8000`。
4. **过渡态跳过**：当程序处于 `Starting`/`Backoff`/`Stopping` 时看门狗不触发重启，避免与 supervisord 自愈竞态导致重启风暴。
5. **注意**：supervisord 的 `status` 输出带 ANSI 颜色码，解析 pid 时不能用 `^` 锚定 grep，需用 awk 全行匹配（看门狗已实现）。

**手动运维：**

```bash
SUPERVISORD_BIN="/.PlnPyKFp4CRfFtgC1/bin/supervisord"
SUPERVISOR_CONF="/.PlnPyKFp4CRfFtgC1/supervisord-conf/supervisord.conf"
${SUPERVISORD_BIN} ctl -c ${SUPERVISOR_CONF} status          # 查看状态
${SUPERVISORD_BIN} ctl -c ${SUPERVISOR_CONF} restart preview-8000   # 重启服务
${SUPERVISORD_BIN} ctl -c ${SUPERVISOR_CONF} restart preview-watchdog  # 重启看门狗
# 若状态彻底失同步（极端情况），用 reload 强制全量重建：
${SUPERVISORD_BIN} ctl -c ${SUPERVISOR_CONF} reload
```

> 本地手动调试可用 `bash run_preview.sh`（含 web_app 崩溃自愈循环），但生产部署请走上面的 supervisord 方案。

---

## 回测指标

| 指标 | 说明 |
|------|------|
| 总收益率 | 策略总盈亏% |
| 年化收益 | 换算为年化 |
| 最大回撤 | 峰值到谷值最大跌幅% |
| 夏普比率 | 风险调整后收益 |
| 胜率 | 盈利交易占比% |
| 买入持有 | 对比基准收益% |
| 超额收益 | 策略 vs 买入持有 |
| 平均持仓 | 每笔交易平均持有天数 |

---

## 策略三层逻辑

```
┌──────────────┐
│  系统层       │ 9均线 + RSI + MACD + ATR + 成交量
├──────────────┤
│  工具层       │ 斐波那契回撤位 + 反应确认 + 扩展目标
├──────────────┤
│  心法层       │ 藤本茂阶梯仓位管理
└──────────────┘
       ↓
  三层一致 → 执行
  任何冲突 → 观望
```

---

## 数据源说明

| 市场 | 接口 | 费用 | 说明 |
|------|------|------|------|
| 美股（主） | Yahoo Finance v8 API | 免费 | 前复权数据，含指数，优先使用 |
| 美股（兜底） | Nasdaq 历史接口 | 免费 | Yahoo 被限流（429）时自动切换，覆盖主流美股 |
| A股 | 新浪财经 K线接口 | 免费 | 日线数据，支持所有A股 |
| CSV | 用户上传 | - | 标准 OHLCV 格式 |

**CSV 格式要求**：包含 `open, high, low, close, volume` 列，可选 `date` 列。

> **关于 Yahoo 429 限流（系统性熔断）**：请求携带隐私同意 Cookie 规避“未同意隐私政策”型 429；并在 `query1`/`query2` 两域名各试一次。一旦 Yahoo 出现 **429 / 非 200 / 连接异常（含超时）**，即触发**全局冷却**（120s），期间所有美股请求直接走 **Nasdaq 兜底接口**，不再逐个代码空等——这是“系统性”处理，无需为每只股票单独写逻辑。注意两点：
> - Nasdaq 免费接口**仅覆盖个股、不覆盖 ETF**，且仅返回最近约 15 个交易日。故纯 ETF（如 SMH/VGT/JEPI）在 Yahoo 被封时可能取不到；在自有 IP / 容器部署时 Yahoo 直连可正常获取全部标的。
> - Nasdaq 数据不足以支撑完整 300 天策略（九转/均线），此时长周期信号会降级提示，但最新价与近期走势正常。
>
> **自选看板采用后台刷新**：接口永远秒回——命中缓存直接返回，缺失/过期则后台计算并立即返回旧缓存或“计算中”占位，前端自动轮询。彻底消除冷启动时接口阻塞的问题。

---

## 注意事项

1. 本工具为**辅助决策工具**，不是自动交易程序。
2. 美股数据优先走 Yahoo Finance；遇限流会自动触发全局冷却并切换到 Nasdaq 兜底，已内置熔断与降级，无需人工干预。
3. A股数据通过新浪财经获取；若按 sh/sz 前缀取不到（部分 ETF 前缀与常规规则不符），会自动切换交易所前缀重试。
4. A股/指数代码示例：有色金属 159880、黄金 518850、船舶 560710、纳指ETF 516150、机器人 562500、中韩半导体 513310、上证 000001、沪深300 399300。
5. 回测结果基于历史数据，不代表未来表现。
6. 自选看板中的“即将上涨关注”基于九转买点与藤本茂阶梯抄底（-15%）综合判定。
7. 所有输出不构成投资建议，交易风险自担。

---

*v2.3 更新：① 修正关注代码——VCX→CF(Fundrise Innovation Fund)、FLY→Firefly Aerospace、FIGR→Figure Technology、SPCX 确认为 SpaceX（已上市）；② 新增关注标的——ICE/SMH/VGT/JEPI/GOOG/LITE/ASTS/FCX/ASM/EUV/WTI/AVGO/NVDA/INTC 及 A股 ETF（159880/518850/560710/516150/562500/513310）与指数（000001 上证、399300 沪深300）；③ 系统性限流熔断（Yahoo 任意失败即全局冷却转 Nasdaq 兜底，非逐只处理）+ 自选看板后台刷新（接口永远秒回）*

*v2.1 更新：新增自选看板（一键查看17只关注股票的状态、操盘建议、神奇九转、新高新低、近5日涨跌）*

*v2.0 更新：新增真实数据源（美股+A股）、策略回测模块、搜索功能、回测可视化*
