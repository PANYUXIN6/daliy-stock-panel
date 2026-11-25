"""Real narrow-table inputs, immutable publication, recovery and HTTP boundaries."""
from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import polars as pl
import pytest
from evening_receipt_fixture import current_receipt
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import evening_report, pipeline
from app.enriched_generation import bump_enriched_generation
from app.indicators.pipeline import run_pipeline
from app.market_time import CN_TZ
from app.services import evening_report_inputs as inputs
from app.services import evening_report_jobs as jobs
from app.services import evening_report_store as archive
from app.services import ext_presets, pipeline_jobs, preferences, shared_runtime
from app.services.daily_coverage import DailyCoverage, DailyCoverageError
from app.services.evening_report_engine import SCOPE_VERSION, build_report
from app.services.evening_report_rules import RULE_VERSION
from app.services.evening_report_store import EveningReportStore, atomic_json


def write(root, table, frame):
    for key, part in frame.partition_by('date', as_dict=True).items():
        path = root / table / f'date={key[0]}' / 'part.parquet'
        path.parent.mkdir(parents=True, exist_ok=True)
        part.write_parquet(path)


@pytest.fixture
def market_batch(private_owner, tmp_path, monkeypatch):
    calendar = []
    cursor = date(2026, 6, 1)
    while len(calendar) < 72:
        if cursor.weekday() < 5:
            calendar.append(cursor)
        cursor += timedelta(days=1)
    day = calendar[-1]
    now = datetime.combine(day, datetime.min.time(), tzinfo=CN_TZ).replace(hour=16)
    monkeypatch.setattr(inputs, 'cn_now', lambda: now)
    monkeypatch.setattr(jobs, 'cn_now', lambda: now)
    monkeypatch.setattr(archive, 'cn_now', lambda: now)
    monkeypatch.setattr(preferences, 'load', lambda: {})
    symbols = [f'600{n:03d}.SH' for n in range(24)]
    metadata = pl.DataFrame([{'symbol': symbol, 'name': f'公司{i}', 'code': symbol[:6],
                              'exchange': 'SH', 'type': 'stock', 'board': 'main',
                              'security_type': 'a_share', 'is_st': False, 'is_current': True,
                              'listing_date': '2010-01-01', 'as_of': day,
                              'metadata_source': 'test-standard-provider',
                              'metadata_observed_at': now.isoformat(), 'metadata_rule_version': 'v1'}
                             for i, symbol in enumerate(symbols)])
    path = tmp_path / 'instruments' / 'instruments.parquet'
    path.parent.mkdir()
    metadata.write_parquet(path)
    rows = []
    for index, symbol in enumerate(symbols):
        for offset, trading_date in enumerate(calendar):
            close = 10 * (1 + .001 + index * .0001) ** offset
            rows.append({'symbol': symbol, 'date': trading_date, 'open': close * .999,
                         'high': close * 1.01, 'low': close * .99, 'close': close,
                         'volume': 10_000_000., 'amount': 200_000_000., 'quote_ts': None})
    raw = pl.DataFrame(rows)
    write(tmp_path, 'kline_daily', raw)
    write(tmp_path, 'kline_index_daily', raw.filter(pl.col('symbol') == symbols[0]).with_columns(pl.lit('000001.SH').alias('symbol')))
    run_pipeline(tmp_path)
    atomic_json(tmp_path / 'calendar' / 'trading_days.json', {
        'days': [str(d) for d in calendar], 'start': str(calendar[0]), 'end': str(day)})
    observations = [{'id': source, 'source': source, 'obtained_at': now.isoformat(),
                     'source_effective_date': None, 'source_published_at': None, 'source_version': None,
                     'complete': True, 'symbols': symbols,
                     'memberships': [{'source': source, 'symbol': s, 'sector_id': source + '-1',
                                      'sector_name': source + '-1'} for s in symbols]}
                    for source in ('concept', 'industry')]
    coverage = DailyCoverage(day)
    coverage.observe(raw.filter(pl.col('date') == day))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: None)
    return SimpleNamespace(root=tmp_path, day=day, calendar=calendar, raw=raw, symbols=symbols,
                           metadata=metadata, observations=observations, coverage=coverage, repo=repo, now=now,
                           market={'phase': 'repair', 'state': 'range', 'promo_rate': .3, 'seal_rate': .8})


@pytest.fixture
def accepted(market_batch):
    state = market_batch
    state.evidence = inputs.save_ready(state.root, state.day, state.coverage, state.observations, state.market)
    return state


def test_narrow_storage_rebuilds_real_indicators_and_full_group(accepted):
    state = accepted
    on_disk = pl.read_parquet(state.root / 'kline_daily_enriched' / f'date={state.day}' / 'part.parquet')
    assert 'ma20' not in on_disk.columns and 'atr_14' not in on_disk.columns
    loaded = inputs.load_inputs(state.root, state.day, state.evidence)
    latest = loaded.prices.filter(pl.col('date') == state.day)
    assert latest['formal'].all()
    assert latest.select(pl.all_horizontal(pl.col(c).is_finite() for c in ('ma5', 'ma10', 'ma20', 'atr_14', 'high_60d')).all()).item()
    report = build_report(loaded)
    assert report['statistics']['eligible_count'] == 24
    assert report['statistics']['scored_count'] == 24
    assert {s['member_count'] for s in report['sectors']} == {24}


def test_readiness_requires_stock_publication_but_not_an_index_marker(accepted):
    state = accepted
    assert not (state.root / '.matrix_generation_index.json').exists()
    assert inputs.require_ready(state.root, state.day)['input_id'] == state.evidence['input_id']
    (state.root / '.matrix_generation_stock.json').unlink()
    with pytest.raises(DailyCoverageError, match='指标版本正在发布或不可用'):
        inputs.require_ready(state.root, state.day)


def test_duplicate_outside_report_does_not_block_publication(market_batch):
    from app.tickflow.repository import DataStore, KlineRepository

    state = market_batch
    day = state.calendar[0]
    bars = state.raw.filter(pl.col('date') == day)
    extra = bars.head(1).with_columns(pl.lit('OUTSIDE').alias('symbol'))
    repeated = pl.concat([bars, extra, extra])
    repo = KlineRepository(DataStore(state.root))
    repo.flush_live_daily(repeated)
    path = state.root / 'kline_daily' / f'date={day}' / 'part.parquet'
    assert pl.read_parquet(path).height == bars.height + 1
    conflict = bars.head(1).with_columns((pl.col('close') - .01).alias('close'))
    pl.concat([repeated, conflict]).write_parquet(path)
    with pytest.raises(DailyCoverageError, match='冲突'):
        inputs.save_ready(state.root, state.day, state.coverage, state.observations, state.market)
    pl.concat([repeated, bars.head(1)]).write_parquet(path)
    inputs.save_ready(state.root, state.day, state.coverage, state.observations, state.market)
    report = jobs.generate(state.repo, state.day, progress=lambda *a, **kw: None)
    assert report['status'] == 'published'


def test_old_peer_snapshot_preserves_current_theme_and_selection(market_batch):
    state = market_batch
    target, peer = state.symbols[:2]
    bars = state.raw.with_columns(
        pl.when(pl.col('symbol') == target)
        .then(1.018 ** pl.col('date').rank('dense') * pl.when(pl.col('date') == state.day).then(1.08).otherwise(1.))
        .otherwise(1.).alias('_scale'))
    bars = bars.with_columns((pl.col(c) * pl.col('_scale')).alias(c) for c in ('open', 'high', 'low', 'close')).drop('_scale')

    def generate(raw):
        write(state.root, 'kline_daily', raw)
        run_pipeline(state.root)
        receipt = inputs.save_ready(state.root, state.day, state.coverage, state.observations, state.market)
        return build_report(inputs.load_inputs(state.root, state.day, receipt))

    original = generate(bars)
    assert original['selected'] == [target]
    revised = generate(bars.with_columns(pl.when((pl.col('symbol') == peer) & (pl.col('date') == state.calendar[3]))
                                         .then(pl.lit(123, dtype=pl.Int64)).alias('quote_ts')))
    assert revised['selected'] == original['selected']
    assert revised['observation_themes'][0]['strength'] == original['observation_themes'][0]['strength']


