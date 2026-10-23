"""保存的盘中快照与实时量比同口径，正式日线和重算时钟不改变结果。"""
from datetime import date, datetime, timedelta

import polars as pl
import pytest

from app.indicators.pipeline import compute_enriched_today, compute_indicators
from app.indicators.volume_ratio import volume_time_factor
from app.market_time import CN_TZ, trading_minutes_elapsed_from_dt
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 10, 22)


def _timestamp(hour, minute, second=0, *, day=DAY):
    return int(datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=CN_TZ).timestamp() * 1000)


def _bars(timestamp):
    days = []
    day = DAY - timedelta(days=1)
    while len(days) < 65:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days = [*sorted(days), DAY]
    return pl.DataFrame({
        "symbol": ["600000.SH"] * len(days), "date": days,
        "open": [10.0] * len(days), "high": [10.5] * len(days),
        "low": [9.5] * len(days), "close": [10.0] * len(days),
        "volume": [200.0] * (len(days) - 1) + [100.0],
        "amount": [2000.0] * (len(days) - 1) + [1000.0],
        "quote_ts": [None] * (len(days) - 1) + [timestamp],
    })


@pytest.mark.parametrize("hour,minute,second", [
    (9, 25, 0), (9, 30, 0), (9, 30, 30), (10, 0, 0),
    (11, 30, 0), (12, 30, 0), (13, 0, 0), (14, 30, 0), (15, 0, 0), (16, 0, 0),
])
def test_rebuild_uses_snapshot_trading_time(hour, minute, second):
    dt = datetime(2026, 10, 22, hour, minute, second, tzinfo=CN_TZ)
    elapsed = trading_minutes_elapsed_from_dt(dt)
    factor = 240 / elapsed if 0 < elapsed < 240 else 1
    out = compute_indicators(_bars(_timestamp(hour, minute, second))).sort("date")
    assert out.tail(1)["vol_ratio_5d"][0] == pytest.approx(0.5 * factor)
    assert out.tail(2).head(1)["vol_ratio_5d"][0] == 1


@pytest.mark.parametrize("timestamp", [None, 0, -1, _timestamp(10, 0, day=date(2026, 10, 21))])
def test_formal_daily_or_invalid_snapshot_date_is_not_scaled(timestamp):
    assert compute_indicators(_bars(timestamp)).tail(1)["vol_ratio_5d"][0] == 0.5


def test_rebuild_is_independent_of_wall_clock_and_keeps_signal_consistent(monkeypatch):
    monkeypatch.setattr("app.market_time.cn_today", lambda: date(2030, 1, 1))
    monkeypatch.setattr("app.market_time.trading_minutes_elapsed", lambda: 240)
    out = compute_indicators(_bars(_timestamp(10, 0)))
    assert out.tail(1)["vol_ratio_5d"][0] == 4
    assert compute_indicators(_bars(None).drop("quote_ts")).tail(1)["vol_ratio_5d"][0] == 0.5


def test_incremental_and_rebuilt_ratios_match_per_quote_timestamp(tmp_path):
    early = _bars(_timestamp(10, 0))
    late = _bars(_timestamp(11, 30)).with_columns(pl.lit("000001.SZ").alias("symbol"))
    bars = pl.concat([early, late])
    history = compute_indicators(bars.filter(pl.col("date") < DAY))
    repo = KlineRepository(DataStore(tmp_path))
    try:
        repo._enriched_history_cache = history
        latest = history["date"].max()
        repo._build_live_agg(latest)
        incremental = compute_enriched_today(
            repo.get_live_agg(), history.filter(pl.col("date") == latest),
            bars.filter(pl.col("date") == DAY), elapsed_minutes=240,
        ).sort("symbol")
        full = compute_indicators(bars).filter(pl.col("date") == DAY).sort("symbol")
        assert incremental["vol_ratio_5d"].to_list() == pytest.approx(full["vol_ratio_5d"].to_list())
        assert full["vol_ratio_5d"].to_list() == [1.0, 4.0]
    finally:
        repo.store.db.close()


def test_explicit_live_fallback_only_applies_when_timestamp_is_missing():
    frame = pl.DataFrame({"date": [DAY] * 3, "quote_ts": [None, 0, _timestamp(11, 30)]})
    assert frame.select(volume_time_factor(frame, elapsed_minutes=30).alias("factor"))["factor"].to_list() == [8, 1, 2]


