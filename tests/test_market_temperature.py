from __future__ import annotations
import sys, json
from pathlib import Path
from datetime import datetime, timezone, date
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"trading_tool"))

class Response:
    def __init__(self,payload,status=200): self.payload,self.status_code=payload,status
    def raise_for_status(self):
        if self.status_code>=400: raise RuntimeError(f"HTTP {self.status_code}")
    def json(self): return self.payload

def test_cn_uses_cls_provider_score(monkeypatch):
    import market_temperature as mt
    calls=[]
    monkeypatch.setattr(mt.requests,"get",lambda url,**kw:(calls.append((url,kw)) or Response({"code":200,"data":{"market_degree":"63.7"}})))
    x=mt.fetch_market_sentiment("cn")
    assert x["temperature"]==63.7 and x["regime"]=="偏热" and x["source"]=="财联社市场情绪"
    assert len(calls)==1 and calls[0][0]==mt.CN_URL and calls[0][1]["params"]==mt.CN_PARAMS

def test_us_uses_cnn_final_score_and_source_time(monkeypatch):
    import market_temperature as mt
    stamp=datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(mt.requests,"get",lambda *a,**k:Response({"fear_and_greed":{"score":27.4,"rating":"fear","timestamp":stamp}}))
    x=mt.fetch_market_sentiment("us")
    assert x["temperature"]==27.4 and x["regime"]=="恐惧" and x["source_timestamp"]==stamp

def test_provider_scores_must_be_numeric_and_in_range(monkeypatch):
    import market_temperature as mt, pytest
    monkeypatch.setattr(mt.requests,"get",lambda *a,**k:Response({"code":200,"data":{"market_degree":"N/A"}}))
    with pytest.raises(ValueError): mt.fetch_market_sentiment("cn")
    monkeypatch.setattr(mt.requests,"get",lambda *a,**k:Response({"fear_and_greed":{"score":101,"rating":"greed","timestamp":datetime.now(timezone.utc).isoformat()}}))
    with pytest.raises(ValueError): mt.fetch_market_sentiment("us")

def test_cnn_stale_data_rejected(monkeypatch):
    import market_temperature as mt, pytest
    monkeypatch.setattr(mt.requests,"get",lambda *a,**k:Response({"fear_and_greed":{"score":50,"rating":"neutral","timestamp":"2020-01-01T00:00:00Z"}}))
    with pytest.raises(ValueError,match="stale"): mt.fetch_market_sentiment("us")

def test_cold_backup_still_restores_provider_snapshot(tmp_path,monkeypatch):
    import market_temperature_cold_backup as cold
    snap={"market":"cn","as_of":date.today().isoformat(),"temperature":61.2,"status":"ok"}
    path=cold.write_cold_snapshot(snap,tmp_path,"market/temperature/test.parquet")
    monkeypatch.setenv("MARKET_TEMPERATURE_COLD_BACKUP_DIR",str(tmp_path))
    monkeypatch.setenv("MARKET_TEMPERATURE_COLD_BACKUP_BASE_URL","")
    got=cold.read_cold_snapshot("cn")
    assert path.name=="latest_cn.json" and got["temperature"]==61.2

def test_ui_is_minimal_provider_temperature_only():
    html=(ROOT/"frontend"/"index.html").read_text(encoding="utf-8")
    start=html.index('id="marketTemperatureCard"')
    end=html.index('  <div class="card" style="margin-bottom:10px;">',start)
    card=html[start:end]
    assert 'id="temperatureMarketCn"' in card and 'id="temperatureMarketUs"' in card
    assert 'id="temperatureSource"' in card and "temperatureStyles" not in card and "temperatureIndustries" not in card
    assert "数据覆盖" not in card and "数据不足" not in card


def test_supabase_restore_is_used_before_cold_file_fallback():
    source=(ROOT/"trading_tool"/"web_app.py").read_text(encoding="utf-8")
    start=source.index("def _load_market_temperature_from_supabase(")
    end=source.index("def _market_temperature_history(",start)
    helper=source[start:end]
    route=source[source.index('@app.get("/api/market-temperature")'):source.index('@app.get("/api/config")')]
    assert 'table("market_temperature_daily")' in helper
    assert '.select("trade_date,snapshot")' in helper
    assert '0 <= score <= 100' in helper
    assert 'age > 14' in helper
    assert '_load_market_temperature_from_supabase,market' in route
    assert route.index('_load_market_temperature_from_supabase,market') < route.index('read_cold_snapshot')
    assert '"supabase_restored":True' in route

def test_position_temperature_prefetches_on_initial_page_load():
    html=(ROOT/"frontend"/"index.html").read_text(encoding="utf-8")
    start=html.index("async function loadMarketTemperature(force,market)")
    end=html.index("async function loadMarketSpread(force)",start)
    loader=html[start:end]
    assert "try { loadMarketTemperature(false); } catch (e) {}" in loader
    assert "['cn','us']" in loader
