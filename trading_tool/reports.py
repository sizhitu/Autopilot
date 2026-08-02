"""
AI 观察报告流水线（周报 / 月报）
================================
定时为用户生成关注股票的分析邮件：
  - 有自选 → 分析其自选；无自选 → 回退默认看板列表。
  - 只深写「即将上涨 / 即将下跌」方向明确的 3～5 只重点标的。
  - 其余观望标的一句话概括。
  - 语气：简洁、有力、客观；**不给任何买卖建议**；文末固定免责声明。
  - AI 不可用时降级为结构化 HTML，不中断主流程。

对外：
  - run_reports(period)
  - generate_for_user(uid, email, period)
  - build_report_html(uid, email, period)
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

# 扫描上限 / 重点深写上限
MAX_SYMBOLS = int(os.getenv("REPORT_MAX_SYMBOLS", "30"))
MAX_FOCUS = int(os.getenv("REPORT_MAX_FOCUS", "5"))
MIN_FOCUS = int(os.getenv("REPORT_MIN_FOCUS", "3"))


def get_target_symbols(uid: str) -> list:
    """用户自选；无则回退默认看板。"""
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
    """按看板结构化字段归类：up / down / watch。"""
    action = (st.get("action") or "").strip()
    signal = (st.get("signal") or "").strip()
    timing = (st.get("timing") or "").strip()
    # 上涨侧
    if action in ("关注买入", "阶梯抄底关注", "策略偏多") or signal == "即将上涨关注":
        return "up"
    if "买点" in timing or "下跌九转" in timing:
        return "up"
    # 下跌侧
    if action in ("关注卖出", "阶梯止盈关注", "策略偏空", "减仓观察") or signal == "上涨见顶关注":
        return "down"
    if "卖点" in timing or "上涨九转" in timing:
        return "down"
    return "watch"


def _priority(st: dict) -> tuple:
    """重点排序：完成 > 临近 > 阶梯/策略；同档按 |近5日| 幅度。"""
    timing = st.get("timing") or ""
    complete = 2 if "完成" in timing else (1 if "临近" in timing else 0)
    try:
        chg = abs(float(st.get("change_5d") or 0))
    except Exception:
        chg = 0.0
    return (complete, chg)


def analyze_symbol(symbol: str) -> dict:
    """复用看板 get_stock_status，输出报告用精简字段。"""
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
    """拆成 focus(up+down 共 3～5 只) 与 watch 一句话列表。"""
    ups = [a for a in analyses if a.get("bucket") == "up"]
    downs = [a for a in analyses if a.get("bucket") == "down"]
    watches = [a for a in analyses if a.get("bucket") == "watch"]

    ups.sort(key=_priority, reverse=True)
    downs.sort(key=_priority, reverse=True)

    focus: List[dict] = []
    # 尽量涨跌都覆盖，总数不超过 MAX_FOCUS
    # 优先各取 2，再按优先级补足到 MIN_FOCUS～MAX_FOCUS
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
    # 若方向标的不足 MIN_FOCUS，不硬凑观望进深写

    # 未进 focus 的方向标的也改为一句话观望区，避免遗漏
    focus_codes = {a["code"] for a in focus}
    one_liners = []
    for a in ups[take_up:] + downs[take_down:] + watches:
        if a["code"] in focus_codes:
            continue
        one_liners.append(a)

    return {
        "focus": focus,
        "watch": one_liners,
        "up_count": len(ups),
        "down_count": len(downs),
        "watch_count": len(watches),
    }


def _one_line(a: dict) -> str:
    """观望/非重点：一句话。"""
    code = a.get("code") or a.get("symbol")
    name = a.get("name") or ""
    px = a.get("price")
    chg = a.get("change_5d")
    trend = a.get("trend_filter") or a.get("trend") or ""
    timing = a.get("timing") or "无明确九转"
    action = a.get("action") or a.get("signal") or "观望"
    chg_s = f"{chg:+.1f}%" if isinstance(chg, (int, float)) else "—"
    return f"{code} {name}：近5日 {chg_s}，{trend}，{timing}，动作标签「{action}」。"


def _focus_facts(a: dict) -> dict:
    """喂给 AI 的重点标的事实卡片（禁止编造）。"""
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
        "bucket": a.get("bucket"),
    }


DISCLAIMER = (
    "免责声明：本邮件仅基于量化规则与公开行情数据的客观描述，"
    "供研究与信息参考，不构成任何投资建议，不承诺收益。"
    "市场有风险，决策需独立判断。"
)


def _build_prompts(period: str, email: str, classified: dict) -> tuple:
    period_cn = "周报" if period == "weekly" else "月报"
    focus = [_focus_facts(a) for a in classified["focus"]]
    watch_lines = [_one_line(a) for a in classified["watch"][:40]]
    payload = {
        "period": period_cn,
        "focus_count": len(focus),
        "up_count": classified["up_count"],
        "down_count": classified["down_count"],
        "watch_count": classified["watch_count"],
        "focus": focus,
        "watch_one_liners": watch_lines,
    }
    data_json = json.dumps(payload, ensure_ascii=False, indent=2)

    system = (
        "你是一名客观的量化观察编辑，为中文读者写股票观察邮件。\n"
        "硬性规则：\n"
        "1) 只使用用户提供的 JSON 事实，严禁编造价格、新闻、公司事件或未给出的数字；\n"
        "2) 禁止任何买卖建议与仓位建议，禁止出现：建议买入/卖出/加仓/减仓/建仓/清仓/推荐持有 等措辞；\n"
        "3) 语气简洁、有力、客观，短句为主；\n"
        "4) 只对 focus 列表做深度分析（每只 3～6 句），watch_one_liners 仅可轻微润色为一句话列表，不要展开；\n"
        "5) 输出纯 HTML 片段（h2/h3/p/ul/li/strong），不要 html/body 外壳，不要 Markdown 代码块；\n"
        "6) 文末必须原样附上免责声明段落。\n"
        f"免责声明固定文案：{DISCLAIMER}"
    )

    user = (
        f"请为收件人 {email} 生成一期【{period_cn}】观察邮件。\n"
        f"结构必须如下：\n"
        f"<h2>Autopilot 股票观察{period_cn}</h2>\n"
        f"<p>概览：方向偏多 N 只 / 方向偏空 N 只 / 观望 N 只（用 JSON 中的计数）。</p>\n"
        f"<h3>一、重点观察（即将上涨 / 即将下跌）</h3>\n"
        f"对 focus 中每一只：标题用 代码+名称+方向（上涨侧/下跌侧），"
        f"写清：现价与近1日/近5日涨跌、九转时机、趋势过滤、高低与相对位置（若有）、"
        f"动作标签与原因字段中的事实。不要给建议。\n"
        f"<h3>二、其余标的（一句话）</h3>\n"
        f"<ul> 使用 watch_one_liners </ul>\n"
        f"<h3>三、免责声明</h3>\n"
        f"<p>{DISCLAIMER}</p>\n\n"
        f"数据 JSON：\n{data_json}"
    )
    return system, user


def _fallback_html(period: str, email: str, classified: dict) -> str:
    period_cn = "周报" if period == "weekly" else "月报"
    parts = [
        f"<h2>Autopilot 股票观察{period_cn}</h2>",
        f"<p>收件人：{email} · 生成时间 {datetime.now():%Y-%m-%d %H:%M}</p>",
        f"<p>概览：方向偏多 <strong>{classified['up_count']}</strong> 只 / "
        f"方向偏空 <strong>{classified['down_count']}</strong> 只 / "
        f"观望 <strong>{classified['watch_count']}</strong> 只。"
        f"以下仅深写 {len(classified['focus'])} 只重点。</p>",
        "<h3>一、重点观察</h3>",
    ]
    if not classified["focus"]:
        parts.append("<p>本期无明确「即将上涨/即将下跌」共振标的，故无重点展开。</p>")
    for a in classified["focus"]:
        side = "上涨侧" if a.get("bucket") == "up" else "下跌侧"
        chg1 = a.get("change_1d")
        chg5 = a.get("change_5d")
        chg1_s = f"{chg1:+.2f}%" if isinstance(chg1, (int, float)) else "—"
        chg5_s = f"{chg5:+.2f}%" if isinstance(chg5, (int, float)) else "—"
        parts.append(
            f"<h4>{a.get('code')} {a.get('name') or ''} · {side}</h4>"
            f"<p>现价 {a.get('price')}，日 {chg1_s}，近5日 {chg5_s}。"
            f"九转时机：{a.get('timing') or '—'}；"
            f"趋势过滤：{a.get('trend_filter') or a.get('trend') or '—'}；"
            f"动作标签：{a.get('action') or '—'}。"
            f"{('原因：' + a['action_reason']) if a.get('action_reason') else ''}"
            f" 新高/新低：{a.get('high_low') or '—'}；"
            f"相对位置：{a.get('valuation') or '—'} {a.get('valuation_detail') or ''}。</p>"
        )
    parts.append("<h3>二、其余标的（一句话）</h3>")
    if classified["watch"]:
        parts.append("<ul>")
        for a in classified["watch"]:
            parts.append(f"<li>{_one_line(a)}</li>")
        parts.append("</ul>")
    else:
        parts.append("<p>无。</p>")
    parts.append(f"<h3>三、免责声明</h3><p style='color:#7f8c8d;font-size:12px;'>{DISCLAIMER}</p>")
    return "\n".join(parts)


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

    if ai_client.available():
        try:
            system, user = _build_prompts(period, email, classified)
            html = ai_client.chat(system, user, max_tokens=2500, temperature=0.25)
            if html and html.strip():
                # 若模型漏了免责，强制追加
                if "不构成" not in html and "免责" not in html:
                    html += f"<h3>免责声明</h3><p>{DISCLAIMER}</p>"
                return html
        except Exception as e:
            logger.warning("AI 生成失败，降级: %s", e)
    return _fallback_html(period, email, classified)


def generate_for_user(uid: str, email: str, period: str = "weekly") -> bool:
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


def _due_for_send(prefs_freq: str, last_sent: str, period: str) -> bool:
    """按频率判断是否该发。period 为任务触发粒度（weekly 任务也可服务 biweekly 用户）。"""
    from datetime import datetime, timedelta
    if not last_sent:
        return True
    try:
        last = datetime.fromisoformat(str(last_sent).replace("Z", ""))
    except Exception:
        return True
    days = (datetime.now() - last).days
    # biweekly：至少间隔 12 天；weekly：至少间隔 6 天（避免同周重复）
    need = 12 if (prefs_freq == "biweekly") else 6
    return days >= need


def run_reports(period: str = "weekly") -> dict:
    """
    只向「开启看板邮件推送」的用户发送。
    频率：weekly（约每周）/ biweekly（约每两周），由用户偏好控制，避免刷屏。
    period 参数保留兼容（weekly|monthly）；monthly 仍可用但默认产品推荐 weekly 任务。
    """
    from datetime import datetime
    subs = user_store.list_digest_subscribers()
    sent = skipped = 0
    total = len(subs)
    for u in subs:
        email = u.get("email")
        uid = u.get("id")
        freq = u.get("freq") or "weekly"
        # monthly 任务：所有开启用户都可发；weekly 任务：按 last_sent 节流
        if period != "monthly":
            if not _due_for_send(freq, u.get("last_sent"), period):
                skipped += 1
                continue
        # biweekly 用户在 weekly cron 下也会被 _due_for_send 挡住直到满约两周
        try:
            if generate_for_user(uid, email, "weekly" if period != "monthly" else period):
                user_store.set_digest_prefs(uid, last_sent=datetime.now().isoformat(timespec="seconds"))
                sent += 1
            else:
                skipped += 1
        except Exception as e:
            logger.warning("报告生成失败 %s: %s", email, e)
            skipped += 1
    logger.info("报告任务完成 period=%s sent=%d skipped=%d subscribers=%d", period, sent, skipped, total)
    return {"sent": sent, "skipped": skipped, "total": total, "subscribers": total}


if __name__ == "__main__":
    import sys
    _period = sys.argv[1] if len(sys.argv) > 1 else "weekly"
    print(json.dumps(run_reports(_period), ensure_ascii=False))
