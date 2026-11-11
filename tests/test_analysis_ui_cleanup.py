from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_analysis_panel_only_exposes_indicators_and_fibonacci():
    html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
    start = html.index("<!-- 分析结果面板")
    end = html.index('<div class="footer-note">', start)
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
