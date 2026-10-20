"""模型用量观测；默认只限制业务点数，不干预模型生成参数。"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import anyio

from app.services.ai_credit_policy import FLASH_MODELS
from app.services.ai_credits import CreditError, current_credit_task
from app.services.user_context import current_user

if TYPE_CHECKING:
    from app.services.ai_credits import CallReservation, CreditTask


def metering_active() -> bool:
    """有点数任务时，由适配层记录包括重试在内的每次请求。"""
    return current_credit_task() is not None


async def retry_metered_request(exc: Exception, attempt: int) -> bool:
    """保留 SDK 默认两次首包前重试，每次尝试分别记账。

    SDK 内部重试不暴露单次 usage，因此受控调用关闭隐藏重试后在此执行。
    收到流式响应后的异常不能重试，否则会重复输出已经展示的正文。
    """
    if attempt >= 2 or not metering_active():
        return False
    from openai import APIConnectionError

    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {})
    directive = headers.get("x-should-retry")
    status = getattr(exc, "status_code", None)
    retryable = isinstance(exc, APIConnectionError) or status in {408, 409, 429} or (
        isinstance(status, int) and status >= 500
    )
    if directive == "false" or (directive != "true" and not retryable):
        return False
    delay = 0.5 * 2 ** attempt
    try:
        retry_after = float(headers.get("retry-after-ms")) / 1000 if headers.get("retry-after-ms") else float(headers.get("retry-after", "0"))
        if 0 < retry_after <= 60:
            delay = retry_after
    except (TypeError, ValueError):
        pass
    await anyio.sleep(delay)
    return True


def _validate_destination(model: str, base_url: str) -> None:
    parsed = urlsplit(base_url.strip())
    if (
        parsed.scheme != "https" or parsed.hostname != "api.deepseek.com"
        or parsed.netloc.lower() not in {"api.deepseek.com", "api.deepseek.com:443"}
        or parsed.path.rstrip("/") not in {"", "/v1"}
        or parsed.query or parsed.fragment
    ):
        raise CreditError("试运营点数仅支持 DeepSeek 官方接口，请联系管理员检查 AI 配置。")
    if model.strip().lower() not in FLASH_MODELS:
        raise CreditError("当前模型尚未配置点数成本规则，请联系管理员切换已支持的 Flash 模型。")


def credit_configuration_reason() -> str | None:
    """可用性只依赖配置；缺少价格不能阻断已有模型。"""
    from app.services.ai_provider import ai_configured

    if not ai_configured():
        return "AI 服务尚未配置，请联系管理员"
    return None


def require_complete_finish(finish_reason: str, *, allow_tools: bool = False) -> None:
    """部分文本或 usage 都不能把截断、中止或缺失终止帧变成成功。"""
    if finish_reason in {"length", "max_tokens", "max_output_tokens"}:
        raise RuntimeError("AI 输出达到长度上限, 内容不完整; 请缩小问题范围后重试")
    if finish_reason != "stop" and not (allow_tools and finish_reason == "tool_calls"):
        raise RuntimeError("AI 生成提前结束，内容不完整，请稍后重试或缩小问题范围。")


def _value(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _token_count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def text_input_tokens_upper(
    messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]] | None,
) -> int:
    """Flash 文本字节上界，另预留角色/工具模板开销，不使用平均字/token 比。

    字节级 BPE 的普通文本 token 不会多于 UTF-8 字节数。序列化整个消息和
    schema，覆盖历史、工具参数、工具结果与字段；另预留模板控制符和工具
    定义包装。图片等输入的计量规则不同，必须在请求发出前拒绝。
    """
    for message in messages:
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise CreditError("试运营 AI 点数暂只支持纯文本，请移除图片或其他附件。")
        if set(message) - {"role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content"}:
            raise CreditError("AI 消息含有未配置成本规则的字段，请使用纯文本提问。")
    if any(tool.get("type") != "function" for tool in tools or ()):
        raise CreditError("该 AI 工具尚未配置成本规则，请联系管理员。")
    try:
        payload = json.dumps(
            {"messages": list(messages), "tools": list(tools or ())},
            ensure_ascii=False, separators=(",", ":"), allow_nan=False,
        )
        size = len(payload.encode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CreditError("AI 请求包含无法计量的内容，请缩短问题后重试。") from exc
    return size + 1024 + len(messages) * 64 + len(tools or ()) * 256


@dataclass
class MeteredCall:
    task: CreditTask
    reservation: CallReservation
    priced: bool = True
    _actual_micros: int | None = field(default=None, init=False)
    _prompt_tokens: int | None = field(default=None, init=False)
    _completion_tokens: int | None = field(default=None, init=False)
    _settled: bool = field(default=False, init=False)
    _usage_reported: bool = field(default=False, init=False)

    @property
    def strict(self) -> bool:
        return getattr(self.task.policy, "enforce_cost_limits", False)

    @property
    def max_tokens(self) -> int | None:
        return self.reservation.max_tokens

    def request_kwargs(self, kwargs: dict[str, Any], *, stream: bool, include_usage: bool = True) -> dict[str, Any]:
        result = dict(kwargs)
        if self.strict:
            result["max_tokens"] = self.max_tokens
            result["extra_body"] = {"thinking": {"type": "disabled"}}
        if stream and include_usage:
            result["stream_options"] = {**result.get("stream_options", {}), "include_usage": True}
        return result

    def record_usage(self, usage: Any) -> None:
        """每帧 usage 是该请求的累计量；最终值替换旧值，不能累加帧。"""
        if usage is None:
            return
        # 无效尾帧不能继续采用较早的部分统计。
        self._actual_micros = self._prompt_tokens = self._completion_tokens = None
        self._usage_reported = False
        prompt = _token_count(_value(usage, "prompt_tokens"))
        completion = _token_count(_value(usage, "completion_tokens"))
        if prompt is None or completion is None:
            return
        self._prompt_tokens, self._completion_tokens = prompt, completion
        self._usage_reported = True
        if not self.priced:
            return
        hit_raw = _value(usage, "prompt_cache_hit_tokens")
        miss_raw = _value(usage, "prompt_cache_miss_tokens")
        details = _value(usage, "prompt_tokens_details")
        if hit_raw is None:
            hit_raw = _value(details, "cached_tokens")
        if hit_raw is None and miss_raw is None:
            hit, miss = 0, prompt
        elif hit_raw is None:
            miss = _token_count(miss_raw)
            if miss is None or miss > prompt:
                return
            hit = prompt - miss
        else:
            hit = _token_count(hit_raw)
            if hit is None or hit > prompt:
                return
            miss = prompt - hit if miss_raw is None else _token_count(miss_raw)
            if miss is None or hit + miss != prompt:
                return
        policy = self.task.policy
        numerator = (
            miss * policy.input_micros_per_million
            + hit * policy.cached_input_micros_per_million
            + completion * policy.output_micros_per_million
        )
        # completion_tokens 已含 reasoning_tokens，不能重复收费。
        self._actual_micros = (numerator + 999_999) // 1_000_000

    def settle(self, *, success: bool) -> None:
        if self._settled:
            return
        # 已收到的真实 usage 在任务失败、截断和取消时同样有成本。
        # 未返回 usage 时保持未知，不能把预留预算冒充实际金额。
        self.task.settle_call(
            self.reservation.id,
            self._actual_micros,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            usage_reported=self._usage_reported,
        )
        self._settled = True


def prepare_metered_call(
    *, model: str, base_url: str, messages: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]] | None, max_tokens: int | None,
) -> MeteredCall | None:
    task = current_credit_task()
    if task is None:
        user = current_user(required=False)
        if user is not None and user.role != "admin":
            raise CreditError("AI 请求未绑定点数任务，请从 AI 助手或分析入口重新发起。")
        return None
    priced = True
    try:
        _validate_destination(model, base_url)
        upper = text_input_tokens_upper(messages, tools)
    except (CreditError, ValueError):
        if getattr(task.policy, "enforce_cost_limits", False):
            raise
        # 其他模型、代理与多模态仍记录供应商 token；未配置价格不估金额。
        priced, upper = False, None
    reservation = task.reserve_call(upper, max_tokens)
    return MeteredCall(task, reservation, priced=priced)
