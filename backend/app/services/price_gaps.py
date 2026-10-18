"""本地完整日线及实时尾部 -> 截止日未回补缺口，不采集、不写盘。"""
from __future__ import annotations

from datetime import date

import polars as pl

from app.indicators.price_gaps import compute_price_gaps


def get_price_gaps(repo, symbol: str, end: date, live_row=None) -> dict:
    asset = repo.resolve_asset_type(symbol)
    columns = ["symbol", "date", "open", "high", "low", "close", "volume", "amount"]
    bars = repo.get_daily_asset(asset, symbol, date(1990, 1, 1), end, columns=columns)
    if live_row is not None:
        live = pl.DataFrame([live_row]).with_columns(pl.col("date").str.to_date())
        bars = pl.concat([bars.filter(pl.col("date") != live["date"][0]), live],
                         how="diagonal_relaxed") if not bars.is_empty() else live
    if not bars.is_empty():
        bars = bars.filter(pl.col("date") <= end)
    prices, gaps = compute_price_gaps(bars)
    return {"symbol": symbol, "status": "no_prices" if prices.is_empty() else "ready",
            "history_start": None if prices.is_empty() else prices["date"][0].isoformat(),
            "as_of": None if prices.is_empty() else prices["date"][-1].isoformat(), "gaps": gaps}
