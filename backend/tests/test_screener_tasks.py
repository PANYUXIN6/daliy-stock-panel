"""手动选股的更新资格、单飞、持久结果与跨用户隔离。"""
import threading
import time
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import screener
from app.services import screener_snapshots, screener_tasks, strategy_cache, strategy_run_queue
from app.services.user_context import UserContext, user_scope
from app.strategy import config
from app.strategy.engine import CompositeChild, CompositeSpec

pytestmark = pytest.mark.usefixtures('private_owner')
DAY = date(2026, 11, 11)


class Engine:
    def __init__(self):
        self._strategies = {sid: SimpleNamespace(meta={'id': sid, 'asset_types': ['stock', 'etf'], 'timeframes': ['1d']},
                                                basic_filter={}, composite=None, file_path=None) for sid in ('a', 'b', 'combo')}
        self._strategies['combo'].composite = CompositeSpec((CompositeChild('a', .8), CompositeChild('b', .2)))
        self.calls = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def has(self, sid):
        return sid in self._strategies

    def get(self, sid):
        return self._strategies[sid]

    def run_all(self, context, *, strategy_ids, params_map, overrides_map, parallel):
        self.calls.append((strategy_ids, overrides_map))
        self.entered.set()
        assert self.release.wait(4)
        return {sid: SimpleNamespace(total=1, rows=[{'symbol': '000001.SZ', 'close': 10}]) for sid in strategy_ids}


@pytest.fixture
def env(tmp_path, monkeypatch):
    manager = strategy_run_queue.StrategyRunManager()
    monkeypatch.setattr(strategy_run_queue, 'MANAGER', manager)
    from app.services import heavy_job_limiter
    monkeypatch.setattr(heavy_job_limiter, '_memory_admission_ok', lambda: True)
    clock = [datetime(2026, 11, 11, 16, tzinfo=ZoneInfo('Asia/Shanghai'))]
    publication = [{'as_of': str(DAY), 'version': 'formal-v1'}]
    monkeypatch.setattr(screener_tasks, 'cn_now', lambda: clock[0])
    monkeypatch.setattr(screener_tasks.review_publication, 'latest_strategy_publication', lambda *args: publication[0])
    monkeypatch.setattr(screener_tasks, 'ScreenerService', lambda *a, **k: SimpleNamespace(build_strategy_context=lambda *a, **k: None))
    engine = Engine()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app = FastAPI()
    app.state.repo = repo
    app.state.user_runtimes = SimpleNamespace(get=lambda owner: SimpleNamespace(strategy_engine=engine))
    app.include_router(screener.router)
    yield SimpleNamespace(root=tmp_path, manager=manager, engine=engine, repo=repo, clock=clock,
                          publication=publication, client=TestClient(app))
    engine.release.set()
    for status in manager.list_statuses(screener_tasks.NAMESPACE):
        manager.cancel(status['run_id'])
        finish(manager, status['run_id'])


def finish(manager, run_id):
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        status = manager.status(run_id)
        if status and status['done']:
            return status
        time.sleep(.01)
    pytest.fail('task did not finish')


def click(env, sid='a'):
    return env.client.post(f'/api/screener/tasks/{sid}', json={'asset_type': 'stock'})


def test_before_1600_and_no_formal_data_are_read_only(env):
    strategy_cache.write_cache(env.root, '2026-11-10', {'a': {'as_of': '2026-11-10', 'total': 3, 'rows': []}})
    env.clock[0] = env.clock[0].replace(hour=15, minute=59)
    response = click(env)
    assert response.status_code == 200 and response.json()['task'] is None
    assert '16:00' in response.json()['notice']
    env.clock[0] = env.clock[0].replace(hour=16, minute=0)
    env.publication[0] = None
    assert click(env).json()['task'] is None
    assert not env.engine.calls
    assert env.client.get('/api/screener/tasks').json()['results']['a']['total'] == 3
    assert env.client.get('/api/screener/cached-result/a?manual=true').json()['result']['as_of'] == '2026-11-10'


