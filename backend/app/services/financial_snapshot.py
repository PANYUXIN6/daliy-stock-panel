"""Reuse immutable financial table reads until the atomic file version changes."""
from pathlib import Path

import polars as pl

from app.services.snapshot_cache import SnapshotCache

_cache = SnapshotCache(ttl=60, maxsize=10)


def read_table(path: Path) -> pl.DataFrame:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return pl.DataFrame()
    key = (str(path.resolve()), stat.st_ino, stat.st_mtime_ns, stat.st_size)
    # Corruption raises; it must never be mistaken for an empty table on writes.
    return _cache.get_or_compute(key, lambda: pl.read_parquet(path)).clone()