def test_needed_indicators_retain_same_ratio():
    bars = _bars(_timestamp(10, 0))
    limited = compute_indicators(bars, needed={"vol_ratio_5d"})
    assert limited["vol_ratio_5d"].to_list() == compute_indicators(bars)["vol_ratio_5d"].to_list()


def test_matrix_volume_filter_does_not_depend_on_score_fields():
    from app.backtest.matrix import build_market_data_matrix, matrix_feature

    panel = compute_indicators(_bars(_timestamp(10, 0)))
    for fields in ({"close", "volume"}, {"vol_ratio_5d"}):
        market = build_market_data_matrix(panel, field_columns=fields)
        assert matrix_feature(market, "vol_ratio_5d")[-1, 0] == 4
        # 时间折算不能改成交量本身或污染下一天的均量分母。
        assert market.volume[-1, 0] == 100
        # 10 日研究因子保留现有定义，本次只统一产品的标准 5 日量比。
        assert matrix_feature(market, "vol_ratio_10d")[-1, 0] == 0.5


@pytest.mark.parametrize("cached", [False, True])
def test_direct_matrix_volume_ratio_matches_snapshots_and_cache_reload(tmp_path, cached):
    from app.backtest.matrix import load_market_data_matrix_from_parquet, matrix_feature

    bars = _bars(_timestamp(10, 0))
    market_root = tmp_path / "kline_daily_enriched"
    for part in bars.partition_by("date"):
        directory = market_root / f"date={part['date'][0]}"
        directory.mkdir(parents=True)
        part.write_parquet(directory / "part.parquet")
    for _ in range(2):
        market = load_market_data_matrix_from_parquet(
            market_root, bars["date"].min(), DAY, field_columns=set(),
            cache_root=tmp_path / "matrix_cache" if cached else None,
        )
        assert matrix_feature(market, "vol_ratio_5d")[-1, 0] == 4
        assert market.volume[-1, 0] == 100


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("legacy_timestamp", ["missing", "null"])
def test_direct_matrix_preserves_quote_time_across_mixed_partitions(tmp_path, cached, legacy_timestamp):
    from app.backtest.matrix import load_market_data_matrix_from_parquet, matrix_feature

    bars = _bars(_timestamp(10, 0))
    expected = compute_indicators(bars)["vol_ratio_5d"][-1]
    market_root = tmp_path / "kline_daily_enriched"
    for part in bars.partition_by("date"):
        directory = market_root / f"date={part['date'][0]}"
        directory.mkdir(parents=True)
        if part["date"][0] < DAY:
            # 保留的旧分区可能完全没有时间戳，也可能只有 Arrow null 类型。
            if legacy_timestamp == "missing":
                part = part.drop("quote_ts")
            else:
                part = part.with_columns(pl.lit(None).alias("quote_ts"))
        part.write_parquet(directory / "part.parquet")

    # 最早的旧分区在请求范围外，仍不能影响范围内盘中分区的字段读取。
    for attempt in range(2):
        market = load_market_data_matrix_from_parquet(
            market_root, bars["date"][1], DAY, field_columns=set(),
            cache_root=tmp_path / "matrix_cache" if cached else None,
        )
        ratio = matrix_feature(market, "vol_ratio_5d")
        assert expected == 4
        assert ratio[-1, 0] == expected
        assert ratio[-2, 0] == 1  # 正式历史日线仍按全天成交量计算。
        assert market.volume[-1, 0] == 100
        if cached:
            assert market.cache_status == ("built" if attempt == 0 else "exact")


def test_realtime_matrix_can_add_timestamp_and_return_to_formal_daily():
    from app.backtest.matrix import RealtimeMarketDataMatrix, matrix_feature

    bars = _bars(_timestamp(10, 0))
    history = bars.filter(pl.col("date") < DAY).drop("quote_ts")
    live = RealtimeMarketDataMatrix(history, field_columns=set())
    today = bars.filter(pl.col("date") == DAY)
    live.update(today)
    assert matrix_feature(live.snapshot(), "vol_ratio_5d")[-1, 0] == 4
    live.update(today.drop("quote_ts"))
    assert matrix_feature(live.snapshot(), "vol_ratio_5d")[-1, 0] == 0.5
