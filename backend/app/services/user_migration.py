"""One-time, restart-safe attribution of legacy personal files to the initial admin."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import suppress
from pathlib import Path

from app.services.preferences import PERSONAL_KEYS
from app.services.user_context import user_data_dir

LEGACY_PRIVATE_PATHS = (
    'user_data/watchlist.parquet', 'user_data/watchlist.parquet.bak',
    'user_data/watchlist_groups.json', 'user_data/monitor_rules', 'user_data/lots',
    'user_data/strategy_overrides', 'user_data/strategy_cache.json',
    'user_data/strategy_run_timings.json', 'user_data/custom_signals',
    'user_data/custom_factors', 'user_data/alerts.jsonl',
    'user_data/ai_reports.json', 'user_data/ai_stock_reports.json',
    'user_data/ai_market_recaps.json', 'user_data/research_candidates.json',
    'paper', 'research', 'backtest_results', 'strategies/composite',
    'strategies/custom', 'strategies/ai',
)


def _reject_linked_path(root: Path, relative: str) -> None:
    candidate = root
    for part in Path(relative).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError('旧数据迁移不允许符号链接')


def _copy_missing(source: Path, target: Path) -> None:
    if source.is_symlink() or target.is_symlink():
        raise ValueError('旧数据迁移不允许符号链接')
    if not source.exists():
        return
    if source.is_dir():
        target.mkdir(parents=True, exist_ok=True)
        for child in source.iterdir():
            _copy_missing(child, target / child.name)
        return
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.migration-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as output, source.open('rb') as incoming:
            shutil.copyfileobj(incoming, output)
            output.flush()
            os.fsync(output.fileno())
        # No replacement: a target created by an earlier attempt remains authoritative.
        with suppress(FileExistsError):
            os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def migrate_legacy_private_data(root: Path, initial_admin_id: str | None) -> None:
    if initial_admin_id is None:
        return
    root = Path(root)
    private = user_data_dir(root, initial_admin_id)
    state = root / 'runtime'
    state.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(state / 'private_migration.sqlite3', timeout=60) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS migrations (name TEXT PRIMARY KEY, owner TEXT NOT NULL)')
        conn.execute('BEGIN IMMEDIATE')
        done = conn.execute("SELECT owner FROM migrations WHERE name='private-v1'").fetchone()
        if done:
            if done[0] != initial_admin_id:
                raise ValueError('旧数据归属与初始管理员不一致')
            return
        for relative in LEGACY_PRIVATE_PATHS:
            _reject_linked_path(root, relative)
            _reject_linked_path(private, relative)
            _copy_missing(root / relative, private / relative)
        _reject_linked_path(root, 'user_data/preferences.json')
        _reject_linked_path(private, 'user_data/preferences.json')
        old_preferences = root / 'user_data' / 'preferences.json'
        target = private / 'user_data' / 'preferences.json'
        if old_preferences.exists() and not target.exists():
            if old_preferences.is_symlink():
                raise ValueError('旧偏好不允许符号链接')
            values = json.loads(old_preferences.read_text(encoding='utf-8'))
            personal = {k: v for k, v in values.items() if k in PERSONAL_KEYS}
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix='.preferences-migration-', dir=target.parent)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as out:
                    json.dump(personal, out, ensure_ascii=False)
                    out.flush()
                    os.fsync(out.fileno())
                with suppress(FileExistsError):
                    os.link(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
        conn.execute("INSERT INTO migrations VALUES ('private-v1', ?)", (initial_admin_id,))
