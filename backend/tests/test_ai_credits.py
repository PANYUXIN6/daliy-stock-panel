"""真实 SQLite 台账验证额度、费用、并发和规则变更的业务不变量。"""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest

from app.services import auth
from app.services.ai_credit_policy import CreditPolicy
from app.services.ai_credits import CreditError, CreditStore
from app.services.user_context import UserContext


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    now = datetime.fromisoformat("2026-10-31T15:59:00+00:00").timestamp()
    monkeypatch.setattr(auth.time, "time", lambda: now)
    accounts = auth.AuthStore(tmp_path)
    admin = accounts.bootstrap("admin", "initial-password")
    token, _ = accounts.login("admin", "initial-password", "test", "")
    admin_owner = UserContext(admin["id"], "admin", tmp_path, session_token=token)
    guest = accounts.create_user("guest", "initial-password")
    token, _ = accounts.login("guest", "initial-password", "test", "")
    token, _ = accounts.change_password(token, "initial-password", "changed-password")
    guest_owner = UserContext(guest["id"], "guest", tmp_path, session_token=token)
    return CreditStore(tmp_path), accounts, admin_owner, guest_owner, now


def change_rules(ledger, **changes):
    store, _accounts, admin, _owner, _now = ledger
    current = store.get_policy()
    current["policy"].update(changes)
    return store.update_policy(
        admin, CreditPolicy.model_validate_json(json.dumps(current["policy"])), current["policy_version"],
    )


@pytest.fixture
def strict_ledger(ledger):
    change_rules(ledger, enforce_cost_limits=True)
    return ledger


def test_reserve_success_failure_and_idempotent_terminal(ledger):
    store, _accounts, _admin, owner, _now = ledger
    task = store.begin_task(owner, "assistant", "question-1")
    assert store.status(owner)["remaining_points"] == 57
    permit = task.reserve_call(1000, 1000)
    task.settle_call(permit.id, 10_000, prompt_tokens=1000, completion_tokens=100)
    task.finish("success")
    task.finish("failed")
    status = store.status(owner)
    assert status["used_points"] == 3
    assert status["reserved_points"] == 0
    assert status["cost_used_cny"] == "0.010000"
    failed = store.begin_task(owner, "financial_analysis", "question-2")
    call = failed.reserve_call(1000, 1000)
    failed.settle_call(call.id, 8_000, prompt_tokens=1000, completion_tokens=100)
    failed.finish("failed")
    assert store.status(owner)["remaining_points"] == 57
    assert store.status(owner)["cost_used_cny"] == "0.018000"
    with pytest.raises(CreditError) as duplicate:
        store.begin_task(owner, "financial_analysis", "question-2")
    assert duplicate.value.code == "AI_REQUEST_ALREADY_USED"


def test_opt_in_cost_limit_does_not_report_reserved_cost_as_actual_usage(strict_ledger):
    store, accounts, _admin, owner, _now = strict_ledger
    costs = 0
    for _ in range(5):
        task = store.begin_task(owner, "assistant")
        call = task.reserve_call(21_808, 100_000)
        costs += call.reserved_micros
        task.settle_call(call.id, None)
        task.finish("failed")
    status = store.status(owner)
    assert status["remaining_points"] == 60
    assert costs == 300_000
    assert status["cost_limit_cny"] == "1.500000"
    assert status["cost_used_cny"] == "0.000000"
    usage = store.usage_for_users([accounts.get_user(owner.id)])[owner.id]
    assert usage["unreported_calls"] == 5
    assert not status["features"]["assistant"]["available"]
    with pytest.raises(CreditError) as rejected:
        store.begin_task(owner, "assistant")
    assert rejected.value.code == "AI_COST_LIMIT"


def test_default_only_points_limit_high_cost_many_rounds_and_old_incidents(ledger):
    store, _accounts, _admin, owner, _now = ledger
    assert store.status(owner)["cost_limits_enabled"] is False
    change_rules(ledger, enforce_cost_limits=True)
    old = store.begin_task(owner, "assistant")
    old_call = old.reserve_call(1000, 1000)
    old.settle_call(old_call.id, old_call.reserved_micros + 1, prompt_tokens=1000, completion_tokens=1000)
    old.finish("failed")
    change_rules(ledger, enforce_cost_limits=False)
    task = store.begin_task(owner, "assistant")
    for _ in range(12):
        call = task.reserve_call(1_000_000, None)
        assert call.reserved_micros == 0
        assert call.max_tokens is None
        task.settle_call(call.id, 2_000_000, prompt_tokens=1_000_000, completion_tokens=0)
    task.finish("success")
    status = store.status(owner)
    assert status["remaining_points"] == 57
    assert status["cost_limit_cny"] is None
    assert status["cost_used_cny"] == "24.010001"
    assert status["features"]["assistant"]["available"]

    failed = store.begin_task(owner, "financial_analysis")
    call = failed.reserve_call(10_000_000, 100_000)
    assert call.max_tokens == 100_000
    failed.settle_call(call.id, None)
    failed.finish("failed")
    assert store.status(owner)["remaining_points"] == 57
    for _ in range(19):
        store.begin_task(owner, "assistant").finish("success")
    assert store.status(owner)["remaining_points"] == 0
    with pytest.raises(CreditError) as exhausted:
        store.begin_task(owner, "assistant")
    assert exhausted.value.status == 402
    assert exhausted.value.code == "AI_POINTS_EXHAUSTED"


