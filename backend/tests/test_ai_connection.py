from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.strategy import router
from app.services import ai_provider


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client


def test_connection_probe_leaves_budget_for_visible_answer(client, monkeypatch):
    async def generate(messages, **kwargs):
        # 已复现：推理内容耗尽原来的 8 token 预算，正文为空。
        return "" if kwargs["max_tokens"] <= 8 else "OK"

    monkeypatch.setattr(ai_provider, "generate_ai_text", generate)
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "deepseek-flash")
    response = client.post("/api/strategies/ai/test")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["response"] == "OK"
    assert response.json()["model"] == "deepseek-flash"


def test_connection_probe_rejects_empty_answer(client, monkeypatch):
    async def generate(messages, **kwargs):
        return " \n "

    monkeypatch.setattr(ai_provider, "generate_ai_text", generate)
    response = client.post("/api/strategies/ai/test")
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "正文" in response.json()["error"]


def test_connection_probe_reports_provider_failure(client, monkeypatch):
    async def generate(messages, **kwargs):
        raise RuntimeError("AI 服务连接失败")

    monkeypatch.setattr(ai_provider, "generate_ai_text", generate)
    response = client.post("/api/strategies/ai/test")
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"] == "AI 服务连接失败"
