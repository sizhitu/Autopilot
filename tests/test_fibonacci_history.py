"""Regression tests for the reconstructed Fibonacci event history."""
import pandas as pd

from trading_tool.strategy_engine import FibLevel, FujimotoStrategy


class TestFibonacciSignalHistory:
    def test_history_replays_each_day_and_keeps_bullish_and_bearish_events(self, monkeypatch):
        dates = pd.date_range("2026-08-01", periods=5, freq="D")
        frame = pd.DataFrame({
            "date": dates,
            "open": [10, 11, 12, 13, 14],
            "high": [12, 13, 14, 15, 16],
            "low": [9, 10, 11, 12, 13],
            "close": [11, 12, 13, 14, 15],
            "volume": [100, 110, 120, 130, 140],
        })
        bullish_dates = {"2026-08-02", "2026-08-05"}
        bearish_dates = {"2026-08-03"}

        def fake_levels(side_dates, date, signal):
            return [
                FibLevel(
                    level=0.618, price=12.345, tested=True, reacted=True,
                    reaction_signal=signal, signal_date=date
                )
            ] if date in side_dates else []

        def fake_bullish(self, prefix, lookback=60):
            day = prefix.iloc[-1]["date"].strftime("%Y-%m-%d")
            return fake_levels(bullish_dates, day, "放量止跌"), 16, 9, []

        def fake_bearish(self, prefix, lookback=60):
            day = prefix.iloc[-1]["date"].strftime("%Y-%m-%d")
            return fake_levels(bearish_dates, day, "放量受阻"), 16, 9, []

        monkeypatch.setattr(FujimotoStrategy, "_calc_fibonacci", fake_bullish)
        monkeypatch.setattr(FujimotoStrategy, "_calc_fibonacci_bearish", fake_bearish)

        events = FujimotoStrategy()._build_fibonacci_signal_history(frame)
        assert [(e["side"], e["signal_date"]) for e in events] == [
            ("bullish", "2026-08-05"),
            ("bearish", "2026-08-03"),
            ("bullish", "2026-08-02"),
        ]
        assert all(e["price"] == 12.345 for e in events)

    def test_history_does_not_repeat_old_confirmation_on_later_days(self, monkeypatch):
        frame = pd.DataFrame({
            "date": pd.date_range("2026-08-01", periods=4, freq="D"),
            "open": [10, 11, 12, 13],
            "high": [12, 13, 14, 15],
            "low": [9, 10, 11, 12],
            "close": [11, 12, 13, 14],
            "volume": [100, 110, 120, 130],
        })

        def levels(prefix):
            # Simulates an existing confirmation remaining visible in latest state
            # after its actual event day; history must only record the event once.
            return [FibLevel(
                level=0.5, price=12.0, tested=True, reacted=True,
                reaction_signal="放量止跌", signal_date="2026-08-02"
            )]

        monkeypatch.setattr(
            FujimotoStrategy, "_calc_fibonacci",
            lambda self, prefix, lookback=60: (levels(prefix), 15, 9, [])
        )
        monkeypatch.setattr(
            FujimotoStrategy, "_calc_fibonacci_bearish",
            lambda self, prefix, lookback=60: ([], 15, 9, [])
        )

        events = FujimotoStrategy()._build_fibonacci_signal_history(frame)
        assert len(events) == 1
        assert events[0]["signal_date"] == "2026-08-02"

    def test_history_ignores_rows_without_real_trading_dates(self):
        frame = pd.DataFrame({
            "open": [10, 11, 12],
            "high": [12, 13, 14],
            "low": [9, 10, 11],
            "close": [11, 12, 13],
            "volume": [100, 110, 120],
        })
        frame.index = [100, 101, 102]
        events = FujimotoStrategy()._build_fibonacci_signal_history(frame)
        assert events == []



