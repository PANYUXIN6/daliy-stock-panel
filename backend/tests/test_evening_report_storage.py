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
    RevisionBranch,
    atomic_json,
    history_view,
)


def rejected_inputs():
    inputs = observation_inputs()
    inputs.metadata = inputs.metadata.with_columns(
        pl.when(pl.col('symbol') == '000003.SZ').then(False)
        .otherwise(pl.col('trading_normal')).alias('trading_normal'))
    return inputs


def test_three_tiers_keep_comparable_snapshots_and_audit_denominators(tmp_path, private_owner):
    inputs = rejected_inputs()
    payload = build_report(inputs)
    original = copy.deepcopy(payload)
    store = EveningReportStore(tmp_path)
    saved = store.save(inputs.trade_date, payload, {'input_id': 'a' * 32})
    assert payload == original  # Projection cannot mutate the live continuation state.
    assert saved['storage_version'] == SCHEMA_VERSION
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
    with pytest.raises(DailyCoverageError, match='保留期'):
        store.history(history[2]['trade_date'], RULE_VERSION)


def test_temporary_evidence_and_explicit_diagnostics_expire(tmp_path, monkeypatch):
    today = date(2026, 11, 13)
    old = today - timedelta(days=31)
    now = datetime.combine(old, datetime.min.time(), tzinfo=CN_TZ)
    monkeypatch.setattr(storage, 'cn_now', lambda: now)
    store = EveningReportStore(tmp_path)
    first = {'date': old.isoformat(), 'complete': True, 'input_id': 'a' * 32}
    second = {'date': today.isoformat(), 'complete': True, 'input_id': 'b' * 32}
    store.save_readiness(old.isoformat(), first)
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
    assert store.input_evidence(second['input_id']) == second
    assert not list((store.root / 'diagnostics').glob('*.gz'))
    assert unrelated.read_text() == 'personal asset'


def test_retention_preserves_legacy_inputs_and_shared_revision_dependencies(tmp_path):
    today = date(2026, 11, 13)
    old = today - timedelta(days=35)
    store = EveningReportStore(tmp_path)
    legacy = {'date': old.isoformat(), 'complete': True, 'input_id': 'a' * 32}
    legacy_path = store.root / 'inputs' / (legacy['input_id'] + '.json')
    atomic_json(legacy_path, legacy)
    atomic_json(store.root / 'ready' / f'{old}.json',
                {'date': str(old), 'complete': True, 'input_id': legacy['input_id']})
    original = store.save(str(old), {'rule_version': RULE_VERSION}, {'input_id': legacy['input_id']})

    # A full-format revision may share an input stored by the new writer.
    shared = {'date': str(old), 'complete': True, 'input_id': 'b' * 32}
    atomic_json(store.root / 'inputs' / (shared['input_id'] + '.json.gz'), shared)
    branch = RevisionBranch(store)
    revision = branch.append(str(old), {'rule_version': RULE_VERSION}, {'input_id': shared['input_id']})
    branch.commit()
    expired_day = old + timedelta(days=1)
    expired = {'date': str(expired_day), 'complete': True, 'input_id': 'c' * 32}
    store.save_readiness(str(expired_day), expired)
    # Freeze mtimes so both old formats and the expired new input exceed the TTL.
    timestamp = datetime.combine(old, datetime.min.time(), tzinfo=CN_TZ).timestamp()
    for path in (store.root / 'inputs').iterdir():
        os.utime(path, (timestamp, timestamp))
    interrupted_legacy = store.root / 'revisions' / ('f' * 32) / f'{old}.json'
    atomic_json(interrupted_legacy, original)
    os.utime(interrupted_legacy.parent, (timestamp, timestamp))
    latest = {'date': str(today), 'complete': True, 'input_id': 'd' * 32}
    store.save_readiness(str(today), latest)

    store.prune(today)
    assert store.input_evidence(legacy['input_id']) == legacy
    assert store.readiness(str(old)) == legacy
    assert store.input_evidence(shared['input_id']) == shared
    assert store.get(str(old)) == original
    assert store.get(str(old), revision['version']) == revision
    assert storage.read_json(interrupted_legacy) == original
    assert store.input_evidence(expired['input_id']) is None
    assert store.readiness_status(str(expired_day)) is None
    assert store.input_evidence(latest['input_id']) == latest


@pytest.mark.parametrize('legacy_is_revision', [False, True])
def test_retention_only_expires_audits_owned_by_new_format(tmp_path, private_owner, legacy_is_revision):
    inputs = rejected_inputs()
    store = EveningReportStore(tmp_path)
    modern_payload = build_report(inputs)
    # Exercise the existing full-archive writer and unversioned metadata.
    legacy_payload = {**modern_payload, 'selection_policy_version': 'observation-v3'}
    original = store.save(inputs.trade_date, modern_payload if legacy_is_revision else legacy_payload,
                          {'input_id': 'a' * 32})
    branch = RevisionBranch(store)
    revision = branch.append(inputs.trade_date, legacy_payload if legacy_is_revision else modern_payload,
                              {'input_id': 'b' * 32})
    branch.commit()
    legacy, modern = (revision, original) if legacy_is_revision else (original, revision)
    tracking.refresh_tracking(tmp_path, inputs)
    # Before tiered storage, audit-only securities also had price evaluations.
    with tracking._database(tracking._shared_path(tmp_path), write=True) as db:
        db.execute('INSERT INTO outcome_evaluations VALUES(?,?,?)',
                   (inputs.trade_date, '000003.SZ', json.dumps({'1': {'status': 'evaluated', 'return': .1}})))
    legacy_before = tracking.audit_history(tmp_path, legacy['version'])
    assert len(legacy_before['records']) == 20 and legacy_before['analyses']
    today = date.fromisoformat(inputs.trade_date) + timedelta(days=AUDIT_RETENTION_DAYS + 1)
    store.prune(today)
    assert tracking.prune_tracking(tmp_path, today)['removed_audit_rows'] == 1
    assert tracking.audit_history(tmp_path, legacy['version']) == legacy_before
    modern_after = tracking.audit_history(tmp_path, modern['version'])
    assert len(modern_after['records']) == 19
    assert modern_after['analyses'] == []


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
