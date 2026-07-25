"""
AI 周报 / 月报流水线
====================
定时（每周末）为每位用户生成一份关注股票的分析总结邮件：
  - 有自选 → 分析其自选股；无自选 → 回退到「未登录默认看板」股票列表。
  - 基于自选看板指标数据（藤本茂策略信号 / 神奇九转 / 估值 / 高低），由 AI 生成总结。
  - 周报：仅陈述事实，**不给买卖建议**。
  - 月报：含当下趋势、机会与风险、操作建议（投资建议的回测/策略占比预测留待后续）。
  - AI 不可用时降级为结构化纯文本 HTML，绝不中断主流程。

对外：
  - run_reports(period)          遍历全部用户生成并发送，返回 {sent, skipped}
  - generate_for_user(uid,email,period)  单用户生成并发送（供后台手动触发 / 测试）
  - build_report_html(uid,email,period)  仅生成 HTML（不发送）
"""

import os
import json
import logging
from datetime import datetime

import ai_client
import mailer
import user_store
import watchlist_store
import watchlist
from data_fetcher import DataFetcher
from strategy_engine import FujimotoStrategy
from nine_turn import calc_nine_turn_display

logger = logging.getLogger("reports")

fetcher = DataFetcher()

# 每次报告最多分析的标的数量（避免单封邮件过长 / 调用超限）
MAX_SYMBOLS = int(os.getenv("REPORT_MAX_SYMBOLS", "15"))


def get_target_symbols(uid: str) -> list:
    """用户的自选股；无则回退默认看板列表（未登录看板）。"""
    items = []
    if uid:
        try:
            items = watchlist_store.get_all(uid)
        except Exception:
            items = []
    if items:
        return [i["symbol"] for i in items][:MAX_SYMBOLS]
    # 默认看板：watchlist.WATCHLIST 为 {symbol: name}
    return list(watchlist.WATCHLIST.keys())[:MAX_SYMBOLS]


def analyze_symbol(symbol: str, days: int = 120) -> dict:
    """拉取行情并算出核心量化指标，返回结构化 dict（喂给 AI / 降级 HTML）。"""
    try:
        df = fetcher.fetch(symbol, days)
    except Exception as e:
        return {"symbol": symbol, "error": f"行情获取失败: {e}"}
    if df is None or len(df) < 5:
        return {"symbol": symbol, "error": "数据不足"}
    try:
        strat = FujimotoStrategy(total_capital=100000)
        result = strat.analyze(df)
        nine = calc_nine_turn_display(df)
        last = float(df["close"].iloc[-1])
        prev = float(df["close"].iloc[-2]) if len(df) >= 2 else last
        chg_1d = (last - prev) / prev * 100 if prev else 0.0
        name = watchlist.WATCHLIST.get(symbol, "")
        rec = {
            "symbol": symbol,
            "name": name,
            "last_close": round(last, 2),
            "change_1d_pct": round(chg_1d, 2),
            "signal": getattr(result.signal, "value", str(result.signal)),
            "trend": getattr(result.trend, "value", str(result.trend)),
            "action": getattr(result, "action", ""),
            "position_pct": round(getattr(result, "position_pct", 0) * 100, 2),
            "nine_turn_daily": nine.get("daily", ""),
            "nine_turn_monthly": nine.get("monthly", ""),
            "indicators": [
                {"name": ind.name, "value": round(float(ind.value), 4),
                 "signal": ind.signal, "detail": ind.detail}
                for ind in getattr(result, "indicators", [])
            ],
            "rows": int(len(df)),
            "start": str(df["date"].iloc[0].date()) if "date" in df.columns else "",
            "end": str(df["date"].iloc[-1].date()) if "date" in df.columns else "",
        }
        return rec
    except Exception as e:
        return {"symbol": symbol, "error": f"分析失败: {e}"}


