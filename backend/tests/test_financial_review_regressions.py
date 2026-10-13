"""已确认财务验收缺陷：稀疏 schema、当次失败证据和 BPS 等待取消。"""
from __future__ import annotations

import asyncio
from threading import Event
from types import SimpleNamespace

import httpx
import polars as pl
import pytest

from app.api import financials, pipeline
from app.data_providers import custom
from app.plugins.fuyao import client as fc
from app.plugins.fuyao import provider as fp
from app.plugins.fuyao.provider import FuyaoProvider
from app.services import (
    collection_status,
    financial_sync,
    pipeline_jobs,
    preferences,
    shared_runtime,
)
from app.services.collection_status import CollectionSyncRequest
from app.tickflow.capabilities import CapabilitySet

_INCOME = {"period_end_ms": 1782748800000, "report_date_ms": 1786723200000,
           "fiscal_year": 2026, "fiscal_period": "Q2", "basic_eps": 2.5}
_SYMBOLS = [f"{600000 + i}.SH" for i in range(101)]


@pytest.mark.parametrize("field", ["roe", "bps"])
def test_metrics_preserves_value_first_present_after_100_rows(field):
    class Client:
        def financial_statements(self, stmt, symbol, **kwargs):
            return [dict(_INCOME)]

        def financial_indicators(self, symbol, report, **kwargs):
            if field == "roe" and symbol == _SYMBOLS[-1]:
                return [{"indicators": [{"index_id": "index_weighted_avg_roe", "value": "12.5"}]}]
            return []

        def valuations_snapshot(self, symbols, **kwargs):
            return [{"thscode": symbol, "pb_mrq": 2.}
                    for symbol in symbols if field == "bps" and symbol == _SYMBOLS[-1]]

        def price_snapshot_batch(self, symbols, **kwargs):
            return [{"thscode": symbol, "last_price": 10.}
                    for symbol in symbols if field == "bps" and symbol == _SYMBOLS[-1]]

    provider = FuyaoProvider()
    provider._client = Client()
    result = provider.get_financials("metrics", _SYMBOLS)

    assert result.height == 101
    assert field in result.columns
    assert result.schema[field] == pl.Float64
    assert result[field].null_count() == 100
    assert result.filter(pl.col("symbol") == _SYMBOLS[-1])[field].item() == (12.5 if field == "roe" else 5.)


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: True)
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "fuyao")
    monkeypatch.setattr(preferences, "set_financial_sync_time", lambda *args: None)
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: None)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline, "job_store", store)
    instruments = tmp_path / "instruments" / "instruments.parquet"
    instruments.parent.mkdir()
    pl.DataFrame({"symbol": ["600519.SH"]}).write_parquet(instruments)
    # 本组验证报表失败与取消；供应商股票范围固定为该用例的本地样本。
    monkeypatch.setattr(FuyaoProvider, "get_financial_universe",
                        lambda self: pl.read_parquet(instruments)["symbol"].to_list())
    scheduler = financial_sync.FinancialScheduler()
    scheduler._data_dir = tmp_path
    scheduler._capset = CapabilitySet()
    scheduler._last_sync = dict.fromkeys(financial_sync.FINANCIAL_TABLES, "2026-01-01T00:00:00+00:00")
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: None)
    return tmp_path, store, scheduler, repo


def _old_tables(data_dir):
    original = {}
    for table in financial_sync.FINANCIAL_TABLES:
        path = data_dir / "financials" / table / "part.parquet"
        path.parent.mkdir(parents=True)
        pl.DataFrame({"symbol": ["600519.SH"], "period_end": ["2026-03-31"], "value": [12.5]}).write_parquet(path)
        original[table] = path.read_bytes()
    return original


def _install(monkeypatch, client):
    provider = FuyaoProvider()
    provider._client = client
    monkeypatch.setattr(custom, "get_provider", lambda name: provider)
    return provider


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["error", "empty"])
async def test_old_financial_history_does_not_turn_empty_or_failed_round_into_success(local, monkeypatch, outcome):
    data_dir, jobs, scheduler, repo = local
    originals = _old_tables(data_dir)

    class Client:
        def financial_statements(self, *args, **kwargs):
            if outcome == "error":
                raise fc.FuyaoError("网络请求失败: isolated test")
            return []

    _install(monkeypatch, Client())
    response = await financials.trigger_financial_sync(repo, CapabilitySet(), scheduler)
    await asyncio.gather(*list(pipeline._tasks))
    job = jobs.get(response["job_id"])

    assert job["status"] == "failed"
    assert "网络请求失败" in job["error"] if outcome == "error" else "未获取" in job["error"]
    assert scheduler.last_sync == dict.fromkeys(financial_sync.FINANCIAL_TABLES, "2026-01-01T00:00:00+00:00")
    assert all((data_dir / "financials" / table / "part.parquet").read_bytes() == old
               for table, old in originals.items())
    assert not collection_status.check_collection_sync(
        CollectionSyncRequest(kind="financial", table="all"), repo, job_store=jobs,
    )["requires_confirmation"]


