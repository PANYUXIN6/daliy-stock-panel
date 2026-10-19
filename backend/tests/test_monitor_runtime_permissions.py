"""权限调整必须收口监控与持仓提醒运行时，同时保留独立模拟盘策略事件。"""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from app.services import alert_store, ordinary_minute, preferences
from app.services.auth import AuthStore
from app.services.quote_service import QuoteService
from app.services.user_context import UserContext, current_user, user_scope
from app.services.user_runtime import UserRuntimeRegistry
from app.strategy import lots, monitor_rules, paper, paper_auto

DAY = date(2026, 10, 16)
SYMBOLS = [f"60000{i}.SH" for i in range(7)]


@pytest.fixture
def environment(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    store = AuthStore(tmp_path)
    admin = store.bootstrap("admin", "initial-password")
    guest = store.create_user("guest", "temporary-password")
    pro = store.create_user("prouser", "temporary-password", role="pro", pro_expires_on="2099-01-01")
    users = {row["role"]: UserContext(row["id"], row["role"], tmp_path) for row in (admin, guest, pro)}
    templates = tmp_path / "strategies" / "templates"
    templates.mkdir(parents=True)
    (templates / "permission_demo.py").write_text('''import polars as pl
META = {"id": "permission_demo", "name": "确定性测试策略", "asset_types": ["stock"], "timeframes": ["1d"]}
BASIC_FILTER = {"enabled": False}
def filter(df, params):
    return pl.col("close") > 20
''', encoding="utf-8")
    repo = SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path),
        get_name_map=lambda *args: {symbol: symbol for symbol in SYMBOLS},
        resolve_asset_type=lambda symbol: "index" if symbol.endswith(".SH") and symbol.startswith("000") else "stock",
    )
    state = SimpleNamespace(repo=repo)
    registry = state.user_runtimes = UserRuntimeRegistry(state)
    service = QuoteService()
    service.set_app_state(state)
    live = {"frame": _frame(10)}
    service.get_enriched_today = lambda: (live["frame"], DAY)
    broadcasts, strategy_updates = [], []
    monkeypatch.setattr(service, "_is_continuous_trading", lambda: True)
    monkeypatch.setattr(service, "_enrich_alerts_ext", lambda events: None)
    monkeypatch.setattr(service, "_broadcast_alerts", lambda events: broadcasts.extend(
        (current_user().id, event) for event in events
    ))
    monkeypatch.setattr(service, "notify_strategy_results_updated", lambda: strategy_updates.append(current_user().id))
    for name in ("_maybe_send_system_notifications", "_maybe_send_webhook", "_maybe_send_paper_webhook"):
        monkeypatch.setattr(service, name, lambda *args: None)
    monkeypatch.setattr("app.services.quote_service.cn_today", lambda: DAY)
    monkeypatch.setattr("app.strategy.monitor.cn_today", lambda: DAY)
    now = datetime(2026, 10, 16, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(paper, "cn_now", lambda: now)
    monkeypatch.setattr(paper_auto, "cn_now", lambda: now)
    yield SimpleNamespace(root=tmp_path, users=users, state=state, registry=registry, service=service,
                          live=live, broadcasts=broadcasts, strategy_updates=strategy_updates)
    registry.shutdown()


def _frame(close, symbols=SYMBOLS[:1]):
    return pl.DataFrame({
        "symbol": symbols, "date": [DAY] * len(symbols),
        "close": [float(close)] * len(symbols), "raw_close": [float(close / 2)] * len(symbols),
        "change_pct": [0.01] * len(symbols),
    })


def _save_rules(root):
    price = monitor_rules.normalize({
        "id": "legacy_price", "name": "旧普通价格", "type": "price", "scope": "symbols",
        "symbols": [SYMBOLS[0]], "conditions": [{"field": "close", "op": ">", "value": 1}],
        "cooldown_seconds": 0,
    })
    signal = {**price, "id": "legacy_signal", "type": "signal", "lot_id": "lot_kept"}
    strategy = monitor_rules.normalize({
        "id": "legacy_strategy", "name": "旧普通策略", "type": "strategy", "scope": "all",
        "strategy_id": "permission_demo", "notify_events": ["buy_signal", "pool_entry"],
        "cooldown_seconds": 0,
    })
    lot = lots.normalize_lot({"id": "lot_kept", "symbol": SYMBOLS[0], "cost_price": 1,
                              "target_pct": 5, "remind_date": DAY.isoformat()})
    lots.save_one(root, lot)
    derived = [monitor_rules.normalize(rule) for rule in lots.lot_to_rules(lot)]
    for rule in (price, signal, strategy, *derived):
        monitor_rules.save_one(root, rule)
    return [price, signal, strategy, *derived]


def _tick(env, owner, close, symbols=SYMBOLS[:1]):
    env.live["frame"] = _frame(close, symbols)
    runtime = env.registry.get(owner)
    with user_scope(owner):
        env.service._evaluate_user_monitors(pl.DataFrame(), None, runtime)
    return runtime


def _auto_rule(env, owner, account="default", **changes):
    with user_scope(owner):
        paper.create_account(env.root, 1_000_000, account_id=account)
        return paper_auto.create_auto_rule(env.root, {
            "name": "策略跟单", "match_kind": "strategy", "match_id": "permission_demo",
            "side": "auto", "size_mode": "fixed_amount", "size_value": 2200,
            "order_type": "next_open", "cooldown_days": 0, **changes,
        }, account_id=account)


@pytest.mark.parametrize("role", ["guest", "pro", "admin"])
def test_old_monitors_and_lot_reminders_require_admin(environment, role):
    env, owner = environment, environment.users[role]
    with user_scope(owner):
        _save_rules(env.root)
    runtime = _tick(env, owner, 22)
    allowed = ({"lot_kept_p", "lot_kept_d", "legacy_price", "legacy_signal", "legacy_strategy"}
               if role == "admin" else set())
    assert set(runtime.monitor_engine.rules) == allowed
    emitted = {event["rule_id"] for _, event in env.broadcasts}
    if role == "admin":
        assert {"lot_kept_p", "lot_kept_d"} <= emitted
    assert emitted <= allowed
    assert bool(runtime.monitor_engine.latest_strategy_results()) is (role == "admin")
    # 持仓编辑沿用整表 reload，不能重新激活同一目录的旧普通规则。
    with user_scope(owner):
        runtime.monitor_engine.set_rules(monitor_rules.load_all(env.root))
        assert {"lot_kept_p", "lot_kept_d"} <= {rule['id'] for rule in monitor_rules.load_all(env.root)}
    assert set(runtime.monitor_engine.rules) == allowed


def test_downgrade_clears_old_monitor_results_and_restricts_minute_subscription_pool(environment):
    env, admin = environment, environment.users["admin"]
    with user_scope(admin):
        rules = _save_rules(env.root)
        monitor_rules.save_one(env.root, {**rules[0], "id": "legacy_index", "asset_type": "index",
                                         "symbols": ["000016.SH"]})
        monitor_rules.save_one(env.root, {**rules[0], "id": "legacy_unused_stock", "symbols": [SYMBOLS[1]]})
        preferences.set_realtime_monitor_config({"strategy_monitor_enabled": True,
                                                "strategy_monitor_ids": ["permission_demo"]})
    runtime = _tick(env, admin, 22)
    assert runtime.monitor_engine.latest_strategy_results()
    downgraded = UserContext(admin.id, "guest", env.root)
    assert env.registry.get(downgraded) is runtime
    assert runtime.monitor_engine.rules == {}
    assert runtime.monitor_engine.latest_strategy_results() == {}
    assert not runtime.monitor_engine._strategy_pools
    assert not runtime.monitor_engine._strategy_signal_state
    assert not runtime.monitor_engine._last_fire
    # 模拟采集进程看到更新后的角色；不启动任何采集。
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("app.services.auth.get_store", lambda *args: SimpleNamespace(
            list_active_users=lambda: [{"id": admin.id, "role": "guest"}]
        ))
        pool = ordinary_minute.collection_pool(env.state.repo)
    assert "000016.SH" not in pool["index"]
    assert SYMBOLS[1] not in pool["stock"]
    assert SYMBOLS[0] not in pool["stock"]
    # 升权重新装载存量规则，保持管理员行为。
    assert "legacy_strategy" in env.registry.get(admin).monitor_engine.rules


@pytest.mark.parametrize("role", ["guest", "pro", "admin"])
def test_strategy_follow_orders_work_without_visible_monitors_and_never_publish_internal_events(environment, role):
    env, owner = environment, environment.users[role]
    rule = _auto_rule(env, owner)
    runtime = _tick(env, owner, 10, SYMBOLS)
    assert runtime.monitor_engine.rule_count == 0
    _tick(env, owner, 22, SYMBOLS)
    with user_scope(owner):
        orders = paper.load_orders(env.root)
        assert len(orders) == len(SYMBOLS)
        assert {order["source"] for order in orders} == {f"auto:{rule['id']}"}
        assert all(order["ref_price"] == 11 for order in orders)
        assert {event["source"] for event in alert_store.list_recent(env.root)} == {"paper"}
    assert env.broadcasts and all(event["source"] == "paper" for _, event in env.broadcasts)
    assert env.strategy_updates == []
    assert runtime.monitor_engine.latest_strategy_results() == {}


def test_visible_admin_strategy_and_internal_strategy_do_not_duplicate_orders(environment):
    env, owner = environment, environment.users["admin"]
    with user_scope(owner):
        _save_rules(env.root)
    _auto_rule(env, owner)
    _tick(env, owner, 10)
    _tick(env, owner, 22)
    with user_scope(owner):
        assert len(paper.load_orders(env.root)) == 1
    assert any(event["source"] == "strategy" for _, event in env.broadcasts)


def test_rule_lifecycle_frozen_accounts_and_owners_keep_strategy_subscriptions_isolated(environment):
    env, guest, pro = environment, environment.users["guest"], environment.users["pro"]
    runtime = _tick(env, guest, 10)
    first = _auto_rule(env, guest)
    _auto_rule(env, guest, "second")
    _auto_rule(env, pro)
    _tick(env, guest, 10)
    _tick(env, pro, 22)  # 不把 guest 的池状态带到 pro；首帧只建立基线。
    _tick(env, guest, 22)
    with user_scope(guest):
        assert len(paper.load_orders(env.root)) == 1
        assert len(paper.load_orders(env.root, "second")) == 1
        paper_auto.set_enabled(env.root, first["id"], False)
        second = paper.get_account(env.root, "second")
        paper.save_account(env.root, {**second, "status": "frozen"}, "second")
    _tick(env, guest, 10)
    assert runtime.paper_monitor_engine.rule_count == 0
    with user_scope(pro):
        assert paper.load_orders(env.root) == []
    with user_scope(guest):
        paper_auto.set_enabled(env.root, first["id"], True)
    _tick(env, guest, 10)
    _tick(env, guest, 22)
    with user_scope(guest):
        assert len(paper.load_orders(env.root)) == 2
        assert len(paper.load_orders(env.root, "second")) == 1
        paper_auto.delete_auto_rule(env.root, first["id"])
    _tick(env, guest, 10)
    assert runtime.paper_monitor_engine.rule_count == 0


def test_rule_source_edit_and_strategy_config_changes_refresh_internal_state(environment):
    from fastapi import Request

    from app.api import strategy as strategy_api

    env, owner = environment, environment.users["guest"]
    rule = _auto_rule(env, owner)
    runtime = _tick(env, owner, 10)
    with user_scope(owner):
        paper_auto.save_auto_rule(env.root, {**rule, "match_id": "missing_strategy"})
    _tick(env, owner, 22)
    with user_scope(owner):
        assert paper.load_orders(env.root) == []
        paper_auto.save_auto_rule(env.root, {**rule, "size_value": 3300})
    _tick(env, owner, 10)
    _tick(env, owner, 22)
    with user_scope(owner):
        assert [order["qty"] for order in paper.load_orders(env.root)] == [300]
        request = Request({"type": "http", "app": SimpleNamespace(state=env.state)})
        strategy_api.patch_config(strategy_api.SaveConfigRequest(
            strategy_id="permission_demo",
            overrides={"overlay_filter": [{"left": "close", "op": ">", "right": "30", "shift": 0}]},
        ), request)
        assert runtime.paper_monitor_engine._strategy_pools == {}
        assert runtime.paper_monitor_engine._strategy_signal_state == {}
        assert runtime.paper_monitor_engine._active_matrix_snapshots == {}
        assert runtime.strategy_engine._realtime_matrices == {}
    _tick(env, owner, 22)  # 新配置排除候选，且建立新的事件基线。
    with user_scope(owner):
        assert len(paper.load_orders(env.root)) == 1
    _tick(env, owner, 32)
    with user_scope(owner):
        orders = paper.load_orders(env.root)
        assert len(orders) == 2
        assert {order["ref_price"] for order in orders} == {11, 16}
