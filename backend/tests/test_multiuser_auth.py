from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services.auth import AuthError, AuthStore


def test_sessions_are_atomic_persistent_and_revocable(tmp_path):
    store = AuthStore(tmp_path)
    admin = store.bootstrap("admin", "initial-secret")
    assert admin and not store.bootstrap("other", "changed-secret")

    def login(_):
        return AuthStore(tmp_path).login("admin", "initial-secret", "127.0.0.1", "browser")[0]

    with ThreadPoolExecutor(max_workers=4) as pool:
        tokens = list(pool.map(login, range(4)))
    fresh = AuthStore(tmp_path)
    valid = [t for t in tokens if fresh.session(t)]
    assert len(valid) == 1
    fresh.revoke_session(valid[0])
    assert store.session(valid[0]) is None
    assert store.get_user(admin["id"])["login_count"] == 4
    assert all(t.encode() not in fresh.path.read_bytes() for t in tokens)


def test_role_dates_last_admin_and_password_lifecycle(tmp_path):
    store = AuthStore(tmp_path)
    admin = store.bootstrap("admin", "initial-secret")
    with pytest.raises(AuthError):
        store.update_user(admin["id"], {"is_active": False})
    with pytest.raises(AuthError):
        store.create_user("pro", "temporary-secret", "pro", None)
    pro = store.create_user("pro", "temporary-secret", "pro", "2026-10-13")
    before = datetime(2026, 10, 13, 15, 59, 59, tzinfo=UTC).timestamp()
    assert store.get_user(pro["id"], now=before)["role"] == "pro"
    assert store.get_user(pro["id"], now=before + 1)["role"] == "guest"
    token, status = store.login("pro", "temporary-secret", "ip", "")
    assert status["user"]["must_change_password"]
    new, status = store.change_password(token, "temporary-secret", "permanent-secret")
    assert not status["user"]["must_change_password"]
    assert store.session(token) is None
    assert store.session(new)
    store.reset_password(pro["id"], "reset-secret")
    assert store.session(new) is None
    token, _ = store.login("pro", "reset-secret", "ip", "")
    store.update_user(pro["id"], {"is_active": False})
    assert store.session(token) is None
    with pytest.raises(AuthError):
        store.login("pro", "reset-secret", "ip", "")


def test_http_boundary_and_forced_change(tmp_path):
    from app.api import auth, users
    from app.services.auth_access import install_auth_middleware
    from app.services.user_context import current_user, user_data_dir

    app = FastAPI()
    app.state.auth_store = AuthStore(tmp_path)
    app.state.auth_store.bootstrap("admin", "initial-secret")
    app.include_router(auth.router)
    app.include_router(users.router)

    @app.get("/api/private")
    def private():
        return {"id": current_user().id, "path": str(user_data_dir())}

    install_auth_middleware(app)
    with TestClient(app) as client:
        assert client.get("/api/private").status_code == 401
        assert client.post("/api/auth/setup", json={"password": "bad-secret"}).status_code in (
            401,
            404,
        )
        assert (
            client.get("/api/private", headers={"Authorization": "Bearer legacy"}).status_code
            == 401
        )
        assert client.get("/api/events?ticket=legacy").status_code == 403
        assert (
            client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "initial-secret"},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        res = client.post(
            "/api/auth/login", json={"username": "admin", "password": "initial-secret"}
        )
        assert res.status_code == 200
        assert "HttpOnly" in res.headers["set-cookie"]
        uid = res.json()["user"]["id"]
        assert client.get("/api/private").json()["path"] == str(tmp_path / "users" / uid)
        created = client.post(
            "/api/users",
            json={"username": "guest", "password": "temporary-secret", "role": "guest"},
        )
        assert created.status_code == 200
        assert client.post("/api/auth/logout", headers={"X-Expected-User": uid}).status_code == 200
        guest_login = client.post("/api/auth/login", json={"username": "guest", "password": "temporary-secret"})
        assert client.get("/api/private").status_code == 403
        assert client.get("/api/auth/status").json()["user"]["must_change_password"]
        assert (
            client.post(
                "/api/auth/change-password",
                json={"old_password": "temporary-secret", "new_password": "changed-secret"},
                headers={"X-Expected-User": guest_login.json()["user"]["id"]},
            ).status_code
            == 200
        )
        assert client.get("/api/private").status_code == 200
        assert client.get("/api/users").status_code == 403


