from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import screener as screener_api
from app.api import strategy as strategy_api
from app.services import shared_runtime, strategy_cache
from app.services.collector_proxy import proxy_middleware
from app.services.events import EventBus
from app.strategy import config as strategy_config
from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine
from app.strategy.monitor import MonitorRuleEngine

pytestmark = pytest.mark.usefixtures("private_owner")



def _make_engine() -> tuple[StrategyEngine, StrategyDataContext]:
    df = pl.DataFrame({"symbol": ["A", "B", "C"], "value": [1, 2, 3]})
    engine = StrategyEngine(strategy_dirs=[])
    engine._strategies["saved_params"] = StrategyDef(
        meta={"id": "saved_params", "scoring": {}, "limit": 100},
        basic_filter={"enabled": False},
        entry_signals=[],
        exit_signals=[],
        stop_loss=None,
        trailing_stop=None,
        trailing_take_profit_activate=None,
        trailing_take_profit_drawdown=None,
        max_hold_days=None,
        filter_fn=lambda _df, params: pl.col("value") >= params.get("min_value", 1),
        filter_history_fn=None,
        lookback_days=1,
        source="custom",
    )
    return engine, StrategyDataContext(
        asset_type="stock",
        timeframe="1d",
        as_of=date(2026, 7, 15),
        current=df,
    )


def test_run_applies_saved_strategy_params():
    engine, context = _make_engine()
    result = engine.run(
        "saved_params",
        context,
        overrides={"params": {"min_value": 2}},
    )

    assert [row["symbol"] for row in result.rows] == ["B", "C"]


def test_explicit_params_override_saved_strategy_params():
    engine, context = _make_engine()
    result = engine.run(
        "saved_params",
        context,
        params={"min_value": 3},
        overrides={"params": {"min_value": 2}},
    )

    assert [row["symbol"] for row in result.rows] == ["C"]


def test_patch_config_preserves_other_user_overrides(tmp_path):
    engine, _ = _make_engine()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        strategy_engine=engine,
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))
    request.app.state.user_runtimes = SimpleNamespace(get=lambda _: request.app.state)
    strategy_config.save_override(tmp_path, "saved_params", {
        "params": {"min_value": 2},
        "stop_loss": -0.05,
    })

    strategy_api.patch_config(strategy_api.SaveConfigRequest(
        strategy_id="saved_params",
        overrides={
            "scoring": {"rsi_14": 1.0},
            "scoring_directions": {"rsi_14": "low"},
            "scoring_replace": True,
        },
    ), request)

    saved = strategy_config.load_override(tmp_path, "saved_params")
    assert saved["params"] == {"min_value": 2}
    assert saved["stop_loss"] == -0.05
    assert saved["scoring"] == {"rsi_14": 1.0}
    assert saved["scoring_directions"] == {"rsi_14": "low"}


def test_save_config_rejects_invalid_scoring_direction(tmp_path):
    engine, _ = _make_engine()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        strategy_engine=engine,
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))
    request.app.state.user_runtimes = SimpleNamespace(get=lambda _: request.app.state)

    with pytest.raises(HTTPException, match="方向无效"):
        strategy_api.save_config(strategy_api.SaveConfigRequest(
            strategy_id="saved_params",
            overrides={"scoring_directions": {"rsi_14": "sideways"}},
        ), request)


@pytest.mark.parametrize("operation", ["save", "patch", "reset"])
def test_strategy_config_invalidates_only_its_results_and_monitor_state(tmp_path, operation):
    engine, context = _make_engine()
    monitor = MonitorRuleEngine()
    targeted = "saved_params"
    other = "unrelated"
    old_result = {"as_of": "2026-07-15", "total": 3, "rows": [{"symbol": symbol} for symbol in ("A", "B", "C")]}
    unrelated_result = {"as_of": "2026-07-15", "total": 1, "rows": [{"symbol": "Z"}]}
    strategy_cache.write_cache(tmp_path, "2026-07-15", {targeted: old_result, other: unrelated_result})
    monitor._latest_strategy_results = {targeted: old_result, other: unrelated_result}
    monitor._building_strategy_results = {targeted: old_result, other: unrelated_result}
    monitor._latest_strategy_result_ids = {targeted, other}
    monitor._strategy_pools = {("rule-a", targeted, "stock"): {"A", "B", "C"}, ("rule-b", other, "stock"): {"Z"}}
    monitor._strategy_signal_state = {("rule-a", targeted, "stock", "entry_signal"): ("2026-07-15", {"A"}), ("rule-b", other, "stock", "entry_signal"): ("2026-07-15", {"Z"})}
    monitor._strategy_signal_seen = {("rule-a", targeted, "stock", "entry_signal", "A"): "2026-07-15", ("rule-b", other, "stock", "entry_signal", "Z"): "2026-07-15"}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        strategy_engine=engine,
        monitor_engine=monitor,
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))
    request.app.state.user_runtimes = SimpleNamespace(get=lambda _: request.app.state)
    if operation == "reset":
        strategy_config.save_override(tmp_path, targeted, {"params": {"min_value": 3}})
        strategy_api.reset_config(targeted, request)
    else:
        action = strategy_api.save_config if operation == "save" else strategy_api.patch_config
        action(strategy_api.SaveConfigRequest(strategy_id=targeted, overrides={"params": {"min_value": 3}}), request)

    cached = strategy_cache.read_cache(tmp_path)
    assert set(cached["results"]) == {other}
    assert set(cached["today_ever_rows"]) == set(cached["today_ever_matched"]) == {other}
    assert set(screener_api.get_cached_summary(request)["results"]) == {other}
    assert set(monitor.latest_strategy_results()) == set(monitor._building_strategy_results) == {other}
    assert monitor._latest_strategy_result_ids == {other}
    assert set(monitor._strategy_pools) == {("rule-b", other, "stock")}
    assert all(key[1] == other for key in monitor._strategy_signal_state)
    assert all(key[1] == other for key in monitor._strategy_signal_seen)
    result = engine.run(targeted, context, overrides=strategy_config.load_override(tmp_path, targeted))
    assert [row["symbol"] for row in result.rows] == (["A", "B", "C"] if operation == "reset" else ["C"])


def test_saved_strategy_config_publishes_existing_config_update(tmp_path, monkeypatch):
    engine, _ = _make_engine()
    app = FastAPI()
    app.include_router(strategy_api.router)
    app.middleware("http")(proxy_middleware)
    app.state.service_role = "collector"
    app.state.repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    app.state.strategy_engine = engine
    app.state.user_runtimes = SimpleNamespace(get=lambda _: app.state)
    app.state.monitor_engine = MonitorRuleEngine()
    app.state.quote_service = SimpleNamespace(_repo=app.state.repo, status=lambda: {}, get_index_quotes=pl.DataFrame)
    bus = EventBus()
    monkeypatch.setattr(shared_runtime, "runtime_bus", bus)
    subscription = bus.subscribe()
    try:
        with TestClient(app) as client:
            response = client.patch("/api/strategies/config", json={"strategy_id": "saved_params", "overrides": {"params": {"min_value": 3}}})
        assert response.status_code == 200
        event = subscription.get_nowait()
        assert event["type"] == "data_updated" and event["data"]["datasets"] == ["config"]
        assert (tmp_path / "runtime" / "data_update.json").exists()
    finally:
        bus.unsubscribe(subscription)
