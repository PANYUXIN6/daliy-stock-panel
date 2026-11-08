from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.api.strategy import _render_composite_code
from app.factors import registry, store
from app.services import strategy_bundle as bundles
from app.services.package_archive import build_archive, read_archive
from app.services.user_context import UserContext, user_data_dir, user_scope
from app.strategy import config, custom_signals
from app.strategy.engine import StrategyEngine

pytestmark = pytest.mark.usefixtures("private_owner")


def engine_for(root):
    trusted = root / "trusted"
    trusted.mkdir(exist_ok=True)
    (trusted / "leaf.py").write_text("""import polars as pl
META = {"id":"leaf", "name":"维护者策略", "asset_types":["stock"], "timeframes":["1d"]}
EXECUTION_BACKEND = "polars_expr"
def filter(df, params):
    return pl.lit(True)
""")
    personal = user_data_dir(root) / "strategies" / "composite"
    personal.mkdir(parents=True, exist_ok=True)
    return StrategyEngine(strategy_dirs=[trusted, personal], declarative_dirs=[personal]), personal


def seed(root, *, signal=True):
    engine, directory = engine_for(root)
    (directory / "composite_pack.py").write_text(
        _render_composite_code(
            "composite_pack",
            "叠加包",
            "只含声明",
            [{"strategy_id": "leaf", "weight": 1.0}],
            "union",
            0,
        )
    )
    store.save_one(
        root, {"id": "uf_base", "label": "基础", "kind": "custom", "formula": "close * 2"}
    )
    store.save_one(
        root, {"id": "uf_chain", "label": "链式", "kind": "custom", "formula": "uf_base + volume"}
    )
    override = {"params": {"threshold": 3}, "scoring": {"uf_chain": 1.0}}
    if signal:
        custom_signals.save_one(
            root,
            {
                "id": "ready",
                "name": "准备",
                "kind": "entry",
                "conditions": [{"left": "uf_chain", "op": ">", "right": 1}],
            },
        )
        override["entry_signals"] = ["csg_ready"]
    config.save_override(root, "leaf", override)
    engine.reload()
    return engine, directory


def edit_packet(data, *, remove=None, add=None):
    entries = read_archive(data)
    manifest = json.loads(entries.pop("manifest.json"))
    if remove:
        entries.pop(remove)
    if add:
        entries.update(add)
    manifest["entries"] = [
        {
            "path": name,
            "kind": bundles._PATHS[str(__import__("pathlib").PurePosixPath(name).parent)],
        }
        for name in entries
    ]
    entries["manifest.json"] = json.dumps(manifest).encode()
    return build_archive(entries)


def test_roundtrip_dependencies_personal_settings_and_hot_reload(tmp_path):
    engine, _ = seed(tmp_path)
    payload, filename, summary = bundles.build_bundle(tmp_path, engine, "composite_pack")
    entries = read_archive(payload)
    assert filename.endswith(".zip")
    assert not any(name.endswith(".py") for name in entries)
    assert summary["references"] == ["leaf"]
    assert summary["counts"]["custom_factors"] == 2
    with user_scope(UserContext("b" * 32, "admin", tmp_path)):
        target_engine, directory = engine_for(tmp_path)
        before_revision = registry.private_registry_revision()
        preview = bundles.inspect_bundle(payload, tmp_path, target_engine)
        assert preview["can_apply"], preview
        assert not (directory / "composite_pack.py").exists()
        assert registry.get_factor("uf_chain") is None
        assert registry.private_registry_revision() == before_revision
        refreshed = []
        result = bundles.apply_bundle(
            payload, tmp_path, target_engine, refresh=lambda: refreshed.append(True)
        )
        assert result["imported"] == 5
        assert target_engine.get("composite_pack").execution_backend == "composite"
        assert config.load_override(tmp_path, "leaf")["params"] == {"threshold": 3}
        assert registry.get_factor("uf_chain") is not None
        assert custom_signals.load_all(tmp_path)[0]["id"] == "ready"
        assert refreshed == [True]
        # 重复包只复用相同内容，不覆盖。
        assert bundles.apply_bundle(payload, tmp_path, target_engine)["imported"] == 0


