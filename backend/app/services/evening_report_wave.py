"""截至报告日的 250 个市场交易日内独立识别波次，直接使用正式前复权行情。"""
from __future__ import annotations

from app.services.evening_report_rules import all_of, compare, finite, ratio

VERSION = "wave-rule-v2"
WINDOW = 250
PARAMETERS = {
    "window_sessions": WINDOW, "zigzag_reversal_pct": .05, "first_impulse_min_return": .20,
    "evidence_lookback": 20, "first_impulse_volume_multiple": 1.5,
    "retrace_min": .25, "retrace_max": .75, "pullback_volume_ratio_max": 1., "pullback_min_sessions": 2,
    "start_volume_vs_pullback": 1.3, "start_volume_vs_impulse": .8,
    "support_distance": .03, "first_pullback_min_pct": .05,
}


def start_key(event):
    return event["security_id"], event["start_id"], event["rule_version"]


def _average(values):
    return sum(values) / len(values) if values and all(finite(v) and v >= 0 for v in values) else None


def _volumes(rows, days, halted, begin, end):
    actual = [rows[day].get("volume") for day in days[begin + 1:end + 1] if day not in halted]
    return {"start_exclusive": days[begin], "end": days[end], "sessions": len(actual), "mean": _average(actual)}


def _impulse_evidence(rows, days, halted, begin, end):
    found_unknown = False
    lookback = PARAMETERS["evidence_lookback"]
    for i in range(begin, end + 1):
        if days[i] in halted:
            continue
        if i < lookback or any(day in halted for day in days[i - lookback:i]):
            found_unknown = True
            continue
        prior = [rows[day] for day in days[i - lookback:i]]
        row = rows[days[i]]
        if compare(row["close"], max(r["high"] for r in prior), "gt"):
            return True
        volume_ratio = ratio(row.get("volume"), _average([r.get("volume") for r in prior]))
        result = all_of(compare(ratio(row["close"], row.get("open")), 1., "gt"),
                        compare(volume_ratio, PARAMETERS["first_impulse_volume_multiple"]))
        if result is True:
            return True
        found_unknown |= result is None
    return None if found_unknown else False


def _price_branches(row, platform):
    return (compare(ratio(row["close"], row.get("ma10")), 1., "gt") is True,
            compare(row["close"], platform, "gt") is True)


def _find_start(rows, days, halted, today, low_index, confirm_today, platform, impulse_volume, pullback_volume):
    """L1 确认日允许回看证据 E；当日仍须站在 E 所满足的同一价格条件上。"""
    current_ma, current_breakout = _price_branches(rows[days[today]], platform)
    if not (current_ma or current_breakout):
        return None
    for i in range(low_index + 1 if confirm_today else today, today + 1):
        if days[i] in halted:
            continue
        row = rows[days[i]]
        ma, breakout = _price_branches(row, platform)
        if not ((ma and current_ma) or (breakout and current_breakout)):
            continue
        relative_pullback = ratio(row.get("volume"), pullback_volume["mean"])
        relative_impulse = ratio(row.get("volume"), impulse_volume["mean"])
        if all_of(compare(relative_pullback, PARAMETERS["start_volume_vs_pullback"]),
                  compare(relative_impulse, PARAMETERS["start_volume_vs_impulse"])) is True:
            return {"evidence_date": days[i], "start_amount": row.get("amount"),
                    "volume": {"start_vs_pullback": relative_pullback, "start_vs_impulse": relative_impulse}}
    return None