def test_historical_snapshot_invalidates_recursive_state_even_when_today_is_formal(accepted):
    state = accepted
    old = state.calendar[3]
    path = state.root / 'kline_daily' / f'date={old}' / 'part.parquet'
    pl.read_parquet(path).with_columns(pl.when(pl.col('symbol') == state.symbols[0]).then(pl.lit(123, dtype=pl.Int64)).otherwise(None).alias('quote_ts')).write_parquet(path)
    prices = inputs._prices(inputs.SectorReviewReader(state.root), state.day, state.metadata, calendar=state.calendar)
    latest = prices.filter(pl.col('symbol') == state.symbols[0]).row(-1, named=True)
    assert latest['formal'] and latest['ma20'] and latest['high_60d']
    assert latest['atr_14'] is None and latest['bull_bear_line'] is None
    assert prices.filter(pl.col('symbol') == state.symbols[1])['formal'].all()
    with pytest.raises(DailyCoverageError, match='版本已变化'):
        inputs.require_ready(state.root, state.day)


def test_legitimate_historical_halt_does_not_poison_after_window_recovers(accepted):
    state = accepted
    day = state.calendar[2]
    path = state.root / 'kline_daily' / f'date={day}' / 'part.parquet'
    frame = pl.read_parquet(path).with_columns(
        [pl.when(pl.col('symbol') == state.symbols[0]).then(0.).otherwise(pl.col(c)).alias(c)
         for c in ('open', 'high', 'volume', 'amount')])
    frame.write_parquet(path)
    evidence = []
    prices = inputs._prices(inputs.SectorReviewReader(state.root), state.day, state.metadata,
                           suspension_evidence=evidence, calendar=state.calendar)
    assert prices.filter(pl.col('symbol') == state.symbols[0])['formal'].all()
    assert prices.filter((pl.col('symbol') == state.symbols[0]) & (pl.col('date') == day)).is_empty()
    assert evidence == [{'symbol': state.symbols[0], 'date': day}]
    metadata = inputs._metadata(state.evidence, [str(d) for d in state.calendar], evidence)
    assert metadata.filter(pl.col('symbol') == state.symbols[0])['suspension_window'][0] is False


@pytest.mark.parametrize('table', ['kline_daily', 'kline_index_daily'])
def test_new_day_generation_does_not_block_recovery_but_rewritten_input_does(accepted, table):
    state = accepted
    future = state.day + timedelta(days=1)
    write(state.root, 'kline_daily', state.raw.tail(1).with_columns(pl.lit(future).alias('date')))
    bump_enriched_generation(state.root)
    assert inputs.require_ready(state.root, state.day)['input_id'] == state.evidence['input_id']
    path = state.root / table / f'date={state.calendar[0]}' / 'part.parquet'
    frame = pl.read_parquet(path).with_columns((pl.col('amount') + 1).alias('amount'))
    frame.write_parquet(path)
    with pytest.raises(DailyCoverageError, match='版本已变化'):
        inputs.require_ready(state.root, state.day)


def test_classification_observation_uses_response_not_merged_table(tmp_path, monkeypatch):
    answers = [[{'symbol': '600000.SH', 'concepts': ['银行']}],
               [{'symbol': '600001.SH', 'concepts': ['机械']}]]
    async def fetch(config_id, data_dir, on_progress=None):
        return answers.pop(0)
    monkeypatch.setattr(ext_presets, '_fetch_classification_rows', fetch)
    first = asyncio.run(ext_presets.prepare_preset('ext_gn_ths', tmp_path))
    ext_presets.commit_prepared_preset(first, tmp_path)
    second = asyncio.run(ext_presets.prepare_preset('ext_gn_ths', tmp_path))
    ext_presets.commit_prepared_preset(second, tmp_path)
    assert second.observation['symbols'] == ['600001.SH']
    assert second.observation['source_effective_date'] is None
    assert first.observation['id'] != second.observation['id']
    assert {r['symbol'] for r in second.observation['memberships']} == {'600001.SH'}


def test_classification_missing_scope_blocks_readiness_and_unknown_effective_date_is_allowed(accepted):
    state = accepted
    observations = state.evidence['classifications']
    observations[0]['symbols'] = observations[0]['symbols'][1:]
    with pytest.raises(DailyCoverageError, match='本轮分类响应缺少 1'):
        inputs.save_ready(state.root, state.day, state.coverage, observations, {})


def test_classification_archive_compares_to_last_acceptance_and_survives_input_expiry(market_batch):
    state = market_batch
    store = EveningReportStore(state.root)
    previous = str(state.calendar[-2])
    concept, industry = state.observations
    concept['source'] = 'https://example.test/concepts'
    members = concept['memberships']
    # The later receipt already contains the first member. Only the second one is new today.
    removed = {**members[0], 'sector_id': 'old-concept'}
    absent_today = {**removed, 'symbol': '600999.SH'}
    for hour, old_members in ((16, members[3:]), (17, [members[0], removed, absent_today, *members[3:]])):
        cutoff = f'{previous}T{hour}:00:00+08:00'
        prior = current_receipt(previous, cutoff, classifications=[
            {**concept, 'id': f'concept-{hour}', 'obtained_at': cutoff, 'memberships': old_members,
             'symbols': [s for s in [*concept['symbols'], absent_today['symbol']] if s != members[2]['symbol']]},
            {**industry, 'obtained_at': cutoff}])
        prior['classification_comparisons'] = inputs._classification_comparisons(prior, None)
        store.save_readiness(previous, prior)
        if hour == 16:
            original = store.save(previous, {'audit': {}, 'trade_date': previous}, {'input_id': prior['input_id']})

    empty_label_symbol = '600998.SH'
    concept['symbols'] = [*concept['symbols'], empty_label_symbol]
    accepted = inputs.save_ready(state.root, state.day, state.coverage, state.observations, state.market)
    assert accepted['previous_accepted_input_id'] == prior['input_id']
    retry = inputs.save_ready(state.root, state.day, state.coverage, state.observations, state.market)
    (store.root / 'inputs' / f"{prior['input_id']}.json.gz").unlink()
    loaded = inputs.load_inputs(state.root, state.day, retry)
    payload = build_report(loaded)
    added, = next(s for s in payload['sectors'] if s['source'] == 'concept')['membership_change']['added']
    assert added == members[1]['symbol']
    store.save(str(state.day), payload, {'input_id': retry['input_id']})
    changes = store.get(str(state.day))['provenance']['membership_changes']
    comparison = next(c for c in changes if c['source'] == concept['source'])
    assert comparison['added'] == [[added, members[1]['sector_id']]]
    assert comparison['removed'] == [[removed['symbol'], removed['sector_id']]]
    assert comparison['first_observed'] == [[members[2]['symbol'], members[2]['sector_id']]]
    assert comparison['first_observed_symbols'] == sorted((members[2]['symbol'], empty_label_symbol))
    assert comparison['previous_observation_id'] == 'concept-17' and comparison['observation_id'] == concept['id']
    public = store.get(str(state.day), view=True)['provenance']['membership_changes']
    summary = next(c for c in public if c['source'] == concept['source'])
    assert (summary['added_count'], summary['first_observed_count']) == (1, 2)
    assert store.get(previous) == original


def test_first_classification_counts_covered_stocks_including_empty_labels(tmp_path):
    from app.services.evening_report_store import report_view

    first = {'source': 'concept', 'id': 'first', 'symbols': ['A', 'B'], 'memberships': [
        {'symbol': 'A', 'sector_id': 'x'}, {'symbol': 'A', 'sector_id': 'y'}]}
    receipt = {'date': '2026-11-04', 'complete': True, 'classifications': [first]}
    def check(evidence, expected_symbols, expected_members, prior=None):
        evidence['classification_comparisons'] = inputs._classification_comparisons(evidence, prior)
        changes = inputs._classification_changes(evidence)
        assert changes[0]['first_observed_symbols'] == expected_symbols
        assert changes[0]['first_observed'] == expected_members
        public = report_view({'provenance': {'membership_changes': changes}})['provenance']['membership_changes'][0]
        assert public['first_observed_count'] == len(expected_symbols)
        assert 'first_observed_symbols' not in public

    check(receipt, ['A', 'B'], [['A', 'x'], ['A', 'y']])
    day = receipt['date']
    cutoff = day + 'T17:00:00+08:00'
    empty = current_receipt(day, cutoff, classifications=[{
        'source': 'concept', 'id': 'empty', 'obtained_at': cutoff, 'complete': True, 'symbols': [], 'memberships': []}])
    check(empty, [], [])
    store = EveningReportStore(tmp_path)
    store.save_readiness(day, empty)
    assert store.readiness(day) == empty
    second = {**receipt, 'date': '2026-11-05',
              'classifications': [{**first, 'id': 'second', 'symbols': ['A', 'B', 'C']}]}
    check(second, ['C'], [], receipt)
    second['classifications'][0]['symbols'] = ['A', 'B']
    check(second, [], [], receipt)


