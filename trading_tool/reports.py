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
    """邮件看板：竖向卡片列表（避免横向滚动与邮件 App 左右滑切换冲突）。"""
    if not analyses:
        return f"<p style='color:{C_DIM};font-size:13px;'>暂无自选数据</p>"

    cards = []
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
        ind = a.get("industry") or ""

        # 两列小表：邮件客户端兼容性好，无需横向滑动
        cards.append(
            f"<div style='border:1px solid {C_BORDER};border-radius:10px;background:{C_CARD};"
            f"padding:12px 14px;margin:0 0 10px;'>"
            f"<div style='margin-bottom:8px;'>"
            f"<span style='color:{C_GOLD};font-weight:700;font-size:15px;'>{code}</span>"
            f"<span style='color:{C_TEXT};font-size:13px;margin-left:8px;'>{name}</span>"
            f"{(f'<span style=\"color:{C_DIM};font-size:11px;margin-left:8px;\">{ind}</span>') if ind else ''}"
            f"</div>"
            f"<table style='width:100%;border-collapse:collapse;font-size:12px;color:{C_TEXT};'>"
            f"<tr>"
            f"<td style='padding:4px 0;color:{C_DIM};width:28%;'>现价</td>"
            f"<td style='padding:4px 0;font-weight:600;'>{px}</td>"
            f"<td style='padding:4px 0;color:{C_DIM};width:28%;'>日 / 近5日</td>"
            f"<td style='padding:4px 0;'>"
            f"<span style='color:{_color_for_chg(chg1)};font-weight:600;'>{_pct(chg1)}</span>"
            f"<span style='color:{C_DIM};'> / </span>"
            f"<span style='color:{_color_for_chg(chg5)};font-weight:600;'>{_pct(chg5)}</span>"
            f"</td></tr>"
            f"<tr>"
            f"<td style='padding:4px 0;color:{C_DIM};'>九转时机</td>"
            f"<td style='padding:4px 0;color:{_color_for_timing(a)};font-weight:600;' colspan='3'>{timing}</td>"
            f"</tr>"
            f"<tr>"
            f"<td style='padding:4px 0;color:{C_DIM};'>趋势过滤</td>"
            f"<td style='padding:4px 0;color:{_color_for_trend(a)};font-weight:600;'>{trend}</td>"
            f"<td style='padding:4px 0;color:{C_DIM};'>建议动作</td>"
            f"<td style='padding:4px 0;color:{_color_for_action(a)};font-weight:700;'>{action}</td>"
            f"</tr>"
            f"<tr>"
            f"<td style='padding:4px 0;color:{C_DIM};'>新高/估值</td>"
            f"<td style='padding:4px 0;color:{C_DIM};' colspan='3'>{hl} · {val}</td>"
            f"</tr>"
            f"</table></div>"
        )
    return "".join(cards)


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
        "focus_count": len(focus),
        "up_count": classified["up_count"],
        "down_count": classified["down_count"],
        "watch_count": classified["watch_count"],
        "focus": focus,
    }
    data_json = json.dumps(payload, ensure_ascii=False, indent=2)

    system = (
        "你是一名严谨的股权研究编辑，熟悉查理·芒格的多学科检查清单与能力圈思想。\n"
        "为中文读者写「重点标的深度分析」HTML。\n\n"
        "硬性规则：\n"
        "1) 必须紧扣 JSON 中每只标的的 industry、business_summary、role、valuation、"
        "timing、trend、price、涨跌幅等字段展开；禁止输出空泛套话"
        "（如「仅凭代码无法判断」「请自行查阅一手资料」作为主体内容）。\n"
        "2) 禁止编造未给出的财务数字（营收、利润、PE、市值等）。信息不足时写清「本邮件未接入该数据」，"
        "并基于已有业务描述做逻辑推演，而不是放弃分析。\n"
        "3) 禁止任何买卖/仓位建议措辞。\n"
        "4) 不要输出看板全表或观望列表。输出纯 HTML 片段（h3/h4/p/ul/li/strong）。\n"
        "5) 每只标的写满实质内容，建议 8～14 句或等价列表项。\n\n"
        "每只标的必须包含以下小节（标题可用 h4）：\n"
        "【业务与能力圈】用 business_summary 与 industry 说明公司/基金实际做什么、收入大致来自哪里、"
        "普通人要理解它需要哪些行业知识；点明能力圈边界（例如强周期制造 vs 订阅医疗 vs 指数ETF）。\n"
        "【商业模式与可能的护城河】基于业务描述推断：规模、网络、转换成本、品牌、成本、监管牌照等"
        "哪一类更可能相关；同时写 1～2 条最可能的反例（护城河可能不成立的原因）。ETF/指数则改写为"
        "「持仓结构特征与工具属性」，不要硬套公司护城河。\n"
        "【价格位置与安全边际讨论】结合 valuation、valuation_detail、high_low、近5日涨跌，"
        "讨论「相对均线/区间位置」意味着什么；强调这是规则化近似，不是内在价值，但要给出可操作的观察点"
        "（例如：跌破某类位置后哪些假设要重检）。\n"
        "【信号与基本面的交叉验证】把九转时机、趋势过滤、动作标签与业务质地对照："
        "同向时说明「价格行为与什么叙事一致」；冲突时说明「更可能是噪声还是风险警示」。\n"
        "【关键不确定与证伪条件】列出 2～3 条：若出现什么公开事实/价格行为，应降低对该叙事的权重。\n"
    )

    user = (
        f"请为 {email} 生成【{period_cn}】重点深度分析 HTML。\n"
        f"概览一句即可：偏多 {classified['up_count']} / 偏空 {classified['down_count']} / 观望 {classified['watch_count']}，"
        f"深写 {len(focus)} 只。\n"
        f"对 focus 每一只用 <h3>代码 名称</h3>，再按上述五小节展开。\n"
        f"文末不要重复长免责（外层模板已有）。\n\n"
        f"数据 JSON：\n{data_json}"
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
    """无 AI 时：用行业/主营/估值/信号写可读的实质分析（避免空泛套话）。"""
    code = a.get("code") or ""
    name = a.get("name") or ""
    industry = (a.get("industry") or "").strip() or "未标注行业"
    biz = (a.get("business_summary") or "").strip()
    role = a.get("role") or "—"
    val = a.get("valuation") or "—"
    val_d = a.get("valuation_detail") or ""
    timing = a.get("timing") or "—"
    trend = a.get("trend_filter") or a.get("trend") or "—"
    action = a.get("action") or "—"
    reason = a.get("action_reason") or ""
    hl = a.get("high_low") or "—"
    side = "上涨侧观察" if a.get("bucket") == "up" else (
        "下跌侧观察" if a.get("bucket") == "down" else "中性观察"
    )

    if not biz:
        biz = "本邮件未取到主营简介；以下仅结合行业标签与价格行为做框架讨论。"

    # 业务段
    biz_para = (
        f"<strong>{code}</strong>（{name}）所属「{industry}」。业务画像：{biz}"
        f"理解它需要先分清：收入是来自产品销售、订阅、制造周期、还是指数/一篮子持仓的工具属性。"
        f"若你无法用自己的话复述「客户是谁、为何付费、主要成本与竞争对象」，则该标的可能落在能力圈之外，"
        f"此时价格信号的参考价值应系统性降权。"
    )

    # 护城河段 — 按行业关键词给出更具体的讨论
    ind = industry
    if "ETF" in ind or "指数" in ind or "基金" in name or "ETF" in name:
        moat = (
            f"该标的更接近<strong>工具/一篮子暴露</strong>，讨论重点不是单体公司护城河，而是："
            f"跟踪误差、持仓集中度、行业景气与费用拖累。优势在于透明与分散；弱点在于你无法对单一公司做深度资本配置判断，"
            f"收益结构由成分与规则决定。"
        )
    elif "半导体" in ind or "芯片" in ind:
        moat = (
            f"半导体链条常见优势来自<strong>工艺节点、客户认证、规模与生态</strong>（设计软件/IP/制造）。"
            f"同时行业资本开支与库存周期极强：景气时利润弹性大，去库存时估值与盈利双杀。"
            f"需警惕把短期算力/存储涨价叙事直接当成不可逆护城河。"
        )
    elif "医疗" in ind or "健康" in ind:
        moat = (
            f"消费医疗/数字医疗常见优势是<strong>品牌、处方合规路径与复购</strong>；"
            f"反面是监管、支付方政策与获客成本变化。订阅模式看起来稳定，实则对续费率与合规事件极度敏感。"
        )
    elif "航天" in ind or "卫星" in ind:
        moat = (
            f"商业航天/卫星通信的关键在于<strong>发射成本曲线、频谱与客户合同</strong>；"
            f"技术里程碑与融资节奏往往比单季利润更能解释波动。失败模式包括发射事故、进度延期与再融资条件恶化。"
        )
    elif "广告" in ind or "互联网" in ind:
        moat = (
            f"广告与互联网平台更看重<strong>流量入口、数据反馈闭环与销售效率</strong>；"
            f"护城河可能体现在规模与算法，但广告预算周期与平台政策会快速改写盈利假设。"
        )
    else:
        moat = (
            f"结合「{industry}」属性，优先识别：是否有切换成本、规模经济、独特资产或监管壁垒；"
            f"并主动寻找反例（技术路线被替代、客户集中、价格战）。在缺少完整财报时，不要把叙事当成已验证的护城河。"
        )

    # 价格与安全边际
    price_para = (
        f"规则化相对位置为「<strong>{val}</strong>」"
        f"{('（' + val_d + '）') if val_d else ''}，新高/新低标记：{hl}；"
        f"近1日 {_pct(a.get('change_1d'))}，近5日 {_pct(a.get('change_5d'))}。"
        f"这反映的是相对均线或历史区间的位置，不是内在价值。可用的研究问题是："
        f"若位置显示偏高估，当前趋势能否用「盈利预期上修」解释，还是仅有价格动量？"
        f"若显示偏低估，是周期底部的时间换空间，还是基本面趋势仍在恶化？"
    )

    # 信号交叉
    cross = (
        f"系统标签：九转「{timing}」，趋势「{trend}」，动作「{action}」"
        f"{('；原因：' + reason) if reason else ''}（{side}）。"
        f"若趋势与九转同向，价格行为与短线叙事较一致，仍要用业务周期去解释「为什么现在该有趋势」。"
        f"若二者冲突，优先假设信号噪声上升，降低对单一标签的权重，直到价格与基本面线索重新对齐。"
    )

    # 证伪
    falsify = (
        f"<li>业务层面：若行业需求、监管或技术路线出现公开的不利变化，应重检「{industry}」叙事。</li>"
        f"<li>价格层面：若相对位置从「{val}」快速漂移到另一端且伴随放量反向，说明原有均值回归/趋势假设可能失效。</li>"
        f"<li>组合层面：作为「{role}」——{_role_hint(role)}若该角色与真实波动特性不符，应调整预期而非加戏。</li>"
    )

    return (
        f"<h3 style='color:{C_GOLD};margin:20px 0 8px;'>{code} {name}</h3>"
        f"<h4 style='color:{C_TEXT};font-size:14px;margin:10px 0 4px;'>业务与能力圈</h4>"
        f"<p style='color:{C_TEXT};line-height:1.7;font-size:13px;margin:0 0 8px;'>{biz_para}</p>"
        f"<h4 style='color:{C_TEXT};font-size:14px;margin:10px 0 4px;'>商业模式与可能的护城河</h4>"
        f"<p style='color:{C_TEXT};line-height:1.7;font-size:13px;margin:0 0 8px;'>{moat}</p>"
        f"<h4 style='color:{C_TEXT};font-size:14px;margin:10px 0 4px;'>价格位置与安全边际讨论</h4>"
        f"<p style='color:{C_TEXT};line-height:1.7;font-size:13px;margin:0 0 8px;'>{price_para}</p>"
        f"<h4 style='color:{C_TEXT};font-size:14px;margin:10px 0 4px;'>信号与基本面的交叉验证</h4>"
        f"<p style='color:{C_TEXT};line-height:1.7;font-size:13px;margin:0 0 8px;'>{cross}</p>"
        f"<h4 style='color:{C_TEXT};font-size:14px;margin:10px 0 4px;'>关键不确定与证伪条件</h4>"
        f"<ul style='color:{C_TEXT};line-height:1.7;font-size:13px;margin:0;padding-left:18px;'>{falsify}</ul>"
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
  <p style="color:{C_DIM};font-size:12px;margin:0 0 8px;">每只标的一张卡片纵向排列，避免左右滑动误触切换邮件；颜色含义与网页看板一致。</p>
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