def test_each_card_has_one_task_and_refresh_recovers_both(env):
    env.engine.release.clear()
    first = click(env).json()['task']
    try:
        assert env.engine.entered.wait(2)
        second = click(env, 'combo').json()['task']
        assert second['run_id'] != first['run_id']
        for _ in range(6):
            response = click(env)
            assert response.status_code == 202
            assert response.json()['task']['run_id'] == first['run_id']
        tasks = env.client.get('/api/screener/tasks').json()['tasks']
        assert set(tasks) == {'a', 'combo'} and not any(t['done'] for t in tasks.values())
        with user_scope(UserContext('b' * 32, 'pro', env.root)):
            assert env.manager.list_statuses(screener_tasks.NAMESPACE) == []
            assert env.manager.cancel(first['run_id']) is None
    finally:
        env.engine.release.set()
    assert not finish(env.manager, second['run_id'])['error']


def test_valid_result_reuses_cache_until_inputs_change(env):
    initial = click(env, 'combo').json()['task']
    assert not finish(env.manager, initial['run_id'])['error']
    assert click(env, 'combo').json()['task'] is None
    strategy_cache.clear_cache(env.root, invalidate_manual=False)
    assert click(env, 'combo').json()['task'] is None
    for sid, value in [('combo', {'children': [{'strategy_id': 'a', 'weight': .5}, {'strategy_id': 'b', 'weight': .5}]}),
                       ('a', {'params': {'threshold': 20}}), ('combo', {'params': {'merge_mode': 'intersection'}})]:
        config.save_override(env.root, sid, value)
        task = click(env, 'combo').json()['task']
        assert task and task['run_id'] != initial['run_id']
        assert not finish(env.manager, task['run_id'])['error']
        assert click(env, 'combo').json()['task'] is None
    env.publication[0] = {'as_of': str(DAY), 'version': 'formal-v2'}
    changed = click(env, 'combo').json()['task']
    assert changed and not finish(env.manager, changed['run_id'])['error']
    assert len(env.engine.calls) == 5


def test_edit_while_running_never_publishes_stale_result(env):
    strategy_cache.write_cache(env.root, '2026-11-10', {'a': {'as_of': '2026-11-10', 'total': 3, 'rows': []}})
    env.engine.release.clear()
    first = click(env).json()['task']
    try:
        assert env.engine.entered.wait(2)
        config.save_override(env.root, 'a', {'params': {'threshold': 20}})
        assert click(env).json()['task']['run_id'] == first['run_id']
        assert env.client.get('/api/screener/tasks').json()['results']['a']['total'] == 3
    finally:
        env.engine.release.set()
    assert '参数' in finish(env.manager, first['run_id'])['error']
    assert env.client.get('/api/screener/tasks').json()['results']['a']['total'] == 3
    new = click(env).json()['task']
    assert new['run_id'] != first['run_id']
    assert not finish(env.manager, new['run_id'])['error']


def test_cancel_preserves_previous_results_and_other_cards(env):
    strategy_cache.write_cache(env.root, '2026-11-10', {'a': {'as_of': '2026-11-10', 'total': 3, 'rows': []},
                                                    'b': {'as_of': '2026-11-10', 'total': 7, 'rows': []}})
    env.engine.release.clear()
    running = click(env).json()['task']
    try:
        assert env.engine.entered.wait(2)
        response = env.client.delete(f"/api/screener/run-status/{running['run_id']}")
        assert response.json()['phase'] == 'cancelling'
    finally:
        env.engine.release.set()
    assert finish(env.manager, running['run_id'])['phase'] == 'cancelled'
    assert env.client.get('/api/screener/tasks').json()['results']['a']['total'] == 3
    fresh = click(env).json()['task']
    finish(env.manager, fresh['run_id'])
    restored = screener_tasks.results(env.root, 'stock')
    assert restored['a']['total'] == 1 and restored['b']['total'] == 7
    env.manager._completed.clear()
    assert click(env).json()['task'] is None


def test_history_browsing_does_not_evict_current_date_result(env):
    first = click(env).json()['task']
    finish(env.manager, first['run_id'])
    env.publication[0] = {'as_of': '2026-11-10', 'version': 'prior'}
    older = click(env).json()['task']
    finish(env.manager, older['run_id'])
    assert screener_tasks.results(env.root, 'stock', date(2026, 11, 10))['a']['as_of'] == '2026-11-10'
    env.publication[0] = {'as_of': str(DAY), 'version': 'formal-v1'}
    assert click(env).json()['task'] is None
    assert screener_tasks.results(env.root, 'stock')['a']['as_of'] == str(DAY)


