from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import secrets_store
from app.api import settings as settings_api
from app.config import settings


@pytest.fixture
def client(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "tickflow_api_key", "TFK1-backend-secret-END1")
    monkeypatch.setattr(settings, "ai_api_key", "AIK2-backend-secret-END2")
    monkeypatch.setattr(settings, "ai_provider", "openai_compat")
    monkeypatch.setattr(settings_api.tf_client, "current_mode", lambda: "api_key")
    monkeypatch.setattr(settings_api.tf_client, "current_endpoint", lambda: "https://api.tickflow.org")
    monkeypatch.setattr(settings_api, "tier_label", lambda: "pro")
    for name in ("probe_log", "missing_caps", "extras_caps"):
        monkeypatch.setattr(settings_api, name, lambda: [])
    for name in ("ai_model", "ai_user_agent", "ai_max_output_tokens"):
        monkeypatch.setattr(settings, name, getattr(settings, name))
    app = FastAPI()
    app.include_router(settings_api.router)
    with TestClient(app) as test_client:
        yield test_client


def test_settings_exposes_status_without_key_fragments(client):
    response = client.get("/api/settings")
    assert response.status_code == 200
    data = response.json()
    assert data["has_tickflow_key"] is True
    assert data["has_ai_key"] is True
    assert data["ai_configured"] is True
    # 保留旧响应字段供旧客户端读取，但不给出任何密钥字符。
    assert data["tickflow_api_key_masked"] == ""
    assert data["ai_api_key_masked"] == ""
    for fragment in ("TFK1", "END1", "AIK2", "END2"):
        assert fragment not in response.text


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/api/settings/tickflow-key", {"api_key": "replacement-secret"}),
        ("DELETE", "/api/settings/tickflow-key", None),
        ("POST", "/api/settings/ai", {"api_key": "replacement-secret", "model": "replacement-model"}),
        ("POST", "/api/settings/ai", {"api_key": "", "provider": "codex_cli"}),
        ("DELETE", "/api/settings/ai", None),
    ],
)
def test_browser_cannot_replace_or_clear_keys(client, method, path, payload):
    secrets_store.save({"ai_api_key": "legacy-secret", "ai_model": "legacy-model"})
    before = secrets_store.load()
    runtime_before = (settings.ai_api_key, settings.ai_provider, settings.ai_model)

    response = client.request(method, path, json=payload)

    assert response.status_code == 403
    assert "后端" in response.json()["detail"]
    assert "replacement-secret" not in response.text
    assert secrets_store.load() == before
    assert (settings.ai_api_key, settings.ai_provider, settings.ai_model) == runtime_before


def test_non_secret_ai_settings_keep_backend_key(client, monkeypatch):
    monkeypatch.setattr(settings, "ai_model", "previous-model")
    response = client.post(
        "/api/settings/ai", json={"model": "deepseek-flash", "max_output_tokens": 8192},
    )
    assert response.status_code == 200
    assert response.json()["ai_model"] == "deepseek-flash"
    assert response.json()["ai_configured"] is True
    assert secrets_store.get_ai_key() == "AIK2-backend-secret-END2"
    assert "ai_api_key" not in secrets_store.load()


@pytest.mark.parametrize(("status", "partial"), [(401, False), (401, True), (400, False)])
def test_upstream_ai_error_cannot_echo_backend_key(client, status, partial):
    import httpx
    import openai

    from app.services.ai_provider import _format_openai_error

    key = secrets_store.get_ai_key()
    echoed = f"{key[:4]}...{key[-4:]}" if partial else key
    response = httpx.Response(
        status, request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
    )
    error = openai.APIStatusError(
        "Request failed", response=response,
        body={"error": {"message": f"Incorrect API key provided: {echoed}"}},
    )
    message = _format_openai_error(error)
    assert key not in message
    assert "AIK2" not in message
    assert "END2" not in message
    assert str(status) in message