def test_new_classification_semantics_start_new_scope_without_rewriting_old_evidence(accepted, monkeypatch):
    from copy import deepcopy

    state = accepted
    old_scope = state.evidence['scope_version']
    observations = deepcopy(state.evidence['classifications'])
    for observation in observations:
        observation['membership_semantics'] = 'fuyao-flat-v1'
    updated = inputs.save_ready(state.root, state.day, state.coverage, observations, {})
    assert updated['scope_version'] == old_scope + '/fuyao-flat-v1'
    old = EveningReportStore(state.root).input_evidence(state.evidence['input_id'])
    assert old['scope_version'] == old_scope
    assert all('membership_semantics' not in observation for observation in old['classifications'])
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *_: None)
    submitted = AsyncMock(return_value={'job_id': 'queued'})
    monkeypatch.setattr(pipeline, '_submit_job', submitted)
    asyncio.run(jobs.trigger(state.repo, day=state.day, automatic=True))
    assert f':{updated["scope_version"]}:' in submitted.call_args.kwargs['request_key']
    jobs.generate(state.repo, state.day, progress=lambda *a, **k: None)
    report = EveningReportStore(state.root).get(str(state.day))
    assert report['scope_version'] == report['provenance']['scope_version'] == updated['scope_version']


def test_generation_restarts_classification_continuity_and_reuses_matching_daily_scopes(tmp_path, monkeypatch):
    from test_evening_report_engine import inputs_at

    store = EveningReportStore(tmp_path)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    values = {value.trade_date: value for value in (inputs_at(index) for index in range(65, 71))}
    days = list(values)
    new_scope = SCOPE_VERSION + '/fuyao-flat-v1'
    for index, day in enumerate(days):
        store.save_readiness(day, current_receipt(day, day, calendar={'days': days},
                                                  scope_version=SCOPE_VERSION if index < 3 else new_scope))

    def ready(root, day, *, input_id=None):
        return store.input_evidence(input_id) if input_id else store.readiness(str(day))

    def load(root, day, evidence, check):
        value = values[str(day)]
        value.provenance['scope_version'] = evidence['scope_version']
        return value

    monkeypatch.setattr(jobs, 'require_ready', ready)
    monkeypatch.setattr(jobs, 'load_inputs', load)
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *_: None)
    publication_clock = {'day': days[0]}
    monkeypatch.setattr(jobs, 'cn_now', lambda: datetime.fromisoformat(publication_clock['day']).replace(hour=18, tzinfo=CN_TZ))
    for day in days[:3]:
        publication_clock['day'] = day
        jobs.generate(repo, date.fromisoformat(day), progress=lambda *a, **k: None)
    previous = store.get(days[2])
    assert any(identity['valid'] is True for identity in store.state(days[2])['state']['identities'].values())

    for day in days[3:]:
        publication_clock['day'] = day
        jobs.generate(repo, date.fromisoformat(day), progress=lambda *a, **k: None)
    reports = [store.get(day) for day in days[3:]]

    first, _second, third = reports
    first_state, second_state, third_state = [store.state(report['date'], report['version'])['state'] for report in reports]
    assert all(report['scope_version'] == new_scope for report in reports)
    assert next(iter(first_state['rounds'].values()))['status'] == 'probing'
    assert not any(identity['valid'] is True for identity in first_state['identities'].values())
    assert first['statistics']['publication_frequency']['report_days'] == 1
    assert next(iter(second_state['rounds'].values()))['status'] == 'active'
    assert third['statistics']['publication_frequency']['report_days'] == 3
    assert any(identity['valid'] is True for identity in third_state['identities'].values())
    assert store.get(days[2]) == previous


def test_concept_filter_uses_original_members_and_never_filters_industry():
    rows = [{'symbol': str(i), 'sector_id': '宽基', 'sector_name': '宽基', 'source': 'concept'} for i in range(121)]
    rows += [{'symbol': 'x', 'sector_id': '宽基', 'sector_name': '宽基', 'source': 'industry'}]
    frame, evidence = inputs._memberships({'classifications': [{'memberships': rows}],
                                          'sector_filter_config': {'min_members': 1, 'max_members': 7, 'blacklist': []}})
    assert frame['source'].to_list() == ['industry']
    assert evidence['excluded'][0]['members'] == 121


def test_original_is_immutable_and_public_view_does_not_load_internal_archive(tmp_path, monkeypatch):
    store = EveningReportStore(tmp_path)
    original = store.save('2026-10-16', {'audit': {}, 'trade_date': '2026-10-16', 'rule_version': RULE_VERSION,
                         'state': {'secret_internal': 'x' * 100_000}, 'records': [], 'sectors': [],
                         'calculations': []}, {'input_id': 'a' * 32})
    assert store.save('2026-10-16', {'audit': {}, 'state': 'replaced'}, {'input_id': 'b' * 32}) == original
    original_read = archive.read_json
    def read(path):
        if path.parent.name == 'archive' and any(part in path.name for part in ('.state.', '.candidates.', '.audit.')):
            raise AssertionError('GET不应解析内部恢复档案')
        return original_read(path)
    monkeypatch.setattr(archive, 'read_json', read)
    view = store.get('2026-10-16', view=True)
    assert 'state' not in view and 'calculations' not in view
    assert len(json.dumps(view)) < 2000
    assert store.dates()[0]['versions'][0]['version'] == original['version']


def test_generation_archives_once_and_repeated_input_does_not_compute(accepted, monkeypatch):
    from app.services import evening_report_engine
    events = []
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda root, datasets: events.append(datasets))
    state = accepted
    first = jobs.generate(state.repo, state.day, progress=lambda *a, **k: None)
    monkeypatch.setattr(evening_report_engine, 'build_report', lambda *a, **k: pytest.fail('同输入不得再次推进状态'))
    second = jobs.generate(state.repo, state.day, progress=lambda *a, **k: None)
    assert first['version'] == second['version'] and second['reused']
    assert events == [['evening_report']]
    assert EveningReportStore(state.root).get(str(state.day), view=True)['statistics']['scored_count'] == 24


def test_history_reads_only_seven_state_files_regardless_of_archive_age(tmp_path, monkeypatch):
    store = EveningReportStore(tmp_path)
    for offset in range(250):
        day = date(2025, 1, 1) + timedelta(days=offset)
        store.save(str(day), {'audit': {}, 'trade_date': str(day), 'rule_version': RULE_VERSION,
                              'state': {'forward': offset}, 'records': [], 'calculations': []},
                   {'input_id': f'{offset:032x}'})
    read = archive.read_json
    state_reads = []
    def watched(path):
        if path.parent.name == 'archive':
            assert path.name.endswith('.state.json.gz')
            state_reads.append(path)
        return read(path)
    monkeypatch.setattr(archive, 'read_json', watched)
    history = store.history('2026-01-01', RULE_VERSION)
    assert len(history) == len(state_reads) == 7
    assert history[-1]['state'] == {'forward': 249}
    assert len(json.dumps(history)) < 4000


@pytest.mark.parametrize(('today', 'expected'), [('2026-11-15', '2026-11-13'), ('2026-11-16', '2026-11-16'),
                                              ('2026-10-04', '2026-09-30')])
def test_default_status_uses_latest_calendar_trade_day_not_today_or_latest_report(tmp_path, monkeypatch, today, expected):
    monkeypatch.setattr(jobs, 'cn_now', lambda: datetime.fromisoformat(today).replace(hour=18, tzinfo=CN_TZ))
    atomic_json(tmp_path / 'calendar' / 'trading_days.json', {
        'days': ['2026-09-30', '2026-10-09', '2026-11-12', '2026-11-13', '2026-11-16'],
        'start': '2026-09-30', 'end': '2026-11-16',
    })
    EveningReportStore(tmp_path).save('2026-09-29', {'audit': {}, 'state': {}}, {'input_id': 'a' * 32})
    jobs._save_attempt(tmp_path, '2026-11-15', {'status': 'failed', 'reason': '非交易日不能生成'})
    result = jobs.status(tmp_path)
    assert result['date'] == expected
    assert result['status'] == ('unpublished' if expected < today else 'waiting')
    if expected < today:
        assert '无正式晚报' in result['reason']
    assert jobs.status(tmp_path, '2026-11-15')['status'] == ('failed' if today == '2026-11-15' else 'unpublished')


