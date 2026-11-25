"""Version-linked changes to accepted raw stock-day partitions and adjustment factors."""
from __future__ import annotations

import json
import logging
from datetime import date
from functools import lru_cache
from pathlib import Path

import polars as pl

from app import market_time
from app.services.fs_utils import atomic_write_text

VALUES = ('open', 'high', 'low', 'close', 'volume', 'amount')
logger = logging.getLogger(__name__)


def raw_daily_flags(formal_source=None):
    if formal_source is None:
        formal_source = pl.col('quote_ts').is_null()
    halt = (((pl.col('open') == 0) & (pl.col('high') == 0))
            | ((pl.col('volume') == 0) & (pl.col('amount') == 0))).fill_null(False)
    price_valid = (pl.all_horizontal(pl.col(c).is_finite() & (pl.col(c) > 0) for c in ('open', 'high', 'low', 'close'))
                   & (pl.col('high') >= pl.max_horizontal('open', 'close', 'low'))
                   & (pl.col('low') <= pl.min_horizontal('open', 'close')))
    valid = price_valid & pl.all_horizontal(pl.col(c).is_finite() & (pl.col(c) >= 0) for c in ('volume', 'amount'))
    return (halt, (formal_source & ~halt & price_valid).fill_null(False),
            (formal_source & (halt | valid)).fill_null(False))


def raw_values(frame):
    names = frame.collect_schema().names()
    fields = [pl.when(pl.col(c).is_finite()).then(pl.col(c)).cast(pl.Float64).alias(c) if c in names
              else pl.lit(None, dtype=pl.Float64).alias(c) for c in VALUES]
    formal = pl.col('quote_ts').is_null() if 'quote_ts' in names else pl.lit(True)
    return frame.select('symbol', pl.struct(*fields, formal.alias('formal_source')).alias('bar'))


def partition_changes(out: Path, old: pl.DataFrame, new: pl.DataFrame) -> tuple[Path, dict] | None:
    if 'symbol' not in old.columns:
        return None
    tracking = tracked_partition(out)
    if tracking is None:
        return None
    directory, same_day = tracking
    if old.equals(new):
        return directory, {}
    # Acceptance rejects duplicates. Use the same projection at both ends of an intermediate,
    # unaccepted write, so its subsequent repair can still restore the last accepted single row.
    old = raw_values(old).unique('symbol', keep='last').rename({'bar': 'old'})
    current = raw_values(new).unique('symbol', keep='last')
    compared = current.join(old, on='symbol', how='full', coalesce=True)
    changed = compared.filter(~pl.col('old').eq_missing(pl.col('bar')))
    if not same_day and not changed.is_empty():
        # Across sessions only a previously missing or incomplete row can add evidence.
        # Keep both sides of an intervening incomplete write until the next acceptance.
        _, _, formal = raw_daily_flags(pl.col('formal_source'))
        prior_complete = changed.select(pl.col('old').struct.unnest()).select(formal).to_series()
        current_complete = changed.select(pl.col('bar').struct.unnest()).select(formal).to_series()
        changed = changed.filter(~(prior_complete & current_complete))
    differences = {}
    for symbol, current, prior in changed.iter_rows():
        if prior is not None and current is not None:
            prior = {key: value for key, value in prior.items() if value != current[key]}
        differences[symbol] = prior
    return directory, differences


def track_receipt(root: Path, day: str) -> None:
    directory = root / 'price_writes'
    cursor = directory / 'current.json'
    if (current := _tracked_date(cursor)) and day <= current:
        return
    directory.mkdir(parents=True, exist_ok=True)
    atomic_write_text(cursor, json.dumps({'date': day}))


def _tracked_date(cursor: Path) -> str | None:
    try:
        return date.fromisoformat(json.loads(cursor.read_text())['date']).isoformat()
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning('无法读取晚报行情订正的跟踪日期 %s', cursor, exc_info=True)
        return None


@lru_cache(maxsize=16)
def _last_publication(directory, identity):
    # Switching a report pointer atomically changes the parent directory.
    return max((path.stem for path in directory.glob('????-??-??.json')), default='')


def published_through(root):
    directory = root / 'reports'
    try:
        stat = directory.stat()
    except FileNotFoundError:
        return ''
    return _last_publication(directory, (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns))


def tracked_partition(out: Path) -> tuple[Path, bool] | None:
    if out.parent.parent.name != 'kline_daily':
        return None
    directory = out.parents[2] / 'evening_report' / 'price_writes'
    tracked = _tracked_date(directory / 'current.json')
    day = out.parent.name.removeprefix('date=')
    if tracked and published_through(directory.parent) < day <= tracked:
        return directory, tracked == market_time.cn_now().date().isoformat()
    return None