@pytest.mark.parametrize('storage', ['manual', 'legacy'])
@pytest.mark.parametrize('verified_day', [str(DAY), '2026-11-10'])
def test_recompute_preserves_only_same_day_verification(env, storage, verified_day):
    verification = {
        'status': 'completed', 'checked_at': f'{verified_day}T08:00:00Z',
        'candidate_count': 1, 'confirmed_count': 0, 'pending_count': 1, 'excluded_count': 0,
        'candidates': [{'symbol': '600001.SH', 'verification_status': 'pending'}],
    }
    previous = {'as_of': verified_day, 'total': 0, 'rows': [], 'verification': verification}
    if storage == 'manual':
        screener_snapshots.save_result(env.root, 'stock', '1d', verified_day, 'a', previous)
    else:
        strategy_cache.write_cache(env.root, verified_day, {'a': previous})
    screener_snapshots.invalidate(env.root, 'a')

    task = click(env).json()['task']
    assert not finish(env.manager, task['run_id'])['error']
    result = env.client.get('/api/screener/cached-result/a?manual=true').json()['result']
    brief = env.client.get('/api/screener/tasks').json()['results']['a']
    assert result['as_of'] == str(DAY) and result['total'] == 1
    assert result['rows'] == [{'symbol': '000001.SZ', 'close': 10}]
    assert 'invalidated' not in brief
    assert click(env).json()['task'] is None
    if verified_day == str(DAY):
        assert result['verification'] == verification
        assert brief['verification'] == {k: v for k, v in verification.items() if k != 'candidates'}
    else:
        assert 'verification' not in result and 'verification' not in brief
        historical = screener_tasks.results(env.root, 'stock', date.fromisoformat(verified_day))['a']
        assert historical['verification'] == verification


def test_recompute_preserves_verification_updated_while_running(env):
    previous = {'as_of': str(DAY), 'total': 0, 'rows': [],
                'verification': {'status': 'running', 'checked_at': None, 'candidates': []}}
    screener_snapshots.save_result(env.root, 'stock', '1d', str(DAY), 'a', previous)
    env.engine.release.clear()
    task = click(env).json()['task']
    latest = {**previous, 'verification': {
        'status': 'completed', 'checked_at': f'{DAY}T09:00:00Z',
        'candidates': [{'symbol': '600001.SH', 'verification_status': 'pending'}],
    }}
    try:
        assert env.engine.entered.wait(2)
        screener_snapshots.save_result(env.root, 'stock', '1d', str(DAY), 'a', latest)
    finally:
        env.engine.release.set()
    assert not finish(env.manager, task['run_id'])['error']
    result = env.client.get('/api/screener/cached-result/a?manual=true').json()['result']
    assert result['verification'] == latest['verification']
    assert result['rows'] == [{'symbol': '000001.SZ', 'close': 10}]


def test_real_composite_uses_saved_child_params_and_persisted_version(env, monkeypatch):
    import polars as pl

    from app.api.strategy import _render_composite_code
    from app.strategy.engine import StrategyDataContext, StrategyEngine
    definitions = env.root / 'definitions'
    definitions.mkdir()
    (definitions / 'leaf.py').write_text('''import polars as pl
META = {"id": "leaf", "name": "leaf", "asset_types": ["stock"], "timeframes": ["1d"],
        "params": [{"id": "threshold", "type": "float", "default": 0}]}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    return pl.col("close") > params["threshold"]
''')
    (definitions / 'combo.py').write_text(_render_composite_code('combo', '组合', '',
        [{'strategy_id': 'leaf', 'weight': 1.0}], 'union', 0))
    engine = StrategyEngine(strategy_dirs=[definitions])
    config.save_override(env.root, 'leaf', {'params': {'threshold': 100}})
    config.save_override(env.root, 'combo', {'basic_filter': {'enabled': False}})
    context = StrategyDataContext(asset_type='stock', timeframe='1d', as_of=DAY,
        current=pl.DataFrame({'symbol': ['LOW.SZ', 'HIGH.SH'], 'date': [DAY, DAY], 'close': [10., 200.]}))
    monkeypatch.setattr(screener_tasks, 'ScreenerService', lambda *a, **k: SimpleNamespace(build_strategy_context=lambda *a, **k: context))
    env.client.app.state.user_runtimes = SimpleNamespace(get=lambda _: SimpleNamespace(strategy_engine=engine))
    accepted = click(env, 'combo').json()['task']
    assert not finish(env.manager, accepted['run_id'])['error']
    result = env.client.get('/api/screener/cached-result/combo?manual=true').json()['result']
    assert [row['symbol'] for row in result['rows']] == ['HIGH.SH']
    assert click(env, 'combo').json()['task'] is None
