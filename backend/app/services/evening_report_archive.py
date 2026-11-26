"""当期定稿、可比候选与限期审计的存储边界。"""
from __future__ import annotations

from collections import Counter, defaultdict

SCHEMA_VERSION = 'evening-storage-v3'
AUDIT_RETENTION_DAYS = 730
INPUT_RETENTION_DAYS = 30
DIAGNOSTIC_RETENTION_DAYS = 30
ATTEMPT_RETENTION_DAYS = 90

_REPORT_FIELDS = (
    'date', 'trade_date', 'next_trade_date', 'cutoff', 'generated_at', 'version',
    'kind', 'mode', 'status', 'rule_version',
    'scope_version', 'selection_policy_version', 'storage_version', 'disclaimer', 'summary', 'market',
    'selected', 'limitations', 'late_generation', 'recovery_note',
    'wave_rule', 'dip_rule_version', 'wave_starts', 'role_state', 'variable_definitions', 'industry_status',
)
_RECORD_FIELDS = ('symbol', 'name', 'purpose', 'regime', 'risk', 'position', 'qualification',
                  'limitations', 'selected', 'wait_conditions', 'observation')
_LARGE_STATISTICS = {'selection_path_audit', 'identity_continuity_audit', 'legacy_confirmation_audit'}
_ROLE_VARIABLES = ('return_20d', 'identity_score', 'high_proximity', 'return_5d', 'acceleration_5_20')


def _audit_row(row: dict) -> dict:
    obs = row['observation']
    decision = obs['decision']
    reasons = set(decision['all_reason_codes'])
    keys = set(_ROLE_VARIABLES) if reasons & {'role_not_qualified'} else set()
    if 'untrusted_current_price' in reasons:
        keys.add('raw_close')
    variables = obs.get('variables', {})
    values = {key: {'value': obs.get('values', {}).get(key),
                    'unknown_reason': variables.get(key, {}).get('missing_reason')}
              for key in sorted(keys)}
    # Hard checks and role conditions are decision evidence, not another copy
    # of every feature. Missing-feature names preserve the full audit denominator.
    return {
        'symbol': row['symbol'], 'selected': row['selected'],
        'tier': 'selected' if row['selected'] else 'comparable_unselected'
        if decision['comparison_eligible'] else 'audit_only',
        'decision': decision,
        'daily_role': obs.get('daily_role'),
        'role_by_history_tracking': obs.get('role_by_history_tracking'),
        'role_status': obs.get('role_status'),
        'role_condition_completeness': obs.get('role_condition_completeness'),
        'related_theme_ids_all': obs.get('related_theme_ids_all', []),
        'missing_features': obs.get('data_completeness', {}).get('missing_fields', []),
        'not_computed_features': [key for key in ('price_gaps', 'abnormal_move') if key not in obs],
        'reason_values': values,
        'hard_checks': obs.get('eligibility', {}).get('inputs', {}),
        'role_conditions': obs.get('conditions', {}),
        'board': obs.get('board'),
    }


def retained_observation_rows(report: dict):
    records = {row['symbol']: row for row in report.get('records', [])}
    for row in report.get('calculations', report.get('records', [])):
        if row['selected'] or row['observation']['decision']['comparison_eligible']:
            yield records.get(row['symbol'], row)


