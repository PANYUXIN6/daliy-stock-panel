import asyncio
import json
import threading
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import polars as pl
import pytest
from fastapi import FastAPI

from app.api import settings as settings_api
from app.market_time import CN_TZ
from app.services import ordinary_minute, preferences
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

pytestmark = pytest.mark.usefixtures("authenticated_actor")

@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "_path", lambda: tmp_path / "preferences.json")
    preferences._invalidate_cache()
    yield
    preferences._invalidate_cache()


def test_pool_and_collection_preserve_assets_and_shared_results(tmp_path, monkeypatch):
    from app.services import shared_runtime
    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(ordinary_minute.watchlist, "list_symbols", lambda: [{"symbol": "600000.SH"}])
    monkeypatch.setattr(ordinary_minute.monitor_rules, "load_all", lambda root: [
        {"enabled": True, "symbols": ["600001.SH"]}, {"enabled": False, "symbols": ["600002.SH"]},
    ])
    groups = ordinary_minute.collection_pool(repo)
    assert groups["stock"] == ["600000.SH", "600001.SH"]
    assert set(groups["index"]) == set(ordinary_minute.CORE_INDEX_SYMBOLS)
    events = []
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda root, datasets: events.append(datasets))
    def fetch(symbols, **kwargs):
        return pl.DataFrame({"symbol": symbols, "datetime": [datetime(2026, 10, 9, 10)] * len(symbols), "close": [42.0] * len(symbols)})
    monkeypatch.setattr(ordinary_minute.kline_sync, "sync_minute_batch", fetch)
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    ordinary_minute.collect_minutes(repo, capset, groups, datetime(2026, 10, 9, 9, 30), datetime(2026, 10, 9, 10))
    second = KlineRepository(DataStore(tmp_path))
    assert second.get_minute("000001.SH", date(2026, 10, 9), asset_type="index")["close"][0] == 42.0
    assert second.get_minute("000001.SH", date(2026, 10, 9), asset_type="stock").is_empty()
    assert second.get_minute("600000.SH", date(2026, 10, 9))["close"][0] == 42.0
    assert events == [["minute"]]


def test_ordinary_round_skips_all_supplier_calls_at_lunch_and_unknown_calendar(tmp_path, monkeypatch):
    from app.services import trading_day
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), _write_lock=threading.Lock())
    state = SimpleNamespace(capabilities=CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()}))
    service = ordinary_minute.OrdinaryMinuteService(repo, app_state=state)
    collect = Mock(side_effect=AssertionError("must not collect"))
    monkeypatch.setattr(ordinary_minute, "collect_minutes", collect)
    monkeypatch.setattr(ordinary_minute, "cn_now", lambda: datetime(2026, 10, 9, 12, tzinfo=CN_TZ))
    assert service.run_round() == 0
    monkeypatch.setattr(ordinary_minute, "cn_now", lambda: datetime(2026, 10, 9, 10, tzinfo=CN_TZ))
    monkeypatch.setattr(trading_day, "is_trading_day", lambda *args, **kwargs: None)
    assert service.run_round() == 0
    collect.assert_not_called()


def test_real_round_collects_once_and_persists(tmp_path, monkeypatch):
    from app.services import pipeline_jobs, shared_runtime, trading_day
    repo = KlineRepository(DataStore(tmp_path))
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    service = ordinary_minute.OrdinaryMinuteService(repo, app_state=SimpleNamespace(capabilities=capset))
    monkeypatch.setattr(ordinary_minute, "cn_now", lambda: datetime(2026, 10, 9, 10, tzinfo=CN_TZ))
    monkeypatch.setattr(trading_day, "is_trading_day", lambda *args, **kwargs: True)
    monkeypatch.setattr(pipeline_jobs, "job_store", pipeline_jobs.JobStore(store_dir=tmp_path / "jobs"))
    monkeypatch.setattr(ordinary_minute.watchlist, "list_symbols", lambda: [])
    monkeypatch.setattr(ordinary_minute.monitor_rules, "load_all", lambda root: [])
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: None)
    fetch = Mock(return_value=pl.DataFrame({"symbol": ["000001.SH"], "datetime": [datetime(2026, 10, 9, 10)], "close": [43.0]}))
    monkeypatch.setattr(ordinary_minute.kline_sync, "sync_minute_batch", fetch)
    assert service.run_round() == 1
    fetch.assert_called_once()
    assert repo.get_minute("000001.SH", date(2026, 10, 9), asset_type="index")["close"][0] == 43.0


def test_disabled_auto_collection_can_resume_without_restarting_service(tmp_path, monkeypatch):
    from app.services import pipeline_jobs, shared_runtime, trading_day

    repo = KlineRepository(DataStore(tmp_path))
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    service = ordinary_minute.OrdinaryMinuteService(repo, app_state=SimpleNamespace(capabilities=capset))
    monkeypatch.setattr(ordinary_minute, "cn_now", lambda: datetime(2026, 10, 9, 10, tzinfo=CN_TZ))
    monkeypatch.setattr(trading_day, "is_trading_day", lambda *args, **kwargs: True)
    monkeypatch.setattr(pipeline_jobs, "job_store", pipeline_jobs.JobStore(store_dir=tmp_path / "jobs"))
    monkeypatch.setattr(ordinary_minute.watchlist, "list_symbols", lambda: [])
    monkeypatch.setattr(ordinary_minute.monitor_rules, "load_all", lambda root: [])
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: None)
    fetch = Mock(return_value=pl.DataFrame({"symbol": ["000001.SH"], "datetime": [datetime(2026, 10, 9, 10)], "close": [43.0]}))
    monkeypatch.setattr(ordinary_minute.kline_sync, "sync_minute_batch", fetch)

    assert service.status()["enabled"] is True
    preferences.save({"ordinary_minute_enabled": False, "realtime_quotes_enabled": False})
    assert service.run_round() == 0
    assert service.status()["enabled"] is False
    fetch.assert_not_called()
    assert repo.get_minute("000001.SH", date(2026, 10, 9), asset_type="index").is_empty()

    preferences.save({"ordinary_minute_enabled": True})
    assert service.run_round() == 1
    assert service.status()["enabled"] is True
    assert preferences.get_realtime_quotes_enabled() is False
    assert repo.get_minute("000001.SH", date(2026, 10, 9), asset_type="index")["close"][0] == 43.0
    fetch.assert_called_once()


