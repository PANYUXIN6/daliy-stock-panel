from datetime import date, timedelta

import polars as pl
import pytest

from app.indicators.pipeline import compute_enriched_today, compute_indicators
from app.tickflow.repository import DataStore, KlineRepository


def _bars(n=4):
    rows = []
    for symbol, offset in (("600001.SH", 0), ("000002.SZ", 20)):
        for i in range(n):
            close = [9, 12, 6, 15][i % 4] + offset
            rows.append({
                "symbol": symbol, "date": date(2026, 1, 1) + timedelta(days=i),
                "open": close, "high": close + 5, "low": close - 2,
                "close": close, "volume": 1000.0,
            })
    return pl.DataFrame(rows)


def test_bull_bear_line_uses_typical_price_recursive_ema_and_separate_symbols():
    bars = _bars().reverse()
    subset = compute_indicators(bars, needed={"bull_bear_line"})
    full = compute_indicators(bars)
    # JJ = [10, 13, 7, 16], EMA alpha = 2/11, first value = first JJ.
    expected = [10, 116 / 11, 1198 / 121, 14654 / 1331]
    for symbol, offset in (("600001.SH", 0), ("000002.SZ", 20)):
        values = subset.filter(pl.col("symbol") == symbol)["bull_bear_line"].to_list()
        assert values == pytest.approx([value + offset for value in expected])
    assert subset["bull_bear_line"].equals(full["bull_bear_line"])
    assert set(subset.columns) == set(bars.columns) | {"bull_bear_line"}


def test_bull_bear_line_empty_single_bar_and_missing_price():
    bars = _bars(1)
    assert compute_indicators(bars.head(0), needed={"bull_bear_line"}).is_empty()
    assert compute_indicators(bars, needed={"bull_bear_line"})["bull_bear_line"].to_list() == [30, 10]
    missing = bars.with_columns(pl.lit(None, dtype=pl.Float64).alias("high"))
    assert compute_indicators(missing, needed={"bull_bear_line"})["bull_bear_line"].null_count() == 2


@pytest.mark.parametrize("cached", [True, False])
@pytest.mark.parametrize("adjustment", [1.0, 0.5])
def test_bull_bear_line_intraday_matches_full_and_seeds_new_listing(tmp_path, cached, adjustment):
    bars = _bars(100)
    today = bars["date"].max()
    history = bars.filter(pl.col("date") < today)
    history_indicators = compute_indicators(history)
    if not cached:
        target = tmp_path / "kline_daily_enriched" / "all"
        target.mkdir(parents=True)
        history.write_parquet(target / "part.parquet")
    repo = KlineRepository(DataStore(tmp_path))
    if cached:
        repo._enriched_history_cache = history_indicators
    repo._build_live_agg(history["date"].max())
    live = bars.filter(pl.col("date") == today)
    new_listing = live.head(1).with_columns(pl.lit("001003.SZ").alias("symbol"))
    live = pl.concat([live, new_listing])
    state = repo.get_live_agg().with_columns(pl.lit(adjustment).alias("_adj_factor"))
    raw_live = live.with_columns([
        pl.when(pl.col("symbol") == "001003.SZ").then(pl.col(column))
        .otherwise(pl.col(column) / adjustment).alias(column)
        for column in ("open", "high", "low", "close")
    ])
    incremental = compute_enriched_today(
        state, history_indicators.filter(pl.col("date") == history["date"].max()), raw_live,
    ).sort("symbol")
    full = compute_indicators(pl.concat([history, live])).filter(pl.col("date") == today).sort("symbol")
    assert incremental["bull_bear_line"].to_list() == pytest.approx(full["bull_bear_line"].to_list(), abs=1e-7)
