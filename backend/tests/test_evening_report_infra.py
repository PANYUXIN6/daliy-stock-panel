"""Real narrow-table inputs, immutable publication, recovery and HTTP boundaries."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import polars as pl
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import evening_report, pipeline
from app.enriched_generation import bump_enriched_generation
from app.indicators.pipeline import run_pipeline
from app.market_time import CN_TZ
from app.services import evening_report_inputs as inputs
from app.services import evening_report_jobs as jobs
from app.services import evening_report_store as archive
from app.services import ext_presets, pipeline_jobs, preferences, shared_runtime
from app.services.daily_coverage import DailyCoverage, DailyCoverageError
from app.services.evening_report_store import EveningReportStore, atomic_json


def write(root, table, frame):
    for key, part in frame.partition_by('date', as_dict=True).items():
        path = root / table / f'date={key[0]}' / 'part.parquet'
        path.parent.mkdir(parents=True, exist_ok=True)
        part.write_parquet(path)


@pytest.fixture
def market_batch(private_owner, tmp_path, monkeypatch):
    calendar = []
    cursor = date(2026, 6, 1)
    while len(calendar) < 72:
        if cursor.weekday() < 5:
            calendar.append(cursor)
        cursor += timedelta(days=1)
    day = calendar[-1]
    now = datetime.combine(day, datetime.min.time(), tzinfo=CN_TZ).replace(hour=16)
    monkeypatch.setattr(inputs, 'cn_now', lambda: now)
    monkeypatch.setattr(jobs, 'cn_now', lambda: now)
    monkeypatch.setattr(archive, 'cn_now', lambda: now)
    monkeypatch.setattr(preferences, 'load', lambda: {})
    symbols = [f'600{n:03d}.SH' for n in range(24)]
    metadata = pl.DataFrame([{'symbol': symbol, 'name': f'公司{i}', 'code': symbol[:6],
                              'exchange': 'SH', 'type': 'stock', 'board': 'main',
                              'security_type': 'a_share', 'is_st': False, 'is_current': True,
                              'listing_date': '2010-01-01', 'as_of': day,
                              'metadata_source': 'test-standard-provider',
                              'metadata_observed_at': now.isoformat(), 'metadata_rule_version': 'v1'}
                             for i, symbol in enumerate(symbols)])
    path = tmp_path / 'instruments' / 'instruments.parquet'
    path.parent.mkdir()
    metadata.write_parquet(path)
    rows = []
    for index, symbol in enumerate(symbols):
        for offset, trading_date in enumerate(calendar):
            close = 10 * (1 + .001 + index * .0001) ** offset
            rows.append({'symbol': symbol, 'date': trading_date, 'open': close * .999,
                         'high': close * 1.01, 'low': close * .99, 'close': close,
                         'volume': 10_000_000., 'amount': 200_000_000., 'quote_ts': None})
    raw = pl.DataFrame(rows)
    write(tmp_path, 'kline_daily', raw)
    write(tmp_path, 'kline_index_daily', raw.filter(pl.col('symbol') == symbols[0]).with_columns(pl.lit('000001.SH').alias('symbol')))
    run_pipeline(tmp_path)
    atomic_json(tmp_path / 'calendar' / 'trading_days.json', {
        'days': [str(d) for d in calendar], 'start': str(calendar[0]), 'end': str(day)})
    observations = [{'kind': source, 'id': source, 'source': source, 'obtained_at': now.isoformat(),
                     'source_effective_date': None, 'source_published_at': None, 'source_version': None,
                     'complete': True, 'symbols': symbols,
                     'memberships': [{'source': source, 'symbol': s, 'sector_id': source + '-1',
                                      'sector_name': source + '-1'} for s in symbols]}
                    for source in ('concept', 'industry')]
    coverage = DailyCoverage(day)
    coverage.observe(raw.filter(pl.col('date') == day))
    # The real shared cache supplies indicators; the narrow on-disk history carries adjusted OHLC.
    from app.indicators.pipeline import compute_indicators
    enriched = pl.read_parquet(sorted((tmp_path / "kline_daily_enriched").glob("date=*/*.parquet")))
    cached = compute_indicators(enriched.sort("symbol", "date"), needed={"ma5", "ma10", "ma20", "atr_14", "bull_bear_line", "high_60d"})
    def shared_range(start, end, *, columns):
        return cached.filter(pl.col("date").is_between(start, end)).select(
            "symbol", "date", *[k for k in columns if k in cached.columns])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: None,
                           get_enriched_history_span=lambda: (calendar[0], day), get_enriched_range=shared_range)
    monkeypatch.setattr("app.services.evening_report_events.fetch_disclosures", lambda *a: ([], []))
    return SimpleNamespace(root=tmp_path, day=day, calendar=calendar, raw=raw, symbols=symbols,
                           metadata=metadata, observations=observations, coverage=coverage, repo=repo, now=now,
                           market={'phase': 'repair', 'state': 'range', 'promo_rate': .3, 'seal_rate': .8})


@pytest.fixture
def accepted(market_batch):
    state = market_batch
    state.evidence = inputs.save_ready(state.root, state.day, state.coverage, state.observations, state.market)
    return state


def test_classification_observation_uses_response_not_merged_table(tmp_path, monkeypatch):
    answers = [[{'symbol': '600000.SH', 'concepts': ['银行']}],
               [{'symbol': '600001.SH', 'concepts': ['机械']}]]
    async def fetch(config_id, data_dir, on_progress=None):
        return answers.pop(0)
    monkeypatch.setattr(ext_presets, '_fetch_classification_rows', fetch)
    first = asyncio.run(ext_presets.prepare_preset('ext_gn_ths', tmp_path))
    ext_presets.commit_prepared_preset(first, tmp_path)
    second = asyncio.run(ext_presets.prepare_preset('ext_gn_ths', tmp_path))
    ext_presets.commit_prepared_preset(second, tmp_path)
    assert second.observation['symbols'] == ['600001.SH']
    assert second.observation['source_effective_date'] is None
    assert first.observation['id'] != second.observation['id']
    assert {r['symbol'] for r in second.observation['memberships']} == {'600001.SH'}


def test_classification_missing_scope_blocks_readiness_and_unknown_effective_date_is_allowed(accepted):
    state = accepted
    observations = state.evidence['classifications']
    observations[0]['symbols'] = observations[0]['symbols'][1:]
    with pytest.raises(DailyCoverageError, match='本轮概念分类'):
        inputs.save_ready(state.root, state.day, state.coverage, observations, {})


@pytest.mark.parametrize(('today', 'expected'), [('2026-11-15', '2026-11-13'), ('2026-11-16', '2026-11-16'),
                                              ('2026-10-04', '2026-09-30')])
def test_default_status_uses_latest_calendar_trade_day_not_today_or_latest_report(tmp_path, monkeypatch, today, expected):
    monkeypatch.setattr(jobs, 'cn_now', lambda: datetime.fromisoformat(today).replace(hour=18, tzinfo=CN_TZ))
    atomic_json(tmp_path / 'calendar' / 'trading_days.json', {
        'days': ['2026-09-30', '2026-10-09', '2026-11-12', '2026-11-13', '2026-11-16'],
        'start': '2026-09-30', 'end': '2026-11-16',
    })
    EveningReportStore(tmp_path).save('2026-09-29', {'audit': {}, 'state': {}}, {'stock_generation': 'test'})
    jobs._save_attempt(tmp_path, '2026-11-15', {'status': 'failed', 'reason': '非交易日不能生成'})
    result = jobs.status(tmp_path)
    assert result['date'] == expected
    assert result['status'] == ('unpublished' if expected < today else 'waiting')
    if expected < today:
        assert '无正式晚报' in result['reason']
    assert jobs.status(tmp_path, '2026-11-15')['status'] == ('failed' if today == '2026-11-15' else 'unpublished')


def test_default_status_does_not_invent_trade_day_without_calendar_or_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'cn_now', lambda: datetime(2026, 11, 15, 18, tzinfo=CN_TZ))
    result = jobs.status(tmp_path)
    assert result['date'] == ''
    assert result['status'] == 'waiting'
    assert '交易日历尚未就绪' in result['reason']
    EveningReportStore(tmp_path).save('2026-11-13', {'audit': {}, 'state': {}}, {'stock_generation': 'test'})
    assert jobs.status(tmp_path)['date'] == '2026-11-13'
    assert jobs.status(tmp_path)['status'] == 'published'


def test_status_distinguishes_incomplete_ready_and_completed_job_without_callback(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'cn_now', lambda: datetime(2026, 10, 16, 18, tzinfo=CN_TZ))
    store = EveningReportStore(tmp_path)
    store.save_readiness('2026-10-16', {'complete': False, 'reason': '分类尚缺证券'})
    assert jobs.status(tmp_path, '2026-10-16')['status'] == 'waiting'
    assert jobs.status(tmp_path, '2026-10-16')['reason'] == '分类尚缺证券'
    registry = pipeline_jobs.JobStore(store_dir=tmp_path / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', registry)
    job_id, _ = registry.create(request_key='evening_report:2026-10-16:live:v1:x')
    jobs._save_attempt(tmp_path, '2026-10-16', {'job_id': job_id, 'status': 'generating'})
    store.save('2026-10-16', {'audit': {}, 'state': {}}, {'stock_generation': 'test'})
    registry.succeed(job_id, {})
    assert jobs.status(tmp_path, '2026-10-16')['status'] == 'published'


def test_published_original_takes_precedence_over_stale_attempt(tmp_path, monkeypatch):
    from app.jobs import daily_pipeline
    from app.services import trading_day
    day = date(2026, 10, 16)
    now = datetime(2026, 10, 16, 18, tzinfo=CN_TZ)
    monkeypatch.setattr(jobs, 'cn_now', lambda: now)
    monkeypatch.setattr(preferences, 'load', lambda: {})
    monkeypatch.setattr(trading_day, 'is_trading_day', lambda *a, **k: True)
    monkeypatch.setattr(daily_pipeline, '_get_app_state', lambda: SimpleNamespace(quote_service=None))
    registry = pipeline_jobs.JobStore(store_dir=tmp_path / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', registry)
    store = EveningReportStore(tmp_path)
    store.save(str(day), {'audit': {}, 'state': {}}, {'stock_generation': 'test'})
    jobs._save_attempt(tmp_path, str(day), {'status': 'failed', 'reason': '刷新通知失败'})
    trigger = AsyncMock()
    monkeypatch.setattr(jobs, 'trigger', trigger)
    derived = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs, '_queue_tracking', derived)
    assert asyncio.run(jobs.run_scheduled(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))) is None
    trigger.assert_not_awaited()
    derived.assert_awaited_once()
    assert jobs.status(tmp_path, str(day))['status'] == 'published'


def test_browser_serves_the_same_original_by_date_or_version(tmp_path, monkeypatch):
    from app.services import evening_report_store

    day = '2026-10-16'
    store = EveningReportStore(tmp_path)
    monkeypatch.setattr(evening_report_store, 'cn_now', lambda: datetime(2026, 10, 16, 18, tzinfo=CN_TZ))
    original = store.save(day, {'audit': {}, 'rule_version': 'initial-rule', 'summary': '原始观察'}, {'stock_generation': 'test'})
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(evening_report.router)
    with TestClient(app) as client:
        endpoint = f'/api/evening-report?date={day}'
        assert client.get(endpoint).json()['version'] == original['version']
        assert client.get('/api/evening-report').json()['summary'] == '原始观察'
        pinned = client.get(f'{endpoint}&version={original["version"]}').json()
        assert pinned['summary'] == '原始观察'
        assert pinned['rule_version'] == 'initial-rule'
        assert client.get(f'{endpoint}&version={"0" * 32}').status_code == 404
    assert store.get(day)['version'] == original['version']


def test_browser_read_is_available_to_guest_but_shared_write_is_admin_only(tmp_path, monkeypatch):
    from app.api import auth
    from app.services.auth import AuthStore
    from app.services.auth_access import install_auth_middleware
    from app.services.collector_proxy import collector_owns
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.auth_store = AuthStore(tmp_path)
    app.state.auth_store.bootstrap('admin', 'initial-secret')
    app.state.auth_store.create_user('guest', 'temporary-secret', 'guest', None)
    token, _ = app.state.auth_store.login('guest', 'temporary-secret', 'ip', '')
    token, _ = app.state.auth_store.change_password(token, 'temporary-secret', 'permanent-secret')
    app.include_router(evening_report.router)
    install_auth_middleware(app)
    trigger = AsyncMock(return_value={'job_id': 'job', 'status': 'generating'})
    monkeypatch.setattr(jobs, 'trigger', trigger)
    with TestClient(app) as client:
        assert client.get('/api/evening-report/dates').status_code == 401
        client.cookies.set(auth.COOKIE_NAME, token)
        assert client.get('/api/evening-report/dates').json() == {'dates': []}
        atomic_json(tmp_path / 'calendar' / 'trading_days.json', {'days': ['2026-11-13']})
        monkeypatch.setattr(jobs, 'cn_now', lambda: datetime(2026, 11, 15, 18, tzinfo=CN_TZ))
        assert client.get('/api/evening-report/status').json()['date'] == '2026-11-13'
        assert client.get('/api/evening-report?date=2026-10-16').json()['date'] == '2026-10-16'
        assert client.post('/api/evening-report/rebuild', json={'date': '2026-10-16'},
                           headers={'Origin': 'http://testserver'}).status_code == 403
        trigger.assert_not_awaited()
    assert collector_owns('POST', '/api/evening-report/rebuild')
    assert collector_owns('GET', '/api/evening-report/status')
    assert not collector_owns('GET', '/api/evening-report?date=2026-10-16')


@pytest.fixture
def task_runtime(accepted, monkeypatch):
    from app.jobs import daily_pipeline
    registry = pipeline_jobs.JobStore(store_dir=accepted.root / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', registry)
    monkeypatch.setattr(pipeline, 'job_store', registry)
    monkeypatch.setattr(pipeline_jobs, '_run_slot_owner', None)
    monkeypatch.setattr(pipeline_jobs, '_CANCEL_FLAGS', {})
    monkeypatch.setattr(daily_pipeline, '_get_app_state', lambda: SimpleNamespace(quote_service=None, capabilities=object()))
    monkeypatch.setattr(daily_pipeline, 'run_weekly_mining_after_sync', lambda: None)
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *_: None)
    return accepted, registry


async def drain_tasks():
    while pipeline._tasks:
        await asyncio.gather(*list(pipeline._tasks))


@pytest.fixture
def manual_runtime(task_runtime, monkeypatch):
    from app.services.auth import AuthStore
    from app.services.user_context import UserContext, user_scope

    state, registry = task_runtime
    auth = AuthStore(state.root)
    admin = auth.bootstrap('manualadmin', 'initial-secret')
    token, _ = auth.login('manualadmin', 'initial-secret', 'ip', '')
    actor = UserContext(admin['id'], 'admin', state.root, session_token=token)
    monkeypatch.setattr(jobs, '_queue_tracking', AsyncMock())
    with user_scope(actor):
        yield state, registry, auth, token


@pytest.mark.parametrize('initial', ['missing', 'changed', 'accepted'])
def test_manual_generation_prepares_only_when_needed_then_publishes(manual_runtime, monkeypatch, initial):
    from app.jobs import daily_pipeline

    state, registry, _, _ = manual_runtime
    store = EveningReportStore(state.root)
    if initial == 'missing':
        store.save_readiness(str(state.day), {'complete': False})
    if initial == 'changed':
        bump_enriched_generation(state.root, 'stock')
    refreshed = Mock()
    monkeypatch.setattr(pipeline, 'finalize_market_data', refreshed)
    caps = object()
    quote_service = SimpleNamespace(paused=nullcontext)

    def prepare(repo, capset, *, on_progress, postmarket, target_date, evening_only):
        assert repo is state.repo and capset is caps
        assert postmarket and evening_only and target_date == state.day
        assert registry.get(pipeline_jobs._run_slot_owner)['request_key'].startswith('evening_report:')
        on_progress('sync_daily', 45, '准备正式行情')
        assert jobs.status(state.root, str(state.day))['reason'] == '准备正式行情'
        inputs.save_ready(state.root, state.day, state.coverage,
                          state.evidence['classifications'], state.evidence['market'])
        return {}

    prepared = Mock(side_effect=prepare)
    monkeypatch.setattr(daily_pipeline, 'run_now', prepared)
    async def run():
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            repo=state.repo, quote_service=quote_service, capabilities=caps)))
        result = await evening_report.rebuild(request, evening_report.RebuildRequest(
            date=state.day))
        await drain_tasks()
        return result

    result = asyncio.run(run())
    job = registry.get(result['job_id'])
    assert job['status'] == 'succeeded', job.get('error')
    assert prepared.call_count == refreshed.call_count == (0 if initial == 'accepted' else 1)
    assert store.get(str(state.day), job['result']['version'])['input_reference']['stock_generation'] == store.readiness(str(state.day))['stock_generation']
    assert pipeline_jobs._run_slot_owner is None


@pytest.mark.parametrize('failure', ['exception', 'unaccepted', 'cancel'])
def test_manual_preparation_failure_never_publishes_and_refreshes_partial_data(manual_runtime, monkeypatch, failure):
    from app.jobs import daily_pipeline

    state, registry, _, _ = manual_runtime
    store = EveningReportStore(state.root)
    store.save_readiness(str(state.day), {'complete': False})
    refreshed = Mock()
    monkeypatch.setattr(pipeline, 'finalize_market_data', refreshed)

    def prepare(*args, on_progress, **kwargs):
        on_progress('sync_daily', 45, '准备正式行情')
        if failure == 'exception':
            raise DailyCoverageError('正式行情不完整')
        if failure == 'cancel':
            pipeline.cancel_job(pipeline_jobs._run_slot_owner)
        return {}

    monkeypatch.setattr(daily_pipeline, 'run_now', prepare)
    async def run():
        result = await jobs.trigger(state.repo, day=state.day, capset=object())
        await drain_tasks()
        return result

    result = asyncio.run(run())
    job = registry.get(result['job_id'])
    assert job['status'] == 'failed'
    assert job['outcome'] == ('cancelled' if failure == 'cancel' else 'failed')
    assert not store.has_original(str(state.day))
    refreshed.assert_called_once_with(state.repo, None)
    jobs._queue_tracking.assert_not_awaited()
    assert pipeline_jobs._run_slot_owner is None


@pytest.mark.parametrize('reason', ['before_close', 'automatic'])
def test_missing_inputs_are_not_prepared_outside_manual_current_postmarket(manual_runtime, monkeypatch, reason):
    state, registry, _, _ = manual_runtime
    EveningReportStore(state.root).save_readiness(str(state.day), {'complete': False})
    if reason == 'before_close':
        monkeypatch.setattr(jobs, 'cn_now', lambda: state.now.replace(hour=15))
    with pytest.raises(HTTPException) as error:
        asyncio.run(jobs.trigger(state.repo, day=state.day, capset=object(), automatic=reason == 'automatic'))
    assert error.value.status_code == 409
    assert not registry.list_recent()


@pytest.mark.parametrize('calendar_result', ['trading', 'closed', 'unknown'])
def test_manual_preparation_requires_confirmed_calendar_after_refresh(manual_runtime, monkeypatch, calendar_result):
    from app.jobs import daily_pipeline
    from app.services import trading_day

    state, registry, _, _ = manual_runtime
    store = EveningReportStore(state.root)
    store.save_readiness(str(state.day), {'complete': False})
    calendar_path = state.root / 'calendar' / 'trading_days.json'
    calendar_path.unlink()

    def sync_calendar(root):
        assert root == state.root
        if calendar_result != 'unknown':
            atomic_json(calendar_path, {**state.evidence['calendar'],
                                       'days': state.evidence['calendar']['days'] if calendar_result == 'trading' else []})
        return {}

    def prepare(*args, **kwargs):
        inputs.save_ready(state.root, state.day, state.coverage,
                          state.evidence['classifications'], state.evidence['market'])
        return {}

    refreshed = Mock(side_effect=sync_calendar)
    prepared = Mock(side_effect=prepare)
    monkeypatch.setattr(trading_day, 'sync_calendar', refreshed)
    monkeypatch.setattr(daily_pipeline, 'run_now', prepared)
    monkeypatch.setattr(pipeline, 'finalize_market_data', lambda *a: None)
    async def run():
        result = await jobs.trigger(state.repo, day=state.day, capset=object())
        await drain_tasks()
        return result

    result = asyncio.run(run())
    assert registry.get(result['job_id'])['status'] == ('succeeded' if calendar_result == 'trading' else 'failed')
    assert store.has_original(str(state.day)) is (calendar_result == 'trading')
    assert prepared.call_count == (1 if calendar_result == 'trading' else 0)
    refreshed.assert_called_once_with(state.root)


@pytest.mark.parametrize('ready_at_submission', [False, True])
def test_manual_preparation_rechecks_date_after_waiting_for_execution(manual_runtime, monkeypatch, ready_at_submission):
    from app.jobs import daily_pipeline

    state, registry, _, _ = manual_runtime
    store = EveningReportStore(state.root)
    if not ready_at_submission:
        store.save_readiness(str(state.day), {'complete': False})
    original_dates = store.dates()
    prepared = Mock()
    monkeypatch.setattr(daily_pipeline, 'run_now', prepared)
    async def run():
        result = await jobs.trigger(state.repo, day=state.day, capset=object())
        store.save_readiness(str(state.day), {'complete': False})
        monkeypatch.setattr(jobs, 'cn_now', lambda: state.now + timedelta(days=1))
        await drain_tasks()
        return result

    result = asyncio.run(run())
    assert registry.get(result['job_id'])['status'] == 'failed'
    assert '跨日期' in registry.get(result['job_id'])['error']
    assert store.dates() == original_dates
    prepared.assert_not_called()


def test_submitted_job_can_publish_its_ready_day_after_midnight(manual_runtime, monkeypatch):
    from app.services import evening_report_engine

    state, registry, _, _ = manual_runtime
    store = EveningReportStore(state.root)
    calculate = evening_report_engine.build_report

    def crossing_midnight(*args, **kwargs):
        payload = calculate(*args, **kwargs)
        monkeypatch.setattr(jobs, 'cn_now', lambda: state.now + timedelta(days=1))
        return payload

    monkeypatch.setattr(evening_report_engine, 'build_report', crossing_midnight)
    async def run():
        result = await jobs.trigger(state.repo, day=state.day, capset=object())
        await drain_tasks()
        return result

    result = asyncio.run(run())
    assert registry.get(result['job_id'])['status'] == 'succeeded'
    assert store.has_original(str(state.day))


def test_later_pipeline_failure_still_generates_independent_report_after_releasing_slot(task_runtime, monkeypatch):
    state, registry = task_runtime
    actual_generate = jobs.generate
    seen = []
    def generation(*args, **kwargs):
        owner = pipeline_jobs._run_slot_owner
        seen.append(owner)
        assert registry.get(owner)['request_key'].startswith('evening_report:')
        return actual_generate(*args, **kwargs)
    monkeypatch.setattr(jobs, 'generate', generation)
    async def run():
        def late_failure(progress):
            def execute():
                progress('minute', 95, '分钟历史失败')
                raise ValueError('分钟历史失败')
            return execute
        parent = await pipeline._submit_job(state.repo, None, late_failure,
                                            request_key=f'postmarket:{state.day}', finalize=lambda: None)
        await drain_tasks()
        return parent
    parent = asyncio.run(run())
    assert registry.get(parent['job_id'])['status'] == 'failed'
    assert len(seen) == 1 and seen[0] != parent['job_id']
    assert EveningReportStore(state.root).has_original(str(state.day))
    assert jobs.status(state.root, str(state.day))['status'] == 'published'
    assert pipeline_jobs._run_slot_owner is None


def test_cancelled_parent_never_derives_evening_and_restart_keeps_cancellation(task_runtime):
    from threading import Event
    state, registry = task_runtime
    started, finish = Event(), Event()
    async def run():
        def make(progress):
            def execute():
                started.set()
                assert finish.wait(3)
                progress('minute', 95, '取消检查')
                return {}
            return execute
        parent = await pipeline._submit_job(state.repo, None, make,
                                            request_key=f'postmarket:{state.day}', finalize=lambda: None)
        assert await asyncio.to_thread(started.wait, 3)
        pipeline.cancel_job(parent['job_id'])
        # This check runs before the parent worker has reached its finally block.
        assert await jobs.run_scheduled(state.repo) is None
        finish.set()
        await drain_tasks()
        return parent
    parent = asyncio.run(run())
    assert registry.get(parent['job_id'])['outcome'] == 'cancelled'
    assert jobs.status(state.root, str(state.day))['status'] == 'cancelled'
    assert not EveningReportStore(state.root).has_original(str(state.day))
    restarted = pipeline_jobs.JobStore(store_dir=state.root / 'jobs')
    assert restarted.get(parent['job_id'])['outcome'] == 'cancelled'
    assert asyncio.run(jobs.run_scheduled(state.repo)) is None


def test_pending_evening_restart_is_failed_and_can_retry_only_its_step(task_runtime, monkeypatch):
    state, registry = task_runtime
    key = f'evening_report:{state.day}:live'
    job_id, _ = registry.create(request_key=key)
    jobs._save_attempt(state.root, str(state.day), {'job_id': job_id, 'status': 'generating'})
    restored = pipeline_jobs.JobStore(store_dir=state.root / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', restored)
    monkeypatch.setattr(pipeline, 'job_store', restored)
    assert jobs.status(state.root, str(state.day))['status'] == 'failed'
    async def retry():
        result = await jobs.run_scheduled(state.repo)
        await drain_tasks()
        return result
    result = asyncio.run(retry())
    assert result['job_id'] != job_id
    assert restored.get(result['job_id'])['status'] == 'succeeded'
    assert all(job.get('request_key', '').startswith(('evening_report:', 'evening_tracking:')) for job in restored.list_recent())


@pytest.mark.parametrize('automatic', [False, True])
def test_published_archive_survives_event_failure_and_retry_only_republishes_update(accepted, monkeypatch, automatic):
    from app.services import auth, evening_report_engine, trading_day
    state = accepted
    def fail(*_):
        raise OSError('事件记录暂不可写')
    monkeypatch.setattr(shared_runtime, 'publish_data_update', fail)
    with pytest.raises(OSError, match='事件记录'):
        jobs.generate(state.repo, state.day, progress=lambda *a, **k: None)
    original = EveningReportStore(state.root).original_metadata(str(state.day))
    assert original
    seen = []
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda root, datasets: seen.append(datasets))
    monkeypatch.setattr(evening_report_engine, 'build_report', lambda *a, **k: pytest.fail('仅补通知不得再次计算'))
    monkeypatch.setattr(auth, 'require_current_session', lambda: {'role': 'admin'})
    monkeypatch.setattr(preferences, 'get_pipeline_schedule', lambda: {'hour': 16, 'minute': 0})
    monkeypatch.setattr(trading_day, 'is_trading_day', lambda *_a, **_k: True)
    monkeypatch.setattr(jobs, '_queue_tracking', AsyncMock())

    def retry():
        if automatic:
            asyncio.run(jobs.run_scheduled(state.repo))
            return
        return asyncio.run(jobs.trigger(state.repo, day=state.day))

    result = retry()
    if not automatic:
        assert result['version'] == original['version'] and result['reused']
    assert EveningReportStore(state.root).original_metadata(str(state.day))['version'] == original['version']
    assert seen == [['evening_report']]
    asyncio.run(jobs.after_job(state.repo, {'id': 'finished', 'request_key': f'evening_report:{state.day}:live:test',
                                          'status': 'succeeded', 'finished_at': state.now.isoformat()}))
    retry()
    assert seen == [['evening_report']]


def test_capacity_queued_evening_can_cancel_without_computing(task_runtime, monkeypatch):
    from app.services import heavy_job_limiter
    state, registry = task_runtime
    limiter = heavy_job_limiter.HeavyJobLimiter(2, cancel_poll_interval=.005)
    monkeypatch.setattr(heavy_job_limiter, 'shared_heavy_job_limiter', limiter)
    assert limiter.acquire('normal', timeout=0)
    async def run():
        result = await jobs.trigger(state.repo, day=state.day, automatic=True)
        for _ in range(200):
            if registry.get(result['job_id'])['log']:
                break
            await asyncio.sleep(.005)
        assert registry.get(result['job_id'])['status'] == 'pending'
        pipeline.cancel_job(result['job_id'])
        await drain_tasks()
        return result
    try:
        result = asyncio.run(run())
    finally:
        limiter.release('normal')
    assert registry.get(result['job_id'])['outcome'] == 'cancelled'
    assert jobs.status(state.root, str(state.day))['status'] == 'cancelled'
    assert not EveningReportStore(state.root).has_original(str(state.day))
    assert pipeline_jobs._run_slot_owner is None


def test_historical_request_returns_existing_original_or_unpublished_without_submitting(manual_runtime, monkeypatch):
    state, _, _, _ = manual_runtime
    store = EveningReportStore(state.root)
    submit = AsyncMock()
    monkeypatch.setattr(pipeline, '_submit_job', submit)
    monkeypatch.setattr(jobs, 'cn_now', lambda: state.now + timedelta(days=1))
    date_key = str(state.day)
    missing = asyncio.run(jobs.trigger(state.repo, day=state.day))
    assert missing['status'] == 'unpublished'
    assert not store.has_original(date_key)
    original = store.save(date_key, {'audit': {}, 'state': {}}, {'stock_generation': 'test'})
    reused = asyncio.run(jobs.trigger(state.repo, day=state.day))
    assert reused['status'] == 'published' and reused['version'] == original['version']
    with pytest.raises(HTTPException) as exc:
        asyncio.run(jobs.trigger(state.repo, day=state.day, revision=True))
    assert exc.value.status_code == 409
    submit.assert_not_awaited()


@pytest.mark.parametrize(('now', 'allowed'), [('2026-11-20T10:00', False), ('2026-11-20T16:00', True),
                                               ('2026-11-22T17:00', False), ('2026-11-23T17:00', True)])
def test_generation_uses_close_time_and_confirmed_calendar_range(tmp_path, monkeypatch, now, allowed):
    instant = datetime.fromisoformat(now).replace(tzinfo=CN_TZ)
    monkeypatch.setattr(jobs, 'cn_now', lambda: instant)
    atomic_json(tmp_path / 'calendar' / 'trading_days.json', {
        'days': ['2026-11-20'], 'start': '2026-11-20', 'end': '2026-11-22'})
    status = jobs.status(tmp_path, str(instant.date()))
    assert status['can_generate'] is allowed
    if not allowed:
        repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(jobs.trigger(repo, day=instant.date(), automatic=True))
        assert (exc.value.status_code, exc.value.detail) == (409, status['reason'])



def test_ready_replacement_requires_current_generation_and_uses_only_latest_concepts(accepted, monkeypatch):
    state = accepted
    first, second = state.observations
    revised = {**first, 'memberships': [], 'id': 'latest'}
    event = {'symbol': state.symbols[0], 'date': str(state.day), 'id': 'notice', 'title': '严重异常波动'}
    monkeypatch.setattr('app.services.evening_report_events.fetch_disclosures', lambda *_: ([event], []))
    last = inputs.save_ready(state.root, state.day, state.coverage, [revised], state.market)
    assert last['industry_status'] == 'missing'
    assert inputs.require_ready(state.root, state.day)['classifications'] == [revised]
    data = inputs.load_inputs(state.repo, state.day, last)
    assert data.memberships.is_empty() and data.provenance['industry_status'] == 'missing'
    assert data.provenance['severe_events']['events'][state.symbols[0]]['e2']['announcement_id'] == 'notice'
    assert list((state.root / 'evening_report' / 'ready').iterdir()) == [
        state.root / 'evening_report' / 'ready' / f'{state.day}.json']
    bump_enriched_generation(state.root, 'stock')
    with pytest.raises(DailyCoverageError, match='指标代次'):
        inputs.require_ready(state.root, state.day)
    with pytest.raises(DailyCoverageError, match='概念分类'):
        inputs.save_ready(state.root, state.day, state.coverage, [second], state.market)


def test_uses_shared_indicators_and_last_source_row_without_full_market_recalculation(accepted):
    state = accepted
    symbol = state.symbols[0]
    latest_path = state.root / 'kline_daily' / f'date={state.day}' / 'part.parquet'
    raw = pl.read_parquet(latest_path)
    before = raw.filter(pl.col('symbol') == symbol).with_columns(pl.lit(1.).alias('volume'))
    pl.concat([before, raw]).write_parquet(latest_path)
    missing_day = state.calendar[12]
    path = state.root / 'kline_daily' / f'date={missing_day}' / 'part.parquet'
    pl.read_parquet(path).filter(pl.col('symbol') != symbol).write_parquet(path)
    reference = state.repo.get_enriched_range(state.day, state.day, columns=['atr_14', 'bull_bear_line'])
    today = inputs.load_inputs(state.repo, state.day, state.evidence).prices.filter(
        (pl.col('symbol') == symbol) & (pl.col('date') == state.day))
    assert today.height == 1 and today['volume'].item() == 10000000.
    assert today.select('atr_14', 'bull_bear_line').row(0) == reference.filter(
        pl.col('symbol') == symbol).select('atr_14', 'bull_bear_line').row(0)


def test_evening_ma10_uses_traded_closes_before_wave_window(tmp_path):
    from test_evening_report_wave import SYMBOL, bars, dates

    from app.services.evening_report_wave import WINDOW, compute_wave
    from app.tickflow.repository import DataStore, KlineRepository

    calendar = dates(WINDOW + 16)
    pattern, _ = bars([(11.9, 11.2, 11.35, 50), (11.4, 10.7, 10.8, 50),
                       (11.4, 10.8, 11.3, 150)])
    # 最后一根放量收盘 11.30，仅站上 MA10，随后缩量横盘至报告日。
    pattern_rows = list(pattern.values())
    traded = dict(pattern_rows[0], close=10.7)
    halt = dict(traded, open=0., high=0., volume=0.)
    source = [traded] * 9 + [halt] * 7 + pattern_rows
    source += [dict(pattern_rows[-1], volume=20)] * (len(calendar) - len(source))
    frame = pl.DataFrame([{**row, 'symbol': SYMBOL, 'date': date.fromisoformat(day),
                           'volume': float(row['volume']), 'amount': 1e8}
                          for day, row in zip(calendar, source, strict=True)]).drop('ma10', 'formal')
    write(tmp_path, 'kline_daily', frame)
    write(tmp_path, 'kline_daily_enriched', frame)
    repo = KlineRepository(DataStore(tmp_path))
    repo.refresh_cache()
    window = calendar[-WINDOW:]
    history, _ = inputs._history(repo, date.fromisoformat(window[-1]), calendar, lambda: None)
    rows = {str(row['date']): row for row in history.to_dicts()}
    assert sorted(rows) == window
    assert rows[window[0]]['ma10'] == pytest.approx((9 * 10.7 + 10.75) / 10)
    wave, _ = compute_wave(SYMBOL, rows, window, set())
    assert wave['second_wave_start']['date'] == window[len(pattern) - 1]
    assert wave['current_wave_index'] == 2 and not wave['first_pullback_active']


def test_restart_waits_for_shared_indicators_before_first_publication(accepted, monkeypatch):
    state = accepted
    store = EveningReportStore(state.root)
    with monkeypatch.context() as cold:
        cold.setattr(state.repo, 'get_enriched_history_span', lambda: None)
        with pytest.raises(DailyCoverageError, match='共享指标'):
            jobs.generate(state.repo, state.day, progress=lambda *args, **kwargs: None)
        assert not store.has_original(str(state.day))
    assert jobs.generate(state.repo, state.day, progress=lambda *args, **kwargs: None)['status'] == 'published'


def test_status_only_suspension_and_evaluation_after_ready_expiry(tmp_path):
    from app.services.postmarket import read_suspensions, save_suspensions

    symbol = '600001.SH'
    k, halted, split = [date(2026, 11, n) for n in [16, 17, 18]]
    days = [str(d) for d in (k, halted, split)]
    atomic_json(tmp_path / 'calendar' / 'trading_days.json', {'days': days})
    coverage = DailyCoverage(halted)
    coverage.record_halted([symbol], source='trading_status', basis='full_session_suspension_status')
    save_suspensions(tmp_path, coverage)
    write(tmp_path, 'kline_daily', pl.DataFrame({'symbol': [symbol], 'date': [split], 'close': [5.5]}))
    factor = tmp_path / 'adj_factor' / 'all.parquet'
    factor.parent.mkdir()
    pl.DataFrame({'symbol': [symbol], 'trade_date': [split], 'ex_factor': [2.]}).write_parquet(factor)
    history = inputs.load_evaluation_inputs(tmp_path, split, symbols={symbol}, days=set(days), include_multipliers=True)
    assert history.provenance['suspensions'][0]['date'] == str(halted)
    multipliers = history.provenance['evaluation_multipliers'][symbol]
    assert multipliers == {str(k): .5, str(halted): .5, str(split): 1.}
    assert history.prices['close'].item() / multipliers[str(k)] == 11.
    save_suspensions(tmp_path, DailyCoverage(halted))
    assert read_suspensions(tmp_path, days) == []

def test_evaluation_missing_local_sources_is_unavailable(tmp_path):
    day = date(2026, 10, 16)
    symbol = '600001.SH'
    def evaluate():
        return inputs.load_evaluation_inputs(tmp_path, day, days={str(day)}, symbols={symbol}, include_multipliers=True)
    with pytest.raises(DailyCoverageError, match='日历'):
        evaluate()
    atomic_json(tmp_path / 'calendar' / 'trading_days.json', {'days': [str(day)]})
    empty = evaluate()
    assert empty.prices.is_empty() and empty.provenance['evaluation_multipliers'] == {}
    write(tmp_path, 'kline_daily', pl.DataFrame({'symbol': [symbol], 'date': [day], 'close': [5.5]}))
    assert evaluate().prices.select('raw_close', 'close').row(0) == (5.5, None)

def test_evaluation_constraints_use_each_endpoint_facts(tmp_path):
    symbol = '600001.SH'
    first, second = date(2026, 11, 2), date(2026, 11, 3)
    days = [str(first), str(second)]
    atomic_json(tmp_path / 'calendar' / 'trading_days.json', {'days': days})
    raw = pl.DataFrame({'symbol': [symbol] * 2, 'date': [first, second], 'close': [10., 11.]})
    write(tmp_path, 'kline_daily', raw)
    write(tmp_path, 'kline_daily_enriched', raw.with_columns(
        pl.Series('consecutive_limit_ups', [0, 1], dtype=pl.UInt32),
        pl.Series('consecutive_limit_downs', [0, 0], dtype=pl.UInt32)))
    path = tmp_path / 'instruments' / 'instruments.parquet'
    path.parent.mkdir()
    pl.DataFrame({'symbol': [symbol], 'is_st': [False], 'as_of': [second]}).write_parquet(path)
    result = inputs.load_evaluation_inputs(tmp_path, second, days=set(days), symbols={symbol}, include_constraints=True)
    assert result.prices.sort('date').select('signal_limit_up', 'signal_limit_down', 'is_st').rows() == [
        (False, False, None), (True, False, False)]
