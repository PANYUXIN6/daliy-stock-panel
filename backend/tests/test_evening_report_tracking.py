from __future__ import annotations

import asyncio
import json
import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth, evening_report, pipeline
from app.market_time import CN_TZ
from app.services import evening_report_jobs as jobs
from app.services import evening_report_tracking as tracking
from app.services import pipeline_jobs, shared_runtime
from app.services.auth import AuthStore
from app.services.auth_access import install_auth_middleware
from app.services.evening_report_store import EveningReportStore, RevisionBranch
from app.services.user_context import UserContext, user_scope

SYMBOL = '300001.SZ'


@pytest.fixture
def history(private_owner, tmp_path):
    days, cursor = [], date(2026, 10, 12)
    while len(days) < 18:
        if cursor.weekday() < 5:
            days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    prices = pl.DataFrame([{'symbol': SYMBOL, 'date': day, 'close': 10 + i / 10,
                           'formal': True, 'raw_high': 12., 'raw_low': 9.,
                           'signal_limit_up': i == 1, 'signal_limit_down': False}
                          for i, day in enumerate(days)])
    inputs = SimpleNamespace(calendar=days, trade_date=days[-1], prices=prices,
                             cutoff=days[-1] + 'T16:00:00+08:00', provenance={})
    store = EveningReportStore(tmp_path)

    def publish(index, selected=True, theme='题材甲'):
        return store.save(days[index], {'trade_date': days[index], 'records': [
            {'symbol': SYMBOL, 'name': '观察股', 'selected': selected,
             'observation': {'role': 'fallback_leader', 'primary_theme': {'name': theme},
                             'variables': {'return_20d': {'value': .35}}}}]}, {})

    return SimpleNamespace(root=tmp_path, days=days, inputs=inputs, store=store, publish=publish)


def mark(history, report, *, starred=True, outcome=None, revision=0, request_id='request_0000000001'):
    return tracking.update_feedback(history.root, day=report['date'], version=report['version'],
                                    symbol=SYMBOL, starred=starred, outcome=outcome,
                                    expected_revision=revision, request_id=request_id)


def test_episode_continues_through_theme_change_and_closes_after_five_valid_absences(history):
    first = history.publish(0)
    history.publish(1, theme='题材乙')
    for index in range(2, 7):
        history.publish(index, selected=False)
    renewed = history.publish(7)
    tracking.refresh_tracking(history.root, history.inputs)
    rows = tracking.personal_history(history.root)['records']
    assert len(rows) == 3
    assert rows[1]['episode_id'] == rows[2]['episode_id']
    assert rows[0]['episode_id'] != rows[1]['episode_id']
    assert rows[2]['ended_at'] == history.days[6]
    assert rows[2]['end_known_at'] and rows[2]['end_reason'] == 'five_consecutive_valid_reports_absent'
    assert rows[2]['absence_count_at_recommendation'] == rows[1]['absence_count_at_recommendation'] == 0
    assert (rows[2]['appearance_seq'], rows[1]['appearance_seq'], rows[0]['appearance_seq']) == (1, 2, 1)
    assert 'ended_at' not in rows[2]['observation']
    assert rows[1]['observation']['primary_theme']['name'] == '题材乙'
    mark(history, first)
    assert not tracking.personal_report(history.root, renewed['date'], renewed['version'])['records'][0]['feedback']['starred']
    overview = tracking.personal_overview(history.root)
    assert overview['episode_count'] == 2 and overview['starred'] == 1
    assert overview['recommendation_count'] == 3
    assert overview['performance']['1']['total'] == 2  # fixed first recommendation anchors


def test_missing_report_interrupts_absence_count_and_repeated_refresh_is_idempotent(history):
    history.publish(0)
    for index in (1, 2, 4, 5, 6, 7):
        history.publish(index, selected=False)
    history.publish(8)
    tracking.refresh_tracking(history.root, history.inputs)
    assert tracking.refresh_tracking(history.root, history.inputs)['registered_reports'] == 0
    rows = tracking.personal_history(history.root)['records']
    assert len({row['episode_id'] for row in rows}) == 1
    assert rows[0]['ended_at'] is None


