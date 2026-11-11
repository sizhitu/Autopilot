from pathlib import Path


def test_watchlist_refresh_uses_business_api_without_health_preflight():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("async function _loadWatchlistBody(forceRefresh)")
    end = source.index("// 编辑某只自选备注", start)
    body = source[start:end]

    assert "Cloudflare 静态前端可先探测 Render" not in body
    assert "probeBackendAlive(!!forceRefresh)" not in body


def test_watchlist_refresh_does_not_auto_hard_purge_on_repeated_clicks():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("async function _loadWatchlistBody(forceRefresh)")
    end = source.index("// 编辑某只自选备注", start)
    body = source[start:end]

    assert "hardQ" not in body
    assert "hard=1" not in body


def test_watchlist_rate_limit_does_not_fall_back_to_snapshot():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("async function _loadWatchlistBody(forceRefresh)")
    end = source.index("// 编辑某只自选备注", start)
    body = source[start:end]

    rate_limit = body.index("限流是业务层明确拒绝")
    snapshot_fallback = body.index("在线 API 确实失败")
    assert rate_limit < snapshot_fallback
    assert body.count("throw new Error('429: '") == 2