def test_default_status_does_not_invent_trade_day_without_calendar_or_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'cn_now', lambda: datetime(2026, 11, 15, 18, tzinfo=CN_TZ))
    result = jobs.status(tmp_path)
    assert result['date'] == ''
    assert result['status'] == 'waiting'
    assert '交易日历尚未就绪' in result['reason']
    EveningReportStore(tmp_path).save('2026-11-13', {'audit': {}, 'state': {}}, {'input_id': 'a' * 32})
    assert jobs.status(tmp_path)['date'] == '2026-11-13'
    assert jobs.status(tmp_path)['status'] == 'published'


def test_status_distinguishes_incomplete_ready_and_completed_job_without_callback(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'cn_now', lambda: datetime(2026, 10, 16, 18, tzinfo=CN_TZ))
    store = EveningReportStore(tmp_path)
    store.save_readiness('2026-10-16', {'complete': False, 'reason': '分类尚缺证券'})
    assert jobs.status(tmp_path, '2026-10-16')['status'] == 'waiting'
    assert jobs.status(tmp_path, '2026-10-16')['reason'] == '分类尚缺证券'
    registry = pipeline_jobs.JobStore(store_dir=tmp_path / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', registry)
    job_id, _ = registry.create(request_key='evening_report:2026-10-16:live:v1:x')
    jobs._save_attempt(tmp_path, '2026-10-16', {'job_id': job_id, 'status': 'generating'})
    store.save('2026-10-16', {'audit': {}, 'state': {}}, {'input_id': 'a' * 32})
    registry.succeed(job_id, {})
    assert jobs.status(tmp_path, '2026-10-16')['status'] == 'published'


def test_published_original_takes_precedence_over_stale_attempt(tmp_path, monkeypatch):
    from app.jobs import daily_pipeline
    from app.services import trading_day
    day = date(2026, 10, 16)
    now = datetime(2026, 10, 16, 18, tzinfo=CN_TZ)
    monkeypatch.setattr(jobs, 'cn_now', lambda: now)
    monkeypatch.setattr(preferences, 'load', lambda: {})
    monkeypatch.setattr(trading_day, 'is_trading_day', lambda *a, **k: True)
    monkeypatch.setattr(daily_pipeline, '_get_app_state', lambda: SimpleNamespace(quote_service=None))
    registry = pipeline_jobs.JobStore(store_dir=tmp_path / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', registry)
    store = EveningReportStore(tmp_path)
    store.save(str(day), {'audit': {}, 'state': {}}, {'input_id': 'a' * 32})
    jobs._save_attempt(tmp_path, str(day), {'status': 'failed', 'reason': '刷新通知失败'})
    trigger = AsyncMock()
    monkeypatch.setattr(jobs, 'trigger', trigger)
    derived = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs, '_queue_tracking', derived)
    assert asyncio.run(jobs.run_scheduled(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))) is None
    trigger.assert_not_awaited()
    derived.assert_awaited_once()
    assert jobs.status(tmp_path, str(day))['status'] == 'published'


def test_browser_serves_the_same_original_by_date_or_version(tmp_path, monkeypatch):
    from app.services import evening_report_store

    day = '2026-10-16'
    store = EveningReportStore(tmp_path)
    monkeypatch.setattr(evening_report_store, 'cn_now', lambda: datetime(2026, 10, 16, 18, tzinfo=CN_TZ))
    original = store.save(day, {'audit': {}, 'rule_version': 'initial-rule', 'summary': '原始观察'}, {'input_id': 'a' * 32})
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(evening_report.router)
    with TestClient(app) as client:
        endpoint = f'/api/evening-report?date={day}'
        assert client.get(endpoint).json()['version'] == original['version']
        assert client.get('/api/evening-report').json()['summary'] == '原始观察'
        pinned = client.get(f'{endpoint}&version={original["version"]}').json()
        assert pinned['summary'] == '原始观察'
        assert pinned['rule_version'] == 'initial-rule'
        assert client.get(f'{endpoint}&version={"0" * 32}').status_code == 404
    assert store.get(day)['version'] == original['version']


def test_browser_read_is_available_to_guest_but_shared_write_is_admin_only(tmp_path, monkeypatch):
    from app.api import auth
    from app.services.auth import AuthStore
    from app.services.auth_access import install_auth_middleware
    from app.services.collector_proxy import collector_owns
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.auth_store = AuthStore(tmp_path)
    app.state.auth_store.bootstrap('admin', 'initial-secret')
    app.state.auth_store.create_user('guest', 'temporary-secret', 'guest', None)
    token, _ = app.state.auth_store.login('guest', 'temporary-secret', 'ip', '')
    token, _ = app.state.auth_store.change_password(token, 'temporary-secret', 'permanent-secret')
    app.include_router(evening_report.router)
    install_auth_middleware(app)
    trigger = AsyncMock(return_value={'job_id': 'job', 'status': 'generating'})
    monkeypatch.setattr(jobs, 'trigger', trigger)
    with TestClient(app) as client:
        assert client.get('/api/evening-report/dates').status_code == 401
        client.cookies.set(auth.COOKIE_NAME, token)
        assert client.get('/api/evening-report/dates').json() == {'dates': []}
        atomic_json(tmp_path / 'calendar' / 'trading_days.json', {'days': ['2026-11-13']})
        monkeypatch.setattr(jobs, 'cn_now', lambda: datetime(2026, 11, 15, 18, tzinfo=CN_TZ))
        assert client.get('/api/evening-report/status').json()['date'] == '2026-11-13'
        assert client.get('/api/evening-report?date=2026-10-16').json()['date'] == '2026-10-16'
        assert client.post('/api/evening-report/rebuild', json={'date': '2026-10-16'},
                           headers={'Origin': 'http://testserver'}).status_code == 403
        trigger.assert_not_awaited()
    assert collector_owns('POST', '/api/evening-report/rebuild')
    assert collector_owns('GET', '/api/evening-report/status')
    assert not collector_owns('GET', '/api/evening-report?date=2026-10-16')


@pytest.fixture
def task_runtime(accepted, monkeypatch):
    from app.jobs import daily_pipeline
    registry = pipeline_jobs.JobStore(store_dir=accepted.root / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', registry)
    monkeypatch.setattr(pipeline, 'job_store', registry)
    monkeypatch.setattr(pipeline_jobs, '_run_slot_owner', None)
    monkeypatch.setattr(pipeline_jobs, '_CANCEL_FLAGS', {})
    monkeypatch.setattr(daily_pipeline, '_get_app_state', lambda: SimpleNamespace(quote_service=None, capabilities=object()))
    monkeypatch.setattr(daily_pipeline, 'run_weekly_mining_after_sync', lambda: None)
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *_: None)
    return accepted, registry


async def drain_tasks():
    while pipeline._tasks:
        await asyncio.gather(*list(pipeline._tasks))


@pytest.fixture
def manual_runtime(task_runtime, monkeypatch):
    from app.services.auth import AuthStore
    from app.services.user_context import UserContext, user_scope

    state, registry = task_runtime
    auth = AuthStore(state.root)
    admin = auth.bootstrap('manualadmin', 'initial-secret')
    token, _ = auth.login('manualadmin', 'initial-secret', 'ip', '')
    actor = UserContext(admin['id'], 'admin', state.root, session_token=token)
    monkeypatch.setattr(jobs, '_queue_tracking', AsyncMock())
    with user_scope(actor):
        yield state, registry, auth, token


