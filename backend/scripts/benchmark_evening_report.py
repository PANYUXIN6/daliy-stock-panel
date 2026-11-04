"""Synthetic evening-report scale check; never opens user data.

Run from backend: uv run --no-sync python scripts/benchmark_evening_report.py
Pass --history-days 8 to exercise bounded forward-state replay at full market scale.
"""

from __future__ import annotations

import argparse
import json
import platform
import resource
import sys
import time
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.evening_report_engine import EveningInputs, build_report

calendar = []
d = date(2025, 1, 1)
while len(calendar) < 120:
    if d.weekday() < 5:
        calendar.append(d.isoformat())
    d += timedelta(days=1)
metadata = pl.DataFrame({"n": range(5000)}).with_columns(
    pl.concat_str(pl.lit("sample"), pl.col("n").cast(pl.String).str.pad_start(4, "0")).alias(
        "symbol"
    )
)
prices = metadata.join(pl.DataFrame({"date": calendar, "i": range(120)}), how="cross").with_columns(
    (20 * (1 + 0.0001 * (pl.col("n") % 100 + 1)).pow(pl.col("i"))).alias("close")
)
prices = prices.with_columns(
    (pl.col("close") * 1.01).alias("high"),
    (pl.col("close") * 0.99).alias("ma5"),
    (pl.col("close") * 0.98).alias("ma10"),
    (pl.col("close") * 0.97).alias("ma20"),
    (pl.col("close") * 0.03).alias("atr_14"),
    pl.col("close").alias("high_60d"),
    (pl.col("close") * 0.99).alias("raw_open"),
    (pl.col("close") * 1.01).alias("raw_high"),
    (pl.col("close") * 0.98).alias("raw_low"),
    pl.col("close").alias("raw_close"),
    pl.lit(200_000_000.0).alias("amount"),
    pl.lit(True).alias("formal"),
    (pl.col("n") % 20 == 0).alias("signal_limit_up"),
    pl.lit(False).alias("signal_limit_down"),
    pl.when(pl.col("n") % 20 == 0).then(3).otherwise(0).alias("consecutive_limit_ups"),
).drop("n", "i")
members = []
for offset in range(6):
    members.append(
        metadata.select(
            "symbol",
            pl.lit("concept").alias("source"),
            pl.concat_str(pl.lit("C"), ((pl.col("n") + offset * 17) % 300).cast(pl.String)).alias(
                "sector_id"
            ),
        )
    )
members.append(
    metadata.select(
        "symbol",
        pl.lit("industry").alias("source"),
        pl.concat_str(pl.lit("I"), (pl.col("n") % 80).cast(pl.String)).alias("sector_id"),
    )
)
members = pl.concat(members).with_columns(pl.col("sector_id").alias("sector_name"))
metadata = metadata.with_columns(
    pl.col("symbol").alias("name"),
    pl.lit(True).alias("in_scope"),
    pl.lit(True).alias("trading_normal"),
    pl.lit(False).alias("suspension_window"),
    pl.lit(False).alias("qualification_pause"),
    pl.when(pl.col("n") < 4000).then(pl.lit("10%")).otherwise(pl.lit("20%")).alias("regime"),
).drop("n")
inputs = EveningInputs(
    trade_date=calendar[-1],
    calendar=calendar,
    prices=prices,
    metadata=metadata,
    memberships=members,
    benchmark=pl.DataFrame({"date": calendar, "close": [100.0] * 120, "formal": [True] * 120}),
    market={"state": "strong", "phase": "rally", "promo_rate": 0.3, "seal_rate": 0.8},
)
parser = argparse.ArgumentParser()
parser.add_argument("--history-days", type=int, choices=range(1, 31), default=1)
args = parser.parse_args()
history = []
elapsed = []
for index in range(120 - args.history_days, 120):
    current = replace(inputs, trade_date=calendar[index], calendar=calendar[: index + 1])
    started = time.perf_counter()
    result = build_report(current, history[-7:])
    elapsed.append(time.perf_counter() - started)
    history.append(
        {
            "trade_date": result["trade_date"],
            "rule_version": result["rule_version"],
            "state": result["state"],
            "statistics": {"publication_frequency": result["statistics"]["publication_frequency"]},
            "records": [
                {"symbol": record["symbol"], "purpose": record["purpose"]}
                for record in result["records"]
            ],
        }
    )
    history = history[-7:]
print(
    json.dumps(
        {
            "stocks": 5000,
            "price_rows": prices.height,
            "sectors": len(result["sectors"]),
            "membership_rows": members.height,
            "history_days": args.history_days,
            "seconds": [round(value, 3) for value in elapsed],
            "report_objects": len(result["records"]),
            "scored_stocks": result["statistics"]["scored_count"],
            "json_mb": round(len(json.dumps(result, ensure_ascii=False).encode()) / 1024**2, 2),
            "retained_state_mb": round(
                len(json.dumps(history, ensure_ascii=False).encode()) / 1024**2, 2
            ),
            "peak_rss_mb": round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                / (1024**2 if platform.system() == "Darwin" else 1024),
                1,
            ),
        }
    )
)
