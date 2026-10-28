"""四ETF场内规模代理：单位换算、同日完整性、持久化与只读查询。"""
from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import polars as pl
import pytest
from fastapi import FastAPI

from app.data_providers.csi300_reference import FUNDS, Csi300ReferenceSource
from app.plugins.fuyao.provider import FuyaoProvider
from app.services import etf_reference as ref

DAY = date(2026, 9, 29)
NEXT = date(2026, 9, 30)


def inputs(days=(DAY,)):
    return [{"symbol": symbol, "date": day.isoformat(), "shares": 100.0, "unit_nav": 2.0}
            for day in days for symbol in FUNDS]


def caps(days=(DAY,)):
    return [{"date": day.isoformat(), "float_cap_yuan": 40000.0} for day in days]


def test_ratio_is_sum_of_shares_times_unit_nav_over_float_cap():
    assert ref.build_points(inputs(), caps(), [DAY]) == [
        {"date": DAY.isoformat(), "etf_assets_yuan": 800.0, "float_cap_yuan": 40000.0, "ratio": 0.02}]


@pytest.mark.parametrize("corrupt", ["missing_fund", "duplicate", "zero", "nan", "null", "empty", "missing_cap"])
def test_incomplete_or_invalid_day_never_produces_a_point(corrupt):
    rows, denominators, days = inputs(), caps(), [DAY]
    if corrupt == "missing_fund":
        rows.pop()
    elif corrupt == "duplicate":
        rows.append(rows[0])
    elif corrupt in {"zero", "nan", "null"}:
        rows[0]["unit_nav"] = {"zero": 0, "nan": float("nan"), "null": None}[corrupt]
    elif corrupt == "empty":
        rows = []
    else:
        rows, days, denominators = inputs((DAY, NEXT)), [DAY, NEXT], caps((NEXT,))
    with pytest.raises(ValueError):
        ref.build_points(rows, denominators, days)


@pytest.fixture
def public_source(monkeypatch):
    source = Csi300ReferenceSource()
    monkeypatch.setattr("app.data_providers.csi300_reference.time.sleep", lambda _: None)
    yield source
    source.close()


def mock_http(source, payload):
    source.client.close()
    def respond(req):
        if req.url.host == "www.szse.cn":
            assert req.headers["Referer"] == "https://www.szse.cn/market/fund/volume/etf/index.html"
        return httpx.Response(200, json=payload)

    source.client = httpx.Client(transport=httpx.MockTransport(respond))


def test_official_units_and_sse_page_completeness(public_source):
    mock_http(public_source, {"result": [{"MDATE": "20260929", "CPT_NGT": "123,456.78"}]})
    assert public_source.index_float_caps(DAY, DAY) == [{"date": "2026-09-29", "float_cap_yuan": 123456.78 * 1e8}]
    payload = {"result": [{"SEC_CODE": symbol[:6], "STAT_DATE": DAY.isoformat(), "TOT_VOL": "1,234.5"}
                          for symbol in FUNDS if symbol.endswith(".SH")], "pageHelp": {"total": 3}}
    mock_http(public_source, payload)
    assert [r["shares"] for r in public_source.sse_shares(DAY)] == [12345000.0] * 3
    payload["pageHelp"]["total"] = 4
    mock_http(public_source, payload)
    with pytest.raises(ValueError, match="分页不完整"):
        public_source.sse_shares(DAY)


def test_szse_units_and_page_validation(public_source):
    payload = [{"metadata": {"pagecount": 1, "recordcount": 1}, "data": [
        {"fund_code": "159919", "size_date": DAY.isoformat(), "current_size": "987.65"},
    ]}]
    mock_http(public_source, payload)
    assert public_source.szse_shares(DAY, DAY) == [
        {"symbol": "159919.SZ", "date": DAY.isoformat(), "shares": 9876500.0},
    ]
    payload[0]["metadata"]["recordcount"] = 2
    mock_http(public_source, payload)
    with pytest.raises(ValueError, match="分页不完整"):
        public_source.szse_shares(DAY, DAY)


