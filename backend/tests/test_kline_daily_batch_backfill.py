"""迷你日 K 跨停牌空档读取本地历史，不触发补采或放大正常标的读取。"""
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import kline as kline_api
from app.tickflow.repository import DataStore, KlineRepository

TODAY = date(2026, 3, 2)
NORMAL = "600000.SH"
SUSPENDED = "002388.SZ"


def _frame(symbol, dates):
    return pl.DataFrame({
        "symbol": [symbol] * len(dates), "date": dates,
        "open": [10.0] * len(dates), "high": [10.5] * len(dates),
        "low": [9.5] * len(dates), "close": [10.2] * len(dates),
        "volume": [100.0] * len(dates),
    })


def _request(repo):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(kline_api, "cn_today", lambda: TODAY)
    result = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(result, "resolve_asset_type", lambda symbol: "stock")
    return result


def _spy_stock_read(repo, monkeypatch, history):
    calls = []

    def read(symbols, start, end, columns=None):
        calls.append((list(symbols), start, end))
        return history.filter(
            pl.col("symbol").is_in(symbols)
            & pl.col("date").is_between(start, end),
        ).select(columns)

    monkeypatch.setattr(repo, "get_daily_batch", read)
    return calls


def test_backfill_only_short_stock_and_keeps_latest_n_bars(repo, monkeypatch):
    recent = [TODAY - timedelta(days=i * 2) for i in range(12)]
    older = [TODAY - timedelta(days=50 + i * 2) for i in range(20)]
    history = pl.concat([_frame(NORMAL, recent), _frame(SUSPENDED, recent[:3] + older)])
    calls = _spy_stock_read(repo, monkeypatch, history)

    data = kline_api.get_daily_batch(_request(repo), {"symbols": [NORMAL, SUSPENDED]})["data"]

    assert len(data[NORMAL]) == len(data[SUSPENDED]) == 12
    assert [row["date"] for row in data[SUSPENDED]] == sorted(recent[:3] + older)[-12:]
    assert [call[0] for call in calls] == [[NORMAL, SUSPENDED], [SUSPENDED]]
    assert all(call[2] == TODAY for call in calls)


def test_empty_initial_window_still_reads_prior_local_bars(repo, monkeypatch):
    older = [TODAY - timedelta(days=200 + i * 2) for i in range(12)]
    calls = _spy_stock_read(repo, monkeypatch, _frame(SUSPENDED, older))

    rows = kline_api.get_daily_batch(_request(repo), {"symbols": [SUSPENDED]})["data"][SUSPENDED]

    assert [row["date"] for row in rows] == sorted(older)
    assert len(calls) == 3


def test_backfill_stops_at_bound_and_keeps_short_history(repo, monkeypatch):
    dates = [TODAY, TODAY - timedelta(days=200), TODAY - timedelta(days=900)]
    calls = _spy_stock_read(repo, monkeypatch, _frame(SUSPENDED, dates))

    rows = kline_api.get_daily_batch(_request(repo), {"symbols": [SUSPENDED], "days": 5})["data"][SUSPENDED]

    assert [row["date"] for row in rows] == sorted(dates[:2])
    assert len(calls) == 3
    assert calls[-1][1] == TODAY - timedelta(days=10 + 480)


@pytest.mark.parametrize("asset,symbol", [("etf", "510300.SH"), ("index", "000001.SH")])
def test_backfill_preserves_asset_route_and_excludes_future(repo, monkeypatch, asset, symbol):
    # 使用实际独立存储验证边界，其他资产中的同代码不能混进结果。
    older = [TODAY - timedelta(days=50 + i * 2) for i in range(6)]
    repo.append_enriched_asset(asset, _frame(symbol, [*older, TODAY + timedelta(days=1)]))
    repo.append_enriched_asset("stock", _frame(symbol, [TODAY]))
    monkeypatch.setattr(repo, "resolve_asset_type", lambda value: asset)
    monkeypatch.setattr(repo, "get_daily_batch", lambda *args, **kwargs: pytest.fail("wrong stock route"))

    rows = kline_api.get_daily_batch(_request(repo), {"symbols": [symbol], "days": 5})["data"][symbol]

    assert [row["date"] for row in rows] == sorted(older)[-5:]


def test_no_saved_history_returns_empty_after_bounded_reads(repo, monkeypatch):
    calls = []

    def read(*args, **kwargs):
        calls.append(args)
        return pl.DataFrame()

    monkeypatch.setattr(repo, "get_daily_batch", read)
    assert kline_api.get_daily_batch(_request(repo), {"symbols": [SUSPENDED]}) == {"data": {}}
    assert len(calls) == 3
