"""
生成策略分析图表（无GUI环境验证用）
将K线+均线+斐波那契+RSI图表保存为PNG
"""
import sys
sys.path.insert(0, '/workspace/trading_tool')

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties, fontManager
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
import pandas as pd
import numpy as np
from strategy_engine import FujimotoStrategy, generate_sample_data

# 注册中文字体
font_path = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
fontManager.addfont(font_path)
CJK_FONT = FontProperties(fname=font_path)
plt.rcParams['font.family'] = CJK_FONT.get_name()
plt.rcParams['axes.unicode_minus'] = False


def save_chart(df, result, path):
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(14, 10),
                                         gridspec_kw={'height_ratios': [3, 1, 1]},
                                         sharex=True)
    show_last = 120
    recent = df.tail(show_last).copy().reset_index(drop=True)
    n = len(recent)

    # K线
    for i in range(n):
        row = recent.iloc[i]
        color = '#e74c3c' if row['close'] < row['open'] else '#2ecc71'
        ax1.plot([i, i], [row['low'], row['high']], color=color, linewidth=0.8, zorder=1)
        body_bottom = min(row['open'], row['close'])
        body_height = abs(row['close'] - row['open'])
        rect = Rectangle((i - 0.3, body_bottom), 0.6, max(body_height, 0.01),
                         facecolor=color, edgecolor=color, zorder=2)
        ax1.add_patch(rect)

    # 均线
    ma_periods = [5, 10, 20, 50, 100, 200]
    ma_colors = ['#3498db', '#9b59b6', '#e67e22', '#1abc9c', '#f39c12', '#e74c3c']
    for period, color in zip(ma_periods, ma_colors):
        if len(df) >= period:
            ma = df['close'].rolling(period).mean().tail(show_last).values
            ax1.plot(range(len(ma)), ma, color=color, linewidth=0.8,
                    label=f'MA{period}', alpha=0.7)

    # 斐波那契
    if result and result.chart_data.get('fib_levels'):
        for fl in result.chart_data['fib_levels']:
            ax1.axhline(y=fl.price, color='purple', linewidth=0.6,
                       linestyle='--', alpha=0.4)
            label = f"{fl.level:.3f}"
            if fl.reacted:
                label += " *"
            ax1.text(n - 0.5, fl.price, f" {label}", fontsize=7,
                    color='purple', va='center', alpha=0.8, fontproperties=CJK_FONT)

    if result and result.chart_data.get('target_prices'):
        for t in result.chart_data['target_prices']:
            ax1.axhline(y=t, color='orange', linewidth=0.5, linestyle=':', alpha=0.5)
            ax1.text(n - 0.5, t, f" Target {t:.2f}", fontsize=7,
                    color='orange', va='center', fontproperties=CJK_FONT)

    if result and result.entry_price:
        ax1.axhline(y=result.entry_price, color='blue', linewidth=1, alpha=0.5,
                   label=f'Entry {result.entry_price:.2f}')
    if result and result.stop_loss:
        ax1.axhline(y=result.stop_loss, color='red', linewidth=1, alpha=0.5,
                   label=f'Stop {result.stop_loss:.2f}')

    ax1.set_xlim(-1, n)
    ax1.legend(fontsize=7, loc='upper left', prop=CJK_FONT)
    title_text = f"藤本茂融合策略分析 | 信号: {result.signal.value} | 趋势: {result.trend.value}"
    ax1.set_title(title_text, fontsize=12, fontweight='bold', fontproperties=CJK_FONT)
    ax1.set_ylabel('价格', fontsize=10, fontproperties=CJK_FONT)
    ax1.grid(True, alpha=0.3)

    # 成交量
    colors = ['#e74c3c' if c < o else '#2ecc71'
              for c, o in zip(recent['close'], recent['open'])]
    ax2.bar(range(n), recent['volume'], color=colors, width=0.6, alpha=0.7)
    ax2.set_ylabel('成交量', fontsize=9, fontproperties=CJK_FONT)
    ax2.grid(True, alpha=0.3)

    # RSI
    delta = df['close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi_tail = rsi.tail(show_last).values
    ax3.plot(range(len(rsi_tail)), rsi_tail, color='#8e44ad', linewidth=1)
    ax3.axhline(y=70, color='red', linestyle='--', linewidth=0.5, alpha=0.5, label='70 Overbought')
    ax3.axhline(y=30, color='green', linestyle='--', linewidth=0.5, alpha=0.5, label='30 Oversold')
    ax3.fill_between(range(len(rsi_tail)), 70, rsi_tail,
                    where=[r > 70 if not np.isnan(r) else False for r in rsi_tail],
                    color='red', alpha=0.1)
    ax3.fill_between(range(len(rsi_tail)), 30, rsi_tail,
                    where=[r < 30 if not np.isnan(r) else False for r in rsi_tail],
                    color='green', alpha=0.1)
    ax3.set_ylim(0, 100)
    ax3.set_ylabel('RSI(14)', fontsize=9, fontproperties=CJK_FONT)
    ax3.set_xlabel(f'K线序号 (最近{show_last}根)', fontsize=9, fontproperties=CJK_FONT)
    ax3.legend(fontsize=7, loc='upper left', prop=CJK_FONT)
    ax3.grid(True, alpha=0.3)

    plt.tight_layout(pad=1.0)
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"图表已保存: {path}")


if __name__ == "__main__":
    df = generate_sample_data(300)
    strategy = FujimotoStrategy(total_capital=100000, entry_price=df['close'].iloc[0])
    result = strategy.analyze(df, current_position_pct=0.3)
    save_chart(df, result, '/workspace/trading_tool/strategy_chart.png')
