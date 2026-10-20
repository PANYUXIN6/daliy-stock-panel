"""详情显式补采只写当前标的/日期，共享在途任务与已保存结果。"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from threading import Event
from unittest.mock import Mock

import httpx
import polars as pl
import pytest
from fastapi import FastAPI

from app.api import kline, pipeline
from app.config import settings
from app.market_time import CN_TZ
from app.services import ordinary_minute, pipeline_jobs, preferences, shared_runtime
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

NOW = datetime(2026, 10, 19, 10, 15, tzinfo=CN_TZ)
DAY = date(2026, 10, 19)
SYMBOL = "600000.SH"
BODY = {"symbol": SYMBOL, "date": DAY.isoformat()}


def minute_frame(symbol=SYMBOL, day=DAY, hour=10):
    return pl.DataFrame({
        "symbol": [symbol], "datetime": [datetime(day.year, day.month, day.day, hour)],
        "open": [12.0], "high": [12.0], "low": [12.0], "close": [12.0],
        "volume": [100.0], "amount": [1200.0],
    })


@pytest.fixture
def market(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    monkeypatch.setattr(ordinary_minute, "cn_now", lambda: NOW)
    monkeypatch.setattr(kline, "cn_today", lambda: DAY)
    jobs = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline, "job_store", jobs)
    monkeypatch.setattr(pipeline_jobs, "job_store", jobs)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(shared_runtime, "publish_data_update", Mock())
    repo = KlineRepository(DataStore(tmp_path))
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    app = FastAPI()
    app.include_router(kline.router)
    app.state.repo = repo
    app.state.capabilities = capset
    yield app, repo, capset, jobs
    repo.db.close()
    preferences._invalidate_cache()


async def settle_jobs():
    if pipeline._tasks:
        await asyncio.gather(*list(pipeline._tasks))


@pytest.mark.parametrize("body", [
    {"symbol": "../../bad", "date": "2026-10-19"},
    {"symbol": 600000, "date": "2026-10-19"},
    {"symbol": SYMBOL, "date": "2026-02-30"},
    {"symbol": SYMBOL, "date": "20261019"},
    {"symbol": SYMBOL, "date": "2026-10-20"},
    {"symbol": SYMBOL},
])
async def test_invalid_request_does_not_create_job(market, monkeypatch, body):
    app, _repo, _capset, jobs = market
    fetch = Mock(side_effect=AssertionError("invalid request reached provider"))
    monkeypatch.setattr(ordinary_minute.kline_sync, "sync_minute_batch", fetch)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/kline/minute/fetch", json=body)
    assert response.status_code == 400
    assert jobs.active_id() is None
    fetch.assert_not_called()


async def test_unavailable_minute_capability_is_rejected(market, monkeypatch):
    app, _repo, _capset, jobs = market
    app.state.capabilities = CapabilitySet({})
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "tickflow")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/kline/minute/fetch", json=BODY)
    assert response.status_code == 403
    assert "分钟数据能力不可用" in response.json()["detail"]
    assert jobs.active_id() is None


async def test_concurrent_clients_share_one_fetch_and_late_click_reuses_disk(market, monkeypatch):
    app, repo, _capset, jobs = market
    entered, release = Event(), Event()
    app.state.quote_service = Mock()
    refresh_daily = Mock(side_effect=AssertionError("minute fetch must not rebuild daily cache"))
    monkeypatch.setattr(repo, "refresh_cache", refresh_daily)

    def fetch(symbols, **kwargs):
        entered.set()
        assert release.wait(5)
        return minute_frame()

    provider = Mock(side_effect=fetch)
    monkeypatch.setattr(ordinary_minute.kline_sync, "sync_minute_batch", provider)
    original_write = ordinary_minute.kline_sync._atomic_write_parquet
    writes = Mock(side_effect=original_write)
    monkeypatch.setattr(ordinary_minute.kline_sync, "_atomic_write_parquet", writes)
    clients = [httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") for _ in range(4)]
    try:
        first = await clients[0].post("/api/kline/minute/fetch", json={**BODY, "symbol": " 600000.sh "})
        assert first.status_code == 200
        assert first.json()["reused"] is False
        assert await asyncio.to_thread(entered.wait, 3)
        others = await asyncio.gather(*[
            client.post("/api/kline/minute/fetch", json=BODY) for client in clients[1:]
        ])
        assert all(response.status_code == 200 for response in others)
        job_id = first.json()["job_id"]
        assert all(response.json()["job_id"] == job_id and response.json()["reused"] for response in others)
        busy = await clients[1].post("/api/kline/minute/fetch", json={**BODY, "symbol": "600001.SH"})
        assert busy.status_code == 409
        release.set()
        await settle_jobs()
        assert jobs.get(job_id)["status"] == "succeeded"
        assert jobs.get(job_id)["result"] == {**BODY, "rows": 1, "reused": False}
        assert repo.get_minute(SYMBOL, DAY).height == 1
        provider.assert_called_once()
        writes.assert_called_once()
        refresh_daily.assert_not_called()
        app.state.quote_service.paused.assert_not_called()
        shared_runtime.publish_data_update.assert_called_once_with(repo.store.data_dir, ["minute"])

        late = await clients[1].post("/api/kline/minute/fetch", json=BODY)
        await settle_jobs()
        assert jobs.get(late.json()["job_id"])["result"] == {**BODY, "rows": 1, "reused": True}
        provider.assert_called_once()
        writes.assert_called_once()
        status = await clients[2].get(f"/api/kline/minute/fetch/{job_id}")
        assert status.status_code == 200
        assert status.json()["status"] == "succeeded"
    finally:
        release.set()
        await settle_jobs()
        await asyncio.gather(*(client.aclose() for client in clients))


@pytest.mark.parametrize("asset,symbol", [("stock", SYMBOL), ("etf", "510300.SH"), ("index", "000001.SH")])
def test_historical_fetch_routes_asset_and_filters_excess_supplier_results(market, monkeypatch, asset, symbol):
    _app, repo, capset, _jobs = market
    target = date(2026, 10, 9)
    monkeypatch.setattr(repo, "resolve_asset_type", lambda _symbol: asset)
    provider = Mock(return_value=pl.concat([
        minute_frame(symbol, target), minute_frame(symbol, DAY),
        minute_frame("600001.SH", target), minute_frame(symbol, target, hour=16),
    ]))
    monkeypatch.setattr(ordinary_minute.kline_sync, "sync_minute_batch", provider)
    result = ordinary_minute.fetch_missing_symbol_minutes(repo, capset, symbol, target)
    assert result == {"symbol": symbol, "date": "2026-10-09", "rows": 1, "reused": False}
    kwargs = provider.call_args.kwargs
    assert provider.call_args.args == ([symbol],)
    assert kwargs["asset_type"] == asset
    assert kwargs["start_time"] == datetime(2026, 10, 9, 9, 30, tzinfo=CN_TZ)
    assert kwargs["end_time"] == datetime(2026, 10, 9, 15, tzinfo=CN_TZ)
    assert repo.get_minute(symbol, target, asset_type=asset).height == 1
    assert repo.get_minute("600001.SH", target, asset_type=asset).is_empty()
    assert repo.get_minute(symbol, DAY, asset_type=asset).is_empty()


def test_collection_lock_rechecks_after_another_request_persists(market, monkeypatch):
    _app, repo, capset, _jobs = market
    entered, release = Event(), Event()

    def fetch(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return minute_frame()

    provider = Mock(side_effect=fetch)
    monkeypatch.setattr(ordinary_minute.kline_sync, "sync_minute_batch", provider)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(ordinary_minute.fetch_missing_symbol_minutes, repo, capset, SYMBOL, DAY)
        try:
            assert entered.wait(3)
            second = executor.submit(ordinary_minute.fetch_missing_symbol_minutes, repo, capset, SYMBOL, DAY)
        finally:
            release.set()
        assert first.result(timeout=5)["reused"] is False
        assert second.result(timeout=5)["reused"] is True
    provider.assert_called_once()
    assert provider.call_args.kwargs["end_time"] == NOW


@pytest.mark.parametrize("initial", ["failure", "empty"])
async def test_failed_or_empty_fetch_can_be_retried(market, monkeypatch, initial):
    app, repo, _capset, jobs = market
    first_result = RuntimeError("上游暂时不可用") if initial == "failure" else pl.DataFrame()
    provider = Mock(side_effect=[first_result, minute_frame()])
    monkeypatch.setattr(ordinary_minute.kline_sync, "sync_minute_batch", provider)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        first = await client.post("/api/kline/minute/fetch", json=BODY)
        await settle_jobs()
        first_job = jobs.get(first.json()["job_id"])
        assert first_job["status"] == ("failed" if initial == "failure" else "succeeded")
        assert repo.get_minute(SYMBOL, DAY).is_empty()
        second = await client.post("/api/kline/minute/fetch", json=BODY)
        await settle_jobs()
    assert second.json()["job_id"] != first.json()["job_id"]
    assert jobs.get(second.json()["job_id"])["status"] == "succeeded"
    assert provider.call_count == 2
    assert repo.get_minute(SYMBOL, DAY).height == 1


async def test_status_hides_other_job_types_and_missing_jobs(market):
    app, _repo, _capset, jobs = market
    other_job, _ = jobs.create(request_key="pipeline")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        for job_id in [other_job, "missing"]:
            response = await client.get(f"/api/kline/minute/fetch/{job_id}")
            assert response.status_code == 404
            assert response.json() == {"detail": "分钟采集任务不存在"}
