"""Real publication/retention contracts for tiered evening storage."""
from __future__ import annotations

import copy
import gzip
import json
import os
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import polars as pl
import pytest
from evening_receipt_fixture import current_receipt
from test_evening_report_engine import inputs_at
from test_evening_report_observation import observation_inputs

from app.market_time import CN_TZ
from app.services import evening_report_jobs as jobs
from app.services import evening_report_store as storage
from app.services import evening_report_tracking as tracking
from app.services import pipeline_jobs
from app.services.daily_coverage import DailyCoverageError
from app.services.evening_report_archive import AUDIT_RETENTION_DAYS, SCHEMA_VERSION
from app.services.evening_report_engine import build_report
from app.services.evening_report_observation import allocate
from app.services.evening_report_rules import RULE_VERSION
from app.services.evening_report_store import (
    EveningReportStore,
    atomic_json,
    history_view,
)


def rejected_inputs():
    inputs = observation_inputs()
    inputs.metadata = inputs.metadata.with_columns(
        pl.when(pl.col('symbol') == '000003.SZ').then(False)
        .otherwise(pl.col('trading_normal')).alias('trading_normal'))
    return inputs


@pytest.mark.parametrize('selection_policy_version', ['theme-observation-v1', 'another-selection-policy'])
def test_three_tiers_keep_comparable_snapshots_and_audit_denominators(tmp_path, private_owner, selection_policy_version):
    inputs = rejected_inputs()
    payload = build_report(inputs)
    payload['selection_policy_version'] = selection_policy_version
    original = copy.deepcopy(payload)
    store = EveningReportStore(tmp_path)
    saved = store.save(inputs.trade_date, payload, {'input_id': 'a' * 32})
    assert payload == original  # Projection cannot mutate the live continuation state.
    assert saved['storage_version'] == SCHEMA_VERSION
    assert saved['selection_policy_version'] == selection_policy_version
    assert saved == store.get(inputs.trade_date)
    expected = {r['symbol']: r for r in payload['calculations']
                if r['selected'] or r['observation']['decision']['comparison_eligible']}
    assert {r['symbol'] for r in saved['records']} == set(expected)
    for row in saved['records']:
        assert row['observation'] == expected[row['symbol']]['observation']
    assert len(store.get(inputs.trade_date, view=True)['records']) == 15
    assert len(saved['records']) == 19
    audit = store.audit_rows(inputs.trade_date, saved['version'])
    assert len(audit) == 20
    rejected = next(r for r in audit if r['symbol'] == '000003.SZ')
    assert rejected['tier'] == 'audit_only'
    assert rejected['decision']['first_blocking_step'] == 'hard_validation'
    assert rejected['hard_checks']['正常交易'] is False
    assert 'variables' not in rejected and 'features' not in rejected
    assert set(rejected['not_computed_features']) == {'price_gaps', 'abnormal_move'}
    assert rejected['missing_features']
    assert saved['audit']['step_counts']['hard_validation']['failed'] == 1

    # The stored allocation pool can reproduce its ranking with no market input.
    candidates = copy.deepcopy(saved['records'])
    for row in candidates:
        row['selected'] = False
    assert allocate(candidates, [s['observation'] for s in saved['sectors']]) == payload['selected']
    boundary = store.candidate_boundary(inputs.trade_date, saved['version'])
    assert {r['symbol'] for r in boundary['records']} == set(expected)
    assert all(r['quota_limit'] in (1, 2) for r in boundary['records'])
    assert all(r['snapshot_ref'].startswith(saved['version'] + ':') for r in boundary['records'])

    tracking.refresh_tracking(tmp_path, inputs)
    history = tracking.audit_history(tmp_path, saved['version'])
    assert len(history['records']) == 20
    analysis = history['analyses'][0]['payload']
    assert analysis['audit_missingness']['return_20d']['groups']['all_unselected']['sample_count'] == 5
    assert analysis['windows']['1']['not_due'] == 19
    assert next(r for r in history['records'] if r['symbol'] == '000003.SZ')['performance'] == {}

    path = store.root / 'archive' / f"{saved['version']}.json.gz"
    with gzip.open(path, 'rt') as stream:
        disk = json.load(stream)
    assert 'state' not in disk and 'calculations' not in disk
    assert all('scores' not in sector for sector in disk['sectors'])
    assert 'values' not in disk['records'][0]['observation']
    assert disk['variable_definitions']['return_20d']['as_of'] == inputs.trade_date
    assert not (store.root / 'diagnostics').exists()


