"""Candidate audit, immutable snapshots and shared outcome storage contracts."""
from __future__ import annotations

import copy
import json
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from test_evening_report_observation import observation_inputs

from app.services import evening_report_tracking as tracking
from app.services.daily_coverage import DailyCoverageError
from app.services.evening_report_engine import build_report
from app.services.evening_report_inputs import prepare_evaluation_inputs
from app.services.evening_report_store import EveningReportStore, RevisionBranch


def test_selection_audit_preserves_quota_ranks_and_same_features_for_unselected():
    inputs = observation_inputs()
    report = build_report(inputs)
    samples = report['records']
    excluded = [row for row in samples if not row['selected']]
    assert len(excluded) == 5
    assert report['audit']['sample_counts'] == {'selected': 15, 'comparable_unselected': 5}
    for row in excluded:
        obs = row['observation']
        decision = obs['decision']
        assert decision['first_blocking_step'] == 'allocation'
        assert decision['blocking_reason_code'] == 'quota_exhausted'
        assert decision['all_reason_codes'] == ['quota_exhausted']
        assert decision['competed_quota_type'] == 'leader'
        assert decision['occupied_quota_type'] is None
        assert decision['rank_within_theme_role'] == 3
        assert decision['rank_global_role'] is not None
        assert decision['comparison_eligible'] and not decision['quota_fallback_allowed']
        assert obs['price_gaps']['status'] == 'known'
        assert obs['variables']['return_20d']['value'] == pytest.approx(.35)
        assert obs['variables']['return_20d']['unit'] == '小数'
        assert 'bucket_label_at_time' in obs['variables']['return_20d']
        assert 'bucket_rule_version' in obs['variables']['return_20d']
        assert obs['snapshot']['as_of'] == inputs.trade_date
        assert obs['verification']['wave_index']['status'] == 'unknown'
        assert obs['verification']['wave_index']['reason_code']
    assert report['audit']['missingness']['price_gaps']['missing_rate_difference'] == 0
    assert report['audit']['non_independent_notice']


def test_unknown_failed_and_skipped_steps_are_distinct_and_theme_flags_stay_on_themes():
    inputs = observation_inputs()
    inputs.metadata = inputs.metadata.with_columns(
        pl.when(pl.col('symbol') == '000000.SZ').then(None)
        .when(pl.col('symbol') == '000001.SZ').then(False)
        .otherwise(pl.col('trading_normal')).alias('trading_normal'))
    # These theme relations must not exclude the entire security.
    broad = [{'symbol': f'outside{n}', 'source': 'concept', 'sector_id': 'broad', 'sector_name': '宽概念'} for n in range(121)]
    extra = [{'symbol': '000003.SZ', 'source': 'concept', 'sector_id': key, 'sector_name': key}
             for key in ('broad', '新疆振兴', 't1')]
    inputs.memberships = pl.concat([inputs.memberships, pl.DataFrame(broad + extra)])
    report = build_report(inputs)
    rows = {row['symbol']: row['observation'] for row in report['records']}
    for symbol, status in [('000000.SZ', 'unknown'), ('000001.SZ', 'failed')]:
        decision = rows[symbol]['decision']
        steps = {item['step']: item['status'] for item in decision['steps']}
        assert steps['hard_validation'] == status
        assert steps['role'] == 'passed'  # It really was evaluated independently.
        assert steps['allocation'] == steps['quota_eligibility'] == 'not_executed'
        assert decision['first_blocking_step'] == 'hard_validation'
        assert decision['blocking_reason_code'] == 'not_trading'
        assert not decision['comparison_eligible']
    obs = rows['000003.SZ']
    assert obs['decision']['selected']
    assert obs['related_theme_count_all'] == 4 and obs['related_theme_count_in_top5'] == 2
    assert obs['decision']['merged_into_row_id'] == '000003.SZ'
    assert len(obs['decision']['competed_themes']) == 2
    assert 'exclude_flag' not in obs
    themes = {theme['id']: theme for theme in report['audit']['themes']}
    assert themes['concept:broad']['theme_member_count'] == 122
    assert themes['concept:broad']['is_broad_concept']
    assert themes['concept:新疆振兴']['is_region_theme']
    assert themes['concept:broad']['ranking_status'] == 'not_executed'


