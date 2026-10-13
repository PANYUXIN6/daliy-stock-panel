from __future__ import annotations

import asyncio
import json
import os
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market_time import CN_TZ
from app.services import collection_status, preferences
from app.services.collection_status import (
    CollectionSyncRequest,
    check_collection_sync,
    collection_context,
)


@pytest.fixture
def local(monkeypatch, tmp_path):
    now = datetime(2026, 10, 9, 16, tzinfo=CN_TZ)
    monkeypatch.setattr(collection_status, "cn_now", lambda: now)
    monkeypatch.setattr(preferences, "load", lambda: {})
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                           earliest_daily_date=lambda: date(2026, 1, 1),
                           resolve_asset_type=lambda symbol: "stock")
    calendar = tmp_path / "calendar" / "trading_days.json"
    calendar.parent.mkdir()
    calendar.write_text(json.dumps({"start": "2026-10-08", "end": "2026-10-12", "days": ["2026-10-08", "2026-10-09", "2026-10-12"]}))
    return repo


def write_rows(repo, relative, *, symbol="600519.SH", rows=1):
    path = repo.store.data_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    pl.DataFrame({"symbol": [symbol] * rows}).write_parquet(path)
    return path


def completed(repo, scope, result, *, started_at="2026-10-09T07:10:00+00:00", status="succeeded", context=True):
    if context:
        result = {**result, "collection_context": collection_context(scope.request_key(), repo)}
    job = {"request_key": scope.request_key(), "status": status, "started_at": started_at,
           "finished_at": "2026-10-09T07:30:00+00:00", "result": result}
    return SimpleNamespace(list_recent=lambda **kwargs: [job])


def test_same_closed_scope_prompts_without_altering_data_or_jobs(local):
    scope = CollectionSyncRequest(kind="pipeline")
    files = [write_rows(local, f"{table}/date=2026-10-09/part.parquet") for table in ("kline_daily", "kline_index_daily")]
    store = completed(local, scope, {"daily_days": 1})
    before = [file.read_bytes() for file in files]
    result = check_collection_sync(scope, local, job_store=store)
    assert result["requires_confirmation"]
    assert "收盘后完成一次全量同步" in result["message"]
    assert result["last_synced_at"] == "2026-10-09T07:30:00+00:00"
    assert before == [file.read_bytes() for file in files]
    assert store.list_recent()[0]["status"] == "succeeded"


@pytest.mark.parametrize(
    ("kind", "result_field", "unit"),
    [("minute", "rows", "5 个交易日"), ("minute_batch", "minute_rows", "首次回溯 5 个自然日")],
)
def test_minute_confirmation_describes_actual_window_units(local, kind, result_field, unit):
    scope = CollectionSyncRequest(kind=kind, symbol="600519.SH", days=5)
    path = write_rows(local, "kline_minute/date=2026-10-09/part.parquet")
    before = path.read_bytes()
    result = check_collection_sync(
        scope, local, job_store=completed(local, scope, {result_field: 1}),
    )

    assert result["requires_confirmation"] is True
    assert unit in result["message"]
    if kind == "minute_batch":
        assert "已有数据从最新时间增量更新" in result["message"]
    assert path.read_bytes() == before


def test_pipeline_disabled_optional_steps_are_not_failures(local, monkeypatch):
    scope = CollectionSyncRequest(kind="pipeline")
    write_rows(local, "kline_daily/date=2026-10-09/part.parquet")
    write_rows(local, "kline_index_daily/date=2026-10-09/part.parquet")
    # 默认管道成功结果也包含这些跳过；不能令正常全量更新永远不提示重复。
    result = {"daily_days": 1, "skipped_stages": ["sync_minute", "regime", "mainline"], "stage_errors": []}
    assert check_collection_sync(scope, local, job_store=completed(local, scope, result))["requires_confirmation"]
    monkeypatch.setattr(preferences, "load", lambda: {"pipeline_pull_index": False})
    result["skipped_stages"].append("sync_index")
    assert check_collection_sync(scope, local, job_store=completed(local, scope, result))["requires_confirmation"]
    monkeypatch.setattr(preferences, "load", lambda: {"minute_sync_enabled": True})
    assert not check_collection_sync(scope, local, job_store=completed(local, scope, result))["requires_confirmation"]


