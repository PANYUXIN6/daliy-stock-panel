"""Real narrow-table inputs, immutable publication, recovery and HTTP boundaries."""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import polars as pl
import pytest
from fastapi import FastAPI
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
from app.services.evening_report_store import EveningReportStore, RevisionBranch, atomic_json


def write(root, table, frame):
    for key, part in frame.partition_by('date', as_dict=True).items():
        path = root / table / f'date={key[0]}' / 'part.parquet'
        path.parent.mkdir(parents=True, exist_ok=True)
        part.write_parquet(path)


@pytest.fixture
def accepted(private_owner, tmp_path, monkeypatch):
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
    evidence = inputs.save_ready(tmp_path, day, coverage, observations,
                                {'phase': 'repair', 'state': 'range', 'promo_rate': .3, 'seal_rate': .8})
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: None)
    return SimpleNamespace(root=tmp_path, day=day, calendar=calendar, raw=raw, symbols=symbols,
                           metadata=metadata, evidence=evidence, coverage=coverage, repo=repo, now=now)


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


def test_historical_snapshot_invalidates_recursive_state_even_when_today_is_formal(accepted):
    state = accepted
    old = state.calendar[3]
    path = state.root / 'kline_daily' / f'date={old}' / 'part.parquet'
    pl.read_parquet(path).with_columns(pl.when(pl.col('symbol') == state.symbols[0]).then(pl.lit(123, dtype=pl.Int64)).otherwise(None).alias('quote_ts')).write_parquet(path)
    prices = inputs._prices(inputs.SectorReviewReader(state.root), state.day, state.metadata)
    assert not prices.filter(pl.col('symbol') == state.symbols[0])['formal'].tail(1)[0]
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
                           suspension_evidence=evidence)
    assert prices.filter(pl.col('symbol') == state.symbols[0])['formal'].all()
    assert prices.filter((pl.col('symbol') == state.symbols[0]) & (pl.col('date') == day)).is_empty()
    assert evidence == [{'symbol': state.symbols[0], 'date': day}]
    metadata = inputs._metadata(state.root, state.evidence, [str(d) for d in state.calendar], evidence)
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
    jobs.generate(state.repo, state.day, revision=False, progress=lambda *a, **k: None)
    report = EveningReportStore(state.root).get(str(state.day))
    assert report['scope_version'] == report['provenance']['scope_version'] == updated['scope_version']


@pytest.mark.parametrize('revision', [False, True])
def test_generation_restarts_classification_continuity_and_reuses_matching_daily_scopes(tmp_path, monkeypatch, revision):
    from test_evening_report_engine import inputs_at

    store = EveningReportStore(tmp_path)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    values = {value.trade_date: value for value in (inputs_at(index) for index in range(65, 71))}
    days = list(values)
    new_scope = SCOPE_VERSION + '/fuyao-flat-v1'
    for index, day in enumerate(days):
        store.save_readiness(day, {
            'date': day, 'complete': True, 'cutoff': day, 'versions': {}, 'classifications': [],
            'calendar': {'days': days}, 'scope_version': SCOPE_VERSION if index < 3 else new_scope,
        })

    def ready(root, day, *, input_id=None):
        return store.input_evidence(input_id) if input_id else store.readiness(str(day))

    def load(root, day, evidence, check):
        value = values[str(day)]
        value.provenance['scope_version'] = evidence['scope_version']
        return value

    monkeypatch.setattr(jobs, 'require_ready', ready)
    monkeypatch.setattr(jobs, 'load_inputs', load)
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *_: None)
    for day in days[:3]:
        jobs.generate(repo, date.fromisoformat(day), revision=False, progress=lambda *a, **k: None)
    previous = store.get(days[2])
    assert any(identity['valid'] is True for identity in previous['state']['identities'].values())

    if revision:
        # Reproduce archives produced by the faulty entry point: new input IDs
        # were saved with the old scope, so matching input IDs alone cannot reuse them.
        for day in days[3:]:
            payload = build_report(values[day], store.history(day, RULE_VERSION))
            store.save(day, payload, {'input_id': store.readiness(day)['input_id']})
        old_branch = RevisionBranch(store)
        for day in days[2:]:
            payload = store.get(day)
            if day in days[4:]:
                payload['scope_version'] = new_scope
            old_branch.append(day, payload, payload['input_reference'])
        old_branch.commit()
        # The last day matches, but the first switched day does not. A branch
        # spanning the source change must compare every date, not its last scope.
        original_reports = [store.get(day) for day in days]
        result = jobs.generate(repo, date.fromisoformat(days[2]), revision=True, progress=lambda *a, **k: None)
        assert not result['reused']
        manifest = next(item for item in store.branch_manifests() if item['branch_id'] == result['branch_id'])
        reports = [store.get(day, manifest['dates'][day]['version']) for day in days[3:]]
        assert [store.get(day) for day in days] == original_reports
        repeated = jobs.generate(repo, date.fromisoformat(days[2]), revision=True, progress=lambda *a, **k: None)
        assert repeated['reused'] and repeated['version'] == result['version']
    else:
        for day in days[3:]:
            jobs.generate(repo, date.fromisoformat(day), revision=False, progress=lambda *a, **k: None)
        reports = [store.get(day) for day in days[3:]]

    first, second, third = reports
    assert all(report['scope_version'] == new_scope for report in reports)
    assert first['sectors'][0]['round']['status'] == 'probing'
    assert not any(identity['valid'] is True for identity in first['state']['identities'].values())
    assert first['statistics']['publication_frequency']['report_days'] == 1
    assert second['sectors'][0]['round']['status'] == 'active'
    assert third['statistics']['publication_frequency']['report_days'] == 3
    assert any(identity['valid'] is True for identity in third['state']['identities'].values())
    assert store.get(days[2]) == previous


