from __future__ import annotations

import io
import json
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.config import settings
from app.services import ext_config_transfer as transfer
from app.services.ext_data import ExtConfig, ExtConfigStore, ExtField, PullConfig
from app.services.ext_pull import _apply_auth


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr("app.secrets_store.load", lambda: {"vendor_api_key": "private-test-key-123"})
    monkeypatch.setattr("app.services.ext_data.get_ext_api_key", lambda _id: "private-test-key-123")
    store = ExtConfigStore(tmp_path)
    store.upsert(ExtConfig(
        id="auction", label="集合竞价", mode="timeseries",
        fields=[ExtField("symbol"), ExtField("ts"), ExtField("price", "float")],
        pull=PullConfig(
            url="https://example.test/day?stride=5", auth={"type": "header", "header": "X-API-Key", "key_ref": "vendor_api_key"},
            enabled=True, time_field="ts", date_param="date",
            page_param="page", page_size_param="size", page_size=100, timeout_seconds=120,
            last_run="2026-11-04", last_message="private-test-key-123",
        ),
    ))
    return tmp_path


def entries(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {name: z.read(name) for name in z.namelist()}


def package(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for name, value in files.items():
            z.writestr(name, value)
    return out.getvalue()


def test_export_is_one_compatible_config_without_secrets_data_or_runtime_state(source):
    data, filename = transfer.export_config(source, "auction")
    files = entries(data)
    assert filename.endswith(".zip")
    assert set(files) == {"manifest.json", "ext_data/auction/config.json"}
    config = json.loads(files["ext_data/auction/config.json"])
    assert b"private-test-key-123" not in b"".join(files.values())
    assert config["pull"]["auth"] == {"type": "header", "header": "X-API-Key", "param": "token"}
    assert config["pull"]["enabled"] is False
    assert "last_run" not in config["pull"] and "last_message" not in config["pull"]
    assert config["pull"]["page_size"] == 100
    assert config["pull"]["timeout_seconds"] == 120
    assert ExtConfigStore(source).get("auction").pull.enabled is True


def test_recipient_can_import_and_use_own_key_without_source_reference(source, tmp_path, monkeypatch):
    data, _ = transfer.export_config(source, "auction")
    target = tmp_path / "recipient"
    preview = transfer.inspect_config(target, data)
    assert preview["id"] == "auction" and preview["requires_key"]
    assert not (target / "ext_data").exists()
    result = transfer.import_config(target, data)
    config = ExtConfigStore(target).get("auction")
    assert result["id"] == config.id
    assert config.pull.time_field == "ts" and config.pull.enabled is False
    monkeypatch.setattr("app.services.ext_data.get_ext_api_key", lambda _id: "recipient-key")
    headers = {}
    _apply_auth(config.id, config.pull.auth, config.pull.url, headers)
    assert headers["X-API-Key"] == "recipient-key"


def test_import_edit_enable_and_pull_keep_complete_pagination(source, monkeypatch):
    from types import SimpleNamespace

    import polars as pl
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import ext_data as api
    from app.services import ext_pull

    config = ExtConfigStore(source).get("auction")
    config.pull.page_start = 0
    config.pull.page_size = 2
    config.pull.max_pages = 50
    config.pull.schedule_time = "09:30"
    config.pull.schedule_period = "weekly"
    config.pull.schedule_day = 5
    config.pull.schedule_trading_only = True
    config.pull.since_param = "since"
    config.pull.granularity = "intraday"
    ExtConfigStore(source).upsert(config)
    payload, _ = transfer.export_config(source, config.id)
    target = source / "recipient"
    transfer.import_config(target, payload)
    app = FastAPI()
    app.include_router(api.router)
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=target))
    monkeypatch.setattr(api.pull_scheduler, "refresh", lambda root: None)
    monkeypatch.setattr(api, "_refresh_views", lambda request: None)
    pages = []

    async def request_json(pull, config_id, day=None, extra_params=None):
        pages.append(extra_params)
        symbols = ["600000.SH", "000001.SZ"] if extra_params.get("page") == "0" else ["300750.SZ"]
        return [{"symbol": symbol, "ts": "09:30:00", "price": 10.0} for symbol in symbols]

    monkeypatch.setattr(ext_pull, "_request_json", request_json)
    body = ExtConfigStore(target).get(config.id).pull.to_dict()
    keys = ("page_param", "page_size_param", "page_size", "page_start", "max_pages",
            "schedule_time", "schedule_period", "schedule_day", "schedule_trading_only", "since_param", "granularity")
    with TestClient(app) as client:
        for enabled in (False, True):
            response = client.put(f"/api/ext-data/{config.id}/pull", json={**body, "enabled": enabled})
            assert response.status_code == 200
            assert {key: response.json()["pull"][key] for key in keys} == {key: body[key] for key in keys}
        response = client.post(f"/api/ext-data/{config.id}/pull/run")
        assert response.status_code == 200, response.text
        assert response.json()["rows"] == 3
    assert pages == [{"page": "0", "size": "2"}, {"page": "1", "size": "2"}]
    part = next((target / "ext_data" / config.id / "timeseries").rglob("*.parquet"))
    assert set(pl.read_parquet(part)["symbol"]) == {"600000.SH", "000001.SZ", "300750.SZ"}


