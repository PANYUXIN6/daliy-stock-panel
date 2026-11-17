"""活跃性以已确认的独立触点为输入；验证时间口径、恢复与填充区段。"""
from datetime import date, timedelta

from app.indicators.box_activity import attach_box_activity, end_box_activity_at_gap


def day(index):
    return (date(2026, 1, 1) + timedelta(days=index)).isoformat()


INITIAL = [(2, 3, "upper"), (6, 7, "lower"), (10, 11, "upper"), (14, 15, "lower")]


def report(last, *, touches=INITIAL, closes=None, confirmed=15, broken=None, short=20, long=60):
    dates = [day(index) for index in range(last + 1)]
    box = {
        "id": "range", "lower": 100, "upper": 110,
        "confirmed_date": day(confirmed), "end_date": day(broken - 1 if broken is not None else last),
        "breakout": {"confirmed_date": day(broken) if broken is not None else None},
        **{f"{side}_touches": [
            {"date": day(extreme), "confirmed_date": day(available)}
            for extreme, available, boundary in touches if boundary == side and available <= last
        ] for side in ("upper", "lower")},
    }
    attach_box_activity(box, dates, closes or [105] * len(dates), short=short, long=long)
    return box


def test_boundary_ages_degrade_only_after_20_and_60_without_deleting_structure():
    assert report(30)["activity"]["state"] == "active"
    weak = report(31)
    assert weak["activity"]["state"] == "weakening"
    assert weak["activity"]["state_change_date"] == day(31)
    assert weak["active_segments"][0]["end_date"] == day(30)
    assert weak["active_segments"][0]["end_event_date"] == day(31)
    assert report(70)["activity"]["state"] == "weakening"
    assert report(71)["activity"]["state"] == "historical"
    assert report(150)["end_date"] == day(150)


def test_single_side_visits_do_not_refresh_roundtrip_or_opposite_boundary():
    touches = INITIAL + [(i, i + 1, "lower") for i in (18, 22, 26, 30)]
    result = report(31, touches=touches)
    assert result["activity"]["state"] == "weakening"
    assert result["activity"]["upper_age_bars"] == 21
    assert result["activity"]["lower_age_bars"] == 1
    assert result["activity"]["last_roundtrip_confirmed_date"] == day(15)


def test_recovery_needs_new_complete_roundtrip_and_preserves_inactive_gap():
    touches = [*INITIAL, (80, 81, "upper"), (84, 85, "lower"), (88, 89, "upper")]
    assert report(81, touches=touches)["activity"]["state"] == "historical"
    assert report(85, touches=touches)["activity"]["state"] == "historical"
    recovered = report(89, touches=touches)
    assert recovered["activity"]["state"] == "active"
    assert [(s["start_date"], s["end_date"]) for s in recovered["active_segments"]] == [
        (day(15), day(30)), (day(89), None),
    ]
    assert recovered["active_segments"][1]["start_reason"] == "reactivated"
    assert recovered["active_segments"][0] == report(71)["active_segments"][0]


def test_confirmation_delay_does_not_extend_extreme_date_window():
    touches = [(0, 1, "upper"), (5, 6, "lower"), (10, 11, "upper"), (19, 21, "lower")]
    # 最后一个往返为下5→上10→下19；在第26根首次成立时起点已过期。
    result = report(26, touches=touches, confirmed=26)
    assert result["activity"]["state"] == "weakening"
    assert result["activity"]["last_roundtrip_confirmed_date"] == day(21)
    assert result["active_segments"] == []
    assert report(25, touches=touches, confirmed=25)["activity"]["state"] == "active"


def test_pending_pauses_fill_but_not_aging_and_return_does_not_reset_activity():
    closes = [105] * 76
    closes[20:75] = [111] * 55
    pending = report(71, closes=closes[:72])
    assert pending["activity"]["state"] == "historical"
    segment = pending["active_segments"][0]
    assert (segment["end_date"], segment["end_event_date"], segment["end_reason"]) == (day(19), day(20), "breakout_pending")
    returned = report(75, closes=closes)
    assert returned["activity"]["state"] == "historical"
    assert returned["active_segments"] == pending["active_segments"]


def test_short_excursion_can_resume_still_fresh_evidence_without_connecting_fill():
    closes = [105] * 26
    closes[20:22] = [111, 111]
    box = report(25, closes=closes)
    assert [(s["start_date"], s["end_date"]) for s in box["active_segments"]] == [
        (day(15), day(19)), (day(22), None),
    ]
    assert box["active_segments"][1]["start_reason"] == "returned"
    assert box["activity"]["last_roundtrip_confirmed_date"] == day(15)


def test_terminal_event_closes_previous_valid_bar_and_cannot_reactivate():
    box = report(100, broken=20)
    assert box["activity"]["as_of"] == day(20)
    assert box["active_segments"][0]["end_date"] == day(19)
    assert box["active_segments"][0]["end_reason"] == "breakout"
    interrupted = report(20)
    end_box_activity_at_gap(interrupted, day(23))
    assert interrupted["active_segments"][0]["end_date"] == day(20)
    assert interrupted["active_segments"][0]["end_event_date"] == day(23)
    assert interrupted["active_segments"][0]["end_reason"] == "data_interrupted"


def test_future_touch_cannot_change_past_activity_or_closed_segments():
    touches = [*INITIAL, (80, 81, "upper"), (84, 85, "lower"), (88, 89, "upper")]
    at_89 = report(89, touches=touches)
    for last in range(31, 89):
        before = report(last, touches=touches)
        assert before["active_segments"][0] == at_89["active_segments"][0]
        assert len(before["active_segments"]) == 1
