"""Drive the polling loops with a local calendar and isolated provider calls."""
from __future__ import annotations

import json
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest

from app.config import settings
from app.market_time import CN_TZ
from app.services import depth_service, quote_service, trading_day
from app.services.depth_service import DepthService
from app.services.quote_service import QuoteService
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def _now(hour, minute=0, *, day=9):
    return datetime(2026, 10, day, hour, minute, tzinfo=CN_TZ)


def _calendar(root):
    path = root / "calendar" / "trading_days.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "start": "2026-10-01", "end": "2026-10-12",
        "days": ["2026-10-09", "2026-10-12"],
    }), encoding="utf-8")


@pytest.fixture
def polling(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    _calendar(tmp_path)
    clock = [_now(9, 29)]
    for module in (quote_service, depth_service, trading_day):
        monkeypatch.setattr(module, "cn_now", lambda: clock[0])
    for module in (quote_service, depth_service):
        monkeypatch.setattr(module, "cn_today", lambda: clock[0].date())

    provider = SimpleNamespace(
        get_realtime=Mock(return_value=[]),
        get_depth_batch=Mock(return_value={"002084.SZ": {"ask_volumes": [0], "bid_volumes": [10]}}),
    )
    monkeypatch.setattr("app.services.preferences.get_realtime_data_provider", lambda: "test_source")
    monkeypatch.setattr("app.services.preferences.get_depth5_data_provider", lambda: "test_source")
    monkeypatch.setattr("app.data_providers.custom.provider_has_dataset", lambda *args: True)
    monkeypatch.setattr("app.data_providers.custom.get_provider", lambda name: provider)

    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_latest=lambda: (pl.DataFrame({
            "symbol": ["002084.SZ"], "signal_limit_up": [True],
        }), clock[0].date()),
    )
    depth = DepthService()
    depth.set_repo(repo)
    depth.set_app_state(SimpleNamespace(capabilities=CapabilitySet({
        Cap.DEPTH5_BATCH: CapabilityLimits(batch=100, rpm=30),
    })))
    monkeypatch.setattr(depth, "_compute_interval", lambda n: (0.5, False, 0.5))
    monkeypatch.setattr(depth_service, "sleep_between_batches", lambda *args, **kwargs: None)

    quote = QuoteService()
    quote._interval = 0.5
    quote._enabled = True

    def run(service, moments, after_round=None):
        clock[0] = moments[0]
        service._running = True
        step = [0]

        def advance(_seconds):
            if after_round:
                after_round(step[0])
            step[0] += 1
            if step[0] == len(moments):
                service._running = False
            else:
                clock[0] = moments[step[0]]

        monkeypatch.setattr(quote_service.time, "sleep", advance)
        service._poll_loop()

    return SimpleNamespace(
        quote=quote, depth=depth, provider=provider, clock=clock, run=run, root=tmp_path,
    )


@pytest.mark.parametrize("service_name", ["quote", "depth"])
def test_polling_enters_and_leaves_sessions_without_restart(polling, service_name):
    calls = []
    fetch = polling.provider.get_realtime if service_name == "quote" else polling.provider.get_depth_batch
    fetch.side_effect = lambda *args: calls.append(polling.clock[0]) or ([] if service_name == "quote" else {})
    polling.run(getattr(polling, service_name), [
        _now(9, 29), _now(9, 30), _now(11, 30), _now(11, 31),
        _now(12, 59), _now(13), _now(15), _now(15, 29), _now(15, 30), _now(23),
        _now(10, day=1), _now(10, day=11),
    ])
    expected = [_now(9, 30), _now(13), _now(15)]
    if service_name == "quote":
        expected.append(_now(15, 29))
    else:
        expected.insert(1, _now(11, 30))
    assert calls == expected


@pytest.mark.parametrize("service_name", ["quote", "depth"])
def test_polling_resumes_when_local_calendar_becomes_ready(polling, service_name):
    (polling.root / "calendar" / "trading_days.json").unlink()
    fetch = polling.provider.get_realtime if service_name == "quote" else polling.provider.get_depth_batch
    polling.run(getattr(polling, service_name), [_now(10), _now(10, 1)],
                after_round=lambda step: _calendar(polling.root) if step == 0 else None)
    assert fetch.call_count == 1


@pytest.mark.parametrize("hour,minute", [(15, 30), (23, 0)])
def test_quote_started_after_confirmation_window_does_not_collect(polling, hour, minute):
    polling.run(polling.quote, [_now(hour, minute)])
    polling.provider.get_realtime.assert_not_called()


def test_depth_boot_does_not_collect_missing_postmarket_file(polling):
    polling.clock[0] = _now(23)
    polling.depth.boot_check()
    polling.provider.get_depth_batch.assert_not_called()
    assert not (polling.root / "depth5").exists()


def test_depth_boot_restores_local_file_without_collecting(polling):
    day = date(2026, 10, 9)
    path = polling.root / "depth5" / f"date={day}" / "part.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": ["002084.SZ"], "sealed_up": [True], "sealed_down": [None],
        "ask1_vol": [0], "bid1_vol": [10], "status": ["limit_up"], "fetched_at": [1234.0],
    }).write_parquet(path)
    polling.depth.boot_check()
    polling.provider.get_depth_batch.assert_not_called()
    assert polling.depth.is_sealed_ready(day)
    assert polling.depth.get_sealed_map(day, False)["002084.SZ"]["sealed"] is True


