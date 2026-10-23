"""Rejected research streams can retry admission without poisoning reconnect state."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.api import backtest as api
from app.backtest import worker
from app.services import task_scheduler

pytestmark = pytest.mark.usefixtures("authenticated_actor")


@pytest.mark.parametrize("kind", ["backtest", "optimize", "walkforward"])
@pytest.mark.parametrize("execution_error", [False, True])
def test_rejected_streams_retry_immediately_but_executed_jobs_remain_reconnectable(
    monkeypatch, kind, execution_error,
):
    monkeypatch.setattr(api, "_running_jobs", {})
    monkeypatch.setattr(api, "_guard_minute_strategy_backtest", lambda *args: None)
    monkeypatch.setattr(api.settings, "backtest_range_guard", False)
    monkeypatch.setattr(worker, "make_worker_task", lambda task_kind, *args: {"kind": task_kind})
    run_worker = Mock(return_value={"ok": kind})
    if execution_error:
        run_worker.side_effect = ValueError("计算数据不完整")
    monkeypatch.setattr(worker, "run_worker_task", run_worker)
    scheduler = task_scheduler.TaskScheduler(workers=1, capacity=1, name="retry-test")
    submit = Mock(side_effect=lambda work: scheduler.submit("research", work))
    monkeypatch.setattr(task_scheduler, "submit_research", submit)
    release = threading.Event()
    blocker = scheduler.submit("busy", lambda: release.wait(5))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=object())))
    endpoint = {"backtest": api.strategy_stream, "optimize": api.optimize_stream,
                "walkforward": api.walkforward_stream}[kind]
    args = {"strategy_id": "test", "start": "2026-01-01", "end": "2026-02-01"}
    if kind != "backtest":
        args["param_grid"] = '{"period": [1, 2]}'

    async def consume(response):
        return "".join([event async for event in response.body_iterator])

    async def exercise():
        first = await endpoint(request, **args)
        # A duplicate can attach between registration and the first generator's
        # admission attempt. Both subscribers must finish even after eviction.
        attached = await endpoint(request, **args)
        rejected = next(iter(api._running_jobs.values()))
        errors = await asyncio.wait_for(asyncio.gather(consume(first), consume(attached)), timeout=2)
        assert all("event: error" in events and "队列已满" in events for events in errors)
        assert rejected.done and rejected.future is None
        assert api._running_jobs == {}
        assert submit.call_count == 1
        run_worker.assert_not_called()

        release.set()
        assert blocker.result(2)
        retried = await endpoint(request, **args)
        accepted = next(iter(api._running_jobs.values()))
        assert accepted is not rejected
        events = await asyncio.wait_for(consume(retried), timeout=2)
        assert ("计算数据不完整" if execution_error else "event: done") in events
        assert submit.call_count == 2
        run_worker.assert_called_once()
        assert api._running_jobs[accepted.key] is accepted
        assert accepted.done
        assert accepted.error == ("计算数据不完整" if execution_error else None)

        # Success and actual execution failure keep their existing 300-second
        # replay contract, unlike requests that never entered the queue.
        replay = await consume(await endpoint(request, **args))
        assert replay.rstrip().split("\n\n")[-1] == events.rstrip().split("\n\n")[-1]
        assert submit.call_count == 2

    try:
        asyncio.run(exercise())
    finally:
        release.set()
        blocker.result(2)


def test_rejected_job_cannot_remove_a_newer_job_with_the_same_key(monkeypatch):
    previous = api._BacktestJob("same-request")
    current = api._BacktestJob("same-request")
    monkeypatch.setattr(api, "_running_jobs", {current.key: current})
    api._finish_job(previous, error="队列已满", retain=False)
    assert previous.done and previous.error == "队列已满"
    assert api._running_jobs[current.key] is current
    assert not current.done
