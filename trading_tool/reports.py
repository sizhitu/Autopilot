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
from typing import List, Dict, Any, Tuple

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
SITE_URL = "https://www.timebricks.bid"


def _report_period_label(period: str = "weekly") -> Tuple[str, str]:
    """返回 (正式期号文案, 短标签)。周报用 ISO 年+周序。"""
    now = datetime.now()
    iso = now.isocalendar()  # year, week, weekday
    y, w = int(iso[0]), int(iso[1])
    if period == "monthly":
        formal = f"{now.year}年{now.month}月月报"
        short = f"{now.year}-{now.month:02d}"
    else:
        formal = f"{y}年第{w}周周报"
        short = f"{y}-W{w:02d}"
    return formal, short



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
            f"观望类已省略，以减少干扰。完整列表请到 <a href="https://www.timebricks.bid" style="color:#5b9fd4;text-decoration:underline;">网页看板</a> 查看。</p>"
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


def _structural_thesis(industry: str, biz: str, name: str) -> tuple:
    """
    参考优质研报骨架：旧地图→新需求→供给约束→质地锚点。
    返回 (叙事锚点, 供需/瓶颈, 质地要点) —— 不编造具体财报数字。
    """
    t = f"{industry} {biz} {name}"
    # AI / 算力 / 半导体
    if any(k in t for k in ("半导体", "AI芯片", "GPU", "存储", "光通信", "Chiplet", "晶圆", "光刻")):
        return (
            "算力扩张最终受<strong>物理瓶颈</strong>约束：芯片、封装、互联与散热比「软件叙事」更硬。",
            "需求侧看数据中心与端侧算力；供给侧看先进制程、设备交期与封装密度。"
            "价格弹性往往来自<strong>产能与良率的滞后</strong>，而非单季主题热度。",
            "质地关键看：是否卡在不可替代环节（设计/设备/材料）、客户认证壁垒、以及周期下行时的现金与库存纪律。",
        )
    # 铜铝有色 / 矿业
    if any(k in t for k in ("铜", "铝", "镍", "锡", "有色", "矿业", "黄金", "金属")):
        return (
            "工业金属正从「地产周期影子」转向<strong>电力与电气化的物理底座</strong>；瓶颈在矿山与冶炼，不在主题口号。",
            "需求看电网、数据中心、新能源车与轻量化；供给看矿山品位下降、审批与资本开支断层。"
            "真正的错配是<strong>多年 CapEx 不足带来的产量滞后</strong>。",
            "质地关键看：现金成本位置、资源品位与年限、是否「纯暴露」于该金属（避免被其他大宗拖累）。",
        )
    # 航天 / 卫星
    if any(k in t for k in ("航天", "卫星", "发射", "星链", "月球")):
        return (
            "商业航天比拼的是<strong>发射成本曲线与合同兑现</strong>，不是单次发射新闻。",
            "需求来自通信、遥感与政府/商业载荷；供给约束在运力、频谱与资质。失败与延期会瞬间改写融资条件。",
            "质地关键看：技术里程碑是否可验证、客户合同质量、以及再融资依赖程度。",
        )
    # 医疗
    if any(k in t for k in ("医疗", "健康", "制药", "生物")):
        return (
            "医疗消费的核心是<strong>续费、合规与获客成本</strong>，增长叙事必须能落到单位经济模型。",
            "需求看渗透率与适应症扩展；约束看监管、支付方与竞争仿制。政策与安全事件是非线性风险。",
            "质地关键看：复购/订阅粘性、合规护城河、以及营销费用是否透支增长。",
        )
    # 互联网 / 广告
    if any(k in t for k in ("广告", "互联网", "搜索", "社交", "电商")):
        return (
            "平台价值取决于<strong>流量入口与变现闭环</strong>；广告预算周期会放大盈利波动。",
            "需求随企业营销开支；约束来自监管、隐私与平台政策。算法优势可被政策一刀切改写。",
            "质地关键看：用户时长与付费深度、广告加载率弹性、以及多元化收入是否降低单一周期暴露。",
        )
    # ETF / 指数
    if any(k in t for k in ("ETF", "指数", "基金")):
        return (
            "工具型暴露：收益由<strong>成分与规则</strong>决定，讨论重点是跟踪误差与行业贝塔，不是单体护城河。",
            "需求是配置需求；「供给」是份额与流动性。主题 ETF 的弹性来自成分景气，也同步承担集中度风险。",
            "质地关键看：费用率、持仓透明度、以及是否与你的宏观判断一致（而非代码本身的故事）。",
        )
    # 电力 / 电气设备
    if any(k in t for k in ("电力", "电气", "电网", "能源", "风电")):
        return (
            "电力设备与电网是算力与电气化的<strong>上游约束</strong>；交货周期与产能比短期订单更决定定价权。",
            "需求来自数据中心、新能源并网与电网改造；供给看产能扩张速度与关键部件瓶颈。",
            "质地关键看：订单能见度、产能利用率、以及是否绑定长周期客户资本开支。",
        )
    # default
    biz_s = (biz or industry or "该行业").strip()
    return (
        f"先问<strong>旧地图是否失效</strong>：市场仍用什么旧框架定价「{industry or name}」，真实需求是否已切到新约束。",
        f"围绕「{biz_s[:80]}」识别：增量需求从哪来、供给最慢的一环是什么；价格波动要区分周期噪声与瓶颈溢价。",
        "质地看三件事：是否可理解、竞争中是否可能有切换成本或成本优势、下行时资产负债表能否撑过错杀。",
    )


