"""Point-in-time selection evidence; consumes decisions, never selects again.

The full report owns feature snapshots. Tracking stores only sample references
and later evaluations, so audit and personal views share one price evaluator.
"""
from __future__ import annotations

from collections import Counter, defaultdict

VERSION = 'evening-audit-v1'
SORT_VERSION = 'theme-role-order-v2'
THEME_RANKING_VERSION = 'valid-member-days-v2'
COHORT_VERSION = 'same-day-theme-quota-role-v2'
DEDUP_VERSION = 'strongest-top5-theme-v1'
NON_INDEPENDENT_NOTE = '同交易日及重叠评价窗口的样本不独立；仅作关联性观察，不能据此认定收益因果。'


def completeness(values: dict, keys) -> dict:
    missing = sorted(key for key in keys if values.get(key) is None)
    return {'status': 'complete' if not missing else 'unknown' if len(missing) == len(keys) else 'partial',
            'missing_fields': missing}


def missingness(groups: dict[str, list[dict]], feature_keys: list[str]) -> dict:
    """Counts and differences, without an IID significance test on clustered data."""
    result = {}
    for key in feature_keys:
        counts = {}
        for name, rows in groups.items():
            n = sum(key in row['missing_features'] for row in rows)
            counts[name] = {'sample_count': len(rows), 'missing_count': n,
                            'missing_rate': n / len(rows) if rows else None}
        rates = [item['missing_rate'] for item in counts.values()]
        selection_proxy = len(rates) == 2 and rates[0] == 0 and rates[1] == 1
        result[key] = {'groups': counts,
                       'missing_rate_difference': rates[0] - rates[1] if len(rates) == 2 and None not in rates else None,
                       'group_exclusive_availability': selection_proxy,
                       'attribution_warning': '该特征只在第一组有值，不能直接用于归因' if selection_proxy else None}
    return result


def _step(name: str, checks: dict[str, bool | None], *, executed=True) -> dict:
    failed = [key for key, value in checks.items() if value is False]
    unknown = [key for key, value in checks.items() if value is None]
    status = ('not_executed' if not executed else 'failed' if failed else 'unknown' if unknown else 'passed')
    return {'step': name, 'status': status, 'failed_reason_codes': failed if executed else [],
            'unknown_reason_codes': unknown if executed else []}