def test_full_audit_and_personal_revisions_share_outcomes_without_sharing_identity(tmp_path, private_owner):
    inputs = observation_inputs()
    store = EveningReportStore(tmp_path)
    original = store.save(inputs.trade_date, build_report(inputs), {})
    saved = copy.deepcopy(original)
    tracking.refresh_tracking(tmp_path, inputs)
    export = tracking.audit_history(tmp_path, original['version'])
    assert len(export['records']) == 20
    unselected = next(row for row in export['records'] if not row['selected'])
    assert unselected['id'].startswith('audit:')
    assert 'episode_id' not in unselected
    assert unselected['performance']['1']['status'] == 'not_due'
    with pytest.raises(ValueError, match='已入选'):
        tracking.update_feedback(tmp_path, day=inputs.trade_date, version=original['version'],
                                 symbol=unselected['symbol'], starred=True, outcome=None,
                                 expected_revision=0, request_id='audit_request_test1')
    branch = RevisionBranch(store)
    payload = build_report(inputs)
    payload['records'][0]['observation']['reason'] = '修订后的事实说明'
    revision = branch.append(inputs.trade_date, payload, {'input_id': 'b' * 32})
    branch.commit()
    tracking.refresh_tracking(tmp_path, inputs)
    tracking.refresh_tracking(tmp_path, inputs)
    personal = tracking.personal_history(tmp_path)['records']
    assert len(personal) == 30
    assert {row['appearance_seq'] for row in personal} == {1}
    assert {row['revision_seq'] for row in personal} == {1, 2}
    for row in personal:
        assert row['first_revision_id'] == f"{original['version']}:{row['symbol']}"
        assert row['latest_revision_id'] == f"{revision['version']}:{row['symbol']}"
        assert row['count_key'][-1] == inputs.trade_date
    summary = tracking.personal_overview(tmp_path)
    assert summary['recommendation_count'] == summary['episode_count'] == 15
    assert summary['revision_count'] == 30
    with sqlite3.connect(tracking._shared_path(tmp_path)) as db:
        assert db.execute('SELECT COUNT(*) FROM outcome_evaluations').fetchone()[0] == 20
        assert db.execute('SELECT COUNT(*) FROM audit_cohorts').fetchone()[0] == 40
    analyses = tracking.audit_history(tmp_path, original['version'])['analyses']
    assert len(analyses) == 1  # Same input + retry does not create a new analysis.
    analysis = analyses[0]['payload']
    assert analysis['same_cycle_sample_count'] == 20
    assert analysis['comparable_sample_count'] == 20
    assert analysis['windows']['1']['not_due'] == 20
    assert analysis['non_independent_notice'] and analysis['significance']['status'] == 'not_tested'
    assert 'return_20d' in analysis['missingness']  # Include fully observed fields too.
    # One missing candidate endpoint cannot erase the selected group's results,
    # nor can later results change either report's recommendation features.
    next_day = (date.fromisoformat(inputs.trade_date) + timedelta(days=1)).isoformat()
    new_prices = inputs.prices.filter((pl.col('date') == inputs.trade_date) & (pl.col('symbol') != unselected['symbol'])).with_columns(
        pl.lit(next_day).alias('date'), (pl.col('close') * 1.1).alias('close'), (pl.col('raw_close') * 1.1).alias('raw_close'))
    inputs.prices = pl.concat([inputs.prices, new_prices])
    inputs.calendar.append(next_day)
    inputs.trade_date = next_day
    inputs.cutoff = next_day + 'T16:00:00+08:00'
    tracking.refresh_tracking(tmp_path, inputs)
    evolved = tracking.audit_history(tmp_path, original['version'])
    assert len(evolved['analyses']) == 2
    assert evolved['analyses'][0] == analyses[0]
    assert evolved['analyses'][1]['payload']['windows']['1']['unavailable'] == 1
    missing = next(row for row in evolved['records'] if row['symbol'] == unselected['symbol'])
    assert missing['performance']['1']['reason_code'] == 'missing_endpoint'
    assert missing['performance']['1']['return'] is None
    assert store.get(original['date']) == saved


def test_audit_registers_role_rejections_from_complete_calculations(tmp_path, private_owner):
    inputs = observation_inputs()
    rejected = '000003.SZ'
    inputs.prices = inputs.prices.with_columns([
        pl.when(pl.col('symbol') == rejected).then(value).otherwise(pl.col(key)).alias(key)
        for key, value in {'close': 10., 'open': 10., 'high': 10.1, 'low': 9.8,
                           'raw_close': 10., 'raw_open': 10., 'raw_high': 10.1, 'raw_low': 9.8,
                           'ma5': 10., 'ma10': 10., 'ma20': 10., 'bull_bear_line': 10.,
                           'high_60d': 10.1}.items()])
    report = build_report(inputs)
    assert len(report['calculations']) == 20 and len(report['records']) == 19
    assert rejected not in {row['symbol'] for row in report['records']}
    assert report['audit']['sample_counts'] == {'selected': 15, 'comparable_unselected': 4, 'audit_only': 1}
    saved = EveningReportStore(tmp_path).save(inputs.trade_date, report, {})
    tracking.refresh_tracking(tmp_path, inputs)
    tracking.refresh_tracking(tmp_path, inputs)
    audit = tracking.audit_history(tmp_path, saved['version'])
    assert len(audit['records']) == 20
    row = next(row for row in audit['records'] if row['symbol'] == rejected)
    assert not row['selected'] and not row['comparable']
    assert row['performance'] == {}  # Audit-only rows do not enter outcome cohorts.
    analysis = audit['analyses'][0]['payload']
    assert analysis['same_cycle_sample_count'] == 20
    assert analysis['comparable_sample_count'] == 19
    assert analysis['audit_missingness']['return_20d']['groups']['all_unselected']['sample_count'] == 5
    assert len(tracking.personal_history(tmp_path)['records']) == 15
    assert tracking.personal_overview(tmp_path)['episode_count'] == 15


