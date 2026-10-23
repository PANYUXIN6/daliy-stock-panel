"""ETF 显式同步从本地日K分批恢复，不让单分区刷新放行未知历史状态。"""
import json
from datetime import date

import polars as pl
import pytest

from app.enriched_generation import EnrichedGenerationUnavailableError, get_enriched_generation
from app.services import index_sync
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository


def _seed(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    symbols = [f"{510000 + i:06d}.SH" for i in range(101)]
    rows = [(symbol, day, value) for symbol in symbols for day, value in (
        (date(2026, 10, 20), 10.0), (date(2026, 10, 21), 11.0),
    )]
    raw = pl.DataFrame(rows, schema=["symbol", "date", "close"], orient="row").with_columns(
        pl.col("close").alias("open"), pl.col("close").alias("high"), pl.col("close").alias("low"),
        pl.lit(100.0).alias("volume"), pl.lit(1000.0).alias("amount"),
    )
    repo.append_etf_daily(raw)
    repo.append_etf_enriched(raw.with_columns(pl.lit(1.0).alias("close")))
    (tmp_path / ".matrix_generation_etf.json").write_text(json.dumps({
        "state": "publishing", "generation": "legacy", "owner_pid": 999999999,
    }))
    return repo


def test_etf_recovery_uses_bounded_batches_and_commits_after_all_dates(tmp_path, monkeypatch):
    repo = _seed(tmp_path)
    original = index_sync.compute_enriched
    sizes = []

    def compute(raw, **kwargs):
        sizes.append(raw["symbol"].n_unique())
        with pytest.raises(EnrichedGenerationUnavailableError):
            get_enriched_generation(tmp_path, "etf")
        return original(raw, **kwargs)

    monkeypatch.setattr(index_sync, "compute_enriched", compute)
    index_sync._recover_etf_enriched(repo)
    assert sizes == [100, 1]
    assert get_enriched_generation(tmp_path, "etf") != "legacy"
    for day, price in (("2026-10-20", 10.0), ("2026-10-21", 11.0)):
        frame = pl.read_parquet(tmp_path / "kline_etf_enriched" / f"date={day}" / "part.parquet")
        assert frame.height == 101
        assert frame["close"].unique().to_list() == [price]


def test_etf_partial_recovery_stays_closed_and_next_attempt_completes(tmp_path, monkeypatch):
    repo = _seed(tmp_path)
    original = index_sync.compute_enriched
    calls = 0

    def fail_second(raw, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("interrupted")
        return original(raw, **kwargs)

    monkeypatch.setattr(index_sync, "compute_enriched", fail_second)
    with pytest.raises(OSError, match="interrupted"):
        index_sync._recover_etf_enriched(repo)
    with pytest.raises(EnrichedGenerationUnavailableError):
        get_enriched_generation(tmp_path, "etf")
    monkeypatch.setattr(index_sync, "compute_enriched", original)
    index_sync._recover_etf_enriched(repo)
    assert get_enriched_generation(tmp_path, "etf")
    assert pl.read_parquet(tmp_path / "kline_etf_enriched" / "date=2026-10-21" / "part.parquet").height == 101


def test_etf_recovery_refuses_missing_source(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    (tmp_path / ".matrix_generation_etf.json").write_text(json.dumps({
        "state": "publishing", "generation": "legacy", "owner_pid": 999999999,
    }))
    with pytest.raises(RuntimeError, match="缺少本地 ETF"):
        index_sync._recover_etf_enriched(repo)
    with pytest.raises(EnrichedGenerationUnavailableError):
        get_enriched_generation(tmp_path, "etf")


def test_etf_recovery_can_cancel_between_batches_and_retry(tmp_path):
    repo = _seed(tmp_path)
    calls = []

    def cancel_after_first(current, total):
        calls.append((current, total))
        if current == 1:
            raise InterruptedError("cancelled")

    with pytest.raises(InterruptedError, match="cancelled"):
        index_sync._recover_etf_enriched(repo, on_chunk_done=cancel_after_first)
    assert calls == [(0, 2), (1, 2)]
    with pytest.raises(EnrichedGenerationUnavailableError):
        get_enriched_generation(tmp_path, "etf")
    index_sync._recover_etf_enriched(repo)
    assert get_enriched_generation(tmp_path, "etf")


def test_etf_recovered_cache_and_views_do_not_depend_on_following_network_sync(tmp_path, monkeypatch):
    repo = _seed(tmp_path)
    repo._etf_enriched_cache = pl.DataFrame({"close": [1.0]})
    repo._etf_enriched_cache_date = date(2026, 10, 21)
    repo._etf_live_agg_cache = pl.DataFrame({"close": [1.0]})
    callbacks = []

    def unavailable(*args, **kwargs):
        raise OSError("upstream unavailable")

    monkeypatch.setattr(index_sync.kline_sync, "sync_daily_batch", unavailable)
    capset = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=50, rpm=30)})
    with pytest.raises(OSError, match="upstream unavailable"):
        index_sync.sync_and_persist_etf_daily(
            repo, capset, symbols_override=["510000.SH"],
            on_chunk_done=lambda current, total: callbacks.append((current, total)),
        )
    assert callbacks == [(0, 2), (1, 2), (2, 2)]
    assert get_enriched_generation(tmp_path, "etf")
    assert repo._etf_enriched_cache is None
    assert repo._etf_enriched_cache_date is None
    assert repo._etf_live_agg_cache is None
    assert repo.execute_all("SELECT DISTINCT close FROM kline_etf_enriched ORDER BY close") == [(10.0,), (11.0,)]
    latest, day = repo.get_enriched_latest_asset("etf")
    assert day == date(2026, 10, 21)
    assert latest["close"].unique().to_list() == [11.0]
