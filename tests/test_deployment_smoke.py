"""线上部署冒烟诊断：健康检查、CORS 预检、前端可达性。"""
import os
import requests
import pytest

API = os.getenv("AUTOPILOT_API_URL", "https://autopilot-598c.onrender.com").rstrip("/")
FRONTEND = os.getenv("AUTOPILOT_FRONTEND_URL", "https://www.timebricks.bid").rstrip("/")
ORIGINS = [
    "https://www.timebricks.bid",
    "https://timebricks.bid",
    "https://autopilot.pages.dev",
]


def test_backend_health():
    response = requests.get(f"{API}/api/health", timeout=45)
    assert response.status_code == 200, f"health HTTP {response.status_code}: {response.text[:500]}"
    body = response.json()
    assert body.get("ok") is True or body.get("success") is True, body


@pytest.mark.parametrize("origin", ORIGINS)
def test_cors_preflight_for_frontend_origins(origin):
    response = requests.options(
        f"{API}/api/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
        timeout=30,
    )
    allowed = response.headers.get("access-control-allow-origin")
    assert allowed == origin or allowed == "*", (
        f"CORS blocked origin={origin}; HTTP={response.status_code}; "
        f"allow-origin={allowed!r}; response={response.text[:300]!r}"
    )


def test_frontend_is_reachable_and_points_to_expected_api():
    response = requests.get(FRONTEND, timeout=30)
    assert response.status_code == 200, f"frontend HTTP {response.status_code}"
    html = response.text
    assert "autopilot-598c.onrender.com" in html, "前端页面未发现预期 Render API 地址"
    assert "probeBackendAlive" in html, "前端缺少后端健康探测函数"
    assert "getBackend" in html, "前端缺少后端地址选择函数"