def test_rebased_prices_use_raw_endpoints_and_ignore_future_factors(tmp_path):
    days = ['2026-11-02', '2026-11-03', '2026-11-04']
    symbol = '300001.SZ'
    factor_path = tmp_path / 'adj_factor' / 'all.parquet'
    factor_path.parent.mkdir()
    pl.DataFrame({'symbol': [symbol, symbol], 'trade_date': [date(2026, 11, 3), date(2026, 11, 4)],
                  'ex_factor': [2., 3.]}).write_parquet(factor_path)
    inputs = SimpleNamespace(trade_date=days[1], metadata=pl.DataFrame({'symbol': [symbol], 'is_st': [True]}),
                             prices=pl.DataFrame({'symbol': [symbol, symbol], 'date': days[:2],
                                                   'raw_close': [10., 6.], 'close': [999., 123.], 'formal': [True, True],
                                                   'signal_limit_up': [False, True]}),
                             provenance={'input_versions': {'files': [['adj_factor/all.parquet', 123, 456]]},
                                         'calendar': {'days': days, 'updated_at': 'calendar-v1'}, 'suspensions': []})
    prepare_evaluation_inputs(tmp_path, inputs)
    prices = {(row['symbol'], str(row['date'])): row for row in inputs.prices.to_dicts()}
    result = tracking.evaluate_performance(symbol, days[0], calendar=days, as_of=days[1],
                                          prices=prices, suspensions=set(), data_version={'input_id': 'test'},
                                          evaluation_context=inputs.provenance['evaluation_context'])['1']
    assert result['d_close_raw'] == 10 and result['target_close_raw'] == 6
    assert result['d_close_adjusted'] == 5 and result['target_close_adjusted'] == 6
    assert result['d_adjustment_multiplier'] == .5 and result['target_adjustment_multiplier'] == 1
    assert (result['target_close_raw'] * result['target_adjustment_multiplier'] /
            (result['d_close_raw'] * result['d_adjustment_multiplier']) - 1) == pytest.approx(result['return'])
    assert result['return'] == pytest.approx(.2) and result['return_unit'] == 'ratio'
    assert result['status'] == 'evaluated'
    assert result['adjust_method'] == 'qfq' and result['adjust_base_date'] == days[1]
    assert result['calendar_version']['updated_at'] == 'calendar-v1'
    assert result['trading_constraints'] == ['目标日涨停', '目标日ST']
    assert result['constraint_coverage'][days[0]]['st_status'] == 'unknown'
    assert result['evaluated_at'] and result['evaluation_rule_version']
    # A malformed factor is unknown data, never identity adjustment.
    pl.DataFrame({'symbol': [symbol], 'trade_date': [date(2026, 11, 3)], 'ex_factor': [0.]}).write_parquet(factor_path)
    with pytest.raises(DailyCoverageError, match='无效'):
        prepare_evaluation_inputs(tmp_path, inputs)


