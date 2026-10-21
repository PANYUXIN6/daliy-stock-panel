"""TickFlow Pro 80% rpm safety budget (process-local)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.rate_limits import (
    SAFETY_RPM_FACTOR,
    apply_safety_rpm,
    resolve_limit,
    sleep_between_batches,
)


def test_apply_safety_rpm_scales_to_80_percent() -> None:
    assert SAFETY_RPM_FACTOR == 0.8
    assert apply_safety_rpm(120) == 96
    assert apply_safety_rpm(60) == 48
    assert apply_safety_rpm(30) == 24
    assert apply_safety_rpm(None) is None


def test_resolve_limit_applies_safety_by_default() -> None:
    capset = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(rpm=60, batch=100)})
    lim = resolve_limit(capset, Cap.KLINE_DAILY_BATCH)
    assert lim.rpm == 48
    assert lim.batch == 100


def test_resolve_limit_can_skip_safety_for_diagnostics() -> None:
    capset = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(rpm=60, batch=100)})
    lim = resolve_limit(capset, Cap.KLINE_DAILY_BATCH, apply_safety=False)
    assert lim.rpm == 60


def test_two_first_batches_share_reserved_budget(monkeypatch) -> None:
    from app.tickflow import rate_limits

    clock = SimpleNamespace(now=0.)
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds
    monkeypatch.setattr(rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(rate_limits, "_next_slot", {})
    sleep_between_batches(0, rpm=60)
    sleep_between_batches(0, rpm=60)
    assert sleeps == [1.]


def test_second_batch_waits_for_shared_slot(monkeypatch) -> None:
    from app.tickflow import rate_limits

    sleeps = []
    monkeypatch.setattr(rate_limits, "time", SimpleNamespace(monotonic=lambda: 0., sleep=sleeps.append))
    monkeypatch.setattr(rate_limits, "_next_slot", {})
    sleep_between_batches(0, rpm=60)
    sleep_between_batches(1, rpm=60)
    assert sleeps == [1.]


def test_concurrent_first_batches_reserve_distinct_slots(monkeypatch) -> None:
    from app.tickflow import rate_limits

    sleeps = []
    monkeypatch.setattr(rate_limits, "time", SimpleNamespace(monotonic=lambda: 0., sleep=sleeps.append))
    monkeypatch.setattr(rate_limits, "_next_slot", {})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: sleep_between_batches(0, rpm=60), range(4)))
    assert sorted(sleeps) == [1., 2., 3.]