def test_revision_keeps_episode_and_first_original_anchor_without_overwriting_report(history):
    original = history.publish(0)
    tracking.refresh_tracking(history.root, history.inputs)
    mark(history, original, outcome='profit')
    branch = RevisionBranch(history.store)
    revised = branch.append(original['date'], {'trade_date': original['date'], 'records': [
        {'symbol': SYMBOL, 'name': '观察股', 'selected': True,
         'observation': {'role': 'true_leader', 'primary_theme': {'name': '修订题材'}}}]}, {'input_id': 'b' * 32})
    branch.commit()
    tracking.refresh_tracking(history.root, history.inputs)
    rows = tracking.personal_history(history.root)['records']
    assert len(rows) == 2 and len({row['episode_id'] for row in rows}) == 1
    assert {row['first_reference'] for row in rows} == {original['version'] + ':' + SYMBOL}
    assert all(row['feedback']['outcome'] == 'profit' for row in rows)
    assert tracking.personal_report(history.root, revised['date'], revised['version'])['records'][0]['observation']['role'] == 'true_leader'
    assert history.store.get(original['date']) == original
    assert tracking.personal_overview(history.root)['episode_count'] == 1


def test_cancel_switch_duplicate_retry_and_account_isolation(history):
    report = history.publish(0)
    tracking.refresh_tracking(history.root, history.inputs)
    first = mark(history, report, outcome='loss')
    assert first['starred'] and first['outcome'] == 'loss'
    assert mark(history, report, outcome='loss') == first
    second = mark(history, report, starred=False, outcome='profit', revision=1, request_id='request_0000000002')
    assert second['revision'] == 2
    mark(history, report, starred=False, outcome=None, revision=2, request_id='request_0000000003')
    state = tracking.personal_report(history.root, report['date'], report['version'])['records'][0]['feedback']
    assert state == {'starred': False, 'outcome': None, 'revision': 3, 'updated_at': state['updated_at']}
    events = tracking.personal_history(history.root)['events']
    assert len(events) == 3
    assert 'star_cancel' in events[1]['actions']
    assert 'loss_cancel' in events[1]['actions']
    assert events[0]['actions'] == ['profit_cancel']
    overview = tracking.personal_overview(history.root)
    assert (overview['starred'], overview['profit'], overview['loss'], overview['unmarked']) == (0, 0, 0, 1)
    with user_scope(UserContext('b' * 32, 'guest', history.root)):
        other = tracking.personal_report(history.root, report['date'], report['version'])
        assert other['records'][0]['feedback']['revision'] == 0
        assert tracking.personal_history(history.root)['events'] == []
    assert history.store.get(report['date']) == report


def _concurrent_mark(root, day, version, request_id):
    from pathlib import Path
    with user_scope(UserContext('a' * 32, 'admin', Path(root))):
        try:
            tracking.update_feedback(Path(root), day=day, version=version, symbol=SYMBOL,
                                     starred=True, outcome=None, expected_revision=0, request_id=request_id)
            return 'saved'
        except tracking.FeedbackConflictError:
            return 'conflict'


def test_cross_process_version_conflict_preserves_one_event(history):
    report = history.publish(0)
    tracking.refresh_tracking(history.root, history.inputs)
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context('spawn')) as pool:
        futures = [pool.submit(_concurrent_mark, str(history.root), report['date'], report['version'], f'parallel_request_{n}')
                   for n in range(2)]
        assert sorted(f.result(timeout=20) for f in futures) == ['conflict', 'saved']
    assert len(tracking.personal_history(history.root)['events']) == 1


