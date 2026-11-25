"""五道共同门与适用事件路径。研究字段不参与低吸判断。"""
from __future__ import annotations

from datetime import datetime

from app.services.evening_report_rules import all_of, any_of, judgement
from app.services.evening_report_wave import PARAMETERS, compare

VERSION = "dip-rule-v2"
REASONS = {
    "phase_ebb": "市场处于退潮期",
    "state_cold": "市场情绪冰冷",
    "ge2_zero": "当日无二板及以上个股",
    "structure_invalid": "收盘跌破结构失效线",
    "first_wave_window_closed": "首波回调窗口已关闭",
    "suspended": "当日停牌",
    "identity_gate_failed": "低吸地位条件未达标",
    "amount_below_threshold": "当日成交额未达门槛",
    "pullback_below_min": "当前回撤不足5%",
    "first_impulse_below_min": "已确认摆动涨幅不足20%",
    "first_wave_unconfirmed": "首段高低点尚待确认",
    "provisional_stale": "候选极值已过期",
    "missing_rows": "日线存在未解释缺口",
    "price_basis_unavailable": "价格复权依据待核验",
    "t4_unverified": "严重异动事件依据待核验",
    "required_input_unknown": "必要条件资料待核验",
    "no_applicable_event": "当前无适用的严重异动事件",
    "path_not_met": "适用路径的数值形态未满足",
    "gate_passed_path_partial": "共同资格通过，适用路径仅部分满足",
    "support_missing": "止跌支撑资料待核验",
    "support_not_met": "止跌支撑未成立",
    "volume_quality_unknown": "正式回调缩量待核验",
    "volume_quality_not_met": "回调未缩量或首段量价证据未成立",
    "wave_quality_weak": "首波回撤质量未达标",
    "all_gates_and_path_passed": "五道门、首波质量和适用形态均通过",
}
QUALITY_FIELDS = ("first_impulse_return", "first_impulse_evidence", "retracement", "volume", "support")


def _bounded(value, lower, upper):
    return all_of(compare(value, lower), compare(value, upper, "le"))


def _check(check_id, value, raw, threshold, unit, day, known_at):
    return {"check_id": check_id, "status": "passed" if value is True else "failed" if value is False else "unknown",
            "value": raw, "threshold": threshold, "unit": unit, "as_of": day, "known_at": known_at}


def _event_for_day(event, day, cutoff):
    if not event or event.get("applicable") is None or not event.get("known_at"):
        return None
    known_at = datetime.fromisoformat(event["known_at"])
    if known_at.tzinfo is None or (cutoff and known_at > datetime.fromisoformat(cutoff)):
        return None
    if not event["applicable"]:
        return False
    if not event.get("valid_from") or not event.get("valid_until"):
        return None
    return event["valid_from"] <= day <= event["valid_until"]


def _branch(name, applicable, passed, raw, threshold, unit, day, known_at):
    check = _check(name, passed, raw, threshold, unit, day, known_at)
    check.update(applicable=applicable, passed=passed,
                 reason=("不适用" if applicable is False else "适用依据待核验" if applicable is None
                         else "原值待核验" if passed is None else "达标" if passed else "未达标"))
    if applicable is False:
        check["status"] = "not_applicable"
    return check


