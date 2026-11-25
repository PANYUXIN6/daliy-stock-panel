"""Shape checks for the historical facts consumed from immutable evening inputs."""
from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
from itertools import pairwise
from sys import float_info


def predecessor_day(receipt, references, through):
    if day := receipt.get('wave_missing_predecessor_day'):
        return day
    pair = through, receipt.get('previous_accepted_input_id')
    day = min(receipt['date'], through[:10])
    if pair in references.get(day, ()):
        return day
    # A delayed acceptance can belong to an earlier session than its cutoff date.
    return next((d for d, entries in references.items() if pair in entries), day)


def valid_input_id(value):
    return isinstance(value, str) and len(value) == 32 and all(c in '0123456789abcdef' for c in value)


def _text(value):
    return isinstance(value, str) and bool(value)


@lru_cache(maxsize=2048)
def _iso_clock(value):
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def valid_acceptance_cutoff(value):
    return _text(value) and _iso_clock(value)


@lru_cache(maxsize=4096)
def _iso_day(value):
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _day(value):
    return _text(value) and _iso_day(value)


def valid_receipt_day(receipt):
    day = receipt.get('date')
    return _day(day) and day == receipt.get('_acceptance_index_day')


def daily_partition_versions(versions: dict) -> dict[str, list]:
    """Group the accepted raw file references by the market date they cover."""
    partitions = {}
    for ref in versions.get('files', []):
        path = ref[0]
        if path.startswith('kline_daily/date='):
            day = path.removeprefix('kline_daily/date=').split('/', 1)[0]
            partitions.setdefault(day, []).append(ref)
    return partitions


def _number(value):
    # JSON integers can exceed the range of a market-data float.
    return type(value) in (int, float) and -float_info.max <= value <= float_info.max


def _items(value, valid):
    return isinstance(value, list) and all(valid(item) for item in value)


def _mapping(value, keys, values):
    return isinstance(value, dict) and all(keys(k) and values(v) for k, v in value.items())


def _clock_map(value):
    return _mapping(value, _text, valid_acceptance_cutoff)


def _day_map(value):
    return _mapping(value, _text, _day)


def _bar_patch(row):
    if row is None:
        return True  # Proven absence before this correction.
    return (isinstance(row, dict) and valid_acceptance_cutoff(row.get('known_at'))
            and ('raw_known_at' not in row or valid_acceptance_cutoff(row['raw_known_at']))
            and all(isinstance(row.get(k), bool) for k in ('halted', 'wave_formal'))
            and ('raw_absent' not in row or (row['raw_absent'] is True and row['halted'] and not row['wave_formal']))
            and ('formal_raw' not in row or isinstance(row['formal_raw'], bool))
            and all(row.get(k) is None or _number(row[k])
                    for k in ('raw_open', 'raw_high', 'raw_low', 'raw_close', 'volume', 'amount')))


def _pending_patch(row):
    return _bar_patch(row) or (isinstance(row, dict) and row.keys() == {'unknown'} and row['unknown'] is True)


def _clocks(clocks):
    return (isinstance(clocks, dict)
            and _mapping(clocks.get('dates'), _day, valid_acceptance_cutoff)
            and _mapping(clocks.get('rows'), _day, _clock_map)
            and _mapping(clocks.get('pending'), _day, lambda rows: _mapping(rows, _text, _pending_patch)))


def _factor(row):
    return (isinstance(row, list) and len(row) == 2 and _number(row[0]) and row[0] > 0
            and valid_acceptance_cutoff(row[1]))


def _halt(row):
    return (isinstance(row, dict) and _text(row.get('symbol')) and _day(row.get('date'))
            and isinstance(row.get('confirmed', False), bool)
            and (row.get('obtained_at') is None or valid_acceptance_cutoff(row['obtained_at'])))


def _metadata(row):
    return (isinstance(row, dict) and _text(row.get('symbol'))
            and all(row.get(k) is None or isinstance(row[k], bool) for k in ('is_st', 'is_current')))


def _member(row):
    return (isinstance(row, dict) and _text(row.get('symbol')) and _text(row.get('sector_id'))
            and _text(row.get('source')) and _text(row.get('sector_name')))


def _observation(row):
    if (not isinstance(row, dict) or not _text(row.get('source')) or not _text(row.get('id'))
            or row.get('complete') is not True or not valid_acceptance_cutoff(row.get('obtained_at'))
            or not _items(row.get('memberships'), _member) or not _items(row.get('symbols'), _text)
            or (row.get('membership_semantics') is not None and not _text(row['membership_semantics']))):
        return False
    covered = set(row['symbols'])
    return all(member['symbol'] in covered for member in row['memberships'])


