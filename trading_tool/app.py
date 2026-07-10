"""
藤本茂交易哲学融合策略 - 智能分析工具 GUI
=============================================
功能：
  1. 导入CSV行情数据 / 生成模拟数据
  2. 可视化K线+均线+斐波那契回撤
  3. 三层策略分析（系统层+工具层+心法层）
  4. 藤本茂阶梯仓位计算器
  5. 结果导出

运行: python3.11 app.py
"""

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('TkAgg')
import matplotlib.pyplot as plt
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
from matplotlib.patches import Rectangle
from datetime import datetime
import json
import os

from strategy_engine import FujimotoStrategy, generate_sample_data, SignalType, TrendType


# ================================================================
#  K线绘制工具
# ================================================================

def plot_candlestick(ax, df, mas_dict=None, fib_levels=None,
                     swing_high=None, swing_low=None,
                     target_prices=None, entry_price=None, stop_loss=None,
                     show_last=120):
    """在指定 ax 上绘制 K 线 + 均线 + 斐波那契"""
    df = df.tail(show_last).copy().reset_index(drop=True)
    n = len(df)

    # 绘制 K 线
    for i in range(n):
        row = df.iloc[i]
        color = '#e74c3c' if row['close'] < row['open'] else '#2ecc71'
        # 影线
        ax.plot([i, i], [row['low'], row['high']], color=color, linewidth=0.8, zorder=1)
        # 实体
        body_bottom = min(row['open'], row['close'])
        body_height = abs(row['close'] - row['open'])
        rect = Rectangle((i - 0.3, body_bottom), 0.6, max(body_height, 0.01),
                         facecolor=color, edgecolor=color, zorder=2)
        ax.add_patch(rect)

    # 均线
    if mas_dict:
        ma_colors = ['#3498db', '#9b59b6', '#e67e22', '#1abc9c', '#f39c12',
                     '#e74c3c', '#34495e', '#7f8c8d', '#bdc3c7']
        for idx, (period, ma_val) in enumerate(sorted(mas_dict.items())):
            if ma_val is None:
                continue
            # 需要在原始 df 上计算完整均线，然后取尾部
            pass  # 均线在外部计算后传入

    ax.set_xlim(-1, n)
    ax.set_ylabel('价格', fontsize=10)

    # 斐波那契水平线
    if fib_levels:
        for fl in fib_levels:
            ax.axhline(y=fl.price, color='purple', linewidth=0.7,
                      linestyle='--', alpha=0.5)
            label = f"{fl.level:.3f}"
            if fl.reacted:
                label += " ★"
            ax.text(n - 0.5, fl.price, f" {label}", fontsize=7,
                   color='purple', va='center', alpha=0.8)

    # 目标价
    if target_prices:
        for t in target_prices:
            ax.axhline(y=t, color='orange', linewidth=0.6,
                      linestyle=':', alpha=0.6)
            ax.text(n - 0.5, t, f" 目标 {t:.2f}", fontsize=7,
                   color='orange', va='center')

    # 入场价 / 止损
    if entry_price:
        ax.axhline(y=entry_price, color='blue', linewidth=1,
                  linestyle='-', alpha=0.5)
        ax.text(0, entry_price, f" 入场 {entry_price:.2f}", fontsize=7,
               color='blue', va='bottom')
    if stop_loss:
        ax.axhline(y=stop_loss, color='red', linewidth=1,
                  linestyle='-', alpha=0.5)
        ax.text(0, stop_loss, f" 止损 {stop_loss:.2f}", fontsize=7,
               color='red', va='top')

    ax.set_xlabel('K线序号（最近{}根）'.format(show_last), fontsize=9)


def plot_volume(ax, df, show_last=120):
    """绘制成交量"""
    df = df.tail(show_last).copy().reset_index(drop=True)
    colors = ['#e74c3c' if c < o else '#2ecc71'
              for c, o in zip(df['close'], df['open'])]
    ax.bar(range(len(df)), df['volume'], color=colors, width=0.6, alpha=0.7)
    ax.set_xlim(-1, len(df))
    ax.set_ylabel('成交量', fontsize=9)