@pytest.mark.parametrize('initial', ['missing', 'changed', 'accepted'])
def test_manual_generation_prepares_only_when_needed_then_publishes(manual_runtime, monkeypatch, initial):
    from app.jobs import daily_pipeline

    state, registry, _, _ = manual_runtime
    store = EveningReportStore(state.root)
    if initial == 'missing':
        store.save_readiness(str(state.day), {'complete': False})
    if initial == 'changed':
        write(state.root, 'kline_daily', state.raw)
    refreshed = Mock()
    monkeypatch.setattr(pipeline, 'finalize_market_data', refreshed)
    caps = object()
    quote_service = SimpleNamespace(paused=nullcontext)

    def prepare(repo, capset, *, on_progress, postmarket, target_date, evening_only):
        assert repo is state.repo and capset is caps
        assert postmarket and evening_only and target_date == state.day
        assert registry.get(pipeline_jobs._run_slot_owner)['request_key'].startswith('evening_report:')
        on_progress('sync_daily', 45, '准备正式行情')
        assert jobs.status(state.root, str(state.day))['reason'] == '准备正式行情'
        inputs.save_ready(state.root, state.day, state.coverage,
                          state.evidence['classifications'], state.evidence['market'])
        return {}

    prepared = Mock(side_effect=prepare)
    monkeypatch.setattr(daily_pipeline, 'run_now', prepared)
    async def run():
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            repo=state.repo, quote_service=quote_service, capabilities=caps)))
        result = await evening_report.rebuild(request, evening_report.RebuildRequest(
            date=state.day))
        await drain_tasks()
        return result

    result = asyncio.run(run())
    job = registry.get(result['job_id'])
    assert job['status'] == 'succeeded', job.get('error')
    assert prepared.call_count == refreshed.call_count == (0 if initial == 'accepted' else 1)
    assert store.get(str(state.day), job['result']['version'])['input_reference']['input_id'] == store.readiness(str(state.day))['input_id']
    assert pipeline_jobs._run_slot_owner is None


@pytest.mark.parametrize('failure', ['exception', 'unaccepted', 'cancel', 'revoked'])
def test_manual_preparation_failure_never_publishes_and_refreshes_partial_data(manual_runtime, monkeypatch, failure):
    from app.jobs import daily_pipeline

    state, registry, auth, token = manual_runtime
    store = EveningReportStore(state.root)
    store.save_readiness(str(state.day), {'complete': False})
    refreshed = Mock()
    monkeypatch.setattr(pipeline, 'finalize_market_data', refreshed)

    def prepare(*args, on_progress, **kwargs):
        on_progress('sync_daily', 45, '准备正式行情')
        if failure == 'exception':
            raise DailyCoverageError('正式行情不完整')
        if failure == 'cancel':
            pipeline.cancel_job(pipeline_jobs._run_slot_owner)
        if failure == 'revoked':
            inputs.save_ready(state.root, state.day, state.coverage,
                              state.evidence['classifications'], state.evidence['market'])
            auth.revoke_session(token)
        return {}

    monkeypatch.setattr(daily_pipeline, 'run_now', prepare)
    async def run():
        result = await jobs.trigger(state.repo, day=state.day, capset=object())
        await drain_tasks()
        return result

    result = asyncio.run(run())
    job = registry.get(result['job_id'])
    assert job['status'] == 'failed'
    assert job['outcome'] == ('cancelled' if failure == 'cancel' else 'failed')
    assert not store.has_original(str(state.day))
    refreshed.assert_called_once_with(state.repo, None)
    jobs._queue_tracking.assert_not_awaited()
    assert pipeline_jobs._run_slot_owner is None


@pytest.mark.parametrize('reason', ['before_close', 'automatic'])
def test_missing_inputs_are_not_prepared_outside_manual_current_postmarket(manual_runtime, monkeypatch, reason):
    state, registry, _, _ = manual_runtime
    EveningReportStore(state.root).save_readiness(str(state.day), {'complete': False})
    if reason == 'before_close':
        monkeypatch.setattr(jobs, 'cn_now', lambda: state.now.replace(hour=15))
    with pytest.raises(HTTPException) as error:
        asyncio.run(jobs.trigger(state.repo, day=state.day, capset=object(), automatic=reason == 'automatic'))
    assert error.value.status_code == 409
    assert not registry.list_recent()


@pytest.mark.parametrize('calendar_result', ['trading', 'closed', 'unknown'])
def test_manual_preparation_requires_confirmed_calendar_after_refresh(manual_runtime, monkeypatch, calendar_result):
    from app.jobs import daily_pipeline
    from app.services import trading_day

    state, registry, _, _ = manual_runtime
    store = EveningReportStore(state.root)
    store.save_readiness(str(state.day), {'complete': False})
    calendar_path = state.root / 'calendar' / 'trading_days.json'
    calendar_path.unlink()

    def sync_calendar(root):
        assert root == state.root
        if calendar_result != 'unknown':
            atomic_json(calendar_path, {**state.evidence['calendar'],
                                       'days': state.evidence['calendar']['days'] if calendar_result == 'trading' else []})
        return {}

    def prepare(*args, **kwargs):
        inputs.save_ready(state.root, state.day, state.coverage,
                          state.evidence['classifications'], state.evidence['market'])
        return {}

    refreshed = Mock(side_effect=sync_calendar)
    prepared = Mock(side_effect=prepare)
    monkeypatch.setattr(trading_day, 'sync_calendar', refreshed)
    monkeypatch.setattr(daily_pipeline, 'run_now', prepared)
    monkeypatch.setattr(pipeline, 'finalize_market_data', lambda *a: None)
    async def run():
        result = await jobs.trigger(state.repo, day=state.day, capset=object())
        await drain_tasks()
        return result

    result = asyncio.run(run())
    assert registry.get(result['job_id'])['status'] == ('succeeded' if calendar_result == 'trading' else 'failed')
    assert store.has_original(str(state.day)) is (calendar_result == 'trading')
    assert prepared.call_count == (1 if calendar_result == 'trading' else 0)
    refreshed.assert_called_once_with(state.root)


@pytest.mark.parametrize('ready_at_submission', [False, True])
def test_manual_preparation_rechecks_date_after_waiting_for_execution(manual_runtime, monkeypatch, ready_at_submission):
    from app.jobs import daily_pipeline

    state, registry, _, _ = manual_runtime
    store = EveningReportStore(state.root)
    if not ready_at_submission:
        store.save_readiness(str(state.day), {'complete': False})
    original_dates = store.dates()
    prepared = Mock()
    monkeypatch.setattr(daily_pipeline, 'run_now', prepared)
    async def run():
        result = await jobs.trigger(state.repo, day=state.day, capset=object())
        store.save_readiness(str(state.day), {'complete': False})
        monkeypatch.setattr(jobs, 'cn_now', lambda: state.now + timedelta(days=1))
        await drain_tasks()
        return result

    result = asyncio.run(run())
    assert registry.get(result['job_id'])['status'] == 'failed'
    assert '跨日期' in registry.get(result['job_id'])['error']
    assert store.dates() == original_dates
    prepared.assert_not_called()


def test_manual_generation_crossing_midnight_before_publication_keeps_originals(manual_runtime, monkeypatch):
    from app.services import evening_report_engine

    state, registry, _, _ = manual_runtime
    store = EveningReportStore(state.root)
    originals = store.dates()
    calculate = evening_report_engine.build_report

    def crossing_midnight(*args, **kwargs):
        payload = calculate(*args, **kwargs)
        monkeypatch.setattr(jobs, 'cn_now', lambda: state.now + timedelta(days=1))
        return payload

    monkeypatch.setattr(evening_report_engine, 'build_report', crossing_midnight)
    async def run():
        result = await jobs.trigger(state.repo, day=state.day, capset=object())
        await drain_tasks()
        return result

    result = asyncio.run(run())
    assert registry.get(result['job_id'])['status'] == 'failed'
    assert '跨日期' in registry.get(result['job_id'])['error']
    assert store.dates() == originals


def test_later_pipeline_failure_still_generates_independent_report_after_releasing_slot(task_runtime, monkeypatch):
    state, registry = task_runtime
    actual_generate = jobs.generate
    seen = []
    def generation(*args, **kwargs):
        owner = pipeline_jobs._run_slot_owner
        seen.append(owner)
        assert registry.get(owner)['request_key'].startswith('evening_report:')
        return actual_generate(*args, **kwargs)
    monkeypatch.setattr(jobs, 'generate', generation)
    async def run():
        def late_failure(progress):
            def execute():
                progress('minute', 95, '分钟历史失败')
                raise ValueError('分钟历史失败')
            return execute
        parent = await pipeline._submit_job(state.repo, None, late_failure,
                                            request_key=f'postmarket:{state.day}', finalize=lambda: None)
        await drain_tasks()
        return parent
    parent = asyncio.run(run())
    assert registry.get(parent['job_id'])['status'] == 'failed'
    assert len(seen) == 1 and seen[0] != parent['job_id']
    assert EveningReportStore(state.root).has_original(str(state.day))
    assert jobs.status(state.root, str(state.day))['status'] == 'published'
    assert pipeline_jobs._run_slot_owner is None


