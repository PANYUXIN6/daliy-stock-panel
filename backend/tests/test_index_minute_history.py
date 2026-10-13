"""指数分钟日期、资产隔离与后台更新可见性。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import indices


@pytest.fixture
def context(monkeypatch):
    monkeypatch.setattr(indices, "cn_today", lambda: date(2026, 9, 14))
    monkeypatch.setattr(indices.trading_day, "is_trading_day", lambda: False)
    repo = MagicMock()
    repo.get_index_instruments.return_value = pl.DataFrame({"symbol": ["000001.SH"], "name": ["上证指数"]})
    repo.latest_minute_date.return_value = date(2026, 9, 11)
    repo.get_minute.return_value = pl.DataFrame({"datetime": [datetime(2026, 9, 11, 9, 35)], "close": [3000.]})
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


@pytest.mark.parametrize("verdict,expected", [(False, "2026-09-11"), (None, "2026-09-11"), (True, "2026-09-14")])
def test_default_date_uses_local_calendar_and_index_history(context, monkeypatch, verdict, expected):
    monkeypatch.setattr(indices.trading_day, "is_trading_day", lambda: verdict)
    result = indices.get_index_minute(context, symbol="000001.SH", trade_date=None)
    assert result["date"] == expected
    assert context.app.state.repo.get_minute.call_args.kwargs["asset_type"] == "index"


def test_explicit_missing_date_stays_empty(context):
    context.app.state.repo.get_minute.return_value = pl.DataFrame()
    result = indices.get_index_minute(context, symbol="000001.SH", trade_date=date(2026, 9, 12))
    assert result["date"] == "2026-09-12"
    assert result["rows"] == []
    assert result["source"] == "none"
    context.app.state.repo.latest_minute_date.assert_not_called()


def test_future_date_does_not_read_or_fetch(context):
    result = indices.get_index_minute(context, symbol="000001.SH", trade_date=date(2026, 9, 15))
    assert result["rows"] == []
    assert result["source"] == "future"
    context.app.state.repo.get_minute.assert_not_called()


def test_background_update_visible_without_provider_ttl(context):
    day = date(2026, 9, 11)
    first = indices.get_index_minute(context, symbol="000001.SH", trade_date=day)
    context.app.state.repo.get_minute.return_value = pl.DataFrame({"datetime": [datetime(2026, 9, 11, 9, 36)], "close": [3001.]})
    second = indices.get_index_minute(context, symbol="000001.SH", trade_date=day)
    assert first["rows"][0]["close"] == 3000.
    assert second["rows"][0]["close"] == 3001.


def test_concurrent_dates_remain_isolated(context):
    context.app.state.repo.get_minute.side_effect = lambda symbol, day, **kwargs: pl.DataFrame({"datetime": [datetime.combine(day, datetime.min.time())], "close": [day.day]})
    days = [date(2026, 9, n) for n in range(1, 9)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda day: indices.get_index_minute(context, symbol="000001.SH", trade_date=day), days))
    assert [result["rows"][0]["close"] for result in results] == list(range(1, 9))


def test_http_date_alias_and_validation(context):
    app = FastAPI()
    app.state.repo = context.app.state.repo
    app.include_router(indices.router)
    with TestClient(app) as client:
        result = client.get("/api/index/minute?symbol=000001.SH&date=2026-09-11")
        assert result.json()["rows"][0]["datetime"] == "2026-09-11T09:35:00"
        assert client.get("/api/index/minute?symbol=000001.SH").json()["date"] == "2026-09-11"
        assert client.get("/api/index/minute?symbol=000001.SH&date=invalid").status_code == 422
