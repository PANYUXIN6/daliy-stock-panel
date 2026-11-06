"""真实 HTTP 适配到分类快照：批量契约、完整性、取消及旧配置迁移。"""
from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

import httpx
import polars as pl
import pytest
from fastapi import FastAPI

from app.api import ext_data as api
from app.market_time import cn_today
from app.plugins.fuyao import client as fc
from app.plugins.fuyao import provider as fp
from app.services import ext_presets, ext_pull, market_mainline, rps_rotation
from app.services.ext_data import ExtConfigStore


@pytest.fixture
def source(monkeypatch):
    state = SimpleNamespace(calls=[], response=None, labels={})

    def handle(request):
        assert request.url.path == fc.CLASSIFICATIONS_PATH
        assert request.headers['X-api-key'] == 'test-key'
        symbols = request.url.params['thscode'].split(',')
        assert 1 <= len(symbols) <= 100
        kind = request.url.params['type']
        assert kind in {'cn_concept', 'industry'}
        state.calls.append((symbols, kind))
        groups = [{'thscode': symbol, 'index_list': [
            {'thscode': f'881{i:03d}.TI', 'name': label, 'type': kind}
            for i, label in enumerate(state.labels.get(symbol, ['银行']))
        ]} for symbol in symbols]
        data = state.response(groups) if state.response else {'item': groups}
        return httpx.Response(200, json={'code': 0, 'data': data})

    original_client = httpx.Client
    monkeypatch.setattr(fc.httpx, 'Client', lambda **kwargs: original_client(
        **kwargs, transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(fp, 'get_api_key', lambda: 'test-key')
    monkeypatch.setattr(fc, '_wait_request_slot', lambda cancel_event=None: None)
    return state


def instruments(root, symbols):
    path = root / 'instruments' / 'instruments.parquet'
    path.parent.mkdir(exist_ok=True)
    pl.DataFrame({'symbol': symbols, 'name': ['示例'] * len(symbols),
                  'type': ['stock'] * len(symbols)}).write_parquet(path)


def test_batch_limit_and_explicit_empty_classification(source):
    symbols = [f'600{i:03d}.SH' for i in range(201)]
    source.labels = {symbols[0]: [], symbols[1]: ['金融科技', '银行', '银行']}
    provider = fp.FuyaoProvider()
    try:
        rows = provider.get_classifications(symbols, 'concept')
    finally:
        provider.close()
    assert [len(batch) for batch, _ in source.calls] == [100, 100, 1]
    assert {kind for _, kind in source.calls} == {'cn_concept'}
    assert len(rows) == 201
    assert rows[0] == {'symbol': symbols[0], 'concepts': []}
    assert rows[1]['concepts'] == ['金融科技', '银行']


@pytest.mark.parametrize('invalid', ['missing', 'duplicate', 'unknown', 'no_list', 'wrong_type', 'bad_label'])
def test_invalid_response_is_not_an_empty_membership(source, invalid):
    def response(groups):
        if invalid == 'missing':
            groups.pop()
        elif invalid == 'duplicate':
            groups.append(groups[0])
        elif invalid == 'unknown':
            groups[0]['thscode'] = '920001.BJ'
        elif invalid == 'no_list':
            groups[0].pop('index_list')
        elif invalid == 'wrong_type':
            groups[0]['index_list'][0]['type'] = 'cn_concept'
        else:
            groups[0]['index_list'][0]['name'] = 'null'
        return {'item': groups}
    source.response = response
    provider = fp.FuyaoProvider()
    try:
        with pytest.raises(fc.FuyaoError):
            provider.get_classifications(['600000.SH'], 'industry')
    finally:
        provider.close()


def test_missing_key_fails_before_fetch(monkeypatch):
    monkeypatch.setattr(fp, 'get_api_key', lambda: '')
    with pytest.raises(fc.FuyaoError, match='未配置'):
        fp.FuyaoProvider().get_classifications(['600000.SH'], 'industry')


def test_cancellation_stops_before_next_batch(source):
    cancel = Event()
    provider = fp.FuyaoProvider()
    def progress(done, total):
        if done >= 100:
            cancel.set()
    try:
        with pytest.raises(CancelledError):
            provider.get_classifications([f'600{i:03d}.SH' for i in range(101)], 'industry',
                                         on_progress=progress, cancel_event=cancel)
    finally:
        provider.close()
    assert len(source.calls) == 1


async def test_preparation_cancellation_waits_for_inflight_and_closes_provider(tmp_path, source, monkeypatch):
    instruments(tmp_path, ['600000.SH'])
    entered, release, finished = Event(), Event(), Event()
    original = fp.FuyaoProvider.close
    def fetch(self, symbols, kind, *, on_progress, cancel_event):
        entered.set()
        assert release.wait(3)
        assert cancel_event.is_set()
        raise CancelledError()
    def close(self):
        original(self)
        finished.set()
    monkeypatch.setattr(fp.FuyaoProvider, 'get_classifications', fetch)
    monkeypatch.setattr(fp.FuyaoProvider, 'close', close)
    task = asyncio.create_task(ext_presets.prepare_preset('ext_hy_ths', tmp_path))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
    assert not (tmp_path / 'ext_data').exists()


async def test_pull_preview_and_full_snapshot_replace_preserve_consumers(tmp_path, source):
    instruments(tmp_path, ['600000.SH', '600001.SH', '600002.SH'])
    await ext_presets.ensure_builtin_presets(tmp_path)
    source.labels = {'600000.SH': ['计算机-软件开发-应用软件'], '600001.SH': ['旧分类'],
                     '600002.SH': ['将退市分类']}
    await ext_presets.fetch_preset('ext_hy_ths', tmp_path)
    path = tmp_path / 'ext_data/ext_hy_ths/part.parquet'
    before = path.read_bytes()
    instruments(tmp_path, ['600000.SH', '600001.SH'])
    source.labels = {'600000.SH': ['软件开发', 'IT服务'], '600001.SH': []}
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(api.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
        response = await client.post('/api/ext-data/ext_hy_ths/pull/test')
    assert response.status_code == 200
    assert response.json()['total_rows'] == 2
    assert response.json()['preview'][0]['所属同花顺行业'] == 'IT服务;软件开发'
    assert path.read_bytes() == before  # 预览不改生产快照

    config = ExtConfigStore(tmp_path).get('ext_hy_ths')
    assert await ext_pull.fetch_and_ingest(config, tmp_path) == (2, cn_today().isoformat())
    rows = pl.read_parquet(path).sort('symbol').to_dicts()
    assert [row['symbol'] for row in rows] == ['600000.SH', '600001.SH']
    assert rows[0]['code'] == '600000' and rows[0]['股票简称'] == '示例'
    assert rows[0]['所属同花顺行业'] == 'IT服务;软件开发'
    assert rows[1]['所属同花顺行业'] == ''
    rps_rotation._map_cache.clear()
    try:
        mapping, count = rps_rotation._load_concept_map_df(app.state.repo, 'industry')
        assert count == 2
        assert set(mapping['industry']) == {'IT服务', '软件开发'}
        assert set(mapping['_sym_up']) <= {'600000.SH', '600000'}
    finally:
        rps_rotation._map_cache.clear()
    with pytest.raises(ValueError, match='不能回补历史'):
        await ext_pull.fetch_and_ingest(config, tmp_path, target_date=cn_today() - timedelta(days=1))


async def test_partial_batch_failure_preserves_old_snapshot(tmp_path, source):
    instruments(tmp_path, ['600000.SH'])
    await ext_presets.fetch_preset('ext_gn_ths', tmp_path)
    path = tmp_path / 'ext_data/ext_gn_ths/part.parquet'
    before = path.read_bytes()
    instruments(tmp_path, [f'600{i:03d}.SH' for i in range(101)])
    source.response = lambda groups: {'item': [] if len(groups) == 1 else groups}
    with pytest.raises(fc.FuyaoError, match='缺少 1 只股票'):
        await ext_presets.fetch_preset('ext_gn_ths', tmp_path)
    assert path.read_bytes() == before


async def test_startup_migrates_only_retired_url_preserving_user_settings(tmp_path):
    store = ExtConfigStore(tmp_path)
    old = ext_presets._concept_preset()
    old.pull.url = ext_presets._RETIRED_URLS[old.id]
    old.pull.enabled = True
    old.pull.schedule_minutes = 180
    old.label = '我的概念'
    store.upsert(old)
    custom = ext_presets._industry_preset()
    custom.pull.url = 'https://example.com/my-classifications'
    store.upsert(custom)
    await ext_presets.ensure_builtin_presets(tmp_path)
    migrated = store.get(old.id)
    assert migrated.pull.url == ext_presets._concept_preset().pull.url
    assert migrated.pull.enabled and migrated.pull.schedule_minutes == 180
    assert migrated.label == '我的概念'
    assert store.get(custom.id).to_dict() == custom.to_dict()
    assert not ext_presets.is_builtin_classification_pull(custom)


async def test_instrument_scope_and_new_industry_observation(tmp_path, source):
    instruments(tmp_path, ['600000.SH', '600001.SH', '600002.SH', '510300.SH'])
    path = tmp_path / 'instruments/instruments.parquet'
    pl.read_parquet(path).with_columns(
        pl.Series('is_current', [True, False, True, True]),
        pl.Series('type', ['stock', 'stock', 'stock', 'etf']),
        pl.Series('listing_date', [None, None, str(cn_today() + timedelta(days=1)), None]),
    ).write_parquet(path)
    prepared = await ext_presets.prepare_preset('ext_hy_ths', tmp_path)
    assert prepared.observation['symbols'] == ['600000.SH']
    assert prepared.observation['memberships'] == [
        {'symbol': '600000.SH', 'sector_id': '银行', 'sector_name': '银行', 'source': 'industry'}]
    assert prepared.observation['membership_semantics'] == 'fuyao-flat-v1'
    assert prepared.observation['source_effective_date'] is None
    assert prepared.observation['source_version'] is None
    assert prepared.rows[0]['所属同花顺行业'] == '银行'
    assert not (tmp_path / 'ext_data').exists()


async def test_mainline_reads_flat_classification_and_preserves_saved_history(tmp_path, source):
    symbols = ['600000.SH', '600001.SH', '600002.SH']
    instruments(tmp_path, symbols)
    today = cn_today()
    previous = today - timedelta(days=1)
    for day in (previous, today):
        path = tmp_path / 'kline_daily_enriched' / f'date={day}' / 'part.parquet'
        path.parent.mkdir(parents=True)
        pl.DataFrame({'symbol': symbols, 'date': [day] * 3,
                      'consecutive_limit_ups': [3, 2, 1], 'amount': [3e8, 2e8, 1e8]}).write_parquet(path)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    options = {'kind': 'industry', 'exclude_st': False,
               'filter_cfg': {'min_members': 3, 'max_members': 3, 'blacklist': []}}
    source.labels = {symbol: ['计算机-软件开发-应用软件'] for symbol in symbols}
    await ext_presets.fetch_preset('ext_hy_ths', tmp_path)
    rps_rotation._map_cache.clear()
    try:
        old = market_mainline.compute_mainline_range(repo, tmp_path, previous, previous, **options)
        assert old['member'].to_list() == ['计算机-软件开发']
        market_mainline.upsert_mainline_history(tmp_path, old)
        source.labels = {symbol: ['软件开发'] for symbol in symbols}
        await ext_presets.fetch_preset('ext_hy_ths', tmp_path)
        rps_rotation._map_cache.clear()
        current = market_mainline.compute_mainline_range(repo, tmp_path, today, today, **options)
        assert current['member'].to_list() == ['软件开发']
        assert current['limit_up_count'].to_list() == [3]
        assert current['boards_sum'].to_list() == [6]
        market_mainline.upsert_mainline_history(tmp_path, current)
        history = market_mainline.load_mainline_history(tmp_path, 'industry')
        assert history.filter(pl.col('date') == previous).equals(old)
    finally:
        rps_rotation._map_cache.clear()