def test_concept_filter_uses_original_members_and_never_filters_industry():
    rows = [{'symbol': str(i), 'sector_id': '宽基', 'sector_name': '宽基', 'source': 'concept'} for i in range(8)]
    rows += [{'symbol': 'x', 'sector_id': '宽基', 'sector_name': '宽基', 'source': 'industry'}]
    frame, evidence = inputs._memberships({'classifications': [{'memberships': rows}],
                                          'sector_filter_config': {'min_members': 1, 'max_members': 7, 'blacklist': []}})
    assert frame['source'].to_list() == ['industry']
    assert evidence['excluded'][0]['members'] == 8


def test_original_is_immutable_and_public_view_does_not_load_internal_archive(tmp_path, monkeypatch):
    store = EveningReportStore(tmp_path)
    original = store.save('2026-10-16', {'trade_date': '2026-10-16', 'rule_version': RULE_VERSION,
                         'state': {'secret_internal': 'x' * 100_000}, 'records': [], 'sectors': [],
                         'calculations': [{'all_features': 'x' * 100_000}]}, {'input_id': 'a' * 32})
    assert store.save('2026-10-16', {'state': 'replaced'}, {'input_id': 'b' * 32}) == original
    original_read = archive.read_json
    def read(path):
        if path.parent.name == 'archive' and not path.name.endswith('.view.json'):
            raise AssertionError('GET不应解析内部恢复档案')
        return original_read(path)
    monkeypatch.setattr(archive, 'read_json', read)
    view = store.get('2026-10-16', view=True)
    assert 'state' not in view and 'calculations' not in view
    assert len(json.dumps(view)) < 1000
    assert store.dates()[0]['versions'][0]['version'] == original['version']


def test_revision_is_invisible_until_whole_branch_commits_and_cancel_discards(tmp_path):
    store = EveningReportStore(tmp_path)
    for day in ('2026-10-15', '2026-10-16'):
        store.save(day, {'rule_version': RULE_VERSION, 'state': {'core': True}}, {'input_id': 'a' * 32})
    branch = RevisionBranch(store)
    branch.append('2026-10-15', {'rule_version': RULE_VERSION, 'state': {'core': False}}, {'input_id': 'b' * 32})
    assert all(len(day['versions']) == 1 for day in store.dates())
    branch.close()
    assert not branch.root.exists()
    branch = RevisionBranch(store)
    first = branch.append('2026-10-15', {'rule_version': RULE_VERSION, 'state': {'core': False}}, {'input_id': 'b' * 32})
    branch.append('2026-10-16', {'rule_version': RULE_VERSION, 'state': {'core': False}}, {'input_id': 'c' * 32})
    branch.commit()
    branch.close()
    assert all(len(day['versions']) == 2 for day in store.dates())
    assert store.get('2026-10-15')['state']['core'] is True
    assert store.get('2026-10-15', first['version'])['state']['core'] is False


