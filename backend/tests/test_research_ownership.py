"""Research identities survive background work without sharing private results."""
import asyncio
import threading

import pytest

from app.api import backtest, mining
from app.backtest.candidates import CandidateStore
from app.backtest.worker import make_worker_task
from app.services.mining_jobs import MiningRunStore
from app.services.mining_manager import MiningJobManager
from app.services.user_context import UserContext, current_user, user_scope


def owner(root, marker, role='guest'):
    return UserContext(marker * 32, role, root)


def test_missing_owner_fails_closed(tmp_path):
    for create in (CandidateStore, MiningRunStore, MiningJobManager):
        with pytest.raises(RuntimeError, match='身份'):
            create(tmp_path)
    with pytest.raises(RuntimeError, match='身份'):
        make_worker_task('mining', tmp_path, {})


@pytest.mark.parametrize('cancel', [backtest.optimize_cancel, backtest.walkforward_cancel])
def test_even_admin_cannot_cancel_another_owners_job(tmp_path, cancel):
    class Request:
        async def json(self):
            return {'job_key': job.key}
    with user_scope(owner(tmp_path, 'a')):
        key = backtest._make_opt_job_key('s', None, None, None, '{}', 'sharpe', None, '')
        job = backtest._BacktestJob(key)
    backtest._running_jobs[key] = job
    try:
        with user_scope(owner(tmp_path, 'b', 'admin')):
            other = backtest._make_opt_job_key('s', None, None, None, '{}', 'sharpe', None, '')
            assert other != key
            assert asyncio.run(cancel(Request()))['ok'] is False
            assert not job.cancel_event.is_set()
        with user_scope(owner(tmp_path, 'a')):
            assert asyncio.run(cancel(Request()))['ok'] is True
    finally:
        backtest._running_jobs.pop(key)


def test_private_runs_candidates_and_result_lookup(tmp_path):
    with user_scope(owner(tmp_path, 'a')):
        runs = MiningRunStore(tmp_path)
        runs.create({}, {}, run_id='secret-run')
        candidate = CandidateStore(tmp_path).create(kind='factor', name='私有', source_id='rsi_14', config={}, metrics={}, data_as_of=None)
    with user_scope(owner(tmp_path, 'b', 'admin')):
        other = MiningRunStore(tmp_path)
        assert other.list_runs() == []
        with pytest.raises(Exception) as error:
            mining._required_manifest(other, 'secret-run')
        assert error.value.status_code == 404
        assert CandidateStore(tmp_path).list() == []
    assert runs.runs_root.is_relative_to(tmp_path / 'users' / ('a' * 32))
    assert candidate['name'] == '私有'


def test_manager_captures_owner_for_threads_and_keeps_market_root_shared(tmp_path):
    captured = []
    ready = threading.Event()
    def runner(task, progress, cancel):
        captured.append((current_user().id, task))
        ready.set()
        return {'status': 'succeeded'}
    managers = []
    try:
        ids = []
        for marker in ('a', 'b'):
            with user_scope(owner(tmp_path, marker)):
                manager = MiningJobManager(tmp_path, worker_runner=runner)
                managers.append(manager)
                ids.append(manager.start({'same': True}, {})['run_id'])
        assert ids[0] != ids[1]
        for manager in managers:
            for future in list(manager._futures.values()):
                future.result(timeout=5)
        assert {item[0] for item in captured} == {'a' * 32, 'b' * 32}
        assert all(item[1]['owner']['id'] == item[0] for item in captured)
        assert all(item[1]['data_dir'] == str(tmp_path.resolve()) for item in captured)
        assert managers[1].store.get(ids[0]) is None
    finally:
        for manager in managers:
            manager.shutdown()
