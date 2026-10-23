import hashlib
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.market_recap import router
from app.config import settings
from app.services import market_recap, market_recap_reports, preferences, recap_prompt
from app.services.auth import AuthStore
from app.services.auth_access import install_auth_middleware
from app.services.user_context import UserContext, user_scope


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    preferences._invalidate_cache()
    store = AuthStore(tmp_path)
    admin = store.bootstrap("admin", "initial-password")
    users = {"admin": admin}
    for role in ("pro", "guest"):
        users[role] = store.create_user(role, "initial-password", role=role,
                                        pro_expires_on="2099-12-31" if role == "pro" else None)
    owners = {}
    for role, user in users.items():
        token, _ = store.login(role, "initial-password", role, "")
        if role != "admin":
            token, _ = store.change_password(token, "initial-password", "changed-password")
        owners[role] = UserContext(user["id"], role, tmp_path, session_token=token)
    app = FastAPI()
    app.state.auth_store = store
    app.include_router(router)
    install_auth_middleware(app)
    return store, owners, app


def test_prompt_http_permissions_isolation_limits_and_restore(accounts):
    _store, owners, app = accounts
    with TestClient(app) as client:
        for role in ("pro", "admin"):
            client.cookies.set("tf_session", owners[role].session_token)
            assert client.get("/api/market-recap/prompt").json()["custom"] == ""
            response = client.put("/api/market-recap/prompt", json={"prompt": f"  {role} 的模板  "})
            assert response.status_code == 200
            assert response.json()["custom"] == f"{role} 的模板"
        client.cookies.set("tf_session", owners["pro"].session_token)
        assert client.get("/api/market-recap/prompt").json()["custom"] == "pro 的模板"
        assert client.put("/api/market-recap/prompt", json={"prompt": "字" * 32_001}).status_code == 400
        assert client.get("/api/market-recap/prompt").json()["custom"] == "pro 的模板"
        assert client.put("/api/market-recap/prompt", json={"prompt": None}).status_code == 422
        restored = client.put("/api/market-recap/prompt", json={"prompt": " \n "}).json()
        assert restored["custom"] == "" and restored["customized"] is False
        assert restored["default"] == market_recap._SYSTEM_PROMPT
        client.cookies.set("tf_session", owners["guest"].session_token)
        assert client.get("/api/market-recap/prompt").status_code == 403
        assert client.put("/api/market-recap/prompt", json={"prompt": "越权"}).status_code == 403
    # No template is ever stored in shared system preferences.
    shared = preferences._path()
    assert not shared.exists() or "market_recap_system_prompt" not in json.loads(shared.read_text())


def test_downgraded_pro_uses_default_even_with_captured_context(accounts):
    store, owners, app = accounts
    owner = owners["pro"]
    with user_scope(owner):
        recap_prompt.save("我的模板")
        assert recap_prompt.snapshot()["source"] == "custom"
        with store._connection(True) as db:
            db.execute("UPDATE users SET role='guest' WHERE id=?", (owner.id,))
        assert recap_prompt.snapshot()["text"] == market_recap._SYSTEM_PROMPT
    with TestClient(app) as client:
        client.cookies.set("tf_session", owner.session_token)
        assert client.get("/api/market-recap/prompt").status_code == 403
    with user_scope(owners["guest"]):
        preferences.save({"market_recap_system_prompt": "旧模板"})
        assert recap_prompt.snapshot()["source"] == "default"


def test_expired_pro_cannot_keep_using_or_changing_template(accounts):
    store, owners, app = accounts
    owner = owners["pro"]
    with user_scope(owner):
        recap_prompt.save("到期前模板")
        with store._connection(True) as db:
            db.execute("UPDATE users SET pro_expires_on='2000-01-01' WHERE id=?", (owner.id,))
        assert recap_prompt.snapshot()["text"] == market_recap._SYSTEM_PROMPT
    with TestClient(app) as client:
        client.cookies.set("tf_session", owner.session_token)
        assert client.get("/api/market-recap/prompt").status_code == 403
        assert client.put("/api/market-recap/prompt", json={"prompt": "过期更新"}).status_code == 403
    # Old personal settings remain available if the account is renewed.
    with user_scope(owner):
        assert preferences.load()["market_recap_system_prompt"] == "到期前模板"


