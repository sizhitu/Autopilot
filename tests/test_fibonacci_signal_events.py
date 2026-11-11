from __future__ import annotations

import pandas as pd

from trading_tool.strategy_engine import FujimotoStrategy


def _bars(lows, volumes):
    dates = pd.date_range("2026-08-01", periods=len(lows), freq="D")
    rows = []
    for i, (low, volume) in enumerate(zip(lows, volumes)):
        rows.append({
            "date": dates[i],
            "open": 86.0,
            "high": 100.0,
            "low": float(low),
            "close": 85.0 if low < 90 else 96.0,
            "volume": float(volume),
        })
    # Establish a stable 100/80 swing range for the Fib calculation.
    rows[0]["low"] = 80.0
    rows[0]["close"] = 90.0
    return pd.DataFrame(rows)


def _fib_0786(strategy, frame):
    levels, *_ = strategy._calc_fibonacci(frame, lookback=60)
    return next(level for level in levels if level.level == 0.786)


def test_fibonacci_confirmation_is_emitted_once_per_continuous_touch_episode():
    frame = _bars(
        lows=[80, 84.2, 84.2, 84.2, 95],
        volumes=[100, 100, 130, 200, 100],
    )

    level = _fib_0786(FujimotoStrategy(), frame)

    assert level.reacted is True
    # The first valid confirmation is the event; later volume spikes during
    # the same uninterrupted touch episode must not create new event dates.
    assert level.signal_date == "2026-08-03"
    assert level.reaction_signal == "放量止跌"


def test_fibonacci_can_emit_a_new_confirmation_after_price_leaves_and_retests():
    frame = _bars(
        lows=[80, 84.2, 84.2, 95, 84.2, 84.2, 84.2],
        volumes=[100, 100, 130, 100, 100, 150, 300],
    )

    level = _fib_0786(FujimotoStrategy(), frame)

    assert level.reacted is True
    # A later, separate touch episode is a genuinely new event, but only its
    # first confirmation should be emitted.
    assert level.signal_date == "2026-08-06"
    assert level.reaction_signal == "放量止跌"
