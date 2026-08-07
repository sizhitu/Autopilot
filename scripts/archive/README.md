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
- 指标不落库，回测时用主仓同一套算法重算