def test_failed_event_insert_rolls_back_current_feedback(history):
    report = history.publish(0)
    tracking.refresh_tracking(history.root, history.inputs)
    mark(history, report)
    with sqlite3.connect(tracking._personal_path(history.root)) as db:
        db.execute("CREATE TRIGGER fail_write BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'disk failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        mark(history, report, starred=False, revision=1, request_id='request_0000000002')
    restored = tracking.personal_report(history.root, report['date'], report['version'])['records'][0]['feedback']
    assert restored['starred'] and restored['revision'] == 1
    assert len(tracking.personal_history(history.root)['events']) == 1


def test_performance_uses_calendar_and_same_adjusted_basis_with_explicit_constraints(history):
    days = history.days
    rows = {(SYMBOL, day): {'close': 10., 'formal': True} for day in days}
    rows[(SYMBOL, days[1])] = {'close': 10.5, 'formal': True, 'signal_limit_up': True,
                              'raw_high': 21., 'raw_low': 21.}
    rows.pop((SYMBOL, days[3]))
    result = tracking.evaluate_performance(SYMBOL, days[0], calendar=days, as_of=days[4],
                                          prices=rows, suspensions=set(), data_version={'stock_generation': 4})
    assert result['1']['return'] == pytest.approx(.05)
    assert result['1']['trading_constraints'] == ['目标日涨停', '目标日一字价格']
    assert result['3']['status'] == 'unavailable' and result['3']['return'] is None
    assert result['5']['status'] == 'not_due' and result['5']['target_date'] == days[5]
    halted = tracking.evaluate_performance(SYMBOL, days[0], calendar=days, as_of=days[5],
                                          prices=rows, suspensions={(SYMBOL, days[1])}, data_version={})
    assert halted['1']['status'] == 'unavailable' and halted['1']['return'] is None
    assert halted['1']['target_date'] == days[1]  # never shifted to resumption


def test_evaluated_prices_are_retained_and_missing_samples_can_recover(history):
    report = history.publish(0)
    history.inputs.prices = history.inputs.prices.filter(pl.col('date') != history.days[3])
    tracking.refresh_tracking(history.root, history.inputs)
    before = tracking.personal_history(history.root)['records'][0]['performance']
    assert before['1']['status'] == 'evaluated' and before['3']['status'] == 'unavailable'
    history.inputs.prices = history.inputs.prices.with_columns((pl.col('close') * 2).alias('close'))
    history.inputs.prices = pl.concat([history.inputs.prices, pl.DataFrame([{
        'symbol': SYMBOL, 'date': history.days[3], 'close': 21., 'formal': True,
        'raw_high': 12., 'raw_low': 9., 'signal_limit_up': False, 'signal_limit_down': False}])])
    tracking.refresh_tracking(history.root, history.inputs)
    after = tracking.personal_report(history.root, report['date'], report['version'])['records'][0]['performance']
    assert after['1'] == before['1']
    assert after['3']['return'] == pytest.approx(.05)


def test_gets_are_readonly_and_feedback_has_exact_guest_permission(history):
    report = history.publish(0)
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=history.root))
    app.state.auth_store = AuthStore(history.root)
    app.state.auth_store.bootstrap('admin', 'initial-secret')
    guest = app.state.auth_store.create_user('guest', 'temporary-secret', 'guest')
    token, _ = app.state.auth_store.login('guest', 'temporary-secret', 'ip', '')
    token, _ = app.state.auth_store.change_password(token, 'temporary-secret', 'changed-secret')
    app.include_router(evening_report.router)
    install_auth_middleware(app)
    with TestClient(app) as client:
        assert client.get('/api/evening-report/history').status_code == 401
        client.cookies.set(auth.COOKIE_NAME, token)
        assert client.get('/api/evening-report/history').json()['records'] == []
        assert client.get('/api/evening-report/overview').json()['episode_count'] == 0
        assert not tracking._shared_path(history.root).exists()
        assert not (history.root / 'users' / guest['id'] / 'evening_report').exists()
        tracking.refresh_tracking(history.root, history.inputs)
        body = {'date': report['date'], 'version': report['version'], 'symbol': SYMBOL,
                'starred': True, 'outcome': None, 'expected_revision': 0, 'request_id': 'request_0000000001'}
        assert client.put('/api/evening-report/feedback', json=body).status_code == 200
        assert client.post('/api/evening-report/rebuild', json={'date': report['date']}).status_code == 403
        assert client.post('/api/evening-report/feedback', json=body).status_code == 403
        assert client.put('/api/evening-report/feedback', json={**body, 'owner': 'a' * 32}).status_code == 422
        assert client.put('/api/evening-report/feedback', json={**body, 'request_id': 'request_0000000002'}).status_code == 409
        assert client.put('/api/evening-report/feedback', json=body,
                          headers={'Origin': 'https://evil.example'}).status_code == 403
    assert tracking.personal_overview(history.root)['starred'] == 0


