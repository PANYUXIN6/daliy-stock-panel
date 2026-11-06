import subprocess
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth, users
from app.services.ai_credits import CreditError, get_credit_store
from app.services.auth import AuthError, AuthStore
from app.services.auth_access import install_auth_middleware
from app.services.user_context import UserContext, user_data_dir, user_scope
from app.services.user_deletion import delete_user


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store = AuthStore(tmp_path)
    admin = store.bootstrap("admin", "initial-password")
    rows = [store.create_user(name, "initial-password", "pro") for name in ("alice", "other")]
    owners = []
    for row in rows:
        token, _ = store.login(row["username"], "initial-password", row["username"], "browser")
        token, _ = store.change_password(token, "initial-password", "changed-password")
        owners.append(UserContext(row["id"], "pro", tmp_path, session_token=token))
    app = FastAPI()
    app.state.auth_store = store
    app.include_router(auth.router)
    app.include_router(users.router)
    install_auth_middleware(app)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "initial-password"})
        yield SimpleNamespace(store=store, admin=admin, owners=owners, client=client)


def test_delete_removes_all_private_files_ledger_sessions_and_events_only_for_target(accounts, tmp_path):
    from app.factors import registry as factors
    from app.services import preferences, strategy_cache, strategy_run_queue, watchlist
    from app.services.user_runtime import UserRuntimeRegistry
    from app.strategy import config, custom_signals, monitor, paper

    registry = UserRuntimeRegistry(SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))))
    accounts.client.app.state.user_runtimes = registry
    ledger = get_credit_store(tmp_path)
    tasks = []
    for owner in accounts.owners:
        with user_scope(owner):
            watchlist.add("600000.SH")
            preferences.save({"nav_order": [owner.id]})
            config.save_override(tmp_path, "demo", {"params": {"owner": owner.id}})
            config.load_override(tmp_path, "demo")
            strategy_cache.write_cache(tmp_path, "2026-11-06", {"demo": {"total": 1, "rows": []}})
            strategy_cache.read_cache(tmp_path)
            custom_signals.save_one(tmp_path, {"id": "private", "name": owner.id, "conditions": [{"left": "close", "op": ">", "right": "10"}]})
            custom_signals.signal_names(tmp_path)
            custom_signals.load_intraday_all(tmp_path)
            factors.register_factor(factors.FactorSpec(id="uf_private", label=owner.id, group="custom", formula_text="close", kind="custom"))
            monitor._watchlist_groups_snapshot()
            handle = strategy_run_queue.MANAGER.get_or_submit(("deletion-test",), ["demo"], lambda run, owner=owner: run.complete("demo", {"owner": owner.id}))
            handle.future.result(timeout=5)
            paper.create_account(tmp_path, 100000, name="模拟盘")
            runtime = registry.get(owner)
            runtime.mining_manager.store.create({}, {}, run_id="saved-run")
            for filename in ("strategies/composite/demo.json", "user_data/reports/report.json", "research/analysis.json"):
                path = user_data_dir() / filename
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(owner.id)
        task = ledger.begin_task(owner, "assistant")
        call = task.reserve_call(100, 100)
        task.settle_call(call.id, 12, prompt_tokens=100, completion_tokens=10)
        tasks.append((task, call))
        with ledger._connection(True) as db:
            db.execute("INSERT INTO credit_incidents VALUES(?,?,?)", (owner.id, task.period, "test"))
    target, other = accounts.owners
    for owner in accounts.owners:
        with user_scope(owner):
            preferences.load()
    # Failed logins from before account creation also belong to the deleted username.
    with accounts.store._connection(True) as db:
        db.execute("INSERT INTO login_events(user_id,username,occurred,ip,user_agent,success,reason) VALUES(NULL,' ALICE ',?,'test','',0,'invalid_credentials')", (datetime.now(UTC).timestamp(),))
    shared = tmp_path / "market.json"
    shared.write_text("shared-market")
    other_dir = user_data_dir(tmp_path, other.id)
    (user_data_dir(tmp_path, target.id) / "outside-link").symlink_to(other_dir, target_is_directory=True)
    before = {str(p.relative_to(other_dir)): p.read_bytes() for p in other_dir.rglob("*") if p.is_file()}
    response = accounts.client.delete(f"/api/users/{target.id}")
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True}
    assert accounts.store.get_user(target.id) is None
    assert accounts.store.session(target.session_token) is None
    assert accounts.store.session(other.session_token) is not None
    assert not user_data_dir(tmp_path, target.id).exists()
    assert shared.read_text() == "shared-market"
    assert before == {str(p.relative_to(other_dir)): p.read_bytes() for p in other_dir.rglob("*") if p.is_file()}
    assert target.id not in registry._runtimes
    assert other.id in registry._runtimes
    for owner, remains in ((target, False), (other, True)):
        root = user_data_dir(tmp_path, owner.id)
        key = str(tmp_path.resolve() / owner.id)
        assert (str(root / "user_data" / "preferences.json") in preferences._cache) is remains
        assert (str(root / "user_data" / "strategy_overrides" / "demo.json") in config._override_cache) is remains
        assert (root / "user_data" / "strategy_cache.json" in strategy_cache._memo) is remains
        assert (root / "user_data" / "custom_signals" in custom_signals._names_cache) is remains
        assert (root / "user_data" / "custom_signals" in custom_signals._intraday_cache) is remains
        assert (key in factors._USER_REGISTRIES) is remains
        assert (key in monitor._group_cache) is remains
        assert any(item[0][1] == owner.id for item in strategy_run_queue.MANAGER._completed.values()) is remains
    assert all(event["username"].strip().lower() != "alice" for event in accounts.store.login_events()["events"])
    with ledger._connection() as db:
        assert db.execute("SELECT user_id FROM credit_tasks").fetchall()[0][0] == other.id
        assert db.execute("SELECT COUNT(*) FROM credit_tasks").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM credit_calls").fetchone()[0] == 1
        assert db.execute("SELECT user_id FROM credit_incidents").fetchall()[0][0] == other.id
    task, call = tasks[0]
    task.finish("success")
    task.settle_call(call.id, 20, prompt_tokens=200, completion_tokens=10)
    with pytest.raises(CreditError):
        task.reserve_call(100, 100)
    with pytest.raises(AuthError):
        ledger.begin_task(target, "assistant")
    with pytest.raises(AuthError), user_scope(target):
        pytest.fail("deleted ownership must not reopen")
    recreated = accounts.client.post("/api/users", json={"username": "alice", "password": "new-password"})
    assert recreated.status_code == 200
    assert recreated.json()["user"]["id"] != target.id


