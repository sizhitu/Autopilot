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


    def test_history_calculates_causal_5_trading_day_win_rate(self, monkeypatch):
        dates = pd.date_range("2026-08-01", periods=13, freq="B")
        closes = [100.0] * 13
        closes[6] = 110.0  # first event: +10% after 5 trading bars
        closes[7] = 90.0   # second event: -10% after 5 trading bars
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
        assert {event["forward_5d_status"] for event in events} == {"matured"}
        assert sorted(event["forward_5d_win"] for event in events) == [False, True]
        assert all(event["forward_5d_win_rate"] == 0.5 for event in events)
        assert all(event["forward_5d_sample_size"] == 2 for event in events)

    def test_history_marks_last_five_trading_days_as_pending(self, monkeypatch):
        dates = pd.date_range("2026-08-01", periods=5, freq="B")
        frame = pd.DataFrame({
            "date": dates, "open": [100.0] * 5, "high": [101.0] * 5,
            "low": [99.0] * 5, "close": [100.0] * 5, "volume": [100.0] * 5,
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
        assert events[0]["forward_5d_status"] == "pending"
        assert events[0]["forward_5d_win_rate"] is None


    def test_one_confirmation_candle_is_assigned_to_nearest_touched_fib_level(self):
        frame = pd.DataFrame({
            "date": [pd.Timestamp("2026-08-28")],
            "open": [108.0], "high": [110.0], "low": [98.0],
            "close": [101.0], "volume": [200.0],
        })
        levels = [
            FibLevel(level=0.786, price=109.0, reacted=True,
                     reaction_signal="阴线反包受阻", signal_date="2026-08-28"),
            FibLevel(level=0.618, price=104.0, reacted=True,
                     reaction_signal="阴线反包受阻", signal_date="2026-08-28"),
            FibLevel(level=0.500, price=100.5, reacted=True,
                     reaction_signal="阴线反包受阻", signal_date="2026-08-28"),
            FibLevel(level=0.382, price=98.5, reacted=True,
                     reaction_signal="阴线反包受阻", signal_date="2026-08-28"),
        ]
        result = FujimotoStrategy._dedupe_fib_confirmations_by_candle(frame, levels)
        confirmed = [item for item in result if item.reacted]
        assert len(confirmed) == 1
        assert confirmed[0].level == 0.500
        assert confirmed[0].signal_date == "2026-08-28"
        assert all(not item.reacted and item.signal_date is None for item in result if item is not confirmed[0])


    def test_win_rate_cohorts_separate_signal_types_at_same_fib_level(self, monkeypatch):
        dates = pd.date_range("2026-08-03", periods=24, freq="B")
        closes = [100.0] * 24
        # Five subsequent trading bars are used, not ten.
        closes[6] = 110.0
        closes[7] = 90.0
        frame = pd.DataFrame({
            "date": dates, "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes, "volume": [100.0] * 24,
        })
        signal_map = {
            dates[1].strftime("%Y-%m-%d"): "放量止跌",
            dates[2].strftime("%Y-%m-%d"): "阳线反包",
        }

        def fake_bullish(self, prefix, lookback=60):
            day = prefix.iloc[-1]["date"].strftime("%Y-%m-%d")
            signal = signal_map.get(day)
            levels = [FibLevel(
                level=0.618, price=100.0, tested=True, reacted=True,
                reaction_signal=signal, signal_date=day
            )] if signal else []
            return levels, 101.0, 99.0, []

        monkeypatch.setattr(FujimotoStrategy, "_calc_fibonacci", fake_bullish)
        monkeypatch.setattr(
            FujimotoStrategy, "_calc_fibonacci_bearish",
            lambda self, prefix, lookback=60: ([], 101, 99, [])
        )

        events = FujimotoStrategy()._build_fibonacci_signal_history(frame)
        by_signal = {event["signal"]: event for event in events}
        assert by_signal["放量止跌"]["forward_5d_sample_size"] == 1
        assert by_signal["放量止跌"]["forward_5d_win_rate"] == 1.0
        assert by_signal["阳线反包"]["forward_5d_sample_size"] == 1
        assert by_signal["阳线反包"]["forward_5d_win_rate"] == 0.0


class TestFibonacciTouchEpisodeOptimization:
    def test_suffix_scan_is_exactly_equivalent_to_full_prefix_mask(self):
        import numpy as np

        rng = np.random.default_rng(20261113)
        for length in (20, 121, 260):
            lows = rng.uniform(80, 120, length)
            highs = lows + rng.uniform(0.1, 8.0, length)
            frame = pd.DataFrame({"low": lows, "high": highs})
            for anchor in (0, max(0, length // 2), length - 2):
                for price in (90.0, 100.0, 110.0):
                    mask = (
                        (frame["low"] <= price * 1.01)
                        & (frame["high"] >= price * 0.99)
                    ).to_numpy()
                    positions = np.flatnonzero(
                        mask & (np.arange(length) > anchor)
                    )
                    expected = []
                    for pos in positions:
                        if not expected or pos > expected[-1][-1] + 1:
                            expected.append([int(pos)])
                        else:
                            expected[-1].append(int(pos))
                    actual = FujimotoStrategy._fib_touch_episodes(
                        frame, price, anchor
                    )
                    assert actual == expected

    def test_suffix_scan_never_reads_rows_before_anchor(self, monkeypatch):
        import numpy as np

        frame = pd.DataFrame({
            "low": np.full(500, 99.0),
            "high": np.full(500, 101.0),
        })
        # A late anchor means the optimized scan processes only the 20-row suffix.
        episodes = FujimotoStrategy._fib_touch_episodes(frame, 100.0, 479)
        assert episodes == [list(range(480, 500))]


class TestFibonacciCurrentSignalValidity:
    def test_only_last_twenty_trading_bars_on_exact_current_wave_are_current(self):
        dates = pd.bdate_range("2026-10-01", periods=30)
        frame = pd.DataFrame({
            "date": dates,
            "open": [100.0] * 30,
            "high": [101.0] * 30,
            "low": [99.0] * 30,
            "close": [100.0] * 30,
            "volume": [1000.0] * 30,
        })
        current_swing = {
            "direction": "bullish",
            "start_idx": 3,
            "end_idx": 15,
            "confirmation_idx": 18,
        }
        def date_at(index):
            return dates[index].strftime("%Y-%m-%d")

        events = [
            {
                "side": "bullish", "signal_date": date_at(29),
                "swing_direction": "bullish",
                "swing_start_date": date_at(3), "swing_end_date": date_at(15),
            },
            {
                "side": "bullish", "signal_date": date_at(10),
                "swing_direction": "bullish",
                "swing_start_date": date_at(3), "swing_end_date": date_at(15),
            },
            {
                "side": "bullish", "signal_date": date_at(28),
                "swing_direction": "bullish",
                "swing_start_date": date_at(3), "swing_end_date": date_at(14),
            },
        ]

        annotated = FujimotoStrategy._annotate_fibonacci_current_validity(
            frame, events, current_swing
        )

        assert annotated[0]["signal_age_trading_days"] == 0
        assert annotated[0]["is_recent_20d"] is True
        assert annotated[0]["is_current_wave"] is True
        assert annotated[0]["is_current_valid"] is True

        # 19 bars ago is still inside the latest 20 trading bars (0..19).
        assert annotated[1]["signal_age_trading_days"] == 19
        assert annotated[1]["is_current_valid"] is True

        # A recent event from an earlier/different wave must not be current.
        assert annotated[2]["is_recent_20d"] is True
        assert annotated[2]["is_current_wave"] is False
        assert annotated[2]["is_current_valid"] is False

    def test_twenty_trading_bars_ago_is_outside_current_signal_window(self):
        dates = pd.bdate_range("2026-10-01", periods=30)
        frame = pd.DataFrame({
            "date": dates,
            "open": [100.0] * 30,
            "high": [101.0] * 30,
            "low": [99.0] * 30,
            "close": [100.0] * 30,
            "volume": [1000.0] * 30,
        })
        current_swing = {
            "direction": "bullish",
            "start_idx": 3,
            "end_idx": 15,
            "confirmation_idx": 18,
        }
        event = {
            "side": "bullish",
            "signal_date": dates[9].strftime("%Y-%m-%d"),
            "swing_direction": "bullish",
            "swing_start_date": dates[3].strftime("%Y-%m-%d"),
            "swing_end_date": dates[15].strftime("%Y-%m-%d"),
        }

        result = FujimotoStrategy._annotate_fibonacci_current_validity(
            frame, [event], current_swing
        )

        assert result[0]["signal_age_trading_days"] == 20
        assert result[0]["is_recent_20d"] is False
        assert result[0]["is_current_wave"] is True
        assert result[0]["is_current_valid"] is False

    def test_expired_current_wave_cannot_authorize_current_signal(self):
        dates = pd.bdate_range("2026-06-01", periods=110)
        frame = pd.DataFrame({
            "date": dates,
            "open": [100.0] * 110,
            "high": [101.0] * 110,
            "low": [99.0] * 110,
            "close": [100.0] * 110,
            "volume": [1000.0] * 110,
        })
        current_swing = {
            "direction": "bullish",
            "start_idx": 3,
            "end_idx": 15,
            "confirmation_idx": 18,
        }
        event = {
            "side": "bullish",
            "signal_date": dates[-1].strftime("%Y-%m-%d"),
            "swing_direction": "bullish",
            "swing_start_date": dates[3].strftime("%Y-%m-%d"),
            "swing_end_date": dates[15].strftime("%Y-%m-%d"),
        }

        result = FujimotoStrategy._annotate_fibonacci_current_validity(
            frame, [event], current_swing
        )

        assert result[0]["is_recent_20d"] is True
        assert result[0]["is_current_wave"] is False
        assert result[0]["is_current_valid"] is False
