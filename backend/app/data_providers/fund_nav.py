"""基金单位净值数据集：symbol/date/unit_nav，单位元/份，不使用累计或复权净值。"""
from __future__ import annotations

from datetime import date

import polars as pl

NAV_SCHEMA = {"symbol": pl.String, "date": pl.Date, "unit_nav": pl.Float64}


def normalize_fund_nav(rows: list[dict], start: date, end: date) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=NAV_SCHEMA)
    frame = pl.DataFrame(rows).select(
        pl.col("symbol").cast(pl.String),
        pl.col("date").cast(pl.String).str.to_date(strict=True),
        pl.col("unit_nav").cast(pl.Float64, strict=True),
    ).filter(pl.col("date").is_between(start, end))
    if frame.filter(
        pl.col("symbol").is_null() | pl.col("date").is_null()
        | pl.col("unit_nav").is_null() | ~pl.col("unit_nav").is_finite()
        | (pl.col("unit_nav") <= 0)
    ).height or frame.select("symbol", "date").is_duplicated().any():
        raise ValueError("基金单位净值存在缺失、无效数值或重复日期")
    return frame.sort("symbol", "date")
