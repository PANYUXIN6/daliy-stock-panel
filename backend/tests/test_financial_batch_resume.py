"""财务完整批次保存、限流恢复及版本时间依据。"""
import json
from threading import Event
from types import SimpleNamespace

import polars as pl
import pytest

from app.data_providers.base import ProviderRateLimitError
from app.services import financial_sync, preferences, shared_runtime
from app.services.pipeline_jobs import JobCancelledError
from app.tickflow.capabilities import CapabilitySet


@pytest.fixture
def install(monkeypatch):
    from app.data_providers import custom

    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: True)
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "test")
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: None)
    def apply(fetch):
        provider = SimpleNamespace(financial_sync_batch_size=2, financial_history_periods=20,
                                   get_financials=fetch)
        monkeypatch.setattr(custom, "get_provider", lambda name: provider)
    return apply


def records(symbols, table):
    return pl.DataFrame([{
        "symbol": symbol, "period_end": "2026-06-30", "source_update_date": "2026-08-15",
        "date_basis": "observed" if table == "metrics" else "source_update",
        "roe": 12., "net_income": 100.,
    } for symbol in symbols])


@pytest.mark.parametrize("table", ["income", "balance_sheet", "cash_flow", "metrics"])
def test_saved_batches_resume_without_recollecting_or_moving_first_observation(tmp_path, install, table):
    symbols = ["600000.SH", "600001.SH", "600002.SH"]
    calls = []
    fail = [True]
    def fetch(name, chunk, latest_only):
        assert name == table and latest_only is False
        calls.append(list(chunk))
        if chunk == symbols[2:] and fail[0]:
            raise ProviderRateLimitError("limited")
        return records(chunk, table)
    install(fetch)
    def sync():
        return financial_sync._sync_history_table_for_symbols(table, symbols, tmp_path, CapabilitySet())
    with pytest.raises(ProviderRateLimitError):
        sync()
    path = tmp_path / f"financials/versions/{table}/part.parquet"
    first = pl.read_parquet(path)
    assert first["symbol"].to_list() == symbols[:2]
    if table == "metrics":
        assert first["availability_basis"].unique().to_list() == ["observed"]
    else:
        assert first["available_date"].unique().to_list() == ["2026-08-15"]
    fail[0] = False
    assert sync() == 3
    assert calls == [symbols[:2], symbols[2:], symbols[2:]]
    final = pl.read_parquet(path)
    assert final.filter(pl.col("symbol").is_in(symbols[:2])).equals(first)
    state = json.loads((tmp_path / f"financials/sync_progress/{table}.json").read_text())
    assert state["status"] == "completed" and set(state["completed_symbols"]) == set(symbols)
    # 完成后的新同步重新查询，但内容相同不增添版本或移动首次观察时间。
    assert sync() == 3
    assert pl.read_parquet(path).equals(final)


@pytest.mark.parametrize("resumed_symbols", [
    ["600002.SH", "600001.SH", "600000.SH"],
    ["600002.SH", "600001.SH", "600003.SH"],
])
def test_resume_keeps_saved_versions_when_universe_changes(tmp_path, install, resumed_symbols):
    original = ["600000.SH", "600001.SH", "600002.SH"]
    calls = []
    fail = [True]

    def fetch(table, chunk, latest_only):
        calls.append(list(chunk))
        if chunk == original[2:] and fail[0]:
            raise ProviderRateLimitError("limited")
        return records(chunk, table)

    install(fetch)
    with pytest.raises(ProviderRateLimitError):
        financial_sync._sync_history_table_for_symbols(
            "metrics", original, tmp_path, CapabilitySet(),
        )
    path = tmp_path / "financials/versions/metrics/part.parquet"
    first = pl.read_parquet(path)
    calls.clear()
    fail[0] = False
    financial_sync._sync_history_table_for_symbols(
        "metrics", resumed_symbols, tmp_path, CapabilitySet(),
    )
    assert calls == [[symbol for symbol in resumed_symbols if symbol not in original[:2]]]
    final = pl.read_parquet(path)
    assert final.filter(pl.col("symbol").is_in(original[:2])).equals(first)
    assert set(final["symbol"]) == set(original[:2]) | set(resumed_symbols)
    state = json.loads((tmp_path / "financials/sync_progress/metrics.json").read_text())
    assert state["status"] == "completed"
    assert state["symbols"] == resumed_symbols
    assert set(state["completed_symbols"]) == set(resumed_symbols)


