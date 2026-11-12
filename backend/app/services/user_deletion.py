"""Account deletion and cross-process exclusion of personal-data operations.

The web process, collector and research workers share a per-user advisory lock.
Deletion first closes admission in the account database, then waits for existing
operations to release their shared locks before removing any personal data.
"""

from __future__ import annotations

import fcntl
import os
import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from app.services.auth import AuthError, AuthStore, get_store
from app.services.path_locks import path_lock

_DRAIN_SECONDS = 5.0


def _lock_path(root: Path, uid: str) -> Path:
    from app.services.user_context import user_data_dir

    user_data_dir(root, uid)  # Same bounded ID and symlink rules as personal storage.
    parent = root.resolve() / "auth" / "user-locks"
    if parent.resolve() != parent:
        raise ValueError("用户操作锁目录不能通过符号链接重定向")
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return parent / f"{uid}.lock"


@contextmanager
def _user_lock(root: Path, uid: str, *, exclusive: bool):
    try:
        path = _lock_path(root, uid)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except (OSError, ValueError) as exc:
        raise AuthError("用户数据目录不可用，请检查目录权限和路径", 503) from exc
    try:
        deadline = time.monotonic() + (_DRAIN_SECONDS if exclusive else 0)
        while True:
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise AuthError(
                        "账号已停用，仍有操作正在结束，请稍后重试彻底删除" if exclusive else "账号正在删除",
                        409, "USER_DELETION_PENDING",
                    ) from None
                time.sleep(0.05)
        yield path
    finally:
        os.close(fd)


def _require_available(accounts: AuthStore, uid: str) -> None:
    user = accounts.get_user(uid)
    if user is None or user["deletion_pending"]:
        raise AuthError("账号正在删除或已删除", 401, "SESSION_INVALID")


@contextmanager
def user_operation(root: Path, uid: str):
    # Domain-only tools/tests can use personal stores without an account server.
    # Every HTTP/worker deployment initializes the account authority before use.
    if not (root / "auth" / "auth.sqlite3").exists():
        yield
        return
    accounts = get_store(root)
    _require_available(accounts, uid)
    with _user_lock(root, uid, exclusive=False):
        # Admission may have closed between the first check and acquiring the lock.
        _require_available(accounts, uid)
        yield


def forget_user_caches(root: Path, uid: str, runtimes=None) -> None:
    from app.factors import registry
    from app.services import preferences, screener_snapshots, strategy_cache, strategy_run_queue
    from app.strategy import config, custom_signals, monitor

    for module in (preferences, screener_snapshots, strategy_cache, config, custom_signals, registry, monitor):
        module.forget_user_cache(root, uid)
    strategy_run_queue.MANAGER.forget_user(root, uid)
    if runtimes is not None:
        runtimes.forget_user(uid)


def delete_user(accounts: AuthStore, uid: str, *, actor_id: str, runtimes=None) -> None:
    # Deletions are routed to the single collector. Serialize retries there,
    # including cancellation, which can itself persist a final job state.
    with path_lock(accounts.path.parent / f".delete-{uid}"):
        _delete_user(accounts, uid, actor_id=actor_id, runtimes=runtimes)


def _delete_user(accounts: AuthStore, uid: str, *, actor_id: str, runtimes=None) -> None:
    from app.services.ai_credits import get_credit_store
    from app.services.user_context import user_data_dir

    try:
        accounts.prepare_user_deletion(uid, actor_id=actor_id)
        if runtimes is not None:
            runtimes.stop_user(uid)
        with _user_lock(accounts.data_dir, uid, exclusive=True):
            # Initialization already in flight may have published a runtime while
            # admission was closing. Finish its queued jobs before removing files.
            if runtimes is not None:
                runtimes.stop_user(uid)
            directory = user_data_dir(accounts.data_dir, uid)
            try:
                directory.lstat()
            except FileNotFoundError:
                pass
            else:
                shutil.rmtree(directory)
            get_credit_store(accounts.data_dir).delete_user_data(uid)
            forget_user_caches(accounts.data_dir, uid, runtimes)
            accounts.finish_user_deletion(uid, actor_id=actor_id)
            from app.services.shared_runtime import runtime_bus
            runtime_bus.publish("user_deleted", {"user_id": uid}, "*")
    except AuthError:
        raise
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise AuthError(
            "用户数据未能全部清理，请检查目录权限和存储状态后重试彻底删除", 503,
            "USER_DELETION_FAILED",
        ) from exc
