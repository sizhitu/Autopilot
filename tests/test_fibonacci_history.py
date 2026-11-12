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


class TestFibonacciHistoryExactOptimization:
    def test_optimized_history_matches_reference_for_randomized_unique_date_series(self):
        import numpy as np

        rng = np.random.default_rng(20261112)
        strategy = FujimotoStrategy()
        for seed in range(12):
            count = 75 + seed * 7
            close = 100 + np.cumsum(rng.normal(0, 2, count))
            spread = rng.uniform(0.2, 4.0, count)
            frame = pd.DataFrame({
                "date": pd.date_range("2025-01-01", periods=count, freq="B"),
                "open": close + rng.normal(0, 1.2, count),
                "high": close + spread,
                "low": close - spread,
                "close": close,
                "volume": rng.uniform(100, 1000, count),
            })
            # Inject both bullish/bearish engulfing and volume spikes, plus long touch runs.
            for i in range(2, count, 11):
                frame.loc[i, "volume"] = frame.loc[i - 1, "volume"] * 1.35
            optimized = strategy._build_fibonacci_signal_history(frame, lookback=60)
            reference = strategy._build_fibonacci_signal_history_reference(frame, lookback=60)
            assert optimized == reference, f"history mismatch for deterministic seed {seed}"

    def test_optimized_history_does_not_call_prefix_fibonacci_calculators(self, monkeypatch):
        frame = _fibonacci_test_bars(
            lows=[80, 84.2, 84.2, 95, 84.2, 84.2, 84.2],
            volumes=[100, 100, 130, 100, 100, 150, 300],
        )

        def forbidden(*args, **kwargs):
            raise AssertionError("optimized replay must not re-run prefix Fib calculators")

        monkeypatch.setattr(FujimotoStrategy, "_calc_fibonacci", forbidden)
        monkeypatch.setattr(FujimotoStrategy, "_calc_fibonacci_bearish", forbidden)
        strategy = FujimotoStrategy()
        optimized = strategy._build_fibonacci_signal_history(frame)
        assert isinstance(optimized, list)

    def test_duplicate_calendar_dates_use_exact_legacy_compatibility_path(self, monkeypatch):
        frame = _fibonacci_test_bars(
            lows=[80, 84.2, 84.2, 95],
            volumes=[100, 100, 130, 100],
        )
        frame.loc[2, "date"] = frame.loc[1, "date"]
        calls = []
        original = FujimotoStrategy._build_fibonacci_signal_history_reference

        def tracked(self, df, lookback=60):
            calls.append(True)
            return original(self, df, lookback=lookback)

        monkeypatch.setattr(FujimotoStrategy, "_build_fibonacci_signal_history_reference", tracked)
        FujimotoStrategy()._build_fibonacci_signal_history(frame)
        assert calls == [True]