def test_delete_authorization_admin_protection_origin_and_missing_account(accounts, tmp_path):
    target = accounts.owners[0]
    before = accounts.store.list_users()
    assert accounts.client.delete(f"/api/users/{target.id}", headers={"Origin": "https://evil.example"}).status_code == 403
    assert accounts.client.delete(f"/api/users/{accounts.admin['id']}").status_code == 409
    assert accounts.client.delete("/api/users/" + "f" * 32).status_code == 404
    accounts.client.cookies.set("tf_session", target.session_token)
    assert accounts.client.delete(f"/api/users/{accounts.owners[1].id}").status_code == 403
    accounts.client.cookies.clear()
    assert accounts.client.delete(f"/api/users/{target.id}").status_code == 401
    with pytest.raises(AuthError, match="仅管理员"):
        delete_user(accounts.store, accounts.owners[1].id, actor_id=target.id)
    assert accounts.store.list_users() == before


@pytest.mark.parametrize("failure", ["files", "ledger"])
def test_partial_cleanup_is_disabled_retryable_and_never_reports_success(accounts, tmp_path, monkeypatch, failure):
    import app.services.user_deletion as deletion

    target = accounts.owners[0]
    directory = user_data_dir(tmp_path, target.id)
    directory.mkdir(parents=True)
    (directory / "personal.json").write_text("personal")
    def fail(*args, **kwargs):
        raise PermissionError("private system path must not leak")
    with monkeypatch.context() as patch:
        if failure == "files":
            patch.setattr(deletion.shutil, "rmtree", fail)
        else:
            patch.setattr(get_credit_store(tmp_path), "delete_user_data", fail)
        response = accounts.client.delete(f"/api/users/{target.id}")
        assert response.status_code == 503
        assert "private system path" not in response.text
    pending = AuthStore(tmp_path).get_user(target.id)
    assert pending["deletion_pending"] and not pending["is_active"]
    assert accounts.store.session(target.session_token) is None
    assert accounts.client.patch(f"/api/users/{target.id}", json={"is_active": True}).status_code == 409
    assert accounts.client.delete(f"/api/users/{target.id}").status_code == 200
    assert not directory.exists()
    assert accounts.store.get_user(target.id) is None


