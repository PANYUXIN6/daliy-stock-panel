"""历史交易日数据完整性检测 — 停机缺口 / 盘中快照判别。

场景: 用户盘中停机后, 次日再启动并开实时行情, 实时 flush 写出"今天"分区后,
盘后管道的「今天已有数据 → 只刷今天」分支会让停机日的盘中快照永久留存
(close=停机时刻价, volume=半日累计, 技术指标全错且污染后续 lookback 类指标)。

判据 (quote_ts 列, 毫秒 Unix 时间戳, 仅实时 flush 写入真实值):
- null              → batch 拉取 / 盘后计算写入的权威历史 → 完整
- d < 今天 且 时刻 < d 15:30 → 实时写入 (盘中快照或 close_final 定版) → 坏
- d < 今天 且 时刻 ≥ d 15:30 → 完整 (仅理论可能: 盘后管道 16:00 前的极晚写入)
- batch 权威行中仅夹杂少量零成交实时行 → 停牌残留 → 忽略
- d == 今天         → 实时更新中, 属正常, 不校验
- 分区缺失的候选日  → 缺口 (配置了 fuyao apikey → 按真实交易日检测;
  未配置 → 直接降级工作日近似; 节假日误报对修复只是空拉取, 但对
  realtime_gate 是 409 死锁 — 休市日永远无数据, 门禁循环放行不了)

自动检查优先读取 parquet 元数据; 显式 deep 自检额外按有界批次验证日K字段和数值。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dt_time
from pathlib import Path

import polars as pl

from app.market_time import CN_TZ, cn_today

logger = logging.getLogger(__name__)

# 尾盘定版线: 实时侧写入的分区一律不算定版 — 15:00 后的 close_final 定版也不
# 可信 (实测有源"时间戳翻新但价格停在竞价前旧价"), 且 15:30 前科创/创业板盘后
# 固定价交易仍在成交 (量未定版)。数据完整以盘后管道 (默认 16:00, 见
# daily_pipeline) 的官方 batch 行覆盖为准; 定版线取盘后固定价终止线 15:30,
# 与 quote_service._FINAL_DEADLINE 的 close_final 重试截止对齐 (15:30 后不再
# 有新实时写入, 判 15:30 与判 15:35 等价)。
CLOSE_CUTOFF = dt_time(15, 30)

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
    kind: str  # snapshot / missing / corrupt / invalid / unconfirmed
    # snapshot 且能定位时: 缺权威行(batch)的 symbol 清单 (修复只拉这些);
    # 空 = 干净或无法定位 (missing / 列缺失 / 读失败), 修复回退全市场。
    symbols: tuple[str, ...] = ()
    reason: str = ""
    files: tuple[str, ...] = ()


def active_daily_tables() -> tuple[str, ...]:
    """自动检查只覆盖当前实际启用的资产族, 显式自检可另选范围。"""
    from app.services import preferences

    enabled = (
        preferences.get_pipeline_pull_a_share() or preferences.get_realtime_pull_stock(),
        preferences.get_pipeline_pull_etf() or preferences.get_realtime_pull_etf(),
        preferences.get_pipeline_pull_index(),
    )
    return tuple(table for table, on in zip(_DAILY_TABLES, enabled, strict=True) if on)


DAILY_REQUIRED = ("symbol", "date", "open", "high", "low", "close", "volume")


def _daily_validation(frame: pl.DataFrame, day: date | None) -> tuple[pl.DataFrame, dict[str, pl.Expr]]:
    """数值判据同时用于拦截与错误说明, 停牌零价沿用现有规则。"""
    if not set(DAILY_REQUIRED).issubset(frame.columns):
        raise ValueError("日K字段不完整")
    frame = frame.with_columns(
        pl.col("symbol").cast(pl.String, strict=False),
        pl.col("date").cast(pl.Date, strict=False),
        *(pl.col(c).cast(pl.Float64, strict=False) for c in DAILY_REQUIRED[2:]),
    )
    prices = ("open", "high", "low", "close")
    halted = ((pl.col("volume") == 0) & (pl.col("open") == 0)
              & (pl.col("high") == 0) & (pl.col("low") == 0) & (pl.col("close") >= 0))
    checks = {
        "股票代码缺失": pl.col("symbol").is_null() | (pl.col("symbol").str.strip_chars() == ""),
        "日期缺失或非法": pl.col("date").is_null(),
        "成交量缺失、非有限或为负": (
            pl.col("volume").is_null() | ~pl.col("volume").is_finite() | (pl.col("volume") < 0)
        ),
        **{f"{label}缺失或非有限": pl.col(column).is_null() | ~pl.col(column).is_finite()
           for column, label in zip(prices, ("开盘价", "最高价", "最低价", "收盘价"), strict=True)},
        "非停牌日开高低收存在零价或负价": (
            ~halted.fill_null(False) & pl.any_horizontal(pl.col(c) <= 0 for c in prices)
        ),
        "最高价小于开盘价、收盘价或最低价": (
            ~halted.fill_null(False) & (pl.col("high") < pl.max_horizontal("open", "close", "low"))
        ),
        "最低价大于开盘价、收盘价或最高价": (
            ~halted.fill_null(False) & (pl.col("low") > pl.min_horizontal("open", "close", "high"))
        ),
    }
    if day is not None:
        checks["日期与文件分区不符"] = pl.col("date") != day
    if "amount" in frame.columns:
        amount = pl.col("amount").cast(pl.Float64, strict=False)
        checks["成交额非有限或为负"] = ((amount < 0) | ~amount.is_finite()).fill_null(False)
    return frame, checks


def invalid_daily_rows(frame: pl.DataFrame, day: date | None = None) -> pl.DataFrame:
    """自检与入库校验共享同一判据; 停牌零价与异常数值明确区分。"""
    frame, checks = _daily_validation(frame, day)
    invalid = pl.lit(False)
    for check in checks.values():
        invalid = invalid | check
    return frame.filter(invalid.fill_null(True))


def _invalid_daily_reason(frame: pl.DataFrame, day: date) -> str:
    frame, checks = _daily_validation(frame, day)
    flags = frame.select(check.fill_null(False).any().alias(label) for label, check in checks.items()).row(0, named=True)
    return "；".join(label for label, found in flags.items() if found)


def inspect_invalid_history(root: Path, table: str, files: list[Path], invalid: pl.DataFrame) -> list[IntegrityIssue]:
    """只在重算发现坏行后定位源分区; 行内日期损坏时仍按真实分区修补。"""
    from app.parquet import scan_daily_parquet

    paths = (
        scan_daily_parquet(files, include_file_paths="__integrity_file")
        .join(invalid.select("symbol", "date").unique().lazy(),
              on=["symbol", "date"], how="semi", nulls_equal=True)
        .select("__integrity_file").unique().collect(engine="streaming")["__integrity_file"]
    )
    days = sorted({date.fromisoformat(Path(path).parent.name.removeprefix("date=")) for path in paths})
    # 与显式自检一致: 当天仍在更新, 不能登记成尚无法修补的历史问题。
    today = cn_today()
    return [issue for day in days if day < today and (issue := inspect_daily_partition(root, table, day)) is not None]


def _partition_problem(
    root: Path, part: Path, day: date, *, deep: bool,
) -> tuple[str, str, tuple[str, ...], tuple[str, ...]] | None:
    """有界逐文件检查, 读取错误不能与「权威历史没有 quote_ts」混为一谈。"""
    import pyarrow.parquet as pq

    paths = sorted(part.glob("*.parquet"))
    if not paths:
        return "corrupt", "日期目录内没有日K文件", (), ()
    bad: list[str] = []
    reasons: list[str] = []
    kind = "invalid"
    rows = 0
    symbols: set[str] = set()
    unknown_symbols = False
    for path in paths:
        if not path.resolve().is_relative_to(root.resolve()):
            return "corrupt", "文件链接指向数据目录以外, 请手动检查目录", (), ()
        try:
            parquet = pq.ParquetFile(path)
            rows += parquet.metadata.num_rows
            if not deep:
                continue
            if not set(DAILY_REQUIRED).issubset(parquet.schema.names):
                bad.append(path.name)
                reasons.append("日K字段不完整")
                unknown_symbols = True
                continue
            columns = [*DAILY_REQUIRED, *(["amount"] if "amount" in parquet.schema.names else [])]
            for batch in parquet.iter_batches(batch_size=65536, columns=columns):
                invalid = invalid_daily_rows(pl.from_arrow(batch), day)
                if not invalid.is_empty():
                    bad.append(path.name)
                    reasons.append(_invalid_daily_reason(invalid, day))
                    values = invalid["symbol"].unique().to_list()
                    unknown_symbols |= any(s is None or not s.strip() for s in values)
                    symbols.update(s for s in values if s and s.strip())
        except Exception as exc:  # noqa: BLE001 — 单文件失败记录为检查问题, 继续检查其他文件
            logger.warning("日K文件检查失败 %s: %s", path, exc)
            bad.append(path.name)
            reasons.append("日K文件损坏或无法读取")
            kind = "corrupt"
            unknown_symbols = True
    if bad:
        return (kind, "；".join(dict.fromkeys(reasons)), tuple(dict.fromkeys(bad)),
                () if unknown_symbols else tuple(sorted(symbols)))
    if rows == 0:
        return "corrupt", "日期目录内的日K文件为空", tuple(p.name for p in paths), ()
    return None


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


def _partition_snapshot_defects(
    day: date, part_dir: Path, quote_ts_max_ms: int | None
) -> set[str] | None:
    """分区级快照判定 → 缺陷 symbol 集合 (issue #514: 判坏下沉到 symbol 级)。

    batch 行用 null quote_ts 标识权威历史。实时轮询曾把停牌股票的 09:15、
    零成交记录写入分区; 后续 batch 会过滤停牌日, merge-upsert 因而留下这些
    孤立行。旧逻辑「任一行有成交即判坏整分区」会让 4 行残留触发全市场重拉。

    返回语义:
    - None      : 无法判定但按坏处理 (volume/amount 列缺失 / 读失败 / 无 symbol
                  列却有成交残留) — 保守 fail-closed, 修复回退全市场;
    - 空集合    : 分区干净 (残留 symbol 均已有权威行, 或仅零成交残留且权威行占多数);
    - 非空集合  : 恰好这些 symbol 在窗口内写了有成交实时行且无权威行 → 只修它们。
    """
    if not _is_snapshot(day, quote_ts_max_ms):
        return set()

    start_ms = int(datetime.combine(day, dt_time.min, tzinfo=CN_TZ).timestamp() * 1000)
    cutoff_ms = int(datetime.combine(day, CLOSE_CUTOFF, tzinfo=CN_TZ).timestamp() * 1000)
    authoritative_rows = 0
    suspicious_rows = 0
    authoritative_symbols: set[str] = set()
    suspicious_symbols: set[str] = set()  # 窗口内实时行的 symbol (含零成交)
    active_symbols: set[str] = set()  # 窗口内且有成交的实时行 symbol

    for path in sorted(part_dir.glob("*.parquet")):
        try:
            schema = pl.read_parquet_schema(path)
            if "quote_ts" not in schema:
                continue
            columns = [
                name for name in ("symbol", "quote_ts", "volume", "amount")
                if name in schema
            ]
            frame = pl.read_parquet(path, columns=columns).with_columns(
                pl.col("quote_ts").cast(pl.Int64, strict=False),
            )
            has_symbol = "symbol" in frame.columns
            authoritative = frame.filter(pl.col("quote_ts").is_null())
            authoritative_rows += authoritative.height
            if has_symbol:
                authoritative_symbols.update(authoritative["symbol"].drop_nulls().to_list())
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
                return None
            active = suspicious.filter(
                pl.any_horizontal(
                    pl.col(name).cast(pl.Float64, strict=False).fill_null(0) > 0
                    for name in activity_columns
                )
            )
            if has_symbol:
                suspicious_symbols.update(suspicious["symbol"].drop_nulls().to_list())
                active_symbols.update(active["symbol"].drop_nulls().to_list())
            elif active.height > 0:
                return None  # 无 symbol 列却存在成交残留, 无法定位 → 整分区保守判坏
        except (OSError, pl.exceptions.PolarsError) as e:
            logger.debug("snapshot residue scan skipped %s: %s", path, e)
            return None

    if active_symbols:
        # 有成交残留: 只把「无权威行」的 symbol 记为缺陷; 两行兼有 = 无害滞后残留。
        return active_symbols - authoritative_symbols
    # 零成交残留: 沿用旧比例兜底 (权威行不占多数 → 整分区仍判坏, 现有行为锁定)。
    if suspicious_rows > 0 and authoritative_rows <= suspicious_rows:
        defects = suspicious_symbols - authoritative_symbols
        return defects or None  # 定位不到 symbol (如无 symbol 列) → 按未知处理
    return set()


def _partition_is_snapshot(day: date, part_dir: Path, quote_ts_max_ms: int | None) -> bool:
    """整个分区是否仍是盘中快照 (旧布尔口径, 供既有调用方; 内部走 symbol 级判据)。"""
    defects = _partition_snapshot_defects(day, part_dir, quote_ts_max_ms)
    return defects is None or bool(defects)


def inspect_daily_partition(data_dir: Path, table: str, day: date, *, deep: bool = True) -> IntegrityIssue | None:
    if table not in TABLE_FAMILY:
        raise ValueError("不支持的日K资产类型")
    part = Path(data_dir) / table / f"date={day}"
    if not part.is_dir():
        return IntegrityIssue(day, table, "missing", reason="交易日日K缺失")
    problem = _partition_problem(Path(data_dir), part, day, deep=deep)
    if problem:
        kind, reason, files, symbols = problem
        return IntegrityIssue(day, table, kind, symbols=symbols, reason=reason, files=files)
    defects = _partition_snapshot_defects(day, part, _quote_ts_max_ms(part))
    if defects is None or defects:
        return IntegrityIssue(day, table, "snapshot", symbols=tuple(sorted(defects)) if defects else ())
    return None


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
    的判定, 故直接消费整年集合。线程安全 (gate / boot / 修复管道共用)。
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
    tables: Iterable[str] | None = None,
    deep: bool = False,
    on_progress: Callable[[int, int], None] | None = None,
) -> list[IntegrityIssue]:
    """扫描最近交易日的数据完整性, 返回坏分区列表 (按日期升序)。

    每族表独立判定; 自动检查跳过近期无活动的表, 显式 deep 自检检查所选资产。
    首次同步前的日期不推断为缺失, deep 模式下未知开市日期仅报告待核实。
    """
    data_dir = Path(data_dir)
    today = today or datetime.now(CN_TZ).date()
    window_start = today - timedelta(days=lookback_days)
    issues: list[IntegrityIssue] = []

    selected = tuple(tables) if tables is not None else _DAILY_TABLES
    if not set(selected).issubset(_DAILY_TABLES):
        raise ValueError("不支持的日K资产类型")
    partitions: dict[str, set[date]] = {}
    for table in _DAILY_TABLES:
        base = data_dir / table
        existing: set[date] = set()
        if base.exists():
            for part in base.glob("date=*"):
                if not part.is_dir():
                    continue
                try:
                    existing.add(date.fromisoformat(part.name[5:]))
                except ValueError:
                    continue
        partitions[table] = existing

    candidates = set() if deep else set(_candidate_days(today, lookback_days))
    unconfirmed: set[date] = set()
    if deep:
        from app.services.trading_day import calendar_status

        stored_calendar = calendar_status(data_dir)
        calendar = {date.fromisoformat(d) for d in stored_calendar.get("days", [])}
        known_days = set().union(*partitions.values())
        # 没有权威日历时, 其他资产的本地分区可以证明当天确实开市;
        # 其余工作日仅列为待核实, 不拿节假日假缺口反复重拉。
        weekdays = {window_start + timedelta(days=i) for i in range(lookback_days)
                    if (window_start + timedelta(days=i)).weekday() < 5}
        if calendar and stored_calendar.get("start") and stored_calendar.get("end"):
            covered_start = date.fromisoformat(stored_calendar["start"])
            covered_end = date.fromisoformat(stored_calendar["end"])
            candidates = {d for d in calendar if window_start <= d < today}
            unconfirmed = {d for d in weekdays if d < covered_start or d > covered_end} - known_days
        else:
            unconfirmed = weekdays - known_days
            candidates = weekdays & known_days
        candidates |= {d for d in known_days if window_start <= d < today}
        candidates |= unconfirmed

    total = max(1, len(selected) * len(candidates))
    done = 0
    for table in selected:
        base = data_dir / table
        existing = partitions[table]
        latest = max(existing) if existing else None
        # 族内近期无活动 → 不判定 (首次启动 / 长期停用)
        if latest is None or (not deep and latest < window_start):
            done += len(candidates)
            if on_progress:
                on_progress(done, total)
            continue

        earliest = min(existing)
        for day in sorted(candidates):
            done += 1
            if on_progress:
                on_progress(done, total)
            if day not in existing:
                if day >= earliest and (deep or day > latest):
                    issues.append(IntegrityIssue(
                        day=day, table=table,
                        kind="unconfirmed" if day in unconfirmed else "missing",
                        reason="交易日历不可用, 该日期是否开市尚未确认" if day in unconfirmed else "交易日日K缺失",
                    ))
                continue
            issue = inspect_daily_partition(data_dir, table, day, deep=deep)
            if issue is not None:
                issues.append(issue)

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
    最近 60 天做历史前缀重算 (修复窗口 ≤5 天, 回看充足)。
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
    """面向用户的一句话描述 (409 详情 / 日志用)。"""
    if not issues:
        return ""
    labels = {
        "kline_daily": "股票日K",
        "kline_etf_daily": "ETF日K",
        "kline_index_daily": "指数日K",
    }
    groups: dict[tuple[str, str], set[date]] = {}
    for issue in issues:
        groups.setdefault((issue.table, issue.kind), set()).add(issue.day)
    parts = []
    for (table, kind), days in groups.items():
        day_text = "、".join(d.isoformat() for d in sorted(days))
        reason = {
            "snapshot": "停机前的盘中快照", "missing": "缺失",
            "corrupt": "文件损坏或为空", "invalid": "日K数值异常", "unconfirmed": "缺失待核实",
        }.get(kind, "需要检查")
        parts.append(f"{labels.get(table, '日K')}：{day_text} 的数据为{reason}")
    return "；".join(parts)
