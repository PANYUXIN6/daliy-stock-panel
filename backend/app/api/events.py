"""保留的短期票据与 SSE 事件流端点；主应用在认证层拒绝此入口。

模块内部仍保留票据消费、scope 过滤与心跳逻辑，供独立测试覆盖。
"""
from __future__ import annotations

import asyncio
import queue
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.services import event_tickets
from app.services.events import bus, sse_format

router = APIRouter(prefix="/api/events", tags=["events"])

_HEARTBEAT_S = 15.0
_POLL_S = 0.5


@router.post("/ticket")
def issue_ticket(request: Request):
    """Bearer Token (任意 scope) → 一次性 SSE 票据。"""
    # 从 Token 记录继承 scope，票据不能放大权限。
    authz = request.headers.get("authorization", "")
    plaintext = authz[len("Bearer "):].strip() if authz.startswith("Bearer ") else ""

    from app.services import api_tokens

    record = api_tokens.verify_token(request.app.state.repo.store.data_dir, plaintext)
    if record is None:
        raise HTTPException(status_code=401, detail="API Token 无效或已吊销")
    try:
        ticket, ttl = event_tickets.issue(record.get("scopes", []))
    except RuntimeError as e:
        raise HTTPException(status_code=429, detail=str(e)) from e
    return {"ticket": ticket, "expires_in": int(ttl), "stream": "/api/events?ticket=" + ticket}


@router.get("")
async def event_stream(ticket: str = ""):
    """SSE 事件流; 票据一次性, 重连需 POST /ticket 换新。"""
    scopes = event_tickets.consume(ticket) if ticket else None
    if scopes is None:
        return JSONResponse(status_code=401, content={"detail": "票据无效、已使用或过期"})

    q = bus.subscribe()

    async def gen():
        allowed = ("*", *scopes)
        try:
            yield sse_format({"type": "hello", "scope": "*", "data": {"scopes": scopes}, "ts": time.time()})
            last_send = time.monotonic()
            while True:
                try:
                    event = q.get_nowait()
                except queue.Empty:
                    if time.monotonic() - last_send >= _HEARTBEAT_S:
                        yield ": ping\n\n"  # SSE 注释帧 — 保活, 不产生 EventSource 事件
                        last_send = time.monotonic()
                    await asyncio.sleep(_POLL_S)
                    continue
                if event["scope"] in allowed:
                    yield sse_format(event)
                    last_send = time.monotonic()
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
