"""盘后自动管道与基础维护调度共存；盘口不另设并发采集定时器。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from app.api import settings as settings_api
from app.jobs import daily_pipeline
from app.services import preferences
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def test_scheduler_registers_beijing_postmarket_and_keeps_maintenance(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    capset = CapabilitySet({Cap.DEPTH5_BATCH: CapabilityLimits()})

    async def run():
        scheduler = daily_pipeline.start_scheduler(repo)
        try:
            ids = {job.id for job in scheduler.get_jobs()}
            assert ids == {"trading_calendar", "pre_market_instruments", "reprobe_capabilities",
                           "daily_pipeline", "post_market_retry"}
            postmarket = scheduler.get_job("daily_pipeline")
            assert str(postmarket.trigger).endswith("hour='16', minute='0']")
            assert str(postmarket.trigger.timezone) == "Asia/Shanghai"
            assert postmarket.max_instances == 1
            assert postmarket.coalesce is True
            instruments = scheduler.get_job("pre_market_instruments")
            assert str(instruments.trigger).endswith("hour='9', minute='10']")

            # 时间偏好立即更新既有 job；盘口时间接口不另起写入任务。
            app = FastAPI()
            app.include_router(settings_api.router)
            app.state.scheduler = scheduler
            app.state.capabilities = capset
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for path, hour, minute in [
                    ("pipeline-schedule", 17, 5), ("depth-finalize-time", 15, 5),
                ]:
                    response = await client.put(f"/api/settings/preferences/{path}", json={"hour": hour, "minute": minute})
                    assert response.status_code == 200, response.text
                    assert response.json() == {"hour": hour, "minute": minute}
            assert {job.id for job in scheduler.get_jobs()} == ids
            assert str(scheduler.get_job("daily_pipeline").trigger).endswith("hour='17', minute='5']")
        finally:
            scheduler.shutdown(wait=False)
            await asyncio.sleep(0)

    try:
        asyncio.run(run())
    finally:
        preferences._invalidate_cache()
