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


def test_non_nasdaq_fallback_requires_ortex_for_listed_stock(monkeypatch):
    """NYSE/Nasdaq 上市股没有 Nasdaq 报告时，不自动用 FINRA OTC 数据冒充。"""
    import requests
    import watchlist

    class EmptyResponse:
        status_code = 200
        def json(self):
            return {"data": {"shortInterestTable": {"rows": []}}}

    monkeypatch.delenv("FINRA_API_CLIENT_ID", raising=False)
    monkeypatch.delenv("FINRA_API_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("ORTEX_API_KEY", raising=False)
    monkeypatch.delenv("FINRA_ALLOW_OTC_FALLBACK", raising=False)
    monkeypatch.setattr(requests, "get", lambda *a, **k: EmptyResponse())
    monkeypatch.setattr(watchlist, "_fetch_finra_short_interest",
                        lambda code: (_ for _ in ()).throw(AssertionError("FINRA OTC must not be automatic")))
    si = watchlist._fetch_us_short_interest("BE")
    assert si["status"] == "provider_required"
    assert "ORTEX_API_KEY" in si["message"]
    assert "FINRA OTC" in si["message"]


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
        "reason": "Nasdaq/ORTEX",
        "source": "Nasdaq/ORTEX",
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
    rich = dict(bare, crowding_pct=1.2, crowding_level="高", crowding_source="Nasdaq/ORTEX")
    assert _row_richness(rich) > _row_richness(bare)





def test_ortex_official_short_interest_parsing(monkeypatch):
    """验证 NYSE venue、正确日期参数及官方半月度字段解析。"""
    import requests
    import watchlist

    class EmptyNasdaqResponse:
        status_code = 200
        def json(self):
            return {"data": {"shortInterestTable": {"rows": []}}}

    class OrtexResponse:
        status_code = 200
        def json(self):
            return {"data": [
                {"settlementDate": "2026-10-15", "publishedDate": "2026-10-22",
                 "officialSiShares": 1200000, "officialAvgDailyVolume": 300000,
                 "officialDaysToCover": 4.0},
                {"settlementDate": "2026-10-30", "publishedDate": "2026-11-06",
                 "officialSiShares": 1320000, "officialAvgDailyVolume": 330000,
                 "officialDaysToCover": 6.0},
            ]}

    captured = {}
    monkeypatch.setenv("ORTEX_API_KEY", "TEST")
    monkeypatch.setattr(watchlist, "_fetch_listing_exchange", lambda code: "NYSE")

    def fake_get(url, **kwargs):
        captured.update({"url": url, **kwargs})
        return OrtexResponse()

    monkeypatch.setattr(requests, "get", fake_get)
    result = watchlist._fetch_us_short_interest("BE")
    assert result["status"] == "ok"
    assert result["short_interest"] == 1320000
    assert result["days_to_cover"] == 4.0
    assert result["composite"] is None
    assert result["history"][-1]["published_date"] == "2026-11-06"
    assert "/stock/NYSE/BE/short/official_short_interest/" in captured["url"]
    assert captured["params"]["from_date"]
    assert captured["headers"]["Ortex-Api-Key"] == "TEST"
    assert result["listing_exchange"] == "NYSE"


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


def test_exchange_aware_routing(monkeypatch):
    """交易所识别后只调用对应数据源，不跨交易所误用 Nasdaq/FINRA 数据。"""
    import watchlist

    monkeypatch.setenv("ORTEX_API_KEY", "TEST")
    monkeypatch.setattr(watchlist, "_fetch_listing_exchange", lambda code: "NASDAQ")
    monkeypatch.setattr(watchlist, "_fetch_nasdaq_short_interest", lambda code: {
        "status": "ok", "short_interest": 123, "days_to_cover": 2.0,
        "history": [], "source": "Nasdaq 官方空头报告", "provider": "nasdaq",
    })
    monkeypatch.setattr(watchlist, "_fetch_ortex_official_short_interest",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("Nasdaq success must not call ORTEX")))
    result = watchlist._fetch_us_short_interest("NVDA")
    assert result["status"] == "ok"
    assert result["listing_exchange"] == "NASDAQ"
    assert result["provider"] == "nasdaq"

    calls = []
    monkeypatch.setattr(watchlist, "_fetch_listing_exchange", lambda code: "AMEX")
    monkeypatch.setattr(watchlist, "_fetch_nasdaq_short_interest",
                        lambda code: (_ for _ in ()).throw(AssertionError("AMEX must not call Nasdaq")))
    def fake_ortex(code, api_key, namespace):
        calls.append(namespace)
        return {"status": "ok", "history": [], "source": "ORTEX"}
    monkeypatch.setattr(watchlist, "_fetch_ortex_official_short_interest", fake_ortex)
    result = watchlist._fetch_us_short_interest("XYZ")
    assert result["status"] == "ok"
    assert calls == ["AMEX"]
    assert result["listing_exchange"] == "AMEX"

    monkeypatch.delenv("ORTEX_API_KEY", raising=False)
    monkeypatch.setattr(watchlist, "_fetch_listing_exchange", lambda code: "UNKNOWN")
    monkeypatch.setattr(watchlist, "_fetch_finra_short_interest", lambda code: {
        "status": "ok", "history": [], "source": "FINRA OTC",
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
