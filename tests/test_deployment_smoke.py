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
    response = requests.get(
        FRONTEND,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        timeout=30,
    )

    # Cloudflare 对 CI 数据中心 IP 可能返回机器人挑战页（403）。
    # 这不等同于真实浏览器用户无法访问；标记为跳过/待浏览器验证，
    # 绝不把挑战页算成通过，也不让它误报为应用前端故障。
    server = response.headers.get("server", "").lower()
    cf_ray = response.headers.get("cf-ray")
        is_cloudflare_challenge = (
        response.status_code == 403
        and server == "cloudflare"
        and bool(cf_ray)
    )
    if is_cloudflare_challenge:
        pytest.skip(
            "Cloudflare challenged the GitHub Actions runner IP; "
            "site availability must be confirmed from a real browser."
        )

    assert response.status_code == 200, (
        f"frontend HTTP {response.status_code}; "
        f"server={server!r}; cf-ray={cf_ray!r}; "
        f"body={response.text[:300]!r}"
    )
    html = response.text
    assert "autopilot-598c.onrender.com" in html, "前端页面未发现预期 Render API 地址"
    assert "probeBackendAlive" in html, "前端缺少后端健康探测函数"
    assert "getBackend" in html, "前端缺少后端地址选择函数"