def split_report(report: dict) -> tuple[dict, dict, dict]:
    """Return the permanent report, medium-term audit, and candidate boundary.

Publication identity and common versions live in the envelope, once. A boundary
references the same full snapshots as recommendations; it cannot reconstruct
securities or features outside the frozen allocation pool.
    """
    rows = report.get('calculations', report.get('records', []))
    audit_rows = [_audit_row(row) for row in rows]
    retained = [{key: row[key] for key in _RECORD_FIELDS if key in row} for row in retained_observation_rows(report)]
    # Keep the original recommendation order, including the stable tie breaks.
    order = {symbol: index for index, symbol in enumerate(report.get('selected', []))}
    retained.sort(key=lambda row: (not row['selected'], order.get(row['symbol'], 0), row['symbol']))
    permanent = {key: report[key] for key in _REPORT_FIELDS if key in report}
    permanent['as_of'] = report.get('trade_date') or report['date']
    permanent['records'] = retained
    permanent['statistics'] = {key: value for key, value in report.get('statistics', {}).items()
                               if key not in _LARGE_STATISTICS}
    permanent['input_reference'] = {key: value for key, value in report.get('input_reference', {}).items()
                                     if key in ('stock_generation', 'cutoff')}
    permanent['data_version'] = permanent['input_reference']
    provenance = report.get('provenance', {})
    permanent['industry_status'] = provenance.get('industry_status')
    permanent['provenance'] = {
        'membership_changes': provenance.get('membership_changes', []),
        'classifications': [{key: value for key, value in observation.items()
                            if key not in ('symbols', 'memberships')}
                            | {'symbol_count': len(observation.get('symbols', [])),
                               'membership_count': len(observation.get('memberships', []))}
                           for observation in provenance.get('classifications', [])],
        'scope_version': provenance.get('scope_version'),
        'sector_filter': {key: value for key, value in provenance.get('sector_filter', {}).items()
                          if key != 'excluded'},
        'suspension_evidence_count': len(provenance.get('suspensions', [])),
    }
    themes = report['audit'].get('themes', [])
    selected_themes = report.get('observation_themes')
    if selected_themes is None:
        selected_themes = [sector['observation'] for sector in report.get('sectors', [])
                           if sector.get('observation', {}).get('selected_theme')]
    sector_changes = {sector['id']: sector['membership_change'] for sector in report.get('sectors', [])
                      if 'membership_change' in sector}
    permanent['sectors'] = [{'id': theme['id'], 'name': theme['name'], 'observation': theme,
                             **({'membership_change': sector_changes[theme['id']]} if theme['id'] in sector_changes else {})}
                             for theme in selected_themes]
    step_counts = defaultdict(Counter)
    for row in audit_rows:
        for step in row['decision']['steps']:
            step_counts[step['step']][step['status']] += 1
    permanent['audit'] = {key: value for key, value in report['audit'].items() if key != 'themes'}
    permanent['audit'].update(
        audit_scope='actually_evaluated_securities',
        step_counts={key: dict(value) for key, value in step_counts.items()},
        themes=[theme for theme in themes if theme.get('selected_theme')],
    )
    permanent['retention'] = {'audit_days': AUDIT_RETENTION_DAYS,
                              'candidate_boundary_days': AUDIT_RETENTION_DAYS,
                              'replay_scope': 'frozen_allocation_pool',
                              'full_market_replay': False}
    header = {key: permanent.get(key) for key in (
        'storage_version', 'version', 'trade_date', 'as_of', 'cutoff', 'generated_at', 'rule_version',
        'scope_version', 'selection_policy_version', 'data_version')}
    audit = {**header, 'themes': themes, 'records': audit_rows}
    boundary = {**header, 'definition_version': report['audit'].get('cohort_definition_version'),
                'sorting_rule_version': report['audit'].get('sorting_rule_version'),
                'dedup_rule_version': report['audit'].get('dedup_rule_version'),
                'scope': 'passed_hard_theme_role_and_quota_eligibility_before_allocation',
                'records': [{'symbol': row['symbol'],
                             'snapshot_ref': f"{report['version']}:{row['symbol']}",
                             'comparison_group': row['decision']['comparison_group'],
                             'competition_group': row['decision'].get('competition_group'),
                             'rank_within_theme_role': row['decision']['rank_within_theme_role'],
                             'rank_global_role': row['decision']['rank_global_role'],
                             'quota_limit': row['decision'].get('quota_limit')}
                            for row in audit_rows if row['decision']['comparison_eligible']]}
    return permanent, audit, boundary
