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


def analyze_symbol(symbol: str) -> dict:
    name = watchlist.WATCHLIST.get(symbol, symbol)
    try:
        st = watchlist.get_stock_status(symbol, name)
        d = watchlist._status_to_dict(st)
        if d.get("error"):
            return {"symbol": symbol, "name": name, "error": d["error"]}
        d["bucket"] = _bucket(d)
        return d
    except Exception as e:
        return {"symbol": symbol, "name": name, "error": str(e)[:80]}


def classify_analyses(analyses: List[dict]) -> Dict[str, Any]:
    ups = [a for a in analyses if a.get("bucket") == "up"]
    downs = [a for a in analyses if a.get("bucket") == "down"]
    watches = [a for a in analyses if a.get("bucket") == "watch"]
    ups.sort(key=_priority, reverse=True)
    downs.sort(key=_priority, reverse=True)

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
    # 方向标的不足时，从自选中按近5日波动补足深写名额（仍不做观望一句话）
    if len(focus) < MIN_FOCUS:
        focus_codes = {a.get("code") for a in focus}
        extra = [a for a in analyses if a.get("code") not in focus_codes]
        extra.sort(key=lambda x: abs(float(x.get("change_5d") or 0)), reverse=True)
        for a in extra:
            if len(focus) >= MIN_FOCUS:
                break
            focus.append(a)

    return {
        "all": analyses,
        "focus": focus,
        "up_count": len(ups),
        "down_count": len(downs),
        "watch_count": len(watches),
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
    """邮件友好的彩色看板表（可横向滚动）。"""
    rows = []
    for a in analyses:
        code = a.get("code") or ""
        name = a.get("name") or ""
        px = a.get("price") if a.get("price") is not None else "—"
        chg1 = a.get("change_1d")
        chg5 = a.get("change_5d")
        timing = a.get("timing") or "—"
        trend = a.get("trend_filter") or a.get("trend") or "—"
        action = a.get("action") or a.get("signal") or "—"
        hl = a.get("high_low") or "—"
        val = a.get("valuation") or "—"
        rows.append(
            "<tr>"
            f"<td style='padding:8px 10px;border-bottom:1px solid {C_BORDER};color:{C_GOLD};font-weight:700;white-space:nowrap;'>{code}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid {C_BORDER};color:{C_TEXT};'>{name}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid {C_BORDER};text-align:right;color:{C_TEXT};white-space:nowrap;'>{px}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid {C_BORDER};text-align:right;color:{_color_for_chg(chg1)};font-weight:600;white-space:nowrap;'>{_pct(chg1)}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid {C_BORDER};text-align:right;color:{_color_for_chg(chg5)};font-weight:600;white-space:nowrap;'>{_pct(chg5)}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid {C_BORDER};color:{_color_for_timing(a)};font-weight:600;'>{timing}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid {C_BORDER};color:{_color_for_trend(a)};font-weight:600;white-space:nowrap;'>{trend}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid {C_BORDER};color:{_color_for_action(a)};font-weight:700;white-space:nowrap;'>{action}</td>"
            f"<td style='padding:8px 10px;border-bottom:1px solid {C_BORDER};color:{C_DIM};font-size:12px;'>{hl} · {val}</td>"
            "</tr>"
        )
    thead = (
        f"<tr style='color:{C_DIM};text-align:left;'>"
        f"<th style='padding:8px 10px;border-bottom:2px solid {C_BORDER};'>代码</th>"
        f"<th style='padding:8px 10px;border-bottom:2px solid {C_BORDER};'>名称</th>"
        f"<th style='padding:8px 10px;border-bottom:2px solid {C_BORDER};text-align:right;'>现价</th>"
        f"<th style='padding:8px 10px;border-bottom:2px solid {C_BORDER};text-align:right;'>日</th>"
        f"<th style='padding:8px 10px;border-bottom:2px solid {C_BORDER};text-align:right;'>近5日</th>"
        f"<th style='padding:8px 10px;border-bottom:2px solid {C_BORDER};'>九转时机</th>"
        f"<th style='padding:8px 10px;border-bottom:2px solid {C_BORDER};'>趋势过滤</th>"
        f"<th style='padding:8px 10px;border-bottom:2px solid {C_BORDER};'>建议动作</th>"
        f"<th style='padding:8px 10px;border-bottom:2px solid {C_BORDER};'>新高/估值</th>"
        "</tr>"
    )
    return (
        f"<div style='overflow-x:auto;-webkit-overflow-scrolling:touch;border:1px solid {C_BORDER};"
        f"border-radius:10px;background:{C_CARD};'>"
        f"<table style='border-collapse:collapse;width:100%;min-width:720px;font-size:13px;"
        f"font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;color:{C_TEXT};'>"
        f"<thead>{thead}</thead><tbody>{''.join(rows)}</tbody></table></div>"
    )


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
        "focus_count": len(focus),
        "up_count": classified["up_count"],
        "down_count": classified["down_count"],
        "watch_count": classified["watch_count"],
        "focus": focus,
    }
    data_json = json.dumps(payload, ensure_ascii=False, indent=2)

    system = (
        "你是一名客观的投研编辑，同时熟悉查理·芒格（Charlie Munger）的多学科检查清单思路。"
        "为中文读者写股票观察邮件正文。\n"
        "硬性规则：\n"
        "1) 只使用用户提供的 JSON 事实；严禁编造未给出的财务数字、新闻、管理层姓名或具体业务数据；"
        "信息不足时明确写「公开数据不足，仅作框架提示」。\n"
        "2) 禁止任何买卖建议与仓位建议；禁止：建议买入/卖出/加仓/减仓/建仓/清仓/推荐持有。\n"
        "3) 不要输出「其余标的一句话」或观望列表；看板全表已在邮件其他部分展示。\n"
        "4) 输出纯 HTML 片段（h2/h3/h4/p/ul/li/strong/table），不要 html/body 外壳，不要 Markdown 代码块。\n"
        "5) 文末必须附免责声明。\n"
        f"免责声明固定文案：{DISCLAIMER}\n\n"
        "深度分析结构（对 focus 中每一只）：\n"
        "A. 量化快照：用 JSON 中的价格、涨跌、九转、趋势、动作、估值/高低，2～4 句说清「现在系统在看见什么」。\n"
        "B. 芒格式质地推演（研究框架，非结论）：用检查清单语气讨论——"
        "①业务是否可能简单可理解；②是否可能有持续竞争优势的迹象（仅基于代码/名称/角色定位合理推断，并标明是推断）；"
        "③管理层与资本配置（若无数据则写未知）；④价格是否相对于「价值」有安全边际的讨论空间（结合 valuation 字段，勿发明 PE）；"
        "⑤主要风险与能力圈边界。每只 4～8 句，短句、有力、客观。\n"
    )

    user = (
        f"请为收件人 {email} 生成【{period_cn}】深度分析章节 HTML（不要重复输出整张看板表）。\n"
        f"结构：\n"
        f"<h2>重点标的深度分析</h2>\n"
        f"<p>本期方向偏多 {classified['up_count']} / 偏空 {classified['down_count']} / 观望 {classified['watch_count']}；"
        f"以下深写 {len(focus)} 只。</p>\n"
        f"对 focus 每一只：\n"
        f"<h3>代码 名称</h3>\n"
        f"<h4>量化快照</h4><p>...</p>\n"
        f"<h4>质地与思维框架（芒格式检查清单）</h4><p>或 ul...</p>\n"
        f"最后：<h3>免责声明</h3><p>{DISCLAIMER}</p>\n\n"
        f"数据 JSON：\n{data_json}"
    )
    return system, user