class TestFibonacciDirectionalVolumeConfirmation:
    @staticmethod
    def _bars(last):
        rows = [
            {"open": 100, "high": 102, "low": 98, "close": 100, "volume": 100},
            {"open": 100, "high": 102, "low": 98, "close": 101, "volume": 110},
            {"open": 101, "high": 103, "low": 99, "close": 100, "volume": 90},
            {"open": 100, "high": 102, "low": 98, "close": 101, "volume": 100},
            {"open": 101, "high": 103, "low": 99, "close": 100, "volume": 100},
        ]
        rows.append(last)
        frame = pd.DataFrame(rows)
        frame["date"] = pd.date_range("2026-10-01", periods=len(frame), freq="D")
        return frame

    def test_bullish_volume_confirmation_requires_bullish_price_action(self):
        frame = self._bars({
            "open": 100, "high": 105, "low": 99, "close": 104,
            "volume": 150,
        })
        assert FujimotoStrategy._is_directional_volume_confirmation(frame, 5, "bullish")
        assert not FujimotoStrategy._is_directional_volume_confirmation(frame, 5, "bearish")

    def test_bearish_volume_confirmation_requires_bearish_price_action(self):
        frame = self._bars({
            "open": 104, "high": 105, "low": 98, "close": 99,
            "volume": 150,
        })
        assert FujimotoStrategy._is_directional_volume_confirmation(frame, 5, "bearish")
        assert not FujimotoStrategy._is_directional_volume_confirmation(frame, 5, "bullish")

    def test_volume_spike_without_directional_close_is_not_a_confirmation(self):
        cases = [
            {"open": 100, "high": 105, "low": 99, "close": 100, "volume": 150},
            {"open": 101, "high": 105, "low": 99, "close": 100, "volume": 150},
            {"open": 100, "high": 105, "low": 99, "close": 104, "volume": 110},
        ]
        for candle in cases:
            frame = self._bars(candle)
            assert not FujimotoStrategy._is_directional_volume_confirmation(frame, 5, "bullish")
            assert not FujimotoStrategy._is_directional_volume_confirmation(frame, 5, "bearish")

    def test_bullish_and_bearish_volume_confirmations_are_mutually_exclusive(self):
        bullish = self._bars({
            "open": 100, "high": 105, "low": 99, "close": 104, "volume": 150,
        })
        bearish = self._bars({
            "open": 104, "high": 105, "low": 98, "close": 99, "volume": 150,
        })
        assert FujimotoStrategy._is_directional_volume_confirmation(bullish, 5, "bullish")
        assert not FujimotoStrategy._is_directional_volume_confirmation(bullish, 5, "bearish")
        assert FujimotoStrategy._is_directional_volume_confirmation(bearish, 5, "bearish")
        assert not FujimotoStrategy._is_directional_volume_confirmation(bearish, 5, "bullish")

    def test_requires_at_least_three_prior_volume_observations(self):
        frame = self._bars({
            "open": 100, "high": 105, "low": 99, "close": 104, "volume": 150,
        }).iloc[:3].copy()
        assert not FujimotoStrategy._is_directional_volume_confirmation(frame, 2, "bullish")


    def test_history_calculates_causal_10_trading_day_win_rate(self, monkeypatch):
        dates = pd.date_range("2026-08-01", periods=13, freq="B")
        closes = [100.0] * 13
        closes[11] = 110.0  # first event: +10% after 10 trading bars
        closes[12] = 90.0   # second event: -10% after 10 trading bars
        frame = pd.DataFrame({
            "date": dates,
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
            "volume": [100.0] * 13,
        })
        signal_dates = {dates[1].strftime("%Y-%m-%d"), dates[2].strftime("%Y-%m-%d")}

        def fake_bullish(self, prefix, lookback=60):
            day = prefix.iloc[-1]["date"].strftime("%Y-%m-%d")
            levels = [FibLevel(
                level=0.618, price=100.0, tested=True, reacted=True,
                reaction_signal="放量止跌", signal_date=day
            )] if day in signal_dates else []
            return levels, 101.0, 99.0, []

        monkeypatch.setattr(FujimotoStrategy, "_calc_fibonacci", fake_bullish)
        monkeypatch.setattr(FujimotoStrategy, "_calc_fibonacci_bearish", lambda self, prefix, lookback=60: ([], 101, 99, []))
        events = FujimotoStrategy()._build_fibonacci_signal_history(frame)
        assert len(events) == 2
        assert {event["forward_10d_status"] for event in events} == {"matured"}
        assert sorted(event["forward_10d_win"] for event in events) == [False, True]
        assert all(event["forward_10d_win_rate"] == 0.5 for event in events)
        assert all(event["forward_10d_sample_size"] == 2 for event in events)

    def test_history_marks_last_ten_trading_days_as_pending(self, monkeypatch):
        dates = pd.date_range("2026-08-01", periods=8, freq="B")
        frame = pd.DataFrame({
            "date": dates, "open": [100.0] * 8, "high": [101.0] * 8,
            "low": [99.0] * 8, "close": [100.0] * 8, "volume": [100.0] * 8,
        })
        day = dates[1].strftime("%Y-%m-%d")
        monkeypatch.setattr(
            FujimotoStrategy, "_calc_fibonacci",
            lambda self, prefix, lookback=60: (
                [FibLevel(level=0.5, price=100, tested=True, reacted=True,
                          reaction_signal="放量止跌", signal_date=day)] if prefix.iloc[-1]["date"].strftime("%Y-%m-%d") == day else [],
                101, 99, []
            )
        )
        monkeypatch.setattr(FujimotoStrategy, "_calc_fibonacci_bearish", lambda self, prefix, lookback=60: ([], 101, 99, []))
        events = FujimotoStrategy()._build_fibonacci_signal_history(frame)
        assert len(events) == 1
        assert events[0]["forward_10d_status"] == "pending"
        assert events[0]["forward_10d_win_rate"] is None
