from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.screener import router
from app.indicators.pipeline import compute_indicators, compute_signals
from app.services.snapshot_cache import SnapshotCache
from app.services.strength_ladder import build_strength_ladder, get_strength_ladder
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 10, 22)


def _frame(momenta):
    count = len(momenta)
    return pl.DataFrame({
        "symbol": [f"{600000 + i}.SH" for i in range(count)], "name": ["测试"] * count,
        "close": [10.0] * count, "change_pct": [0.01] * count,
        "momentum_20d": momenta, "vol_ratio_5d": [1.0] * count,
        "high_60d": [12.0] * count, "low_60d": [8.0] * count,
    })


def _stocks(result):
    return [stock for tier in result["tiers"] for stock in tier["stocks"]]


def test_exact_momentum_boundaries_and_descending_order():
    result = build_strength_ladder(_frame([0.0299, 0.03, 0.0799, 0.08, 0.1499, 0.15, 0.2499, 0.25, 0.3]), DAY)
    assert [(tier["level"], tier["count"]) for tier in result["tiers"]] == [(5, 2), (4, 2), (3, 2), (2, 2)]
    assert [stock["momentum_20d"] for stock in _stocks(result)] == [0.3, 0.25, 0.2499, 0.15, 0.1499, 0.08, 0.0799, 0.03]
    assert result["eligible"] == result["total"] == 9
    assert all(stock["status"] == "momentum" for stock in _stocks(result))
    price_boundaries = build_strength_ladder(_frame([price / 10 - 1 for price in (10.3, 10.8, 11.5, 12.5)]), DAY)
    assert [(tier["level"], tier["count"]) for tier in price_boundaries["tiers"]] == [(5, 1), (4, 1), (3, 1), (2, 1)]


def test_status_priority_volume_threshold_and_high_counts_share_tolerance():
    frame = _frame([0.1] * 5).with_columns(
        pl.Series("close", [9.95, 9.949, 10.0, 10.0, 8.04]),
        pl.Series("high_60d", [10.0, 10.0, 12.0, 12.0, 12.0]),
        pl.Series("vol_ratio_5d", [2.0, 1.5, 1.5, 2.0, 1.0]),
        pl.Series("change_pct", [0.01, 0.01, 0.01, 0.0, -0.01]),
        pl.Series("signal_n_day_high", [False] * 5),
    )
    result = build_strength_ladder(frame, DAY)
    stocks = {stock["symbol"]: stock for stock in _stocks(result)}
    assert [stocks[f"{600000 + i}.SH"]["status"] for i in range(5)] == ["high", "volume", "volume", "momentum", "momentum"]
    assert result["counts"] == {"near_high": 1, "near_low": 1}
    assert sum(stock["status"] == "high" for stock in stocks.values()) == result["counts"]["near_high"]
    # 新高低统计覆盖全市场，不仅是动量入榜股票。
    below = frame.with_columns(pl.lit(0.01).alias("momentum_20d"))
    assert build_strength_ladder(below, DAY)["counts"] == result["counts"]


def test_missing_and_nonfinite_data_never_become_breakouts_or_zero_values():
    frame = _frame([0.1, None, float("nan"), float("inf"), 0.1]).with_columns(
        pl.Series("close", [10.0, 10.0, 10.0, 10.0, float("inf")]),
        pl.lit(None, dtype=pl.Float64).alias("high_60d"),
        pl.lit(float("nan")).alias("vol_ratio_5d"),
    )
    result = build_strength_ladder(frame, DAY)
    assert result["eligible"] == 1
    assert result["counts"]["near_high"] == 0
    assert len(_stocks(result)) == 1
    assert _stocks(result)[0]["status"] == "momentum"
    assert _stocks(result)[0]["vol_ratio_5d"] is None
    assert result["warnings"]
    json.dumps(result, allow_nan=False)
    missing = build_strength_ladder(frame.drop("momentum_20d", "high_60d", "low_60d"), DAY)
    assert missing["tiers"] == []
    assert missing["eligible"] == 0
    assert missing["warnings"]


def _historical_repo(tmp_path):
    days = []
    current = date(2026, 6, 1)
    while len(days) < 62:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    frames = []
    for i, day in enumerate(days):
        close = 10.0 if i < 60 else (11.0 if i == 60 else 200.0)
        frame = pl.DataFrame({
            "symbol": ["600000.SH"], "date": [day], "open": [close], "high": [close], "low": [close],
            "close": [close], "raw_close": [close * 10], "raw_high": [close * 10], "raw_low": [close * 10],
            "volume": [100.0], "amount": [close * 100],
        })
        root = tmp_path / "kline_daily_enriched" / f"date={day.isoformat()}"
        root.mkdir(parents=True)
        frame.write_parquet(root / "part.parquet")
        frames.append(frame)
    latest = compute_signals(compute_indicators(pl.concat(frames))).filter(pl.col("date") == days[-1])
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path), enriched_latest_date=lambda: days[-1],
        get_enriched_latest_asset=lambda asset: (latest, days[-1]), get_enriched_history=lambda *args: None,
        get_instruments_asset=lambda asset: pl.DataFrame(), get_historical_shares=lambda: pl.DataFrame(),
    )
    return repo, days