def _fallback_deep(a: dict) -> str:
    side = "上涨侧观察" if a.get("bucket") == "up" else (
        "下跌侧观察" if a.get("bucket") == "down" else "中性观察"
    )
    role = a.get("role") or "—"
    return (
        f"<h3 style='color:{C_GOLD};margin:18px 0 8px;'>{a.get('code')} {a.get('name') or ''}</h3>"
        f"<h4 style='color:{C_TEXT};margin:8px 0 4px;font-size:14px;'>量化快照 · {side}</h4>"
        f"<p style='color:{C_TEXT};line-height:1.65;margin:0 0 8px;font-size:13px;'>"
        f"现价 <strong>{a.get('price')}</strong>，日 {_pct(a.get('change_1d'))}，近5日 {_pct(a.get('change_5d'))}。"
        f"九转时机：<span style='color:{_color_for_timing(a)};'>{a.get('timing') or '—'}</span>；"
        f"趋势：<span style='color:{_color_for_trend(a)};'>{a.get('trend_filter') or a.get('trend') or '—'}</span>；"
        f"动作标签：<span style='color:{_color_for_action(a)};'>{a.get('action') or '—'}</span>"
        f"{('（' + a['action_reason'] + '）') if a.get('action_reason') else ''}。"
        f"新高/新低：{a.get('high_low') or '—'}；相对位置：{a.get('valuation') or '—'} {a.get('valuation_detail') or ''}。"
        f"组合定位标签：{role}。"
        f"</p>"
        f"<h4 style='color:{C_TEXT};margin:8px 0 4px;font-size:14px;'>质地与思维框架（芒格式检查清单）</h4>"
        f"<ul style='color:{C_TEXT};line-height:1.7;font-size:13px;margin:0;padding-left:18px;'>"
        f"<li><strong>可理解性</strong>：仅凭代码与名称无法替代完整业务说明；请结合你熟悉的行业与一手资料，确认是否落在能力圈内。</li>"
        f"<li><strong>优势与护城河</strong>：公开量化字段未提供护城河证据；避免把短期九转或趋势信号误当成商业模式优势。</li>"
        f"<li><strong>价格与安全边际</strong>：系统相对位置为「{a.get('valuation') or '—'}」"
        f"{('（' + str(a.get('valuation_detail')) + '）') if a.get('valuation_detail') else ''}；"
        f"这是规则化近似，不是内在价值评估。</li>"
        f"<li><strong>风险与反证</strong>：关注趋势与九转是否冲突、波动是否异常；任何单周信号都可能被后续数据证伪。</li>"
        f"</ul>"
    )