@pytest.mark.parametrize("link_parent", [False, True])
def test_delete_rejects_redirected_user_directory(accounts, tmp_path, link_parent):
    target = accounts.owners[0]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("safe")
    users_dir = tmp_path / "users"
    # API authentication itself creates no personal user directories.
    if link_parent:
        users_dir.symlink_to(outside, target_is_directory=True)
    else:
        users_dir.mkdir()
        (users_dir / target.id).symlink_to(outside, target_is_directory=True)
    before = accounts.store.get_user(target.id)
    assert accounts.client.delete(f"/api/users/{target.id}").status_code == 503
    assert (outside / "keep").read_text() == "safe"
    assert accounts.store.get_user(target.id) == before


def test_delete_drains_inflight_write_and_rejects_later_scopes(accounts, tmp_path):
    target = accounts.owners[0]
    started, release = threading.Event(), threading.Event()
    def writer():
        with user_scope(target):
            path = user_data_dir() / "last-write.json"
            started.set()
            assert release.wait(5)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("last-write")
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(writer)
        assert started.wait(5)
        original = accounts.store.prepare_user_deletion
        def prepare(*args, **kwargs):
            result = original(*args, **kwargs)
            release.set()
            return result
        accounts.store.prepare_user_deletion = prepare
        removing = pool.submit(delete_user, accounts.store, target.id, actor_id=accounts.admin["id"])
        writing.result(timeout=5)
        removing.result(timeout=5)
    assert not user_data_dir(tmp_path, target.id).exists()
    with pytest.raises(AuthError), user_scope(target):
        pytest.fail("stale owner must not write after deletion")


