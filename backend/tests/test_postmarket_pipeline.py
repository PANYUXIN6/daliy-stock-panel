"""盘后自动执行依赖顺序、当日覆盖验收和调度恢复。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import polars as pl
import pytest

from app.api import pipeline
from app.indicators.pipeline import run_pipeline as calculate_indicators
from app.jobs import daily_pipeline
from app.market_time import CN_TZ
from app.services import (
    data_integrity,
    ext_presets,
    market_mainline,
    pipeline_jobs,
    postmarket,
    preferences,
    regime_builder,
    review_publication,
    user_runtime,
)
from app.services.daily_coverage import DailyCoverage, DailyCoverageError
from app.services.index_const import CORE_INDEX_SYMBOLS
from app.services.pipeline_jobs import JobStore
from app.services.regime_builder import compute_regime_incremental as calculate_environment
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

DAY = date(2026, 10, 16)
SYMBOLS = ["600000.SH", "000001.SZ"]


def bars(symbols=SYMBOLS, day=DAY):
    return pl.DataFrame({
        "symbol": symbols, "date": [day] * len(symbols),
        **{c: [v] * len(symbols) for c, v in {
            "open": 10.0, "high": 11.0, "low": 9.0, "close": 10.5,
            "volume": 100.0, "amount": 100000.0,
        }.items()},
    })


def write(data_dir, table, frame):
    p = data_dir / table / f"date={DAY}" / "part.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(p)


@pytest.fixture
def closing(tmp_path, monkeypatch):
    steps = []
    controls = SimpleNamespace(daily=bars(), bad_enriched=False, index_symbols=list(CORE_INDEX_SYMBOLS))
    monkeypatch.setattr(daily_pipeline, "cn_today", lambda: DAY)
    monkeypatch.setattr(daily_pipeline, "cn_now", lambda: datetime(2026, 10, 16, 16, tzinfo=CN_TZ))
    monkeypatch.setattr(preferences, "load", lambda: {
        "pipeline_pull_a_share": False, "pipeline_pull_index": False,
        "pipeline_regime_enabled": False, "minute_sync_enabled": True,
    })
    monkeypatch.setattr(daily_pipeline, "_invalidate", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_views", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_refresh_instruments_view", lambda *_: None)
    monkeypatch.setattr(data_integrity, "scan_recent_integrity", lambda *_, **__: [])
    monkeypatch.setattr(daily_pipeline, "warn_if_enriched_too_thin", lambda *_: 250)
    monkeypatch.setattr(daily_pipeline, "_resolve_minute_symbols", lambda *_: SYMBOLS)

    def instruments(data_dir, **kwargs):
        assert kwargs == {"require_complete": True}
        steps.append("instruments")
        path = data_dir / "instruments" / "instruments.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({"symbol": SYMBOLS, "as_of": [DAY, DAY]}).write_parquet(path)
        return len(SYMBOLS)

    def daily(symbols, repo, capset, *, coverage, **kwargs):
        assert symbols == SYMBOLS or set(symbols) == set(SYMBOLS)
        assert kwargs["end_date"].date() == DAY
        steps.append("daily")
        coverage.observe(controls.daily)
        write(tmp_path, "kline_daily", controls.daily)
        return controls.daily.height

    def indicators(**kwargs):
        steps.append("enriched")
        frame = controls.daily.with_columns(
            pl.col("close").alias("raw_close"), pl.col("high").alias("raw_high"),
            pl.col("low").alias("raw_low"), pl.lit(0).alias("consecutive_limit_ups"),
            pl.lit(0).alias("consecutive_limit_downs"),
        )
        if controls.bad_enriched:
            frame = frame.head(1)
        write(tmp_path, "kline_daily_enriched", frame)
        return frame.height

    def index(repo, capset, *, coverage, symbols_override, **kwargs):
        steps.append("indices")
        assert symbols_override == list(CORE_INDEX_SYMBOLS)
        frame = bars(controls.index_symbols)
        coverage.observe(frame)
        write(tmp_path, "kline_index_daily", frame)
        write(tmp_path, "kline_index_enriched", frame.with_columns(
            pl.col("close").alias("raw_close"), pl.col("high").alias("raw_high"), pl.col("low").alias("raw_low"),
        ))
        return len(controls.index_symbols)

    async def classifications(data_dir, results, on_progress):
        steps.append("classifications")
        results.update({"ext_gn_ths": 2, "ext_hy_ths": 2})
        return results

    def environment(repo, data_dir, **kwargs):
        steps.append("regime")
        result = pl.DataFrame({"date": [DAY], "state": ["range"], "score": [50], "phase": ["repair"]})
        regime_builder.upsert_regime_history(data_dir, result)
        return result

    def mainline(repo, data_dir, *, kind, refresh_today):
        assert refresh_today is True
        steps.append(kind)
        return pl.DataFrame()

    monkeypatch.setattr(daily_pipeline.instrument_sync, "sync_instruments", instruments)
    monkeypatch.setattr(daily_pipeline.kline_sync, "sync_and_persist_daily_batch", daily)
    monkeypatch.setattr(daily_pipeline.kline_sync, "sync_daily_by_quotes", Mock(side_effect=AssertionError("不能用实时快照替代正式日线")))
    monkeypatch.setattr(daily_pipeline.kline_sync, "sync_adj_factor", lambda *_, **__: (steps.append("adj") or 0, []))
    monkeypatch.setattr(daily_pipeline, "run_pipeline", indicators)
    monkeypatch.setattr(daily_pipeline.index_sync, "sync_index_instruments", lambda *_, **__: 4)
    monkeypatch.setattr(daily_pipeline.index_sync, "sync_and_persist_index_daily", index)
    monkeypatch.setattr(ext_presets, "sync_classifications", classifications)
    monkeypatch.setattr(regime_builder, "compute_regime_incremental", environment)
    monkeypatch.setattr(market_mainline, "compute_mainline_incremental", mainline)
    monkeypatch.setattr(user_runtime, "settle_user_accounts", lambda *_: steps.append("paper") or {})
    monkeypatch.setattr(daily_pipeline.kline_sync, "sync_and_persist_minute", lambda *_, **__: steps.append("minute") or 0)
    monkeypatch.setattr(daily_pipeline, "_push_phase_change_alert", lambda *_: None)
    monkeypatch.setattr(daily_pipeline, "_sync_recap_boards", lambda *_: steps.append("boards"))
    monkeypatch.setattr(review_publication, "publish_review_if_ready", lambda *_: steps.append("publication"))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), latest_daily_date=lambda: DAY,
                           refresh_cache=lambda: steps.append("cache"), refresh_index_views=lambda: None,
                           symbols_lagging=lambda *_, **__: [])
    state = SimpleNamespace(repo=repo, quote_service=None,
                            depth_service=SimpleNamespace(finalize=lambda: steps.append("depth")))
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: state)
    caps = CapabilitySet({cap: CapabilityLimits() for cap in (Cap.KLINE_DAILY_BATCH, Cap.ADJ_FACTOR, Cap.QUOTE_POOL, Cap.KLINE_MINUTE_BATCH)})
    state.capabilities = caps
    return repo, caps, steps, controls


def test_automatic_order_forces_core_inputs_and_environment_but_keeps_manual_preferences(closing):
    repo, caps, steps, _ = closing
    result = daily_pipeline.run_now(repo, caps, postmarket=True)
    ordered = ["instruments", "daily", "adj", "enriched", "indices", "classifications",
               "regime", "concept", "industry", "paper", "minute", "depth", "boards", "publication"]
    assert [step for step in steps if step != "cache"] == ordered
    assert steps.index("cache") < steps.index("regime")
    assert result["postmarket_date"] == DAY.isoformat()
    assert result["readiness"]["daily"]["active"] == 2
    assert result["readiness"]["indices"]["active"] == 4
    assert result["readiness"]["enriched"]["symbols"] == 2
    assert preferences.get_pipeline_regime_enabled() is False


@pytest.mark.parametrize("problem", ["daily", "enriched", "indices"])
def test_missing_current_inputs_never_compute_or_publish_environment(closing, problem):
    repo, caps, steps, controls = closing
    if problem == "daily":
        controls.daily = bars(SYMBOLS[:1])
    elif problem == "enriched":
        controls.bad_enriched = True
    else:
        controls.index_symbols = list(CORE_INDEX_SYMBOLS[:1])
    with pytest.raises(DailyCoverageError):
        daily_pipeline.run_now(repo, caps, postmarket=True)
    assert "regime" not in steps
    assert "paper" not in steps
    assert "publication" not in steps


def test_existing_today_indicator_is_recomputed_even_when_only_amount_changes(closing):
    repo, caps, steps, _ = closing
    daily_pipeline.run_now(repo, caps, postmarket=True)
    steps.clear()
    daily_pipeline.run_now(repo, caps, postmarket=True)
    assert "enriched" in steps
    assert steps.index("enriched") < steps.index("regime")


def test_empty_recompute_cannot_reuse_old_same_day_environment(closing, monkeypatch):
    repo, caps, steps, _ = closing
    regime_builder.upsert_regime_history(repo.store.data_dir, pl.DataFrame({
        "date": [DAY], "state": ["range"], "score": [50], "phase": ["repair"],
    }))
    monkeypatch.setattr(regime_builder, "compute_regime_incremental", lambda *_, **__: pl.DataFrame())
    with pytest.raises(DailyCoverageError, match="未完成"):
        daily_pipeline.run_now(repo, caps, postmarket=True)
    assert "publication" not in steps


def test_queued_automatic_job_cannot_run_for_a_different_day(closing):
    repo, caps, steps, _ = closing
    with pytest.raises(DailyCoverageError, match="跨日期"):
        daily_pipeline.run_now(repo, caps, postmarket=True, target_date=date(2026, 10, 15))
    assert steps == []


def test_real_indicator_storage_and_environment_builder_complete_together(closing, monkeypatch):
    mocked_repo, caps, _, _ = closing
    repo = KlineRepository(DataStore(mocked_repo.store.data_dir))
    history = []
    cursor = DAY - timedelta(days=1)
    while len(history) < 35:
        if cursor.weekday() < 5:
            history.append(bars(day=cursor))
        cursor -= timedelta(days=1)
    repo.append_daily(pl.concat(history))
    monkeypatch.setattr(daily_pipeline, "run_pipeline", calculate_indicators)
    monkeypatch.setattr(regime_builder, "compute_regime_incremental", calculate_environment)
    monkeypatch.setattr(daily_pipeline, "_refresh_single_view", lambda r, _: r.rebuild_views())
    monkeypatch.setattr(daily_pipeline, "_refresh_instruments_view", lambda r: r.rebuild_views())
    monkeypatch.setattr(daily_pipeline, "_refresh_views", lambda r: r.rebuild_views())
    try:
        result = daily_pipeline.run_now(repo, caps, postmarket=True)
        frame = regime_builder.load_regime_history(repo.store.data_dir).filter(pl.col("date") == DAY)
        assert result["regime_days"] > 0
        assert frame.height == 1
        assert frame["phase"][0] is not None
        assert frame["score"][0] is not None
        assert repo.get_enriched_latest()[1] == DAY
    finally:
        repo.db.close()


@pytest.fixture
def schedule(tmp_path, monkeypatch):
    now = SimpleNamespace(value=datetime(2026, 10, 16, 16, tzinfo=CN_TZ))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    monkeypatch.setattr(postmarket, "cn_now", lambda: now.value)
    monkeypatch.setattr(preferences, "load", lambda: {})
    from app.services import trading_day
    monkeypatch.setattr(trading_day, "is_trading_day", lambda when, **kw: when.date() == DAY)
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: SimpleNamespace(capabilities=object(), quote_service=None))
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    submit = AsyncMock(return_value={"job_id": "new"})
    monkeypatch.setattr(pipeline, "trigger_pipeline_job", submit)
    return repo, now, store, submit


def test_schedule_respects_beijing_time_calendar_and_busy_retry(schedule):
    from fastapi import HTTPException
    repo, now, _, submit = schedule
    now.value = now.value.replace(hour=15, minute=59)
    assert asyncio.run(postmarket.run_scheduled(repo)) is None
    submit.assert_not_awaited()
    now.value = now.value.replace(hour=16, minute=0)
    submit.side_effect = [HTTPException(409, "busy"), {"job_id": "after_busy"}]
    assert asyncio.run(postmarket.run_scheduled(repo)) is None
    assert asyncio.run(postmarket.run_scheduled(repo)) == {"job_id": "after_busy"}
    now.value = now.value.replace(day=17)
    assert asyncio.run(postmarket.run_scheduled(repo)) is None
    assert submit.await_count == 2


def test_failed_day_retries_and_success_deduplicates_after_restart(schedule, monkeypatch):
    repo, _, store, submit = schedule
    job_id, _ = store.create(request_key=f"postmarket:{DAY}")
    store.start(job_id)
    store.fail(job_id, "上游未齐")
    assert asyncio.run(postmarket.run_scheduled(repo)) == {"job_id": "new"}
    job_id, _ = store.create(request_key=f"postmarket:{DAY}")
    store.start(job_id)
    store.succeed(job_id, {"postmarket_date": str(DAY)})
    monkeypatch.setattr(pipeline_jobs, "job_store", JobStore(store_dir=repo.store.data_dir / "jobs"))
    assert asyncio.run(postmarket.run_scheduled(repo)) is None
    assert submit.await_count == 1


def test_user_cancel_pauses_automatic_retries_for_that_day(schedule):
    repo, _, store, submit = schedule
    job_id, _ = store.create(request_key=f"postmarket:{DAY}")
    store.fail(job_id, "用户手动取消")
    assert asyncio.run(postmarket.run_scheduled(repo)) is None
    submit.assert_not_awaited()


@pytest.mark.parametrize("failed", [False, True])
def test_automatic_api_job_publishes_after_cache_refresh_even_on_partial_failure(tmp_path, monkeypatch, failed):
    from app.services import shared_runtime

    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(preferences, "load", lambda: {})
    steps = []
    def compute(*args, **kwargs):
        steps.append("compute")
        assert kwargs["postmarket"] is True
        if failed:
            raise RuntimeError("上游未齐")
        return {"daily_days": 1}
    monkeypatch.setattr(daily_pipeline, "run_now", compute)
    monkeypatch.setattr(daily_pipeline, "run_weekly_mining_after_sync", Mock())
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda root, datasets: steps.append("publish"))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_cache=lambda: steps.append("cache"))

    async def run():
        result = await pipeline.trigger_pipeline_job(repo, object(), postmarket=True)
        await asyncio.gather(*pipeline._tasks)
        return result["job_id"]
    job_id = asyncio.run(run())
    assert steps == ["compute", "cache", "publish"]
    assert store.get(job_id)["status"] == ("failed" if failed else "succeeded")


def test_default_and_legacy_pipeline_time_are_not_before_1600(monkeypatch):
    monkeypatch.setattr(preferences, "load", lambda: {})
    assert preferences.get_pipeline_schedule() == {"hour": 16, "minute": 0}
    monkeypatch.setattr(preferences, "load", lambda: {"pipeline_schedule": {"hour": 15, "minute": 35}})
    assert preferences.get_pipeline_schedule() == {"hour": 16, "minute": 0}


@pytest.mark.parametrize("change", ["yesterday", "snapshot", "null", "missing"])
def test_coverage_does_not_treat_stale_snapshot_or_invalid_prices_as_complete(change):
    frame = bars()
    if change == "yesterday":
        frame = frame.with_columns(pl.lit(date(2026, 10, 15)).alias("date"))
    elif change == "snapshot":
        frame = frame.with_columns(pl.lit(123).alias("quote_ts"))
    elif change == "null":
        frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias("close"))
    else:
        frame = frame.head(1)
    coverage = DailyCoverage(DAY)
    coverage.observe(frame)
    with pytest.raises(DailyCoverageError):
        coverage.require(SYMBOLS)


def test_explicit_halted_rows_are_allowed_but_unknown_missing_symbols_are_not():
    coverage = DailyCoverage(DAY)
    coverage.observe(pl.concat([bars(SYMBOLS[:1]), bars(SYMBOLS[1:]).with_columns(
        pl.lit(0.0).alias("open"), pl.lit(0.0).alias("high"),
    )]))
    assert coverage.require(SYMBOLS)["halted"] == 1
    assert coverage.active == {SYMBOLS[0]}
    with pytest.raises(DailyCoverageError):
        coverage.require([*SYMBOLS, "600001.SH"])
