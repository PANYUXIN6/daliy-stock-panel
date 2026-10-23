from __future__ import annotations

import json
import os
import threading
import time
import uuid
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import polars as pl


class EnrichedGenerationUnavailableError(RuntimeError):
    """The enriched dataset has no stable generation available for readers."""


_WRITER_LOCKS_GUARD = threading.Lock()
_WRITER_LOCKS: dict[tuple[str, str], threading.RLock] = {}
_ACTIVE_PUBLICATIONS: weakref.WeakValueDictionary[str, EnrichedPublication] = (
    weakref.WeakValueDictionary()
)


def _marker_path(data_dir: Path, asset_type: str) -> Path:
    return Path(data_dir) / f".matrix_generation_{asset_type}.json"


def _publication_lease(data_dir: Path, asset_type: str) -> BinaryIO:
    """Hold a separate flock for the whole publication, including computation gaps."""
    path = Path(data_dir) / f".matrix_publication_{asset_type}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        _try_lock_file(stream)
    except BaseException:
        stream.close()
        raise
    return stream


def _writer_lock(data_dir: Path, asset_type: str) -> threading.RLock:
    key = (str(Path(data_dir).resolve()), asset_type)
    with _WRITER_LOCKS_GUARD:
        return _WRITER_LOCKS.setdefault(key, threading.RLock())


