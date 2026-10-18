"""市场级股债利差。只服务看板顶部一条，不进自选列。"""
import hashlib
import logging
import math
import statistics
from datetime import date, datetime, timedelta
from urllib.request import Request, build_opener, HTTPCookieProcessor
import http.cookiejar
import json

logger = logging.getLogger("market_spread")

UA = "Mozilla/5.0 (compatible; AutopilotMarket/1.0)"


def _get(url, timeout=12, referer=""):
    req = Request(url, headers={"User-Agent": UA, "Referer": referer or url, "Accept": "application/json,text/html"})
    with build_opener().open(req, timeout=timeout) as r:
        return r.read()


def _legu_pe():
    """沪深300 总市值加权滚动市盈率（addTtmPe），与中证口径接近。"""
    cj = http.cookiejar.CookieJar()
    op = build_opener(HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA)]
    op.open("https://legulegu.com/stockdata/hs300-ttm-lyr", timeout=15).read()
    token = hashlib.md5(date.today().isoformat().encode()).hexdigest()
    url = f"https://legulegu.com/api/stockdata/index-basic-pe?indexCode=000300.SH&freq=day&token={token}"
    raw = op.open(Request(url, headers={"User-Agent": UA, "Referer": "https://legulegu.com/stockdata/hs300-ttm-lyr"}), timeout=20).read()
    rows = json.loads(raw).get("data") or []
    out = []
    for r in rows:
        pe = r.get("addTtmPe")
        if not pe or float(pe) <= 0:
            continue
        out.append({"date": str(r.get("date") or "")[:10], "pe": float(pe)})
    return out


def _cn10y_eastmoney_kline():
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=171.CN10Y&klt=101&fqt=1&lmt=900&end=20500101&fields1=f1,f2,f3&fields2=f51,f53"
    raw = _get(url, referer="https://quote.eastmoney.com/")
    data = json.loads(raw)
    klines = ((data.get("data") or {}).get("klines")) or []
    out = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 2:
            continue
        try:
            out.append({"date": parts[0][:10], "yield": float(parts[1])})
        except Exception:
            continue
    return out


def _cn10y_datacenter():
    """东财数据中心中美国债收益率。EMM00166466 为中债十年。行情接口 403 时走这条。"""
    url = "https://datacenter-web.eastmoney.com/api/data/get?type=RPTA_WEB_TREASURYYIELD&sty=ALL&st=SOLAR_DATE&sr=-1&p=1&ps=800"
    raw = _get(url, referer="https://data.eastmoney.com/cjsj/zmgzsyl.html")
    rows = ((json.loads(raw).get("result") or {}).get("data")) or []
    out = []
    for r in rows:
        val = r.get("EMM00166466")
        day = str(r.get("SOLAR_DATE") or "")[:10]
        if not day or val in (None, ""):
            continue
        try:
            out.append({"date": day, "yield": float(val)})
        except Exception:
            continue
    out.sort(key=lambda x: x["date"])
    return out


def _cn10y():
    try:
        rows = _cn10y_eastmoney_kline()
        if len(rows) >= 30:
            return rows
    except Exception as e:
        logger.warning("东财国债行情接口失败，改走数据中心: %s", e)
    return _cn10y_datacenter()


def _spx_pe():
    """Multpl 标普滚动市盈率月表。免费、公开、有历史。"""
    import re
    raw = _get("https://www.multpl.com/s-p-500-pe-ratio/table/by-month", timeout=20, referer="https://www.multpl.com/")
    html = raw.decode("utf-8", "ignore")
    rows = re.findall(r"<td>([A-Za-z]+\s+\d{1,2},\s+\d{4})</td>\s*<td>[\s\S]*?([0-9]+\.[0-9]+)\s*</td>", html)
    out = []
    for ds, val in rows:
        try:
            day = datetime.strptime(ds, "%b %d, %Y").strftime("%Y-%m-%d")
            out.append({"date": day, "pe": float(val)})
        except Exception:
            continue
    out.sort(key=lambda x: x["date"])
    return out