def _comparison(row):
    def member_pair(pair):
        return isinstance(pair, list) and len(pair) == 2 and all(_text(v) for v in pair)

    return (isinstance(row, dict) and _text(row.get('source'))
            and (row.get('classification_type') is None or _text(row['classification_type']))
            and ((row.get('comparison') == 'first_observation' and row.get('previous_observation_id') is None
                  and row.get('previous_obtained_at') is None)
                 or (row.get('comparison') == 'adjacent_complete_observation' and _text(row.get('previous_observation_id'))
                     and valid_acceptance_cutoff(row.get('previous_obtained_at'))))
            and all(_items(row.get(k), member_pair) for k in ('added', 'removed'))
            and _items(row.get('first_observed_symbols', []), _text))


def valid_receipt_body(receipt):
    """Validate the stored facts before replaying a historical acceptance."""
    from app.tickflow.daily_revisions import valid_file_references

    required = {'date', 'cutoff', 'versions', 'calendar', 'metadata', 'market', 'sector_filter_config',
                'classifications', 'classification_comparisons', 'active_symbols', 'halted_symbols',
                'halted_evidence', 'historical_suspensions', 'wave_first_acceptances', 'wave_st_reentry',
                'wave_factor_facts', 'wave_factor_known_at', 'wave_factor_first_accepted', 'wave_price_clocks'}
    if not required <= receipt.keys() or not valid_acceptance_cutoff(receipt['wave_factor_known_at']):
        return False
    versions = receipt['versions']
    if (not isinstance(versions, dict) or not _text(versions.get('stock_generation'))
            or not valid_file_references(versions.get('files'))):
        return False
    calendar = receipt['calendar']
    if (not isinstance(calendar, dict) or not _items(calendar.get('days'), _day)
            or any(earlier >= later for earlier, later in pairwise(calendar['days']))):
        return False
    if any(not isinstance(receipt[key], dict) for key in ('market', 'sector_filter_config')):
        return False
    if 'wave_missing_predecessor_day' in receipt and not _day(receipt['wave_missing_predecessor_day']):
        return False
    if 'wave_unverified_price_days' in receipt:
        affected = receipt['wave_unverified_price_days']
        if (not _day_map(affected) or not receipt.get('wave_missing_predecessor_day')
                or any(day > receipt['wave_missing_predecessor_day'] for day in affected.values())):
            return False
    for key in ('wave_factor_first_accepted', 'wave_previous_acceptance', 'wave_price_compared_after'):
        if receipt.get(key) is not None and not valid_acceptance_cutoff(receipt[key]):
            return False
    if (ref := receipt.get('previous_accepted_input_id')) is not None and not valid_input_id(ref):
        return False
    for key, validate in (('active_symbols', _text), ('halted_symbols', _text), ('metadata', _metadata),
                          ('halted_evidence', _halt), ('historical_suspensions', _halt), ('classifications', _observation),
                          ('classification_comparisons', _comparison)):
        if not _items(receipt[key], validate):
            return False
    observations = sorted((o['source'], o['id']) for o in receipt['classifications'])
    if any(not _text(c.get('observation_id')) for c in receipt['classification_comparisons']):
        return False
    if sorted((c['source'], c['observation_id']) for c in receipt['classification_comparisons']) != observations:
        return False
    def bounds_valid(row):
        return (isinstance(row, list) and len(row) == 3 and _text(row[0])
                and all(v is None or (_number(v) and v > 0) for v in row[1:]))

    def session_limits(proof):
        return (isinstance(proof, dict) and valid_input_id(proof.get('input_id'))
                and _items(proof.get('rows'), bounds_valid)
                and len({r[0] for r in proof['rows']}) == len(proof['rows']))

    if not _mapping(receipt.get('historical_limits', {}), _day, session_limits):
        return False
    clocks = receipt['wave_first_acceptances']
    if not _clock_map(clocks) or any(s not in clocks for key in ('active_symbols', 'halted_symbols') for s in receipt[key]):
        return False
    def quote_valid(value):
        return value is None or value == 'suspended' or (
            isinstance(value, list) and len(value) == 2 and all(_number(v) and v > 0 for v in value))

    if not _mapping(receipt.get('study_quotes'), _day, lambda quotes: _mapping(quotes, _text, quote_valid)):
        return False
    def quote_clocks_valid(pair):
        return _items(pair, valid_acceptance_cutoff) and len(pair) == 2 and max(pair) <= receipt['cutoff']

    if not _mapping(receipt.get('study_quote_clocks', {}), _day, lambda rows: _mapping(rows, _text, quote_clocks_valid)):
        return False
    facts = receipt['wave_factor_facts']
    if facts is not None and not _mapping(facts, _text, lambda rows: rows is None or _mapping(rows, _day, _factor)):
        return False
    bound = receipt.get('wave_price_bootstrap_from')
    if bound is not None and not _day_map(bound):
        return False
    return (_clocks(receipt['wave_price_clocks'])
            and daily_partition_versions(versions).keys() <= receipt['wave_price_clocks']['dates'].keys()
            and _mapping(receipt.get('wave_price_changes', {}), _text, lambda rows: _mapping(rows, _day, _bar_patch))
            and _mapping(receipt['wave_st_reentry'], _text, lambda start: start is None or _day(start)))
