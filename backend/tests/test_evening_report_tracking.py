from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth, evening_report, pipeline
from app.market_time import CN_TZ
from app.services import evening_report_jobs as jobs
from app.services import evening_report_store, pipeline_jobs, shared_runtime
from app.services import evening_report_tracking as tracking
from app.services.auth import AuthStore
from app.services.auth_access import install_auth_middleware
from app.services.evening_report_store import EveningReportStore
from app.services.user_context import UserContext, user_scope

SYMBOL = '300001.SZ'


@pytest.fixture
def history(private_owner, tmp_path, monkeypatch):
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

    def publish(index, selected=True, theme='题材甲', wave=None):
        day = days[index]
        with monkeypatch.context() as patch:
            patch.setattr(evening_report_store, 'cn_now', lambda: datetime.fromisoformat(day + 'T17:00:00+08:00'))
            return store.save(day, {'trade_date': day, 'cutoff': day + 'T16:00:00+08:00', 'audit': {}, 'records': [
                {'symbol': SYMBOL, 'name': '观察股', 'selected': selected,
                 'observation': {**({'wave': wave} if wave else {}), 'decision': {'all_reason_codes': [], 'steps': [], 'comparison_eligible': selected,
                                              'comparison_group': None, 'rank_global_role': 1, 'rank_within_theme_role': 1},
                                 'role': 'fallback_leader', 'primary_theme': {'name': theme},
                                 'variables': {'return_20d': {'value': .35}}}}]}, {})

    return SimpleNamespace(root=tmp_path, days=days, inputs=inputs, store=store, publish=publish)


def mark(history, report, *, starred=True, outcome=None):
    return tracking.update_feedback(history.root, day=report['date'], version=report['version'],
                                    symbol=SYMBOL, starred=starred, outcome=outcome)


@pytest.mark.parametrize('calendar_state', ['known_target', 'short_calendar', 'unconfirmed_recommendation'])
def test_mature_window_pending_review_is_shared_by_history_detail_and_overview(history, monkeypatch, calendar_state):
    report = history.publish(0)
    target = history.days[3]
    history.inputs.trade_date = history.days[1]
    if calendar_state == 'unconfirmed_recommendation':
        evening_report_store.atomic_json(history.root / 'calendar' / 'trading_days.json', {'days': history.days[1:]})
        assert tracking.refresh_after_publication(history.root, history.days[1])['evaluation_ready']
    else:
        if calendar_state == 'short_calendar':
            history.inputs.calendar = history.days[:2]
        tracking.refresh_tracking(history.root, history.inputs)

    def window():
        return tracking.personal_history(history.root)['records'][0]['performance']['3']

    monkeypatch.setattr(tracking, 'cn_now', lambda: datetime.fromisoformat(target + 'T14:59:59+08:00'))
    if calendar_state == 'unconfirmed_recommendation':
        assert window()['reason_code'] == 'calendar_unconfirmed'
    if calendar_state != 'known_target':
        assert window()['target_date'] is None
        evening_report_store.atomic_json(history.root / 'calendar' / 'trading_days.json', {'days': history.days})
    assert window()['status'] == 'not_due' and window()['target_date'] == target
    monkeypatch.setattr(tracking, 'cn_now', lambda: datetime.fromisoformat(target + 'T15:00:00+08:00'))
    overdue = window()
    assert overdue['status'] == 'overdue_pending_review' and overdue['as_of'] == history.days[1]
    assert tracking.personal_report(history.root, report['date'], report['version'])['records'][0]['performance']['3'] == overdue
    assert tracking.personal_overview(history.root)['performance']['3'] == {
        'total': 1, 'evaluated': 0, 'not_due': 0, 'overdue_pending_review': 1, 'unavailable': 0, 'mean_return': None,
    }
    history.inputs.trade_date = target
    history.inputs.calendar = history.days
    tracking.refresh_tracking(history.root, history.inputs)
    assert window()['status'] == 'evaluated'
    assert tracking.personal_overview(history.root)['performance']['3']['mean_return'] == pytest.approx(.03)


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