def test_fund_nav_uses_unit_value_and_shanghai_date(monkeypatch):
    provider = FuyaoProvider()
    ms = int(datetime(2026, 9, 29, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)
    client = SimpleNamespace(fund_nav=lambda symbol: {"item": [
        {"nav_date": ms, "unit_nav": 2, "accumulated_nav": 9, "adjusted_nav": 12},
        {"nav_date": ms + 86400000, "unit_nav": 3},
    ]})
    monkeypatch.setattr(provider, "_get_client", lambda: client)
    assert provider.get_fund_nav(["510310.SH"], DAY, DAY).to_dicts() == [
        {"symbol": "510310.SH", "date": DAY, "unit_nav": 2.0}]


class FakeSource:
    def __init__(self):
        self.days = [DAY]
        self.fail = False
        self.sh_calls = []
        self.sz_calls = []

    def index_float_caps(self, start, end):
        if self.fail:
            raise ValueError("source unavailable")
        return [r for r in caps(self.days) if start.isoformat() <= r["date"] <= end.isoformat()]

    def trading_days(self, start, end):
        return [day for day in self.days if start <= day <= end]

    def sse_shares(self, day):
        self.sh_calls.append(day)
        return [{k: v for k, v in r.items() if k != "unit_nav"}
                for r in inputs((day,)) if r["symbol"].endswith(".SH")]

    def szse_shares(self, start, end):
        self.sz_calls.append((start, end))
        return [{k: v for k, v in r.items() if k != "unit_nav"}
                for r in inputs(self.days) if r["symbol"].endswith(".SZ") and start.isoformat() <= r["date"] <= end.isoformat()]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from app.services import pipeline_jobs, preferences, shared_runtime

    monkeypatch.setattr(pipeline_jobs, "job_store", pipeline_jobs.JobStore(store_dir=tmp_path / "job_store"))

    monkeypatch.setattr(ref, "cn_now", lambda: datetime(2026, 10, 28, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    monkeypatch.setattr(ref, "cn_today", lambda: date(2026, 10, 28))
    monkeypatch.setattr(preferences, "get_fund_nav_provider", lambda: "fuyao")
    events = []
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: events.append(args))
    source = FakeSource()
    provider = SimpleNamespace(get_fund_nav=lambda symbols, start, end: pl.DataFrame(inputs(source.days)).select(
        "symbol", pl.col("date").str.to_date(), "unit_nav"))
    return source, provider, events


def test_daily_check_incremental_shares_and_revised_nav(tmp_path, runtime, monkeypatch):
    source, provider, events = runtime
    assert ref.sync_reference(tmp_path, source=source, nav_provider=provider)["as_of"] == DAY.isoformat()
    assert ref.read_reference(tmp_path)["rows"][0]["ratio"] == 0.02
    source.fail = True
    assert ref.sync_reference(tmp_path, source=source, nav_provider=provider)["updated"] is False
    source.fail = False
    source.days = [DAY, NEXT]
    monkeypatch.setattr(ref, "cn_now", lambda: datetime(2026, 10, 29, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    provider.get_fund_nav = lambda *args: pl.DataFrame(inputs(source.days)).select(
        "symbol", pl.col("date").str.to_date(), (pl.col("unit_nav") * 2))
    assert ref.sync_reference(tmp_path, source=source, nav_provider=provider)["rows"] == 2
    assert source.sh_calls == [DAY, NEXT]
    assert source.sz_calls == [(DAY, DAY), (NEXT, NEXT)]
    assert ref.read_reference(tmp_path)["rows"][0]["ratio"] == 0.04
    assert len(events) == 2


def test_failed_update_and_cancellation_keep_previous_snapshot(tmp_path, runtime):
    source, provider, events = runtime
    ref.sync_reference(tmp_path, source=source, nav_provider=provider)
    before = ref._path(tmp_path).read_bytes()
    source.fail = True
    with pytest.raises(ValueError, match="source unavailable"):
        ref.sync_reference(tmp_path, force=True, source=source, nav_provider=provider)
    assert ref._path(tmp_path).read_bytes() == before
    assert ref.read_reference(tmp_path)["update_failed"] is True
    source.fail = False

    from app.services.pipeline_jobs import JobCancelledError

    def cancel():
        raise JobCancelledError("etf_reference")

    with pytest.raises(JobCancelledError):
        ref.sync_reference(tmp_path, force=True, source=source, nav_provider=provider, check_cancel=cancel)
    assert ref._path(tmp_path).read_bytes() == before
    assert len(events) == 1


def test_query_is_local_and_respects_selected_date(tmp_path, runtime, monkeypatch):
    from app.api import indices

    source, provider, _ = runtime
    source.days = [DAY, NEXT]
    ref.sync_reference(tmp_path, source=source, nav_provider=provider)
    monkeypatch.setattr(ref, "sync_reference", lambda *a, **kw: pytest.fail("GET cannot collect"))
    app = FastAPI()
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.include_router(indices.router)

    async def query():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            result = await client.get("/api/index/etf-reference", params={"as_of": DAY.isoformat()})
            assert result.status_code == 200
            assert [r["date"] for r in result.json()["rows"]] == [DAY.isoformat()]
            assert result.json()["as_of"] == DAY.isoformat()
            result = await client.get("/api/index/etf-reference", params={"as_of": "2026-09-28"})
            assert result.json()["state"] == "no_data"
    asyncio.run(query())


def test_http_nav_mapping_batching_and_empty_schema(monkeypatch):
    from app.data_providers.custom.config import config_from_dict
    from app.data_providers.custom.provider import GenericHTTPProvider
    from app.data_providers.fund_nav import NAV_SCHEMA

    config = config_from_dict({"name": "test", "datasets": {"fund_nav": {
        "url": "https://example.com/nav", "batch": 1, "rpm": 600,
        "field_map": {"code": "symbol", "day": "date", "nav": "unit_nav"},
    }}})
    provider = GenericHTTPProvider(config)
    calls = []

    def fetch(cfg, *, symbols, start_time, end_time):
        calls.append(symbols)
        assert start_time.date() == end_time.date() == DAY
        return [{"code": symbols[0], "day": DAY.isoformat(), "nav": "2.5"}]

    monkeypatch.setattr(provider, "_request_rows", fetch)
    monkeypatch.setattr("app.data_providers.custom.provider.sleep_between_batches", lambda *args: None)
    try:
        assert provider.validate() == []
        result = provider.get_fund_nav(list(FUNDS[:2]), DAY, DAY)
        assert calls == [[FUNDS[0]], [FUNDS[1]]]
        assert result["unit_nav"].to_list() == [2.5, 2.5]
        assert provider.get_fund_nav([], DAY, DAY).schema == NAV_SCHEMA
    finally:
        provider.close()


def test_reference_collection_requires_admin_but_chart_is_readable():
    from app.services.auth_access import admin_required

    assert admin_required("/api/index/etf-reference/sync", "POST")
    assert not admin_required("/api/index/etf-reference", "GET")


def test_busy_scheduler_retries_without_claiming_a_daily_check(tmp_path, runtime, monkeypatch):
    from fastapi import HTTPException

    from app.api import indices
    from app.jobs import daily_pipeline

    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: SimpleNamespace(quote_service=None))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    calls = []

    async def submit(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise HTTPException(409, "busy")
        return {"job_id": "reference-job"}

    monkeypatch.setattr(indices, "submit_etf_reference", submit)
    assert asyncio.run(ref.run_scheduled(repo)) is None
    assert asyncio.run(ref.run_scheduled(repo)) == {"job_id": "reference-job"}
    assert calls == [{"force": False}, {"force": False}]


def test_curve_stops_at_denominator_then_extends_when_it_is_published(tmp_path, runtime, monkeypatch):
    import json

    source, provider, _ = runtime
    # ETF已经有下一交易日净值，官方分母尚只发布到DAY。
    provider.get_fund_nav = lambda *args: pl.DataFrame(inputs((DAY, NEXT))).select(
        "symbol", pl.col("date").str.to_date(), "unit_nav")
    ref.sync_reference(tmp_path, source=source, nav_provider=provider)
    snapshot = json.loads(ref._path(tmp_path).read_text())
    assert [row["date"] for row in snapshot["rows"]] == [DAY.isoformat()]
    assert {row["date"] for row in snapshot["inputs"]} == {DAY.isoformat()}

    # 次日盘后查到新分母，四只ETF同日数据齐全后才能增加点。
    source.days = [DAY, NEXT]
    monkeypatch.setattr(ref, "cn_now", lambda: datetime(2026, 10, 29, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    ref.sync_reference(tmp_path, source=source, nav_provider=provider)
    snapshot = json.loads(ref._path(tmp_path).read_text())
    assert [row["date"] for row in snapshot["rows"]] == [DAY.isoformat(), NEXT.isoformat()]
    assert {row["date"] for row in snapshot["inputs"]} == {DAY.isoformat(), NEXT.isoformat()}
    assert snapshot["rows"][-1]["ratio"] == 0.02


def test_morning_check_does_not_replace_postmarket_check(tmp_path, runtime, monkeypatch):
    source, provider, events = runtime
    monkeypatch.setattr(ref, "cn_now", lambda: datetime(2026, 10, 28, 8, 45, tzinfo=ZoneInfo("Asia/Shanghai")))
    ref.sync_reference(tmp_path, source=source, nav_provider=provider)
    source.days = [DAY, NEXT]
    monkeypatch.setattr(ref, "cn_now", lambda: datetime(2026, 10, 28, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    assert ref.sync_reference(tmp_path, source=source, nav_provider=provider)["as_of"] == NEXT.isoformat()
    assert ref.sync_reference(tmp_path, source=source, nav_provider=provider)["updated"] is False
    assert len(events) == 2


@pytest.mark.parametrize("hour, already_checked, should_submit", [
    (15, False, False), (16, False, True), (20, False, True), (20, True, False),
])
def test_scheduler_only_submits_unchecked_postmarket_run(tmp_path, runtime, monkeypatch, hour, already_checked, should_submit):
    from app.api import indices
    from app.jobs import daily_pipeline

    source, provider, _ = runtime
    if already_checked:
        ref.sync_reference(tmp_path, source=source, nav_provider=provider)
    monkeypatch.setattr(ref, "cn_now", lambda: datetime(2026, 10, 28, hour, 0, tzinfo=ZoneInfo("Asia/Shanghai")))
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: SimpleNamespace(quote_service=None))
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    calls = []

    async def submit(*args, **kwargs):
        calls.append(kwargs)
        return {"job_id": "reference-job"}

    monkeypatch.setattr(indices, "submit_etf_reference", submit)
    result = asyncio.run(ref.run_scheduled(repo))
    assert bool(calls) is should_submit
    assert result == ({"job_id": "reference-job"} if should_submit else None)


@pytest.fixture
def cancellation_runtime(tmp_path, runtime, monkeypatch):
    import socket
    import threading
    from datetime import UTC

    from app.api import pipeline
    from app.config import settings
    from app.data_providers import custom
    from app.jobs import daily_pipeline
    from app.services import heavy_job_limiter, pipeline_jobs, preferences

    def deny_network(*args, **kwargs):
        pytest.fail("取消回归测试不应访问网络")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    now = [datetime(2026, 10, 28, 16, 0, tzinfo=ZoneInfo("Asia/Shanghai"))]
    gate, entered = threading.Event(), threading.Event()
    calls = []
    store = pipeline_jobs.job_store
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(pipeline, "job_store", store)
    monkeypatch.setattr(pipeline_jobs, "_CANCEL_FLAGS", {})
    monkeypatch.setattr(pipeline_jobs, "_run_slot_owner", None)
    monkeypatch.setattr(pipeline_jobs, "_now_iso", lambda: now[0].astimezone(UTC).isoformat())
    monkeypatch.setattr(heavy_job_limiter, "shared_heavy_job_limiter", heavy_job_limiter.HeavyJobLimiter())
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: SimpleNamespace(quote_service=None))
    monkeypatch.setattr(ref, "cn_now", lambda: now[0])
    monkeypatch.setattr(ref, "cn_today", lambda: now[0].date())
    monkeypatch.setattr(preferences, "get_data_source_job_timeout_s", lambda: 1200)
    monkeypatch.setattr(custom, "provider_has_dataset", lambda *_: True)
    monkeypatch.setattr(custom, "get_provider", lambda *_: runtime[1])
    monkeypatch.setattr(pipeline, "invalidate_storage_cache", lambda: None)

    class Source(FakeSource):
        def __init__(self, check):
            super().__init__()
            self.check = check

        def index_float_caps(self, start, end):
            calls.append("source")
            entered.set()
            assert gate.wait(3), "测试未释放取数等待"
            self.check()
            return super().index_float_caps(start, end)

        def close(self):
            pass

    monkeypatch.setattr(ref, "Csi300ReferenceSource", Source)
    return SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
                           store=store, now=now, gate=gate, entered=entered, calls=calls,
                           monkeypatch=monkeypatch)


async def _drain_reference_jobs():
    from app.api import pipeline

    if pipeline._tasks:
        await asyncio.wait_for(asyncio.gather(*tuple(pipeline._tasks)), timeout=5)


@pytest.mark.parametrize("restart", [False, True])
def test_real_running_cancel_blocks_evening_retry_but_allows_manual_and_next_day(cancellation_runtime, restart):
    from app.api import indices, pipeline
    from app.services import pipeline_jobs

    rt = cancellation_runtime
    snapshot = ref._path(rt.repo.store.data_dir)
    snapshot.parent.mkdir(parents=True)
    original = b'{"rows": [{"date": "2026-09-29", "ratio": 0.02}]}'
    snapshot.write_bytes(original)

    async def scenario():
        first = await ref.run_scheduled(rt.repo)
        try:
            assert await asyncio.to_thread(rt.entered.wait, 3)
            pipeline.cancel_job(first["job_id"])
        finally:
            rt.gate.set()
            await _drain_reference_jobs()
        assert rt.store.get(first["job_id"])["outcome"] == "cancelled"
        assert snapshot.read_bytes() == original
        if restart:
            rt.store = pipeline_jobs.JobStore(store_dir=rt.repo.store.data_dir / "job_store")
            rt.monkeypatch.setattr(pipeline_jobs, "job_store", rt.store)
            rt.monkeypatch.setattr(pipeline, "job_store", rt.store)
        rt.now[0] = rt.now[0].replace(hour=17)
        automatic = await ref.run_scheduled(rt.repo)
        await _drain_reference_jobs()
        assert automatic is None
        assert rt.calls == ["source"]

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=rt.repo)))
        manual = await indices.sync_etf_reference(request)
        await _drain_reference_jobs()
        assert rt.store.get(manual["job_id"])["outcome"] == "succeeded"
        rt.now[0] = rt.now[0].replace(day=29, hour=16)
        following_day = await ref.run_scheduled(rt.repo)
        await _drain_reference_jobs()
        assert rt.store.get(following_day["job_id"])["outcome"] == "succeeded"
        assert rt.calls == ["source"] * 3

    asyncio.run(scenario())


def test_real_pending_cancel_blocks_automatic_retry_before_service_entry(cancellation_runtime):
    from app.api import pipeline
    from app.services.heavy_job_limiter import shared_heavy_job_limiter

    rt = cancellation_runtime
    limiter = shared_heavy_job_limiter
    assert limiter.acquire("exclusive", timeout=0)

    async def scenario():
        try:
            first = await ref.run_scheduled(rt.repo)
            # 等待工作线程进入容量排队，确保覆盖同步体开始前的真实取消路径。
            for _ in range(100):
                if rt.store.get(first["job_id"])["log"]:
                    break
                await asyncio.sleep(0.01)
            assert rt.store.get(first["job_id"])["log"]
            assert rt.store.get(first["job_id"])["status"] == "pending"
            pipeline.cancel_job(first["job_id"])
            await _drain_reference_jobs()
            assert rt.calls == []
            assert rt.store.get(first["job_id"])["outcome"] == "cancelled"
        finally:
            limiter.release("exclusive")
            rt.gate.set()
        rt.now[0] = rt.now[0].replace(hour=17)
        automatic = await ref.run_scheduled(rt.repo)
        await _drain_reference_jobs()
        assert automatic is None
        assert rt.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("key, error, finished_at, should_submit", [
    ("etf_reference", "用户手动取消", "2026-10-27T23:30:00Z", False),  # 北京当天，UTC前一天
    ("etf_reference", "用户手动取消", "2026-10-27T15:59:00Z", True),  # 北京前一天
    ("pipeline", "用户手动取消", "2026-10-28T08:00:00Z", True),
    ("etf_reference", "排队等待重任务执行槽超时", "2026-10-28T08:00:00Z", True),
])
def test_cancel_guard_uses_task_identity_outcome_and_beijing_date(
    tmp_path, runtime, monkeypatch, key, error, finished_at, should_submit,
):
    from unittest.mock import AsyncMock

    from app.api import indices
    from app.jobs import daily_pipeline
    from app.services import pipeline_jobs

    store = pipeline_jobs.job_store
    monkeypatch.setattr(pipeline_jobs, "_now_iso", lambda: finished_at)
    job_id, _ = store.create(timeout_s=1200, request_key=key)
    store.fail(job_id, error)
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: SimpleNamespace(quote_service=None))
    submit = AsyncMock(return_value={"job_id": "new"})
    monkeypatch.setattr(indices, "submit_etf_reference", submit)
    result = asyncio.run(ref.run_scheduled(SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))))
    assert result == ({"job_id": "new"} if should_submit else None)
    assert submit.await_count == int(should_submit)