def test_expiry_retention_rate_limit_and_restored_account(tmp_path, monkeypatch):
    from app.services import auth

    now = 1800000000.0
    monkeypatch.setattr(auth.time, "time", lambda: now)
    store = AuthStore(tmp_path)
    admin = store.bootstrap("admin", "initial-secret")
    token, _ = store.login("admin", "initial-secret", "ip1", "browser")
    assert store.session(token, now=now + auth.SESSION_TTL - 1)
    assert not store.session(token, now=now + auth.SESSION_TTL)
    for _ in range(5):
        with pytest.raises(AuthError) as exc:
            AuthStore(tmp_path).login("unknown", "wrong-password", "ip2", "device")
        assert exc.value.status == 401
    with pytest.raises(AuthError) as exc:
        store.login("admin", "initial-secret", "ip2", "")
    assert exc.value.status == 429
    now += 301
    assert store.login("admin", "initial-secret", "ip2", "")[1]["authenticated"]
    guest = store.create_user("guest", "guest-password")
    store.update_user(guest["id"], {"is_active": False})
    store.update_user(guest["id"], {"is_active": True})
    assert store.login("guest", "guest-password", "ip3", "")[1]["authenticated"]
    now += 91 * 86400
    assert store.login_events()["total"] == 0
    assert store.get_user(admin["id"])["login_count"] == 2
    assert store.get_user(admin["id"])["last_login_ip"] == "ip2"


def test_trusted_proxy_and_private_context_validation(tmp_path, monkeypatch):
    from starlette.requests import Request

    from app.api.auth import _client_ip, same_origin
    from app.config import settings
    from app.services.user_context import UserContext, current_user, user_data_dir, user_scope

    def request(peer, xff, origin="http://panel"):
        return Request(
            {
                "type": "http",
                "scheme": "http",
                "server": ("panel", 80),
                "path": "/",
                "client": (peer, 1),
                "headers": [(b"x-forwarded-for", xff.encode()), (b"origin", origin.encode())],
            }
        )

    monkeypatch.setattr(settings, "auth_trusted_proxies", "127.0.0.1/32,10.0.0.1/32")
    assert _client_ip(request("192.168.1.2", "1.2.3.4")) == "192.168.1.2"
    assert _client_ip(request("127.0.0.1", "6.6.6.6, 1.2.3.4, 10.0.0.1")) == "1.2.3.4"
    assert _client_ip(request("127.0.0.1", "bad")) == "127.0.0.1"
    assert same_origin(request("peer", ""))
    assert not same_origin(request("peer", "", "http://evil"))
    with pytest.raises(RuntimeError):
        user_data_dir(tmp_path)
    with user_scope(UserContext("a" * 32, "guest", tmp_path)):
        assert user_data_dir() == tmp_path / "users" / ("a" * 32)
        with user_scope(UserContext("b" * 32, "guest", tmp_path)):
            assert current_user().id == "b" * 32
        assert current_user().id == "a" * 32
    assert current_user(False) is None
    with pytest.raises(ValueError):
        user_data_dir(tmp_path, "../../admin")


def test_unconfigured_and_corrupt_authority_fail_closed(tmp_path):
    from app.api import auth
    from app.services.auth_access import install_auth_middleware

    app = FastAPI()
    store = AuthStore(tmp_path)
    app.state.auth_store = store
    app.include_router(auth.router)

    @app.get("/api/private")
    def private():
        return {"secret": "never"}

    install_auth_middleware(app)
    with TestClient(app) as client:
        assert not client.get("/api/auth/status").json()["authenticated"]
        assert client.get("/api/private").status_code == 401
        store.path.write_bytes(b"not a database")
        assert client.get("/api/auth/status").status_code == 503
        client.cookies.set("tf_session", "legacy")
        assert client.get("/api/private").status_code == 503


