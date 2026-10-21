"""Concurrent pipeline progress must keep independent, durable task outcomes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from app.services import pipeline_jobs
from app.services.pipeline_jobs import JobCancelledError, JobStore


@pytest.fixture
def clock(monkeypatch):
    current = datetime(2026, 10, 21, 8, tzinfo=UTC)

    def now():
        return current.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def advance(seconds):
        nonlocal current
        current += timedelta(seconds=seconds)

    monkeypatch.setattr(pipeline_jobs, "_now_iso", now)
    return advance


def running_store(tmp_path):
    store = JobStore(store_dir=tmp_path / "jobs")
    job_id, _ = store.create(timeout_s=60)
    store.start(job_id)
    return store, job_id


def test_stage_switch_resets_only_stage_progress_and_total_never_regresses(tmp_path):
    store, job_id = running_store(tmp_path)
    store.progress(job_id, "sync_daily", 40, "股票", stage_pct=80)
    store.progress(job_id, "sync_daily", 42, "同一阶段未给百分比")
    assert store.get(job_id)["stage_pct"] == 80
    store.progress(job_id, "sync_index", 35, "指数")
    job = store.get(job_id)
    assert job["progress"] == 42
    assert job["stage_pct"] == 0


def test_background_task_updates_preserve_primary_stage_and_other_tasks(tmp_path, clock):
    store, job_id = running_store(tmp_path)
    store.progress(job_id, "compute_enriched", 60, "计算指标", stage_pct=45)
    store.progress(job_id, "fetch_index", 55, "指数请求中", task_id="index",
                   task_label="核心指数", task_status="running", task_progress=20)
    clock(1)
    store.progress(job_id, "fetch_concept", 50, "概念请求中", task_id="concept",
                   task_label="概念分类", task_status="running", task_progress=10)
    clock(2)
    store.progress(job_id, "fetch_index", 55, "指数已就绪", task_id="index",
                   task_status="succeeded", task_progress=100)
    job = store.get(job_id)
    assert (job["stage"], job["stage_pct"], job["progress"]) == ("compute_enriched", 45, 60)
    assert job["tasks"]["index"]["status"] == "succeeded"
    assert job["tasks"]["index"]["duration_s"] == 3
    assert job["tasks"]["concept"]["status"] == "running"
    assert job["tasks"]["concept"]["duration_s"] == 2
    assert job["tasks"]["concept"]["message"] == "概念请求中"


def test_concurrent_workers_do_not_overwrite_each_other_or_resurrect_terminal_tasks(tmp_path):
    store, job_id = running_store(tmp_path)
    ready = Barrier(3)

    def worker(task_id):
        ready.wait(timeout=5)
        for percent in range(0, 100, 10):
            store.progress(job_id, "fetch", percent // 2, "下载", task_id=task_id,
                           task_status="running", task_progress=percent, skip_log=True)
        store.progress(job_id, "fetch", 50, "已完成", task_id=task_id,
                       task_status="succeeded", skip_log=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(worker, task_id) for task_id in ("index", "concept")]
        ready.wait(timeout=5)
        for future in futures:
            future.result(timeout=5)
    store.progress(job_id, "fetch", 10, "迟到的请求进度", task_id="index",
                   task_status="running", task_progress=20, skip_log=True)
    job = store.get(job_id)
    assert job["stage"] == "init"
    assert job["progress"] == 50
    assert len(job["tasks"]) == 2
    assert all(task["status"] == "succeeded" and task["progress"] == 100
               for task in job["tasks"].values())
    assert job["log"] == []


def test_primary_and_parallel_updates_log_once_and_keep_legacy_batch_log_coalescing(tmp_path):
    store, job_id = running_store(tmp_path)
    store.progress(job_id, "compute_enriched", 60, "计算指标", stage_pct=30)
    store.progress(job_id, "compute_enriched", 60, "计算指标", task_id="compute_enriched",
                   task_status="running", task_progress=30, skip_log=True)
    store.progress(job_id, "compute_enriched", 61, "批次1/3", skip_log=True)
    store.progress(job_id, "compute_enriched", 62, "批次2/3", skip_log=True)
    assert [line["msg"] for line in store.get(job_id)["log"]] == ["计算指标", "批次2/3"]


def test_stage_durations_accumulate_and_task_metrics_survive_persistence(tmp_path, clock):
    store, job_id = running_store(tmp_path)
    clock(1)
    store.progress(job_id, "sync_daily", 20, "日线")
    store.progress(job_id, "fetch", 20, "开始下载", task_id="index", task_status="running")
    clock(2)
    store.progress(job_id, "compute_enriched", 50, "指标")
    store.progress(job_id, "fetch", 50, "下载完成", task_id="index", task_status="succeeded")
    clock(4)
    assert store.get(job_id)["stage_timings"] == {"init": 1, "sync_daily": 2, "compute_enriched": 4}
    store.succeed(job_id, {"rows": 42})
    restored = JobStore(store_dir=tmp_path / "jobs", recover=False).get(job_id)
    assert restored["outcome"] == "succeeded"
    assert restored["tasks"]["index"]["duration_s"] == 2
    assert restored["stage_timings"] == {"init": 1, "sync_daily": 2, "compute_enriched": 4}


@pytest.mark.parametrize("reason, outcome, waiting_state", [
    ("用户手动取消", "cancelled", "cancelled"),
    ("正式日线缺失", "failed", "skipped"),
    ("超时自动取消", "failed", "skipped"),
])
def test_job_failure_closes_unfinished_tasks_without_changing_completed_tasks(tmp_path, reason, outcome, waiting_state):
    store, job_id = running_store(tmp_path)
    for task_id, state in (("done", "succeeded"), ("waiting", "pending"), ("running", "running")):
        store.progress(job_id, "fetch", 10, task_id, task_id=task_id, task_status=state)
    store.terminate(job_id, reason)
    job = store.get(job_id)
    assert job["status"] == "failed"
    assert job["outcome"] == outcome
    assert job["tasks"]["done"]["status"] == "succeeded"
    assert job["tasks"]["waiting"]["status"] == waiting_state
    assert job["tasks"]["running"]["status"] == "cancelled"
    summary = store.list_recent()[0]
    assert summary["outcome"] == outcome
    assert summary["error"] == reason
    with pytest.raises(JobCancelledError):
        store.progress(job_id, "fetch", 50, "迟到回调", task_id="running")


def test_explicit_task_error_is_preserved_when_job_fails(tmp_path):
    store, job_id = running_store(tmp_path)
    store.progress(job_id, "fetch", 10, "下载失败", task_id="index", task_status="failed",
                   task_error="请求超时")
    store.fail(job_id, "指数取数失败")
    task = store.get(job_id)["tasks"]["index"]
    assert task["status"] == "failed"
    assert task["error"] == "请求超时"


def test_get_and_list_return_independent_snapshots(tmp_path):
    store, job_id = running_store(tmp_path)
    store.progress(job_id, "fetch", 10, "下载", task_id="index", task_status="running")
    first = store.get(job_id)
    first["tasks"]["index"]["status"] = "failed"
    first["log"].clear()
    recent = store.list_recent()[0]
    recent["tasks"].clear()
    actual = store.get(job_id)
    assert actual["tasks"]["index"]["status"] == "running"
    assert len(actual["log"]) == 1