def test_cancelled_parent_never_derives_evening_and_restart_keeps_cancellation(task_runtime):
    from threading import Event
    state, registry = task_runtime
    started, finish = Event(), Event()
    async def run():
        def make(progress):
            def execute():
                started.set()
                assert finish.wait(3)
                progress('minute', 95, '取消检查')
                return {}
            return execute
        parent = await pipeline._submit_job(state.repo, None, make,
                                            request_key=f'postmarket:{state.day}', finalize=lambda: None)
        assert await asyncio.to_thread(started.wait, 3)
        pipeline.cancel_job(parent['job_id'])
        # This check runs before the parent worker has reached its finally block.
        assert await jobs.run_scheduled(state.repo) is None
        finish.set()
        await drain_tasks()
        return parent
    parent = asyncio.run(run())
    assert registry.get(parent['job_id'])['outcome'] == 'cancelled'
    assert jobs.status(state.root, str(state.day))['status'] == 'cancelled'
    assert not EveningReportStore(state.root).has_original(str(state.day))
    restarted = pipeline_jobs.JobStore(store_dir=state.root / 'jobs')
    assert restarted.get(parent['job_id'])['outcome'] == 'cancelled'
    assert asyncio.run(jobs.run_scheduled(state.repo)) is None


def test_pending_evening_restart_is_failed_and_can_retry_only_its_step(task_runtime, monkeypatch):
    state, registry = task_runtime
    key = f'evening_report:{state.day}:live:{RULE_VERSION}:{state.evidence["input_id"]}'
    job_id, _ = registry.create(request_key=key)
    jobs._save_attempt(state.root, str(state.day), {'job_id': job_id, 'status': 'generating'})
    restored = pipeline_jobs.JobStore(store_dir=state.root / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', restored)
    monkeypatch.setattr(pipeline, 'job_store', restored)
    assert jobs.status(state.root, str(state.day))['status'] == 'failed'
    async def retry():
        result = await jobs.run_scheduled(state.repo)
        await drain_tasks()
        return result
    result = asyncio.run(retry())
    assert result['job_id'] != job_id
    assert restored.get(result['job_id'])['status'] == 'succeeded'
    assert all(job.get('request_key', '').startswith(('evening_report:', 'evening_tracking:')) for job in restored.list_recent())


@pytest.mark.parametrize('scenario', ['same-day-retry', 'after-long-outage', 'only-original-after-long-outage'])
def test_missing_last_input_takes_precedence_over_readable_history(market_batch, monkeypatch, scenario):
    state = market_batch
    store = EveningReportStore(state.root)
    monkeypatch.setattr(inputs, 'cn_now', lambda: state.now + timedelta(hours=2))
    same_day = scenario == 'same-day-retry'
    older_day, missing_day = (state.calendar[-2], state.day) if same_day else state.calendar[:2]
    older = current_receipt(str(older_day), f'{older_day}T16:00:00+08:00', active_symbols=state.symbols)
    store.save_readiness(str(older_day), older)
    missing = {**older, 'date': str(missing_day), 'cutoff': f'{missing_day}T17:00:00+08:00'}
    del missing['input_id']
    store.save_readiness(str(missing_day), missing)
    store.save_readiness(str(missing_day), {'complete': False})
    (store.root / 'inputs' / f"{missing['input_id']}.json.gz").unlink()
    if scenario == 'only-original-after-long-outage':
        store.save(str(missing_day), {'audit': {}, 'trade_date': str(missing_day)}, {'input_id': missing['input_id']})
        (store.root / 'ready' / f'{missing_day}.json').unlink()
        (store.root / 'acceptances' / f'{missing_day}.json').unlink()

    receipt = inputs.save_ready(state.root, state.day, state.coverage, state.observations, state.market)
    assert receipt.get('wave_previous_acceptance') == (None if scenario == 'only-original-after-long-outage' else missing['cutoff'])
    assert receipt['wave_missing_predecessor_day'] == str(missing_day)
    assert receipt.get('previous_accepted_input_id') == (None if scenario == 'only-original-after-long-outage' else missing['input_id'])
    assert receipt['wave_first_acceptances'][state.symbols[0]] == older['cutoff']
    assert 'wave_price_compared_after' not in receipt


def test_same_input_uses_saved_historical_suspension_not_mutable_ready(accepted):
    from app.services.evening_report_start_study import evaluate_start
    from app.services.evening_report_wave import VERSION, PriceBasis, build_waves

    state = accepted
    symbol = state.symbols[0]
    previous = str(state.calendar[-2])
    store = EveningReportStore(state.root)
    partition = state.root / 'kline_daily' / f'date={previous}' / 'part.parquet'
    original_prices = pl.read_parquet(partition)
    original_prices.filter(pl.col('symbol') != symbol).write_parquet(partition)
    factors = state.root / 'adj_factor' / 'all.parquet'
    factors.parent.mkdir(exist_ok=True)
    pl.DataFrame(schema={'symbol': pl.String, 'trade_date': pl.Date, 'ex_factor': pl.Float64}).write_parquet(factors)
    prior_evidence = current_receipt(
        previous, previous + 'T16:00:00+08:00', halted_symbols=[symbol],
        halted_evidence=[{'symbol': symbol, 'date': previous, 'confirmed': True, 'source': 'explicit_daily_status',
                          'obtained_at': previous + 'T16:00:00+08:00'}],
        classifications=state.evidence['classifications'])
    prior_evidence['classification_comparisons'] = inputs._classification_comparisons(prior_evidence, None)
    store.save_readiness(previous, prior_evidence)
    evidence = inputs.save_ready(state.root, state.day, state.coverage, state.evidence['classifications'], state.evidence['market'])
    before = inputs.load_inputs(state.root, state.day, evidence).metadata
    store.save_readiness(previous, current_receipt(previous, previous + 'T17:00:00+08:00'))
    assert store.input_evidence(evidence['input_id'])['historical_suspensions']
    loaded = inputs.load_inputs(state.root, state.day, store.input_evidence(evidence['input_id']))
    assert before.equals(loaded.metadata)
    assert loaded.metadata.filter(pl.col('symbol') == symbol)['suspension_window'][0] is True
    suspended = {
        (row['symbol'], str(row['date'])) for row in loaded.provenance['suspensions'] if row['confirmed']
    }
    assert (symbol, previous) in suspended
    wave, snapshots, _, _, _ = build_waves(loaded, [{'symbol': symbol}], {}, loaded.calendar, lambda: None)
    assert snapshots[symbol]['missing_from'] is None
    assert wave['stocks'][symbol]['state']['density_days'][previous] == 'suspended'

    basis = PriceBasis([], loaded.provenance['wave_factor_source'], loaded.cutoff)
    anchor_day = str(state.calendar[-3])
    event = {'security_id': symbol, 'segment_index': 2, 'start_id': 'suspended-test', 'rule_version': VERSION,
             'known_at': previous + 'T16:00:00+08:00', 'evidence_date': anchor_day,
             'target_high': basis.evidence(100., 'high', anchor_day, anchor_day) | {'pivot_id': 'H1'},
             'failure_low': basis.evidence(1., 'low', anchor_day, anchor_day) | {'pivot_id': 'L1'}}
    evaluation = evaluate_start(event, calendar=loaded.calendar, prices={}, suspended=suspended, basis=basis,
                                invalidated=None, as_of=loaded.trade_date, evaluated_at=loaded.cutoff)
    assert evaluation['evaluation_reason_code'] == 'suspended'

    original = store.save(previous, {'audit': {}, 'trade_date': previous}, {'input_id': prior_evidence['input_id']})
    original_prices.write_parquet(partition)
    restored = inputs.save_ready(state.root, state.day, state.coverage, state.evidence['classifications'], state.evidence['market'])
    loaded = inputs.load_inputs(state.root, state.day, inputs.require_ready(state.root, state.day))
    row = loaded.prices.filter((pl.col('date').cast(pl.String) == previous) & (pl.col('symbol') == symbol)).row(0, named=True)
    assert row['formal'] and row['wave_formal']
    suspended = {(ref['symbol'], str(ref['date'])) for ref in loaded.provenance['suspensions'] if ref['confirmed']}
    _, snapshots, _, _, _ = build_waves(loaded, [{'symbol': symbol}], wave, loaded.calendar, lambda: None)
    evaluation = evaluate_start(event, calendar=loaded.calendar, prices={(symbol, previous): row}, suspended=suspended,
                                basis=basis, invalidated=None, as_of=previous, evaluated_at=restored['cutoff'])
    assert (symbol, previous) not in suspended
    assert loaded.metadata.filter(pl.col('symbol') == symbol)['suspension_window'][0] is False
    assert snapshots[symbol]['pivot_density']['suspended'] == 1
    assert evaluation['status'] == 'not_due'
    assert store.input_evidence(original['input_reference']['input_id'])['halted_evidence'] == prior_evidence['halted_evidence']


def test_replay_orders_recovered_trading_days_by_actual_acceptance_time(tmp_path):
    store = EveningReportStore(tmp_path)
    previous, day, tomorrow = '2026-11-04', '2026-11-05', '2026-11-06'
    normal = current_receipt(day, day + 'T17:00:00+08:00')
    recovered = current_receipt(previous, day + 'T18:00:00+08:00')
    store.save_readiness(day, normal)
    store.save_readiness(previous, recovered)
    current = current_receipt(tomorrow, tomorrow + 'T17:00:00+08:00')
    history, _ = inputs._accepted_history(store, store.acceptance_references(), current, day + 'T16:00:00+08:00', {})
    assert [r['cutoff'] for r in history] == [normal['cutoff'], recovered['cutoff']]


def test_receipt_dates_only_covered_stocks_in_the_same_partition(market_batch, monkeypatch):
    state = market_batch
    prior_day = state.calendar[-2]
    prior_cutoff = datetime.combine(prior_day, state.now.timetz()).isoformat()
    store = EveningReportStore(state.root)
    factor_path = state.root / 'adj_factor' / 'all.parquet'
    factor_path.parent.mkdir(exist_ok=True)
    factors = pl.DataFrame({'symbol': [state.symbols[0]], 'trade_date': [prior_day], 'ex_factor': [1.01]})
    factors.write_parquet(factor_path)
    prior_coverage = DailyCoverage(prior_day)
    prior_coverage.observe(state.raw.filter((pl.col('symbol') == state.symbols[0]) & (pl.col('date') == prior_day)))
    with monkeypatch.context() as previous:
        previous.setattr(inputs, 'cn_now', lambda: datetime.fromisoformat(prior_cutoff))
        inputs.save_ready(state.root, prior_day, prior_coverage,
                          [{**o, 'obtained_at': prior_cutoff} for o in state.observations], state.market)
    evidence = inputs.save_ready(state.root, state.day, state.coverage, state.observations, state.market)
    loaded = inputs.load_inputs(state.root, state.day, evidence)
    prior_bars = loaded.prices.filter(pl.col('date') == prior_day)
    assert prior_bars.filter(pl.col('symbol') == state.symbols[0])['known_at'].item() == prior_cutoff
    assert prior_bars.filter(pl.col('symbol') != state.symbols[0])['known_at'].unique().to_list() == [evidence['cutoff']]
    assert loaded.prices.filter(pl.col('date') == state.day)['known_at'].unique().to_list() == [evidence['cutoff']]
    assert loaded.provenance['wave_factor_known_at'] == prior_cutoff
    assert loaded.provenance['wave_factor_facts'][state.symbols[0]][str(prior_day)] == [1.01, prior_cutoff]

    partition = state.root / 'kline_daily' / f'date={prior_day}' / 'part.parquet'
    pl.read_parquet(partition).with_columns(pl.col('high') + 1).write_parquet(partition)
    factors.with_columns(pl.lit(1.02).alias('ex_factor')).write_parquet(factor_path)
    monkeypatch.setattr(inputs, 'cn_now', lambda: state.now + timedelta(minutes=15))
    revised = inputs.save_ready(state.root, state.day, state.coverage, state.observations, state.market)
    loaded = inputs.load_inputs(state.root, state.day, revised)
    assert loaded.prices.filter(pl.col('date') == prior_day)['known_at'].unique().to_list() == [revised['cutoff']]
    assert loaded.provenance['wave_factor_known_at'] == revised['cutoff']
    assert loaded.provenance['wave_factor_facts'][state.symbols[0]][str(prior_day)] == [1.02, revised['cutoff']]
    [earlier], _ = inputs._accepted_history(store, store.acceptance_references(), revised, prior_cutoff, {})
    assert earlier['cutoff'] == evidence['cutoff']
    assert earlier['facts'] == evidence['wave_factor_facts']
    assert earlier['source'] == inputs.factor_file_reference(evidence['versions'])


def test_automatic_generation_rejects_cross_day_before_any_publication(accepted, monkeypatch):
    state = accepted
    monkeypatch.setattr(jobs, 'cn_now', lambda: state.now + timedelta(days=1))
    with pytest.raises(DailyCoverageError, match='跨日期'):
        jobs.generate(state.repo, state.day, progress=lambda *a, **k: None)
    assert not EveningReportStore(state.root).dates()


def test_exact_non_industry_mapping_excludes_small_region_but_keeps_real_industry_labels():
    rows = [{'symbol': f's{i}', 'sector_id': label, 'sector_name': label, 'source': 'concept'}
            for label in ('天津自贸区', '沪股通', '沪深300', '2026三季报预增', '昨日涨停', '智慧城市', '东数西算', '中国AI 50')
            for i in range(6)]
    frame, evidence = inputs._memberships({'classifications': [{'memberships': rows}],
                                          'sector_filter_config': {'min_members': 4, 'max_members': 600, 'blacklist': []}})
    assert set(frame['sector_id']) == {'智慧城市', '东数西算', '中国AI 50'}
    assert evidence['excluded_counts'] == {'region': 1, 'trading_channel': 1, 'index_constituent': 1, 'style': 1, 'review_attribute': 1}
    assert evidence['rule_version'] == 'evening-classification-v2'


@pytest.mark.parametrize('automatic', [False, True])
def test_published_archive_survives_event_failure_and_retry_only_republishes_update(accepted, monkeypatch, automatic):
    from app.services import auth, evening_report_engine, trading_day
    state = accepted
    def fail(*_):
        raise OSError('事件记录暂不可写')
    monkeypatch.setattr(shared_runtime, 'publish_data_update', fail)
    with pytest.raises(OSError, match='事件记录'):
        jobs.generate(state.repo, state.day, progress=lambda *a, **k: None)
    original = EveningReportStore(state.root).original_metadata(str(state.day))
    assert original
    seen = []
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda root, datasets: seen.append(datasets))
    monkeypatch.setattr(evening_report_engine, 'build_report', lambda *a, **k: pytest.fail('仅补通知不得再次计算'))
    monkeypatch.setattr(auth, 'require_current_session', lambda: {'role': 'admin'})
    monkeypatch.setattr(preferences, 'get_pipeline_schedule', lambda: {'hour': 16, 'minute': 0})
    monkeypatch.setattr(trading_day, 'is_trading_day', lambda *_a, **_k: True)
    monkeypatch.setattr(jobs, '_queue_tracking', AsyncMock())

    def retry():
        if automatic:
            asyncio.run(jobs.run_scheduled(state.repo))
            return
        return asyncio.run(jobs.trigger(state.repo, day=state.day))

    result = retry()
    if not automatic:
        assert result['version'] == original['version'] and result['reused']
    assert EveningReportStore(state.root).original_metadata(str(state.day))['version'] == original['version']
    assert seen == [['evening_report']]
    asyncio.run(jobs.after_job(state.repo, {'id': 'finished', 'request_key': f'evening_report:{state.day}:live:test',
                                          'status': 'succeeded', 'finished_at': state.now.isoformat()}))
    retry()
    assert seen == [['evening_report']]


