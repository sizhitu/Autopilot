"""Independent CN/US market sentiment temperature; missing inputs are never imputed."""
from __future__ import annotations
import logging, math
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests

logger = logging.getLogger("market_temperature")
WEIGHTS = {"breadth": 35.0, "trend": 25.0, "activity": 20.0, "risk": 20.0}
MIN_COVERAGE = 80.0
CN_INDEXES = ["sh000001", "sz399001", "sz399006", "sh000300", "sh000905"]
US_INDEXES = ["SPY", "QQQ", "IWM", "DIA"]

def _clean_frame(frame):
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty or not {"date","close"}.issubset(frame.columns): return None
    df=frame.copy(); df["date"]=pd.to_datetime(df["date"],errors="coerce"); df["close"]=pd.to_numeric(df["close"],errors="coerce")
    if "volume" in df: df["volume"]=pd.to_numeric(df["volume"],errors="coerce")
    df=df.dropna(subset=["date","close"]).sort_values("date").drop_duplicates("date")
    return df.reset_index(drop=True) if len(df)>=60 else None

def _rank_score(value, history):
    vals=pd.to_numeric(pd.Series(history),errors="coerce").dropna()
    if value is None or not math.isfinite(float(value)) or len(vals)<40: return None
    return round(float(((vals<float(value)).sum()+.5*(vals==float(value)).sum())/len(vals))*100,1)

def _trend_score(frames):
    scores=[]; dates=[]
    for df in frames:
        if df is None or len(df)<220: continue
        c=df["close"].astype(float); last=float(c.iloc[-1]); ma50=float(c.tail(50).mean()); ma200=float(c.tail(200).mean())
        ret20=last/float(c.iloc[-21])-1 if c.iloc[-21] else 0
        structure=(int(last>ma50)+int(last>ma200)+int(ma50>ma200))/3*70
        momentum=max(0.,min(30.,15.+ret20*300))
        scores.append(structure+momentum); dates.append(str(df["date"].iloc[-1].date()))
    return (round(float(np.mean(scores)),1),max(dates)) if scores else (None,None)

def _activity_score(frame):
    if frame is None or "volume" not in frame: return None
    v=pd.to_numeric(frame["volume"],errors="coerce").dropna()
    if len(v)<60: return None
    avg=v.rolling(20).mean().dropna()
    return _rank_score(float(avg.iloc[-1]),avg.tail(250)) if len(avg)>=40 and avg.iloc[-1]>0 else None

def _risk_score_cn(frame):
    if frame is None: return None
    rv=frame["close"].astype(float).pct_change().rolling(20).std()*math.sqrt(252)*100
    rv=rv.dropna()
    if len(rv)<60: return None
    p=_rank_score(float(rv.iloc[-1]),rv.tail(250))
    return None if p is None else round(100-p,1)

def _risk_score_us(frame):
    if frame is None: return None
    c=pd.to_numeric(frame["close"],errors="coerce").dropna()
    if len(c)<60: return None
    p=_rank_score(float(c.iloc[-1]),c.tail(250))
    return None if p is None else round(100-p,1)

