from __future__ import annotations
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"trading_tool"))

def test_temperature_weights_and_missing_data_gate():
    import market_temperature as mt
    assert sum(mt.WEIGHTS.values())==100
    assert mt.MIN_COVERAGE==80
    assert 100-mt.WEIGHTS["breadth"]<mt.MIN_COVERAGE

def test_temperature_score_and_regime(monkeypatch):
    import market_temperature as mt
    monkeypatch.setattr(mt,"_fetch_frames",lambda f,s:({},[]))
    monkeypatch.setattr(mt,"_trend_score",lambda f:(70.,"2026-11-11"))
    monkeypatch.setattr(mt,"_activity_score",lambda f:60.)
    monkeypatch.setattr(mt,"_risk_score_cn",lambda f:80.)
    monkeypatch.setattr(mt,"_fetch_cn_breadth",lambda:{"score":40.,"source":"test","data_date":"2026-11-11"})
    monkeypatch.setattr(mt,"_clean_frame",lambda f:None)
    r=mt.build_market_temperature_snapshot("cn",fetcher=object())
    assert r["coverage"]==100 and r["temperature"]==58.5 and r["regime"]=="中性"

def test_temperature_is_withheld_when_breadth_missing(monkeypatch):
    import market_temperature as mt
    monkeypatch.setattr(mt,"_fetch_frames",lambda f,s:({},[]))
    monkeypatch.setattr(mt,"_trend_score",lambda f:(70.,"2026-11-11"))
    monkeypatch.setattr(mt,"_activity_score",lambda f:60.)
    monkeypatch.setattr(mt,"_risk_score_cn",lambda f:80.)
    monkeypatch.setattr(mt,"_fetch_cn_breadth",lambda:(_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(mt,"_clean_frame",lambda f:None)
    r=mt.build_market_temperature_snapshot("cn",fetcher=object())
    assert r["coverage"]==60 and r["temperature"] is None and r["status"]=="insufficient_data"

def test_trend_score_uses_multiple_indices():
    import pandas as pd
    import market_temperature as mt
    dates=pd.date_range("2025-01-01",periods=260,freq="B")
    frames=[pd.DataFrame({"date":dates,"close":[100+i for i in range(260)]}) for _ in range(3)]
    score,day=mt._trend_score(frames)
    assert score is not None and score>70 and day=="2025-12-30"

def test_activity_and_risk_require_history():
    import pandas as pd
    import market_temperature as mt
    df=pd.DataFrame({"date":pd.date_range("2026-01-01",periods=30),"close":range(100,130),"volume":range(1000,1030)})
    assert mt._activity_score(df) is None and mt._risk_score_cn(df) is None

def test_cn_breadth_parser_requires_market_coverage(monkeypatch):
    import market_temperature as mt
    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"data":{"total":1000,"diff":[{"f3":2,"f86":"202611111500"},{"f3":-1,"f86":"202611111500"},{"f3":0,"f86":"202611111500"}]*334}}
    monkeypatch.setattr(mt.requests,"get",lambda *a,**k:Response())
    result=mt._fetch_cn_breadth()
    assert result["advancers"]==334 and result["decliners"]==334
    assert result["score"]==50.0

def test_us_breadth_uses_equal_weight_vs_cap_weighted_etfs():
    import pandas as pd
    import market_temperature as mt
    dates=pd.date_range("2025-01-01",periods=260,freq="B")
    def frame(rate):
        return pd.DataFrame({"date":dates,"close":[100*(1+rate)**i for i in range(260)]})
    frames={"RSP":frame(.002),"SPY":frame(.001),"QQEW":frame(.0025),"QQQ":frame(.0015)}
    class Fetcher:
        def fetch(self,symbol,days): return frames[symbol]
    result=mt._fetch_us_breadth(Fetcher())
    assert result["score"]>50 and result["series"]==2
    assert result["cap_weighted_spread_pct"]<0
    assert result["cap_weight_effect"]=="equal_weight_lead"
    assert result["data_date"]==str(dates[-1].date())


def test_us_breadth_requires_220_bars_for_both_weighting_pairs():
    import pandas as pd, pytest
    import market_temperature as mt
    dates=pd.date_range("2026-01-01",periods=180,freq="B")
    frame=pd.DataFrame({"date":dates,"close":[100+i for i in range(180)]})
    class Fetcher:
        def fetch(self,symbol,days): return frame
    with pytest.raises(ValueError): mt._fetch_us_breadth(Fetcher())

def test_position_page_contains_market_switch_and_missing_data_state():
    html=(ROOT/"frontend/index.html").read_text(encoding="utf-8")
    assert 'id="marketTemperatureCard"' in html
    assert "selectMarketTemperature('cn')" in html and "selectMarketTemperature('us')" in html
    assert 'id="temperatureCompare"' in html
    assert "数据不足" in html
    assert "loadMarketTemperature(false)" in html

def test_temperature_api_persists_daily_idempotent_snapshots():
    source=(ROOT/"trading_tool/web_app.py").read_text(encoding="utf-8")
    assert '@app.get("/api/market-temperature")' in source
    assert 'on_conflict="market,trade_date"' in source
    assert '_market_temperature_history(market,30)' in source
    assert 'MIN_COVERAGE' not in source  # API consumes score/coverage from the engine

def test_temperature_api_uses_close_only_during_market_hours():
    source=(ROOT/"trading_tool/web_app.py").read_text(encoding="utf-8")
    assert "_market_temperature_session_open(market)" in source
    assert '"status":"waiting_for_close"' in source
    assert "stale_during_session" in source


def test_temperature_v2_weights_and_proxy_universes_are_separate():
    import market_temperature as mt
    assert mt.WEIGHTS == {"breadth":40.0,"trend":25.0,"activity":15.0,"risk":20.0}
    assert mt.STYLE_WEIGHTS == {"large_cap":50.0,"small_mid":25.0,"technology":25.0}
    assert len(mt.INDUSTRY_CONFIG["cn"]) == len(mt.INDUSTRY_CONFIG["us"]) == 7


def test_cn_breadth_date_uses_quote_timestamp_and_dict_diff(monkeypatch):
    import market_temperature as mt
    class Response:
        def raise_for_status(self): pass
        def json(self):
            rows=[{"f3":2,"f86":"202611111500"},{"f3":-1,"f86":"202611111500"},{"f3":0,"f86":"202611111500"}]*334
            return {"data":{"total":1002,"diff":{str(i):row for i,row in enumerate(rows)}}}
    monkeypatch.setattr(mt.requests,"get",lambda *a,**k:Response())
    result=mt._fetch_cn_breadth()
    assert result["data_date"]=="2026-11-11"


def test_industry_score_aligns_common_dates_and_requires_220_bars():
    import pandas as pd
    import market_temperature as mt
    dates=pd.date_range("2025-01-01",periods=260,freq="B")
    market=pd.DataFrame({"date":dates,"close":[100+i*.1 for i in range(260)]})
    asset=pd.DataFrame({"date":dates,"close":[100+i*.2 for i in range(260)]})
    result=mt._industry_score(asset,market)
    assert result is not None and result["relative_60d"]>0
    assert mt._industry_score(asset.tail(180),market) is None


def test_industry_stale_quote_is_not_scored():
    import pandas as pd
    import market_temperature as mt
    dates=pd.date_range("2025-01-01",periods=260,freq="B")
    market=pd.DataFrame({"date":dates,"close":[100+i*.1 for i in range(260)]})
    asset=pd.DataFrame({"date":dates-pd.Timedelta(days=10),"close":[100+i*.2 for i in range(260)]})
    assert mt._industry_score(asset,market) is None


def test_us_breadth_requires_220_real_observations():
    import pandas as pd, pytest
    import market_temperature as mt
    dates=pd.date_range("2026-01-01",periods=60,freq="B")
    frame=pd.DataFrame({"date":dates,"close":[100+i for i in range(60)]})
    class Fetcher:
        def fetch(self,symbol,days): return frame
    with pytest.raises(ValueError): mt._fetch_us_breadth(Fetcher())


def test_position_temperature_ui_contains_style_industry_and_weight_effect_sections():
    html=(ROOT/"frontend/index.html").read_text(encoding="utf-8")
    assert 'id="temperatureStyles"' in html and 'id="temperatureIndustries"' in html
    assert 'id="temperatureCapEffect"' in html
    assert "等权参与度" in html
    assert "市场风格" in html and "七大产业热度" in html


def test_cold_backup_reads_local_sidecar_and_marks_source(tmp_path, monkeypatch):
    import json
    from datetime import date
    import market_temperature_cold_backup as cold
    snapshot={"market":"cn","as_of":date.today().isoformat(),"temperature":61.2,"status":"ok","regime":"偏乐观"}
    (tmp_path/"latest_cn.json").write_text(json.dumps({"market":"cn","trade_date":snapshot["as_of"],"parquet_file":"market/temperature/test.parquet","snapshot":snapshot}),encoding="utf-8")
    monkeypatch.setenv("MARKET_TEMPERATURE_COLD_BACKUP_DIR",str(tmp_path))
    monkeypatch.setenv("MARKET_TEMPERATURE_COLD_BACKUP_BASE_URL","")
    result=cold.read_cold_snapshot("cn")
    assert result["temperature"]==61.2
    assert result["cold_backup_source"]=="local_file"
    assert result["cold_backup_file"]=="market/temperature/test.parquet"


def test_cold_backup_rejects_wrong_market_and_old_snapshot(tmp_path, monkeypatch):
    import json
    from datetime import date, timedelta
    import market_temperature_cold_backup as cold
    old=(date.today()-timedelta(days=30)).isoformat()
    (tmp_path/"latest_us.json").write_text(json.dumps({"snapshot":{"market":"cn","as_of":old,"temperature":55,"status":"ok"}}),encoding="utf-8")
    monkeypatch.setenv("MARKET_TEMPERATURE_COLD_BACKUP_DIR",str(tmp_path))
    monkeypatch.setenv("MARKET_TEMPERATURE_COLD_BACKUP_BASE_URL","")
    assert cold.read_cold_snapshot("us") is None


def test_cold_backup_remote_sidecar_is_bounded_and_authenticated(monkeypatch):
    import market_temperature_cold_backup as cold
    from datetime import date
    class Response:
        content=b'{}'
        def raise_for_status(self): pass
        def json(self): return {"market":"us","snapshot":{"market":"us","as_of":date.today().isoformat(),"temperature":48.5,"status":"ok"}}
    captured={}
    def fake_get(url,headers,timeout):
        captured.update(url=url,headers=headers,timeout=timeout)
        return Response()
    monkeypatch.setattr("requests.get",fake_get)
    monkeypatch.setenv("MARKET_TEMPERATURE_COLD_BACKUP_DIR","")
    monkeypatch.setenv("MARKET_TEMPERATURE_COLD_BACKUP_BASE_URL","https://example.invalid/temperature")
    monkeypatch.setenv("MARKET_TEMPERATURE_COLD_BACKUP_TOKEN","test-token")
    result=cold.read_cold_snapshot("us")
    assert result["temperature"]==48.5 and result["cold_backup_source"]=="remote_file"
    assert captured["timeout"]==(0.6,1.5)
    assert captured["headers"]["Authorization"]=="Bearer test-token"


def test_temperature_refresh_is_single_worker_and_api_has_cold_fallback():
    source=(ROOT/"trading_tool/web_app.py").read_text(encoding="utf-8")
    assert "_MARKET_TEMPERATURE_REFRESH_LOCK = threading.Lock()" in source
    assert "acquire(blocking=False)" in source
    assert "read_cold_snapshot" in source
    assert "timeout=2.5" in source


def test_temperature_archive_writes_atomic_json_sidecar(tmp_path, monkeypatch):
    from datetime import date
    import market_temperature_cold_backup as cold
    snapshot={"market":"cn","as_of":date.today().isoformat(),"temperature":63.1,"status":"ok"}
    target=cold.write_cold_snapshot(snapshot,tmp_path,"market/temperature/test.parquet")
    monkeypatch.setenv("MARKET_TEMPERATURE_COLD_BACKUP_DIR",str(tmp_path))
    monkeypatch.setenv("MARKET_TEMPERATURE_COLD_BACKUP_BASE_URL","")
    assert target.name=="latest_cn.json" and target.is_file()
    restored=cold.read_cold_snapshot("cn")
    assert restored["temperature"]==63.1
    assert restored["cold_backup_file"]=="market/temperature/test.parquet"


def test_temperature_archive_does_not_require_supabase_for_file_backup():
    source=(ROOT/"scripts/archive/archive_market_temperature.py").read_text(encoding="utf-8")
    assert "continuing with file archive only" in source
    assert "write_cold_snapshot(snapshot" in source
    assert "refusing to produce non-durable temperature history" not in source
