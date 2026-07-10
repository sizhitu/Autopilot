"""
藤本茂融合策略 - 回测引擎
=================================
基于策略引擎，模拟历史交易，评估策略表现。

核心逻辑：
  1. 遍历历史K线，逐日调用策略引擎分析
  2. 根据三层信号模拟建仓/加仓/减仓
  3. 记录每笔交易、资金曲线、回撤曲线
  4. 计算收益率、最大回撤、胜率、夏普比率等指标
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

    def run(self, df: pd.DataFrame, entry_price: Optional[float] = None) -> BacktestResult:
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

        trades: List[Trade] = []
        equity_curve = []
        drawdown_curve = []
        position_curve = []
        peak_equity = self.initial_capital

        # 用于计算平均持仓天数
        buy_dates = []
        last_trade_bar = -999  # 冷却期控制
        cooldown = 3            # 交易冷却K线数
        bear_trend_sold = False  # 空头趋势是否已减仓（避免反复触发）
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
                entry_price=first_price
            )
            result = strategy.analyze(current_df, current_position_pct=position_pct)

            # 从策略结果中提取信号
            action = None
            trade_shares = 0
            trade_reason = result.action[:50]

            # --- 卖出逻辑（优先执行）---
            # 1. 藤本茂阶梯触发卖出
            # 2. 空头趋势 + 持仓 > 0
            price_change = (close - first_price) / first_price if first_price > 0 else 0

            sell_signal = False
            sell_pct = 0

            # 藤本茂阶梯卖出（最高优先级，不受冷却限制）
            if price_change >= 0.25 and shares > 0:
                _, delta = strategy._fujimoto_action(price_change, position_pct)
                if delta < 0:
                    sell_signal = True
                    sell_pct = min(abs(delta), 1.0)

            # 空头趋势减仓（需冷却，且每次空头趋势只减一次）
            elif result.trend == TrendType.BEAR and shares > 0 and \
                 not bear_trend_sold and (i - last_trade_bar) >= cooldown:
                sell_signal = True
                sell_pct = 0.3  # 减仓30%
                trade_reason = "空头趋势减仓30%"
                bear_trend_sold = True

            # RSI极端超买（需冷却）
            elif shares > 0 and (i - last_trade_bar) >= cooldown:
                for ind in result.indicators:
                    if ind.name == "RSI" and ind.value > 80:
                        sell_signal = True
                        sell_pct = 0.2
                        trade_reason = f"RSI={ind.value:.0f}超买减仓20%"
                        break

            # 趋势转多时重置空头标记
            if result.trend == TrendType.BULL:
                bear_trend_sold = False

            if sell_signal and shares > 0:
                trade_shares = shares * sell_pct
                if trade_shares > 0:
                    proceeds = trade_shares * close * (1 - self.commission)
                    cash += proceeds
                    shares -= trade_shares
                    position_pct = max(0, position_pct - sell_pct)
                    action = "SELL"
                    last_trade_bar = i

            # --- 买入逻辑 ---
            # 1. 趋势多头 + 斐波那契有反应确认 → 初始建仓
            # 2. 趋势多头 + 价格回撤到斐波那契位 → 加仓
            # 3. 藤本茂阶梯触发加仓
            elif position_pct < self.max_position and (i - last_trade_bar) >= cooldown:
                buy_signal = False
                buy_pct = 0

                # 初始建仓：趋势多头 + 斐波那契确认
                if position_pct == 0 and result.trend == TrendType.BULL:
                    # 检查是否有斐波那契反应确认
                    fib_confirmed = any(fl.reacted for fl in result.fib_levels)
                    if fib_confirmed:
                        buy_signal = True
                        buy_pct = 0.20  # 初始20%仓位
                        trade_reason = "多头趋势+斐波那契确认建仓"
                    # 或者 RSI 超卖反弹
                    elif any(ind.name == "RSI" and ind.value < 35 for ind in result.indicators):
                        buy_signal = True
                        buy_pct = 0.15
                        trade_reason = "RSI超卖反弹建仓"

                # 加仓：已持仓 + 藤本茂阶梯触发
                elif position_pct > 0 and price_change < 0:
                    _, delta = strategy._fujimoto_action(price_change, position_pct)
                    if delta > 0:
                        buy_signal = True
                        buy_pct = min(delta, self.max_position - position_pct)
                        trade_reason = f"藤本茂加仓(跌幅{price_change*100:.1f}%)"

                # 加仓：趋势多头 + RSI从超卖回升
                elif position_pct > 0 and position_pct < self.max_position * 0.6:
                    for ind in result.indicators:
                        if ind.name == "RSI" and 30 < ind.value < 45 and result.trend == TrendType.BULL:
                            buy_signal = True
                            buy_pct = 0.10
                            trade_reason = f"RSI={ind.value:.0f}低位加仓"

                if buy_signal and buy_pct > 0.01:
                    invest = self.initial_capital * buy_pct
                    trade_shares = invest / close
                    cost = trade_shares * close * (1 + self.commission)
                    if cost <= cash:
                        cash -= cost
                        shares += trade_shares
                        position_pct += buy_pct
                        action = "BUY" if position_pct == buy_pct else "ADD"
                        buy_dates.append(i)
                        last_trade_bar = i

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