def test_historical_route_uses_trading_bars_adjusted_prices_and_no_future_rows(tmp_path):
    repo, days = _historical_repo(tmp_path)
    app = FastAPI()
    app.state.repo = repo
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/api/screener/strength-ladder", params={"as_of": days[60].isoformat()})
        assert response.status_code == 200
        historical = response.json()
        stock = _stocks(historical)[0]
        assert historical["as_of"] == days[60].isoformat()
        assert stock["close"] == 11.0
        assert stock["momentum_20d"] == pytest.approx(0.1)
        assert historical["tiers"][0]["level"] == 3
        assert stock["status"] == "high"
        # 最新日内存快照与历史分区分开，回看历史不能读到未来的大涨。
        latest = client.get("/api/screener/strength-ladder").json()
        assert latest["as_of"] == days[-1].isoformat()
        assert latest["tiers"][0]["level"] == 5
        assert client.get("/api/screener/strength-ladder", params={"as_of": days[60].isoformat()}).json() == historical
        assert client.get("/api/screener/strength-ladder?as_of=invalid").status_code == 422


def test_empty_database_and_missing_historical_date_are_explicit():
    repo = SimpleNamespace(enriched_latest_date=lambda: None, execute_one=lambda *args: None)
    result = get_strength_ladder(repo)
    assert result["as_of"] is None
    assert result["tiers"] == []
    assert result["eligible"] == result["total"] == 0
    assert build_strength_ladder(pl.DataFrame(), DAY)["as_of"] == DAY.isoformat()


@pytest.mark.parametrize("history_cache", ["cold", "partial", "complete"])
def test_historical_ladder_keeps_resumed_stock_across_long_suspension(tmp_path, monkeypatch, history_cache):
    """61根有效日线跨两年停牌，次日回看仍应命中10%动量和60日新高。"""
    target = date(2026, 10, 16)
    following = date(2026, 10, 19)
    old_days = []
    day = date(2024, 1, 1)
    while len(old_days) < 60:
        if day.weekday() < 5:
            old_days.append(day)
        day += timedelta(days=1)

    rows = []

    def add(symbol, day, close):
        rows.append({
            "symbol": symbol, "date": day, "open": close, "high": close, "low": close,
            "close": close, "raw_close": close * 10, "raw_high": close * 10, "raw_low": close * 10,
            "volume": 100.0, "amount": close * 100,
        })

    for i, day in enumerate(old_days):
        add("600000.SH", day, 20.0 if i == 0 else 10.0)
        add("600002.SH", day, 10.0)  # 目标日仍停牌，不得混入目标日股票池。
    add("600000.SH", target, 11.0)
    add("600000.SH", following, 200.0)  # 不能泄漏到历史动量和新高窗口。
    for day in (target, following):
        add("301001.SZ", day, 3.0)  # 真正不足21/60根的新股仍应缺值。
    day = target - timedelta(days=300)
    while day <= following:
        if day.weekday() < 5:
            add("000001.SZ", day, 12.0 if day < target - timedelta(days=30) else 10.0)
        day += timedelta(days=1)

    raw = pl.DataFrame(rows).sort(["symbol", "date"])
    for part in raw.partition_by("date"):
        root = tmp_path / "kline_daily_enriched" / f"date={part['date'][0].isoformat()}"
        root.mkdir(parents=True)
        part.write_parquet(root / "part.parquet")
    computed = compute_signals(compute_indicators(raw))
    repo = KlineRepository(DataStore(tmp_path))
    repo._enriched_cache = computed.filter(pl.col("date") == target)
    repo._enriched_cache_date = target
    expected = get_strength_ladder(repo, target)
    assert _stocks(expected)[0]["momentum_20d"] == 0.1
    assert expected["eligible"] == 2 and expected["counts"]["near_high"] == 1
    assert expected["total"] == 3

    repo._enriched_cache = computed.filter(pl.col("date") == following)
    repo._enriched_cache_date = following
    if history_cache == "partial":
        recent = raw.filter(pl.col("date") >= target - timedelta(days=300))
        repo._enriched_history_cache = compute_signals(compute_indicators(recent))
    elif history_cache == "complete":
        repo._enriched_history_cache = computed
    monkeypatch.setattr("app.services.screener._history_cache", SnapshotCache(ttl=120, maxsize=10))

    def unexpected_scan(_path):
        pytest.fail("完整缓存或已补足的重复查询不应重新读取Parquet")

    historical = get_strength_ladder(repo, target)
    assert historical == expected
    monkeypatch.setattr("app.services.screener.scan_enriched_parquet", unexpected_scan)
    assert get_strength_ladder(repo, target) == expected
