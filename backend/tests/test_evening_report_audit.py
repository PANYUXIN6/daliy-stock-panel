"""Candidate audit, immutable snapshots and shared outcome storage contracts."""
from __future__ import annotations

import copy
import sqlite3
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from test_evening_report_observation import observation_inputs

from app.services import evening_report_tracking as tracking
from app.services.evening_report_engine import build_report
from app.services.evening_report_store import EveningReportStore


def test_selection_audit_preserves_quota_ranks_and_same_features_for_unselected():
    inputs = observation_inputs()
    report = build_report(inputs)
    samples = report['records']
    excluded = [row for row in samples if not row['selected']]
    assert len(excluded) == 5
    assert report['audit']['sample_counts'] == {'selected': 15, 'comparable_unselected': 5}
    assert {theme['ranking_rule_version'] for theme in report['audit']['themes']} == {'valid-member-days-v2'}
    assert report['audit']['sorting_rule_version'] == 'theme-role-order-v2'
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
        assert obs['values']['return_20d'] == pytest.approx(.20)
        assert report['variable_definitions']['return_20d']['unit'] == '小数'
        assert obs['snapshot']['as_of'] == inputs.trade_date
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
    rows = {row['symbol']: row['observation'] for row in report['calculations']}
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


def test_full_audit_and_recommendations_share_outcomes_without_sharing_identity(tmp_path, private_owner):
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
                                 symbol=unselected['symbol'], starred=True, outcome=None)
    tracking.refresh_tracking(tmp_path, inputs)
    personal = tracking.personal_history(tmp_path)['records']
    assert len(personal) == 15
    assert {row['appearance_seq'] for row in personal} == {1}
    for row in personal:
        assert row['id'] == f"{original['version']}:{row['symbol']}"
        assert row['count_key'][-1] == inputs.trade_date
    summary = tracking.personal_overview(tmp_path)
    assert summary['recommendation_count'] == summary['episode_count'] == 15
    with sqlite3.connect(tracking._shared_path(tmp_path)) as db:
        assert db.execute('SELECT COUNT(*) FROM outcome_evaluations').fetchone()[0] == 20
        assert db.execute('SELECT COUNT(*) FROM audit_cohorts').fetchone()[0] == 20
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


def test_registered_audit_only_report_is_analysed_when_market_recovers(tmp_path, private_owner):
    inputs = observation_inputs()
    inputs.memberships = inputs.memberships.clear()
    report = build_report(inputs)
    assert not report['selected'] and report['audit']['same_cycle_sample_count'] > 0
    saved = EveningReportStore(tmp_path).save(inputs.trade_date, report, {})
    tracking.refresh_tracking(tmp_path, inputs, evaluate=False)
    assert not tracking.audit_history(tmp_path, saved['version'])['analyses']
    assert tracking.refresh_tracking(tmp_path, inputs)['evaluated_recommendations'] == 0
    tracking.refresh_tracking(tmp_path, inputs)
    analysis, = tracking.audit_history(tmp_path, saved['version'])['analyses']
    assert analysis['payload']['same_cycle_sample_count'] == report['audit']['same_cycle_sample_count']


def test_missingness_reports_selection_proxy_without_false_significance():
    from app.services.evening_report_audit import missingness

    result = missingness({'selected': [{'missing_features': []}],
                          'unselected': [{'missing_features': ['gap']}]}, ['gap', 'return'])
    assert result['gap']['group_exclusive_availability']
    assert result['gap']['attribution_warning']
    assert result['gap']['missing_rate_difference'] == -1
    assert result['return']['missing_rate_difference'] == 0


def test_not_due_keeps_dated_constraints_but_never_reuses_the_old_adjusted_price(tmp_path, private_owner, monkeypatch):
    symbol, days = '300001.SZ', ['2026-11-02', '2026-11-03']
    store = EveningReportStore(tmp_path)
    report = store.save(days[0], {'audit': {}, 'records': [{'symbol': symbol, 'selected': True, 'observation': {'decision': {
        'steps': [], 'all_reason_codes': [], 'comparison_eligible': True, 'comparison_group': None,
        'rank_within_theme_role': 1, 'rank_global_role': 1}}}]}, {})
    inputs = SimpleNamespace(calendar=days, trade_date=days[0], cutoff=days[0], provenance={},
                             prices=pl.DataFrame({'symbol': [symbol, symbol], 'date': days,
                                                  'close': [10., 6.], 'formal': [True, True],
                                                  'is_st': [False, False], 'signal_limit_up': [True, False],
                                                  'signal_limit_down': [False, False]}))
    monkeypatch.setattr(tracking, 'cn_now', lambda: datetime.fromisoformat(inputs.trade_date + 'T17:00:00+08:00'))
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
