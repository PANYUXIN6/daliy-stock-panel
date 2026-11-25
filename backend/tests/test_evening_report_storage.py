"""Real publication/retention contracts for tiered evening storage."""
from __future__ import annotations

import copy
import gzip
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import polars as pl
import pytest
from test_evening_report_observation import observation_inputs

from app.market_time import CN_TZ
from app.services import evening_report_jobs as jobs
from app.services import evening_report_tracking as tracking
from app.services import pipeline_jobs
from app.services.evening_report_archive import AUDIT_RETENTION_DAYS, SCHEMA_VERSION
from app.services.evening_report_engine import build_report
from app.services.evening_report_store import EveningReportStore, atomic_json


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
    saved = store.save(inputs.trade_date, payload, {'stock_generation': 'current'})
    assert payload == original  # Projection cannot mutate the live continuation state.
    assert saved['storage_version'] == SCHEMA_VERSION
    assert saved['selection_policy_version'] == 'theme-observation-v2'
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
    assert disk['records'][0]['observation']['values']['return_20d'] is not None
    assert disk['variable_definitions']['return_20d']['as_of'] == inputs.trade_date
    assert not (store.root / 'diagnostics').exists()


def test_retention_preserves_feedback_candidates_and_price_evidence(tmp_path, private_owner):
    inputs = rejected_inputs()
    store = EveningReportStore(tmp_path)
    saved = store.save(inputs.trade_date, build_report(inputs), {'stock_generation': 'current'})
    tracking.refresh_tracking(tmp_path, inputs)
    symbol = saved['selected'][0]
    feedback = tracking.update_feedback(tmp_path, day=inputs.trade_date, version=saved['version'],
                                        symbol=symbol, starred=True, outcome='profit')
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
    assert 'role_state' in saved
    assert tracking.personal_history(tmp_path) == history_before
    actual = next(row['feedback'] for row in tracking.personal_report(tmp_path, inputs.trade_date, saved['version'])['records'] if row['symbol'] == symbol)
    assert actual == {key: value for key, value in feedback.items() if key != 'episode_id'}
    assert len(tracking.audit_history(tmp_path, saved['version'])['records']) == 19
    assert store.prune(today)['removed_files'] == 0


def test_cancelled_database_retention_rolls_back_audit_deletion(tmp_path, private_owner):
    inputs = rejected_inputs()
    store = EveningReportStore(tmp_path)
    saved = store.save(inputs.trade_date, build_report(inputs), {'stock_generation': 'current'})
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


def test_daily_ready_replacement_and_retention(tmp_path):
    store = EveningReportStore(tmp_path)
    day = '2026-10-09'
    for generation in ('first', 'latest'):
        store.save_readiness(day, {'complete': True, 'stock_generation': generation})
    assert store.readiness(day)['stock_generation'] == 'latest'
    assert [path.relative_to(store.root).as_posix() for path in store.root.rglob('*.json')] == [f'ready/{day}.json']
    store.prune(date(2026, 11, 9))
    assert store.readiness(day) is None


@pytest.mark.parametrize('damaged', [False, True])
def test_missing_published_file_is_local_and_cannot_be_overwritten(tmp_path, damaged, caplog):
    from app.api.evening_report import report as read_report

    store = EveningReportStore(tmp_path)
    first, second = '2026-10-09', '2026-10-12'
    payload = {'audit': {}, 'records': []}
    saved = store.save(first, payload, {})
    other = store.save(second, payload, {})
    path = store._base(saved).with_suffix('.json.gz')
    if damaged:
        path.write_bytes(b'incomplete')
    else:
        path.unlink()
    req = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))))
    result = read_report(req, date.fromisoformat(first), saved['version'])
    assert result['status'] == 'missing' and first in caplog.text
    assert store.get(second, view=True)['version'] == other['version']
    with pytest.raises(ValueError, match='已发布'):
        store.save(first, payload, {})
    assert store.original_metadata(first)['version'] == saved['version']


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


def test_earliest_plain_json_and_v2_packed_originals_remain_readable(tmp_path):
    store = EveningReportStore(tmp_path)
    for day, version, storage_version in [('2026-10-08', '1' * 32, None), ('2026-10-09', '2' * 32, 'evening-storage-v2')]:
        header = {'version': version, 'kind': 'original', 'storage_version': storage_version}
        atomic_json(store._path(day), {'versions': [header]})
        obs = {'variables': {'return_20d': {'value': .5}}}
        archive = {'trade_date': day, **header, 'selected': ['A'], 'records': [{'symbol': 'A', 'selected': True, 'observation': obs}]}
        path = store._base(header)
        if storage_version:
            atomic_json(path.with_suffix('.json.gz'), {**archive, 'variable_definitions': {'return_20d': {'unit': '小数'}}})
            assert store.get(day, view=True)['records'][0]['observation']['values']['return_20d'] == .5
        else:
            atomic_json(path.with_suffix('.json'), archive)
            atomic_json(path.with_suffix('.view.json'), archive)
            assert store.get(day, view=True) == archive
        assert store.get(day)['version'] == version
