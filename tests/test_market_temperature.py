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
    monkeypatch.setattr(mt,"_fetch_frames",lambda f,s:([],[]))
    monkeypatch.setattr(mt,"_trend_score",lambda f:(70.,"2026-11-11"))
    monkeypatch.setattr(mt,"_activity_score",lambda f:60.)
    monkeypatch.setattr(mt,"_risk_score_cn",lambda f:80.)
    monkeypatch.setattr(mt,"_fetch_cn_breadth",lambda:{"score":40.,"source":"test","data_date":"2026-11-11"})
    monkeypatch.setattr(mt,"_clean_frame",lambda f:None)
    r=mt.build_market_temperature_snapshot("cn",fetcher=object())
    assert r["coverage"]==100 and r["temperature"]==59.0 and r["regime"]=="中性"

def test_temperature_is_withheld_when_breadth_missing(monkeypatch):
    import market_temperature as mt
    monkeypatch.setattr(mt,"_fetch_frames",lambda f,s:([],[]))
    monkeypatch.setattr(mt,"_trend_score",lambda f:(70.,"2026-11-11"))
    monkeypatch.setattr(mt,"_activity_score",lambda f:60.)
    monkeypatch.setattr(mt,"_risk_score_cn",lambda f:80.)
    monkeypatch.setattr(mt,"_fetch_cn_breadth",lambda:(_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(mt,"_clean_frame",lambda f:None)
    r=mt.build_market_temperature_snapshot("cn",fetcher=object())
    assert r["coverage"]==65 and r["temperature"] is None and r["status"]=="insufficient_data"

def test_trend_score_uses_multiple_indices():
    import pandas as pd
    import market_temperature as mt
    dates=pd.date_range("2025-01-01",periods=100,freq="B")
    frames=[pd.DataFrame({"date":dates,"close":[100+i for i in range(100)]}) for _ in range(3)]
    score,day=mt._trend_score(frames)
    assert score is not None and score>70 and day=="2025-05-20"

def test_activity_and_risk_require_history():
    import pandas as pd
    import market_temperature as mt
    df=pd.DataFrame({"date":pd.date_range("2026-01-01",periods=30),"close":range(100,130),"volume":range(1000,1030)})
    assert mt._activity_score(df) is None and mt._risk_score_cn(df) is None
