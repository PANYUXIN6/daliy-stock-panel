"""历史日线的实时行也必须遵守请求窗口；使用临时仓库验证 HTTP 输出。"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import indices, kline
from app.tickflow.repository import DataStore, KlineRepository

TODAY = date(2026, 10, 12)
HISTORY = [date(2026, 10, 8), date(2026, 10, 9)]
ASSETS = {"stock": "600000.SH", "etf": "510300.SH", "index": "000001.SH"}
ROUTES = [
    pytest.param("/api/kline/daily", "stock", id="stock"),
    pytest.param("/api/kline/daily", "etf", id="etf"),
    pytest.param("/api/kline/daily", "index", id="index-detail"),
    pytest.param("/api/index/daily", "index", id="index-page"),
]


def _frame(symbol: str, dates: list[date], close: float) -> pl.DataFrame:
    return pl.DataFrame([
        {
            "symbol": symbol, "date": day,
            "open": close, "high": close + 1, "low": close - 1, "close": close,
            "volume": 1_000.0, "amount": close * 1_000,
        }
        for day in dates
    ])


@pytest.fixture
def daily_client(tmp_path, monkeypatch):
    monkeypatch.setattr(kline, "cn_today", lambda: TODAY)
    monkeypatch.setattr(indices, "cn_today", lambda: TODAY)
    monkeypatch.setattr("app.services.preferences.get_daily_batch_compress", lambda: False)
    stores = []

    def make_client(*, include_today: bool = True):
        store = DataStore(tmp_path, read_only=True)
        stores.append(store)
        repo = KlineRepository(store)
        dates = [date(2026, 10, 7), *HISTORY]
        if include_today:
            dates.append(TODAY)
        dates.append(date(2026, 10, 13))
        frames = {"_enriched_cache": pl.DataFrame()}
        for asset, symbol in ASSETS.items():
            table = "kline_daily_enriched" if asset == "stock" else f"kline_{asset}_enriched"
            _frame(symbol, dates, 10.0).write_parquet(tmp_path / table / "part.parquet")
            instruments = pl.DataFrame({
                "symbol": [symbol], "name": [asset],
                "total_shares": [1e9], "float_shares": [1e9],
            })
            prefix = "" if asset == "stock" else f"{asset}_"
            frames[f"_{prefix}instruments_cache"] = instruments
            if asset != "stock":
                frames[f"_{asset}_enriched_cache"] = _frame(symbol, [TODAY], 12.0)
        repo.install_shared_snapshot(frames, {
            "_etf_enriched_cache_date": TODAY.isoformat(),
            "_index_enriched_cache_date": TODAY.isoformat(),
        })
        app = FastAPI()
        app.include_router(kline.router)
        app.include_router(indices.router)
        app.state.repo = repo
        app.state.quote_service = SimpleNamespace(
            get_enriched_today=lambda: (_frame(ASSETS["stock"], [TODAY], 12.0), TODAY),
        )
        return TestClient(app)

    yield make_client
    for store in stores:
        store.db.close()


@pytest.mark.parametrize(("path", "asset"), ROUTES)
@pytest.mark.parametrize("explicit_start", [True, False], ids=["explicit-range", "end-and-days"])
def test_historical_daily_excludes_live_today(daily_client, path, asset, explicit_start):
    params = {"symbol": ASSETS[asset], "end_date": "2026-10-09", "days": 10}
    if explicit_start:
        params["start_date"] = "2026-10-08"
    response = daily_client().get(path, params=params)

    assert response.status_code == 200
    rows = response.json()["rows"]
    expected = HISTORY if explicit_start else [date(2026, 10, 7), *HISTORY]
    assert [row["date"] for row in rows] == [day.isoformat() for day in expected]
    assert all(row["close"] == 10.0 and not row.get("is_live") for row in rows)


@pytest.mark.parametrize(("path", "asset"), ROUTES)
@pytest.mark.parametrize("include_today", [False, True], ids=["append-default-end", "replace-inclusive-end"])
def test_daily_including_today_keeps_live_candle(daily_client, path, asset, include_today):
    params = {"symbol": ASSETS[asset], "start_date": "2026-10-08"}
    if include_today:
        params["end_date"] = TODAY.isoformat()
    response = daily_client(include_today=include_today).get(path, params=params)

    assert response.status_code == 200
    rows = response.json()["rows"]
    assert [row["date"] for row in rows] == [day.isoformat() for day in [*HISTORY, TODAY]]
    assert [row["close"] for row in rows] == [10.0, 10.0, 12.0]
    assert rows[-1]["is_live"] is True


@pytest.mark.parametrize(("path", "asset"), ROUTES)
def test_daily_starting_after_today_excludes_live_today(daily_client, path, asset):
    response = daily_client().get(path, params={
        "symbol": ASSETS[asset], "start_date": "2026-10-13", "end_date": "2026-10-13",
    })

    assert response.status_code == 200
    rows = response.json()["rows"]
    assert [row["date"] for row in rows] == ["2026-10-13"]
    assert rows[0]["close"] == 10.0
    assert not rows[0].get("is_live")


def test_daily_crossing_midnight_still_respects_end_date(daily_client, monkeypatch):
    client = daily_client()
    next_day = date(2026, 10, 13)

    def next_session_quote():
        monkeypatch.setattr(kline, "cn_today", lambda: next_day)
        return _frame(ASSETS["stock"], [next_day], 12.0), next_day

    client.app.state.quote_service.get_enriched_today = next_session_quote
    response = client.get("/api/kline/daily", params={
        "symbol": ASSETS["stock"], "start_date": "2026-10-08", "end_date": TODAY.isoformat(),
    })

    assert response.status_code == 200
    rows = response.json()["rows"]
    assert [row["date"] for row in rows] == [day.isoformat() for day in [*HISTORY, TODAY]]
    assert all(row["close"] == 10.0 and not row.get("is_live") for row in rows)
