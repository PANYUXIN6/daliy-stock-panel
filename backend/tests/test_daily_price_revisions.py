"""Stock-day corrections between formal receipts, through the warehouse write paths."""
import json
from datetime import date, datetime
from unittest.mock import patch

import polars as pl
import pytest
from evening_receipt_fixture import current_receipt

from app import market_time
from app.services.daily_coverage import DailyCoverageError
from app.services.evening_report_price_facts import accept_price_facts, add_accepted_price_times
from app.services.evening_report_store import EveningReportStore
from app.tickflow.repository import DataStore, KlineRepository

DAY = '2026-10-28'
A, B = '000001.SZ', '000002.SZ'


@pytest.fixture(autouse=True)
def _acceptance_day(monkeypatch):
    monkeypatch.setattr(market_time, 'cn_now', lambda: datetime.fromisoformat(f'{DAY}T16:00:00+08:00'))


def bars(volume=100.):
    return pl.DataFrame({'symbol': [A, B], 'date': [date.fromisoformat(DAY)] * 2,
                         'open': [10., 9.], 'high': [12., 10.], 'low': [9., 8.], 'close': [11., 9.],
                         'amount': [12000., 6000.], 'volume': [volume, 50.], 'quote_ts': [None, None]})


def receive(store, previous, hour, symbols=(A, B), *, unknown=None):
    root = store.root.parent
    references = {}
    for p in sorted((root / 'kline_daily').glob('date=*/*.parquet')):
        state = p.stat()
        references.setdefault(p.parent.name[5:], []).append([str(p.relative_to(root)), state.st_size, state.st_mtime_ns])
    at = f'{DAY}T{hour}:00:00+08:00'
    first = dict.fromkeys(symbols, at) | previous.get('wave_first_acceptances', {})
    clocks, changes, unclear = accept_price_facts(root, store.root, references, previous, previous.get('partitions', {}),
                                                 set(symbols), first, at)
    assert unclear == (unknown or {})
    receipt = current_receipt(DAY, at, active_symbols=list(symbols), partitions=references, wave_first_acceptances=first, **clocks)
    store.save_readiness(DAY, receipt)
    return receipt, changes


