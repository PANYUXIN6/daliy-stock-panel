"""ETF看板：金额口径、缺失边界、目录分类、授权与采集生命周期。"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth
from app.api import etf_flow as endpoint
from app.data_providers.etf_public import EtfPublicSource, category_from_name
from app.market_time import CN_TZ
from app.services import etf_flow as flow
from app.services import etf_flow_sync as sync
from app.services import index_sync, kline_sync, ordinary_minute, trading_day
from app.services.auth import AuthStore
from app.services.auth_access import install_auth_middleware
from app.services.collector_proxy import collector_owns
from app.services.etf_flow_universe import FOCUS_SYMBOLS
from app.services.pipeline_jobs import JobCancelledError
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository

DAYS = ["2026-10-30", "2026-11-02", "2026-11-03"]
SYMBOL = FOCUS_SYMBOLS[0]


def fund(symbol=SYMBOL, category="沪深300", list_date="2012-05-28"):
    return {"symbol": symbol, "name": symbol, "category": category, "list_date": list_date}


def values(field, symbols=(SYMBOL,), numbers=(100, 120, 120)):
    return [{"symbol": symbol, "date": day, field: value}
            for symbol in symbols for day, value in zip(DAYS, numbers, strict=True)]


def test_flow_uses_previous_trading_session_and_unit_nav_in_yuan():
    rows = flow.build_daily([fund()], DAYS, values("shares"), values("unit_nav", numbers=(7, 2, 2)), [])
    assert rows[0]["net_flow_yuan"] == 40  # Friday -> Monday, shares × yuan/share.
    assert rows[1]["net_flow_yuan"] == 0  # Known zero differs from missing.
    assert rows[0]["amount_yuan"] is None
    assert rows[0]["categories"]["沪深300"]["covered"] == 1
    assert rows[0]["categories"]["中证1000"]["net_flow_yuan"] is None


@pytest.mark.parametrize("missing", ["previous_shares", "current_shares", "nav", "invalid_nav"])
def test_missing_input_does_not_become_zero_or_bridge_a_gap(missing):
    shares, nav = values("shares"), values("unit_nav", numbers=(2, 2, 2))
    if missing == "previous_shares":
        shares.pop(0)
    elif missing == "current_shares":
        shares.pop(1)
    elif missing == "nav":
        nav.pop(1)
    else:
        nav[1]["unit_nav"] = float("nan")
    rows = flow.build_daily([fund()], DAYS, shares, nav, [])
    assert rows[0]["net_flow_yuan"] is None
    assert rows[0]["available_flow_yuan"] is None
    if missing == "current_shares":
        assert rows[1]["net_flow_yuan"] is None


def test_partial_category_and_new_listing_never_imply_zero_shares():
    other = FOCUS_SYMBOLS[1]
    rows = flow.build_daily([fund(), fund(other, list_date=DAYS[1])], DAYS,
                           values("shares"), values("unit_nav", numbers=(2, 2, 2)), [])
    assert rows[0]["net_flow_yuan"] is None
    assert rows[0]["available_flow_yuan"] == 40
    assert (rows[0]["covered"], rows[0]["expected"]) == (1, 2)
    with pytest.raises(ValueError, match="重复"):
        flow.build_daily([fund()], DAYS, values("shares") * 2, [], [])


@pytest.mark.parametrize(("change", "expected"), [(5e9, False), (5e9 + 1, True), (-5e9, False)])
def test_signal_threshold_is_strictly_greater_than_100_yi(change, expected):
    rows = flow.build_daily([fund()], DAYS, values("shares", numbers=(1e10, 1e10 + change, 1e10)),
                           values("unit_nav", numbers=(2, 2, 2)), [])
    assert rows[0]["signal"] is expected


def minutes(day=DAYS[1], missing=None):
    return pl.DataFrame([{"symbol": symbol, "datetime": datetime.fromisoformat(f"{day}T{minute}:00"),
                          "close": 2.2, "amount": 100.0}
                         for symbol in FOCUS_SYMBOLS for minute in ("09:30", "09:31", "09:32")
                         if (symbol, minute) != missing])


def test_minute_amount_and_cumulative_stop_at_missing_bars():
    profile = flow.minute_profile(minutes(missing=(SYMBOL, "09:31")), pl.DataFrame(), DAYS[1], {SYMBOL: 2})
    first, missing, next_ = profile["points"][:3]
    assert first["amount_yuan"] == 2000
    assert missing["amount_yuan"] is None and missing["covered"] == 19
    assert next_["amount_yuan"] == 2000 and next_["cumulative_yuan"] is None
    broken = next(row for row in profile["ranking"] if row["symbol"] == SYMBOL)
    assert broken["amount_yuan"] is None
    assert broken["change_pct"] == pytest.approx(.1)
    assert all(row["weight"] is None for row in profile["ranking"])
    good = flow.minute_profile(minutes(), pl.DataFrame(), DAYS[1])
    average = flow.average_profiles([profile, good])
    assert average[1]["samples"] == 1 and average[1]["amount_yuan"] == 2000
    assert average[2]["samples"] == 2 and average[2]["cumulative_samples"] == 1
    assert average[3]["amount_yuan"] is None


def test_amount_reader_skips_vwap_adjustment_projection(tmp_path):
    from app.services.minute_adjust import mark_minute_basis_raw

    mark_minute_basis_raw(tmp_path)
    minute_dir = tmp_path / "kline_etf_minute" / f"date={DAYS[1]}"
    minute_dir.mkdir(parents=True)
    minutes().write_parquet(minute_dir / "part.parquet")
    factors = tmp_path / "adj_factor_etf"
    factors.mkdir()
    pl.DataFrame({"symbol": [SYMBOL], "trade_date": [date(2026, 11, 3)], "ex_factor": [2.0]}).write_parquet(factors / "all.parquet")
    repo = KlineRepository(DataStore(tmp_path))
    assert repo.get_minute_batch([SYMBOL], date(2026, 11, 2), "etf")["amount"][0] == 50
    assert repo.get_minute_batch([SYMBOL], date(2026, 11, 2), "etf", adjust=False)["amount"][0] == 100


@pytest.mark.parametrize(("name", "expected"), [
    ("中证A500ETF国泰", "中证A500"), ("创业板50ETF华安", "创业板50"),
    ("创业板ETF易方达", "创业板指"), ("沪深300医药ETF", None),
    ("中证500ETF增强", None), ("中证A500红利低波ETF", None), ("纳指100ETF", None),
])
def test_official_name_classification_is_exact(name, expected):
    assert category_from_name(name) == expected


def test_catalog_pagination_index_names_and_listing_dates(monkeypatch):
    source = EtfPublicSource()
    source.client.close()
    monkeypatch.setattr("app.data_providers.csi300_reference.time.sleep", lambda _: None)

    def respond(request):
        if request.url.host == "query.sse.com.cn":
            return httpx.Response(200, json={"pageHelp": {"total": 2}, "result": [
                {"FUND_CODE": "588000", "FUND_EXPANSION_ABBR": "科创50ETF华夏", "INDEX_NAME": "上证科创板50成份指数", "LISTING_DATE": "2020-11-16"},
                {"FUND_CODE": "510001", "FUND_EXPANSION_ABBR": "300医药ETF", "INDEX_NAME": "沪深300医药卫生指数", "LISTING_DATE": "2020-11-16"},
            ]})
        assert request.headers["referer"].startswith("https://fund.szse.cn/")
        page = int(request.url.params["PAGENO"])
        code = "159915" if page == 1 else "159949"
        return httpx.Response(200, json=[{"metadata": {"pagecount": 2, "recordcount": 2}, "data": [
            {"sys_key": f"<a>{code}</a>", "jjjcurl": "<a>创业板ETF易方达</a>" if page == 1 else "创业板50ETF华安",
             "jjlb": "ETF", "ssrq": "2010-01-01"},
        ]}])

    source.client = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        rows = source.catalog()
        assert {r["symbol"]: r["category"] for r in rows} == {
            "588000.SH": "科创50", "159915.SZ": "创业板指", "159949.SZ": "创业板50"}
    finally:
        source.close()


def test_board_get_reads_local_snapshot_and_no_future_comparison_samples(tmp_path, monkeypatch):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_minute_batch=lambda *a, **k: pl.DataFrame(),
                           get_minute=lambda *a, **k: pl.DataFrame())
    path = flow.snapshot_path(tmp_path)
    path.parent.mkdir()
    profiles = {day: flow.minute_profile(minutes(day), pl.DataFrame(), day) for day in DAYS}
    path.write_text(json.dumps({"sessions": DAYS, "profiles": profiles, "daily": []}))
    monkeypatch.setattr(trading_day, "calendar_status", lambda _: {"days": DAYS, "end": DAYS[-1]})
    monkeypatch.setattr(EtfPublicSource, "catalog", lambda _: pytest.fail("GET must not fetch"))
    first = flow.read_board(repo, date(2026, 11, 2))
    assert first["previous"]["date"] == DAYS[0]
    assert first["coverage"]["minute_days"] == 1
    assert first["average"][0]["samples"] == 1
    assert first["intraday"]["amount_yuan"] is None
    path.write_text(json.dumps({"sessions": DAYS, "profiles": {}, "daily": []}))
    assert flow.read_board(repo)["coverage"]["minute_days"] == 0


@pytest.fixture
def sync_env(tmp_path, monkeypatch):
    monkeypatch.setattr(sync, "cn_now", lambda: datetime(2026, 11, 3, 21, tzinfo=CN_TZ))
    monkeypatch.setattr(trading_day, "calendar_status", lambda _: {"days": DAYS, "end": DAYS[-1]})
    monkeypatch.setattr(ordinary_minute, "minute_allowed", lambda _: False)
    monkeypatch.setattr(sync.preferences, "get_fund_nav_provider", lambda: "test-nav")
    monkeypatch.setattr(sync.preferences, "get_daily_data_provider", lambda: "test-daily")
    monkeypatch.setattr(sync.preferences, "get_minute_data_provider", lambda: "test-minute")
    source = SimpleNamespace(catalog=lambda: [fund(symbol) for symbol in FOCUS_SYMBOLS],
                             sse_shares=lambda day, symbols, **k: [r for r in values("shares", symbols) if r["date"] == str(day)],
                             szse_shares=lambda start, end, symbol: values("shares", (symbol,)))
    nav = SimpleNamespace(get_fund_nav=lambda symbols, start, end: pl.DataFrame(values("unit_nav", symbols, (2, 2, 2)))
                          .with_columns(pl.col("date").str.to_date()))
    daily = SimpleNamespace(get_daily=lambda *args: pl.DataFrame())
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), refresh_index_views=lambda: None)
    return repo, source, nav, daily


def test_sync_persists_partial_results_and_refreshes_snapshot(sync_env):
    repo, source, nav, daily = sync_env
    result = sync.sync_board(repo, None, source=source, nav_provider=nav, daily_provider=daily)
    assert result["flow_as_of"] == DAYS[-1]
    snapshot = flow.read_snapshot(repo.store.data_dir)
    assert snapshot["daily"][0]["net_flow_yuan"] == 800
    assert "分钟数据能力不可用" in result["stage_errors"]
    assert len(flow.enabled_symbols(repo.store.data_dir)) == 20


@pytest.mark.parametrize("factor_state", ["updated", "failed", "unavailable"])
def test_sync_prepares_factors_before_publishing_shared_etf_prices(sync_env, monkeypatch, factor_state):
    old_repo, source, nav, daily = sync_env
    data_dir = old_repo.store.data_dir
    repo = KlineRepository(DataStore(data_dir))

    def bar(day, close):
        return pl.DataFrame({"symbol": [SYMBOL], "date": [date.fromisoformat(day)],
                             "open": [close], "high": [close], "low": [close], "close": [close],
                             "volume": [1000.0], "amount": [close * 1000]})

    index_sync.persist_etf_daily(bar(DAYS[-2], 10.0), repo)
    previous_path = data_dir / "kline_etf_enriched" / f"date={DAYS[-2]}" / "part.parquet"
    previous = previous_path.read_bytes()
    daily.get_daily = lambda symbols, start, end, asset: bar(DAYS[-1], 8.0) if asset == "etf" else pl.DataFrame()
    monkeypatch.setattr(kline_sync.preferences, "get_adj_factor_provider", lambda: "tickflow")
    monkeypatch.setattr(kline_sync, "sleep_between_batches", lambda *a: None)

    def factors(symbols, **kwargs):
        assert symbols == [SYMBOL]
        # Shared history can start before this request's daily window.
        assert "start_time" not in kwargs
        if factor_state == "failed":
            raise RuntimeError("upstream unavailable")
        return {SYMBOL: [{"trade_date": date.fromisoformat(DAYS[-1]), "ex_factor": 1.25}]}

    monkeypatch.setattr(kline_sync, "get_client", lambda: SimpleNamespace(klines=SimpleNamespace(ex_factors=factors)))
    caps = CapabilitySet({Cap.ADJ_FACTOR: CapabilityLimits(batch=50, rpm=30)} if factor_state != "unavailable" else {})
    result = sync.sync_board(repo, caps, source=source, nav_provider=nav, daily_provider=daily)
    new_path = data_dir / "kline_etf_enriched" / f"date={DAYS[-1]}" / "part.parquet"
    if factor_state == "updated":
        assert pl.read_parquet(previous_path)["close"][0] == pytest.approx(8.0)
        assert pl.read_parquet(previous_path)["raw_close"][0] == pytest.approx(10.0)
        assert pl.read_parquet(new_path)["close"][0] == pytest.approx(8.0)
    else:
        assert "ETF除权因子" in result["stage_errors"]
        assert previous_path.read_bytes() == previous
        assert not new_path.exists()
    # The board's turnover remains based on the independently fetched raw daily bar.
    prices = flow.read_snapshot(data_dir)["prices"]
    assert next(r["amount"] for r in prices if r["symbol"] == SYMBOL and r["date"] == DAYS[-1]) == 8000.0


@pytest.mark.parametrize("full_day", [False, True])
def test_postmarket_minutes_require_full_day_before_stopping_retries(sync_env, monkeypatch, full_day):
    from app.jobs import daily_pipeline
    from app.services import pipeline_jobs

    repo, source, nav, daily = sync_env
    path = flow.snapshot_path(repo.store.data_dir)
    path.parent.mkdir()
    path.write_text(json.dumps({"prices": [
        {"symbol": symbol, "date": day, "amount": 10000.0, "close": 2.0}
        for symbol in FOCUS_SYMBOLS for day in DAYS]}))
    times = flow.MINUTE_TIMES if full_day else ["09:30", "09:31", "09:32"]
    repo.get_minute_batch = lambda symbols, day, **kwargs: pl.DataFrame([
        {"symbol": symbol, "datetime": datetime.fromisoformat(f"{day}T{minute}:00"), "close": 2.0, "amount": 100.0}
        for symbol in symbols for minute in times])
    repo.get_minute = lambda *a, **k: pl.DataFrame()
    monkeypatch.setattr(ordinary_minute, "minute_allowed", lambda _: True)
    monkeypatch.setattr(ordinary_minute, "collect_minutes", lambda *a, **k: 0)
    result = sync.sync_board(repo, None, source=source, nav_provider=nav, daily_provider=daily)
    profile = flow.read_snapshot(repo.store.data_dir)["profiles"][DAYS[-1]]
    assert profile["covered"] == 20
    assert profile["complete"] is full_day
    status = json.loads(path.with_name("etf_flow_status.json").read_text())
    assert status["state"] == ("ready" if full_day else "partial")

    monkeypatch.setattr(trading_day, "is_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: SimpleNamespace(capabilities=None))
    monkeypatch.setattr(pipeline_jobs, "job_store", SimpleNamespace(list_recent=lambda **_: [
        {"request_key": "etf_flow", "status": "succeeded", "finished_at": sync.cn_now().isoformat(), "result": result}]))
    submitted = AsyncMock(return_value={"job_id": "retry"})
    monkeypatch.setattr(sync, "submit", submitted)
    assert asyncio.run(sync.run_scheduled(repo)) == (None if full_day else {"job_id": "retry"})
    assert submitted.await_count == (0 if full_day else 1)


@pytest.mark.parametrize("refresh_succeeds", [True, False])
def test_sync_refreshes_expired_calendar_before_selecting_dates(sync_env, monkeypatch, refresh_succeeds):
    repo, source, nav, daily = sync_env
    calendar = {"days": DAYS[:-1], "end": DAYS[-2]}
    monkeypatch.setattr(trading_day, "calendar_status", lambda _: calendar)

    def refresh(_):
        if not refresh_succeeds:
            raise ValueError("upstream unavailable")
        calendar.update(days=DAYS, end=DAYS[-1])

    monkeypatch.setattr(trading_day, "sync_calendar", refresh)
    result = sync.sync_board(repo, None, source=source, nav_provider=nav, daily_provider=daily)
    assert result["flow_as_of"] == (DAYS[-1] if refresh_succeeds else DAYS[-2])
    assert ("交易日历未覆盖今天，仅更新已确认日期" in result["stage_errors"]) is not refresh_succeeds


def test_cancel_preserves_snapshot_and_supplier_errors_are_sanitized(sync_env):
    repo, source, nav, daily = sync_env
    sync.sync_board(repo, None, source=source, nav_provider=nav, daily_provider=daily)
    path = flow.snapshot_path(repo.store.data_dir)
    previous = path.read_bytes()

    def cancel():
        raise JobCancelledError("etf_flow")

    with pytest.raises(JobCancelledError):
        sync.sync_board(repo, None, source=source, nav_provider=nav, daily_provider=daily, check_cancel=cancel)
    assert path.read_bytes() == previous

    def fail(*_):
        raise ValueError("secret-credential")

    nav.get_fund_nav = fail
    sync.sync_board(repo, None, source=source, nav_provider=nav, daily_provider=daily)
    status = path.with_name("etf_flow_status.json").read_text()
    assert "secret-credential" not in status and "单位净值" in status
    assert flow.read_snapshot(repo.store.data_dir)["daily"][0]["net_flow_yuan"] == 800


def test_api_auth_validation_and_collector_ownership(tmp_path, monkeypatch):
    app = FastAPI()
    app.state.auth_store = store = AuthStore(tmp_path)
    store.bootstrap("admin", "initial-secret")
    store.create_user("guest", "guest-secret", "guest", None)
    token, _ = store.login("guest", "guest-secret", "local", "")
    token, _ = store.change_password(token, "guest-secret", "guest-permanent-secret")
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
                                    get_minute_batch=lambda *a, **k: pl.DataFrame(), get_minute=lambda *a, **k: pl.DataFrame())
    app.state.capabilities = None
    app.include_router(auth.router)
    app.include_router(endpoint.router)
    install_auth_middleware(app)
    submitted = AsyncMock(return_value={"job_id": "job", "reused": False})
    monkeypatch.setattr(sync, "submit", submitted)
    with TestClient(app) as client:
        assert client.get("/api/etf-flow").status_code == 401
        client.cookies.set(auth.COOKIE_NAME, token)
        assert client.get("/api/etf-flow").json()["state"] == "no_data"
        assert client.post("/api/etf-flow/sync").status_code == 403
        client.cookies.clear()
        assert client.post("/api/auth/login", json={"username": "admin", "password": "initial-secret"}).status_code == 200
        assert client.post("/api/etf-flow/sync?days=121").status_code == 422
        assert client.get("/api/etf-flow?trade_date=2999-01-01").status_code == 422
        assert client.post("/api/etf-flow/sync?days=22").json()["job_id"] == "job"
        assert client.post("/api/etf-flow/sync?full_history=true").json()["job_id"] == "job"
        assert submitted.call_args.kwargs["full_history"] is True
        assert client.get("/api/etf-flow?window=0").status_code == 200
        assert client.get("/api/etf-flow?window=1").status_code == 422
        assert submitted.await_count == 2
    assert collector_owns("POST", "/api/etf-flow/sync")
    assert not collector_owns("GET", "/api/etf-flow")


def test_scheduler_activation_and_today_cancel(tmp_path, monkeypatch):
    from app.jobs import daily_pipeline
    from app.services import pipeline_jobs

    now = datetime(2026, 11, 3, 21, tzinfo=CN_TZ)
    monkeypatch.setattr(sync, "cn_now", lambda: now)
    monkeypatch.setattr(trading_day, "is_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(daily_pipeline, "_get_app_state", lambda: SimpleNamespace(capabilities=None))
    monkeypatch.setattr(pipeline_jobs, "job_store", SimpleNamespace(list_recent=lambda **_: []))
    submitted = AsyncMock(return_value={"job_id": "new"})
    monkeypatch.setattr(sync, "submit", submitted)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    assert asyncio.run(sync.run_scheduled(repo)) is None
    path = flow.snapshot_path(tmp_path)
    path.parent.mkdir()
    path.write_text("{}")
    assert asyncio.run(sync.run_scheduled(repo)) == {"job_id": "new"}
    # Legacy successful jobs did not establish all-day minute completeness.
    monkeypatch.setattr(pipeline_jobs, "job_store", SimpleNamespace(list_recent=lambda **_: [
        {"request_key": "etf_flow", "status": "succeeded", "finished_at": now.isoformat(),
         "result": {"flow_as_of": DAYS[-1], "stage_errors": []}}]))
    assert asyncio.run(sync.run_scheduled(repo)) == {"job_id": "new"}
    monkeypatch.setattr(pipeline_jobs, "job_store", SimpleNamespace(list_recent=lambda **_: [
        {"request_key": "etf_flow", "outcome": "cancelled", "finished_at": now.isoformat()}]))
    assert asyncio.run(sync.run_scheduled(repo)) is None
    assert submitted.await_count == 2


def test_history_keeps_daily_since_2024_and_reports_only_observed_minutes(sync_env):
    repo, source, nav, daily = sync_env
    source.trading_days = lambda *a: [date(2023, 12, 29),
                                     *(date(2024, 1, 2) + timedelta(weeks=i) for i in range(125)),
                                     *map(date.fromisoformat, DAYS)]
    sync.sync_board(repo, None, full_history=True, source=source, nav_provider=nav, daily_provider=daily)
    snapshot = flow.read_snapshot(repo.store.data_dir)
    assert len(snapshot['sessions']) > 121
    assert snapshot['sessions'][0] == '2023-12-29'
    assert snapshot['daily'][0]['date'] == '2024-01-02'
    assert snapshot['daily'][0]['net_flow_yuan'] is None
    # Recent updates retain the historical raw inputs and date range.
    sync.sync_board(repo, None, source=source, nav_provider=nav, daily_provider=daily)
    assert flow.read_snapshot(repo.store.data_dir)['daily'][0]['date'] == '2024-01-02'
    snapshot = {**snapshot, 'profiles': {DAYS[0]: flow.minute_profile(pl.DataFrame(), pl.DataFrame(), DAYS[0]),
                                      DAYS[1]: flow.minute_profile(minutes(), pl.DataFrame(), DAYS[1])}}
    history = flow.history_coverage(snapshot)
    assert history['daily']['missing_flow_dates'][0] == '2024-01-02'
    assert history['minute']['start'] == DAYS[1]
    assert history['minute']['complete_days'] == 0
    assert history['minute']['symbols'][0]['days'] == 1


def test_cancelled_history_resumes_saved_inputs(sync_env):
    repo, source, nav, daily = sync_env
    original = source.sse_shares
    calls = []

    def shares(day, symbols, **kwargs):
        calls.append(str(day))
        if str(day) == DAYS[1]:
            raise JobCancelledError('etf_flow')
        return original(day, symbols, **kwargs)

    source.sse_shares = shares
    with pytest.raises(JobCancelledError):
        sync.sync_board(repo, None, source=source, nav_provider=nav, daily_provider=daily)
    pending = flow.snapshot_path(repo.store.data_dir).with_name('etf_flow_work.json')
    assert any(r['date'] == DAYS[0] for r in json.loads(pending.read_text())['shares'])
    calls.clear()
    source.sse_shares = lambda day, symbols, **kwargs: (calls.append(str(day)) or original(day, symbols, **kwargs))
    sync.sync_board(repo, None, source=source, nav_provider=nav, daily_provider=daily)
    assert DAYS[0] not in calls
    assert not pending.exists()


def test_fund_nav_history_request_reaches_2024(monkeypatch):
    from app.plugins.fuyao.client import FuyaoClient
    from app.plugins.fuyao.provider import FuyaoProvider

    client = FuyaoClient("test")
    client.close()
    historical = int(datetime(2024, 1, 2, tzinfo=CN_TZ).timestamp() * 1000)

    def respond(request):
        assert request.url.params["nav_type"] == "unit"
        rows = ([{"nav_date": historical, "unit_nav": 2.5}]
                if request.url.params["range"] == "fyear" else [])
        return httpx.Response(200, json={"code": 0, "data": {"item": rows}})

    client._http = httpx.Client(base_url="https://example.test", transport=httpx.MockTransport(respond))
    provider = FuyaoProvider()
    monkeypatch.setattr(provider, "_get_client", lambda: client)
    monkeypatch.setattr("app.plugins.fuyao.client._wait_request_slot", lambda *a, **k: None)
    try:
        assert provider.get_fund_nav([SYMBOL], date(2024, 1, 2), date(2024, 1, 2)).to_dicts() == [
            {"symbol": SYMBOL, "date": date(2024, 1, 2), "unit_nav": 2.5}]
    finally:
        client.close()


def test_history_resume_does_not_refetch_complete_share_and_nav_inputs(sync_env):
    repo, source, nav, daily = sync_env
    sync.sync_board(repo, None, source=source, nav_provider=nav, daily_provider=daily)
    path = flow.snapshot_path(repo.store.data_dir)
    snapshot = json.loads(path.read_text())
    snapshot['history_calendar_start'] = DAYS[0]
    path.write_text(json.dumps(snapshot))

    def unexpected(*a, **k):
        pytest.fail('completed historical input must not be requested again')

    source.sse_shares = source.szse_shares = unexpected
    nav.get_fund_nav = unexpected
    result = sync.sync_board(repo, None, full_history=True, source=source, nav_provider=nav, daily_provider=daily)
    assert result['flow_as_of'] == DAYS[-1]
    assert flow.read_snapshot(repo.store.data_dir)['daily'][0]['net_flow_yuan'] == 800