def test_generic_preferences_cannot_override_prompt(accounts):
    from app.api.settings import router as settings_router
    _store, owners, app = accounts
    app.include_router(settings_router)
    with TestClient(app) as client:
        for role in ("guest", "pro"):
            client.cookies.set("tf_session", owners[role].session_token)
            response = client.put("/api/settings/preferences/realtime-monitor", json={
                "market_recap_system_prompt": "绕过专用接口" * 32_001,
            })
            assert response.status_code == 200
            with user_scope(owners[role]):
                assert "market_recap_system_prompt" not in preferences.load()


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr(market_recap, "build_market_overview", lambda *args: {"as_of": "2026-10-22"})
    monkeypatch.setattr("app.services.dragon_tiger.build_recap_context", lambda *args: "")
    monkeypatch.setattr("app.services.auction_benchmark.build_recap_context", lambda *args: "")
    captured = []
    async def stream(messages, **kwargs):
        captured.append(messages)
        yield "复盘正文"
    monkeypatch.setattr("app.services.ai_provider.stream_ai_text", stream)
    return captured


async def test_manual_generation_uses_one_snapshot_and_archives_it(accounts, model, monkeypatch):
    from app.services.analysis_archive import archive_analysis
    monkeypatch.setattr("app.services.shared_runtime.publish_data_update", lambda *args, **kwargs: None)
    _store, owners, _app = accounts
    owner = owners["pro"]
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=owner.data_dir))
    with user_scope(owner):
        recap_prompt.save("生成时模板")
        async def changing_stream():
            async for event in market_recap.recap_market_stream(repo):
                if json.loads(event)["type"] == "meta":
                    recap_prompt.save("下一次模板")
                yield event
        events = [json.loads(e) async for e in archive_analysis(
            changing_stream(), market_recap_reports.save_report, owner.data_dir, "review", {},
        )]
        report = market_recap_reports.list_reports()[0]
    assert events[-1]["archived"]
    assert model[0][0]["content"] == "生成时模板"
    assert report["system_prompt"] == {
        "text": "生成时模板", "source": "custom",
        "sha256": hashlib.sha256("生成时模板".encode()).hexdigest(),
    }


async def test_schedule_uses_admin_template_and_keeps_pro_schedule_forbidden(accounts, model, monkeypatch):
    from app.jobs import daily_pipeline
    store, owners, _app = accounts
    monkeypatch.setattr("app.services.trading_day.is_trading_day", lambda: True)
    monkeypatch.setattr("app.secrets_store.get_ai_key", lambda: "test-only")
    monkeypatch.setattr("app.services.shared_runtime.publish_data_update", lambda *args, **kwargs: None)
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: None)
    with user_scope(owners["pro"]):
        recap_prompt.save("Pro 模板")
        with pytest.raises(PermissionError):
            preferences.set_review_schedule(True, 16, 30)
    with user_scope(owners["admin"]):
        recap_prompt.save("管理员模板")
        preferences.set_review_schedule(True, 16, 30)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=owners["admin"].data_dir))
    await daily_pipeline._run_scheduled_review(repo)
    assert model[0][0]["content"] == "管理员模板"
    with user_scope(owners["admin"]):
        assert market_recap_reports.list_reports()[0]["system_prompt"]["text"] == "管理员模板"
    with user_scope(owners["pro"]):
        assert market_recap_reports.list_reports() == []
    with store._connection(True) as db:
        db.execute("UPDATE users SET is_active=0 WHERE id=?", (owners["admin"].id,))
    await daily_pipeline._run_scheduled_review(repo)
    assert len(model) == 1
