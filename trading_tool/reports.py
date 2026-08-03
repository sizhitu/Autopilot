"""
AI 观察报告流水线（周报 / 月报）
================================
定时为「开启看板邮件推送」的用户生成观察邮件：
  - 有自选 → 分析其自选；无自选 → 回退默认看板。
  - 邮件含：彩色看板表格（类似网页）+ 重点标的深度分析（量化事实 + 芒格式质地推演）。
  - 不再附「观望一句话」列表，避免冗长刷屏。
  - **不给买卖建议**；文末固定免责声明。
  - AI 不可用时降级为结构化 HTML。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import List, Dict, Any

import ai_client
import mailer
import user_store
import watchlist_store
import watchlist

logger = logging.getLogger("reports")

MAX_SYMBOLS = int(os.getenv("REPORT_MAX_SYMBOLS", "30"))
MAX_FOCUS = int(os.getenv("REPORT_MAX_FOCUS", "5"))
MIN_FOCUS = int(os.getenv("REPORT_MIN_FOCUS", "3"))

# 邮件配色（与网页看板接近，内联样式兼容多数客户端）
C_BG = "#0f1218"
C_CARD = "#1a1f2b"
C_BORDER = "#2a3344"
C_TEXT = "#e8eaed"
C_DIM = "#8b95a8"
C_GREEN = "#2ecc71"
C_RED = "#e74c3c"
C_ORANGE = "#e67e22"
C_GOLD = "#d4af37"
C_BLUE = "#5b9fd4"


def get_target_symbols(uid: str) -> list:
    items = []
    if uid:
        try:
            items = watchlist_store.get_all(uid)
        except Exception:
            items = []
    if items:
        return [i["symbol"] for i in items][:MAX_SYMBOLS]
    return list(watchlist.WATCHLIST.keys())[:MAX_SYMBOLS]


def _bucket(st: dict) -> str:
    action = (st.get("action") or "").strip()
    signal = (st.get("signal") or "").strip()
    timing = (st.get("timing") or "").strip()
    if action in ("关注买入", "阶梯抄底关注", "策略偏多") or signal == "即将上涨关注":
        return "up"
    if "买点" in timing or "下跌九转" in timing:
        return "up"
    if action in ("关注卖出", "阶梯止盈关注", "策略偏空", "减仓观察") or signal == "上涨见顶关注":
        return "down"
    if "卖点" in timing or "上涨九转" in timing:
        return "down"
    return "watch"


def _priority(st: dict) -> tuple:
    timing = st.get("timing") or ""
    complete = 2 if "完成" in timing else (1 if "临近" in timing else 0)
    try:
        chg = abs(float(st.get("change_5d") or 0))
    except Exception:
        chg = 0.0
    return (complete, chg)


_fetcher = None

def _get_fetcher():
    global _fetcher
    if _fetcher is None:
        from data_fetcher import DataFetcher
        _fetcher = DataFetcher()
    return _fetcher


def analyze_symbol(symbol: str) -> dict:
    name = watchlist.WATCHLIST.get(symbol, symbol)
    try:
        st = watchlist.get_stock_status(symbol, name)
        d = watchlist._status_to_dict(st)
        if d.get("error"):
            return {"symbol": symbol, "name": name, "error": d["error"]}
        d["bucket"] = _bucket(d)
        try:
            f = _get_fetcher()
            d["industry"] = f.fetch_industry(symbol) or ""
            d["business_summary"] = f.fetch_profile(symbol) or ""
        except Exception:
            d["industry"] = ""
            d["business_summary"] = ""
        return d
    except Exception as e:
        return {"symbol": symbol, "name": name, "error": str(e)[:80]}


def classify_analyses(analyses: List[dict]) -> Dict[str, Any]:
    """邮件只关心方向明确的标的：观望一律不进入邮件正文。"""
    ups = [a for a in analyses if a.get("bucket") == "up"]
    downs = [a for a in analyses if a.get("bucket") == "down"]
    watches = [a for a in analyses if a.get("bucket") == "watch"]
    ups.sort(key=_priority, reverse=True)
    downs.sort(key=_priority, reverse=True)

    # 看板区：仅上涨侧 + 下跌侧（按优先级）
    board = ups + downs

    focus: List[dict] = []
    take_up = min(2, len(ups))
    take_down = min(2, len(downs))
    focus.extend(ups[:take_up])
    focus.extend(downs[:take_down])
    rest_pool = ups[take_up:] + downs[take_down:]
    rest_pool.sort(key=_priority, reverse=True)
    for a in rest_pool:
        if len(focus) >= MAX_FOCUS:
            break
        focus.append(a)
    # 不把观望凑进深写

    return {
        "board": board,
        "focus": focus,
        "up_count": len(ups),
        "down_count": len(downs),
        "watch_count": len(watches),  # 仅统计，不展示列表
    }


def _pct(v) -> str:
    if isinstance(v, (int, float)):
        return f"{v:+.2f}%"
    return "—"


def _color_for_action(a: dict) -> str:
    c = (a.get("action_color") or a.get("signal_color") or "gray").lower()
    return {
        "red": C_RED, "orange": C_ORANGE, "green": C_GREEN,
        "blue": C_BLUE, "gold": C_GOLD,
    }.get(c, C_DIM)


def _color_for_chg(v) -> str:
    if not isinstance(v, (int, float)):
        return C_DIM
    if v > 0:
        return C_GREEN
    if v < 0:
        return C_RED
    return C_DIM


def _color_for_timing(a: dict) -> str:
    c = (a.get("timing_color") or "").lower()
    return {
        "red": C_RED, "orange": C_ORANGE, "green": C_GREEN,
    }.get(c, C_DIM)


def _color_for_trend(a: dict) -> str:
    c = (a.get("trend_filter_color") or "").lower()
    t = (a.get("trend_filter") or a.get("trend") or "")
    if c in ("green", "red", "orange"):
        return {"green": C_GREEN, "red": C_RED, "orange": C_ORANGE}[c]
    if "多头" in t:
        return C_GREEN
    if "空头" in t:
        return C_RED
    return C_DIM


def _build_board_table(analyses: List[dict]) -> str:
    """仅即将上涨/下跌：竖向卡片，无横向滚动。"""
    if not analyses:
        return (
            f"<p style='color:{C_DIM};font-size:13px;line-height:1.6;'>"
            f"本期自选中<strong>没有</strong>系统标为「即将上涨 / 即将下跌」的标的，"
            f"观望类已省略，以减少干扰。可到网页看板查看完整列表。</p>"
        )

    ups = [a for a in analyses if a.get("bucket") == "up"]
    downs = [a for a in analyses if a.get("bucket") == "down"]
    parts = []

    def _card(a: dict, side_label: str, side_color: str) -> str:
        code = a.get("code") or ""
        name = a.get("name") or ""
        px = a.get("price") if a.get("price") is not None else "—"
        chg1, chg5 = a.get("change_1d"), a.get("change_5d")
        timing = a.get("timing") or "—"
        trend = a.get("trend_filter") or a.get("trend") or "—"
        action = a.get("action") or a.get("signal") or "—"
        return (
            f"<div style='border-left:4px solid {side_color};border:1px solid {C_BORDER};"
            f"border-left:4px solid {side_color};border-radius:8px;background:{C_CARD};"
            f"padding:10px 12px;margin:0 0 8px;'>"
            f"<div style='margin-bottom:6px;'>"
            f"<span style='background:{side_color};color:#0f1218;font-size:11px;font-weight:700;"
            f"padding:2px 8px;border-radius:4px;margin-right:8px;'>{side_label}</span>"
            f"<span style='color:{C_GOLD};font-weight:700;font-size:15px;'>{code}</span>"
            f"<span style='color:{C_TEXT};font-size:13px;margin-left:6px;'>{name}</span>"
            f"</div>"
            f"<div style='font-size:12px;line-height:1.65;color:{C_TEXT};'>"
            f"现价 <strong>{px}</strong>"
            f" · 日 <span style='color:{_color_for_chg(chg1)};font-weight:700;'>{_pct(chg1)}</span>"
            f" · 近5日 <span style='color:{_color_for_chg(chg5)};font-weight:700;'>{_pct(chg5)}</span>"
            f"<br>九转 <span style='color:{_color_for_timing(a)};font-weight:700;'>{timing}</span>"
            f" · 趋势 <span style='color:{_color_for_trend(a)};font-weight:700;'>{trend}</span>"
            f"<br>动作 <span style='color:{_color_for_action(a)};font-weight:700;'>{action}</span>"
            f"</div></div>"
        )

    if ups:
        parts.append(f"<p style='color:{C_GREEN};font-weight:700;font-size:13px;margin:12px 0 6px;'>▲ 即将上涨关注（{len(ups)}）</p>")
        for a in ups:
            parts.append(_card(a, "上涨侧", C_GREEN))
    if downs:
        parts.append(f"<p style='color:{C_RED};font-weight:700;font-size:13px;margin:12px 0 6px;'>▼ 即将下跌关注（{len(downs)}）</p>")
        for a in downs:
            parts.append(_card(a, "下跌侧", C_RED))
    return "".join(parts)


def _focus_facts(a: dict) -> dict:
    return {
        "code": a.get("code"),
        "name": a.get("name"),
        "price": a.get("price"),
        "change_1d": a.get("change_1d"),
        "change_5d": a.get("change_5d"),
        "timing": a.get("timing"),
        "trend_filter": a.get("trend_filter") or a.get("trend"),
        "action": a.get("action"),
        "action_reason": a.get("action_reason"),
        "nine_turn_daily": a.get("nine_turn_daily"),
        "nine_turn_monthly": a.get("nine_turn_monthly"),
        "high_low": a.get("high_low"),
        "valuation": a.get("valuation"),
        "valuation_detail": a.get("valuation_detail"),
        "role": a.get("role"),
        "bucket": a.get("bucket"),
        "industry": a.get("industry") or "",
        "business_summary": a.get("business_summary") or "",
    }


DISCLAIMER = (
    "免责声明：本邮件仅基于量化规则与公开行情数据的客观描述与思维框架推演，"
    "供研究与信息参考，不构成任何投资建议，不承诺收益。"
    "「公司质地」部分为查理·芒格式检查清单思路的结构化讨论，"
    "在缺乏完整财报与一手调研时结论有限，请独立验证。市场有风险，决策需独立判断。"
)


def _build_prompts(period: str, email: str, classified: dict) -> tuple:
    period_cn = "周报" if period == "weekly" else "月报"
    focus = [_focus_facts(a) for a in classified["focus"]]
    payload = {
        "period": period_cn,
        "focus": focus,
        "up_count": classified["up_count"],
        "down_count": classified["down_count"],
    }
    data_json = json.dumps(payload, ensure_ascii=False, indent=2)

    system = (
        "你是简洁的投研编辑。输出中文 HTML 片段，帮助用户少思考、抓住重点。\n"
        "规则：\n"
        "1) 只根据 JSON 写，禁止编造财务数字；禁止买卖建议。\n"
        "2) 每只标的最多 4 条短要点（ul/li），总长控制；不要长段落、不要五六节小标题。\n"
        "3) 每条要点必须具体：业务一句话 + 价格位置含义 + 信号含义 + 一条证伪条件。\n"
        "4) 用 <strong> 标出代码、关键数字与结论词；不要空泛套话。\n"
        "5) 不要写观望列表，不要重复看板卡片已有字段堆砌。\n"
    )
    user = (
        f"为 {email} 写【{period_cn}】重点速读（仅 focus）。\n"
        f"格式：每只 <h3 style='color:#d4af37'>代码 名称</h3> 后跟 <ul> 最多 4 条 <li>。\n"
        f"上涨 {classified['up_count']} / 下跌 {classified['down_count']}。\n"
        f"JSON：\n{data_json}"
    )
    return system, user


def _role_hint(role: str) -> str:
    r = role or ""
    mapping = {
        "压舱石": "组合中偏稳健、波动期望较低的底仓型工具/标的，更看重可理解性与回撤特征，而非短期弹性。",
        "高赔率": "组合中偏进攻、上涨弹性更大的仓位，成败更取决于行业景气与竞争格局，容错空间通常更小。",
        "周期弹性": "与经济或行业周期联动较强，判断重点在供需与价格周期位置，而非线性成长故事。",
        "卫星仓": "卫星/主题型暴露，权重宜有限；叙事变化快，更依赖纪律与证伪条件。",
    }
    for k, v in mapping.items():
        if k in r:
            return v
    return "未标注明确组合定位时，先按「能否讲清如何赚钱」再谈仓位角色。"


def _fallback_deep(a: dict) -> str:
    """短要点：业务一句 + 位置 + 信号 + 证伪。彩色强调。"""
    code = a.get("code") or ""
    name = a.get("name") or ""
    industry = (a.get("industry") or "").strip() or "相关行业"
    biz = (a.get("business_summary") or "").strip()
    val = a.get("valuation") or "—"
    val_d = a.get("valuation_detail") or ""
    timing = a.get("timing") or "—"
    trend = a.get("trend_filter") or a.get("trend") or "—"
    action = a.get("action") or "—"
    side = "上涨侧" if a.get("bucket") == "up" else "下跌侧"
    side_c = C_GREEN if a.get("bucket") == "up" else C_RED

    biz_one = biz if biz else f"主营信息有限，按「{industry}」框架理解。"
    if len(biz_one) > 90:
        biz_one = biz_one[:90] + "…"

    return (
        f"<h3 style='color:{C_GOLD};font-size:15px;margin:16px 0 6px;'>{code} {name} "
        f"<span style='color:{side_c};font-size:12px;font-weight:700;'>· {side}</span></h3>"
        f"<ul style='color:{C_TEXT};font-size:13px;line-height:1.65;margin:0;padding-left:18px;'>"
        f"<li><strong style='color:{C_BLUE};'>业务</strong>：{biz_one}</li>"
        f"<li><strong style='color:{C_ORANGE};'>位置</strong>：相对位置 "
        f"<span style='color:{C_GOLD};font-weight:700;'>{val}</span>"
        f"{('（' + val_d + '）') if val_d else ''}；近5日 "
        f"<span style='color:{_color_for_chg(a.get('change_5d'))};font-weight:700;'>{_pct(a.get('change_5d'))}</span>"
        f"</li>"
        f"<li><strong style='color:{side_c};'>信号</strong>：九转 "
        f"<span style='color:{_color_for_timing(a)};font-weight:700;'>{timing}</span>，趋势 "
        f"<span style='color:{_color_for_trend(a)};font-weight:700;'>{trend}</span>，动作 "
        f"<span style='color:{_color_for_action(a)};font-weight:700;'>{action}</span></li>"
        f"<li><strong style='color:{C_DIM};'>证伪</strong>：若趋势与九转持续背离，或行业叙事被公开数据否定，降低对该方向标签的权重。</li>"
        f"</ul>"
    )


def _wrap_email(period_cn: str, email: str, classified: dict, deep_html: str) -> str:
    table = _build_board_table(classified.get("board") or [])
    w = classified.get("watch_count") or 0
    watch_note = (
        f"另有 {w} 只观望未列入本邮件（请到网页看板查看）。"
        if w else "本期无额外观望省略项。"
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autopilot {period_cn}</title></head>
<body style="margin:0;padding:0;background:{C_BG};color:{C_TEXT};">
<div style="max-width:640px;margin:0 auto;padding:18px 12px;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;">
  <h1 style="color:{C_GOLD};font-size:18px;margin:0 0 6px;">Autopilot 方向速读 · {period_cn}</h1>
  <p style="color:{C_DIM};font-size:12px;margin:0 0 14px;line-height:1.5;">
    {datetime.now():%Y-%m-%d %H:%M}
    · <span style="color:{C_GREEN};font-weight:700;">上涨侧 {classified.get('up_count', 0)}</span>
    · <span style="color:{C_RED};font-weight:700;">下跌侧 {classified.get('down_count', 0)}</span>
    <br>{watch_note}
  </p>

  <h2 style="color:{C_TEXT};font-size:15px;margin:0 0 8px;">一、只需盯的方向</h2>
  <p style="color:{C_DIM};font-size:11px;margin:0 0 8px;">只列即将上涨 / 即将下跌；纵向阅读，无需左右滑。</p>
  {table}

  <div style="margin-top:22px;">
    <h2 style="color:{C_TEXT};font-size:15px;margin:0 0 8px;">二、重点速读</h2>
    <p style="color:{C_DIM};font-size:11px;margin:0 0 6px;">每只最多几条要点，彩色标注关键信息。</p>
    {deep_html}
  </div>

  <p style="color:{C_DIM};font-size:11px;margin:22px 0 0;line-height:1.55;border-top:1px solid {C_BORDER};padding-top:10px;">
    {DISCLAIMER}
  </p>
</div>
</body></html>"""