@pytest.mark.parametrize("condition", ["failed", "old_record", "source_changed", "scope_changed", "missing_partition", "skipped", "zero_rows", "before_close"])
def test_latest_date_alone_does_not_prove_repeat(local, monkeypatch, condition):
    scope = CollectionSyncRequest(kind="pipeline")
    write_rows(local, "kline_daily/date=2026-10-09/part.parquet", rows=0 if condition == "zero_rows" else 1)
    if condition != "missing_partition":
        write_rows(local, "kline_index_daily/date=2026-10-09/part.parquet")
    store = completed(local, scope, {"skipped_stages": ["sync_adj"]} if condition == "skipped" else {},
                      status="failed" if condition == "failed" else "succeeded",
                      context=condition != "old_record",
                      started_at="2026-10-09T04:00:00+00:00" if condition == "before_close" else "2026-10-09T07:10:00+00:00")
    if condition == "source_changed":
        monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "fuyao")
    elif condition == "scope_changed":
        monkeypatch.setattr(preferences, "load", lambda: {"pipeline_pull_etf": True})
    assert not check_collection_sync(scope, local, job_store=store)["requires_confirmation"]


@pytest.mark.parametrize("now", [datetime(2026, 10, 9, 10, tzinfo=CN_TZ), datetime(2026, 10, 9, 12, tzinfo=CN_TZ), datetime(2026, 10, 13, 16, tzinfo=CN_TZ)])
def test_market_changing_or_calendar_unknown_has_no_repeat_claim(local, monkeypatch, now):
    monkeypatch.setattr(collection_status, "cn_now", lambda: now)
    scope = CollectionSyncRequest(kind="index_daily", days=365)
    write_rows(local, "kline_index_daily/date=2026-10-09/part.parquet")
    store = completed(local, scope, {"rows_written": 10})
    assert not check_collection_sync(scope, local, job_store=store)["requires_confirmation"]


def test_weekend_compares_last_real_session_and_bigger_range_is_new(local, monkeypatch):
    monkeypatch.setattr(collection_status, "cn_now", lambda: datetime(2026, 10, 11, 16, tzinfo=CN_TZ))
    scope = CollectionSyncRequest(kind="index_daily", days=365)
    write_rows(local, "kline_index_daily/date=2026-10-09/part.parquet")
    store = completed(local, scope, {"rows_written": 10})
    assert check_collection_sync(scope, local, job_store=store)["requires_confirmation"]
    assert not check_collection_sync(CollectionSyncRequest(kind="index_daily", days=400), local, job_store=store)["requires_confirmation"]


def test_financial_positive_result_and_selected_tables_are_required(local):
    scope = CollectionSyncRequest(kind="financial", table="income")
    write_rows(local, "financials/income/part.parquet")
    result = check_collection_sync(scope, local, job_store=completed(local, scope, {"income": 1}))
    assert result["requires_confirmation"]
    assert "已完成过同源同步任务，本地已有结果" in result["message"]
    assert not check_collection_sync(scope, local, job_store=completed(local, scope, {"income": 0}))["requires_confirmation"]
    all_scope = CollectionSyncRequest(kind="financial", table="all")
    assert not check_collection_sync(all_scope, local, job_store=completed(local, all_scope, {"income": 1}))["requires_confirmation"]


@pytest.mark.parametrize("unavailable", ["zero_result", "zero_result_existing_file", "missing_file", "empty_file"])
def test_financial_all_repeat_describes_currently_available_tables(local, unavailable):
    scope = CollectionSyncRequest(kind="financial", table="all")
    result = dict.fromkeys(collection_status.FINANCIAL_TABLES, 1)
    for table in collection_status.FINANCIAL_TABLES:
        if table == "shares":
            if unavailable.startswith("zero_result"):
                result[table] = 0
            if unavailable in {"zero_result", "missing_file"}:
                continue
        write_rows(local, f"financials/{table}/part.parquet",
                   rows=0 if table == "shares" and unavailable == "empty_file" else 1)
    response = check_collection_sync(scope, local, job_store=completed(local, scope, result))
    assert response["requires_confirmation"]
    assert "今天已执行过同源同步任务" in response["message"]
    assert "当前有本地可用结果：核心指标、利润表、资产负债表、现金流量表" in response["message"]
    assert "本次任务未确认有可用结果：历史股本" in response["message"]
    assert "财务公告仍可能新增或修订" in response["message"]


