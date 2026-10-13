"""自定义财务采集在实际批次返回后推进任务并响应现有取消。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest

from app.data_providers.custom.config import CustomSourceConfig, DatasetConfig
from app.data_providers.custom.provider import GenericHTTPProvider
from app.services import financial_sync, pipeline_jobs, preferences
from app.services.pipeline_jobs import JobCancelledError, JobStore
from app.tickflow.capabilities import CapabilitySet


@pytest.fixture
def custom(monkeypatch):
    from app.data_providers import custom as sources

    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: True)
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "test")
    def install(provider):
        monkeypatch.setattr(sources, "get_provider", lambda name: provider)
    return install


def test_custom_batches_report_real_progress_and_merge_sparse_columns(custom):
    symbols = [f"{600000 + i}.SH" for i in range(205)]
    calls = []
    progress = []
    def fetch(table, chunk, latest_only):
        calls.append((table, list(chunk), latest_only))
        frame = pl.DataFrame({"symbol": chunk, "period_end": ["2026-06-30"] * len(chunk)})
        if len(calls) == 2:
            frame = frame.with_columns(pl.lit(12.5).alias("roe"))
        return frame
    custom(SimpleNamespace(get_financials=fetch))

    result = financial_sync._fetch_table("metrics", symbols, CapabilitySet(), latest_only=False,
                                         on_progress=lambda done, total: progress.append((done, total, len(calls))))

    assert [len(call[1]) for call in calls] == [100, 100, 5]
    assert all(call[0] == "metrics" and call[2] is False for call in calls)
    assert result["symbol"].to_list() == symbols
    assert result["roe"].drop_nulls().to_list() == [12.5] * 100
    assert (1, 3, 1) in progress and (2, 3, 2) in progress
    assert progress[-1] == (3, 3, 3)


def test_financial_batch_progress_prevents_false_stall_and_cancel_stops_next_batch(custom, tmp_path, monkeypatch):
    store = JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    job_id, _ = store.create(timeout_s=1200)
    store.start(job_id)
    calls = []
    def fetch(table, symbols, latest_only):
        calls.append(list(symbols))
        store._active_jobs[job_id]["last_progress_at"] = (datetime.now(UTC) - timedelta(minutes=21)).isoformat()
        return pl.DataFrame({"symbol": symbols})
    custom(SimpleNamespace(get_financials=fetch))
    def progress(done, total):
        store.progress(job_id, "financials", int(done * 100 / total), f"{done}/{total}")
        if done == 1:
            store.reap_stale()
            assert store.get(job_id)["status"] == "running"
            store.terminate(job_id, "用户取消")

    with pytest.raises(JobCancelledError):
        financial_sync._fetch_table("income", [str(i) for i in range(205)], CapabilitySet(), on_progress=progress)
    assert [len(chunk) for chunk in calls] == [100]
    assert store.get(job_id)["status"] == "failed"


def test_cancelled_financial_batch_keeps_previous_table(custom, tmp_path):
    path = tmp_path / "financials" / "income" / "part.parquet"
    path.parent.mkdir(parents=True)
    old = pl.DataFrame({"symbol": ["old"], "period_end": ["2025-12-31"], "net_income": [1.]})
    old.write_parquet(path)
    fetch = Mock(side_effect=lambda table, symbols, latest_only: pl.DataFrame({
        "symbol": symbols, "period_end": ["2026-06-30"] * len(symbols), "net_income": [2.] * len(symbols),
    }))
    custom(SimpleNamespace(get_financials=fetch))
    def progress(done, total):
        if done == 1:
            raise JobCancelledError("test")
    with pytest.raises(JobCancelledError):
        financial_sync._sync_history_table_for_symbols("income", [str(i) for i in range(205)],
                                                       tmp_path, CapabilitySet(), on_progress=progress)
    assert fetch.call_count == 1
    assert pl.read_parquet(path).equals(old)


def test_empty_custom_table_stays_empty(custom):
    fetch = Mock(return_value=pl.DataFrame())
    custom(SimpleNamespace(get_financials=fetch))
    assert financial_sync._fetch_table("shares", [str(i) for i in range(205)], CapabilitySet()).is_empty()
    assert [len(call.args[1]) for call in fetch.call_args_list] == [100, 100, 5]


def test_failed_batch_does_not_overwrite_table_with_partial_results(custom, tmp_path):
    path = tmp_path / "financials" / "income" / "part.parquet"
    path.parent.mkdir(parents=True)
    old = pl.DataFrame({"symbol": ["old"], "net_income": [1.]})
    old.write_parquet(path)
    fetch = Mock(side_effect=[pl.DataFrame({"symbol": ["new"]}), RuntimeError("batch failed")])
    custom(SimpleNamespace(get_financials=fetch))
    with pytest.raises(RuntimeError, match="batch failed"):
        financial_sync._sync_table("income", [str(i) for i in range(205)], tmp_path, CapabilitySet())
    assert fetch.call_count == 2
    assert pl.read_parquet(path).equals(old)


@pytest.mark.parametrize("batch,expected", [(60, [60, 60, 60, 25]), (200, [200, 5]), (None, [205])])
def test_generic_http_keeps_configured_batches_without_duplicate_rpm_reservations(custom, monkeypatch, batch, expected):
    from app.data_providers.custom import provider as http_provider

    dataset = DatasetConfig(url="http://test/financial", batch=batch, rpm=120, field_map={"symbol": "symbol"})
    provider = GenericHTTPProvider(CustomSourceConfig("test", "test", datasets={"financial": dataset}))
    custom(provider)
    sizes, reservations, progress = [], [], []
    def request_rows(cfg, *, symbols, **kwargs):
        sizes.append(len(symbols))
        return [{"symbol": symbol} for symbol in symbols]
    monkeypatch.setattr(provider, "_request_rows", request_rows)
    monkeypatch.setattr(http_provider, "sleep_between_batches", lambda index, rpm: reservations.append((index, rpm)))
    try:
        result = financial_sync._fetch_table("income", [str(i) for i in range(205)], CapabilitySet(),
                                             on_progress=lambda done, total: progress.append((done, total)))
    finally:
        provider.close()
    assert result.height == 205
    assert sizes == expected
    assert reservations == [(index, 120) for index in range(len(expected))]
    assert progress[-1] == (len(expected), len(expected))


def test_generic_cancel_after_first_batch_stops_next_request(custom, monkeypatch):
    dataset = DatasetConfig(url="http://test/financial", batch=60, field_map={"symbol": "symbol"})
    provider = GenericHTTPProvider(CustomSourceConfig("test", "test", datasets={"financial": dataset}))
    custom(provider)
    requests = []
    def request_rows(cfg, *, symbols, **kwargs):
        requests.append(list(symbols))
        return [{"symbol": symbol} for symbol in symbols]
    monkeypatch.setattr(provider, "_request_rows", request_rows)
    def progress(done, total):
        if done == 1:
            raise JobCancelledError("test")
    try:
        with pytest.raises(JobCancelledError):
            financial_sync._fetch_table("income", [str(i) for i in range(125)], CapabilitySet(), on_progress=progress)
    finally:
        provider.close()
    assert [len(chunk) for chunk in requests] == [60]


def test_generic_progress_preserves_existing_shared_future_slot(custom, monkeypatch):
    from app.tickflow import rate_limits

    dataset = DatasetConfig(url="http://test/financial", batch=60, rpm=120, field_map={"symbol": "symbol"})
    provider = GenericHTTPProvider(CustomSourceConfig("test", "test", datasets={"financial": dataset}))
    custom(provider)
    clock = SimpleNamespace(now=0.)
    requests = []
    def sleep(seconds):
        clock.now += seconds
    monkeypatch.setattr(rate_limits, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    monkeypatch.setattr(rate_limits, "_next_slot", {120: 10.})
    def request_rows(cfg, *, symbols, **kwargs):
        requests.append(clock.now)
        return [{"symbol": symbol} for symbol in symbols]
    monkeypatch.setattr(provider, "_request_rows", request_rows)
    try:
        assert financial_sync._fetch_table("income", [str(i) for i in range(125)], CapabilitySet()).height == 125
    finally:
        provider.close()
    # 首批沿现有不等待契约；之后必须等其他同 rpm 调用已预约的未来槽。
    assert requests == [0., 10.5, 11.]
