"""策略 run_all 渐进式执行 — 单飞后台执行 + 快策略先返回。

页面进入策略页时 run_all 全量跑需要 ~2 分钟, 用户只能盯着空卡片等。此模块把
执行拆成「提交取得任务编号 + 后台计算 + 状态查询」:

- 全局同一时刻只执行一个选股任务 (包括单策略、分钟和 run_all),
  按用户轮转调度; 相同 key (资产/周期/日期/策略集) 的重复请求
  直接搭车现有执行, 不重复算。
- 按历史耗时升序执行: 页面提交立即返回任务编号，快策略先完成，
  慢策略留在后台继续；原有调用方仍可短暂等待首批结果。
- 每个策略算完立刻增量写入 strategy_cache, 前端轮询任务状态获知完成或失败，
  并刷新 cached-summary 逐个点亮卡片数字。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future
from contextvars import copy_context
from copy import deepcopy
from pathlib import Path

from app.services.private_storage import private_path
from app.services.task_scheduler import TaskScheduler
from app.services.user_context import current_user, user_scope

logger = logging.getLogger(__name__)

_TIMINGS_FILENAME = "strategy_run_timings.json"
_timings_lock = threading.Lock()
_STATUS_TTL = 30 * 60
_MAX_COMPLETED_STATUSES = 256
_CAPACITY_WAIT_TIMEOUT_S = 300.0


def _timings_path(data_dir: Path) -> Path:
    return private_path(data_dir, "user_data", _TIMINGS_FILENAME)


def load_run_timings(data_dir: Path) -> dict[str, float]:
    """读取各策略上次执行耗时 (ms); 无文件/损坏时返回空。"""
    with _timings_lock:
        try:
            data = json.loads(_timings_path(data_dir).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}


def record_run_timings(data_dir: Path, elapsed_ms: dict[str, float]) -> None:
    """批量记录策略耗时 (ms), 与已有文件合并后原子重写。"""
    if not elapsed_ms:
        return
    with _timings_lock:
        path = _timings_path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        merged: dict[str, float] = {}
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(old, dict):
                merged = {str(k): float(v) for k, v in old.items() if isinstance(v, (int, float))}
        except (FileNotFoundError, ValueError, OSError):
            pass
        merged.update({sid: float(ms) for sid, ms in elapsed_ms.items()})
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)


def order_strategy_ids(all_ids: list[str], timings: dict[str, float]) -> list[str]:
    """快策略先算: 有历史耗时的按耗时升序, 未知耗时的保持原顺序排在后面。"""
    known = sorted(
        (timings[sid], i, sid) for i, sid in enumerate(all_ids) if sid in timings
    )
    known_ids = {sid for _, _, sid in known}
    unknown = [sid for sid in all_ids if sid not in known_ids]
    return [sid for _, _, sid in known] + unknown


class StrategyRunHandle:
    """一次 run_all 的执行状态; 端点线程 (读) 与后台执行线程 (写) 共享。"""

    def __init__(self, key: tuple, ordered_ids: list[str], metadata: dict | None = None) -> None:
        self.key = key
        self.metadata = metadata or {}
        self.run_id = uuid.uuid4().hex
        self.started_at_ms = int(time.time() * 1000)
        self._lock = threading.Lock()
        self._results: dict[str, dict] = {}
        self._remaining: list[str] = list(ordered_ids)
        self._errors: dict[str, str] = {}
        self._error: str | None = None
        self._done = False
        self._phase = "queued"
        self.cancel_event = threading.Event()
        self.future: Future = Future()

    def set_phase(self, phase: str) -> None:
        with self._lock:
            if not self._done:
                self._phase = "cancelling" if self.cancel_event.is_set() else phase

    def request_cancel(self) -> None:
        with self._lock:
            if not self._done:
                self.cancel_event.set()
                self._phase = "cancelling"

    def checkpoint(self) -> None:
        from app.services.heavy_job_limiter import HeavyJobCancelledError
        if self.cancel_event.is_set():
            raise HeavyJobCancelledError("策略计算已取消")

    def complete(self, sid: str, payload: dict) -> None:
        with self._lock:
            self._results[sid] = payload
            if sid in self._remaining:
                self._remaining.remove(sid)

    def fail_one(self, sid: str, message: str) -> None:
        """单个策略失败: 记错误并移出待算队列, 不影响其余策略继续。"""
        with self._lock:
            self._errors[sid] = message
            if sid in self._remaining:
                self._remaining.remove(sid)

    def fail(self, message: str) -> None:
        with self._lock:
            self._error = message
            self._remaining.clear()
            self._done = True
            self._phase = "done"

    def finish(self) -> None:
        with self._lock:
            self._done = True
            self._phase = "cancelled" if self.cancel_event.is_set() else "done"
            self._remaining.clear()

    def snapshot(self) -> dict:
        """线程安全快照: 结果拷贝 + 剩余/逐策略错误/整体错误/完成状态。"""
        with self._lock:
            return {
                "run_id": self.run_id,
                "metadata": dict(self.metadata),
                "results": dict(self._results),
                "pending": list(self._remaining),
                "errors": dict(self._errors),
                "error": self._error,
                "done": self._done,
                "phase": self._phase,
                "started_at_ms": self.started_at_ms,
            }


class StrategyRunManager:
    """run_all 单飞管理器。

    - 相同 key 且仍在执行 (含排队中) 的重复请求搭车现有执行, 不重复算
      (页面 reload / StrictMode / 反复切换); 已完成的不再搭车, 重跑即新执行。
    - 不同 key 在唯一 daemon 工作线程里排队; 端点在首返时限内等不到也只能
      先返回 pending, 前端靠 run_id 轮询终态，再从缓存拿最终结果。
    - 工作线程为 daemon: 进程退出不等待剩余计算 (缓存写入均为原子替换,
      中断只留部分结果, 下次进入页面补算)。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: dict[tuple, StrategyRunHandle] = {}
        self._runs: dict[str, StrategyRunHandle] = {}
        # Completed entries contain only compact status, never Future/tracebacks.
        self._completed: OrderedDict = OrderedDict()
        self._scheduler = TaskScheduler(workers=1, name="strategy")

    def _prune_statuses(self):
        now = time.monotonic()
        while self._completed:
            _, (_, finished, _) = next(iter(self._completed.items()))
            if len(self._completed) <= _MAX_COMPLETED_STATUSES and now - finished < _STATUS_TTL:
                break
            self._completed.popitem(last=False)

    def forget_user(self, data_dir: Path, uid: str) -> None:
        """Drop personal results after active operations have drained."""
        owner_key = (str(data_dir.resolve()), uid)
        with self._lock:
            for key, handle in list(self._handles.items()):
                if key[:2] == owner_key:
                    handle.future.cancel()
                    handle.fail("账号已删除")
                    self._handles.pop(key)
                    self._runs.pop(handle.run_id, None)
            for run_id, (owner, _, _) in list(self._completed.items()):
                if owner == owner_key:
                    self._completed.pop(run_id)

    def status(self, run_id: str) -> dict | None:
        owner = current_user()
        owner_key = (str(owner.data_dir.resolve()), owner.id)
        with self._lock:
            self._prune_statuses()
            handle = self._runs.get(run_id)
            if handle is not None:
                return deepcopy(handle.snapshot()) if handle.key[:2] == owner_key else None
            completed = self._completed.get(run_id)
            if completed is None or completed[0] != owner_key:
                return None
            return deepcopy(completed[2])

    def active(self, key: tuple) -> dict | None:
        owner = current_user()
        full_key = (str(owner.data_dir.resolve()), owner.id, *key)
        with self._lock:
            handle = self._handles.get(full_key)
            if handle is None or handle.snapshot()["done"]:
                return None
            return deepcopy(handle.snapshot())

    def list_statuses(self, namespace: str) -> list[dict]:
        """One owner-scoped read restores every card, including terminal failures."""
        owner = current_user()
        owner_key = (str(owner.data_dir.resolve()), owner.id)
        with self._lock:
            self._prune_statuses()
            statuses = [h.snapshot() for h in self._runs.values() if h.key[:2] == owner_key]
            statuses.extend(s for key, _, s in self._completed.values() if key == owner_key)
            return deepcopy([s for s in statuses if s.get("metadata", {}).get("namespace") == namespace])

    def get_or_submit(
        self,
        key: tuple,
        ordered_ids: list[str],
        job: Callable[[StrategyRunHandle], None],
        *,
        metadata: dict | None = None,
    ) -> StrategyRunHandle:
        owner = current_user()
        key = (str(owner.data_dir.resolve()), owner.id, *key)
        context = copy_context()
        original_job = job
        def owned_job(handle):
            def run():
                with user_scope(owner):
                    return original_job(handle)
            return context.run(run)
        with self._lock:
            self._prune_statuses()
            existing = self._handles.get(key)
            if existing is not None and not existing.snapshot()["done"]:
                return existing
            handle = StrategyRunHandle(key, ordered_ids, metadata)
            self._handles[key] = handle
            if ordered_ids:
                self._runs[handle.run_id] = handle
            try:
                handle.future = self._scheduler.submit(key[:2], lambda: self._run_one(handle, owned_job))
            except Exception:
                del self._handles[key]
                self._runs.pop(handle.run_id, None)
                raise
        return handle

    def cancel(self, run_id: str) -> dict | None:
        owner = current_user()
        owner_key = (str(owner.data_dir.resolve()), owner.id)
        with self._lock:
            self._prune_statuses()
            handle = self._runs.get(run_id)
            if handle is None:
                completed = self._completed.get(run_id)
                return deepcopy(completed[2]) if completed and completed[0] == owner_key else None
            if handle.key[:2] != owner_key:
                return None
            handle.request_cancel()
        # Future cancellation releases queued capacity immediately; never mark an
        # executing native computation as finished before its thread has exited.
        if handle.future.cancel():
            handle.finish()
            self._retire(handle)
        return deepcopy(handle.snapshot())

    def _retire(self, handle):
        with self._lock:
            if self._handles.get(handle.key) is handle:
                self._handles.pop(handle.key)
            if self._runs.pop(handle.run_id, None) is not None:
                self._completed[handle.run_id] = (handle.key[:2], time.monotonic(), handle.snapshot())
                self._prune_statuses()

    def _run_one(self, handle, job):
        from app.services.heavy_job_limiter import (
            HeavyJobCancelledError,
            HeavyJobLimitTimeoutError,
            shared_heavy_job_limiter,
        )
        try:
            # Cold repository loads can rebuild the full enriched matrix and
            # require an exclusive reservation; nested loads reuse this slot.
            handle.set_phase("waiting")
            with shared_heavy_job_limiter.slot(
                "exclusive", timeout=_CAPACITY_WAIT_TIMEOUT_S, cancel_event=handle.cancel_event,
            ):
                handle.checkpoint()
                handle.set_phase("running")
                result = job(handle)
        except HeavyJobCancelledError:
            handle.finish()
        except HeavyJobLimitTimeoutError as exc:
            message = "等待服务器计算资源超时，可能内存不足或有其他重任务正在运行，请稍后重试"
            handle.fail(message)
            raise TimeoutError(message) from exc
        except BaseException as exc:
            # Native compute failures can inherit BaseException; mirror the worker
            # boundary so every failed Future also has a terminal UI status.
            logger.exception("策略后台执行失败: %s", exc)
            handle.fail(str(exc))
            raise
        else:
            handle.finish()
            return result
        finally:
            self._retire(handle)

    def run_sync(self, key: tuple, work: Callable):
        """单策略、分钟与完整批次共用执行槽；只复用在途结果，返回独立快照。"""
        handle = self.get_or_submit(key, [], lambda _handle: work())
        return deepcopy(handle.future.result())


def command_key(repo, engine, asset_type: str, *inputs) -> tuple:
    """身份由 manager 补入；参数、引擎注册表及数据版本均参与在途复用。"""
    generation = getattr(repo, "get_matrix_data_generation", None)
    return (
        str(repo.store.data_dir.resolve()),
        id(getattr(engine, "_strategies", engine)),
        generation(asset_type) if callable(generation) else None,
        json.dumps(inputs, sort_keys=True, default=str, ensure_ascii=False),
    )


# 进程级单例: 与 strategy_cache 的模块级锁同风格, 生命周期跟随进程
MANAGER = StrategyRunManager()