def test_financial_all_complete_results_keep_existing_message(local):
    scope = CollectionSyncRequest(kind="financial", table="all")
    result = dict.fromkeys(collection_status.FINANCIAL_TABLES, 1)
    for table in collection_status.FINANCIAL_TABLES:
        write_rows(local, f"financials/{table}/part.parquet")
    response = check_collection_sync(scope, local, job_store=completed(local, scope, result))
    assert response["requires_confirmation"]
    assert response["message"] == "所选财务表今天已完成过同源同步任务，本地已有结果。财务公告仍可能新增或修订，是否重新拉取？"


@pytest.mark.parametrize("evidence", ["zero_results", "missing_files", "incomplete_record"])
def test_financial_all_needs_complete_record_and_some_available_result(local, evidence):
    scope = CollectionSyncRequest(kind="financial", table="all")
    result = dict.fromkeys(collection_status.FINANCIAL_TABLES, 0 if evidence == "zero_results" else 1)
    if evidence == "incomplete_record":
        result.pop("shares")
    if evidence != "missing_files":
        for table in collection_status.FINANCIAL_TABLES:
            write_rows(local, f"financials/{table}/part.parquet")
    assert not check_collection_sync(scope, local, job_store=completed(local, scope, result))["requires_confirmation"]


def test_minute_other_symbol_does_not_count_as_requested_coverage(local):
    scope = CollectionSyncRequest(kind="minute", symbol="600519.SH", days=5)
    store = completed(local, scope, {"rows": 10})
    write_rows(local, "kline_minute/date=2026-10-09/part.parquet", symbol="000001.SZ")
    assert not check_collection_sync(scope, local, job_store=store)["requires_confirmation"]
    write_rows(local, "kline_minute/date=2026-10-09/part.parquet")
    assert check_collection_sync(scope, local, job_store=store)["requires_confirmation"]


def test_adj_event_date_is_not_used_as_sync_date(local):
    scope = CollectionSyncRequest(kind="adj_factor")
    write_rows(local, "adj_factor/part.parquet")
    assert check_collection_sync(scope, local, job_store=completed(local, scope, {"adj_written": 2}))["requires_confirmation"]
    assert not check_collection_sync(scope, local, job_store=completed(local, scope, {"adj_written": 0}))["requires_confirmation"]


def test_quote_requires_actual_close_confirmation(local):
    scope = CollectionSyncRequest(kind="quotes")
    fetched_at = datetime(2026, 10, 9, 15, 2, tzinfo=CN_TZ)
    service = SimpleNamespace(status=lambda: {"final_sync_done": True, "last_fetch_ms": fetched_at.timestamp() * 1000})
    assert check_collection_sync(scope, local, quote_service=service)["requires_confirmation"]
    service.status = lambda: {"final_sync_done": False, "last_fetch_ms": fetched_at.timestamp() * 1000}
    assert not check_collection_sync(scope, local, quote_service=service)["requires_confirmation"]
    # 截止后 done 也可表示已放弃；失败原因仍在时不能误称收盘已确认。
    service.status = lambda: {"final_sync_done": True, "final_sync_failed": "unconfirmed_snapshot", "last_fetch_ms": fetched_at.timestamp() * 1000}
    assert not check_collection_sync(scope, local, quote_service=service)["requires_confirmation"]


def test_depth_message_does_not_claim_intraday_snapshot_is_final(local):
    write_rows(local, "depth5/date=2026-10-09/part.parquet")
    result = check_collection_sync(CollectionSyncRequest(kind="depth"), local)
    assert result["requires_confirmation"]
    assert "快照可能来自盘中" in result["message"]


def test_api_validates_scope_and_ext_table_without_collecting(local, monkeypatch):
    from app.api.data import router
    from app.services import pipeline_jobs
    app = FastAPI()
    app.state.repo = local
    app.include_router(router)
    monkeypatch.setattr(pipeline_jobs, "job_store", SimpleNamespace(list_recent=lambda **kwargs: []))
    with TestClient(app) as client:
        assert client.post("/api/data/sync-check", json={"kind": "pipeline"}).json()["requires_confirmation"] is False
        assert client.post("/api/data/sync-check", json={"kind": "financial", "table": "invalid"}).status_code == 422
        assert client.post("/api/data/sync-check", json={"kind": "minute", "symbol": "600519.SH", "days": 31}).status_code == 422
        assert client.post("/api/data/sync-check", json={"kind": "minute_batch", "days": 31}).status_code == 422
        assert client.post("/api/data/sync-check", json={"kind": "minute_batch", "days": 5}).json()["requires_confirmation"] is False
        assert client.post("/api/data/sync-check", json={"kind": "classifications"}).json()["requires_confirmation"] is False
        assert client.post("/api/data/sync-check", json={"kind": "ext", "config_id": "missing"}).status_code == 400
        assert client.post("/api/data/sync-check", json={"kind": "ext", "config_id": "../outside"}).status_code == 400


