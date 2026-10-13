"""财务浏览与 AI 分析依赖已采集数据，只有采集命令检查数据源能力。"""
import json
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api import financials
from app.services import ai_reports, financial_sync, pipeline_jobs
from app.services.collector_proxy import proxy_middleware
from app.tickflow.capabilities import CapabilityDenied, CapabilitySet


@pytest.fixture
def local_financials(tmp_path, monkeypatch, authenticated_actor):
    app = FastAPI()
    app.include_router(financials.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.capabilities = CapabilitySet()
    app.state.financial_scheduler = SimpleNamespace(last_sync={}, run_now=lambda *args: pytest.fail("浏览不采集"))
    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: False)
    monkeypatch.setattr(pipeline_jobs, "job_store", pipeline_jobs.JobStore(store_dir=tmp_path / "jobs"))

    @app.exception_handler(CapabilityDenied)
    async def denied(request, exc):
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    return TestClient(app), tmp_path


@pytest.mark.parametrize("table,path", [
    ("metrics", "metrics"), ("income", "income"), ("balance_sheet", "balance-sheet"),
    ("cash_flow", "cash-flow"), ("shares", "shares"),
])
def test_local_financial_table_reads_without_provider(local_financials, table, path):
    client, root = local_financials
    output = root / "financials" / table / "part.parquet"
    output.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"], "value": [12.5, 8.0]}).write_parquet(output)

    response = client.get(f"/api/financials/{path}", params={"symbol": "600000.SH"})
    assert response.status_code == 200
    assert response.json()["data"] == [{"symbol": "600000.SH", "value": 12.5}]
    status = client.get("/api/financials/status").json()
    assert status["available"] is True
    assert status["tables"][table] == {"rows": 2, "symbols": 2}
    execution = client.get("/api/financials/execution-status").json()
    assert execution["syncing"] is False and execution["active_job_id"] is None
    assert client.post(f"/api/financials/sync/{table}").status_code == 403


def test_empty_financials_remain_empty_without_provider(local_financials):
    client, _ = local_financials
    assert client.get("/api/financials/metrics").json() == {"data": []}
    status = client.get("/api/financials/status").json()
    assert status["available"] is False
    assert set(status["tables"]) == set(financial_sync.FINANCIAL_TABLES)
    assert all(table["rows"] == 0 for table in status["tables"].values())
    response = client.post("/api/financials/analyze", json={"symbol": "600000.SH"})
    assert response.status_code == 200
    event = json.loads(response.text.strip())
    assert event["type"] == "error" and "暂无任何财务数据" in event["message"]


def test_financial_ai_uses_local_data_without_provider(local_financials, monkeypatch):
    from app.services import ai_provider

    client, root = local_financials
    output = root / "financials" / "metrics" / "part.parquet"
    output.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"], "roe": [12.5]}).write_parquet(output)

    async def stream(messages, **kwargs):
        assert "12.5" in messages[1]["content"]
        yield "财务分析结果"

    monkeypatch.setattr(ai_provider, "stream_ai_text", stream)
    monkeypatch.setattr(ai_reports, "save_report", lambda payload: {"id": "local-analysis", **payload})
    response = client.post("/api/financials/analyze", json={"symbol": "600000.SH"})
    assert response.status_code == 200
    events = [json.loads(line) for line in response.text.splitlines()]
    assert any(event.get("content") == "财务分析结果" for event in events)
    assert events[-1]["type"] == "done"


def test_saved_financial_reports_survive_provider_removal(local_financials, monkeypatch):
    client, _ = local_financials
    monkeypatch.setattr(ai_reports, "list_reports", lambda: [{"id": "saved", "content": "已有分析"}])
    monkeypatch.setattr(ai_reports, "save_report", lambda payload: {"id": "new", **payload})
    monkeypatch.setattr(ai_reports, "delete_report", lambda report_id: report_id == "saved")
    assert client.get("/api/financials/reports").json()["reports"][0]["id"] == "saved"
    assert client.post("/api/financials/reports", json={"symbol": "600000.SH", "content": "已有分析"}).json()["ok"]
    assert client.delete("/api/financials/reports/saved").json() == {"ok": True}


def test_web_financial_profile_and_reports_survive_collector_offline(tmp_path, monkeypatch):
    import httpx

    async def offline(request):
        raise httpx.ConnectError("collector unavailable", request=request)

    app = FastAPI()
    app.include_router(financials.router)
    app.middleware("http")(proxy_middleware)
    app.state.service_role = "web"
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.collector_url = "http://collector"
    app.state.collector_client = httpx.AsyncClient(transport=httpx.MockTransport(offline))
    output = tmp_path / "financials" / "metrics" / "part.parquet"
    output.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"], "roe": [12.5]}).write_parquet(output)
    monkeypatch.setattr(ai_reports, "list_reports", lambda: [{"id": "saved", "content": "已保存报告"}])
    monkeypatch.setattr(financials, "_active_financial_job", lambda: pytest.fail("网页不得回答采集进程任务状态"))

    with TestClient(app) as client:
        profile = client.get("/api/financials/status")
        assert profile.status_code == 200
        assert profile.json()["available"] is True
        assert profile.json()["tables"]["metrics"] == {"rows": 1, "symbols": 1}
        assert client.get("/api/financials/metrics").json()["data"] == [{"symbol": "600000.SH", "roe": 12.5}]
        assert client.get("/api/financials/reports").json()["reports"][0]["id"] == "saved"
        assert client.get("/api/financials/execution-status").status_code == 503