def test_disable_during_asset_fetch_preserves_result_and_stops_next_asset(tmp_path, monkeypatch):
    from app.services import pipeline_jobs, shared_runtime, trading_day

    repo = KlineRepository(DataStore(tmp_path))
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    service = ordinary_minute.OrdinaryMinuteService(repo, app_state=SimpleNamespace(capabilities=capset))
    monkeypatch.setattr(ordinary_minute, "cn_now", lambda: datetime(2026, 10, 9, 10, tzinfo=CN_TZ))
    monkeypatch.setattr(trading_day, "is_trading_day", lambda *args, **kwargs: True)
    monkeypatch.setattr(pipeline_jobs, "job_store", pipeline_jobs.JobStore(store_dir=tmp_path / "jobs"))
    monkeypatch.setattr(ordinary_minute.watchlist, "list_symbols", lambda: [{"symbol": "600000.SH"}])
    monkeypatch.setattr(ordinary_minute.monitor_rules, "load_all", lambda root: [])
    events = []
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda root, datasets: events.append(datasets))

    def fetch(symbols, **kwargs):
        preferences.save({"ordinary_minute_enabled": False})
        return pl.DataFrame({"symbol": symbols, "datetime": [datetime(2026, 10, 9, 10)] * len(symbols), "close": [43.0] * len(symbols)})

    supplier = Mock(side_effect=fetch)
    monkeypatch.setattr(ordinary_minute.kline_sync, "sync_minute_batch", supplier)
    assert service.run_round() == 1
    supplier.assert_called_once()
    assert supplier.call_args.kwargs["asset_type"] == "stock"
    assert repo.get_minute("600000.SH", date(2026, 10, 9))["close"][0] == 43.0
    assert repo.get_minute("000001.SH", date(2026, 10, 9), asset_type="index").is_empty()
    assert service.status()["enabled"] is False
    assert events == [["minute"]]


def test_disabled_auto_collection_keeps_explicit_minute_sync_available(tmp_path, monkeypatch):
    from app.services import shared_runtime, trading_day

    preferences.save({"ordinary_minute_enabled": False})
    repo = KlineRepository(DataStore(tmp_path))
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    monkeypatch.setattr(ordinary_minute, "cn_now", lambda: datetime(2026, 10, 9, 10, tzinfo=CN_TZ))
    monkeypatch.setattr(trading_day, "calendar_status", lambda root: {"days": ["2026-10-09"]})
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: None)
    fetch = Mock(return_value=pl.DataFrame({"symbol": ["600000.SH"], "datetime": [datetime(2026, 10, 9, 10)], "close": [43.0]}))
    monkeypatch.setattr(ordinary_minute.kline_sync, "sync_minute_batch", fetch)

    assert ordinary_minute.sync_symbol_minutes(repo, capset, "600000.SH", 1) == {"symbol": "600000.SH", "rows": 1}
    assert preferences.get_ordinary_minute_enabled() is False
    assert repo.get_minute("600000.SH", date(2026, 10, 9))["close"][0] == 43.0
    fetch.assert_called_once()


def test_ordinary_minute_preference_api_persists_independent_switch(tmp_path):
    preferences.save({"realtime_quotes_enabled": False, "minute_sync_enabled": True, "minute_intraday_refresh": False})
    app = FastAPI()
    app.include_router(settings_api.router)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/api/settings/preferences")
            assert response.status_code == 200, response.text
            assert response.json()["ordinary_minute_enabled"] is True

            response = await client.put("/api/settings/preferences/ordinary-minute", json={"enabled": False})
            assert response.status_code == 200, response.text
            assert response.json() == {"ordinary_minute_enabled": False}
            preferences._invalidate_cache()
            saved = json.loads((tmp_path / "preferences.json").read_text())
            assert saved["ordinary_minute_enabled"] is False
            assert saved["realtime_quotes_enabled"] is False
            assert saved["minute_sync_enabled"] is True
            assert (await client.get("/api/settings/preferences")).json()["ordinary_minute_enabled"] is False

            response = await client.put("/api/settings/preferences/ordinary-minute", json={"enabled": "invalid"})
            assert response.status_code == 422
            assert preferences.get_ordinary_minute_enabled() is False

            response = await client.put("/api/settings/preferences/ordinary-minute", json={"enabled": True})
            assert response.status_code == 200, response.text
            assert response.json() == {"ordinary_minute_enabled": True}
            preferences._invalidate_cache()
            assert preferences.get_ordinary_minute_enabled() is True

    asyncio.run(run())
