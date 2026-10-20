"""成本预留必须覆盖两条流式链路、取消、重试与完整性失败。"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import openai
import pytest
from test_ai_metering import CreditTaskStub

from app.custom.assistant import streaming
from app.services import ai_metering, ai_provider
from app.services.ai_credit_policy import CreditPolicy
from app.services.ai_credits import CreditError, CreditStore, credit_task_scope
from app.services.auth import AuthStore
from app.services.user_context import UserContext, user_scope


def _chunk(*, content=None, finish_reason=None, usage=None, tools=None):
    choices = [] if usage else [SimpleNamespace(
        delta=SimpleNamespace(content=content, tool_calls=tools), finish_reason=finish_reason,
    )]
    return SimpleNamespace(choices=choices, usage=usage)


class ModelStream:
    def __init__(self, chunks, *, block=False):
        self.chunks = list(chunks)
        self.closed = False
        self.block = block
        self.waiting = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.chunks:
            chunk = self.chunks.pop(0)
            if isinstance(chunk, BaseException):
                raise chunk
            return chunk
        if self.block:
            self.waiting.set()
            await asyncio.Event().wait()
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


class ModelClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.chat = self.completions = self

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        result = self.responses.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


@pytest.fixture
def model_boundary(monkeypatch):
    task = CreditTaskStub(strict=True)
    monkeypatch.setattr(ai_metering, "current_credit_task", lambda: task)
    monkeypatch.setattr(ai_provider.secrets_store, "get_ai_key", lambda: "test-key")
    config = {"ai_model": "deepseek-flash", "ai_base_url": "https://api.deepseek.com/v1"}
    monkeypatch.setattr(
        ai_provider.secrets_store, "get_ai_config", lambda key, default="": config.get(key, default),
    )
    return task


def _model_client(monkeypatch, responses):
    client = ModelClient(responses)
    monkeypatch.setattr(ai_provider, "_openai_client", lambda *args, **kwargs: client)
    monkeypatch.setattr(streaming, "_client", lambda *args, **kwargs: client)
    return client


def _stream(assistant):
    messages = [{"role": "user", "content": "市场表现如何"}]
    if assistant:
        return streaming.stream_openai_round(messages, [])
    return ai_provider._stream_openai(
        messages, temperature=None, max_tokens=None, timeout=1, prefer_final_answer=True,
    )


@pytest.mark.parametrize("assistant", [False, True])
async def test_usage_only_tail_is_settled_and_request_has_cost_constraints(
    model_boundary, monkeypatch, assistant,
):
    stream = ModelStream([
        _chunk(content="完整回答", finish_reason="stop"),
        _chunk(usage={"prompt_tokens": 1000, "completion_tokens": 100}),
    ])
    client = _model_client(monkeypatch, [stream])
    result = [item async for item in _stream(assistant)]
    assert result
    assert stream.closed
    assert client.requests[0]["max_tokens"] == 512
    assert client.requests[0]["stream_options"] == {"include_usage": True}
    assert client.requests[0]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert model_boundary.settlements[0][1] == 2800


async def test_nonstream_completion_uses_the_same_reservation_and_actual_usage(
    model_boundary, monkeypatch,
):
    response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="完整回答"))],
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100),
    )
    client = _model_client(monkeypatch, [response])
    message = await ai_provider._run_openai_message_once(
        [{"role": "user", "content": "市场表现如何"}], temperature=None,
        max_tokens=None, timeout=1,
    )
    assert message.content == "完整回答"
    assert client.requests[0]["max_tokens"] == 512
    assert "stream_options" not in client.requests[0]
    assert model_boundary.settlements[0][1] == 2800


@pytest.mark.parametrize("assistant", [False, True])
async def test_truncated_content_and_usage_tail_are_still_a_failed_task(
    model_boundary, monkeypatch, assistant,
):
    stream = ModelStream([
        _chunk(content="未完成的报告", finish_reason="length"),
        _chunk(usage={"prompt_tokens": 1000, "completion_tokens": 512}),
    ])
    _model_client(monkeypatch, [stream])
    with pytest.raises(RuntimeError, match="输出达到长度上限"):
        _ = [item async for item in _stream(assistant)]
    assert stream.closed
    assert model_boundary.settlements[0][1] == 6096


@pytest.mark.parametrize("assistant", [False, True])
@pytest.mark.parametrize("finish_reason", [None, "aborted", "insufficient_system_resource", "content_filter"])
@pytest.mark.parametrize("strict", [False, True])
async def test_incomplete_finish_is_not_success_even_when_usage_is_available(
    model_boundary, monkeypatch, assistant, finish_reason, strict,
):
    model_boundary.policy.enforce_cost_limits = strict
    stream = ModelStream([
        _chunk(content="部分内容", finish_reason=finish_reason),
        _chunk(usage={"prompt_tokens": 1000, "completion_tokens": 100}),
    ])
    _model_client(monkeypatch, [stream])
    with pytest.raises(RuntimeError, match="生成提前结束"):
        _ = [item async for item in _stream(assistant)]
    assert model_boundary.settlements[0][1] == 2800


@pytest.mark.parametrize("strict", [False, True])
async def test_truncated_tool_arguments_do_not_produce_an_executable_round_end(model_boundary, monkeypatch, strict):
    model_boundary.policy.enforce_cost_limits = strict
    tool = SimpleNamespace(
        index=0, id="call", function=SimpleNamespace(name="get_stock_quote", arguments='{"symbols":['),
    )
    stream = ModelStream([
        _chunk(tools=[tool], finish_reason="length"),
        _chunk(usage={"prompt_tokens": 1000, "completion_tokens": 512}),
    ])
    _model_client(monkeypatch, [stream])
    events = []
    with pytest.raises(RuntimeError, match="输出达到长度上限"):
        async for event in _stream(True):
            events.append(event)
    assert not any(event["type"] == "round_end" for event in events)
    assert model_boundary.settlements[0][1] == 6096


@pytest.mark.parametrize("assistant", [False, True])
@pytest.mark.parametrize("ending", ["close", "cancel"])
async def test_disconnect_keeps_cost_reserved_and_closes_upstream(
    model_boundary, monkeypatch, assistant, ending,
):
    stream = ModelStream([_chunk(content="生成中")], block=True)
    _model_client(monkeypatch, [stream])
    generator = _stream(assistant)
    await anext(generator)
    if ending == "close":
        await generator.aclose()
    else:
        async def consume():
            return [item async for item in generator]
        consumer = asyncio.create_task(consume())
        await stream.waiting.wait()
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer
    assert stream.closed
    assert len(model_boundary.settlements) == 1
    assert model_boundary.settlements[0][1] is None


@pytest.mark.parametrize("assistant", [False, True])
@pytest.mark.parametrize("strict", [False, True])
async def test_missing_usage_records_unknown_without_discarding_complete_answer(
    model_boundary, monkeypatch, assistant, strict,
):
    model_boundary.policy.enforce_cost_limits = strict
    _model_client(monkeypatch, [ModelStream([_chunk(content="完整回答", finish_reason="stop")])])
    assert [item async for item in _stream(assistant)]
    assert model_boundary.settlements[0][1] is None


@pytest.mark.parametrize("assistant", [False, True])
async def test_compatibility_retry_reserves_each_network_attempt(model_boundary, monkeypatch, assistant):
    response = httpx.Response(
        400, request=httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions"),
    )
    error = openai.BadRequestError(
        "unsupported temperature", response=response,
        body={"message": "unsupported parameter: temperature"},
    )
    stream = ModelStream([
        _chunk(content="完整回答", finish_reason="stop"),
        _chunk(usage={"prompt_tokens": 1000, "completion_tokens": 100}),
    ])
    client = _model_client(monkeypatch, [error, stream])
    generator = (
        streaming.stream_openai_round([{"role": "user", "content": "问题"}], [])
        if assistant else ai_provider._stream_openai(
            [{"role": "user", "content": "问题"}], temperature=0.3,
            max_tokens=None, timeout=1, prefer_final_answer=True,
        )
    )
    _ = [item async for item in generator]
    assert len(client.requests) == len(model_boundary.reservations) == 2
    assert "temperature" not in client.requests[1]
    assert [settlement[1] for settlement in model_boundary.settlements] == [None, 2800]


@pytest.mark.parametrize("assistant", [False, True])
async def test_required_metering_parameters_cannot_be_removed_on_retry(
    model_boundary, monkeypatch, assistant,
):
    response = httpx.Response(
        400, request=httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions"),
    )
    error = openai.BadRequestError(
        "unsupported thinking", response=response, body={"message": "unsupported parameter: thinking"},
    )
    client = _model_client(monkeypatch, [error])
    with pytest.raises(Exception, match="thinking"):
        _ = [item async for item in _stream(assistant)]
    assert len(client.requests) == 1
    assert model_boundary.settlements[0][1] is None


async def test_metered_sdk_clients_disable_hidden_retries(model_boundary):
    clients = [ai_provider._openai_client("test-key", 1), streaming._client(1)]
    try:
        assert all(client.max_retries == 0 for client in clients)
    finally:
        for client in clients:
            await client.close()


@pytest.mark.parametrize("assistant", [False, True])
async def test_request_uses_the_priced_endpoint_even_if_configuration_changes(
    model_boundary, monkeypatch, assistant,
):
    config = {"ai_model": "deepseek-flash", "ai_base_url": "https://api.deepseek.com/v1"}
    monkeypatch.setattr(
        ai_provider.secrets_store, "get_ai_config", lambda key, default="": config.get(key, default),
    )
    prepare = ai_metering.prepare_metered_call

    def prepare_and_change_configuration(**kwargs):
        permit = prepare(**kwargs)
        config["ai_base_url"] = "https://unpriced.example/v1"
        return permit

    adapter = streaming if assistant else ai_provider
    monkeypatch.setattr(adapter, "prepare_metered_call", prepare_and_change_configuration)
    requests = []

    async def respond(request):
        requests.append(request)
        base = {"id": "completion", "object": "chat.completion.chunk", "created": 0, "model": "deepseek-flash"}
        chunks = [
            base | {"choices": [{"index": 0, "delta": {"content": "完整回答"}, "finish_reason": "stop"}]},
            base | {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}},
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    sdk = openai.AsyncOpenAI
    clients = []

    def make_client(**kwargs):
        client = sdk(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        clients.append(client)
        return client

    monkeypatch.setattr(openai, "AsyncOpenAI", make_client)
    monkeypatch.setattr(streaming, "AsyncOpenAI", make_client)
    try:
        _ = [item async for item in _stream(assistant)]
    finally:
        for client in clients:
            await client.close()
    assert len(requests) == 1
    assert requests[0].url.host == "api.deepseek.com"


@pytest.fixture
def real_credit_task(tmp_path, monkeypatch, request):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(ai_provider.secrets_store, "get_ai_key", lambda: "test-key")
    config = {"ai_model": "deepseek-flash", "ai_base_url": "https://api.deepseek.com/v1"}
    monkeypatch.setattr(
        ai_provider.secrets_store, "get_ai_config", lambda key, default="": config.get(key, default),
    )
    auth = AuthStore(tmp_path)
    admin = auth.bootstrap("admin", "admin-initial-secret")
    user = auth.create_user("guest", "temporary-secret", "guest", None, actor_id=admin["id"])
    temporary, _ = auth.login("guest", "temporary-secret", "test", "")
    token, _ = auth.change_password(temporary, "temporary-secret", "permanent-secret")
    owner = UserContext(user["id"], "guest", tmp_path, session_token=token)
    store = CreditStore(tmp_path)
    if getattr(request, "param", False):
        with store._connection(True) as db:
            db.execute("UPDATE credit_policy SET rules=?", (CreditPolicy(enforce_cost_limits=True).model_dump_json(),))
    with user_scope(owner):
        task = store.begin_task(owner, "assistant", "provider-integration")
        with credit_task_scope(task):
            yield task


@pytest.mark.parametrize("assistant", [False, True])
@pytest.mark.parametrize("real_credit_task", [True], indirect=True)
async def test_real_budget_refuses_call_when_remaining_output_is_below_minimum(
    real_credit_task, monkeypatch, assistant,
):
    first = real_credit_task.reserve_call(27_000, 256)
    real_credit_task.settle_call(first.id, None)
    client = _model_client(monkeypatch, [])
    with pytest.raises(CreditError, match="费用或调用轮次上限"):
        _ = [item async for item in _stream(assistant)]
    assert client.requests == []
    with real_credit_task.store._connection() as db:
        assert db.execute("SELECT COUNT(*) FROM credit_calls").fetchone()[0] == 1


async def test_multiple_assistant_rounds_share_one_persistent_task_budget(real_credit_task, monkeypatch):
    first = ModelStream([
        _chunk(content="第一轮", finish_reason="stop"),
        _chunk(usage={"prompt_tokens": 1000, "completion_tokens": 100}),
    ])
    second = ModelStream([
        _chunk(content="完整回答", finish_reason="stop"),
        _chunk(usage={"prompt_tokens": 2000, "completion_tokens": 100}),
    ])
    _model_client(monkeypatch, [first, second])
    _ = [item async for item in _stream(True)]
    _ = [item async for item in _stream(True)]
    with real_credit_task.store._connection() as db:
        calls, cost = db.execute("SELECT COUNT(*),SUM(cost_micros) FROM credit_calls").fetchone()
    assert calls == 2
    assert cost == 7600


@pytest.mark.parametrize("assistant", [False, True])
async def test_points_only_metering_does_not_change_generation_parameters(model_boundary, monkeypatch, assistant):
    model_boundary.policy.enforce_cost_limits = False
    client = _model_client(monkeypatch, [ModelStream([
        _chunk(content="兼容服务的完整回答", finish_reason="stop"),
        _chunk(usage={"prompt_tokens": 1000, "completion_tokens": 100}),
    ])])
    assert [item async for item in _stream(assistant)]
    assert "max_tokens" not in client.requests[0]
    assert "extra_body" not in client.requests[0]
    assert model_boundary.settlements[0][1] == 2800


async def test_points_only_nonstream_incomplete_response_fails_and_keeps_actual_usage(model_boundary, monkeypatch):
    model_boundary.policy.enforce_cost_limits = False
    response = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="aborted", message=SimpleNamespace(content="部分回答"))],
        usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=100),
    )
    _model_client(monkeypatch, [response])
    with pytest.raises(RuntimeError, match="生成提前结束"):
        await ai_provider._run_openai_message_once(
            [{"role": "user", "content": "市场表现如何"}], temperature=None,
            max_tokens=None, timeout=1,
        )
    assert model_boundary.settlements[0][1] == 2800


@pytest.mark.parametrize("assistant", [False, True])
async def test_optional_usage_request_falls_back_without_blocking_answer(model_boundary, monkeypatch, assistant):
    model_boundary.policy.enforce_cost_limits = False
    error = openai.BadRequestError(
        "unsupported stream_options", response=httpx.Response(
            400, request=httpx.Request("POST", "https://compatible.example/chat/completions"),
        ), body={"message": "unsupported parameter: stream_options"},
    )
    client = _model_client(monkeypatch, [error, ModelStream([
        _chunk(content="完整回答", finish_reason="stop"),
    ])])
    assert [item async for item in _stream(assistant)]
    assert len(client.requests) == len(model_boundary.reservations) == 2
    assert client.requests[0]["stream_options"] == {"include_usage": True}
    assert "stream_options" not in client.requests[1]
    assert [row[1] for row in model_boundary.settlements] == [None, None]
    assert all(not row[2]["usage_reported"] for row in model_boundary.settlements)


@pytest.mark.parametrize("assistant", [False, True])
async def test_unpriced_provider_keeps_reported_tokens(model_boundary, monkeypatch, assistant):
    model_boundary.policy.enforce_cost_limits = False
    config = {"ai_model": "other-model", "ai_base_url": "https://compatible.example/v1"}
    monkeypatch.setattr(ai_provider.secrets_store, "get_ai_config", lambda key, default="": config.get(key, default))
    _model_client(monkeypatch, [ModelStream([
        _chunk(content="完整回答", finish_reason="stop"),
        _chunk(usage={"prompt_tokens": 1000, "completion_tokens": 100}),
    ])])
    assert [item async for item in _stream(assistant)]
    assert model_boundary.settlements == [(
        "0", None, {"prompt_tokens": 1000, "completion_tokens": 100, "usage_reported": True},
    )]


@pytest.mark.parametrize("assistant", [False, True])
async def test_transient_retry_preserves_two_retries_and_records_each_attempt(model_boundary, monkeypatch, assistant):
    model_boundary.policy.enforce_cost_limits = False

    async def no_delay(_):
        pass

    monkeypatch.setattr(ai_metering.anyio, "sleep", no_delay)
    error = openai.InternalServerError(
        "busy", response=httpx.Response(500, request=httpx.Request("POST", "https://api.deepseek.com")), body=None,
    )
    client = _model_client(monkeypatch, [error, error, ModelStream([
        _chunk(content="完整回答", finish_reason="stop"),
        _chunk(usage={"prompt_tokens": 1000, "completion_tokens": 100}),
    ])])
    assert [item async for item in _stream(assistant)]
    assert len(client.requests) == len(model_boundary.reservations) == 3
    assert [row[1] for row in model_boundary.settlements] == [None, None, 2800]


@pytest.mark.parametrize("assistant", [False, True])
async def test_stream_failure_after_text_is_not_replayed(model_boundary, monkeypatch, assistant):
    model_boundary.policy.enforce_cost_limits = False
    error = openai.APIConnectionError(request=httpx.Request("POST", "https://api.deepseek.com"))
    client = _model_client(monkeypatch, [ModelStream([
        _chunk(content="已经展示"), error,
    ])])
    with pytest.raises((RuntimeError, openai.APIConnectionError)):
        _ = [item async for item in _stream(assistant)]
    assert len(client.requests) == 1
    assert model_boundary.settlements[0][1] is None


async def test_points_only_rounds_continue_beyond_previous_money_and_call_limits(real_credit_task, monkeypatch):
    first = real_credit_task.reserve_call(None, None)
    real_credit_task.settle_call(first.id, 100_000, prompt_tokens=10_000, completion_tokens=10_000, usage_reported=True)
    streams = [ModelStream([
        _chunk(content="完整回答", finish_reason="stop"),
        _chunk(usage={"prompt_tokens": 1000, "completion_tokens": 100}),
    ]) for _ in range(10)]
    client = _model_client(monkeypatch, streams)
    for _ in range(10):
        assert [item async for item in _stream(True)]
    assert all("max_tokens" not in request for request in client.requests)
    with real_credit_task.store._connection() as db:
        calls, cost = db.execute("SELECT COUNT(*),SUM(cost_micros) FROM credit_calls").fetchone()
    assert calls == 11
    assert cost == 128_000
