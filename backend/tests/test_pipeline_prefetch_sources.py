"""网络预取不触碰共享仓库；覆盖证据与部分写入在串行提交时保留。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from threading import get_ident
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import ext_presets, index_sync
from app.services.daily_coverage import DailyCoverage
from app.services.ext_data import ExtConfigStore
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet


def test_index_prepare_is_bounded_and_only_commit_computes_and_writes(monkeypatch):
    day = date(2026, 10, 21)
    start = datetime(2026, 10, 20)
    end = datetime(2026, 10, 21)
    calls = []
    caps = CapabilitySet({Cap.KLINE_DAILY_BATCH: CapabilityLimits(batch=2)})
    monkeypatch.setattr(index_sync.preferences, "get_index_daily_batch_size", lambda: 2)
    monkeypatch.setattr(index_sync, "sleep_between_batches", lambda *args: None)

    def fetch(symbols, **kwargs):
        calls.append(("fetch", tuple(symbols)))
        rows = pl.DataFrame({
            "symbol": symbols, "date": [day] * len(symbols),
            "open": [10.] * len(symbols), "high": [12.] * len(symbols),
            "low": [9.] * len(symbols), "close": [11.] * len(symbols),
            "volume": [100.] * len(symbols), "amount": [1000.] * len(symbols),
        })
        kwargs["coverage"].observe(rows)
        # 本轮失败不能被已保存的旧数据掩盖。
        kwargs["coverage"].failed.add("000300.SH")
        return rows

    def compute(raw, **kwargs):
        calls.append(("compute", get_ident()))
        return raw

    monkeypatch.setattr(index_sync.kline_sync, "sync_daily_batch", fetch)
    monkeypatch.setattr(index_sync, "compute_enriched", compute)
    progress = []
    prepared = index_sync.prepare_index_daily(
        caps, ["000001.SH", "399001.SZ", "399006.SZ", "000300.SH"], start, end,
        coverage_day=day, on_chunk_done=lambda *args: progress.append(args),
    )
    assert [kind for kind, _ in calls] == ["fetch", "fetch"]
    assert progress == [(1, 2), (2, 2)]
    assert len(prepared.frames) == 2
    target = DailyCoverage(day)
    repo = SimpleNamespace(
        append_index_daily=lambda raw: calls.append(("daily", get_ident())),
        append_index_enriched=lambda enriched: calls.append(("enriched", get_ident())),
        refresh_index_views=lambda: calls.append(("refresh", get_ident())),
    )
    assert index_sync.commit_prepared_index_daily(prepared, repo, coverage=target) == 4
    assert target.active == {"000001.SH", "399001.SZ", "399006.SZ", "000300.SH"}
    assert target.failed == {"000300.SH"}
    assert all(thread == get_ident() for kind, thread in calls if kind != "fetch")

    with pytest.raises(ValueError, match="14 天"):
        index_sync.prepare_index_daily(caps, ["000001.SH"], start - timedelta(days=14), end)
    with pytest.raises(ValueError, match="4 个"):
        index_sync.prepare_index_daily(caps, [str(index) for index in range(5)], start, end)
    with pytest.raises(ValueError, match="日期"):
        index_sync.commit_prepared_index_daily(prepared, repo, coverage=DailyCoverage(day + timedelta(days=1)))


async def test_classifications_fetch_concurrently_then_commit_without_overwriting_config(tmp_path, monkeypatch):
    started = set()
    both_started = asyncio.Event()
    original_commit = ext_presets.commit_prepared_preset
    commits = []
    user_config = ext_presets._concept_preset()
    user_config.label = "自定义概念标题"
    ExtConfigStore(tmp_path).upsert(user_config)

    async def fetch(url):
        started.add(url)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        # 两个请求完成前均未写入结果。
        assert not commits
        if "concepts" in url:
            return [{"symbol": "600000.SH", "concepts": ["金融"]}]
        return [{"symbol": "600000.SH", "industries": ["金融", "银行"]}]

    def commit(prepared, root):
        commits.append(prepared.config.id)
        return original_commit(prepared, root)

    monkeypatch.setattr(ext_presets, "_fetch_json", fetch)
    monkeypatch.setattr(ext_presets, "commit_prepared_preset", commit)
    results = {}
    await ext_presets.sync_classifications(tmp_path, results, lambda *args: None)
    assert len(started) == 2
    assert commits == ["ext_gn_ths", "ext_hy_ths"]
    assert results == {"ext_gn_ths": 1, "ext_hy_ths": 1}
    assert ExtConfigStore(tmp_path).get("ext_gn_ths").label == "自定义概念标题"


async def test_preset_prepare_does_not_create_config_or_parquet(tmp_path, monkeypatch):
    async def fetch(url):
        return [{"symbol": "600000.SH", "concepts": ["金融"]}]

    monkeypatch.setattr(ext_presets, "_fetch_json", fetch)
    prepared = await ext_presets.prepare_preset("ext_gn_ths", tmp_path)
    assert not list(tmp_path.iterdir())
    assert prepared.rows[0]["所属概念"] == "金融"
    assert ext_presets.commit_prepared_preset(prepared, tmp_path) == 1
    assert ExtConfigStore(tmp_path).get("ext_gn_ths") is not None
