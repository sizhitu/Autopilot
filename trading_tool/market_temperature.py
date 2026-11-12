"""Market temperature v2: market-wide temperature, style leadership and industry heat."""
from __future__ import annotations
import logging, math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import requests

logger=logging.getLogger("market_temperature")
WEIGHTS={"breadth":40.0,"trend":25.0,"activity":15.0,"risk":20.0}
MIN_COVERAGE=80.0
METHOD_VERSION="2.0.0"
CN_INDEXES=["sh000001","sz399001","sz399006","sh000300","sh000905"]
US_INDEXES=["SPY","QQQ","IWM","DIA"]
STYLE_CONFIG={
 "cn":[{"key":"large_cap","label":"大盘","symbol":"sh000300","weight":50.0},{"key":"small_mid","label":"中小盘","symbol":"sh000852","weight":25.0},{"key":"technology","label":"科技成长","symbol":"sh000688","weight":25.0}],
 "us":[{"key":"large_cap","label":"大盘","symbol":"SPY","weight":50.0},{"key":"small_mid","label":"中小盘","symbol":"IWM","weight":25.0},{"key":"technology","label":"科技成长","symbol":"QQQ","weight":25.0}]}
STYLE_WEIGHTS={"large_cap":50.0,"small_mid":25.0,"technology":25.0}
INDUSTRY_CONFIG={
 "cn":[
  {"key":"semiconductor","label":"芯片半导体","symbol":"512480","proxy":"半导体ETF"},
  {"key":"datacenter","label":"数据中心","symbol":"516510","proxy":"云计算/数据中心链代理"},
  {"key":"power","label":"核能与电力","symbol":"561560","proxy":"电力ETF，非纯核能"},
  {"key":"robotics","label":"机器人制造","symbol":"562500","proxy":"机器人ETF"},
  {"key":"biotech","label":"生物科技","symbol":"512290","proxy":"生物医药ETF"},
  {"key":"space","label":"太空经济","symbol":"512630","proxy":"卫星产业代理"},
  {"key":"low_altitude","label":"低空经济","symbol":"159378","proxy":"通用航空ETF"}],
 "us":[
  {"key":"semiconductor","label":"芯片半导体","symbol":"SMH","proxy":"VanEck Semiconductor ETF"},
  {"key":"datacenter","label":"数据中心","symbol":"SRVR","proxy":"Data & Infrastructure REIT ETF"},
  {"key":"power","label":"核能与电力","symbol":"NLR","proxy":"核能行业ETF"},
  {"key":"robotics","label":"机器人制造","symbol":"BOTZ","proxy":"Robotics & AI ETF"},
  {"key":"biotech","label":"生物科技","symbol":"XBI","proxy":"SPDR S&P Biotech ETF"},
  {"key":"space","label":"太空经济","symbol":"UFO","proxy":"Procure Space ETF"},
  {"key":"low_altitude","label":"低空经济","symbol":"DRNZ","proxy":"Drone ETF"}]}
INDUSTRY_WEIGHTS={"relative_strength":40.0,"trend":35.0,"momentum":25.0}

def _clean_frame(frame):
 if frame is None or not isinstance(frame,pd.DataFrame) or frame.empty or not {"date","close"}.issubset(frame.columns): return None
 df=frame.copy(); df["date"]=pd.to_datetime(df["date"],errors="coerce").dt.tz_localize(None); df["close"]=pd.to_numeric(df["close"],errors="coerce")
 if "volume" in df: df["volume"]=pd.to_numeric(df["volume"],errors="coerce")
 df=df.dropna(subset=["date","close"]); df=df[df["close"]>0].sort_values("date").drop_duplicates("date")
 return df.reset_index(drop=True) if len(df)>=60 else None

def _rank_score(value,history):
 vals=pd.to_numeric(pd.Series(history),errors="coerce").dropna()
 if value is None or not math.isfinite(float(value)) or len(vals)<40:return None
 return round(float(((vals<float(value)).sum()+.5*(vals==float(value)).sum())/len(vals))*100,1)

