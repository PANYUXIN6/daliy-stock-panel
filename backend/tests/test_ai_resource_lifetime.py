from __future__ import annotations

import asyncio
import subprocess
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import anyio
import pytest

from app.services import ai_provider


@pytest.mark.asyncio
@pytest.mark.parametrize('entrypoint', ['cli', 'process'])
async def test_disabled_codex_does_not_create_process_or_temporary_home(monkeypatch, entrypoint):
    process = Mock(side_effect=AssertionError('disabled CLI must not spawn a process'))
    temporary = Mock(side_effect=AssertionError('disabled CLI must not create a home'))
    prepare = Mock(side_effect=AssertionError('disabled CLI must not copy credentials'))
    monkeypatch.setattr(subprocess, 'Popen', process)
    monkeypatch.setattr(tempfile, 'TemporaryDirectory', temporary)
    monkeypatch.setattr(ai_provider, '_prepare_codex_home', prepare)

    with pytest.raises(RuntimeError, match='本地 Codex CLI 模式已停用'):
        if entrypoint == 'cli':
            await asyncio.wait_for(
                ai_provider._run_codex_cli([], max_tokens=None, timeout=10), timeout=1,
            )
        else:
            ai_provider._run_codex_process(['codex'], '', {}, 10)

    process.assert_not_called()
    temporary.assert_not_called()
    prepare.assert_not_called()


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