def test_capacity_queued_evening_can_cancel_without_computing(task_runtime, monkeypatch):
    from app.services import heavy_job_limiter
    state, registry = task_runtime
    limiter = heavy_job_limiter.HeavyJobLimiter(2, cancel_poll_interval=.005)
    monkeypatch.setattr(heavy_job_limiter, 'shared_heavy_job_limiter', limiter)
    assert limiter.acquire('normal', timeout=0)
    async def run():
        result = await jobs.trigger(state.repo, day=state.day, automatic=True)
        for _ in range(200):
            if registry.get(result['job_id'])['log']:
                break
            await asyncio.sleep(.005)
        assert registry.get(result['job_id'])['status'] == 'pending'
        pipeline.cancel_job(result['job_id'])
        await drain_tasks()
        return result
    try:
        result = asyncio.run(run())
    finally:
        limiter.release('normal')
    assert registry.get(result['job_id'])['outcome'] == 'cancelled'
    assert jobs.status(state.root, str(state.day))['status'] == 'cancelled'
    assert not EveningReportStore(state.root).has_original(str(state.day))
    assert pipeline_jobs._run_slot_owner is None


@pytest.mark.parametrize('missing_input', [False, True])
def test_explicit_rebuild_revalidates_revoked_session_before_delayed_execution(task_runtime, monkeypatch, missing_input):
    from app.jobs import daily_pipeline
    from app.services.auth import AuthStore
    from app.services.user_context import UserContext, user_scope
    state, registry = task_runtime
    if missing_input:
        EveningReportStore(state.root).save_readiness(str(state.day), {'complete': False})
    prepared = Mock()
    monkeypatch.setattr(daily_pipeline, 'run_now', prepared)
    auth = AuthStore(state.root)
    admin = auth.bootstrap('reviewadmin', 'initial-secret')
    token, _ = auth.login('reviewadmin', 'initial-secret', 'ip', '')
    actor = UserContext(admin['id'], 'admin', state.root, session_token=token)
    async def run():
        with user_scope(actor):
            result = await jobs.trigger(state.repo, day=state.day, capset=object())
            auth.revoke_session(token)
            await drain_tasks()
            return result
    result = asyncio.run(run())
    assert registry.get(result['job_id'])['status'] == 'failed'
    assert '会话已过期' in registry.get(result['job_id'])['error']
    assert not EveningReportStore(state.root).has_original(str(state.day))
    prepared.assert_not_called()


