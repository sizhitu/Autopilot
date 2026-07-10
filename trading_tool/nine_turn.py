"""
神奇九转计数模块
=================================
实现类似腾讯自选股的"神奇九转"趋势计数。

规则（基于 TD Sequential 简化版）：
  - 下跌九转：连续下跌中，若当日收盘价 < 前第4根K线收盘价，则计数+1
  - 上涨九转：连续上涨中，若当日收盘价 > 前第4根K线收盘价，则计数+1
  - 计数范围 1-9，到9表示九转完成
  - 方向反转时计数重置

状态分类：
  - 第7,8天：即将完成（密切关注）
  - 第9天：九转完成（趋势可能反转）
"""

from dataclasses import dataclass
from typing import List, Optional
import pandas as pd
import numpy as np


@dataclass
class NineTurnResult:
    """九转计数结果"""
    direction: str = "none"     # "down" | "up" | "none"
    count: int = 0              # 当前计数 1-9
    status: str = "无"          # 状态描述
    is_completing: bool = False  # 即将完成(7-8)
    is_complete: bool = False    # 已完成(=9)
    days_to_complete: int = 0    # 距离完成还差几天


def calc_nine_turn(df: pd.DataFrame) -> NineTurnResult:
    """
    计算神奇九转计数

    Args:
        df: 含 close 列的 DataFrame（按时间升序）
    Returns:
        NineTurnResult
    """
    closes = df['close'].values
    n = len(closes)
    if n < 5:
        return NineTurnResult()

    # 逐日计算九转计数
    counts = [0] * n
    current_dir = "none"
    current_count = 0

    for i in range(4, n):
        prev4_close = closes[i - 4]
        cur_close = closes[i]

        if cur_close < prev4_close:
            # 下跌计数
            if current_dir == "down":
                current_count += 1
            else:
                current_dir = "down"
                current_count = 1
        elif cur_close > prev4_close:
            # 上涨计数
            if current_dir == "up":
                current_count += 1
            else:
                current_dir = "up"
                current_count = 1
        else:
            # 平盘，重置
            current_dir = "none"
            current_count = 0

        # 限制最大9
        if current_count > 9:
            # 超过9后保持9直到方向改变
            current_count = 9

        counts[i] = current_count

    # 取最新的计数状态
    final_count = counts[-1]
    final_dir = current_dir

    result = NineTurnResult()

    if final_dir == "down" and final_count > 0:
        result.direction = "down"
        result.count = final_count
        result.status = f"下跌九转第{final_count}天"
        if final_count >= 7 and final_count < 9:
            result.is_completing = True
            result.status = f"下跌九转第{final_count}天(即将完成)"
            result.days_to_complete = 9 - final_count
        elif final_count == 9:
            result.is_complete = True
            result.status = "下跌九转完成(买点)"
            result.days_to_complete = 0

    elif final_dir == "up" and final_count > 0:
        result.direction = "up"
        result.count = final_count
        result.status = f"上涨九转第{final_count}天"
        if final_count >= 7 and final_count < 9:
            result.is_completing = True
            result.status = f"上涨九转第{final_count}天(即将完成)"
            result.days_to_complete = 9 - final_count
        elif final_count == 9:
            result.is_complete = True
            result.status = "上涨九转完成(卖点)"
            result.days_to_complete = 0

    else:
        result.direction = "none"
        result.count = 0
        result.status = "无九转信号"

    return result


# ================================================================
#  测试
# ================================================================
if __name__ == "__main__":
    # 构造测试数据
    # 下跌趋势
    dates = pd.date_range('2026-01-01', periods=30, freq='D')
    # 模拟连续下跌（满足九转条件：每日收盘 < 前4日收盘）
    prices_down = np.array([100 - i * 0.5 - (i // 4) * 0.3 for i in range(30)])
    df_down = pd.DataFrame({'date': dates, 'close': prices_down})
    r1 = calc_nine_turn(df_down)
    print(f"下跌趋势: dir={r1.direction} count={r1.count} status={r1.status} complete={r1.is_complete} completing={r1.is_completing}")

    # 上涨趋势
    prices_up = np.array([100 + i * 0.5 + (i // 4) * 0.3 for i in range(30)])
    df_up = pd.DataFrame({'date': dates, 'close': prices_up})
    r2 = calc_nine_turn(df_up)
    print(f"上涨趋势: dir={r2.direction} count={r2.count} status={r2.status} complete={r2.is_complete} completing={r2.is_completing}")

    # 震荡
    np.random.seed(42)
    prices_noise = 100 + np.random.randn(30) * 2
    df_noise = pd.DataFrame({'date': dates, 'close': prices_noise})
    r3 = calc_nine_turn(df_noise)
    print(f"震荡: dir={r3.direction} count={r3.count} status={r3.status}")
