"""手动采集反馈本轮实际结果，失败不显示成功。"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import intraday
from app.services.quote_service import QuoteService


@pytest.mark.parametrize("success", [True, False])
def test_manual_refresh_keeps_final_boundary_and_reports_result(monkeypatch, success):
    service = QuoteService()
    calls = []
    monkeypatch.setattr(service, "_market_phase", lambda: "close_final")
    monkeypatch.setattr(service, "_final_boundary_ms", lambda phase: 1234)
    monkeypatch.setattr(service, "_fetch_quotes", lambda **kwargs: calls.append(kwargs) or success)
    monkeypatch.setattr(service, "status", lambda: {
        "symbol_count": 3, "running": False, "paused": False,
    })
    app = FastAPI()
    app.include_router(intraday.router)
    app.state.quote_service = service

    response = TestClient(app).post("/api/intraday/refresh")
    assert calls == [{"final": True, "final_boundary_ms": 1234}]
    if success:
        assert response.status_code == 200
        assert response.json() == {
            "symbol_count": 3, "running": False, "paused": False, "fetch_succeeded": True,
        }
    else:
        assert response.status_code == 502
        assert "未取得有效行情" in response.json()["detail"]


def test_manual_refresh_without_collector_service_is_unavailable():
    app = FastAPI()
    app.include_router(intraday.router)
    response = TestClient(app).post("/api/intraday/refresh")
    assert response.status_code == 503


@pytest.mark.parametrize("queued", [False, True], ids=["already_paused", "queued_before_pause"])
def test_manual_refresh_during_pause_never_fetches_or_writes(monkeypatch, queued):
    service = QuoteService()
    service._fetched_at = 1234
    fetches = []
    entered = threading.Event()

    def market_phase():
        entered.set()
        return "afternoon"

    monkeypatch.setattr(service, "_market_phase", market_phase)
    monkeypatch.setattr(service, "status", lambda: {
        "paused": service.is_paused(), "last_fetch_ms": service._fetched_at,
    })
    # This boundary performs both provider access and local daily/enriched writes.
    monkeypatch.setattr(service, "_fetch_full_market_quotes", lambda **kwargs: fetches.append(kwargs))
    app = FastAPI()
    app.include_router(intraday.router)
    app.state.quote_service = service
    client = TestClient(app)

    if queued:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with service._fetch_lock:
                pending = executor.submit(client.post, "/api/intraday/refresh")
                assert entered.wait(5), "manual request did not reach the existing fetch lock"
                service.pause()
            response = pending.result(timeout=5)
    else:
        service.pause()
        response = client.post("/api/intraday/refresh")

    assert response.status_code == 409
    assert "暂停" in response.json()["detail"]
    assert fetches == []
    assert service._fetched_at == 1234