def _fallback_html(period: str, email: str, classified: dict) -> str:
    period_cn = "周报" if period == "weekly" else "月报"
    if not classified.get("focus"):
        deep = f"<p style='color:{C_DIM};'>本期无明确上涨/下跌标签标的，故无重点展开。</p>"
    else:
        deep = "\n".join(_fallback_deep(a) for a in classified["focus"])
    return _wrap_email(period_cn, email, classified, deep)


def build_report_html(uid: str, email: str, period: str = "weekly") -> "str | None":
    symbols = get_target_symbols(uid)
    if not symbols:
        return None
    analyses = []
    for s in symbols:
        a = analyze_symbol(s)
        if not a.get("error"):
            analyses.append(a)
    if not analyses:
        return None
    classified = classify_analyses(analyses)
    period_cn = "周报" if period == "weekly" else "月报"

    deep_html = ""
    if ai_client.available():
        try:
            system, user = _build_prompts(period, email, classified)
            deep_html = ai_client.chat(system, user, max_tokens=3500, temperature=0.3)
            if deep_html:
                deep_html = deep_html.strip()
                if deep_html.startswith("```"):
                    deep_html = deep_html.strip("`")
                    if deep_html.startswith("html"):
                        deep_html = deep_html[4:].lstrip()
        except Exception as e:
            logger.warning("AI 生成失败，降级: %s", e)
            deep_html = ""

    if not deep_html:
        return _fallback_html(period, email, classified)

    # AI 只负责深度分析段；看板表由模板保证彩色与可滚动
    if "免责" not in deep_html and "不构成" not in deep_html:
        deep_html += f"<h3>免责声明</h3><p style='color:{C_DIM};font-size:12px;'>{DISCLAIMER}</p>"
    return _wrap_email(period_cn, email, classified, deep_html)