def test_ordinary_user_cannot_read_secrets_or_change_shared_monitor_config(tmp_path, monkeypatch):
    from app.api import auth
    from app.api import settings as settings_api
    from app.config import settings
    from app.services.auth_access import install_auth_middleware

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store = AuthStore(tmp_path)
    store.bootstrap("admin", "initial-secret")
    guest = store.create_user("guest", "temporary-secret")
    token, _ = store.login("guest", "temporary-secret", "ip", "")
    token, _ = store.change_password(token, "temporary-secret", "changed-secret")
    app = FastAPI()
    app.state.auth_store = store
    app.include_router(auth.router)
    app.include_router(settings_api.router)
    install_auth_middleware(app)
    with TestClient(app) as client:
        client.cookies.set("tf_session", token)
        assert client.get("/api/auth/status").json()["user"]["id"] == guest["id"]
        response = client.get("/api/settings/preferences")
        assert response.status_code == 200
        assert "feishu_webhook_secret" not in response.json()
        assert "wecom_bot_secret" not in response.json()
        assert "email_smtp_config" not in response.json()
        assert "watchlist_columns" in response.json()
        assert client.get("/api/settings/data-sources/source").status_code == 403
        assert client.get("/api/ext-data/source/api-key").status_code == 403
        assert (
            client.put(
                "/api/settings/preferences/realtime-monitor", json={"minute_refresh_enabled": True}
            ).status_code
            == 403
        )
        assert (
            client.put(
                "/api/settings/preferences/realtime-monitor", json={"screener_auto_run": True}
            ).status_code
            == 200
        )
        for payload in (
            {"strategy_monitor_enabled": True},
            {"strategy_monitor_ids": ["demo"]},
            {"monitor_ext_fields": {}},
        ):
            response = client.put('/api/settings/preferences/realtime-monitor', json=payload)
            assert response.status_code == 403


@pytest.mark.parametrize("operation", ["create", "promote", "role", "expiry", "disable", "reset", "revoke"])
def test_unique_admin_is_protected_without_account_or_session_changes(tmp_path, operation):
    store = AuthStore(tmp_path)
    admin = store.bootstrap("custom-admin", "initial-secret")
    guest = store.create_user("guest", "temporary-secret")
    token, _ = store.login("custom-admin", "initial-secret", "ip", "")
    before = store.list_users()
    actions = {
        "create": lambda: store.create_user("admin2", "temporary-secret", "admin", actor_id=admin["id"]),
        "promote": lambda: store.update_user(guest["id"], {"role": "admin"}, actor_id=admin["id"]),
        "role": lambda: store.update_user(admin["id"], {"role": "guest"}, actor_id=admin["id"]),
        "expiry": lambda: store.update_user(admin["id"], {"pro_expires_on": "2099-12-31"}, actor_id=admin["id"]),
        "disable": lambda: store.update_user(admin["id"], {"is_active": False}, actor_id=admin["id"]),
        "reset": lambda: store.reset_password(admin["id"], "reset-secret", actor_id=admin["id"]),
        "revoke": lambda: store.revoke_user_session(admin["id"], actor_id=admin["id"]),
    }
    with pytest.raises(AuthError) as error:
        actions[operation]()
    assert error.value.status == 409
    assert store.list_users() == before
    assert store.session(token)["user"]["id"] == admin["id"]
    assert store.login("custom-admin", "initial-secret", "ip", "")[1]["authenticated"]


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/api/users", {"username": "admin2", "password": "temporary-secret", "role": "admin"}),
    ("PATCH", "/api/users/{guest}", {"role": "admin"}),
    ("PATCH", "/api/users/{admin}", {"role": "pro", "pro_expires_on": "2099-12-31"}),
    ("PATCH", "/api/users/{admin}", {"is_active": False}),
    ("POST", "/api/users/{admin}/reset-password", {"password": "reset-secret"}),
    ("POST", "/api/users/{admin}/revoke-session", {}),
])
def test_http_rejects_admin_management_and_preserves_login(tmp_path, method, path, payload):
    from app.api import auth, users
    from app.services.auth_access import install_auth_middleware

    store = AuthStore(tmp_path)
    admin = store.bootstrap("custom-admin", "initial-secret")
    guest = store.create_user("guest", "temporary-secret")
    app = FastAPI()
    app.state.auth_store = store
    app.include_router(auth.router)
    app.include_router(users.router)
    install_auth_middleware(app)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "custom-admin", "password": "initial-secret"})
        token = client.cookies.get("tf_session")
        before = store.list_users()
        response = client.request(method, path.format(admin=admin["id"], guest=guest["id"]), json=payload)
        assert response.status_code == 409
        assert store.list_users() == before
        assert store.session(token)["user"]["id"] == admin["id"]
        assert client.get("/api/users").status_code == 200
        assert client.get("/api/users/login-events", params={"user_id": admin["id"]}).json()["total"] == 1