def _read_marker(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EnrichedGenerationUnavailableError(
            "enriched data generation marker is invalid"
        ) from exc
    if not isinstance(payload, dict):
        raise EnrichedGenerationUnavailableError(
            "enriched data generation marker is invalid"
        )
    return payload


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_marker(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _unlock_file(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _try_lock_file(stream: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise EnrichedGenerationUnavailableError(
                "another enriched publication is active"
            ) from exc
        return
    import fcntl

    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise EnrichedGenerationUnavailableError(
            "another enriched publication is active"
        ) from exc


@contextmanager
def _exclusive_generation_lock(data_dir: Path, asset_type: str) -> Iterator[None]:
    lock_path = Path(data_dir) / f".matrix_generation_{asset_type}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with (
        _writer_lock(data_dir, asset_type),
        lock_path.open("a+b") as stream,
    ):
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        _try_lock_file(stream)
        try:
            yield
        finally:
            _unlock_file(stream)


def _process_is_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError) as exc:
        # Windows 对不存在的 pid 返回 WinError 87 (ERROR_INVALID_PARAMETER),
        # 不会映射为 ProcessLookupError; 按存活处理会让孤儿发布锁永远无法恢复。
        return getattr(exc, "winerror", None) != 87
    return True


def _ready_payload(generation: str) -> dict[str, Any]:
    return {
        "state": "ready",
        "generation": generation,
        "updated_at_ns": time.time_ns(),
    }


def _is_ready_payload(payload: dict[str, Any]) -> bool:
    generation = payload.get("generation")
    return (
        payload.get("state", "ready") == "ready"
        and isinstance(generation, str)
        and bool(generation)
    )


def _publication_claim_is_running(
    payload: dict[str, Any], data_dir: Path, asset_type: str,
) -> bool:
    """标记指向的发布是否仍在推进: 进程内活跃对象存在, 或属主进程仍存活。

    owner_pid 等于当前进程但无活跃对象视为可接管 (同进程上一次尝试的遗留),
    与写入方 recover 接管的判定一致。
    """
    if _ACTIVE_PUBLICATIONS.get(str(payload.get("publication_id"))) is not None:
        return True
    if payload.get("lease"):
        try:
            stream = _publication_lease(data_dir, asset_type)
        except EnrichedGenerationUnavailableError:
            return True
        stream.close()
        return False
    owner_pid = payload.get("owner_pid")
    return owner_pid != os.getpid() and _process_is_alive(owner_pid)


def _orphaned_publishing_claim(
    payload: dict[str, Any], data_dir: Path, asset_type: str,
) -> bool:
    """仅新格式明确声明未写入、且租约已释放的发布可由读取方恢复。

    旧标记未记录写入进度，即使属主已退出也可能有半批分区，必须完整重建。
    """
    if payload.get("lease") is not True or payload.get("dirty") is not False:
        return False
    if payload.get("full_rebuild_required"):
        return False
    return not _publication_claim_is_running(payload, data_dir, asset_type)


def get_enriched_generation(
    data_dir: Path,
    asset_type: str = "stock",
    *,
    initialize: bool = True,
) -> str:
    path = _marker_path(data_dir, asset_type)
    payload = _read_marker(path)
    if payload is None:
        if not initialize:
            raise EnrichedGenerationUnavailableError(
                "enriched data generation marker is unavailable"
            )
    elif _is_ready_payload(payload):
        return payload["generation"]
    elif not _orphaned_publishing_claim(payload, data_dir, asset_type):
        # 发布仍在推进, 或为同进程异常遗留 (无法证明属主已死): 读取保持 fail-closed。
        raise EnrichedGenerationUnavailableError(
            "enriched data is being published; retry after the update finishes"
        )
    # 指向已死发布的僵死标记: 在独占锁内二次确认后恢复 ready。
    with _exclusive_generation_lock(data_dir, asset_type):
        payload = _read_marker(path)
        if payload is None:
            generation = uuid.uuid4().hex
            _write_marker(path, _ready_payload(generation))
            return generation
        if _is_ready_payload(payload):
            return payload["generation"]
        if not _orphaned_publishing_claim(payload, data_dir, asset_type):
            raise EnrichedGenerationUnavailableError(
                "enriched data is being published; retry after the update finishes"
            )
        # 只释放确定未写入的遗留发布，并使等待期间的缓存重新核对代号。
        generation = uuid.uuid4().hex
        _write_marker(path, _ready_payload(generation))
        return generation


def recover_stale_publications(
    data_dir: Path, asset_types: tuple[str, ...] = ("stock", "etf"),
) -> list[str]:
    """Recover only untouched abandoned publications; partial writes await rebuild."""
    recovered = []
    for asset_type in asset_types:
        try:
            payload = _read_marker(_marker_path(data_dir, asset_type))
            if payload and not _is_ready_payload(payload) and _orphaned_publishing_claim(
                payload, data_dir, asset_type,
            ):
                get_enriched_generation(data_dir, asset_type)
                recovered.append(asset_type)
        except EnrichedGenerationUnavailableError:
            continue
    return recovered


def enriched_publication_incomplete(
    data_dir: Path,
    asset_type: str = "stock",
) -> bool:
    try:
        payload = _read_marker(_marker_path(data_dir, asset_type))
    except EnrichedGenerationUnavailableError:
        return True
    if payload is None:
        return False
    return (
        payload.get("state", "ready") != "ready"
        or not isinstance(payload.get("generation"), str)
        or not payload["generation"]
    )


def bump_enriched_generation(data_dir: Path, asset_type: str = "stock") -> str:
    path = _marker_path(data_dir, asset_type)
    with _exclusive_generation_lock(data_dir, asset_type):
        current = _read_marker(path)
        if current is not None and current.get("state", "ready") != "ready":
            raise EnrichedGenerationUnavailableError(
                "cannot bump an incomplete enriched publication"
            )
        generation = uuid.uuid4().hex
        _write_marker(path, _ready_payload(generation))
        return generation


class EnrichedPublication:
    """Publish one logical enriched write batch under a stable generation token."""

    def __init__(
        self,
        data_dir: Path,
        asset_type: str = "stock",
        *,
        recover: bool = False,
        full_rebuild: bool = False,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.asset_type = asset_type
        self.recover = recover
        self.full_rebuild = full_rebuild
        self._publishing = False
        self._changed = False
        self._base_generation: str | None = None
        self._publication_id = uuid.uuid4().hex
        self._lease: BinaryIO | None = None
        self._base_incomplete = False

    def begin(self) -> None:
        with _exclusive_generation_lock(self.data_dir, self.asset_type):
            self._claim_or_verify()

    def mark_changed(self) -> None:
        if not self._publishing:
            raise RuntimeError("enriched publication has not started")
        with _exclusive_generation_lock(self.data_dir, self.asset_type):
            self._mark_dirty(full_rebuild_required=True)

    def _mark_dirty(self, *, full_rebuild_required: bool = False) -> None:
        path = _marker_path(self.data_dir, self.asset_type)
        current = _read_marker(path)
        if current is None or current.get("publication_id") != self._publication_id:
            raise EnrichedGenerationUnavailableError("enriched publication ownership was lost")
        self._changed = True
        if not current.get("dirty") or (full_rebuild_required and not current.get("full_rebuild_required")):
            current["dirty"] = True
            current["full_rebuild_required"] = bool(
                current.get("full_rebuild_required") or full_rebuild_required or self.full_rebuild
            )
            _write_marker(path, current)

    def _release(self) -> None:
        self._publishing = False
        self._changed = False
        _ACTIVE_PUBLICATIONS.pop(self._publication_id, None)
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    def abandon(self) -> None:
        path = _marker_path(self.data_dir, self.asset_type)
        try:
            if self._publishing and not self._changed and not self._base_incomplete:
                with _exclusive_generation_lock(self.data_dir, self.asset_type):
                    current = _read_marker(path)
                    if current is not None and current.get("publication_id") == self._publication_id:
                        _write_marker(path, _ready_payload(str(self._base_generation)))
        except (OSError, EnrichedGenerationUnavailableError):
            # Preserve the original failure; a clean marker can be recovered later.
            pass
        finally:
            self._release()

    def write_parquet(self, df: pl.DataFrame, out: Path) -> None:
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary = out.with_name(f".{out.name}.{uuid.uuid4().hex}.tmp")
        try:
            df.write_parquet(temporary)
            with temporary.open("r+b") as stream:
                stream.flush()
                os.fsync(stream.fileno())
            with _exclusive_generation_lock(self.data_dir, self.asset_type):
                self._claim_or_verify()
                # Persist before replacement so a crash cannot make partial data readable.
                self._mark_dirty()
                os.replace(temporary, out)
                _fsync_directory(out.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def commit(self) -> str | None:
        if not self._changed:
            self.abandon()
            return None
        if not self._publishing:
            # 本次是同一发布对象的重复提交: 上一次 commit 已落盘并复位
            # _publishing, 标记也回到了 ready (ready payload 不含
            # publication_id)。写入方会把一个发布对象复用到多个分区
            # (如 ETF enriched 按完整本地历史一次写 245+ 个交易日分区),
            # 其中内容无变化的分区会直接走到这里 —— 此时没有任何待提交
            # 内容, 必须幂等返回, 否则会误判为 "ownership lost" 并打断
            # 整批写入。
            self._changed = False
            return None
        path = _marker_path(self.data_dir, self.asset_type)
        with _exclusive_generation_lock(self.data_dir, self.asset_type):
            current = _read_marker(path)
            if current is None or current.get("publication_id") != self._publication_id:
                raise EnrichedGenerationUnavailableError(
                    "enriched publication ownership was lost"
                )
            generation = uuid.uuid4().hex
            _write_marker(path, _ready_payload(generation))
        self._release()
        # 待发布内容已全部落盘, 复位脏标记: 多日期分区写入复用同一发布对象,
        # 无变化分区会再调 commit() (空提交) — 不复位会以过期脏标记命中
        # ownership 校验而误抛 "ownership was lost" (Issue #417)。
        # 之后的写入会经 _claim_or_verify 重新认领并再次置位。
        self._changed = False
        return generation

    def _claim_or_verify(self) -> None:
        path = _marker_path(self.data_dir, self.asset_type)
        marker_invalid = False
        try:
            current = _read_marker(path)
        except EnrichedGenerationUnavailableError:
            if not self.recover or not self.full_rebuild:
                raise
            marker_invalid = True
            current = None
        if self._publishing:
            if current is None or current.get("publication_id") != self._publication_id:
                raise EnrichedGenerationUnavailableError(
                    "enriched publication ownership was lost"
                )
            return
        if current is not None and not _is_ready_payload(current):
            if _publication_claim_is_running(current, self.data_dir, self.asset_type):
                raise EnrichedGenerationUnavailableError(
                    "another enriched publication is active"
                )
            if not self.recover:
                raise EnrichedGenerationUnavailableError(
                    "another enriched publication is incomplete"
                )
            if (
                current.get("full_rebuild_required")
                or current.get("lease") is not True
                or not isinstance(current.get("dirty"), bool)
            ) and not self.full_rebuild:
                raise EnrichedGenerationUnavailableError("enriched data requires a full rebuild")
        self._base_incomplete = marker_invalid or bool(current and not _is_ready_payload(current))
        generation = None if current is None else current.get("generation")
        if not isinstance(generation, str) or not generation:
            generation = uuid.uuid4().hex
        self._base_generation = generation
        self._lease = _publication_lease(self.data_dir, self.asset_type)
        try:
            _write_marker(path, {
                "state": "publishing",
                "generation": generation,
                "publication_id": self._publication_id,
                "owner_pid": os.getpid(),
                "updated_at_ns": time.time_ns(),
                "lease": True,
                "dirty": self._base_incomplete,
                "full_rebuild_required": self._base_incomplete and (
                    self.full_rebuild or bool(current and current.get("full_rebuild_required"))
                ),
            })
        except BaseException:
            self._release()
            raise
        _ACTIVE_PUBLICATIONS[self._publication_id] = self
        self._publishing = True