def test_conflicts_never_overwrite_existing_configs_data_or_keys(source):
    data, _ = transfer.export_config(source, "auction")
    marker = source / "ext_data" / "auction" / "part.parquet"
    marker.write_bytes(b"existing rows")
    old = (marker.parent / "config.json").read_bytes()
    with pytest.raises(FileExistsError):
        transfer.import_config(source, data)
    assert marker.read_bytes() == b"existing rows"
    assert (marker.parent / "config.json").read_bytes() == old
    imported = transfer.import_config(source, data, config_id="auction_copy")
    assert imported["id"] == "auction_copy"
    assert transfer.inspect_config(source, data)["conflict"] is True


@pytest.mark.parametrize("name", ["../outside.json", "user_data/secrets.json", "extra.py"])
def test_unexpected_or_unsafe_archive_members_are_rejected(source, name):
    data, _ = transfer.export_config(source, "auction")
    files = entries(data)
    files[name] = b"{}"
    with pytest.raises(ValueError):
        transfer.import_config(source / "target", package(files))


@pytest.mark.parametrize("change", [
    lambda c: c.update(id="../outside"),
    lambda c: c.update(mode="bad"),
    lambda c: c.update(fields=[]),
    lambda c: c["pull"].update(timeout_seconds=10000),
    lambda c: c["pull"].update(schedule_time="25:00"),
    lambda c: c["pull"].update(schedule_period="weekly", schedule_day=8),
    lambda c: c["pull"].update(since_param="since", time_field=None),
    lambda c: c["pull"].update(since_param="page", page_param="page"),
])
def test_invalid_contracts_fail_before_creating_files(source, change):
    data, _ = transfer.export_config(source, "auction")
    files = entries(data)
    config = json.loads(files["ext_data/auction/config.json"])
    change(config)
    files["ext_data/auction/config.json"] = json.dumps(config)
    target = source / "recipient"
    with pytest.raises(ValueError):
        transfer.import_config(target, package(files))
    assert not (target / "ext_data").exists()


def test_legacy_query_and_header_credentials_are_removed_and_auth_can_be_refilled(source):
    store = ExtConfigStore(source)
    config = store.get("auction")
    config.pull.auth = None
    config.pull.url = "https://example.test/day?token=private-test-key-123&stride=5"
    config.pull.headers = {"Cookie": "session=private", "Accept": "application/json"}
    config.pull.body = '{"nested":{"api_key":"body-private"},"limit":20}'
    config.description = "Key: private-test-key-123"
    store.upsert(config)
    files = entries(transfer.export_config(source, "auction")[0])
    joined = b"".join(files.values())
    for secret in (b"private-test-key-123", b"body-private", b"session=private"):
        assert secret not in joined
    exported = json.loads(files["ext_data/auction/config.json"])
    assert exported["pull"]["auth"]["type"] == "query"
    assert exported["pull"]["auth"]["param"] == "token"
    assert exported["pull"]["headers"] == {"Accept": "application/json"}
    assert "stride=5" in exported["pull"]["url"]


def test_concurrent_imports_create_only_one_config(source):
    data, _ = transfer.export_config(source, "auction")
    target = source / "recipient"
    def apply(_):
        try:
            transfer.import_config(target, data)
            return True
        except FileExistsError:
            return False
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(apply, range(8)))
    assert sum(results) == 1
    assert ExtConfigStore(target).get("auction") is not None


def test_failed_atomic_publication_can_be_retried_without_leftover_reservation(source, monkeypatch):
    data, _ = transfer.export_config(source, "auction")
    target = source / "recipient"
    import app.services.fs_utils as fs

    replace = fs.os.replace
    def fail(*_args):
        raise OSError("simulated write failure")
    monkeypatch.setattr(fs.os, "replace", fail)
    with pytest.raises(OSError):
        transfer.import_config(target, data)
    assert not (target / "ext_data" / "auction").exists()
    monkeypatch.setattr(fs.os, "replace", replace)
    assert transfer.import_config(target, data)["id"] == "auction"