def test_concurrent_admin_promotions_cannot_add_an_admin(tmp_path):
    store = AuthStore(tmp_path)
    admin = store.bootstrap("admin", "initial-secret")
    guests = [store.create_user(f"guest{i}", "temporary-secret") for i in range(2)]

    def promote(uid):
        try:
            AuthStore(tmp_path).update_user(uid, {"role": "admin"}, actor_id=admin["id"])
            return True
        except AuthError:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(promote, [guest["id"] for guest in guests])) == [False, False]
    assert len([u for u in store.list_active_users() if u["role"] == "admin"]) == 1


def test_admin_can_manage_ordinary_users_and_change_own_password(tmp_path):
    store = AuthStore(tmp_path)
    admin = store.bootstrap("admin", "initial-secret")
    guest = store.create_user("guest", "temporary-secret", actor_id=admin["id"])
    user = store.update_user(guest["id"], {"role": "pro", "pro_expires_on": "2099-12-31"}, actor_id=admin["id"])
    assert user["role"] == "pro"
    guest_token, _ = store.login("guest", "temporary-secret", "ip", "")
    store.revoke_user_session(guest["id"], actor_id=admin["id"])
    assert store.session(guest_token) is None
    token, _ = store.login("admin", "initial-secret", "ip", "")
    new_token, status = store.change_password(token, "initial-secret", "changed-secret")
    assert status["user"]["role"] == "admin"
    assert store.session(token) is None
    assert store.session(new_token)["user"]["id"] == admin["id"]
    store.revoke_session(new_token)
    assert store.session(new_token) is None


@pytest.mark.parametrize('path', [
    '/api/kline/sync', '/api/kline/sync_batch', '/api/kline/refresh_views',
    '/api/kline/sync_minute', '/api/kline/sync_minute_single', '/api/kline/minute-migrate',
    '/api/kline/clear_minute', '/api/kline/extend_history', '/api/kline/repair_daily',
    '/api/kline/rebuild_enriched', '/api/index/sync_instruments', '/api/index/sync_daily',
    '/api/financials/sync/balance', '/api/intraday/refresh', '/api/analysis-menus',
])
def test_shared_maintenance_gate(path):
    from app.services.auth_access import admin_required
    assert admin_required(path, 'POST')


@pytest.mark.parametrize('path', [
    '/api/kline/instruments/names', '/api/kline/daily-batch', '/api/kline/minute-batch',
    '/api/financials/analyze', '/api/financials/reports',
])
def test_queries_and_personal_ai_are_not_admin_maintenance(path):
    from app.services.auth_access import admin_required
    assert not admin_required(path, 'POST')


