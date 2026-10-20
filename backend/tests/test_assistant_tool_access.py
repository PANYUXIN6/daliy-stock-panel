"""助手工具权限在模型清单、动作确认和实际执行之间保持一致。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from app.config import settings
from app.custom.assistant import actions as assistant_actions
from app.custom.assistant import chat_service
from app.custom.assistant import tools as assistant_tools
from app.services import watchlist
from app.services.auth import AuthStore
from app.services.user_context import UserContext, user_scope

_GUEST_TOOLS = {
    "get_stock_quote", "get_stock_daily", "get_market_overview", "get_indices",
    "get_sector_rotation", "get_regime", "get_abnormal", "get_watchlist", "add_to_watchlist",
}
_PRO_TOOLS = _GUEST_TOOLS | {
    "get_stock_analysis", "get_financials", "list_strategies", "run_strategy",
    "list_factors", "get_factor_values",
}
_ADMIN_TOOLS = _PRO_TOOLS | {
    "list_data_capabilities", "check_data_coverage", "list_ext_tables", "query_ext_table",
    "get_sync_status", "run_backtest", "list_signals", "create_signal_strategy", "get_lots",
}


@pytest.fixture
def actors(tmp_path, monkeypatch):
    """真实账号及已完成首次改密的会话，所有数据只写入临时目录。"""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store = AuthStore(tmp_path)
    admin = store.bootstrap("admin", "initial-admin-secret")
    token, _ = store.login("admin", "initial-admin-secret", "test", "")
    owners = {"admin": UserContext(admin["id"], "admin", tmp_path, session_token=token)}
    for role in ("guest", "pro"):
        user = store.create_user(
            role, "temporary-secret", role,
            "2099-12-31" if role == "pro" else None,
            actor_id=admin["id"],
        )
        temporary, _ = store.login(role, "temporary-secret", "test", "")
        token, _ = store.change_password(temporary, "temporary-secret", "permanent-secret")
        owners[role] = UserContext(user["id"], role, tmp_path, session_token=token)
    return store, owners


@pytest.mark.parametrize("role,expected", [
    ("guest", _GUEST_TOOLS), ("pro", _PRO_TOOLS), ("admin", _ADMIN_TOOLS),
])
def test_model_only_sees_current_roles_tools(actors, role, expected):
    _, owners = actors
    with user_scope(owners[role]):
        schemas = assistant_tools.build_tool_schemas()
    assert {schema["function"]["name"] for schema in schemas} == expected
    assert len(schemas) == len(expected)


@pytest.mark.parametrize("name", ["get_financials", "list_factors"])
async def test_unauthorized_local_and_core_tools_do_not_reach_handlers(
    actors, monkeypatch, name,
):
    _, owners = actors

    def forbidden(*args, **kwargs):
        pytest.fail("未授权工具不得执行本地 handler")

    async def forbidden_core(*args, **kwargs):
        pytest.fail("未授权工具不得透传至核心目录")

    monkeypatch.setitem(assistant_tools._LOCAL_TOOLS, "get_financials", forbidden)
    monkeypatch.setattr(assistant_tools.tool_catalog, "execute_tool", forbidden_core)
    with user_scope(owners["guest"]):
        result = await assistant_tools.execute_assistant_tool(
            name, {}, assistant_tools.ToolContext.build(),
        )
    assert result["ok"] is False
    assert result["error"]


async def test_queued_local_tool_rechecks_latest_role(actors, monkeypatch):
    store, owners = actors
    real_to_thread = asyncio.to_thread

    async def downgrade_before_worker(func):
        store.update_user(owners["pro"].id, {"role": "guest"}, actor_id=owners["admin"].id)
        return await real_to_thread(func)

    def forbidden(*args, **kwargs):
        pytest.fail("排队期间已失去权限的工具不得执行")

    monkeypatch.setitem(assistant_tools._LOCAL_TOOLS, "get_financials", forbidden)
    monkeypatch.setattr(assistant_tools.asyncio, "to_thread", downgrade_before_worker)
    with user_scope(owners["pro"]):
        result = await assistant_tools.execute_assistant_tool(
            "get_financials", {}, assistant_tools.ToolContext.build(),
        )
    assert result["ok"] is False
    assert result["error"]


def _model_script(monkeypatch, tool_name: str, captures: list[dict[str, Any]]) -> None:
    """只有模型流是模拟的；权限、会话、确认与自选持久化均走真实实现。"""
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)

    async def fake_round(messages, tool_schemas, **kwargs):
        captures.append({
            "tools": {schema["function"]["name"] for schema in tool_schemas},
            "system": messages[0]["content"],
        })
        if len(captures) == 1:
            calls = [{
                "id": "call_1", "name": tool_name,
                "arguments": json.dumps({"symbol": "600519.SH", "note": "关注"}),
            }]
        else:
            calls = []
            yield {"type": "text", "delta": "已处理"}
        yield {"type": "round_end", "tool_calls": calls, "finish_reason": "stop"}

    monkeypatch.setattr(chat_service, "stream_openai_round", fake_round)


async def _chat_events(on_confirm: Callable | None = None) -> list[dict[str, Any]]:
    events = []
    async for line in chat_service.chat_stream(history=[{"role": "user", "content": "帮我操作"}]):
        event = json.loads(line)
        events.append(event)
        if event["type"] == "action_confirm":
            assert on_confirm is not None, "未授权动作不应出现确认卡"
            await on_confirm(event)
    assert events[-1]["type"] == "done"
    return events


async def test_hallucinated_admin_action_is_denied_before_confirmation(actors, monkeypatch):
    _, owners = actors
    captures = []
    _model_script(monkeypatch, "run_backtest", captures)

    async def forbidden(*args, **kwargs):
        pytest.fail("模型虚构管理员工具调用不能触达执行器")

    monkeypatch.setattr(assistant_tools, "execute_assistant_tool", forbidden)
    with user_scope(owners["guest"]):
        events = await _chat_events()
    assert not any(event["type"] == "action_confirm" for event in events)
    assert any(event["type"] == "tool_result" and event["ok"] is False for event in events)
    assert captures[0]["tools"] == _GUEST_TOOLS
    for name in _ADMIN_TOOLS - _GUEST_TOOLS:
        assert name not in captures[0]["system"]
    assert "add_to_watchlist" in captures[0]["system"]


@pytest.mark.parametrize("approve", [True, False])
async def test_guest_watchlist_requires_confirmation_and_only_writes_current_user(
    actors, monkeypatch, approve,
):
    _, owners = actors
    _model_script(monkeypatch, "add_to_watchlist", [])

    async def decide(event):
        assert watchlist.list_symbols() == []
        assert await assistant_actions.registry.resolve(event["call_id"], approve) is not None

    with user_scope(owners["guest"]):
        events = await _chat_events(decide)
        rows = watchlist.list_symbols()
    assert sum(event["type"] == "action_confirm" for event in events) == 1
    results = [event for event in events if event["type"] == "tool_result"]
    assert len(results) == 1 and results[0]["ok"] is approve
    assert [row["symbol"] for row in rows] == (["600519.SH"] if approve else [])
    for role in ("pro", "admin"):
        with user_scope(owners[role]):
            assert watchlist.list_symbols() == []


async def test_permission_revoked_while_waiting_for_confirmation_prevents_write_and_refreshes_schema(
    actors, monkeypatch,
):
    from app.custom.assistant.permissions import set_tool_permissions

    _, owners = actors
    captures = []
    _model_script(monkeypatch, "add_to_watchlist", captures)
    permissions = {name: "admin" for name in _ADMIN_TOOLS}
    permissions.update(dict.fromkeys(_PRO_TOOLS, "pro"))
    permissions.update(dict.fromkeys(_GUEST_TOOLS, "guest"))
    permissions["add_to_watchlist"] = "pro"

    async def revoke_and_approve(event):
        with user_scope(owners["admin"]):
            set_tool_permissions(permissions)
        assert await assistant_actions.registry.resolve(event["call_id"], True) is not None

    with user_scope(owners["guest"]):
        events = await _chat_events(revoke_and_approve)
        assert watchlist.list_symbols() == []
    assert any(event["type"] == "action_confirm" for event in events)
    assert any(event["type"] == "tool_result" and event["ok"] is False for event in events)
    assert len(captures) == 2
    assert captures[0]["tools"] == _GUEST_TOOLS
    assert captures[1]["tools"] == _GUEST_TOOLS - {"add_to_watchlist"}
    assert "add_to_watchlist" not in captures[1]["system"]


async def test_all_tools_can_be_disabled_without_sending_an_empty_tools_parameter(
    actors, monkeypatch,
):
    from types import SimpleNamespace

    from app.custom.assistant import streaming
    from app.custom.assistant.permissions import set_tool_permissions

    _, owners = actors
    with user_scope(owners["admin"]):
        set_tool_permissions(dict.fromkeys(_ADMIN_TOOLS, "admin"))

    class ModelStream:
        def __aiter__(self):
            async def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="当前没有可用工具"), finish_reason="stop",
                )])
            return chunks()

        async def close(self):
            pass

    captured = []

    async def create(**kwargs):
        captured.append(kwargs)
        return ModelStream()

    monkeypatch.setattr(streaming, "_client", lambda timeout: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    ))
    monkeypatch.setattr(streaming, "current_ai_model", lambda: "test-model")
    # 本用例只验证权限清单与空 tools 协议，成本边界由独立计费测试覆盖。
    monkeypatch.setattr(streaming, "prepare_metered_call", lambda **kwargs: None)
    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda provider=None: False)

    with user_scope(owners["guest"]):
        assert assistant_tools.build_tool_schemas() == []
        events = await _chat_events()

    assert len(captured) == 1
    assert "tools" not in captured[0]
    for name in _ADMIN_TOOLS:
        assert name not in captured[0]["messages"][0]["content"]
    assert any(event["type"] == "delta" for event in events)
    assert not any(event["type"] == "error" for event in events)


def test_individually_granted_tools_do_not_advertise_unavailable_directory_tools(actors):
    from app.custom.assistant.permissions import set_tool_permissions
    from app.custom.assistant.prompt import build_system_prompt

    _, owners = actors
    granted = {"run_strategy", "get_factor_values", "query_ext_table"}
    permissions = dict.fromkeys(_ADMIN_TOOLS, "admin")
    permissions.update(dict.fromkeys(granted, "guest"))
    with user_scope(owners["admin"]):
        set_tool_permissions(permissions)
    with user_scope(owners["guest"]):
        schemas = assistant_tools.build_tool_schemas()
        prompt = build_system_prompt(None, allowed_tools=granted)
    assert {schema["function"]["name"] for schema in schemas} == granted
    model_instructions = json.dumps(schemas) + prompt
    for name in _ADMIN_TOOLS - granted:
        assert name not in model_instructions
