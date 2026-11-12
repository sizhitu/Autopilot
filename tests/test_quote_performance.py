"""Performance regressions for quote analysis and Fibonacci history loading."""
from pathlib import Path

import pandas as pd

from trading_tool.strategy_engine import FujimotoStrategy, generate_sample_data


ROOT = Path(__file__).resolve().parents[1]


def test_lite_analysis_skips_expensive_fibonacci_history_replay(monkeypatch):
    calls = []

    def fail_if_called(self, df, lookback=60):
        calls.append(len(df))
        raise AssertionError("lite/backtest path must not replay Fibonacci history")

    monkeypatch.setattr(FujimotoStrategy, "_build_fibonacci_signal_history", fail_if_called)
    result = FujimotoStrategy().analyze(generate_sample_data(90), _lite=True)
    assert result.chart_data["fib_signal_history"] == []
    assert calls == []


def test_full_analysis_still_builds_fibonacci_history(monkeypatch):
    calls = []

    def fake_history(self, df, lookback=60):
        calls.append(len(df))
        return [{"side": "bullish", "level": 0.618, "price": 10.0,
                 "signal": "放量止跌", "signal_date": "2026-11-10"}]

    monkeypatch.setattr(FujimotoStrategy, "_build_fibonacci_signal_history", fake_history)
    result = FujimotoStrategy().analyze(generate_sample_data(90))
    assert calls == [90]
    assert result.chart_data["fib_signal_history"][0]["signal_date"] == "2026-11-10"


def test_quote_endpoint_has_same_symbol_singleflight_lock():
    source = (ROOT / "trading_tool" / "web_app.py").read_text(encoding="utf-8")
    assert "_QUOTE_LOCKS" in source
    assert "async with lock:" in source
    assert "effective_req = req.copy(update={\"force\": False})" in source


def test_frontend_deduplicates_analysis_requests_and_does_not_force_unlock():
    source = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "const _analysisInFlight = new Map();" in source
    assert "_analysisInFlight.has(key)" in source
    assert "_analysisLoadStarted" not in source
    assert "setTimeout(function () { _analysisLoading = false; }, 8000)" not in source


def test_fibonacci_history_loads_latest_record_first_and_preserves_previous_data():
    source = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    loader = source[source.index("async function loadFibSignalHistory()"):
                    source.index("function updateResults(data, meta, nineTurn)")]
    assert "'&limit=5'" in loader
    assert "'&limit=100'" in loader  # legacy fallback only
    assert "保留上次数据" in loader

def test_fibonacci_history_expands_for_mixed_legacy_and_archive_snapshots():
    source = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    loader = source[source.index("async function loadFibSignalHistory()"):
                    source.index("function updateResults(data, meta, nineTurn)")]
    assert "const archivedEvents = new Map();" in loader
    assert "const collectSnapshot = data =>" in loader
    assert "data.fib_signal_history.forEach(addArchivedEvent);" in loader
    assert "if (legacyRecords.length > 0)" in loader
    assert "'&limit=100'" in loader
    assert "for (const item of items) collectSnapshot(parseRecord(item));" in loader
    expanded_query = loader.index("if (legacyRecords.length > 0)")
    collect_expanded = loader.index("for (const item of items) collectSnapshot(parseRecord(item));", expanded_query)
    convert_expanded_legacy = loader.index("addLegacyEvents(legacyRecords);", collect_expanded)
    assert collect_expanded < convert_expanded_legacy
    assert "events = Array.from(archivedEvents.values());" in loader
    assert "if (!hasEventArchive)" not in loader
    assert "break;" not in loader


def test_fibonacci_legacy_migration_reads_both_bullish_and_bearish_levels():
    source = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    loader = source[source.index("async function loadFibSignalHistory()"):
                    source.index("function updateResults(data, meta, nineTurn)")]
    assert "['bullish', 'fib_levels']" in loader
    assert "['bearish', 'fib_bearish_levels']" in loader
    assert "for (const [side, field] of" in loader
    assert "side, level: fl.level, price: fl.price" in loader


def test_fibonacci_history_deduplicates_archive_and_legacy_events_with_same_key():
    source = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    loader = source[source.index("async function loadFibSignalHistory()"):
                    source.index("function updateResults(data, meta, nineTurn)")]
    assert "const key = [normalized.side, normalized.level, normalized.date, normalized.signal].join('|');" in loader
    assert "if (!archivedEvents.has(key)) archivedEvents.set(key, normalized);" in loader
    assert "for (const item of items) collectSnapshot(parseRecord(item));" in loader