def test_depth_manual_postmarket_sync_still_collects_and_persists(polling):
    polling.clock[0] = _now(23)
    result = polling.depth.run_once()
    polling.provider.get_depth_batch.assert_called_once_with(["002084.SZ"])
    assert result["ok"] is True
    assert result["count"] == 1
    assert (polling.root / "depth5" / "date=2026-10-09" / "part.parquet").exists()


def _quote_snapshot(polling, monkeypatch, confirmed):
    quote = polling.quote
    boundary = int(_now(15).timestamp() * 1000)
    polling.provider.get_realtime.return_value = [{
        "symbol": "002084.SZ", "last_price": 7.07,
        "open": 7.0, "high": 7.1, "low": 6.95,
        "volume": 100_000, "amount": 707_000.0,
        "timestamp": boundary + (30_000 if confirmed else -60_000),
    }]
    repo = SimpleNamespace(
        get_index_symbol_set=lambda: set(), get_etf_instruments=lambda: pl.DataFrame(),
        get_live_daily_quote_timestamps=lambda _asset, _day: {},
        flush_live_daily=Mock(),
        merge_live_daily_asset=Mock(),
    )
    quote.set_repo(repo)
    monkeypatch.setattr(quote_service, "_persist_last_fetch", lambda ms: None)
    monkeypatch.setattr(quote, "_flush_live_enriched", Mock())
    monkeypatch.setattr(quote, "_broadcast_quote_updated", Mock())
    monkeypatch.setattr(quote, "_evaluate_monitors", Mock())
    return repo


def test_quote_stops_final_polling_after_confirmed_snapshot(polling, monkeypatch):
    repo = _quote_snapshot(polling, monkeypatch, confirmed=True)
    polling.run(polling.quote, [_now(15), _now(15, 1), _now(15, 29), _now(15, 30), _now(23)])
    polling.provider.get_realtime.assert_called_once()
    repo.merge_live_daily_asset.assert_called_once()
    assert repo.merge_live_daily_asset.call_args.args[0] == "stock"
    assert (date(2026, 10, 9), "close") in polling.quote._final_sync_done
    assert polling.quote.status()["final_sync_done"] is True
    assert polling.quote.status()["final_sync_failed"] is None


@pytest.mark.parametrize("reason", ["fetch_failed", "unconfirmed_snapshot"])
def test_quote_final_window_closes_locally_and_keeps_failure_visible(polling, monkeypatch, caplog, reason):
    repo = _quote_snapshot(polling, monkeypatch, confirmed=False)
    if reason == "fetch_failed":
        polling.provider.get_realtime.return_value = []
    polling.run(polling.quote, [_now(15, 29), _now(15, 30), _now(23)])
    polling.provider.get_realtime.assert_called_once()
    repo.flush_live_daily.assert_not_called()
    status = polling.quote.status()
    assert status["market_phase"] == "closed"
    assert status["is_polling_window"] is False
    assert status["final_sync_done"] is True
    assert status["final_sync_failed"] == reason
    endings = [record.getMessage() for record in caplog.records if "定版窗口结束" in record.getMessage()]
    assert len(endings) == 1
    assert "数据页手动同步" in endings[0]


@pytest.mark.parametrize("hour,minute", [(15, 30), (23, 0)])
@pytest.mark.parametrize("confirmed", [False, True], ids=["stale", "confirmed"])
def test_manual_postmarket_refresh_keeps_snapshot_boundary(polling, monkeypatch, hour, minute, confirmed):
    polling.clock[0] = _now(hour, minute)
    quote = polling.quote
    repo = _quote_snapshot(polling, monkeypatch, confirmed)
    result = quote.refresh()
    polling.provider.get_realtime.assert_called_once()
    assert result["fetch_succeeded"] is True
    assert repo.merge_live_daily_asset.call_count == int(confirmed)
    assert quote._last_final_confirmed is confirmed


def test_mixed_final_snapshot_does_not_stop_until_stock_confirms(polling, monkeypatch):
    repo = _quote_snapshot(polling, monkeypatch, confirmed=False)
    stale_stock = polling.provider.get_realtime.return_value[0]
    boundary = int(_now(15).timestamp() * 1000)
    fresh_index = {**stale_stock, "symbol": "000001.SH", "timestamp": boundary + 30_000}
    fresh_stock = {**stale_stock, "timestamp": boundary + 40_000}
    polling.provider.get_realtime.side_effect = [
        [stale_stock, fresh_index], [fresh_stock, fresh_index],
    ]
    states = []
    polling.run(polling.quote, [_now(15), _now(15, 1), _now(15, 2)],
                after_round=lambda _step: states.append(polling.quote._last_final_confirmed))
    assert states == [False, True, True]
    assert polling.provider.get_realtime.call_count == 2
    stock_writes = [args for args in repo.merge_live_daily_asset.call_args_list
                    if args.args[0] == "stock"]
    assert len(stock_writes) == 1
    assert stock_writes[0].args[1]["quote_ts"][0] == boundary + 40_000
    assert (date(2026, 10, 9), "close") in polling.quote._final_sync_done
