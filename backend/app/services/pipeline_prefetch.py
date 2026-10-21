"""盘后管道的有界网络预取；工作线程不写仓库或刷新视图。"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from contextlib import suppress
from threading import Event, RLock
from typing import Any, TypeVar

from app.services.pipeline_jobs import JobCancelledError

T = TypeVar("T")


class PipelineProgress:
    """保留主阶段协议，同时明确上报主阶段和预取子任务的独立状态。"""

    def __init__(self, callback: Callable[..., None]) -> None:
        self.callback = callback
        self._lock = RLock()
        self._stage: str | None = None
        self._pct = 0
        self._message = ""

    def __call__(self, stage: str, pct: int, message: str, **kwargs: Any) -> None:
        with self._lock:
            self._pct = max(self._pct, pct)
            if stage != self._stage:
                self.finish()
                self._stage = None if stage == "done" else stage
            self._message = message
            self.callback(stage, self._pct, message, **kwargs)
            if self._stage is not None:
                self.task(stage, stage, "running", kwargs.get("stage_pct", 0), message)

    def task(self, task_id: str, label: str, status: str, progress: int = 0,
             message: str = "", error: str | None = None) -> None:
        with self._lock:
            self.callback(
                self._stage or "init", self._pct, message,
                task_id=task_id, task_label=label, task_status=status,
                task_progress=progress, task_error=error, skip_log=True,
            )

    def finish(self, status: str = "succeeded", error: str | None = None) -> None:
        with self._lock:
            if self._stage is not None:
                self.task(self._stage, self._stage, status,
                          100 if status == "succeeded" else 0, self._message, error)
                self._stage = None


class PipelinePrefetch:
    """一个管道最多两路网络预取，失败／取消退出前等待在途请求结束。"""

    def __init__(self, progress: PipelineProgress) -> None:
        self.progress = progress
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="postmarket-fetch")
        self._stop = Event()
        self._futures: dict[str, Future[Any]] = {}
        self._labels: dict[str, str] = {}

    def _ready(self, task_id: str, result: Any) -> None:
        state = result.get("state") if isinstance(result, dict) else None
        if state in {"source_unavailable", "no_data"}:
            self.progress.task(task_id, self._labels[task_id], "skipped", message=(
                "数据源不可用，已跳过" if state == "source_unavailable" else "上游暂无数据，已跳过"
            ))
        else:
            day = result.get("trade_date") if isinstance(result, dict) else None
            message = "取数完成，等待顺序写入" + (f"；来源日期 {day}" if day else "")
            self.progress.task(task_id, self._labels[task_id], "succeeded", 100, message)

    def submit(self, task_id: str, label: str, fetch: Callable[[], T]) -> None:
        if task_id in self._futures:
            raise ValueError(f"重复预取任务: {task_id}")
        self._labels[task_id] = label
        self.progress.task(task_id, label, "pending", message="等待取数")

        def run() -> T:
            if self._stop.is_set():
                raise JobCancelledError(task_id)
            try:
                self.progress.task(task_id, label, "running", message="正在取数")
                result = fetch()
                if self._stop.is_set():
                    raise JobCancelledError(task_id)
                self._ready(task_id, result)
                return result
            except BaseException as exc:
                status = "cancelled" if isinstance(exc, (JobCancelledError, CancelledError)) else "failed"
                with suppress(JobCancelledError):
                    self.progress.task(task_id, label, status, error=str(exc), message=str(exc))
                raise

        self._futures[task_id] = self._executor.submit(run)

    def has(self, task_id: str) -> bool:
        return task_id in self._futures

    def check_cancelled(self) -> None:
        """池退出后不再取结果或发出正式重取；任务取消另由进度回调校验。"""
        if self._stop.is_set():
            raise JobCancelledError("postmarket-fetch")

    def result(self, task_id: str) -> Any:
        self.check_cancelled()
        try:
            result = self._futures[task_id].result()
        except CancelledError as exc:
            raise JobCancelledError(task_id) from exc
        self.check_cancelled()
        # 同步入口的进度回调同时校验 JobStore 取消信号；取消后不得继续写入。
        self._ready(task_id, result)
        return result

    def close(self) -> None:
        self._stop.set()
        for task_id, future in self._futures.items():
            if future.cancel():
                with suppress(JobCancelledError):
                    self.progress.task(task_id, self._labels[task_id], "cancelled", message="预取已停止")
        # 在途网络请求沿用供应商超时；清理完成前不释放管道执行槽。
        self._executor.shutdown(wait=True, cancel_futures=True)
