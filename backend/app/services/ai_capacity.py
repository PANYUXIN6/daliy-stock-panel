"""所有 AI provider 共用的小型并发闸，含流关闭和取消路径。"""
from __future__ import annotations

import asyncio
import functools
import inspect
import threading
from contextlib import asynccontextmanager

from app.config import settings

_lock = threading.Lock()
_active = 0


@asynccontextmanager
async def ai_slot():
    global _active
    while True:
        with _lock:
            acquired = _active < settings.ai_max_concurrent
            if acquired:
                _active += 1
        if acquired:
            break
        await asyncio.sleep(0.05)
    try:
        yield
    finally:
        with _lock:
            _active -= 1


def limited_ai(function):
    if inspect.isasyncgenfunction(function):
        @functools.wraps(function)
        async def stream(*args, **kwargs):
            async with ai_slot():
                generator = function(*args, **kwargs)
                try:
                    async for item in generator:
                        yield item
                finally:
                    await generator.aclose()
        return stream

    @functools.wraps(function)
    async def call(*args, **kwargs):
        async with ai_slot():
            return await function(*args, **kwargs)
    return call
