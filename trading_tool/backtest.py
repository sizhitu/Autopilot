"""
藤本茂融合策略 - 回测引擎
=================================
基于策略引擎，模拟历史交易，评估策略表现。

核心逻辑：
  1. 遍历历史K线，逐日调用策略引擎分析
  2. 交易以策略 signal 为准：观望/等待不交易；三层一致才开/加仓；卖出原因与动作一致
  3. 加减仓偏好藤本茂第二档：跌约25%加约25%；涨约35%减约20%；三层一致才开仓
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
        trades: List[Trade] = []
        seed_bar = self.warmup
        if ip0 > 0.001 and first_price > 0:
            inv0 = self.initial_capital * ip0
            shares = inv0 / first_price
            cash = self.initial_capital - inv0
            position_pct = ip0
            avg_cost_seed = float(first_price)
            # 写入初始 BUY，便于交易记录完整（价格=建仓价，金额=回测资金×持仓%）
            d0 = df['date'].iloc[seed_bar] if 'date' in df.columns else str(seed_bar)
            if hasattr(d0, 'strftime'):
                d0 = d0.strftime('%Y-%m-%d')
            trades.append(Trade(
                date=str(d0),
                action="BUY",
                price=round(float(first_price), 2),
                shares=round(shares, 2),
                amount=round(shares * float(first_price), 2),
                reason=f"建仓价入场·持仓{ip0*100:.0f}%（待三层/九转信号后再加仓）"
            ))
        equity_curve = []
        drawdown_curve = []
        position_curve = []
        peak_equity = self.initial_capital

        # 用于计算平均持仓天数
        buy_dates = [seed_bar] if position_pct > 0.001 else []
        last_trade_bar = seed_bar if position_pct > 0.001 else -999
        last_buy_bar = seed_bar if position_pct > 0.001 else -999
        cooldown = 10           # 操作冷却（减少频繁交易）
        min_hold_bars = 15      # 开/加仓后最少持有才允许兑现
        bear_trend_sold = False
        sold_in_bear = False
        ladder_sold_steps = set()
        avg_cost = float(first_price) if first_price else 0.0  # 持仓成本
        last_add_price = 0.0  # 上次加仓价，防止同价±5%连加
        last_trend = None

        for i in range(self.warmup, n):
            # 仅用最近窗口做策略分析，显著降低回测耗时，避免网关 Failed to fetch 超时
            _w0 = max(0, i + 1 - max(self.warmup + 40, 180))
            current_df = df.iloc[_w0:i+1].reset_index(drop=True)
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
            sys_ok = tool_ok = time_ok = False
            try:
                def _layer_ok(substrs):
                    for k, v in (layers or {}).items():
                        if not isinstance(v, dict):
                            continue
                        if any(s in str(k) for s in substrs) and v.get("通过"):
                            return True
                    return False
                sys_ok = _layer_ok(("系统层",))
                tool_ok = _layer_ok(("工具层", "斐波那契"))
                time_ok = _layer_ok(("时机层", "九转"))
                all_pass = bool(sys_ok and tool_ok and time_ok)
            except Exception:
                all_pass = False
            if not all_pass and "三层一致" in action_txt:
                all_pass = True
            # 宽松建仓：九转/时机层、系统+工具、策略买侧信号均可（不要求三层全过）
            high_weight_buy = bool(
                all_pass
                or time_ok
                or (sys_ok and tool_ok)
                or (sys_ok and time_ok)
                or (tool_ok and time_ok)
                or (sig in (SignalType.BUY, SignalType.ADD) and any(
                    k in action_txt for k in ("买入", "加仓", "建仓", "九转", "三层", "时机主导", "试探")
                ))
            )

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

            # 2) 减仓：藤本茂第二档为主（相对成本 +35% 卖约 20%）；浮亏不卖；忽略时机卖侧连砍
            elif can_sell:
                sell_pct = 0.0
                do_sell = False

                if pnl_from_cost < 0.08:
                    do_sell = False
                elif pnl_from_cost >= 0.35 and 0.35 not in ladder_sold_steps:
                    # 第二档：上涨约 35% 卖出约 20%（只触发一次）
                    do_sell = True
                    sell_pct = 0.20
                    trade_reason = "藤本茂第二档减仓：上涨35%卖出20%"
                    ladder_sold_steps.add(0.35)
                elif pnl_from_cost >= 0.60 and 0.60 not in ladder_sold_steps:
                    # 更深档可选：大趋势末端再减一成，避免过早清空
                    do_sell = True
                    sell_pct = 0.15
                    trade_reason = "藤本茂加深兑现：上涨60%再减15%"
                    ladder_sold_steps.add(0.60)
                # 不再：+25%第一档、震荡时机卖侧、浮亏空头分批割

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
            if action is None and cooled and (sig != SignalType.WAIT or high_weight_buy or all_pass):
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
                    # 九转买入、时机层、或三层中任意高权重组合通过即可建仓
                    nt_entry = False
                    try:
                        if calc_nine_turn_display is not None:
                            _nt0 = calc_nine_turn_display(current_df)
                            nt_entry = bool(
                                _nt0.get("direction") == "down"
                                and (_nt0.get("is_complete") or _nt0.get("is_completing"))
                            )
                    except Exception:
                        nt_entry = False
                    if high_weight_buy or nt_entry:
                        do_buy = True
                        buy_pct = float(result.position_pct or 0)
                        if buy_pct < 0.05:
                            buy_pct = min(self.max_position, max(0.12, self.max_position * 0.4))
                        # 震荡/时机主导略减仓
                        if "时机主导" in action_txt or "轻仓" in action_txt:
                            buy_pct = min(buy_pct, self.max_position * 0.35)
                        buy_pct = min(max(buy_pct, 0.05), self.max_position)
                        parts = []
                        try:
                            for k, v in (layers or {}).items():
                                if isinstance(v, dict) and v.get("通过"):
                                    parts.append(str(k).split("（")[0])
                        except Exception:
                            parts = []
                        if nt_entry and "九转" not in "".join(parts):
                            parts.append("九转买点")
                        layer_txt = "+".join(parts) if parts else ("九转买点" if nt_entry else "策略买侧")
                        base_reason = action_txt or ("九转下跌买点建仓" if nt_entry else "高权重因子建仓")
                        trade_reason = f"{base_reason}·建仓({layer_txt}|信号价{close:.2f})"
                else:
                    # 已有仓：仅藤本茂第二档加仓（相对成本跌约 25% 增持约 25%）
                    # 忽略 -15% 第一档；三层一致只表示可持有，不自动加仓
                    allow_add = price_gap_ok and (i - last_buy_bar) >= cooldown
                    second_tier_add = pnl_from_cost <= -0.25
                    if allow_add and second_tier_add and not near_high:
                        do_buy = True
                        room = max(0.0, self.max_position - position_pct)
                        buy_pct = min(0.25, room)  # 第二档增持 25%
                        trade_reason = "藤本茂第二档加仓：下跌25%增持25%"
                    # 否则持有，不加

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
