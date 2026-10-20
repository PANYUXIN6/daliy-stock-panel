from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import httpx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.collector_proxy import collector_owns


def test_web_lifespan_never_starts_collector_or_recovers_jobs(tmp_path, monkeypatch):
    import app.main as main
    from app.services import auth, shared_runtime
    from app.services.pipeline_jobs import job_store

    monkeypatch.setattr(main.settings, 'data_dir', tmp_path)
    monkeypatch.setattr(auth, 'bootstrap_from_env', lambda: None)

    def forbidden(*args, **kwargs):
        raise AssertionError('网页启动了采集或任务恢复')

    monkeypatch.setattr(job_store, 'recover_interrupted', forbidden)
    monkeypatch.setattr(main, 'detect_capabilities', forbidden)
    monkeypatch.setattr(main.QuoteService, 'boot_check', forbidden)

    async def bridge(*args):
        await asyncio.Event().wait()
    monkeypatch.setattr(shared_runtime, 'bridge_events', bridge)
    with TestClient(create_app('web')) as client:
        assert client.get('/health').json()['service_role'] == 'web'
        state = client.app.state
        assert state.quote_service.get_enriched_today()[0].is_empty()
        assert not hasattr(state, 'scheduler')
        assert not hasattr(state, 'mining_manager')
    assert not any(route.path == '/internal/runtime/events' for route in create_app('web').routes)
    assert any(route.path == '/internal/runtime/events' for route in create_app('collector').routes)


def test_proxy_routes_keep_batch_queries_local_and_forward_owner_state():
    assert not collector_owns('POST', '/api/kline/minute-batch')
    assert not collector_owns('POST', '/api/kline/daily-batch')
    assert not collector_owns('GET', '/api/kline/minute')
    assert collector_owns('POST', '/api/kline/sync_minute_single')
    assert collector_owns('POST', '/api/kline/minute/fetch')
    assert collector_owns('GET', '/api/kline/minute/fetch/job1')
    assert collector_owns('GET', '/api/pipeline/jobs/job1')
    assert not collector_owns('GET', '/api/financials/status')
    assert collector_owns('GET', '/api/financials/execution-status')
    assert collector_owns('POST', '/api/custom/assistant/chat')


def test_web_auth_runs_before_collector_forward(monkeypatch, tmp_path):
    from app.services import auth
    store = auth.AuthStore(tmp_path)
    store.bootstrap('admin', 'initial-secret')
    token, _ = store.login('admin', 'initial-secret', 'test', '')
    calls = []

    async def respond(request):
        calls.append((request.method, request.url.path))
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"job_id": "same-owner-job"}'
        return httpx.Response(200, headers={'content-type': 'application/json'}, stream=Body())

    @asynccontextmanager
    async def fake_lifespan(app):
        app.state.auth_store = store
        app.state.collector_url = 'http://collector'
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            app.state.collector_client = client
            yield

    app = create_app('web')
    app.router.lifespan_context = fake_lifespan
    with TestClient(app) as client:
        assert client.post('/api/pipeline/run').status_code == 401
        assert calls == []
        from app.api.auth import COOKIE_NAME
        client.cookies.set(COOKIE_NAME, token)
        response = client.post('/api/pipeline/run')
        assert response.json() == {'job_id': 'same-owner-job'}
    assert calls == [('POST', '/api/pipeline/run')]


def test_collector_initial_snapshot_includes_etf_and_index_without_stocks(tmp_path, monkeypatch):
    from datetime import date
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import polars as pl

    import app.main as main
    from app.services import auth, shared_runtime
    from app.tickflow.repository import DataStore, KlineRepository

    monkeypatch.setattr(main.settings, 'data_dir', tmp_path)
    monkeypatch.setattr(main.settings, 'backtest_matrix_disk_cache_enabled', False)
    monkeypatch.setattr(main, 'detect_capabilities', lambda: shared_runtime.CapabilitySet())
    monkeypatch.setattr(auth, 'bootstrap_from_env', lambda: None)
    monkeypatch.setattr('app.data_providers.custom.load_all', lambda: None)
    calendar_ready = []
    catchup_observations = []
    def sync_calendar(*args):
        calendar_ready.append(True)
        return {'updated': False}
    monkeypatch.setattr('app.services.trading_day.sync_calendar', sync_calendar)
    monkeypatch.setattr(main.daily_pipeline, 'run_scheduled_instruments_sync',
                        lambda repo: catchup_observations.append(bool(calendar_ready)), raising=False)
    monkeypatch.setattr(main.QuoteService, 'boot_check', lambda self: None)
    monkeypatch.setattr(main.daily_pipeline, 'start_scheduler', lambda *args: None)
    for target in [
        'app.services.depth_service.DepthService.boot_check',
        'app.services.depth_service.DepthService.start_polling',
        'app.services.minute_refresh.MinuteRefreshService.start',
        'app.services.ordinary_minute.OrdinaryMinuteService.start',
        'app.services.wecom_bot_service.WecomBotService.boot_check',
        'app.services.ext_pull.pull_scheduler.start',
        'app.services.ext_pull.pull_scheduler.refresh',
        'app.services.financial_sync.financial_scheduler.start',
    ]:
        monkeypatch.setattr(target, lambda *args: None)
    monkeypatch.setattr('app.services.ext_presets.ensure_builtin_presets', AsyncMock())
    monkeypatch.setattr('app.watchdog.start_watchdog', lambda *args: None)
    day = date(2026, 10, 9)
    for asset, symbol in [('etf', '510300.SH'), ('index', '000001.SH')]:
        directory = tmp_path / f'kline_{asset}_enriched' / 'date=2026-10-09'
        directory.mkdir(parents=True)
        pl.DataFrame({'symbol': [symbol], 'date': [day], 'open': [3.4], 'high': [3.6],
                      'low': [3.3], 'close': [3.5], 'volume': [1000.0], 'amount': [3500.0]}).write_parquet(directory / 'part.parquet')

    # Force the observed ordering: stock warmup completes before quote_service exists.
    original_refresh = KlineRepository.refresh_cache
    def refresh(self, **kwargs):
        original_refresh(self, background=False)
        if self._on_warmup_done:
            self._on_warmup_done()
    monkeypatch.setattr(KlineRepository, 'refresh_cache', refresh)
    with TestClient(create_app('collector')):
        reader = KlineRepository(DataStore(tmp_path))
        mirror = shared_runtime.QuoteMirror(reader)
        assert mirror.load_snapshot(SimpleNamespace())
        assert reader.get_enriched_latest_asset('etf')[0]['symbol'].to_list() == ['510300.SH']
        assert reader.get_enriched_latest_asset('index')[0]['symbol'].to_list() == ['000001.SH']
        reader.db.close()
    assert catchup_observations == [True]
