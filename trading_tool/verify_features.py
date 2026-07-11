import sys, os, re, json
sys.path.insert(0, "/workspace/trading_tool")
import pandas as pd, numpy as np
from data_fetcher import DataFetcher
import watchlist as wl
from watchlist import get_stock_status, _detect_high_low, WATCHLIST
from nine_turn import calc_nine_turn, calc_nine_turn_monthly, _compact, _nt_state, to_monthly
from strategy_engine import FujimotoStrategy
from web_app import result_to_dict, df_to_chart_json

f = DataFetcher()

# 典型标的：美股、美股ETF、A股、A股ETF基金、指数
SYMS = ['NVDA','SMH','MU','600887','600111','518850','000001']

# 先统一抓取一次，避免重复抓取造成的数据不一致（美股兜底仅15根，易前后不一致）
DFCACHE = {}
for code in SYMS:
    try:
        DFCACHE[code] = f.fetch(code, 300)
    except Exception as e:
        DFCACHE[code] = None
        print(f"[WARN] {code} fetch失败: {e}")

# monkeypatch：让 get_stock_status 用我们已抓好的同一份 df
orig_fetch = wl.fetcher.fetch
def patched(code, days=300):
    return DFCACHE.get(code)
wl.fetcher.fetch = patched

def ref_high_low(df):
    if len(df) < 5: return ("—","none")
    cur = df['close'].iloc[-1]; n=len(df)
    wins=[(99999,'历史新高'),(250,'近一年新高'),(120,'近半年新高'),(60,'近60天新高'),
          (30,'近30天新高'),(20,'近20天新高'),(10,'近10天新高'),(5,'近5天新高')]
    lows=[(250,'近一年新低'),(120,'近半年新低'),(60,'近60天新低'),(30,'近30天新低'),
          (20,'近20天新低'),(10,'近10天新低'),(5,'近5天新低')]
    for w,lab in wins:
        lb=min(w,n-1)
        if lb>=1:
            d=df['close'].iloc[:-1].tail(lb)
            if len(d)>0 and cur>d.max(): return (lab,"high")
    for w,lab in lows:
        lb=min(w,n-1)
        if lb>=1:
            d=df['close'].iloc[:-1].tail(lb)
            if len(d)>0 and cur<d.min(): return (lab,"low")
    return ("—","none")

def ref_nine_turn(df, unit="天"):
    closes=df['close'].values; n=len(closes)
    if n<5: return (None,0)
    counts=[0]*n; dirs=["none"]*n; cd="none"; cc=0
    for i in range(4,n):
        p4=closes[i-4]; c=closes[i]
        if c<p4:
            if cd=="down": cc+=1
            else: cd="down"; cc=1
        elif c>p4:
            if cd=="up": cc+=1
            else: cd="up"; cc=1
        else: cd="none"; cc=0
        if cc>9: cc=9
        counts[i]=cc; dirs[i]=cd
    bi=n-1
    for i in range(n-2, max(n-3,-1),-1):
        if counts[i]>counts[bi]: bi=i
    return (dirs[bi], counts[bi])

def compact_ref(direction, count, unit):
    if direction in (None,"none") or count==0: return f"{unit}–"
    arrow="▼" if direction=="down" else "▲"
    return f"{unit}{arrow}{count}"

total=0; passed=0
report=[]
for code in SYMS:
    name = WATCHLIST.get(code, code)
    df = DFCACHE.get(code)
    if df is None:
        report.append(f"[ERR] {code} 无数据"); continue
    st = get_stock_status(code, name)
    if st.error:
        report.append(f"[ERR] {code} status.error={st.error}"); continue

    closes = df['close'].values.astype(float)
    n=len(closes)
    last = float(closes[-1]); prev = float(closes[-2])
    last5 = float(closes[-6]) if n>=6 else float(closes[0])

    exp_price = round(last,2)
    exp_c1 = round((last-prev)/prev*100,2) if n>=2 else None
    exp_c5 = round((last-last5)/last5*100,2) if n>=6 else None

    dres = calc_nine_turn(df,"天"); mres = calc_nine_turn_monthly(df)
    dtext = _compact(dres,"日"); mtext = _compact(mres,"月")
    hl_text, hl_type = ref_high_low(df)

    checks=[]
    def chk(label, got, exp, tol=0.02):
        global total, passed
        total+=1
        if isinstance(exp,(int,float)) and isinstance(got,(int,float)):
            ok = abs(got-exp)<=tol
        else:
            ok = (got==exp)
        if ok: passed+=1
        checks.append(("✓" if ok else "✗", label, got, exp))

    chk("现价", st.price, exp_price)
    chk("日涨跌幅", st.change_1d, exp_c1)
    chk("近5日涨跌幅", st.change_5d, exp_c5)
    chk("九转日文本", st.nine_turn_daily, dtext)
    chk("九转月文本", st.nine_turn_monthly, mtext)
    chk("九转日状态", st.nine_turn_daily_state, _nt_state(dres))
    chk("九转月状态", st.nine_turn_monthly_state, _nt_state(mres))
    chk("新高新低", st.high_low, hl_text)

    valid_signals={"即将上涨关注","上涨见顶关注","下跌观望"}
    sig_ok = st.signal in valid_signals
    checks.append(("✓" if sig_ok else "✗","操盘建议有效", st.signal, "∈有效集"))
    total+=1; passed+=1 if sig_ok else 0

    # 策略+图表
    try:
        res = FujimotoStrategy(total_capital=100000).analyze(df)
        rd = result_to_dict(res)
        chart = df_to_chart_json(df, res)
        strat_ok = (res.trend.value in {"多头趋势","空头趋势","震荡"} and
                    res.signal.value in {"买入","卖出","持有","观望","加仓"} and
                    isinstance(rd.get("position_pct"),(int,float)) and
                    chart.get("count",0)>0 and len(chart.get("candles",[]))>0 and
                    len(rd.get("indicators",[]))>=4)
        checks.append(("✓" if strat_ok else "✗","策略+图表",
                       f"趋势={res.trend.value}/信号={res.signal.value}/K线={chart['count']}根/指标{len(rd['indicators'])}",
                       "有效"))
        total+=1; passed+=1 if strat_ok else 0
    except Exception as e:
        checks.append(("✗","策略+图表", f"异常:{e}", "有效")); total+=1

    report.append(f"\n=== {code} {name} | 行数={n} | 现价={st.price} 日={st.change_1d}% 5日={st.change_5d}% | 信号={st.signal} 九转={st.nine_turn_daily}|{st.nine_turn_monthly} 高低={st.high_low} ===")
    for m,lab,got,exp in checks:
        report.append(f"  {m} {lab}: got={got}  exp={exp}")

print("\n".join(report))
print(f"\n=== 总计: {passed}/{total} 项通过 ===")
print("RESULT:", "ALL_PASS" if passed==total else "HAS_FAIL")
wl.fetcher.fetch = orig_fetch