def test_ext_prompt_describes_saved_data_without_upstream_freshness(local):
    from app.services.ext_data import ExtConfig, ExtConfigStore
    ExtConfigStore(local.store.data_dir).upsert(ExtConfig(id="example", label="分类样本", mode="snapshot", fields=[]))
    path = write_rows(local, "ext_data/example/part.parquet")
    import os
    saved_at = datetime(2026, 10, 9, 14, tzinfo=CN_TZ).timestamp()
    os.utime(path, (saved_at, saved_at))
    result = check_collection_sync(CollectionSyncRequest(kind="ext", config_id="example"), local)
    assert result["requires_confirmation"]
    assert "数据源可能修订" in result["message"]


@pytest.mark.parametrize("condition", ["both", "missing", "empty", "yesterday"])
def test_classifications_need_both_tables_saved_today(local, monkeypatch, condition):
    scope = CollectionSyncRequest(kind="classifications")
    now = datetime(2026, 10, 9, 14, tzinfo=CN_TZ).timestamp()
    for config_id in ("ext_gn_ths", "ext_hy_ths"):
        if condition == "missing" and config_id == "ext_hy_ths":
            continue
        path = write_rows(local, f"ext_data/{config_id}/part.parquet",
                          rows=0 if condition == "empty" and config_id == "ext_hy_ths" else 1)
        timestamp = now - 86400 if condition == "yesterday" and config_id == "ext_hy_ths" else now
        os.utime(path, (timestamp, timestamp))
    monkeypatch.setattr("socket.socket.connect", lambda *args: pytest.fail("预检查不得出站"))
    result = check_collection_sync(scope, local)
    assert result["requires_confirmation"] is (condition == "both")
    if condition == "both":
        assert "概念与行业" in result["message"] and "可能修订" in result["message"]
        assert "最新" not in result["message"]


@pytest.mark.parametrize("condition", ["done", "failed", "missing", "zero", "source_changed", "before_close"])
def test_minute_batch_repeat_uses_completed_same_scope_and_local_results(local, monkeypatch, condition):
    scope = CollectionSyncRequest(kind="minute_batch", days=5)
    if condition != "missing":
        write_rows(local, "kline_minute/date=2026-10-09/part.parquet")
    store = completed(local, scope, {"minute_rows": 0 if condition == "zero" else 10},
                      status="failed" if condition == "failed" else "succeeded",
                      started_at="2026-10-09T04:00:00+00:00" if condition == "before_close" else "2026-10-09T07:10:00+00:00")
    if condition == "source_changed":
        monkeypatch.setattr(preferences, "get_minute_data_provider", lambda: "fuyao")
    monkeypatch.setattr("socket.socket.connect", lambda *args: pytest.fail("预检查不得出站"))
    result = check_collection_sync(scope, local, job_store=store)
    assert result["requires_confirmation"] is (condition == "done")
    if condition == "done":
        assert "同源同范围" in result["message"] and "本地已有结果" in result["message"]
        assert "全部标的" not in result["message"]
        assert not check_collection_sync(CollectionSyncRequest(kind="minute_batch", days=10), local, job_store=store)["requires_confirmation"]


@pytest.mark.parametrize("days", [None, 0, 31])
def test_minute_batch_check_rejects_unbounded_scope(days):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        CollectionSyncRequest(kind="minute_batch", days=days)


