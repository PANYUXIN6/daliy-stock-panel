"""Accepted postmarket evidence and strictly local, formal evening-report inputs.

File references detect a raw-only failed rewrite that does not advance enriched
publication generation. They never establish formalness or classification coverage.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter
from datetime import date
from pathlib import Path

import polars as pl

from app.enriched_generation import EnrichedGenerationUnavailableError, get_enriched_generation
from app.market_time import cn_now
from app.parquet import scan_enriched_parquet
from app.polars_guard import guarded_collect
from app.price_limits import NO_PRICE_LIMIT_SENTINEL
from app.services.daily_coverage import DailyCoverageError
from app.services.evening_report_price_facts import (
    accept_price_facts,
    accepted_study_quotes,
    add_accepted_price_times,
    raw_daily_flags,
    study_quote_clocks,
)
from app.services.evening_report_receipt import (
    daily_partition_versions,
    predecessor_day,
)
from app.services.evening_report_store import EveningReportStore
from app.services.trading_day import calendar_status
from app.tickflow import daily_revisions
from app.tickflow.sector_review_reader import SectorReviewReader


def input_versions(data_dir: Path, day: date) -> dict:
    reader = SectorReviewReader(data_dir)
    paths = []
    for table in ('kline_daily', 'kline_daily_enriched', 'kline_index_daily'):
        paths.extend(reader.files(table, end=day))
    factor_path = data_dir / 'adj_factor' / 'all.parquet'
    if factor_path.exists():
        paths.append(factor_path)
    references = []
    for path in sorted(paths):
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            raise DailyCoverageError('晚报输入文件已变化，请重新等待盘后验收') from exc
        references.append([str(path.relative_to(data_dir)), stat.st_size, stat.st_mtime_ns])
    # The benchmark consumes raw index daily bars, not index enriched data.
    # Index writes have no enriched publication marker; their actual input
    # partitions are covered by the file references above.
    return {'stock_generation': get_enriched_generation(data_dir, 'stock', initialize=False),
            'files': references}


def factor_file_reference(versions: dict) -> list | None:
    return next((ref for ref in versions.get('files', []) if ref[0] == 'adj_factor/all.parquet'), None)


def _wave_history_receipt(evidence, reentries):
    cutoff = evidence['cutoff']
    missing_day = evidence.get('wave_missing_predecessor_day')
    affected = evidence.get('wave_unverified_price_days')
    return {'cutoff': cutoff, 'facts': evidence['wave_factor_facts'],
            'source': factor_file_reference(evidence['versions']),
            'covered_symbols': [*evidence['active_symbols'], *evidence['halted_symbols']],
            'wave_price_changes': {} if missing_day and affected is None else evidence.get('wave_price_changes', {}),
            'wave_price_bootstrap_from': _scoped_price_bootstrap(evidence, reentries),
            'wave_previous_acceptance': evidence.get('wave_previous_acceptance'),
            'wave_price_compared_after': None if missing_day else evidence.get('wave_price_compared_after'),
            'wave_missing_predecessor_day': missing_day,
            'wave_unverified_price_days': affected,
            'source_known_at': evidence['wave_factor_known_at']}


def _record_missing_predecessor(store, references, previous, receipt, gaps, *, after=''):
    """Keep a missing link before a survivor independent of any changes after that survivor."""
    cutoff = receipt['cutoff']
    predecessor = receipt.get('wave_previous_acceptance')
    if not predecessor or receipt.get('wave_missing_predecessor_day'):
        return
    if previous and previous['cutoff'] == predecessor and previous['input_id'] == receipt.get('previous_accepted_input_id'):
        return
    if previous is None or previous['cutoff'] >= predecessor or previous['date'] > receipt['date']:
        # A version at the expected time with a different identity cannot fill this link.
        previous = store.latest_readable_input((
            (at, ref) for day, entries in references.items() if day <= receipt['date'] for at, ref in entries
            if at < predecessor or (at == predecessor and ref == receipt.get('previous_accepted_input_id'))
        ))
    if previous is None:
        # The successor identifies a real missing acceptance even if none of the earlier originals
        # can bound its stock rows. Keep its interval recoverable from the last published cutoff.
        if after < predecessor:
            day = predecessor_day(receipt, references, predecessor)
            gaps[cutoff] = after, predecessor, day
        return
    if previous['cutoff'] >= predecessor:
        return
    affected = _unverified_market_days(store.root, previous, daily_partition_versions(receipt['versions']),
                                       factor_file_reference(receipt['versions']), accepted=receipt)
    # A surviving row can be unchanged while its later receipts still carry a stale clock.
    end = predecessor_day(receipt, references, predecessor)
    gaps[cutoff] = previous['cutoff'], predecessor, {s: [first, end] for s, first in affected.items()}


def _accepted_history(store, references, current, after, reentries):
    """Read retained versions by acceptance time, including later recovery of an older trading day."""
    after = after or ''
    versions = sorted((at, input_id) for day, entries in references.items() if day <= current['date']
                      for at, input_id in entries if after < at < current['cutoff'])
    history, gaps = [], {}
    previous = None
    for at, input_id in versions:
        receipt = store.accepted_input(input_id, at)
        if receipt:
            if previous or (receipt.get('wave_previous_acceptance') or '') > after:
                _record_missing_predecessor(store, references, previous, receipt, gaps, after=after)
            history.append(_wave_history_receipt(receipt, reentries))
            previous = receipt
    if previous or (current.get('wave_previous_acceptance') or '') > after:
        _record_missing_predecessor(store, references, previous, current, gaps, after=after)
    return history, gaps


def _wave_st_reentry(evidence, previous):
    """Carry the first confirmed non-ST session after the latest accepted ST status."""
    reentry = dict(previous.get('wave_st_reentry', {}))
    for row in evidence['metadata']:
        symbol = row['symbol']
        if row.get('is_st') is True:
            reentry[symbol] = None
        elif row.get('is_st') is False and symbol in reentry and reentry[symbol] is None:
            reentry[symbol] = evidence['date']
    return reentry


def _next_stock_acceptances(checkpoint_cutoff, receipts):
    """First stock inputs on the uninterrupted chain following the published ledger."""
    result = {}
    prior = checkpoint_cutoff
    for receipt in receipts:
        predecessor = receipt['wave_previous_acceptance']
        if not prior or predecessor != prior or receipt.get('wave_missing_predecessor_day'):
            break
        prior = receipt['cutoff']
        for symbol in receipt['covered_symbols']:
            result.setdefault(symbol, prior)
        for symbol, facts in (receipt['facts'] or {}).items():
            if any(known == prior for _, known in facts.values()):
                result.setdefault(symbol, prior)
    return result


def _receipt_gap(after, receipt):
    predecessor, cutoff = receipt['wave_previous_acceptance'], receipt['cutoff']
    through = predecessor if predecessor and predecessor > after else cutoff
    day = receipt['wave_missing_predecessor_day']
    prices = receipt['wave_unverified_price_days']
    return after, through, {s: [first, day] for s, first in prices.items()} if prices is not None else day


def _unverified_market_days(root, previous, partitions, factor_ref, *, accepted=None):
    """Bound unobserved versions by raw stock rows and effective adjustment events."""
    if accepted and accepted.get('wave_missing_predecessor_day'):
        # Its differences span the gap and cannot date the change to the final batch.
        accepted = None
    previous_files = daily_partition_versions(previous['versions'])
    # A missing batch could first cover a deferred stock-day without any further
    # write to that price partition. Its applicable date is the bar's trading day.
    changed = {day: set(rows) for day, rows in previous['wave_price_clocks']['pending'].items() if rows}
    closed = daily_revisions.published_through(root)
    for day in previous_files.keys() | partitions.keys():
        if day <= closed or day > previous['date']:
            continue
        before, after = previous_files.get(day, []), partitions.get(day, [])
        if before != after:
            writes = daily_revisions.before_changes(root, day, before, after)
            if writes is None:
                changed[day] = None
            else:
                counts = Counter(symbol for write in writes for symbol in write)
                # A sole rewrite first accepted by the surviving batch was absent at its predecessor.
                changed.setdefault(day, set()).update(s for s, n in counts.items() if not accepted or n > 1
                                                      or day not in accepted.get('wave_price_changes', {}).get(s, {}))
    previous_ref = factor_file_reference(previous['versions'])
    if previous_ref and previous['wave_factor_facts'] is None:
        # A later batch could read the same physical file successfully.
        changed.update(dict.fromkeys(previous_files))
    elif previous_ref != factor_ref:
        writes = daily_revisions.factor_changes(root, previous_ref, factor_ref)
        if writes is None:
            # A replaced or unreadable all-market source can hide changes to any covered stock.
            changed.update(dict.fromkeys(previous_files))
        elif writes:
            first_day = min(previous_files.keys() | partitions.keys(), default=previous['date'])
            floors = _price_scope_floors(previous['metadata'], previous['wave_st_reentry'])
            counts = Counter((day, s) for write in writes for day, symbols in write.items() for s in symbols)
            facts = (accepted['wave_factor_facts'] or {}) if accepted else {}
            for (day, symbol), count in counts.items():
                if not max(first_day, floors.get(symbol, '')) < day <= previous['date'] or changed.get(day, ()) is None:
                    continue
                fact = (facts.get(symbol) or {}).get(day)
                if accepted and count == 1 and fact and fact[1] == accepted['cutoff']:
                    continue
                changed.setdefault(day, set()).add(symbol)
    return _price_bootstrap_by_symbol(changed, previous['metadata'], previous['wave_first_acceptances'].keys(),
                                      previous['wave_st_reentry'])


def _factor_resumptions(history, checkpoint):
    """Find the next jointly usable acceptance after each receipt without factors."""
    prior = checkpoint.get('factor_resumptions', {})
    missing = {*prior, *checkpoint.get('missing_factor_receipts', ())}
    missing.update(r['cutoff'] for r in history if r['facts'] is None)
    if not missing:
        return {}
    valid = sorted(set(prior.values()).union(r['cutoff'] for r in history if r['facts'] is not None))
    resumptions = {}
    for at in missing:
        i = bisect_left(valid, at)
        if i < len(valid):
            resumptions[at] = valid[i]
    return resumptions


def prepare_wave_history(store: EveningReportStore, inputs, evidence: dict, checkpoint: dict) -> None:
    prior_cutoff = checkpoint.get('cutoff')
    reentries = inputs.wave_st_reentry
    references = store.acceptance_references()
    history, prefix_gaps = _accepted_history(store, references, evidence, prior_cutoff, reentries)
    history.append(_wave_history_receipt(evidence, reentries))
    revisions = {}
    bootstrap = {}
    gaps = []
    observed = {}
    previous = prior_cutoff or ''
    for receipt in history:
        if receipt['wave_missing_predecessor_day']:
            observed[receipt['cutoff']] = _receipt_gap(previous, receipt)
        previous = receipt['cutoff']
    inputs.wave_factor_history = [r for r in history[:-1] if r['facts'] is not None]
    inputs.wave_factor_resumptions = _factor_resumptions(history, checkpoint)
    for receipt in history:
        predecessor = receipt['wave_previous_acceptance']
        if predecessor and predecessor > (prior_cutoff or ''):
            gaps.append((prior_cutoff or '', predecessor))
        prior_cutoff = receipt['cutoff']
        if earliest := receipt.get('wave_price_bootstrap_from'):
            bootstrap[prior_cutoff] = earliest
        for symbol, changes in receipt['wave_price_changes'].items():
            revisions.setdefault(symbol, []).append((prior_cutoff, changes))
    inputs.wave_price_revisions = revisions
    inputs.wave_price_bootstrap = bootstrap
    inputs.wave_next_stock_acceptances = _next_stock_acceptances(checkpoint.get('cutoff'), history) if bootstrap else {}
    inputs.wave_acceptance_gaps = gaps
    inputs.wave_unverified_receipts = {at: gap for at, gap in (prefix_gaps | observed).items() if gap[2]}
    # A surviving adjacent comparison can narrow an otherwise unbounded prefix against
    # each stock's published ledger, using the same row clocks as other acceptance gaps.
    inputs.wave_comparable_prefixes = {r['cutoff'] for r in history if r['cutoff'] in prefix_gaps
                                       and isinstance(prefix_gaps[r['cutoff']][2], str)
                                       and not r['wave_missing_predecessor_day']
                                       and r['wave_price_compared_after'] == r['wave_previous_acceptance']}


def _accept_factor_facts(factors: pl.DataFrame, previous: dict, cutoff: str) -> dict:
    """Keep the acceptance of each unchanged adjustment, including a confirmed removal."""
    accepted = {}
    for symbol, day, value in factors.iter_rows():
        if value is None:
            accepted[symbol] = None
        elif value != 1.:
            accepted.setdefault(symbol, {})[day] = value
    for symbol in previous:
        values = accepted.setdefault(symbol, {})
        if values is None:
            continue
        for day in previous[symbol] or {}:
            values.setdefault(day, 1.)
    for symbol, values in accepted.items():
        if values is None:
            continue
        prior = previous.get(symbol) or {}
        for day, value in values.items():
            old = prior.get(day)
            values[day] = old if old and old[0] == value else [value, cutoff]
    return accepted


def _price_scope_floors(metadata, reentries):
    from app.price_limits import parse_listing_date

    return {row['symbol']: max(listed.isoformat() if (listed := parse_listing_date(row.get('listing_date'))) else '',
                               reentries.get(row['symbol']) or '') for row in metadata}


def _price_bootstrap_by_symbol(unknown_days, metadata, covered, reentries):
    """Bound a warehouse rewrite by listing and the start of the current non-ST wave."""
    shared_days = sorted(day for day, symbols in unknown_days.items() if symbols is None)
    floors = _price_scope_floors(metadata, reentries)
    bounds = {}
    for symbol in covered:
        index = bisect_left(shared_days, floors.get(symbol, ''))
        if index < len(shared_days):
            bounds[symbol] = shared_days[index]
    for day, symbols in unknown_days.items():
        if symbols:
            for symbol in covered & symbols:
                if day >= floors.get(symbol, ''):
                    bounds[symbol] = min(day, bounds.get(symbol, day))
    return bounds


def _scoped_price_bootstrap(receipt, reentries):
    """Restrict a historical rewrite to the current non-ST wave."""
    bounds = receipt.get('wave_price_bootstrap_from')
    if not bounds or not reentries:
        return bounds
    bounds = bounds.copy()
    cutoff = receipt['cutoff']
    clocks = receipt['wave_price_clocks']
    scoped = {symbol: since for symbol, since in reentries.items() if since and bounds.get(symbol, since) < since}
    for symbol, since in scoped.items():
        bounds.pop(symbol)
        if cutoff[:10] <= since:
            continue
        changes = receipt.get('wave_price_changes', {}).get(symbol, {})
        candidates = (day for day, clock in clocks.get('dates', {}).items() if since <= day < cutoff[:10]
                      and day not in changes and clocks.get('rows', {}).get(day, {}).get(symbol, clock) == cutoff)
        if (earliest := min(candidates, default=None)) is not None:
            bounds[symbol] = earliest
    return bounds


def _restore_prior_halts(price_changes, previous):
    """A missing/non-trading raw row could still have a confirmed, separately sourced full-day halt."""
    for proof in [*previous.get('historical_suspensions', ()), *previous.get('halted_evidence', ())]:
        days = price_changes.get(proof['symbol'])
        day = str(proof['date'])
        if not days or day not in days or proof.get('confirmed') is not True:
            continue
        prior = days[day]
        if prior and prior['wave_formal']:
            continue
        known = proof.get('obtained_at') or previous['cutoff']
        if prior and prior['halted']:
            known = min(known, prior['known_at'])
        # A status receipt can explain an absent or nonformal raw row. Keep the actual
        # prior fields for price replay without attributing this status to an earlier receipt.
        raw = {'raw_absent': True} if prior is None else {**prior, 'formal_raw': prior['halted']}
        if prior and prior['known_at'] != known:
            raw['raw_known_at'] = prior['known_at']
        days[day] = {**raw, 'halted': True, 'wave_formal': False, 'known_at': known}


def _inherit_historical_suspensions(history, receipt, report_day):
    for item in receipt.get('historical_suspensions', ()):
        key = item['symbol'], str(item['date'])
        if key not in history:
            history[key] = item
    halts = receipt.get('halted_evidence', ())
    if not halts:
        return
    for item in halts:
        day = str(item['date'])
        if day < report_day:
            symbol = item['symbol']
            proof = {**item, 'source_input_id': receipt.get('input_id')}
            history[(symbol, day)] = proof


def _prior_day_inputs(store, day, references):
    """Keep the last successful reference separate from the newest readable market facts."""
    ref, at = store.historical_reference(day, references)
    original_id = (store.original_metadata(day) or {}).get('input_id')
    if not (ref or original_id):
        return None
    latest = store.accepted_input(ref, at, day=day)
    original = store.input_evidence(original_id) if original_id and original_id != ref else latest
    baseline = latest
    if ref and at and not latest:
        baseline = store.latest_readable_input((t, r) for t, r in references.get(day, ()) if t < at)
    proofs = [original] if original else []
    if baseline and (not original or original['input_id'] != baseline['input_id']):
        proofs.append(baseline)
    return ref, at, None if latest else day, proofs


def save_ready(data_dir: Path, day: date, coverage, observations: list[dict], market: dict,
               *, before_commit=None) -> dict:
    """Called only after required stages 1–8 succeeded, inside the pipeline slot."""
    if len(observations) != 2 or any(not o or o.get('complete') is not True for o in observations):
        raise DailyCoverageError('本轮概念及行业完整分类观察尚未就绪')
    if any(date.fromisoformat(o['obtained_at'][:10]) != day for o in observations):
        raise DailyCoverageError('分类观察并非目标交易日取得，不能作为当晚原发布输入')
    metadata = pl.read_parquet(data_dir / 'instruments' / 'instruments.parquet').to_dicts()
    expected = {row['symbol'] for row in metadata if _scope(row) is True}
    for observation in observations:
        missing = expected - set(observation['symbols'])
        if missing:
            raise DailyCoverageError(f"本轮分类响应缺少 {len(missing)} 只范围内证券，不能由合并旧表补齐")
    from app.services import evening_report_classification
    from app.services.preferences import get_mainline_filter_config
    from app.services.sentiment_observations import load_observation

    # Freeze the same dated domain result displayed by the environment page.
    sentiment = load_observation(data_dir, day.isoformat())
    market = {**market, 'sentiment': sentiment}

    store = EveningReportStore(data_dir)
    calendar = calendar_status(data_dir)
    previous_days = [value for value in calendar.get('days', []) if value < day.isoformat()]
    cutoff = cn_now().isoformat()
    versions = input_versions(data_dir, day)
    partitions = daily_partition_versions(versions)
    factor_ref = factor_file_reference(versions)
    factor_known_at = cutoff
    first_factor = None
    previous_factors_absent = True
    prior_factor_facts = None
    previous_receipt = {}
    previous_cutoff = missing_predecessor_day = None
    previous_accepted_input_id = None
    covered = coverage.active | coverage.halted
    first_acceptances = dict.fromkeys(covered, cutoff)
    historical_suspensions = {}
    historical_limits = {}
    previous_classification = None
    report_day = day.isoformat()
    references = store.acceptance_references()
    for prior_day in [*previous_days[-20:], report_day]:
        prior = _prior_day_inputs(store, prior_day, references)
        if not prior:
            continue
        previous_accepted_input_id, previous_cutoff, missing_predecessor_day, proofs = prior
        if missing_predecessor_day:
            previous_factors_absent = False
        for proof in proofs:
            if proof['wave_factor_facts'] is not None:
                prior_factor_facts = proof['wave_factor_facts']
            previous_factors_absent = previous_factors_absent and proof['wave_factor_facts'] is None
            previous_receipt = proof
            for symbol, first in proof['wave_first_acceptances'].items():
                first_acceptances[symbol] = min(first_acceptances.get(symbol, first), first)
            established = proof['wave_factor_first_accepted']
            if established:
                first_factor = min(first_factor or established, established)
            if (factor_ref and proof['wave_factor_facts'] is not None
                    and factor_file_reference(proof['versions']) == factor_ref):
                factor_known_at = min(factor_known_at, proof['wave_factor_known_at'])
            _inherit_historical_suspensions(historical_suspensions, proof, report_day)
        if prior_day < report_day and proofs and not missing_predecessor_day:
            historical_limits[prior_day] = _accepted_limit_bounds(proofs[-1])
        if prior_day == report_day or (previous_days and prior_day == previous_days[-1]):
            previous_classification = proofs[-1] if proofs and not missing_predecessor_day else None
    if not previous_receipt and len(previous_days) > 20:
        # Maintenance preserves the newest successful input even after an extended outage.
        older_days = {path.stem for directory in ('ready', 'reports') for path in (store.root / directory).glob('*.json')
                      if path.stem < previous_days[-20]}
        older_days.update(d for d in references if d < previous_days[-20])
        for older_day in sorted(older_days, reverse=True):
            prior = _prior_day_inputs(store, older_day, references)
            if not prior:
                continue
            receipt_id, accepted_at, missing, proofs = prior
            if not missing_predecessor_day and not previous_cutoff:
                previous_accepted_input_id = receipt_id
                previous_cutoff, missing_predecessor_day = accepted_at, missing
                if missing_predecessor_day:
                    previous_factors_absent = False
            if proofs:
                retained = proofs[-1]
                previous_receipt = retained
                _inherit_historical_suspensions(historical_suspensions, retained, report_day)
                previous_factors_absent = previous_factors_absent and retained['wave_factor_facts'] is None
                first_acceptances.update(retained['wave_first_acceptances'])
                prior_factor_facts = retained['wave_factor_facts']
                first_factor = retained['wave_factor_first_accepted']
                if factor_ref and prior_factor_facts is not None and factor_file_reference(retained['versions']) == factor_ref:
                    factor_known_at = retained['wave_factor_known_at']
                break
    factors, _ = wave_factor_inputs(data_dir, day, versions)
    if factors is not None:
        first_factor = min(first_factor or factor_known_at, factor_known_at)
    factor_facts = None if factors is None else _accept_factor_facts(
        factors, prior_factor_facts or {}, factor_known_at if prior_factor_facts is None else cutoff)
    # 关系源/行业语义变化后不续接旧来源的核心身份与连续轮次；历史证据不回写。
    membership_semantics = sorted({o['membership_semantics'] for o in observations if o.get('membership_semantics')})
    scope_version = 'main-chinext-nonst-v1/' + evening_report_classification.RULE_VERSION
    if membership_semantics:
        scope_version += '/' + '+'.join(membership_semantics)
    evidence = {
        'date': day.isoformat(), 'cutoff': cutoff,
        'versions': versions, 'classifications': observations,
        'active_symbols': sorted(coverage.active), 'halted_symbols': sorted(coverage.halted),
        'halted_evidence': list(coverage.halted_evidence.values()),
        'market': market, 'complete': True, 'metadata': metadata,
        'calendar': calendar, 'sector_filter_config': get_mainline_filter_config(),
        'filter_rule_version': evening_report_classification.RULE_VERSION,
        'scope_version': scope_version,
        'historical_suspensions': list(historical_suspensions.values()),
        'historical_limits': {d: bounds for d in previous_days[-20:]
                              if (bounds := historical_limits.get(d, previous_receipt.get('historical_limits', {}).get(d)))},
        'wave_factor_known_at': factor_known_at, 'wave_factor_facts': factor_facts,
        'wave_factor_first_accepted': first_factor,
    }
    evidence['classification_comparisons'] = _classification_comparisons(evidence, previous_classification)
    if coverage.halted - coverage.halted_evidence.keys():
        raise DailyCoverageError('部分停牌股票缺少逐股来源证据')
    previous_files = daily_partition_versions(previous_receipt.get('versions', {}))
    unverified_price_days = None
    if previous_cutoff and missing_predecessor_day == previous_receipt.get('date') and previous_files:
        # A missing last body proves no prices. The earlier original and the warehouse
        # can still bound which stocks could have qualified before that last acceptance.
        unverified_price_days = _unverified_market_days(store.root, previous_receipt, partitions, factor_ref)
    elif previous_receipt and not missing_predecessor_day:
        prior_day = previous_receipt['date']
        selected = previous_accepted_input_id, previous_cutoff
        if (store.historical_reference(prior_day) != selected
                and (affected := _unverified_market_days(store.root, previous_receipt, partitions, factor_ref))):
            # A promoted receipt proves its own facts. Without a last-acceptance witness a
            # separate accepted market state may have existed between it and today's input.
            unverified_price_days = affected
            previous_accepted_input_id = previous_cutoff = None
            missing_predecessor_day = prior_day
    if unverified_price_days is not None:
        evidence['wave_unverified_price_days'] = unverified_price_days
    required = _price_scope(evidence, _memberships(evidence)[0])
    prior_required = _price_scope(previous_receipt, _memberships(previous_receipt)[0]) if previous_receipt else set()
    price_state, price_changes, unknown_days = accept_price_facts(
        data_dir, store.root, partitions, previous_receipt, previous_files, covered, first_acceptances, cutoff,
        required_symbols=required, added_symbols=required - prior_required,
        waiting_bars=store.waiting_wave_bars(daily_revisions.published_through(store.root)),
        missing_predecessor_day=missing_predecessor_day if unverified_price_days is None else None)
    evidence.update(price_state, wave_first_acceptances=first_acceptances)
    if previous_cutoff:
        evidence['wave_previous_acceptance'] = previous_cutoff
    if previous_accepted_input_id:
        evidence['previous_accepted_input_id'] = previous_accepted_input_id
    evidence['wave_st_reentry'] = reentries = _wave_st_reentry(evidence, previous_receipt)
    if missing_predecessor_day:
        evidence['wave_missing_predecessor_day'] = missing_predecessor_day
    elif previous_receipt and not unknown_days:
        evidence['wave_price_compared_after'] = previous_receipt['cutoff']
    evidence["study_quotes"] = accepted_study_quotes(data_dir, partitions, report_day, coverage, price_changes, unknown_days)
    # With a proven first valid factor, a raw rewrite cannot make a wave knowable earlier.
    if unknown_days and not (first_factor == cutoff and previous_factors_absent):
        previously_covered = {s for s in covered if first_acceptances[s] < cutoff}
        affected = _price_bootstrap_by_symbol(unknown_days, metadata, previously_covered, reentries)
        if affected:
            evidence['wave_price_bootstrap_from'] = affected
    if price_changes:
        _restore_prior_halts(price_changes, previous_receipt)
        evidence['wave_price_changes'] = price_changes
    quote_clocks = study_quote_clocks(previous_receipt if not missing_predecessor_day else {},
                                      evidence['study_quotes'], price_changes, unknown_days, cutoff)
    if quote_clocks:
        evidence['study_quote_clocks'] = quote_clocks
    store.save_readiness(report_day, evidence, before_commit=before_commit)
    return evidence


def require_ready(data_dir: Path, day: date, *, input_id: str | None = None) -> dict:
    store = EveningReportStore(data_dir)
    evidence = store.input_evidence(input_id) if input_id else store.readiness(day.isoformat())
    if input_id and evidence is None:
        raise DailyCoverageError(f'{day} 的完整输入证据已过保留期或不存在；可核验原快照，不能完整重演历史筛选')
    if not evidence or not evidence.get('complete'):
        raise DailyCoverageError('等待正式行情、分类与市场环境完成本轮验收')
    if evidence.get('date') != day.isoformat() or not evidence.get('metadata') or not evidence.get('calendar'):
        raise DailyCoverageError(f'{day} 缺少可恢复的当时证券资格或交易日历证据')
    try:
        current = input_versions(data_dir, day)
    except EnrichedGenerationUnavailableError as exc:
        raise DailyCoverageError('指标版本正在发布或不可用，等待正式输入恢复') from exc
    if evidence.get('versions', {}).get('files') != current['files']:
        raise DailyCoverageError('就绪后输入版本已变化，等待新的盘后验收记录')
    return evidence


def _accepted_limit_bounds(receipt):
    from app.services.evening_report_rules import finite

    rows = []
    for record in receipt['metadata']:
        if str(record.get('as_of')) != receipt['date']:
            continue
        up, down = (v if type(v) in (float, int) and finite(v, positive=True) else None for v in
                    (record.get('limit_up'), record.get('limit_down')))
        if up is not None or down is not None:
            rows.append([record['symbol'], up, down])
    return {'input_id': receipt['input_id'], 'rows': rows} if rows else None


def _historical_limits(history):
    if history:
        rows = [(symbol, date.fromisoformat(day), up, down) for day, proof in history.items()
                for symbol, up, down in proof['rows']]
        return pl.DataFrame(rows, schema={'symbol': pl.String, 'date': pl.Date,
                                          '_dated_limit_up': pl.Float64, '_dated_limit_down': pl.Float64},
                            orient='row')


def _price_continuity(frame, calendar, halts, instruments, market_start):
    """Verify successive traded bars and the opening seed against the covered sessions."""
    from app.price_limits import parse_listing_date

    dates = [date.fromisoformat(d) if isinstance(d, str) else d for d in calendar]
    session = pl.col('date').replace_strict({d: i for i, d in enumerate(dates)}, default=None,
                                            return_dtype=pl.Int32)
    frame = frame.with_columns(session.alias('_session')).with_columns(
        pl.col('_session').shift().over('symbol').alias('_previous_session'))
    distance = pl.col('_session') - pl.col('_previous_session')
    continuous = (distance == 1).fill_null(False)
    if halts:
        bridges = [(symbol, current) for symbol, previous, current in
                   frame.filter(distance > 1).select('symbol', '_previous_session', '_session').iter_rows()
                   if all((symbol, dates[i]) in halts for i in range(previous + 1, current))]
        if bridges:
            keys = pl.DataFrame(bridges, schema={'symbol': pl.String, '_session': pl.Int32}, orient='row')
            frame = frame.join(keys.with_columns(_halt_bridge=True), on=['symbol', '_session'], how='left')
            continuous = continuous | pl.col('_halt_bridge').fill_null(False)
    missing_prefix = []
    if 'listing_date' in instruments.columns:
        listings = dict(instruments.select('symbol', 'listing_date').iter_rows())
        first_sessions = frame.group_by('symbol').agg(pl.col('_session').first())
        for symbol, first in first_sessions.drop_nulls().iter_rows():
            listed = parse_listing_date(listings.get(symbol))
            if listed:
                beginning = bisect_left(dates, max(market_start, listed))
                if any((symbol, dates[i]) not in halts for i in range(beginning, first)):
                    missing_prefix.append(symbol)
    seed = pl.col('date').shift().over('symbol').is_null()
    return frame.with_columns(
        continuous.alias('_continuous'), (seed & ~pl.col('symbol').is_in(missing_prefix)).alias('_indicator_seed'),
    ).drop('_session', '_previous_session')


def _prices(reader: SectorReviewReader, day: date, instruments: pl.DataFrame, cancel_check=None,
            suspension_evidence: list[dict] | None = None, *, calendar, accepted_halts=(), required_symbols=None, dated_limits=None) -> pl.DataFrame:
    paths = reader.files('kline_daily', end=day)
    if not paths:
        raise DailyCoverageError('正式历史日线为空')
    start = date.fromisoformat(paths[0].parent.name[5:])
    required = required_symbols if required_symbols is not None else instruments['symbol'].to_list()

    def confirmed_rows(frame):
        duplicate = pl.struct('symbol', 'date').is_duplicated()
        if not frame.select(duplicate.any()).item():
            return frame
        distinct = frame.unique(maintain_order=True)
        if distinct.select((duplicate & pl.col('symbol').is_in(required)).any()).item():
            raise DailyCoverageError('正式历史行情存在冲突的证券日期')
        return distinct.filter(~duplicate)

    raw = confirmed_rows(reader.raw('kline_daily', start, day)).sort('symbol', 'date')
    halt, wave_formal, formal_raw = raw_daily_flags()
    raw = raw.with_columns(halt.alias('_halt'),
                           wave_formal.alias('_wave_formal'), formal_raw.alias('_formal_raw'))
    # ATR is recursive: an earlier snapshot cannot silently become a formal seed.
    raw_halts = raw.filter(pl.col('_halt') & pl.col('_formal_raw')).select('symbol', 'date')
    if suspension_evidence is not None:
        suspension_evidence.extend(raw_halts.to_dicts())
    halts = set(raw_halts.iter_rows())
    status_halts = {(proof['symbol'], date.fromisoformat(str(proof['date']))) for proof in accepted_halts
                    if proof.get('confirmed') is True}
    halts.update(status_halts)
    if status_halts:
        # A snapshot on a confirmed halt is not a traded bar in the subsequent indicator window.
        keys = pl.DataFrame(list(status_halts), schema={'symbol': pl.String, 'date': pl.Date}, orient='row')
        raw = raw.join(keys.with_columns(_status_halt=True), on=['symbol', 'date'], how='left').filter(
            ~pl.col('_status_halt').fill_null(False) | pl.col('_wave_formal'))
    enriched_paths = reader.files('kline_daily_enriched', start, day)
    if not enriched_paths:
        raise DailyCoverageError('正式历史指标为空')
    lf = scan_enriched_parquet(enriched_paths, hive_partitioning=True)
    names = set(lf.collect_schema().names())
    wanted = ['symbol', 'date', 'open', 'low', 'close', 'high', 'ma5', 'ma10', 'ma20', 'bull_bear_line', 'atr_14', 'high_60d',
              'signal_limit_up', 'signal_limit_down', 'consecutive_limit_ups',
              'raw_close', 'raw_high', 'raw_low', 'amount', 'volume', 'quote_ts']
    enriched = guarded_collect(lf.select([pl.col(c) if c in names else pl.lit(None).alias(c)
                                         for c in wanted]))
    enriched = confirmed_rows(enriched)
    raw = raw.select('symbol', 'date', '_halt', '_formal_raw', '_wave_formal',
                     *[pl.col(c).alias('_source_' + c) for c in ('open', 'high', 'low', 'close', 'amount', 'volume')])
    frame = raw.join(enriched, on=['symbol', 'date'], how='left')
    equality = pl.all_horizontal(
        (pl.col(left) - pl.col('_source_' + right)).abs() < 0.005
        for left, right in [('raw_close', 'close'), ('raw_high', 'high'), ('raw_low', 'low'),
                            ('amount', 'amount'), ('volume', 'volume')])
    frame = frame.filter(~(pl.col('_halt') & pl.col('_formal_raw')).fill_null(False)).with_columns(
        (pl.col('_formal_raw') & ~pl.col('_halt') & pl.col('quote_ts').is_null()
         & equality).fill_null(False).alias('formal'),
        *[pl.col('_source_' + key).alias('raw_' + key) for key in ('open', 'close', 'high', 'low')],
        *[pl.when(pl.col('_source_' + key).is_finite() & (pl.col('_source_' + key) >= 0))
          .then(pl.col('_source_' + key)).otherwise(None).alias('wave_' + key) for key in ('volume', 'amount')],
        pl.col('_wave_formal').alias('wave_formal'),
    ).drop([c for c in frame.columns if c.startswith('_')]).sort('symbol', 'date')
    frame = frame.with_columns(pl.when(pl.col('formal')).then(pl.col(c)).alias(c)
                               for c in ('open', 'high', 'low', 'close'))
    # Disk enriched partitions intentionally contain only storage columns. Reuse
    # the indicator pipeline in bounded batches, including the entire recursive
    # seed history checked above; never substitute missing disk columns with zero.
    from app.indicators.pipeline import compute_indicators, compute_limit_signals

    frame = _price_continuity(frame, calendar, halts, instruments, start)
    symbols = frame['symbol'].unique().sort().to_list()
    parts = []
    for offset in range(0, len(symbols), 256):
        if cancel_check:
            cancel_check()
        batch = symbols[offset:offset + 256]
        part = compute_indicators(frame.filter(pl.col('symbol').is_in(batch)),
                                  needed={'ma5', 'ma10', 'ma20', 'bull_bear_line', 'atr_14', 'high_60d', 'change_pct', 'vol_ratio_5d'})
        part = compute_limit_signals(part, instruments.filter(pl.col('symbol').is_in(batch)),
                                     dated_limits=dated_limits,
                                     reference_continuous=pl.col('_continuous') & pl.col('formal').shift().over('symbol'),
                                     needed={'signal_limit_up', 'signal_limit_down', 'consecutive_limit_ups'})
        formal = pl.col('formal')
        checked = [pl.when(formal).then(c).alias(c) for c in ('signal_limit_up', 'signal_limit_down')]
        # N traded closes require N formal bars and N-1 verified transitions.
        link = pl.col('_continuous')
        for length, column in ((5, 'ma5'), (10, 'ma10'), (20, 'ma20'), (60, 'high_60d')):
            valid = formal.rolling_min(length).over('symbol') & link.rolling_min(length - 1).over('symbol')
            checked.append(pl.when(valid).then(column).alias(column))
        recursive_valid = (formal & (link | pl.col('_indicator_seed'))).cum_min().over('symbol')
        checked.extend(pl.when(recursive_valid).then(c).alias(c) for c in ('atr_14', 'bull_bear_line'))
        part = part.with_columns(checked)
        # A verified non-limit session resets a streak even after an unknown session.
        reset = ((~pl.col('signal_limit_up')).fill_null(False)).cum_sum().over('symbol')
        part = part.with_columns(reset.alias('_reset')).with_columns(
            pl.when((pl.col('signal_limit_up').is_not_null() & (pl.col('_continuous') | ~pl.col('signal_limit_up')))
                    .cum_min().over('symbol', '_reset'))
            .then('consecutive_limit_ups').alias('consecutive_limit_ups'))
        keep = [column for column in [*wanted, 'formal', 'raw_open', 'wave_formal', 'wave_volume', 'wave_amount'] if column in part.columns]
        parts.append(part.select(keep))
    return pl.concat(parts, how='diagonal_relaxed')


def _scope(record: dict) -> bool | None:
    checks = [record.get('type') == 'stock' if record.get('type') else None,
              record.get('exchange') in ('SH', 'SZ') if record.get('exchange') else None,
              record.get('board') in ('main', 'chinext') if record.get('board') else None,
              record.get('security_type') == 'a_share' if record.get('security_type') else None,
              not record['is_st'] if record.get('is_st') is not None else None,
              record.get('is_current') is not False]
    return False if False in checks else (None if None in checks else True)


def _price_scope(evidence, members):
    return {r['symbol'] for r in evidence['metadata'] if _scope(r) is True} | set(members['symbol'])


def _metadata(evidence: dict, calendar: list[str], suspensions: list[dict]) -> pl.DataFrame:
    from app.price_limits import is_no_limit_day, parse_listing_date, price_limit_pct

    rows = []
    active, halted = set(evidence['active_symbols']), set(evidence['halted_symbols'])
    recent = set(calendar[-21:])
    historical_halts = {r['symbol'] for r in suspensions if str(r['date']) in recent}
    day = date.fromisoformat(evidence['date'])
    for record in evidence['metadata']:
        symbol = record['symbol']
        in_scope = _scope(record)
        normal = False if symbol in halted else (True if symbol in active else None)
        listing = parse_listing_date(record.get('listing_date'))
        day_rank = sum(d >= listing.isoformat() for d in calendar) if listing else None
        regime = None
        if record.get('board') and record.get('is_st') is not None:
            if is_no_limit_day(symbol, listing, day, day_rank=day_rank):
                regime = 'no_limit'
            else:
                regime = f"{price_limit_pct(symbol, day, is_risk_warning=record['is_st']) * 100:g}%"
        rows.append({**record, 'in_scope': in_scope, 'trading_normal': normal, 'regime': regime,
                     'suspension_window': symbol in historical_halts and symbol not in halted,
                     'qualification_pause': symbol in halted})
    return pl.DataFrame(rows, infer_schema_length=None)


def _member_keys(observation):
    return {(row['symbol'], row['sector_id']) for row in observation['memberships']}


def _classification_comparisons(evidence, previous):
    """Pin adjacent observation deltas while both accepted inputs are still available."""
    previous_by_source = {o['source']: o for o in previous['classifications']} if previous else {}
    reused = {c['source']: c for c in previous['classification_comparisons']} if previous else {}
    changes = []
    for observation in evidence['classifications']:
        prior = previous_by_source.get(observation['source'])
        if prior and prior.get('membership_semantics') != observation.get('membership_semantics'):
            prior = None
        if (prior and previous.get('date') == evidence['date'] and prior['id'] == observation['id']
                and prior.get('obtained_at') == observation.get('obtained_at')):
            # A retry may reuse the very same source response. Keep its original comparison.
            changes.append(reused[observation['source']])
            continue
        added, removed, first_symbols = [], [], []
        if prior:
            current_members, prior_members = _member_keys(observation), _member_keys(prior)
            prior_scope = set(prior['symbols'])
            current_scope = set(observation['symbols'])
            added = [list(member) for member in sorted(current_members - prior_members) if member[0] in prior_scope]
            removed = [list(member) for member in sorted(prior_members - current_members) if member[0] in current_scope]
            first_symbols = sorted(current_scope - prior_scope)
        rows = observation['memberships'] or (prior or {}).get('memberships', ())
        changes.append({'source': observation['source'], 'comparison': 'adjacent_complete_observation' if prior else 'first_observation',
                        'classification_type': rows[0].get('source') if rows else None,
                        'observation_id': observation['id'],
                        'previous_observation_id': prior['id'] if prior else None,
                        'previous_obtained_at': prior.get('obtained_at') if prior else None,
                        'added': added, 'removed': removed,
                        **({'first_observed_symbols': first_symbols} if first_symbols else {}),
                        'interpretation': '比较口径发生变化，排名变化原因尚未分离' if added or removed else None})
    return changes


def _classification_changes(evidence):
    observations = {o['source']: o for o in evidence['classifications']}
    changes = []
    for comparison in evidence['classification_comparisons']:
        observation = observations[comparison['source']]
        first_symbols = set(comparison.get('first_observed_symbols', ()))
        first_observation = comparison['comparison'] == 'first_observation'
        members = _member_keys(observation) if first_observation or first_symbols else ()
        if first_observation:
            first_symbols.update(observation['symbols'])
        changes.append({**comparison, 'first_observed_symbols': sorted(first_symbols),
                        'first_observed': sorted([symbol, sector] for symbol, sector in members if symbol in first_symbols)})
    return changes


def _memberships(evidence: dict) -> tuple[pl.DataFrame, dict]:
    from collections import Counter, defaultdict

    from app.services import evening_report_classification

    rows = [row for observation in evidence['classifications'] for row in observation['memberships']]
    cfg = evidence['sector_filter_config']
    counts = defaultdict(set)
    for row in rows:
        if row['source'] == 'concept':
            counts[row['sector_id']].add(row['symbol'])
    excluded = []
    for sector_id, symbols in sorted(counts.items()):
        declared = evening_report_classification.exclusion(sector_id)
        reasons = [declared[1]] if declared else []
        if len(symbols) > 120:
            reasons.append('分类完整成员数超过120只')
        if reasons:
            excluded.append({'sector_id': sector_id, 'members': len(symbols), 'reasons': reasons,
                             'category': declared[0] if declared else 'configured_filter'})
    blocked = {entry['sector_id'] for entry in excluded}
    kept = [row for row in rows if row['source'] != 'concept' or row['sector_id'] not in blocked]
    frame = pl.DataFrame(kept, schema={'symbol': pl.String, 'sector_id': pl.String,
                                      'sector_name': pl.String, 'source': pl.String})
    return frame, {'config': {'max_members': 120}, 'previous_config': cfg,
                   'rule_version': evening_report_classification.RULE_VERSION, 'excluded': excluded,
                   'excluded_counts': dict(Counter(item['category'] for item in excluded)),
                   'limitation': '明确非产业标签按版本化完整名称排除；成员上限按本轮完整分类统计，未列入语义映射的标签不凭名称猜测'}


def wave_factor_inputs(data_dir: Path, day: date, versions: dict) -> tuple[pl.DataFrame | None, list | None]:
    source = factor_file_reference(versions)
    if source is None:
        return None, None
    from app.indicators.pipeline import validated_factor_events

    try:
        factors = validated_factor_events(pl.read_parquet(data_dir / source[0]), through=day)
    except (OSError, pl.exceptions.PolarsError):
        return None, source
    if factors["symbol"].null_count():
        return None, source
    return factors.with_columns(pl.col('trade_date').cast(pl.String)).sort('symbol', 'trade_date'), source


def load_inputs(data_dir: Path, day: date, evidence: dict, cancel_check=None):
    from app.services.evening_report_engine import EveningInputs

    check = cancel_check or (lambda: None)
    check()
    calendar = evidence.get('calendar', {}).get('days', [])
    if day.isoformat() not in calendar:
        raise DailyCoverageError('交易日历尚未确认目标日')
    before = [d for d in calendar if d <= day.isoformat()]
    future = [d for d in calendar if d > day.isoformat()]
    reader = SectorReviewReader(data_dir)
    instruments = pl.DataFrame(evidence['metadata'], infer_schema_length=None)
    historical_suspensions = []
    memberships, sector_filter = _memberships(evidence)
    required = _price_scope(evidence, memberships)
    prices = _prices(reader, day, instruments, check, historical_suspensions, calendar=before,
                     accepted_halts=[*evidence.get('historical_suspensions', []), *evidence['halted_evidence']],
                     required_symbols=list(required),
                     dated_limits=_historical_limits(evidence.get('historical_limits', {})))
    prices = add_accepted_price_times(prices, evidence)
    if historical_suspensions:
        historical_suspensions = add_accepted_price_times(
            pl.DataFrame(historical_suspensions), evidence).to_dicts()
    for suspension in historical_suspensions:
        suspension.update(confirmed=True, source='formal_daily_history', basis='formal_daily_zero_price_or_volume_amount',
                          source_version=evidence['versions']['stock_generation'],
                          obtained_at=suspension.pop('known_at') or evidence['cutoff'])
    suspensions = {
        (item['symbol'], str(item['date'])): item
        for item in [*historical_suspensions, *evidence.get('historical_suspensions', []), *evidence['halted_evidence']]
    }
    if suspensions:
        # A confirmed halt explains a missing bar until an accepted trading bar replaces it.
        restored = prices.select('symbol', pl.col('date').cast(pl.String), 'wave_formal').join(
            pl.DataFrame(list(suspensions), schema=['symbol', 'date'], orient='row'),
            on=['symbol', 'date'], how='semi',
        ).filter(pl.col('wave_formal')).select('symbol', 'date')
        for key in restored.iter_rows():
            del suspensions[key]
    suspension_refs = list(suspensions.values())
    check()
    all_benchmarks = reader.raw('kline_index_daily', date.fromisoformat(before[0]), day).with_columns(
        (pl.col('quote_ts').is_null() & pl.col('close').is_finite() & (pl.col('close') > 0)).alias('formal'))
    benchmark = all_benchmarks.filter(pl.col('symbol') == '000001.SH')
    factor_source = factor_file_reference(evidence['versions'])
    facts = evidence['wave_factor_facts']
    wave_factors = None
    invalid_factors = []
    if facts is not None:
        rows = []
        for symbol, events in facts.items():
            if events is None:
                invalid_factors.append(symbol)
                rows.append((symbol, None, None))
            else:
                rows.extend((symbol, d, value) for d, (value, _) in sorted(events.items()) if value != 1.)
        wave_factors = pl.DataFrame(rows, schema={'symbol': pl.String, 'trade_date': pl.String, 'ex_factor': pl.Float64},
                                    orient='row')
    if invalid_factors:
        adjusted_fields = ('open', 'high', 'low', 'close', 'ma5', 'ma10', 'ma20', 'bull_bear_line', 'atr_14', 'high_60d')
        prices = prices.with_columns(pl.when(~pl.col('symbol').is_in(invalid_factors)).then(pl.col(k)).alias(k)
                                     for k in adjusted_fields if k in prices.columns)
    check()
    return EveningInputs(trade_date=day.isoformat(), next_trade_date=future[0] if future else None,
                         cutoff=evidence['cutoff'], calendar=before, prices=prices,
                         metadata=_metadata(evidence, before, suspension_refs), memberships=memberships,
                         benchmark=benchmark, abnormal_benchmarks=all_benchmarks, market=evidence['market'],
                         wave_factors=wave_factors,
                         study_price_receipt=evidence,
                         wave_st_reentry=evidence['wave_st_reentry'],
                         wave_first_acceptances={s: evidence['wave_first_acceptances'][s] for s in
                                                  [*evidence['active_symbols'], *evidence['halted_symbols']]},
                         provenance={'classifications': evidence['classifications'], 'sector_filter': sector_filter,
                                     'calendar': evidence['calendar'],
                                     'scope_version': evidence.get('scope_version'),
                                     'suspensions': suspension_refs,
                                     'membership_changes': _classification_changes(evidence),
                                     'input_id': evidence.get('input_id'), 'input_versions': evidence['versions'],
                                     'wave_factor_source': factor_source,
                                     'wave_factor_first_accepted': evidence['wave_factor_first_accepted'],
                                     'wave_factor_known_at': evidence['wave_factor_known_at'],
                                     'wave_factor_facts': evidence.get('wave_factor_facts') or {}})


def prepare_evaluation_inputs(inputs) -> None:
    """Recompute both endpoints from raw closes at one explicit factor cutoff.

    Runs only in the post-publication evaluator, never while reading a report.
    Report-date enriched values are not reused as evaluation prices.
    """
    from app.indicators.pipeline import _apply_adj_factor

    factors = inputs.wave_factors
    source = inputs.provenance.get('wave_factor_source')
    if factors is None or not source:
        raise DailyCoverageError('客观回看缺少可核验的复权依据')
    dates = (pl.col('date').str.to_date() if inputs.prices.schema['date'] == pl.String else pl.col('date').cast(pl.Date))
    frame = inputs.prices.with_columns(dates)
    raw = frame.select('symbol', 'date', pl.col('raw_close').alias('close'))
    adjusted = _apply_adj_factor(raw, factors).select('symbol', 'date', 'close')
    inputs.prices = frame.drop('close').join(adjusted, on=['symbol', 'date'], how='left')
    if 'is_st' in inputs.metadata.columns:
        inputs.prices = inputs.prices.join(inputs.metadata.select('symbol', pl.col('is_st').alias('_is_st_at_cutoff')),
                                          on='symbol', how='left').with_columns(
            pl.when(pl.col('date') == date.fromisoformat(inputs.trade_date))
            .then(pl.col('_is_st_at_cutoff')).otherwise(None).alias('is_st')).drop('_is_st_at_cutoff')
    # Historical limit tests in tracking need a dated authority; today's ST rule only applies today.
    history = (getattr(inputs, 'study_price_receipt', None) or {}).get('historical_limits', {})
    bounds = _historical_limits(history)
    if bounds is not None:
        inputs.prices = inputs.prices.join(bounds, on=['symbol', 'date'], how='left')
    today = pl.col('date') == date.fromisoformat(inputs.trade_date)
    signals = []
    for side in ('up', 'down'):
        signal = 'signal_limit_' + side
        if signal in inputs.prices.columns:
            known = today
            if bounds is not None:
                limit = pl.col('_dated_limit_' + side)
                known = known | ((limit > 0) & (limit < NO_PRICE_LIMIT_SENTINEL)) | (pl.col('_dated_limit_up') >= NO_PRICE_LIMIT_SENTINEL)
            signals.append(pl.when(known).then(signal).alias(signal))
    inputs.prices = inputs.prices.with_columns(signals).drop('_dated_limit_up', '_dated_limit_down', strict=False)
    calendar = inputs.provenance.get('calendar', {})
    inputs.provenance['evaluation_context'] = {
        'adjust_method': 'qfq', 'adjust_base_date': inputs.trade_date,
        'adjust_rule_version': 'raw-times-event-cumfactor-over-base-v1',
        'adjustment_source': source,
        'calendar_version': {key: calendar.get(key) for key in ('start', 'end', 'updated_at')},
        'suspension_source': sorted({row.get('source', 'unspecified') for row in inputs.provenance.get('suspensions', [])}),
    }