def test_generation_archives_once_and_repeated_input_does_not_compute(accepted, monkeypatch):
    from app.services import evening_report_engine
    events = []
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda root, datasets: events.append(datasets))
    state = accepted
    first = jobs.generate(state.repo, state.day, revision=False, progress=lambda *a, **k: None)
    monkeypatch.setattr(evening_report_engine, 'build_report', lambda *a, **k: pytest.fail('同输入不得再次推进状态'))
    second = jobs.generate(state.repo, state.day, revision=False, progress=lambda *a, **k: None)
    assert first['version'] == second['version'] and second['reused']
    assert events == [['evening_report'], ['evening_report']]
    assert EveningReportStore(state.root).get(str(state.day), view=True)['statistics']['scored_count'] == 24


def test_revision_recomputes_forward_without_borrowing_original_future_states(tmp_path, monkeypatch):
    from app.services import evening_report_engine
    store = EveningReportStore(tmp_path)
    days = [date(2026, 10, d) for d in (14, 15, 16)]
    for index, day in enumerate(days):
        store.save(str(day), {'trade_date': str(day), 'rule_version': RULE_VERSION,
                              'state': {'core': index > 0}}, {'input_id': str(index) * 32})
    plan = [(day, chr(97 + index) * 32) for index, day in enumerate(days[1:])]
    monkeypatch.setattr(jobs, '_revision_plan', lambda *_: (plan, []))
    monkeypatch.setattr(jobs, 'require_ready', lambda root, day, input_id=None: {
        'input_id': input_id, 'date': str(day), 'cutoff': str(day), 'versions': {}, 'classifications': [],
        'scope_version': SCOPE_VERSION})
    monkeypatch.setattr(jobs, 'load_inputs', lambda root, day, evidence, check: SimpleNamespace(trade_date=str(day), provenance={}))
    observed = []
    def calculate(value, history, cancel_check):
        assert all(not old['state']['core'] for old in history)
        observed.append([old['trade_date'] for old in history])
        return {'trade_date': value.trade_date, 'rule_version': RULE_VERSION, 'state': {'core': False}}
    monkeypatch.setattr(evening_report_engine, 'build_report', calculate)
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *_: None)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    result = jobs.generate(repo, days[1], revision=True, progress=lambda *a, **k: None)
    assert observed == [['2026-10-14'], ['2026-10-14', '2026-10-15']]
    manifest = store.branch_manifests()[0]
    assert result['branch_id'] == manifest['branch_id']
    assert store.get('2026-10-16')['state']['core'] is True
    revised = store.get('2026-10-16', manifest['dates']['2026-10-16']['version'])
    assert revised['state']['core'] is False


def test_revision_cancel_after_first_staged_date_exposes_no_new_version(tmp_path, monkeypatch):
    from app.services import evening_report_engine
    store = EveningReportStore(tmp_path)
    days = [date(2026, 10, d) for d in (15, 16)]
    for index, day in enumerate(days):
        store.save(str(day), {'trade_date': str(day), 'rule_version': RULE_VERSION, 'state': {}}, {'input_id': str(index) * 32})
    monkeypatch.setattr(jobs, '_revision_plan', lambda *_: ([(day, chr(97 + i) * 32) for i, day in enumerate(days)], []))
    monkeypatch.setattr(jobs, 'require_ready', lambda root, day, input_id=None: {
        'input_id': input_id, 'date': str(day), 'cutoff': str(day), 'versions': {}, 'classifications': [],
        'scope_version': SCOPE_VERSION})
    def load(root, day, evidence, check):
        if day == days[1]:
            raise pipeline_jobs.JobCancelledError('cancel')
        return SimpleNamespace(trade_date=str(day), provenance={})
    monkeypatch.setattr(jobs, 'load_inputs', load)
    monkeypatch.setattr(evening_report_engine, 'build_report', lambda value, **kw: {'trade_date': value.trade_date, 'state': {}})
    with pytest.raises(pipeline_jobs.JobCancelledError):
        jobs.generate(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)), days[0], revision=True,
                      progress=lambda *a, **k: None)
    assert not store.branch_manifests()
    assert all(len(entry['versions']) == 1 for entry in store.dates())
    assert not list((store.root / 'revisions').glob('*'))


def test_history_reads_only_seven_state_files_regardless_of_archive_age(tmp_path, monkeypatch):
    store = EveningReportStore(tmp_path)
    for offset in range(250):
        day = date(2025, 1, 1) + timedelta(days=offset)
        store.save(str(day), {'trade_date': str(day), 'rule_version': RULE_VERSION,
                              'state': {'forward': offset}, 'records': [], 'calculations': ['unused' * 1000]},
                   {'input_id': f'{offset:032x}'})
    read = archive.read_json
    state_reads = []
    def watched(path):
        if path.parent.name == 'archive':
            assert path.name.endswith('.state.json')
            state_reads.append(path)
        return read(path)
    monkeypatch.setattr(archive, 'read_json', watched)
    history = store.history('2026-01-01', RULE_VERSION)
    assert len(history) == len(state_reads) == 7
    assert history[-1]['state'] == {'forward': 249}
    assert len(json.dumps(history)) < 4000


