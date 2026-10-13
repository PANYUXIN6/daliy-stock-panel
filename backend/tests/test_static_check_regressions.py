"""Regression evidence for delayed SDK callbacks and repair-task lifetime."""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.api import data, kline
from app.market_time import CN_TZ
from app.services import collection_status, data_integrity, kline_sync, pipeline_jobs
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


@pytest.mark.parametrize("mode", ["daily_count", "daily_range", "minute_count", "minute_range", "adj_factor"])
def test_delayed_sdk_callbacks_keep_original_chunk_and_window(monkeypatch, mode):
    target = kline_sync
    callbacks = []
    requests = []

    def delayed(fn):
        # A worker can begin evaluating its callback only after the caller abandoned
        # the deadline and continued with another chunk or date segment.
        callbacks.append(fn)
        raise target.SDKCallTimeoutError("abandoned")

    def sdk_request(symbols, **kwargs):
        requests.append((list(symbols), kwargs))
        return []

    monkeypatch.setattr(target, "_sdk_call_with_deadline", delayed)
    monkeypatch.setattr(target, "sleep_between_batches", lambda *args: None)
    monkeypatch.setattr(target, "get_client", lambda: SimpleNamespace(
        klines=SimpleNamespace(batch=sdk_request, ex_factors=sdk_request),
    ))
    symbols = ["600000.SH", "000001.SZ"]
    if mode.startswith("daily"):
        window = {"start_time": datetime(2026, 8, 3), "end_time": datetime(2026, 8, 5)} if mode.endswith("range") else {}
        assert target.sync_daily_batch(symbols, batch_size=1, **window).is_empty()
    elif mode.startswith("minute"):
        monkeypatch.setattr(target, "_try_custom_minute", lambda *a, **kw: (None, True))
        window = {"start_time": datetime(2026, 8, 3), "end_time": datetime(2026, 8, 5)} if mode.endswith("range") else {}
        assert target.sync_minute_batch(symbols, batch_size=1, segment_trading_days=1, **window).is_empty()
    else:
        monkeypatch.setattr(target.preferences, "get_adj_factor_provider", lambda: "tickflow")
        monkeypatch.setattr(target, "resolve_limit", lambda *a, **kw: SimpleNamespace(batch=1, rpm=None))
        assert target.sync_adj_factor(symbols, None, SimpleNamespace(has=lambda _: True)) == (0, [])
    for callback in callbacks:
        callback()
    assert [request[0] for request in requests] == [[symbols[i % 2]] for i in range(len(requests))]
    if mode == "minute_range":
        assert len(requests) == 4
        start = int(datetime(2026, 8, 3, tzinfo=CN_TZ).timestamp() * 1000)
        middle = int(datetime(2026, 8, 4, tzinfo=CN_TZ).timestamp() * 1000)
        end = int(datetime(2026, 8, 5, tzinfo=CN_TZ).timestamp() * 1000)
        windows = [(request[1]["start_time"], request[1]["end_time"]) for request in requests]
        assert windows == [
            (start, middle),
            (start, middle),
            (middle, end),
            (middle, end),
        ]


@pytest.mark.asyncio
async def test_integrity_repair_retains_running_task_and_releases_on_completion(monkeypatch):
    target = data_integrity
    loop = asyncio.get_running_loop()
    gate = loop.create_future()
    tasks = []
    real_create_task = asyncio.create_task

    def create_task(coro):
        task = real_create_task(coro)
        tasks.append(task)
        return task

    monkeypatch.setattr(target.asyncio if hasattr(target, "asyncio") else asyncio, "create_task", create_task)
    monkeypatch.setattr(loop, "run_in_executor", lambda *_args: gate)
    monkeypatch.setattr(pipeline_jobs, "job_store", SimpleNamespace(create=lambda: ("repair-job", True)))
    state = SimpleNamespace(repo=object(), capabilities=SimpleNamespace(has=lambda _: True))
    assert target.launch_integrity_repair(state, date(2026, 8, 3), "gap") == ("repair-job", True)
    try:
        registry = getattr(target, "_REPAIR_TASKS", set())
        assert tasks[0] in registry
        await asyncio.sleep(0)
        assert tasks[0] in registry and not tasks[0].done()
        gate.set_result(None)
        await tasks[0]
        await asyncio.sleep(0)
        assert tasks[0] not in registry
    finally:
        if not gate.done():
            gate.set_result(None)
        await tasks[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["minute", "extend", "repair", "rebuild"])
async def test_maintenance_task_owned_until_worker_finishes(tmp_path, monkeypatch, kind):
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(kline, "_tasks", set(), raising=False)
    monkeypatch.setattr(collection_status, "collection_context", lambda *_args: None)
    monkeypatch.setattr("app.tickflow.pools.get_pool", lambda _name: ["600000.SH"])
    entered, release = Event(), Event()

    def queued(_job_id, _run):
        entered.set()
        assert release.wait(3)
        return 0 if kind in {"minute", "rebuild"} else {"days": 1}

    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", queued)
    capset = CapabilitySet({
        Cap.KLINE_MINUTE_BATCH: CapabilityLimits(),
        Cap.KLINE_DAILY_BATCH: CapabilityLimits(),
    })
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path), db=Mock(), get_index_symbol_set=lambda: set(),
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo, capabilities=capset)))
    previous = asyncio.all_tasks()
    if kind == "minute":
        result = await kline.trigger_minute_sync(repo, capset, override_days=3)
    elif kind == "extend":
        request.json = AsyncMock(return_value={"value": 1, "unit": "month"})
        result = await kline.extend_history(request)
    elif kind == "repair":
        request.json = AsyncMock(return_value={"start_date": "2026-08-03"})
        result = await kline.repair_daily(request)
    else:
        result = await kline.rebuild_enriched(request)
    tasks = asyncio.all_tasks() - previous
    try:
        assert result["status"] == "started"
        assert len(tasks) == 1
        assert kline._tasks == tasks
        assert await asyncio.to_thread(entered.wait, 2)
        assert all(not task.done() for task in kline._tasks)
        release.set()
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        assert store.get(result["job_id"])["status"] == "succeeded"
        assert not kline._tasks
        assert pipeline_jobs._run_slot_owner is None
    finally:
        release.set()
        await asyncio.gather(*tasks)


def test_storage_nested_partitions_keep_file_count_and_total_size(tmp_path):
    paths = [tmp_path / "root.parquet", tmp_path / "date=2026-08-03" / "part.parquet",
             tmp_path / "date=2026-08-04" / "symbol=600000.SH" / "part.parquet"]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"0" * 1048576)
    assert data._scan_dir_stats(tmp_path) == (3, 3.0)
