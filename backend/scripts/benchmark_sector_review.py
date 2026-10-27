"""Isolated reproducible read benchmark; never opens the user's data directory.

Run from backend: uv run python scripts/benchmark_sector_review.py
"""

from __future__ import annotations

import json
import platform
import resource
import sys
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from types import SimpleNamespace

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import sector_review
from app.tickflow.sector_review_reader import SectorReviewReader


def write_inputs(root: Path):
    days = [
        date(2026, 1, 5) + timedelta(days=i)
        for i in range(145)
        if (date(2026, 1, 5) + timedelta(days=i)).weekday() < 5
    ][:100]
    symbols = [f"{600000 + i:06d}.SH" for i in range(6000)]
    (root / "calendar").mkdir()
    (root / "calendar/trading_days.json").write_text(
        json.dumps({"start": str(days[0]), "end": str(days[-1]), "days": [str(d) for d in days]})
    )
    (root / "instruments").mkdir()
    pl.DataFrame(
        {
            "symbol": symbols,
            "name": symbols,
            "type": ["stock"] * 6000,
            "exchange": ["SH"] * 6000,
            "listing_date": ["2020-01-01"] * 6000,
        }
    ).write_parquet(root / "instruments/part.parquet")
    base = root / "ext_data/benchmark_concepts"
    base.mkdir(parents=True)
    (base / "config.json").write_text(
        json.dumps(
            {
                "id": "benchmark_concepts",
                "label": "合成概念",
                "mode": "snapshot",
                "fields": [{"name": "concept"}],
            }
        )
    )
    # Twelve overlapping concepts per stock, 180 members per concept on average.
    pl.DataFrame(
        {
            "symbol": symbols,
            "concept": [
                ";".join(f"概念{(i + j * 31) % 400}" for j in range(12)) for i in range(6000)
            ],
        }
    ).write_parquet(base / "part.parquet")
    for index, day in enumerate(days):
        closes = [10 * (1 + (i % 9 - 3) / 1000) ** index for i in range(6000)]
        frame = pl.DataFrame(
            {
                "symbol": symbols,
                "date": [day] * 6000,
                "close": closes,
                "volume": [1000.0] * 6000,
                "amount": [1000000.0 * (2 if index >= 96 else 1)] * 6000,
            }
        ).with_columns(
            pl.col("close").alias("open"),
            (pl.col("close") * 1.01).alias("high"),
            (pl.col("close") * 0.99).alias("low"),
        )
        for table in ("kline_daily", "kline_daily_enriched"):
            path = root / table / f"date={day}/part.parquet"
            path.parent.mkdir(parents=True)
            (
                frame
                if table == "kline_daily"
                else frame.with_columns(pl.col("close").alias("raw_close"))
            ).write_parquet(path)
        path = root / "kline_index_daily" / f"date={day}/part.parquet"
        path.parent.mkdir(parents=True)
        pl.DataFrame(
            {
                "symbol": list(sector_review.CORE_INDEX_NAMES),
                "date": [day] * 4,
                "open": [100.0] * 4,
                "high": [101.0] * 4,
                "low": [99.0] * 4,
                "close": [100.0] * 4,
                "amount": [1000000.0] * 4,
                "volume": [1000.0] * 4,
            }
        ).write_parquet(path)
    return days


def main():
    with TemporaryDirectory(prefix="sector-review-benchmark-") as directory:
        root = Path(directory)
        days = write_inputs(root)
        reader = SectorReviewReader(root)
        counts = {"prices": 0, "raw": 0}
        original_prices, original_raw = reader.prices, reader.raw

        def prices(*args, **kwargs):
            counts["prices"] += 1
            return original_prices(*args, **kwargs)

        def raw(*args, **kwargs):
            counts["raw"] += 1
            return original_raw(*args, **kwargs)

        reader.prices, reader.raw = prices, raw
        repo = SimpleNamespace(sector_review_reader=lambda: reader)
        results = []
        for mode in ("event", "countertrend"):
            params = (
                {"kind": "concept", "mode": mode, "event_date": days[96], "view_as_of": days[99]}
                if mode == "event"
                else {"kind": "concept", "mode": mode, "as_of": days[99], "window": 60}
            )
            counts.update(prices=0, raw=0)
            start = perf_counter()
            result = sector_review.get_snapshot(repo, **params)
            cold = perf_counter() - start
            reads = dict(counts)
            start = perf_counter()
            sector_review.get_snapshot(repo, **params)
            hot = perf_counter() - start
            results.append(
                {
                    "mode": mode,
                    "cold_seconds": round(cold, 3),
                    "hot_seconds": round(hot, 3),
                    "repository_calls_cold": reads,
                    "repository_calls_hot": {k: counts[k] - reads[k] for k in counts},
                    "summary_bytes": len(
                        json.dumps(result.payload, default=str, ensure_ascii=False).encode()
                    ),
                    "coverage": result.payload["coverage"],
                }
            )
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        print(
            json.dumps(
                {
                    "hardware": platform.machine(),
                    "system": platform.system(),
                    "polars_threads": pl.thread_pool_size(),
                    "stocks": 6000,
                    "concepts": 400,
                    "calendar_days": 100,
                    "peak_rss_mb": round(
                        peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1
                    ),
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