def test_cross_process_active_operation_blocks_cleanup_until_retry(accounts, tmp_path, monkeypatch):
    import app.services.user_deletion as deletion

    target = accounts.owners[0]
    code = """
import sys
from pathlib import Path
from app.services.user_context import UserContext, user_scope, user_data_dir
with user_scope(UserContext(sys.argv[2], 'pro', Path(sys.argv[1]))):
    path = user_data_dir() / 'worker.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('worker-data')
    print('ready', flush=True)
    sys.stdin.readline()
"""
    monkeypatch.setattr(deletion, "_DRAIN_SECONDS", 0)
    with subprocess.Popen([sys.executable, "-c", code, str(tmp_path), target.id], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as worker:
        try:
            assert worker.stdout.readline().strip() == "ready"
            response = accounts.client.delete(f"/api/users/{target.id}")
            assert response.status_code == 409
            assert (user_data_dir(tmp_path, target.id) / "worker.json").exists()
        finally:
            worker.communicate("exit\n", timeout=5)
    assert accounts.client.delete(f"/api/users/{target.id}").status_code == 200
    assert not user_data_dir(tmp_path, target.id).exists()


def test_permanent_pro_can_be_created_and_switched_to_and_from_expiring_pro(accounts):
    client, store = accounts.client, accounts.store
    created = client.post("/api/users", json={"username": "forever", "password": "temporary-password", "role": "pro", "pro_expires_on": None})
    assert created.status_code == 200
    uid = created.json()["user"]["id"]
    future = datetime(2099, 1, 1, tzinfo=UTC).timestamp()
    assert AuthStore(store.data_dir).get_user(uid, now=future)["role"] == "pro"
    assert client.patch(f"/api/users/{uid}", json={"pro_expires_on": "2026-10-13"}).status_code == 200
    boundary = datetime(2026, 10, 13, 16, tzinfo=UTC).timestamp()
    assert store.get_user(uid, now=boundary - 1)["role"] == "pro"
    assert store.get_user(uid, now=boundary)["role"] == "guest"
    restored = client.patch(f"/api/users/{uid}", json={"pro_expires_on": None})
    assert restored.json()["user"]["role"] == "pro"
    assert store.get_user(uid, now=future)["role"] == "pro"
    assert client.patch(f"/api/users/{uid}", json={"pro_expires_on": ""}).status_code == 400
    assert client.patch(f"/api/users/{uid}", json={"pro_expires_on": "2026-02-30"}).status_code == 400
    client.patch(f"/api/users/{uid}", json={"role": "guest"})
    assert store.get_user(uid)["role"] == "guest"
    assert store.get_user(uid)["pro_expires_on"] is None


def test_existing_accounts_survive_concurrent_schema_upgrade(accounts):
    before = accounts.store.list_users()
    with accounts.store._connection(True) as db:
        db.execute("ALTER TABLE users DROP COLUMN deletion_pending")
    with ThreadPoolExecutor(max_workers=2) as pool:
        stores = list(pool.map(lambda _: AuthStore(accounts.store.data_dir), range(2)))
    for store in stores:
        assert store.list_users() == before
        assert store.session(accounts.owners[0].session_token) is not None


def test_user_deletion_is_owned_by_collector():
    from app.services.collector_proxy import collector_owns
    assert collector_owns("DELETE", "/api/users/a")
    assert not collector_owns("PATCH", "/api/users/a")
    assert not collector_owns("GET", "/api/users")


def test_queued_mining_job_does_not_start_or_recreate_data_after_deletion(accounts, tmp_path, monkeypatch):
    import app.services.mining_manager as mining
    from app.services.user_runtime import UserRuntimeRegistry

    queued = []
    future = Future()
    # Model a scheduler that has claimed the task but has not entered its owner scope.
    future.set_running_or_notify_cancel()
    def defer(work):
        queued.append(work)
        return future
    monkeypatch.setattr(mining, "submit_research", defer)
    registry = UserRuntimeRegistry(SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))))
    owner = accounts.owners[0]
    with user_scope(owner):
        runtime = registry.get(owner)
        runtime.mining_manager.start({}, {}, run_id="queued")
    accounts.store.prepare_user_deletion(owner.id, actor_id=accounts.admin["id"])
    queued[0]()
    future.set_result(None)
    assert runtime.mining_manager._futures == {}
    delete_user(accounts.store, owner.id, actor_id=accounts.admin["id"], runtimes=registry)
    queued[0]()  # A stale scheduler callback must remain harmless after removal.
    assert not user_data_dir(tmp_path, owner.id).exists()
    assert accounts.store.get_user(owner.id) is None


def test_pending_deletion_does_not_block_other_users_cache_maintenance(accounts, tmp_path):
    from app.services import strategy_cache

    owner, other = accounts.owners
    for user in accounts.owners:
        with user_scope(user):
            strategy_cache.write_cache(tmp_path, "2026-11-06", {"demo": {"total": 1, "rows": []}})
    accounts.store.prepare_user_deletion(owner.id, actor_id=accounts.admin["id"])
    strategy_cache.clear_all_user_caches(tmp_path)
    with user_scope(other):
        assert strategy_cache.read_cache(tmp_path) is None
    assert accounts.store.get_user(owner.id)["deletion_pending"]