@pytest.mark.asyncio
async def test_supported_tables_sync_without_claiming_old_unsupported_shares(local, monkeypatch):
    data_dir, jobs, scheduler, repo = local
    originals = _old_tables(data_dir)

    class Client:
        def financial_statements(self, *args, **kwargs):
            return [dict(_INCOME)]

        def financial_indicators(self, *args, **kwargs):
            return []

        def valuations_snapshot(self, *args, **kwargs):
            return []

        def price_snapshot_batch(self, *args, **kwargs):
            return []

    _install(monkeypatch, Client())
    response = await financials.trigger_financial_sync(repo, CapabilitySet(), scheduler)
    await asyncio.gather(*list(pipeline._tasks))
    job = jobs.get(response["job_id"])

    assert job["status"] == "succeeded"
    assert job["result"]["shares"] == 0
    assert scheduler.last_sync["shares"] == "2026-01-01T00:00:00+00:00"
    assert (data_dir / "financials" / "shares" / "part.parquet").read_bytes() == originals["shares"]
    assert all(job["result"][table] > 0 for table in financial_sync.FINANCIAL_TABLES if table != "shares")
    confirmation = collection_status.check_collection_sync(
        CollectionSyncRequest(kind="financial", table="all"), repo, job_store=jobs,
    )
    assert confirmation["requires_confirmation"]
    assert "本次任务未确认有可用结果：历史股本" in confirmation["message"]


@pytest.mark.asyncio
async def test_failed_table_keeps_old_timestamp_while_completed_table_is_saved(local, monkeypatch):
    data_dir, jobs, scheduler, repo = local
    originals = _old_tables(data_dir)

    class Client:
        def financial_statements(self, table, *args, **kwargs):
            if table == "balance_sheet":
                raise fc.FuyaoError("资产负债表取数失败")
            return [dict(_INCOME)]

        def financial_indicators(self, *args, **kwargs):
            return []

        def valuations_snapshot(self, *args, **kwargs):
            return []

        def price_snapshot_batch(self, *args, **kwargs):
            return []

    _install(monkeypatch, Client())
    response = await financials.trigger_financial_sync(repo, CapabilitySet(), scheduler)
    await asyncio.gather(*list(pipeline._tasks))
    job = jobs.get(response["job_id"])

    assert job["status"] == "failed" and "资产负债表取数失败" in job["error"]
    assert scheduler.last_sync["balance_sheet"] == "2026-01-01T00:00:00+00:00"
    assert scheduler.last_sync["metrics"] != "2026-01-01T00:00:00+00:00"
    assert financial_sync.get_financial_df(data_dir, "metrics").height == 2
    assert (data_dir / "financials" / "balance_sheet" / "part.parquet").read_bytes() == originals["balance_sheet"]
    assert not collection_status.check_collection_sync(
        CollectionSyncRequest(kind="financial", table="all"), repo, job_store=jobs,
    )["requires_confirmation"]


@pytest.mark.asyncio
async def test_provider_worker_error_does_not_forge_job_cancellation(local, monkeypatch):
    data_dir, jobs, scheduler, repo = local
    second_started = Event()
    instruments = data_dir / "instruments" / "instruments.parquet"
    pl.DataFrame({"symbol": ["600000.SH", "600001.SH"]}).write_parquet(instruments)

    class Client:
        def financial_statements(self, table, symbol, *, cancel_event=None, **kwargs):
            if symbol == "600000.SH":
                assert second_started.wait(2)
                raise RuntimeError("报表解析失败")
            second_started.set()
            assert cancel_event.wait(2)
            return []

    _install(monkeypatch, Client())
    response = await financials.trigger_financial_sync(repo, CapabilitySet(), scheduler, "metrics")
    await asyncio.gather(*list(pipeline._tasks))
    job = jobs.get(response["job_id"])

    assert job["status"] == "failed" and "报表解析失败" in job["error"]
    assert not pipeline_jobs.get_cancel_event(response["job_id"]).is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["/api/a-share/valuations/snapshot", "/api/a-share/prices/snapshot"])
