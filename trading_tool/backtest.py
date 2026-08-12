"""
藤本茂融合策略 - 回测引擎
=================================
基于策略引擎，模拟历史交易，评估策略表现。

核心逻辑：
  1. 遍历历史K线，逐日调用策略引擎分析
  2. 交易以策略 signal 为准：观望/等待不交易；三层一致才开/加仓；卖出原因与动作一致
  3. 三层价值侧重：识别下跌与风险、减少逆势亏损；顺势加仓用藤本茂阶梯放大收益
  4. 记录交易、资金曲线、回撤与收益风险指标
"""

import sys
import os
import numpy as np
import pandas as pd
from datetime import datetime
from dataclasses import dataclass, field
from typing import List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strategy_engine import FujimotoStrategy, SignalType, TrendType
try:
    from nine_turn import calc_nine_turn_display
except Exception:
    calc_nine_turn_display = None


@dataclass
class Trade:
    """单笔交易记录"""
    date: str
    action: str        # "BUY" / "SELL" / "ADD"
    price: float
    shares: float
    amount: float
    reason: str


@dataclass
class BacktestResult:
    """回测结果"""
    # 统计指标
    total_return: float = 0          # 总收益率%
    annual_return: float = 0         # 年化收益率%
    max_drawdown: float = 0          # 最大回撤%
    win_rate: float = 0              # 胜率%
    sharpe_ratio: float = 0          # 夏普比率
    total_trades: int = 0            # 总交易次数
    buy_trades: int = 0
    sell_trades: int = 0
    avg_hold_days: float = 0         # 平均持仓天数

    # 曲线数据
    equity_curve: list = field(default_factory=list)      # 资金曲线
    drawdown_curve: list = field(default_factory=list)    # 回撤曲线
    trades: list = field(default_factory=list)            # 交易记录
    position_curve: list = field(default_factory=list)    # 持仓比例曲线

    # 对比
    buy_hold_return: float = 0       # 买入持有收益率%
    excess_return: float = 0         # 超额收益%

    # 配置
    config: dict = field(default_factory=dict)