def _ref(path: Path, physical: Path, root: Path) -> list:
    stat = physical.stat()
    return [str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns]


def record_rewrite(directory: Path, out: Path, staged: Path, difference: dict,
                   *, replaces_all: bool) -> None:
    """Seal changes against the staged file identity before its atomic rename makes them visible."""
    sources = sorted(out.parent.glob('*.parquet'))
    before = [_ref(path, path, out.parents[2]) for path in sources]
    result = _ref(out, staged, out.parents[2])
    after = [result] if replaces_all else sorted([result, *(r for r in before if r[0] != result[0])])
    directory.mkdir(parents=True, exist_ok=True)
    day = out.parent.name.removeprefix('date=')
    path = directory / f'{day}_{result[2]}-{result[1]}.json'
    atomic_write_text(path, json.dumps({'before': before, 'after': after, 'rows': difference},
                                      allow_nan=False, separators=(',', ':')))


def _valid_prior_patch(patch) -> bool:
    if patch is None:
        return True  # The row was absent before the write.
    if not isinstance(patch, dict):
        return False
    return all((key == 'formal_source' and isinstance(value, bool))
               or (key in VALUES and (value is None or type(value) in (int, float)))
               for key, value in patch.items())


def valid_file_references(refs) -> bool:
    return isinstance(refs, list) and all(isinstance(ref, list) and len(ref) == 3
            and isinstance(ref[0], str) and all(isinstance(n, int) for n in ref[1:]) for ref in refs)


def factor_change_rows(existing: pl.DataFrame, merged: pl.DataFrame, compared: pl.DataFrame) -> dict | None:
    """Store exact changes relevant to accepted price bases, including changes below the indicator tolerance."""
    keys = ['symbol', 'trade_date']
    invalid = (pl.any_horizontal(pl.col(keys).is_null()) | pl.col('ex_factor').is_null()
               | ~pl.col('ex_factor').is_finite() | (pl.col('ex_factor') <= 0))
    # Merging also removes duplicate keys. Either a malformed input or its repair can
    # change factor availability for the entire market, beyond the edited stock.
    if existing.select(invalid.any()).item() or merged.select(invalid.any()).item():
        return None
    final = compared.unique(keys, keep='last')
    if existing.height + final['ex_factor_old'].null_count() != merged.height:
        return None  # Existing duplicate keys collapsed in the final file.
    revised = final.filter(pl.col('ex_factor') != pl.col('ex_factor_old').fill_null(1.))
    return {d.isoformat(): symbols for d, symbols in
            revised.group_by('trade_date').agg(pl.col('symbol').unique()).iter_rows()}


def record_factor_rewrite(out: Path, staged: Path, rows: dict) -> None:
    """Bind the affected ex-dates to this exact stock factor file before it becomes visible."""
    root = out.parents[1]
    before = [_ref(out, out, root)]
    after = _ref(out, staged, root)
    path = root / 'evening_report' / 'price_writes' / f'factor_{after[2]}-{after[1]}.json'
    atomic_write_text(path, json.dumps({'before': before, 'after': [after], 'rows': rows},
                                       allow_nan=False, separators=(',', ':')))


def _before_changes(root, prefix, previous, current, filename, valid_rows):
    """Visit only committed source versions, newest first. A bypassed rewrite has no proof."""
    directory = root / 'price_writes'
    versions = []
    visited = set()
    while current != previous:
        part = next((r for r in current if r[0].endswith(filename)), None)
        if part is None:
            return None
        path = directory / f'{prefix}_{part[2]}-{part[1]}.json'
        if path in visited:
            return None
        visited.add(path)
        try:
            change = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(change, dict) or change.get('after') != current:
            return None
        before, rows = change.get('before'), change.get('rows')
        if not valid_file_references(before) or not isinstance(rows, dict) or not valid_rows(rows):
            return None
        versions.append(rows)
        current = before
    return versions


def before_changes(root: Path, day: str, previous: list, current: list) -> list[dict] | None:
    return _before_changes(root, day, previous, current, '/part.parquet',
                           lambda rows: all(_valid_prior_patch(patch) for patch in rows.values()))


def _valid_factor_rows(rows):
    try:
        return all(date.fromisoformat(day).isoformat() == day and isinstance(symbols, list) and symbols
                   and all(isinstance(s, str) and s for s in symbols) for day, symbols in rows.items())
    except ValueError:
        return False


def factor_changes(root: Path, previous: list | None, current: list | None) -> list[dict] | None:
    return _before_changes(root, 'factor', [previous] if previous else [], [current] if current else [],
                           'adj_factor/all.parquet', _valid_factor_rows)
