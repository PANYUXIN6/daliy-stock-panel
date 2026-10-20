"""显式数据任务的共享发布、冲突和取消边界。"""
import asyncio
import json
from datetime import date, datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import financials, kline, pipeline
from app.services import financial_sync, ordinary_minute, pipeline_jobs, preferences, shared_runtime
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    store = pipeline_jobs.JobStore(store_dir=tmp_path / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', store)
    monkeypatch.setattr(pipeline, 'job_store', store)
    monkeypatch.setattr(pipeline_jobs, '_run_slot_owner', None)
    monkeypatch.setattr(pipeline_jobs, '_CANCEL_FLAGS', {})
    return store


async def terminal(store, job_id):
    async with asyncio.timeout(3):
        while store.get(job_id)['status'] in {'pending', 'running'}:
            await asyncio.sleep(.005)
    return store.get(job_id)


@pytest.mark.asyncio
async def test_full_market_minute_publishes_to_existing_reader(tmp_path, monkeypatch, jobs):
    writer = KlineRepository(DataStore(tmp_path))
    reader = KlineRepository(DataStore(tmp_path))
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    monkeypatch.setattr('app.tickflow.pools.get_pool', lambda name: ['600000.SH', '000001.SH'])
    day = date(2026, 10, 9)
    calls = []

    def collect(symbols, repo, capset, **kwargs):
        assert ordinary_minute.MINUTE_COLLECTION_LOCK.locked()
        assert symbols == ['600000.SH']
        frame = pl.DataFrame({'symbol': symbols, 'datetime': [datetime(2026, 10, 9, 10)], 'close': [12.]})
        return kline.kline_sync._write_minute_partition(frame, repo.store.data_dir / 'kline_minute')

    def publish(root, datasets):
        calls.append(datasets)
        shared_runtime.refresh_web_data(SimpleNamespace(repo=reader, quote_service=Mock()), datasets)

    monkeypatch.setattr(kline.kline_sync, 'sync_and_persist_minute', collect)
    monkeypatch.setattr(shared_runtime, 'publish_data_update', publish)
    try:
        assert reader.get_minute('600000.SH', day).is_empty()
        result = await kline.trigger_minute_sync(writer, capset, override_days=3)
        assert (await terminal(jobs, result['job_id']))['status'] == 'succeeded'
        assert calls == [['minute']]
        assert reader.get_minute('600000.SH', day)['close'].to_list() == [12.]
    finally:
        writer.db.close()
        reader.db.close()


@pytest.mark.asyncio
async def test_full_market_minute_rejects_other_active_job(jobs):
    jobs.create(request_key='financial:all')
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    with pytest.raises(HTTPException) as exc:
        await kline.trigger_minute_sync(None, capset)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["days", "source"])
async def test_minute_batch_keeps_submitted_days_and_records_stable_source(tmp_path, monkeypatch, jobs, change):
    writer = KlineRepository(DataStore(tmp_path))
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    settings = {"minute_sync_days": 3}
    provider = ["tickflow"]
    entered, release = Event(), Event()
    original = pipeline_jobs.run_with_capacity
    collected = []
    monkeypatch.setattr(preferences, "load", lambda: settings)
    monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: provider[0])
    monkeypatch.setattr("app.tickflow.pools.get_pool", lambda name: ["600000.SH"])
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: None)

    def queued(job_id, run):
        entered.set()
        assert release.wait(3)
        return original(job_id, run)

    def collect(symbols, repo, capset, **kwargs):
        collected.append(kwargs["days"])
        return 10

    monkeypatch.setattr(pipeline_jobs, "run_with_capacity", queued)
    monkeypatch.setattr(kline.kline_sync, "sync_and_persist_minute", collect)
    try:
        result = await kline.trigger_minute_sync(writer, capset)
        assert await asyncio.to_thread(entered.wait, 2)
        if change == "days":
            settings["minute_sync_days"] = 10
        else:
            provider[0] = "fuyao"
        release.set()
        job = await terminal(jobs, result["job_id"])
        assert job["status"] == "succeeded" and job["request_key"] == "minute_batch:None:False"
        assert collected == [3]
        if change == "days":
            assert job["result"]["collection_context"] == {
                "provider": "tickflow", "asset_type": "stock", "days": 3, "extend_backward": False,
            }
        else:
            assert "collection_context" not in job["result"]
    finally:
        release.set()
        await terminal(jobs, result["job_id"])
        writer.db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('table', ['metrics', 'balance_sheet', 'cash_flow', 'shares'])
