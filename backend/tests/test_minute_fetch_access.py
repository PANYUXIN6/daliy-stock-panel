"""详情分钟补采的登录、来源与维护权限边界。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.auth import AuthStore
from app.services.auth_access import install_auth_middleware


@pytest.mark.parametrize("role", [None, "guest", "pro", "admin"])
def test_minute_fetch_is_available_to_signed_in_users_without_opening_maintenance(tmp_path, role):
    store = AuthStore(tmp_path)
    store.bootstrap("admin", "initial-secret")
    token = None
    if role:
        if role != "admin":
            store.create_user(role, "initial-secret", role,
                              "2099-12-31" if role == "pro" else None)
        token, _ = store.login(role, "initial-secret", "test", "")
        if role != "admin":
            token, _ = store.change_password(token, "initial-secret", "changed-secret")

    app = FastAPI()
    app.state.auth_store = store
    reached = []

    async def allowed():
        reached.append(True)
        return {"ok": True}

    for path, method in [
        ("/api/kline/minute/fetch", "POST"),
        ("/api/kline/minute/fetch/job1", "GET"),
        ("/api/kline/sync_minute_single", "POST"),
        ("/api/kline/sync_minute", "POST"),
        ("/api/pipeline/jobs/job1", "GET"),
        ("/api/pipeline/jobs/job1/cancel", "POST"),
    ]:
        app.add_api_route(path, allowed, methods=[method])
    install_auth_middleware(app)

    with TestClient(app) as client:
        if token:
            client.cookies.set("tf_session", token)
        for method, path in [
            ("POST", "/api/kline/minute/fetch"),
            ("GET", "/api/kline/minute/fetch/job1"),
        ]:
            response = client.request(method, path)
            assert response.status_code == (200 if role else 401)
        for method, path in [
            ("POST", "/api/kline/sync_minute_single"),
            ("POST", "/api/kline/sync_minute"),
            ("GET", "/api/pipeline/jobs/job1"),
            ("POST", "/api/pipeline/jobs/job1/cancel"),
        ]:
            response = client.request(method, path)
            assert response.status_code == (401 if role is None else 200 if role == "admin" else 403)
        before = len(reached)
        response = client.post("/api/kline/minute/fetch", headers={"Origin": "https://other.invalid"})
        assert response.status_code == 403
        assert response.json()["code"] == "CSRF_REJECTED"
        assert len(reached) == before
