"""板块分类通过单个后台任务完成，部分写入也通知既有消费者。"""
import asyncio
from contextlib import nullcontext
from datetime import date
from threading import Event
from types import SimpleNamespace

import httpx
import polars as pl
import pytest
from fastapi import FastAPI

from app.api import data, ext_data, pipeline
from app.services import collector_proxy, ext_presets, pipeline_jobs, preferences, shared_runtime
from app.services.events import EventBus
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
async def classification_app(tmp_path, monkeypatch):
    writer = KlineRepository(DataStore(tmp_path))
    readers = [KlineRepository(DataStore(tmp_path, read_only=True)) for _ in range(2)]
    app = FastAPI()
    app.include_router(ext_data.router)
    app.include_router(pipeline.router)
    app.include_router(data.router)
    app.state.repo = writer
    source = SimpleNamespace(_repo=writer, status=lambda: {}, get_index_quotes=lambda: pl.DataFrame(), paused=nullcontext)
    app.state.quote_service = source
    jobs = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", jobs)
    monkeypatch.setattr(pipeline, "job_store", jobs)
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(preferences, "load", lambda: {})
    data.invalidate_storage_cache()
    events = EventBus()
    monkeypatch.setattr(shared_runtime, "runtime_bus", events)
    subscriptions = [events.subscribe() for _ in readers]
    refreshes = []

    def refresh():
        refreshes.append(True)
        writer.install_shared_snapshot({
            "_enriched_cache": pl.DataFrame({"symbol": ["600000.SH"], "close": [12.5], "date": [date(2026, 10, 9)]}),
        }, {"_enriched_cache_date": "2026-10-09"})

    monkeypatch.setattr(writer, "refresh_cache", refresh)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
        try:
            yield SimpleNamespace(client=client, jobs=jobs, writer=writer, readers=readers,
                                  subscriptions=subscriptions, refreshes=refreshes, root=tmp_path)
        finally:
            await asyncio.gather(*list(pipeline._tasks))
            for subscription in subscriptions:
                events.unsubscribe(subscription)
            writer.db.close()
            for reader in readers:
                reader.db.close()
            ext_data.invalidate_dimension_intraday_cache()
            data.invalidate_storage_cache()


async def consume_updates(state, expected_tables):
    for reader, subscription in zip(state.readers, state.subscriptions, strict=True):
        event = subscription.get_nowait()
        assert event["type"] == "data_updated"
        assert event["data"]["datasets"] == ["enriched", "ext_data"]
        shared_runtime.refresh_web_data(SimpleNamespace(repo=reader, quote_service=shared_runtime.QuoteMirror(reader)), event["data"]["datasets"])
        app = FastAPI()
        app.state.repo = reader
        app.include_router(ext_data.router)
        for table, label in expected_tables.items():
            # collector 刷新动态视图, web 现有查询通过本地文件读取分类。
            assert state.writer.db.execute(f'SELECT "{label}" FROM ext_{table}').fetchone()[0] in {"机器人;人工智能", "制造-设备"}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                rows = (await client.get(f"/api/ext-data/{table}/rows")).json()["rows"]
            assert rows[0][label] in {"机器人;人工智能", "制造-设备"}
        assert subscription.empty()


async def test_classifications_action_preserves_both_schemas_and_publishes_once(classification_app, monkeypatch):
    state = classification_app
    calls = []

    async def fetch(url):
        calls.append(url)
        if "concepts" in url:
            return [{"symbol": "600000.SH", "name": "示例", "concepts": ["机器人", "人工智能"]}]
        return [{"symbol": "600000.SH", "name": "示例", "industries": ["制造", "设备"]}]

    monkeypatch.setattr(ext_presets, "_fetch_json", fetch)
    assert collector_proxy.collector_owns("POST", "/api/ext-data/presets/sync")
    response = await state.client.post("/api/ext-data/presets/sync")
    assert response.status_code == 200
    assert response.json()["reused"] is False
    await asyncio.gather(*list(pipeline._tasks))
    job = state.jobs.get(response.json()["job_id"])
    assert job["status"] == "succeeded" and job["request_key"] == "classifications"
    assert job["result"] == {"ext_gn_ths": 1, "ext_hy_ths": 1}
    assert len(calls) == 2 and len(state.refreshes) == 1
    await consume_updates(state, {"ext_gn_ths": "所属概念", "ext_hy_ths": "所属同花顺行业"})