def test_missing_dependencies_conflicts_and_python_are_fail_closed(tmp_path):
    engine, directory = seed(tmp_path, signal=False)
    payload, _, _ = bundles.build_bundle(tmp_path, engine, "composite_pack")
    with user_scope(UserContext("b" * 32, "guest", tmp_path)):
        target, _ = engine_for(tmp_path)
        missing = edit_packet(payload, remove="user_data/custom_factors/uf_base.json")
        preview = bundles.inspect_bundle(missing, tmp_path, target)
        assert not preview["can_apply"]
        with pytest.raises(bundles.StrategyBundleError):
            bundles.apply_bundle(missing, tmp_path, target)
        config.save_override(tmp_path, "leaf", {"params": {"threshold": 99}})
        preview = bundles.inspect_bundle(payload, tmp_path, target)
        assert any(item["action"] == "conflict" for item in preview["entries"])
        with pytest.raises(bundles.StrategyBundleError):
            bundles.apply_bundle(payload, tmp_path, target, allow_overwrite=True)
        assert config.load_override(tmp_path, "leaf")["params"]["threshold"] == 99
        with pytest.raises(bundles.StrategyBundleError, match="源码"):
            bundles.inspect_bundle(
                build_archive({"strategies/custom/evil.py": b"raise Exception()"}), tmp_path, target
            )
    assert (directory / "composite_pack.py").exists()


def test_signal_role_and_other_owner_are_not_bypassed(tmp_path):
    engine, _ = seed(tmp_path)
    payload, _, _ = bundles.build_bundle(tmp_path, engine, "composite_pack")
    with user_scope(UserContext("b" * 32, "guest", tmp_path)):
        target, _ = engine_for(tmp_path)
        assert custom_signals.load_all(tmp_path) == []
        with pytest.raises(bundles.StrategyBundleError, match="管理员"):
            bundles.inspect_bundle(payload, tmp_path, target)
        with pytest.raises(ValueError, match="unknown"):
            bundles.build_bundle(tmp_path, target, "composite_pack")


@pytest.mark.parametrize("failure", ["reload", "refresh", "factor_trial"])
def test_failed_import_removes_every_created_resource(tmp_path, monkeypatch, failure):
    engine, _ = seed(tmp_path, signal=False)
    payload, _, _ = bundles.build_bundle(tmp_path, engine, "composite_pack")
    with user_scope(UserContext("b" * 32, "admin", tmp_path)):
        target, _ = engine_for(tmp_path)
        previous = target.reload
        calls = []

        def reload():
            calls.append(True)
            if failure == "reload" and len(calls) == 1:
                raise RuntimeError("synthetic reload failure")
            previous()

        def fail(*args):
            raise RuntimeError("synthetic failure")

        monkeypatch.setattr(target, "reload", reload)
        with pytest.raises(bundles.StrategyBundleError, match="撤销"):
            bundles.apply_bundle(
                payload,
                tmp_path,
                target,
                refresh=fail if failure == "refresh" else None,
                validate_factor=fail if failure == "factor_trial" else None,
            )
        assert list(user_data_dir(tmp_path).rglob("*.json")) == []
        assert list((user_data_dir(tmp_path) / "strategies").rglob("*.py")) == []
        assert registry.get_factor("uf_chain") is None
        assert not target.has("composite_pack")


def test_symlink_target_and_nested_composite_rejected(tmp_path):
    engine, _ = seed(tmp_path, signal=False)
    payload, _, _ = bundles.build_bundle(tmp_path, engine, "composite_pack")
    with user_scope(UserContext("b" * 32, "admin", tmp_path)):
        target, folder = engine_for(tmp_path)
        external = tmp_path / "outside.py"
        external.write_text("untouched")
        (folder / "composite_pack.py").symlink_to(external)
        with pytest.raises(bundles.StrategyBundleError):
            bundles.inspect_bundle(payload, tmp_path, target)
        assert external.read_text() == "untouched"
        (folder / "composite_pack.py").unlink()
        entries = read_archive(payload)
        doc = json.loads(entries["strategies/composite/composite_pack.json"])
        doc["meta"]["children"] = [{"strategy_id": "composite_pack", "weight": 1.0}]
        invalid = edit_packet(
            payload, add={"strategies/composite/composite_pack.json": json.dumps(doc).encode()}
        )
        assert not bundles.inspect_bundle(invalid, tmp_path, target)["can_apply"]


