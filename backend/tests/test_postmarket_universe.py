"""盘后使用当轮当前 A 股成员，历史维表与停牌验收保持独立。"""
from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest

from app.data_providers import custom as custom_sources
from app.services import instrument_sync, postmarket, preferences
from app.services.daily_coverage import DailyCoverage, DailyCoverageError

DAY = date(2026, 10, 21)
ACTIVE = "600000.SH"
HALTED = "000016.SZ"
DELISTED = "000004.SZ"
MEMBERS = [ACTIVE, "000001.SZ", HALTED, "920001.BJ"]


def instrument(symbol, *, name="股票", listing_date="2000-01-01", current=None):
    code, exchange = symbol.split(".")
    return {
        "symbol": symbol, "name": name, "code": code, "exchange": exchange,
        "region": "CN", "type": "stock",
        "ext": {"listing_date": listing_date, "is_current": current},
    }


def save_instruments(data_dir, frame):
    path = data_dir / "instruments" / "instruments.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(path)
    return path


def repo_at(data_dir):
    return SimpleNamespace(store=SimpleNamespace(data_dir=data_dir))


def bars(symbols, *, halted=False):
    return pl.DataFrame({
        "symbol": symbols, "date": [DAY] * len(symbols),
        **{column: [value] * len(symbols) for column, value in {
            "open": 0.0 if halted else 10.0,
            "high": 0.0 if halted else 11.0,
            "low": 0.0 if halted else 9.0,
            "close": 10.0,
            "volume": 0.0 if halted else 100.0,
            "amount": 0.0 if halted else 100000.0,
        }.items()},
    })


@pytest.fixture
def tickflow(monkeypatch):
    items = [instrument(symbol) for symbol in MEMBERS]
    items.append(instrument(DELISTED, name="国华退"))
    by_exchange = {
        exchange: [row for row in items if row["exchange"] == exchange]
        for exchange in ("SH", "SZ", "BJ")
    }
    client = SimpleNamespace(
        exchanges=SimpleNamespace(get_instruments=Mock(
            side_effect=lambda exchange, **_: by_exchange[exchange],
        )),
        universes=SimpleNamespace(get=Mock(return_value={
            "id": "CN_Equity_A", "symbols": MEMBERS.copy(), "symbol_count": len(MEMBERS),
        })),
    )
    monkeypatch.setattr(instrument_sync, "get_client", lambda: client)
    monkeypatch.setattr(instrument_sync, "cn_today", lambda: DAY)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")
    return client


def test_strict_sync_excludes_delisted_members_but_preserves_history(tmp_path, tickflow):
    assert instrument_sync.sync_instruments(tmp_path, require_complete=True) == 5
    path = tmp_path / "instruments" / "instruments.parquet"
    frame = pl.read_parquet(path)
    assert frame.schema["is_current"] == pl.Boolean
    assert set(frame["symbol"]) == {*MEMBERS, DELISTED}
    assert frame.filter(pl.col("symbol") == DELISTED)["is_current"].item() is False
    assert postmarket.stock_universe(repo_at(tmp_path), DAY) == sorted(MEMBERS)
    tickflow.universes.get.assert_called_once_with("CN_Equity_A")


def test_current_halted_member_still_requires_target_day_evidence(tmp_path, tickflow):
    instrument_sync.sync_instruments(tmp_path, require_complete=True)
    expected = postmarket.stock_universe(repo_at(tmp_path), DAY)
    coverage = DailyCoverage(DAY)
    coverage.observe(bars([symbol for symbol in MEMBERS if symbol != HALTED]))
    with pytest.raises(DailyCoverageError, match=HALTED):
        coverage.require(expected)
    coverage.observe(bars([HALTED], halted=True))
    assert coverage.require(expected) == {
        "date": DAY.isoformat(), "expected": 4, "active": 3, "halted": 1,
    }


@pytest.mark.parametrize("with_membership", [False, True])
def test_stock_universe_filters_future_listing_and_nonstocks_with_legacy_compatibility(
    tmp_path, with_membership,
):
    symbols = [ACTIVE, HALTED, "001999.SZ", "510300.SH", DELISTED]
    frame = pl.DataFrame({
        "symbol": symbols, "as_of": [DAY] * len(symbols),
        "listing_date": ["2000-01-01", None, "2026-10-22", "2000-01-01", "1990-01-01"],
        "type": ["stock", None, "stock", "etf", "stock"],
    })
    if with_membership:
        frame = frame.with_columns(pl.Series("is_current", [True, None, True, True, False]))
    save_instruments(tmp_path, frame)
    expected = [ACTIVE, HALTED] if with_membership else [ACTIVE, HALTED, DELISTED]
    assert postmarket.stock_universe(repo_at(tmp_path), DAY) == sorted(expected)