@pytest.mark.parametrize("changed", [False, True])
def test_actual_completed_task_records_scope_only_when_stable(local, monkeypatch, changed):
    from app.api import pipeline
    from app.services import pipeline_jobs
    from app.services.pipeline_jobs import JobStore

    store = JobStore(store_dir=local.store.data_dir / "jobs")
    class TaskClock(datetime):
        @classmethod
        def utcnow(cls):
            return cls(2026, 10, 9, 7, 10)
    monkeypatch.setattr(pipeline_jobs, "datetime", TaskClock)
    monkeypatch.setattr(pipeline, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    local.refresh_cache = lambda: None
    monkeypatch.setattr(pipeline.daily_pipeline, "run_weekly_mining_after_sync", lambda: None)
    scope = CollectionSyncRequest(kind="pipeline")
    initial_context = collection_context(scope.request_key(), local)

    def run():
        if changed:
            monkeypatch.setattr(preferences, "load", lambda: {"pipeline_pull_etf": True})
        return {"daily_days": 1}

    async def submit():
        result = await pipeline._submit_job(local, None, lambda progress: run, request_key="pipeline")
        await asyncio.gather(*pipeline._tasks)
        return result["job_id"]

    job_id = asyncio.run(submit())
    job = store.get(job_id)
    assert job["status"] == "succeeded"
    assert job["result"]["daily_days"] == 1
    if changed:
        assert "collection_context" not in job["result"]
    else:
        assert job["result"]["collection_context"] == initial_context
        restored = JobStore(store_dir=local.store.data_dir / "jobs")
        assert restored.get(job_id)["result"]["collection_context"] == initial_context
        for table in ("kline_daily", "kline_index_daily"):
            write_rows(local, f"{table}/date=2026-10-09/part.parquet")
        # 真实 list_recent 是摘要投影，而非 get 的完整记录；预检查必须走通它。
        assert check_collection_sync(scope, local, job_store=restored)["requires_confirmation"]


@pytest.mark.parametrize("kind", ["financial", "minute_batch"])
@pytest.mark.parametrize("legacy", [False, True], ids=["new_records", "legacy_records"])
@pytest.mark.parametrize("retry_succeeded", [True, False], ids=["failed_then_succeeded", "succeeded_then_failed"])
def test_same_second_retry_uses_latest_task_for_confirmation(local, monkeypatch, kind, legacy, retry_succeeded):
    from pathlib import Path

    from app.services import pipeline_jobs
    from app.services.pipeline_jobs import JobStore

    class SameSecond(datetime):
        @classmethod
        def utcnow(cls):
            return cls(2026, 10, 9, 7, 10)

    monkeypatch.setattr(pipeline_jobs, "datetime", SameSecond)
    ticks = iter([1791529800100000000, 1791529800200000000])
    monkeypatch.setattr(pipeline_jobs, "time_ns", lambda: next(ticks), raising=False)
    identifiers = iter(["0000000001", "0000000002"])
    monkeypatch.setattr(pipeline_jobs.uuid, "uuid4", lambda: SimpleNamespace(hex=next(identifiers)))
    directory = local.store.data_dir / "jobs"
    scope = CollectionSyncRequest(kind=kind, **({"table": "income"} if kind == "financial" else {"days": 5}))
    if kind == "financial":
        path = write_rows(local, "financials/income/part.parquet")
        result = {"income": 1}
    else:
        path = write_rows(local, "kline_minute/date=2026-10-09/part.parquet")
        result = {"minute_rows": 1}
    result["collection_context"] = collection_context(scope.request_key(), local)
    store = JobStore(store_dir=directory)
    ids = []
    for succeeded in [not retry_succeeded, retry_succeeded]:
        jid, _ = store.create(timeout_s=60, request_key=scope.request_key())
        store.start(jid)
        if succeeded:
            store.succeed(jid, result)
        else:
            store.fail(jid, "网络失败")
        ids.append(jid)
    if legacy:
        for index, jid in enumerate(ids):
            record = directory / f"{jid}.json"
            data = json.loads(record.read_text())
            data.pop("created_at_ns", None)
            # 兼容旧秒精度Z及等价UTC偏移格式，不按字符串字典序比较。
            data["started_at"] = "2026-10-09T07:10:00" + ("Z" if index == 0 else "+00:00")
            record.write_text(json.dumps(data))
            stamp = 1791529800100000000 + index * 100000000
            os.utime(record, ns=(stamp, stamp))
    original_glob = Path.glob
    monkeypatch.setattr(Path, "glob", lambda self, pattern: (
        iter(sorted(original_glob(self, pattern))) if self == directory else original_glob(self, pattern)
    ))
    original_rows = path.read_bytes()
    original_jobs = [(directory / f"{jid}.json").read_bytes() for jid in ids]
    for reader in [store, JobStore(store_dir=directory, recover=False)]:
        assert reader.list_recent(limit=1)[0]["id"] == ids[1]
        confirmation = check_collection_sync(scope, local, job_store=reader)
        assert confirmation["requires_confirmation"] is retry_succeeded
    assert path.read_bytes() == original_rows
    assert [(directory / f"{jid}.json").read_bytes() for jid in ids] == original_jobs
