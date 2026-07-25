"""
股票代码归一化（多市场）
========================
统一不同市场的代码表示，便于存储、缓存 key、搜索与展示。
判定优先级：港股(数字+.HK) → A股(6位纯数字) → 美股/ETF(字母)。

返回 dict：
  symbol   归一化后的代码（A股保持数字串；港股加 .HK；美股大写）
  market   '美股' / 'A股' / '港股' / '指数' / 'ETF' / '其他'
  display  展示名（与输入一致，原样返回）
"""


def detect_market(raw: str) -> str:
    s = (raw or "").strip().upper()
    if not s:
        return "其他"
    # 指数：雅虎风格 ^ 前缀（^IXIC / ^GSPC 等）
    if s.startswith("^"):
        return "指数"
    # 港股：5 位数字 + 可选 .HK 后缀
    if s.endswith(".HK"):
        return "港股"
    if s.isdigit() and len(s) == 5:
        return "港股"
    # A股：6 位纯数字（沪 60/68、深 00/30、京 8/4 等）
    if s.isdigit() and len(s) == 6:
        return "A股"
    # 含字母的视为美股 / ETF：以 .ETF / .ETF 等后缀或常见指数代码识别
    if any(c.isalpha() for c in s):
        if s.endswith(".ETF") or s.endswith((".IXIC", ".INX", ".DJI", ".HSI", ".CSI")):
            return "指数" if s.startswith("^") or s.endswith((".IXIC", ".INX", ".DJI", ".HSI", ".CSI")) else "ETF"
        return "美股"
    if s.isdigit():
        return "指数" if len(s) <= 6 else "其他"
    return "其他"


def normalize_symbol(raw: str) -> dict:
    """归一化输入为统一代码格式。"""
    raw = (raw or "").strip()
    s = raw.upper()
    market = detect_market(raw)

    if market == "港股":
        code = s[:-3] if s.endswith(".HK") else s
        code = code.zfill(5) + ".HK"
    elif market == "A股":
        code = s.zfill(6)
    else:
        # 美股 / ETF / 指数：保持大写字母代码
        code = s

    return {
        "symbol": code,
        "market": market,
        "display": raw,
    }


def is_code_like(raw: str) -> bool:
    """是否像代码（不含中文），用于判断「代码」还是「名称」输入。"""
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9.\-^]+", (raw or "").strip()))
