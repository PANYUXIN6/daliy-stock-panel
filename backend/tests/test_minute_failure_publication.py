"""Minute task failures publish completed writes and deletion failures stay visible."""
import asyncio
import errno
import os
from datetime import date, datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import ext_data, kline
from app.services import pipeline_jobs, shared_runtime
from app.services.events import EventBus
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository


def _frame(symbol="600000.SH", day=9):
    return pl.DataFrame({
        "symbol": [symbol], "datetime": [datetime(2026, 10, day, 10)],
        "open": [12.0], "high": [12.0], "low": [12.0], "close": [12.0],
        "volume": [100.0], "amount": [1200.0],
    })


@pytest.mark.parametrize("mode", ["cancel", "failure", "success", "no_write_failure"])
async def test_minute_task_publishes_only_completed_writes(tmp_path, monkeypatch, mode):
    writer = KlineRepository(DataStore(tmp_path))
    reader = KlineRepository(DataStore(tmp_path))
    peer = SimpleNamespace(repo=reader, quote_service=Mock())
    jobs = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", jobs)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    bus = EventBus()
    monkeypatch.setattr(shared_runtime, "runtime_bus", bus)
    events = bus.subscribe()
    monkeypatch.setattr("app.tickflow.pools.get_pool", lambda _pool: ["600000.SH"])
    monkeypatch.setattr(ext_data, "_store", lambda _request: SimpleNamespace(get=lambda _id: object()))
    monkeypatch.setattr(ext_data, "_dimension_intraday_compute", lambda *_args: {
        "minute_rows": reader.get_minute("600000.SH", date(2026, 10, 9)).height,
    })
    ext_data.invalidate_dimension_intraday_cache()
    request = SimpleNamespace(app=SimpleNamespace(state=peer))
    def cached_rows():
        return ext_data.dimension_intraday(request, "sample", "industry", "test", "2026-10-09")["minute_rows"]
    entered, release = Event(), Event()
    def collect(_symbols, _repo, _capset, **kwargs):
        if mode == "no_write_failure":
            raise RuntimeError("provider failed before writing")
        count = kline.kline_sync._write_minute_partition(_frame(), tmp_path / "kline_minute")
        if kwargs.get("on_persist"):
            kwargs["on_persist"](count)
        entered.set()
        assert release.wait(5)
        kwargs["on_chunk_done"](1, 2, "first")
        if mode == "failure":
            raise RuntimeError("provider failed after writing")
        return count
    monkeypatch.setattr(kline.kline_sync, "sync_and_persist_minute", collect)
    try:
        assert cached_rows() == 0
        before = set(asyncio.all_tasks())
        result = await kline.trigger_minute_sync(
            writer, CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()}), override_days=3,
        )
        task_set = set(asyncio.all_tasks()) - before
        if mode != "no_write_failure":
            assert await asyncio.to_thread(entered.wait, 3)
            if mode == "cancel":
                jobs.terminate(result["job_id"], "user cancel")
            release.set()
        await asyncio.gather(*task_set)
        published = []
        while not events.empty():
            event = events.get_nowait()
            if event["type"] == "data_updated":
                published.append(event)
                shared_runtime.refresh_web_data(peer, event["data"]["datasets"])
        assert len(published) == int(mode != "no_write_failure")
        assert cached_rows() == int(mode != "no_write_failure")
        assert jobs.get(result["job_id"])["status"] == ("succeeded" if mode == "success" else "failed")
    finally:
        release.set()
        writer.db.close()
        reader.db.close()
        ext_data.invalidate_dimension_intraday_cache()


async def test_clear_minute_delete_failure_reports_error_and_preserves_other_assets(tmp_path, monkeypatch):
    kline.kline_sync._write_minute_partition(_frame(), tmp_path / "kline_minute")
    kline.kline_sync._write_minute_partition(_frame("510300.SH"), tmp_path / "kline_etf_minute")
    repo = KlineRepository(DataStore(tmp_path))
    class Request:
        method = "POST"
        app = SimpleNamespace(state=SimpleNamespace(repo=repo))
        async def json(self):
            return {"confirm": True}
    original_unlink = os.unlink
    def deny_file(path, *args, **kwargs):
        if str(path) == "part.parquet":
            raise PermissionError(errno.EACCES, "fixture file locked", path)
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(os, "unlink", deny_file)
    try:
        with pytest.raises(HTTPException) as error:
            await kline.clear_minute(Request())
        assert error.value.status_code == 500
        assert "清空分钟" in error.value.detail
        assert "fixture file locked" in error.value.detail
        assert repo.get_minute("600000.SH", date(2026, 10, 9)).height == 1
        assert (tmp_path / "kline_etf_minute" / "date=2026-10-09" / "part.parquet").exists()
    finally:
        repo.db.close()


def test_partition_callback_keeps_first_commit_visible_when_later_partition_fails(tmp_path, monkeypatch):
    original_write = kline.kline_sync._atomic_write_parquet
    def write(df, path):
        if "date=2026-10-12" in str(path):
            raise OSError("second partition unavailable")
        original_write(df, path)
    monkeypatch.setattr(kline.kline_sync, "_atomic_write_parquet", write)
    commits = []
    with pytest.raises(OSError, match="second partition unavailable"):
        kline.kline_sync._write_minute_partition(
            pl.concat([_frame(day=9), _frame(day=12)]), tmp_path / "kline_minute",
            on_persist=commits.append,
        )
    assert commits == [1]
    assert pl.read_parquet(tmp_path / "kline_minute" / "date=2026-10-09" / "part.parquet").height == 1