def test_concurrent_requests_cannot_overspend_points(ledger):
    store, _accounts, admin, owner, _now = ledger
    rules = store.get_policy()
    rules["policy"]["monthly_points"]["guest"] = 6
    policy = CreditPolicy.model_validate_json(json.dumps(rules["policy"]))
    store.update_policy(admin, policy, rules["policy_version"])

    def attempt(_index):
        try:
            return store.begin_task(owner, "assistant")
        except CreditError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks = [task for task in pool.map(attempt, range(8)) if task is not None]
    assert len(tasks) == 2
    assert store.status(owner)["reserved_points"] == 6
    assert store.status(owner)["remaining_points"] == 0
    for task in tasks:
        task.finish("failed")
    assert store.status(owner)["remaining_points"] == 6


def test_upgrade_expiry_and_renewal_do_not_grant_again(ledger):
    store, accounts, _admin, owner, now = ledger
    store.begin_task(owner, "assistant").finish("success")
    accounts.update_user(owner.id, {"role": "pro", "pro_expires_on": "2099-12-31"})
    assert store.status(owner)["remaining_points"] == 3997
    store.begin_task(owner, "market_recap").finish("success")
    accounts.update_user(owner.id, {"pro_expires_on": "2099-12-30"})
    assert store.status(owner)["remaining_points"] == 3992
    accounts.update_user(owner.id, {"role": "guest"})
    assert store.status(owner)["remaining_points"] == 52
    # 到期的 Pro 使用 auth 的有效角色，不能相信请求开始前捕获的 owner.role。
    accounts.update_user(owner.id, {"role": "pro", "pro_expires_on": "2026-10-30"})
    forged_role = UserContext(owner.id, "pro", owner.data_dir, session_token=owner.session_token)
    assert store.status(forged_role, now=now)["role"] == "guest"
    assert store.status(forged_role, now=now)["remaining_points"] == 52


def test_beijing_month_and_late_refund_stay_in_original_period(ledger):
    store, _accounts, _admin, owner, now = ledger
    task = store.begin_task(owner, "stock_analysis", "late-refund", now=now)
    assert task.period == "2026-10"
    new_month = now + 120
    assert store.status(owner, now=new_month)["period"] == "2026-11"
    assert store.status(owner, now=new_month)["remaining_points"] == 60
    task.finish("failed")
    assert store.status(owner, now=new_month)["remaining_points"] == 60
    assert store.status(owner, now=now)["remaining_points"] == 60


def test_policy_changes_preserve_task_price_and_reject_stale_preview(ledger):
    store, _accounts, admin, owner, _now = ledger
    old = store.get_policy()
    task = store.begin_task(owner, "assistant", expected_policy_version=old["policy_version"])
    updated = old["policy"]
    updated["feature_points"]["assistant"] = 4
    updated["cost_per_point_micros"] = 30_000
    store.update_policy(admin, CreditPolicy.model_validate_json(json.dumps(updated)), old["policy_version"])
    assert task.policy.feature_points["assistant"] == 3
    assert task.policy.cost_per_point_micros == 20_000
    task.finish("success")
    assert store.status(owner)["used_points"] == 3
    assert store.status(owner)["features"]["assistant"]["points"] == 4
    with pytest.raises(CreditError) as stale:
        store.begin_task(owner, "assistant", expected_policy_version=old["policy_version"])
    assert stale.value.code == "AI_POLICY_CHANGED"
    with pytest.raises(CreditError):
        store.update_policy(admin, task.policy, old["policy_version"])


@pytest.mark.parametrize("strict", [False, True])
def test_recovery_refunds_points_without_inventing_actual_provider_usage(ledger, strict):
    if strict:
        change_rules(ledger, enforce_cost_limits=True)
    store, accounts, _admin, owner, _now = ledger
    task = store.begin_task(owner, "stock_analysis")
    task.reserve_call(1000, 1000)
    assert store.recover_interrupted() == 1
    assert store.recover_interrupted() == 0
    task.finish("success")
    assert store.status(owner)["remaining_points"] == 60
    assert store.status(owner)["cost_used_cny"] == "0.000000"
    usage = store.usage_for_users([accounts.get_user(owner.id)])[owner.id]
    assert usage["unreported_calls"] == usage["total_calls"] == 1


