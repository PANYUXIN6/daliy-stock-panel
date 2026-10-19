"""正式日线覆盖证据来自本轮返回，停牌须有明确证据。"""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest

from app.data_providers import custom
from app.market_time import CN_TZ
from app.services import kline_sync, preferences
from app.services.daily_coverage import DailyCoverage, DailyCoverageError
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

DAY = date(2026, 10, 16)


def test_tickflow_captures_halted_row_before_normalization_filters_it(monkeypatch):
    midnight = int(datetime(2026, 10, 16, tzinfo=CN_TZ).timestamp() * 1000)
    payload = {
        symbol: {"timestamp": [midnight], "open": [opening], "high": [opening],
                 "low": [opening], "close": [10.0], "volume": [volume], "amount": [volume * 1000]}
        for symbol, opening, volume in [("600000.SH", 10.0, 100), ("000001.SZ", 0.0, 0)]
    }
    monkeypatch.setattr(kline_sync, "get_client", lambda: SimpleNamespace(klines=SimpleNamespace(batch=lambda *_, **__: payload)))
    monkeypatch.setattr(kline_sync, "sleep_between_batches", lambda *_: None)
    coverage = DailyCoverage(DAY)
    frame = kline_sync.sync_daily_batch(list(payload), start_time=datetime(2026, 10, 16),
                                      end_time=datetime(2026, 10, 16), coverage=coverage)
    assert frame["symbol"].to_list() == ["600000.SH"]
    assert coverage.require(list(payload)) == {"date": str(DAY), "expected": 2, "active": 1, "halted": 1}


def test_failed_tickflow_chunk_is_not_complete_even_if_local_prices_exist(monkeypatch):
    monkeypatch.setattr(kline_sync, "get_client", lambda: SimpleNamespace(klines=SimpleNamespace(
        batch=Mock(side_effect=OSError("network unavailable")),
    )))
    monkeypatch.setattr(kline_sync, "sleep_between_batches", lambda *_: None)
    coverage = DailyCoverage(DAY)
    assert kline_sync.sync_daily_batch(["600000.SH"], coverage=coverage).is_empty()
    assert coverage.failed == {"600000.SH"}
    with pytest.raises(DailyCoverageError, match="取数失败 1 只"):
        coverage.require(["600000.SH"])


def test_streaming_custom_source_can_supply_explicit_halt_evidence(monkeypatch, tmp_path):
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "example")
    monkeypatch.setattr(custom, "provider_has_dataset", lambda *_: True)
    frame = pl.DataFrame({"symbol": ["600000.SH"], "date": [DAY], "open": [10.0],
                          "high": [10.0], "low": [10.0], "close": [10.0],
                          "volume": [100], "amount": [100000.0]})
    halted = Mock(return_value={"000001.SZ"})
    source = SimpleNamespace(iter_daily=lambda *_, **__: iter([frame]), get_daily_halted_symbols=halted)
    monkeypatch.setattr(custom, "get_provider", lambda *_: source)
    written = Mock(return_value=1)

    def persist(chunks, repo):
        chunks = list(chunks)
        written(chunks)
        return sum(frame.height for frame in chunks)

    monkeypatch.setattr(kline_sync, "_persist_daily_chunks", persist)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    coverage = DailyCoverage(DAY)
    count = kline_sync.sync_and_persist_daily_batch(
        ["600000.SH", "000001.SZ"], repo, CapabilitySet({}), coverage=coverage,
    )
    assert count == 1
    assert coverage.require(["600000.SH", "000001.SZ"])["halted"] == 1
    halted.assert_called_once_with(DAY)
    assert written.call_args[0][0][0].equals(frame)


def test_strict_adj_factor_batch_failure_stops_derived_calculations(monkeypatch, tmp_path):
    monkeypatch.setattr(preferences, "get_adj_factor_provider", lambda: "tickflow")
    monkeypatch.setattr(kline_sync, "get_client", lambda: SimpleNamespace(klines=SimpleNamespace(
        ex_factors=Mock(side_effect=OSError("upstream failed")),
    )))
    monkeypatch.setattr(kline_sync, "sleep_between_batches", lambda *_: None)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    caps = CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits()})
    with pytest.raises(RuntimeError, match="除权因子同步失败"):
        kline_sync.sync_adj_factor(["600000.SH"], repo, caps, require_complete=True)


def test_missing_custom_capability_never_falls_back_for_automatic_sync(monkeypatch, tmp_path):
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "example")
    monkeypatch.setattr(custom, "provider_has_dataset", lambda *_: False)
    client = Mock(side_effect=AssertionError("不应跨源回退"))
    monkeypatch.setattr(kline_sync, "get_client", client)
    with pytest.raises(RuntimeError, match="未提供正式日线"):
        kline_sync.sync_and_persist_daily_batch(["600000.SH"], SimpleNamespace(
            store=SimpleNamespace(data_dir=tmp_path)), CapabilitySet({}), coverage=DailyCoverage(DAY))
    client.assert_not_called()
