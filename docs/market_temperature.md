# 市场情绪温度（第三方现成指标）

## 页面
只保留A股/美股切换、0–100分数、情绪文字、数据日期、数据来源。移除自算的宽度/趋势/活跃度/风险、风格评分和七大产业热度。

| 市场 | 来源 | 直接读取字段 |
|---|---|---|
| A股 | 财联社 | `data.market_degree` |
| 美股 | CNN Business Fear & Greed | `fear_and_greed.score/rating/timestamp` |

## 请求与数据校验
- A股接口：`https://x-quote.cls.cn/v2/quote/a/stock/emotion`。
- 美股接口：`https://production.dataviz.cnn.io/index/fearandgreed/graphdata/2021-02-01`。
- 分数必须是有限数值且位于0–100；CNN时间戳必须存在且不超过7天。
- A股接口未保证提供稳定更新时间字段；没有源时间戳时显示抓取日期，不伪造精确更新时间。
- PR CI 除离线单测外，会真实请求两个来源。HTTP错误、结构变化、无效分数或过期数据会阻止合并。
- 两个来源都不是带SLA的正式商业API；供应商可能调整反爬或字段结构。

## 性能、缓存与冷备
- 每次刷新只请求所选市场的一个第三方接口，不再扫描个股或下载多组历史K线。
- 5分钟缓存；后台线程刷新；API立即返回最后一个有效值。
- 失败不覆盖有效缓存；缓存失效时继续读取JSON冷备。
- 缓存键升级为 `market_temperature_v4_cn/us`，避免旧算法结果混入。
- 每日归档保留Supabase、JSON sidecar与Parquet；归档仅保存来源分数，不重新计算。
- A股与美股分数是各自供应商定义的情绪指标，不能视为完全同口径的横向排名。
