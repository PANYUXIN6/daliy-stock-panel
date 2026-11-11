"""strategy_run_queue 单测 — 耗时排序、耗时落盘、handle 状态、单飞管理器。"""
from __future__ import annotations

import threading
import time

import pytest

from app.services import strategy_run_queue as q

pytestmark = pytest.mark.usefixtures("private_owner")



def test_order_strategy_ids_fast_first_unknown_last():
    ids = ["slow_a", "mid_b", "fast_c", "new_d", "new_e"]
    timings = {"slow_a": 5000.0, "fast_c": 100.0, "mid_b": 1000.0}
    assert q.order_strategy_ids(ids, timings) == ["fast_c", "mid_b", "slow_a", "new_d", "new_e"]


def test_run_timings_record_merge_and_load(tmp_path):
    assert q.load_run_timings(tmp_path) == {}
    q.record_run_timings(tmp_path, {"a": 100.0})
    q.record_run_timings(tmp_path, {"b": 200.0, "a": 50.0})
    assert q.load_run_timings(tmp_path) == {"a": 50.0, "b": 200.0}


def test_run_timings_survives_corrupt_file(tmp_path):
    path = q._timings_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")
    assert q.load_run_timings(tmp_path) == {}
    q.record_run_timings(tmp_path, {"a": 1.0})
    assert q.load_run_timings(tmp_path) == {"a": 1.0}


def test_handle_snapshot_lifecycle():
    h = q.StrategyRunHandle(("k",), ["s1", "s2"])
    snap = h.snapshot()
    assert snap["pending"] == ["s1", "s2"]
    assert snap["done"] is False

    h.complete("s1", {"total": 3})
    snap = h.snapshot()
    assert snap["results"] == {"s1": {"total": 3}}
    assert snap["pending"] == ["s2"]

    h.fail("boom")
    snap = h.snapshot()
    assert snap["error"] == "boom"
    assert snap["done"] is True