def test_retention_preserves_feedback_candidates_and_price_evidence(tmp_path, private_owner):
    inputs = rejected_inputs()
    store = EveningReportStore(tmp_path)
    saved = store.save(inputs.trade_date, build_report(inputs), {'input_id': 'a' * 32})
    tracking.refresh_tracking(tmp_path, inputs)
    symbol = saved['selected'][0]
    feedback = tracking.update_feedback(tmp_path, day=inputs.trade_date, version=saved['version'],
                                        symbol=symbol, starred=True, outcome='profit', expected_revision=0,
                                        request_id='retention_feedback_001')
    history_before = tracking.personal_history(tmp_path)
    before = date.fromisoformat(inputs.trade_date) + timedelta(days=AUDIT_RETENTION_DAYS)
    store.prune(before)
    assert store.audit_rows(inputs.trade_date, saved['version'])
    today = before + timedelta(days=1)
    result = store.prune(today)
    assert result['removed_files'] == 2  # only medium-term audit and pool boundary
    assert tracking.prune_tracking(tmp_path, today)['removed_audit_rows'] == 1
    assert store.audit_rows(inputs.trade_date, saved['version']) == []
    assert store.candidate_boundary(inputs.trade_date, saved['version']) is None
    assert store.get(inputs.trade_date) == saved
    assert store.state(inputs.trade_date)  # still the latest operating state
    assert tracking.personal_history(tmp_path) == history_before
    actual = next(row['feedback'] for row in tracking.personal_report(tmp_path, inputs.trade_date, saved['version'])['records'] if row['symbol'] == symbol)
    assert actual == {key: value for key, value in feedback.items() if key != 'episode_id'}
    assert len(tracking.audit_history(tmp_path, saved['version'])['records']) == 19
    assert store.prune(today)['removed_files'] == 0


def test_cancelled_database_retention_rolls_back_audit_deletion(tmp_path, private_owner):
    inputs = rejected_inputs()
    store = EveningReportStore(tmp_path)
    saved = store.save(inputs.trade_date, build_report(inputs), {'input_id': 'a' * 32})
    tracking.refresh_tracking(tmp_path, inputs)
    before = tracking.audit_history(tmp_path, saved['version'])
    calls = 0

    def cancel_before_commit():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise pipeline_jobs.JobCancelledError('cancel')

    with pytest.raises(pipeline_jobs.JobCancelledError):
        tracking.prune_tracking(tmp_path, date.fromisoformat(inputs.trade_date)
                                + timedelta(days=AUDIT_RETENTION_DAYS + 1), cancel_before_commit)
    assert tracking.audit_history(tmp_path, saved['version']) == before


def test_rolling_state_keeps_next_day_decisions_identical(tmp_path):
    store = EveningReportStore(tmp_path)
    history = []
    for index in range(60, 69):
        inputs = inputs_at(index)
        payload = build_report(inputs, history[-7:])
        history.append(history_view(payload))
        store.save(inputs.trade_date, payload, {'input_id': f'{index:032x}'})
        store.prune(date.fromisoformat(inputs.trade_date))
    inputs = inputs_at(69)
    actual_history = store.history(inputs.trade_date, RULE_VERSION)
    assert len(actual_history) == 7
    assert len(list((store.root / 'archive').glob('*.state.json.gz'))) == 7
    expected = build_report(inputs, history[-7:])
    actual = build_report(inputs, actual_history)
    assert actual['selected'] == expected['selected']
    assert actual['state'] == expected['state']
    assert actual['statistics'] == expected['statistics']
    older = actual_history[-3]['trade_date']
    version = store.original_metadata(older)['version']
    (store.root / 'archive' / (version + '.state.json.gz')).unlink()
    partial = store.history(inputs.trade_date, RULE_VERSION, allow_partial=True)
    assert [r['trade_date'] for r in partial] == [r['trade_date'] for r in actual_history if r['trade_date'] != older]
    assert build_report(inputs, partial)['selected'] == actual['selected']
    with pytest.raises(DailyCoverageError, match='保留期'):
        store.history(history[2]['trade_date'], RULE_VERSION)


