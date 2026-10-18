"""候选股利润表局部更新保留版本事实，不能伪装成全市场完成。"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest

from app.data_providers import custom
from app.data_providers.base import ProviderRateLimitError
from app.services import financial_sync, preferences, shared_runtime
from app.services.financial_versions import read_versions, record_versions, version_path
from app.services.pipeline_jobs import JobCancelledError
from app.tickflow.capabilities import CapabilitySet


def income_rows(symbol, *, period="2026-09-30", updated="2026-10-16", profit=100.):
    return pl.DataFrame([{
        "symbol": symbol, "period_end": period, "source_update_date": updated,
        "announce_date": updated, "date_basis": "source_update", "net_income": profit,
    }])


class ObservationTime(datetime):
    @classmethod
    def now(cls, tz=None):
        instant = datetime(2026, 10, 18, 2, tzinfo=UTC)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


@pytest.fixture
def install(monkeypatch):
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "fuyao")
    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: True)
    monkeypatch.setattr(financial_sync, "datetime", ObservationTime)
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: None)
    monkeypatch.setattr(financial_sync, "_get_symbols", Mock(side_effect=AssertionError("不能扫描全市场")))
    monkeypatch.setattr(preferences, "set_financial_sync_time", Mock(side_effect=AssertionError("不能标记全量完成")))

    def apply(fetch):
        provider = SimpleNamespace(
            get_financials=fetch, financial_sync_batch_size=20, financial_history_periods=20,
        )
        monkeypatch.setattr(custom, "get_provider", lambda source: provider)
    return apply


def save_existing(data_dir):
    baseline = pl.concat([
        income_rows("600000.SH", period="2026-06-30", updated="2026-08-15", profit=80.),
        income_rows("000001.SZ", period="2026-06-30", updated="2026-08-15", profit=50.),
    ])
    captured = record_versions(data_dir, "income", baseline, "fuyao",
                               datetime(2026, 10, 1, tzinfo=UTC))
    browse = data_dir / "financials/income/part.parquet"
    browse.parent.mkdir(parents=True)
    captured.write_parquet(browse)
    return browse


def test_candidate_only_merge_preserves_other_stocks_checkpoint_and_observation_rules(tmp_path, install):
    browse = save_existing(tmp_path)
    original_other = pl.read_parquet(browse).filter(pl.col("symbol") == "000001.SZ")
    checkpoint = tmp_path / "financials/sync_progress/income.json"
    checkpoint.parent.mkdir()
    checkpoint.write_text(json.dumps({"status": "running", "symbols": ["600000.SH", "000001.SZ"],
                                      "completed_symbols": ["000001.SZ"]}))
    checkpoint_before = checkpoint.read_bytes()
    calls, progress = [], []
    cancel = Event()

    def fetch(table, symbols, latest_only, *, cancel_event=None):
        calls.append((table, list(symbols), latest_only, cancel_event))
        return pl.concat([
            income_rows(symbols[0]),
            income_rows(symbols[0], period="2025-09-30", updated="2025-10-20", profit=50.),
        ])
    install(fetch)
    result = financial_sync.sync_candidate_income(
        [" 600000.SH ", "", "600000.SH", "600001.SH"], tmp_path, CapabilitySet(),
        lambda done, total: progress.append((done, total)), cancel_event=cancel,
    )

    assert result == {"requested": 2, "updated_symbols": ["600000.SH", "600001.SH"], "errors": {}}
    assert calls == [("income", [symbol], False, cancel) for symbol in ["600000.SH", "600001.SH"]]
    assert progress[0] == (0, 2) and progress[-1] == (2, 2)
    assert checkpoint.read_bytes() == checkpoint_before
    assert pl.read_parquet(browse).filter(pl.col("symbol") == "000001.SZ").equals(original_other)
    history = read_versions(tmp_path, "income")
    observed = history.filter((pl.col("symbol") == "600000.SH") & (pl.col("period_end") == "2026-09-30"))
    assert observed["available_date"].item() == "2026-10-18"
    assert observed["observed_at"].item() == "2026-10-18T02:00:00+00:00"
    bootstrap = history.filter((pl.col("symbol") == "600001.SH") & (pl.col("period_end") == "2026-09-30"))
    assert bootstrap["available_date"].item() == "2026-10-16"
    assert not (tmp_path / "financials/universe.json").exists()
    assert preferences.set_financial_sync_time.call_count == 0


def test_missing_and_failed_candidates_do_not_stop_other_symbols(tmp_path, install):
    calls = []

    def fetch(table, symbols, latest_only):
        calls.extend(symbols)
        if symbols == ["600000.SH"]:
            raise RuntimeError("供应商暂不可用")
        if symbols == ["600001.SH"]:
            return pl.DataFrame()
        return income_rows(symbols[0])
    install(fetch)
    result = financial_sync.sync_candidate_income(
        ["600000.SH", "600001.SH", "600002.SH"], tmp_path, CapabilitySet(),
    )
    assert calls == ["600000.SH", "600001.SH", "600002.SH"]
    assert result["requested"] == 3 and result["updated_symbols"] == ["600002.SH"]
    assert set(result["errors"]) == {"600000.SH", "600001.SH"}
    assert "供应商暂不可用" in result["errors"]["600000.SH"]
    assert read_versions(tmp_path, "income")["symbol"].to_list() == ["600002.SH"]
    assert not (tmp_path / "financials/sync_progress/income.json").exists()


def test_out_of_scope_provider_row_is_rejected_before_any_write(tmp_path, install):
    browse = save_existing(tmp_path)
    before_browse, before_versions = browse.read_bytes(), version_path(tmp_path, "income").read_bytes()
    install(lambda table, symbols, latest_only: income_rows("600999.SH"))
    result = financial_sync.sync_candidate_income(["600000.SH"], tmp_path, CapabilitySet())
    assert result["updated_symbols"] == [] and set(result["errors"]) == {"600000.SH"}
    assert browse.read_bytes() == before_browse
    assert version_path(tmp_path, "income").read_bytes() == before_versions


def test_rejected_session_check_before_write_preserves_financials_and_full_checkpoint(tmp_path, install):
    browse = save_existing(tmp_path)
    versions = version_path(tmp_path, "income")
    checkpoint = tmp_path / "financials/sync_progress/income.json"
    checkpoint.parent.mkdir()
    checkpoint.write_text('{"status":"running","symbols":["000001.SZ","600000.SH"]}')
    before = {path: path.read_bytes() for path in [browse, versions, checkpoint]}
    install(lambda table, symbols, latest_only: income_rows(symbols[0]))
    before_write = Mock(side_effect=PermissionError("管理员会话已失效"))

    with pytest.raises(PermissionError, match="管理员会话已失效"):
        financial_sync.sync_candidate_income(
            ["600000.SH"], tmp_path, CapabilitySet(), before_write=before_write,
        )
    before_write.assert_called_once_with()
    assert all(path.read_bytes() == content for path, content in before.items())


def test_session_is_checked_before_both_version_and_browse_publication(tmp_path, install):
    install(lambda table, symbols, latest_only: income_rows(symbols[0]))
    states = []

    def before_write():
        states.append((version_path(tmp_path, "income").exists(),
                       (tmp_path / "financials/income/part.parquet").exists()))

    result = financial_sync.sync_candidate_income(
        ["600000.SH"], tmp_path, CapabilitySet(), before_write=before_write,
    )
    assert result["updated_symbols"] == ["600000.SH"]
    assert states == [(False, False), (True, False)]


@pytest.mark.parametrize("broken", ["browse", "versions"])
def test_corrupt_existing_financial_data_fails_closed_before_fetch(tmp_path, install, broken):
    browse = save_existing(tmp_path)
    versions = version_path(tmp_path, "income")
    (browse if broken == "browse" else versions).write_bytes(b"broken parquet")
    before = {path: path.read_bytes() for path in [browse, versions]}
    fetch = Mock(return_value=income_rows("600000.SH"))
    install(fetch)
    with pytest.raises(pl.exceptions.PolarsError):
        financial_sync.sync_candidate_income(["600000.SH"], tmp_path, CapabilitySet())
    fetch.assert_not_called()
    assert all(path.read_bytes() == content for path, content in before.items())


def test_rate_limit_cools_down_and_retries_same_candidate_without_losing_next(tmp_path, install, monkeypatch):
    calls, waits, progress = [], [], []
    cancel = Event()

    def fetch(table, symbols, latest_only, *, cancel_event=None):
        calls.extend(symbols)
        if len(calls) == 1:
            raise ProviderRateLimitError("limited")
        return income_rows(symbols[0])

    def wait(attempt, on_wait, cancel_event=None):
        waits.append((attempt, cancel_event))
        on_wait(600)
        return True
    install(fetch)
    monkeypatch.setattr(financial_sync, "wait_financial_rate_limit", wait)
    result = financial_sync.sync_candidate_income(
        ["600000.SH", "600001.SH"], tmp_path, CapabilitySet(),
        lambda done, total: progress.append((done, total)), cancel_event=cancel,
    )
    assert calls == ["600000.SH", "600000.SH", "600001.SH"]
    assert waits == [(1, cancel)]
    assert result["updated_symbols"] == ["600000.SH", "600001.SH"] and result["errors"] == {}
    assert progress[-1] == (2, 2) and all(0 <= done <= total == 2 for done, total in progress)


def test_cancel_after_saved_candidate_keeps_it_and_stops_next_request(tmp_path, install):
    calls = []
    cancel = Event()

    def fetch(table, symbols, latest_only, *, cancel_event=None):
        calls.extend(symbols)
        return income_rows(symbols[0])

    def progress(done, total):
        if done == 1:
            cancel.set()
    install(fetch)
    with pytest.raises(JobCancelledError):
        financial_sync.sync_candidate_income(
            ["600000.SH", "600001.SH"], tmp_path, CapabilitySet(), progress, cancel_event=cancel,
        )
    assert calls == ["600000.SH"]
    assert read_versions(tmp_path, "income")["symbol"].to_list() == ["600000.SH"]


def test_empty_candidates_skip_fetch_and_files(tmp_path, install):
    fetch = Mock()
    install(fetch)
    assert financial_sync.sync_candidate_income(["", " "], tmp_path, CapabilitySet()) == {
        "requested": 0, "updated_symbols": [], "errors": {},
    }
    fetch.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_candidate_update_requires_fuyao_route(tmp_path, install, monkeypatch):
    fetch = Mock()
    install(fetch)
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "tickflow")
    with pytest.raises(ValueError, match="扶摇"):
        financial_sync.sync_candidate_income(["600000.SH"], tmp_path, CapabilitySet())
    fetch.assert_not_called()


def test_unavailable_fuyao_financial_capability_cannot_fall_through_to_other_provider(tmp_path, install, monkeypatch):
    fetch = Mock()
    install(fetch)
    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: False)
    with pytest.raises(ValueError, match="扶摇"):
        financial_sync.sync_candidate_income(["600000.SH"], tmp_path, CapabilitySet())
    fetch.assert_not_called()


def test_scheduler_restart_never_treats_candidate_parquet_mtime_as_full_sync(tmp_path, install, monkeypatch):
    install(lambda table, symbols, latest_only: income_rows(symbols[0]))
    financial_sync.sync_candidate_income(["600000.SH"], tmp_path, CapabilitySet())
    monkeypatch.setattr(preferences, "get_financial_sync_times", lambda: {})
    scheduler = financial_sync.FinancialScheduler()
    scheduler.start(tmp_path, CapabilitySet())
    assert scheduler.last_sync == {}
    preferences.set_financial_sync_time.assert_not_called()