async def test_cancelling_metrics_at_bps_gate_does_not_send_waiting_request(local, monkeypatch, endpoint):
    data_dir, jobs, scheduler, repo = local
    originals = _old_tables(data_dir)
    waiting, release = Event(), Event()
    wire = []
    gate_calls = 0

    def wait_slot(cancel_event=None):
        nonlocal gate_calls
        gate_calls += 1
        target = 2 if endpoint.endswith("valuations/snapshot") else 3
        if gate_calls == target:
            waiting.set()
            assert release.wait(3)

    def respond(request):
        wire.append(request.url.path)
        rows = [dict(_INCOME)] if request.url.path.endswith("income-statements") else []
        return httpx.Response(200, json={"code": 0, "data": {"item": rows}})

    client = fc.FuyaoClient("test-key")
    client._http.close()
    client._http = httpx.Client(base_url=fc.BASE_URL, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(fc, "_wait_request_slot", wait_slot)
    _install(monkeypatch, client)
    try:
        response = await financials.trigger_financial_sync(repo, CapabilitySet(), scheduler, "metrics")
        assert await asyncio.to_thread(waiting.wait, 2)
        jobs.terminate(response["job_id"], "用户取消")
        release.set()
        await asyncio.gather(*list(pipeline._tasks))
    finally:
        release.set()
        client.close()

    assert endpoint not in wire
    assert jobs.get(response["job_id"])["status"] == "failed"
    assert not scheduler.is_syncing
    assert (data_dir / "financials" / "metrics" / "part.parquet").read_bytes() == originals["metrics"]


_PARTIAL_SYMBOLS = ["600000.SH", "600519.SH"]
_PARTIAL_STAGES = [
    ("income", "statement"),
    ("metrics", "statement"),
    ("metrics", "indicator"),
    ("metrics", "valuation"),
    ("metrics", "price"),
]


class _PartialFailureClient:
    """两股中的一股在指定取数阶段网络失败，其余请求正常返回。"""

    def __init__(self, stage):
        self.stage = stage
        self.calls = []

    def _fail(self, stage, symbol):
        self.calls.append((stage, symbol))
        if stage == self.stage and symbol == _PARTIAL_SYMBOLS[1]:
            raise fc.FuyaoError(f"网络请求失败: {stage}")

    def financial_statements(self, table, symbol, **kwargs):
        self._fail("statement", symbol)
        return [dict(_INCOME)]

    def financial_indicators(self, symbol, report, **kwargs):
        self._fail("indicator", symbol)
        return [{"indicators": [{"index_id": "index_weighted_avg_roe", "value": "12.5"}]}]

    def valuations_snapshot(self, symbols, **kwargs):
        for symbol in symbols:
            self._fail("valuation", symbol)
        return [{"thscode": symbol, "pb_mrq": 2.} for symbol in symbols]

    def price_snapshot_batch(self, symbols, **kwargs):
        for symbol in symbols:
            self._fail("price", symbol)
        return [{"thscode": symbol, "last_price": 10.} for symbol in symbols]


@pytest.mark.parametrize("table,stage", _PARTIAL_STAGES)
def test_partial_financial_network_failure_does_not_return_complete_table(monkeypatch, table, stage):
    # 快照按一股一批隔离，证明前一批已有数据也不能掩盖后一批失败。
    monkeypatch.setattr(fp, "_VALUATION_BATCH", 1)
    client = _PartialFailureClient(stage)
    provider = FuyaoProvider()
    provider._client = client

    with pytest.raises(fc.FuyaoError, match="网络请求失败") as caught:
        provider.get_financials(table, _PARTIAL_SYMBOLS)

    assert _PARTIAL_SYMBOLS[1] in str(caught.value)
    if stage in {"statement", "indicator"}:
        assert "1/2 只取数失败" in str(caught.value)
    assert (stage, _PARTIAL_SYMBOLS[0]) in client.calls
    assert (stage, _PARTIAL_SYMBOLS[1]) in client.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("table,stage", _PARTIAL_STAGES)
async def test_partial_financial_failure_keeps_history_time_and_repeat_evidence(
    local, monkeypatch, table, stage,
):
    data_dir, jobs, scheduler, repo = local
    monkeypatch.setattr(fp, "_VALUATION_BATCH", 1)
    pl.DataFrame({"symbol": _PARTIAL_SYMBOLS}).write_parquet(
        data_dir / "instruments" / "instruments.parquet",
    )
    path = data_dir / "financials" / table / "part.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": _PARTIAL_SYMBOLS, "period_end": ["2026-03-31"] * 2,
                  "value": [12.5, 15.]}).write_parquet(path)
    original = path.read_bytes()
    recorded = []
    published = []
    monkeypatch.setattr(preferences, "set_financial_sync_time", lambda *args: recorded.append(args))
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: published.append(args))
    client = _PartialFailureClient(stage)
    _install(monkeypatch, client)

    response = await financials.trigger_financial_sync(repo, CapabilitySet(), scheduler, table)
    await asyncio.gather(*list(pipeline._tasks))
    job = jobs.get(response["job_id"])

    assert job["status"] == "failed"
    assert "网络请求失败" in job["error"]
    assert _PARTIAL_SYMBOLS[1] in job["error"]
    if stage in {"statement", "indicator"}:
        assert "1/2 只取数失败" in job["error"]
    assert scheduler.last_sync[table] == "2026-01-01T00:00:00+00:00"
    assert not scheduler.is_syncing
    assert not recorded
    assert not published
    assert path.read_bytes() == original
    assert not pipeline_jobs.get_cancel_event(response["job_id"]).is_set()
    assert not collection_status.check_collection_sync(
        CollectionSyncRequest(kind="financial", table=table), repo, job_store=jobs,
    )["requires_confirmation"]
