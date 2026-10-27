"""Versioned daily observations. Pure functions; all ratios use decimal units."""

from __future__ import annotations

import math
from datetime import date
from statistics import mean

import polars as pl

RULES_VERSION = "sector-review-v2"
MIN_MEMBERS = 5
WINDOWS = (5, 10, 20, 60)


def tri_and(*values: bool | None) -> bool | None:
    return False if False in values else None if None in values else True


def tri_or(*values: bool | None) -> bool | None:
    return True if True in values else None if None in values else False


def above(value: float | None, threshold: float = 0) -> bool | None:
    return None if value is None else value > threshold


def relative(sector: float | None, benchmark: float | None) -> float | None:
    return (
        (1 + sector) / (1 + benchmark) - 1
        if sector is not None and benchmark is not None and benchmark > -1
        else None
    )


def compound(values: list[float | None]) -> float | None:
    return (
        math.prod(1 + v for v in values) - 1
        if values and all(v is not None for v in values)
        else None
    )


def classify(sector: float | None, benchmark: float | None) -> str:
    if sector is None or benchmark is None:
        return "pending"
    s = 0.0 if abs(sector) < 1e-12 else sector
    b = 0.0 if abs(benchmark) < 1e-12 else benchmark
    if b < 0:
        if s > 0:
            return "countertrend_up"
        if s == 0:
            return "flat_in_decline"
        return (
            "resilient"
            if s > b and not math.isclose(s, b, abs_tol=1e-12, rel_tol=0)
            else "lagging_in_decline"
        )
    if b > 0:
        return (
            "relative_leader"
            if s > b and not math.isclose(s, b, abs_tol=1e-12, rel_tol=0)
            else "lagging_in_rise"
        )
    return "benchmark_flat"


def down_statistics(sector: list[float | None], benchmark: list[float | None]) -> dict:
    pairs = [(s, b) for s, b in zip(sector, benchmark, strict=True) if b is not None and b < 0]
    valid = [(s, b) for s, b in pairs if s is not None]
    n = len(valid)
    wins = sum(s > b for s, b in valid)
    total = len(pairs) if all(b is not None for b in benchmark) else None
    return {
        "wins": wins,
        "comparable_days": n,
        "benchmark_down_days": total,
        "known_down_days": len(pairs),
        "ratio": wins / n if n else None,
        "mean_excess": mean(s - b for s, b in valid) if n else None,
        "complete": total is not None and n == total and total > 0,
        "small_sample": n < 5,
    }


def response_path(observations: list[dict]) -> dict:
    flags = [d["strength"] for d in observations]
    first = next((i for i, flag in enumerate(flags) if flag is True), None)
    confirmed = (
        first if first is not None and all(flag is False for flag in flags[:first]) else None
    )
    streak = 0
    complete = True
    if not flags or flags[-1] is None:
        streak, complete = None, False
    else:
        for flag in reversed(flags):
            if flag is False:
                break
            if flag is None:
                complete = False
                break
            streak += 1
    return {
        "earliest_observed_strength_date": observations[first]["date"]
        if first is not None
        else None,
        "confirmed_first_strength_date": observations[confirmed]["date"]
        if confirmed is not None
        else None,
        "first_strength_offset": confirmed,
        "strength_streak": streak,
        "streak_complete": complete,
        "candidate": tri_or(observations[0].get("daily_resonance_candidate"), *flags)
        if observations
        else None,
    }


