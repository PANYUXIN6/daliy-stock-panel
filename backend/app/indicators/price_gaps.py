"""截至末根日线仍未完全回补的价格缺口区间。"""
from __future__ import annotations

import polars as pl

from app.indicators.pipeline import filter_halt_days


def compute_price_gaps(bars: pl.DataFrame) -> tuple[pl.DataFrame, list[dict]]:
    """前复权、单标的日线；部分回补收窄区间，触及远端边界即完全回补。

    后缀最低/最高价只用于截止日的剩余区域，不能作为逐日历史交易信号。
    """
    frame = filter_halt_days(bars).sort("date") if not bars.is_empty() else bars
    if frame.is_empty():
        return frame, []
    frame = frame.filter(
        pl.col("high").is_finite() & pl.col("low").is_finite()
        & (pl.col("low") > 0) & (pl.col("high") >= pl.col("low")))
    if frame.height < 2:
        return frame, []
    work = frame.with_columns(
        pl.col("high").shift(1).alias("_prev_high"),
        pl.col("low").shift(1).alias("_prev_low"),
        pl.col("low").reverse().cum_min().reverse().shift(-1).alias("_later_low"),
        pl.col("high").reverse().cum_max().reverse().shift(-1).alias("_later_high"),
    )
    up = work.filter(pl.col("low") > pl.col("_prev_high")).select(
        "date", pl.lit("up").alias("direction"),
        pl.col("_prev_high").alias("original_lower"), pl.col("low").alias("original_upper"),
        pl.col("_prev_high").alias("lower"),
        pl.min_horizontal("low", "_later_low").alias("upper"),
    )
    down = work.filter(pl.col("high") < pl.col("_prev_low")).select(
        "date", pl.lit("down").alias("direction"),
        pl.col("high").alias("original_lower"), pl.col("_prev_low").alias("original_upper"),
        pl.max_horizontal("high", "_later_high").alias("lower"),
        pl.col("_prev_low").alias("upper"),
    )
    active = pl.concat([up, down]).filter(pl.col("upper") > pl.col("lower")).sort("date")
    return frame, [{**row, "date": row["date"].isoformat()} for row in active.to_dicts()]
