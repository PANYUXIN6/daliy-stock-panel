"""每日晚报的纯领域规则。None 表示未知，禁止将缺失解释为零或不成立。"""

from __future__ import annotations

import math
from typing import Any

RULE_VERSION = "evening-v1"
SELECTION_POLICY_VERSION = "observation-v3"
PURPOSES = (
    "风险暂停",
    "地位失效",
    "证据不足",
    "等待位置确认",
    "已确认核心跟踪",
    "新增观察候选",
    "早期变化观察",
)
Tri = bool | None


def all_of(*values: Tri) -> Tri:
    return False if False in values else None if None in values else True


def any_of(*values: Tri) -> Tri:
    return True if True in values else None if None in values else False


def negate(value: Tri) -> Tri:
    return None if value is None else not value


def finite(value: Any, *, positive: bool = False) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and (not positive or value > 0)


def compare(value: Any, threshold: float, op: str = "ge") -> Tri:
    if not finite(value):
        return None
    return {
        "ge": value >= threshold,
        "gt": value > threshold,
        "le": value <= threshold,
        "lt": value < threshold,
    }[op]


def normalized(value: float | None, low: float, high: float) -> float | None:
    return 100 * min(max((value - low) / (high - low), 0), 1) if finite(value) else None


def weighted(values: list[float | None], weights: list[float]) -> float | None:
    return (
        sum(v * w for v, w in zip(values, weights, strict=True)) / sum(weights)
        if all(finite(v) for v in values)
        else None
    )


def judgement(parts: dict[str, Tri], *, mode: str = "and") -> dict:
    value = (all_of if mode == "and" else any_of)(*parts.values())
    return {
        "value": value,
        "failed_reasons": [k for k, v in parts.items() if v is False],
        "unknown_reasons": [k for k, v in parts.items() if v is None],
        "inputs": parts,
    }


def consecutive(previous: int, value: Tri) -> int:
    return previous + 1 if value is True else 0


def advance_round(
    previous: dict | None, *, date: str, entry: Tri, active: Tri, medium_failed: Tri
) -> dict:
    state = dict(previous or {})
    state.setdefault("round_id", None)
    state["entry_count"] = consecutive(state.get("entry_count", 0), entry)
    if state.get("status") in {"active", "paused", "unverified"}:
        state["exit_count"] = consecutive(state.get("exit_count", 0), medium_failed)
        if state["exit_count"] >= 2:
            state.update(status="ended", ended_at=date, entry_count=0)
        else:
            was = state.get("status")
            state["status"] = (
                "active" if active is True else "paused" if active is False else "unverified"
            )
            if was != state["status"]:
                state[f"{state['status']}_at"] = date
    elif state["entry_count"] >= 2:
        state.update(
            status="active",
            round_id=state.get("chain_id", date),
            confirmed_at=date,
            exit_count=0,
            ended_at=None,
        )
    else:
        if entry is True:
            if state["entry_count"] == 1:
                state["chain_id"] = date
            state["status"] = "probing"
        else:
            state.update(status="none", chain_id=None)
    state["observed_at"] = date
    return state


def confirmation(today: Tri, recent: list[Tri], active: Tri) -> Tri:
    history = None if len(recent) != 3 or None in recent else sum(v is True for v in recent) >= 2
    return all_of(today, history, active)


def advance_identity(
    previous: dict | None,
    *,
    date: str,
    entry: Tri,
    confirmed: Tri,
    maintenance: Tri,
    immediate_exit: bool = False,
) -> dict:
    state = dict(previous or {})
    state.setdefault("intervals", [])
    state["verification_events"] = [dict(x) for x in state.get("verification_events", [])]
    state["intervals"] = [dict(x) for x in state["intervals"]]
    live = bool(state["intervals"] and state["intervals"][-1].get("ended_at") is None)
    if live:
        if immediate_exit:
            state["intervals"][-1]["ended_at"] = date
            state.update(status="invalidated", valid=False, failed_count=0, recovery_count=0)
        elif maintenance is None:
            state.update(status="unverified", valid=None, failed_count=0, recovery_count=0)
        elif maintenance is False:
            failures = state.get("failed_count", 0) + 1
            state.update(
                failed_count=failures,
                recovery_count=0,
                valid=failures < 3,
                status="divergent" if failures < 3 else "invalidated",
                recovering=True,
            )
            if failures >= 3:
                state["intervals"][-1]["ended_at"] = date
        else:
            recovery = state.get("recovery_count", 0) + 1
            recovering = state.get("recovering", False) and recovery < 2
            state.update(
                failed_count=0,
                recovery_count=recovery,
                valid=True,
                status="divergent" if recovering else "confirmed",
                recovering=recovering,
            )
    elif confirmed is True and not immediate_exit:
        state["intervals"].append({"confirmed_at": date, "ended_at": None})
        state.update(
            status="confirmed",
            valid=True,
            failed_count=0,
            recovery_count=0,
            recovering=False,
            returned=len(state["intervals"]) > 1,
        )
    else:
        state.update(
            status="candidate" if entry is True else "unverified" if entry is None else "none",
            valid=False,
        )
    if live and state["status"] == "unverified":
        state["unverified_since"] = state.get("unverified_since") or date
    elif state.get("unverified_since"):
        state["verification_events"] = [
            *state.get("verification_events", []),
            {
                "started_at": state["unverified_since"],
                "resolved_at": date,
                "outcome": "invalidated" if state["status"] == "invalidated" else "decidable",
            },
        ]
        state["unverified_since"] = None
    if state["intervals"]:
        state["intervals"][-1]["observed_at"] = date
    state.update(entry=entry, confirmation=confirmed, maintenance=maintenance, observed_at=date)
    return state