@pytest.mark.parametrize("reported", [False, True])
def test_cancel_before_call_refunds_after_call_consumes(ledger, reported):
    store, _accounts, _admin, owner, _now = ledger
    before = store.begin_task(owner, "assistant")
    before.finish("cancelled")
    assert store.status(owner)["remaining_points"] == 60
    after = store.begin_task(owner, "assistant")
    call = after.reserve_call(1000, 1000)
    if reported:
        after.settle_call(call.id, 2800, prompt_tokens=1000, completion_tokens=100)
    after.finish("cancelled")
    assert store.status(owner)["remaining_points"] == 57
    assert store.status(owner)["cost_used_cny"] == ("0.002800" if reported else "0.000000")


def test_opt_in_metering_incident_keeps_reported_cost_and_stops_future_calls(strict_ledger):
    store, _accounts, _admin, owner, _now = strict_ledger
    task = store.begin_task(owner, "assistant")
    call = task.reserve_call(1000, 1000)
    task.settle_call(call.id, call.reserved_micros + 1, prompt_tokens=1000, completion_tokens=1000)
    with pytest.raises(CreditError) as in_task:
        task.reserve_call(100, 100)
    assert in_task.value.code == "AI_METERING_UNAVAILABLE"
    task.finish("failed")
    assert store.status(owner)["cost_used_cny"] == f"0.{call.reserved_micros + 1:06d}"
    with pytest.raises(CreditError) as rejected:
        store.begin_task(owner, "assistant")
    assert rejected.value.code == "AI_METERING_UNAVAILABLE"


def test_opt_in_failure_usage_above_reservation_is_not_discarded(strict_ledger):
    from app.services.ai_credits import credit_task_scope
    from app.services.ai_metering import prepare_metered_call

    store, _accounts, _admin, owner, _now = strict_ledger
    task = store.begin_task(owner, "assistant")
    with credit_task_scope(task):
        permit = prepare_metered_call(
            model="deepseek-flash", base_url="https://api.deepseek.com",
            messages=[{"role": "user", "content": "test"}], tools=None, max_tokens=256,
        )
    permit.record_usage({"prompt_tokens": 100_000, "completion_tokens": 256})
    permit.settle(success=False)
    task.finish("failed")
    status = store.status(owner)
    assert status["remaining_points"] == 60
    assert status["cost_used_cny"] == "0.202048"
    with pytest.raises(CreditError) as rejected:
        store.begin_task(owner, "assistant")
    assert rejected.value.code == "AI_METERING_UNAVAILABLE"


def test_current_session_is_authority_and_admin_has_no_debit(ledger):
    store, accounts, admin, owner, _now = ledger
    admin_task = store.begin_task(admin, "assistant")
    call = admin_task.reserve_call(1000, 1000)
    admin_task.settle_call(call.id, 2800, prompt_tokens=1000, completion_tokens=100)
    admin_task.finish("success")
    assert store.status(admin)["unlimited"]
    assert store.status(admin)["used_points"] == 0
    assert store.status(admin)["remaining_points"] is None
    assert store.status(admin)["cost_used_cny"] == "0.002800"
    forged_admin = UserContext(owner.id, "admin", owner.data_dir, session_token=owner.session_token)
    task = store.begin_task(forged_admin, "assistant")
    assert task is not None
    task.finish("success")
    accounts.revoke_session(owner.session_token)
    with pytest.raises(auth.AuthError):
        store.begin_task(owner, "assistant")


@pytest.mark.parametrize("strict", [False, True])
def test_only_reported_and_priced_usage_counts_as_actual_cost(ledger, strict):
    if strict:
        change_rules(ledger, enforce_cost_limits=True)
    store, accounts, _admin, owner, _now = ledger
    for cost, prompt, completion in ((None, None, None), (None, 1000, 100), (2800, 1000, 100)):
        task = store.begin_task(owner, "assistant")
        call = task.reserve_call(1000, 1000)
        task.settle_call(call.id, cost, prompt_tokens=prompt, completion_tokens=completion)
        task.finish("success")
    assert store.status(owner)["cost_used_cny"] == "0.002800"
    usage = store.usage_for_users([accounts.get_user(owner.id)])[owner.id]
    assert usage["used_points"] == 9
    assert usage["total_tokens"] == 2200
    assert usage["total_calls"] == 3
    assert usage["unreported_calls"] == usage["unpriced_calls"] == 1
    assert usage["estimated_cost_cny"] == "0.002800"


