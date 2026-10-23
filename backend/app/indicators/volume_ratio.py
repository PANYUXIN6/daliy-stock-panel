"""量比的快照时间折算；历史重算不依赖执行时钟。"""
from __future__ import annotations

import math

import polars as pl


def volume_time_factor(
    frame: pl.DataFrame, *, elapsed_minutes: float | None = None,
) -> pl.Expr:
    """按行使用同日北京时间 quote_ts；缺时间时仅用调用方显式提供的分钟数。

    正式日线无 quote_ts，默认按全天量计算。保存的盘中快照即使次日重算，
    仍以原快照时刻折算，不把服务端当前时间应用到历史或完整日线。
    """
    fallback = 1.0
    if elapsed_minutes is not None and math.isfinite(elapsed_minutes) and 0 < elapsed_minutes < 240:
        fallback = 240.0 / elapsed_minutes
    if "quote_ts" not in frame.columns or "date" not in frame.columns:
        return pl.lit(fallback)

    timestamp = pl.col("quote_ts").cast(pl.Int64, strict=False)
    clock = pl.from_epoch(timestamp, time_unit="ms").dt.replace_time_zone("UTC").dt.convert_time_zone("Asia/Shanghai")
    minutes = clock.dt.hour().cast(pl.Float64) * 60 + clock.dt.minute() + clock.dt.second() / 60.0
    elapsed = (minutes - 570).clip(0, 120) + (minutes - 780).clip(0, 120)
    valid = (
        (timestamp > 0)
        & (clock.dt.date() == pl.col("date").cast(pl.Date, strict=False))
        & (clock.dt.weekday() <= 5)
        & (elapsed > 0)
        & (elapsed < 240)
    )
    return (
        pl.when(pl.col("quote_ts").is_null()).then(fallback)
        .when(valid).then(240.0 / elapsed)
        .otherwise(1.0)
    )
