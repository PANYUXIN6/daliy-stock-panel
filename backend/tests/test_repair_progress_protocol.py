"""The manual repair entry point must forward the pipeline's concurrent task metadata."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.api import kline
from app.services import heavy_job_limiter, pipeline_jobs, repair_daily
from app.services.pipeline_prefetch import PipelineProgress
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


async def test_repair_entry_forwards_task_metadata_and_persists_outcome(tmp_path, monkeypatch):
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(heavy_job_limiter, "shared_heavy_job_limiter",
                        heavy_job_limiter.HeavyJobLimiter(capacity=2))
    monkeypatch.setattr(kline, "_tasks", set())

    def compute(repo, capset, start_date, *, on_progress):
        progress = PipelineProgress(on_progress)
        progress("compute_enriched", 60, "计算技术指标", stage_pct=25)
        progress.task("prefetch_index", "预取指数", "running", 10, "请求指数数据")
        progress.task("prefetch_index", "预取指数", "succeeded", 100, "指数就绪")
        progress("done", 100, "修复完成")
        return {"repaired_from": start_date.isoformat()}

    monkeypatch.setattr(repair_daily, "run_repair_daily", compute)
    state = SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
        capabilities=CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits()}),
        quote_service=None,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state),
                              json=AsyncMock(return_value={"start_date": "2026-01-01"}))
    response = await kline.repair_daily(request)
    job_id = response["job_id"]
    tasks = list(kline._tasks)
    assert tasks
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    job = store.get(job_id)
    assert job["status"] == "succeeded", job["error"]
    assert job["outcome"] == "succeeded"
    assert job["tasks"]["compute_enriched"]["status"] == "succeeded"
    assert job["tasks"]["prefetch_index"]["status"] == "succeeded"
    assert job["tasks"]["prefetch_index"]["label"] == "预取指数"
    assert job["tasks"]["prefetch_index"]["message"] == "指数就绪"
    assert pipeline_jobs._run_slot_owner is None
