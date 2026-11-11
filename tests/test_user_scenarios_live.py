"""User-flow scenario checks against the live API.

These are deliberately read-only. They verify the response contract for common
US tickers, an A-share symbol, and an empty-input validation case.
"""
import os
import requests
import pytest

API = os.getenv("AUTOPILOT_API_URL", "https://autopilot-598c.onrender.com").rstrip("/")
SCENARIOS = [
    ("AAPL", "US mega-cap"),
    ("NVDA", "US semiconductor"),
    ("ASTS", "US growth / volatile"),
    ("BE", "US NYSE-listed"),
    ("600519", "A-share numeric symbol"),
]


def _get_stock_advice(symbol):
    return requests.get(
        f"{API}/api/position/stock-advice",
        params={"symbol": symbol},
        headers={"Accept": "application/json"},
        timeout=45,
    )


def test_empty_symbol_has_expected_validation_response():
    response = _get_stock_advice("")
    assert response.status_code == 200, response.text[:500]
    body = response.json()
    assert body.get("success") is False, body
    assert body.get("error") == "缺少代码", body
    assert body.get("timeline") == [], body


@pytest.mark.parametrize("symbol,scenario", SCENARIOS, ids=[x[0] for x in SCENARIOS])
def test_stock_advice_user_scenarios(symbol, scenario):
    response = _get_stock_advice(symbol)
    assert response.status_code == 200, (
        f"{scenario} ({symbol}) returned HTTP {response.status_code}: "
        f"{response.text[:500]}"
    )
    body = response.json()
    assert isinstance(body, dict), f"{symbol}: response is not a JSON object"
    assert isinstance(body.get("success"), bool), f"{symbol}: missing boolean success: {body}"
    assert isinstance(body.get("timeline"), list), f"{symbol}: timeline must be a list: {body}"
    advice = body.get("advice")
    assert advice is None or isinstance(advice, dict), (
        f"{symbol}: advice must be an object or null: {type(advice).__name__}"
    )
    if isinstance(advice, dict):
        metric_type = advice.get("metric_type")
        assert metric_type in {
            "short_composite_score",
            "short_interest_incomplete",
            "volume_percentile",
            "volume",
        }, f"{symbol}: unexpected metric_type={metric_type!r}; advice={advice}"
        if metric_type == "short_composite_score":
            assert advice.get("score") is not None, (
                f"{symbol}: composite metric must include score: {advice}"
            )
        if metric_type == "short_interest_incomplete":
            assert advice.get("score") is None, (
                f"{symbol}: incomplete short data must not fabricate composite score: {advice}"
            )
    print(
        f"SCENARIO symbol={symbol} case={scenario} "
        f"success={body.get('success')} metric_type="
        f"{(advice or {}).get('metric_type') if isinstance(advice, dict) else 'none'} "
        f"timeline={len(body.get('timeline') or [])}"
    )
