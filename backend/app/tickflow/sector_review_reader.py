"""Sector review's narrow, strictly local read boundary. Never warms indicators."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import polars as pl

from app.parquet import (
    DAILY_STORAGE_SCHEMA,
    scan_daily_parquet,
    scan_enriched_parquet,
)
from app.polars_guard import guarded_collect


class ReviewDataUnavailableError(RuntimeError):
    pass


def fingerprint(paths: list[Path], root: Path) -> str:
    entries = []
    for path in sorted(set(paths)):
        try:
            stat = path.stat()
            entries.append((str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns))
        except FileNotFoundError:
            entries.append((str(path.relative_to(root)), None, None))
    return hashlib.sha256(json.dumps(entries).encode()).hexdigest()[:24]


class SectorReviewReader:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)

    def files(self, table: str, start: date | None = None, end: date | None = None) -> list[Path]:
        files = []
        for path in (self.data_dir / table).glob("date=*/*.parquet"):
            try:
                day = date.fromisoformat(path.parent.name[5:])
            except ValueError:
                continue
            if (start is None or day >= start) and (end is None or day <= end):
                files.append(path)
        return sorted(files)

    def versions(self, start: date | None = None, end: date | None = None) -> dict:
        versions = {}
        for asset in ("stock", "etf"):
            marker = self.data_dir / f".matrix_generation_{asset}.json"
            try:
                payload = json.loads(marker.read_text())
            except FileNotFoundError:
                payload = None
            except (OSError, ValueError) as exc:
                if asset == "etf":
                    versions["etf"] = "unavailable"
                    continue
                raise ReviewDataUnavailableError("股票指标版本不可读，请等待数据维护完成") from exc
            if payload is not None and (
                not isinstance(payload, dict)
                or payload.get("state", "ready") != "ready"
                or not payload.get("generation")
            ):
                if asset == "etf":
                    versions["etf"] = "publishing"
                    continue
                raise ReviewDataUnavailableError("股票指标正在发布，请稍后刷新")
            versions[asset] = payload.get("generation") if payload else "legacy_file_signature"
        for name, tables in {
            "stock_files": ("kline_daily", "kline_daily_enriched", "instruments"),
            "index_files": ("kline_index_daily",),
            "etf_files": ("kline_etf_daily", "kline_etf_enriched", "instruments_etf"),
        }.items():
            paths = []
            for table in tables:
                paths.extend(
                    self.files(table, start, end)
                    if table.startswith("kline")
                    else (self.data_dir / table).glob("*.parquet")
                )
            versions[name] = fingerprint(paths, self.data_dir)
        versions["calendar"] = fingerprint(
            [self.data_dir / "calendar/trading_days.json"], self.data_dir
        )
        # Includes config changes and additions/deletions, not only parquet mtimes.
        versions["classification"] = fingerprint(
            list((self.data_dir / "ext_data").glob("*/config.json"))
            + list((self.data_dir / "ext_data").rglob("*.parquet")),
            self.data_dir,
        )
        versions["etf_basket"] = "unconfigured-v1"
        return versions

    def instruments(self) -> pl.DataFrame:
        """Keep historical instruments and their current-membership evidence separately."""
        files = sorted((self.data_dir / "instruments").glob("*.parquet"))
        if not files:
            return pl.DataFrame(
                schema={
                    "symbol": pl.String,
                    "name": pl.String,
                    "exchange": pl.String,
                    "listing_date": pl.String,
                    "is_current": pl.Boolean,
                }
            )
        frame = guarded_collect(pl.scan_parquet(files))
        if not {"symbol", "type", "exchange"}.issubset(frame.columns):
            raise ReviewDataUnavailableError("证券维表缺少资产类型或交易所，无法确认A股样本")
        # Asset metadata is required; prefixes only distinguish A/B share classes.
        frame = frame.filter(
            (pl.col("type").str.to_lowercase() == "stock")
            & (
                (
                    (pl.col("exchange") == "SH")
                    & pl.col("symbol").str.contains(r"^(60|68)\d{4}\.SH$")
                )
                | (
                    (pl.col("exchange") == "SZ")
                    & pl.col("symbol").str.contains(r"^(00|30)\d{4}\.SZ$")
                )
                | (
                    (pl.col("exchange") == "BJ")
                    & pl.col("symbol").str.contains(r"^(4|8|92)\d{4,5}\.BJ$")
                )
            )
        )
        for field in ("name", "listing_date"):
            if field not in frame.columns:
                frame = frame.with_columns(pl.lit(None, dtype=pl.String).alias(field))
        if "is_current" not in frame.columns:
            frame = frame.with_columns(pl.lit(None, dtype=pl.Boolean).alias("is_current"))
        return frame.select(
            "symbol", "name", "exchange", pl.col("listing_date").cast(pl.String), "is_current"
        ).unique("symbol")

    def raw(self, table: str, start: date, end: date) -> pl.DataFrame:
        paths = self.files(table, start, end)
        if not paths:
            return pl.DataFrame(schema=DAILY_STORAGE_SCHEMA)
        return guarded_collect(
            scan_daily_parquet(paths, hive_partitioning=True).filter(
                pl.col("date").is_between(start, end)
            )
        )

    def prices(self, start: date, end: date, instruments: pl.DataFrame) -> pl.DataFrame:
        """Formal raw amounts and same-generation adjusted closes, with independent validity."""
        raw = self.raw("kline_daily", start, end).filter(
            pl.col("symbol").is_in(instruments["symbol"].implode())
        )
        paths = self.files("kline_daily_enriched", start, end)
        enriched = (
            guarded_collect(
                scan_enriched_parquet(paths, hive_partitioning=True)
                .filter(pl.col("date").is_between(start, end))
                .select(
                    "symbol",
                    "date",
                    pl.col("close").alias("adj_close"),
                    "raw_close",
                    pl.col("quote_ts").alias("enriched_ts"),
                )
            )
            if paths
            else pl.DataFrame(
                schema={
                    "symbol": pl.String,
                    "date": pl.Date,
                    "adj_close": pl.Float64,
                    "raw_close": pl.Float64,
                    "enriched_ts": pl.Int64,
                }
            )
        )
        if (
            raw.select(pl.struct("symbol", "date").is_duplicated().any()).item()
            or enriched.select(pl.struct("symbol", "date").is_duplicated().any()).item()
        ):
            raise ReviewDataUnavailableError("日线存在重复标的日期，需先核对数据")
        valid = (
            pl.all_horizontal(
                pl.col(c).is_finite() & (pl.col(c) > 0) for c in ("open", "high", "low", "close")
            )
            & (pl.col("high") >= pl.max_horizontal("open", "close", "low"))
            & (pl.col("low") <= pl.min_horizontal("open", "close"))
        )
        halted = ((pl.col("open") == 0) & (pl.col("high") == 0)) | (
            (pl.col("volume") == 0) & (pl.col("amount") == 0)
        )
        frame = (
            raw.join(enriched, on=["symbol", "date"], how="left")
            .with_columns(
                pl.when(pl.col("quote_ts").is_not_null())
                .then(pl.lit("non_formal_snapshot"))
                .when(halted)
                .then(pl.lit("halted"))
                .when(~valid.fill_null(False))
                .then(pl.lit("invalid_quote"))
                .otherwise(None)
                .alias("quote_reason")
            )
            .with_columns(
                pl.when(
                    pl.col("quote_reason").is_null()
                    & pl.col("enriched_ts").is_null()
                    & pl.col("adj_close").is_finite()
                    & (pl.col("adj_close") > 0)
                    & (
                        (pl.col("raw_close") - pl.col("close")).abs()
                        <= pl.col("close").abs() * 1e-8
                    )
                )
                .then(pl.col("adj_close"))
                .otherwise(None)
                .alias("price"),
                pl.when(
                    pl.col("quote_ts").is_null()
                    & ~halted.fill_null(False)
                    & pl.col("amount").is_finite()
                    & (pl.col("amount") >= 0)
                )
                .then(pl.col("amount"))
                .otherwise(None)
                .alias("amount"),
            )
            .with_columns(
                pl.when(pl.col("quote_reason").is_not_null())
                .then(pl.col("quote_reason"))
                .when(pl.col("price").is_null())
                .then(pl.lit("adjusted_price_unavailable"))
                .when(pl.col("amount").is_null())
                .then(pl.lit("invalid_amount"))
                .otherwise(None)
                .alias("reason")
            )
        )
        frame = frame.with_columns(
            (pl.col("reason").is_null() & pl.col("volume").is_finite() & (pl.col("volume") >= 0))
            .fill_null(False)
            .alias("formal_complete")
        )
        return frame.select("symbol", "date", "price", "amount", "reason", "formal_complete")

    def classification(self, config, field: str, cutoff: date) -> pl.DataFrame:
        base = self.data_dir / "ext_data" / config.id
        files = (
            sorted((base / "timeseries").rglob("*.parquet"))
            if config.mode == "timeseries"
            else sorted(base.glob("*.parquet"))
        )
        if not files:
            return pl.DataFrame()
        lf = pl.scan_parquet(files, hive_partitioning=True)
        columns = lf.collect_schema().names()
        if field not in columns:
            return pl.DataFrame()
        if config.mode == "timeseries":
            if "date" not in columns:
                return pl.DataFrame()
            lf = lf.filter(pl.col("date").cast(pl.Date) <= cutoff)
            lf = lf.filter(pl.col("date") == pl.col("date").max())
        needed = {field, "symbol", "code", "股票代码", "代码"}
        for mapping in (config.symbol_map, config.code_map):
            if mapping.get("type") == "mapped":
                needed.add(mapping.get("col"))
        return guarded_collect(lf.select([c for c in columns if c in needed]))