def dip_labels(values, role, market, regime, *, wave=None, events=None, day="", cutoff=""):
    wave = wave or {}
    events = events or {}
    structure = wave.get("first_pullback_eligible", {"value": None, "reason_codes": ["price_basis_unavailable"]})
    quality = wave.get("required_checks", {})
    quality_passed = all_of(*(quality.get(field) for field in QUALITY_FIELDS))
    notes = []

    def note(code, *, check_id=None, path=None):
        notes.append({"code": code, "message": REASONS[code], "check_id": check_id, "path": path})

    classification = (market.get("sentiment") or {}).get("classification") or market.get("classification") or {}
    state = classification.get("sentiment_state")
    phase = market.get("phase") or classification.get("phase")
    ge2 = market.get("ge2_count")
    if ge2 is None:
        ge2 = classification.get("evidence", {}).get("ge2_count")
    environment = False if phase == "ebb" or state == "cold" else (
        None if phase in (None, "unknown") or state in (None, "unknown") else True)
    for value, code in ((phase == "ebb", "phase_ebb"), (state == "cold", "state_cold"), (ge2 == 0, "ge2_zero")):
        if value:
            note(code)
    leader = all_of(compare(values.get("space_score"), 50), compare(values.get("identity_score"), 45),
                    compare(values.get("high_proximity"), .85))
    emotion = all_of(role.get("emotion_attribute"), compare(values.get("identity_score"), 60),
                     compare(values.get("return_20d"), .20))
    acceleration_thresholds = {"return_10d": .30, "return_5d": .03, "high_proximity": .85}
    acceleration = all_of(*(compare(values.get(key), threshold)
                            for key, threshold in acceleration_thresholds.items()))
    first_wave_quality = all_of(wave.get("first_pullback_active"), quality_passed)
    exemption = _branch("first_wave_acceleration_exemption", first_wave_quality,
                        acceleration if first_wave_quality is not False else None,
                        {key: values.get(key) for key in acceleration_thresholds}, acceleration_thresholds,
                        "ratio", day, cutoff)
    permissive = all_of(first_wave_quality, acceleration)
    standard, relaxed = {"10%": (500_000_000, 250_000_000), "20%": (800_000_000, 400_000_000)}.get(regime, (None, None))
    amount = values.get("amount_day")
    standard_pass = compare(amount, standard) if standard is not None else None
    relaxed_pass = compare(amount, relaxed) if relaxed is not None else None
    allowance = _branch("first_wave_relaxed_amount", permissive, relaxed_pass if permissive is not False else None,
                        amount, relaxed, "CNY", day, cutoff)
    gates = {
        "environment": environment, "identity": any_of(leader, emotion, all_of(first_wave_quality, acceleration)),
        "amount": any_of(standard_pass, all_of(permissive, relaxed_pass)),
        "ge2": compare(ge2, 0, "gt"), "first_pullback": structure["value"],
    }
    for gate, value in gates.items():
        if gate == "first_pullback":
            for code in structure["reason_codes"]:
                note(code, check_id=gate)
        elif value is None:
            note("required_input_unknown", check_id=gate)
        elif value is False and gate in ("identity", "amount"):
            note("identity_gate_failed" if gate == "identity" else "amount_below_threshold", check_id=gate)
    for key, code_missing, code_failed in (
        ("support", "support_missing", "support_not_met"),
        ("volume", "volume_quality_unknown", "volume_quality_not_met"),
        ("first_impulse_evidence", "volume_quality_unknown", "volume_quality_not_met"),
        ("retracement", None, "wave_quality_weak"),
    ):
        value = quality.get(key)
        if value is None and code_missing:
            note(code_missing, check_id=key)
        elif value is False:
            note(code_failed, check_id=key)
    ma, bull_bear = values.get("distance_ma10"), values.get("distance_bull_bear")
    specs = {
        "e1": [
            ("return_3d", compare(values.get("return_3d"), .15, "le"), values.get("return_3d"), {"max": .15}),
            ("volume_ratio_2_10", compare(values.get("volume_ratio_2_10"), .65, "le"), values.get("volume_ratio_2_10"), {"max": .65}),
            ("distance_ma10", _bounded(ma, -.02, .06), ma, {"min": -.02, "max": .06}),
            ("amplitude_ratio_2_10", compare(values.get("amplitude_ratio_2_10"), 1.2, "le"), values.get("amplitude_ratio_2_10"), {"max": 1.2}),
        ],
        "e2": [
            ("volume_ratio_2_10", compare(values.get("volume_ratio_2_10"), .55, "le"), values.get("volume_ratio_2_10"), {"max": .55}),
            ("rising_lows_3d", values.get("rising_lows_3d"), values.get("rising_lows_3d"), True),
            ("distance_bull_bear", _bounded(bull_bear, -.05, .05), bull_bear, {"min": -.05, "max": .05}),
        ],
    }
    paths = {}
    for name, rules in specs.items():
        event = events.get(name)
        applicable = _event_for_day(event, day, cutoff)
        path = judgement({key: value for key, value, _, _ in rules})
        paths[name] = {**path, "event_applicable": applicable, "event": event,
                       "checks": [_check(key, value, raw, threshold, "boolean" if key == "rising_lows_3d" else "ratio", day, cutoff)
                                  for key, value, raw, threshold in rules]}
    applicable_paths = {name: path for name, path in paths.items() if path["event_applicable"] is True}
    evidence_passes = any(True in path["inputs"].values() for path in applicable_paths.values())
    status = "none"
    if all_of(*gates.values()) is True:
        if quality_passed is True and any(path["value"] is True for path in applicable_paths.values()):
            status = "full"
        elif evidence_passes:
            status = "partial"
    if status == "full":
        notes.clear()
        note("all_gates_and_path_passed")
    else:
        for name, path in paths.items():
            if path["event_applicable"] is None:
                note("t4_unverified", path=name)
            elif path["event_applicable"] is True:
                for check_id in path["unknown_reasons"]:
                    note("required_input_unknown", check_id=check_id, path=name)
                if not path["unknown_reasons"] and True not in path["inputs"].values():
                    note("path_not_met", path=name)
        if all(path["event_applicable"] is False for path in paths.values()):
            note("no_applicable_event")
        if status == "partial" and any(path["value"] is not True and True in path["inputs"].values()
                                       for path in applicable_paths.values()):
            note("gate_passed_path_partial")
    reason_codes = [code for code in REASONS if any(entry["code"] == code for entry in notes)]
    return {"status": status, "rule_version": VERSION, "reason_codes": reason_codes,
            "primary_reason_code": reason_codes[0] if reason_codes else None,
            "primary_reason": REASONS[reason_codes[0]] if reason_codes else None,
            "reasons": [REASONS[code] for code in reason_codes], "reason_details": notes,
            "gates": judgement(gates), **paths, "leader_qualification": leader, "emotion_qualification": emotion,
            "amount_threshold": standard, "risk_hot_fragile": state == "hot_fragile", "branches": [exemption, allowance],
            "checks": [
                _check("environment", environment, {"phase": phase, "sentiment_state": state}, "允许低吸", "state", day, cutoff),
                _check("identity", gates["identity"], {"leader": leader, "emotion": emotion, "acceleration": acceleration},
                       "任一地位分支成立", "boolean", day, cutoff),
                _check("amount", gates["amount"], amount, {"standard": standard, "relaxed": relaxed}, "CNY", day, cutoff),
                _check("ge2", gates["ge2"], ge2, {"min_exclusive": 0}, "count", day, cutoff),
                _check("first_pullback", gates["first_pullback"], wave.get("first_wave_pullback"),
                       {"min": PARAMETERS["first_pullback_min_pct"]}, "ratio", day, cutoff),
            ]}