def _fetch_cn_breadth():
    """One paginated-universe quote call; require >=75% of reported A-share universe."""
    url="https://push2.eastmoney.com/api/qt/clist/get"
    params={"pn":1,"pz":6000,"po":1,"np":1,"fltt":2,"invt":2,"fid":"f3",
            "fs":"m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048","fields":"f12,f14,f3"}
    r=requests.get(url,params=params,headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/"},timeout=12)
    r.raise_for_status(); data=(r.json() or {}).get("data") or {}; rows=data.get("diff") or []
    total=int(data.get("total") or len(rows)); changes=[]
    for row in rows:
        try:
            x=float(row.get("f3"))
            if math.isfinite(x) and x>-1000: changes.append(x)
        except (TypeError,ValueError): pass
    if total<1000 or len(changes)<1000 or len(changes)<total*.75: raise ValueError(f"Eastmoney breadth coverage insufficient: {len(changes)}/{total}")
    adv=sum(x>0 for x in changes); dec=sum(x<0 for x in changes); unchanged=sum(x==0 for x in changes); n=adv+dec+unchanged
    if n<1000: raise ValueError("A-share breadth sample too small")
    return {"score":round(adv/(adv+dec)*100,1) if adv+dec else 50.0,"advancers":adv,"decliners":dec,"unchanged":unchanged,"universe":n,
            "source":"Eastmoney A股全市场实时涨跌家数","data_date":datetime.now().date().isoformat()}

def _fetch_us_breadth(fetcher):
    """NYSE/Nasdaq advance-decline lines; unavailable means no overall score."""
    scores=[]; dates=[]
    for symbol in ("^NYAD","^NAAD"):
        try:
            df=_clean_frame(fetcher.fetch(symbol,300))
            if df is None or len(df)<60: continue
            c=df["close"].astype(float)
            daily=c.diff().dropna()
            if len(daily)<60: continue
            sigma=float(daily.tail(250).std())*math.sqrt(20)
            if not math.isfinite(sigma) or sigma<=0: continue
            d20=float(c.iloc[-1]-c.iloc[-21])
            d60=float(c.iloc[-1]-c.iloc[-61]) if len(c)>=61 else d20*3
            z20=d20/sigma; z60=d60/(sigma*math.sqrt(3))
            s=50+25*math.tanh(z20/2)*.65+25*math.tanh(z60/2)*.35
            scores.append(max(0.,min(100.,s))); dates.append(str(df["date"].iloc[-1].date()))
        except Exception as exc: logger.info("US breadth %s unavailable: %s",symbol,exc)
    if not scores: raise ValueError("NYSE/Nasdaq advance-decline line unavailable")
    return {"score":round(float(np.mean(scores)),1),"series":len(scores),"source":"NYSE/Nasdaq advance-decline line","data_date":max(dates)}

def _fetch_frames(fetcher,symbols):
    frames=[]; errors=[]
    for sym in symbols:
        try:
            df=_clean_frame(fetcher.fetch(sym,300))
            if df is not None: frames.append(df)
            else: errors.append(sym+":insufficient")
        except Exception as exc: errors.append(sym+":"+type(exc).__name__)
    return frames,errors

def build_market_temperature_snapshot(market,fetcher=None):
    market=str(market or "").lower()
    if market not in ("cn","us"): raise ValueError("market must be cn or us")
    if fetcher is None:
        from data_fetcher import DataFetcher
        fetcher=DataFetcher()
    frames,errors=_fetch_frames(fetcher,CN_INDEXES if market=="cn" else US_INDEXES)
    trend,trend_date=_trend_score(frames)
    activity_symbol="510300" if market=="cn" else "SPY"
    try: activity_frame=_clean_frame(fetcher.fetch(activity_symbol,300))
    except Exception: activity_frame=None
    activity=_activity_score(activity_frame)
    if market=="cn":
        try: breadth=_fetch_cn_breadth(); breadth_score=breadth["score"]; breadth_date=trend_date
        except Exception as exc: breadth={"source":"Eastmoney A股全市场涨跌家数","error":str(exc)[:180]}; breadth_score=breadth_date=None
        try: risk_frame=_clean_frame(fetcher.fetch("000001",300))
        except Exception: risk_frame=None
        risk=_risk_score_cn(risk_frame); risk_source="上证指数20日实现波动率历史分位（反向）"
    else:
        try: breadth=_fetch_us_breadth(fetcher); breadth_score=breadth["score"]; breadth_date=breadth["data_date"]
        except Exception as exc: breadth={"source":"NYSE/Nasdaq advance-decline line","error":str(exc)[:180]}; breadth_score=breadth_date=None
        try: vix_frame=_clean_frame(fetcher.fetch("^VIX",300))
        except Exception: vix_frame=None
        risk=_risk_score_us(vix_frame); risk_source="VIX历史分位（反向）"; risk_frame=vix_frame
    risk_date=str(risk_frame["date"].iloc[-1].date()) if risk_frame is not None else None
    components={
      "breadth":{"score":breadth_score,"weight":WEIGHTS["breadth"],"source":breadth.get("source"),"data_date":breadth_date,"detail":breadth},
      "trend":{"score":trend,"weight":WEIGHTS["trend"],"source":"宽基指数MA50/MA200结构与20日动量","data_date":trend_date,"samples":len(frames)},
      "activity":{"score":activity,"weight":WEIGHTS["activity"],"source":f"{activity_symbol}近20日均量的250日历史分位","data_date":str(activity_frame["date"].iloc[-1].date()) if activity_frame is not None else None},
      "risk":{"score":risk,"weight":WEIGHTS["risk"],"source":risk_source,"data_date":risk_date}}
    coverage=round(sum(x["weight"] for x in components.values() if x["score"] is not None),1)
    temperature=round(sum(float(x["score"])*x["weight"] for x in components.values() if x["score"] is not None)/coverage,1) if coverage>=MIN_COVERAGE else None
    regime=("数据不足" if temperature is None else "极度恐慌" if temperature<=20 else "偏谨慎" if temperature<=40 else "中性" if temperature<=60 else "偏乐观" if temperature<=80 else "过热")
    dates=[x["data_date"] for x in components.values() if x["score"] is not None and x.get("data_date")]
    return {"market":market,"temperature":temperature,"regime":regime,"coverage":coverage,"min_coverage":MIN_COVERAGE,
            "components":components,"as_of":min(dates) if dates else None,"calculated_at":datetime.now(timezone.utc).isoformat(),
            "method_version":"1.0.0","status":"ok" if temperature is not None else "insufficient_data","notes":errors}
