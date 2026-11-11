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
    snapshot_fallback = body.index("业务接口报错不等于整个后端不可用")
    assert rate_limit < snapshot_fallback
    assert body.count("throw new Error('429: '") == 1
    assert "if (!res.ok)" in body
    assert "'HTTP ' + res.status" in body



def test_short_interest_lookup_is_nonblocking_for_watchlist():
    source = Path("trading_tool/watchlist.py").read_text(encoding="utf-8")
    start = source.index("def _get_cached_short(code: str) -> dict:")
    end = source.index("\n\n_FINRA_TOKEN_CACHE", start)
    body = source[start:end]

    assert "_SHORT_EXECUTOR.submit(_refresh_short_cache, key)" in body
    assert "_fetch_us_short_interest(code)" not in body
    assert "_fetch_us_short_interest(key)" not in body


def test_watchlist_crowding_annotation_does_not_call_network_fallbacks():
    source = Path("trading_tool/watchlist.py").read_text(encoding="utf-8")
    start = source.index("def _fill_crowding_if_missing(stocks: list) -> list:")
    end = source.index("\ndef _annotate_and_maybe_refresh_dates", start)
    body = source[start:end]

    assert "_fetch_volume_only" not in body
    assert "_get_cached_short(code)" in body


def test_watchlist_refresh_watchdog_handles_stalled_partial_progress():
    source = Path("trading_tool/watchlist.py").read_text(encoding="utf-8")
    assert "if age > 180:" in source
    assert "pdata['stalled'] = True" in source
    assert "entry['ts'] = time.time()" in source


def test_dashboard_crowding_shows_raw_short_metrics_when_composite_is_unavailable():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("function renderWatchlist(stocks)")
    end = source.index("function rememberActionSnap(s)", start)
    body = source[start:end]

    assert "s.short_pct_float" in body
    assert "s.days_to_cover" in body
    assert "单项指标" not in body
    assert "回补 " in body
    assert "空头 " in body
    # A single raw metric must not be represented as a composite score.
    assert "复合评分缺失时直接展示" in body


def test_crowding_market_detection_handles_china_exchange_suffixes():
    source = Path("trading_tool/watchlist.py").read_text(encoding="utf-8")
    assert "def _is_us_crowding_symbol(code: str, market: str = \"\") -> bool:" in source
    assert 'raw.endswith((".SH", ".SZ", ".BJ"))' in source
    assert 'raw.startswith(("SH", "SZ", "BJ"))' in source
    assert 'is_us = _is_us_crowding_symbol(code, status.market)' in source
    assert 'is_us = _is_us_crowding_symbol(code, s.get("market"))' in source



def test_a_share_crowding_is_computed_in_live_status_path():
    source = Path("trading_tool/watchlist.py").read_text(encoding="utf-8")
    start = source.index("def get_stock_status(code: str, name: str, days: int = 1250) -> StockStatus:")
    end = source.index("def _status_to_dict", start)
    body = source[start:end]

    assert "_is_us_crowding_symbol(code, status.market)" in body
    assert "and 'volume' in df.columns" in body
    assert "status.crowding_pct = round(pct, 1)" in body
    assert 'status.crowding_source = "成交量历史分位"' in body
    assert "_fetch_volume_only" not in body


def test_a_share_crowding_dashboard_value_has_no_out_of_100_suffix():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("const pctText = hasPct")
    end = source.index("if (hasShortPct || hasDtc)", start)
    body = source[start:end]
    assert "s.market === '美股' ? '/100' : ''" in body
    assert "Number(pct).toFixed(1) + '/100'" not in body


def test_watchlist_snapshot_fallback_requires_confirmed_backend_outage():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("async function _loadWatchlistBody(forceRefresh)")
    end = source.index("// 编辑某只自选备注", start)
    body = source[start:end]

    catch_start = body.index("// 业务接口报错不等于整个后端不可用")
    catch_end = body.index("let usedSnap = false;", catch_start)
    gate = body[catch_start:catch_end]
    assert "await confirmBackendUnavailable()" in gate
    assert "if (!backendUnavailable)" in gate
    assert gate.index("if (!backendUnavailable)") < gate.index("return;")
    assert "探测自身异常时采取保守策略，不切快照" in gate

    snapshot = body.index("const snap = await fetchWatchlistSnapshot();", catch_end)
    assert snapshot > catch_end


def test_dashboard_crowding_comment_does_not_trip_single_metric_assertion():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("function renderWatchlist(stocks)")
    end = source.index("function rememberActionSnap(s)", start)
    body = source[start:end]
    assert "单项指标" not in body