def _wrap_email(period_cn: str, email: str, classified: dict, deep_html: str) -> str:
    table = _build_board_table(classified.get("all") or [])
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autopilot {period_cn}</title></head>
<body style="margin:0;padding:0;background:{C_BG};color:{C_TEXT};">
<div style="max-width:900px;margin:0 auto;padding:20px 14px;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;">
  <h1 style="color:{C_GOLD};font-size:20px;margin:0 0 6px;">Autopilot 股票观察{period_cn}</h1>
  <p style="color:{C_DIM};font-size:12px;margin:0 0 16px;">
    {email} · {datetime.now():%Y-%m-%d %H:%M}
    · 偏多 {classified.get('up_count', 0)} / 偏空 {classified.get('down_count', 0)} / 观望 {classified.get('watch_count', 0)}
  </p>

  <h2 style="color:{C_TEXT};font-size:16px;margin:0 0 10px;">一、自选看板</h2>
  <p style="color:{C_DIM};font-size:12px;margin:0 0 8px;">可左右滑动查看完整表格；颜色含义与网页看板一致。</p>
  {table}

  <div style="margin-top:28px;">
    <h2 style="color:{C_TEXT};font-size:16px;margin:0 0 10px;">二、重点标的深度分析</h2>
    {deep_html}
  </div>

  <p style="color:{C_DIM};font-size:11px;margin:28px 0 0;line-height:1.6;border-top:1px solid {C_BORDER};padding-top:12px;">
    {DISCLAIMER}
  </p>
</div>
</body></html>"""


def _fallback_html(period: str, email: str, classified: dict) -> str:
    period_cn = "周报" if period == "weekly" else "月报"
    if not classified.get("focus"):
        deep = f"<p style='color:{C_DIM};'>本期无特别方向共振标的；上表为完整自选快照。</p>"
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
