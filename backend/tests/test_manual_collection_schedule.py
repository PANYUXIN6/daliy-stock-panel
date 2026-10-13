"""盘后采集只由手动任务触发, 基础维护调度仍保留。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from app.api import settings as settings_api
from app.jobs import daily_pipeline
from app.services import preferences
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def test_scheduler_keeps_maintenance_without_postmarket_collection(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    capset = CapabilitySet({Cap.DEPTH5_BATCH: CapabilityLimits()})

    async def run():
        scheduler = daily_pipeline.start_scheduler(repo)
        try:
            ids = {job.id for job in scheduler.get_jobs()}
            assert ids == {"trading_calendar", "pre_market_instruments", "reprobe_capabilities"}
            instruments = scheduler.get_job("pre_market_instruments")
            assert str(instruments.trigger).endswith("hour='9', minute='10']")

            # 保留旧时间偏好接口的调用能力, 保存不会创建盘后自动采集任务。
            app = FastAPI()
            app.include_router(settings_api.router)
            app.state.scheduler = scheduler
            app.state.capabilities = capset
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for path, hour, minute in [
                    ("pipeline-schedule", 16, 0), ("depth-finalize-time", 15, 5),
                ]:
                    response = await client.put(f"/api/settings/preferences/{path}", json={"hour": hour, "minute": minute})
                    assert response.status_code == 200, response.text
                    assert response.json() == {"hour": hour, "minute": minute}
            assert {job.id for job in scheduler.get_jobs()} == ids
        finally:
            scheduler.shutdown(wait=False)
            await asyncio.sleep(0)

    try:
        asyncio.run(run())
    finally:
        preferences._invalidate_cache()