def test_cancel_switch_duplicate_retry_and_account_isolation(history):
    report = history.publish(0)
    tracking.refresh_tracking(history.root, history.inputs)
    first = mark(history, report, outcome='loss')
    assert first['starred'] and first['outcome'] == 'loss'
    assert mark(history, report, outcome='loss')['outcome'] == 'loss'
    second = mark(history, report, starred=False, outcome='profit')
    assert not second['starred'] and second['outcome'] == 'profit'
    mark(history, report, starred=False, outcome=None)
    state = tracking.personal_report(history.root, report['date'], report['version'])['records'][0]['feedback']
    assert not state['starred'] and state['outcome'] is None
    overview = tracking.personal_overview(history.root)
    assert (overview['starred'], overview['profit'], overview['loss'], overview['unmarked']) == (0, 0, 0, 1)
    with user_scope(UserContext('b' * 32, 'guest', history.root)):
        other = tracking.personal_report(history.root, report['date'], report['version'])
        assert other['records'][0]['feedback']['updated_at'] is None
    assert history.store.get(report['date']) == report


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
                'starred': True, 'outcome': None}
        assert client.put('/api/evening-report/feedback', json=body).status_code == 200
        assert client.post('/api/evening-report/rebuild', json={'date': report['date']}).status_code == 403
        assert client.post('/api/evening-report/feedback', json=body).status_code == 403
        assert client.put('/api/evening-report/feedback', json={**body, 'owner': 'a' * 32}).status_code == 422
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
    monkeypatch.setattr(tracking, 'calendar_status', lambda *_: {'days': history.days})
    result = tracking.refresh_after_publication(history.root, history.days[-1], request_key='test_unready')
    assert result['evaluation_ready'] is False
    assert tracking.personal_report(history.root, report['date'], report['version'])['ready']
    assert mark(history, report)['starred']
    assert not tracking.refresh_completed(history.root, 'test_unready')
    monkeypatch.setattr(tracking, 'cn_now', lambda: datetime.fromisoformat(history.days[-1] + 'T17:00:00+08:00'))
    row = tracking.personal_history(history.root)['records'][0]
    assert all(item['status'] == 'overdue_pending_review' for item in row['performance'].values())


def test_tracking_notification_failure_retries_after_restart_without_duplicate_records(history, monkeypatch):
    from app.services import preferences, trading_day

    report = history.publish(0)
    day = report['date']
    now = datetime.fromisoformat(day).replace(hour=16, tzinfo=CN_TZ)
    registry = pipeline_jobs.JobStore(store_dir=history.root / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', registry)
    monkeypatch.setattr(pipeline, 'job_store', registry)
    monkeypatch.setattr(pipeline_jobs, '_run_slot_owner', None)
    monkeypatch.setattr(jobs, 'cn_now', lambda: now)
    monkeypatch.setattr(preferences, 'get_pipeline_schedule', lambda: {'hour': 16, 'minute': 0})
    monkeypatch.setattr(trading_day, 'is_trading_day', lambda *_args, **_kwargs: True)
    monkeypatch.setattr(trading_day, 'calendar_status', lambda *_: {'days': history.days})
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=history.root))
    write_notification = shared_runtime.atomic_write_text

    def fail_notification(*_args, **_kwargs):
        raise OSError('更新通知暂时无法写入')

    async def scheduled_check():
        queued = await jobs.run_scheduled(repo)
        while pipeline._tasks:
            await asyncio.gather(*list(pipeline._tasks))
        return queued

    monkeypatch.setattr(shared_runtime, 'atomic_write_text', fail_notification)
    first = asyncio.run(scheduled_check())
    failed = registry.get(first['job_id'])
    assert failed['status'] == 'failed' and '更新通知暂时无法写入' in failed['error']
    assert history.store.get(day) == report
    assert not tracking.refresh_completed(history.root, failed['request_key'])
    # Registration succeeded before the notification failed; feedback must survive retry.
    assert tracking.personal_report(history.root, day, report['version'])['ready']
    feedback = mark(history, report, outcome='profit')

    restarted = pipeline_jobs.JobStore(store_dir=history.root / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', restarted)
    monkeypatch.setattr(pipeline, 'job_store', restarted)
    monkeypatch.setattr(shared_runtime, 'atomic_write_text', write_notification)
    now += timedelta(minutes=15)
    retried = asyncio.run(scheduled_check())
    completed = restarted.get(retried['job_id'])
    assert completed['request_key'] == failed['request_key']
    assert completed['status'] == 'succeeded'
    assert completed['result']['registered_reports'] == 0
    assert tracking.refresh_completed(history.root, failed['request_key'])
    notification = json.loads((history.root / 'runtime' / 'data_update.json').read_text())
    assert notification['datasets'] == ['evening_report_tracking']
    rows = tracking.personal_history(history.root)['records']
    assert len(rows) == 1 and rows[0]['feedback']['starred'] == feedback['starred']
    assert rows[0]['feedback']['outcome'] == 'profit'
    assert asyncio.run(scheduled_check()) is None


def test_derived_request_key_is_stable_for_same_report_and_market_input(history, monkeypatch):
    from unittest.mock import AsyncMock
    report = history.publish(0)
    submit = AsyncMock(return_value={'job_id': 'queued'})
    monkeypatch.setattr(pipeline, '_submit_job', submit)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=history.root))
    asyncio.run(jobs._queue_tracking(repo, report['date']))
    original_key = submit.call_args.kwargs['request_key']
    asyncio.run(jobs._queue_tracking(repo, report['date']))
    assert submit.call_args.kwargs['request_key'] == original_key


