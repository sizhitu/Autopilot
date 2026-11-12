from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_analysis_panel_only_exposes_indicators_and_fibonacci():
    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    start = html.index("<!-- 分析结果面板")
    end = html.index("<!-- /分析模式视图 -->", start)
    panel = html[start:end]

    assert 'switchTab(\'indicators\'' in panel
    assert 'switchTab(\'fibonacci\'' in panel
    assert "回测配置" not in panel
    assert "管理" not in panel
    assert 'id="tab-backtest"' not in html
    assert 'id="tab-admin"' not in html
    assert "const SIDE_PANELS = ['indicators', 'fibonacci'];" in html

    # Backtest execution remains available from the toolbar/chart view,
    # but no longer depends on controls in the removed config panel.
    assert 'onclick="runBacktest()"' in html
    assert 'id="viewBacktest"' in html
    for removed_id in ("btDays", "btCapital", "btRisk", "btMaxPos", "btCommission", "btWarmup"):
        assert f'id="{removed_id}"' not in html
        assert f"getElementById('{removed_id}')" not in html


def test_fibonacci_levels_are_already_saved_inside_analysis_history():
    web_app = (ROOT / "trading_tool/web_app.py").read_text(encoding="utf-8")
    assert '"fib_levels": [' in web_app
    assert '"data": payload["data"]' in web_app
    assert "analysis_store.add(user[\"id\"], req.symbol" in web_app


