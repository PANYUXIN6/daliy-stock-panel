"""完整财务记录的本地版本。旧浏览表不构成历史版本证据。

首次采集的 source_update 记录允许保守回填历史；开始观察该源该股票后，
新记录只能从 max(实际观察日, 源版本日期) 之后使用。相同记录不移动首次观察时间。
"""
from __future__ import annotations

import math
from collections.abc import Collection
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import polars as pl

from app.services.fs_utils import atomic_write_parquet

_CHINA = ZoneInfo("Asia/Shanghai")
_LOCAL_COLUMNS = {"observed_at", "available_date", "availability_basis"}
_KEY = ("source", "symbol", "period_end", "disclosure_type")


def version_path(data_dir: Path, table: str) -> Path:
    return data_dir / "financials" / "versions" / table / "part.parquet"


def read_versions(
    data_dir: Path, table: str, columns: Collection[str] | None = None, *, symbol: str | None = None,
) -> pl.DataFrame:
    """损坏时抛错，不能把已有历史当作空表再覆盖。"""
    path = version_path(data_dir, table)
    if not path.exists():
        return pl.DataFrame()
    selected = sorted(set(columns) & set(pl.read_parquet_schema(path))) if columns is not None else None
    if symbol is None:
        return pl.read_parquet(path, columns=selected)
    frame = pl.scan_parquet(path).filter(pl.col("symbol") == symbol)
    if selected is not None:
        frame = frame.select(selected)
    return frame.collect()


def _day(value) -> str | None:
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return None


def _content(row: dict) -> dict:
    # Parquet 的稀疏列与供应商未返回列均视为缺失；NaN 也不代表有效数值。
    return {k: v for k, v in row.items() if k not in _LOCAL_COLUMNS and v is not None
            and not (isinstance(v, float) and math.isnan(v))}


def record_versions(
    data_dir: Path, table: str, frame: pl.DataFrame, source: str, observed_at: datetime,
) -> pl.DataFrame:
    """保存新增版本，返回本次采集各行（带首次观察时间与生效依据）。

    调用方沿用采集执行槽的唯一写入边界。先保存版本再发布浏览快照，崩溃重试
    仍能识别相同内容；不从浏览快照、文件修改时间或报告期推断旧版本。
    """
    if frame.is_empty():
        return frame
    if not {"symbol", "period_end"} <= set(frame.columns):
        raise ValueError("财务记录缺少股票代码或报告期，不能保存版本")
    history = read_versions(data_dir, table)
    latest = {}
    seen = set()
    for row in history.iter_rows(named=True):
        latest[tuple(row[k] for k in _KEY)] = row
        seen.add((row["source"], row["symbol"]))
    if observed_at.tzinfo is None:
        raise ValueError("财务版本观察时间必须包含时区")
    observed_day = observed_at.astimezone(_CHINA).date().isoformat()
    new, captured = [], []
    # seen 使用本轮之前的历史，首次全量采集的各旧报告期同属基线。
    for original in frame.iter_rows(named=True):
        row = {k: v for k, v in original.items() if k not in _LOCAL_COLUMNS}
        if not row.get("symbol") or not _day(row.get("period_end")):
            raise ValueError("财务记录的股票代码或报告期无效，停止保存本次表")
        row["period_end"] = _day(row["period_end"])
        for column in ("announce_date", "source_update_date"):
            if column in row and (day := _day(row[column])) is not None:
                row[column] = day
        row["source"] = source
        # financial 五表默认正式财务数据，预告和快报需显式声明，分别保存。
        row["disclosure_type"] = row.get("disclosure_type") or "formal"
        key = tuple(row[k] for k in _KEY)
        previous = latest.get(key)
        if previous is not None and _content(previous) == _content(row):
            captured.append(previous)
            continue
        updated = _day(row.get("source_update_date"))
        dated = row.get("date_basis") == "source_update" and updated is not None
        bootstrap = (source, row["symbol"]) not in seen
        if bootstrap and dated:
            available = updated
            basis = "source_update_conservative"
        else:
            available = max(observed_day, updated) if dated else observed_day
            basis = "observed"
        row.update(observed_at=observed_at.isoformat(), available_date=available,
                   availability_basis=basis)
        new.append(row)
        captured.append(row)
        latest[key] = row
    if new:
        added = pl.DataFrame(new, infer_schema_length=None)
        merged = pl.concat([history, added], how="diagonal_relaxed") if not history.is_empty() else added
        path = version_path(data_dir, table)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_parquet(merged, path)
    return pl.DataFrame(captured, infer_schema_length=None) if captured else pl.DataFrame()