def plot_indicators(fig, df, show_last=120):
    """在独立子图绘制 RSI"""
    pass  # 简化：RSI 文字显示即可


# ================================================================
#  主应用
# ================================================================

class TradingApp:
    def __init__(self, root):
        self.root = root
        self.root.title("藤本茂交易哲学融合策略 - 智能分析工具 v1.0")
        self.root.geometry("1400x900")
        self.root.minsize(1200, 800)

        # 数据
        self.df = None
        self.strategy = None
        self.result = None
        self.current_position_pct = 0.0
        self.entry_price = None

        self._build_ui()

        # 自动加载模拟数据
        self._load_sample_data()

    def _build_ui(self):
        """构建界面"""
        # 顶部工具栏
        toolbar = ttk.Frame(self.root, padding=5)
        toolbar.pack(side=tk.TOP, fill=tk.X)

        ttk.Button(toolbar, text="导入CSV", command=self._import_csv).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="生成模拟数据", command=self._load_sample_data).pack(side=tk.LEFT, padx=2)
        ttk.Separator(toolbar, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=5)

        ttk.Label(toolbar, text="总资金:").pack(side=tk.LEFT)
        self.capital_var = tk.StringVar(value="100000")
        ttk.Entry(toolbar, textvariable=self.capital_var, width=10).pack(side=tk.LEFT, padx=2)

        ttk.Label(toolbar, text="当前持仓%:").pack(side=tk.LEFT)
        self.position_var = tk.StringVar(value="0")
        ttk.Entry(toolbar, textvariable=self.position_var, width=8).pack(side=tk.LEFT, padx=2)

        ttk.Label(toolbar, text="建仓价:").pack(side=tk.LEFT)
        self.entry_var = tk.StringVar(value="")
        ttk.Entry(toolbar, textvariable=self.entry_var, width=10).pack(side=tk.LEFT, padx=2)

        ttk.Button(toolbar, text="▶ 执行分析", command=self._run_analysis,
                  style='Accent.TButton').pack(side=tk.LEFT, padx=10)

        ttk.Button(toolbar, text="导出报告", command=self._export_report).pack(side=tk.RIGHT, padx=2)

        # 主体：左图右面板
        main = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 左侧：图表
        left_frame = ttk.Frame(main)
        main.add(left_frame, weight=3)

        self.fig = Figure(figsize=(10, 7), dpi=100)
        self.ax_price = self.fig.add_subplot(3, 1, 1)
        self.ax_volume = self.fig.add_subplot(3, 1, 2, sharex=self.ax_price)
        self.ax_rsi = self.fig.add_subplot(3, 1, 3, sharex=self.ax_price)
        self.fig.tight_layout(pad=2.0)

        self.canvas = FigureCanvasTkAgg(self.fig, left_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        nav_frame = ttk.Frame(left_frame)
        nav_frame.pack(fill=tk.X)
        NavigationToolbar2Tk(self.canvas, nav_frame)

        # 右侧：分析面板
        right_frame = ttk.Frame(main, width=450)
        main.add(right_frame, weight=1)

        self._build_right_panel(right_frame)

        # 底部状态栏
        self.status_var = tk.StringVar(value="就绪")
        status_bar = ttk.Label(self.root, textvariable=self.status_var,
                              relief=tk.SUNKEN, anchor=tk.W)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def _build_right_panel(self, parent):
        """构建右侧分析面板"""
        notebook = ttk.Notebook(parent)
        notebook.pack(fill=tk.BOTH, expand=True)

        # Tab 1: 策略信号
        tab1 = ttk.Frame(notebook, padding=10)
        notebook.add(tab1, text="策略信号")

        # 信号摘要
        sig_frame = ttk.LabelFrame(tab1, text="信号摘要", padding=10)
        sig_frame.pack(fill=tk.X, pady=5)

        self.lbl_trend = ttk.Label(sig_frame, text="趋势: -", font=('', 11, 'bold'))
        self.lbl_trend.pack(anchor=tk.W)
        self.lbl_signal = ttk.Label(sig_frame, text="信号: -", font=('', 14, 'bold'),
                                    foreground='blue')
        self.lbl_signal.pack(anchor=tk.W, pady=5)
        self.lbl_action = ttk.Label(sig_frame, text="操作: -", wraplength=380)
        self.lbl_action.pack(anchor=tk.W)
        self.lbl_position = ttk.Label(sig_frame, text="建议仓位: -", font=('', 11, 'bold'))
        self.lbl_position.pack(anchor=tk.W, pady=3)
        self.lbl_entry = ttk.Label(sig_frame, text="入场价: -")
        self.lbl_entry.pack(anchor=tk.W)
        self.lbl_stop = ttk.Label(sig_frame, text="止损价: -", foreground='red')
        self.lbl_stop.pack(anchor=tk.W)
        self.lbl_target = ttk.Label(sig_frame, text="目标价: -", foreground='orange')
        self.lbl_target.pack(anchor=tk.W)
        self.lbl_risk = ttk.Label(sig_frame, text="", wraplength=380, foreground='red')
        self.lbl_risk.pack(anchor=tk.W, pady=3)

        # 三层一致性
        layer_frame = ttk.LabelFrame(tab1, text="三层一致性检验", padding=10)
        layer_frame.pack(fill=tk.X, pady=5)

        self.layer_labels = {}
        for layer_name in ["系统层（趋势+指标）", "工具层（斐波那契反应）", "心法层（藤本茂阶梯）"]:
            f = ttk.Frame(layer_frame)
            f.pack(fill=tk.X, pady=2)
            lbl_status = ttk.Label(f, text="○", font=('', 12), width=2)
            lbl_status.pack(side=tk.LEFT)
            lbl_detail = ttk.Label(f, text=layer_name + ": -", wraplength=360, justify=tk.LEFT)
            lbl_detail.pack(side=tk.LEFT, fill=tk.X)
            self.layer_labels[layer_name] = (lbl_status, lbl_detail)

        # Tab 2: 指标详情
        tab2 = ttk.Frame(notebook, padding=10)
        notebook.add(tab2, text="指标详情")

        self.indicator_text = tk.Text(tab2, wrap=tk.WORD, font=('Consolas', 10))
        self.indicator_text.pack(fill=tk.BOTH, expand=True)

        # Tab 3: 藤本茂阶梯表
        tab3 = ttk.Frame(notebook, padding=10)
        notebook.add(tab3, text="藤本茂阶梯")

        # 下跌阶梯
        ttk.Label(tab3, text="▼ 下跌阶梯（加仓）", font=('', 11, 'bold'),
                 foreground='green').pack(anchor=tk.W, pady=5)
        buy_tree = ttk.Treeview(tab3, columns=('trigger', 'action', 'desc'),
                               show='headings', height=4)
        buy_tree.heading('trigger', text='触发条件')
        buy_tree.heading('action', text='操作')
        buy_tree.heading('desc', text='说明')
        buy_tree.column('trigger', width=100)
        buy_tree.column('action', width=80)
        buy_tree.column('desc', width=220)
        for item in [("-5%", "不操作", "噪音区间，不动如山"),
                     ("-15%", "+10%", "第一档承接"),
                     ("-25%", "+25%", "加重仓接筹"),
                     ("-35%+", "止损评估", "设硬止损避免深套")]:
            buy_tree.insert('', tk.END, values=item)
        buy_tree.pack(fill=tk.X, pady=5)

        ttk.Label(tab3, text="▲ 上涨阶梯（减仓）", font=('', 11, 'bold'),
                 foreground='red').pack(anchor=tk.W, pady=10)
        sell_tree = ttk.Treeview(tab3, columns=('trigger', 'action', 'desc'),
                                show='headings', height=8)
        sell_tree.heading('trigger', text='触发条件')
        sell_tree.heading('action', text='操作')
        sell_tree.heading('desc', text='说明')
        sell_tree.column('trigger', width=100)
        sell_tree.column('action', width=80)
        sell_tree.column('desc', width=220)
        for item in [("+5%", "持有", "让利润奔跑"),
                     ("+15%", "持有", "趋势确认不动"),
                     ("+25%", "-10%", "开始兑现"),
                     ("+35%", "-20%", "加速兑现"),
                     ("+45%", "-30%", "大幅减仓"),
                     ("+60%", "-40%", "接近清仓"),
                     ("+100%", "清仓", "极端泡沫离场")]:
            sell_tree.insert('', tk.END, values=item)
        sell_tree.pack(fill=tk.X, pady=5)

        # Tab 4: 日志
        tab4 = ttk.Frame(notebook, padding=10)
        notebook.add(tab4, text="操作日志")
        self.log_text = tk.Text(tab4, wrap=tk.WORD, font=('Consolas', 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ================================================================
    #  数据加载
    # ================================================================

    def _import_csv(self):
        path = filedialog.askopenfilename(
            title="选择行情CSV文件",
            filetypes=[("CSV文件", "*.csv"), ("所有文件", "*.*")]
        )
        if not path:
            return

        try:
            df = pd.read_csv(path)
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

            required = ['open', 'high', 'low', 'close', 'volume']
            missing = [c for c in required if c not in df.columns]
            if missing:
                messagebox.showerror("错误", f"CSV缺少列: {missing}\n需要: {required}")
                return

            if 'date' in df.columns:
                df['date'] = pd.to_datetime(df['date'])
                df = df.sort_values('date').reset_index(drop=True)

            self.df = df
            self._log(f"导入CSV: {path} ({len(df)}行)")
            self.status_var.set(f"已导入 {len(df)} 根K线")
            self._draw_chart()

        except Exception as e:
            messagebox.showerror("导入错误", str(e))

    def _load_sample_data(self):
        """生成模拟数据"""
        df = generate_sample_data(300)
        self.df = df
        self._log("生成模拟数据: 300根K线")
        self.status_var.set("已加载模拟数据（300根K线）")
        self._draw_chart()

    # ================================================================
    #  图表绘制
    # ================================================================

    def _draw_chart(self):
        if self.df is None:
            return

        df = self.df
        show_last = min(120, len(df))

        # 清空
        self.ax_price.clear()
        self.ax_volume.clear()
        self.ax_rsi.clear()

        # 计算均线用于绘图
        ma_periods = [5, 10, 20, 50, 100, 200]
        ma_colors = ['#3498db', '#9b59b6', '#e67e22', '#1abc9c', '#f39c12', '#e74c3c']
        ma_data = {}
        for p in ma_periods:
            if len(df) >= p:
                ma_data[p] = df['close'].rolling(p).mean()

        # K线 + 均线
        recent = df.tail(show_last).copy().reset_index(drop=True)
        plot_candlestick(self.ax_price, df, show_last=show_last)

        # 覆盖均线
        for (period, ma_series), color in zip(ma_data.items(), ma_colors):
            if ma_series is not None:
                ma_tail = ma_series.tail(show_last).values
                self.ax_price.plot(range(len(ma_tail)), ma_tail,
                                  color=color, linewidth=0.8, label=f'MA{period}', alpha=0.7)

        # 斐波那契
        if self.result:
            cd = self.result.chart_data
            fib_levels = cd.get('fib_levels', [])
            target_prices = cd.get('target_prices', [])
            entry = self.result.entry_price
            stop = self.result.stop_loss

            for fl in fib_levels:
                self.ax_price.axhline(y=fl.price, color='purple', linewidth=0.6,
                                      linestyle='--', alpha=0.4)
                label = f"{fl.level:.3f}"
                if fl.reacted:
                    label += " ★确认"
                self.ax_price.text(len(recent) - 0.5, fl.price, f" {label}",
                                  fontsize=7, color='purple', va='center', alpha=0.8)

            for t in target_prices:
                self.ax_price.axhline(y=t, color='orange', linewidth=0.5,
                                      linestyle=':', alpha=0.5)
                self.ax_price.text(len(recent) - 0.5, t, f" 目标{t:.2f}",
                                  fontsize=7, color='orange', va='center')

            if entry:
                self.ax_price.axhline(y=entry, color='blue', linewidth=1, alpha=0.5)
            if stop:
                self.ax_price.axhline(y=stop, color='red', linewidth=1, alpha=0.5)

        self.ax_price.legend(fontsize=7, loc='upper left')
        self.ax_price.set_title(f"K线图 + 均线 + 斐波那契（最近{show_last}根）", fontsize=11)

        # 成交量
        plot_volume(self.ax_volume, df, show_last)

        # RSI
        if len(df) >= 15:
            delta = df['close'].diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(14).mean()
            rs = gain / loss.replace(0, np.nan)
            rsi = 100 - (100 / (1 + rs))
            rsi_tail = rsi.tail(show_last).values
            self.ax_rsi.plot(range(len(rsi_tail)), rsi_tail, color='#8e44ad', linewidth=1)
            self.ax_rsi.axhline(y=70, color='red', linestyle='--', linewidth=0.5, alpha=0.5)
            self.ax_rsi.axhline(y=30, color='green', linestyle='--', linewidth=0.5, alpha=0.5)
            self.ax_rsi.fill_between(range(len(rsi_tail)), 70, rsi_tail,
                                    where=[r > 70 if not np.isnan(r) else False for r in rsi_tail],
                                    color='red', alpha=0.1)
            self.ax_rsi.fill_between(range(len(rsi_tail)), 30, rsi_tail,
                                    where=[r < 30 if not np.isnan(r) else False for r in rsi_tail],
                                    color='green', alpha=0.1)
            self.ax_rsi.set_ylim(0, 100)
            self.ax_rsi.set_ylabel('RSI(14)', fontsize=9)

        self.fig.tight_layout(pad=1.5)
        self.canvas.draw()

    # ================================================================
    #  执行分析
    # ================================================================

    def _run_analysis(self):
        if self.df is None:
            messagebox.showwarning("提示", "请先导入数据或生成模拟数据")
            return

        try:
            capital = float(self.capital_var.get())
            current_pos = float(self.position_var.get()) / 100.0
            entry = float(self.entry_var.get()) if self.entry_var.get() else None
        except ValueError:
            messagebox.showerror("错误", "请输入有效数字")
            return

        self.current_position_pct = current_pos
        self.entry_price = entry

        self.strategy = FujimotoStrategy(
            total_capital=capital,
            entry_price=entry
        )
        self.result = self.strategy.analyze(self.df, current_position_pct=current_pos)

        self._update_result_panel()
        self._draw_chart()

        self._log(f"[{datetime.now().strftime('%H:%M:%S')}] 分析完成: "
                  f"{self.result.signal.value} - {self.result.action}")
        self.status_var.set(f"分析完成: {self.result.signal.value}")

    def _update_result_panel(self):
        r = self.result

        # 信号摘要
        trend_colors = {
            TrendType.BULL: 'green',
            TrendType.BEAR: 'red',
            TrendType.RANGE: 'gray'
        }
        self.lbl_trend.config(text=f"趋势: {r.trend.value}",
                             foreground=trend_colors.get(r.trend, 'black'))

        sig_colors = {
            SignalType.BUY: 'red',
            SignalType.SELL: 'green',
            SignalType.HOLD: 'blue',
            SignalType.WAIT: 'gray',
            SignalType.ADD: 'orange'
        }
        self.lbl_signal.config(text=f"信号: {r.signal.value}",
                              foreground=sig_colors.get(r.signal, 'black'))
        self.lbl_action.config(text=f"操作: {r.action}")

        pos_text = f"建议仓位变动: {r.position_pct*100:+.1f}%"
        if r.position_pct > 0:
            pos_text += f" (约{r.position_pct * float(self.capital_var.get()):.0f}元)"
        self.lbl_position.config(text=pos_text)

        self.lbl_entry.config(text=f"入场价: {r.entry_price:.2f}" if r.entry_price else "入场价: -")
        self.lbl_stop.config(text=f"止损价: {r.stop_loss:.2f}" if r.stop_loss else "止损价: -")

        if r.target_prices:
            targets = ", ".join([f"{t:.2f}" for t in r.target_prices])
            self.lbl_target.config(text=f"目标价: {targets}")
        else:
            self.lbl_target.config(text="目标价: -")

        self.lbl_risk.config(text=r.risk_warning if r.risk_warning else "")

        # 三层一致性
        for layer_name, (lbl_status, lbl_detail) in self.layer_labels.items():
            info = r.layers_consistent.get(layer_name, {})
            passed = info.get("通过", False)
            status = info.get("状态", "-")
            lbl_status.config(text="✓" if passed else "✗",
                            foreground='green' if passed else 'red')
            lbl_detail.config(text=f"{layer_name}: {status}")

        # 指标详情
        self.indicator_text.delete(1.0, tk.END)
        self.indicator_text.insert(tk.END, "=" * 50 + "\n")
        self.indicator_text.insert(tk.END, "指标详情\n")
        self.indicator_text.insert(tk.END, "=" * 50 + "\n\n")
        for ind in r.indicators:
            self.indicator_text.insert(tk.END, f"【{ind.name}】\n")
            self.indicator_text.insert(tk.END, f"  信号: {ind.signal}\n")
            self.indicator_text.insert(tk.END, f"  详情: {ind.detail}\n")
            self.indicator_text.insert(tk.END, f"  数值: {ind.value:.4f}\n\n")

        self.indicator_text.insert(tk.END, "=" * 50 + "\n")
        self.indicator_text.insert(tk.END, "斐波那契回撤位\n")
        self.indicator_text.insert(tk.END, "=" * 50 + "\n\n")
        for fl in r.fib_levels:
            star = " ★已确认" if fl.reacted else (" ●已测试" if fl.tested else "")
            self.indicator_text.insert(tk.END,
                f"  {fl.level:.3f} → {fl.price:.2f}{star}\n")
            if fl.reaction_signal:
                self.indicator_text.insert(tk.END,
                    f"    反应: {fl.reaction_signal}\n")

    # ================================================================
    #  导出报告
    # ================================================================

    def _export_report(self):
        if self.result is None:
            messagebox.showwarning("提示", "请先执行分析")
            return

        path = filedialog.asksaveasfilename(
            title="保存报告",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if not path:
            return

        r = self.result
        lines = []
        lines.append("=" * 60)
        lines.append("藤本茂交易哲学融合策略 - 分析报告")
        lines.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("=" * 60)
        lines.append("")
        lines.append(f"趋势: {r.trend.value}")
        lines.append(f"信号: {r.signal.value}")
        lines.append(f"操作: {r.action}")
        lines.append(f"建议仓位: {r.position_pct*100:+.1f}%")
        if r.entry_price:
            lines.append(f"入场价: {r.entry_price:.2f}")
        if r.stop_loss:
            lines.append(f"止损价: {r.stop_loss:.2f}")
        if r.target_prices:
            lines.append(f"目标价: {', '.join([f'{t:.2f}' for t in r.target_prices])}")
        lines.append("")
        lines.append("--- 三层一致性检验 ---")
        for layer, info in r.layers_consistent.items():
            status = "通过" if info["通过"] else "未通过"
            lines.append(f"  [{status}] {layer}")
            lines.append(f"    {info['状态']}")
        lines.append("")
        lines.append("--- 指标详情 ---")
        for ind in r.indicators:
            lines.append(f"  {ind.name}: {ind.signal} | {ind.detail}")
        lines.append("")
        lines.append("--- 斐波那契 ---")
        for fl in r.fib_levels:
            star = " ★确认" if fl.reacted else ""
            lines.append(f"  {fl.level:.3f} @ {fl.price:.2f}{star} {fl.reaction_signal}")
        lines.append("")
        if r.risk_warning:
            lines.append(f"风控提示: {r.risk_warning}")
        lines.append("")
        lines.append("免责声明: 本报告由策略引擎自动生成，仅供参考，不构成投资建议。")

        with open(path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        self._log(f"报告已导出: {path}")
        messagebox.showinfo("成功", f"报告已保存至:\n{path}")

    # ================================================================
    #  日志
    # ================================================================

    def _log(self, msg):
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)


# ================================================================
#  启动
# ================================================================

def main():
    root = tk.Tk()
    # 尝试设置主题
    style = ttk.Style()
    try:
        style.theme_use('clam')
    except:
        pass

    app = TradingApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