async def test_retired_financial_tables_cannot_start_collection(tmp_path, jobs, table):
    run_now = Mock()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: None)
    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    with pytest.raises(HTTPException) as exc:
        await financials.trigger_financial_sync(repo, capset, SimpleNamespace(run_now=run_now), table)
    assert exc.value.status_code == 400
    run_now.assert_not_called()
    assert jobs.active_id() is None


@pytest.mark.asyncio
async def test_all_financial_collection_only_updates_income(tmp_path, monkeypatch, jobs):
    calls = []
    def run_now(table, on_progress, *, cancel_event=None):
        calls.append(table)
        on_progress(1, 1)
        return {table: 20}
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *args: None)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: None)
    result = await financials.trigger_financial_sync(
        repo, CapabilitySet({Cap.FINANCIAL: CapabilityLimits()}), SimpleNamespace(run_now=run_now),
    )
    job = await terminal(jobs, result['job_id'])
    assert job['status'] == 'succeeded'
    assert calls == ['income']


@pytest.mark.asyncio
async def test_scheduler_only_restores_income_and_rejects_legacy_table_entry(tmp_path, monkeypatch):
    from app.services import preferences

    monkeypatch.setattr(preferences, 'get_financial_sync_times', lambda: {
        'income': '2026-10-16T04:00:00Z', 'metrics': '2026-10-16T05:00:00Z',
    })
    scheduler = financial_sync.FinancialScheduler()
    scheduler.start(tmp_path, CapabilitySet({Cap.FINANCIAL: CapabilityLimits()}))
    assert scheduler.last_sync == {'income': '2026-10-16T04:00:00Z'}
    with pytest.raises(ValueError, match='仅支持利润表'):
        scheduler._run_body('metrics')


@pytest.mark.asyncio
async def test_financial_job_reuse_conflict_and_cancel_before_next_table(tmp_path, monkeypatch, jobs):
    entered, release = Event(), Event()
    tables = []

    def run_now(table, on_progress, *, cancel_event=None):
        tables.append(table)
        entered.set()
        assert release.wait(3)
        on_progress(1, 1)
        return {table: 1}

    scheduler = SimpleNamespace(run_now=run_now)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: None)
    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *args: None)
    first = await financials.trigger_financial_sync(repo, capset, scheduler)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        duplicate = await financials.trigger_financial_sync(repo, capset, scheduler)
        assert duplicate['job_id'] == first['job_id'] and duplicate['reused']
        assert financials._active_financial_job() == first['job_id']
        with pytest.raises(HTTPException) as exc:
            await financials.trigger_financial_sync(repo, capset, scheduler, 'income')
        assert exc.value.status_code == 409
        jobs.terminate(first['job_id'], '用户取消')
    finally:
        release.set()
        await asyncio.gather(*list(pipeline._tasks))
    assert tables == ['income']
    assert jobs.get(first['job_id'])['status'] == 'failed'
    assert financials._active_financial_job() is None


@pytest.mark.asyncio
@pytest.mark.parametrize('source,periods', [('fuyao', 20), ('other', 20), ('fuyao', 8)])
async def test_unfinished_financial_job_runs_remaining_tables_without_conversation(
    tmp_path, monkeypatch, jobs, source, periods,
):
    path = tmp_path / 'financials/sync_progress/income.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'source': source, 'history_periods': periods, 'status': 'running'}))
    monkeypatch.setattr(financial_sync, '_financial_batch_policy', lambda: ('fuyao', 20, 20))
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *args: None)
    calls = []
    def run_now(table, on_progress, *, cancel_event=None):
        calls.append(table)
        on_progress(1, 1)
        return {table: 1}
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: None)
    result = await financials.trigger_financial_sync(
        repo, CapabilitySet({Cap.FINANCIAL: CapabilityLimits()}), SimpleNamespace(run_now=run_now), 'unfinished',
    )
    job = await terminal(jobs, result['job_id'])
    assert job['status'] == 'succeeded'
    assert calls == ['income']
    assert not any(name in job['result'] for name in ('metrics', 'balance_sheet', 'cash_flow', 'shares'))


@pytest.mark.asyncio
async def test_unfinished_financial_job_does_not_create_empty_success(tmp_path, monkeypatch, jobs):
    root = tmp_path / 'financials/sync_progress'
    root.mkdir(parents=True)
    for table in ['balance_sheet', 'cash_flow', 'metrics', 'income']:
        (root / f'{table}.json').write_text(json.dumps({
            'source': 'fuyao', 'history_periods': 20, 'status': 'completed',
        }))
    monkeypatch.setattr(financial_sync, '_financial_batch_policy', lambda: ('fuyao', 20, 20))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: None)
    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    run_now = Mock()
    with pytest.raises(HTTPException) as exc:
        await financials.trigger_financial_sync(repo, capset, SimpleNamespace(run_now=run_now), 'unfinished')
    assert exc.value.status_code == 400
    run_now.assert_not_called()
    assert jobs.active_id() is None


