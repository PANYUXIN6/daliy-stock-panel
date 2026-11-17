"""从已确认触点因果重放活跃性与可填充区段，不改变结构身份和价格边界。"""
from __future__ import annotations


def attach_box_activity(box: dict, dates: list[str], closes: list[float], *, short: int, long: int) -> None:
    box["activity"] = None
    box["active_segments"] = []
    if box["confirmed_date"] is None:
        return

    indices = {day: index for index, day in enumerate(dates)}
    first = indices[box["confirmed_date"]]
    terminal = box["breakout"]["confirmed_date"]
    last = indices[terminal] if terminal else len(dates) - 1
    touches = sorted(
        (indices[touch["confirmed_date"]], indices[touch["date"]], side)
        for side in ("upper", "lower") for touch in box[f"{side}_touches"]
        if touch["confirmed_date"] is not None
    )
    cursor = 0
    turns: list[tuple[int, int, str]] = []
    latest: dict[str, int] = {}
    trip: tuple[tuple[int, int, str], ...] | None = None
    state: str | None = None
    changed = first
    downgraded = -1
    segments = box["active_segments"]
    reasons: list[str] = []

    for index in range(first, last + 1):
        # 发生顺序定义往返，确认日期限定可用性；同侧新触点不刷新往返时间。
        while cursor < len(touches) and touches[cursor][0] <= index:
            confirmed, extreme, side = touches[cursor]
            cursor += 1
            latest[side] = extreme
            if turns and turns[-1][2] == side:
                turns[-1] = (confirmed, extreme, side)
            else:
                turns.append((confirmed, extreme, side))
                turns = turns[-3:]
                if len(turns) == 3:
                    trip = tuple(turns)

        ages = {side: index - latest[side] if side in latest else None for side in ("upper", "lower")}
        trip_age = index - trip[-1][0] if trip else None
        evidence_ages = [*ages.values(), trip_age]
        expired_long = any(age is None or age > long for age in evidence_ages)
        expired_short = any(age is None or age > short for age in evidence_ages)
        inside = box["lower"] <= closes[index] <= box["upper"]
        recent_trip = trip is not None and all(index - touch[1] <= short for touch in trip)
        fresh_recovery = trip is not None and trip[-1][0] > downgraded and trip[-1][1] > downgraded

        reasons = []
        for side, age in ages.items():
            if age is None or age > short:
                reasons.append(f"{side}_stale")
        if trip_age is None or trip_age > short:
            reasons.append("roundtrip_stale")

        ended = terminal == dates[index]
        if not ended:
            previous = state
            if expired_long:
                state = "historical"
            elif state == "active" and expired_short:
                state = "weakening"
            elif state != "active" and recent_trip and fresh_recovery and inside and not expired_short:
                state = "active"
            elif state is None:
                state = "weakening"
            if state != previous:
                changed = index
                if previous is not None and state != "active":
                    downgraded = index
            if state != "active" and not reasons:
                reasons.append("recent_roundtrip_required")

        can_fill = not ended and state == "active" and inside
        opened = segments and segments[-1]["end_date"] is None
        if opened and not can_fill:
            segment = segments[-1]
            segment["end_date"] = dates[index - 1]
            segment["end_event_date"] = dates[index]
            segment["end_reason"] = "breakout" if ended else "activity_downgraded" if state != "active" else "breakout_pending"
        elif can_fill and not opened:
            start_reason = "first_active" if not segments else "returned" if segments[-1]["end_reason"] == "breakout_pending" else "reactivated"
            segments.append({
                "id": f"{box['id']}:{dates[index]}", "start_date": dates[index], "end_date": None,
                "start_reason": start_reason, "end_reason": None, "end_event_date": None,
                "duration_bars": 0,
            })
        if can_fill:
            segments[-1]["duration_bars"] += 1

    box["activity"] = {
        "state": state, "state_change_date": dates[changed], "as_of": dates[last],
        "upper_age_bars": ages["upper"], "lower_age_bars": ages["lower"],
        "roundtrip_age_bars": trip_age,
        "last_roundtrip_confirmed_date": dates[trip[-1][0]] if trip else None,
        "reasons": reasons,
    }


def end_box_activity_at_gap(box: dict, event_date: str) -> None:
    """服务已识别的数据断点结束填充；终点保持最后一根有效 K。"""
    segments = box.get("active_segments", [])
    if segments and segments[-1]["end_date"] is None:
        segments[-1].update(end_date=box["end_date"], end_event_date=event_date, end_reason="data_interrupted")