class Backtester:
    """回测引擎"""

    def __init__(self, initial_capital: float = 100000,
                 risk_per_trade: float = 0.02,
                 max_position: float = 0.70,
                 commission: float = 0.0003,  # 手续费万三
                 warmup: int = 60):
        """
        Args:
            initial_capital: 初始资金
            risk_per_trade: 单笔风险
            max_position: 最大仓位
            commission: 手续费率
            warmup: 预热期（前N根K线不交易，用于计算均线/指标）
        """
        self.initial_capital = initial_capital
        self.risk_per_trade = risk_per_trade
        self.max_position = max_position
        self.commission = commission
        self.warmup = warmup

    def run(self, df: pd.DataFrame, entry_price: Optional[float] = None,
            initial_position_pct: float = 0.0) -> BacktestResult:
        """
        执行回测

        Args:
            df: OHLCV 数据
            entry_price: 初始建仓参考价（None则自动用第一根K线收盘价）
        Returns:
            BacktestResult
        """
        df = df.copy().reset_index(drop=True)
        n = len(df)

        if n < self.warmup + 30:
            return BacktestResult(
                config={"error": f"数据不足: 需要{self.warmup+30}根，实际{n}根"}
            )

        # 初始化状态
        cash = self.initial_capital
        shares = 0.0
        position_pct = 0.0
        first_price = entry_price or df['close'].iloc[self.warmup]
        # 工具栏「持仓%」：作为起始仓位种子（0~max_position）
        ip0 = float(initial_position_pct or 0.0)
        if ip0 > 1.0:
            ip0 = ip0 / 100.0
        ip0 = max(0.0, min(ip0, self.max_position))
        if ip0 > 0.001 and first_price > 0:
            inv0 = self.initial_capital * ip0
            shares = inv0 / first_price
            cash = self.initial_capital - inv0
            position_pct = ip0

        trades: List[Trade] = []
        equity_curve = []
        drawdown_curve = []
        position_curve = []
        peak_equity = self.initial_capital

        # 用于计算平均持仓天数
        buy_dates = []
        last_trade_bar = -999
        last_buy_bar = -999     # 最近一次开/加仓
        cooldown = 10           # 操作冷却（减少频繁交易）
        min_hold_bars = 15      # 开/加仓后最少持有才允许兑现
        bear_trend_sold = False
        sold_in_bear = False
        ladder_sold_steps = set()
        avg_cost = float(first_price) if first_price else 0.0  # 持仓成本
        last_add_price = 0.0  # 上次加仓价，防止同价±5%连加
        last_trend = None

        for i in range(self.warmup, n):
            current_df = df.iloc[:i+1]
            close = df['close'].iloc[i]
            date_str = df['date'].iloc[i] if 'date' in df.columns else str(i)
            if hasattr(date_str, 'strftime'):
                date_str = date_str.strftime('%Y-%m-%d')

            # 调用策略引擎
            strategy = FujimotoStrategy(
                total_capital=self.initial_capital,
                risk_per_trade=self.risk_per_trade,
                max_position=self.max_position,
                entry_price=(avg_cost if shares > 0 and avg_cost > 0 else first_price)
            )
            result = strategy.analyze(current_df, current_position_pct=position_pct)

            # --- 交易决策：降频 + 禁高位加仓 + 原因与操作一致 ---
            action = None
            trade_shares = 0
            trade_reason = ""
            sig = result.signal
            action_txt = (result.action or "")[:80]
            layers = getattr(result, "layers_consistent", None) or {}
            all_pass = False
            try:
                all_pass = bool(
                    (layers.get("系统层（趋势+指标）") or layers.get("系统层") or {}).get("通过")
                    and (layers.get("工具层（斐波那契）") or layers.get("工具层") or {}).get("通过")
                    and (layers.get("时机层（九转序列）") or layers.get("时机层") or {}).get("通过")
                )
            except Exception:
                all_pass = False
            if not all_pass and "三层一致" in action_txt:
                all_pass = True

            # 相对成本的盈亏（阶梯/加仓一律用均价，避免高位加仓后仍按旧低成本「上涨45%卖出」）
            cost_basis = avg_cost if (shares > 0 and avg_cost > 0) else first_price
            pnl_from_cost = (close - cost_basis) / cost_basis if cost_basis and cost_basis > 0 else 0.0
            # 近20日高点：用于禁止追高加仓
            win = current_df['close'].iloc[-min(20, len(current_df)):]
            recent_high = float(win.max()) if len(win) else close
            near_high = recent_high > 0 and close >= recent_high * 0.97

            if result.trend == TrendType.BULL:
                bear_trend_sold = False
                sold_in_bear = False

            cooled = (i - last_trade_bar) >= cooldown
            can_sell = shares > 0 and cooled and (i - last_buy_bar) >= min_hold_bars

            # 1) 观望：不交易
            if sig == SignalType.WAIT and not all_pass:
                pass

            # 2) 卖出纪律（针对「越改越低」）：
            #    - 浮亏一律不卖（不割肉、不连续止损）
            #    - 禁止「时机卖侧」在成本附近反复减仓
            #    - 空头若曾有浮盈：最多一次性清仓一次；浮亏空头则继续持有等加仓
            #    - 大浮盈才分批小幅兑现，留主力吃趋势
            elif can_sell:
                sell_pct = 0.0
                do_sell = False

                # 铁律：相对成本浮亏或几乎无盈利 → 不卖
                if pnl_from_cost < 0.08:
                    do_sell = False
                elif all_pass or result.trend == TrendType.BULL:
                    # 多头/三层一致：仅大浮盈兑现一小部分
                    if pnl_from_cost >= 0.50:
                        step = round(pnl_from_cost * 5) / 5.0  # 20% 一档
                        if step not in ladder_sold_steps:
                            do_sell = True
                            sell_pct = 0.12
                            trade_reason = f"趋势浮盈分批兑现({pnl_from_cost*100:.0f}%)"
                            ladder_sold_steps.add(step)
                else:
                    # 非多头：有浮盈时，空头阶段最多「一次性」退出，避免 24→26→20 连砍
                    if result.trend == TrendType.BEAR and not sold_in_bear and pnl_from_cost >= 0.08:
                        do_sell = True
                        sell_pct = 1.0  # 一次性出清，不再分批割
                        trade_reason = f"空头一次退出(浮盈{pnl_from_cost*100:.0f}%)"
                        sold_in_bear = True
                        bear_trend_sold = True
                    elif pnl_from_cost >= 0.50 and "阶梯" not in action_txt:
                        # 非多头但大浮盈：小幅兑现一次
                        step = round(pnl_from_cost * 5) / 5.0
                        if step not in ladder_sold_steps:
                            do_sell = True
                            sell_pct = 0.15
                            trade_reason = f"非趋势浮盈兑现({pnl_from_cost*100:.0f}%)"
                            ladder_sold_steps.add(step)
                    # 明确忽略：震荡时机卖侧、策略反复 SELL、浮亏空头减仓

                if do_sell and sell_pct > 0:
                    trade_shares = shares * min(sell_pct, 1.0)
                    if trade_shares > 0:
                        proceeds = trade_shares * close * (1 - self.commission)
                        cash += proceeds
                        shares -= trade_shares
                        position_pct = max(0.0, position_pct * (1.0 - min(sell_pct, 1.0)))
                        if shares < 1e-8 or position_pct < 0.005:
                            shares = 0.0
                            position_pct = 0.0
                            first_price = close
                            avg_cost = close
                            ladder_sold_steps = set()
                        action = "SELL"
                        last_trade_bar = i

            # 3) 买/加仓：空仓可用三层建仓；已有仓默认持有，仅九转买点或成本下/深回调才加
            if action is None and cooled and sig != SignalType.WAIT:
                buy_pct = 0.0
                do_buy = False
                is_flat = position_pct < 0.01

                # 九转下跌买侧（完成或临近）
                nt_buy = False
                try:
                    if calc_nine_turn_display is not None:
                        nt = calc_nine_turn_display(current_df)
                        nt_buy = bool(
                            nt.get("direction") == "down"
                            and (nt.get("is_complete") or nt.get("is_completing"))
                        )
                except Exception:
                    nt_buy = False

                below_cost = cost_basis > 0 and close <= cost_basis * 0.98
                deep_dip = pnl_from_cost <= -0.12  # 相对成本跌超 12%
                off_high = recent_high > 0 and close <= recent_high * 0.90
                # 与上次加仓价相差至少 5%（避免同价带连加）
                price_gap_ok = (last_add_price <= 0) or (abs(close - last_add_price) / last_add_price >= 0.05)

                if is_flat:
                    # 仅三层一致才建仓；震荡轻仓试探等一律观望不买
                    if all_pass:
                        do_buy = True
                        # 仓位受 max_position 约束，默认用上限的一部分，避免「轻仓」却很大
                        buy_pct = min(float(result.position_pct or 0) or (self.max_position * 0.5), self.max_position)
                        buy_pct = max(min(buy_pct, self.max_position), min(0.1, self.max_position))
                        trade_reason = "三层一致关注买入·建仓"
                else:
                    # 已有仓：三层一致 ≠ 自动加仓；须买点结构或成本下加仓
                    allow_add = price_gap_ok and (i - last_buy_bar) >= cooldown
                    if near_high and not below_cost:
                        allow_add = False
                    structural_add = nt_buy or below_cost or (deep_dip and off_high)
                    if allow_add and structural_add and (all_pass or nt_buy):
                        do_buy = True
                        buy_pct = min(float(result.position_pct or 0) or 0.08, 0.12)
                        if nt_buy:
                            trade_reason = "九转下跌买点加仓"
                        elif below_cost or deep_dip:
                            desc, delta = strategy._fujimoto_action(pnl_from_cost, position_pct)
                            if delta > 0:
                                buy_pct = max(buy_pct, min(delta, 0.15))
                            trade_reason = f"成本下/深回调加仓" + (f"：{desc}" if delta > 0 else "")
                        else:
                            trade_reason = "结构回调加仓"
                    # 否则：三层一致持有 → 不加不卖（让利润跑）

                if do_buy and buy_pct > 0.01 and cash > 0:
                    room = max(0.0, self.max_position - position_pct)
                    buy_pct = min(buy_pct, room)
                    invest = min(self.initial_capital * buy_pct, cash * 0.99)
                    if invest > 1 and buy_pct > 0.01:
                        trade_shares = invest / close
                        cost = trade_shares * close * (1 + self.commission)
                        if cost <= cash:
                            if shares > 0:
                                avg_cost = (avg_cost * shares + close * trade_shares) / (shares + trade_shares)
                            else:
                                avg_cost = close
                                first_price = close
                                ladder_sold_steps = set()
                            cash -= cost
                            shares += trade_shares
                            was_flat = is_flat
                            position_pct = min(self.max_position, position_pct + buy_pct)
                            action = "BUY" if was_flat else "ADD"
                            buy_dates.append(i)
                            last_trade_bar = i
                            last_buy_bar = i
                            last_add_price = close

            if action:
                trades.append(Trade(
                    date=date_str,
                    action=action,
                    price=round(close, 2),
                    shares=round(trade_shares, 2),
                    amount=round(trade_shares * close, 2),
                    reason=trade_reason
                ))

            # 记录每日权益
            equity = cash + shares * close
            equity_curve.append({
                "date": date_str,
                "equity": round(equity, 2),
                "close": round(close, 2),
            })

            if equity > peak_equity:
                peak_equity = equity
            drawdown = (equity - peak_equity) / peak_equity * 100 if peak_equity > 0 else 0
            drawdown_curve.append({
                "date": date_str,
                "drawdown": round(drawdown, 2)
            })

            position_curve.append({
                "date": date_str,
                "position": round(position_pct * 100, 1)
            })

        # 最终结算
        final_equity = cash + shares * df['close'].iloc[-1]
        total_return = (final_equity - self.initial_capital) / self.initial_capital * 100

        # 买入持有收益
        buy_hold_shares = self.initial_capital / first_price * (1 - self.commission)
        buy_hold_final = buy_hold_shares * df['close'].iloc[-1] * (1 - self.commission)
        buy_hold_return = (buy_hold_final - self.initial_capital) / self.initial_capital * 100

        # 年化收益率
        trading_days = len(equity_curve)
        years = trading_days / 252
        annual_return = ((final_equity / self.initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0

        # 最大回撤
        max_dd = min(d['drawdown'] for d in drawdown_curve) if drawdown_curve else 0

        # 胜率：卖出时是否盈利
        sell_trades = [t for t in trades if t.action == "SELL"]
        buy_trades = [t for t in trades if t.action in ("BUY", "ADD")]
        wins = 0
        total_sells = 0
        avg_cost = 0
        total_shares_held = 0

        # 简化胜率计算：每次卖出对比加权平均成本
        for t in trades:
            if t.action in ("BUY", "ADD"):
                if total_shares_held > 0:
                    avg_cost = (avg_cost * total_shares_held + t.price * t.shares) / (total_shares_held + t.shares)
                else:
                    avg_cost = t.price
                total_shares_held += t.shares
            elif t.action == "SELL":
                if total_shares_held > 0:
                    total_sells += 1
                    if t.price > avg_cost:
                        wins += 1
                    total_shares_held -= t.shares
                    if total_shares_held <= 0:
                        total_shares_held = 0
                        avg_cost = 0

        win_rate = (wins / total_sells * 100) if total_sells > 0 else 0

        # 夏普比率（日收益率）
        equities = [d['equity'] for d in equity_curve]
        if len(equities) > 1:
            daily_returns = [(equities[i] - equities[i-1]) / equities[i-1]
                            for i in range(1, len(equities)) if equities[i-1] > 0]
            if daily_returns:
                mean_ret = np.mean(daily_returns)
                std_ret = np.std(daily_returns)
                sharpe = (mean_ret / std_ret * np.sqrt(252)) if std_ret > 0 else 0
            else:
                sharpe = 0
        else:
            sharpe = 0

        # 平均持仓天数
        hold_days_list = []
        if len(buy_dates) >= 2:
            for j in range(1, len(buy_dates)):
                hold_days_list.append(buy_dates[j] - buy_dates[j-1])
        avg_hold = np.mean(hold_days_list) if hold_days_list else 0

        return BacktestResult(
            total_return=round(total_return, 2),
            annual_return=round(annual_return, 2),
            max_drawdown=round(abs(max_dd), 2),
            win_rate=round(win_rate, 1),
            sharpe_ratio=round(sharpe, 2),
            total_trades=len(trades),
            buy_trades=len(buy_trades),
            sell_trades=len(sell_trades),
            avg_hold_days=round(avg_hold, 1),
            equity_curve=equity_curve,
            drawdown_curve=drawdown_curve,
            trades=[{
                "date": t.date, "action": t.action, "price": t.price,
                "shares": t.shares, "amount": t.amount, "reason": t.reason
            } for t in trades],
            position_curve=position_curve,
            buy_hold_return=round(buy_hold_return, 2),
            excess_return=round(total_return - buy_hold_return, 2),
            config={
                "initial_capital": self.initial_capital,
                "risk_per_trade": self.risk_per_trade,
                "max_position": self.max_position,
                "commission": self.commission,
                "warmup": self.warmup,
                "start_date": equity_curve[0]["date"] if equity_curve else "",
                "end_date": equity_curve[-1]["date"] if equity_curve else "",
                "trading_days": trading_days,
                "final_equity": round(final_equity, 2),
            }
        )


def result_to_dict(result: BacktestResult) -> dict:
    """转JSON"""
    return {
        "total_return": result.total_return,
        "annual_return": result.annual_return,
        "max_drawdown": result.max_drawdown,
        "win_rate": result.win_rate,
        "sharpe_ratio": result.sharpe_ratio,
        "total_trades": result.total_trades,
        "buy_trades": result.buy_trades,
        "sell_trades": result.sell_trades,
        "avg_hold_days": result.avg_hold_days,
        "equity_curve": result.equity_curve,
        "drawdown_curve": result.drawdown_curve,
        "trades": result.trades,
        "position_curve": result.position_curve,
        "buy_hold_return": result.buy_hold_return,
        "excess_return": result.excess_return,
        "config": result.config,
    }


# ================================================================
#  测试
# ================================================================
if __name__ == "__main__":
    from data_fetcher import DataFetcher

    fetcher = DataFetcher()

    print("=== 回测: A股 600519 贵州茅台 ===")
    df = fetcher.fetch('600519', 300)
    print(f"数据: {len(df)}根K线, {df.iloc[0]['date'].strftime('%Y-%m-%d')} ~ {df.iloc[-1]['date'].strftime('%Y-%m-%d')}")

    bt = Backtester(initial_capital=100000, warmup=60)
    result = bt.run(df)

    print(f"\n总收益率: {result.total_return}%")
    print(f"年化收益: {result.annual_return}%")
    print(f"最大回撤: {result.max_drawdown}%")
    print(f"夏普比率: {result.sharpe_ratio}")
    print(f"胜率: {result.win_rate}%")
    print(f"总交易: {result.total_trades}次 (买{result.buy_trades}/卖{result.sell_trades})")
    print(f"平均持仓: {result.avg_hold_days}天")
    print(f"买入持有: {result.buy_hold_return}%")
    print(f"超额收益: {result.excess_return}%")

    print(f"\n--- 交易记录(前10笔) ---")
    for t in result.trades[:10]:
        print(f"  {t['date']} {t['action']:4s} @{t['price']:.2f} x{t['shares']:.1f} = {t['amount']:.0f} | {t['reason']}")
