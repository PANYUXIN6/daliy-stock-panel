from __future__ import annotations

from copy import deepcopy

import polars as pl
import pytest

from app.services import alert_store
from app.services.quote_service import rule_event_to_alert
from app.strategy.monitor import MonitorRuleEngine


def _rule(conditions, **overrides):
    return {
        "id": "r", "name": "规则", "type": "signal", "enabled": True,
        "asset_type": "stock", "scope": "all", "conditions": conditions,
        "logic": "or", "cooldown_seconds": 0, **overrides,
    }


def _evaluate(frame, conditions, **overrides):
    engine = MonitorRuleEngine()
    engine.set_rules([_rule(conditions, **overrides)])
    return engine.evaluate(frame, reset_strategy_results=False)


def test_numeric_or_attributes_each_row_without_changing_input():
    conditions = [
        {"field": "change_pct", "op": ">=", "value": 0.05},
        {"field": "rsi_14", "op": ">=", "value": 80},
        {"field": "vol_ratio_5d", "op": ">=", "value": 3},
    ]
    original = deepcopy(conditions)
    frame = pl.DataFrame({
        "symbol": ["600000.SH", "000001.SZ", "300001.SZ"], "close": [10.0] * 3,
        "change_pct": [0.06, 0.01, 0.02], "rsi_14": [70.0, 85.0, None],
        "vol_ratio_5d": [1.0, 4.0, None],
    })
    events = _evaluate(frame, conditions)
    assert [event["symbol"] for event in events] == ["600000.SH", "000001.SZ"]
    assert events[0]["matched_conditions"] == conditions[:1]
    assert events[1]["matched_conditions"] == conditions[1:]
    assert all(event["conditions"] == original for event in events)
    assert "rsi_14" not in events[0]["message"]
    assert "量比" not in events[0]["message"]
    assert " 且 " in events[1]["message"]
    assert " 或 " not in events[1]["message"]
    assert conditions == original
    assert not any(name.startswith("__cond_hit_") for name in frame.columns)


def test_mixed_or_includes_only_satisfied_truth_and_numeric_conditions():
    conditions = [
        {"field": "signal_macd_dead", "op": "truth"},
        {"field": "close", "op": ">=", "value": 9},
        {"field": "vol_ratio_5d", "op": ">=", "value": 3},
    ]
    events = _evaluate(pl.DataFrame({
        "symbol": ["600000.SH"], "close": [10.0], "change_pct": [0.01],
        "signal_macd_dead": [True], "vol_ratio_5d": [1.0],
    }), conditions)
    assert events[0]["matched_conditions"] == conditions[:2]
    assert events[0]["signals"] == ["signal_macd_dead"]
    assert "MACD死叉 且 收盘价>=9" in events[0]["message"]
    assert "量比" not in events[0]["message"]


@pytest.mark.parametrize("logic,expected", [("and", 1), ("or", 2)])
def test_null_handling_and_cooldown_are_preserved(logic, expected, monkeypatch):
    conditions = [{"field": "close", "op": ">=", "value": 9}, {"field": "rsi_14", "op": ">=", "value": 80}]
    frame = pl.DataFrame({"symbol": ["600000.SH", "000001.SZ"], "close": [10.0, 10.0], "rsi_14": [81.0, None]})
    engine = MonitorRuleEngine()
    engine.set_rules([_rule(conditions, logic=logic, cooldown_seconds=3600)])
    monkeypatch.setattr("app.strategy.monitor.time.time", lambda: 1000.0)
    events = engine.evaluate(frame)
    assert len(events) == expected
    assert events[0]["matched_conditions"] == conditions
    assert engine.evaluate(frame) == []


@pytest.mark.parametrize("condition", [{"field": "missing", "op": ">", "value": 1}, {"field": "close", "op": "bad", "value": 1}])
def test_missing_field_or_invalid_operator_still_fails_closed(condition):
    assert _evaluate(pl.DataFrame({"symbol": ["600000.SH"], "close": [10.0]}), [condition]) == []


def test_custom_message_persistence_and_sse_keep_attribution(tmp_path, private_owner):
    conditions = [{"field": "close", "op": ">=", "value": 9}, {"field": "close", "op": ">", "value": 11}]
    event = _evaluate(pl.DataFrame({"symbol": ["600000.SH"], "close": [10.0]}), conditions, message="自定义消息")[0]
    assert event["message"] == "自定义消息"
    alert_store.append_many(tmp_path, [event])
    saved = alert_store.list_recent(tmp_path)[0]
    assert saved["conditions"] == conditions
    assert saved["matched_conditions"] == conditions[:1]
    alert = rule_event_to_alert(saved)
    assert alert["conditions"] == conditions
    assert alert["matched_conditions"] == conditions[:1]
    # 原有其他监控类型的扩展字段必须继续透传。
    event.update(source="volume_delta", volume_delta=300, volume_delta_span=5, abnormal_window="3d")
    alert = rule_event_to_alert(event)
    assert alert["volume_delta"] == 300
    assert alert["volume_delta_span"] == 5
    assert alert["abnormal_window"] == "3d"