def _resolve_uid_by_email(email: str) -> str:
    """根据邮箱从 profiles 反查 uid（测试单发时用）。"""
    email = (email or "").strip().lower()
    if not email:
        return ""
    try:
        rows, _ = user_store.list_profiles(limit=10000, offset=0)
        for r in rows:
            if (r.get("email") or "").strip().lower() == email:
                return r.get("id") or ""
    except Exception as e:
        logger.warning("按邮箱查 uid 失败: %s", e)
    return ""


def generate_for_user(uid: str, email: str, period: str = "weekly") -> bool:
    if not email:
        return False
    if not uid:
        uid = _resolve_uid_by_email(email)
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


def _due_for_send(prefs_freq: str, last_sent: str, period: str) -> bool:
    from datetime import datetime
    if not last_sent:
        return True
    try:
        last = datetime.fromisoformat(str(last_sent).replace("Z", ""))
    except Exception:
        return True
    days = (datetime.now() - last).days
    need = 12 if (prefs_freq == "biweekly") else 6
    return days >= need


def run_reports(period: str = "weekly", force: bool = False) -> dict:
    """force=True 时忽略 last_sent 节流（仅用于测试新模板）。"""
    from datetime import datetime
    subs = user_store.list_digest_subscribers()
    sent = skipped = 0
    total = len(subs)
    for u in subs:
        email = u.get("email")
        uid = u.get("id")
        freq = u.get("freq") or "weekly"
        if period != "monthly" and not force:
            if not _due_for_send(freq, u.get("last_sent"), period):
                skipped += 1
                continue
        try:
            if generate_for_user(uid, email, "weekly" if period != "monthly" else period):
                user_store.set_digest_prefs(uid, last_sent=datetime.now().isoformat(timespec="seconds"))
                sent += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning("报告生成失败 %s: %s", email, e)
            skipped += 1
    logger.info(
        "报告任务完成 period=%s force=%s sent=%d skipped=%d subscribers=%d",
        period, force, sent, skipped, total,
    )
    return {"sent": sent, "skipped": skipped, "total": total, "subscribers": total, "force": force}


if __name__ == "__main__":
    import sys
    _period = sys.argv[1] if len(sys.argv) > 1 else "weekly"
    print(json.dumps(run_reports(_period), ensure_ascii=False))
