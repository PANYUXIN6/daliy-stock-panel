"""真实账号与点数台账下的 HTTP 扣费；仅替换付费模型和报告落盘。"""
from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.services.ai_credits import CreditError

ROUTES = [
    ("/api/custom/assistant/chat", {"messages": [{"role": "user", "content": "市场如何？"}]}, 3),
    ("/api/stock-analysis/analyze", {"symbol": "000001.SZ"}, 5),
    ("/api/financials/analyze", {"symbol": "000001.SZ"}, 5),
    ("/api/market-recap/analyze", {}, 5),
    ("/api/rps/rotation-analyze", {"kind": "concept"}, 5),
    ("/api/rps/rotation-analyze", {"kind": "industry"}, 5),
]


@pytest.fixture
def credit_client(tmp_path, monkeypatch):
    from app import secrets_store
    from app.api import ai_credits, financials, market_recap, rps, stock_analysis, strategy
    from app.config import settings
    from app.custom.assistant import routes as assistant_routes
    from app.services import ai_provider, preferences, shared_runtime, user_runtime
    from app.services.ai_credits import current_credit_task
    from app.services.auth import AuthStore
    from app.services.auth_access import install_auth_middleware

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(ai_provider, "ai_configured", lambda: True)
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "deepseek-flash")
    monkeypatch.setattr(ai_provider, "current_ai_provider", lambda: "openai_compat")
    monkeypatch.setattr(
        secrets_store, "get_ai_config",
        lambda key, default=None: "https://api.deepseek.com" if key == "ai_base_url" else default,
    )
    store = AuthStore(tmp_path)
    store.bootstrap("admin", "initial-secret")
    admin_token, _ = store.login("admin", "initial-secret", "test", "")
    store.create_user("guest", "temporary-secret")
    token, _ = store.login("guest", "temporary-secret", "test", "")
    guest_token, _ = store.change_password(token, "temporary-secret", "changed-secret")
    model = {"failure": None, "calls": 0, "features": []}

    async def events(*_args, **_kwargs):
        model["calls"] += 1
        task = current_credit_task()
        model["features"].append(task.feature if task is not None else None)
        if task is not None and model.get("metered"):
            call = task.reserve_call(1000, 256)
            task.settle_call(call.id, 2800, prompt_tokens=1000, completion_tokens=100)
        yield json.dumps({"type": "meta", "symbol": "000001.SZ", "as_of": "2026-10-20"})
        yield json.dumps({"type": "delta", "content": "模型生成的完整分析"}, ensure_ascii=False)
        if model["failure"] == "raise":
            raise RuntimeError("private-upstream-error")
        if model["failure"] == "event":
            yield json.dumps({"type": "error", "message": "模型生成失败"}, ensure_ascii=False)
        yield json.dumps({"type": "done"})

    async def connection_test(*_args, **_kwargs):
        model["calls"] += 1
        if model["failure"]:
            raise RuntimeError("connection unavailable")
        return "OK"

    monkeypatch.setattr(assistant_routes, "chat_stream", events)
    monkeypatch.setattr(stock_analysis, "analyze_stock_stream", events)
    monkeypatch.setattr(financials, "analyze_financials_stream", events)
    monkeypatch.setattr(market_recap, "recap_market_stream", events)
    monkeypatch.setattr(rps, "analyze_rotation_stream", events)
    monkeypatch.setattr(ai_provider, "generate_ai_text", connection_test)
    monkeypatch.setattr(user_runtime, "runtime_for", lambda _request: SimpleNamespace(strategy_engine=None))
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(preferences, "get_review_push_mode", lambda: "manual")
    for module in (stock_analysis.stock_reports, financials.ai_reports, market_recap.market_recap_reports):
        monkeypatch.setattr(module, "save_report", lambda _report: {"id": "saved-report"})

    app = FastAPI()
    app.state.auth_store = store
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.service_role = "collector"
    for router in (
        stock_analysis.router, financials.router, market_recap.router,
        rps.router, strategy.router, ai_credits.router,
    ):
        app.include_router(router)
    app.include_router(assistant_routes.build_router())

    @app.exception_handler(CreditError)
    async def credit_error(_request, exc):
        return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=exc.status)

    install_auth_middleware(app)
    with TestClient(app) as client:
        client.cookies.set("tf_session", guest_token)
        yield client, model, admin_token


