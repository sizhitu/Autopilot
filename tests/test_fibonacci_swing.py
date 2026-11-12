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


def test_bullish_reaction_before_swing_confirmation_is_not_recorded():
    frame = _frame([100, 98, 96, 94, 80, 86, 92, 98, 104, 110, 120, 100, 103, 106])
    # The 120 high at index 10 is confirmed only at index 13. A bullish
    # engulfing touch at index 12 must not be treated as a knowable Fib signal.
    frame.loc[11, ["open", "high", "low", "close"]] = [102, 103, 99, 99]
    frame.loc[12, ["open", "high", "low", "close"]] = [98, 104, 99, 103]
    frame.loc[13, ["open", "high", "low", "close"]] = [104, 107, 103, 106]

    levels, *_ = FujimotoStrategy()._calc_fibonacci(frame)
    level_50 = next(level for level in levels if level.level == 0.5)
    assert level_50.reacted is False
    assert level_50.signal_date is None


def test_bearish_reaction_before_swing_confirmation_is_not_recorded():
    frame = _frame([80, 84, 88, 92, 120, 114, 108, 102, 96, 90, 80, 100, 97, 94])
    # The 80 low at index 10 is confirmed only at index 13. A bearish
    # engulfing touch at index 12 must not be treated as a knowable Fib signal.
    frame.loc[11, ["open", "high", "low", "close"]] = [98, 101, 97, 101]
    frame.loc[12, ["open", "high", "low", "close"]] = [102, 101, 97, 96]
    frame.loc[13, ["open", "high", "low", "close"]] = [95, 97, 93, 94]

    levels, *_ = FujimotoStrategy()._calc_fibonacci_bearish(frame)
    level_50 = next(level for level in levels if level.level == 0.5)
    assert level_50.reacted is False
    assert level_50.signal_date is None


def test_bullish_signal_scan_ignores_current_close_gate_and_uses_full_candle_range():
    frame = _frame([100, 99, 101, 90, 82])
    frame["date"] = pd.date_range("2026-11-01", periods=len(frame), freq="D")
    # 0.618 retracement = 95.28; reaction candles span the whole tolerance band.
    frame.loc[0, ["open", "high", "low", "close"]] = [110, 120, 80, 100]
    frame.loc[1, ["open", "high", "low", "close"]] = [100, 100, 90, 98]
    frame.loc[2, ["open", "high", "low", "close"]] = [97, 102, 90, 101]
    frame.loc[3, ["open", "high", "low", "close"]] = [88, 92, 86, 90]
    frame.loc[4, ["open", "high", "low", "close"]] = [82, 84, 78, 80]

    strategy = FujimotoStrategy()
    swing = {
        "direction": "bullish", "start_idx": 0, "end_idx": 0,
        "start_price": 80.0, "end_price": 120.0, "confirmation_idx": 0,
    }
    strategy._detect_valid_fib_swing = lambda df, lookback=120: swing
    levels, *_ = strategy._calc_fibonacci(frame)
    level = next(item for item in levels if item.level == 0.618)

    assert not bool(level.tested)
    assert level.reacted is True
    assert level.reaction_signal == "阳线反包确认"
    assert level.signal_date == "2026-11-03"


def test_bearish_signal_scan_ignores_current_close_gate_and_uses_full_candle_range():
    frame = _frame([100, 108, 99, 112, 110])
    frame["date"] = pd.date_range("2026-11-01", periods=len(frame), freq="D")
    # 0.618 rebound = 104.72; reaction candles span the whole resistance band.
    frame.loc[0, ["open", "high", "low", "close"]] = [110, 120, 80, 100]
    frame.loc[1, ["open", "high", "low", "close"]] = [100, 110, 100, 108]
    frame.loc[2, ["open", "high", "low", "close"]] = [109, 110, 98, 99]
    frame.loc[3, ["open", "high", "low", "close"]] = [112, 115, 110, 112]
    frame.loc[4, ["open", "high", "low", "close"]] = [110, 112, 108, 110]

    strategy = FujimotoStrategy()
    swing = {
        "direction": "bearish", "start_idx": 0, "end_idx": 0,
        "start_price": 120.0, "end_price": 80.0, "confirmation_idx": 0,
    }
    strategy._detect_valid_fib_swing = lambda df, lookback=120: swing
    levels, *_ = strategy._calc_fibonacci_bearish(frame)
    level = next(item for item in levels if item.level == 0.618)

    assert level.tested is False
    assert level.reacted is True
    assert level.reaction_signal == "阴线反包受阻"
    assert level.signal_date == "2026-11-03"


