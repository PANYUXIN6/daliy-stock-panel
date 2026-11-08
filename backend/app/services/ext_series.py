"""扩展数值列的只读图表序列，按可见标的批量读取有限日期窗口。"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

import polars as pl

from app.services.ext_data import ExtConfig


def _partitions(config: ExtConfig, data_dir: Path) -> list[tuple[str, Path]]:
    base = data_dir / "ext_data" / config.id / "timeseries"
    if not base.is_dir():
        return []
    parts = []
    for directory in base.iterdir():
        if not directory.name.startswith("date="):
            continue
        day = directory.name[5:]
        try:
            if date.fromisoformat(day).isoformat() != day:
                continue
        except ValueError:
            continue
        path = directory / "part.parquet"
        if path.is_file():
            parts.append((day, path))
    return sorted(parts)


def _time_of_day(column: str) -> pl.Expr:
    text = pl.col(column).cast(pl.String)
    # 沿用扩展数据的北京时间墙钟格式：HH:MM[:SS] 或 ISO 日期前缀。
    value = pl.when(text.str.len_chars() >= 16).then(text.str.slice(11, 8)).otherwise(text)
    return pl.when(value.str.len_chars() == 5).then(value + ":00").otherwise(value)


def effective_granularity(config: ExtConfig, data_dir: Path) -> Literal["intraday", "daily"] | None:
    if config.mode != "timeseries":
        return None
    if config.pull and config.pull.granularity in ("intraday", "daily"):
        return config.pull.granularity
    time_field = config.pull.time_field if config.pull else None
    if not time_field:
        return "daily"
    partitions = _partitions(config, data_dir)
    if not partitions:
        return "intraday"
    try:
        times = pl.read_parquet(partitions[-1][1], columns=[time_field]).select(
            _time_of_day(time_field).alias("time")
        )["time"].drop_nulls()
    except (OSError, pl.exceptions.PolarsError):
        # 无可读样本时保留 time_field 配置意图，真正读取序列仍会报告错误。
        return "intraday"
    return "intraday" if (times.str.contains(":") & (times != "00:00:00")).any() else "daily"


def read_ext_series(
    config: ExtConfig,
    data_dir: Path,
    field: str,
    *,
    end_date: str | None,
    symbols: list[str] | None,
    days: int,
    granularity: Literal["auto", "intraday", "daily"],
) -> dict:
    """保留实际交易分区；缺日不补零，日内指定日期不存在时不回退。"""
    if config.mode != "timeseries":
        raise ValueError("仅时序扩展表支持序列查询")
    effective = effective_granularity(config, data_dir) if granularity == "auto" else granularity
    time_field = config.pull.time_field if config.pull else None
    if effective == "intraday" and not time_field:
        raise ValueError("日内序列需要配置时间列 (time_field)")
    partitions = _partitions(config, data_dir)
    if end_date:
        partitions = [part for part in partitions if part[0] <= end_date]
    if effective == "intraday":
        partitions = [part for part in partitions[-1:] if not end_date or part[0] == end_date]
    else:
        partitions = partitions[-days:]
    result = {
        "config_id": config.id, "field": field, "granularity": effective,
        "date": end_date if effective == "intraday" else None, "items": {},
    }
    if not partitions:
        return result
    result["date"] = partitions[-1][0] if effective == "intraday" else f"{partitions[0][0]}..{partitions[-1][0]}"
    if effective == "daily":
        result["days"] = len(partitions)

    frames = []
    for day, path in partitions:
        frame = pl.scan_parquet(path)
        columns = frame.collect_schema().names()
        if "symbol" not in columns or field not in columns:
            continue
        if symbols is not None:
            frame = frame.filter(pl.col("symbol").cast(pl.String).is_in(symbols))
        if effective == "intraday":
            if time_field not in columns:
                continue
            point_time = _time_of_day(time_field)
        else:
            mapped_date = config.pull.field_map.get("date") if config.pull else None
            date_column = next((name for name in (mapped_date, "date", "data_date") if name and name in columns), None)
            point_time = pl.col(date_column).cast(pl.String).str.slice(0, 10) if date_column else pl.lit(day)
            # 多盘表按真正时刻取日末值；不能依赖旧分区的物理行顺序。
            if time_field and time_field in columns:
                frame = frame.sort(time_field, maintain_order=True)
        frame = frame.select(
            pl.col("symbol").cast(pl.String), point_time.alias("time"),
            pl.col(field).cast(pl.Float64, strict=False).alias("value"),
        ).filter(pl.col("symbol").is_not_null() & pl.col("value").is_finite())
        if effective == "intraday":
            frame = frame.filter(pl.col("time").str.contains(r"^(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d$"))
        else:
            frame = frame.filter(
                pl.col("time").str.to_date("%Y-%m-%d", strict=False).is_not_null()
                & (pl.col("time") <= (end_date or partitions[-1][0]))
            )
        frames.append(frame.collect())
    if not frames:
        return result
    points = (
        pl.concat(frames)
        .group_by(["symbol", "time"], maintain_order=True).agg(pl.col("value").last())
        .sort(["symbol", "time"])
    )
    for symbol, point_time, value in points.iter_rows():
        result["items"].setdefault(symbol, []).append([point_time, value])
    return result