def advance_risk(
    previous: dict | None, *, risk: Tri, price_clear: Tri, recovery: Tri, date: str
) -> dict:
    state = dict(previous or {})
    state["clear_count"] = consecutive(state.get("clear_count", 0), price_clear)
    if risk is True:
        state.update(
            paused=True,
            started_at=state.get("started_at") if state.get("paused") else date,
            clear_count=0,
        )
    elif state.get("paused") and state["clear_count"] >= 2 and recovery is True and risk is False:
        state.update(paused=False, ended_at=date)
    state.setdefault("paused", False)
    state["risk_now"] = risk
    return state


def resilience(
    closes: list[float | None],
    atrs: list[float | None],
    peers: list[float | None],
    market: list[float | None],
    dates: list[str],
) -> dict:
    """21 closes / ATR points, 20 peer and benchmark returns; never truncate a missing day."""
    result = {
        "status": "data_missing",
        "quality": None,
        "R": None,
        "D": None,
        "C": None,
        "F": None,
        "pressure_days": None,
        "mature_events": 0,
        "events": [],
        "ongoing": None,
        "local_weak": False,
        "unknown_reasons": [],
    }
    if len(closes) != 21 or not all(finite(x, positive=True) for x in closes):
        result["unknown_reasons"].append("近21个市场交易日价格不完整")
        return result
    returns = [closes[i] / closes[i - 1] - 1 for i in range(1, 21)]
    peak, mdd = closes[0], 0.0
    for close in closes:
        peak = max(peak, close)
        mdd = max(mdd, 1 - close / peak)
    result.update(
        MDD20=mdd,
        DD_now=1 - closes[-1] / max(closes),
        peak_date=dates[max(i for i, x in enumerate(closes) if x == max(closes))],
    )
    vols = [a / c if finite(a, positive=True) else None for a, c in zip(atrs, closes, strict=True)]
    if vols[0] is not None:
        result["C"] = 100 * (1 - min(mdd / (6 * vols[0]), 1))
    pressure = [
        any_of(compare(p, -0.01, "le"), compare(m, -0.01, "le"))
        for p, m in zip(peers, market, strict=True)
    ]
    complete = len(pressure) == 20 and None not in pressure and all(finite(p) for p in peers)
    hits = [i for i, value in enumerate(pressure) if value is True]
    if complete:
        result["pressure_days"] = len(hits)
    zs = [
        max(-2, min(2, (returns[i] - peers[i]) / vols[i]))
        for i in hits
        if finite(peers[i]) and vols[i] is not None
    ]
    if hits and len(zs) == len(hits) and complete:
        win = sum(returns[i] > peers[i] for i in hits) / len(hits)
        result.update(win_ratio=win, D=0.6 * normalized(sum(zs) / len(zs), -1, 1) + 40 * win)
    end = -1
    scores = []
    for i in hits:
        if i <= end:
            continue
        end = i + 3
        event = {
            "start": dates[i + 1],
            "end": dates[end + 1] if end < 20 else None,
            "mature": end < 20,
            "score": None,
        }
        if end < 20 and vols[i] is not None:
            minimum, before = min(closes[i + 1 : i + 5]), closes[i]
            loss = (before - minimum) / before
            score = (
                100
                if loss <= 0.25 * vols[i]
                else 100 * min(max((closes[i + 4] - minimum) / (before - minimum), 0), 1)
            )
            event.update(score=score, held=loss <= 0.25 * vols[i])
            scores.append(score)
        result["events"].append(event)
    result["mature_events"] = len(scores) if complete else None
    result["ongoing"] = any(not e["mature"] for e in result["events"]) if complete else None
    result["F"] = sum(scores) / len(scores) if scores and complete else None
    required_atrs = {0, *hits}
    complete = complete and all(vols[i] is not None for i in required_atrs)
    if complete:
        result["status"] = (
            "untested"
            if not hits
            else "limited"
            if len(hits) < 3 or len(scores) < 2
            else "sufficient"
        )
    else:
        result["unknown_reasons"].append("压力日、同组收益或必要ATR证据缺失")
    if result["status"] == "sufficient":
        values = [result[k] for k in ("D", "C", "F")]
        result["R"] = weighted(values, [0.45, 0.30, 0.25])
        result["quality"] = (
            "good"
            if result["R"] >= 70 and min(values) >= 40
            else "fair"
            if result["R"] >= 60 and min(values) >= 30
            else "poor"
        )
    result["local_weak"] = any(finite(result[k]) and result[k] < 30 for k in ("D", "C", "F"))
    run = 0
    for value in reversed(pressure):
        if value is not True:
            break
        run += 1
    result["consecutive_pressure_days"] = run
    return result


def reference_interval(
    interval: dict, *, k: str | None, dates: list[str], validity: dict[str, Tri]
) -> dict:
    c, e = interval["confirmed_at"], interval.get("ended_at")
    order = None if k is None else c < k
    alive = not (e is not None and dates and e <= dates[-1])
    continuous = all_of(*(validity.get(d) for d in dates)) if dates else None
    return {
        **interval,
        **judgement(
            {"确认严格早于启动": order, "身份区间未失效": alive, "逐日身份有效": continuous}
        ),
    }


def primary_purpose(
    *,
    paused: bool,
    invalidated: bool,
    live: bool,
    maintenance_unknown: bool,
    candidate: bool,
    candidate_unknown: bool,
    position: Tri,
    early: bool,
) -> str | None:
    if paused:
        return PURPOSES[0]
    if invalidated and not live:
        return PURPOSES[1]
    if (maintenance_unknown and not live) or (not live and not candidate and candidate_unknown):
        return PURPOSES[2]
    if (live or candidate) and position is False:
        return PURPOSES[3]
    if live:
        return PURPOSES[4]
    if candidate:
        return PURPOSES[5]
    return PURPOSES[6] if early else None
