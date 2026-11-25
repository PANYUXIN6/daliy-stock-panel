"""Current-format evidence for tests that exercise persistence without running the market pipeline."""

from app.services.evening_report_receipt import daily_partition_versions


def current_receipt(day, cutoff, **fields):
    covered = [*fields.get('active_symbols', ()), *fields.get('halted_symbols', ())]
    versions = {'stock_generation': 'test-generation', **fields.pop('versions', {'files': []})}
    dates = dict.fromkeys(daily_partition_versions(versions), cutoff)
    return {
        'date': day, 'cutoff': cutoff, 'complete': True,
        'versions': versions, 'calendar': {'days': [day]}, 'metadata': [],
        'classifications': [], 'classification_comparisons': [], 'sector_filter_config': {},
        'active_symbols': [], 'halted_symbols': [], 'halted_evidence': [], 'historical_suspensions': [],
        'market': {}, 'study_quotes': {}, 'wave_price_clocks': {'dates': dates, 'rows': {}, 'pending': {}},
        'wave_first_acceptances': dict.fromkeys(covered, cutoff), 'wave_st_reentry': {},
        'wave_factor_known_at': cutoff,
        'wave_factor_first_accepted': cutoff if fields.get('wave_factor_facts') is not None else None,
        'wave_factor_facts': None,
        **fields,
    }
