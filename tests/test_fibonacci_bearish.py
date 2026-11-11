"""Independent regression tests for bearish Fibonacci reference signals.

These tests intentionally live in a dedicated class/file and verify that the
new bearish reference path is isolated from the existing bullish strategy.
"""
import pandas as pd

from trading_tool.strategy_engine import FujimotoStrategy


class TestBearishFibonacciSignals:
    @staticmethod
    def bars(rows, dates=True):
        frame = pd.DataFrame(rows)
        if dates:
            frame.insert(0, "date", pd.date_range("2026-08-01", periods=len(frame), freq="D"))
        return frame

    @staticmethod
    def anchor():
        # Establish a fixed 120 -> 80 swing; 0.786 rebound resistance is 111.44.
        return {
            "open": 110.0, "high": 120.0, "low": 80.0,
            "close": 100.0, "volume": 1000.0,
        }

    @staticmethod
    def touch(open_, close, volume=100.0, high=111.5, low=100.0):
        return {
            "open": float(open_), "high": float(high), "low": float(low),
            "close": float(close), "volume": float(volume),
        }

    def get_0786(self, frame):
        levels, high, low, targets = FujimotoStrategy()._calc_fibonacci_bearish(
            frame, lookback=60
        )
        assert high == 120.0
        assert low == 80.0
        assert targets and len(levels) == 4
        return next(level for level in levels if level.level == 0.786)

    def test_bearish_engulfing_confirms_rebound_resistance(self):
        frame = self.bars([
            self.anchor(),
            self.touch(108, 110, high=111.5),
            self.touch(111, 107, high=112.0),  # bearish body engulfs prior bullish body
        ])
        level = self.get_0786(frame)
        assert level.price == 111.44
        assert bool(level.tested) is True
        assert level.reacted is True
        assert level.reaction_signal == "阴线反包受阻"
        assert level.signal_date == "2026-08-03"

    def test_volume_rejection_confirms_once_using_same_1_2_threshold(self):
        frame = self.bars([
            self.anchor(),
            self.touch(108, 109, volume=100, high=111.5),
            self.touch(109, 108, volume=121, high=112.0),
        ])
        level = self.get_0786(frame)
        assert level.reacted is True
        assert level.reaction_signal == "放量受阻"
        assert level.signal_date == "2026-08-03"

    def test_continuous_resistance_touches_emit_only_first_confirmation(self):
        frame = self.bars([
            self.anchor(),
            self.touch(108, 109, volume=100, high=111.5),
            self.touch(109, 108, volume=130, high=112.0),
            self.touch(109, 107, volume=250, high=112.0),
        ])
        level = self.get_0786(frame)
        assert level.reacted is True
        assert level.signal_date == "2026-08-03"

    def test_new_confirmation_can_appear_after_price_leaves_and_retests(self):
        frame = self.bars([
            self.anchor(),
            self.touch(108, 109, volume=100, high=111.5),
            self.touch(109, 108, volume=100, high=112.0),
            self.touch(100, 101, volume=100, high=105.0),  # leaves resistance zone
            self.touch(108, 109, volume=100, high=111.5),
            self.touch(111, 107, volume=150, high=112.0),
        ])
        level = self.get_0786(frame)
        assert level.reacted is True
        assert level.reaction_signal == "阴线反包受阻"
        assert level.signal_date == "2026-08-06"

    def test_touch_without_confirmation_is_not_a_signal(self):
        frame = self.bars([
            self.anchor(),
            self.touch(108, 109, volume=100, high=111.5),
            self.touch(109, 108.5, volume=100, high=112.0),
        ])
        level = self.get_0786(frame)
        assert bool(level.tested) is True
        assert level.reacted is False
        assert level.reaction_signal == ""
        assert level.signal_date is None

    def test_level_not_reached_by_rebound_does_not_emit_signal(self):
        frame = self.bars([
            self.anchor(),
            self.touch(95, 96, volume=100, high=100.0),
            self.touch(96, 97, volume=100, high=101.0),
        ])
        level = self.get_0786(frame)
        # No candle high enters the ±1% resistance touch zone.
        assert all(not (level.price * 0.99 <= high <= level.price * 1.01)
                   for high in frame["high"])
        assert level.reacted is False

    def test_numeric_index_is_not_misreported_as_trading_date(self):
        frame = self.bars([
            self.anchor(),
            self.touch(108, 109, volume=100, high=111.5),
            self.touch(111, 107, volume=100, high=112.0),
        ], dates=False)
        frame.index = [100, 101, 102]
        level = self.get_0786(frame)
        assert level.reacted is True
        assert level.signal_date is None

    def test_flat_price_range_returns_no_bearish_levels(self):
        frame = self.bars([
            {"open": 100, "high": 100, "low": 100, "close": 100, "volume": 100},
            {"open": 100, "high": 100, "low": 100, "close": 100, "volume": 100},
        ])
        levels, high, low, targets = FujimotoStrategy()._calc_fibonacci_bearish(frame)
        assert levels == []
        assert high == low == 100
        assert targets == []

    def test_bearish_reference_does_not_change_existing_strategy_decision(self, monkeypatch):
        from trading_tool.strategy_engine import FibLevel, generate_sample_data

        frame = generate_sample_data(120, seed=17)
        baseline = FujimotoStrategy().analyze(frame)
        monkeypatch.setattr(
            FujimotoStrategy,
            "_calc_fibonacci_bearish",
            lambda self, df, lookback=60: (
                [FibLevel(0.786, 111.44, True, True, "阴线反包受阻", "2026-08-03")],
                120.0, 80.0, [],
            ),
        )
        with_reference = FujimotoStrategy().analyze(frame)

        assert with_reference.signal == baseline.signal
        assert with_reference.action == baseline.action
        assert with_reference.position_pct == baseline.position_pct
        assert with_reference.layers_consistent == baseline.layers_consistent

    def test_existing_bullish_calculation_remains_independently_available(self):
        frame = self.bars([
            {"open": 86, "high": 100, "low": 80, "close": 90, "volume": 100},
            {"open": 86, "high": 100, "low": 84.2, "close": 85, "volume": 100},
            {"open": 86, "high": 100, "low": 84.2, "close": 85, "volume": 130},
        ])
        bullish, *_ = FujimotoStrategy()._calc_fibonacci(frame, lookback=60)
        bearish, *_ = FujimotoStrategy()._calc_fibonacci_bearish(frame, lookback=60)
        assert len(bullish) == 4
        assert len(bearish) == 4
        assert bullish[0].price != bearish[0].price