def _smh_pe():
    """Yahoo 报价页里的 SMH trailingPE。只有当前值，没有免费长历史。"""
    import re
    raw = _get("https://finance.yahoo.com/quote/SMH/", timeout=20, referer="https://finance.yahoo.com/")
    html = raw.decode("utf-8", "ignore")
    m = re.search(r'trailingPE\\*":\{\\*"raw\\*":([0-9.]+)', html)
    if not m:
        return None
    return float(m.group(1))


def _us10y():
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX?range=5y&interval=1mo"
    raw = _get(url)
    data = json.loads(raw)
    res = (data.get("chart") or {}).get("result") or []
    if not res:
        return []
    ts = res[0].get("timestamp") or []
    closes = (((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close")) or []
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        out.append({"date": datetime.utcfromtimestamp(t).strftime("%Y-%m-%d"), "yield": float(c)})
    return out


def _nearest(series, day):
    best = None
    best_gap = 10**9
    target = datetime.strptime(day, "%Y-%m-%d")
    for r in series:
        try:
            gap = abs((datetime.strptime(r["date"], "%Y-%m-%d") - target).days)
        except Exception:
            continue
        if gap < best_gap:
            best_gap = gap
            best = r
    return best if best and best_gap <= 20 else None


def _band(spreads):
    if len(spreads) < 8:
        return None
    vals = [s["spread"] for s in spreads]
    mean = statistics.fmean(vals)
    std = statistics.pstdev(vals) or 0.01
    last = spreads[-1]
    upper = mean + 2 * std
    lower = mean - 2 * std
    if last["spread"] >= upper - 0.4:
        pos = "靠近上轨·相对债券便宜"
    elif last["spread"] <= lower + 0.4:
        pos = "靠近下轨·相对债券贵"
    elif last["spread"] >= mean + 0.3:
        pos = "略高于两年半中枢"
    elif last["spread"] <= mean - 0.3:
        pos = "略低于两年半中枢"
    else:
        pos = "两年半中枢附近"
    return {
        "mean": round(mean, 2),
        "std": round(std, 2),
        "upper": round(upper, 2),
        "lower": round(lower, 2),
        "position": pos,
        "to_upper": round(upper - last["spread"], 2),
    }


def _join(pe_rows, yld_rows, months=30):
    if not pe_rows or not yld_rows:
        return []
    cutoff = (date.today() - timedelta(days=int(months * 30.5))).isoformat()
    out = []
    for pe in pe_rows:
        if pe["date"] < cutoff:
            continue
        y = _nearest(yld_rows, pe["date"])
        if not y:
            continue
        ey = 100.0 / pe["pe"]
        out.append({
            "date": pe["date"],
            "pe": round(pe["pe"], 2),
            "earnings_yield": round(ey, 2),
            "bond_yield": round(y["yield"], 2),
            "spread": round(ey - y["yield"], 2),
        })
    return out



def _cn_congestion():
    """乐咕大盘拥挤度：前5%个股成交额 / 全A成交额。优先读页面描述，不依赖表格结构。"""
    import re
    raw = _get("https://www.legulegu.com/stockdata/ashares-congestion", timeout=18, referer="https://www.legulegu.com/")
    html = raw.decode("utf-8", "ignore")
    m = re.search(r"(20\d{2}-\d{2}-\d{2})，大盘拥挤度：([0-9]+(?:\.[0-9]+)?)%", html)
    if not m:
        m = re.search(r"大盘拥挤度：([0-9]+(?:\.[0-9]+)?)%", html)
        if m:
            m = type("M", (), {"group": lambda self, i: (date.today().isoformat() if i == 1 else m.group(1))})()
    if not m:
        return None
    val = float(m.group(2))
    if val < 40:
        pos, note = "分散", "交易分散，微观结构正常"
    elif val < 50:
        pos, note = "集中升温", "成交向头部集中，留意局部过热"
    else:
        pos, note = "拥挤预警", "接近或超过50%，历史上常对应反转或风格切换"
    return {"ok": True, "date": m.group(1), "value": round(val, 2), "position": pos, "note": note,
            "source": "前5%成交额占比（乐咕）"}


def _us_vix():
    """CBOE VIX 收盘，衡量美股恐慌/拥挤。"""
    raw = _get("https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=5d&interval=1d", timeout=12)
    data = json.loads(raw)
    result = ((data.get("chart") or {}).get("result") or [None])[0] or {}
    meta = result.get("meta") or {}
    px = meta.get("regularMarketPrice") or meta.get("previousClose")
    if not px:
        return None
    val = float(px)
    if val < 15:
        pos, note = "低波动", "波动极低，容易自满，不算便宜"
    elif val < 25:
        pos, note = "正常", "波动正常，不构成拥挤或恐慌"
    elif val < 35:
        pos, note = "偏恐慌", "波动抬升，风险偏好下降"
    else:
        pos, note = "极端恐慌", "极端波动，常对应情绪底部区域"
    return {"ok": True, "value": round(val, 2), "position": pos, "note": note, "source": "VIX"}


def build_market_spread():
    cn = {"ok": False, "name": "沪深300"}
    us = {"ok": False, "name": "标普500"}
    try:
        pe = _legu_pe()
        yld = _cn10y()
        series = _join(pe, yld)
        if series:
            last = series[-1]
            cn = {
                "ok": True,
                "name": "沪深300",
                "date": last["date"],
                "pe": last["pe"],
                "earnings_yield": last["earnings_yield"],
                "bond_yield": last["bond_yield"],
                "spread": last["spread"],
                "band": _band(series),
                "note": "盈利收益率=1/总市值加权滚动市盈率，减去中国十年国债",
            }
    except Exception as e:
        logger.warning("A股利差失败: %s", e)
        cn["error"] = str(e)[:80]
    try:
        pe = _spx_pe()
        yld = _us10y()
        series = _join(pe, yld)
        if series:
            last = series[-1]
            us = {
                "ok": True,
                "name": "标普500",
                "date": last["date"],
                "pe": last["pe"],
                "earnings_yield": last["earnings_yield"],
                "bond_yield": last["bond_yield"],
                "spread": last["spread"],
                "band": _band(series),
                "note": "盈利收益率=1/标普滚动市盈率（Multpl），减去美国十年国债",
            }
    except Exception as e:
        logger.warning("美股利差失败: %s", e)
        us["error"] = str(e)[:80]
    smh = {"ok": False, "name": "SMH半导体"}
    try:
        pe = _smh_pe()
        yld = _us10y()
        if pe and pe > 0 and yld:
            bond = float(yld[-1]["yield"])
            ey = 100.0 / pe
            smh = {
                "ok": True,
                "name": "SMH半导体",
                "date": yld[-1]["date"],
                "pe": round(pe, 2),
                "earnings_yield": round(ey, 2),
                "bond_yield": round(bond, 2),
                "spread": round(ey - bond, 2),
                "note": "盈利收益率=1/SMH滚动市盈率，减去美国十年国债。行业周期强，不能和标普中枢直接比",
            }
    except Exception as e:
        logger.warning("SMH利差失败: %s", e)
        smh["error"] = str(e)[:80]
    crowd = {"cn": {"ok": False}, "us": {"ok": False}}
    try:
        hit = _cn_congestion()
        if hit:
            crowd["cn"] = hit
    except Exception as e:
        logger.warning("A股拥挤度失败: %s", e)
        crowd["cn"] = {"ok": False, "error": str(e)[:80]}
    try:
        hit = _us_vix()
        if hit:
            crowd["us"] = hit
    except Exception as e:
        logger.warning("美股VIX失败: %s", e)
        crowd["us"] = {"ok": False, "error": str(e)[:80]}
    return {
        "success": True,
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "cn": cn,
        "us": us,
        "smh": smh,
        "congestion": crowd,
        "disclaimer": "只管总仓位和节奏，不替代个股操盘动作。不是投资建议。",
    }