def _build_prompts(period: str, email: str, analyses: list) -> tuple:
    period_cn = "周报" if period == "weekly" else "月报"
    data_json = json.dumps(analyses, ensure_ascii=False, indent=2)

    system = (
        "你是一名严谨的量化分析助手，为中文投资者撰写股票观察周报/月报。"
        "必须只基于用户提供的量化指标与价格数据进行分析，严禁编造未提供的行业新闻、"
        "公司事件或具体数字；若需要实时资讯可明确注明「需结合实时资讯」。\n"
        "输出要求：直接返回可用于邮件正文的 HTML 片段（使用 <h2>/<h3>/<p>/<ul>/<li>/<strong> 等，"
        "不要包含 <html>/<head>/<body> 外壳，不要使用 Markdown 代码块）。语言：简体中文。"
    )

    if period == "weekly":
        system += (
            "\n本报告为【周报】，规则：只陈述事实（价格、信号、指标、趋势），"
            "不得给出任何买卖或操作建议，不得出现'建议买入/卖出/加仓/减仓'等措辞。"
        )
        user = (
            f"请为邮箱 {email} 生成一期股票观察【周报】。\n"
            f"以下是各标的的最新量化指标数据（JSON）：\n{data_json}\n\n"
            "请逐标的用 <h3> 给出代码与名称，再用 <ul>/<li> 列出事实要点"
            "（收盘价、单日涨跌、策略信号、神奇九转状态、关键指标）。"
            "最后用 <h3> 写一段「本周市场观察」仅描述整体趋势事实。不要给建议。"
        )
    else:
        system += (
            "\n本报告为【月报】，可在事实基础上给出当下趋势、机会与风险、以及操作建议"
            "（如仓位参考、关注价位），但建议须明确标注为「参考」而非确定性指令。"
        )
        user = (
            f"请为邮箱 {email} 生成一期股票观察【月报】。\n"
            f"以下是各标的的最新量化指标数据（JSON）：\n{data_json}\n\n"
            "请逐标的用 <h3> 给出代码与名称，再用 <ul>/<li> 列出核心事实"
            "（收盘价、单日/区间涨跌、策略信号、神奇九转、估值）。\n"
            "随后用 <h3> 写「趋势与机会/风险」与「操作参考」（明确标注为参考）。"
        )
    return system, user


def _fallback_html(period: str, email: str, analyses: list) -> str:
    """AI 不可用时的降级 HTML（结构化、事实为主）。"""
    period_cn = "周报" if period == "weekly" else "月报"
    today = datetime.now().strftime("%Y-%m-%d")
    parts = [f"<h2>Autopilot 股票观察{period_cn}（{today}）</h2>",
             f"<p>邮箱：{email}（本期为指标数据自动汇总，未使用 AI 润色）</p>"]
    for a in analyses:
        if a.get("error"):
            parts.append(f"<h3>{a['symbol']}</h3><p style='color:#c0392b'>{a['error']}</p>")
            continue
        parts.append(f"<h3>{a['symbol']} {a.get('name','')}</h3>")
        parts.append("<ul>")
        parts.append(f"<li>收盘价：{a['last_close']}（单日 {a['change_1d_pct']:+}%）</li>")
        parts.append(f"<li>策略信号：{a['signal']}（{a['trend']}，动作：{a['action']}）</li>")
        parts.append(f"<li>神奇九转：日 {a['nine_turn_daily']} / 月 {a['nine_turn_monthly']}</li>")
        for ind in a.get("indicators", []):
            parts.append(f"<li>{ind['name']}：{ind['value']}（{ind['signal']}）{ind.get('detail','')}</li>")
        parts.append("</ul>")
    parts.append("<p style='color:#7f8c8d;font-size:12px;'>数据区间："
                 f"{analyses[0].get('start','')} ~ {analyses[0].get('end','')}。"
                 "本报告仅基于量化指标，不构成投资建议。</p>")
    return "\n".join(parts)


def build_report_html(uid: str, email: str, period: str = "weekly") -> "str | None":
    """生成报告 HTML；无有效数据返回 None。"""
    symbols = get_target_symbols(uid)
    if not symbols:
        return None
    analyses = [analyze_symbol(s) for s in symbols]
    analyses = [a for a in analyses if not a.get("error")]
    if not analyses:
        return None

    if ai_client.available():
        try:
            system, user = _build_prompts(period, email, analyses)
            html = ai_client.chat(system, user)
            if html and html.strip():
                return html
        except Exception as e:
            logger.warning("AI 生成失败，降级为结构化 HTML: %s", e)
    return _fallback_html(period, email, analyses)


def generate_for_user(uid: str, email: str, period: str = "weekly") -> bool:
    """为单用户生成并发送报告邮件。成功返回 True。"""
    if not email:
        return False
    html = build_report_html(uid, email, period)
    if not html:
        return False
    period_cn = "周报" if period == "weekly" else "月报"
    subject = f"Autopilot 股票观察{period_cn} · {datetime.now():%Y-%m-%d}"
    try:
        return mailer.send_email(email, subject, html, html=html)
    except Exception as e:
        logger.warning("发送报告邮件失败 %s: %s", email, e)
        return False


def run_reports(period: str = "weekly") -> dict:
    """遍历全部用户生成并发送报告。返回 {sent, skipped}。"""
    users, total = user_store.list_profiles(limit=10000, offset=0)
    sent = skipped = 0
    for u in users:
        email = (u.get("email") or "").strip()
        if not email:
            skipped += 1
            continue
        try:
            if generate_for_user(u.get("id"), email, period):
                sent += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning("报告生成失败 %s: %s", email, e)
            skipped += 1
    logger.info("报告任务完成 period=%s sent=%d skipped=%d total=%d", period, sent, skipped, total)
    return {"sent": sent, "skipped": skipped, "total": total}


if __name__ == "__main__":
    import sys
    _period = sys.argv[1] if len(sys.argv) > 1 else "weekly"
    print(json.dumps(run_reports(_period), ensure_ascii=False))
