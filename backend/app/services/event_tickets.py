"""保留的一次性 SSE 票据实现；当前认证入口已停用事件票据通道。

票据只继承已有 scope，60 秒过期且消费即作废。
票据保存在进程内存中，重启后失效。
"""
from __future__ import annotations

import secrets
import threading
import time

TICKET_PREFIX = "tse_"
TTL_S = 60.0
_MAX_OUTSTANDING = 1000  # 防堆积: 远超正常并发, 超限先清理过期

_LOCK = threading.Lock()
_TICKETS: dict[str, tuple[float, list[str]]] = {}  # ticket → (过期时刻, scopes)


def _prune(now: float) -> None:
    expired = [t for t, (exp, _) in _TICKETS.items() if exp <= now]
    for t in expired:
        _TICKETS.pop(t, None)


def issue(scopes: list[str]) -> tuple[str, float]:
    """签发一次性票据; 返回 (票据, 有效期秒)。"""
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        if len(_TICKETS) >= _MAX_OUTSTANDING:
            raise RuntimeError("待使用票据过多, 稍后重试")
        ticket = TICKET_PREFIX + secrets.token_hex(16)
        _TICKETS[ticket] = (now + TTL_S, list(scopes))
    return ticket, TTL_S


def consume(ticket: str) -> list[str] | None:
    """校验并消费票据; 返回继承的 scopes, 无效/过期/已用 → None。"""
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        hit = _TICKETS.pop(ticket, None)
    if hit is None or hit[0] <= now:
        return None
    return hit[1]
