"""正式财报断层的价格形态及通达信 9、42 回踩条件。"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.indicators.pipeline import compute_indicators, filter_halt_days


def upward_gap_expr() -> pl.Expr:
    """完整向上跳空：最低价严格高于前一根有效交易K线最高价。"""
    return (pl.col("low") > pl.col("high").shift(1).over("symbol")).fill_null(False)


def _pullback_prices(bars: pl.DataFrame) -> pl.DataFrame:
    return compute_indicators(bars.sort("date"), needed={"bull_bear_line", "ma20"}).with_columns(
        (pl.col("close") > pl.col("close").shift(1)).fill_null(False)
        .cast(pl.Float64).rolling_sum(12, min_samples=1).truediv(12).mul(100).alias("_eg_psy"),
    )


def _pullback_common() -> pl.Expr:
    line, ma20 = pl.col("bull_bear_line"), pl.col("ma20")
    return ((pl.col("_eg_psy") <= 50) & (pl.col("close") > line)
            & ((line > line.shift(1)) | (ma20 > ma20.shift(1)))
            & (pl.col("close") > pl.col("open")))


def pullback_price_candidates(bars: pl.DataFrame, targets: pl.DataFrame) -> pl.DataFrame:
    """仅依据回踩共同价格条件筛候选；不需要财报，不修改最终信号口径。"""
    frames = []
    for group in filter_halt_days(bars).partition_by("symbol", maintain_order=True):
        eligible = _pullback_prices(group).filter(_pullback_common()).select("symbol", "date")
        frames.append(eligible.join(targets.select("symbol", "date").unique(), on=["symbol", "date"], how="semi"))
    return pl.concat(frames) if frames else targets.select("symbol", "date").head(0)


def compute_earnings_breakout(bars: pl.DataFrame, event_dates: set[date]) -> pl.DataFrame:
    """单股完整前序日线；事件已按当时财务版本确定，不重新读取最新财报。"""
    bars = filter_halt_days(bars)
    if bars.is_empty():
        return bars
    df = _pullback_prices(bars)
    df = df.with_row_index("_eg_i").with_columns(
        (pl.col("date").is_in(sorted(event_dates)) & upward_gap_expr()).alias("earnings_gap"),
    )
    h, low, event = pl.col("high"), pl.col("low"), pl.col("earnings_gap")
    df = df.with_columns(
        (event.shift(2) & (low.shift(1) > h.shift(3)) & (low > h.shift(3)))
        .fill_null(False).alias("_eg_t1"),
        (event.shift(2) & (low.shift(1) > h.shift(2)) & (low > h.shift(2)))
        .fill_null(False).alias("_eg_t3"),
        (event.shift(4) & (low.rolling_min(4) > ((h + low) / 2).shift(4)))
        .fill_null(False).alias("_eg_t7"),
    )
    for anchor, condition in (("a1", "t1"), ("a2", "t3"), ("a3", "t7")):
        df = df.with_columns(
            pl.when(pl.col(f"_eg_{condition}")).then(pl.col("_eg_i"))
            .otherwise(None).forward_fill().alias(f"_eg_{anchor}"),
        )
    i, a1, a2, a3 = (pl.col(f"_eg_{c}").cast(pl.Int64) for c in ("i", "a1", "a2", "a3"))
    df = df.with_columns(
        (low <= h.gather(a1 - 3)).fill_null(False).alias("_eg_b1"),
        (low <= h.gather(a2 - 2)).fill_null(False).alias("_eg_b2"),
        (low <= h.gather(a2 - 3)).fill_null(False).alias("_eg_b5"),
        (low < ((h + low) / 2).gather(a3 - 4)).fill_null(False).alias("_eg_b3"),
    )
    # COUNT(X,0) 是全历史计数；N>0 的窗口从最近确认点的下一根到当前根。
    # 确认点自身由 T1/T3/T7 保证未破位，故分组累积与原式窗口一致。
    for suffix, anchor in (("1", "a1"), ("2", "a2"), ("5", "a2"), ("3", "a3")):
        breach = pl.col(f"_eg_b{suffix}").cast(pl.Int64)
        df = df.with_columns(
            pl.when(i == pl.col(f"_eg_{anchor}"))
            .then(breach.cum_sum()).otherwise(breach.cum_sum().over(f"_eg_{anchor}"))
            .alias(f"_eg_n{suffix}"),
        )
    n1, n2, n5, n3 = (pl.col(f"_eg_n{n}") for n in ("1", "2", "5", "3"))
    t2 = a1.is_not_null() & (n1 == 0) & (low < low.gather(a1 - 2) * 1.05)
    t4 = a2.is_not_null() & (n2 == 0) & (low < low.gather(a2 - 1) * 1.03)
    t5 = a2.is_not_null() & (n5 == 0) & (low < low.gather(a2 - 2) * 1.05)
    t6 = (t2 & ((i - a1) < 125)) | ((t4 | t5) & ((i - a2) < 125))
    t8 = (a3.is_not_null() & (n3 == 0) & (low < h.gather(a3 - 4))
          & ((i - a3) > 0) & ((i - a3) < 10))
    common = _pullback_common()
    df = df.with_columns(
        (common & t6).fill_null(False).alias("signal_earnings_9"),
        (common & t8).fill_null(False).alias("signal_earnings_42"),
        pl.when(t2 & ((i - a1) < 125)).then(pl.col("date").gather(a1 - 2))
        .otherwise(pl.col("date").gather(a2 - 2)).alias("earnings_9_event_date"),
        pl.col("date").gather(a3 - 4).alias("earnings_42_event_date"),
    )
    return df.drop([c for c in df.columns if c.startswith("_eg_")])
