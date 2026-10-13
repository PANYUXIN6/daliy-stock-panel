"""定时复盘只有协议 done 才能归档或推送，残文不是成功证据。"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app.jobs import daily_pipeline
from app.services import market_recap, market_recap_reports, preferences, shared_runtime


@pytest.fixture(autouse=True)
def scheduled_owner(authenticated_actor):
    preferences.set_review_schedule(True, 15, 40)


@pytest.mark.parametrize("failure", ["error", "eof"])
def test_scheduled_review_rejects_all_failed_attempts(tmp_path, monkeypatch, failure):
    attempts = []
    events = []
    archived = []
    pushed = []
    published = []

    async def stream(*args):
        attempts.append(len(attempts) + 1)
        yield json.dumps({"type": "meta", "as_of": "2026-10-09"})
        yield json.dumps({"type": "delta", "content": "未完成残文"})
        if failure == "error":
            yield json.dumps({"type": "error", "message": "流失败"})

    async def no_delay(seconds):
        pass

    quote = SimpleNamespace(push_review_event=lambda event: events.append(json.loads(event)))
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: SimpleNamespace(quote_service=quote))
    monkeypatch.setattr("app.services.trading_day.is_trading_day", lambda: True)
    monkeypatch.setattr("app.secrets_store.get_ai_key", lambda: "test-key")
    monkeypatch.setattr(market_recap, "recap_market_stream", stream)
    monkeypatch.setattr(asyncio, "sleep", no_delay)
    monkeypatch.setattr(market_recap_reports, "save_report", lambda report: archived.append(report))
    monkeypatch.setattr(daily_pipeline, "_maybe_push_review", lambda *args: pushed.append(args))
    monkeypatch.setattr(preferences, "get_review_push_mode", lambda: "auto")
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args, **kwargs: published.append(args))

    asyncio.run(daily_pipeline._run_scheduled_review(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))))

    assert attempts == [1, 2, 3]
    assert archived == pushed == published == []
    assert events[-1]["type"] == "error"
    assert not any(event.get("archived") for event in events)


def test_scheduled_review_archives_only_successful_retry(tmp_path, monkeypatch):
    attempts = []
    events = []
    archived = []
    pushed = []

    async def stream(*args):
        attempts.append(len(attempts) + 1)
        yield json.dumps({"type": "meta", "as_of": "2026-10-09"})
        yield json.dumps({"type": "delta", "content": "残文" if len(attempts) == 1 else "完整报告"})
        yield json.dumps({"type": "error", "message": "流失败"} if len(attempts) == 1 else {"type": "done"})

    async def no_delay(seconds):
        pass

    quote = SimpleNamespace(push_review_event=lambda event: events.append(json.loads(event)))
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: SimpleNamespace(quote_service=quote))
    monkeypatch.setattr("app.services.trading_day.is_trading_day", lambda: True)
    monkeypatch.setattr("app.secrets_store.get_ai_key", lambda: "test-key")
    monkeypatch.setattr(market_recap, "recap_market_stream", stream)
    monkeypatch.setattr(asyncio, "sleep", no_delay)
    monkeypatch.setattr(market_recap_reports, "save_report", lambda report: archived.append(report))
    monkeypatch.setattr(daily_pipeline, "_maybe_push_review", lambda content, meta: pushed.append(content))
    monkeypatch.setattr(preferences, "get_review_push_mode", lambda: "auto")
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args, **kwargs: None)

    asyncio.run(daily_pipeline._run_scheduled_review(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))))

    assert attempts == [1, 2]
    assert len(archived) == 1 and archived[0]["content"] == "完整报告"
    assert pushed == ["完整报告"]
    assert events[-1] == {"type": "done", "archived": True}
