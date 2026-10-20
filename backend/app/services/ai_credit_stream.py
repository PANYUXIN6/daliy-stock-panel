"""AI HTTP 任务的点数预留与流式结算，模型费用由底层调用单独记账。"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import aclosing

import anyio
from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from app.services.ai_credits import (
    CreditError,
    CreditTask,
    credit_task_scope,
    get_credit_store,
)
from app.services.ai_metering import credit_configuration_reason
from app.services.auth import AuthError, require_current_session
from app.services.ndjson_heartbeat import with_heartbeat
from app.services.user_context import current_user


async def prepare_credit_task(request: Request, feature: str) -> CreditTask | None:
    """在响应开始前预留；身份始终来自服务端会话，而非客户端计费参数。"""
    actor = await run_in_threadpool(require_current_session)
    if actor["role"] != "admin":
        reason = await run_in_threadpool(credit_configuration_reason)
        if reason:
            raise CreditError(reason, 503, "AI_METERING_UNAVAILABLE")
    owner = current_user()
    version_header = request.headers.get("x-ai-policy-version")
    version = None
    if version_header is not None:
        try:
            version = int(version_header)
            if version < 1:
                raise ValueError
        except ValueError:
            raise CreditError("AI 点数规则版本无效，请刷新页面后重试", 400, "AI_POLICY_INVALID") from None
    store = await run_in_threadpool(get_credit_store, owner.data_dir)
    return await run_in_threadpool(
        store.begin_task,
        owner,
        feature,
        request_id=request.headers.get("x-ai-request-id"),
        expected_policy_version=version,
    )


async def finish_credit_task(task: CreditTask | None, outcome: str) -> None:
    if task is not None:
        with anyio.CancelScope(shield=True):
            await run_in_threadpool(task.finish, outcome)


class CreditStreamingResponse(StreamingResponse):
    """响应级兜底覆盖生成器首次迭代前的断连，结算本身保持幂等。"""

    def __init__(self, events: AsyncIterator[str], *, credit_task: CreditTask | None):
        self.credit_task = credit_task

        async def body():
            async with aclosing(with_heartbeat(charged_stream(credit_task, events))) as stream:
                async for line in stream:
                    yield line + "\n"

        super().__init__(
            body(), media_type="application/x-ndjson",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    task = self.credit_task
                    await finish_credit_task(task, "cancelled" if task is not None and task.started else "failed")


async def charged_stream(task: CreditTask | None, events: AsyncIterator[str]) -> AsyncIterator[str]:
    """消费完整业务流后结算；error 后的 done 不能被误认为生成成功。"""
    failed = False
    result_seen = False
    settled = False
    with credit_task_scope(task):
        try:
            async for line in events:
                event = json.loads(line)
                kind = event.get("type")
                if kind == "error":
                    failed = True
                elif ((kind == "delta" and event.get("content", "").strip())
                      or (kind == "tool_result" and event.get("ok"))):
                    result_seen = True
                elif kind == "done":
                    if not failed and not result_seen:
                        failed = True
                        yield json.dumps({
                            "type": "error", "message": "AI 未返回有效结果，请重试", "code": "AI_EMPTY_RESULT",
                        }, ensure_ascii=False)
                    await finish_credit_task(task, "failed" if failed else "success")
                    settled = True
                    yield line
                    return
                yield line
            if failed:
                await finish_credit_task(task, "failed")
                settled = True
                return
            failed = True
            await finish_credit_task(task, "failed")
            settled = True
            yield json.dumps({
                "type": "error", "message": "AI 生成未完整结束，请重试", "code": "AI_STREAM_INCOMPLETE",
            }, ensure_ascii=False)
            yield json.dumps({"type": "done"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- 流已开始，必须映射为安全的 NDJSON 终态
            failed = True
            await finish_credit_task(task, "failed")
            settled = True
            known = isinstance(exc, (CreditError, AuthError))
            yield json.dumps({
                "type": "error",
                "message": exc.detail if known else "AI 生成失败，请稍后重试",
                "code": exc.code if known else "AI_GENERATION_FAILED",
            }, ensure_ascii=False)
            yield json.dumps({"type": "done"})
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    close = getattr(events, "aclose", None)
                    if close is not None:
                        await close()
                finally:
                    if not settled:
                        await finish_credit_task(
                            task, "cancelled" if task is not None and task.started and not failed else "failed",
                        )
