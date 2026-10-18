"""候选利润核验任务只采缺口股，遵循统一写入槽、会话和个人缓存边界。"""
from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from threading import Event, RLock
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest
from fastapi import HTTPException
from test_collector_data_jobs import jobs as jobs
from test_collector_data_jobs import terminal

from app.api import pipeline
from app.api import screener as api
from app.services import auth, financial_sync, preferences, shared_runtime, strategy_cache
from app.services.auth_access import admin_required
from app.services.financial_versions import read_versions, record_versions
from app.strategy import config as strategy_config
from app.strategy import market_data
from app.strategy.engine import StrategyEngine
from app.tickflow.capabilities import CapabilitySet

DAY = date(2026, 10, 16)
STRATEGY_ID = "earnings_breakout_gap"


class Repo:
    def __init__(self, root):
        self.store = SimpleNamespace(data_dir=root, _register_views=Mock())
        self._lock = RLock()
        self.refresh_cache = Mock()
        rows = []
        for symbol in ["600000.SH", "600001.SH", "600002.SH"]:
            for day in [date(2026, 10, 15), DAY]:
                gap = day == DAY and symbol != "600002.SH"
                rows.append({"symbol": symbol, "date": day, "name": symbol,
                             "open": 11.1 if gap else 10., "high": 12. if gap else 10.2,
                             "low": 11. if gap else 9.8, "close": 11.5 if gap else 10.,
                             "volume": 1000., "amount": 10000.})
        self.prices = pl.DataFrame(rows)

    def enriched_latest_date(self):
        return DAY

    def get_enriched_latest_asset(self, asset_type="stock", refresh=True):
        return self.prices.filter(pl.col("date") == DAY), DAY

    def get_instruments_asset(self, asset_type):
        return pl.DataFrame()

    def get_enriched_history(self, as_of, lookback_days):
        return self.prices.filter(pl.col("date") <= as_of).sort(["symbol", "date"]).group_by(
            "symbol", maintain_order=True,
        ).tail(lookback_days)

    def get_daily_batch(self, symbols, start, end, columns=None):
        prices = self.prices.filter(pl.col("symbol").is_in(symbols) & pl.col("date").is_between(start, end))
        return prices.select(columns) if columns else prices

    def get_financial_versions(self, table, symbol=None, columns=None):
        return read_versions(self.store.data_dir, table, columns, symbol=symbol)


@pytest.fixture
def env(tmp_path, monkeypatch, authenticated_actor, jobs):
    repo = Repo(tmp_path)
    folder = tmp_path / "calendar"
    folder.mkdir()
    (folder / "trading_days.json").write_text(json.dumps({
        "start": "2026-10-15", "end": "2026-10-16", "days": ["2026-10-15", "2026-10-16"],
    }))
    record_versions(tmp_path, "income", pl.DataFrame([
        {"symbol": "600000.SH", "period_end": "2025-09-30", "net_income": 50.,
         "announce_date": "2025-10-20", "source_update_date": "2025-10-20", "date_basis": "source_update"},
        {"symbol": "600000.SH", "period_end": "2026-09-30", "net_income": 100.,
         "announce_date": "2026-10-15", "source_update_date": "2026-10-15", "date_basis": "source_update"},
    ]), "fuyao", datetime.fromisoformat("2026-10-15T20:00:00+08:00"))
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "fuyao")
    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: True)
    monkeypatch.setattr(shared_runtime, "publish_data_update", lambda *args: None)
    monkeypatch.setattr(shared_runtime, "publish_quotes", lambda *args: None)
    engine = StrategyEngine(strategy_dirs=[Path(__file__).parents[1] / "app/strategy/builtin"])
    monkeypatch.setattr(market_data, "_repo", repo)
    runtime = SimpleNamespace(strategy_engine=engine,
                              monitor_engine=SimpleNamespace(latest_strategy_results=lambda: {}))
    state = SimpleNamespace(repo=repo, capabilities=CapabilitySet(), quote_service=None,
                            user_runtimes=SimpleNamespace(get=lambda owner: runtime))
    request = SimpleNamespace(app=SimpleNamespace(state=state))
    return SimpleNamespace(repo=repo, request=request, owner=authenticated_actor, jobs=jobs, root=tmp_path)