@pytest.mark.asyncio
async def test_resumable_financial_job_cools_down_then_continues_without_pausing_quotes(tmp_path, monkeypatch, jobs):
    from app.data_providers.base import ProviderRateLimitError

    calls = []
    def run_now(table, on_progress, *, cancel_event=None):
        calls.append(table)
        if len(calls) == 1:
            raise ProviderRateLimitError('limited')
        on_progress(2, 2)
        return {table: 40}
    monkeypatch.setattr(financial_sync, '_financial_batch_policy', lambda: ('fuyao', 20, 20))
    monkeypatch.setattr(financial_sync, '_RATE_LIMIT_COOLDOWN_S', 0)
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *args: None)
    monkeypatch.setattr(shared_runtime, 'publish_quotes', lambda *args: None)
    quotes = Mock()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: None)
    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    result = await financials.trigger_financial_sync(repo, capset, SimpleNamespace(run_now=run_now), 'income', quotes)
    job = await terminal(jobs, result['job_id'])
    assert job['status'] == 'succeeded' and job['result']['income'] == 40
    assert calls == ['income', 'income']
    quotes.paused.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_partial_financial_write_reaches_two_readers(tmp_path, monkeypatch, jobs, cancel):
    from app.services import financial_sync
    from app.services.events import EventBus

    writer = KlineRepository(DataStore(tmp_path))
    readers = [KlineRepository(DataStore(tmp_path)) for _ in range(2)]
    events = EventBus()
    monkeypatch.setattr(shared_runtime, "runtime_bus", events)
    subscriptions = [events.subscribe() for _ in readers]
    entered, release = Event(), Event()
    frame = pl.DataFrame({'symbol': ['600000.SH'], 'period_end': ['2026-06-30'], 'net_income': [12.5]})

    def run_now(table, on_progress, *, cancel_event=None):
        financial_sync._write_table('income', frame, tmp_path)
        entered.set()
        assert release.wait(3)
        on_progress(0, 1)
        raise RuntimeError('第二张表失败')

    capset = CapabilitySet({Cap.FINANCIAL: CapabilityLimits()})
    try:
        result = await financials.trigger_financial_sync(writer, capset, SimpleNamespace(run_now=run_now), 'income')
        assert await asyncio.to_thread(entered.wait, 2)
        for reader, subscription in zip(readers, subscriptions, strict=True):
            event = subscription.get_nowait()
            assert event['type'] == 'data_updated'
            assert event['data']['datasets'] == ['financials']
            shared_runtime.refresh_web_data(SimpleNamespace(repo=reader, quote_service=Mock()), event['data']['datasets'])
            assert reader.db.execute('SELECT net_income FROM financials_income').fetchall() == [(12.5,)]
        if cancel:
            jobs.terminate(result['job_id'], '用户取消')
        release.set()
        await terminal(jobs, result['job_id'])
        await asyncio.gather(*list(pipeline._tasks))
        assert jobs.get(result['job_id'])['status'] == 'failed'
        assert financial_sync.get_financial_df(tmp_path, 'income')['net_income'].to_list() == [12.5]
        assert financial_sync.get_financial_df(tmp_path, 'metrics').is_empty()
    finally:
        release.set()
        await asyncio.gather(*list(pipeline._tasks))
        writer.db.close()
        for reader in readers:
            reader.db.close()


def test_unified_minute_view_contains_all_asset_directories(tmp_path):
    for asset, symbol in [('stock', '600000.SH'), ('etf', '510300.SH'), ('index', '000001.SH')]:
        directory = 'kline_minute' if asset == 'stock' else f'kline_{asset}_minute'
        frame = pl.DataFrame({
            'symbol': [symbol], 'datetime': [datetime(2026, 10, 9, 10)],
            'open': [12.], 'high': [12.], 'low': [12.], 'close': [12.],
            'volume': [100.], 'amount': [1200.],
        })
        kline.kline_sync._write_minute_partition(frame, tmp_path / directory)
    store = DataStore(tmp_path)
    try:
        assert set(store.db.execute('SELECT symbol, asset_type FROM kline_minute_all').fetchall()) == {
            ('600000.SH', 'stock'), ('510300.SH', 'etf'), ('000001.SH', 'index'),
        }
    finally:
        store.db.close()