def _trend_score(frames):
 scores=[]; dates=[]
 for df in frames:
  if df is None or len(df)<220:continue
  c=df["close"].astype(float); last=float(c.iloc[-1]); ma50=float(c.tail(50).mean()); ma200=float(c.tail(200).mean()); ret20=last/float(c.iloc[-21])-1
  structure=(int(last>ma50)+int(last>ma200)+int(ma50>ma200))/3*70
  scores.append(structure+max(0.,min(30.,15.+ret20*300))); dates.append(str(df["date"].iloc[-1].date()))
 return (round(float(np.mean(scores)),1),min(dates)) if scores else (None,None)

def _activity_score(frame):
 if frame is None or "volume" not in frame:return None
 v=pd.to_numeric(frame["volume"],errors="coerce").dropna()
 if len(v)<60:return None
 avg=v.rolling(20).mean().dropna()
 return _rank_score(float(avg.iloc[-1]),avg.tail(250)) if len(avg)>=40 and avg.iloc[-1]>0 else None

def _risk_score_cn(frame):
 if frame is None:return None
 rv=frame["close"].astype(float).pct_change().rolling(20).std()*math.sqrt(252)*100; rv=rv.dropna()
 if len(rv)<60:return None
 p=_rank_score(float(rv.iloc[-1]),rv.tail(250)); return None if p is None else round(100-p,1)

def _risk_score_us(frame):
 if frame is None:return None
 c=pd.to_numeric(frame["close"],errors="coerce").dropna()
 if len(c)<60:return None
 p=_rank_score(float(c.iloc[-1]),c.tail(250)); return None if p is None else round(100-p,1)