def verification(env):
    cached = strategy_cache.read_cache(env.root)
    return (cached or {}).get("results", {}).get(STRATEGY_ID, {})


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [
    {"basic_filter": {"enabled": True, "price_min": 100}},
    {"overlay_filter": [{"left": "close", "op": ">", "right": "100"}]},
    {"overlay_filter": [{"left": "close", "op": ">", "right": "11", "leftDays": 1}]},
])
async def test_verified_financial_evidence_still_obeys_saved_personal_filters(env, monkeypatch, overrides):
    strategy_config.save_override(env.root, STRATEGY_ID, overrides)
    monkeypatch.setattr(financial_sync, "sync_candidate_income", lambda *args, **kwargs: {"errors": {}})

    ordinary = api.run_preset(api.PresetRequest(strategy_id=STRATEGY_ID, as_of=DAY), env.request)
    assert ordinary["total"] == 0
    result = await api.verify_earnings_gap(api.EarningsGapVerifyRequest(as_of=DAY), env.request)
    assert (await terminal(env.jobs, result["job_id"]))["status"] == "succeeded"

    cached = strategy_cache.read_cache(env.root)
    selected = cached["results"][STRATEGY_ID]
    assert selected["verification"]["confirmed_count"] == 1
    assert selected["total"] == 0 and selected["rows"] == []
    assert cached["today_ever_matched"].get(STRATEGY_ID, []) == []
    assert cached["today_ever_rows"].get(STRATEGY_ID, {}) == {}


@pytest.mark.asyncio
async def test_verified_results_share_saved_scoring_order_and_display_limit(env, monkeypatch):
    versions = read_versions(env.root, "income")
    record_versions(env.root, "income", versions.with_columns(
        pl.lit("600001.SH").alias("symbol"),
    ), "fuyao", datetime.fromisoformat("2026-10-15T20:00:00+08:00"))
    env.repo.prices = env.repo.prices.with_columns(
        pl.when(pl.col("symbol") == "600001.SH").then(2000.).otherwise(pl.col("volume")).alias("volume"),
    )
    strategy_config.save_override(env.root, STRATEGY_ID, {
        "scoring": {"volume": 1.}, "scoring_directions": {"volume": "low"}, "display_limit": 1,
    })
    monkeypatch.setattr(financial_sync, "sync_candidate_income", lambda *args, **kwargs: {"errors": {}})
    ordinary = api.run_preset(api.PresetRequest(strategy_id=STRATEGY_ID, as_of=DAY), env.request)
    assert [(row["symbol"], row["score"]) for row in ordinary["rows"]] == [("600001.SH", 0.)]

    result = await api.verify_earnings_gap(api.EarningsGapVerifyRequest(as_of=DAY), env.request)
    assert (await terminal(env.jobs, result["job_id"]))["status"] == "succeeded"
    cached = strategy_cache.read_cache(env.root)
    selected = cached["results"][STRATEGY_ID]
    assert selected["verification"]["confirmed_count"] == 2
    assert selected["total"] == ordinary["total"] == 1
    assert [(row["symbol"], row["score"]) for row in selected["rows"]] == [("600001.SH", 0.)]
    assert selected["rows"][0]["verification_status"] == "confirmed"
    assert cached["today_ever_matched"][STRATEGY_ID] == ["600001.SH"]


@pytest.mark.asyncio
async def test_only_price_gap_candidates_are_updated_and_only_confirmed_enter_results(env, monkeypatch):
    calls = []

    def sync(symbols, root, capset, on_progress=None, *, cancel_event=None, before_write=None):
        calls.append(list(symbols))
        assert root == env.root and before_write is not None
        before_write()
        if on_progress:
            on_progress(len(symbols), len(symbols))
        return {"requested": len(symbols), "updated_symbols": list(symbols), "errors": {}}
    monkeypatch.setattr(financial_sync, "sync_candidate_income", sync)

    result = await api.verify_earnings_gap(api.EarningsGapVerifyRequest(as_of=DAY), env.request)
    job = await terminal(env.jobs, result["job_id"])
    assert job["status"] == "succeeded"
    assert job["request_key"].startswith(f"financial:earnings-gap:{env.owner.id}:{DAY}:")
    assert len(calls) == 1 and set(calls[0]) == {"600000.SH", "600001.SH"}
    cached = strategy_cache.read_cache(env.root)
    selected = cached["results"][STRATEGY_ID]
    assert selected["total"] == 1
    assert [row["symbol"] for row in selected["rows"]] == ["600000.SH"]
    details = selected["verification"]
    assert details["status"] == "completed" and details["candidate_count"] == 2
    assert details["confirmed_count"] == 1 and details["pending_count"] == 1
    assert cached["today_ever_matched"][STRATEGY_ID] == ["600000.SH"]
    assert "600001.SH" not in cached["today_ever_rows"][STRATEGY_ID]
    response = api.get_cached_result(STRATEGY_ID, env.request, ext_columns=None)
    assert response["result"]["verification"] == details
    brief = api.get_cached_summary(env.request)["results"][STRATEGY_ID]["verification"]
    assert brief["pending_count"] == 1 and "candidates" not in brief
    assert len(calls) == 1  # 读取缓存明细与摘要不触发采集。


@pytest.mark.asyncio
async def test_candidate_collection_obeys_requested_stock_pool(env, monkeypatch):
    calls = []

    def sync(symbols, root, capset, on_progress=None, *, cancel_event=None, before_write=None):
        calls.append(list(symbols))
        before_write()
        return {"requested": len(symbols), "updated_symbols": list(symbols), "errors": {}}
    monkeypatch.setattr(financial_sync, "sync_candidate_income", sync)
    req = api.EarningsGapVerifyRequest(as_of=DAY, pool=["600001.SH", "600002.SH"])
    result = await api.verify_earnings_gap(req, env.request)
    assert (await terminal(env.jobs, result["job_id"]))["status"] == "succeeded"
    assert calls == [["600001.SH"]]
    payload = verification(env)
    assert payload["total"] == 0 and payload["verification"]["candidate_count"] == 1