def test_status_distinguishes_incomplete_ready_and_completed_job_without_callback(tmp_path, monkeypatch):
    store = EveningReportStore(tmp_path)
    store.save_readiness('2026-10-16', {'complete': False, 'reason': '分类尚缺证券'})
    assert jobs.status(tmp_path, '2026-10-16')['status'] == 'waiting'
    assert jobs.status(tmp_path, '2026-10-16')['reason'] == '分类尚缺证券'
    registry = pipeline_jobs.JobStore(store_dir=tmp_path / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', registry)
    job_id, _ = registry.create(request_key='evening_report:2026-10-16:live:v1:x')
    jobs._save_attempt(tmp_path, '2026-10-16', 'live', {'job_id': job_id, 'status': 'generating'})
    store.save('2026-10-16', {'state': {}}, {'input_id': 'a' * 32})
    registry.succeed(job_id, {})
    assert jobs.status(tmp_path, '2026-10-16')['status'] == 'published'


def test_revision_failure_does_not_turn_into_automatic_original_retry(tmp_path, monkeypatch):
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
    store.save(str(day), {'state': {}}, {'input_id': 'a' * 32})
    jobs._save_attempt(tmp_path, str(day), 'revision', {'status': 'failed', 'reason': '历史输入已改写'})
    trigger = AsyncMock()
    monkeypatch.setattr(jobs, 'trigger', trigger)
    assert asyncio.run(jobs.run_scheduled(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))) is None
    trigger.assert_not_awaited()
    assert jobs.status(tmp_path, str(day))['status'] == 'failed'
    assert jobs.status(tmp_path, str(day), mode='live')['status'] == 'published'


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
    jobs._save_attempt(state.root, str(state.day), 'live', {'job_id': job_id, 'status': 'generating'})
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
    assert all(job.get('request_key', '').startswith('evening_report:') for job in restored.list_recent())


def test_same_input_uses_saved_historical_suspension_not_mutable_ready(accepted):
    state = accepted
    previous = str(state.calendar[-2])
    store = EveningReportStore(state.root)
    store.save_readiness(previous, {
        'date': previous, 'complete': True,
        'halted_evidence': [{'symbol': state.symbols[0], 'date': previous, 'confirmed': True,
                             'source': 'explicit_daily_status', 'obtained_at': previous + 'T16:00:00+08:00'}],
        'halted_symbols': [state.symbols[0]], 'classifications': state.evidence['classifications'],
    })
    evidence = inputs.save_ready(state.root, state.day, state.coverage, state.evidence['classifications'], state.evidence['market'])
    before = inputs._metadata(state.root, evidence, [str(d) for d in state.calendar], [])
    store.save_readiness(previous, {'date': previous, 'complete': True, 'halted_evidence': [],
                                    'halted_symbols': [], 'classifications': []})
    after = inputs._metadata(state.root, evidence, [str(d) for d in state.calendar], [])
    assert before.equals(after)
    assert after.filter(pl.col('symbol') == state.symbols[0])['suspension_window'][0] is True
    assert store.input_evidence(evidence['input_id'])['historical_suspensions']


def test_automatic_generation_rejects_cross_day_before_any_publication(accepted, monkeypatch):
    state = accepted
    monkeypatch.setattr(jobs, 'cn_now', lambda: state.now + timedelta(days=1))
    with pytest.raises(ValueError, match='跨日期'):
        jobs.generate(state.repo, state.day, revision=False, progress=lambda *a, **k: None, automatic=True)
    assert not EveningReportStore(state.root).dates()


def test_exact_non_industry_mapping_excludes_small_region_but_keeps_real_industry_labels():
    rows = [{'symbol': f's{i}', 'sector_id': label, 'sector_name': label, 'source': 'concept'}
            for label in ('天津自贸区', '沪股通', '沪深300', '2026三季报预增', '智慧城市', '东数西算', '中国AI 50')
            for i in range(6)]
    frame, evidence = inputs._memberships({'classifications': [{'memberships': rows}],
                                          'sector_filter_config': {'min_members': 4, 'max_members': 600, 'blacklist': []}})
    assert set(frame['sector_id']) == {'智慧城市', '东数西算', '中国AI 50'}
    assert evidence['excluded_counts'] == {'region': 1, 'trading_channel': 1, 'index_constituent': 1, 'style': 1}
    assert evidence['rule_version'] == 'evening-classification-v1'


