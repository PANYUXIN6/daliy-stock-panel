from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.services.ai_capacity import limited_ai
from app.services.pipeline_jobs import JobBusyError, JobStore
from app.services.shared_runtime import QuoteMirror, publish_quotes
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

pytestmark = pytest.mark.usefixtures("private_owner")

def test_two_web_readers_install_same_persisted_quote_without_recomputation(tmp_path, monkeypatch):
    writer = KlineRepository(DataStore(tmp_path))
    day = date(2026, 10, 9)
    writer.install_shared_snapshot({
        '_enriched_cache': pl.DataFrame({'symbol': ['000001.SZ'], 'close': [12.5], 'date': [day]}),
        '_live_agg_cache': pl.DataFrame({'symbol': ['000001.SZ'], '_prev_consec_up': [2]}),
        '_index_instruments_cache': pl.DataFrame({'symbol': ['000001.SH'], 'name': ['上证指数']}),
        '_etf_instruments_cache': pl.DataFrame({'symbol': ['510300.SH'], 'name': ['沪深300ETF']}),
        '_instruments_cache': pl.DataFrame({'symbol': ['000001.SZ'], 'name': ['平安银行']}),
    }, {'_enriched_cache_date': day.isoformat()})
    source = SimpleNamespace(
        _repo=writer,
        _app_state=SimpleNamespace(capabilities=CapabilitySet({Cap.INTRADAY: CapabilityLimits()})),
        status=lambda: {'symbol_count': 1, 'interval_s': 30, 'last_fetch_ms': 123},
        get_index_quotes=lambda: pl.DataFrame({'symbol': ['000001.SH'], 'last_price': [3000.0]}),
    )
    publish_quotes(source)
    for _ in range(2):
        reader = KlineRepository(DataStore(tmp_path))
        reader._index_symbol_set_cache = {'obsolete'}
        reader._etf_symbol_set_cache = {'obsolete'}
        reader._name_map_cache = {'obsolete': 'old'}
        monkeypatch.setattr(reader, '_refresh_enriched', lambda: pytest.fail('历史重算'))
        state = SimpleNamespace()
        mirror = QuoteMirror(reader)
        assert mirror.load_snapshot(state)
        frame, received_day = mirror.get_enriched_today()
        assert received_day == day
        assert frame['close'].to_list() == [12.5]
        assert mirror.get_index_quotes()['last_price'].to_list() == [3000.0]
        assert '000001.SH' in reader.get_index_symbol_set()
        assert 'obsolete' not in reader.get_index_symbol_set()
        assert reader.get_etf_symbol_set() == {'510300.SH'}
        assert reader.get_name_map()['000001.SZ'] == '平安银行'
        assert state.capabilities.has(Cap.INTRADAY)
        reader.db.close()
    writer.db.close()


def test_web_job_store_reopen_never_recovers_collectors_running_job(tmp_path):
    owner = JobStore(store_dir=tmp_path)
    job_id, _ = owner.create(timeout_s=30, request_key='minute:000001.SZ')
    owner.start(job_id)
    reader = JobStore(store_dir=tmp_path, recover=False)
    assert reader.get(job_id)['status'] == 'running'
    owner.progress(job_id, 'minute', 50, '正在保存')
    assert owner.get(job_id)['progress'] == 50
    with pytest.raises(JobBusyError):
        owner.create(request_key='financial:metrics')
    assert owner.create(request_key='minute:000001.SZ') == (job_id, False)
    owner.succeed(job_id, {'rows': 1})
    assert reader.get(job_id)['status'] == 'succeeded'


def test_ai_stream_close_and_cancel_release_capacity(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, 'ai_max_concurrent', 1)

    @limited_ai
    async def stream():
        yield 'first'
        await asyncio.Event().wait()

    @limited_ai
    async def once():
        return 'done'

    async def exercise():
        generator = stream()
        assert await anext(generator) == 'first'
        pending = asyncio.create_task(once())
        await asyncio.sleep(0.06)
        assert not pending.done()
        await generator.aclose()
        assert await asyncio.wait_for(pending, 1) == 'done'
        generator = stream()
        await anext(generator)
        blocked = asyncio.create_task(anext(generator))
        await asyncio.sleep(0)
        blocked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await blocked
        assert await asyncio.wait_for(once(), 1) == 'done'

    asyncio.run(exercise())
