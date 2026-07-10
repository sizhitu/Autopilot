"""
藤本茂交易哲学融合策略引擎
=======================================
三层一体：心法层(藤本茂阶梯) + 工具层(斐波那契) + 系统层(九转均线+多指标)

独立模块，不含 GUI，可被任何前端调用。
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional
from enum import Enum


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

    MA_PERIODS = [5, 10, 20, 30, 50, 100, 150, 200, 250]

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

    def _judge_trend(self, mas: dict, vwma: Optional[float], close: float,
                     short_periods: list, long_periods: list) -> tuple:
        """自适应判断趋势（根据可用均线周期）"""
        short_mas = [mas.get(p) for p in short_periods]
        long_mas = [mas.get(p) for p in long_periods]

        short_valid = len(short_mas) >= 2  # 至少需要2条短期均线判断排列
        long_valid = len(long_mas) >= 2

        # 检查均线是否纠缠（短期均线差异小）——短期均线不足3条时不判断纠缠
        if short_valid and len(short_mas) >= 3:
            short_vals = short_mas
            spread = (max(short_vals) - min(short_vals)) / close
            if spread < 0.01:  # 均线差异<1%，视为纠缠
                return TrendType.RANGE, "短期均线纠缠（差异<1%），趋势不明"

        if short_valid:
            short_bull = all(short_mas[i] > short_mas[i+1]
                             for i in range(len(short_mas)-1))
            short_bear = all(short_mas[i] < short_mas[i+1]
                             for i in range(len(short_mas)-1))
        else:
            short_bull = short_bear = False

        long_up = False
        long_down = False
        if long_valid:
            long_up = long_mas[0] is not None and long_mas[-1] is not None and \
                       all(m is not None for m in long_mas) and \
                       long_mas[0] > long_mas[-1]
            long_down = long_mas[0] is not None and long_mas[-1] is not None and \
                         all(m is not None for m in long_mas) and \
                         long_mas[0] < long_mas[-1]

        # VWMA 确认
        vwma_bull = vwma is not None and close > vwma
        vwma_bear = vwma is not None and close < vwma

        if short_bull and (long_up or not long_valid) and vwma_bull:
            note = "" if long_valid else "（长期数据不足）"
            return TrendType.BULL, "短期多头排列+VWMA确认" + note
        elif short_bear and (long_down or not long_valid) and vwma_bear:
            note = "" if long_valid else "（长期数据不足）"
            return TrendType.BEAR, "短期空头排列+VWMA确认" + note
        else:
            return TrendType.RANGE, "均线排列混乱，趋势不明"

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
    #  心法层：藤本茂阶梯
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
        trend, trend_detail = self._judge_trend(mas, vwma, close, short_periods, long_periods)

        rsi_res = self._calc_rsi(df)
        macd_res = self._calc_macd(df)
        atr_res = self._calc_atr(df)
        vol_res = self._calc_volume_signal(df)
        indicators = [rsi_res, macd_res, atr_res, vol_res]

        # === 工具层 ===
        fib_result = self._calc_fibonacci(df)
        if len(fib_result) == 4:
            fib_levels, swing_high, swing_low, target_prices = fib_result
        else:
            fib_levels, swing_high, swing_low, target_prices = [], close, close, []

        fib_buy = self._find_fib_buy_point(fib_levels, close)

        # === 心法层 ===
        price_change = 0
        if self.entry_price and self.entry_price > 0:
            price_change = (close - self.entry_price) / self.entry_price
        fujimoto_desc, position_delta = self._fujimoto_action(price_change, current_position_pct)

        # === 三层一致性检验 ===
        # 系统层：趋势+指标
        sys_signals = [i.signal for i in indicators]
        sys_bull = trend == TrendType.BULL and \
                   sys_signals.count("看多") >= 2 and \
                   "看空" not in sys_signals
        sys_bear = trend == TrendType.BEAR and \
                   sys_signals.count("看空") >= 2 and \
                   "看多" not in sys_signals

        # 工具层：斐波那契反应确认
        tool_confirmed = fib_buy is not None and fib_buy.reacted
        tool_tested = fib_buy is not None and fib_buy.tested

        # 心法层：藤本茂触发
        mind_trigger = position_delta != 0

        layers = {
            "系统层（趋势+指标）": {
                "通过": sys_bull or sys_bear,
                "状态": f"趋势={trend.value}，{trend_detail}；" +
                        "；".join([f"{i.name}={i.signal}" for i in indicators])
            },
            "工具层（斐波那契反应）": {
                "通过": tool_confirmed,
                "状态": f"买点确认={tool_confirmed}，" +
                        (f"0.{int(fib_buy.level*1000)}有反应({fib_buy.reaction_signal})"
                         if fib_buy else "无有效斐波那契买点") +
                        ("（已测试但未确认反应）" if tool_tested and not tool_confirmed else "")
            },
            "心法层（藤本茂阶梯）": {
                "通过": mind_trigger,
                "状态": f"涨跌幅={price_change*100:+.1f}%，{fujimoto_desc}"
            },
        }

        # === 综合信号决策 ===
        all_pass = sys_bull and tool_confirmed and mind_trigger and position_delta > 0
        sell_trigger = position_delta < 0

        if all_pass:
            signal = SignalType.BUY if current_position_pct == 0 else SignalType.ADD
            action = f"三层一致 → {'初始建仓' if current_position_pct == 0 else '加仓'}：{fujimoto_desc}"
        elif sell_trigger:
            signal = SignalType.SELL
            action = f"触发藤本茂减仓：{fujimoto_desc}"
        elif sys_bull and tool_confirmed and not mind_trigger:
            signal = SignalType.HOLD
            action = "趋势+斐波那契确认，但未触发藤本茂阶梯，持有等待"
        elif sys_bear:
            signal = SignalType.SELL if current_position_pct > 0 else SignalType.WAIT
            action = "空头趋势，" + ("减仓避险" if current_position_pct > 0 else "观望")
        elif trend == TrendType.RANGE:
            signal = SignalType.WAIT
            action = "震荡市，三层不一致，观望等待"
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
            position_pct = min(position_pct, position_delta if position_delta > 0 else position_pct)

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
