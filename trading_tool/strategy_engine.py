"""
藤本茂交易哲学融合策略引擎
=======================================
三层一体：系统层(趋势+指标) + 工具层(斐波那契) + 时机层(九转+量价)
藤本茂阶梯仅作仓位建议，不参与「通过/不通过」门槛。

独立模块，不含 GUI，可被任何前端调用。
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum

try:
    from nine_turn import calc_nine_turn
except Exception:  # pragma: no cover
    calc_nine_turn = None

try:
    from volume_price_divergence import detect_volume_price_divergence, volume_supports_side
except Exception:  # pragma: no cover
    detect_volume_price_divergence = None
    volume_supports_side = None

try:
    from volume_convergence import compute_volume_convergence
except Exception:  # pragma: no cover
    compute_volume_convergence = None

try:
    from price_triangle import detect_price_triangle
except Exception:  # pragma: no cover
    detect_price_triangle = None


class SignalType(Enum):
    BUY = "买入"
    SELL = "卖出"
    HOLD = "持有"
    WAIT = "观望"
    ADD = "加仓"


class TrendType(Enum):
    BULL = "多头趋势"
    BEAR = "空头趋势"
    RANGE = "震荡"


@dataclass
class IndicatorResult:
    """单个指标计算结果"""
    name: str
    value: float
    signal: str       # "看多" / "看空" / "中性"
    detail: str = ""


@dataclass
class FibLevel:
    level: float      # 0.382, 0.5, 0.618, 0.786
    price: float
    tested: bool = False       # 价格是否触及
    reacted: bool = False      # 是否有市场反应确认
    reaction_signal: str = ""  # 反应类型描述


@dataclass
class StrategyResult:
    """策略完整输出"""
    trend: TrendType
    signal: SignalType
    action: str               # 人类可读操作建议
    position_pct: float       # 建议仓位百分比
    entry_price: Optional[float]
    stop_loss: Optional[float]
    target_prices: list       # 目标价列表
    fib_levels: list          # FibLevel 列表
    indicators: list          # IndicatorResult 列表
    layers_consistent: dict   # 三层一致性检验
    risk_warning: str = ""
    chart_data: dict = field(default_factory=dict)


class FujimotoStrategy:
    """藤本茂融合策略引擎"""

    # 藤本茂阶梯规则
    BUY_LADDER = [
        (-0.05, 0.00, "下跌5%不操作（噪音区间）"),
        (-0.15, 0.10, "下跌15%增持10%"),
        (-0.25, 0.25, "下跌25%增持25%"),
    ]
    SELL_LADDER = [
        (0.05, 0.00, "上涨5%继续持有"),
        (0.15, 0.00, "上涨15%继续持有"),
        (0.25, 0.10, "上涨25%卖出10%"),
        (0.35, 0.20, "上涨35%卖出20%"),
        (0.45, 0.30, "上涨45%卖出30%"),
        (0.60, 0.40, "上涨60%卖出40%"),
        (1.00, 1.00, "上涨100%清仓"),
    ]

    MA_PERIODS = [5, 10, 20, 30, 50, 100, 120, 150, 200, 250]

    def __init__(self, total_capital: float = 100000,
                 risk_per_trade: float = 0.02,
                 max_position: float = 0.70,
                 entry_price: Optional[float] = None):
        """
        Args:
            total_capital: 总资金
            risk_per_trade: 单笔最大风险比例
            max_position: 最大总仓位比例
            entry_price: 初始建仓价（用于追踪涨跌幅）
        """
        self.total_capital = total_capital
        self.risk_per_trade = risk_per_trade
        self.max_position = max_position
        self.entry_price = entry_price

    # ================================================================
    #  系统层：均线与指标
    # ================================================================

    def _calc_ma(self, df: pd.DataFrame, periods: Optional[list] = None) -> dict:
        """计算均线（周期可自适应）"""
        if periods is None:
            periods = self.MA_PERIODS
        mas = {}
        for p in periods:
            if len(df) >= p:
                mas[p] = df['close'].rolling(p).mean().iloc[-1]
            else:
                mas[p] = None
        return mas

    def _calc_vwma(self, df: pd.DataFrame, period: int = 20) -> Optional[float]:
        """成交量加权均线（周期自适应）"""
        period = min(period, max(5, len(df) // 2))
        if len(df) < period:
            return None
        subset = df.tail(period)
        if subset['volume'].sum() == 0:
            return None
        return (subset['close'] * subset['volume']).sum() / subset['volume'].sum()

    def _strong_gap_threshold(self, atr_pct: Optional[float]) -> float:
        """
        「>>」相对 MA120 的最小偏离，随标的波动自适应。
        - 稳健低波（日 ATR% 较低）：约 3%~5%
        - 波动抬升：阈值逐步上调
        - 高波：至少约 20%，可再上浮
        atr_pct: ATR/收盘价*100，例如 1.5 表示 1.5%。
        """
        if atr_pct is None or atr_pct <= 0:
            return 0.05
        a = float(atr_pct)
        if a < 1.0:
            thr = 0.03
        elif a < 1.5:
            # 1.0→3%, 1.5→5%
            thr = 0.03 + (a - 1.0) * 0.04
        elif a < 2.5:
            # 1.5→5%, 2.5→12%
            thr = 0.05 + (a - 1.5) * 0.07
        elif a < 4.0:
            # 2.5→12%, 4.0→20%
            thr = 0.12 + (a - 2.5) * (0.08 / 1.5)
        else:
            # 高波：≥20%，随 ATR 再缓增，封顶 35%
            thr = 0.20 + (a - 4.0) * 0.025
        return float(min(0.35, max(0.03, thr)))

    def _judge_trend(self, mas: dict, vwma: Optional[float], close: float,
                     short_periods: list, long_periods: list,
                     atr_pct: Optional[float] = None) -> tuple:
        """
        均线趋势判断（权重核心）。
        强多头参考：MA5>MA10>MA20>MA30 且相对 MA120「>>」
        （偏离阈值随 ATR 波动自适应：稳 3%~5%，高波 ≥20%）。
        """
        def _m(p):
            v = mas.get(p)
            return float(v) if v is not None else None

        m5, m10, m20, m30 = _m(5), _m(10), _m(20), _m(30)
        m50, m100, m120, m200 = _m(50), _m(100), _m(120) or _m(100), _m(200)

        # 纠缠：短均线挤在一起
        short_chain = [x for x in (m5, m10, m20, m30) if x is not None]
        if len(short_chain) >= 3 and close > 0:
            spread = (max(short_chain) - min(short_chain)) / close
            if spread < 0.01:
                return TrendType.RANGE, "短期均线纠缠（差异<1%），趋势不明"

        # 标准多头/空头排列：5>10>20>30 / 反向
        stack_bull = (
            m5 is not None and m10 is not None and m20 is not None and m30 is not None
            and m5 > m10 > m20 > m30
        )
        stack_bear = (
            m5 is not None and m10 is not None and m20 is not None and m30 is not None
            and m5 < m10 < m20 < m30
        )
        # 退化：至少 5>10>20
        soft_bull = (
            m5 is not None and m10 is not None and m20 is not None
            and m5 > m10 > m20 and not stack_bear
        )
        soft_bear = (
            m5 is not None and m10 is not None and m20 is not None
            and m5 < m10 < m20 and not stack_bull
        )

        # >>120：偏离阈值随波动自适应（稳约3%~5%，高波≥20%）
        gap_thr = self._strong_gap_threshold(atr_pct)
        strong_above_120 = False
        strong_below_120 = False
        gap = 0.0
        if m120 is not None and m30 is not None and m120 > 0:
            gap = (m30 - m120) / m120
            strong_above_120 = gap >= gap_thr
            strong_below_120 = gap <= -gap_thr

        vwma_bull = vwma is not None and close > vwma
        vwma_bear = vwma is not None and close < vwma
        price_above_30 = m30 is not None and close > m30
        price_below_30 = m30 is not None and close < m30

        # 强上涨：5>10>20>30 >> 120，价在短均线上方
        if stack_bull and strong_above_120 and (vwma_bull or price_above_30):
            gap_pct = (m30 - m120) / m120 * 100 if m120 else 0
            return (
                TrendType.BULL,
                f"强多头排列 MA5>10>20>30 且 MA30 高于 MA120 约 {gap_pct:.1f}%"
                f"（>>阈值 {gap_thr*100:.0f}%·ATR约{atr_pct if atr_pct is not None else '—'}%）"
                + ("，VWMA 确认" if vwma_bull else ""),
            )
        if stack_bear and strong_below_120 and (vwma_bear or price_below_30):
            gap_pct = (m120 - m30) / m120 * 100 if m120 else 0
            return (
                TrendType.BEAR,
                f"强空头排列 MA5<10<20<30 且 MA30 低于 MA120 约 {gap_pct:.1f}%"
                f"（>>阈值 {gap_thr*100:.0f}%·ATR约{atr_pct if atr_pct is not None else '—'}%）"
                + ("，VWMA 确认" if vwma_bear else ""),
            )

        # 普通多头：完整或软排列 + 价/VWMA 配合
        if stack_bull and (vwma_bull or price_above_30):
            note = "（相对 120 日线未拉开）" if m120 and not strong_above_120 else ""
            return TrendType.BULL, "多头排列 MA5>10>20>30" + note
        if soft_bull and (vwma_bull or (m20 is not None and close > m20)):
            return TrendType.BULL, "偏多排列 MA5>10>20（未齐 30）"
        if stack_bear and (vwma_bear or price_below_30):
            return TrendType.BEAR, "空头排列 MA5<10<20<30"
        if soft_bear and (vwma_bear or (m20 is not None and close < m20)):
            return TrendType.BEAR, "偏空排列 MA5<10<20"

        return TrendType.RANGE, "均线未形成稳定多空排列"

    def _system_layer_score(self, trend: TrendType, indicators: list) -> tuple:
        """
        系统层加权评分（非等权四票）。
        MACD 动量 > RSI > VOL 量能 > ATR 波动（ATR 偏风控，权重最低）。
        趋势市中：RSI 超买/超卖的逆向信号降权（强趋势里超买常见，不视为否决）。
        """
        weights = {"MACD": 0.40, "RSI": 0.30, "VOL": 0.20, "ATR": 0.10}
        score = 0.0
        parts = []
        for ind in indicators:
            w = weights.get(ind.name, 0.15)
            sig = ind.signal
            # 趋势跟随语境：多头里 RSI 超买、空头里 RSI 超卖 → 按中性处理（半权惩罚最多）
            if ind.name == "RSI":
                if trend == TrendType.BULL and sig == "看空":
                    score -= w * 0.25
                    parts.append(f"RSI超买降权")
                    continue
                if trend == TrendType.BEAR and sig == "看多":
                    score += w * 0.25
                    parts.append(f"RSI超卖降权")
                    continue
            # ATR 过高仅作轻量风险扣分，不对称否决趋势
            if ind.name == "ATR" and sig == "看空":
                score -= w * 0.5
                parts.append(f"ATR波动偏高")
                continue
            if sig == "看多":
                score += w
                parts.append(f"{ind.name}多×{w:.2f}")
            elif sig == "看空":
                score -= w
                parts.append(f"{ind.name}空×{w:.2f}")
            else:
                parts.append(f"{ind.name}中")

        sys_bull = trend == TrendType.BULL and score >= 0.15
        sys_bear = trend == TrendType.BEAR and score <= -0.15
        detail = f"加权分={score:+.2f}（" + "，".join(parts) + "）"
        return sys_bull, sys_bear, score, detail

    def _system_pass(self, trend: TrendType, trend_detail: str, indicators: list) -> tuple:
        """结合均线趋势与加权指标，返回 sys_bull, sys_bear, status_extra"""
        sys_bull, sys_bear, score, wdetail = self._system_layer_score(trend, indicators)
        # 强排列：均线已是主证据，允许指标接近中性
        if trend == TrendType.BULL and "强多头" in (trend_detail or "") and score >= -0.15:
            sys_bull = True
        if trend == TrendType.BEAR and "强空头" in (trend_detail or "") and score <= 0.15:
            sys_bear = True
        return sys_bull, sys_bear, wdetail

    def _calc_rsi(self, df: pd.DataFrame, period: int = 14) -> IndicatorResult:
        """RSI 指标"""
        if len(df) < period + 1:
            return IndicatorResult("RSI", 0, "中性", "数据不足")

        delta = df['close'].diff()
        gain = delta.where(delta > 0, 0).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        rsi_val = rsi.iloc[-1]

        if pd.isna(rsi_val):
            return IndicatorResult("RSI", 0, "中性", "计算异常")

        if rsi_val < 30:
            sig = "看多"
            detail = f"RSI={rsi_val:.1f} 超卖，反弹可能"
        elif rsi_val > 70:
            sig = "看空"
            detail = f"RSI={rsi_val:.1f} 超买，回调风险"
        else:
            sig = "中性"
            detail = f"RSI={rsi_val:.1f} 正常区间"
        return IndicatorResult("RSI", rsi_val, sig, detail)

    def _calc_macd(self, df: pd.DataFrame,
                   fast: int = 12, slow: int = 26, signal: int = 9) -> IndicatorResult:
        """MACD 指标"""
        if len(df) < slow + signal:
            return IndicatorResult("MACD", 0, "中性", "数据不足")

        ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
        ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
        dif = ema_fast - ema_slow
        dea = dif.ewm(span=signal, adjust=False).mean()
        hist = dif - dea

        dif_val, dea_val, hist_val = dif.iloc[-1], dea.iloc[-1], hist.iloc[-1]
        prev_hist = hist.iloc[-2] if len(hist) >= 2 else 0

        if dif_val > dea_val and hist_val > prev_hist:
            sig = "看多"
            detail = f"MACD金叉，柱状图扩大 DIF={dif_val:.2f}"
        elif dif_val < dea_val and hist_val < prev_hist:
            sig = "看空"
            detail = f"MACD死叉，柱状图扩大 DIF={dif_val:.2f}"
        else:
            sig = "中性"
            detail = f"MACD方向不明 DIF={dif_val:.2f} DEA={dea_val:.2f}"
        return IndicatorResult("MACD", dif_val, sig, detail)

    def _calc_atr(self, df: pd.DataFrame, period: int = 14) -> IndicatorResult:
        """ATR 波动率"""
        if len(df) < period + 1:
            return IndicatorResult("ATR", 0, "中性", "数据不足")

        high, low, close = df['high'], df['low'], df['close']
        tr1 = high - low
        tr2 = (high - close.shift()).abs()
        tr3 = (low - close.shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(period).mean()
        atr_val = atr.iloc[-1]
        close_val = close.iloc[-1]
        atr_pct = atr_val / close_val * 100 if close_val else 0

        if atr_pct > 5:
            sig = "看空"
            detail = f"ATR={atr_val:.2f} ({atr_pct:.1f}%) 波动过高，建议减仓"
        elif atr_pct < 1:
            sig = "中性"
            detail = f"ATR={atr_val:.2f} ({atr_pct:.1f}%) 波动极低，关注突破"
        else:
            sig = "看多"
            detail = f"ATR={atr_val:.2f} ({atr_pct:.1f}%) 波动正常"
        return IndicatorResult("ATR", atr_val, sig, detail)

    def _calc_volume_signal(self, df: pd.DataFrame, period: int = 10) -> IndicatorResult:
        """成交量信号"""
        if len(df) < period + 1:
            return IndicatorResult("VOL", 0, "中性", "数据不足")

        recent_vol = df['volume'].tail(period).mean()
        prev_vol = df['volume'].iloc[-period*2:-period].mean() if len(df) >= period*2 else recent_vol
        ratio = recent_vol / prev_vol if prev_vol else 1.0

        price_up = df['close'].iloc[-1] > df['close'].iloc[-period] if len(df) > period else True

        if ratio > 1.5 and price_up:
            sig = "看多"
            detail = f"放量上涨 (量比={ratio:.2f})"
        elif ratio > 1.5 and not price_up:
            sig = "看空"
            detail = f"放量下跌 (量比={ratio:.2f})"
        elif ratio < 0.6:
            sig = "中性"
            detail = f"缩量 (量比={ratio:.2f})"
        else:
            sig = "中性"
            detail = f"量能平稳 (量比={ratio:.2f})"
        return IndicatorResult("VOL", ratio, sig, detail)

    # ================================================================
    #  工具层：斐波那契
    # ================================================================

    def _calc_fibonacci(self, df: pd.DataFrame, lookback: int = 60) -> tuple:
        """
        计算斐波那契回撤与扩展位
        返回: (fib_levels列表, swing_high, swing_low)
        """
        recent = df.tail(lookback) if len(df) >= lookback else df
        swing_high = recent['high'].max()
        swing_low = recent['low'].min()
        diff = swing_high - swing_low

        if diff <= 0:
            return [], swing_high, swing_low

        levels_config = [
            (0.786, "极深回调"),
            (0.618, "黄金回调"),
            (0.500, "中等回调"),
            (0.382, "浅回调"),
        ]
        extensions = [1.272, 1.618]

        close = df['close'].iloc[-1]
        fib_levels = []
        for level, label in levels_config:
            price = swing_high - diff * level
            tested = swing_low * 0.995 <= price <= close * 1.02

            # 检查反应确认：该位置附近是否有阳线反包
            reacted = False
            reaction_signal = ""
            if tested:
                near_candles = df[(df['low'] <= price * 1.01) &
                                  (df['low'] >= price * 0.99)]
                if len(near_candles) > 0:
                    last_near = near_candles.iloc[-1]
                    idx = near_near_idx = df.index.get_loc(last_near.name)
                    if idx > 0:
                        prev = df.iloc[idx - 1]
                        # 阳线反包：当前实体覆盖前一根
                        body = last_near['close'] - last_near['open']
                        prev_body = prev['close'] - prev['open']
                        if body > 0 and prev_body < 0 and \
                           last_near['close'] > prev['open'] and \
                           last_near['open'] < prev['close']:
                            reacted = True
                            reaction_signal = "阳线反包确认"
                        # 下方阴线放量
                        elif idx > 0 and df['volume'].iloc[idx] > df['volume'].iloc[idx-1] * 1.2:
                            reacted = True
                            reaction_signal = "放量止跌"

            fib_levels.append(FibLevel(
                level=level, price=price, tested=tested,
                reacted=reacted, reaction_signal=reaction_signal
            ))

        # 扩展位作为目标
        target_prices = [swing_high + diff * (ext - 1) for ext in extensions]

        return fib_levels, swing_high, swing_low, target_prices

    def _find_fib_buy_point(self, fib_levels: list, close: float) -> Optional[FibLevel]:
        """找到有市场反应确认的斐波那契买点"""
        # 优先找有反应确认的
        for fl in fib_levels:
            if fl.tested and fl.reacted:
                return fl
        # 其次找已测试的
        for fl in fib_levels:
            if fl.tested:
                return fl
        return None

    # ================================================================
    #  仓位建议：藤本茂阶梯（不参与三层门槛）
    # ================================================================

    def _fujimoto_action(self, price_change: float, current_position: float = 0) -> tuple:
        """
        根据涨跌幅返回藤本茂操作建议
        返回: (操作描述, 仓位变动)
        """
        if price_change < 0:
            # 下跌阶梯：从最深的阈值开始匹配，确保分层正确
            for threshold, action_pct, desc in reversed(self.BUY_LADDER):
                if price_change <= threshold:
                    if action_pct == 0:
                        return desc, 0
                    else:
                        return desc, action_pct
            return "跌幅不足5%，继续持有", 0

        else:
            # 上涨阶梯：从最深的阈值开始匹配
            for threshold, action_pct, desc in reversed(self.SELL_LADDER):
                if price_change >= threshold:
                    if action_pct == 0:
                        return desc, 0
                    else:
                        return desc, -action_pct  # 负数表示卖出
            return "涨幅不足5%，继续持有", 0

    # ================================================================
    #  三层融合
    # ================================================================

    def analyze(self, df: pd.DataFrame, current_position_pct: float = 0) -> StrategyResult:
        """
        完整三层分析

        Args:
            df: OHLCV 数据 (columns: open, high, low, close, volume)
            current_position_pct: 当前持仓比例
        Returns:
            StrategyResult
        """
        df = df.copy()
        MIN_BARS = 10
        if len(df) < MIN_BARS:
            return StrategyResult(
                trend=TrendType.RANGE, signal=SignalType.WAIT,
                action="数据不足（至少需要10根K线）",
                position_pct=0, entry_price=None, stop_loss=None,
                target_prices=[], fib_levels=[], indicators=[],
                layers_consistent={},
                risk_warning="数据量不足，无法分析"
            )

        # 数据有限标记：沙箱环境常只有十余根K线，仍可做短期分析
        data_limited = len(df) < 30

        close = df['close'].iloc[-1]

        # === 系统层 ===
        # 根据可用数据量自适应选择均线周期
        short_periods = [p for p in self.MA_PERIODS if p <= 50 and len(df) >= p]
        long_periods = [p for p in self.MA_PERIODS if p >= 100 and len(df) >= p]
        mas = self._calc_ma(df, short_periods + long_periods)
        vwma = self._calc_vwma(df, period=20)
        atr_res = self._calc_atr(df)
        atr_pct = None
        if atr_res and atr_res.value and close:
            atr_pct = float(atr_res.value) / float(close) * 100.0
        trend, trend_detail = self._judge_trend(
            mas, vwma, close, short_periods, long_periods, atr_pct=atr_pct
        )

        rsi_res = self._calc_rsi(df)
        macd_res = self._calc_macd(df)
        vol_res = self._calc_volume_signal(df)
        indicators = [rsi_res, macd_res, atr_res, vol_res]

        # === 工具层 ===
        fib_result = self._calc_fibonacci(df)
        if len(fib_result) == 4:
            fib_levels, swing_high, swing_low, target_prices = fib_result
        else:
            fib_levels, swing_high, swing_low, target_prices = [], close, close, []

        fib_buy = self._find_fib_buy_point(fib_levels, close)

        # === 藤本茂阶梯（仅仓位建议，不参与三层门槛）===
        price_change = 0
        if self.entry_price and self.entry_price > 0:
            price_change = (close - self.entry_price) / self.entry_price
        fujimoto_desc, position_delta = self._fujimoto_action(price_change, current_position_pct)

        # === 时机层：九转确认 + 量价叠加 ===
        nt = None
        if calc_nine_turn is not None:
            try:
                nt = calc_nine_turn(df, unit="天")
            except Exception:
                nt = None

        nine_buy = bool(nt and nt.direction == "down" and (nt.is_complete or nt.is_completing))
        nine_sell = bool(nt and nt.direction == "up" and (nt.is_complete or nt.is_completing))

        # 量价背离形态
        vp_div = {"divergence": "none", "label": "未检测", "detail": "", "patterns": []}
        if detect_volume_price_divergence is not None:
            try:
                vp_div = detect_volume_price_divergence(df)
            except Exception:
                pass

        vol_bull = vol_res.signal == "看多"
        vol_bear = vol_res.signal == "看空"
        # 量价支持：背离同向优先；否则至少不是反向放量
        if volume_supports_side is not None:
            vol_ok_buy = volume_supports_side(vp_div, "buy") and not vol_bear
            vol_ok_sell = volume_supports_side(vp_div, "sell") and not vol_bull
        else:
            vol_ok_buy = not vol_bear
            vol_ok_sell = not vol_bull

        # 成交量收敛三角形（周线为主，辅助时机，不单独定方向）
        conv_info = {"converging": False, "status": "未检测", "tape": "", "summary": ""}
        if compute_volume_convergence is not None:
            try:
                vc = compute_volume_convergence(df)
                w = vc.get("weekly") or {}
                tape = vc.get("weekly_tape") or {}
                conv_info = {
                    "converging": bool(w.get("converging")),
                    "status": w.get("status") or "—",
                    "tape": tape.get("label") or "",
                    "summary": (w.get("summary") or "")[:120],
                    "score": w.get("score") or 0,
                }
            except Exception:
                pass

        # 价格收敛三角形（高点渐低 + 低点渐高）
        price_tri = {"forming": False, "status": "未检测", "label": "—", "breakout": "none"}
        if detect_price_triangle is not None:
            try:
                price_tri = detect_price_triangle(df)
            except Exception:
                pass

        # —— 斐波那契纳入时机层（中等权重）——
        # 纯触及权重低；「放量止跌 / 阳线反包」在关键位是较强时机，可作独立买侧路径
        # 九转仍是主路径；Fib 放量止跌为次主路径；两者叠加时置信最高
        fib_react_buy = bool(fib_buy is not None and fib_buy.reacted)
        fib_vol_stop = bool(
            fib_react_buy
            and fib_buy.reaction_signal in ("放量止跌", "阳线反包确认")
        )
        fib_txt = "无斐波那契买点"
        if fib_buy is not None:
            lv = f"0.{int(fib_buy.level * 1000)}"
            if fib_vol_stop:
                fib_txt = f"Fib{lv}{fib_buy.reaction_signal}"
            elif fib_react_buy:
                fib_txt = f"Fib{lv}有反应({fib_buy.reaction_signal or '确认'})"
            elif fib_buy.tested:
                fib_txt = f"Fib{lv}已触及未确认"
            else:
                fib_txt = f"Fib{lv}附近"

        # 收敛辅助（不单独定方向）
        conv_boost_buy = (conv_info["converging"] or price_tri.get("forming")) and nine_buy
        conv_boost_sell = (conv_info["converging"] or price_tri.get("forming")) and nine_sell

        # 买侧路径：
        # A 九转主路径：九转到位 + 量价支持
        # B Fib 次主路径：关键位放量止跌/反包 + 量价支持（可无九转）
        nine_path_buy = nine_buy and vol_ok_buy
        fib_path_buy = fib_vol_stop and vol_ok_buy
        timing_buy = nine_path_buy or fib_path_buy
        timing_sell = nine_sell and vol_ok_sell
        # 价格三角突破强化（需九转同向）
        if price_tri.get("breakout") == "up" and nine_buy and vol_ok_buy:
            timing_buy = True
            nine_path_buy = True
        if price_tri.get("breakout") == "down" and nine_sell and vol_ok_sell:
            timing_sell = True
        timing_pass = timing_buy or timing_sell
        # 仅 Fib 路径、无九转 → 仓位略折（见后）
        fib_led_only = bool(fib_path_buy and not nine_path_buy)

        if nt and nt.count > 0:
            nine_txt = f"{'下跌' if nt.direction=='down' else '上涨' if nt.direction=='up' else ''}九转{nt.count}（{nt.status}）"
        else:
            nine_txt = "无有效九转计数"
        vol_txt = vol_res.detail or vol_res.signal
        div_txt = vp_div.get("label") or "量价—"
        conv_txt = f"周线量能{conv_info['status']}" + (f"·{conv_info['tape']}" if conv_info.get("tape") else "")
        pt_txt = price_tri.get("label") or "价格三角—"
        if price_tri.get("forming"):
            pt_txt += f"（{price_tri.get('status')}）"
        if price_tri.get("breakout") in ("up", "down"):
            pt_txt += f"·已偏{'上' if price_tri['breakout']=='up' else '下'}破"
        if timing_buy:
            path_note = ""
            if nine_path_buy and fib_vol_stop:
                path_note = "（九转+Fib放量止跌·高置信）"
            elif fib_led_only:
                path_note = "（Fib放量止跌路径·中等权重）"
            elif conv_boost_buy:
                path_note = "（收敛辅助）"
            timing_status = (
                f"买侧确认：{nine_txt} + {vol_txt}；{div_txt}；{fib_txt}；{conv_txt}；{pt_txt}{path_note}"
            )
        elif timing_sell:
            extra = "（收敛辅助）" if conv_boost_sell else ""
            timing_status = f"卖侧确认：{nine_txt} + {vol_txt}；{div_txt}；{fib_txt}；{conv_txt}；{pt_txt}{extra}"
        else:
            parts = []
            if not (nine_buy or nine_sell or fib_vol_stop):
                parts.append(f"九转未到位（{nine_txt}）")
                if fib_buy and fib_buy.tested and not fib_react_buy:
                    parts.append(f"{fib_txt}（触及未放量确认，权重不足）")
            if nine_buy and not vol_ok_buy:
                parts.append(f"量价不支持买（{vol_txt} / {div_txt}）")
            if nine_sell and not vol_ok_sell:
                parts.append(f"量价不支持卖（{vol_txt} / {div_txt}）")
            if fib_vol_stop and not vol_ok_buy:
                parts.append(f"Fib已放量止跌但量价结构仍不支持（{div_txt}）")
            if conv_info["converging"]:
                parts.append(f"{conv_txt}（量能收束，需突破确认）")
            if price_tri.get("forming"):
                parts.append(f"{pt_txt}（整理压缩，方向看收盘突破）")
            if not parts:
                parts.append(f"{nine_txt}；{vol_txt}；{div_txt}；{fib_txt}；{conv_txt}；{pt_txt}")
            timing_status = "；".join(parts)

        # === 三层一致性检验 ===
        # 系统层：均线趋势为主 + 指标加权（非四票等权）
        sys_bull, sys_bear, sys_wdetail = self._system_pass(trend, trend_detail, indicators)

        # 工具层：斐波那契反应确认
        tool_confirmed = fib_buy is not None and fib_buy.reacted
        tool_tested = fib_buy is not None and fib_buy.tested

        # 震荡市：趋势不稳，时机层权重上调（九转/量价主导，趋势降权）
        is_range = trend == TrendType.RANGE
        # 工具层在震荡中可略放宽：已测试触及也算结构参考
        tool_ok_range = tool_confirmed or tool_tested

        # 系统层状态：区分「仅均线多头」与「系统层通过」
        if sys_bull or sys_bear:
            sys_status = f"趋势={trend.value}，{trend_detail}；{sys_wdetail}"
        elif trend == TrendType.BULL:
            sys_status = (
                f"均线已偏多（{trend_detail}），但加权指标未达阈值，系统层未通过；{sys_wdetail}"
                "。看板「趋势过滤」只反映均线方向，不等于系统层打勾。"
            )
        elif trend == TrendType.BEAR:
            sys_status = (
                f"均线已偏空（{trend_detail}），但加权指标未达阈值，系统层未通过；{sys_wdetail}"
                "。看板「趋势过滤」只反映均线方向，不等于系统层打勾。"
            )
        else:
            sys_status = (
                f"趋势={trend.value}，{trend_detail}；{sys_wdetail}"
                + ("；震荡市中趋势降权，以时机层为主" if is_range else "")
            )

        layers = {
            "系统层（趋势+指标）": {
                "通过": sys_bull or sys_bear,
                "状态": sys_status
            },
            "工具层（斐波那契反应）": {
                "通过": tool_confirmed if not is_range else tool_ok_range,
                "状态": f"买点确认={tool_confirmed}，" +
                        (f"0.{int(fib_buy.level*1000)}有反应({fib_buy.reaction_signal})"
                         if fib_buy else "无有效斐波那契买点") +
                        ("（已测试但未确认反应）" if tool_tested and not tool_confirmed else "") +
                        ("；震荡市结构放宽为「触及可参考」" if is_range and tool_tested and not tool_confirmed else "")
            },
            "时机层（九转+量价+Fib）": {
                "通过": timing_pass,
                "状态": timing_status + ("；震荡市权重优先" if is_range and timing_pass else "")
            },
        }

        # === 综合信号决策 ===
        all_pass = sys_bull and tool_confirmed and timing_buy
        # 震荡路径：时机买侧 + 结构参考（确认或已测试），不要求系统层多头
        range_timing_buy = is_range and timing_buy and tool_ok_range
        range_timing_sell = is_range and timing_sell
        # 藤本茂减仓：有持仓且阶梯触发时仍可提示卖出（仓位管理，非三层门槛）
        sell_trigger = position_delta < 0 and current_position_pct > 0
        range_mode = False  # 标记是否震荡时机主导（仓位打折）

        if all_pass:
            signal = SignalType.BUY if current_position_pct == 0 else SignalType.ADD
            ladder_note = f"；仓位参考：{fujimoto_desc}" if position_delta > 0 else ""
            action = f"三层一致 → {'初始建仓' if current_position_pct == 0 else '加仓'}（九转+量价确认）{ladder_note}"
        elif range_timing_buy:
            range_mode = True
            signal = SignalType.BUY if current_position_pct == 0 else SignalType.ADD
            action = (
                "震荡市·时机主导 → "
                + ("轻仓试探" if current_position_pct == 0 else "轻仓加仓")
                + "（九转+量价优先，趋势未稳，仓位折减）"
            )
        elif range_timing_sell and current_position_pct > 0:
            signal = SignalType.SELL
            action = "震荡市·时机卖侧确认，建议减仓/兑现"
        elif timing_sell and (sys_bear or tool_confirmed):
            signal = SignalType.SELL if current_position_pct > 0 else SignalType.WAIT
            action = "时机层卖侧确认" + ("，建议减仓" if current_position_pct > 0 else "，空仓观望")
        elif sell_trigger:
            signal = SignalType.SELL
            action = f"持仓阶梯减仓参考：{fujimoto_desc}"
        elif sys_bull and tool_confirmed and not timing_buy:
            signal = SignalType.HOLD if current_position_pct > 0 else SignalType.WAIT
            action = "趋势+结构到位，等待九转与量价确认"
        elif sys_bear:
            signal = SignalType.SELL if current_position_pct > 0 else SignalType.WAIT
            action = "空头趋势，" + ("减仓避险" if current_position_pct > 0 else "观望")
        elif is_range:
            signal = SignalType.WAIT
            action = "震荡市：趋势不稳，等待时机层（九转+量价）到位"
        else:
            signal = SignalType.WAIT
            action = "三层未完全一致，观望等待"

        # === 仓位与风控 ===
        position_pct = 0
        stop_loss = None
        entry_price = None
        risk_warning = ""

        if signal in (SignalType.BUY, SignalType.ADD):
            # ATR 动态仓位
            atr_val = atr_res.value if atr_res.value > 0 else close * 0.03
            risk_amount = self.total_capital * self.risk_per_trade
            position_pct = min(
                (risk_amount / (atr_val * 1.5)) / self.total_capital,
                self.max_position - current_position_pct
            )
            position_pct = max(position_pct, 0)
            # 若藤本茂给出加仓比例则取较小者；无建仓价/未触发阶梯时保留 ATR 仓位
            if position_delta > 0:
                position_pct = min(position_pct, position_delta)
            # 震荡市时机主导：趋势不稳，仓位折减约一半
            if range_mode:
                position_pct = position_pct * 0.5
                risk_warning = (risk_warning + "；" if risk_warning else "") + "震荡市轻仓（时机主导，仓位×0.5）"
            # 仅 Fib 放量止跌、无九转：中等权重，仓位约 70%
            elif fib_led_only:
                position_pct = position_pct * 0.7
                risk_warning = (risk_warning + "；" if risk_warning else "") + "Fib放量止跌路径（无九转，仓位×0.7）"

            entry_price = close
            stop_loss = close - 1.5 * atr_val

            if atr_res.signal == "看空":
                risk_warning = f"⚠ ATR过高({atr_res.detail})，建议降低仓位或暂停"

        elif signal == SignalType.SELL:
            position_pct = position_delta  # 负数，表示应卖出比例
            if current_position_pct > 0:
                risk_warning = f"当前持仓{current_position_pct*100:.0f}%，建议卖出{abs(position_delta)*100:.0f}%"

        elif signal == SignalType.HOLD:
            position_pct = 0
            risk_warning = "持有不动，让利润奔跑"

        else:  # WAIT
            position_pct = 0
            risk_warning = "观望为主，保留现金等待三层一致信号"

        # 数据有限提示（沙箱常只有十余根K线）
        if data_limited:
            note = f"⚠ 数据有限（仅{len(df)}根K线），指标偏短期，结论仅供参考"
            risk_warning = (risk_warning + "；" + note) if risk_warning else note

        # 图表数据
        chart_data = {
            "swing_high": swing_high,
            "swing_low": swing_low,
            "fib_levels": fib_levels,
            "target_prices": target_prices,
            "mas": {p: v for p, v in mas.items() if v is not None},
            "vwma": vwma,
            "volume_price_divergence": vp_div,
            "volume_convergence_timing": conv_info,
            "price_triangle": price_tri,
        }

        return StrategyResult(
            trend=trend,
            signal=signal,
            action=action,
            position_pct=position_pct,
            entry_price=entry_price,
            stop_loss=stop_loss,
            target_prices=target_prices,
            fib_levels=fib_levels,
            indicators=indicators,
            layers_consistent=layers,
            risk_warning=risk_warning,
            chart_data=chart_data
        )


# ================================================================
#  示例数据生成器（用于测试和演示）
# ================================================================

def generate_sample_data(days: int = 300, seed: int = 42) -> pd.DataFrame:
    """生成模拟K线数据"""
    np.random.seed(seed)
    dates = pd.bdate_range(start='2024-01-01', periods=days)

    # 模拟一段先跌后涨的行情
    base = 100
    trend = np.concatenate([
        np.linspace(0, -0.15, days // 3),    # 先跌15%
        np.linspace(-0.15, 0.05, days // 3), # 反弹
        np.linspace(0.05, 0.25, days - 2 * days // 3)  # 上涨25%
    ])
    noise = np.random.normal(0, 0.015, days)
    returns = np.diff(trend, prepend=0) + noise * 0.5

    prices = base * np.cumprod(1 + returns)

    df = pd.DataFrame({
        'date': dates,
        'open': prices * (1 + np.random.uniform(-0.005, 0.005, days)),
        'high': prices * (1 + np.abs(np.random.normal(0, 0.008, days))),
        'low': prices * (1 - np.abs(np.random.normal(0, 0.008, days))),
        'close': prices,
        'volume': np.random.randint(500000, 2000000, days).astype(float)
    })
    return df


if __name__ == "__main__":
    # 快速测试
    df = generate_sample_data(300)
    print(f"数据: {len(df)}根K线, 最新收盘={df['close'].iloc[-1]:.2f}")

    strategy = FujimotoStrategy(total_capital=100000, entry_price=df['close'].iloc[0])
    result = strategy.analyze(df, current_position_pct=0.3)

    print(f"\n{'='*60}")
    print(f"趋势: {result.trend.value}")
    print(f"信号: {result.signal.value}")
    print(f"操作: {result.action}")
    print(f"建议仓位: {result.position_pct*100:.1f}%")
    if result.entry_price:
        print(f"入场价: {result.entry_price:.2f}")
    if result.stop_loss:
        print(f"止损价: {result.stop_loss:.2f}")
    if result.target_prices:
        print(f"目标价: {[f'{t:.2f}' for t in result.target_prices]}")

    print(f"\n--- 指标 ---")
    for ind in result.indicators:
        print(f"  {ind.name}: {ind.detail}")

    print(f"\n--- 斐波那契 ---")
    for fl in result.fib_levels:
        print(f"  {fl.level:.3f} @ {fl.price:.2f}  测试={fl.tested}  反应={fl.reacted}  {fl.reaction_signal}")

    print(f"\n--- 三层一致性 ---")
    for layer, info in result.layers_consistent.items():
        status = "✓" if info["通过"] else "✗"
        print(f"  {status} {layer}: {info['状态']}")

    print(f"\n风控提示: {result.risk_warning}")
