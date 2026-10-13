import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config
from app.services import auth


@pytest.fixture
def isolated_admin_env(monkeypatch):
    for name in list(os.environ):
        if name.upper() in {"AUTH_ADMIN_USERNAME", "AUTH_ADMIN_PASSWORD"}:
            monkeypatch.delenv(name)
    monkeypatch.setenv("literal", "expanded")


@pytest.mark.parametrize("username_key,password_key", [
    ("AUTH_ADMIN_USERNAME", "AUTH_ADMIN_PASSWORD"),
    ("auth_admin_username", "auth_admin_password"),
    ("Auth_Admin_UserName", "Auth_Admin_PassWord"),
])
def test_bootstrap_preserves_literal_password_and_never_overwrites(
    tmp_path, monkeypatch, isolated_admin_env, username_key, password_key,
):
    env_path = tmp_path / ".env"
    password = "file${literal}-secret"
    env_path.write_text(f"{username_key}=admin\n{password_key}={password}\n")
    monkeypatch.setattr(config, "_ENV_FILE", env_path)
    monkeypatch.setattr(config, "settings", config.Settings(_env_file=env_path, data_dir=tmp_path))
    assert auth.bootstrap_from_env()
    store = auth.get_store()
    token, status = store.login("admin", password, "ip", "")
    assert status["user"]["is_initial_admin"]
    assert store.first_admin_id() == status["user"]["id"]
    assert password.encode() not in store.path.read_bytes()
    env_path.write_text("AUTH_ADMIN_USERNAME=other\nAUTH_ADMIN_PASSWORD=changed-secret\n")
    assert not auth.bootstrap_from_env()
    assert store.session(token)
    assert store.list_users()["total"] == 1


@pytest.mark.parametrize("username_key,password_key", [
    ("AUTH_ADMIN_USERNAME", "AUTH_ADMIN_PASSWORD"),
    ("auth_admin_username", "auth_admin_password"),
    ("Auth_Admin_UserName", "Auth_Admin_PassWord"),
])
@pytest.mark.parametrize("overrides,expected", [
    ({"AUTH_ADMIN_USERNAME": "systemadmin", "AUTH_ADMIN_PASSWORD": "system${literal}-secret"},
     ("systemadmin", "system${literal}-secret")),
    ({"AUTH_ADMIN_USERNAME": "systemadmin"}, ("systemadmin", "file${literal}-secret")),
    ({"AUTH_ADMIN_PASSWORD": "system-secret"}, ("fileadmin", "system-secret")),
    ({"auth_admin_username": "systemadmin", "auth_admin_password": "system-secret"},
     ("systemadmin", "system-secret")),
    ({"AUTH_ADMIN_USERNAME": "", "AUTH_ADMIN_PASSWORD": ""}, ("pyxadmin", "19950412")),
    ({"AUTH_ADMIN_PASSWORD": ""}, None),
])
def test_bootstrap_honors_system_environment_before_dotenv(
    tmp_path, monkeypatch, isolated_admin_env, username_key, password_key, overrides, expected,
):
    env_path = tmp_path / ".env"
    env_path.write_text(f"{username_key}=fileadmin\n{password_key}=file${{literal}}-secret\n")
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(config, "_ENV_FILE", env_path)
    monkeypatch.setattr(config, "settings", config.Settings(_env_file=env_path, data_dir=tmp_path))

    if expected is None:
        with pytest.raises(auth.AuthError):
            auth.bootstrap_from_env()
        assert not auth.is_configured()
    else:
        assert auth.bootstrap_from_env()
        username, password = expected
        _, status = auth.get_store().login(username, password, "ip", "")
        assert status["user"]["username"] == username
        assert status["user"]["is_initial_admin"]


def test_legacy_password_and_sessions_cannot_initialize_or_authenticate(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("AUTH_PASSWORD=old-shared-secret\n")
    old = tmp_path / "user_data" / "auth.json"
    old.parent.mkdir()
    old.write_text('{"password_hash":"legacy","sessions":{"oldtoken":9999999999}}')
    monkeypatch.setattr(config, "_ENV_FILE", env_path)
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(config.settings, "auth_admin_username", "")
    monkeypatch.setattr(config.settings, "auth_admin_password", "")
    assert auth.bootstrap_from_env()
    assert auth.is_configured()
    with pytest.raises(auth.AuthError) as exc:
        auth.get_store().login("pyxadmin", "old-shared-secret", "ip", "")
    assert exc.value.status == 401
    assert not auth.is_valid_session("oldtoken")
    assert old.read_text() == '{"password_hash":"legacy","sessions":{"oldtoken":9999999999}}'


@pytest.fixture
def empty_auth(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(config.settings, "data_dir", tmp_path)
    monkeypatch.setattr(config.settings, "auth_admin_username", "")
    monkeypatch.setattr(config.settings, "auth_admin_password", "")
    return tmp_path


def test_default_admin_http_login_and_password_survive_restart(empty_auth):
    from app.api import auth as auth_api
    from app.api import users
    from app.services.auth_access import install_auth_middleware

    assert auth.bootstrap_from_env()
    store = auth.get_store()
    app = FastAPI()
    app.state.auth_store = store
    app.include_router(auth_api.router)
    app.include_router(users.router)
    install_auth_middleware(app)
    with TestClient(app) as client:
        assert client.get("/api/auth/status").json()["configured"]
        assert client.get("/api/users").status_code == 401
        response = client.post("/api/auth/login", json={"username": "pyxadmin", "password": "19950412"})
        assert response.status_code == 200
        user = response.json()["user"]
        assert user["role"] == "admin"
        assert user["is_initial_admin"]
        assert not user["must_change_password"]
        assert store.first_admin_id() == user["id"]
        assert client.get("/api/users").status_code == 200
        assert client.post("/api/auth/change-password", json={
            "old_password": "19950412", "new_password": "changed-secret",
        }, headers={"X-Expected-User": user["id"]}).status_code == 200
        token = client.cookies.get("tf_session")
    assert not auth.bootstrap_from_env()
    restarted = auth.AuthStore(empty_auth)
    assert restarted.session(token)["user"]["id"] == user["id"]
    assert restarted.list_users()["total"] == 1
    with pytest.raises(auth.AuthError) as exc:
        restarted.login("pyxadmin", "19950412", "ip", "")
    assert exc.value.status == 401
    assert restarted.login("pyxadmin", "changed-secret", "ip", "")[1]["authenticated"]
    assert b"19950412" not in store.path.read_bytes()


@pytest.mark.parametrize("field,value", [
    ("auth_admin_username", "customadmin"), ("auth_admin_password", "custom-secret"),
])
def test_incomplete_override_does_not_create_unexpected_admin(empty_auth, monkeypatch, field, value):
    monkeypatch.setattr(config.settings, field, value)
    with pytest.raises(auth.AuthError):
        auth.bootstrap_from_env()
    assert not auth.is_configured()


def test_existing_accounts_skip_default_and_incomplete_overrides(empty_auth, monkeypatch):
    store = auth.get_store()
    admin = store.bootstrap("existing", "existing-secret")
    token, _ = store.login("existing", "existing-secret", "ip", "")
    assert not auth.bootstrap_from_env()
    monkeypatch.setattr(config.settings, "auth_admin_username", "partial")
    assert not auth.bootstrap_from_env()
    restarted = auth.AuthStore(empty_auth)
    assert restarted.session(token)["user"]["id"] == admin["id"]
    assert restarted.list_users()["total"] == 1
    with pytest.raises(auth.AuthError) as exc:
        restarted.login("pyxadmin", "19950412", "ip", "")
    assert exc.value.status == 401
