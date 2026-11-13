"""Regression tests for IPO-aware short-history validation and caching."""
import pandas as pd

import trading_tool.data_fetcher as data_fetcher


def _bars(count=108, listing_date="2026-06-01"):
    frame = pd.DataFrame({
        "date": pd.bdate_range("2026-06-01", periods=count),
        "open": [10.0] * count,
        "high": [11.0] * count,
        "low": [9.0] * count,
        "close": [10.0] * count,
        "volume": [1000.0] * count,
    })
    if listing_date:
        frame.attrs["listing_date"] = listing_date
    return frame


def test_complete_ipo_history_is_recognized_only_with_source_listing_metadata(monkeypatch):
    monkeypatch.setattr(data_fetcher, "_bar_is_stale", lambda last_date, market="us": False)

    assert data_fetcher.is_complete_ipo_history(_bars())
    assert not data_fetcher.is_complete_ipo_history(_bars(listing_date=None))

    truncated = _bars()
    truncated["date"] = pd.bdate_range("2026-07-01", periods=len(truncated))
    assert not data_fetcher.is_complete_ipo_history(truncated)


def test_complete_ipo_history_is_cached_and_reused_without_refetch(monkeypatch):
    monkeypatch.setattr(data_fetcher, "_bar_is_stale", lambda last_date, market="us": False)
    monkeypatch.setattr(data_fetcher, "_load_stored_daily_frame", lambda symbol: None)
    data_fetcher._KLINE_CACHE.clear()
    data_fetcher._KLINE_CACHE_TS.clear()

    calls = []
    frame = _bars()

    def fake_fetch_us_stock(self, symbol, days):
        calls.append(days)
        return frame.copy()

    monkeypatch.setattr(data_fetcher.DataFetcher, "fetch_us_stock", fake_fetch_us_stock)
    fetcher = data_fetcher.DataFetcher()

    first = fetcher.fetch("SPCX", 300)
    second = fetcher.fetch("SPCX", 300)

    assert len(first) == 108
    assert len(second) == 108
    assert first.attrs["listing_date"] == "2026-06-01"
    assert second.attrs["listing_date"] == "2026-06-01"
    assert calls == [300]


def test_short_history_without_listing_metadata_is_not_cached_as_complete(monkeypatch):
    monkeypatch.setattr(data_fetcher, "_bar_is_stale", lambda last_date, market="us": False)
    monkeypatch.setattr(data_fetcher, "_load_stored_daily_frame", lambda symbol: None)
    data_fetcher._KLINE_CACHE.clear()
    data_fetcher._KLINE_CACHE_TS.clear()

    calls = []
    frame = _bars(listing_date=None)

    def fake_fetch_us_stock(self, symbol, days):
        calls.append(days)
        return frame.copy()

    monkeypatch.setattr(data_fetcher.DataFetcher, "fetch_us_stock", fake_fetch_us_stock)
    fetcher = data_fetcher.DataFetcher()

    fetcher.fetch("OLDSTOCK", 300)
    fetcher.fetch("OLDSTOCK", 300)

    # 未知上市日时仍走原有长历史补取流程，短结果不会进入K线缓存。
    assert calls == [300, 400, 300, 400]
    assert ("OLDSTOCK", 300) not in data_fetcher._KLINE_CACHE