def test_duplicate_paths_and_future_versions_fail_closed(source):
    data, _ = transfer.export_config(source, "auction")
    files = entries(data)
    files["manifest.json"] = json.dumps({"format": "tick-config-backup", "version": 100})
    with pytest.raises(ValueError, match="格式或版本"):
        transfer.inspect_config(source, package(files))
    duplicate = io.BytesIO()
    with zipfile.ZipFile(duplicate, "w") as archive:
        for name, value in entries(data).items():
            archive.writestr(name, value)
        with pytest.warns(UserWarning):
            archive.writestr("ext_data/auction/config.json", b"{}")
    with pytest.raises(ValueError, match="重复"):
        transfer.inspect_config(source, duplicate.getvalue())


@pytest.mark.parametrize("bad_id", ["../escape", "a/b", "", "auction\n", "CON", "lpt1"])
def test_import_rename_rejects_unsafe_and_windows_reserved_ids(source, bad_id):
    data, _ = transfer.export_config(source, "auction")
    target = source / "recipient"
    with pytest.raises(ValueError):
        transfer.import_config(target, data, config_id=bad_id)
    assert not (target / "ext_data").exists()


def test_invalid_url_error_does_not_echo_embedded_credentials(source):
    store = ExtConfigStore(source)
    config = store.get("auction")
    config.pull.url = "https://user:private-test-key-123@[invalid"
    store.upsert(config)
    with pytest.raises(ValueError) as error:
        transfer.export_config(source, "auction")
    assert "private-test-key-123" not in str(error.value)


@pytest.mark.parametrize("link_kind", ["base", "table", "config"])
def test_export_and_import_do_not_follow_symlink_targets(source, link_kind):
    data, _ = transfer.export_config(source, "auction")
    target = source / "linked"
    target.mkdir()
    if link_kind == "base":
        (target / "ext_data").symlink_to(source / "ext_data", target_is_directory=True)
    else:
        (target / "ext_data").mkdir()
        table = target / "ext_data" / "auction"
        if link_kind == "table":
            table.symlink_to(source / "ext_data" / "auction", target_is_directory=True)
        else:
            table.mkdir()
            (table / "config.json").symlink_to(source / "ext_data" / "auction" / "config.json")
    with pytest.raises((ValueError, FileExistsError)):
        transfer.import_config(target, data)
    with pytest.raises(ValueError, match="符号链接"):
        transfer.export_config(target, "auction")
    assert ExtConfigStore(source).get("auction").pull.enabled is True


def test_http_permissions_preview_and_import_refresh(source, monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import auth, ext_data
    from app.services.auth import AuthStore
    from app.services.auth_access import install_auth_middleware
    from app.services.collector_proxy import collector_owns

    data, _ = transfer.export_config(source, "auction")
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=source))
    app.state.auth_store = AuthStore(source)
    app.state.auth_store.bootstrap("admin", "initial-secret")
    user = app.state.auth_store.create_user("guest", "temporary-secret", "guest", None)
    token, _ = app.state.auth_store.login("guest", "temporary-secret", "ip", "browser")
    guest_token, _ = app.state.auth_store.change_password(token, "temporary-secret", "changed-secret")
    app.include_router(auth.router)
    app.include_router(ext_data.router)
    install_auth_middleware(app)
    refreshed = []
    monkeypatch.setattr(ext_data, "_refresh_views", lambda _request: refreshed.append(True))
    files = {"file": ("auction.zip", data, "application/zip")}
    with TestClient(app) as client:
        assert client.get("/api/ext-data/auction/config/export").status_code == 401
        client.cookies.set(auth.COOKIE_NAME, guest_token)
        assert client.get("/api/ext-data/auction/config/export").status_code == 403
        for action in ("inspect", "import"):
            assert client.post(f"/api/ext-data/config/{action}", files=files).status_code == 403
        assert not (source / "users" / user["id"] / "ext_data").exists()
        client.cookies.clear()
        assert client.post("/api/auth/login", json={"username": "admin", "password": "initial-secret"}).status_code == 200
        response = client.get("/api/ext-data/auction/config/export")
        assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
        preview = client.post("/api/ext-data/config/inspect", files=files)
        assert preview.status_code == 200 and preview.json()["conflict"] is True
        assert not refreshed
        conflict = client.post("/api/ext-data/config/import", files=files)
        assert conflict.status_code == 409 and not refreshed
        response = client.post("/api/ext-data/config/import", files=files, data={"config_id": "auction_copy"})
        assert response.status_code == 200 and refreshed == [True]
        assert ExtConfigStore(source).get("auction_copy").pull.enabled is False
        assert client.post("/api/ext-data/config/inspect", files={"file": ("bad.zip", b"bad")}).status_code == 400
    assert collector_owns("POST", "/api/ext-data/config/import")