def _wait_done(handle, timeout=5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if handle.snapshot()["done"]:
            return
        time.sleep(0.01)
    raise AssertionError("handle 未在时限内完成")


def test_manager_piggybacks_same_running_key():
    mgr = q.StrategyRunManager()
    started = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    def job(handle):
        calls.append(1)
        started.set()
        assert release.wait(timeout=5)

    h1 = mgr.get_or_submit(("k",), ["s"], job)
    assert started.wait(timeout=5)
    # 执行中: 相同 key 搭车, 不重复提交
    h2 = mgr.get_or_submit(("k",), ["s"], job)
    assert h2 is h1

    release.set()
    _wait_done(h1)
    assert calls == [1]

    # 已完成后: 同 key 再来 → 新执行 (重跑语义)
    h3 = mgr.get_or_submit(("k",), ["s"], job)
    assert h3 is not h1
    _wait_done(h3)
    assert calls == [1, 1]


def test_manager_piggybacks_same_queued_key():
    mgr = q.StrategyRunManager()
    started = threading.Event()
    release = threading.Event()

    def job(handle):
        started.set()
        assert release.wait(timeout=5)

    h1 = mgr.get_or_submit(("a",), ["s"], job)  # 占住唯一 worker
    assert started.wait(timeout=5)
    # key b 排队 (未开始), 此时 b 的重复请求应搭车排队中的 handle
    h2 = mgr.get_or_submit(("b",), ["s"], job)
    h3 = mgr.get_or_submit(("b",), ["s"], job)
    assert h2 is h3

    release.set()
    _wait_done(h1)
    _wait_done(h2, timeout=10)


def test_manager_serializes_different_keys():
    mgr = q.StrategyRunManager()
    lock = threading.Lock()
    active: list[str] = []
    overlap: list[list[str]] = []

    def make_job(name):
        def job(handle):
            with lock:
                active.append(name)
                if len(active) > 1:
                    overlap.append(list(active))
            time.sleep(0.1)
            with lock:
                active.remove(name)

        return job

    h1 = mgr.get_or_submit(("a",), ["s"], make_job("a"))
    h2 = mgr.get_or_submit(("b",), ["s"], make_job("b"))
    _wait_done(h1, timeout=10)
    _wait_done(h2, timeout=10)
    assert overlap == []


def test_sync_job_can_cold_load_repository_with_exclusive_reservation():
    from app.services.heavy_job_limiter import shared_heavy_job_limiter

    def cold_load():
        with shared_heavy_job_limiter.slot('exclusive'):
            return {'rows': [{'symbol': 'A'}]}
    manager = q.StrategyRunManager()
    assert manager.run_sync(('cold',), cold_load) == {'rows': [{'symbol': 'A'}]}
    assert shared_heavy_job_limiter.in_use == 0


def test_terminal_status_survives_new_run_and_is_owner_scoped(tmp_path):
    from app.services.user_context import UserContext, user_scope

    manager = q.StrategyRunManager()
    first = manager.get_or_submit(('same',), ['bad'], lambda h: h.fail_one('bad', '财务数据获取失败'))
    first.future.result(5)
    second = manager.get_or_submit(('same',), ['bad'], lambda h: h.complete('bad', {'total': 0}))
    second.future.result(5)
    assert first.run_id != second.run_id
    status = manager.status(first.run_id)
    assert status['done'] and status['pending'] == []
    assert status['errors'] == {'bad': '财务数据获取失败'}
    status['errors'].clear()
    assert manager.status(first.run_id)['errors']
    for owner in (UserContext('b' * 32, 'admin', tmp_path), UserContext('a' * 32, 'admin', tmp_path / 'other')):
        with user_scope(owner):
            assert manager.status(first.run_id) is None


def test_completed_statuses_are_bounded_and_expire(monkeypatch):
    manager = q.StrategyRunManager()
    monkeypatch.setattr(q, '_MAX_COMPLETED_STATUSES', 1)
    first = manager.get_or_submit(('first',), ['a'], lambda h: h.complete('a', {'total': 0}))
    first.future.result(5)
    second = manager.get_or_submit(('second',), ['b'], lambda h: h.complete('b', {'total': 0}))
    second.future.result(5)
    assert manager.status(first.run_id) is None
    assert manager.status(second.run_id)['done']
    monkeypatch.setattr(q, '_STATUS_TTL', 0)
    assert manager.status(second.run_id) is None


@pytest.mark.parametrize('error_type', [ValueError, BaseException])
def test_job_level_failure_preserves_error_and_clears_pending(error_type):
    manager = q.StrategyRunManager()
    def fail(handle):
        raise error_type('准备历史行情失败')
    handle = manager.get_or_submit(('error',), ['a'], fail)
    with pytest.raises(error_type, match='历史行情'):
        handle.future.result(5)
    assert manager.status(handle.run_id)['error'] == '准备历史行情失败'
    assert manager.status(handle.run_id)['pending'] == []


def test_capacity_wait_times_out_and_releases_owner_quota(monkeypatch):
    from app.services import heavy_job_limiter as limiter

    monkeypatch.setattr(limiter, '_memory_admission_ok', lambda: False)
    monkeypatch.setattr(q, '_CAPACITY_WAIT_TIMEOUT_S', .02)
    manager = q.StrategyRunManager()
    executed = []
    for i in range(5):  # More than the per-owner quota must remain possible after failure.
        handle = manager.get_or_submit(('low-memory', i), ['a'], lambda h: executed.append(True))
        with pytest.raises(TimeoutError, match='等待服务器计算资源超时'):
            handle.future.result(5)
        status = manager.status(handle.run_id)
        assert status['done'] and not status['pending']
        assert '内存不足' in status['error']
    assert not executed


def test_cancel_queued_task_releases_quota_without_executing():
    manager = q.StrategyRunManager()
    entered, release = threading.Event(), threading.Event()

    def block(handle):
        entered.set()
        assert release.wait(5)

    first = manager.get_or_submit(('blocker',), ['a'], block)
    assert entered.wait(5)
    try:
        pending = [manager.get_or_submit(('queued', i), ['b'], lambda h: pytest.fail('cancelled work ran'))
                   for i in range(3)]
        assert manager.cancel(pending[0].run_id)['phase'] == 'cancelled'
        assert pending[0].future.cancelled()
        replacement = manager.get_or_submit(('replacement',), ['c'], lambda h: h.complete('c', {'total': 0}))
        for handle in pending[1:]:
            assert manager.cancel(handle.run_id)['done']
    finally:
        release.set()
    first.future.result(5)
    replacement.future.result(5)
    assert manager.status(replacement.run_id)['results'] == {'c': {'total': 0}}


def test_cancel_waiting_for_memory_does_not_require_memory_recovery(monkeypatch):
    from app.services import heavy_job_limiter as limiter

    checked = threading.Event()
    def no_memory():
        checked.set()
        return False
    monkeypatch.setattr(limiter, '_memory_admission_ok', no_memory)
    manager = q.StrategyRunManager()
    handle = manager.get_or_submit(('memory',), ['a'], lambda h: pytest.fail('work ran'))
    assert checked.wait(5)
    assert manager.status(handle.run_id)['phase'] == 'waiting'
    manager.cancel(handle.run_id)
    handle.future.result(5)
    assert manager.status(handle.run_id)['phase'] == 'cancelled'
    assert manager.status(handle.run_id)['error'] is None
    assert limiter.shared_heavy_job_limiter.in_use == 0


def test_cancel_running_task_waits_for_worker_and_preserves_completed_results(private_owner):
    from app.services.user_context import UserContext, user_scope

    manager = q.StrategyRunManager()
    entered, release = threading.Event(), threading.Event()
    def compute(handle):
        handle.complete('fast', {'total': 2})
        entered.set()
        assert release.wait(5)
        handle.checkpoint()
        pytest.fail('cancelled strategy continued')
    handle = manager.get_or_submit(('compute',), ['fast', 'slow'], compute)
    assert entered.wait(5)
    try:
        for other in (UserContext('b' * 32, 'admin', private_owner.data_dir),
                      UserContext(private_owner.id, 'admin', private_owner.data_dir / 'other')):
            with user_scope(other):
                assert manager.cancel(handle.run_id) is None
        assert not handle.cancel_event.is_set()
        status = manager.cancel(handle.run_id)
        assert status['phase'] == 'cancelling' and not status['done']
        assert status['results'] == {'fast': {'total': 2}}
    finally:
        release.set()
    handle.future.result(5)
    assert manager.status(handle.run_id)['phase'] == 'cancelled'
    assert manager.status(handle.run_id)['pending'] == []