def test_sparse_receipts_follow_stock_rewrites_and_ignore_reverted_raw_changes(tmp_path):
    store = EveningReportStore(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    price = bars()
    repo.append_daily(price)
    first, diff = receive(store, {}, 16)
    assert diff == {} and first['wave_price_clocks']['rows'] == {}
    repo.append_daily(bars(105.))
    repo.flush_live_daily(bars())
    reverted, diff = receive(store, first, 17)
    assert diff == {}
    repo.flush_live_daily(bars(106.))
    repo.replace_daily_rows('stock', date.fromisoformat(DAY), bars(120.).filter(pl.col('symbol') == A),
                            symbols=[A], backup_dir=tmp_path / 'backup')
    receipt, diff = receive(store, reverted, 18)
    assert diff == {A: {DAY: {'volume': 100., 'known_at': first['cutoff'], 'halted': False, 'wave_formal': True}}}
    clocks = add_accepted_price_times(price, receipt)
    assert dict(clocks.select('symbol', 'known_at').iter_rows()) == {A: receipt['cutoff'], B: first['cutoff']}
    links = list((store.root / 'price_writes').glob(f'{DAY}_*.json'))
    assert len(links) == 4
    for p in links:
        assert set(json.loads(p.read_text())['rows'][A]) == {'volume'}


@pytest.mark.parametrize('gap', ['missing', 'snapshot', 'volume'])
def test_next_day_warehouse_write_only_tracks_incomplete_bars(tmp_path, monkeypatch, gap):
    store = EveningReportStore(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    initial = bars()
    if gap == 'missing':
        initial = initial.filter(pl.col('symbol') == A)
    elif gap == 'snapshot':
        initial = initial.with_columns(pl.when(pl.col('symbol') == B).then(1).alias('quote_ts'))
    else:
        initial = initial.with_columns(pl.when(pl.col('symbol') == A).then('volume').alias('volume'))
    repo.append_daily(initial)
    receive(store, {}, 16)
    tomorrow = '2026-10-29'
    monkeypatch.setattr(market_time, 'cn_now', lambda: datetime.fromisoformat(f'{tomorrow}T16:00:00+08:00'))
    repo.append_daily(bars(120.))
    link, = (store.root / 'price_writes').glob(f'{DAY}_*.json')
    assert set(json.loads(link.read_text())['rows']) == {B}
    part = tmp_path / 'kline_daily' / f'date={DAY}' / 'part.parquet'
    assert pl.read_parquet(part).select('symbol', 'volume').rows() == [(A, 120.), (B, 50.)]


def test_published_partition_stays_closed_when_the_next_day_is_accepted(tmp_path, monkeypatch):
    store = EveningReportStore(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(bars())
    receive(store, {}, 16)
    store.save(DAY, {'trade_date': DAY, 'audit': {}}, {})
    tomorrow = '2026-10-29'
    monkeypatch.setattr(market_time, 'cn_now', lambda: datetime.fromisoformat(f'{tomorrow}T16:00:00+08:00'))
    store.save_readiness(tomorrow, current_receipt(tomorrow, f'{tomorrow}T16:00:00+08:00'))
    repo.append_daily(bars(125.))
    assert not list((store.root / 'price_writes').glob(f'{DAY}_*.json'))
    part = tmp_path / 'kline_daily' / f'date={DAY}' / 'part.parquet'
    assert pl.read_parquet(part).filter(pl.col('symbol') == A)['volume'].item() == 125.


@pytest.mark.parametrize('corrected', [False, True], ids=['only-empty-groups', 'empty-alongside-real-correction'])
def test_empty_stock_clock_groups_preserve_defaults_and_actual_corrections(tmp_path, corrected):
    store = EveningReportStore(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(bars())
    first, _ = receive(store, {}, 16)
    prior = first
    if corrected:
        repo.append_daily(bars(105.))
        prior, _ = receive(store, first, 17)
    native_clocks = prior['wave_price_clocks']
    other_day = '2026-10-27'
    clocks = {**native_clocks, 'rows': {DAY: {}, other_day: {}, **native_clocks['rows']},
              'pending': {other_day: {}}}
    historical = {**prior, 'wave_price_clocks': clocks}
    expected = {A: prior['cutoff'], B: first['cutoff']}
    prices = add_accepted_price_times(bars(), historical)
    assert dict(prices.select('symbol', 'known_at').iter_rows()) == expected
    retry, changes = receive(store, historical, 18)
    assert retry['wave_price_clocks'] == native_clocks
    assert changes == {}


def test_factor_revision_uses_committed_rows_and_only_dates_between_eligible_prices(tmp_path):
    from app.services.evening_report_inputs import _unverified_market_days
    from app.services.kline_sync import _merge_adj_factor_store
    from app.tickflow.daily_revisions import factor_changes, track_receipt

    store = EveningReportStore(tmp_path)
    out = tmp_path / 'adj_factor' / 'all.parquet'
    first_day = '2026-10-21'
    effective = '2026-10-23'
    events = pl.DataFrame({'symbol': [A, B], 'trade_date': [date.fromisoformat(effective)] * 2, 'ex_factor': [1., 1.]})

    def ref():
        stat = out.stat()
        return [str(out.relative_to(tmp_path)), stat.st_size, stat.st_mtime_ns]

    _merge_adj_factor_store(out, events)
    initial = ref()
    track_receipt(store.root, DAY)
    raised = events.with_columns(pl.lit(1.1).alias('ex_factor'))
    with patch('app.services.kline_sync.replace_with_retry', side_effect=OSError('interrupted')), pytest.raises(OSError):
        _merge_adj_factor_store(out, raised)
    _merge_adj_factor_store(out, pl.concat([raised, events]))
    assert factor_changes(store.root, initial, ref()) == [{}]
    _merge_adj_factor_store(out, raised)
    prices = {first_day: [[f'kline_daily/date={first_day}/part.parquet', 10, 1]]}
    old = {'date': DAY, 'versions': {'files': [*prices[first_day], initial]}, 'wave_factor_facts': {},
           'wave_price_clocks': {'pending': {}},
           'metadata': [{'symbol': A, 'listing_date': effective}, {'symbol': B, 'listing_date': first_day}],
           'wave_st_reentry': {B: effective}, 'wave_first_acceptances': dict.fromkeys([A, B], DAY)}
    assert _unverified_market_days(store.root, old, prices, ref()) == {}
    old['wave_st_reentry'] = {}
    assert _unverified_market_days(store.root, old, prices, ref()) == {B: effective}
    prior = ref()
    with patch('app.tickflow.daily_revisions.record_factor_rewrite', side_effect=OSError('disk full')):
        assert _merge_adj_factor_store(out, events) == (0, [A, B])
    assert pl.read_parquet(out)['ex_factor'].to_list() == [1., 1.]
    assert factor_changes(store.root, prior, ref()) is None
    assert len(list((store.root / 'price_writes').glob('factor_*.json'))) == 3
    store.prune(date(2027, 12, 1))
    assert not list((store.root / 'price_writes').glob('factor_*.json'))


def test_staged_revision_before_failed_rename_cannot_become_an_accepted_fact(tmp_path):
    store = EveningReportStore(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(bars())
    first, _ = receive(store, {}, 16)
    with patch('app.tickflow.repository.replace_with_retry', side_effect=OSError('interrupted')), pytest.raises(OSError):
        repo.append_daily(bars(110.))
    repo.append_daily(bars(120.))
    _, changes = receive(store, first, 17)
    assert changes[A][DAY]['volume'] == 100.
    assert len(list((store.root / 'price_writes').glob(f'{DAY}_*.json'))) == 2
    store.prune(date(2027, 12, 1))
    assert not list((store.root / 'price_writes').glob(f'{DAY}_*.json'))


def test_partial_acceptance_defers_stock_corrections_until_next_coverage(tmp_path):
    store = EveningReportStore(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(bars())
    first, _ = receive(store, {}, 16)
    repo.append_daily(bars(120.))
    partial, changes = receive(store, first, 17, symbols=(B,))
    assert not changes
    prior = {'volume': 100., 'halted': False, 'wave_formal': True, 'known_at': first['cutoff']}
    assert partial['wave_price_clocks']['pending'] == {DAY: {A: prior}}
    full, changes = receive(store, partial, 18)
    assert changes == {A: {DAY: prior}}
    assert not full['wave_price_clocks']['pending']


def test_receiving_an_older_date_leaves_newer_accepted_raw_partitions_tracked(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    store = EveningReportStore(tmp_path)
    repo.append_daily(bars())
    first, _ = receive(store, {}, 16)
    store.save_readiness('2026-10-27', current_receipt('2026-10-27', '2026-10-27T17:00:00+08:00'))
    repo.append_daily(bars(120.))
    _, changes = receive(store, first, 17)
    assert changes[A][DAY]['volume'] == 100.


def test_unreadable_current_prices_still_block_acceptance_after_a_successful_receipt(tmp_path):
    store = EveningReportStore(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(bars())
    receipt, _ = receive(store, {}, 16)
    part, = (tmp_path / 'kline_daily').glob('date=*/*.parquet')
    part.write_bytes(b'truncated current input')
    with pytest.raises(pl.exceptions.PolarsError):
        receive(store, receipt, 17)
    assert store.readiness(DAY) == receipt


def test_new_receipt_rejects_conflicting_stock_days_until_partition_is_fixed(tmp_path):
    store = EveningReportStore(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(bars())
    (path,) = (tmp_path / 'kline_daily').glob('date=*/*.parquet')
    duplicate = path.with_name('duplicate.parquet')
    bars().filter(pl.col('symbol') == B).with_columns(pl.lit(49.).alias('volume')).write_parquet(duplicate)
    with pytest.raises(DailyCoverageError, match='冲突的证券日期'):
        receive(store, {}, 16)
    assert store.readiness(DAY) is None
    only_a, _ = receive(store, {}, 16, symbols=(A,))
    with pytest.raises(DailyCoverageError, match='冲突的证券日期'):
        receive(store, only_a, 17)
    duplicate.unlink()
    first, _ = receive(store, only_a, 17, unknown={DAY: None})
    bars(90.).filter(pl.col('symbol') == A).write_parquet(duplicate)
    with pytest.raises(DailyCoverageError, match='冲突的证券日期'):
        receive(store, first, 18)
    assert store.readiness(DAY)['input_id'] == first['input_id']
    duplicate.unlink()
    revised = bars().with_columns(pl.when(pl.col('symbol') == A).then(13.).otherwise('high').alias('high'))
    repo.flush_live_daily(pl.concat([bars(80.).filter(pl.col('symbol') == A), revised]))
    with pytest.raises(DailyCoverageError, match='冲突的证券日期'):
        receive(store, first, 18)
    repo.flush_live_daily(revised)
    _, changes = receive(store, first, 19)
    assert changes == {A: {DAY: {'raw_high': 12., 'known_at': first['cutoff'], 'halted': False, 'wave_formal': True}}}


@pytest.mark.parametrize('writer', ['repair', 'flush'])
def test_unreadable_repair_cannot_advance_clocks_outside_the_accepted_universe(tmp_path, writer):
    store = EveningReportStore(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    repo.append_daily(bars())
    first, _ = receive(store, {}, 16)
    (part,) = (tmp_path / 'kline_daily').glob('date=*/*.parquet')
    part.write_bytes(b'damaged parquet')
    if writer == 'repair':
        repo.replace_daily_rows('stock', date.fromisoformat(DAY), bars(105.), symbols=None,
                                backup_dir=tmp_path / 'backup')
    else:
        repo.flush_live_daily(bars(105.))
    partial, _ = receive(store, first, 17, symbols=(B,), unknown={DAY: None})
    full, _ = receive(store, partial, 18, unknown={DAY: {A}})
    clocks = add_accepted_price_times(bars(), full)
    assert dict(clocks.select('symbol', 'known_at').iter_rows()) == {A: full['cutoff'], B: partial['cutoff']}


def test_empty_accepted_partition_proves_absence_when_rows_are_filled(tmp_path):
    store = EveningReportStore(tmp_path)
    repo = KlineRepository(DataStore(tmp_path))
    part = tmp_path / 'kline_daily' / f'date={DAY}' / 'part.parquet'
    part.parent.mkdir(parents=True)
    bars().clear().write_parquet(part)
    first, _ = receive(store, {}, 16)
    repo.append_daily(bars())
    _, changes = receive(store, first, 17)
    assert changes == {A: {DAY: None}, B: {DAY: None}}
