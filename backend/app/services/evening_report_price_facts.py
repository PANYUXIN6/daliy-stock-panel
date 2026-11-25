"""Acceptance clocks and sparse before-images of actual stock-day corrections."""
from __future__ import annotations

import polars as pl

from app.parquet import scan_daily_parquet
from app.polars_guard import guarded_collect
from app.services.daily_coverage import DailyCoverageError
from app.tickflow import daily_revisions
from app.tickflow.daily_revisions import raw_daily_flags

PRICE_FIELDS = ('raw_open', 'raw_high', 'raw_low', 'raw_close', 'volume', 'amount', 'wave_formal', 'halted', 'formal_raw')
UNKNOWN_PRIOR = {'unknown': True}


def _raw_rows(root, refs, symbols=None):
    if not refs:
        return {}
    frame = scan_daily_parquet([root / r[0] for r in refs], hive_partitioning=True)
    if symbols is not None:
        frame = frame.filter(pl.col('symbol').is_in(list(symbols)))
    return dict(guarded_collect(daily_revisions.raw_values(frame)).iter_rows())


def _facts(rows):
    if not rows:
        return {}
    schema = {'symbol': pl.String, **dict.fromkeys(daily_revisions.VALUES, pl.Float64), 'formal_source': pl.Boolean}
    frame = pl.from_dicts([{'symbol': s, **bar} for s, bar in rows.items() if bar is not None], schema=schema)
    halt, wave_formal, formal_raw = raw_daily_flags(pl.col('formal_source'))
    fields = [pl.col(key).alias('raw_' + key) for key in ('open', 'high', 'low', 'close')]
    fields += [pl.when(pl.col(key) >= 0).then(key).alias(key) for key in ('volume', 'amount')]
    return dict(frame.select('symbol', pl.struct(*fields, wave_formal.alias('wave_formal'),
                     (halt & formal_raw).alias('halted'), formal_raw.alias('formal_raw')).alias('bar')).iter_rows())


def _before(value, patch):
    return None if patch is None else {**(value or {}), **patch}


def _stock_clock(overrides, symbol, day_clock, first):
    return overrides.get(symbol) or max(day_clock, first)


def accepted_study_quotes(data_dir, partitions, day, coverage, changes, unknown_days):
    """Save today's quotes and accepted amendments in the still unpublished interval."""
    covered = coverage.active | coverage.halted
    requested = {day: covered}
    for symbol, rows in changes.items():
        for changed_day in rows:
            requested.setdefault(changed_day, set()).add(symbol)
    for changed_day, uncertain in unknown_days.items():
        requested.setdefault(changed_day, set()).update(covered if uncertain is None else covered & uncertain)

    result = {}
    for trading_day, symbols in requested.items():
        quotes = dict.fromkeys(symbols)
        if refs := partitions.get(trading_day):
            frame = scan_daily_parquet([data_dir / ref[0] for ref in refs], hive_partitioning=True)
            halt, valid, formal = raw_daily_flags()
            rows = guarded_collect(frame.filter(pl.col('symbol').is_in(list(symbols)) & (valid | (halt & formal)))
                                   .select('symbol', 'close', 'low', halt.alias('halted')))
            for symbol, close, low, halted in rows.iter_rows():
                quotes[symbol] = 'suspended' if halted else [close, low]
        if trading_day == day:
            for symbol in coverage.halted:
                if not quotes[symbol]:
                    quotes[symbol] = 'suspended'
        result[trading_day] = quotes
    return result


def study_quote_clocks(previous, quotes, changes, unknown_days, cutoff):
    """Keep field clocks only where the accepted close or low predates the last bar change."""
    clocks = {day: rows.copy() for day, rows in previous.get('study_quote_clocks', {}).items()}
    for day, uncertain in unknown_days.items():
        if uncertain is None:
            clocks.pop(day, None)
        elif day in clocks:
            for symbol in uncertain:
                clocks[day].pop(symbol, None)
    for symbol, rows in changes.items():
        for day, prior in rows.items():
            by_symbol = clocks.setdefault(day, {})
            prior_times = by_symbol.pop(symbol, None)
            quote = quotes.get(day, {}).get(symbol)
            if not isinstance(quote, list) or not prior or not prior['wave_formal']:
                continue
            times = prior_times or [prior['known_at']] * 2
            updated = [cutoff if prior.get(field, value) != value else at
                       for field, value, at in zip(('raw_close', 'raw_low'), quote, times, strict=True)]
            if updated != [cutoff, cutoff]:
                by_symbol[symbol] = updated
    return {day: rows for day, rows in clocks.items() if rows}


