from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest

from app.services import ai_provider


@pytest.mark.asyncio
@pytest.mark.parametrize('level_cancel', [False, True])
async def test_cancelled_codex_holds_slot_and_directory_until_process_exits(monkeypatch, level_cancel):
    entered = threading.Event()
    release = threading.Event()
    paths = []
    monkeypatch.setattr(ai_provider.settings, 'ai_max_concurrent', 1)
    monkeypatch.setattr(ai_provider, '_prepare_codex_home', lambda path: None)
    monkeypatch.setattr(ai_provider, '_codex_base_command', lambda: ['codex'])
    monkeypatch.setattr(ai_provider, 'current_ai_model', lambda: '')

    def process(args, prompt, env, timeout):
        paths.append(Path(env['CODEX_HOME']).parent)
        entered.set()
        assert release.wait(5)
        return 0, b'done', b''

    monkeypatch.setattr(ai_provider, '_run_codex_process', process)
    scope = anyio.CancelScope()

    async def run():
        with scope:
            return await ai_provider._run_codex_cli([], max_tokens=None, timeout=10)

    first = asyncio.create_task(run())
    second = None
    try:
        while not entered.is_set():
            await asyncio.sleep(.01)
        if level_cancel:
            scope.cancel()
        else:
            first.cancel()
        second = asyncio.create_task(ai_provider._run_codex_cli([], max_tokens=None, timeout=10))
        await asyncio.sleep(.15)
        assert len(paths) == 1
        assert paths[0].exists()
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
    assert all(not path.exists() for path in paths)


@pytest.mark.asyncio
@pytest.mark.parametrize('assistant', [False, True])
@pytest.mark.parametrize('ending', ['complete', 'close', 'cancel'])
async def test_sdk_stream_is_closed(monkeypatch, assistant, ending):
    from app.custom.assistant import streaming

    class Stream:
        def __init__(self):
            self.close = AsyncMock(side_effect=self.finish)
            self.closed = False
            self.index = 0

        async def finish(self):
            await asyncio.sleep(0)
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.index += 1
            if self.index > 1:
                if ending == 'cancel':
                    await asyncio.Event().wait()
                raise StopAsyncIteration
            return SimpleNamespace(choices=[SimpleNamespace(
                finish_reason=None, delta=SimpleNamespace(content='text', tool_calls=None),
            )])

    stream = Stream()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=stream))))
    monkeypatch.setattr(ai_provider.secrets_store, 'get_ai_key', lambda: 'fake')
    monkeypatch.setattr(ai_provider, '_openai_client', lambda *args: client)
    monkeypatch.setattr(streaming, '_client', lambda *args: client)
    generator = (streaming.stream_openai_round([], []) if assistant else ai_provider._stream_openai(
        [], temperature=None, max_tokens=None, timeout=1, prefer_final_answer=False,
    ))
    assert await anext(generator)
    if ending == 'complete':
        _ = [item async for item in generator]
    elif ending == 'close':
        await generator.aclose()
    else:
        with anyio.CancelScope() as scope:
            scope.cancel()
            await anext(generator)
        await generator.aclose()
    assert stream.closed
    stream.close.assert_awaited_once()
