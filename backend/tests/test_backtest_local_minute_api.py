"""分钟成交研究只依赖本地数据覆盖, 不依赖当前供应商采集能力。"""

from datetime import date
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import backtest as api
from app.backtest import worker
from app.tickflow.capabilities import CapabilitySet

pytestmark = pytest.mark.usefixtures("authenticated_actor")


@pytest.mark.parametrize("earliest", [date(2026, 10, 8), date(2026, 10, 9)])
def test_minute_fill_research_uses_local_coverage_without_collection_capability(
    monkeypatch, tmp_path, earliest,
):
    monkeypatch.setattr(api.settings, "data_dir", tmp_path)
    monkeypatch.setattr(api.settings, "backtest_range_guard", False)
    monkeypatch.setattr(api, "_running_jobs", {})
    tasks = []

    def run_local_worker(task, on_progress, cancel_event):
        tasks.append(task)
        return {"trades": [{"symbol": "600000.SH", "entry_price": 10.5}]}

    monkeypatch.setattr(worker, "run_worker_task", run_local_worker)
    app = FastAPI()
    app.include_router(api.router)
    app.state.user_runtimes = SimpleNamespace(get=lambda user: SimpleNamespace(strategy_engine=None))
    app.state.capabilities = CapabilitySet()
    app.state.repo = SimpleNamespace(earliest_minute_date=lambda: earliest)
    response = TestClient(app).get("/api/backtest/strategy/stream", params={
        "strategy_id": "ma_cross", "symbols": "600000.SH",
        "start": "2026-10-08", "end": "2026-10-09", "minute_fill": "true",
    })

    assert response.status_code == 200
    if earliest == date(2026, 10, 8):
        assert "event: done" in response.text
        assert '"entry_price": 10.5' in response.text
        assert len(tasks) == 1
        assert tasks[0]["kind"] == "backtest"
        assert tasks[0]["config"]["minute_fill"] is True
        assert tasks[0]["data_dir"] == str(tmp_path)
    else:
        assert "event: error" in response.text
        assert "本地分钟K历史最早到 2026-10-09" in response.text
        assert "无法覆盖回测起始日 2026-10-08" in response.text
        assert "拉取" not in response.text
        assert "Pro+" not in response.text
        assert tasks == []
