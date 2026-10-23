"""按需采集的并发、共享持久化、权限与显式任务准备契约。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import financials
from app.services import financial_demand as demand
from app.services import financial_sync, preferences, shared_runtime
from app.services.financial_versions import read_versions
from app.tickflow.capabilities import CapabilitySet


def records(symbol, profit=100.):
    return pl.DataFrame([
        {"symbol": symbol, "source": "fuyao", "period_end": "2026-06-30", "net_income": profit,
         "net_income_attributable": profit, "revenue": 1000., "operating_cost": 600.,
         "date_basis": "source_update", "source_update_date": "2026-08-20"},
        {"symbol": symbol, "source": "fuyao", "period_end": "2025-06-30", "net_income": 50.,
         "net_income_attributable": 50., "revenue": 800., "operating_cost": 500.,
         "date_basis": "source_update", "source_update_date": "2025-08-20"},
    ])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "fuyao")
    monkeypatch.setattr(demand, "supported_tables", lambda caps: ["income", "metrics", "balance_sheet", "cash_flow"])
    monkeypatch.setattr(shared_runtime, "publish_data_update", Mock())
    fetch = Mock(side_effect=lambda table, symbols, *args, **kwargs: records(symbols[0]))
    monkeypatch.setattr(financial_sync, "_fetch_table", fetch)
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    demand.register(repo, CapabilitySet())
    yield SimpleNamespace(root=tmp_path, fetch=fetch, repo=repo)
    demand.unregister(tmp_path)


def fetch(env, symbol="600000.SH", table="income", **kwargs):
    return demand.fetch_stock(env.root, CapabilitySet(), symbol, table, **kwargs)


def test_concurrent_same_stock_table_shares_request_and_survives_service_restart(env):
    entered, release = Event(), Event()

    def slow(table, symbols, *args, **kwargs):
        entered.set()
        assert release.wait(5)
        return records(symbols[0])
    env.fetch.side_effect = slow
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(fetch, env)
        assert entered.wait(5)
        second = pool.submit(fetch, env, " 600000.sh ")
        release.set()
        results = [first.result(5), second.result(5)]
    assert env.fetch.call_count == 1
    assert sum(not r["reused"] for r in results) == 1
    assert len(read_versions(env.root, "income")) == 2
    assert fetch(env)["reused"]
    assert env.fetch.call_count == 1
    assert pl.read_parquet(env.root / "financials/income/part.parquet")["symbol"].unique().to_list() == ["600000.SH"]


def test_concurrent_different_stocks_merge_without_lost_updates(env):
    barrier = Barrier(2)

    def both(table, symbols, *args, **kwargs):
        barrier.wait(timeout=5)
        return records(symbols[0])
    env.fetch.side_effect = both
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda symbol: fetch(env, symbol), ["600000.SH", "000001.SZ"]))
    assert all(r["rows"] == 2 for r in results)
    assert set(read_versions(env.root, "income")["symbol"]) == {"600000.SH", "000001.SZ"}
    assert len(pl.read_parquet(env.root / "financials/income/part.parquet")) == 4


def test_failure_is_retryable_and_does_not_erase_existing_or_mark_full_sync(env):
    fetch(env, "000001.SZ")
    path = env.root / "financials/income/part.parquet"
    before = path.read_bytes()
    env.fetch.side_effect = RuntimeError("upstream unavailable")
    with pytest.raises(RuntimeError):
        fetch(env)
    assert path.read_bytes() == before
    env.fetch.side_effect = lambda table, symbols, *args, **kwargs: records(symbols[0])
    assert fetch(env)["rows"] == 2
    assert not (env.root / "financials/sync_progress/income.json").exists()
    assert not (env.root / "financials/universe.json").exists()


@pytest.mark.parametrize("kind", ["foreign", "empty", "broken"])
def test_invalid_response_or_existing_corruption_never_overwrites_history(env, kind):
    fetch(env, "000001.SZ")
    path = env.root / "financials/income/part.parquet"
    if kind == "broken":
        path.write_bytes(b"broken parquet")
    before = path.read_bytes()
    env.fetch.reset_mock()
    env.fetch.side_effect = lambda *args, **kwargs: records("600999.SH") if kind == "foreign" else pl.DataFrame()
    with pytest.raises((ValueError, pl.exceptions.PolarsError)):
        fetch(env)
    assert path.read_bytes() == before
    if kind == "broken":
        env.fetch.assert_not_called()


def test_next_day_rechecks_data_but_identical_versions_keep_first_observation(env, monkeypatch):
    fetch(env)
    old = read_versions(env.root, "income")
    monkeypatch.setattr(demand, "cn_today", lambda: date(2099, 1, 1))
    assert not fetch(env)["reused"]
    assert env.fetch.call_count == 2
    assert read_versions(env.root, "income").equals(old)


def test_financial_write_invalidates_financial_and_storage_stats_only(env, monkeypatch):
    from app.api import data
    daily = {"rows": 5000}
    monkeypatch.setitem(data._table_cache, "daily", daily)
    monkeypatch.setitem(data._table_cache, "financials", {"rows": 0})
    monkeypatch.setattr(data, "_storage_cache", {"total": 0})
    fetch(env)
    assert data._table_cache["daily"] is daily
    assert data._table_cache["financials"] is None
    assert data._storage_cache is None


def test_table_and_provider_are_part_of_reuse_and_unsupported_shares_are_explicit(env):
    fetch(env)
    assert not fetch(env, table="balance_sheet")["reused"]
    assert env.fetch.call_count == 2
    with pytest.raises(ValueError, match="不支持"):
        fetch(env, table="shares")


def test_provider_change_does_not_reuse_other_source_receipt(env, monkeypatch):
    fetch(env)
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "another")
    env.fetch.side_effect = lambda table, symbols, *args, **kwargs: records(symbols[0]).with_columns(pl.lit("another").alias("source"))
    assert not fetch(env)["reused"]
    assert set(read_versions(env.root, "income")["source"]) == {"fuyao", "another"}


def test_cancelled_fetch_does_not_publish_a_late_result(env):
    from app.services.pipeline_jobs import JobCancelledError
    cancel = Event()

    def late(table, symbols, *args, **kwargs):
        cancel.set()
        return records(symbols[0])
    env.fetch.side_effect = late
    with pytest.raises(JobCancelledError):
        fetch(env, cancel_event=cancel)
    assert not (env.root / "financials/income/part.parquet").exists()


def test_revoked_authorization_before_commit_preserves_saved_data(env):
    fetch(env, "000001.SZ")
    path = env.root / "financials/income/part.parquet"
    before = path.read_bytes()
    check = Mock(side_effect=PermissionError("session revoked"))
    with pytest.raises(PermissionError):
        fetch(env, before_write=check)
    assert path.read_bytes() == before
    assert "600000.SH" not in read_versions(env.root, "income")["symbol"].to_list()


def test_factor_preparation_only_fetches_required_table_and_pool_then_reuses(env):
    frame = pl.DataFrame({"symbol": ["600000.SH", "600000.SH"], "date": [date(2026, 9, 1)] * 2})
    demand.prepare_factors(env.root, frame, ["momentum_20d"])
    env.fetch.assert_not_called()
    demand.prepare_factors(env.root, frame, ["gross_margin_latest", "net_income_yoy_latest"])
    assert env.fetch.call_count == 1
    assert env.fetch.call_args.args[:2] == ("income", ["600000.SH"])
    assert env.fetch.call_args.kwargs["latest_only"] is False
    demand.prepare_factors(env.root, frame, ["net_margin_latest"])
    assert env.fetch.call_count == 1


def test_warm_pool_reads_each_table_once_instead_of_per_symbol(env, monkeypatch):
    from app.services import financial_snapshot

    symbols = ['600000.SH', '000001.SZ', '000002.SZ']
    for symbol in symbols:
        fetch(env, symbol)
    financial_snapshot._cache.clear()
    read = Mock(wraps=pl.read_parquet)
    monkeypatch.setattr(pl, 'read_parquet', read)
    frame = pl.DataFrame({'symbol': symbols, 'date': [date(2026, 9, 1)] * 3})
    demand.prepare_tables(env.root, frame, ['income'])
    demand.prepare_tables(env.root, frame, ['income'])
    assert read.call_count == 2  # one current table and one versions table
    assert env.fetch.call_count == 3


def test_factor_preparation_fails_with_unavailable_symbols_instead_of_silent_ranking(env):
    env.fetch.side_effect = lambda *args, **kwargs: pl.DataFrame()
    frame = pl.DataFrame({"symbol": ["600000.SH"], "date": [date(2026, 9, 1)]})
    with pytest.raises(ValueError, match="准备未完成"):
        demand.prepare_factors(env.root, frame, ["net_margin_latest"])


def test_authenticated_guest_can_fetch_but_bulk_and_anonymous_stay_closed(env, monkeypatch):
    from app.config import settings
    from app.services.auth import AuthStore
    from app.services.auth_access import install_auth_middleware
    monkeypatch.setattr(settings, "data_dir", env.root)
    store = AuthStore(env.root)
    store.bootstrap("admin", "initial-password")
    store.create_user("guest", "temporary-password")
    token, _ = store.login("guest", "temporary-password", "", "")
    store.change_password(token, "temporary-password", "new-guest-password")
    token, _ = store.login("guest", "new-guest-password", "", "")
    app = FastAPI()
    app.state.repo = env.repo
    app.state.capabilities = CapabilitySet()
    app.include_router(financials.router)
    install_auth_middleware(app)
    with TestClient(app) as client:
        payload = {"symbol": "600000.SH", "table": "income"}
        assert client.post("/api/financials/fetch", json=payload).status_code == 401
        client.cookies.set("tf_session", token)
        response = client.post("/api/financials/fetch", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["rows"] == 2
        assert client.get("/api/financials/income", params={"symbol": "600000.SH"}).json()["data"]
        assert client.post("/api/financials/sync/income").status_code == 403
        assert client.post("/api/financials/fetch", json={**payload, "symbol": "../bad"}).status_code == 400
        assert client.post("/api/financials/fetch", json={**payload, "table": "all"}).status_code == 422
        assert client.post("/api/financials/fetch", json=payload, headers={"Origin": "https://foreign.test"}).status_code == 403
        admin_token, _ = store.login("admin", "initial-password", "", "")
        client.cookies.set("tf_session", admin_token)
        assert client.post("/api/financials/fetch", json=payload).json()["reused"] is True
        assert env.fetch.call_count == 1
        monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: True)
        assert client.post("/api/financials/sync/all").status_code == 410
