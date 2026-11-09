"""显式检查并补齐看板所需的正式日线及其依赖。"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import polars as pl

from app.indicators.pipeline import filter_halt_days
from app.market_time import CN_TZ, cn_now
from app.parquet import scan_daily_parquet
from app.services import daily_health, data_integrity, kline_sync, preferences, trading_day
from app.services.data_integrity import IntegrityIssue
from app.services.index_const import CORE_INDEX_SYMBOLS
from app.tickflow.repository import daily_dirname, enriched_dirname

START = date(2024, 1, 1)
KEYS = ["symbol", "date"]
KEY_SCHEMA = {"symbol": pl.String, "date": pl.Date}


def _keys(frame: pl.DataFrame) -> pl.DataFrame:
    return frame.select(pl.col(name).cast(dtype) for name, dtype in KEY_SCHEMA.items())


def _day_files(repo, asset: str, end: date) -> dict[date, list[Path]]:
    files = defaultdict(list)
    for path in repo.daily_repair_files(asset):
        try:
            day = date.fromisoformat(path.parent.name.removeprefix("date="))
        except ValueError:
            continue
        if START <= day <= end:
            files[day].append(path)
    return files


def _missing_indicators(repo, asset: str, day: date, raw: pl.DataFrame) -> list[str | None]:
    active = filter_halt_days(raw)
    if active.is_empty():
        return []
    part = repo.store.data_dir / enriched_dirname(asset) / f"date={day}"
    paths = sorted(part.glob("*.parquet"))
    for path in paths:
        repo._validate_repair_path(path)
    if not paths:
        return [None]
    try:
        enriched = pl.concat([pl.read_parquet(p) for p in paths], how="diagonal_relaxed")
        # 使用原始价和行情来源判断指标输入是否仍和当前日线一致。
        cols = {"close": "raw_close", "high": "raw_high", "low": "raw_low", "volume": "volume"}
        if "amount" in active.columns:
            cols["amount"] = "amount"
        for frame, names in ((active, cols), (enriched, cols.values())):
            if not set(names).issubset(frame.columns):
                return [None]
        inputs = [*cols, "quote_ts"]
        actual = [*cols.values(), "quote_ts"]

        def values(frame, columns):
            return _keys(frame).hstack(frame.select([
                pl.col(c).cast(pl.Int64 if c == "quote_ts" else pl.Float64, strict=False).alias(name)
                if c in frame.columns else pl.lit(None, dtype=pl.Int64).alias(name)
                for c, name in zip(columns, inputs, strict=True)
            ]))

        expected = values(active, inputs)
        missing = expected.join(values(enriched, actual), on=[*KEYS, *inputs], how="anti", nulls_equal=True)
        return missing["symbol"].unique().to_list()
    except (OSError, pl.exceptions.PolarsError):
        return [None]


def _scan_local(repo, asset: str, files: dict[date, list[Path]], progress):
    keys, problems, indicators = [], {}, {}
    root, table = repo.store.data_dir, daily_dirname(asset)
    for done, (day, paths) in enumerate(sorted(files.items()), 1):
        progress("integrity_check", 5, f"检查{daily_health.ASSET_LABELS[asset]} {done}/{len(files)}",
                 stage_pct=100 * done // len(files), skip_log=done < len(files))
        issue = data_integrity.inspect_daily_partition(root, table, day)
        if issue:
            repo.validate_daily_repair_target(asset, day)
            problems[day] = issue
            if not issue.symbols:
                continue
        raw = pl.concat([pl.read_parquet(p) for p in paths], how="diagonal_relaxed")
        if issue:
            raw = raw.filter(~pl.col("symbol").is_in(issue.symbols))
        active = filter_halt_days(raw)
        if "quote_ts" in active.columns:
            cutoff = int(_time(day, data_integrity.CLOSE_CUTOFF).timestamp() * 1000)
            midnight = int(_time(day + timedelta(days=1), time.min).timestamp() * 1000)
            ts = pl.col("quote_ts")
            closed = ts.is_null() | ts.cast(pl.Int64, strict=False).is_between(cutoff, midnight, closed="left")
            snapshots = set(active.filter(~closed.fill_null(False))["symbol"].to_list())
            snapshots.difference_update(active.filter(closed)["symbol"].to_list())
            if snapshots:
                snapshots.update(issue.symbols if issue else ())
                issue = problems[day] = IntegrityIssue(day, table, "snapshot", tuple(sorted(snapshots)), "日线尚未正式定版")
                raw = raw.filter(~pl.col("symbol").is_in(snapshots))
        if asset == "stock":
            # 缺成交额的正式行也不能作为可用的看板输入。
            amount = (pl.col("amount").cast(pl.Float64, strict=False) if "amount" in raw.columns
                      else pl.lit(None, dtype=pl.Float64))
            missing_amount = filter_halt_days(raw).filter(~amount.is_finite().fill_null(False))["symbol"].to_list()
            if missing_amount:
                symbols = set(missing_amount) | (set(issue.symbols) if issue else set())
                problems[day] = IntegrityIssue(day, table, "invalid", tuple(sorted(symbols)), "正式日线缺成交额或数值异常")
                raw = raw.filter(~pl.col("symbol").is_in(missing_amount))
        keys.append(_keys(raw))
        if missing := _missing_indicators(repo, asset, day, raw):
            indicators[day] = missing
    return (pl.concat(keys).unique() if keys else pl.DataFrame(schema=KEY_SCHEMA)), problems, indicators


def _bounds(repo, asset: str, local: pl.DataFrame, end: date) -> pl.DataFrame:
    observed = local.group_by("symbol").agg(pl.col("date").min().alias("start"), pl.col("date").max().alias("end"))
    instruments = repo.get_instruments_asset(asset)
    if asset == "index":
        instruments = pl.DataFrame({"symbol": list(CORE_INDEX_SYMBOLS)})
        observed = observed.filter(pl.col("symbol").is_in(CORE_INDEX_SYMBOLS))
    elif asset == "stock" and "type" in instruments.columns:
        instruments = instruments.filter(pl.col("type").is_null() | (pl.col("type") == asset))
    if instruments.is_empty():
        return observed
    listing = (pl.col("listing_date").cast(pl.String).str.slice(0, 10).str.to_date(strict=False)
               if "listing_date" in instruments.columns else pl.lit(None, dtype=pl.Date))
    current = (pl.col("is_current").fill_null(True) if "is_current" in instruments.columns else pl.lit(True))
    return (instruments.select("symbol", listing.alias("listed"), current.alias("current")).unique("symbol")
            .join(observed, on="symbol", how="full", coalesce=True)
            .select("symbol", pl.coalesce("listed", pl.when(pl.col("current")).then(pl.lit(START)), "start").alias("start"),
                    pl.when(pl.col("current")).then(pl.lit(end)).otherwise(pl.col("end")).alias("end"))
            .drop_nulls())


def _time(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=CN_TZ)


def _stage_daily(capset, asset, local, bounds, problems, days, unknown, staging, progress):
    candidates = (pl.DataFrame({"date": sorted(days)}, schema={"date": pl.Date}).join(bounds, how="cross")
                  .filter(pl.col("date").is_between("start", "end")).select(KEYS)
                  .join(local, on=KEYS, how="anti"))
    targets = set(candidates["date"].unique().to_list()) | set(problems) | unknown
    if not targets:
        return {}, candidates, pl.DataFrame(schema=KEY_SCHEMA)
    symbols = set(candidates["symbol"].unique().to_list())
    symbols.update(s for issue in problems.values() for s in issue.symbols)
    if unknown or any(not issue.symbols for issue in problems.values()):
        symbols.update(bounds["symbol"].to_list())
    if not symbols:
        return {}, candidates, pl.DataFrame(schema=KEY_SCHEMA)
    label = daily_health.ASSET_LABELS[asset]
    progress("sync_daily", 20, f"向数据源核对{label}日线缺口（{len(symbols)} 只）")

    def tick(done, total, *_):
        progress("sync_daily", 20, f"核对{label}日线 {done}/{total}",
                 stage_pct=100 * done // max(total, 1), skip_log=done < total)

    staged = defaultdict(list)
    received = []
    for batch, frame in enumerate(kline_sync.iter_daily_frames(
        sorted(symbols), capset, asset_type=asset, start_time=_time(min(targets), time.min),
        end_time=_time(max(targets), time.max), on_chunk_done=tick,
    )):
        if frame.is_empty():
            continue
        frame = (frame.with_columns(pl.col("date").cast(pl.Date, strict=False), pl.col("symbol").cast(pl.String))
                 .filter(pl.col("date").is_in(targets) & pl.col("symbol").is_in(symbols))
                 .join(local, on=KEYS, how="anti"))
        if frame.is_empty():
            continue
        if not data_integrity.invalid_daily_rows(frame).is_empty() or (
            "quote_ts" in frame.columns and frame["quote_ts"].is_not_null().any()
        ):
            raise ValueError(f"{label}数据源返回了异常日线或实时快照，请检查数据源")
        if asset == "stock" and (not {"amount"} <= set(frame.columns) or
                                  filter_halt_days(frame).filter(~pl.col("amount").is_finite().fill_null(False)).height):
            raise ValueError("股票数据源返回的正式日线缺少有效成交额")
        received.append(_keys(frame))
        for (month,), chunk in frame.with_columns(pl.col("date").dt.month_start().alias("month")).partition_by("month", as_dict=True).items():
            path = staging / f"{asset}-{month}-{batch}.parquet"
            chunk.drop("month").sort("date").write_parquet(path)
            staged[month].append(path)
    available = pl.concat(received).unique() if received else pl.DataFrame(schema=KEY_SCHEMA)
    return staged, candidates.join(available, on=KEYS, how="anti"), available


def _collect_targets(asset, problems, available):
    targets, received = dict(problems), {}
    for (day,), group in available.partition_by("date", as_dict=True).items():
        symbols = set(group["symbol"].to_list())
        received[day] = len(symbols)
        issue = problems.get(day)
        if issue is None:
            targets[day] = IntegrityIssue(day, daily_dirname(asset), "missing", tuple(sorted(symbols)), "源日线有记录，本地缺失")
        elif issue.symbols:
            targets[day] = replace(issue, symbols=tuple(sorted(symbols | set(issue.symbols))))
    return targets, received


def _classification_gaps(repo, progress):
    from app.services.ext_data import ExtConfigStore
    from app.services.ext_presets import fetch_preset
    from app.services.rps_rotation import invalidate_cache

    root = repo.store.data_dir
    store = ExtConfigStore(root)
    added = 0
    for name in ("ext_gn_ths", "ext_hy_ths"):
        path = root / "ext_data" / name / "part.parquet"
        repo._validate_repair_path(path)
        repo._validate_repair_path(path.with_name("config.json"))
        if (store.get(name) is not None and path.is_file()
                and not pl.read_parquet(path, columns=["symbol"]).is_empty()):
            continue
        progress("sync_classifications", 40, "补齐缺失的概念或行业分类")
        added += asyncio.run(fetch_preset(name, root))
    if added:
        invalidate_cache()
    return added


def complete_history(repo, capset, progress) -> dict:
    from app.services import (
        index_sync,
        instrument_sync,
        market_mainline,
        postmarket,
        regime_builder,
    )
    from app.services.integrity_check import known_issues
    from app.services.repair_daily import repair_integrity_issues
    from app.services.sentiment_observations import analysis_readiness

    root = repo.store.data_dir
    now = cn_now()
    end = now.date() if now.time() >= time(16) else now.date() - timedelta(days=1)
    assets = ["stock", "index"] + (["etf"] if preferences.get_pipeline_pull_etf() else [])
    progress("integrity_check", 0, f"检查 {START} 至 {end} 的正式日线及指标")
    calendar = trading_day.calendar_status(root)
    if not calendar or calendar["end"] < end.isoformat():
        trading_day.sync_calendar(root)
        calendar = trading_day.calendar_status(root)
    files = {asset: _day_files(repo, asset, end) for asset in assets}
    days = {d for d in map(date.fromisoformat, calendar.get("days", [])) if START <= d <= end}
    days.update(set().union(*(set(found) for found in files.values())))
    unknown = {day for day in (START + timedelta(days=i) for i in range((end - START).days + 1))
               if day.weekday() < 5 and day not in days
               and not calendar.get("start", "9999") <= day.isoformat() <= calendar.get("end", "0000")}
    local = {asset: _scan_local(repo, asset, files[asset], progress) for asset in assets}

    if not instrument_sync.instruments_synced_on(root, now.date()):
        progress("sync_instruments", 12, "更新用于核对覆盖的股票名单")
        if not instrument_sync.sync_instruments(root, require_complete=True):
            raise ValueError("未获取到完整股票名单，请检查数据源")
        repo.rebuild_views()
        repo._refresh_instruments()
    postmarket.stock_universe(repo, now.date())
    if "etf" in assets and repo.get_instruments_asset("etf").is_empty():
        index_sync.sync_index_instruments(repo, pull_index=False, pull_etf=True)
    bounds = {asset: _bounds(repo, asset, local[asset][0], end) for asset in assets}
    if not index_sync.daily_source_available(capset):
        raise ValueError("当前日线数据源不可用，请检查数据源配置")

    with TemporaryDirectory(prefix=".history-completion-", dir=root) as temp:
        staged, registered, issues, missing, received = {}, {}, [], {}, {}
        for asset in assets:
            good, problems, _ = local[asset]
            staged[asset], missing[asset], available = _stage_daily(
                capset, asset, good, bounds[asset], problems, days, unknown, Path(temp), progress,
            )
            targets, received[asset] = _collect_targets(asset, problems, available)
            issues.extend(targets.values())
            registered[asset] = [replace(i, symbols=()) if len(i.symbols) > 200 else i for i in targets.values()]

        factor_rows = 0
        for asset in (a for a in assets if a != "index"):
            progress("sync_adj_factor", 35, f"核对{daily_health.ASSET_LABELS[asset]}除权因子")
            factor_dir = "adj_factor_etf" if asset == "etf" else "adj_factor"
            repo._validate_repair_path(root / factor_dir / "all.parquet")
            added, changed = kline_sync.sync_adj_factor(
                bounds[asset]["symbol"].to_list(), repo, capset, start_time=_time(START, time.min),
                end_time=_time(end, time.max), asset_type=asset, require_complete=True, only_missing=True,
                on_chunk_done=lambda done, total: progress("sync_adj_factor", 35, f"核对除权因子 {done}/{total}",
                                                          stage_pct=100 * done // max(total, 1), skip_log=True),
            )
            factor_rows += added
            if changed:
                earliest = min((p.parent.name[5:] for p in repo.daily_repair_files(asset)), default=START.isoformat())
                daily_health.record_recompute(root, asset, date.fromisoformat(earliest), changed)

        progress("sync_classifications", 40, "检查概念和行业分类")
        classifications = _classification_gaps(repo, progress)

        for asset in assets:
            daily_health.record_scan(root, registered[asset], assets=[asset], start=START, end=end)
            for day, symbols in local[asset][2].items():
                daily_health.record_recompute(root, asset, day, symbols if len(symbols) <= 200 else [None])

        def fetch(symbols, _caps, *, start_time, asset_type, **_):
            day = start_time.date()
            paths = staged[asset_type].get(day.replace(day=1), [])
            if paths:
                yield scan_daily_parquet(paths).filter(
                    (pl.col("date") == day) & pl.col("symbol").is_in(symbols),
                ).collect()

        planned = {(i.table, i.day) for i in issues}
        issues.extend(i for i in known_issues(root, assets, end)
                      if i.kind == "recompute" and (i.table, i.day) not in planned)
        repaired = repair_integrity_issues(
            repo, capset, issues, backup_dir=root / "integrity_backups" / uuid4().hex,
            on_progress=lambda stage, pct, message, **kw: progress(stage, max(pct, 45), message, **kw),
            fetch_daily=fetch,
        ) if issues else {"repaired_partitions": 0}

    progress("compute_regime", 94, "复查市场环境和主线覆盖")
    prior = regime_builder.load_regime_history(root)
    unclosed = (prior.filter(pl.col("date").is_between(START, end) & ~pl.col("is_complete_day"))["date"].min()
                if "is_complete_day" in prior.columns else None)
    regime_builder.compute_regime_incremental(repo, root, today=end, refresh_from=unclosed)
    for kind in ("concept", "industry"):
        market_mainline.compute_mainline_incremental(repo, root, today=end, kind=kind,
                                                    refresh_from=START if classifications else None)
    observed = regime_builder.load_regime_history(root)
    valid_dates = observed.filter(pl.col("date") <= end)["date"].to_list() if not observed.is_empty() else []
    analysis = analysis_readiness(root, max(valid_dates).isoformat()) if valid_dates else None
    if not analysis:
        raise ValueError("补齐后仍无可用的市场环境数据，请检查股票与指数日线")
    progress("done", 100, "历史数据检查与补齐结束")
    return {**repaired, "analysis": analysis, "history_completion": {
        "start_date": START.isoformat(), "end_date": end.isoformat(), "today_deferred": end != now.date(),
        "daily_rows": sum(sum(by_day.values()) for by_day in received.values()),
        "factor_rows": factor_rows, "unverified_days": len(unknown - set().union(*(set(v) for v in received.values()))),
        "unverified_rows": sum(frame.height for frame in missing.values()),
    }}
