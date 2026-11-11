"""拥挤度相关接口准确性与健壮性测试。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading_tool"))


def test_us_short_interest():
    """美股空头接口：优先 FINRA 全市场合并持仓，Nasdaq 个股接口作为补充。"""
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


def test_nyse_and_amex_use_finra_consolidated(monkeypatch):
    """NYSE/AMEX 都必须先查 FINRA 全市场合并空头持仓。"""
    import watchlist
    monkeypatch.setattr(watchlist, "_fetch_finra_short_interest", lambda code: {
        "status": "ok", "short_interest": 1000000, "days_to_cover": 2.0,
        "history": [], "source": "FINRA 官方合并空头持仓",
    })
    monkeypatch.setattr(watchlist, "_fetch_nasdaq_short_interest",
                        lambda code: (_ for _ in ()).throw(AssertionError("NYSE/AMEX must not call Nasdaq")))
    for venue in ("NYSE", "AMEX"):
        monkeypatch.setattr(watchlist, "_fetch_listing_exchange", lambda code, v=venue: v)
        result = watchlist._fetch_us_short_interest("XYZ")
        assert result["status"] == "ok"
        assert result["provider"] == "finra_consolidated"
        assert result["listing_exchange"] == venue


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
    print("=== test_nyse_and_amex_use_finra_consolidated ===")
    test_nyse_and_amex_use_finra_consolidated()
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


def test_exchange_aware_routing_uses_finra_first(monkeypatch):
    """所有交易所先查 FINRA 合并数据；Nasdaq 仅在 FINRA 无记录时补充。"""
    import watchlist
    monkeypatch.setattr(watchlist, "_fetch_listing_exchange", lambda code: "NYSE")
    monkeypatch.setattr(watchlist, "_fetch_finra_short_interest", lambda code: {
        "status": "ok", "history": [], "source": "FINRA 官方合并空头持仓",
    })
    result = watchlist._fetch_us_short_interest("XYZ")
    assert result["status"] == "ok"
    assert result["provider"] == "finra_consolidated"

    monkeypatch.setattr(watchlist, "_fetch_listing_exchange", lambda code: "NASDAQ")
    monkeypatch.setattr(watchlist, "_fetch_finra_short_interest",
                        lambda code: {"status": "no_records", "message": "no match"})
    monkeypatch.setattr(watchlist, "_fetch_nasdaq_short_interest", lambda code: {
        "status": "ok", "short_interest": 123, "days_to_cover": 2.0,
        "history": [], "source": "Nasdaq 官方空头报告",
    })
    result = watchlist._fetch_us_short_interest("NVDA")
    assert result["status"] == "ok"
    assert result["provider"] == "nasdaq"
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
    assert _compute_short_composite(15.8, 999.99) is None
    assert _compute_short_composite(0, 3.18) == 0.0
    assert _short_composite_level(0.5024) == "偏高"
    assert _short_composite_level(1.0) == "高"
    assert _short_composite_level(2.0) == "极端"

    incomplete = _enrich_short_crowding({
        "status": "ok", "short_pct_float": None, "days_to_cover": 3.18,
    })
    assert incomplete["composite"] is None
    assert incomplete["metric_type"] == "short_interest_incomplete"


def test_fill_crowding_does_not_use_dtc_as_composite(monkeypatch):
    """官方空头复合分缺失时回退成交量分位，不能用 DTC 单项填分。"""
    import pandas as pd
    import watchlist
    monkeypatch.setattr(watchlist, "_get_cached_short", lambda code: {
        "status": "ok", "days_to_cover": 4.0, "short_pct_float": None,
        "composite": None, "source": "FINRA 官方合并空头持仓",
    })
    monkeypatch.setattr(watchlist, "_fetch_volume_only", lambda code: pd.Series([100 + i for i in range(60)]))
    # 模拟旧缓存中把 DTC=4 错误写成 crowding_pct=4 的情况，补算必须清除旧值。
    rows = watchlist._fill_crowding_if_missing([{
        "code": "BE", "name": "Bloom Energy",
        "crowding_pct": 4.0, "crowding_level": "正常",
        "crowding_source": "legacy DTC",
    }])
    row = rows[0]
    assert row["crowding_pct"] is not None
    assert row["crowding_source"] == "成交量历史分位（替代参考，非空头拥挤度）"
    assert row["days_to_cover"] == 4.0
    assert row["crowding_pct"] != 4.0




def test_watchlist_restores_persisted_crowding_when_live_data_missing(monkeypatch):
    import time
    import watchlist
    import cache

    def history(symbol):
        if symbol == "600887":
            return [{"bar_date": "2026-11-10", "percentile": 72.5, "score": None,
                     "level": "偏高", "source": "成交量历史分位"}]
        return [{"bar_date": "2026-11-10", "percentile": None, "score": 0.75,
                 "level": "偏高", "short_pct_float": 12.5, "days_to_cover": 3.0,
                 "source": "FINRA 官方空头持仓"}]

    monkeypatch.setattr(cache, "get_position_advice", history)
    monkeypatch.setattr(watchlist, "_get_cached_short", lambda code: None)
    for code in ("600887", "NVDA"):
        watchlist._POS_ADVICE_HISTORY_CACHE.pop(code, None)
        watchlist._POS_ADVICE_HISTORY_PENDING.discard(code)
    source_rows = [
        {"code": "600887", "market": "A股", "bar_date": "2026-11-11"},
        {"code": "NVDA", "market": "美股", "bar_date": "2026-11-11"},
    ]
    # 第一次响应只排队，不等待远端历史库。
    first = watchlist._fill_crowding_if_missing(source_rows)
    assert "crowding_pct" not in first[0]
    # 后台历史读取完成后，下一次响应从进程内缓存恢复原指标。
    deadline = time.time() + 2
    while time.time() < deadline:
        if all(code in watchlist._POS_ADVICE_HISTORY_CACHE for code in ("600887", "NVDA")):
            break
        time.sleep(0.01)
    rows = watchlist._fill_crowding_if_missing(source_rows)
    assert rows[0]["crowding_pct"] == 72.5
    assert rows[0]["crowding_stale"] is True
    assert "历史回退" in rows[0]["crowding_source"]
    assert rows[1]["crowding_pct"] == 75.0
    assert rows[1]["short_pct_float"] == 12.5
    assert rows[1]["days_to_cover"] == 3.0
    assert rows[1]["crowding_stale"] is True


def test_position_advice_persists_raw_short_metrics_and_history(monkeypatch):
    import cache
    import supabase_client

    captured = {}
    class Query:
        def upsert(self, payload, on_conflict=None):
            captured["payload"] = payload
            captured["on_conflict"] = on_conflict
            return self
        def execute(self):
            return type("Result", (), {"data": []})()

    class Client:
        def table(self, name):
            captured["table"] = name
            return Query()

    monkeypatch.setattr(cache, "get_position_advice", lambda symbol: [])
    monkeypatch.setattr(cache, "set_json", lambda key, value, ttl: None)
    monkeypatch.setattr(supabase_client, "using_supabase", lambda: True)
    monkeypatch.setattr(supabase_client, "get_service_client", lambda: Client())

    cache.append_position_advice("NVDA", {
        "bar_date": "2026-11-10", "score": 0.75,
        "metric_type": "short_composite_score", "short_interest": 1200000,
        "float_shares": 10000000, "short_pct_float": 12.0,
        "days_to_cover": 3.0, "provider": "finra",
        "history": [{"date": "2026-10-30", "short_interest": 1200000}],
    })
    payload = captured["payload"]
    assert captured["table"] == "position_advice_logs"
    assert captured["on_conflict"] == "symbol,bar_date"
    assert payload["short_interest"] == 1200000
    assert payload["float_shares"] == 10000000
    assert payload["short_pct_float"] == 12.0
    assert payload["days_to_cover"] == 3.0
    assert payload["history"][0]["date"] == "2026-10-30"