def test_derived_history_failure_keeps_publication_in_a_separate_job(history, monkeypatch, caplog):
    report = history.publish(0)
    registry = pipeline_jobs.JobStore(store_dir=history.root / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', registry)
    monkeypatch.setattr(pipeline, 'job_store', registry)
    monkeypatch.setattr(pipeline_jobs, '_run_slot_owner', None)
    def fail(*args, **kwargs):
        assert registry.get(pipeline_jobs._run_slot_owner)['request_key'].startswith('evening_tracking:')
        raise OSError('unavailable')
    monkeypatch.setattr(tracking, 'refresh_after_publication', fail)
    async def run():
        await jobs.after_job(SimpleNamespace(store=SimpleNamespace(data_dir=history.root)), {
            'request_key': f'evening_report:{report["date"]}:live:test', 'id': 'job', 'status': 'succeeded'})
        while pipeline._tasks:
            await asyncio.gather(*list(pipeline._tasks))
    asyncio.run(run())
    assert history.store.has_original(report['date'])
    assert jobs._read_attempt(history.root, report['date'])['status'] == 'published'
    derived = registry.list_recent()[0]
    assert derived['request_key'].startswith('evening_tracking:') and derived['status'] == 'failed'
    assert 'unavailable' in caplog.text
    assert json.loads(history.store._path(report['date']).read_text())['versions'][0]['version'] == report['version']


def test_unready_market_inputs_do_not_block_feedback_on_published_reports(history, monkeypatch):
    report = history.publish(0)
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *_: None)
    from app.services import trading_day
    monkeypatch.setattr(trading_day, 'calendar_status', lambda *_: {'days': history.days})
    result = tracking.refresh_after_publication(history.root, history.days[-1], request_key='test_unready')
    assert result['evaluation_ready'] is False
    assert tracking.personal_report(history.root, report['date'], report['version'])['ready']
    assert mark(history, report)['starred']
    assert tracking.refresh_completed(history.root, 'test_unready')
    row = tracking.personal_history(history.root)['records'][0]
    assert all(item['status'] == 'unavailable' for item in row['performance'].values())


def test_derived_request_key_changes_for_revision_with_identical_market_input(history, monkeypatch):
    from unittest.mock import AsyncMock
    report = history.publish(0)
    submit = AsyncMock(return_value={'job_id': 'queued'})
    monkeypatch.setattr(pipeline, '_submit_job', submit)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=history.root))
    asyncio.run(jobs._queue_tracking(repo, report['date']))
    original_key = submit.call_args.kwargs['request_key']
    branch = RevisionBranch(history.store)
    revised = branch.append(report['date'], {'trade_date': report['date'], 'records': []}, {'input_id': 'b' * 32})
    branch.commit()
    asyncio.run(jobs._queue_tracking(repo, report['date']))
    revised_key = submit.call_args.kwargs['request_key']
    assert original_key != revised_key and revised['version'] in revised_key