def test_snapshot_display_label_does_not_pollute_canonical_updated_at():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("function applyWatchlistSnapshot(json)")
    end = source.index("async function loadWatchlist", start)
    block = source[start:end]
    assert "_lastWatchUpdatedAt = asOf;" in block
    assert "updated_at: asOf ? (asOf + ' · ' + tag) : tag" in block


def test_successful_watchlist_api_response_exits_snapshot_mode_immediately():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("async function _loadWatchlistBody(forceRefresh)")
    end = source.index("// 编辑某只自选备注", start)
    block = source[start:end]
    api_request = block.index("apiWithTimeout('/api/watchlist'")
    response = block.index("const json = await res.json();", api_request)
    success = block.index("if (!json.success) throw new Error", response)
    mark_up = block.index("markBackendUp();", success)
    leave = block.index("leaveSnapshotMode();", mark_up)
    computing = block.index("if (json.computing)", leave)
    assert response < success < mark_up < leave < computing


def test_watchlist_shows_local_cache_before_waiting_for_live_api():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("async function _loadWatchlistBody(forceRefresh)")
    end = source.index("// 编辑某只自选备注", start)
    body = source[start:end]

    local_cache = body.index("const localSnapshot = loadWatchlistLocalCache();")
    apply_cache = body.index("applyWatchlistSnapshot(localSnapshot);", local_cache)
    live_request = body.index("apiWithTimeout('/api/watchlist'", apply_cache)
    assert local_cache < apply_cache < live_request
    assert "fetchWatchlistSnapshot().then(function (snapshot)" in body
    assert "_watchlistLiveRevision !== revisionAtStart" in body
    assert "_watchlistLiveRevision += 1;" in body


def test_late_snapshot_cannot_overwrite_successful_live_watchlist_response():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("async function _loadWatchlistBody(forceRefresh)")
    end = source.index("// 编辑某只自选备注", start)
    body = source[start:end]

    fallback = body.index("fetchWatchlistSnapshot().then(function (snapshot)")
    revision_guard = body.index("_watchlistLiveRevision !== revisionAtStart", fallback)
    live_success = body.index("_watchlistLiveRevision += 1;", body.index("apiWithTimeout('/api/watchlist'"))
    assert fallback < revision_guard < live_success


def test_local_snapshot_updates_initial_loading_state_before_overlay_decision():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("async function _loadWatchlistBody(forceRefresh)")
    end = source.index("// 编辑某只自选备注", start)
    body = source[start:end]

    restored = body.index("applyWatchlistSnapshot(localSnapshot);")
    refresh_state = body.index("hasRows = !!(_lastWatchRows && _lastWatchRows.length);", restored)
    loading_branch = body.index("if (hasRows)", refresh_state)
    assert restored < refresh_state < loading_branch


def test_watchlist_non_429_http_errors_preserve_status_and_backend_detail():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("async function _loadWatchlistBody(forceRefresh)")
    end = source.index("// 编辑某只自选备注", start)
    body = source[start:end]

    request = body.index("apiWithTimeout('/api/watchlist'")
    non_ok = body.index("if (!res.ok)", request)
    parse_error = body.index("const errBody = await res.json()", non_ok)
    status = body.index("'HTTP ' + res.status", parse_error)
    assert request < non_ok < parse_error < status
    assert "接口异常：' + apiErrorText" in body
    assert "（保留上次数据）" in body
    assert "已保留上次数据" in body



def test_watchlist_backend_resource_contention_uses_cooldown_instead_of_retry_thread_storm():
    source = Path("trading_tool/watchlist.py").read_text(encoding="utf-8")
    assert "ent['retry_after'] = time.time() + 20" in source
    assert "out['refresh_deferred'] = True" in source
    assert "if retry_after > now and isinstance(cache.get('data'), dict):" in source
    # Force refresh must not block a worker thread waiting 45 seconds for the global semaphore.
    assert "_BG_SEM.acquire(timeout=45)" not in source


def test_watchlist_auth_pending_polling_is_bounded_with_backoff():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    assert "let _wlAuthPollCount = 0;" in source
    assert "const delays = [1000, 2000, 4000, 8000, 12000]" in source
    assert "scheduleWatchlistAuthRetry();" in source
    assert "}, 600);" not in source


def test_watchlist_resource_deferred_response_stops_frontend_polling_and_keeps_rows():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("async function _loadWatchlistBody(forceRefresh)")
    end = source.index("// 编辑某只自选备注", start)
    body = source[start:end]
    assert "if (json.refresh_deferred)" in body
    assert "mergeWatchRows(deferredStocks, _lastWatchRows)" in body
    assert "_watchWasComputing = false;" in body
    assert "后台计算资源繁忙，已保留现有行情" in body
