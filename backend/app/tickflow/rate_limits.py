"""TickFlow capability rate-limit helpers.

This module centralizes the small pieces of batch/rpm resolution used by
TickFlow-backed services. It intentionally does not manage custom data sources.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TypeVar

from app.tickflow.capabilities import Cap, CapabilitySet

T = TypeVar("T")

# TickFlow Pro 单进程安全预算: 套餐标称 rpm 的 80%。
# _next_slot 只在本进程内共享；其他进程的请求不会扣减同一预算。
SAFETY_RPM_FACTOR = 0.8

# 进程级共享限速器: 按 rpm 分桶的「下一个可用时刻」表 (Lock 守护)。
# 首批与后续批次都等待预约时刻，避免多个调用方同时启动时绕过共享预算。
# 预算只在本进程内共享；多个 probe 或批量 sync 进程仍需错峰，不是账户级限频。
_slot_lock = threading.Lock()
_next_slot: dict[int, float] = {}


def apply_safety_rpm(rpm: int | None, *, factor: float = SAFETY_RPM_FACTOR) -> int | None:
    """Scale a package rpm by the shared safety factor (default 80%)."""
    if rpm is None or rpm <= 0:
        return rpm
    return max(1, int(rpm * factor))


def _reserve_slot(rpm: int, interval: float) -> float:
    """在共享时间轴上为一次请求预约一个发包槽, 返回需等待的秒数 (>=0)。

    interval = 60/rpm。now 早于该 rpm 桶的 next_slot 时排到 next_slot, 否则排到 now;
    随后把该桶 next_slot 后移 interval。持锁仅做时间账目, 不在锁内 sleep。
    """
    key = rpm if rpm and rpm > 0 else -1
    with _slot_lock:
        now = time.monotonic()
        scheduled = max(now, _next_slot.get(key, now))
        _next_slot[key] = scheduled + interval
        return scheduled - now


@dataclass(frozen=True)
class ResolvedLimit:
    batch: int | None
    rpm: int | None


def resolve_limit(
    capset: CapabilitySet,
    cap: Cap,
    *,
    default_batch: int | None = None,
    default_rpm: int | None = None,
    default_rpm_when_unset: bool = True,
    apply_safety: bool = True,
) -> ResolvedLimit:
    """Return a capability's batch/rpm with caller-provided fallbacks.

    By default rpm is scaled by SAFETY_RPM_FACTOR (0.8) inside this process only.
    This is not a cross-container account budget. Pass apply_safety=False for diagnostics.
    """
    lim = capset.limits(cap)
    if lim is None:
        rpm = default_rpm
    else:
        rpm = lim.rpm if lim.rpm else (default_rpm if default_rpm_when_unset else None)
        default_batch = lim.batch if lim.batch else default_batch
    if apply_safety:
        rpm = apply_safety_rpm(rpm)
    if lim is None:
        return ResolvedLimit(batch=default_batch, rpm=rpm)
    return ResolvedLimit(
        batch=default_batch,
        rpm=rpm,
    )


def batch_interval(rpm: int | None, *, default: float = 0.0) -> float:
    """Return the existing uniform batch interval formula: 60 / rpm."""
    return 60.0 / rpm if rpm and rpm > 0 else default


def chunked(items: list[T], batch_size: int | None) -> list[list[T]]:
    """Split items by batch size, preserving the existing None-as-one-batch behavior."""
    if batch_size is None:
        return [items]
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


def sleep_between_batches(index: int, rpm: int | None, *, default_interval: float = 0.0) -> None:
    """Pace batches via the process-local shared slot table.

    Every batch, including a new caller's first batch, waits for its reserved slot.
    The first request on an idle budget proceeds immediately. The slot table is
    not shared across processes, so concurrent probe/sync processes need coordination.
    """
    interval = batch_interval(rpm, default=default_interval)
    if interval <= 0:
        return
    wait = _reserve_slot(rpm or -1, interval)
    if wait > 0:
        time.sleep(wait)


def min_batch(preferred: int, limit: ResolvedLimit) -> int:
    """Clamp a user-preferred batch size by a resolved capability batch limit."""
    return min(preferred, limit.batch) if limit.batch else preferred