@pytest.mark.asyncio
async def test_no_price_gap_does_not_call_financial_provider(env, monkeypatch):
    env.repo.prices = env.repo.prices.with_columns(pl.lit(9.8).alias("low"))
    sync = Mock()
    monkeypatch.setattr(financial_sync, "sync_candidate_income", sync)
    result = await api.verify_earnings_gap(api.EarningsGapVerifyRequest(), env.request)
    if result.get("job_id"):
        assert (await terminal(env.jobs, result["job_id"]))["status"] == "succeeded"
    sync.assert_not_called()
    assert verification(env)["verification"]["candidate_count"] == 0
    assert verification(env)["total"] == 0


@pytest.mark.asyncio
async def test_same_verification_reuses_running_job_and_does_not_pause_quotes(env, monkeypatch):
    entered, release = Event(), Event()
    calls = []
    quotes = SimpleNamespace(paused=Mock(side_effect=AssertionError("财务核验不能暂停实时行情")))
    env.request.app.state.quote_service = quotes

    def sync(symbols, root, capset, on_progress=None, *, cancel_event=None, before_write=None):
        calls.append(list(symbols))
        entered.set()
        assert release.wait(3)
        before_write()
        return {"requested": len(symbols), "updated_symbols": list(symbols), "errors": {}}
    monkeypatch.setattr(financial_sync, "sync_candidate_income", sync)
    req = api.EarningsGapVerifyRequest(as_of=DAY)
    result = await api.verify_earnings_gap(req, env.request)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        assert verification(env)["verification"]["status"] == "running"
        duplicate = await api.verify_earnings_gap(req, env.request)
        assert duplicate["job_id"] == result["job_id"] and duplicate["reused"] is True
    finally:
        release.set()
        await asyncio.gather(*list(pipeline._tasks))
    assert (await terminal(env.jobs, result["job_id"]))["status"] == "succeeded"
    assert len(calls) == 1
    quotes.paused.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "revoke", "demote"])
async def test_cancelled_or_revoked_verification_cannot_publish_late_financial_write(env, monkeypatch, stop):
    entered, release = Event(), Event()
    written = []
    store = auth.get_store(env.root)
    if stop == "demote":
        backup = store.create_user("backupadmin", "backup-initial-password", "admin", actor_id=env.owner.id)
        backup_token, _ = store.login("backupadmin", "backup-initial-password", "test", "")
        store.change_password(backup_token, "backup-initial-password", "backup-permanent-password")

    def sync(symbols, root, capset, on_progress=None, *, cancel_event=None, before_write=None):
        entered.set()
        assert release.wait(3)
        before_write()
        if on_progress:
            on_progress(0, len(symbols))
        written.extend(symbols)
        return {"requested": len(symbols), "updated_symbols": list(symbols), "errors": {}}
    monkeypatch.setattr(financial_sync, "sync_candidate_income", sync)
    result = await api.verify_earnings_gap(api.EarningsGapVerifyRequest(as_of=DAY), env.request)
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        if stop == "cancel":
            env.jobs.terminate(result["job_id"], "用户取消")
        elif stop == "revoke":
            store.revoke_session(env.owner.session_token)
        else:
            store.update_user(env.owner.id, {"role": "guest"}, actor_id=backup["id"])
    finally:
        release.set()
        await asyncio.gather(*list(pipeline._tasks))
    assert (await terminal(env.jobs, result["job_id"]))["status"] == "failed"
    assert written == []
    assert verification(env)["verification"]["status"] != "completed"


@pytest.mark.asyncio
async def test_historical_date_is_rejected_before_collection(env, monkeypatch):
    sync = Mock()
    monkeypatch.setattr(financial_sync, "sync_candidate_income", sync)
    with pytest.raises(HTTPException) as error:
        await api.verify_earnings_gap(api.EarningsGapVerifyRequest(as_of=date(2026, 10, 15)), env.request)
    assert error.value.status_code == 400
    sync.assert_not_called()
    assert env.jobs.active_id() is None


@pytest.mark.asyncio
async def test_other_financial_provider_is_rejected_before_collection(env, monkeypatch):
    sync = Mock()
    monkeypatch.setattr(financial_sync, "sync_candidate_income", sync)
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "tickflow")
    with pytest.raises(HTTPException) as error:
        await api.verify_earnings_gap(api.EarningsGapVerifyRequest(as_of=DAY), env.request)
    assert error.value.status_code == 400
    sync.assert_not_called()
    assert env.jobs.active_id() is None


def test_verification_route_requires_administrator():
    assert admin_required("/api/screener/earnings-gap/verify", "POST")