@pytest.fixture
def tracking_scheduler(history, monkeypatch):
    from unittest.mock import AsyncMock

    from app.services import preferences, trading_day

    day = history.days[0]
    now = datetime.fromisoformat(day).replace(hour=16, tzinfo=CN_TZ)
    history.store.save_readiness(day, {'complete': True, 'stock_generation': 'b'})
    report = history.store.save(day, {'audit': {}, 'records': []}, {'input_id': 'b' * 32})
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
    state.store.save_readiness(state.day, {'complete': True, 'stock_generation': 'c'})
    asyncio.run(jobs.after_job(state.repo, {'request_key': f'postmarket:{state.day}',
                                           'status': 'succeeded', 'id': 'parent'}))
    state.submit.assert_not_awaited()
    assert state.store.get(state.day) == state.report


@pytest.mark.parametrize('mode', ['ordinary_failure', 'next_day', 'read_final'])
def test_tracking_retry_respects_cancelled_state_after_reading_final(tracking_scheduler, monkeypatch, mode):
    from app.services import auth as auth_service

    state = tracking_scheduler
    job_id, _ = state.registry.create(request_key=state.key)
    if mode == 'ordinary_failure':
        state.registry.fail(job_id, '任务超时')
    else:
        pipeline.cancel_job(job_id)
    if mode == 'next_day':
        monkeypatch.setattr(jobs, 'cn_now', lambda: state.now + timedelta(days=1))
    elif mode == 'read_final':
        monkeypatch.setattr(auth_service, 'require_current_session', lambda: {'role': 'admin'})
        asyncio.run(jobs.trigger(state.repo, day=date.fromisoformat(state.day)))
        state.submit.reset_mock()
    result = asyncio.run(jobs._queue_tracking(state.repo, state.day))
    if mode == 'read_final':
        assert result is None
        state.submit.assert_not_awaited()
    else:
        assert result == {'job_id': 'queued'}
        assert state.submit.call_args.kwargs['request_key'] == state.key


def test_observation_path_keeps_signed_extremes_and_separates_missing_middle_row():
    days = ['2026-11-09', '2026-11-10', '2026-11-11', '2026-11-12']
    rows = {(SYMBOL, day): {'close': close, 'formal': True}
            for day, close in zip(days, [100., 108., 95., 102.], strict=True)}

    def evaluate():
        return tracking.evaluate_performance(SYMBOL, days[0], calendar=days, as_of=days[-1],
                                             prices=rows, suspensions=set(), data_version={})['3']

    result = evaluate()
    assert result['return'] == pytest.approx(.02)
    assert result['path']['max_favorable_return'] == pytest.approx(.08)
    assert result['path']['max_adverse_return'] == pytest.approx(-.05)
    del rows[(SYMBOL, days[2])]
    result = evaluate()
    assert result['return'] == pytest.approx(.02)
    assert result['path']['missing_count'] == 1
    assert result['path']['max_favorable_return'] is None
    assert result['path']['max_adverse_return'] is None


def test_completed_endpoints_retry_only_the_missing_path(history):
    report = history.publish(0)
    inputs = history.inputs
    middle = history.days[2]
    complete = inputs.prices
    inputs.prices = complete.filter(pl.col('date') != middle)
    tracking.refresh_tracking(history.root, inputs)
    before = tracking.personal_history(history.root)['records'][0]['performance']
    assert all(item['status'] == 'evaluated' for item in before.values())
    assert before['3']['path']['missing_count'] == 1
    inputs.prices = complete.with_columns(pl.when(pl.col('date') == middle).then(9.).otherwise('close').alias('close'))
    tracking.refresh_tracking(history.root, inputs)
    after = tracking.personal_report(history.root, report['date'], report['version'])['records'][0]['performance']
    assert tracking.personal_history(history.root)['records'][0]['performance'] == after
    assert after['1'] == before['1']
    for window in ('3', '5'):
        assert after[window] == {**before[window], 'path': after[window]['path']}
        assert after[window]['path']['missing_count'] == 0
        assert after[window]['path']['max_adverse_return'] == pytest.approx(-.10)
    assert tracking.refresh_tracking(history.root, inputs)['evaluated_recommendations'] == 0
