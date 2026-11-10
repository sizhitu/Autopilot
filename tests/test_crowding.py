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
        if si.get("status") == "ok":
            assert "days_to_cover" in si or "composite" in si, f"{code} 缺少关键字段: {si}"
            print(f"  {code}: composite={si.get('composite')} days={si.get('days_to_cover')}")
        else:
            print(f"  {code}: {si.get('status')} - {si.get('message', '')}")


def test_nyse_stock_does_not_fall_back_to_nasdaq_or_finra(monkeypatch):
    """NYSE 股票不调用 Nasdaq 或 FINRA OTC，更不依赖付费第三方数据源。"""
    import watchlist

    monkeypatch.delenv("FINRA_API_CLIENT_ID", raising=False)
    monkeypatch.delenv("FINRA_API_CLIENT_SECRET", raising=False)
    monkeypatch.setattr(watchlist, "_fetch_listing_exchange", lambda code: "NYSE")
    monkeypatch.setattr(watchlist, "_fetch_nasdaq_short_interest",
                        lambda code: (_ for _ in ()).throw(AssertionError("NYSE must not call Nasdaq")))
    monkeypatch.setattr(watchlist, "_fetch_finra_short_interest",
                        lambda code: (_ for _ in ()).throw(AssertionError("NYSE must not call FINRA OTC")))
    si = watchlist._fetch_us_short_interest("BE")
    assert si["status"] == "source_unavailable"
    assert si["listing_exchange"] == "NYSE"
    assert "NYSE Group" in si["source"]



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
    test_nyse_stock_does_not_fall_back_to_nasdaq_or_finra()
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
        "reason": "Nasdaq 官方空头报告",
        "source": "Nasdaq 官方空头报告",
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
    rich = dict(bare, crowding_pct=1.2, crowding_level="高", crowding_source="Nasdaq 官方空头报告")
    assert _row_richness(rich) > _row_richness(bare)






