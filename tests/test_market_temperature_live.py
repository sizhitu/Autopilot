"""Live provider smoke tests; provider access/schema failures block the PR."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"trading_tool"))
from market_temperature import fetch_market_sentiment

def test_live_cls_market_degree():
    d=fetch_market_sentiment("cn")
    assert d["market"]=="cn" and isinstance(d["temperature"],float) and 0<=d["temperature"]<=100 and d["as_of"]

def test_live_cnn_fear_greed():
    d=fetch_market_sentiment("us")
    assert d["market"]=="us" and isinstance(d["temperature"],float) and 0<=d["temperature"]<=100 and d["source_timestamp"] and d["as_of"]
