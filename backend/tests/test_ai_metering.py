"""实际调用前的信任边界与供应商累计用量结算。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services import ai_metering
from app.services.ai_credits import CreditError
from app.services.user_context import UserContext, user_scope


class CreditTaskStub:
    def __init__(self, *, strict=False):
        self.policy = SimpleNamespace(
            enforce_cost_limits=strict,
            allowed_models=("deepseek-flash",),
            input_micros_per_million=2_000_000,
            cached_input_micros_per_million=40_000,
            output_micros_per_million=8_000_000,
        )
        self.reservations = []
        self.settlements = []

    def reserve_call(self, input_tokens_upper, requested_max_tokens):
        reservation = SimpleNamespace(
            id=str(len(self.reservations)),
            max_tokens=512 if self.policy.enforce_cost_limits else requested_max_tokens,
            reserved_micros=10_000 if self.policy.enforce_cost_limits else 0,
        )
        self.reservations.append((input_tokens_upper, requested_max_tokens, reservation))
        return reservation

    def settle_call(self, call_id, actual_micros, **usage):
        self.settlements.append((call_id, actual_micros, usage))


@pytest.fixture
def credit_task(monkeypatch):
    task = CreditTaskStub()
    monkeypatch.setattr(ai_metering, "current_credit_task", lambda: task)
    return task


def _prepare(**overrides):
    kwargs = {
        "model": "deepseek-flash", "base_url": "https://api.deepseek.com/v1",
        "messages": [{"role": "user", "content": "分析一下"}],
        "tools": None, "max_tokens": None,
    }
    return ai_metering.prepare_metered_call(**(kwargs | overrides))


@pytest.mark.parametrize("overrides", [
    {"base_url": "https://api.deepseek.com.example/v1"},
    {"base_url": "http://api.deepseek.com/v1"},
    {"base_url": "https://api.deepseek.com:8443/v1"},
    {"base_url": "https://api.deepseek.com/proxy/v1"},
    {"model": "unpriced-model"},
    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]},
    {"messages": [{"role": "user", "content": "hello", "audio": {"data": "unmetered"}}]},
    {"tools": [{"type": "web_search"}]},
])
def test_unpriced_or_nontext_requests_are_refused_before_reservation(credit_task, overrides):
    credit_task.policy.enforce_cost_limits = True
    with pytest.raises(CreditError):
        _prepare(**overrides)
    assert credit_task.reservations == []


def test_non_admin_cannot_call_an_unmetered_model(monkeypatch, tmp_path):
    monkeypatch.setattr(ai_metering, "current_credit_task", lambda: None)
    with (
        user_scope(UserContext("a" * 32, "guest", tmp_path)),
        pytest.raises(CreditError, match="未绑定点数任务"),
    ):
        _prepare()
    with user_scope(UserContext("b" * 32, "admin", tmp_path)):
        assert _prepare() is None


def test_all_message_and_tool_bytes_are_included_in_the_upper_bound():
    messages = [{"role": "user", "content": "📈营收增长"}]
    base = ai_metering.text_input_tokens_upper(messages, None)
    history = [*messages, {"role": "tool", "tool_call_id": "call", "content": "结果" * 100}]
    tools = [{"type": "function", "function": {"name": "financials", "description": "财务" * 100}}]
    expanded = ai_metering.text_input_tokens_upper(history, tools)
    assert base >= len("📈营收增长".encode())
    assert expanded >= base + len(("结果" * 100 + "财务" * 100).encode())


def test_usage_counts_cache_and_reasoning_once_and_settles_idempotently(credit_task):
    permit = _prepare()
    usage = {
        "prompt_tokens": 1000, "completion_tokens": 100,
        "prompt_cache_hit_tokens": 200, "prompt_cache_miss_tokens": 800,
        "completion_tokens_details": {"reasoning_tokens": 80},
    }
    permit.record_usage(usage)
    permit.record_usage(SimpleNamespace(**usage))
    permit.settle(success=True)
    permit.settle(success=True)
    assert credit_task.settlements == [(
        "0", 2408, {"prompt_tokens": 1000, "completion_tokens": 100, "usage_reported": True},
    )]


@pytest.mark.parametrize("usage", [
    None,
    {"prompt_tokens": 10},
    {"prompt_tokens": -1, "completion_tokens": 20},
    {"prompt_tokens": True, "completion_tokens": 20},
    {"prompt_tokens": 10, "completion_tokens": 20, "prompt_cache_hit_tokens": 11},
    {"prompt_tokens": 10, "completion_tokens": 20, "prompt_cache_hit_tokens": 5, "prompt_cache_miss_tokens": 10},
])
def test_missing_or_invalid_usage_does_not_invent_a_cost(credit_task, usage):
    permit = _prepare()
    permit.record_usage(usage)
    permit.settle(success=True)
    assert credit_task.settlements[0][1] is None


def test_invalid_usage_tail_does_not_reuse_partial_usage(credit_task):
    permit = _prepare()
    permit.record_usage({"prompt_tokens": 10, "completion_tokens": 20})
    permit.record_usage({"prompt_tokens": 10})
    permit.settle(success=True)
    assert credit_task.settlements[0][1] is None


def test_usage_cache_details_and_rounding_up(credit_task):
    permit = _prepare()
    permit.record_usage({
        "prompt_tokens": 1, "completion_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 1},
    })
    permit.settle(success=True)
    assert credit_task.settlements[0][1] == 1


@pytest.mark.parametrize("overrides", [
    {"base_url": "https://unpriced.example/v1"},
    {"model": "unpriced-model"},
    {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}]}]},
])
def test_unpriced_requests_keep_usage_without_blocking_generation(credit_task, overrides):
    permit = _prepare(**overrides)
    assert len(credit_task.reservations) == 1
    permit.record_usage({"prompt_tokens": 1000, "completion_tokens": 100})
    permit.settle(success=True)
    assert credit_task.settlements == [(
        "0", None, {"prompt_tokens": 1000, "completion_tokens": 100, "usage_reported": True},
    )]


def test_default_metering_preserves_output_and_thinking_configuration(credit_task):
    permit = _prepare(max_tokens=16_384)
    kwargs = {"max_tokens": 16_384, "extra_body": {"thinking": {"type": "enabled"}}, "temperature": 0.6}
    assert permit.request_kwargs(kwargs, stream=False) == kwargs
    assert permit.max_tokens == 16_384
    assert "max_tokens" not in _prepare().request_kwargs({}, stream=True)


def test_failed_generation_retains_reported_actual_usage(credit_task):
    permit = _prepare()
    permit.record_usage({"prompt_tokens": 1000, "completion_tokens": 100})
    permit.settle(success=False)
    assert credit_task.settlements[0] == (
        "0", 2800, {"prompt_tokens": 1000, "completion_tokens": 100, "usage_reported": True},
    )