def test_cancel_after_saved_batch_preserves_it_and_does_not_fetch_next(tmp_path, install):
    cancel = Event()
    calls = []
    def fetch(table, chunk, latest_only):
        calls.append(list(chunk))
        return records(chunk, table)
    install(fetch)
    def progress(done, total):
        if (tmp_path / "financials/versions/income/part.parquet").exists():
            cancel.set()
    with pytest.raises(JobCancelledError):
        financial_sync._sync_history_table_for_symbols(
            "income", ["600000.SH", "600001.SH", "600002.SH"], tmp_path, CapabilitySet(),
            progress, cancel_event=cancel,
        )
    assert calls == [["600000.SH", "600001.SH"]]
    assert pl.read_parquet(tmp_path / "financials/versions/income/part.parquet").height == 2


def test_explicit_skip_is_separate_from_saved_symbols_and_only_applies_to_resumed_run(tmp_path, install):
    symbols = ["600000.SH", "600001.SH", "605179.SH", "600002.SH"]
    calls = []
    fail = [True]

    def fetch(table, chunk, latest_only):
        calls.append(list(chunk))
        if "605179.SH" in chunk and fail[0]:
            raise RuntimeError("Data source unavailable")
        return records(chunk, table)

    install(fetch)
    progress = []

    def sync():
        return financial_sync._sync_history_table_for_symbols(
            "metrics", symbols, tmp_path, CapabilitySet(),
            lambda done, total: progress.append((done, total)),
        )

    with pytest.raises(RuntimeError, match="Data source unavailable"):
        sync()
    version_path = tmp_path / "financials/versions/metrics/part.parquet"
    first = pl.read_parquet(version_path)
    checkpoint_path = tmp_path / "financials/sync_progress/metrics.json"
    state = json.loads(checkpoint_path.read_text())
    state["skipped_symbols"] = {"605179.SH": "User approved skip after repeated source failure"}
    checkpoint_path.write_text(json.dumps(state))
    calls.clear()
    assert sync() == 3
    assert calls == [["600002.SH"]]
    assert progress[-1] == (4, 4)
    final = pl.read_parquet(version_path)
    assert "605179.SH" not in final["symbol"]
    assert final.filter(pl.col("symbol").is_in(symbols[:2])).equals(first)
    state = json.loads(checkpoint_path.read_text())
    assert state["status"] == "completed"
    assert set(state["completed_symbols"]) == set(symbols) - {"605179.SH"}
    assert state["skipped_symbols"] == {"605179.SH": "User approved skip after repeated source failure"}

    # 本轮的人工例外不成为永久黑名单；新一轮完整同步重新尝试该代码。
    calls.clear()
    fail[0] = False
    sync()
    assert any("605179.SH" in chunk for chunk in calls)
    state = json.loads(checkpoint_path.read_text())
    assert set(state["completed_symbols"]) == set(symbols)
    assert not state.get("skipped_symbols")


def test_cooldown_is_cancellable_and_non_resumable_sources_do_not_retry(install, monkeypatch):
    install(lambda *args: pl.DataFrame())
    cancel = Event()
    cancel.set()
    notices = []
    with pytest.raises(JobCancelledError):
        financial_sync.wait_financial_rate_limit(1, notices.append, cancel)
    assert notices and 0 < notices[0] <= 600
    monkeypatch.setattr(financial_sync, "_financial_batch_policy", lambda: None)
    assert financial_sync.wait_financial_rate_limit(1, notices.append) is False