def test_temporary_evidence_and_explicit_diagnostics_expire(tmp_path, monkeypatch):
    today = date(2026, 11, 13)
    old = today - timedelta(days=31)
    now = datetime.combine(old, datetime.min.time(), tzinfo=CN_TZ)
    monkeypatch.setattr(storage, 'cn_now', lambda: now)
    store = EveningReportStore(tmp_path)
    first = current_receipt(old.isoformat(), now.isoformat(), input_id='a' * 32)
    second = current_receipt(today.isoformat(), datetime.combine(today, now.timetz()).isoformat(), input_id='b' * 32)
    store.save_readiness(old.isoformat(), first)
    store.save_readiness(old.isoformat(), {'date': old.isoformat(), 'complete': False})
    path = store.root / 'inputs' / ('a' * 32 + '.json.gz')
    os.utime(path, (now.timestamp(), now.timestamp()))
    store.save_readiness(today.isoformat(), second)
    assert store.input_evidence(first['input_id']) == first
    store.save_diagnostic({'calculations': ['temporary']}, reason='administrator_requested')
    unrelated = tmp_path / 'users' / 'owner' / 'preserve.json'
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text('personal asset')
    store.prune(today)
    assert store.input_evidence(first['input_id']) is None
    assert store.readiness_status(old.isoformat()) is None
    assert store.input_evidence(second['input_id']) == second
    assert store.acceptance_references() == {str(today): [(second['cutoff'], second['input_id'])]}
    assert not list((store.root / 'diagnostics').glob('*.gz'))
    assert unrelated.read_text() == 'personal asset'


def test_incomplete_reference_cannot_replace_accepted_input(tmp_path):
    store = EveningReportStore(tmp_path)
    day = '2026-11-04'
    receipt = current_receipt(day, day + 'T17:00:00+08:00')
    store.save_readiness(day, receipt)
    for fields in ({'date': '2026-11-05'}, {'cutoff': None}):
        with pytest.raises(ValueError, match='交易日和有效截止时间'):
            store.save_readiness(day, {**receipt, **fields})
    assert store.readiness(day) == receipt
    assert store.acceptance_references() == {day: [(receipt['cutoff'], receipt['input_id'])]}


def test_acceptance_reader_rejects_malformed_nested_proofs(tmp_path):
    store = EveningReportStore(tmp_path)
    day, symbol = '2026-11-04', '000001.SZ'
    at = day + 'T17:00:00+08:00'
    receipt = current_receipt(day, at, active_symbols=[symbol], versions={'files': [[f'kline_daily/date={day}/part.parquet', 100, 1]]})
    store.save_readiness(day, receipt)
    assert store.readiness(day) == receipt
    path = store.root / 'inputs' / f"{receipt['input_id']}.json.gz"
    original = storage.read_json(path)
    damage = {
        'active_symbols': [[symbol]], 'halted_symbols': None, 'metadata': [{'is_st': False}],
        'historical_suspensions': [{'date': day}], 'halted_evidence': [{'date': day, 'symbol': [], 'confirmed': True}],
        'versions': {'files': []}, 'calendar': {'days': [None]}, 'wave_price_clocks': {'dates': {}, 'rows': {}, 'pending': {day: {symbol: []}}},
        'wave_first_acceptances': {}, 'wave_price_changes': {symbol: {day: {'volume': 60., 'known_at': []}}},
        'wave_factor_facts': {symbol: {day: [0.0, at]}}, 'wave_st_reentry': {symbol: []},
        'classifications': [{'source': 'industry', 'id': 'test', 'memberships': [{'symbol': symbol}]}],
        'classification_comparisons': [{'source': 'industry', 'comparison': 'first_observation', 'added': [[symbol]]}],
        'previous_accepted_input_id': ['wrong-id'],
    }
    for field in ('wave_first_acceptances', 'wave_price_clocks', 'wave_st_reentry', 'wave_factor_facts',
                  'classification_comparisons'):
        atomic_json(path, {k: v for k, v in original.items() if k != field})
        assert store.readiness(day) is None, field
        assert store.historical_reference(day) == (receipt['input_id'], at), field
    atomic_json(path, {**original, 'wave_price_clocks': {**original['wave_price_clocks'], 'dates': {}}})
    assert store.readiness(day) is None
    for field, invalid in damage.items():
        atomic_json(path, {**original, field: invalid})
        assert store.readiness(day) is None, field
        assert store.historical_reference(day) == (receipt['input_id'], at), field
    # A successfully promoted file still proves its cutoff when its body and both pointers are unavailable.
    (store.root / 'ready' / f'{day}.json').unlink()
    (store.root / 'acceptances' / f'{day}.json').unlink()
    assert store.historical_reference(day, store.acceptance_references()) == (receipt['input_id'], at)
    assert store.input_evidence(receipt['input_id']) is None
    for wrong_day in ([], '2026-11-05'):
        atomic_json(path, {**original, '_acceptance_index_day': wrong_day})
        assert store.input_evidence(receipt['input_id']) is None
        assert store.acceptance_references() == {}
    atomic_json(path, original)
    assert store.input_evidence(receipt['input_id']) == receipt


