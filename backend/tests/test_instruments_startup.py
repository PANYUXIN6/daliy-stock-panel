"""盘前维表晚启动补跑只使用北京日期和已有本地成功证据。"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest

from app.jobs import daily_pipeline
from app.market_time import CN_TZ
from app.services import instrument_sync, pipeline_jobs, preferences, trading_day
from app.services.pipeline_jobs import JobStore


@pytest.fixture
def instruments(tmp_path, monkeypatch):
    from app.config import settings

    now = datetime(2026, 10, 9, 10, tzinfo=CN_TZ)
    clock = SimpleNamespace(now=now)
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(daily_pipeline, "cn_now", lambda: clock.now, raising=False)
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: clock.now.date())
    monkeypatch.setattr(trading_day, "cn_now", lambda: clock.now)
    monkeypatch.setattr(instrument_sync, "cn_today", lambda: clock.now.date(), raising=False)
    monkeypatch.setattr(preferences, "load", lambda: {})
    monkeypatch.setattr(pipeline_jobs, "job_store", JobStore(store_dir=tmp_path / "jobs"))
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *args: None)
    fetch = Mock(return_value=[{"symbol": "600000.SH", "name": "浦发银行"}])
    monkeypatch.setattr(instrument_sync, "_fetch_instruments_via_provider", fetch)
    calendar = tmp_path / "calendar" / "trading_days.json"
    calendar.parent.mkdir()
    calendar.write_text(json.dumps({"start": "2026-10-01", "end": "2026-10-11", "days": ["2026-10-09"]}))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           db=SimpleNamespace(execute=lambda *args: None),
                           clear_cache=lambda: None, refresh_cache=lambda: None)
    return repo, clock, fetch


@pytest.mark.parametrize("hour,minute,expected", [(9, 9, False), (9, 10, True), (10, 0, True)])
def test_startup_respects_configured_time_and_trading_day(instruments, hour, minute, expected):
    repo, clock, fetch = instruments
    clock.now = clock.now.replace(hour=hour, minute=minute)
    assert daily_pipeline.run_scheduled_instruments_sync(repo) is expected
    assert fetch.call_count == int(expected)


@pytest.mark.parametrize("day", [10, 11, 12])
def test_holiday_and_unknown_calendar_do_not_catch_up(instruments, day):
    repo, clock, fetch = instruments
    clock.now = clock.now.replace(day=day)
    assert daily_pipeline.run_scheduled_instruments_sync(repo) is False
    fetch.assert_not_called()
    assert pipeline_jobs.job_store.list_recent() == []


def test_startup_and_cron_share_persisted_success_after_restart(instruments, monkeypatch):
    repo, _, fetch = instruments
    assert daily_pipeline.run_scheduled_instruments_sync(repo) is True
    monkeypatch.setattr(pipeline_jobs, "job_store", JobStore(store_dir=repo.store.data_dir / "restarted_jobs"))

    async def run_cron():
        scheduler = daily_pipeline.start_scheduler(repo)
        try:
            job = scheduler.get_job("pre_market_instruments")
            assert job.func(*job.args, **job.kwargs) is False
        finally:
            scheduler.shutdown(wait=False)
            await asyncio.sleep(0)

    asyncio.run(run_cron())
    assert daily_pipeline.run_scheduled_instruments_sync(repo) is False
    assert fetch.call_count == 1
    assert pipeline_jobs.job_store.list_recent() == []


def test_custom_time_and_explicit_manual_refresh(instruments, monkeypatch):
    repo, clock, fetch = instruments
    clock.now = clock.now.replace(hour=8, minute=50)
    monkeypatch.setattr(preferences, "get_instruments_schedule", lambda: {"hour": 8, "minute": 45})
    assert daily_pipeline.run_scheduled_instruments_sync(repo) is True
    assert daily_pipeline.run_instruments_sync(repo)["instruments_rows"] == 1
    assert daily_pipeline.run_instruments_sync(repo)["instruments_rows"] == 1
    assert daily_pipeline.run_scheduled_instruments_sync(repo) is False
    assert fetch.call_count == 3


def test_empty_fetch_is_not_success_and_later_startup_can_run(instruments):
    repo, _, fetch = instruments
    fetch.return_value = []
    assert daily_pipeline.run_scheduled_instruments_sync(repo) is False
    assert pipeline_jobs.job_store.list_recent()[0]["status"] == "failed"
    assert not (repo.store.data_dir / "instruments" / "instruments.parquet").exists()
    fetch.return_value = [{"symbol": "600000.SH", "name": "浦发银行"}]
    assert daily_pipeline.run_scheduled_instruments_sync(repo) is True
    assert fetch.call_count == 2


@pytest.mark.parametrize("saved", [None, [], [date(2026, 10, 8)], [None], [date(2026, 10, 9)]])
def test_only_nonempty_today_table_counts_as_saved(instruments, saved):
    repo, clock, _ = instruments
    path = repo.store.data_dir / "instruments" / "instruments.parquet"
    if saved is not None:
        path.parent.mkdir()
        pl.DataFrame({"as_of": pl.Series(saved, dtype=pl.Date)}).write_parquet(path)
    assert instrument_sync.instruments_synced_on(repo.store.data_dir, clock.now.date()) is (saved == [clock.now.date()])


def test_legacy_table_without_date_does_not_count_as_saved(instruments):
    repo, clock, _ = instruments
    path = repo.store.data_dir / "instruments" / "instruments.parquet"
    path.parent.mkdir()
    pl.DataFrame({"symbol": ["600000.SH"]}).write_parquet(path)
    assert instrument_sync.instruments_synced_on(repo.store.data_dir, clock.now.date()) is False


def test_success_date_uses_beijing_even_when_server_date_is_previous_day(instruments, monkeypatch):
    repo, clock, _ = instruments

    class ServerDate(date):
        @classmethod
        def today(cls):
            return date(2026, 10, 8)

    monkeypatch.setattr(instrument_sync, "date", ServerDate)
    assert instrument_sync.sync_instruments(repo.store.data_dir) == 1
    frame = pl.read_parquet(repo.store.data_dir / "instruments" / "instruments.parquet")
    assert frame["as_of"].to_list() == [clock.now.date()]
