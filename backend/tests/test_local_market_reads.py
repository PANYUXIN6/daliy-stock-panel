"""浏览只能消费后台结果；冷缓存和残缺分钟均不能触发供应商请求。"""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import indices, kline
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


class Repo:
    def __init__(self, data_dir):
        self.store = SimpleNamespace(data_dir=data_dir)
        self.rows = pl.DataFrame({
            "symbol": ["600000.SH"], "datetime": [datetime(2026, 10, 9, 10)], "close": [10.5],
        })

    def resolve_asset_type(self, symbol):
        return "index" if symbol == "000001.SH" else "stock"

    def get_instruments(self):
        return pl.DataFrame()

    get_index_instruments = get_instruments

    def get_daily_asset(self, *args, **kwargs):
        return pl.DataFrame()

    get_index_daily = get_daily_asset

    def get_minute(self, *args, **kwargs):
        return self.rows

    def get_minute_batch(self, *args, **kwargs):
        return self.rows

    def get_etf_symbol_set(self):
        return set()

    def get_index_symbol_set(self):
        return {"000001.SH"}


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    app = FastAPI()
    app.include_router(kline.router)
    app.include_router(indices.router)
    app.state.repo = Repo(tmp_path)
    app.state.capabilities = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits()})
    forbidden = Mock(side_effect=AssertionError("浏览不得调用供应商"))
    for name in ("sync_daily_batch", "fetch_adj_factor_single", "fetch_minute_single", "sync_minute_batch"):
        monkeypatch.setattr(kline.kline_sync, name, forbidden)
    monkeypatch.setattr(kline, "cn_today", lambda: date(2026, 10, 9))
    monkeypatch.setattr(indices, "cn_today", lambda: date(2026, 10, 9))
    return TestClient(app), app.state.repo, forbidden


@pytest.mark.parametrize("path", ["/api/kline/daily", "/api/index/daily"])
def test_empty_daily_does_not_fetch(local_client, path):
    client, _, forbidden = local_client
    for _ in range(2):
        response = client.get(path, params={"symbol": "000001.SH", "start_date": "2026-10-01", "end_date": "2026-10-09"})
        assert response.status_code == 200
        assert response.json()["rows"] == []
    forbidden.assert_not_called()


@pytest.mark.parametrize("path", ["/api/kline/minute", "/api/index/minute"])
def test_partial_minute_stays_local_even_live(local_client, path):
    client, _, forbidden = local_client
    for _ in range(2):
        response = client.get(path, params={"symbol": "600000.SH", "date": "2026-10-09", "live": True})
        assert response.status_code == 200
        assert response.json()["rows"][0]["close"] == 10.5
        assert response.json()["source"] == "local"
    forbidden.assert_not_called()


def test_batch_returns_partial_local_and_keeps_incremental_contract(local_client):
    client, _, forbidden = local_client
    response = client.post("/api/kline/minute-batch", json={"symbols": ["600000.SH", "600001.SH"], "date": "2026-10-09", "since": "2026-10-09T10:00:00"})
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["600000.SH"][0]["close"] == 10.5
    assert "600001.SH" not in body["data"]
    assert body["incremental"] is True
    forbidden.assert_not_called()
