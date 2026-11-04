from app.services.evening_report_rules import (
    advance_identity,
    advance_risk,
    advance_round,
    all_of,
    any_of,
    confirmation,
    primary_purpose,
    reference_interval,
    resilience,
)


def test_three_state_and_confirmation_missing_day():
    assert all_of(False, None) is False
    assert any_of(True, None) is True
    assert all_of(True, None) is None
    assert any_of(False, None) is None
    assert confirmation(True, [None, True, True], True) is None
    assert confirmation(True, [False, True, True], True) is True


def test_round_pause_unknown_and_end_priority():
    state = advance_round(None, date="01", entry=True, active=True, medium_failed=False)
    assert state["status"] == "probing"
    state = advance_round(state, date="02", entry=True, active=True, medium_failed=False)
    assert state["status"] == "active"
    chain = state["round_id"]
    state = advance_round(state, date="03", entry=False, active=False, medium_failed=True)
    assert state["status"] == "paused"
    state = advance_round(state, date="04", entry=None, active=None, medium_failed=None)
    assert state["exit_count"] == 0
    state = advance_round(state, date="05", entry=False, active=False, medium_failed=True)
    state = advance_round(state, date="06", entry=True, active=False, medium_failed=True)
    assert state["status"] == "ended"
    state = advance_round(state, date="07", entry=True, active=True, medium_failed=False)
    assert state["status"] == "probing"
    state = advance_round(state, date="08", entry=True, active=True, medium_failed=False)
    assert state["round_id"] != chain


def test_identity_unknown_breaks_counts_but_keeps_interval_and_return_new_c():
    state = advance_identity(None, date="01", entry=True, confirmed=True, maintenance=True)
    for day in ("02", "03"):
        state = advance_identity(state, date=day, entry=False, confirmed=False, maintenance=False)
        assert state["valid"] is True
    state = advance_identity(state, date="04", entry=None, confirmed=None, maintenance=None)
    assert state["valid"] is None and state["failed_count"] == 0
    state = advance_identity(state, date="05", entry=True, confirmed=True, maintenance=True)
    assert state["status"] == "divergent"
    state = advance_identity(state, date="06", entry=True, confirmed=True, maintenance=True)
    assert state["status"] == "confirmed"
    for day in ("07", "08", "09"):
        state = advance_identity(state, date=day, entry=False, confirmed=False, maintenance=False)
    assert state["intervals"][0]["ended_at"] == "09"
    state = advance_identity(state, date="10", entry=True, confirmed=True, maintenance=True)
    assert state["intervals"][1]["confirmed_at"] == "10"


def test_risk_clear_requires_two_known_price_days():
    state = advance_risk(None, risk=True, price_clear=False, recovery=False, date="01")
    state = advance_risk(state, risk=False, price_clear=True, recovery=True, date="02")
    assert state["paused"]
    state = advance_risk(state, risk=None, price_clear=None, recovery=True, date="03")
    assert state["paused"] and state["clear_count"] == 0
    state = advance_risk(state, risk=False, price_clear=True, recovery=True, date="04")
    state = advance_risk(state, risk=False, price_clear=True, recovery=True, date="05")
    assert not state["paused"]


def test_multireference_unknown_does_not_override_true_or_join_return():
    dates = ["03", "04", "05"]
    a = reference_interval(
        {"confirmed_at": "01", "ended_at": None},
        k="04",
        dates=dates,
        validity={"03": True, "04": None, "05": True},
    )
    b = reference_interval(
        {"confirmed_at": "02", "ended_at": None},
        k="04",
        dates=dates,
        validity=dict.fromkeys(dates, True),
    )
    assert a["value"] is None and b["value"] is True
    assert any_of(a["value"], b["value"]) is True
    returned = reference_interval(
        {"confirmed_at": "04", "ended_at": None},
        k="04",
        dates=dates,
        validity=dict.fromkeys(dates, True),
    )
    assert returned["value"] is False


def test_resilience_raw_atr_no_pressure_and_historical_damage():
    dates = [str(i).zfill(2) for i in range(21)]
    prices = [100.0] * 21
    result = resilience(prices, [0.1] * 21, [0.0] * 20, [0.0] * 20, dates)
    assert result["status"] == "untested" and result["R"] is None and result["C"] == 100
    prices[5] = 99.0
    result = resilience(prices, [0.1] * 21, [0.0] * 20, [0.0] * 20, dates)
    assert result["C"] == 0 and result["local_weak"]


def test_resilience_nonoverlapping_blocks_repair60_and_immature():
    dates = [str(i).zfill(2) for i in range(21)]
    prices = [100.0] * 21
    prices[1:5] = [97, 95, 96, 98]
    market = [-0.02, -0.02, -0.02, 0] + [0.0] * 15 + [-0.02]
    result = resilience(prices, [2.0] * 21, [0.0] * 20, market, dates)
    assert len(result["events"]) == 2
    assert result["events"][0]["score"] == 60
    assert result["mature_events"] == 1 and result["ongoing"]
    assert result["status"] == "limited"


def test_purpose_dependency_scope():
    args = dict(
        paused=False,
        invalidated=False,
        live=True,
        maintenance_unknown=False,
        candidate=False,
        candidate_unknown=True,
        position=True,
        early=False,
    )
    assert primary_purpose(**args) == "已确认核心跟踪"
    assert primary_purpose(**{**args, "position": False}) == "等待位置确认"
    assert (
        primary_purpose(**{**args, "live": False, "maintenance_unknown": True, "candidate": True})
        == "证据不足"
    )
