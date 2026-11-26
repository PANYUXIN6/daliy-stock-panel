from datetime import date, timedelta

import pytest

from app.services.evening_report_dip import dip_labels
from app.services.evening_report_rules import any_of
from app.services.evening_report_wave import compute_wave

SYMBOL = "000993.SZ"


def dates(n):
    d = date(2026, 1, 5)
    result = []
    while len(result) < n:
        if d.weekday() < 5:
            result.append(d.isoformat())
        d += timedelta(days=1)
    return result


def bars(points):
    # Start above 10 to observe a real decline, a confirmed L0=10, and H1=12 with breakout evidence.
    source = [(10.8, 10.7, 10.75, 100)] * 20 + [
        (10.8, 10.15, 10.20, 100), (10.3, 10., 10.2, 100), (10.6, 10.2, 10.55, 100),
        (11.1, 10.6, 11., 100), (12., 11., 11.90, 100),
    ] + list(points)
    calendar = dates(len(source))
    rows = {d: {"formal": True, "open": low, "high": high, "low": low, "close": close,
                "raw_high": high, "raw_low": low, "raw_close": close, "volume": volume, "ma10": 11.60}
            for d, (high, low, close, volume) in zip(calendar, source, strict=True)}
    return rows, calendar


def at(rows, calendar, halted=()):
    return compute_wave(SYMBOL, rows, calendar, set(halted), listing_date=calendar[0])


def dip(wave, events, **override):
    values = {"space_score": 60, "identity_score": 54, "high_proximity": .90, "amount_day": 550e6,
              "return_20d": .50, "return_3d": .03, "volume_ratio_2_10": .5, "low_vs_previous": 0,
              "distance_bull_bear": .01, "amplitude_ratio_2_10": .7, "distance_ma10": .01, **override}
    return dip_labels(values, {}, {"phase": "rise", "classification": {"sentiment_state": "warm"},
                                   "ge2_count": 1}, "10%", wave=wave, events=events, day=wave["as_of"])


def test_unconfirmed_shallow_pullback_can_be_full_without_starting_second_wave():
    rows, days = bars([(11.9, 11.2, 11.35, 50), (11.5, 11.2, 11.30, 60)])
    earlier, _ = at(rows, days[:-1])  # rows also contain tomorrow
    assert earlier["as_of"] == days[-2]
    assert earlier["pullback_volume"]["sessions"] == 1
    assert earlier["required_checks"]["volume"] is None
    wave, start = at(rows, days)
    assert wave["first_wave_start"]["price"] == 10
    assert wave["first_wave_high"]["price"] == 12
    assert wave["first_pullback_low"] is start is None
    assert wave["first_impulse_return"] == pytest.approx(.20)
    assert wave["first_wave_pullback"] == pytest.approx(1 - 11.30 / 12)
    assert wave["retrace_pct"] == pytest.approx(.4)
    assert wave["pullback_volume"] == {"sessions": 2, "mean": 55, "start_exclusive": days[-3], "end": days[-1]}
    assert wave["required_checks"]["first_impulse_evidence"] is True
    wave["required_checks"]["support"] = True
    assert dip(wave, {"e2": True, "e1": False})["status"] == "full"
    earlier["required_checks"]["support"] = True
    assert dip(earlier, {"e2": True, "e1": False})["status"] == "partial"


@pytest.mark.parametrize(("delta", "expected"), [(0, True), (.01, True), (-.001, False), (None, None)])
def test_e2_two_session_lows_independent_of_three_session_support(delta, expected):
    rows, days = bars([(11.9, 11.2, 11.35, 50), (11.5, 11.2, 11.3, 60)])
    wave, _ = at(rows, days)
    wave["required_checks"]["support"] = True
    result = dip(wave, {"e1": False, "e2": True}, low_vs_previous=delta, rising_lows_3d=False)
    assert result["e2"]["inputs"]["low_vs_previous"] is expected
    assert result["status"] == ("full" if expected is True else "partial")


def test_event_absent_unknown_or_applied_and_cold_environment():
    rows, days = bars([(11.9, 11.2, 11.35, 50), (11.5, 11.2, 11.3, 60)])
    wave, _ = at(rows, days)
    wave["required_checks"]["support"] = True
    for events, reason in [(None, "t4_unverified"), ({"e1": False, "e2": False}, "no_applicable_event")]:
        result = dip(wave, events)
        assert result["status"] == "none"
        assert result["primary_reason_code"] == reason
    assert dip(wave, {"e1": True, "e2": False})["status"] == "full"
    no_shape = dip(wave, {"e1": True, "e2": False}, volume_ratio_2_10=None,
                   return_3d=None, distance_ma10=None, amplitude_ratio_2_10=None)
    assert no_shape["status"] == "none"
    assert any_of(*no_shape["e1"]["inputs"].values()) is None
    result = dip_labels({}, {}, {"phase": "ebb"}, "10%", wave=wave, events={"e1": True}, day=days[-1])
    assert result["status"] == "none" and result["primary_reason_code"] == "phase_ebb"