def test_market_availability_exposes_only_safe_stats_without_storage_scan(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.api import data
    from app.services.auth_access import install_auth_middleware
    store = AuthStore(tmp_path)
    store.bootstrap('admin', 'initial-secret')
    store.create_user('guest', 'temporary-secret')
    token, _ = store.login('guest', 'temporary-secret', 'ip', '')
    token, _ = store.change_password(token, 'temporary-secret', 'changed-secret')
    app = FastAPI()
    app.state.auth_store = store
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(data.router)
    install_auth_middleware(app)
    monkeypatch.setattr(data, '_get_table_stats', lambda name, fetch: {
        'rows': 100, 'earliest_date': '2026-01-01', 'latest_date': '2026-10-13',
        'symbols_covered': 2, 'directory': '/sensitive/owner', 'task': 'private-job',
    })
    monkeypatch.setattr(data, '_get_storage', lambda root: pytest.fail('guest scanned storage'))
    monkeypatch.setattr(data, '_last_finished', lambda name: pytest.fail('guest read jobs'))
    monkeypatch.setattr(data, '_next_cron_run', lambda *args: pytest.fail('guest read schedule'))
    with TestClient(app) as client:
        client.cookies.set('tf_session', token)
        response = client.get('/api/data/status')
        assert response.status_code == 200
        payload = response.json()
        assert payload['daily']['latest_date'] == '2026-10-13'
        assert payload['indicators_ready']
        assert 'checked_at' in payload
        assert 'storage' not in payload
        assert 'directory' not in response.text
        assert 'private-job' not in response.text
        assert 'next_pipeline_run' not in payload
        assert client.post('/api/data/clear').status_code == 403


def test_expected_owner_rejects_other_tab_cookie_before_business_write(tmp_path):
    from app.api import auth
    from app.services.auth_access import install_auth_middleware
    store = AuthStore(tmp_path)
    first = store.bootstrap('admin', 'initial-secret')
    store.create_user('other', 'temporary-secret')
    token, _ = store.login('other', 'temporary-secret', 'ip', '')
    token, _ = store.change_password(token, 'temporary-secret', 'changed-secret')
    second = store.session(token)['user']
    app = FastAPI()
    app.state.auth_store = store
    app.include_router(auth.router)
    writes = []
    @app.post('/api/private')
    def write():
        writes.append(True)
        return {'ok': True}
    @app.get('/api/private/stream')
    def stream():
        return {'ok': True}
    install_auth_middleware(app)
    with TestClient(app) as client:
        client.cookies.set('tf_session', token)
        result = client.post('/api/private', headers={'X-Expected-User': first['id']})
        assert result.status_code == 401
        assert result.json()['code'] == 'SESSION_OWNER_CHANGED'
        assert not writes
        assert client.get('/api/private/stream', params={'expected_user_id': first['id']}).status_code == 401
        assert client.post('/api/private', headers={'X-Expected-User': second['id']}).status_code == 200
        assert client.get('/api/auth/status', headers={'X-Expected-User': first['id']}).json()['user']['id'] == second['id']


@pytest.mark.parametrize('path', ['/api/auth/logout', '/api/auth/change-password'])
@pytest.mark.parametrize('expected', ['old_header', 'old_query', 'conflicting', 'missing'])
def test_auth_mutations_reject_other_tab_owner_without_cookie_or_account_changes(tmp_path, path, expected):
    from app.api import auth
    from app.services.auth_access import install_auth_middleware

    store = AuthStore(tmp_path)
    first = store.bootstrap('first', 'shared-password')
    second = store.create_user('second', 'shared-password')
    first_token, _ = store.login('first', 'shared-password', 'ip', '')
    second_token, _ = store.login('second', 'shared-password', 'ip', '')
    app = FastAPI()
    app.state.auth_store = store
    app.include_router(auth.router)
    install_auth_middleware(app)
    kwargs = {
        'old_header': {'headers': {'X-Expected-User': first['id']}},
        'old_query': {'params': {'expected_user_id': first['id']}},
        'conflicting': {'headers': {'X-Expected-User': second['id']}, 'params': {'expected_user_id': first['id']}},
        'missing': {},
    }[expected]
    with TestClient(app) as client:
        client.cookies.set('tf_session', second_token)
        response = client.post(path, json={'old_password': 'shared-password', 'new_password': 'changed-password'}, **kwargs)
        assert response.status_code == 401
        assert response.json()['code'] in {'SESSION_OWNER_CHANGED', 'SESSION_OWNER_REQUIRED'}
        assert 'set-cookie' not in response.headers
        assert client.cookies.get('tf_session') == second_token
        assert store.session(first_token)['user']['id'] == first['id']
        assert store.session(second_token)['user']['id'] == second['id']
    assert store.login('second', 'shared-password', 'ip', '')[1]['authenticated']


def test_temporary_password_owner_can_change_password_and_logout(tmp_path):
    from app.api import auth
    from app.services.auth_access import install_auth_middleware

    store = AuthStore(tmp_path)
    store.bootstrap('admin', 'initial-secret')
    user = store.create_user('guest', 'temporary-secret')
    app = FastAPI()
    app.state.auth_store = store
    app.include_router(auth.router)
    install_auth_middleware(app)
    with TestClient(app) as client:
        login = client.post('/api/auth/login', json={'username': 'guest', 'password': 'temporary-secret'})
        assert login.json()['user']['must_change_password']
        initial_token = client.cookies.get('tf_session')
        headers = {'X-Expected-User': user['id']}
        assert client.post('/api/auth/logout', headers=headers).status_code == 200
        assert store.session(initial_token) is None
        assert client.cookies.get('tf_session') is None
        client.post('/api/auth/login', json={'username': 'guest', 'password': 'temporary-secret'})
        old_token = client.cookies.get('tf_session')
        changed = client.post('/api/auth/change-password', headers=headers, json={
            'old_password': 'temporary-secret', 'new_password': 'changed-secret',
        })
        assert changed.status_code == 200
        assert changed.json()['user']['id'] == user['id']
        assert not changed.json()['user']['must_change_password']
        assert store.session(old_token) is None
        assert store.session(client.cookies.get('tf_session'))['user']['id'] == user['id']
        assert client.post('/api/auth/logout', headers=headers).status_code == 200


def test_weekly_mining_schedule_write_requires_admin():
    from app.services.auth_access import admin_required
    assert admin_required('/api/backtest/mining/config', 'PATCH')
    assert not admin_required('/api/backtest/mining/config', 'GET')


@pytest.mark.parametrize('service_role', ['web', 'collector'])
@pytest.mark.parametrize('role', ['guest', 'pro'])
def test_real_mining_schedule_route_denies_non_admin_without_writing(tmp_path, monkeypatch, service_role, role):
    from contextlib import asynccontextmanager

    from app.config import settings
    from app.main import create_app
    from app.services import preferences

    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    preferences.save({'mining_schedule_enabled': False, 'mining_schedule_weekday': 4,
                      'mining_budget_profile': 'balanced'})
    path = tmp_path / 'user_data' / 'preferences.json'
    before = path.read_bytes()
    store = AuthStore(tmp_path)
    store.bootstrap('admin', 'initial-secret')
    store.create_user(role, 'temporary-secret', role, '2099-12-31' if role == 'pro' else None)
    token, _ = store.login(role, 'temporary-secret', 'test', '')
    token, _ = store.change_password(token, 'temporary-secret', 'changed-secret')

    @asynccontextmanager
    async def lifespan(app):
        app.state.auth_store = store
        yield

    app = create_app(service_role)
    app.router.lifespan_context = lifespan
    with TestClient(app) as client:
        client.cookies.set('tf_session', token)
        response = client.patch('/api/backtest/mining/config', json={
            'mining_schedule_enabled': True, 'mining_schedule_weekday': 2,
            'mining_budget_profile': 'strict',
        })
    assert response.status_code == 403
    assert path.read_bytes() == before