def _build_prompts(period: str, email: str, classified: dict) -> tuple:
    period_cn = "周报" if period == "weekly" else "月报"
    focus = [_focus_facts(a) for a in classified["focus"]]
    # 预置结构提纲，帮助模型对齐研报思路且保持短
    for f in focus:
        th, sup, qual = _structural_thesis(
            f.get("industry") or "", f.get("business_summary") or "", f.get("name") or ""
        )
        f["thesis_hint"] = th
        f["supply_demand_hint"] = sup
        f["quality_hint"] = qual
    payload = {
        "period": period_cn,
        "focus": focus,
        "up_count": classified["up_count"],
        "down_count": classified["down_count"],
    }
    data_json = json.dumps(payload, ensure_ascii=False, indent=2)

    system = (
        "你是投研编辑。写法对齐高质量主题研究的骨架，但篇幅必须短（每只 5 条以内）。\n"
        "研究骨架（学习，不要写成长文）：\n"
        "① 范式/叙事：旧框架哪里失效，新约束是什么（如「算力尽头是电力与金属」这类结构判断）；\n"
        "② 供需与瓶颈：需求机制 + 供给最慢的一环（品位、CapEx 断层、交期、监管）；\n"
        "③ 公司/工具质地：成本位置、暴露纯度、价值链卡位（对标「谁掌握瓶颈谁有溢价」）；\n"
        "④ 与当前价格行为交叉：JSON 中的估值位置、九转、趋势，只解释含义，不给操作建议；\n"
        "⑤ 证伪条件：1 条即可。\n\n"
        "硬规则：\n"
        "- 可使用 JSON 里的 thesis_hint / supply_demand_hint / quality_hint 并改写得更贴该标的，禁止空话；\n"
        "- 禁止编造未给出的财报数字、矿区产量、具体 PE；信息不足就基于业务描述做逻辑推演并标明推断；\n"
        "- 禁止买入/卖出/加仓等建议；\n"
        "- 输出 HTML：每只一个 h3 + ul，最多 5 个 li；用 strong 标关键词；不要长段落。\n"
    )
    user = (
        f"为用户写【{period_cn}】重点研究速读（仅 focus，{len(focus)} 只）。\n"
        f"上涨侧 {classified['up_count']} / 下跌侧 {classified['down_count']}。\n"
        f"每只固定 5 条 li 标签建议：叙事 / 供需瓶颈 / 质地 / 信号交叉 / 证伪。\n"
        f"JSON：\n{data_json}"
    )
    return system, user


def _fallback_deep(a: dict) -> str:
    """短版研报骨架：叙事→供需→质地→信号→证伪。"""
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
    th, sup, qual = _structural_thesis(industry, biz, name)
    biz_one = biz if biz else ""
    if biz_one and len(biz_one) > 70:
        biz_one = biz_one[:70] + "…"

    return (
        f"<div style='margin:0 0 28px;padding:0 0 20px;border-bottom:1px solid {C_BORDER};'>"
        f"<h3 style='color:{C_GOLD};font-size:15px;margin:0 0 12px;line-height:1.4;'>{code} {name} "
        f"<span style='color:{side_c};font-size:12px;font-weight:700;'>· {side}</span></h3>"
        f"<ul style='color:{C_TEXT};font-size:13px;line-height:1.85;margin:0;padding-left:18px;'>"
        f"<li style='margin:0 0 10px;'><strong style='color:{C_BLUE};'>叙事</strong>：{th}"
        f"{(' 业务画像：' + biz_one) if biz_one else ''}</li>"
        f"<li style='margin:0 0 10px;'><strong style='color:{C_ORANGE};'>供需瓶颈</strong>：{sup}</li>"
        f"<li style='margin:0 0 10px;'><strong style='color:{C_GOLD};'>质地</strong>：{qual}</li>"
        f"<li style='margin:0 0 10px;'><strong style='color:{side_c};'>信号交叉</strong>：相对位置 "
        f"<span style='color:{C_GOLD};font-weight:700;'>{val}</span>"
        f"{('（' + val_d + '）') if val_d else ''}，近5日 "
        f"<span style='color:{_color_for_chg(a.get('change_5d'))};font-weight:700;'>{_pct(a.get('change_5d'))}</span>；"
        f"九转 <span style='color:{_color_for_timing(a)};font-weight:700;'>{timing}</span>，"
        f"趋势 <span style='color:{_color_for_trend(a)};font-weight:700;'>{trend}</span>，"
        f"动作 <span style='color:{_color_for_action(a)};font-weight:700;'>{action}</span>。"
        f"同向则价格行为与结构叙事较一致；背离则优先降低单标签权重。</li>"
        f"<li style='margin:0;'><strong style='color:{C_DIM};'>证伪</strong>：若行业资本开支/政策或需求主线被公开数据证伪，"
        f"或价格跌破关键均线区且放量反向，原「瓶颈溢价」叙事需降权。</li>"
        f"</ul></div>"
    )