def test_halts_keep_extreme_and_do_not_count_as_pullback_or_idle_days():
    rows, days = bars([(12.0, 11.4, 11.7, 60), (12., 11.5, 11.7, 20), (11.8, 11.2, 11.30, 55)])
    # Raw price halves following an ex-right event. The adjusted series remains continuous.
    rows[days[-1]]["raw_low"] /= 2
    rows[days[-1]]["raw_high"] /= 2
    del rows[days[-2]]
    wave, _ = at(rows, days, [days[-2]])
    assert wave["first_wave_high"]["extreme_date"] == days[-4]
    assert wave["first_wave_high"]["confirmed_at"] == days[-1]
    assert wave["pullback_volume"]["sessions"] == 2
    assert wave["required_checks"]["volume"] is True
    missing, _ = at(rows, days)
    assert missing["data_status"] == "data_gap"
    assert missing["first_pullback_eligible"]["value"] is None
    halted, _ = at(rows, days[:-1], [days[-2]])
    assert halted["first_pullback_eligible"] == {"value": False, "reason_codes": ["suspended"]}


def test_start_uses_evidence_at_or_after_l1_extreme_and_only_publishes_on_k():
    points = [(11.9, 11.2, 11.35, 50), (11.5, 11., 11.20, 60), (11.42, 11.1, 11.4, 100),
              (11.7, 11.15, 11.6, 40), (11.8, 11.5, 11.7, 110)]
    rows, days = bars(points)
    rows[days[-3]]["ma10"] = rows[days[-2]]["ma10"] = 11.3
    wave, start = at(rows, days[:-1])
    assert wave["second_wave_start"] == {"date": days[-2], "evidence_date": days[-3]}
    assert (start["date"], start["evidence_date"]) == (days[-2], days[-3])
    assert (start["target_high"]["price"], start["failure_low"]["price"]) == (12., 11.)
    assert (start["start_low"], start["start_close"]) == (11.15, 11.6)
    assert wave["window_close_reason_codes"] == ["closed_by_start"]
    assert wave["current_wave_index"] == 2
    _, next_start = at(rows, days)
    assert next_start is None
    original = rows.pop(days[-3])
    unknown, missed = at(rows, days)
    assert unknown["data_status"] == "data_gap" and missed is None
    rows[days[-3]] = original
    assert at(rows, days)[1] is None


def test_volume_start_below_h1_completes_at_its_own_confirmed_high():
    rows, days = bars([(11.9, 11.2, 11.35, 50), (11.5, 11., 11.20, 60), (11.8, 11.1, 11.6, 150),
                       (11.95, 11.30, 11.32, 70), (11.8, 11.33, 11.65, 150)])
    rows[days[-3]]["ma10"] = 11.4
    live, event = at(rows, days[:-2])
    assert event["start_close"] < event["target_high"]["price"]
    assert (live["current_wave_index"], live["completed_wave_count"]) == (2, 1)
    confirmed, retried = at(rows, days[:-1])
    assert (confirmed["current_wave_index"], confirmed["completed_wave_count"]) == (2, 2)
    assert retried is None and at(rows, days)[1] is None


def test_missing_volume_preserves_structure_and_current_drawdown_gate():
    rows, days = bars([(11.9, 11.2, 11.35, 50), (11.6, 11.25, 11.55, None)])
    wave, start = at(rows, days)
    assert wave["data_status"] == "known" and start is None
    assert wave["first_wave_pullback"] == pytest.approx(.0375)
    assert wave["first_pullback_eligible"] == {"value": False, "reason_codes": ["pullback_below_min"]}
    assert wave["retrace_pct"] == pytest.approx(.40)
    assert wave["required_checks"]["volume"] is None


def test_weak_volume_breakout_can_confirm_second_high_on_same_day_and_never_reopen():
    rows, days = bars([(11.9, 11.2, 11.35, 50), (11.5, 11., 11.20, 60), (11.8, 11.1, 11.6, 40),
                       (13., 11.45, 12.2, 25), (12.4, 11.95, 12.05, 300)])
    wave, start = at(rows, days[:-1])
    assert wave["completed_wave_count"] == wave["current_wave_index"] == 2
    assert start is None
    assert wave["window_close_reason_codes"] == ["closed_by_new_high"]
    later, start = at(rows, days)
    assert start is None and later["first_pullback_active"] is False


def test_candidate_survives_more_than_twenty_idle_sessions_and_250_day_bound():
    rows, days = bars([(11.9, 11.2, 11.35, 50)] + [(11.5, 11.2, 11.3, 60)] * 25)
    wave, _ = at(rows, days)
    assert wave["provisional_extreme"]["idle_sessions"] == 24
    assert wave["first_pullback_active"] is True
    rows, days = bars([(11.9, 11.2, 11.35, 50)] + [(11.5, 11.2, 11.3, 60)] * 251)
    wave, start = at(rows, days)
    assert start is None and wave["first_wave_high"] is None
    assert wave["first_pullback_eligible"]["value"] is None