def _price_patch(old, new, known_at):
    if old is None:
        return None
    if new is None or (not old['halted'] and
                       (new['raw_open'] == new['raw_high'] == 0 or new['volume'] == new['amount'] == 0)):
        return {**old, 'known_at': known_at}
    changed = {key: value for key, value in old.items() if value != new.get(key)}
    return {**changed, 'halted': old['halted'], 'wave_formal': old['wave_formal'], 'known_at': known_at}


def accept_price_facts(data_dir, root, partitions, previous, previous_files, covered, first_acceptances, cutoff,
                       *, missing_predecessor_day=None, waiting_bars=None, required_symbols=None, added_symbols=None):
    """Use the source warehouse for present values and only record a changed field's old value."""
    clocks = previous.get('wave_price_clocks', {})
    dates = dict(clocks.get('dates', {}))
    overrides = {day: rows for day, rows in clocks.get('rows', {}).items() if rows}
    pending = {day: rows for day, rows in clocks.get('pending', {}).items() if rows}
    previously_covered = set(previous.get('active_symbols', ())) | set(previous.get('halted_symbols', ()))
    closed = daily_revisions.published_through(root)
    required = covered if required_symbols is None else required_symbols
    added = covered - previously_covered if added_symbols is None else added_symbols
    files = {True: [], False: []}
    for day, refs in partitions.items():
        changed = day not in dates or (day > closed and previous_files.get(day) != refs)
        if changed or added:
            files[changed].extend(data_dir / ref[0] for ref in refs)
    for changed, paths in files.items():
        if not paths:
            continue
        symbols = required if changed else added
        bars = scan_daily_parquet(paths, hive_partitioning=True).filter(pl.col('symbol').is_in(list(symbols)))
        duplicate = pl.struct('symbol', 'date').is_duplicated().any()
        if guarded_collect(bars.select(duplicate)).item() and guarded_collect(bars.unique().select(duplicate)).item():
            raise DailyCoverageError('正式历史日线存在冲突的证券日期')
    changes, unknown_days = {}, {}
    for day in sorted(partitions.keys() | previous_files.keys() | pending.keys()):
        refs, prior_refs = partitions.get(day, []), previous_files.get(day, [])
        if day <= closed:
            dates.setdefault(day, cutoff)
            pending.pop(day, None)
            # A published stock resumes only when its missing formal bar first arrives.
            requests = (waiting_bars or {}).get(day, {})
            exceptions = overrides.get(day, {})
            needed = {s for s, since in requests.items() if s in covered and since < cutoff
                      and exceptions.get(s, '') <= since}
            if refs == prior_refs:
                needed.difference_update(previously_covered)
            if needed and refs:
                frame = scan_daily_parquet([data_dir / ref[0] for ref in refs], hive_partitioning=True)
                halt, wave_formal, formal = raw_daily_flags()
                ready = guarded_collect(frame.filter(pl.col('symbol').is_in(list(needed)) & (wave_formal | (halt & formal)))
                                        .select('symbol'))
                if ready.height:
                    exceptions = dict(exceptions)
                    for symbol in ready['symbol']:
                        changes.setdefault(symbol, {})[day] = None
                        exceptions[symbol] = cutoff
                    overrides[day] = exceptions
            continue
        waiting = pending.get(day, {})
        if day in dates and refs == prior_refs and covered.isdisjoint(waiting):
            continue
        earlier_clock = dates.get(day, cutoff)
        exceptions = dict(overrides.get(day, {}))
        if day in dates and refs and not prior_refs:
            # Even after a deletion whose old contents were lost, the intervening receipt proves the empty day.
            source_before = {}
            source_after = _facts(_raw_rows(data_dir, refs))
            touched = source_after.keys() | waiting.keys()
        elif day in dates:
            versions = (daily_revisions.before_changes(root, day, prior_refs, refs)
                        if refs != prior_refs else [])
            if versions is None:
                # A missing warehouse link invalidates partial before-images. A previously proved absence
                # or a complete accepted row still stands even if there have been more raw rewrites since.
                complete = {s: v for s, v in waiting.items() if v is None or all(key in v for key in PRICE_FIELDS)}
                uncertain = covered - complete.keys()
                if uncertain:
                    unknown_days[day] = uncertain if complete else None
                waiting = {**dict.fromkeys(first_acceptances.keys() - covered, UNKNOWN_PRIOR), **complete}
                exceptions = {}
                earlier_clock = cutoff
                versions = []
            touched = set(waiting).intersection(covered)
            for version in versions:
                touched.update(version)
            raw = _raw_rows(data_dir, refs, touched) if touched else {}
            source_after = _facts(raw)
            for version in versions:
                for symbol, patch in version.items():
                    raw[symbol] = _before(raw.get(symbol), patch)
            source_before = _facts(raw)
        else:
            # Ordinary first acceptance (or a new market day) needs just one clock, no bar copy.
            dates[day] = cutoff
            if prior_refs or not previous or not refs:
                continue
            late = day <= previous['date']
            not_covered = first_acceptances.keys() - covered
            if not late and not not_covered:
                continue
            # An accepted missing bar (or a warehouse row still outside the accepted scope) is an absence,
            # so only its symbol is needed. Regular new days do not generate a market-wide list of changes.
            eligible = first_acceptances.keys() if late else not_covered
            frame = scan_daily_parquet([data_dir / r[0] for r in refs], hive_partitioning=True)
            symbols = guarded_collect(frame.filter(pl.col('symbol').is_in(list(eligible))).select('symbol'))['symbol']
            for symbol in symbols:
                if first_acceptances[symbol] < cutoff:
                    if symbol not in covered:
                        pending.setdefault(day, {})[symbol] = None
                    elif late:
                        changes.setdefault(symbol, {})[day] = None
            continue
        waiting = dict(waiting)
        for symbol in touched:
            old, new = source_before.get(symbol), source_after.get(symbol)
            known = _stock_clock(exceptions, symbol, earlier_clock, first_acceptances.get(symbol, cutoff))
            if symbol in waiting:
                if waiting[symbol] == UNKNOWN_PRIOR:
                    if symbol in covered:
                        # A rewrite outside this security's accepted scope first becomes observable now.
                        waiting.pop(symbol)
                        exceptions[symbol] = cutoff
                        unknown_days.setdefault(day, set()).add(symbol)
                    continue
                patch = waiting.pop(symbol)
                old = _before(old, patch)
                if old is not None:
                    known = old.pop('known_at', known)
                    if old == new and known != max(earlier_clock, first_acceptances.get(symbol, cutoff)):
                        exceptions[symbol] = known
            if old == new or symbol not in first_acceptances:
                continue
            patch = _price_patch(old, new, known)
            if symbol not in covered:
                waiting[symbol] = patch
                continue
            if first_acceptances[symbol] < cutoff:
                changes.setdefault(symbol, {})[day] = patch
            if cutoff != max(earlier_clock, first_acceptances[symbol]):
                exceptions[symbol] = cutoff
            else:
                exceptions.pop(symbol, None)
        dates[day] = earlier_clock
        if exceptions:
            overrides[day] = exceptions
        else:
            overrides.pop(day, None)
        if waiting:
            pending[day] = waiting
        else:
            pending.pop(day, None)
    if missing_predecessor_day:
        # Older accepted values are not before-images of the missing receipt, including deferred comparisons.
        changes.clear()
        for day, waiting in pending.items():
            if day <= missing_predecessor_day:
                pending[day] = dict.fromkeys(waiting, UNKNOWN_PRIOR)
    state = {'wave_price_clocks': {'dates': dates, 'rows': overrides, 'pending': pending}}
    return state, changes, unknown_days


def add_accepted_price_times(frame, evidence):
    cutoff = evidence['cutoff']
    clocks = evidence['wave_price_clocks']
    observed = pl.col('date').cast(pl.String).replace_strict(clocks.get('dates', {}), default=cutoff, return_dtype=pl.String)
    covered = pl.col('symbol').replace_strict(evidence.get('wave_first_acceptances', {}), default=cutoff, return_dtype=pl.String)
    frame = frame.with_columns(pl.max_horizontal(observed, covered).alias('known_at'))
    rows = [(day, symbol, at) for day, values in clocks.get('rows', {}).items() for symbol, at in values.items()]
    if rows:
        exceptions = pl.DataFrame(rows, schema={'_day': pl.String, 'symbol': pl.String, '_at': pl.String}, orient='row')
        frame = frame.with_columns(pl.col('date').cast(pl.String).alias('_day')).join(
            exceptions, on=['_day', 'symbol'], how='left').with_columns(
            pl.coalesce('_at', 'known_at').alias('known_at')).drop('_at', '_day')
    return frame
