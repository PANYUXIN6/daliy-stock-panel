from concurrent.futures import CancelledError
from contextvars import ContextVar
from threading import Event

import pytest

from app.services.task_scheduler import JobQueueFullError, TaskScheduler


def test_bounded_queue_round_robin_context_and_cancel_release():
    scheduler = TaskScheduler(workers=1, capacity=4, per_owner=3)
    entered, release = Event(), Event()
    owner = ContextVar('test_owner', default='missing')
    order = []

    def blocker():
        entered.set()
        assert release.wait(5)

    first = scheduler.submit('a', blocker)
    assert entered.wait(5)
    token = owner.set('a')
    try:
        a1 = scheduler.submit('a', lambda: order.append(owner.get()))
        a2 = scheduler.submit('a', lambda: order.append(owner.get()))
        with pytest.raises(JobQueueFullError, match='你的'):
            scheduler.submit('a', lambda: None)
    finally:
        owner.reset(token)
    token = owner.set('b')
    try:
        b = scheduler.submit('b', lambda: order.append(owner.get()))
    finally:
        owner.reset(token)
    with pytest.raises(JobQueueFullError, match='服务器'):
        scheduler.submit('c', lambda: None)
    assert a2.cancel()
    replacement = scheduler.submit('a', lambda: order.append('a-last'))
    release.set()
    for future in (first, a1, b, replacement):
        future.result(5)
    with pytest.raises(CancelledError):
        a2.result()
    assert order == ['a', 'b', 'a-last']
    assert scheduler.submit('a', lambda: 42).result(5) == 42


def test_failed_task_releases_capacity_and_does_not_kill_worker():
    scheduler = TaskScheduler(workers=1, capacity=1, per_owner=1)
    def fail():
        raise ValueError('broken')
    with pytest.raises(ValueError, match='broken'):
        scheduler.submit('a', fail).result(5)
    assert scheduler.submit('b', lambda: 'ok').result(5) == 'ok'


def test_sse_queue_rejection_finishes_even_inside_registry_lock(monkeypatch, private_owner):
    from concurrent.futures import ThreadPoolExecutor

    from app.api import backtest
    from app.services import task_scheduler

    def reject(work):
        raise JobQueueFullError('queue full')
    monkeypatch.setattr(task_scheduler, 'submit_research', reject)
    job = backtest._BacktestJob('key')
    def submit():
        with backtest._jobs_lock:
            backtest._submit_research_job(job, lambda: None)
    with ThreadPoolExecutor(1) as pool:
        pool.submit(submit).result(5)
    assert job.done and job.error == 'queue full'