async def test_second_classification_failure_keeps_first_table_readable(classification_app, monkeypatch):
    state = classification_app

    async def fetch(url):
        if "industries" in url:
            raise RuntimeError("行业接口失败")
        return [{"symbol": "600000.SH", "name": "示例", "concepts": ["机器人", "人工智能"]}]

    monkeypatch.setattr(ext_presets, "_fetch_json", fetch)
    response = await state.client.post("/api/ext-data/presets/sync")
    await asyncio.gather(*list(pipeline._tasks))
    job = state.jobs.get(response.json()["job_id"])
    assert job["status"] == "failed" and "行业接口失败" in job["error"]
    assert not (state.root / "ext_data/ext_hy_ths/part.parquet").exists()
    assert len(state.refreshes) == 1
    await consume_updates(state, {"ext_gn_ths": "所属概念"})


async def test_classification_cancel_preserves_written_table_and_reuses_shared_job(classification_app, monkeypatch):
    state = classification_app
    written, release = Event(), Event()
    original = ext_presets.fetch_preset
    calls = []

    async def fetch(url):
        return [{"symbol": "600000.SH", "name": "示例", "concepts": ["机器人", "人工智能"]}]

    async def held_fetch(config_id, root):
        calls.append(config_id)
        rows = await original(config_id, root)
        written.set()
        assert release.wait(3)
        return rows

    monkeypatch.setattr(ext_presets, "_fetch_json", fetch)
    monkeypatch.setattr(ext_presets, "fetch_preset", held_fetch)
    before = (await state.client.get("/api/data/status")).json()["storage"]["ext_data_files"]
    assert before == 0
    response = await state.client.post("/api/ext-data/presets/sync")
    job_id = response.json()["job_id"]
    try:
        assert await asyncio.to_thread(written.wait, 2)
        duplicate = await state.client.post("/api/ext-data/presets/sync")
        assert duplicate.json() == {"job_id": job_id, "reused": True}
        with pytest.raises(pipeline_jobs.JobBusyError):
            state.jobs.create(request_key="financial:all")
        assert (await state.client.post(f"/api/pipeline/jobs/{job_id}/cancel")).status_code == 200
    finally:
        release.set()
    await asyncio.gather(*list(pipeline._tasks))
    assert state.jobs.get(job_id)["status"] == "failed"
    assert calls == ["ext_gn_ths"] and len(state.refreshes) == 1
    after = (await state.client.get("/api/data/status")).json()["storage"]["ext_data_files"]
    assert after > before
    await consume_updates(state, {"ext_gn_ths": "所属概念"})


async def test_classification_action_reports_busy_and_no_write_does_not_publish(classification_app, monkeypatch):
    state = classification_app
    busy_id, _ = state.jobs.create(request_key="financial:all")
    assert (await state.client.post("/api/ext-data/presets/sync")).status_code == 409
    state.jobs.fail(busy_id, "测试完成")

    async def fail(url):
        raise RuntimeError("概念接口失败")

    monkeypatch.setattr(ext_presets, "_fetch_json", fail)
    response = await state.client.post("/api/ext-data/presets/sync")
    await asyncio.gather(*list(pipeline._tasks))
    assert state.jobs.get(response.json()["job_id"])["status"] == "failed"
    assert len(state.refreshes) == 1
    assert all(subscription.empty() for subscription in state.subscriptions)
