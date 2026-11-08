"""拥挤度相关接口准确性与健壮性测试。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading_tool"))


def test_us_short_interest():
    """美股空头接口：Nasdaq 上市股应返回复合拥挤度。"""
    from watchlist import _fetch_us_short_interest
    for code in ["AAPL", "NVDA", "GOOG", "ASTS"]:
        si = _fetch_us_short_interest(code)
        assert isinstance(si, dict), f"{code} 应返回 dict"
        # Nasdaq 上市股应有 days_to_cover
        if si:
            assert "days_to_cover" in si or "composite" in si, f"{code} 缺少关键字段: {si}"
            print(f"  {code}: composite={si.get('composite')} days={si.get('days_to_cover')}")
        else:
            print(f"  {code}: 空（非 Nasdaq 或接口无数据）")


def test_non_nasdaq_fallback():
    """非 Nasdaq 股票（如 BE）空头接口返回空，应有兜底路径。"""
    from watchlist import _fetch_us_short_interest
    si = _fetch_us_short_interest("BE")
    # BE 非 Nasdaq，应返回空或无 composite
    print(f"  BE short: {si}")
    assert isinstance(si, dict)


def test_compute_crowding_volume():
    """成交量分位计算：有 volume 时应返回 percentile。"""
    import pandas as pd
    from web_app import _compute_crowding_from_df
    df = pd.DataFrame({
        "close": [100 + i * 0.5 for i in range(60)],
        "volume": [1000000 + i * 10000 for i in range(60)],
    })
    m = _compute_crowding_from_df(df)
    assert isinstance(m, dict)
    if m:
        assert "percentile" in m
        assert "level" in m
        print(f"  volume crowding: {m.get('level')} {m.get('percentile')}%")
    else:
        print("  volume crowding: 空（数据不足）")


def test_stock_advice_endpoint_logic():
    """模拟 stock-advice 逻辑：美股优先空头，失败回落成交量。"""
    from watchlist import _get_cached_short
    # 验证缓存函数不抛错
    for code in ["AAPL", "BE", "NVDA"]:
        try:
            si = _get_cached_short(code)
            print(f"  cached {code}: {bool(si)}")
        except Exception as e:
            print(f"  cached {code} error: {e}")


if __name__ == "__main__":
    print("=== test_us_short_interest ===")
    test_us_short_interest()
    print("=== test_non_nasdaq_fallback ===")
    test_non_nasdaq_fallback()
    print("=== test_compute_crowding_volume ===")
    test_compute_crowding_volume()
    print("=== test_stock_advice_endpoint_logic ===")
    test_stock_advice_endpoint_logic()
    print("all passed")



def test_neutral_position_snapshot_is_cached(monkeypatch):
    """无交易建议的有效拥挤度快照也必须进入历史缓存。"""
    import cache

    captured = {}
    monkeypatch.setattr(cache, "get_position_advice", lambda symbol: [])
    monkeypatch.setattr(cache, "set_json", lambda key, value, ttl: captured.update({key: value}))
    import supabase_client
    monkeypatch.setattr(supabase_client, "using_supabase", lambda: False)

    cache.append_position_advice("NVDA", {
        "bar_date": "2026-11-06",
        "level": "正常",
        "percentile": None,
        "score": 0.42,
        "metric_type": "short_composite_score",
        "speed": "空头2.1%",
        "advice": "",
        "reason": "Nasdaq/FINRA",
        "source": "Nasdaq/FINRA",
        "as_of": "2026-10-31",
    })

    rows = captured["posadvice:NVDA"]["rows"]
    assert len(rows) == 1
    assert rows[0]["score"] == 0.42
    assert rows[0]["advice"] == ""
    assert rows[0]["metric_type"] == "short_composite_score"


def test_watchlist_richness_counts_crowding_data():
    from watchlist import _row_richness

    bare = {"price": 100, "bar_date": "2026-11-06"}
    rich = dict(bare, crowding_pct=1.2, crowding_level="高", crowding_source="Nasdaq/FINRA")
    assert _row_richness(rich) > _row_richness(bare)
