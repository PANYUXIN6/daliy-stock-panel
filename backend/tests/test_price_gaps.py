from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import kline
from app.indicators.price_gaps import compute_price_gaps
from app.services.price_gaps import get_price_gaps


def prices(intervals):
    return pl.DataFrame([
        {"symbol": "600483.SH", "date": date(2026, 1, 1) + timedelta(days=i),
         "low": float(low), "high": float(high), "open": (low + high) / 2,
         "close": (low + high) / 2, "volume": 100., "amount": 1000.}
        for i, (low, high) in enumerate(intervals)
    ])


@pytest.mark.parametrize("intervals,direction,original,remaining,filled", [
    ([(9, 10), (12, 13), (11, 12.5)], "up", (10, 12), (10, 11), (10, 11.2)),
    ([(12, 13), (9, 10), (9.5, 11)], "down", (10, 12), (11, 12), (10.5, 12)),
])
def test_both_directions_shrink_and_disappear_at_full_fill(intervals, direction, original, remaining, filled):
    frame = prices(intervals)
    gaps = compute_price_gaps(frame)[1]
    assert gaps == [{"date": "2026-01-02", "direction": direction,
                     "original_lower": original[0], "original_upper": original[1],
                     "lower": remaining[0], "upper": remaining[1]}]
    # 远端边界触及即完全回补，之后再次离开不恢复原缺口。
    fully_filled = prices([*intervals, filled, (11, 12)])
    assert not any(gap["date"] == "2026-01-02" for gap in compute_price_gaps(fully_filled)[1])


def test_near_edge_touch_is_not_full_fill_and_no_minimum_gap_width():
    frame = prices([(9, 10), (10.01, 11), (10.01, 10.5)])
    gap = compute_price_gaps(frame)[1][0]
    assert gap["lower"] == 10 and gap["upper"] == 10.01
    assert compute_price_gaps(prices([(9, 10), (10, 11), (9, 10)]))[1] == []


def test_all_gaps_preserved_without_level_aggregation_or_three_gap_limit():
    gaps = compute_price_gaps(prices([(9, 10), (11, 11.5), (12, 12.5), (13, 13.5), (14, 14.5)]))[1]
    assert len(gaps) == 4
    assert [gap["lower"] for gap in gaps] == [10, 11.5, 12.5, 13.5]


def test_halt_rows_do_not_fill_or_form_gaps_and_single_row_has_none():
    frame = prices([(9, 10), (0, 0), (12, 13)]).with_columns(
        pl.when(pl.col("low") == 0).then(0.).otherwise(pl.col("volume")).alias("volume"),
        pl.when(pl.col("low") == 0).then(0.).otherwise(pl.col("amount")).alias("amount"),
    )
    assert compute_price_gaps(frame)[1][0]["date"] == "2026-01-03"
    assert compute_price_gaps(frame.head(1))[1] == []
    assert compute_price_gaps(pl.DataFrame())[1] == []


def test_service_cutoff_preserves_historical_gap_and_replaces_live_tail():
    frame = prices([(9, 10), (12, 13), (11, 12.5), (10, 11.5)])
    repo = SimpleNamespace(resolve_asset_type=lambda symbol: "stock", get_daily_asset=lambda *a, **k: frame)
    historical = get_price_gaps(repo, "600483.SH", date(2026, 1, 3))
    assert historical["gaps"][0]["upper"] == 11
    assert historical["history_start"] == "2026-01-01" and historical["as_of"] == "2026-01-03"
    assert get_price_gaps(repo, "600483.SH", date(2026, 1, 4))["gaps"] == []
    # 同日尾部替换而非重复追加；未来行不会影响历史截止日。
    live = {**frame.row(2, named=True), "date": "2026-01-03", "low": 10.}
    assert get_price_gaps(repo, "600483.SH", date(2026, 1, 3), live_row=live)["gaps"] == []


def test_api_local_read_etf_validation_live_and_read_failure(monkeypatch):
    frame = prices([(9, 10), (12, 13), (11, 12.5)])
    calls = []
    def read(asset, symbol, start, end, *, columns):
        calls.append((asset, start, end, columns))
        return frame
    repo = SimpleNamespace(resolve_asset_type=lambda symbol: "etf", get_daily_asset=read)
    app = FastAPI()
    app.include_router(kline.router)
    app.state.repo = repo
    client = TestClient(app)
    params = {"symbol": "510300.SH", "end_date": "2026-01-03"}
    response = client.get("/api/kline/gaps", params=params)
    assert response.status_code == 200 and response.json()["gaps"][0]["upper"] == 11
    assert calls[0][0] == "etf" and calls[0][1] == date(1990, 1, 1)
    assert calls[0][3] == ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    assert client.get("/api/kline/gaps", params={**params, "symbol": "../x"}).status_code == 422
    assert client.get("/api/kline/gaps", params={**params, "end_date": "bad"}).status_code == 422
    monkeypatch.setattr(kline, "cn_today", lambda: date(2026, 1, 3))
    monkeypatch.setattr(kline, "_latest_live_candle", lambda *a, **k: {
        **frame.row(2, named=True), "date": "2026-01-03", "low": 10.,
    })
    assert client.get("/api/kline/gaps", params=params).json()["gaps"] == []
    monkeypatch.setattr(kline, "_latest_live_candle", lambda *a, **k: None)
    repo.get_daily_asset = lambda *a, **k: pl.DataFrame()
    assert client.get("/api/kline/gaps", params=params).json()["status"] == "no_prices"
    def broken(*a, **k):
        raise OSError("bad local parquet")
    repo.get_daily_asset = broken
    assert client.get("/api/kline/gaps", params=params).status_code == 503
