"""推理内容在流式/工具轮/会话边界保真，不绕过本地认证、额度和工具权限。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from openai.types.chat import ChatCompletionChunk, ChatCompletionMessage

from app.custom.assistant import chat_service, streaming
from app.custom.assistant import tools as assistant_tools
from app.custom.assistant.routes import HistoryMessage, build_router
from app.services import ai_provider

pytestmark = pytest.mark.usefixtures("authenticated_actor")


def chunk(delta, finish_reason=None):
    return ChatCompletionChunk.model_validate({
        "id": "test", "object": "chat.completion.chunk", "created": 0, "model": "deepseek-test",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    })


class Stream:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self):
        pass


@pytest.mark.parametrize("reasoning", ["先查询，再回答", "", None])
@pytest.mark.parametrize("tool_round", [False, True])
async def test_stream_preserves_reasoning_including_empty_strings(monkeypatch, reasoning, tool_round):
    captured = []

    async def create(**kwargs):
        captured.append(kwargs)
        chunks = [] if reasoning is None else [chunk({"reasoning_content": reasoning[:2]}),
                                               chunk({"reasoning_content": reasoning[2:]})]
        end = ({"tool_calls": [{"index": 0, "id": "one", "type": "function",
                                "function": {"name": "list_strategies", "arguments": "{}"}}]}
               if tool_round else {"content": "完成"})
        return Stream([*chunks, chunk(end, "tool_calls" if tool_round else "stop")])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(streaming, "_client", lambda timeout: client)
    monkeypatch.setattr(streaming, "current_ai_model", lambda: "deepseek-test")
    monkeypatch.setattr(streaming, "_build_kwargs", lambda _: {})
    events = [event async for event in streaming.stream_openai_round([
        {"role": "assistant", "content": "旧回答"}, {"role": "user", "content": "继续"},
    ], [])]
    assert captured[0]["messages"][0]["reasoning_content"] == ""
    end = events[-1]
    if reasoning is None:
        assert "reasoning_content" not in end
    else:
        assert end["reasoning_content"] == reasoning
        assert len(end["reasoning_context"]) == 64
    assert "reasoning_context" not in captured[0]["messages"][0]


@pytest.mark.parametrize("reasoning", ["模型推理", ""])
async def test_chat_tool_round_and_restored_followup_keep_reasoning(monkeypatch, reasoning):
    captured = []
    context = "a" * 64
    script = [
        {"reasoning_content": "工具推理", "tool_calls": [{"id": "one", "name": "list_strategies", "arguments": "{}"}]},
        {"reasoning_content": reasoning, "text": "完成"},
        {"text": "继续回答"},
    ]

    async def round_(messages, *args, **kwargs):
        captured.append([dict(m) for m in messages])
        step = script.pop(0)
        if "text" in step:
            yield {"type": "text", "delta": step["text"]}
        yield {"type": "round_end", "tool_calls": step.get("tool_calls", []),
               **({"reasoning_content": step["reasoning_content"], "reasoning_context": context}
                  if "reasoning_content" in step else {})}

    async def execute(*args):
        return {"ok": True, "result": {}}

    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda: False)
    monkeypatch.setattr(chat_service, "current_reasoning_context_key", lambda: context)
    monkeypatch.setattr(chat_service, "stream_openai_round", round_)
    monkeypatch.setattr(assistant_tools, "execute_assistant_tool", execute)
    user = {"role": "user", "content": "有哪些策略"}
    events = [json.loads(line) async for line in chat_service.chat_stream(history=[user])]
    assert not any(event["type"] == "error" for event in events)
    assert captured[1][-2]["reasoning_content"] == "工具推理"
    metadata = next(event for event in events if event["type"] == "assistant_context")
    assert metadata["reasoning_content"] == reasoning
    # HTTP模型序列化/反序列化后，字段仍包括显式空串。
    restored = HistoryMessage.model_validate({
        "role": "assistant", "content": "完成", **{key: metadata[key] for key in ("reasoning_content", "reasoning_context")},
    }).model_dump()
    followup = [json.loads(line) async for line in chat_service.chat_stream(history=[
        user, restored, {"role": "user", "content": "继续"},
    ])]
    assert not any(event["type"] == "error" for event in followup)
    assert captured[2][-2]["reasoning_content"] == reasoning


@pytest.mark.parametrize("reasoning", ["最终推理", ""])
def test_http_followup_roundtrips_reasoning_with_existing_credit_response(monkeypatch, tmp_path, reasoning):
    captured = []
    context = "a" * 64

    async def round_(messages, *args, **kwargs):
        captured.append([dict(message) for message in messages])
        yield {"type": "text", "delta": "回答"}
        yield {"type": "round_end", "tool_calls": [], "reasoning_content": reasoning,
               "reasoning_context": context}

    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda: False)
    monkeypatch.setattr(chat_service, "current_reasoning_context_key", lambda: context)
    monkeypatch.setattr(chat_service, "stream_openai_round", round_)
    monkeypatch.setattr("app.services.user_runtime.runtime_for", lambda request: SimpleNamespace(strategy_engine=None))
    app = FastAPI()
    app.include_router(build_router())
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    with TestClient(app) as client:
        user = {"role": "user", "content": "问题"}
        first = client.post("/api/custom/assistant/chat", json={"messages": [user]})
        assert first.status_code == 200
        events = [json.loads(line) for line in first.text.splitlines() if line]
        metadata = next(event for event in events if event["type"] == "assistant_context")
        second = client.post("/api/custom/assistant/chat", json={"messages": [user, {
            "role": "assistant", "content": "回答", "reasoning_content": metadata["reasoning_content"],
            "reasoning_context": metadata["reasoning_context"],
        }, {"role": "user", "content": "继续"}]})
        assert second.status_code == 200
        assert captured[1][-2]["reasoning_content"] == reasoning
        assert not any(json.loads(line).get("type") == "error" for line in second.text.splitlines() if line)


@pytest.mark.parametrize("reasoning", ["思考内容", ""])
async def test_nonstream_tool_and_final_round_preserve_reasoning(monkeypatch, reasoning):
    replies = [ChatCompletionMessage.model_validate({
        "role": "assistant", "content": None, "reasoning_content": reasoning,
        "tool_calls": [{"id": "one", "type": "function", "function": {"name": "test", "arguments": "{}"}}],
    }), ChatCompletionMessage.model_validate({"role": "assistant", "content": "完成", "reasoning_content": reasoning})]

    async def once(*args, **kwargs):
        return replies.pop(0)

    async def execute(*args):
        return {"ok": True}

    monkeypatch.setattr(ai_provider, "is_codex_cli_provider", lambda: False)
    monkeypatch.setattr(ai_provider, "_run_openai_message_once", once)
    monkeypatch.setattr(ai_provider, "_check_input_budget", lambda *args, **kwargs: None)
    result = await ai_provider.generate_ai_text_with_tools(
        [{"role": "user", "content": "hi"}], [{"type": "function", "function": {"name": "test"}}],
        execute_tool=execute,
    )
    assert result[1]["reasoning_content"] == result[-1]["reasoning_content"] == reasoning


def test_reasoning_identity_isolated_by_provider_protocol_endpoint_and_model(monkeypatch):
    monkeypatch.setattr(ai_provider, "current_ai_provider", lambda: "openai_compat")
    profile = {"model": "model-a", "base_url": "https://example.test/v1"}
    key = ai_provider.current_reasoning_context_key(**profile)
    for changed in ({"model": "model-b"}, {"base_url": "https://other.test/v1"}, {"protocol": "responses"}):
        assert ai_provider.current_reasoning_context_key(**{**profile, **changed}) != key
    monkeypatch.setattr(ai_provider, "current_ai_provider", lambda: "openai")
    assert ai_provider.current_reasoning_context_key(**profile) != key
    history = [{"role": "assistant", "content": "回答", "reasoning_content": "old trace", "reasoning_context": key}]
    prepared = ai_provider.prepare_tool_messages(history, **profile)
    assert prepared == [{"role": "assistant", "content": "回答"}]
    assert history[0]["reasoning_content"] == "old trace"
    assert "reasoning_context" not in prepared[0]


def test_legacy_history_and_unattributed_reasoning_are_safe():
    old = [{"role": "user", "content": "问题"}, {"role": "assistant", "content": "旧回答"}]
    assert chat_service._truncate_history(old, reasoning_context="current")[0] == old
    unknown = [{"role": "assistant", "content": "回答", "reasoning_content": "未知来源"}]
    assert chat_service._truncate_history(unknown, reasoning_context="current")[0] == [{"role": "assistant", "content": "回答"}]
    assert ai_provider.prepare_tool_messages(old, model="deepseek-test", base_url="https://api.deepseek.com")[1]["reasoning_content"] == ""
    assert ai_provider.prepare_tool_messages(old, model="plain", base_url="https://example.test") == old


async def test_tool_reasoning_counts_toward_next_round_budget(monkeypatch):
    calls = []

    async def round_(*args, **kwargs):
        calls.append(True)
        yield {"type": "round_end", "reasoning_content": "推理" * 100,
               "tool_calls": [{"id": "one", "name": "list_strategies", "arguments": "{}"}]}

    async def execute(*args):
        return {"ok": True, "result": {}}

    monkeypatch.setattr(chat_service, "ai_configured", lambda: True)
    monkeypatch.setattr(chat_service, "is_codex_cli_provider", lambda: False)
    monkeypatch.setattr(chat_service, "build_system_prompt", lambda *args, **kwargs: "s")
    monkeypatch.setattr(chat_service, "current_ai_context_window", lambda: 100)
    monkeypatch.setattr(chat_service, "current_ai_max_output_tokens", lambda: 10)
    monkeypatch.setattr(chat_service, "stream_openai_round", round_)
    monkeypatch.setattr(assistant_tools, "execute_assistant_tool", execute)
    events = [json.loads(line) async for line in chat_service.chat_stream(history=[{"role": "user", "content": "hi"}])]
    assert len(calls) == 1
    assert next(event for event in events if event["type"] == "error")["kind"] == "input_too_long"
    monkeypatch.setattr(ai_provider, "current_ai_context_window", lambda: 100)
    with pytest.raises(ValueError, match="输入过长"):
        ai_provider._check_input_budget([{"role": "assistant", "content": "a", "reasoning_content": "推理" * 100}], max_tokens=10)