def test_current_fib_level_matches_its_replayed_history_event():
    frame = _frame([100, 99, 101, 90, 82])
    frame["date"] = pd.date_range("2026-11-01", periods=len(frame), freq="D")
    frame.loc[0, ["open", "high", "low", "close"]] = [110, 120, 80, 100]
    frame.loc[1, ["open", "high", "low", "close"]] = [100, 100, 90, 98]
    frame.loc[2, ["open", "high", "low", "close"]] = [97, 102, 90, 101]
    frame.loc[3, ["open", "high", "low", "close"]] = [88, 92, 86, 90]
    frame.loc[4, ["open", "high", "low", "close"]] = [82, 84, 78, 80]

    strategy = FujimotoStrategy()
    swing = {
        "direction": "bullish", "start_idx": 0, "end_idx": 0,
        "start_price": 80.0, "end_price": 120.0, "confirmation_idx": 0,
    }
    strategy._detect_valid_fib_swing = lambda df, lookback=120: swing
    current, *_ = strategy._calc_fibonacci(frame)
    history = strategy._build_fibonacci_signal_history(frame)
    current_event = next(item for item in current if item.level == 0.618 and item.reacted)
    matching = [
        event for event in history
        if event["side"] == "bullish" and event["level"] == 0.618
        and event["signal_date"] == current_event.signal_date
        and event["signal"] == current_event.reaction_signal
    ]
    assert len(matching) == 1

def test_trend_context_distinguishes_with_trend_and_countertrend_reactions():
    rising = _frame([100 + i for i in range(60)])
    falling = _frame([160 - i for i in range(60)])

    assert FujimotoStrategy._fib_trend_context(rising, "bullish") == {
        "background_trend": "bullish", "signal_context": "顺势"
    }
    assert FujimotoStrategy._fib_trend_context(rising, "bearish") == {
        "background_trend": "bullish", "signal_context": "逆势"
    }
    assert FujimotoStrategy._fib_trend_context(falling, "bearish") == {
        "background_trend": "bearish", "signal_context": "顺势"
    }


def test_trend_context_does_not_force_a_direction_in_a_flat_market():
    flat = _frame([100.0] * 60)
    assert FujimotoStrategy._fib_trend_context(flat, "bullish") == {
        "background_trend": "range_or_transition", "signal_context": "震荡/趋势转换"
    }


def test_fib_swing_expires_after_90_calendar_days_but_not_on_day_90():
    frame = _frame([100 + i for i in range(91)])
    frame.index = pd.date_range("2026-01-01", periods=len(frame), freq="D")
    swing = {"confirmation_idx": 0, "direction": "bullish"}

    assert FujimotoStrategy._is_fib_swing_fresh(frame.iloc[:91], swing)
    assert FujimotoStrategy._is_fib_swing_fresh(frame.iloc[:91], swing, max_age_days=90)

    expired = frame.copy()
    expired.loc[len(expired)] = expired.iloc[-1]
    expired.index = pd.date_range("2026-01-01", periods=len(expired), freq="D")
    assert not FujimotoStrategy._is_fib_swing_fresh(expired, swing)


def test_expired_bullish_and_bearish_swings_are_not_current_fib_levels(monkeypatch):
    strategy = FujimotoStrategy()
    frame = _frame([100 + i for i in range(140)])
    frame["date"] = pd.date_range("2026-01-01", periods=len(frame), freq="D").astype(str)
    swing = {
        "direction": "bullish",
        "start_idx": 5,
        "end_idx": 20,
        "start_price": 100.0,
        "end_price": 130.0,
        "confirmation_idx": 23,
    }
    monkeypatch.setattr(strategy, "_detect_valid_fib_swing", lambda df, lookback=120: swing)
    bullish, *_ = strategy._calc_fibonacci(frame)
    assert bullish == []

    bearish_swing = dict(swing, direction="bearish", start_price=130.0, end_price=100.0)
    monkeypatch.setattr(strategy, "_detect_valid_fib_swing", lambda df, lookback=120: bearish_swing)
    bearish, *_ = strategy._calc_fibonacci_bearish(frame)
    assert bearish == []
