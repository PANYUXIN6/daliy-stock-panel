"""个人手动选股快照：与实时监控缓存隔离，更新期间保留上次结果。"""
from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

from app.services.fs_utils import atomic_write_text
from app.services.path_locks import path_lock
from app.services.private_storage import private_path


def path(data_dir: Path) -> Path:
    return private_path(data_dir, 'user_data', 'screener_snapshots.json')


@lru_cache(maxsize=4)
def _read_cached(filename: str, signature: tuple) -> dict:
    return json.loads(Path(filename).read_text(encoding='utf-8'))


def read(data_dir: Path, *, summary_only: bool = False) -> dict:
    filename = path(data_dir)
    with path_lock(filename):
        try:
            stat = filename.stat()
            records = _read_cached(str(filename), (stat.st_ino, stat.st_mtime_ns, stat.st_size))
            if summary_only:
                records = {namespace: {sid: {k: v for k, v in result.items() if k not in ('rows', 'input_version')}
                                       for sid, result in entries.items()} for namespace, entries in records.items()}
            return deepcopy(records)
        except FileNotFoundError:
            # 旧盘后结果只用于展示；没有版本证据，不冒充已验证的新快照。
            from app.services.strategy_cache import read_cache, read_summary
            old = (read_summary(data_dir) if summary_only else read_cache(data_dir)) or {}
            return {'stock:1d': old.get('results', {})}


def write(data_dir: Path, records: dict) -> None:
    filename = path(data_dir)
    filename.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(filename, json.dumps(records, ensure_ascii=False, allow_nan=False, default=str))
    _read_cached.cache_clear()


def invalidate(data_dir: Path, strategy_id: str | None = None) -> None:
    """失效不删除历史结果；在下次手动点击时重新计算。"""
    with path_lock(path(data_dir)):
        records = read(data_dir)
        for results in records.values():
            for sid, result in results.items():
                if strategy_id is None or sid == strategy_id:
                    result['invalidated'] = True
        if any(records.values()):
            write(data_dir, records)


def forget_user_cache(data_dir: Path, uid: str) -> None:
    _read_cached.cache_clear()


def save_result(data_dir: Path, asset: str, timeframe: str, day: str, sid: str, result: dict) -> None:
    """显式候选核验与普通手动任务共享结果归属，保留其他策略和日期。"""
    import time
    with path_lock(path(data_dir)):
        records = read(data_dir)
        entries = records.setdefault(f'{asset}:{timeframe}:{day}', {})
        entries[sid] = {**entries.get(sid, {}), **result, 'computed_at': int(time.time() * 1000)}
        write(data_dir, records)
