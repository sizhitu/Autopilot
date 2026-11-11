# Autopilot 行情归档脚本

将请求/关注标的的日 K（前复权口径与 `data_fetcher` 一致）写入 **Parquet**，供独立仓库 `Autopilot-data` 保存，并用 DuckDB 分析。

## 本地试跑

```bash
cd Autopilot
pip install pandas pyarrow requests numpy
PYTHONPATH=trading_tool python scripts/archive/fetch_and_archive.py \
  --out /tmp/data-out \
  --symbols-file scripts/archive/symbols.default.txt \
  --days 8

PYTHONPATH=trading_tool python scripts/archive/merge_month.py --data-root /tmp/data-out --prev-month
```

## DuckDB 示例

```sql
INSTALL httpfs; -- 若读远程可加
SELECT symbol, date, close
FROM read_parquet('data/monthly/*.parquet')
WHERE symbol = 'NVDA'
ORDER BY date;
```

## GitHub Actions

见 `.github/workflows/daily-archive.yml` 与 `monthly-merge.yml`。

需在 **Autopilot** 仓库 Secrets 配置：

| Secret | 含义 |
|--------|------|
| `DATA_REPO_TOKEN` | 有权限 push 到 `sizhitu/Autopilot-data` 的 PAT（`repo` 权限） |
| `DATA_REPO` | 可选，默认 `sizhitu/Autopilot-data` |

## 体积策略

- 日文件：每标的仅保留最近约 8 根 K 线（可调 `--days`）
- 月并后：建议保留约 90 天 daily，更旧可删（workflow 可选）
- 原始日 K 仍归档到 daily/monthly Parquet；拥挤度计算快照另存 Supabase 的 `position_advice_logs`。
- `archive_position_advice.py` 每日分页导出完整快照到 `position_advice_logs/YYYY-MM-DD.parquet`，包含指标口径、原始空头指标、数据来源/发布日期及历史序列 JSON。
- 首次部署需在 Supabase SQL Editor 执行 `supabase/migrations/0008_crowding_persistence.sql`，否则新增字段写入会被数据库拒绝（旧字段仍可用）。


## 看板日更快照（降级）

```bash
PYTHONPATH=trading_tool python scripts/archive/build_watchlist_snapshot.py \
  --out /tmp/watchlist_latest.json \
  --symbols-file scripts/archive/symbols.default.txt \
  --limit 20
```

GitHub Action：`.github/workflows/daily-snapshot.yml`  
产出：`Autopilot-data/snapshots/watchlist_latest.json` 与 `frontend/snapshots/watchlist_latest.json`。  
前端在 `/api/watchlist` 失败时自动回落该 JSON。