def test_broken_history_entries_preserve_recognizable_acceptance_references(tmp_path):
    store = EveningReportStore(tmp_path)
    day = '2026-11-04'
    at = f'{day}T17:00:00+08:00'
    receipt = current_receipt(day, at)
    store.save_readiness(day, receipt)
    input_id = receipt['input_id']
    manifest = store.root / 'acceptances' / f'{day}.json'
    manifest.write_text('{"truncated":')
    (manifest.parent / '2016-01-04.json').write_text('[1]')
    # A promoted receipt survives a damaged index.
    unrelated = store.root / 'inputs' / ('b' * 32 + '.json.gz')
    unrelated.write_bytes(gzip.compress(b'[]'))
    refs = store.acceptance_references()
    assert refs[day] == [(at, input_id)]
    assert store.historical_reference(day, refs) == (input_id, at)
    original = store.root / 'inputs' / f'{input_id}.json.gz'
    held = original.read_bytes()
    original.write_bytes(held[:-5])
    atomic_json(manifest, {at: input_id, 'invalid entry': []})
    assert store.acceptance_references()[day] == [(at, input_id)]
    assert store.readiness(day) is None
    original.write_bytes(held)
    assert store.readiness(day) == receipt


def test_same_cutoff_uses_the_committed_identity_after_index_loss(tmp_path):
    store = EveningReportStore(tmp_path)
    day, at = '2026-10-28', '2026-10-28T17:00:00+08:00'
    old, latest = 'a' * 32, 'b' * 32
    for ref in (old, latest):
        store.save_readiness(day, current_receipt(day, at, input_id=ref))
    (store.root / 'acceptances' / f'{day}.json').unlink()
    assert store.acceptance_references()[day] == [(at, latest)]


def test_wrong_index_cutoff_cannot_replace_the_promoted_cutoff(tmp_path):
    store = EveningReportStore(tmp_path)
    day = '2026-11-04'
    at16, at17 = [f'{day}T{hour}:00:00+08:00' for hour in (16, 17)]
    receipt = current_receipt(day, at16)
    store.save_readiness(day, receipt)
    ref = receipt['input_id']
    atomic_json(store.root / 'acceptances' / f'{day}.json', {at17: ref})
    refs = store.acceptance_references()
    assert refs[day] == [(at16, ref)]
    assert store.historical_reference(day, refs) == (ref, at16)


