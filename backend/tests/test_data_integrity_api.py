from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import data, pipeline
from app.services import (
    data_integrity,
    heavy_job_limiter,
    integrity_check,
    pipeline_jobs,
    preferences,
    shared_runtime,
    trading_day,
)


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    from app.tickflow.repository import DataStore, KlineRepository
    store = pipeline_jobs.JobStore(store_dir=tmp_path / "jobs")
    monkeypatch.setattr(pipeline, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(heavy_job_limiter, "shared_heavy_job_limiter", heavy_job_limiter.HeavyJobLimiter())
    monkeypatch.setattr(heavy_job_limiter, "_memory_admission_ok", lambda: True)
    monkeypatch.setattr(preferences, "load", lambda: {})
    monkeypatch.setattr(integrity_check, "cn_today", lambda: date(2026, 10, 30))
    monkeypatch.setattr(data_integrity, "_trading_calendar", lambda: {date(2026, 10, 29)})
    monkeypatch.setattr(trading_day, "calendar_status", lambda root: {
        "start": "2020-01-01", "end": "2030-01-01",
        "days": [d.isoformat() for d in (data_integrity._trading_calendar() or [])],
    })
    monkeypatch.setattr(shared_runtime, "publish_quotes", lambda qs: None)
    paused = []

    @contextmanager
    def pause():
        paused.append("pause")
        try:
            yield
        finally:
            paused.append("resume")

    app = FastAPI()
    app.include_router(data.router)
    app.state.repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(app.state.repo, "get_instruments_asset", lambda asset: pl.DataFrame({"symbol": ["600001.SH"]}))
    app.state.capabilities = SimpleNamespace(has=lambda cap: True)
    app.state.quote_service = SimpleNamespace(paused=pause)
    return app, store, tmp_path, paused


def finished(store, jid):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        job = store.get(jid)
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_complete_history_repairs_sparse_partitions_before_analysis(app_env, monkeypatch):
    import json

    from app.market_time import CN_TZ
    from app.services import history_completion, kline_sync, regime_builder
    from app.services.index_const import CORE_INDEX_SYMBOLS

    app, store, root, paused = app_env
    repo = app.state.repo
    old, recent, today = date(2025, 6, 10), date(2026, 11, 6), date(2026, 11, 9)
    a, b = "600001.SH", "600002.SH"

    def rows(day, symbols, *, close=10.0, ts=None):
        return pl.DataFrame({"symbol": list(symbols), "date": [day] * len(symbols)}).with_columns(
            pl.lit(close).alias("open"), pl.lit(close + 1).alias("high"), pl.lit(close - 1).alias("low"),
            pl.lit(close).alias("close"), pl.lit(100.0).alias("volume"), pl.lit(1000.0).alias("amount"),
            pl.lit(ts, dtype=pl.Int64).alias("quote_ts"),
        )

    monkeypatch.setattr(history_completion, "cn_now", lambda: datetime(2026, 11, 9, 11, tzinfo=CN_TZ))
    monkeypatch.setattr(trading_day, "calendar_status", lambda root: {
        "start": "2024-01-01", "end": today.isoformat(), "days": [str(old), str(recent), str(today)],
    })
    monkeypatch.setattr(history_completion, "_classification_gaps", lambda *_: 0)
    monkeypatch.setattr(kline_sync, "sync_adj_factor", lambda *_, **__: (0, []))
    monkeypatch.setattr(preferences, "get_daily_data_provider", lambda: "tickflow")
    instruments = pl.DataFrame({"symbol": [a, b], "as_of": [today, today],
                                "listing_date": [old, old], "name": ["甲", "乙"]})
    (root / "instruments").mkdir(exist_ok=True)
    instruments.write_parquet(root / "instruments/instruments.parquet")
    monkeypatch.setattr(repo, "get_instruments_asset", lambda asset: instruments if asset == "stock" else pl.DataFrame())
    repo.append_daily(rows(old, [a]))
    repo.append_daily(rows(recent, [a, b]))
    repo.append_daily(rows(recent, [a], ts=int(datetime(2026, 11, 6, 11, tzinfo=CN_TZ).timestamp() * 1000)))
    repo.append_daily(rows(today, [a, b]))
    repo.append_index_daily(rows(old, CORE_INDEX_SYMBOLS))
    repo.append_index_daily(rows(recent, CORE_INDEX_SYMBOLS))
    today_path = root / f"kline_daily/date={today}/part.parquet"
    before_today = today_path.read_bytes()
    requests = []

    def fetch(symbols, _caps, *, asset_type, start_time, end_time, **_):
        requests.append((asset_type, start_time.date(), end_time.date()))
        if asset_type == "stock":
            yield rows(old, [a, b], close=22.0)
            yield rows(recent, [a, b], close=21.0)
            yield rows(today, [a, b], close=88.0)

    monkeypatch.setattr(kline_sync, "iter_daily_frames", fetch)
    with TestClient(app) as client:
        response = client.post("/api/data/history/complete")
        assert response.status_code == 200
        result = finished(store, response.json()["job_id"])
        assert result["status"] == "succeeded", result["error"]
        assert result["result"]["history_completion"] == {
            "start_date": "2024-01-01", "end_date": "2026-11-08", "today_deferred": True,
            "daily_rows": 2, "factor_rows": 0, "unverified_days": 0, "unverified_rows": 0,
        }
        assert result["result"]["analysis"]["is_complete_day"] is True
        assert paused == ["pause", "resume"]
        assert set(json.loads((root / "runtime/data_update.json").read_text())["datasets"]) == {
            "daily", "enriched", "regime", "mainline", "ext_data", "daily_integrity",
        }
        history = regime_builder.load_regime_history(root).filter(pl.col("date") == old)
        assert history["total_count"].item() == 2
        older_path = root / f"kline_daily/date={old}/part.parquet"
        assert dict(pl.read_parquet(older_path).select("symbol", "close").iter_rows()) == {a: 10.0, b: 22.0}
        latest = pl.read_parquet(root / f"kline_daily/date={recent}/part.parquet").sort("symbol")
        assert latest["close"].to_list() == [21.0, 10.0]
        assert latest["quote_ts"].null_count() == 2
        assert today_path.read_bytes() == before_today

        original = older_path.read_bytes()
        repeated = client.post("/api/data/history/complete")
        result = finished(store, repeated.json()["job_id"])
        assert result["status"] == "succeeded", result["error"]
        assert result["result"]["history_completion"]["daily_rows"] == 0
        assert requests == [("stock", old, recent)]
        assert older_path.read_bytes() == original


def test_api_scan_and_repair_publish_rechecked_report(app_env, monkeypatch):
    from app.services import kline_sync

    app, store, root, paused = app_env
    part = root / "kline_daily/date=2026-10-29"
    part.mkdir(parents=True)
    (part / "part.parquet").write_bytes(b"bad parquet")

    def fetch(*args, **kwargs):
        assert kwargs["asset_type"] == "stock"
        yield pl.DataFrame({
            "symbol": ["600001.SH"], "date": [date(2026, 10, 29)],
            "open": [10.0], "high": [11.0], "low": [9.0], "close": [10.0],
            "volume": [100.0], "amount": [100000.0],
        })

    monkeypatch.setattr(kline_sync, "iter_daily_frames", fetch)
    with TestClient(app) as client:
        assert client.get("/api/data/integrity").json()["report"] is None
        response = client.post("/api/data/integrity/scan", json={"lookback_days": 7, "asset_types": ["stock"]})
        assert response.status_code == 200
        job = finished(store, response.json()["job_id"])
        assert job["status"] == "succeeded"
        report = client.get("/api/data/integrity").json()["report"]
        assert report["repairable_count"] == 1
        assert paused == []  # 只读扫描不暂停实时行情

        response = client.post("/api/data/integrity/repair", json={"report_id": report["id"]})
        assert response.status_code == 200
        job = finished(store, response.json()["job_id"])
        assert job["status"] == "succeeded"
        final = client.get("/api/data/integrity").json()["report"]
        assert final["status"] == "clean" and final["issues"] == []
        assert paused == ["pause", "resume"]
        import json
        event = json.loads((root / "runtime/data_update.json").read_text())
        assert event["datasets"] == ["daily_integrity"]


def test_api_repair_reports_old_history_and_next_attempt_repairs_it(app_env, monkeypatch):
    from app.services import daily_health, kline_sync

    app, store, root, paused = app_env
    old, recent = date(2026, 8, 31), date(2026, 10, 29)
    symbol = "600001.SH"
    monkeypatch.setattr(data_integrity, "_trading_calendar", lambda: {old, recent})

    def frame(day, bad=False):
        return pl.DataFrame({
            "symbol": [symbol], "date": [day], "open": [10.0], "high": [11.0],
            "low": [9.0], "close": [999.0 if bad else 10.0],
            "volume": [100.0], "amount": [1000.0],
        })

    for day in (old, recent):
        app.state.repo.append_daily(frame(day, bad=day == old))
        app.state.repo.append_enriched(frame(day))
    issue = data_integrity.IntegrityIssue(recent, "kline_daily", "invalid", symbols=(symbol,))
    daily_health.record_scan(root, [issue], assets=["stock"], start=recent, end=recent)
    daily_health.record_scan(root, [], assets=["stock"], start=recent, end=recent)
    fetched = []

    def fetch(symbols, _caps, **kwargs):
        fetched.append((kwargs["start_time"].date(), symbols))
        yield frame(old)

    monkeypatch.setattr(kline_sync, "iter_daily_frames", fetch)
    with TestClient(app) as client:
        response = client.post("/api/data/integrity/scan", json={"lookback_days": 30, "asset_types": ["stock"]})
        assert finished(store, response.json()["job_id"])["status"] == "succeeded"
        report = client.get("/api/data/integrity").json()["report"]
        response = client.post("/api/data/integrity/repair", json={"report_id": report["id"]})
        failure = finished(store, response.json()["job_id"])
        assert failure["status"] == "failed"
        assert "2026-08-31" in failure["error"] and symbol in failure["error"]
        result = client.get("/api/data/integrity").json()
        assert result["blocking_assets"] == ["stock"]
        historical = next(i for i in result["report"]["issues"] if i["date"] == old.isoformat())
        assert historical["symbols"] == [symbol] and "最高价" in historical["reason"]
        assert fetched == []
        response = client.post("/api/data/integrity/repair", json={"report_id": result["report"]["id"]})
        assert finished(store, response.json()["job_id"])["status"] == "succeeded"
        final = client.get("/api/data/integrity").json()
        assert final["report"]["status"] == "clean" and final["blocking_assets"] == []
        assert final["report"]["lookback_days"] == 30
        assert fetched == [(old, [symbol])]
        assert paused == ["pause", "resume", "pause", "resume"]


@pytest.mark.parametrize("body", [
    {"lookback_days": 0}, {"lookback_days": 367}, {"asset_types": []},
    {"asset_types": ["../../outside"]},
])
def test_api_rejects_invalid_scan_scope(app_env, body):
    app, _, _, _ = app_env
    with TestClient(app) as client:
        assert client.post("/api/data/integrity/scan", json=body).status_code == 422


def test_api_refuses_other_active_job_and_stale_report(app_env):
    app, store, _, _ = app_env
    store.create(timeout_s=60)
    with TestClient(app) as client:
        assert client.post("/api/data/integrity/scan", json={}).status_code == 409
        response = client.post("/api/data/integrity/repair", json={"report_id": "f" * 32})
        assert response.status_code == 409 and "重新自检" in response.json()["detail"]


def test_api_empty_scan_does_not_require_data_source(app_env):
    app, store, _, _ = app_env
    app.state.capabilities = SimpleNamespace(has=lambda cap: False)
    with TestClient(app) as client:
        response = client.post("/api/data/integrity/scan", json={"asset_types": ["etf"]})
        assert response.status_code == 200
        assert finished(store, response.json()["job_id"])["status"] == "succeeded"
        report = client.get("/api/data/integrity").json()["report"]
        assert report["status"] == "empty" and report["issues"] == []
        assert report["no_data_assets"] == ["etf"]


def test_api_repair_reports_unavailable_source_without_starting_job(app_env):
    app, store, root, _ = app_env
    part = root / "kline_daily/date=2026-10-29"
    part.mkdir(parents=True)
    (part / "part.parquet").write_bytes(b"bad parquet")
    report = integrity_check.run_check(root, asset_types=["stock"])
    app.state.capabilities = SimpleNamespace(has=lambda cap: False)
    with TestClient(app) as client:
        response = client.post("/api/data/integrity/repair", json={"report_id": report["id"]})
        assert response.status_code == 403 and "数据源" in response.json()["detail"]
        assert store.list_recent() == []


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/data/integrity"),
    ("POST", "/api/data/integrity/scan"),
    ("POST", "/api/data/integrity/repair"),
])
def test_integrity_routes_belong_to_collector_and_require_admin(method, path):
    from app.services.auth_access import admin_required
    from app.services.collector_proxy import collector_owns

    assert collector_owns(method, path)
    assert admin_required(path, method)


def test_recompute_only_retry_works_without_available_daily_source(app_env, monkeypatch):
    from app.services import daily_health

    app, store, root, _ = app_env
    day = date(2026, 10, 29)
    symbol = "600001.SH"
    frame = pl.DataFrame({
        "symbol": [symbol], "date": [day], "open": [10.0], "high": [11.0],
        "low": [9.0], "close": [10.0], "volume": [100.0], "amount": [1000.0],
    })
    app.state.repo.append_daily(frame)
    app.state.repo.append_enriched(frame)
    daily_health.record_issues(root, [data_integrity.IntegrityIssue(day, "kline_daily", "invalid", symbols=(symbol,))])
    report = integrity_check.run_check(root, asset_types=["stock"])
    assert report["issues"][0]["kind"] == "recompute"
    app.state.capabilities = SimpleNamespace(has=lambda cap: False)
    with TestClient(app) as client:
        response = client.post("/api/data/integrity/repair", json={"report_id": report["id"]})
        assert response.status_code == 200
        assert finished(store, response.json()["job_id"])["status"] == "succeeded"
        assert client.get("/api/data/integrity").json()["blocking_assets"] == []
