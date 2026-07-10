"""
命令行版本 - 藤本茂交易策略分析工具
====================================
无需 GUI，适合服务器/脚本环境使用。

用法:
  python3.11 cli.py                    # 用模拟数据分析
  python3.11 cli.py data.csv           # 用CSV数据分析
  python3.11 cli.py --generate sample.csv  # 生成示例CSV数据
"""

import sys
import argparse
import pandas as pd
import numpy as np
from datetime import datetime
from strategy_engine import FujimotoStrategy, generate_sample_data


def print_result(result, capital=100000):
    """格式化打印分析结果"""
    r = result
    print()
    print("=" * 65)
    print("        藤本茂交易哲学融合策略 - 分析报告")
    print(f"        {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 65)

    # 信号区
    trend_icon = {"多头趋势": "🟢", "空头趋势": "🔴", "震荡": "🟡"}
    signal_icon = {"买入": "🔥买入", "卖出": "💸卖出", "持有": "⏸持有",
                   "观望": "👀观望", "加仓": "📈加仓"}

    print(f"\n  趋势: {trend_icon.get(r.trend.value, '')} {r.trend.value}")
    print(f"  信号: {signal_icon.get(r.signal.value, r.signal.value)}")
    print(f"  操作: {r.action}")
    print(f"  建议仓位变动: {r.position_pct*100:+.1f}%", end="")
    if r.position_pct != 0:
        print(f"  (约{abs(r.position_pct * capital):.0f}元)", end="")
    print()

    if r.entry_price:
        print(f"  入场价: {r.entry_price:.2f}")
    if r.stop_loss:
        print(f"  止损价: {r.stop_loss:.2f}  (风险: {(r.entry_price - r.stop_loss)/r.entry_price*100:.1f}%)")
    if r.target_prices:
        targets = " / ".join([f"{t:.2f}" for t in r.target_prices])
        print(f"  目标价: {targets}")
        if r.entry_price and r.stop_loss:
            rr = (r.target_prices[0] - r.entry_price) / (r.entry_price - r.stop_loss)
            print(f"  风险回报比: 1 : {rr:.1f}")

    # 三层一致性
    print(f"\n{'─'*65}")
    print("  三层一致性检验")
    print(f"{'─'*65}")
    for layer_name, info in r.layers_consistent.items():
        status = "✓ 通过" if info["通过"] else "✗ 未通过"
        icon = "🟢" if info["通过"] else "🔴"
        print(f"\n  {icon} {layer_name}  [{status}]")
        # 状态文本换行显示
        status_text = info["状态"]
        while len(status_text) > 55:
            print(f"      {status_text[:55]}")
            status_text = status_text[55:]
        print(f"      {status_text}")

    # 指标详情
    print(f"\n{'─'*65}")
    print("  指标详情")
    print(f"{'─'*65}")
    for ind in r.indicators:
        icon = {"看多": "🟢", "看空": "🔴", "中性": "⚪"}.get(ind.signal, "⚪")
        print(f"  {icon} {ind.name:6s} [{ind.signal}] {ind.detail}")

    # 斐波那契
    print(f"\n{'─'*65}")
    print("  斐波那契回撤位")
    print(f"{'─'*65}")
    for fl in r.fib_levels:
        star = " ★已确认" if fl.reacted else (" ●已测试" if fl.tested else "")
        reaction = f"  ← {fl.reaction_signal}" if fl.reaction_signal else ""
        print(f"    {fl.level:.3f}  →  {fl.price:>10.2f}{star}{reaction}")

    # 风控
    if r.risk_warning:
        print(f"\n{'─'*65}")
        print(f"  ⚠ 风控提示: {r.risk_warning}")
        print(f"{'─'*65}")

    print(f"\n  免责声明: 本报告由策略引擎自动生成，仅供参考，不构成投资建议。")
    print("=" * 65)


def generate_csv(path, days=300):
    """生成示例CSV"""
    df = generate_sample_data(days)
    df.to_csv(path, index=False)
    print(f"已生成示例数据: {path} ({days}根K线)")


def main():
    parser = argparse.ArgumentParser(description="藤本茂交易策略分析工具")
    parser.add_argument("csv", nargs="?", help="行情CSV文件路径")
    parser.add_argument("--generate", metavar="PATH", help="生成示例CSV数据到指定路径")
    parser.add_argument("--capital", type=float, default=100000, help="总资金 (默认100000)")
    parser.add_argument("--position", type=float, default=0, help="当前持仓百分比 (默认0)")
    parser.add_argument("--entry", type=float, default=None, help="建仓价格")
    parser.add_argument("--lookback", type=int, default=60, help="斐波那契回溯周期 (默认60)")
    args = parser.parse_args()

    if args.generate:
        generate_csv(args.generate)
        return

    # 加载数据
    if args.csv:
        try:
            df = pd.read_csv(args.csv)
            # 标准化列名
            col_map = {}
            for c in df.columns:
                cl = c.lower().strip()
                if cl in ['date', 'datetime', 'time', '日期']:
                    col_map[c] = 'date'
                elif cl in ['open', '开盘']:
                    col_map[c] = 'open'
                elif cl in ['high', '最高']:
                    col_map[c] = 'high'
                elif cl in ['low', '最低']:
                    col_map[c] = 'low'
                elif cl in ['close', '收盘']:
                    col_map[c] = 'close'
                elif cl in ['volume', 'vol', '成交量']:
                    col_map[c] = 'volume'
            df = df.rename(columns=col_map)
            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df = df.sort_values('date').reset_index(drop=True)
            print(f"已加载: {args.csv} ({len(df)}行)")
        except Exception as e:
            print(f"加载失败: {e}")
            sys.exit(1)
    else:
        print("未指定CSV，使用模拟数据 (300根K线)")
        df = generate_sample_data(300)

    # 执行分析
    strategy = FujimotoStrategy(
        total_capital=args.capital,
        entry_price=args.entry
    )
    result = strategy.analyze(df, current_position_pct=args.position / 100.0)

    print_result(result, capital=args.capital)


if __name__ == "__main__":
    main()
