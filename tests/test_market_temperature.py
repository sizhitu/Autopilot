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
            return {"data":{"total":1000,"diff":[{"f3":2,"f124":1794412800},{"f3":-1,"f124":1794412800},{"f3":0,"f124":1794412800}]*334}}
    monkeypatch.setattr(mt.requests,"get",lambda *a,**k:Response())
    result=mt._fetch_cn_breadth()
    assert result["advancers"]==334 and result["decliners"]==334
    assert result["score"]==50.0

def test_us_breadth_uses_ad_line_change_not_percent_of_negative_index():
    import pandas as pd
    import market_temperature as mt
    dates=pd.date_range("2026-01-01",periods=100,freq="B")
    frame=pd.DataFrame({"date":dates,"close":[-500+i*3+(i%5)*0.5 for i in range(100)]})
    class Fetcher:
        def fetch(self,symbol,days): return frame
    result=mt._fetch_us_breadth(Fetcher())
    assert 50 < result["score"] <= 100
    assert result["series"]==2

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


def test_cn_breadth_date_uses_quote_timestamp(monkeypatch):
    import market_temperature as mt
    stamp=1794412800
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"data":{"total":1002,"diff":[{"f3":2,"f124":stamp},{"f3":-1,"f124":stamp},{"f3":0,"f124":stamp}]*334}}
    monkeypatch.setattr(mt.requests,"get",lambda *a,**k:Response())
    result=mt._fetch_cn_breadth()
    expected=mt.datetime.fromtimestamp(stamp,mt.timezone.utc).astimezone(mt.ZoneInfo("Asia/Shanghai")).date().isoformat()
    assert result["data_date"]==expected


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


def test_us_breadth_requires_61_real_observations():
    import pandas as pd, pytest
    import market_temperature as mt
    dates=pd.date_range("2026-01-01",periods=60,freq="B")
    frame=pd.DataFrame({"date":dates,"close":[100+i for i in range(60)]})
    class Fetcher:
        def fetch(self,symbol,days): return frame
    with pytest.raises(ValueError): mt._fetch_us_breadth(Fetcher())


def test_position_temperature_ui_contains_style_and_industry_sections():
    html=(ROOT/"frontend/index.html").read_text(encoding="utf-8")
    assert 'id="temperatureStyles"' in html and 'id="temperatureIndustries"' in html
    assert "市场风格" in html and "七大产业热度" in html
