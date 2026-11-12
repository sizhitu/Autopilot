"""Thin adapters for provider-computed market sentiment indexes; no local scoring."""
from __future__ import annotations
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import math
import requests

CN_URL = "https://x-quote.cls.cn/v2/quote/a/stock/emotion"
US_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata/2021-02-01"
CN_PARAMS = {"app":"CailianpressWeb","os":"web","sv":"8.4.6","sign":"9f8797a1f4de66c2370f7a03990d2737"}
CN_HEADERS = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36","Accept":"application/json, text/plain, */*","Referer":"https://www.cls.cn/","Origin":"https://www.cls.cn"}
US_HEADERS = {"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36","Accept":"application/json, text/plain, */*","Origin":"https://www.cnn.com","Referer":"https://www.cnn.com/"}
TIMEOUT = (3.05, 8)

def _score(value):
    try: number = float(value)
    except (TypeError, ValueError): raise ValueError("provider score is not numeric")
    if not math.isfinite(number) or not 0 <= number <= 100: raise ValueError(f"provider score outside 0-100: {value!r}")
    return round(number, 1)

def _cn_regime(score):
    return "极度冰冷" if score < 20 else "偏冷" if score < 40 else "中性" if score < 60 else "偏热" if score < 80 else "过热"

def _cn_snapshot():
    response=requests.get(CN_URL,params=CN_PARAMS,headers=CN_HEADERS,timeout=TIMEOUT)
    response.raise_for_status()
    payload=response.json()
    if not isinstance(payload,dict) or payload.get("code") != 200: raise RuntimeError(f"CLS API rejected request: {str(payload)[:200]}")
    body=payload.get("data")
    if not isinstance(body,dict): raise ValueError("CLS response missing data")
    score=_score(body.get("market_degree"))
    fetched=datetime.now(ZoneInfo("Asia/Shanghai"))
    source_time=body.get("update_time") or body.get("timestamp") or body.get("trade_date")
    source_date=str(source_time)[:10] if source_time else fetched.date().isoformat()
    if len(source_date)!=10 or source_date[4:5]!="-": source_date=fetched.date().isoformat()
    return {"market":"cn","temperature":score,"regime":_cn_regime(score),"as_of":source_date,
      "source":"财联社市场情绪","source_url":"https://www.cls.cn/","source_timestamp":str(source_time) if source_time else None,
      "fetched_at":fetched.isoformat(),"status":"ok","provider_rating":None}

def _us_snapshot():
    response=requests.get(US_URL,headers=US_HEADERS,timeout=TIMEOUT)
    response.raise_for_status()
    payload=response.json()
    headline=payload.get("fear_and_greed") if isinstance(payload,dict) else None
    if not isinstance(headline,dict): raise ValueError("CNN response missing fear_and_greed")
    score=_score(headline.get("score"))
    stamp=headline.get("timestamp")
    if not stamp: raise ValueError("CNN response missing timestamp")
    try: parsed=datetime.fromisoformat(str(stamp).replace("Z","+00:00"))
    except ValueError: raise ValueError("CNN timestamp invalid")
    if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
    age=(datetime.now(timezone.utc)-parsed.astimezone(timezone.utc)).total_seconds()
    if age < -3600 or age > 7*86400: raise ValueError(f"CNN sentiment timestamp stale: {stamp}")
    rating=str(headline.get("rating") or "").strip().lower()
    labels={"extreme fear":"极度恐惧","fear":"恐惧","neutral":"中性","greed":"贪婪","extreme greed":"极度贪婪"}
    return {"market":"us","temperature":score,"regime":labels.get(rating,_cn_regime(score)),
      "as_of":parsed.date().isoformat(),"source":"CNN Fear & Greed",
      "source_url":"https://www.cnn.com/markets/fear-and-greed","source_timestamp":str(stamp),
      "fetched_at":datetime.now(timezone.utc).isoformat(),"status":"ok","provider_rating":rating}

def fetch_market_sentiment(market):
    if market=="cn": return _cn_snapshot()
    if market=="us": return _us_snapshot()
    raise ValueError(f"unsupported market: {market}")

def build_market_temperature_snapshot(market, fetcher=None):
    """Compatibility alias for the existing archive job; fetcher is deliberately unused."""
    return fetch_market_sentiment(market)