@pytest.fixture
def tracking_scheduler(history, monkeypatch):
    from unittest.mock import AsyncMock

    from app.services import preferences, trading_day

    day = history.days[0]
    now = datetime.fromisoformat(day).replace(hour=16, tzinfo=CN_TZ)
    history.store.save_readiness(day, {'complete': True, 'input_id': 'b' * 32, 'scope_version': 'test'})
    report = history.store.save(day, {'records': []}, {'input_id': 'b' * 32})
    registry = pipeline_jobs.JobStore(store_dir=history.root / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', registry)
    monkeypatch.setattr(pipeline, 'job_store', registry)
    monkeypatch.setattr(jobs, 'cn_now', lambda: now)
    monkeypatch.setattr(preferences, 'get_pipeline_schedule', lambda: {'hour': 16, 'minute': 0})
    monkeypatch.setattr(trading_day, 'is_trading_day', lambda *_args, **_kwargs: True)
    # 00:30 Beijing is the previous UTC date. Cancellation follows Beijing days.
    finished = now.replace(hour=0, minute=30).astimezone(UTC).isoformat()
    monkeypatch.setattr(pipeline_jobs, '_now_iso', lambda: finished)
    submit = AsyncMock(return_value={'job_id': 'queued'})
    monkeypatch.setattr(pipeline, '_submit_job', submit)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=history.root))
    asyncio.run(jobs._queue_tracking(repo, day))
    key = submit.call_args.kwargs['request_key']
    submit.reset_mock()
    return SimpleNamespace(root=history.root, store=history.store, report=report, day=day, now=now,
                           registry=registry, repo=repo, submit=submit, key=key)


@pytest.mark.parametrize('running', [False, True])
def test_tracking_cancellation_blocks_auto_retries_and_survives_restart(tracking_scheduler, monkeypatch, running):
    state = tracking_scheduler
    job_id, _ = state.registry.create(request_key=state.key)
    if running:
        state.registry.start(job_id)
    pipeline.cancel_job(job_id)
    assert state.registry.get(job_id)['outcome'] == 'cancelled'
    assert asyncio.run(jobs.run_scheduled(state.repo)) is None
    restarted = pipeline_jobs.JobStore(store_dir=state.root / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', restarted)
    monkeypatch.setattr(pipeline, 'job_store', restarted)
    assert asyncio.run(jobs.run_scheduled(state.repo)) is None
    # A fresh input and another successful parent must not undo the user's stop.
    state.store.save_readiness(state.day, {'complete': True, 'input_id': 'c' * 32, 'scope_version': 'test'})
    asyncio.run(jobs.after_job(state.repo, {'request_key': f'postmarket:{state.day}',
                                           'status': 'succeeded', 'id': 'parent'}))
    state.submit.assert_not_awaited()
    assert state.store.get(state.day) == state.report


@pytest.mark.parametrize('mode', ['ordinary_failure', 'next_day', 'manual_resume'])
def test_tracking_failure_next_day_and_manual_resume_can_retry(tracking_scheduler, monkeypatch, mode):
    from app.services import auth as auth_service

    state = tracking_scheduler
    job_id, _ = state.registry.create(request_key=state.key)
    if mode == 'ordinary_failure':
        state.registry.fail(job_id, '任务超时')
    else:
        pipeline.cancel_job(job_id)
    if mode == 'next_day':
        monkeypatch.setattr(jobs, 'cn_now', lambda: state.now + timedelta(days=1))
    elif mode == 'manual_resume':
        monkeypatch.setattr(auth_service, 'require_current_session', lambda: {'role': 'admin'})
        asyncio.run(jobs.trigger(state.repo, day=date.fromisoformat(state.day)))
        state.submit.reset_mock()
    result = asyncio.run(jobs._queue_tracking(state.repo, state.day))
    assert result == {'job_id': 'queued'}
    assert state.submit.call_args.kwargs['request_key'] == state.key
    if mode == 'manual_resume':
        # A new cancellation after the explicit resume takes precedence again.
        monkeypatch.setattr(pipeline_jobs, '_now_iso', lambda: (state.now + timedelta(minutes=1)).isoformat())
        job_id, _ = state.registry.create(request_key=state.key)
        pipeline.cancel_job(job_id)
        state.submit.reset_mock()
        assert asyncio.run(jobs._queue_tracking(state.repo, state.day)) is None
        state.submit.assert_not_awaited()