def test_published_archive_survives_event_failure_and_retry_only_republishes_update(accepted, monkeypatch):
    from app.services import evening_report_engine
    state = accepted
    def fail(*_):
        raise OSError('事件记录暂不可写')
    monkeypatch.setattr(shared_runtime, 'publish_data_update', fail)
    with pytest.raises(OSError, match='事件记录'):
        jobs.generate(state.repo, state.day, revision=False, progress=lambda *a, **k: None)
    original = EveningReportStore(state.root).original_metadata(str(state.day))
    assert original
    seen = []
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda root, datasets: seen.append(datasets))
    monkeypatch.setattr(evening_report_engine, 'build_report', lambda *a, **k: pytest.fail('仅补通知不得再次计算'))
    result = jobs.generate(state.repo, state.day, revision=False, progress=lambda *a, **k: None)
    assert result['version'] == original['version'] and result['reused']
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


def test_explicit_rebuild_revalidates_revoked_session_before_delayed_execution(task_runtime):
    from app.services.auth import AuthStore
    from app.services.user_context import UserContext, user_scope
    state, registry = task_runtime
    auth = AuthStore(state.root)
    admin = auth.bootstrap('reviewadmin', 'initial-secret')
    token, _ = auth.login('reviewadmin', 'initial-secret', 'ip', '')
    actor = UserContext(admin['id'], 'admin', state.root, session_token=token)
    async def run():
        with user_scope(actor):
            result = await jobs.trigger(state.repo, day=state.day)
            auth.revoke_session(token)
            await drain_tasks()
            return result
    result = asyncio.run(run())
    assert registry.get(result['job_id'])['status'] == 'failed'
    assert '会话已过期' in registry.get(result['job_id'])['error']
    assert not EveningReportStore(state.root).has_original(str(state.day))


def test_selection_policy_revision_recomputes_same_inputs_and_preserves_original(accepted, monkeypatch):
    from app.services import evening_report_rules
    state = accepted
    store = EveningReportStore(state.root)
    current_policy = evening_report_rules.SELECTION_POLICY_VERSION
    assert current_policy == 'observation-v3'
    submitted = AsyncMock(return_value={'job_id': 'queued'})
    monkeypatch.setattr(pipeline, '_submit_job', submitted)
    monkeypatch.setattr(shared_runtime, 'publish_data_update', lambda *_: None)
    monkeypatch.setattr(evening_report_rules, 'SELECTION_POLICY_VERSION', 'observation-v2')
    first = jobs.generate(state.repo, state.day, revision=False, progress=lambda *a, **k: None)
    original = store.get(str(state.day))
    asyncio.run(jobs.trigger(state.repo, day=state.day, revision=True, automatic=True))
    old_key = submitted.call_args.kwargs['request_key']
    evidence = store.readiness(str(state.day))
    # A v2 archive and revision with the same inputs cannot satisfy v3.
    old_payload = {**original, 'scope_version': SCOPE_VERSION}
    old_branch = RevisionBranch(store)
    old_branch.append(str(state.day), old_payload, original['input_reference'])
    old_branch.commit()
    monkeypatch.setattr(evening_report_rules, 'SELECTION_POLICY_VERSION', current_policy)
    asyncio.run(jobs.trigger(state.repo, day=state.day, revision=True, automatic=True))
    assert submitted.call_args.kwargs['request_key'] != old_key
    monkeypatch.setattr(jobs, '_revision_plan', lambda *_: ([(state.day, evidence['input_id'])], []))
    result = jobs.generate(state.repo, state.day, revision=True, progress=lambda *a, **k: None)
    assert not result['reused'] and result['version'] != first['version']
    revised = store.get(str(state.day), result['version'], view=True)
    assert revised['selection_policy_version'] == current_policy
    manifest = next(item for item in store.branch_manifests() if item['branch_id'] == result['branch_id'])
    assert manifest['selection_policy_version'] == current_policy
    assert manifest['dates'][str(state.day)]['selection_policy_version'] == current_policy
    assert store.get(str(state.day)) == original
    repeated = jobs.generate(state.repo, state.day, revision=True, progress=lambda *a, **k: None)
    assert repeated['reused'] and repeated['version'] == result['version']