def compute_wave(symbol, rows_by_date, calendar, halted, *, listing_date=None):
    """calendar ends at D. Extra past/future price rows do not enlarge the analysis window."""
    target = calendar[-1]
    days = calendar[-WINDOW:]
    if listing_date:
        days = [day for day in days if day >= listing_date]
    else:
        # Old listings may have a short local history. Begin at the first actual bar.
        first = next((i for i, day in enumerate(days) if day in rows_by_date), len(days))
        days = days[first:]
    rows = rows_by_date
    idx = {day: i for i, day in enumerate(days)}
    candidate = ordinary_low = cycle = invalidation = last_impulse = gap = None
    direction = "H"
    saw_decline = False
    second_today = None

    for i, day in enumerate(days):
        if day in halted:
            continue
        row = rows.get(day)
        if not row or row.get("formal") is not True or not all(
            finite(row.get(k), positive=True) for k in ("high", "low", "close")
        ):
            gap = day
            break
        if cycle:
            line = cycle["low"] or cycle["base_low"]
            if compare(row["close"], line["price"], "lt"):
                invalidation = {"date": day, "failure_low": line}
                cycle = candidate = ordinary_low = None
                direction = "L"
                saw_decline = True
        field = "high" if direction == "H" else "low"
        price = row[field]
        if not candidate or (price > candidate["price"] if direction == "H" else price < candidate["price"]):
            candidate = {"kind": direction, "price": price, "raw_price": row.get("raw_" + field),
                         "extreme_date": day, "idle_sessions": 0}
        else:
            candidate["idle_sessions"] += 1
        z = PARAMETERS["zigzag_reversal_pct"]
        reversed_now = (compare(row["close"] / candidate["price"], 1 - z, "le") if direction == "H"
                        else compare(row["close"] / candidate["price"], 1 + z))
        point = None
        if reversed_now:
            if saw_decline:
                point = {key: value for key, value in candidate.items() if key != "idle_sessions"}
                point.update(confirmed_at=day, pivot_id=f"{direction}:{point['extreme_date']}:{day}")
            saw_decline = True
            direction = "L" if direction == "H" else "H"
            candidate = None

        if point and point["kind"] == "L":
            if cycle is None:
                ordinary_low = point
            elif (cycle["low"] is None and cycle["current"] == cycle["completed"]
                  and point["price"] > cycle["base_low"]["price"]):
                cycle["low"] = point
                high_index, low_index = idx[cycle["high"]["extreme_date"]], idx[point["extreme_date"]]
                cycle["platform"] = max(rows[d]["close"] for d in days[high_index + 1:low_index + 1] if d not in halted)
                cycle["pullback_volume"] = _volumes(rows, days, halted, high_index, low_index)
                if cycle["completed"] == 1:
                    cycle["L1"] = point
        if cycle and cycle["low"] is not None:
            low, high = cycle["low"], cycle["high"]
            if cycle["completed"] == 1 and cycle["start"] is None:
                evidence = _find_start(rows, days, halted, i, idx[low["extreme_date"]], low["confirmed_at"] == day,
                                       cycle["platform"], cycle["impulse_volume"], cycle["pullback_volume"])
                if evidence:
                    cycle["start"] = {"date": day, "evidence_date": evidence["evidence_date"]}
                    cycle["current"] = 2
                    cycle["close_reason"] = cycle["close_reason"] or "closed_by_start"
                    if day == target:
                        second_today = {**evidence, "security_id": symbol, "start_id": cycle["id"] + ":2",
                                        "rule_version": VERSION, "segment_index": 2, "date": day,
                                        "target_high": high, "failure_low": low,
                                        "start_low": row["low"], "start_close": row["close"]}
            # A weak-volume new high advances the structure even without a registered volume start.
            if compare(row["close"], high["price"], "gt"):
                cycle["current"] = cycle["completed"] + 1
                if cycle["completed"] == 1:
                    cycle["close_reason"] = cycle["close_reason"] or "closed_by_new_high"

        if point and point["kind"] == "H":
            if cycle and cycle["current"] > cycle["completed"]:
                cycle.update(completed=cycle["current"], base_low=cycle["low"], high=point, low=None)
            elif cycle is None and ordinary_low is not None:
                amplitude = point["price"] / ordinary_low["price"] - 1
                last_impulse = amplitude
                if compare(amplitude, PARAMETERS["first_impulse_min_return"]):
                    begin, end = idx[ordinary_low["extreme_date"]], idx[point["extreme_date"]]
                    cycle = {
                        "id": f"{symbol}:{days[begin]}:{days[end]}", "L0": ordinary_low, "H1": point, "L1": None,
                        "high": point, "low": None, "base_low": ordinary_low, "current": 1, "completed": 1,
                        "amplitude": amplitude, "evidence": _impulse_evidence(rows, days, halted, begin, end),
                        "impulse_volume": _volumes(rows, days, halted, begin, end), "start": None, "close_reason": None,
                    }
                    invalidation = None
                ordinary_low = None

    cycle = cycle or {}
    l0, h1, l1 = (cycle.get(key) for key in ("L0", "H1", "L1"))
    closed = cycle.get("close_reason")
    suspended = target in halted
    retracement = pullback_volume = pullback = None
    impulse_volume = cycle.get("impulse_volume")
    if h1 and not gap:
        first = idx[h1["extreme_date"]]
        end = idx[l1["extreme_date"]] if l1 else len(days) - 1
        lows = [rows[d]["low"] for d in days[first + 1:end + 1] if d not in halted]
        if lows:
            low_price = l1["price"] if l1 else min(lows)
            retracement = ratio(h1["price"] - low_price, h1["price"] - l0["price"])
        pullback_volume = _volumes(rows, days, halted, first, end)
        if not suspended:
            pullback = 1 - rows[target]["close"] / h1["price"]
    volume_ratio = ratio(pullback_volume["mean"], impulse_volume["mean"]) if pullback_volume and impulse_volume else None
    volume_check = (compare(volume_ratio, PARAMETERS["pullback_volume_ratio_max"], "lt")
                    if pullback_volume and pullback_volume["sessions"] >= PARAMETERS["pullback_min_sessions"] else None)
    phase = None
    if suspended:
        eligible, reason = False, "suspended"
    elif gap or not days:
        eligible, reason = None, "missing_rows"
    elif invalidation:
        eligible, reason = False, "structure_invalid"
    elif closed:
        eligible, reason, phase = False, "first_wave_window_closed", "window_closed"
    elif not h1:
        eligible, reason = ((False, "first_impulse_below_min") if last_impulse is not None
                            else (None, "first_wave_unconfirmed"))
    else:
        eligible = compare(pullback, PARAMETERS["first_pullback_min_pct"])
        reason = None if eligible else "pullback_below_min"
        phase = "pullback_ge_5pct" if eligible else "recovered_lt_5pct"
    amplitude = cycle.get("amplitude", last_impulse)
    return {
        "rule_version": VERSION, "as_of": target, "round_id": cycle.get("id"),
        "data_status": "data_gap" if gap or not days else "known" if amplitude is not None else "warming_up",
        "missing_from": gap, "first_wave_start": l0, "first_wave_high": h1, "first_pullback_low": l1,
        "provisional_extreme": candidate, "current_wave_index": cycle.get("current") if not gap else None,
        "completed_wave_count": cycle.get("completed", 0) if not gap else None,
        "first_impulse_return": amplitude, "first_wave_pullback": pullback, "retrace_pct": retracement,
        "impulse_volume": impulse_volume, "pullback_volume": pullback_volume, "pullback_volume_ratio": volume_ratio,
        "first_pullback_active": bool(h1 and not closed) if not gap else None,
        "first_pullback_eligible": {"value": eligible, "reason_codes": [reason] if reason else []},
        "pullback_phase_at_time": phase, "window_close_reason_codes": [closed] if closed else [],
        "structure_invalidated": invalidation, "second_wave_started": bool(cycle.get("start")) if not gap else None,
        "second_wave_start": cycle.get("start"),
        "required_checks": {
            "first_impulse_return": compare(amplitude, PARAMETERS["first_impulse_min_return"]),
            "first_impulse_evidence": cycle.get("evidence"), "volume": volume_check,
            "retracement": all_of(compare(retracement, PARAMETERS["retrace_min"]),
                                  compare(retracement, PARAMETERS["retrace_max"], "le")), "support": None,
        },
    }, second_today
