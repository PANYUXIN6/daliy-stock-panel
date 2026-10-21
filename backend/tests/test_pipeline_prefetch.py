from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import pytest

from app.services.pipeline_jobs import JobCancelledError, JobStore
from app.services.pipeline_prefetch import PipelinePrefetch, PipelineProgress


def progress_store(tmp_path):
    store = JobStore(store_dir=tmp_path / "jobs")
    job_id, _ = store.create()
    store.start(job_id)
    progress = PipelineProgress(lambda *args, **kwargs: store.progress(job_id, *args, **kwargs))
    return store, job_id, progress


def test_two_network_workers_overlap_primary_computation_without_overwriting_it(tmp_path):
    store, job_id, progress = progress_store(tmp_path)
    pool = PipelinePrefetch(progress)
    started = [Event(), Event()]
    release = Event()

    def fetch(index):
        started[index].set()
        assert release.wait(5)
        return index

    try:
        progress("compute_enriched", 65, "计算指标", stage_pct=12)
        for index in range(2):
            pool.submit(f"fetch_{index}", "取数", lambda index=index: fetch(index))
        assert all(event.wait(5) for event in started)
        job = store.get(job_id)
        assert job["stage"] == "compute_enriched"
        assert job["stage_pct"] == 12
        assert job["tasks"]["compute_enriched"]["status"] == "running"
        assert [job["tasks"][f"fetch_{index}"]["status"] for index in range(2)] == ["running"] * 2
        release.set()
        assert [pool.result(f"fetch_{index}") for index in range(2)] == [0, 1]
        progress("verify_postmarket", 89, "验收")
        job = store.get(job_id)
        assert job["tasks"]["compute_enriched"]["status"] == "succeeded"
        assert job["stage_pct"] == 0
        assert job["stage"] == "verify_postmarket"
    finally:
        release.set()
        pool.close()


def test_prefetch_concurrency_is_bounded_and_failed_inputs_propagate(tmp_path):
    store, job_id, progress = progress_store(tmp_path)
    pool = PipelinePrefetch(progress)
    active = 0
    peak = 0
    lock = Lock()
    release = Event()
    two_started = Event()

    def fetch(index):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 2:
                two_started.set()
        try:
            assert release.wait(5)
            if index == 3:
                raise ValueError("缺少当日数据")
            return index
        finally:
            with lock:
                active -= 1

    try:
        for index in range(5):
            pool.submit(str(index), "取数", lambda index=index: fetch(index))
        assert two_started.wait(5)
        assert store.get(job_id)["tasks"]["4"]["status"] == "pending"
        release.set()
        assert pool.result("4") == 4
        with pytest.raises(ValueError, match="缺少当日数据"):
            pool.result("3")
        assert peak == 2
        assert store.get(job_id)["tasks"]["3"]["status"] == "failed"
    finally:
        release.set()
        pool.close()


def test_close_waits_inflight_requests_and_cancels_queued_requests(tmp_path):
    store, job_id, progress = progress_store(tmp_path)
    pool = PipelinePrefetch(progress)
    started = [Event(), Event()]
    release = Event()
    queued_ran = Event()

    def fetch(index):
        started[index].set()
        assert release.wait(5)

    for index in range(2):
        pool.submit(str(index), "取数", lambda index=index: fetch(index))
    pool.submit("queued", "待取数", queued_ran.set)
    assert all(event.wait(5) for event in started)
    with ThreadPoolExecutor(max_workers=1) as closer:
        closing = closer.submit(pool.close)
        assert pool._stop.wait(5)
        assert not closing.done()
        release.set()
        closing.result(timeout=5)
    assert not queued_ran.is_set()
    assert store.get(job_id)["tasks"]["queued"]["status"] == "cancelled"


def test_cancellation_after_fetch_completes_is_checked_before_commit(tmp_path):
    store, job_id, progress = progress_store(tmp_path)
    pool = PipelinePrefetch(progress)
    try:
        pool.submit("fetch", "取数", lambda: "prepared")
        assert pool._futures["fetch"].result(timeout=5) == "prepared"
        store.terminate(job_id, "用户手动取消")
        with pytest.raises(JobCancelledError):
            pool.result("fetch")
        assert store.get(job_id)["outcome"] == "cancelled"
    finally:
        pool.close()


def test_stage_failure_remains_failed_when_cleanup_changes_primary_stage(tmp_path):
    store, job_id, progress = progress_store(tmp_path)
    progress("compute_regime", 90, "计算环境", stage_pct=20)
    progress.finish("failed", "环境无效")
    progress("refresh_views", 95, "刷新")
    assert store.get(job_id)["tasks"]["compute_regime"]["status"] == "failed"
    assert store.get(job_id)["tasks"]["compute_regime"]["error"] == "环境无效"


def test_optional_source_unavailable_is_skipped_and_real_source_date_is_visible(tmp_path):
    store, job_id, progress = progress_store(tmp_path)
    pool = PipelinePrefetch(progress)
    try:
        pool.submit("missing", "竞价", lambda: {"state": "source_unavailable"})
        pool.submit("old", "龙虎榜", lambda: {"state": "ok", "trade_date": "2026-10-15"})
        pool.result("missing")
        pool.result("old")
        tasks = store.get(job_id)["tasks"]
        assert tasks["missing"]["status"] == "skipped"
        assert "不可用" in tasks["missing"]["message"]
        assert "2026-10-15" in tasks["old"]["message"]
    finally:
        pool.close()
