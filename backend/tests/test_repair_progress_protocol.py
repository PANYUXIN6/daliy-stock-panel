"""The manual repair entry point must forward the pipeline's concurrent task metadata."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.api import kline, pipeline
from app.services import heavy_job_limiter, pipeline_jobs, repair_daily, shared_runtime
from app.services.pipeline_prefetch import PipelineProgress
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


@pytest.mark.parametrize("failed", [False, True])
async def test_repair_entry_forwards_progress_and_refreshes_partial_writes(tmp_path, monkeypatch, failed):
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(heavy_job_limiter, "shared_heavy_job_limiter",
                        heavy_job_limiter.HeavyJobLimiter(capacity=2))
    monkeypatch.setattr(pipeline, "_tasks", set())
    publish = Mock()
    monkeypatch.setattr(shared_runtime, "publish_data_update", publish)

    def compute(repo, capset, start_date, *, on_progress):
        progress = PipelineProgress(on_progress)
        progress("compute_enriched", 60, "计算技术指标", stage_pct=25)
        progress.task("prefetch_index", "预取指数", "running", 10, "请求指数数据")
        progress.task("prefetch_index", "预取指数", "succeeded", 100, "指数就绪")
        if failed:
            raise RuntimeError("行业分类请求失败")
        progress("done", 100, "修复完成")
        return {"repaired_from": start_date.isoformat()}

    monkeypatch.setattr(repair_daily, "run_repair_daily", compute)
    state = SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=Mock()),
        capabilities=CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits()}),
        quote_service=None,
    )
    request = SimpleNamespace(app=SimpleNamespace(state=state),
                              json=AsyncMock(return_value={"start_date": "2026-01-01"}))
    response = await kline.repair_daily(request)
    job_id = response["job_id"]
    tasks = list(pipeline._tasks)
    assert tasks
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    job = store.get(job_id)
    assert job["status"] == ("failed" if failed else "succeeded"), job["error"]
    assert job["outcome"] == ("failed" if failed else "succeeded")
    if failed:
        assert job["error"] == "行业分类请求失败"
    else:
        assert job["tasks"]["compute_enriched"]["status"] == "succeeded"
    assert job["tasks"]["prefetch_index"]["status"] == "succeeded"
    assert job["tasks"]["prefetch_index"]["label"] == "预取指数"
    assert job["tasks"]["prefetch_index"]["message"] == "指数就绪"
    assert pipeline_jobs._run_slot_owner is None
    state.repo.refresh_cache.assert_called_once()
    publish.assert_called_once_with(tmp_path, ["daily", "enriched", "regime", "mainline", "ext_data"])