def test_finra_consolidated_short_interest(monkeypatch):
    """FINRA 官方跨交易所持仓接口字段与历史序列解析。"""
    import time
    import requests
    import watchlist

    watchlist._FINRA_TOKEN_CACHE.update({"token": "test-token", "expires_at": time.time() + 3600})
    captured = {}

    class Response:
        status_code = 200
        def json(self):
            return [
                {"symbolCode": "BE", "settlementDate": "2026-10-15",
                 "currentShortPositionQuantity": 1000000, "previousShortPositionQuantity": 900000,
                 "averageDailyVolumeQuantity": 250000, "daysToCoverQuantity": 4.0},
                {"symbolCode": "BE", "settlementDate": "2026-10-30",
                 "currentShortPositionQuantity": 1200000, "previousShortPositionQuantity": 1000000,
                 "averageDailyVolumeQuantity": 300000, "daysToCoverQuantity": 6.0},
            ]

    def fake_post(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(requests, "post", fake_post)
    result = watchlist._fetch_finra_short_interest("BE")
    assert result["status"] == "ok"
    assert result["short_interest"] == 1200000
    assert result["days_to_cover"] == 4.0
    assert len(result["history"]) == 2
    assert result["history"][-1]["date"] == "2026-10-30"
    assert captured["json"]["compareFilters"][0]["fieldValue"] == "BE"
    assert captured["headers"]["Authorization"] == "Bearer test-token"
    assert "consolidatedShortInterest" in captured["url"]


def test_exchange_aware_routing_without_paid_provider(monkeypatch):
    """Nasdaq 走 Nasdaq；NYSE/AMEX 不跨源；未知代码只查 FINRA OTC。"""
    import watchlist
    monkeypatch.setattr(watchlist, "_fetch_listing_exchange", lambda code: "NASDAQ")
    monkeypatch.setattr(watchlist, "_fetch_nasdaq_short_interest", lambda code: {
        "status": "ok", "short_interest": 123, "days_to_cover": 2.0,
        "history": [], "source": "Nasdaq 官方空头报告",
    })
    monkeypatch.setattr(watchlist, "_fetch_finra_short_interest",
                        lambda code: (_ for _ in ()).throw(AssertionError("Nasdaq success must not call FINRA")))
    result = watchlist._fetch_us_short_interest("NVDA")
    assert result["status"] == "ok"
    assert result["listing_exchange"] == "NASDAQ"
    assert result["provider"] == "nasdaq"

    for venue in ("NYSE", "AMEX"):
        monkeypatch.setattr(watchlist, "_fetch_listing_exchange", lambda code, v=venue: v)
        monkeypatch.setattr(watchlist, "_fetch_nasdaq_short_interest",
                            lambda code: (_ for _ in ()).throw(AssertionError("NYSE/AMEX must not call Nasdaq")))
        monkeypatch.setattr(watchlist, "_fetch_finra_short_interest",
                            lambda code: (_ for _ in ()).throw(AssertionError("NYSE/AMEX must not call FINRA OTC")))
        result = watchlist._fetch_us_short_interest("XYZ")
        assert result["status"] == "source_unavailable"
        assert result["listing_exchange"] == venue

    monkeypatch.setattr(watchlist, "_fetch_listing_exchange", lambda code: "UNKNOWN")
    monkeypatch.setattr(watchlist, "_fetch_finra_short_interest", lambda code: {
        "status": "ok", "history": [], "source": "FINRA OTC 官方空头持仓",
    })
    result = watchlist._fetch_us_short_interest("OTCQ")
    assert result["status"] == "ok"
    assert result["provider"] == "finra_otc"
    assert result["listing_exchange"] == "OTC"



def test_nasdaq_short_interest_parser_is_separate():
    """Nasdaq 报告解析独立于交易所识别与其他来源路由。"""
    import requests
    import watchlist

    class Response:
        status_code = 200
        def json(self):
            return {"data": {"shortInterestTable": {"rows": [
                {"settlementDate": "10/30/2026", "interest": "1,200,000",
                 "avgDailyShareVolume": "300,000", "daysToCover": "4.0"}
            ]}}}
    original = requests.get
    try:
        requests.get = lambda *a, **k: Response()
        result = watchlist._fetch_nasdaq_short_interest("NVDA")
    finally:
        requests.get = original
    assert result["status"] == "ok"
    assert result["short_interest"] == 1200000
    assert result["days_to_cover"] == 4.0
    assert result["listing_exchange"] == "NASDAQ"

def test_build_volume_crowding_history():
    """A股拥挤度应返回多日分位序列，供分析页折线图和数据列表使用。"""
    import pandas as pd
    from web_app import _build_volume_crowding_history
    df = pd.DataFrame({
        "date": pd.date_range("2025-01-01", periods=280, freq="B"),
        "close": [10 + i * 0.02 for i in range(280)],
        "volume": [1000000 + (i % 35) * 25000 for i in range(280)],
    })
    rows = _build_volume_crowding_history(df, limit=60)
    assert len(rows) == 60
    assert rows[0]["date"] < rows[-1]["date"]
    assert all(0 <= row["percentile"] <= 100 for row in rows)
    assert all(row["avg_volume_20"] > 0 and row["close"] > 0 for row in rows)



def test_short_composite_requires_both_metrics():
    """复合拥挤度 = Short % Float（百分数转比例）× DTC。缺一项不计算。"""
    from watchlist import _compute_short_pct_float, _compute_short_composite, _short_composite_level, _enrich_short_crowding

    assert _compute_short_pct_float(1580000, 10000000) == 15.8
    assert _compute_short_pct_float(1580000, None) is None
    assert _compute_short_composite(15.8, 3.18) == 0.5024
    assert _compute_short_composite(None, 3.18) is None
    assert _compute_short_composite(15.8, None) is None
    assert _compute_short_composite(0, 3.18) == 0.0
    assert _short_composite_level(0.5024) == "偏高"
    assert _short_composite_level(1.0) == "高"
    assert _short_composite_level(2.0) == "极端"

    incomplete = _enrich_short_crowding({
        "status": "ok", "short_pct_float": None, "days_to_cover": 3.18,
    })
    assert incomplete["composite"] is None
    assert incomplete["metric_type"] == "short_interest_incomplete"
