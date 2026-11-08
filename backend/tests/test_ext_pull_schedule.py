"""服务器扩展表固定日程：北京时间、本地交易日历、重启、配置修改与写入互斥。"""
from datetime import UTC, datetime

import pytest

from app.market_time import CN_TZ
from app.services import ext_pull, pipeline_jobs
from app.services.ext_data import ExtConfig, ExtConfigStore, PullConfig


def at(day, hhmm="10:00"):
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=CN_TZ)


def config(**kwargs):
    return ExtConfig(id="scheduled", label="定时数据", mode="snapshot", fields=[],
                     pull=PullConfig(url="https://example.test", enabled=True, schedule_time="09:30", **kwargs))


@pytest.fixture
def scheduler(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr("app.services.shared_runtime.publish_data_update", lambda *args: None)
    s = ext_pull.PullScheduler()
    s._running = True
    s._data_dir = tmp_path
    return s, ExtConfigStore(tmp_path)


@pytest.mark.parametrize("period,day,now,expected", [
    ("daily", 1, "2026-11-06", "2026-11-07"),
    ("weekly", 5, "2026-11-06", "2026-11-13"),
    ("monthly", 31, "2026-11-06", "2026-12-31"),
])
def test_fixed_schedule_next_occurrence_and_short_month(period, day, now, expected):
    pull = config(schedule_period=period, schedule_day=day).pull
    assert ext_pull._next_scheduled_target(at(now), "09:30", pull) == at(expected, "09:30")


def test_trading_only_uses_confirmed_calendar_and_does_not_guess_weekdays():
    pull = config(schedule_trading_only=True).pull
    calendar = {"start": "2026-11-02", "end": "2026-11-03", "days": ["2026-11-03"]}
    assert ext_pull._is_scheduled_day(at("2026-11-02"), pull, calendar) is False
    assert ext_pull._is_scheduled_day(at("2026-11-03"), pull, calendar) is True
    assert ext_pull._is_scheduled_day(at("2026-11-04"), pull, calendar) is None
    assert ext_pull._next_scheduled_target(at("2026-11-02"), "09:30", pull, calendar) == at("2026-11-03", "09:30")
    assert ext_pull._next_scheduled_target(at("2026-11-03"), "09:30", pull, calendar) is None


async def test_fixed_time_waits_until_due_and_restart_does_not_repeat(scheduler, monkeypatch):
    s, store = scheduler
    cfg = config()
    store.upsert(cfg)
    now = at("2026-11-06", "09:29")
    calls = []
    monkeypatch.setattr(ext_pull, "cn_now", lambda: now)

    async def fetch(fresh, root, **kwargs):
        calls.append(now)
        return 3, now.date().isoformat()

    async def sleep(seconds):
        nonlocal now
        assert seconds <= 60
        if calls:
            s._running = False
        else:
            now = at("2026-11-06", "09:30")

    monkeypatch.setattr(ext_pull, "fetch_and_ingest", fetch)
    monkeypatch.setattr(ext_pull.asyncio, "sleep", sleep)
    await s._run_loop(cfg)
    assert calls == [at("2026-11-06", "09:30")]
    saved = store.get(cfg.id).pull
    assert saved.last_status == "success" and saved.last_rows == 3
    assert datetime.fromisoformat(saved.next_run) == at("2026-11-07", "09:30").astimezone(UTC)
    s._running = True
    await s._run_loop(cfg)
    assert len(calls) == 1


async def test_unknown_calendar_waits_without_marking_plan_executed(scheduler, monkeypatch):
    s, store = scheduler
    cfg = config(schedule_trading_only=True)
    store.upsert(cfg)
    monkeypatch.setattr(ext_pull, "cn_now", lambda: at("2026-11-06"))
    monkeypatch.setattr("app.services.trading_day.calendar_status", lambda root: {})

    async def sleep(seconds):
        s._running = False

    async def unexpected(*args, **kwargs):
        pytest.fail("日历未覆盖不应拉取")

    monkeypatch.setattr(ext_pull.asyncio, "sleep", sleep)
    monkeypatch.setattr(ext_pull, "fetch_and_ingest", unexpected)
    await s._run_loop(cfg)
    saved = store.get(cfg.id).pull
    assert saved.last_status == "waiting"
    assert saved.last_run is None and saved.next_run is None


async def test_interval_schedule_change_takes_effect_within_poll(scheduler, monkeypatch):
    s, store = scheduler
    cfg = config()
    cfg.pull.schedule_time = None
    store.upsert(cfg)
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        cfg.pull.schedule_time = "09:30"
        store.upsert(cfg)

    monkeypatch.setattr(ext_pull.asyncio, "sleep", sleep)
    await s._sleep_interval_reloading(cfg.id, store, 86400)
    assert sleeps == [60]


async def test_busy_slot_defers_without_losing_plan_and_preserves_edits_during_fetch(scheduler, monkeypatch):
    s, store = scheduler
    cfg = config()
    store.upsert(cfg)
    calls = []

    async def fetch(fresh, root, **kwargs):
        calls.append(fresh.id)
        latest = store.get(fresh.id)
        latest.pull.url = "https://new.example.test"
        latest.pull.enabled = False
        store.upsert(latest)
        return 2, "2026-11-06"

    monkeypatch.setattr(ext_pull, "fetch_and_ingest", fetch)
    assert pipeline_jobs.try_acquire_run_slot("pipeline")
    assert await s._execute_pull(cfg, store) is False
    assert calls == [] and store.get(cfg.id).pull.last_run is None
    pipeline_jobs.release_run_slot("pipeline")
    assert await s._execute_pull(cfg, store) is True
    saved = store.get(cfg.id).pull
    assert saved.url == "https://new.example.test" and saved.enabled is False
    assert saved.last_status == "success"
    assert pipeline_jobs.try_acquire_run_slot("next")
    pipeline_jobs.release_run_slot("next")


async def test_cancel_releases_writer_slot(scheduler, monkeypatch):
    import asyncio

    s, store = scheduler
    cfg = config()
    store.upsert(cfg)
    started = asyncio.Event()

    async def fetch(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ext_pull, "fetch_and_ingest", fetch)
    task = asyncio.create_task(s._execute_pull(cfg, store))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pipeline_jobs.try_acquire_run_slot("after-cancel")
    pipeline_jobs.release_run_slot("after-cancel")