async def test_isolated_registry_task_thread_and_exception_restore(tmp_path):
    before = registry.private_registry_revision()

    def spec(fid):
        return registry.FactorSpec(
            id=fid, label=fid, group="test", formula_text="close", kind="custom"
        )

    async def preview(fid, other):
        with registry.isolated_registry():
            registry.register_factor(spec(fid))
            await asyncio.sleep(0)
            assert registry.get_factor(fid) is not None
            assert registry.get_factor(other) is None
            assert await asyncio.to_thread(registry.get_factor, fid) is not None

    await asyncio.gather(preview("uf_one", "uf_two"), preview("uf_two", "uf_one"))
    assert registry.get_factor("uf_one") is registry.get_factor("uf_two") is None
    with pytest.raises(RuntimeError), registry.isolated_registry():
        registry.register_factor(spec("uf_throw"))
        raise RuntimeError("preview failed")
    assert registry.get_factor("uf_throw") is None
    assert registry.private_registry_revision() == before


def test_http_auth_upload_preview_and_no_overwrite(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.auth import COOKIE_NAME
    from app.api.strategy import router
    from app.services.auth import AuthStore
    from app.services.auth_access import install_auth_middleware

    auth = AuthStore(tmp_path)
    admin = auth.bootstrap("bundleadmin", "initial-password")
    token, _ = auth.login("bundleadmin", "initial-password", "test", "")
    token, _ = auth.change_password(token, "initial-password", "permanent-password")
    with user_scope(UserContext(admin["id"], "admin", tmp_path)):
        engine, _ = seed(tmp_path)
    runtime = SimpleNamespace(strategy_engine=engine)
    app = FastAPI()
    app.state.auth_store = auth
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.user_runtimes = SimpleNamespace(get=lambda owner: runtime)
    app.include_router(router)
    install_auth_middleware(app)
    with TestClient(app) as client:
        assert client.get("/api/strategies/composite_pack/bundle").status_code == 401
        client.cookies.set(COOKIE_NAME, token)
        export = client.get("/api/strategies/composite_pack/bundle")
        assert export.status_code == 200
        upload = {"file": ("strategy.zip", export.content, "application/zip")}
        preview = client.post("/api/strategies/bundle/inspect", files=upload)
        assert preview.status_code == 200 and preview.json()["can_apply"]
        applied = client.post("/api/strategies/bundle/apply", files=upload)
        assert applied.status_code == 200 and applied.json()["imported"] == 0
        assert (
            client.post(
                "/api/strategies/bundle/apply", files=upload, data={"allow_overwrite": "true"}
            ).status_code
            == 400
        )
        auth.create_user("guest", "guest-initial-password", "guest")
        guest, _ = auth.login("guest", "guest-initial-password", "test", "")
        guest, _ = auth.change_password(guest, "guest-initial-password", "guest-permanent-password")
        client.cookies.set(COOKIE_NAME, guest)
        assert client.post("/api/strategies/bundle/inspect", files=upload).status_code == 403


def test_role_change_during_import_rolls_back_signal_resources(tmp_path, monkeypatch):
    engine, _ = seed(tmp_path)
    payload, _, _ = bundles.build_bundle(tmp_path, engine, "composite_pack")
    role = {"value": "admin"}
    monkeypatch.setattr(bundles, "require_current_session", lambda: {"role": role["value"]})
    with user_scope(UserContext("b" * 32, "admin", tmp_path, session_token="captured-session")):
        target, _ = engine_for(tmp_path)

        def change_role(definition):
            role["value"] = "guest"

        with pytest.raises(bundles.StrategyBundlePermissionError, match="管理员"):
            bundles.apply_bundle(payload, tmp_path, target, validate_factor=change_role)
        assert list(user_data_dir(tmp_path).rglob("*.json")) == []
        assert not target.has("composite_pack")


def test_nonfinite_composite_weight_cannot_enter_runtime(tmp_path):
    engine, _ = seed(tmp_path, signal=False)
    payload, _, _ = bundles.build_bundle(tmp_path, engine, "composite_pack")
    entries = read_archive(payload)
    declaration = json.loads(entries["strategies/composite/composite_pack.json"])
    declaration["meta"]["children"][0]["weight"] = "nan"
    invalid = edit_packet(
        payload, add={"strategies/composite/composite_pack.json": json.dumps(declaration).encode()}
    )
    with user_scope(UserContext("b" * 32, "admin", tmp_path)):
        target, _ = engine_for(tmp_path)
        with pytest.raises(bundles.StrategyBundleError, match="有限"):
            bundles.inspect_bundle(invalid, tmp_path, target)