def test_historical_request_returns_existing_original_or_unpublished_without_submitting(manual_runtime, monkeypatch):
    state, _, _, _ = manual_runtime
    store = EveningReportStore(state.root)
    submit = AsyncMock()
    monkeypatch.setattr(pipeline, '_submit_job', submit)
    monkeypatch.setattr(jobs, 'cn_now', lambda: state.now + timedelta(days=1))
    date_key = str(state.day)
    missing = asyncio.run(jobs.trigger(state.repo, day=state.day))
    assert missing['status'] == 'unpublished'
    assert not store.has_original(date_key)
    original = store.save(date_key, {'audit': {}, 'state': {}}, {'input_id': 'a' * 32})
    reused = asyncio.run(jobs.trigger(state.repo, day=state.day))
    assert reused['status'] == 'published' and reused['version'] == original['version']
    with pytest.raises(HTTPException) as exc:
        asyncio.run(jobs.trigger(state.repo, day=state.day, revision=True))
    assert exc.value.status_code == 409
    submit.assert_not_awaited()


def test_limit_reference_requires_proof_for_each_intervening_session(accepted):
    state = accepted
    symbol = state.symbols[0]
    first, second, resumed = state.calendar[3:6]
    for day in (first, second):
        path = state.root / 'kline_daily' / f'date={day}' / 'part.parquet'
        pl.read_parquet(path).filter(pl.col('symbol') != symbol).write_parquet(path)

    def signals(halts, instruments=state.metadata):
        proofs = [{'symbol': symbol, 'date': d, 'confirmed': True} for d in halts]
        frame = inputs._prices(inputs.SectorReviewReader(state.root), state.day, instruments,
                               calendar=state.calendar, accepted_halts=proofs)
        return frame.filter((pl.col('date') == resumed) & (pl.col('symbol') == symbol)).select(
            'signal_limit_up', 'signal_limit_down', 'consecutive_limit_ups').row(0)

    assert signals([first]) == (None, None, None)
    assert signals([first, second]) == (False, False, 0)
    close = state.raw.filter((pl.col('symbol') == symbol) & (pl.col('date') == resumed))['close'].item()
    bounds = state.metadata.with_columns(pl.lit(resumed).alias('as_of'), pl.lit(close).alias('limit_up'))
    assert signals([], bounds) == (True, None, None)


@pytest.mark.parametrize(('now', 'allowed'), [('2026-11-20T10:00', False), ('2026-11-20T16:00', True),
                                               ('2026-11-22T17:00', False), ('2026-11-23T17:00', True)])
def test_generation_uses_close_time_and_confirmed_calendar_range(tmp_path, monkeypatch, now, allowed):
    instant = datetime.fromisoformat(now).replace(tzinfo=CN_TZ)
    monkeypatch.setattr(jobs, 'cn_now', lambda: instant)
    atomic_json(tmp_path / 'calendar' / 'trading_days.json', {
        'days': ['2026-11-20'], 'start': '2026-11-20', 'end': '2026-11-22'})
    status = jobs.status(tmp_path, str(instant.date()))
    assert status['can_generate'] is allowed
    if not allowed:
        repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(jobs.trigger(repo, day=instant.date(), automatic=True))
        assert (exc.value.status_code, exc.value.detail) == (409, status['reason'])


def test_accepted_daily_bounds_survive_metadata_rollover_without_prior_formal_close(market_batch, monkeypatch):
    state = market_batch
    up, down = state.symbols[:2]
    folder = state.root / 'adj_factor'
    folder.mkdir(exist_ok=True)
    pl.DataFrame(schema={'symbol': pl.String, 'trade_date': pl.Date, 'ex_factor': pl.Float64}).write_parquet(folder / 'all.parquet')
    earlier, prior, today = state.calendar[-3:]
    raw = state.raw.filter(~((pl.col('symbol') == up) & (pl.col('date') < prior))).with_columns(
        pl.when((pl.col('symbol') == down) & (pl.col('date') == earlier)).then(pl.lit(123, dtype=pl.Int64)).alias('quote_ts'))
    write(state.root, 'kline_daily', raw)
    run_pipeline(state.root)
    for day in (prior, today):
        now = datetime.combine(day, state.now.timetz())
        monkeypatch.setattr(inputs, 'cn_now', lambda at=now: at)
        rows = raw.filter(pl.col('date') == day)
        bounds = rows.select('symbol', pl.col('close').alias('bound'))
        metadata = state.metadata.join(bounds, on='symbol').with_columns(
            pl.lit(day).alias('as_of'), pl.when(pl.col('symbol') == up).then('bound').alias('limit_up'),
            pl.when(pl.col('symbol') == down).then('bound').alias('limit_down')).drop('bound')
        metadata.write_parquet(state.root / 'instruments' / 'instruments.parquet')
        coverage = DailyCoverage(day)
        coverage.observe(rows)
        receipt = inputs.save_ready(state.root, day, coverage,
                                    [dict(o, obtained_at=now.isoformat()) for o in state.observations], state.market)
        report_inputs = inputs.load_inputs(state.root, day, receipt)
        loaded = report_inputs.prices.filter(pl.col('date') == prior)
        assert loaded.filter(pl.col('symbol') == up).select('signal_limit_up', 'consecutive_limit_ups').row(0) == (True, None)
        assert loaded.filter(pl.col('symbol') == down)['signal_limit_down'].item()
        inputs.prepare_evaluation_inputs(report_inputs)
        history = report_inputs.prices.filter(pl.col('date') == prior)
        assert history.filter(pl.col('symbol') == up)['signal_limit_up'].item()
        assert history.filter(pl.col('symbol') == down)['signal_limit_down'].item()


def test_indicator_windows_recover_independently_after_an_unexplained_gap(accepted):
    state = accepted
    symbol, peer = state.symbols[:2]
    gap = state.calendar[-8]
    path = state.root / 'kline_daily' / f'date={gap}' / 'part.parquet'
    pl.read_parquet(path).filter(pl.col('symbol') != symbol).write_parquet(path)
    fields = ['ma5', 'ma10', 'ma20', 'high_60d', 'atr_14', 'bull_bear_line']
    for confirmed in (False, True):
        halts = [{'symbol': symbol, 'date': gap, 'confirmed': confirmed}]
        prices = inputs._prices(inputs.SectorReviewReader(state.root), state.day, state.metadata,
                                calendar=state.calendar, accepted_halts=halts).filter(pl.col('date') == state.day)
        actual = prices.filter(pl.col('symbol') == symbol).select(fields).row(0)
        assert [v is not None for v in actual] == [True, *([confirmed] * 5)]
        assert all(v is not None for v in prices.filter(pl.col('symbol') == peer).select(fields).row(0))
