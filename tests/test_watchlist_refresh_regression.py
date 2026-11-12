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



def test_watchlist_navigation_abort_is_silent_and_does_not_probe_backend():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    assert "let _appPageLeaving = false;" in source
    assert "window.addEventListener('pagehide'" in source
    assert "window.addEventListener('pageshow'" in source
    assert "if (_appPageLeaving || (e && (e.code === 'API_ABORTED' || e.name === 'AbortError')))" in source
    assert "请求已取消" in source
    # Abort/cancel handling must happen before the ordinary business-error health probe.
    start = source.index("async function _loadWatchlistBody(forceRefresh)")
    end = source.index("// 编辑某只自选备注", start)
    body = source[start:end]
    catch_block = body.index("} catch(e) {")
    abort_branch = body.index("e.code === 'API_ABORTED'", catch_block)
    health_probe = body.index("await confirmBackendUnavailable()", catch_block)
    assert abort_branch < health_probe


def test_watchlist_timeout_is_not_misclassified_as_backend_outage():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    helper = source[source.index("async function apiWithTimeout"):source.index("// 登录态")]
    assert "timeoutError.code = 'API_TIMEOUT'" in helper
    assert "cancelled || (e && e.name === 'AbortError')" in helper
    assert "if (timedOut)" in helper
    assert "if (e && e.code === 'API_TIMEOUT')" in source
    assert "请求超时（保留上次数据）" in source


def test_fetch_abort_does_not_mark_backend_down():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("async function api(path")
    end = source.index("// 取 JSON（带容错）", start)
    body = source[start:end]
    assert "opts.signal.aborted" in body
    assert "if (!cancelled)" in body


def test_watchlist_local_cache_persists_all_dashboard_metric_columns():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("function persistWatchlistCache(stocks, meta)")
    end = source.index("function loadWatchlistLocalCache()", start)
    body = source[start:end]
    for field in (
        "high_low", "high_low_type", "role", "valuation", "valuation_detail",
        "nine_turn_daily", "nine_turn_monthly", "nine_turn_daily_state",
        "nine_turn_monthly_state", "crowding_pct", "crowding_level",
        "crowding_speed", "crowding_source", "short_pct_float", "days_to_cover",
    ):
        assert field + ":" in body
    assert "const complete = stocks.every" in body
    assert "stocks.length < expectedCount" in body


def test_watchlist_computing_partial_rows_never_overwrite_local_complete_cache():
    source = Path("frontend/index.html").read_text(encoding="utf-8")
    start = source.index("async function _loadWatchlistBody(forceRefresh)")
    end = source.index("// 编辑某只自选备注", start)
    body = source[start:end]
    computing_start = body.index("if (json.computing)")
    computing_end = body.index("_wlPollCount = 0;", computing_start)
    computing = body[computing_start:computing_end]
    assert "persistWatchlistCache(" not in computing
    # Only the fully completed response persists the full merged rows.
    assert "persistWatchlistCache(merged" in body


def test_watchlist_response_crowding_history_and_persistence_are_nonblocking():
    source = Path("trading_tool/watchlist.py").read_text(encoding="utf-8")
    start = source.index("def _fill_crowding_if_missing(stocks: list) -> list:")
    end = source.index("\ndef _annotate_and_maybe_refresh_dates", start)
    body = source[start:end]
    assert "_cached_pos_advice_history(code)" in body
    assert "_persist_pos_advice_async(code, s)" in body
    assert "_cache.get_position_advice(" not in body
    assert "_cache.append_position_advice(" not in body
    assert "_POS_ADVICE_IO.submit(" in source
    assert "_POS_ADVICE_HISTORY_PENDING" in source
    assert "_POS_ADVICE_WRITE_PENDING" in source


def test_watchlist_crowding_history_loader_deduplicates_remote_reads():
    source = Path("trading_tool/watchlist.py").read_text(encoding="utf-8")
    start = source.index("def _load_pos_advice_history_async")
    end = source.index("\ndef _cached_pos_advice_history", start)
    body = source[start:end]
    assert "if key in _POS_ADVICE_HISTORY_PENDING" in body
    assert "_cache.get_position_advice(key)" in body
    assert "_POS_ADVICE_HISTORY_PENDING.discard(key)" in body


def test_watchlist_crowding_persistence_deduplicates_same_symbol_day():
    source = Path("trading_tool/watchlist.py").read_text(encoding="utf-8")
    start = source.index("def _persist_pos_advice_async")
    end = source.index("\ndef _fill_crowding_if_missing", start)
    body = source[start:end]
    assert "marker = (key, day)" in body
    assert "marker in _POS_ADVICE_WRITE_PENDING" in body
    assert "_cache.append_position_advice(key, payload)" in body
    assert "_POS_ADVICE_WRITE_RETRY" in body


def test_watchlist_cold_start_does_not_read_persisted_status_serially_in_request_path():
    source = Path("trading_tool/watchlist.py").read_text(encoding="utf-8")
    start = source.index("    # 无可用缓存：骨架 + 后台计算", source.index("def get_watchlist_status"))
    end = source.index("            all_fresh = bool(seeded)", start)
    cold_seed = source[start:end]
    assert "_load_persisted_status(" not in cold_seed
    assert "冷启动 API 响应不能逐只同步读取远端状态缓存" in cold_seed
    # Persistent rows are still recovered inside background compute workers.
    compute_start = source.index("def _compute_watchlist")
    compute_end = source.index("\nif __name__ == \"__main__\"", compute_start)
    compute = source[compute_start:compute_end]
    assert "persisted = _load_persisted_status(cu)" in compute


def test_watchlist_strategy_skips_full_fibonacci_history_replay():
    strategy_source = Path("trading_tool/strategy_engine.py").read_text(encoding="utf-8")
    watchlist_source = Path("trading_tool/watchlist.py").read_text(encoding="utf-8")

    assert "include_fib_history: bool = True" in strategy_source
    assert "if include_fib_history and not _lite else []" in strategy_source
    assert "strategy.analyze(df, include_fib_history=False)" in watchlist_source


def test_watchlist_user_rows_are_loaded_once_and_offloaded_from_event_loop():
    source = Path("trading_tool/web_app.py").read_text(encoding="utf-8")
    start = source.index('@app.get("/api/watchlist")')
    end = source.index('\n\n@app.', start + 1)
    body = source[start:end]

    assert "await asyncio.to_thread(watchlist_store.get_all, user_id, token or None)" in body
    assert "watchlist_items=items" in body
    assert body.count("watchlist_store.get_all(") == 0