def test_analysis_history_archive_preserves_jsonb_payload():
    path = ROOT / "scripts/archive/archive_analysis_history.py"
    spec = importlib.util.spec_from_file_location("archive_analysis_history", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    frame = module._rows_to_frame([{
        "id": 1,
        "user_id": "user-1",
        "symbol": "NVDA",
        "name": "NVIDIA",
        "created_at": "2026-11-11T00:00:00Z",
        "result_json": {"data": {"fib_levels": [{"level": 0.618, "price": 100.0}]}},
    }])
    archived = frame.loc[0, "result_json"]
    assert isinstance(archived, str)
    assert json.loads(archived)["data"]["fib_levels"][0]["level"] == 0.618


def test_analysis_history_archive_reads_all_pages():
    source = (ROOT / "scripts/archive/archive_analysis_history.py").read_text(encoding="utf-8")
    assert ".range(start, start + page_size - 1)" in source
    assert "start += page_size" in source
    assert '.table("analysis_history")' in source
    assert '"result_json"' in source


def test_fibonacci_panel_keeps_signal_history_with_concise_fallback_dates():
    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")

    assert "const fibUpdatedAt = String((meta && meta.end_date) || (meta && meta.updated_at) || (data && data.updated_at) || '').slice(0, 10);" in html
    assert "历史强信号" in html
    assert "loadFibSignalHistory();" in html
    assert "const date = String(fl.signal_date || '').slice(0,10);" in html
    assert "if (!date) continue;" in html
    history_loader = html[html.index("async function loadFibSignalHistory()"):html.index("function updateResults(data, meta, nineTurn)")]
    assert "const dataDate =" not in history_loader
    assert "fl.signal_date || dataDate" not in history_loader
    assert 'class="fib-history-head"' in html
    assert "记录于" not in html
    assert "旧记录未保存信号发生日" not in html
    assert "仅供参考 · 不构成投资建议" not in html
    assert "刷新</button>" not in html[html.index("历史强信号"):html.index("document.getElementById('chartMeta')")]


def test_fibonacci_history_columns_date_last_and_confirmation_markers_restored():
    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    assert 'grid-template-columns:minmax(0,1.5fr) 44px minmax(58px,.7fr) 78px 70px' in html
    assert 'fib-history-head span:nth-last-child(-n+2),.fib-history-date,.fib-history-win-rate { text-align:right; }' in html
    assert "const bearish = ev.side === 'bearish';" in html
    assert "fib-history-bullish" in html
    assert "fib-history-bearish" in html
    assert "fib-history-direction" in html
    assert "fib-history-star" in html
    assert "aria-label=\"强信号\"" in html
    assert "data.fib_signal_history" in html
    assert 'class="fib-history-date"' in html
    assert 'class="fib-star">★</span>' in html
    assert '.fib-history-bullish .fib-history-direction { color:var(--green); }' in html
    assert '.fib-history-bearish .fib-history-direction { color:var(--red); }' in html
    assert '.fib-history-row { padding:7px 0; border-bottom:1px solid var(--border); font-size:11px; }' in html
    assert 'border-radius:6px' not in html[html.index('.fib-history-row {'):html.index('/* 图表 */')]
    assert 'fl.reacted' in html
    assert 'fl.tested' in html
    assert 'fl.reaction_signal' in html
    assert 'fib-history-head,.fib-history-row' in html
    assert '@media (max-width:420px)' in html



def test_fibonacci_panel_renders_bullish_and_bearish_current_levels():
    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    update_results = html[html.index("function updateResults(data, meta, nineTurn)"):]

    assert "const _fibBull = Array.isArray(data && data.fib_levels) ? data.fib_levels : [];" in update_results
    assert "const _fibBear = Array.isArray(data && data.fib_bearish_levels) ? data.fib_bearish_levels : [];" in update_results
    assert "_renderFibGroup('↑ 上涨波段 · 回撤支撑位', _fibBull, 'bullish')" in update_results
    assert "_renderFibGroup('↓ 下跌波段 · 反弹阻力位', _fibBear, 'bearish')" in update_results
    assert "const hasSignal = !!event || fallbackSignal;" in update_results
    assert "const _chartData = (data && data.chart_data) || {};" in update_results
    assert "const _fallbackLevels = [" in update_results
    assert "'参考区间 · Fib 回撤位'" in update_results
    assert "当前没有达到确认条件的有效 Fib 波段" not in update_results

def _fibonacci_test_bars(lows, volumes):
    import pandas as pd

    dates = pd.date_range("2026-08-01", periods=len(lows), freq="D")
    rows = []
    for i, (low, volume) in enumerate(zip(lows, volumes)):
        rows.append({
            "date": dates[i],
            "open": 86.0,
            "high": 86.7 if 84.0 <= low < 90.0 else 100.0,
            "low": float(low),
            "close": (86.3 + (i - 1) * 0.1) if 84.0 <= low < 90.0 else (90.0 if i == 0 else 96.0),
            "volume": float(volume),
        })
    rows[0]["low"] = 80.0
    rows[0]["close"] = 90.0
    return pd.DataFrame(rows)


def _get_fibonacci_0786(frame):
    from trading_tool.strategy_engine import FujimotoStrategy

    strategy = FujimotoStrategy()
    # Isolate touch-episode behavior from the separately tested swing detector.
    strategy._detect_valid_fib_swing = lambda df, lookback=120: {
        "direction": "bullish", "start_idx": 0, "end_idx": 0,
        "start_price": 80.0, "end_price": 100.0,
            "confirmation_idx": 0,
    }
    levels, *_ = strategy._calc_fibonacci(frame, lookback=60)
    return next(level for level in levels if level.level == 0.786)


def test_fibonacci_confirmation_is_emitted_once_per_continuous_touch_episode():
    frame = _fibonacci_test_bars(
        lows=[80, 84.2, 84.2, 84.2, 95],
        volumes=[100, 100, 130, 200, 100],
    )

    level = _get_fibonacci_0786(frame)

    assert level.reacted is True
    assert level.signal_date == "2026-08-04"
    assert level.reaction_signal == "放量止跌"


def test_fibonacci_can_emit_a_new_confirmation_after_price_leaves_and_retests():
    frame = _fibonacci_test_bars(
        lows=[80, 84.2, 84.2, 95, 84.2, 84.2, 84.2],
        volumes=[100, 100, 130, 100, 100, 150, 300],
    )

    level = _get_fibonacci_0786(frame)

    assert level.reacted is True
    assert level.signal_date == "2026-08-06"
    assert level.reaction_signal == "放量止跌"


def test_current_fibonacci_levels_show_confirmation_date_as_a_separate_column():
    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    update_results = html[html.index("function updateResults(data, meta, nineTurn)"):]
    renderer = update_results[update_results.index("const _renderFibGroup"):update_results.index("if (_fibBull.length)")]

    assert '<span>最近确认</span>' in renderer
    assert '<span>信号日期</span><span>所属波段</span>' in renderer
    assert "波段最近确认" not in renderer
    assert "class=\"fib-table-head\"" in renderer
    assert "grid-template-columns:42px minmax(54px,.75fr) minmax(88px,1.2fr) 72px 64px" in html



def test_current_fibonacci_reference_rows_show_latest_signal_and_its_own_wave_status():
    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    update_results = html[html.index("function updateResults(data, meta, nineTurn)"):]
    renderer = update_results[update_results.index("const _fibHistory"):update_results.index("if (_fibBull.length)")]

    assert "const _fibHistory = Array.isArray(data && data.fib_signal_history)" in renderer
    assert "Math.abs(Number(ev.level) - Number(level)) < 0.0005" in renderer
    assert "if (side && ev.side !== side) return false;" in renderer
    assert "ev.swing_confirmation_date" in renderer
    assert "return (asOfAt - confirmedAt) / 86400000 <= 90 ? '有效' : '已过期';" in renderer
    assert "'无法追溯'" in renderer
    assert '<span>参考价</span><span>最近确认</span><span>信号日期</span><span>所属波段</span>' in renderer
    assert "_renderFibGroup('↑ 上涨波段 · 回撤支撑位', _fibBull, 'bullish')" in update_results
    assert "_renderFibGroup('↓ 下跌波段 · 反弹阻力位', _fibBear, 'bearish')" in update_results
    assert "_renderFibGroup('参考区间 · Fib 回撤位', _fallbackLevels, null)" in update_results
    assert "fib-wave-expired" in html


def test_fibonacci_history_has_ten_trading_day_win_rate_column():
    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    loader = html[html.index("async function loadFibSignalHistory()"):html.index("function updateResults(data, meta, nineTurn)")]
    assert '<span>10日胜率</span>' in loader
    assert "forward_10d_win_rate" in loader
    assert "forward_10d_sample_size" in loader
    assert "样本不足" in loader
    assert "10个交易日尚未走完" in loader