def annotate_audit(inputs, records: list[dict], themes: list[dict]) -> dict:
    """Annotate the decisions already made, including rejected theme relations."""
    relations = defaultdict(set)
    ranked_count = sum(theme.get('rank') is not None for theme in themes)
    theme_snapshots = []
    for theme in sorted(themes, key=lambda t: t['id']):
        reasons = theme['exclusion_reason_codes']
        for symbol in theme['members']:
            relations[symbol].add(theme['id'])
        theme_snapshots.append({
            'id': theme['id'], 'name': theme['name'], 'theme_member_count': theme['member_count'],
            'is_broad_concept': 'broad_concept' in reasons, 'is_region_theme': 'region' in reasons,
            'exclusion_reason_codes': reasons, 'theme_strength_5d': theme['strength'],
            'strength_unit': '加权个数', 'strength_window_trade_days': 5,
            'rank': theme.get('rank'), 'selected_theme': theme['selected_theme'],
            'ranking_status': 'not_executed' if reasons else 'unknown' if theme['strength'] is None else 'computed',
            'ranking_scope': 'eligible_positive_strength_concepts_with_full_classification_members',
            'missing_count': theme['missing_count'], 'theme_total_count': ranked_count, 'observed_theme_count': len(themes),
            'ranking_as_of': inputs.trade_date, 'ranking_rule_version': THEME_RANKING_VERSION,
        })
    top5 = {theme['id'] for theme in themes if theme.get('selected_theme')}
    theme_by_id = {theme['id']: theme for theme in theme_snapshots}
    role_fields = {'true_leader': ['return_20d', 'identity_score', 'high_proximity'],
                   'fallback_leader': ['return_20d', 'identity_score'],
                   'emotion': ['return_5d', 'acceleration_5_20', 'return_20d']}
    feature_keys = sorted({key for row in records for key in row['observation']['values']} | {'price_gaps', 'abnormal_move'})
    samples = []
    for row in records:
        obs, symbol = row['observation'], row['symbol']
        related = sorted(relations[symbol])
        primary = obs['primary_theme'].get('id')
        values = obs['values']
        role = obs['role']
        role_complete = completeness(values, role_fields.get(role, sorted({k for fields in role_fields.values() for k in fields})))
        missing = sorted(key for key in feature_keys if (
            obs.get(key, {}).get('status') not in ('known', 'approximate')
            if key in ('price_gaps', 'abnormal_move') else values.get(key) is None))
        pre = obs['eligibility']['inputs']
        hard_checks = {code: pre.get(label) for code, label in (
            ('out_of_scope', '研究范围'), ('not_trading', '正常交易'),
            ('qualification_paused', '资格允许观察'))}
        hard_checks['untrusted_current_price'] = obs['current_price_known']
        theme_checks = {'no_theme_in_top5': True}
        if not primary:
            applicable = [theme_by_id[key] for key in related if not theme_by_id[key]['exclusion_reason_codes']]
            theme_checks = ({'no_theme_membership': False} if not related else {'all_themes_excluded': False} if not applicable
                            else {'theme_inputs_missing': None} if any(t['ranking_status'] == 'unknown' for t in applicable)
                            else {'no_theme_in_top5': False})
        # Role calculation really runs even if a theme misses the top five.
        # Only allocation is skipped after a previous blocking stage.
        role_valid = True if role != 'other' else None if any(v is None for v in obs['conditions'].values()) else False
        steps = [
            _step('hard_validation', hard_checks),
            _step('theme_selection', theme_checks),
            _step('role', {'role_not_qualified': role_valid})]
        steps.append(_step('quota_eligibility', {'main_board_emotion': obs.get('quota_eligibility')},
                           executed='quota_eligibility' in obs))
        quota = obs.get('competed_quota_type')
        ready = quota is not None
        steps.append(_step('allocation', {'quota_exhausted': row['selected']}, executed=ready))
        blocked = next((step for step in steps if step['status'] in ('failed', 'unknown')), None)
        reasons = [code for step in steps for code in step['failed_reason_codes'] + step['unknown_reason_codes']]
        obs['role_condition_completeness'] = role_complete
        obs['data_completeness'] = {'status': 'complete' if not missing else 'partial', 'missing_fields': missing}
        obs['related_theme_ids_all'] = related
        obs['related_theme_count_all'] = len(related)
        obs['related_theme_count_in_top5'] = len(set(related) & top5)
        obs['decision'] = {
            'steps': steps, 'first_blocking_step': blocked['step'] if blocked else None,
            'blocking_reason_code': (blocked['failed_reason_codes'] + blocked['unknown_reason_codes'])[0] if blocked else None,
            'all_reason_codes': reasons, 'selected': row['selected'], 'comparison_eligible': ready,
            'competition_group': obs.get('theme_lead_evidence', {}).get('competition_group'),
            'competed_quota_type': quota, 'occupied_quota_type': quota if row['selected'] else None,
            'quota_limit': (1 if quota == 'emotion' else 2) if ready else None,
            'quota_fallback_allowed': False, 'rank_within_theme_role': obs.get('rank_within_theme_role'),
            'rank_global_role': obs.get('rank_global_role'), 'sorting_rule_version': SORT_VERSION,
            'comparison_group': [inputs.trade_date, primary, quota, role] if ready else None,
            'competed_themes': [t['id'] for t in themes if t.get('selected_theme') and t['id'] in relations[symbol]],
            'final_theme': primary, 'dedup_rule_version': DEDUP_VERSION,
            'dedup_decision_basis': 'theme_strength_then_limit_count_then_height_then_stable_id',
            # One security row exists before allocation; theme paths are merged
            # into that row, not into another security or a duplicate sample.
            'merged_into_row_id': symbol if len(set(related) & top5) > 1 else None,
        }
        obs['snapshot'] = {'as_of': inputs.trade_date, 'data_cutoff': inputs.cutoff,
                           'schema_version': VERSION, 'rule_version': obs['rule_version']}
        samples.append({'selected': row['selected'], 'comparable': ready, 'missing_features': missing})
    return {'schema_version': VERSION, 'as_of': inputs.trade_date, 'data_cutoff': inputs.cutoff,
            'cohort_definition_version': COHORT_VERSION, 'sorting_rule_version': SORT_VERSION,
            'dedup_rule_version': DEDUP_VERSION, 'publication_sample_policy': 'original_per_trade_date',
            'themes': theme_snapshots, 'feature_keys': feature_keys,
            'sample_counts': dict(Counter('selected' if s['selected'] else 'comparable_unselected' if s['comparable'] else 'audit_only' for s in samples)),
            'same_cycle_sample_count': len(samples), 'distinct_trade_dates': 1,
            'non_independent_notice': NON_INDEPENDENT_NOTE,
            'audit_missingness': missingness({'selected': [s for s in samples if s['selected']],
                                             'all_unselected': [s for s in samples if not s['selected']]}, feature_keys),
            'missingness': missingness({'selected': [s for s in samples if s['selected']],
                                       'comparable_unselected': [s for s in samples if s['comparable'] and not s['selected']]}, feature_keys)}