def prepare_members(
    prices: pl.DataFrame,
    instruments: pl.DataFrame,
    days: list[date],
    formation: date,
    event_date: date | None,
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Use the calendar grid, never last-N-valid rows or subsequent completeness."""
    grid = (
        instruments.select("symbol", "name", "listing_date")
        .join(pl.DataFrame({"date": days}), how="cross")
        .join(prices, on=["symbol", "date"], how="left")
        .sort("symbol", "date")
    )
    grid = grid.with_columns(
        pl.when(pl.col("price").is_null() & pl.col("reason").is_null())
        .then(pl.lit("missing_quote"))
        .otherwise(pl.col("reason"))
        .alias("reason"),
        (pl.col("price") / pl.col("price").shift(1).over("symbol") - 1).alias("day_return"),
        pl.col("amount")
        .shift(1)
        .rolling_mean(20, min_samples=20)
        .over("symbol")
        .alias("rolling_amount_base"),
    )
    fidx = days.index(formation)
    pre21 = days[max(0, fidx - 20) : fidx + 1]
    pre20 = days[max(0, fidx - 19) : fidx + 1]
    eligibility = (
        grid.filter(pl.col("date").is_in(pre21))
        .group_by("symbol")
        .agg(
            pl.col("price").count().alias("price_days"),
            pl.col("reason").drop_nulls().first().alias("first_reason"),
            pl.col("listing_date").first(),
        )
        .join(
            grid.filter(pl.col("date").is_in(pre20))
            .group_by("symbol")
            .agg(
                pl.col("amount").count().alias("amount_days"),
                pl.col("amount").mean().alias("formation_amount_base"),
            ),
            on="symbol",
        )
    )
    eligibility = eligibility.with_columns(
        (
            (pl.col("price_days") == 21)
            & (pl.col("amount_days") == 20)
            & (pl.col("formation_amount_base") > 0)
        ).alias("formation_eligible")
    ).with_columns(
        pl.when(pl.col("formation_eligible"))
        .then(None)
        .when(pl.col("listing_date").str.to_date(strict=False) > formation)
        .then(pl.lit("not_listed"))
        .when(pl.col("first_reason").is_not_null())
        .then(pl.col("first_reason"))
        .otherwise(pl.lit("insufficient_formation_history"))
        .alias("exclusion_reason")
    )
    grid = grid.join(
        eligibility.select("symbol", "formation_eligible", "formation_amount_base"), on="symbol"
    )
    base = (
        pl.col("formation_amount_base") if event_date is not None else pl.col("rolling_amount_base")
    )
    grid = grid.with_columns(
        pl.when(base > 0).then(pl.col("amount") / base).otherwise(None).alias("amount_ratio"),
    )
    return grid, eligibility


def aggregate(
    grid: pl.DataFrame,
    mapping: pl.DataFrame,
    event_date: date | None,
    formation: date,
    days: list[date],
) -> pl.DataFrame:
    """Only complete fixed-member inputs enter formal aggregates; no null-as-zero."""
    selected = grid.filter(pl.col("formation_eligible")).select(
        "symbol", "date", "day_return", "amount", "amount_ratio"
    )
    if selected.is_empty() or mapping.is_empty():
        return pl.DataFrame()
    aggregations = [
        pl.len().alias("members"),
        pl.col("day_return").count().alias("return_count"),
        pl.col("amount").count().alias("amount_count"),
        pl.col("day_return").mean().alias("day_return"),
        pl.col("day_return").median().alias("median_return"),
        (pl.col("day_return") > 0).mean().alias("breadth_up"),
        pl.col("amount").sum().alias("amount"),
        pl.col("amount_ratio").count().alias("ratio_count"),
        ((pl.col("day_return") > 0) & (pl.col("amount_ratio") > 1))
        .mean()
        .alias("breadth_expanding"),
    ]
    membership = mapping.select("sector_id", "symbol").unique()
    # Overlapping concepts amplify rows. Bound the temporary join by ten dates,
    # while loading stock history only once for the whole request.
    batches = []
    for offset in range(0, len(days), 10):
        batch = selected.filter(pl.col("date").is_in(days[offset : offset + 10]))
        batches.append(
            batch.lazy()
            .join(membership.lazy(), on="symbol")
            .group_by("sector_id", "date")
            .agg(aggregations)
            .collect(engine="streaming")
        )
    frame = pl.concat(batches).sort("sector_id", "date")
    frame = frame.with_columns(
        *[
            pl.when(pl.col("return_count") == pl.col("members"))
            .then(pl.col(c))
            .otherwise(None)
            .alias(c)
            for c in ("day_return", "median_return", "breadth_up")
        ],
        pl.when(pl.col("amount_count") == pl.col("members"))
        .then(pl.col("amount"))
        .otherwise(None)
        .alias("amount"),
        pl.when(
            (pl.col("return_count") == pl.col("members"))
            & (pl.col("ratio_count") == pl.col("members"))
        )
        .then(pl.col("breadth_expanding"))
        .otherwise(None)
        .alias("breadth_expanding"),
    )
    market = frame.filter(pl.col("sector_id") == "_market").select(
        "date",
        pl.col("amount").alias("market_amount"),
        pl.col("breadth_up").alias("market_breadth"),
    )
    frame = (
        frame.join(market, on="date", how="left")
        .with_columns(
            pl.when(pl.col("market_amount") > 0)
            .then(pl.col("amount") / pl.col("market_amount"))
            .otherwise(None)
            .alias("amount_share"),
            (pl.col("breadth_up") - pl.col("market_breadth")).alias("breadth_diff"),
        )
        .sort("sector_id", "date")
    )
    if event_date is not None:
        idx = days.index(formation)
        baseline = (
            frame.filter(pl.col("date").is_in(days[max(0, idx - 19) : idx + 1]))
            .group_by("sector_id")
            .agg(
                pl.when(pl.col("amount").count() == 20)
                .then(pl.col("amount").mean())
                .otherwise(None)
                .alias("amount_base"),
                pl.when(pl.col("amount_share").count() == 20)
                .then(pl.col("amount_share").mean())
                .otherwise(None)
                .alias("share_base"),
            )
        )
        frame = frame.join(baseline, on="sector_id", how="left")
    else:
        frame = frame.with_columns(
            pl.col("amount")
            .shift(1)
            .rolling_mean(20, min_samples=20)
            .over("sector_id")
            .alias("amount_base"),
            pl.col("amount_share")
            .shift(1)
            .rolling_mean(20, min_samples=20)
            .over("sector_id")
            .alias("share_base"),
        )
    return frame.with_columns(
        pl.when(pl.col("amount_base") > 0)
        .then(pl.col("amount") / pl.col("amount_base"))
        .otherwise(None)
        .alias("amount_ratio"),
        (pl.col("amount_share") - pl.col("share_base")).alias("share_delta"),
    )


def period_summary(daily: dict[date, dict], benchmark: dict[date, dict], days: list[date]) -> dict:
    sector_returns = [daily.get(d, {}).get("day_return") for d in days]
    benchmark_returns = [benchmark.get(d, {}).get("day_return") for d in days]
    s, b = compound(sector_returns), compound(benchmark_returns)
    return {
        "sector_return": s,
        "benchmark_return": b,
        "relative_return": relative(s, b),
        "classification": classify(s, b),
        "down_days": down_statistics(sector_returns, benchmark_returns),
        "return_days": sum(v is not None for v in sector_returns),
        "required_days": len(days),
    }


def observation(day: date, sector: dict, benchmark: dict) -> dict:
    s, b = sector.get("day_return"), benchmark.get("day_return")
    lead = None if s is None or b is None else s > b
    same_day_up = tri_and(above(s), above(b))
    expansion = tri_and(
        above(benchmark.get("amount_ratio"), 1), above(sector.get("amount_ratio"), 1)
    )
    majority = above(sector.get("breadth_up"), 0.5)
    complete = (
        all(
            sector.get(c) is not None
            for c in ("day_return", "amount_ratio", "breadth_up", "breadth_diff", "share_delta")
        )
        and b is not None
        and all(benchmark.get(c) is not None for c in ("amount", "amount_ratio"))
    )
    return {
        **sector,
        "date": day.isoformat(),
        "benchmark_return": b,
        "relative_return": relative(s, b),
        "daily_classification": classify(s, b),
        "strength": tri_and(above(s), lead),
        "same_day_up": same_day_up,
        "amount_expansion": expansion,
        "majority_up": majority,
        "daily_resonance_candidate": tri_and(same_day_up, expansion, majority),
        "relative_lead": lead,
        "status": "ready" if complete else "partial" if s is not None else "data_missing",
    }
