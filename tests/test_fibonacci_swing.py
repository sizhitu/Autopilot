"""Tests for confirmed, direction-aware Fibonacci swing anchors."""
import pandas as pd

from trading_tool.strategy_engine import FujimotoStrategy


def _frame(values):
    rows = []
    for close in values:
        rows.append({
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1000.0,
        })
    return pd.DataFrame(rows)


def test_detects_confirmed_low_to_high_impulse_without_lookahead():
    # The low at 4 and high at 10 are only knowable after three right bars.
    frame = _frame([100, 98, 96, 94, 80, 86, 92, 98, 104, 110, 120, 115, 110, 105])
    swing = FujimotoStrategy._detect_valid_fib_swing(frame)
    assert swing is not None
    assert swing["direction"] == "bullish"
    assert swing["start_idx"] == 4
    assert swing["end_idx"] == 10
    assert swing["confirmation_idx"] == 13


def test_does_not_confirm_pivot_before_right_side_bars_exist():
    frame = _frame([100, 98, 96, 94, 80, 86, 92, 98, 104, 110, 120])
    assert FujimotoStrategy._detect_valid_fib_swing(frame) is None


def test_detects_high_to_low_impulse_as_bearish():
    frame = _frame([80, 84, 88, 92, 120, 114, 108, 102, 96, 90, 80, 84, 88, 92])
    swing = FujimotoStrategy._detect_valid_fib_swing(frame)
    assert swing is not None
    assert swing["direction"] == "bearish"
    assert swing["start_idx"] == 4
    assert swing["end_idx"] == 10


def test_rejects_small_or_short_noise_swings():
    frame = _frame([100, 99, 100, 99, 100, 99, 100, 99, 100, 99, 100, 99, 100])
    assert FujimotoStrategy._detect_valid_fib_swing(frame) is None


def test_latest_confirmed_swing_selects_only_its_direction():
    strategy = FujimotoStrategy()
    bullish = _frame([100, 98, 96, 94, 80, 86, 92, 98, 104, 110, 120, 115, 110, 105])
    bull_levels, *_ = strategy._calc_fibonacci(bullish)
    bear_levels, *_ = strategy._calc_fibonacci_bearish(bullish)
    assert len(bull_levels) == 4
    assert bear_levels == []

    bearish = _frame([80, 84, 88, 92, 120, 114, 108, 102, 96, 90, 80, 84, 88, 92])
    bull_levels, *_ = strategy._calc_fibonacci(bearish)
    bear_levels, *_ = strategy._calc_fibonacci_bearish(bearish)
    assert bull_levels == []
    assert len(bear_levels) == 4