def balance(client):
    from app.services.ai_credits import get_credit_store
    from app.services.user_context import UserContext

    store = client.app.state.auth_store
    token = client.cookies.get("tf_session")
    user = store.session(token)["user"]
    owner = UserContext(user["id"], user["role"], store.data_dir, session_token=token)
    return get_credit_store(store.data_dir).status(owner)


@pytest.mark.parametrize("path,payload,price", ROUTES)
def test_each_generation_entry_consumes_its_monthly_points(credit_client, path, payload, price):
    client, model, _admin_token = credit_client
    for _ in range(60 // price):
        response = client.post(path, json=payload)
        assert response.status_code == 200
        events = [json.loads(line) for line in response.text.splitlines()]
        assert events[-1]["type"] == "done"
        assert not any(event["type"] == "error" for event in events)
        assert balance(client)["reserved_points"] == 0
    calls_before = model["calls"]
    response = client.post(path, json=payload)
    assert response.status_code == 402
    assert model["calls"] == calls_before
    assert balance(client)["remaining_points"] == 0
    expected_feature = {
        "/api/custom/assistant/chat": "assistant", "/api/stock-analysis/analyze": "stock_analysis",
        "/api/financials/analyze": "financial_analysis", "/api/market-recap/analyze": "market_recap",
        "/api/rps/rotation-analyze": f"{payload.get('kind')}_rotation",
    }[path]
    assert set(model["features"]) == {expected_feature}


@pytest.mark.parametrize("path,payload,price", [ROUTES[0], ROUTES[1]])
def test_error_followed_by_done_refunds_points(credit_client, path, payload, price):
    client, model, _admin_token = credit_client
    model["failure"] = "event"
    response = client.post(path, json=payload)
    assert any(json.loads(line)["type"] == "error" for line in response.text.splitlines())
    assert balance(client)["used_points"] == 0
    assert balance(client)["reserved_points"] == 0
    model["failure"] = None
    for _ in range(60 // price):
        assert client.post(path, json=payload).status_code == 200
    assert client.post(path, json=payload).status_code == 402


def test_stream_exception_becomes_sanitized_error_and_refunds(credit_client):
    client, model, _admin_token = credit_client
    path, payload, price = ROUTES[1]
    model["failure"] = "raise"
    response = client.post(path, json=payload)
    events = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert events[-2]["code"] == "AI_GENERATION_FAILED"
    assert events[-1]["type"] == "done"
    assert "private-upstream-error" not in response.text
    model["failure"] = None
    for _ in range(60 // price):
        assert client.post(path, json=payload).status_code == 200


def test_request_id_cannot_replay_even_after_failure_refund(credit_client):
    client, model, _admin_token = credit_client
    path, payload, _price = ROUTES[0]
    headers = {"X-AI-Request-ID": uuid.uuid4().hex}
    model["failure"] = "event"
    assert client.post(path, json=payload, headers=headers).status_code == 200
    calls_before = model["calls"]
    model["failure"] = None
    assert client.post(path, json=payload, headers=headers).status_code == 409
    assert model["calls"] == calls_before


def test_admin_is_unlimited_and_anonymous_is_rejected(credit_client):
    client, model, admin_token = credit_client
    path, payload, _price = ROUTES[0]
    client.cookies.clear()
    assert client.post(path, json=payload).status_code == 401
    assert model["calls"] == 0
    client.cookies.set("tf_session", admin_token)
    for _ in range(21):
        assert client.post(path, json=payload).status_code == 200
    assert balance(client)["unlimited"]
    assert balance(client)["used_points"] == 0


def test_pro_uses_same_price_with_larger_monthly_allowance(credit_client):
    client, _model, _admin_token = credit_client
    store = client.app.state.auth_store
    store.create_user("pro-user", "temporary-secret", "pro", "2099-12-31")
    token, _ = store.login("pro-user", "temporary-secret", "test", "")
    token, _ = store.change_password(token, "temporary-secret", "changed-secret")
    client.cookies.set("tf_session", token)
    path, payload, price = ROUTES[0]
    assert client.post(path, json=payload).status_code == 200
    status = balance(client)
    assert status["monthly_points"] == 4000
    assert status["remaining_points"] == 4000 - price


def test_invalid_policy_version_is_rejected_before_model_call(credit_client):
    client, model, _admin_token = credit_client
    path, payload, _price = ROUTES[0]
    response = client.post(path, json=payload, headers={"X-AI-Policy-Version": "invalid"})
    assert response.status_code == 400
    assert model["calls"] == 0


def test_missing_configuration_is_unavailable_in_both_status_and_generation(credit_client, monkeypatch):
    from app.services import ai_provider

    client, model, _admin_token = credit_client
    monkeypatch.setattr(ai_provider, "ai_configured", lambda: False)
    status = client.get("/api/ai/credits").json()
    access = status["features"]["assistant"]
    assert not access["available"]
    path, payload, _price = ROUTES[0]
    response = client.post(path, json=payload)
    assert response.status_code == 503
    assert response.json()["detail"] == access["reason"]
    assert status["remaining_points"] == 60
    assert model["calls"] == 0


def test_unpriced_model_does_not_disable_generation(credit_client, monkeypatch):
    from app.services import ai_provider

    client, model, _admin_token = credit_client
    monkeypatch.setattr(ai_provider, "current_ai_model", lambda: "unpriced-model")
    status = client.get("/api/ai/credits").json()
    assert status["features"]["assistant"]["available"]
    assert status["cost_limits_enabled"] is False
    assert status["cost_limit_cny"] is None
    path, payload, _price = ROUTES[0]
    assert client.post(path, json=payload).status_code == 200
    assert model["calls"] == 1
    assert balance(client)["remaining_points"] == 57


def test_connection_test_uses_same_assistant_balance(credit_client):
    client, model, _admin_token = credit_client
    for _ in range(20):
        response = client.post("/api/strategies/ai/test")
        assert response.status_code == 200
        assert response.json()["ok"]
    calls_before = model["calls"]
    path, payload, _price = ROUTES[0]
    assert client.post(path, json=payload).status_code == 402
    assert client.post("/api/strategies/ai/test").status_code == 402
    assert model["calls"] == calls_before


def test_credit_api_is_private_and_policy_updates_are_persistent_and_versioned(credit_client):
    from app.services.ai_credits import CreditStore

    client, model, admin_token = credit_client
    guest_token = client.cookies.get("tf_session")
    path, payload, _price = ROUTES[0]
    assert client.post(path, json=payload).status_code == 200
    guest_status = client.get("/api/ai/credits").json()
    assert guest_status["used_points"] == 3
    assert guest_status["monthly_points"] == 60
    assert client.get("/api/ai/credits/policy").status_code == 403

    client.cookies.set("tf_session", admin_token)
    policy = client.get("/api/ai/credits/policy").json()
    assert client.get("/api/ai/credits").json()["used_points"] == 0
    client.cookies.set("tf_session", guest_token)
    assert client.put("/api/ai/credits/policy", json=policy).status_code == 403

    client.cookies.set("tf_session", admin_token)
    old_version = policy["policy_version"]
    policy["policy"]["monthly_points"]["guest"] = 80
    policy["policy"]["feature_points"]["assistant"] = 4
    changed = client.put("/api/ai/credits/policy", json=policy)
    assert changed.status_code == 200
    assert changed.json()["policy_version"] == old_version + 1
    store = client.app.state.auth_store
    assert CreditStore(store.data_dir).get_policy() == changed.json()

    client.cookies.set("tf_session", guest_token)
    fresh = client.get("/api/ai/credits", params={"user_id": store.session(admin_token)["user"]["id"]}).json()
    assert fresh["policy_version"] == old_version + 1
    assert fresh["monthly_points"] == 80
    assert fresh["features"]["assistant"]["points"] == 4
    assert fresh["used_points"] == 3
    calls_before = model["calls"]
    stale = client.post(path, json=payload, headers={"X-AI-Policy-Version": str(old_version)})
    assert stale.status_code == 409
    assert stale.json()["code"] == "AI_POLICY_CHANGED"
    assert model["calls"] == calls_before
    assert client.post(path, json=payload, headers={"X-AI-Policy-Version": str(old_version + 1)}).status_code == 200
    assert client.get("/api/ai/credits").json()["used_points"] == 7

    store.create_user("another-guest", "temporary-secret")
    token, _ = store.login("another-guest", "temporary-secret", "test", "")
    token, _ = store.change_password(token, "temporary-secret", "changed-secret")
    client.cookies.set("tf_session", token)
    other = client.get("/api/ai/credits").json()
    assert other["used_points"] == 0
    assert other["remaining_points"] == 80


def test_web_proxy_bills_only_collector_and_preserves_admin_restrictions(credit_client):
    from app.services.auth_access import install_auth_middleware
    from app.services.collector_proxy import proxy_middleware

    collector, model, _admin_token = credit_client
    model["metered"] = True
    forwarded = []

    async def record(request):
        forwarded.append((request.method, request.url.path))

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=collector.app), event_hooks={"request": [record]},
        ) as upstream:
            app.state.collector_client = upstream
            yield

    web = FastAPI(lifespan=lifespan)
    web.state.service_role = "web"
    web.state.auth_store = collector.app.state.auth_store
    web.state.collector_url = "http://collector"
    web.middleware("http")(proxy_middleware)

    @web.get("/api/ai/credits")
    def unexpected_local_status():
        pytest.fail("AI 额度应由实际执行的 collector 回答")

    install_auth_middleware(web)
    path, payload, _price = ROUTES[0]
    request_headers = {"X-AI-Request-ID": uuid.uuid4().hex}
    with TestClient(web) as client:
        client.cookies.set("tf_session", collector.cookies.get("tf_session"))
        assert client.post(path, json=payload, headers=request_headers).status_code == 200
        status = client.get("/api/ai/credits")
        assert status.status_code == 200
        assert status.json()["used_points"] == 3
        assert status.json()["reserved_points"] == 0
        assert status.json()["cost_used_cny"] == "0.002800"
        assert status.json() == collector.get("/api/ai/credits").json()
        assert forwarded == [("POST", path), ("GET", "/api/ai/credits")]
        assert model["calls"] == 1
        assert collector.post(path, json=payload, headers=request_headers).status_code == 409
        assert collector.get("/api/ai/credits").json()["used_points"] == 3
        assert model["calls"] == 1
        assert client.post("/api/backtest/strategy/run", json={}).status_code == 403
        assert forwarded == [("POST", path), ("GET", "/api/ai/credits")]


@pytest.mark.parametrize("started,expected", [(True, "cancelled"), (False, "failed")])
async def test_cancelling_generation_keeps_points_only_after_model_started(monkeypatch, started, expected):
    from app.services import ai_credit_stream

    outcomes = []
    task = SimpleNamespace(started=started, finish=outcomes.append)
    monkeypatch.setattr(ai_credit_stream, "credit_task_scope", lambda _task: nullcontext())

    async def events():
        yield json.dumps({"type": "delta", "content": "已收到的正文"})

    stream = ai_credit_stream.charged_stream(task, events())
    await anext(stream)
    await stream.aclose()
    assert outcomes == [expected]


async def test_response_cancelled_before_first_iteration_releases_reservation():
    from app.services.ai_credit_stream import CreditStreamingResponse

    outcomes = []
    entered = []
    task = SimpleNamespace(started=False, finish=outcomes.append)

    async def events():
        entered.append(True)
        yield json.dumps({"type": "delta", "content": "不会执行"})

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            raise asyncio.CancelledError

    response = CreditStreamingResponse(events(), credit_task=task)
    with pytest.raises(asyncio.CancelledError):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert not entered
    assert outcomes == ["failed"]
