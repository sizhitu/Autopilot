# 美股行情源：产品需求、探测模块、结论

独立探测脚本：`trading_tool/probe_us_sources.py`  
不依赖 `data_fetcher.py`，方便以后换源、加 Key、加样本。

```bash
python3 trading_tool/probe_us_sources.py
# 产出 artifacts 同结构的 JSON/CSV；仓库内样例见 docs/us_source_probe_smoke.json
```

当前探测的三个免 Key 源（可落地、与现网一致）：

1. **Yahoo** `query1.finance.yahoo.com/v8/finance/chart`  
2. **Nasdaq** `api.nasdaq.com/api/quote/{sym}/historical`  
3. **Stooq** `stooq.com/q/d/l/?s={sym}.us&i=d`

Finnhub / Tiingo / Twelve Data 需要申请 Key，下一轮把 Key 配进环境变量后再扩 `FETCHERS`。

---

## 本产品真正需要的数据

看板和分析页都是 **日线 OHLCV**，不是盘口。

| 用途 | 字段 | 完整度 | 可接受延迟 |
|------|------|--------|------------|
| 看板现价 | 最新交易日 `close`、日期 | 必须有 | 收盘后数小时内到当天 bar 即可 |
| 看板日涨幅 | 近 2 根 `close` | 必须有 | 同上 |
| 近 5 日 | 近 6 根 `close` | 最好有 | 同上 |
| 九转 / 量价 | 约 60～120 根完整 OHLCV | ≥60 | 用已收盘序列，不必盘中跳变 |
| MA120 / MA250 / 分位 | **约 300～1250 根** | ≥250 才能画分析页 | 日频，隔夜更新即可 |
| 成交量 | `volume` | 九转/量价需要 | 允许与 Yahoo 有小差异 |
| 复权 | 前复权 close | 分析页需要 | Yahoo adjclose 优先 |

不需要（现阶段）：Level 2、逐笔、秒级 WebSocket。上了反而会让九转盘中跳档。

验收线（写在脚本 `SPEC` 里）：

- 分析页：`bars >= 250`，最后一根日期 ≥ 上一完整美股交易日  
- 看板现价：至少 10 根，最后收盘价与多源中位数偏差 ≤ 1%  
- 单只拉取：3s 内算顺，>8s 算体验差  

---

## 2026-09-24 探测摘要

环境：本机/沙箱对 12 只流动性标的连打三源（样例 JSON 已入库）。

| 源 | 成功率 | ≥250 根 | 最后日期 | 延迟 | 主要失败 |
|----|--------|---------|----------|------|----------|
| Yahoo | 0/12 | 0 | — | — | **HTTP 429**（IP 限流） |
| Nasdaq | 12/12 | **0**（均为约 15 根） | 多为 2026-09-22 | ~0.2s | 历史窗口被接口截断 |
| Stooq | 0/12 | 0 | — | — | TLS/连接失败 |

同日稍早另一次单点实测（限流前）：Yahoo 对 EQT/AAPL **0.1s、316 根**，末根 `close` 常为空、`regularMarketPrice` 可补；Nasdaq EQT **3s、15 根**，末根有价。

含义：

- Yahoo **能用时是唯一能撑分析页的免 Key 源**，但不能当唯一生产源。  
- Nasdaq **适合补现价**，不能当 300 根 K 线。  
- Stooq 适合冷备，本环境连通性不稳定，不能进用户点击热路径。

---

## 结论（给后续迭代）

1. **日线与现价拆开**：分析页只接受 ≥250 根的 Yahoo（或以后的 Tiingo/EODHD）；看板现价可用 Nasdaq 15 根，禁止用这 15 根覆盖长 K。  
2. **自己存日线**：收盘后写入 parquet / daily cache，用户刷新默认读本地。这是行业默认，不是换语言能替代的。  
3. **下一轮 Top3 升级（要 Key）**  
   - Finnhub 免费档补美股现价（额度够 20～40 只自选）  
   - Tiingo EOD 做日线主备  
   - Yahoo 仅在日线缺口时补长历史，429 必须冷却  
4. 探测模块加源时只扩 `FETCHERS`，用同一套 `SPEC` 打 50 只再改线上分流。

样例里 `expected_session=2026-09-23`、Nasdaq 停在 `09-22`，说明「日期新鲜」也要按源各自的结算节奏看，不能只信一个日历日。