def _fetch_cn_breadth():
 url="https://push2.eastmoney.com/api/qt/clist/get"
 params={"pn":1,"pz":6000,"po":1,"np":1,"fltt":2,"invt":2,"fid":"f3","fs":"m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048","fields":"f12,f14,f3,f124"}
 r=requests.get(url,params=params,headers={"User-Agent":"Mozilla/5.0","Referer":"https://quote.eastmoney.com/"},timeout=12); r.raise_for_status()
 data=(r.json() or {}).get("data") or {}; rows=data.get("diff") or []; total=int(data.get("total") or len(rows)); changes=[]; stamps=[]
 for row in rows:
  try:
   x=float(row.get("f3"))
   if math.isfinite(x) and x>-1000:
    changes.append(x); stamp=float(row.get("f124"))
    if stamp>0:stamps.append(stamp/1000 if stamp>1e12 else stamp)
  except (TypeError,ValueError):pass
 if total<1000 or len(changes)<1000 or len(changes)<total*.75:raise ValueError(f"Eastmoney breadth coverage insufficient: {len(changes)}/{total}")
 if not stamps:raise ValueError("Eastmoney breadth quote timestamp missing")
 day=datetime.fromtimestamp(max(stamps),timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
 adv=sum(x>0 for x in changes); dec=sum(x<0 for x in changes); unchanged=sum(x==0 for x in changes)
 return {"score":round(adv/(adv+dec)*100,1) if adv+dec else None,"advancers":adv,"decliners":dec,"unchanged":unchanged,"universe":len(changes),"source":"Eastmoney A股全市场实时涨跌家数","data_date":day}

def _fetch_us_breadth(fetcher):
 scores=[]; dates=[]
 for symbol in ("^NYAD","^NAAD"):
  try:
   df=_clean_frame(fetcher.fetch(symbol,300))
   if df is None or len(df)<61:continue
   c=df["close"].astype(float); daily=c.diff().dropna(); sigma=float(daily.tail(250).std())*math.sqrt(20)
   if not math.isfinite(sigma) or sigma<=0:continue
   z20=float(c.iloc[-1]-c.iloc[-21])/sigma; z60=float(c.iloc[-1]-c.iloc[-61])/(sigma*math.sqrt(3))
   scores.append(max(0.,min(100.,50+25*math.tanh(z20/2)*.65+25*math.tanh(z60/2)*.35)))
   dates.append(str(df["date"].iloc[-1].date()))
  except Exception as exc:logger.info("US breadth %s unavailable: %s",symbol,exc)
 if not scores:raise ValueError("NYSE/Nasdaq advance-decline line unavailable")
 return {"score":round(float(np.mean(scores)),1),"series":len(scores),"source":"NYSE/Nasdaq cumulative advance-decline line","data_date":min(dates)}

def _fetch_frames(fetcher,symbols):
 frames={}; errors=[]
 for sym in symbols:
  try:
   df=_clean_frame(fetcher.fetch(sym,300))
   if df is None:errors.append(f"{sym}:insufficient_history")
   else:frames[sym]=df
  except Exception as exc:errors.append(f"{sym}:{type(exc).__name__}")
 return frames,errors

def _fresh(frame,reference):
 if frame is None or reference is None or frame.empty:return False
 return abs((pd.Timestamp(reference).date()-frame["date"].iloc[-1].date()).days)<=5

def _build_styles(market,fetcher,benchmark):
 config=STYLE_CONFIG[market]; frames,errors=_fetch_frames(fetcher,[x["symbol"] for x in config]); out={}; weighted=weight=0.; dates=[]
 ref=benchmark["date"].iloc[-1] if benchmark is not None else None
 for item in config:
  df=frames.get(item["symbol"]); score,day=_trend_score([df])
  if score is not None and not _fresh(df,ref):score=day=None
  out[item["key"]]={"label":item["label"],"symbol":item["symbol"],"weight":item["weight"],"score":score,"data_date":day}
  if score is not None:weighted+=score*item["weight"];weight+=item["weight"];dates.append(day)
 out.update({"composite":round(weighted/weight,1) if weight else None,"coverage":weight,"data_date":min(dates) if dates else None,"errors":errors})
 return out

def _industry_score(frame,benchmark):
 if frame is None or benchmark is None or len(frame)<220 or len(benchmark)<220:return None
 joined=pd.merge(frame[["date","close"]],benchmark[["date","close"]],on="date",suffixes=("_asset","_market"),how="inner").dropna()
 if len(joined)<61 or not _fresh(frame,benchmark["date"].iloc[-1]):return None
 a=joined["close_asset"].astype(float); m=joined["close_market"].astype(float)
 rel60=(float(a.iloc[-1]/a.iloc[-61]))/(float(m.iloc[-1]/m.iloc[-61]))-1; rel_score=50+50*math.tanh(rel60*5)
 c=frame["close"].astype(float); last=float(c.iloc[-1]); ma50=float(c.tail(50).mean()); ma200=float(c.tail(200).mean())
 trend=(int(last>ma50)+int(last>ma200)+int(ma50>ma200))/3*100
 ret5=last/float(c.iloc[-6])-1; ret20=last/float(c.iloc[-21])-1
 momentum=max(0.,min(100.,50+50*math.tanh((ret5-ret20/4)*12)))
 score=(rel_score*40+trend*35+momentum*25)/100
 return {"score":round(score,1),"relative_60d":round(rel60*100,2),"relative_score":round(rel_score,1),"trend_score":round(trend,1),"momentum_score":round(momentum,1),"data_date":str(frame["date"].iloc[-1].date())}

def _build_industries(market,fetcher,benchmark):
 config=INDUSTRY_CONFIG[market]; frames,errors=_fetch_frames(fetcher,[x["symbol"] for x in config]); rows=[]
 for item in config:
  df=frames.get(item["symbol"]); metrics=_industry_score(df,benchmark)
  reason=("行情日期落后" if df is not None and benchmark is not None and not _fresh(df,benchmark["date"].iloc[-1]) else "历史数据不足或不可用") if metrics is None else None
  rows.append({"key":item["key"],"label":item["label"],"symbol":item["symbol"],"proxy":item["proxy"],"status":"ok" if metrics else "insufficient_data","reason":reason,**(metrics or {"score":None,"data_date":None})})
 rows.sort(key=lambda x:(x["score"] is not None,x["score"] if x["score"] is not None else -1),reverse=True)
 for i,row in enumerate(rows,1):row["rank"]=i if row["score"] is not None else None
 return {"items":rows,"available":sum(x["score"] is not None for x in rows),"total":len(rows),"errors":errors,"benchmark":"沪深300" if market=="cn" else "SPY"}

def build_market_temperature_snapshot(market,fetcher=None):
 market=str(market or "").lower()
 if market not in ("cn","us"):raise ValueError("market must be cn or us")
 if fetcher is None:
  from data_fetcher import DataFetcher
  fetcher=DataFetcher()
 frames,errors=_fetch_frames(fetcher,CN_INDEXES if market=="cn" else US_INDEXES)
 trend,trend_date=_trend_score(list(frames.values()))
 activity_symbol="510300" if market=="cn" else "SPY"
 try:activity_frame=_clean_frame(fetcher.fetch(activity_symbol,300))
 except Exception:activity_frame=None
 activity=_activity_score(activity_frame)
 if market=="cn":
  try:breadth=_fetch_cn_breadth();breadth_score,breadth_date=breadth["score"],breadth["data_date"]
  except Exception as exc:breadth={"source":"Eastmoney A股全市场涨跌家数","error":str(exc)[:180]};breadth_score=breadth_date=None
  try:risk_frame=_clean_frame(fetcher.fetch("000001",300))
  except Exception:risk_frame=None
  risk,risk_source=_risk_score_cn(risk_frame),"上证指数20日实现波动率历史分位（反向）"
 else:
  try:breadth=_fetch_us_breadth(fetcher);breadth_score,breadth_date=breadth["score"],breadth["data_date"]
  except Exception as exc:breadth={"source":"NYSE/Nasdaq cumulative advance-decline line","error":str(exc)[:180]};breadth_score=breadth_date=None
  try:risk_frame=_clean_frame(fetcher.fetch("^VIX",300))
  except Exception:risk_frame=None
  risk,risk_source=_risk_score_us(risk_frame),"VIX历史分位（反向）"
 risk_date=str(risk_frame["date"].iloc[-1].date()) if risk_frame is not None else None
 components={"breadth":{"score":breadth_score,"weight":40.,"source":breadth.get("source"),"data_date":breadth_date,"detail":breadth},
 "trend":{"score":trend,"weight":25.,"source":"宽基指数MA50/MA200结构与20日动量","data_date":trend_date,"samples":len(frames)},
 "activity":{"score":activity,"weight":15.,"source":f"{activity_symbol}近20日均量的250日历史分位","data_date":str(activity_frame["date"].iloc[-1].date()) if activity_frame is not None else None},
 "risk":{"score":risk,"weight":20.,"source":risk_source,"data_date":risk_date}}
 coverage=round(sum(x["weight"] for x in components.values() if x["score"] is not None),1)
 temp=round(sum(float(x["score"])*x["weight"] for x in components.values() if x["score"] is not None)/coverage,1) if coverage>=MIN_COVERAGE else None
 regime="数据不足" if temp is None else "极度恐慌" if temp<=20 else "偏谨慎" if temp<=40 else "中性" if temp<=60 else "偏乐观" if temp<=80 else "过热"
 dates=[x["data_date"] for x in components.values() if x["score"] is not None and x.get("data_date")]
 benchmark_symbol="sh000300" if market=="cn" else "SPY"; benchmark=frames.get(benchmark_symbol)
 if benchmark is None:
  try:benchmark=_clean_frame(fetcher.fetch(benchmark_symbol,300))
  except Exception:benchmark=None
 styles=_build_styles(market,fetcher,benchmark); industries=_build_industries(market,fetcher,benchmark)
 return {"market":market,"temperature":temp,"regime":regime,"coverage":coverage,"min_coverage":MIN_COVERAGE,
 "components":components,"as_of":min(dates) if dates else None,"calculated_at":datetime.now(timezone.utc).isoformat(),
 "method_version":METHOD_VERSION,"status":"ok" if temp is not None else "insufficient_data","notes":errors,
 "styles":styles,"industries":industries,"method":{"temperature_weights":WEIGHTS,"style_weights":STYLE_WEIGHTS,"industry_weights":INDUSTRY_WEIGHTS}}
