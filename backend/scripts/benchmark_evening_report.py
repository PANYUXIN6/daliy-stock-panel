"""Synthetic evening-report scale check; never opens user data.

Run from backend: uv run --no-sync python scripts/benchmark_evening_report.py
Use --history-days 8 to measure consecutive independent reports at full market scale.
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
from app.services.evening_report_store import EveningReportStore

calendar = []
d = date(2025, 1, 1)
while len(calendar) < 250:
    if d.weekday() < 5:
        calendar.append(d.isoformat())
    d += timedelta(days=1)
metadata = pl.DataFrame({"n": range(5000)}).with_columns(
    pl.concat_str(pl.lit("sample"), pl.col("n").cast(pl.String).str.pad_start(4, "0")).alias(
        "symbol"
    )
)
prices = metadata.join(pl.DataFrame({"date": calendar, "i": range(250)}), how="cross").with_columns(
    (20 * (1 + 0.0001 * (pl.col("n") % 100 + 1)).pow(pl.col("i"))).alias("close")
)
prices = prices.with_columns(
    (pl.col("close") * 1.01).alias("high"),
    (pl.col("close") * .98).alias("low"),
    (pl.col("close") * .99).alias("open"),
    pl.lit(100_000.).alias("volume"),
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
    pl.when(pl.col("n") < 4000).then(pl.lit("main")).otherwise(pl.lit("chinext")).alias("board"),
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
    market={"state": "strong", "phase": "rally", "promo_rate": 0.3, "seal_rate": 0.8},
)
parser = argparse.ArgumentParser()
parser.add_argument("--history-days", type=int, choices=range(1, 31), default=1)
parser.add_argument("--storage", action="store_true", help="Measure real tiered writes in a temporary directory")
args = parser.parse_args()
if args.storage:
    from tempfile import TemporaryDirectory

    temporary = TemporaryDirectory(prefix="evening-storage-benchmark-")
    store = EveningReportStore(Path(temporary.name))
previous = None
elapsed = []
for index in range(len(calendar) - args.history_days, len(calendar)):
    current = replace(inputs, trade_date=calendar[index], calendar=calendar[: index + 1],
                      cutoff=calendar[index] + "T16:00:00+08:00")
    started = time.perf_counter()
    result = build_report(current, previous)
    elapsed.append(time.perf_counter() - started)
    if args.storage:
        store.save(current.trade_date, result, {"stock_generation": "benchmark"})
    previous = {"trade_date": result["trade_date"], "role_state": result["role_state"]}
storage_result = None
if args.storage:
    from collections import defaultdict

    sizes = defaultdict(int)
    for path in store.root.rglob('*'):
        if path.is_file():
            group = path.relative_to(store.root).parts[0]
            if group == 'archive':
                group = ('audit' if '.audit.' in path.name
                         else 'candidate_boundary' if '.candidates.' in path.name else 'permanent')
            sizes[group] += path.stat().st_size
    storage_result = {'bytes_by_tier': dict(sizes), 'total_bytes': sum(sizes.values()),
                      'retained_candidate_count': len(store.get(current.trade_date)['records'])}
    temporary.cleanup()
print(
    json.dumps(
        {
            "stocks": 5000,
            "price_rows": prices.height,
            "sectors": len(result["sectors"]),
            "membership_rows": members.height,
            "history_days": args.history_days,
            "storage": storage_result,
            "seconds": [round(value, 3) for value in elapsed],
            "report_objects": len(result["records"]),
            "eligible_stocks": result["statistics"]["eligible_count"],
            "json_mb": round(len(json.dumps(result, ensure_ascii=False).encode()) / 1024**2, 2),
            "retained_state_mb": round(
                len(json.dumps(previous, ensure_ascii=False).encode()) / 1024**2, 2
            ),
            "peak_rss_mb": round(
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                / (1024**2 if platform.system() == "Darwin" else 1024),
                1,
            ),
        }
    )
)