def _wrap_email(period_cn: str, email: str, classified: dict, deep_html: str) -> str:
    table = _build_board_table(classified.get("board") or [])
    w = classified.get("watch_count") or 0
    watch_note = (
        f'另有 {w} 只观望未列入本邮件（'
        f'<a href="{SITE_URL}" style="color:{C_BLUE};text-decoration:underline;">到网页查看</a>）。'
        if w else
        f'本期无额外观望省略项 · '
        f'<a href="{SITE_URL}" style="color:{C_BLUE};text-decoration:underline;">www.timebricks.bid</a>'
    )
    period_key = "monthly" if "月" in (period_cn or "") else "weekly"
    formal, short = _report_period_label(period_key)
    gen_ts = datetime.now().strftime("%Y年%m月%d日 %H:%M")
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Autopilot {formal}</title></head>
<body style="margin:0;padding:0;background:{C_BG};color:{C_TEXT};">
<div style="max-width:640px;margin:0 auto;padding:18px 12px;font-family:Georgia,'Times New Roman',Songti SC,SimSun,serif;">
  <p style="margin:0 0 4px;text-align:center;letter-spacing:0.28em;font-size:11px;color:{C_DIM};text-transform:uppercase;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;">
    AUTOPILOT RESEARCH BRIEF
  </p>
  <p style="margin:0 0 14px;text-align:center;font-size:20px;font-weight:600;color:{C_GOLD};letter-spacing:0.06em;line-height:1.4;">
    {formal}
  </p>
  <p style="margin:0 0 16px;text-align:center;font-size:12px;color:{C_DIM};font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;line-height:1.6;">
    <span style="color:{C_TEXT};">{short}</span>
    · {gen_ts}
    <br>
    <span style="color:{C_GREEN};font-weight:700;">上涨侧 {classified.get('up_count', 0)}</span>
    · <span style="color:{C_RED};font-weight:700;">下跌侧 {classified.get('down_count', 0)}</span>
    · {watch_note}
  </p>
  <h1 style="color:{C_TEXT};font-size:16px;margin:0 0 12px;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;font-weight:600;border-top:1px solid {C_BORDER};padding-top:14px;">
    方向速读
  </h1>

  <h2 style="color:{C_TEXT};font-size:15px;margin:0 0 8px;">一、只需盯的方向</h2>
  <p style="color:{C_DIM};font-size:11px;margin:0 0 8px;">只列即将上涨 / 即将下跌；纵向阅读，无需左右滑。</p>
  {table}

  <div style="margin-top:28px;">
    <h2 style="color:{C_TEXT};font-size:15px;margin:0 0 10px;">二、重点研究速读</h2>
    <p style="color:{C_DIM};font-size:11px;margin:0 0 14px;line-height:1.6;">叙事 → 供需瓶颈 → 质地 → 信号交叉 → 证伪（短版深研，非操作建议）。</p>
    <div style="line-height:1.85;">
    {deep_html}
    </div>
  </div>

  <p style="color:{C_DIM};font-size:11px;margin:28px 0 0;line-height:1.65;border-top:1px solid {C_BORDER};padding-top:14px;">
    {DISCLAIMER}
  </p>
  <p style="text-align:center;margin:18px 0 8px;font-size:13px;font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;">
    <a href="{SITE_URL}" style="color:{C_BLUE};text-decoration:underline;font-weight:600;">打开 TimeBricks 网页看板 →</a>
  </p>
  <p style="text-align:center;margin:0 0 6px;font-size:11px;color:{C_DIM};font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif;">
    {SITE_URL}
  </p>
</div>
</body></html>"""


def _fallback_html(period: str, email: str, classified: dict) -> str:
    period_cn = "周报" if period == "weekly" else "月报"
    if not classified.get("focus"):
        deep = f"<p style='color:{C_DIM};'>本期无明确上涨/下跌标签标的，故无重点展开。</p>"
    else:
        deep = "".join(_fallback_deep(a) for a in classified["focus"])
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
    formal, short = _report_period_label(period)
    subject = f"Autopilot {formal} · 方向速读"
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
