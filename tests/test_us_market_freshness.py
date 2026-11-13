from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

import data_fetcher


def test_us_expected_session_date_uses_new_york_dst_and_close_time():
    before_close = datetime(2026, 11, 13, 15, 30, tzinfo=ZoneInfo("America/New_York"))
    after_close = datetime(2026, 11, 13, 16, 30, tzinfo=ZoneInfo("America/New_York"))

    assert data_fetcher._expected_session_date("us", before_close) == date(2026, 11, 12)
    assert data_fetcher._expected_session_date("us", after_close) == date(2026, 11, 13)
    assert before_close.utcoffset().total_seconds() == -5 * 3600


def test_us_bar_one_missed_trading_day_is_stale(monkeypatch):
    monkeypatch.setattr(data_fetcher, "_expected_session_date", lambda market="us": date(2026, 11, 12))

    assert data_fetcher._bar_is_stale(date(2026, 11, 11), "us")
    assert not data_fetcher._bar_is_stale(date(2026, 11, 12), "us")


def test_stale_long_yahoo_history_still_tries_fresh_fallback(monkeypatch):
    dates = pd.date_range(end="2026-11-11", periods=100, freq="B")
    timestamps = [int(ts.timestamp()) for ts in dates]
    quote = {
        "open": [100.0] * len(dates),
        "high": [101.0] * len(dates),
        "low": [99.0] * len(dates),
        "close": [100.0] * len(dates),
        "volume": [1000] * len(dates),
    }
    payload = {
        "chart": {
            "result": [{
                "timestamp": timestamps,
                "indicators": {"quote": [quote]},
                "meta": {},
            }],
            "error": None,
        }
    }

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return payload

    class Session:
        @staticmethod
        def get(*args, **kwargs):
            return Response()

    monkeypatch.setattr(data_fetcher, "_expected_session_date", lambda market="us": date(2026, 11, 12))
    monkeypatch.setattr(data_fetcher, "_yahoo_in_cooldown", lambda: False)
    monkeypatch.setattr(data_fetcher, "_clear_yahoo_cooldown", lambda: None)
    monkeypatch.setattr(data_fetcher, "_trigger_yahoo_cooldown", lambda: None)

    fallback = pd.DataFrame([{
        "date": pd.Timestamp("2026-11-12"),
        "open": 102.0,
        "high": 103.0,
        "low": 101.0,
        "close": 102.0,
        "volume": 1200.0,
    }])
    called = {"nasdaq": 0}

    def nasdaq(self, symbol, days):
        called["nasdaq"] += 1
        return fallback

    def stooq(self, symbol, days):
        raise RuntimeError("not available in this test")

    monkeypatch.setattr(data_fetcher.DataFetcher, "_fetch_us_stock_nasdaq", nasdaq)
    monkeypatch.setattr(data_fetcher.DataFetcher, "_fetch_stooq", stooq)

    fetcher = data_fetcher.DataFetcher()
    fetcher.session = Session()
    result = fetcher.fetch_us_stock("AAPL", days=300)

    assert called["nasdaq"] == 1
    assert data_fetcher._df_last_date(result) == date(2026, 11, 12)
    assert len(result) == 101



def test_stale_full_yahoo_history_returns_without_slow_fallback(monkeypatch):
    dates = pd.date_range(end="2026-11-11", periods=270, freq="B")
    timestamps = [int(ts.timestamp()) for ts in dates]
    quote = {
        "open": [100.0] * len(dates),
        "high": [101.0] * len(dates),
        "low": [99.0] * len(dates),
        "close": [100.0] * len(dates),
        "volume": [1000] * len(dates),
    }
    payload = {"chart": {"result": [{
        "timestamp": timestamps,
        "indicators": {"quote": [quote]},
        "meta": {},
    }], "error": None}}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return payload

    class Session:
        @staticmethod
        def get(*args, **kwargs):
            return Response()

    monkeypatch.setattr(data_fetcher, "_expected_session_date", lambda market="us": date(2026, 11, 12))
    monkeypatch.setattr(data_fetcher, "_yahoo_in_cooldown", lambda: False)
    monkeypatch.setattr(data_fetcher, "_clear_yahoo_cooldown", lambda: None)
    monkeypatch.setattr(data_fetcher, "_trigger_yahoo_cooldown", lambda: None)

    def should_not_call(*args, **kwargs):
        raise AssertionError("long Yahoo history must not wait on fallback providers")

    monkeypatch.setattr(data_fetcher.DataFetcher, "_fetch_stooq", should_not_call)
    monkeypatch.setattr(data_fetcher.DataFetcher, "_fetch_us_stock_nasdaq", should_not_call)

    fetcher = data_fetcher.DataFetcher()
    fetcher.session = Session()
    result = fetcher.fetch_us_stock("AAPL", days=300)

    assert len(result) == 270
    assert data_fetcher._df_last_date(result) == date(2026, 11, 11)


def test_stooq_long_history_is_preferred_before_short_nasdaq_fallback(monkeypatch):
    class Response:
        status_code = 429

    class Session:
        @staticmethod
        def get(*args, **kwargs):
            return Response()

    monkeypatch.setattr(data_fetcher, "_yahoo_in_cooldown", lambda: False)
    monkeypatch.setattr(data_fetcher, "_trigger_yahoo_cooldown", lambda: None)

    stooq = pd.DataFrame({
        "date": pd.date_range(end="2026-11-12", periods=270, freq="B"),
        "open": [100.0] * 270,
        "high": [101.0] * 240,
        "low": [99.0] * 240,
        "close": [100.0] * 240,
        "volume": [1000.0] * 240,
    })

    monkeypatch.setattr(data_fetcher.DataFetcher, "_fetch_stooq", lambda self, symbol, days: stooq)

    def should_not_call(*args, **kwargs):
        raise AssertionError("Nasdaq short-history fallback must not run when Stooq has long history")

    monkeypatch.setattr(data_fetcher.DataFetcher, "_fetch_us_stock_nasdaq", should_not_call)

    fetcher = data_fetcher.DataFetcher()
    fetcher.session = Session()
    result = fetcher.fetch_us_stock("AAPL", days=300)

    assert len(result) == 270
    assert data_fetcher._df_last_date(result) == date(2026, 11, 12)
