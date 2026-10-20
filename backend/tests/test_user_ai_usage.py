"""管理员用户列表批量展示当月 AI 用量，不借用被查看账号的会话。"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import users
from app.config import settings
from app.services import auth
from app.services.ai_credits import CreditStore, get_credit_store
from app.services.auth_access import install_auth_middleware
from app.services.user_context import UserContext


@pytest.fixture
def usage_env(tmp_path, monkeypatch):
    clock = {"now": datetime.fromisoformat("2026-10-31T15:55:00+00:00").timestamp()}
    monkeypatch.setattr(auth.time, "time", lambda: clock["now"])
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    accounts = auth.AuthStore(tmp_path)
    admin = accounts.bootstrap("admin", "initial-password")
    admin_token, _ = accounts.login("admin", "initial-password", "test", "")
    owners = {"admin": UserContext(admin["id"], "admin", tmp_path, session_token=admin_token)}
    for name, role in (("guest_alpha", "guest"), ("pro_beta", "pro")):
        clock["now"] += 1
        user = accounts.create_user(name, "temporary-password", role, "2026-12-31" if role == "pro" else None)
        token, _ = accounts.login(name, "temporary-password", "test", "")
        token, _ = accounts.change_password(token, "temporary-password", "changed-password")
        owners[name] = UserContext(user["id"], role, tmp_path, session_token=token)
    clock["now"] += 1
    empty = accounts.create_user("guest_empty", "temporary-password")

    app = FastAPI()
    app.state.auth_store = accounts
    app.include_router(users.router)
    install_auth_middleware(app)
    with TestClient(app) as client:
        client.cookies.set("tf_session", admin_token)
        yield SimpleNamespace(
            client=client, accounts=accounts, ledger=get_credit_store(tmp_path),
            owners=owners, empty=empty, clock=clock,
        )


def record_call(env, name, *, feature="assistant", prompt=1000, completion=100,
                cost=2800, outcome="success", usage_reported=None):
    task = env.ledger.begin_task(env.owners[name], feature)
    call = task.reserve_call(prompt, 1000)
    task.settle_call(
        call.id, cost, prompt_tokens=prompt, completion_tokens=completion,
        usage_reported=usage_reported,
    )
    task.finish(outcome)
    return task


def listed(env, search):
    response = env.client.get("/api/users", params={"search": search})
    assert response.status_code == 200
    return response.json()


def test_user_list_aggregates_points_tokens_and_cost_including_refunded_failures(usage_env):
    env = usage_env
    record_call(env, "guest_alpha", cost=10_000)
    record_call(env, "guest_alpha", feature="financial_analysis", prompt=2000, completion=200,
                cost=8000, outcome="failed")
    pending = env.ledger.begin_task(env.owners["guest_alpha"], "assistant")
    record_call(env, "pro_beta", feature="stock_analysis", prompt=4000, completion=600, cost=32_000)
    # 账号已经停用、会话被撤销，管理员仍应能读到汇总，不能代入该账号会话查询。
    env.accounts.update_user(env.owners["guest_alpha"].id, {"is_active": False})

    rows = {user["username"]: user for user in listed(env, "")["users"]}
    guest = rows["guest_alpha"]["ai_usage"]
    assert guest == {
        "period": "2026-10", "monthly_points": 60, "used_points": 3,
        "reserved_points": 3, "remaining_points": 54, "unlimited": False,
        "prompt_tokens": 3000, "completion_tokens": 300, "total_tokens": 3300,
        "estimated_cost_cny": "0.018000", "unreported_calls": 0,
        "unpriced_calls": 0, "total_calls": 2,
    }
    assert rows["guest_alpha"]["is_active"] is False
    pro = rows["pro_beta"]["ai_usage"]
    assert pro["monthly_points"] == 4000
    assert pro["used_points"] == 5
    assert pro["remaining_points"] == 3995
    assert pro["total_tokens"] == 4600
    assert pro["estimated_cost_cny"] == "0.032000"
    assert pending.period == "2026-10"


def test_search_pagination_queries_usage_once_for_only_the_returned_users(usage_env, monkeypatch):
    env = usage_env
    calls = []
    original = CreditStore.usage_for_users

    def observed(store, page, **kwargs):
        calls.append([user["id"] for user in page])
        return original(store, page, **kwargs)

    monkeypatch.setattr(CreditStore, "usage_for_users", observed)
    response = env.client.get("/api/users", params={"search": "guest_", "limit": 1, "offset": 1})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 2
    assert [user["username"] for user in payload["users"]] == ["guest_empty"]
    assert calls == [[env.empty["id"]]]
    usage = payload["users"][0]["ai_usage"]
    assert usage["monthly_points"] == usage["remaining_points"] == 60
    assert usage["used_points"] == usage["reserved_points"] == usage["total_calls"] == 0
    assert usage["prompt_tokens"] == usage["completion_tokens"] == usage["total_tokens"] == 0
    assert usage["unreported_calls"] == usage["unpriced_calls"] == 0
    assert usage["estimated_cost_cny"] == "0.000000"


def test_monthly_usage_uses_beijing_month_and_late_failure_stays_in_old_month(usage_env):
    env = usage_env
    record_call(env, "guest_alpha", prompt=1000, completion=100, cost=2800)
    old_pending = env.ledger.begin_task(env.owners["guest_alpha"], "assistant")
    old_call = old_pending.reserve_call(500, 1000)
    old_pending.settle_call(old_call.id, 1800, prompt_tokens=500, completion_tokens=100)
    env.clock["now"] = datetime.fromisoformat("2026-10-31T16:01:00+00:00").timestamp()
    fresh = listed(env, "guest_alpha")["users"][0]["ai_usage"]
    assert fresh["period"] == "2026-11"
    assert fresh["used_points"] == fresh["reserved_points"] == fresh["total_tokens"] == 0
    assert fresh["estimated_cost_cny"] == "0.000000"
    record_call(env, "guest_alpha", prompt=2000, completion=300, cost=6400)
    old_pending.finish("failed")
    current = listed(env, "guest_alpha")["users"][0]["ai_usage"]
    assert current["used_points"] == 3
    assert current["total_tokens"] == 2300
    assert current["estimated_cost_cny"] == "0.006400"


def test_unreported_and_unpriced_calls_are_explicit_in_admin_summary(usage_env):
    env = usage_env
    record_call(env, "guest_alpha", prompt=None, completion=None, cost=None, outcome="failed", usage_reported=False)
    record_call(env, "guest_alpha", prompt=300, completion=50, cost=None)
    usage = listed(env, "guest_alpha")["users"][0]["ai_usage"]
    assert usage["used_points"] == 3
    assert usage["prompt_tokens"] == 300
    assert usage["completion_tokens"] == 50
    assert usage["total_tokens"] == 350
    assert usage["total_calls"] == 2
    assert usage["unreported_calls"] == 1
    assert usage["unpriced_calls"] == 1
    assert usage["estimated_cost_cny"] == "0.000000"


def test_admin_usage_is_counted_without_debit_and_expired_pro_uses_guest_quota(usage_env):
    env = usage_env
    record_call(env, "admin", prompt=1000, completion=100, cost=2800)
    record_call(env, "pro_beta", feature="stock_analysis")
    env.accounts.update_user(env.owners["pro_beta"].id, {"pro_expires_on": "2026-10-30"})
    rows = {user["username"]: user for user in listed(env, "")["users"]}
    admin = rows["admin"]["ai_usage"]
    assert admin["unlimited"]
    assert admin["monthly_points"] is None and admin["remaining_points"] is None
    assert admin["used_points"] == 0
    assert admin["total_tokens"] == 1100
    assert admin["estimated_cost_cny"] == "0.002800"
    expired = rows["pro_beta"]
    assert expired["role"] == "guest"
    assert expired["ai_usage"]["monthly_points"] == 60
    assert expired["ai_usage"]["remaining_points"] == 55


@pytest.mark.parametrize("name,expected", [(None, 401), ("guest_alpha", 403), ("pro_beta", 403)])
def test_user_usage_is_admin_only(usage_env, name, expected):
    env = usage_env
    env.client.cookies.clear()
    if name is not None:
        env.client.cookies.set("tf_session", env.owners[name].session_token)
    response = env.client.get("/api/users")
    assert response.status_code == expected
    assert "users" not in response.json()