def test_custom_source_optional_membership_survives_sync_without_tickflow(
    tmp_path, monkeypatch,
):
    rows = [instrument(ACTIVE, current=True), instrument(DELISTED, current=False),
            instrument(HALTED)]
    provider = SimpleNamespace(get_instruments=Mock(return_value=rows))
    monkeypatch.setattr(instrument_sync, "cn_today", lambda: DAY)
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "custom-source")
    monkeypatch.setattr(custom_sources, "is_custom_provider", lambda name: name == "custom-source")
    monkeypatch.setattr(custom_sources, "get_provider", lambda _: provider)
    get_client = Mock(side_effect=AssertionError("自定义维表不应访问 TickFlow"))
    monkeypatch.setattr(instrument_sync, "get_client", get_client)
    assert instrument_sync.sync_instruments(tmp_path, require_complete=True) == 3
    frame = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert frame.schema["is_current"] == pl.Boolean
    assert dict(zip(frame["symbol"], frame["is_current"], strict=True)) == {
        ACTIVE: True, DELISTED: False, HALTED: None,
    }
    assert postmarket.stock_universe(repo_at(tmp_path), DAY) == sorted([ACTIVE, HALTED])
    provider.get_instruments.assert_called_once_with("stock")
    get_client.assert_not_called()


def test_normal_refresh_does_not_reuse_membership_and_strict_retry_fetches_again(
    tmp_path, tickflow,
):
    instrument_sync.sync_instruments(tmp_path, require_complete=True)
    instrument_sync.sync_instruments(tmp_path)
    frame = pl.read_parquet(tmp_path / "instruments" / "instruments.parquet")
    assert frame.schema["is_current"] == pl.Boolean
    assert frame["is_current"].null_count() == frame.height
    tickflow.universes.get.assert_called_once_with("CN_Equity_A")

    updated_members = [symbol for symbol in MEMBERS if symbol != "000001.SZ"]
    tickflow.universes.get.return_value = {"symbols": updated_members}
    instrument_sync.sync_instruments(tmp_path, require_complete=True)
    assert tickflow.universes.get.call_count == 2
    assert postmarket.stock_universe(repo_at(tmp_path), DAY) == sorted(updated_members)


@pytest.mark.parametrize("payload", [
    None,
    {},
    {"symbols": []},
    {"symbols": [ACTIVE, ACTIVE]},
    {"symbols": [""]},
    {"symbols": ACTIVE},
    {"symbols": [123]},
    {"symbols": [ACTIVE], "symbol_count": 2},
    {"symbols": [ACTIVE, "600999.SH"]},
])
def test_invalid_or_incomplete_membership_does_not_replace_existing_file(
    tmp_path, tickflow, payload,
):
    path = save_instruments(tmp_path, pl.DataFrame({
        "symbol": [ACTIVE], "as_of": [DAY - timedelta(days=1)], "is_current": [True],
    }))
    original = path.read_bytes()
    tickflow.universes.get.return_value = payload
    with pytest.raises(RuntimeError):
        instrument_sync.sync_instruments(tmp_path, require_complete=True)
    assert path.read_bytes() == original


def test_membership_request_failure_does_not_replace_existing_file(tmp_path, tickflow):
    path = save_instruments(tmp_path, pl.DataFrame({
        "symbol": [ACTIVE], "as_of": [DAY - timedelta(days=1)],
    }))
    original = path.read_bytes()
    tickflow.universes.get.side_effect = RuntimeError("上游暂不可用")
    with pytest.raises(RuntimeError):
        instrument_sync.sync_instruments(tmp_path, require_complete=True)
    assert path.read_bytes() == original


def test_membership_without_optional_count_is_accepted(tmp_path, tickflow):
    tickflow.universes.get.return_value = {"symbols": MEMBERS.copy()}
    instrument_sync.sync_instruments(tmp_path, require_complete=True)
    assert postmarket.stock_universe(repo_at(tmp_path), DAY) == sorted(MEMBERS)