def test_committed_acceptance_is_readable_after_interrupted_promotion(tmp_path, monkeypatch):
    store = EveningReportStore(tmp_path)
    day = '2026-11-04'
    at = f'{day}T17:00:00+08:00'
    receipt = current_receipt(day, at)
    replace = storage.os.replace
    def fail_promotion(src, target):
        if src.parent.name == 'pending' and target.parent.name == 'inputs':
            raise OSError('interrupted promotion')
        return replace(src, target)
    monkeypatch.setattr(storage.os, 'replace', fail_promotion)
    store.save_readiness(day, receipt)
    recovered = EveningReportStore(tmp_path)
    assert recovered.readiness(day) == receipt
    (store.root / 'ready' / f'{day}.json').unlink()
    assert recovered.acceptance_references()[day] == [(at, receipt['input_id'])]
    recovered.prune(date.fromisoformat(day) + timedelta(days=90))
    assert recovered.input_evidence(receipt['input_id']) == receipt


@pytest.mark.parametrize('latest_location', ['promoted', 'pending', 'damaged', 'missing', 'pointer_only'])
def test_prune_preserves_latest_complete_cutoff_and_missing_reference_after_a_long_outage(tmp_path, latest_location):
    store = EveningReportStore(tmp_path)
    day = '2026-10-28'
    resumed = date(2026, 12, 10)
    by_hour = {}
    for hour in (15, 16, 17):
        receipt = current_receipt(day, f'{day}T{hour}:00:00+08:00')
        store.save_readiness(day, receipt)
        path = store.root / 'inputs' / f"{receipt['input_id']}.json.gz"
        stamp = datetime.fromisoformat(receipt['cutoff']).timestamp()
        os.utime(path, (stamp, stamp))
        by_hour[hour] = (receipt, path)
    first, recovered = by_hour[15]
    older, _ = by_hour[16]
    latest, last_path = by_hour[17]
    # The restored copy is still within its file TTL, but cannot take the latest input exemption.
    stamp = datetime.combine(resumed, datetime.min.time(), tzinfo=CN_TZ).timestamp()
    os.utime(recovered, (stamp, stamp))
    if latest_location == 'pending':
        last_path.rename(last_path.parent / 'pending' / last_path.name)
    elif latest_location == 'damaged':
        last_path.write_bytes(b'truncated')
    elif latest_location in ('missing', 'pointer_only'):
        last_path.unlink()
        if latest_location == 'pointer_only':
            (store.root / 'acceptances' / f'{day}.json').unlink()

    store.prune(resumed)
    complete = latest_location in ('promoted', 'pending')
    assert store.input_evidence(first['input_id']) == first
    assert store.input_evidence(older['input_id']) == (None if complete else older)
    assert store.input_evidence(latest['input_id']) == (latest if complete else None)
    assert store.historical_reference(day) == (latest['input_id'], latest['cutoff'])


def test_retention_refuses_a_symlink_outside_the_module(tmp_path):
    store = EveningReportStore(tmp_path)
    saved = store.save('2026-08-09', build_report(observation_inputs()), {})
    target = tmp_path / 'preserve.json'
    target.write_text('keep')
    audit = store.root / 'archive' / f"{saved['version']}.audit.json.gz"
    audit.unlink()
    audit.symlink_to(target)
    with pytest.raises(ValueError, match='目录'):
        store.prune(date(2029, 8, 10))
    assert target.read_text() == 'keep'


@pytest.mark.asyncio
async def test_retention_uses_existing_slot_and_does_not_repeat_success(tmp_path, monkeypatch):
    from app.api import pipeline

    store = EveningReportStore(tmp_path)
    store.save_readiness('2026-11-13', {'complete': False})
    registry = pipeline_jobs.JobStore(store_dir=tmp_path / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', registry)
    monkeypatch.setattr(jobs, 'cn_now', lambda: datetime(2026, 11, 13, 2, 10, tzinfo=CN_TZ))
    submit = AsyncMock(return_value={'job_id': 'maintenance'})
    monkeypatch.setattr(pipeline, '_submit_job', submit)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    assert (await jobs.run_retention(repo))['job_id'] == 'maintenance'
    args, kwargs = submit.call_args
    result = args[2](lambda *a, **k: None)()
    assert result['removed_files'] == 0 and result['removed_audit_rows'] == 0
    assert kwargs['pause_quotes'] is False and kwargs['datasets'] == []
    job_id, _ = registry.create(request_key=kwargs['request_key'])
    registry.start(job_id)
    registry.succeed(job_id, {})
    assert await jobs.run_retention(repo) is None