def test_policy_persisted_without_cost_flag_defaults_to_points_only(ledger):
    store, _accounts, _admin, owner, _now = ledger
    old = store.get_policy()
    old["policy"].pop("enforce_cost_limits")
    with store._connection(True) as db:
        db.execute("UPDATE credit_policy SET rules=? WHERE singleton=1", (json.dumps(old["policy"]),))
    reopened = CreditStore(store.data_dir)
    assert reopened.get_policy()["policy_version"] == old["policy_version"]
    assert reopened.get_policy()["policy"]["enforce_cost_limits"] is False
    task = reopened.begin_task(owner, "stock_analysis")
    call = task.reserve_call(1_000_000, None)
    assert call.reserved_micros == 0
    assert call.max_tokens is None
    task.settle_call(call.id, 2_000_000, prompt_tokens=1_000_000, completion_tokens=0)
    task.finish("success")
    assert reopened.status(owner)["remaining_points"] == 55
    assert reopened.status(owner)["cost_used_cny"] == "2.000000"


def test_legacy_schema_migration_preserves_rows_without_claiming_reserved_cost(tmp_path):
    path = tmp_path / "auth" / "ai_credits.sqlite3"
    path.parent.mkdir()
    user_id = "a" * 32
    old_policy = CreditPolicy().model_dump(mode="json")
    old_policy.pop("enforce_cost_limits")
    # 旧版本表没有 usage_state，费用既可能来自上报，也可能只是预留。
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE credit_policy (singleton INTEGER PRIMARY KEY, version INTEGER NOT NULL, rules TEXT NOT NULL);
            CREATE TABLE credit_tasks (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, request_id TEXT NOT NULL,
                period TEXT NOT NULL, feature TEXT NOT NULL, points INTEGER NOT NULL,
                policy_version INTEGER NOT NULL, policy_json TEXT NOT NULL,
                budget_micros INTEGER NOT NULL, state TEXT NOT NULL,
                created_at REAL NOT NULL, finished_at REAL, UNIQUE(user_id,request_id)
            );
            CREATE TABLE credit_calls (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES credit_tasks(id),
                input_upper INTEGER NOT NULL, output_limit INTEGER NOT NULL,
                reserved_micros INTEGER NOT NULL, cost_micros INTEGER,
                prompt_tokens INTEGER, completion_tokens INTEGER,
                settled INTEGER NOT NULL DEFAULT 0
            );
        """)
        db.execute("INSERT INTO credit_policy VALUES(1,7,?)", (json.dumps(old_policy),))
        db.execute("""INSERT INTO credit_tasks VALUES(
            'legacy-task',?,'legacy-request','2026-10','assistant',3,7,?,60000,'success',1,2
        )""", (user_id, json.dumps(old_policy)))
        db.execute("INSERT INTO credit_calls VALUES('legacy-tokens','legacy-task',1000,1000,60000,60000,1000,100,1)")
        db.execute("INSERT INTO credit_calls VALUES('legacy-missing','legacy-task',1000,1000,60000,60000,NULL,NULL,1)")

    migrated = CreditStore(tmp_path)
    assert migrated.get_policy()["policy_version"] == 7
    assert migrated.get_policy()["policy"]["enforce_cost_limits"] is False
    with sqlite3.connect(path) as db:
        preserved = db.execute("""SELECT id,cost_micros,prompt_tokens,completion_tokens,usage_state
                                  FROM credit_calls ORDER BY id""").fetchall()
    assert preserved == [
        ("legacy-missing", 60000, None, None, "legacy"),
        ("legacy-tokens", 60000, 1000, 100, "legacy"),
    ]
    now = datetime.fromisoformat("2026-10-20T12:00:00+08:00").timestamp()
    users = [{"id": user_id, "role": "guest"}]
    usage = migrated.usage_for_users(users, now=now)[user_id]
    assert usage["used_points"] == 3 and usage["remaining_points"] == 57
    assert usage["total_tokens"] == 1100
    assert usage["total_calls"] == 2
    assert usage["unreported_calls"] == usage["unpriced_calls"] == 1
    assert usage["estimated_cost_cny"] == "0.000000"
    assert CreditStore(tmp_path).usage_for_users(users, now=now)[user_id] == usage


@pytest.mark.parametrize("change", [
    {"monthly_points": {"guest": 60}},
    {"feature_points": {"assistant": 3}},
    {"cost_per_point_micros": -1},
    {"model": "unpriced-model"},
])
def test_invalid_policy_is_rejected(change):
    rules = CreditPolicy().model_dump(mode="json")
    rules.update(change)
    with pytest.raises(ValueError):
        CreditPolicy.model_validate_json(json.dumps(rules))
