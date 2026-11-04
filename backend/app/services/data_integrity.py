"""历史交易日数据完整性检测 — 停机缺口 / 盘中快照判别。

场景: 用户盘中停机后, 次日再启动并开实时行情, 实时 flush 写出"今天"分区后,
盘后管道的「今天已有数据 → 只刷今天」分支会让停机日的盘中快照永久留存
(close=停机时刻价, volume=半日累计, 技术指标全错且污染后续 lookback 类指标)。

判据 (quote_ts 列, 毫秒 Unix 时间戳, 仅实时 flush 写入真实值):
- null              → batch 拉取 / 盘后计算写入的权威历史 → 完整
- d < 今天 且 时刻 < d 15:00 → 盘中快照 (停机前实时写的) → 坏
- d < 今天 且 时刻 ≥ d 15:00 → 尾盘定版 (close_final) → 完整
- batch 权威行中仅夹杂少量零成交实时行 → 停牌残留 → 忽略
- d == 今天         → 实时更新中, 属正常, 不校验
- 分区缺失的候选日  → 缺口 (fuyao 日历可用按真实交易日, 否则工作日近似)

显式盘后管道使用检测结果选择修复起点；启动和实时开关不创建修复任务。

检测成本: 每分区只读 parquet 元数据 statistics (不解压数据页), 实测 ~0.5ms/分区。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ

logger = logging.getLogger(__name__)

# 尾盘定版线: quote_ts 达到当日 15:00 即视为收盘后写入 (含 close_final 定版)
CLOSE_CUTOFF = dt_time(15, 0)

# 扫描窗口: 最近 N 个自然日内、今天之前的交易日
SCAN_WINDOW_DAYS = 7

# 参与检测的日K族表 (实时 flush 会写这三族的 daily/enriched)
_DAILY_TABLES = ("kline_daily", "kline_etf_daily", "kline_index_daily")

# 表 → 资产族 (用于管道/修复侧按族取起点)
TABLE_FAMILY = {
    "kline_daily": "stock",
    "kline_etf_daily": "etf",
    "kline_index_daily": "index",
}


@dataclass(frozen=True)
class IntegrityIssue:
    day: date
    table: str
    kind: str  # "snapshot"=盘中快照 | "missing"=分区缺失


def _quote_ts_max_ms(part_dir: Path) -> int | None:
    """读单个日期分区的 max(quote_ts); 列不存在/全 null/无统计 → None。

    优先走 parquet 元数据 row-group statistics (不解压数据页),
    statistics 缺失时回退 polars 列扫描。
    """
    import pyarrow.parquet as pq

    candidates: list[int | None] = []
    files = sorted(part_dir.glob("*.parquet"))
    if not files:
        return None
    for path in files:
        try:
            meta = pq.read_metadata(path)
            names = [meta.schema.column(i).name for i in range(meta.num_columns)]
            if "quote_ts" not in names:
                continue
            idx = names.index("quote_ts")
            file_max: int | None = None
            for rg in range(meta.num_row_groups):
                stats = meta.row_group(rg).column(idx).statistics
                if stats is not None and stats.max is not None:
                    value = stats.max
                    file_max = int(value) if file_max is None else max(file_max, int(value))
            if file_max is None and meta.num_rows > 0:
                # statistics 未写入 → 回退列扫描
                file_max = (
                    pl.scan_parquet(path)
                    .select(pl.col("quote_ts").max())
                    .collect()
                    .item()
                )
            candidates.append(file_max)
        except Exception as e:  # noqa: BLE001
            logger.debug("quote_ts scan skipped %s: %s", path, e)
    values = [v for v in candidates if v is not None]
    return max(values) if values else None


def _is_snapshot(day: date, quote_ts_ms: int | None) -> bool:
    """非空 quote_ts 且对应北京时间时刻早于当日收盘线 → 盘中快照。"""
    if quote_ts_ms is None:
        return False
    try:
        ts = datetime.fromtimestamp(int(quote_ts_ms) / 1000, tz=CN_TZ)
    except (OverflowError, OSError, ValueError):
        return False
    return ts.date() == day and ts.time() < CLOSE_CUTOFF


def _partition_is_snapshot(day: date, part_dir: Path, quote_ts_max_ms: int | None) -> bool:
    """判断整个分区是否仍是盘中快照, 而非同步后遗留的停牌实时行。

    batch 行用 null quote_ts 标识权威历史。实时轮询曾把停牌股票的 09:15、
    零成交记录写入分区; 后续 batch 会过滤停牌日, merge-upsert 因而留下这些
    孤立行。若分区已有 batch 行, 且当日收盘前的实时行全部零成交, 则它们不应
    让整个分区反复进入修复。整分区都是实时行时仍按快照处理, 包括盘前零成交。
    """
    if not _is_snapshot(day, quote_ts_max_ms):
        return False

    start_ms = int(datetime.combine(day, dt_time.min, tzinfo=CN_TZ).timestamp() * 1000)
    cutoff_ms = int(datetime.combine(day, CLOSE_CUTOFF, tzinfo=CN_TZ).timestamp() * 1000)
    authoritative_rows = 0
    suspicious_rows = 0

    for path in sorted(part_dir.glob("*.parquet")):
        try:
            schema = pl.read_parquet_schema(path)
            if "quote_ts" not in schema:
                continue
            columns = [
                name for name in ("quote_ts", "volume", "amount")
                if name in schema
            ]
            frame = pl.read_parquet(path, columns=columns).with_columns(
                pl.col("quote_ts").cast(pl.Int64, strict=False),
            )
            authoritative_rows += frame["quote_ts"].null_count()
            suspicious = frame.filter(
                pl.col("quote_ts").is_between(start_ms, cutoff_ms, closed="left")
            )
            if suspicious.is_empty():
                continue
            suspicious_rows += suspicious.height

            activity_columns = [
                name for name in ("volume", "amount") if name in suspicious.columns
            ]
            if not activity_columns:
                return True
            has_activity = suspicious.select(
                pl.any_horizontal(
                    pl.col(name).cast(pl.Float64, strict=False).fill_null(0) > 0
                    for name in activity_columns
                ).any()
            ).item()
            if has_activity:
                return True
        except (OSError, pl.exceptions.PolarsError) as e:
            logger.debug("snapshot residue scan skipped %s: %s", path, e, exc_info=True)
            return True

    return suspicious_rows > 0 and authoritative_rows <= suspicious_rows


def _candidate_days(today: date, lookback_days: int) -> list[date]:
    """最近 lookback_days 自然日内、严格早于今天的交易日。

    fuyao 交易日历可用时按真实日历过滤 (休市日不进候选, 消除节假日误报 —
    2026 中秋 09-25 休市日曾被当缺失日, realtime_gate 409 死锁);
    从未取到日历时回退工作日近似 (历史行为)。
    """
    calendar = _trading_calendar()
    days: list[date] = []
    for offset in range(1, lookback_days + 1):
        d = today - timedelta(days=offset)
        if calendar is not None:
            if d in calendar:
                days.append(d)
        elif d.weekday() < 5:
            days.append(d)
    return sorted(days)


# ---------------------------------------------------------------------------
# 交易日历 (fuyao) — 「工作日近似」在节假日误报 missing; 修复任务对休市日
# 永远拉不到数据, realtime_gate 因此循环 409 (2026-09-25 中秋实证)。
# 取数失败时沿用上一次成功结果 (stale-while-error): 节假日表近乎不变,
# 陈旧日历仍远好于退回工作日近似 (长假期内误报会让门禁反复死锁)。
# ---------------------------------------------------------------------------
_CAL_TTL_S = 1800.0  # 成功/失败结论统一 30 分钟复取 (日历变化极稀疏)
_CAL_LOCK = threading.Lock()
_CAL: tuple[float, set[date] | None] = (0.0, None)  # (取数时刻, 交易日集合)


def _trading_calendar() -> set[date] | None:
    """A 股交易日集合 (fuyao 近一年); 从未取到过 → None (调用方回退周几近似)。

    与 trading_day.is_trading_day 探测链第 1 环同源; 这里需要**任意历史日**
    的判定, 故直接消费整年集合。线程安全，供显式盘后管道扫描使用。
    """
    now = time.monotonic()
    global _CAL
    with _CAL_LOCK:
        fetched_at, cached = _CAL
        if fetched_at and now - fetched_at < _CAL_TTL_S:
            return cached
    fetched: set[date] | None = None
    try:
        from app.data_providers import custom as custom_sources

        if custom_sources.is_custom_provider("fuyao"):
            raw = custom_sources.get_provider("fuyao").trading_days()
            if raw:
                fetched = set(raw)
    except Exception:  # noqa: BLE001 — 日历不可用不上抛, 回退 stale/近似
        fetched = None
    with _CAL_LOCK:
        _CAL = (now, fetched if fetched is not None else cached)
        return _CAL[1]


def scan_recent_integrity(
    data_dir: Path,
    *,
    today: date | None = None,
    lookback_days: int = SCAN_WINDOW_DAYS,
) -> list[IntegrityIssue]:
    """扫描最近交易日的数据完整性, 返回坏分区列表 (按日期升序)。

    每族表独立判定; 族内"最近无任何活动"(最新分区早于窗口)时整族跳过 —
    覆盖首次启动(无数据)与长期停用(用户自主)两类不应自动修复的场景。
    """
    data_dir = Path(data_dir)
    today = today or datetime.now(CN_TZ).date()
    window_start = today - timedelta(days=lookback_days)
    issues: list[IntegrityIssue] = []

    for table in _DAILY_TABLES:
        base = data_dir / table
        existing: set[date] = set()
        if base.exists():
            for part in base.glob("date=*"):
                try:
                    existing.add(date.fromisoformat(part.name[5:]))
                except ValueError:
                    continue
        latest = max(existing) if existing else None
        # 族内近期无活动 → 不判定 (首次启动 / 长期停用)
        if latest is None or latest < window_start:
            continue

        for day in _candidate_days(today, lookback_days):
            if day not in existing:
                # 只报"尾部缺口": 晚于本地最新分区的缺失日。
                # 历史内部空洞是另一类问题(laggards), 已有独立告警, 不在此扩面。
                if day > latest:
                    issues.append(IntegrityIssue(day=day, table=table, kind="missing"))
                continue
            part_dir = base / f"date={day.isoformat()}"
            quote_ts = _quote_ts_max_ms(part_dir)
            if _partition_is_snapshot(day, part_dir, quote_ts):
                issues.append(IntegrityIssue(day=day, table=table, kind="snapshot"))

    issues.sort(key=lambda i: (i.day, i.table))
    return issues


def earliest_issue_day(
    issues: list[IntegrityIssue],
    tables: Iterable[str] | None = None,
) -> date | None:
    """坏分区中最早的一天; tables 限定参与的表族 (None=全部)。"""
    scoped = (
        [i for i in issues if i.table in tables] if tables is not None else issues
    )
    return min((i.day for i in scoped), default=None)


def prune_enriched_partitions(
    data_dir: Path,
    start: date,
    table: str = "kline_daily_enriched",
) -> int:
    """删除 enriched 表 date ≥ start 的日期分区, 使修复重算把它们当"新日期"。

    股票 enriched 增量重算只算 enriched 里不存在的日期; 盘中快照日分区已存在
    (虽是错的), 不删则永远不会被重算。指数/ETF 的 enriched 走 merge-upsert
    全行覆盖, 无需删除。删除后 run_pipeline(new_dates_only=True) 用剩余分区
    最近 60 天做历史前缀重算，修复起点由调用方明确传入。
    """
    base = Path(data_dir) / table
    if not base.exists():
        return 0
    import shutil

    removed = 0
    for part in base.glob("date=*"):
        try:
            d = date.fromisoformat(part.name[5:])
        except ValueError:
            continue
        if d >= start:
            shutil.rmtree(part, ignore_errors=True)
            removed += 1
    return removed


def describe_issues(issues: list[IntegrityIssue]) -> str:
    """面向用户的一句话描述，供管道日志使用。"""
    if not issues:
        return ""
    days = sorted({i.day for i in issues})
    day_text = "、".join(d.isoformat() for d in days)
    kinds = {i.kind for i in issues}
    reason = "停机前的盘中快照" if "snapshot" in kinds else "缺失"
    return f"{day_text} 的数据为{reason}"
