"""交易日盘后入口与当日行情／指标验收；执行复用原有管道和 JobStore。"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import polars as pl

from app.market_time import cn_now
from app.services.daily_coverage import DailyCoverage, DailyCoverageError

logger = logging.getLogger(__name__)
JOB_ID = "daily_pipeline"


def stock_universe(repo, day: date) -> list[str]:
    # 使用本轮刚落盘的维表，不能读尚未刷新的进程缓存或混入 ETF／指数自选。
    path = repo.store.data_dir / "instruments" / "instruments.parquet"
    frame = pl.read_parquet(path)
    if frame.is_empty() or not {"symbol", "as_of"}.issubset(frame.columns):
        raise DailyCoverageError("当日股票维表为空或缺少同步日期")
    if not frame["as_of"].eq(day).fill_null(False).all():
        raise DailyCoverageError("股票维表尚未更新到当日")
    if "listing_date" in frame.columns:
        listed = pl.col("listing_date").cast(pl.String).str.slice(0, 10).str.to_date(strict=False)
        frame = frame.filter(listed.is_null() | (listed <= day))
    if "type" in frame.columns:
        frame = frame.filter(pl.col("type").is_null() | (pl.col("type") == "stock"))
    if "is_current" in frame.columns:
        # 只排除来源明确声明的非当前成员；旧表／未声明状态的插件仍严格验收缺行。
        frame = frame.filter(pl.col("is_current").fill_null(True))
    symbols = sorted(set(frame["symbol"].drop_nulls().to_list()))
    if not symbols:
        raise DailyCoverageError("当日 A 股股票池为空")
    return symbols


def reconcile_daily_status(coverage: DailyCoverage, expected: list[str], *,
                           check_cancel=None) -> None:
    """仅在自动任务中，以独立交易状态证据解释本轮正式日线的缺行。"""
    from app.data_providers import trading_status

    check = check_cancel or (lambda: None)
    check()
    requested = set(expected)
    if requested & coverage.failed or not coverage.active:
        return
    missing = requested - coverage.active - coverage.halted - coverage.not_listed
    if not missing:
        return
    try:
        status = trading_status.get_daily_trading_status(coverage.day, sorted(missing), check_cancel=check)
    except trading_status.TradingStatusError as exc:
        raise DailyCoverageError(f"{coverage.day} 正式日线缺行的交易状态核验失败：{exc}") from exc
    check()
    if status.day != coverage.day:
        raise DailyCoverageError("交易状态证据日期与盘后目标日期不一致")
    halted = missing & status.halted
    not_listed = missing & status.not_listed
    if halted & not_listed:
        raise DailyCoverageError("交易状态证据冲突：同一标的同时为停牌和未上市")
    coverage.halted.update(halted)
    coverage.not_listed.update(not_listed)
    logger.info("%s 缺行状态核验(%s)：确认停牌 %d 只，尚未上市 %d 只，未解释 %d 只",
                coverage.day, status.source, len(halted), len(not_listed),
                len(missing - halted - not_listed))


def require_enriched(data_dir: Path, coverage: DailyCoverage, *, asset_type: str = "stock") -> dict:
    from app.enriched_generation import enriched_publication_incomplete

    day = coverage.day
    if enriched_publication_incomplete(data_dir, asset_type):
        raise DailyCoverageError("日线指标尚未完成发布")
    table = {"stock": "kline_daily", "index": "kline_index_daily"}[asset_type]
    enriched_table = {"stock": "kline_daily_enriched", "index": "kline_index_enriched"}[asset_type]
    daily_paths = list((data_dir / table / f"date={day}").glob("*.parquet"))
    enriched_paths = list((data_dir / enriched_table / f"date={day}").glob("*.parquet"))
    if not daily_paths or not enriched_paths:
        raise DailyCoverageError(f"{day} 日线或指标分区尚未生成")
    columns = ["symbol", "date", "raw_close", "raw_high", "raw_low", "amount", "volume"]
    if asset_type == "stock":
        columns.extend(["consecutive_limit_ups", "consecutive_limit_downs"])
    enriched = pl.read_parquet(enriched_paths, columns=columns)
    if asset_type == "index":
        enriched = enriched.filter(pl.col("symbol").is_in(sorted(coverage.active)))
    if (enriched["date"].ne(day).any()
            or set(enriched["symbol"].drop_nulls().to_list()) != coverage.active):
        raise DailyCoverageError(f"{day} 指标标的覆盖与本轮正式日线不一致")
    if enriched["symbol"].n_unique() != enriched.height:
        raise DailyCoverageError(f"{day} 指标存在重复标的")
    daily = pl.read_parquet(daily_paths, columns=["symbol", "close", "high", "low", "amount", "volume"])
    paired = enriched.join(daily, on="symbol", how="inner", suffix="_daily")
    checks = [
        pl.col(column).is_not_null() & pl.col(column).is_finite()
        for column in columns if column not in {"symbol", "date"}
    ]
    checks.extend((pl.col(left) - pl.col(right)).abs() < 0.005 for left, right in (
        ("raw_close", "close"), ("raw_high", "high"), ("raw_low", "low"),
        ("amount", "amount_daily"), ("volume", "volume_daily"),
    ))
    if paired.height != enriched.height or not paired.select(pl.all_horizontal(checks).fill_null(False).all()).item():
        raise DailyCoverageError(f"{day} 指标缺值或与正式日线数值不一致")
    return {"date": day.isoformat(), "symbols": enriched.height}


async def run_scheduled(repo) -> dict | None:
    """16:00 起每 15 分钟检查一次；只在本地日历确认的交易日运行。"""
    from fastapi import HTTPException

    from app.api.pipeline import trigger_pipeline_job
    from app.jobs.daily_pipeline import _get_app_state
    from app.services import preferences, trading_day
    from app.services.pipeline_jobs import job_store

    now = cn_now()
    schedule = preferences.get_pipeline_schedule()
    if (now.hour, now.minute) < (schedule["hour"], schedule["minute"]):
        return None
    if trading_day.is_trading_day(now, data_dir=repo.store.data_dir) is not True:
        return None
    key = f"postmarket:{now.date()}"
    # 既有任务终态已落盘，重启仍能去重；失败或取消不视为完成。
    if any(job.get("request_key") == key and (job["status"] == "succeeded"
           or job.get("error") == "用户手动取消")
           for job in job_store.list_recent(limit=200)):
        return None
    state = _get_app_state()
    if state is None or not getattr(state, "capabilities", None):
        return None
    try:
        return await trigger_pipeline_job(repo, state.capabilities,
                                          getattr(state, "quote_service", None), postmarket=True)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        logger.info("盘后自动同步等待现有数据任务完成")
        return None