def test_legacy_tracking_migration_preserves_feedback_anchors_and_deduplicates_prices(tmp_path, private_owner):
    path = tracking._shared_path(tmp_path)
    path.parent.mkdir(parents=True)
    value = json.dumps({'1': {'status': 'evaluated', 'return': .1}})
    with sqlite3.connect(path) as db:
        db.executescript('''
            CREATE TABLE reports(version TEXT PRIMARY KEY,day TEXT NOT NULL,kind TEXT NOT NULL);
            CREATE TABLE episodes(id TEXT PRIMARY KEY,symbol TEXT,first_reference TEXT,started_at TEXT,last_seen TEXT,
                                  absence_count INTEGER,ended_at TEXT,historical INTEGER DEFAULT 0);
            CREATE TABLE recommendations(id TEXT PRIMARY KEY,version TEXT,day TEXT,month TEXT,symbol TEXT,name TEXT,
                                         episode_id TEXT,observation TEXT,performance TEXT DEFAULT '{}');
            INSERT INTO reports VALUES('v1','2026-11-02','original');
            INSERT INTO episodes VALUES('e','300001.SZ','v1:300001.SZ','2026-11-02','2026-11-02',0,NULL,0);
        ''')
        for version in ('v1', 'v2'):
            db.execute('INSERT INTO recommendations VALUES(?,?,?,?,?,?,?,?,?)',
                       (f'{version}:300001.SZ', version, '2026-11-02', '2026-11', '300001.SZ', '股票', 'e', '{}', value))
    before = path.read_bytes()
    assert tracking.personal_history(tmp_path)['records'][0]['performance']['1']['return'] == .1
    assert path.read_bytes() == before  # GET does not migrate.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE recommendations SET performance='broken' WHERE version='v2'")
    with pytest.raises(json.JSONDecodeError), tracking._database(path, write=True) as db:
        tracking._schema(db)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='outcome_evaluations'").fetchone() is None
        assert db.execute("SELECT performance FROM recommendations WHERE version='v1'").fetchone()[0] == value
        db.execute("UPDATE recommendations SET performance=? WHERE version='v2'", (value,))
        db.execute("UPDATE recommendations SET performance=? WHERE version='v1'",
                   (json.dumps({'1': {'status': 'not_due', 'return': None}}),))
    with tracking._database(path, write=True) as db:
        tracking._schema(db)
        tracking._schema(db)
        assert db.execute('SELECT COUNT(*) FROM outcome_evaluations').fetchone()[0] == 1
        assert 'performance' not in {row['name'] for row in db.execute('PRAGMA table_info(recommendations)')}
    rows = tracking.personal_history(tmp_path)['records']
    assert {row['appearance_seq'] for row in rows} == {1}
    assert {row['revision_seq'] for row in rows} == {1, 2}
    assert {row['first_revision_id'] for row in rows} == {'v1:300001.SZ'}
    assert all(row['performance']['1']['return'] == .1 for row in rows)
    assert all(row['end_known_at'] is None for row in rows)
    assert tracking.personal_overview(tmp_path)['recommendation_count'] == 1


def test_missingness_reports_selection_proxy_without_false_significance():
    from app.services.evening_report_audit import missingness

    result = missingness({'selected': [{'missing_features': []}],
                          'unselected': [{'missing_features': ['gap']}]}, ['gap', 'return'])
    assert result['gap']['group_exclusive_availability']
    assert result['gap']['attribution_warning']
    assert result['gap']['missing_rate_difference'] == -1
    assert result['return']['missing_rate_difference'] == 0


def test_not_due_keeps_dated_constraints_but_never_reuses_the_old_adjusted_price(tmp_path, private_owner):
    symbol, days = '300001.SZ', ['2026-11-02', '2026-11-03']
    store = EveningReportStore(tmp_path)
    report = store.save(days[0], {'records': [{'symbol': symbol, 'selected': True, 'observation': {}}]}, {})
    inputs = SimpleNamespace(calendar=days, trade_date=days[0], cutoff=days[0], provenance={},
                             prices=pl.DataFrame({'symbol': [symbol, symbol], 'date': days,
                                                  'close': [10., 6.], 'formal': [True, True],
                                                  'is_st': [False, False], 'signal_limit_up': [True, False],
                                                  'signal_limit_down': [False, False]}))
    tracking.refresh_tracking(tmp_path, inputs)
    first = tracking.personal_history(tmp_path)['records'][0]['performance']['1']
    assert first['status'] == 'not_due'
    assert first['trading_constraints'] == ['推荐日涨停']
    assert first['target_close_adjusted'] is None  # Future input rows cannot leak.
    inputs.trade_date, inputs.cutoff = days[1], days[1]
    inputs.prices = inputs.prices.with_columns(
        pl.when(pl.col('date') == days[0]).then(5.).otherwise(pl.col('close')).alias('close'),
        *[pl.when(pl.col('date') == days[0]).then(None).otherwise(pl.col(key)).alias(key)
          for key in ('signal_limit_up', 'signal_limit_down', 'is_st')])
    tracking.refresh_tracking(tmp_path, inputs)
    final = tracking.personal_report(tmp_path, days[0], report['version'])['records'][0]['performance']['1']
    assert final['return'] == pytest.approx(.2)
    assert final['d_close_adjusted'] == 5
    assert final['trading_constraints'] == ['推荐日涨停']
    assert final['constraint_coverage'][days[0]]['st_status'] == 'known'
    assert final['constraint_coverage'][days[0]]['retained_source_versions']['limit_up_status']['cutoff'] == days[0]
