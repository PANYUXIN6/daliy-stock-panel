"""指数/ETF走所选日线源，保留各自存储与盘后覆盖证据。"""
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import indices, pipeline
from app.data_providers import custom
from app.services import index_sync, kline_sync
from app.services.daily_coverage import DailyCoverage
from app.tickflow.capabilities import CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 10, 21)


def bars(symbol):
    return pl.DataFrame({
        "symbol": [symbol], "date": [DAY], "open": [10.], "high": [11.],
        "low": [9.], "close": [10.5], "volume": [1000.], "amount": [10500.],
    })


@pytest.fixture
def route(monkeypatch):
    calls = []

    class Provider:
        def get_daily(self, symbols, *, asset_type, **kwargs):
            calls.append((asset_type, symbols))
            return bars(symbols[0])

    provider = Provider()
    monkeypatch.setattr(index_sync.preferences, "get_daily_data_provider", lambda: "custom_test")
    monkeypatch.setattr(custom, "provider_has_dataset", lambda name, dataset: name == "custom_test" and dataset == "daily")
    monkeypatch.setattr(custom, "get_provider", lambda name: provider)
    monkeypatch.setattr(kline_sync, "sync_daily_batch", lambda *_a, **_k: pytest.fail("不得跨源回退"))
    return calls, provider


@pytest.mark.parametrize("asset,symbol", [("index", "000001.SH"), ("etf", "588200.SH")])
def test_custom_source_without_tickflow_permission_preserves_asset_storage(tmp_path, route, asset, symbol):
    calls, _ = route
    store = DataStore(tmp_path)
    repo = KlineRepository(store)
    try:
        func = getattr(index_sync, f"sync_and_persist_{asset}_daily")
        coverage = DailyCoverage(DAY)
        kwargs = {"coverage": coverage} if asset == "index" else {}
        written = func(repo, CapabilitySet({}), symbols_override=[symbol],
                       start_date=datetime(2026, 10, 21), end_date=datetime(2026, 10, 21), **kwargs)
        assert written == 1 and calls == [(asset, [symbol])]
        rows = pl.read_parquet(tmp_path / f"kline_{asset}_daily/date={DAY}/part.parquet")
        assert rows["symbol"].to_list() == [symbol]
        assert rows["close"].to_list() == [10.5]
        assert not list((tmp_path / "kline_daily").glob("**/*.parquet"))
        assert (tmp_path / f"kline_{asset}_enriched/date={DAY}/part.parquet").exists()
        if asset == "index":
            assert coverage.active == {symbol}
    finally:
        store.db.close()


def test_custom_index_prefetch_remains_read_only_and_tracks_coverage(route):
    prepared = index_sync.prepare_index_daily(
        CapabilitySet({}), ["000001.SH"], datetime(2026, 10, 21), datetime(2026, 10, 21),
        coverage_day=DAY,
    )
    assert len(prepared.frames) == 1
    assert prepared.coverage.active == {"000001.SH"}


@pytest.mark.parametrize("behavior", ["unsupported", "empty", "wrong_symbol", "failed"])
def test_unavailable_or_invalid_custom_source_fails_without_tickflow_or_writes(tmp_path, route, monkeypatch, behavior):
    _, provider = route
    if behavior == "unsupported":
        monkeypatch.setattr(custom, "provider_has_dataset", lambda *_: False)
    elif behavior == "failed":
        def fail(*_args, **_kwargs):
            raise RuntimeError("source unavailable")
        monkeypatch.setattr(provider, "get_daily", fail)
    else:
        monkeypatch.setattr(provider, "get_daily", lambda *_a, **_k:
                            pl.DataFrame() if behavior == "empty" else bars("600000.SH"))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), append_index_daily=lambda _: pytest.fail("非法数据不得入库"))
    with pytest.raises((RuntimeError, ValueError)):
        index_sync.sync_and_persist_index_daily(repo, CapabilitySet({}), symbols_override=["000001.SH"])


def test_explicit_index_sync_api_accepts_available_custom_source(route, monkeypatch):
    jobs = []

    async def submit(repo, quotes, run, **kwargs):
        jobs.append(kwargs)
        return {"job_id": "test", "reused": False}

    monkeypatch.setattr(pipeline, "_submit_job", submit)
    app = FastAPI()
    app.state.repo = object()
    app.state.capabilities = CapabilitySet({})
    app.include_router(indices.router)
    response = TestClient(app).post("/api/index/sync_daily")
    assert response.status_code == 200
    assert jobs[0]["request_key"] == "index_daily:365"
    assert jobs[0]["datasets"] == ["daily"]


@pytest.mark.parametrize("asset,symbol", [("index", "000001.SH"), ("etf", "588200.SH")])
def test_partial_stream_failure_never_publishes_a_half_batch(tmp_path, route, monkeypatch, asset, symbol):
    _, provider = route
    def stream(*_args, **_kwargs):
        yield bars(symbol)
        raise RuntimeError("later chunk failed")
    monkeypatch.setattr(provider, "iter_daily", stream, raising=False)
    store = DataStore(tmp_path)
    repo = KlineRepository(store)
    try:
        func = getattr(index_sync, f"sync_and_persist_{asset}_daily")
        with pytest.raises(RuntimeError, match="later chunk failed"):
            func(repo, CapabilitySet({}), symbols_override=[symbol])
        assert not list(tmp_path.glob(f"kline_{asset}_*/**/*.parquet"))
        assert not list(tmp_path.glob(".asset-daily-*"))
    finally:
        store.db.close()
