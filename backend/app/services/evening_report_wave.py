"""收盘确认的逐日波次。运行状态只用于后续交易日，推荐快照独立归档。"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from copy import deepcopy
from itertools import chain, pairwise
from math import isclose, prod
from operator import itemgetter
from statistics import mean
from typing import TypedDict

import polars as pl

from app.services.evening_report_receipt import daily_partition_versions
from app.services.evening_report_rules import all_of, any_of, finite

VERSION = "wave-rule-v1"
ADJUST_RULE = "raw-times-event-cumfactor-over-base-v1"
PARAMETERS = {
    "zigzag_reversal_pct": .05, "first_impulse_min_return": .20, "evidence_lookback": 20,
    "first_impulse_volume_multiple": 1.5, "retrace_min": .25, "retrace_max": .75,
    "pullback_volume_ratio_max": 1., "pullback_min_sessions": 2,
    "start_volume_vs_pullback": 1.3, "start_volume_vs_impulse": .8, "support_distance": .03,
    "first_pullback_min_pct": .05, "provisional_max_idle_sessions": 20,
}


class RequiredChecks(TypedDict):
    first_impulse_return: bool | None
    first_impulse_evidence: bool | None
    retracement: bool | None
    volume: bool | None
    support: bool | None


class AnalysisFeatures(TypedDict):
    variable_refs: list[str]
    start_amount_as_of: str | None


def compare(value, threshold, op="ge"):
    """Treat arithmetic noise at an explicit inclusive/exclusive boundary consistently."""
    if not finite(value):
        return None
    if isclose(value, threshold, rel_tol=0, abs_tol=1e-12):
        return op in ("ge", "le")
    return {"ge": value >= threshold, "gt": value > threshold,
            "le": value <= threshold, "lt": value < threshold}[op]


def ratio(numerator, denominator):
    return numerator / denominator if finite(numerator) and finite(denominator, positive=True) else None


def start_key(event):
    return event["security_id"], event["start_id"], event["rule_version"]


STRUCTURE_EVENT_TYPES = ("wave_new_high", "wave_high_confirmed", "candidate_expired", "structure_invalid")

WINDOW_CLOSE_REASONS = {
    "wave_started": "closed_by_start", "wave_new_high": "closed_by_new_high", "candidate_expired": "closed_by_stale",
}


def structure_event_key(event):
    key = event["event_id"], event.get("round_id")
    if event["type"] == "candidate_expired":
        return *key, event["candidate_id"], event["last_extreme_date"]
    if event["type"] == "structure_invalid":
        line = event["failure_low"]
        # The event is the breach of this line. The first accepted close is its evidence.
        return *key, line["pivot_id"], line["raw_price"]
    anchors = (event["target_high"], event["after_low"]) if event["type"] == "wave_new_high" else (event["pivot_id"],)
    return *key, event["segment_index"], *anchors


def wave_event_key(event):
    # Segment identity deduplicates published starts. A provisional receipt proves only its own clue.
    if event["type"] == "wave_started":
        return *start_key(event), event["date"], event["evidence_date"]
    return structure_event_key(event)


def known_by(event):
    """Latest time proved by the available evidence, including events with an unknown first receipt."""
    return event["known_at"] or event["known_by"]


def start_receipt(event):
    return event["known_at"] or {key: event[key] for key in ("known_by", "acceptance_gap_after")}


def window_close_from_events(events):
    known_at = min(map(known_by, events))
    unresolved = [event for event in events if event.get("acceptance_gap_after", known_at) < known_at]
    if unresolved:
        return {"date": None, "known_at": None, "known_by": known_at,
                "acceptance_gap_after": min(event["acceptance_gap_after"] for event in unresolved),
                "reason_codes": [], "event_refs": []}
    first = [event for event in events if event["known_at"] == known_at]
    order = tuple(WINDOW_CLOSE_REASONS)
    first.sort(key=lambda event: (order.index(event["type"]), event["date"], event["event_id"]))
    return {"date": min(event["date"] for event in first), "known_at": known_at,
            "reason_codes": list(dict.fromkeys(WINDOW_CLOSE_REASONS[event["type"]] for event in first)),
            "event_refs": list(dict.fromkeys(event["event_id"] for event in first))}


class PriceBasis:
    def __init__(self, factors, source, known_at, *, factor_known_at=None):
        self.factors = factors
        self.dates = [day for day, _ in factors]
        self.source = source
        self.known_at = known_at
        self.factor_known_at = factor_known_at or {}
        self.observed_dates = sorted(self.factor_known_at)
        self.prior_versions = ()

    @classmethod
    def from_accepted_facts(cls, facts, source, known_at):
        factors = sorted((day, value) for day, (value, _) in facts.items() if value != 1.)
        observed = {day: at for day, (_, at) in facts.items()}
        return cls(factors, source, known_at, factor_known_at=observed)

    def available_at(self, day, base):
        if not self.observed_dates or day == base:
            return ""
        start, end = sorted((day, base))
        left = bisect_right(self.observed_dates, start)
        right = bisect_right(self.observed_dates, end)
        return max((self.factor_known_at[d] for d in self.observed_dates[left:right]), default="")

    def multiplier(self, day, base):
        left, right = bisect_right(self.dates, day), bisect_right(self.dates, base)
        if left > right:
            return prod(value for _, value in self.factors[right:left])
        return 1 / prod(value for _, value in self.factors[left:right])

    def price(self, raw, day, base):
        return raw * self.multiplier(day, base)

    def evidence(self, raw, key, day, base, *, at=None):
        multiple = self.multiplier(day, base)
        source = self.source
        known = self.available_at(day, base) or self.known_at
        if at and max(known, self.known_at) > at:
            source = None
            known = known if known <= at else None
            for older in reversed(self.prior_versions):
                observed = older.available_at(day, base) or older.known_at
                if max(observed, older.known_at) <= at and older.multiplier(day, base) == multiple:
                    source, known = older.source, observed
                    break
        return {"raw_price": raw, "price_field": key, "price": raw * multiple,
                "applied_adjustment_multiplier": multiple, "adjust_base_date": base,
                "adjust_rule_version": ADJUST_RULE, "factor_source": source, "factor_known_at": known}


def _joint_input_time(observed, factor_resumptions):
    if factor_resumptions:
        i = bisect_right(factor_resumptions, observed, key=itemgetter(0))
        if i:
            return max(observed, factor_resumptions[i - 1][1])
    return observed


class _WaveReplay:
    """First observations under the accepted price and factor versions preceding a revision."""

    def __init__(self, previous_stock, first_day=None):
        self.first_day = first_day
        self.known = {}
        self.points = {}
        self.events = {}
        self.before = None
        self.revised_from = None
        self.revised_at = None
        state = (previous_stock or {}).get("state", {})
        if candidate := state.get("candidate"):
            key = "candidate", candidate["candidate_id"], candidate["extreme_date"], candidate["raw_price"]
            self.known[key] = candidate["known_at"]
        points = [state.get("ordinary_low")]
        warm = state.get("warmed")
        if warm:
            low, high = warm["low"], warm["high"]
            points.extend((low, high))
            self.known[("impulse", low["pivot_id"], high["pivot_id"], warm["first_impulse_return"])] = warm["known_at"]
        for cycle in (state.get("round"), state.get("ended_round")):
            if cycle:
                points.extend(cycle["pivots"])
                for event in cycle["breakouts"].values():
                    if event and event["known_at"]:
                        self.known[structure_event_key(event)] = event["known_at"]
                invalidated = cycle.get("invalidated")
                if invalidated and invalidated["known_at"]:
                    self.events[structure_event_key(invalidated)] = invalidated
        for point in points:
            if point:
                key = point["pivot_id"], point["raw_price"]
                self.points[key] = point
                self.known[key] = point["known_at"]

    def time(self, key, day, observed):
        if key in self.known:
            return self.known[key]
        if self.first_day is None or day < self.first_day:
            return observed
        if self.revised_from and day >= self.revised_from:
            observed = max(observed, self.revised_at)
        if self.before and observed < self.before:
            self.known[key] = observed
        return observed

    def point(self, point):
        key = point["pivot_id"], point["raw_price"]
        previous = self.points.get(key)
        if previous and previous["known_at"] <= point["known_at"] and (
            previous.get("factor_known_at") or previous["known_at"]
        ) <= point["known_at"]:
            # A confirmed pivot keeps its original price proof. Later comparisons rebase its raw price.
            for field in ("price", "applied_adjustment_multiplier", "factor_source", "factor_known_at"):
                point[field] = previous[field]
        if self.before and key in self.known and point["known_at"] < self.before:
            self.points[key] = point

    def event(self, key, event):
        saved = self.events.get(key)
        if saved and (not self.before or saved["known_at"] < self.before):
            return saved
        event["known_at"] = self.time(key, event["date"], event["known_at"])
        if self.before and event["known_at"] < self.before and key in self.known:
            self.events[key] = event
        return event


class WaveRun:
    def __init__(self, symbol, days, rows, halted, basis, cutoff, state=None, *,
                 calendar_index=None, prior_state=None, previous_cutoff=None, published_start=None, published_close=None,
                 published_structure_event=None, replay=None, first_acceptance=None, factor_available_from="",
                 price_bootstrap=None, acceptance_gaps=(), start_checkpoint_until=None, deferred_events=None,
                 unverified_receipts=None, factor_resumptions=()):
        self.symbol = symbol
        self.days = days
        self.index = calendar_index if calendar_index is not None else {day: i for i, day in enumerate(days)}
        self.rows = rows
        self.halted = halted
        self.basis = basis
        self.factor_available_from = factor_available_from
        self.factor_resumptions = factor_resumptions
        self.price_bootstrap = price_bootstrap or {}
        self.start_checkpoint_until = start_checkpoint_until
        self.acceptance_gaps = acceptance_gaps
        self.unverified_receipts = unverified_receipts or {}
        self.encountered_gaps = set()
        self.deferred_events = deferred_events or {}
        self.inputs_available_from = max(first_acceptance or "", factor_available_from)
        self.cutoff = cutoff
        self.target = days[-1]
        prior_state = prior_state or state or {}
        self.collect_after = prior_state.get("date")
        self.processed_through = (state or prior_state).get("date")
        self.previous_cutoff = previous_cutoff or (state or {}).get("known_at")
        # At a security's first acceptance, older bars establish its opening wave state.
        self.warmup_cutoff = ""
        if not self.collect_after and cutoff:
            observed = (row["known_at"] for row in rows.values() if row.get("known_at"))
            self.warmup_cutoff = first_acceptance or min(observed, default=cutoff)
        self.published_start = published_start
        self.replay = replay
        self.published_close = published_close
        self.published_structure_event = (published_structure_event if state is None
                                          or self.processed_through != self.collect_after else None)
        self.events = []
        self.starts = []
        self.window_closes = {}
        self._closing_events = {}
        self.state = deepcopy(state) if state else {
            "date": None, "direction": "H", "saw_decline": False, "warmed": None,
            "candidate": None, "session": 0, "ordinary_low": None, "round": None,
            "ended_round": None, "density_points": [], "density_days": {}, "density_rounds": {}, "rebuilding": False,
        }
        # Historical warm-up starts need only their first-known time after the round rolls out of state.
        self.state["start_known_at"] = dict(prior_state.get("start_known_at", {}))
        self.prior_rounds = {cycle["round_id"]: cycle for cycle in (prior_state.get("round"), prior_state.get("ended_round"))
                             if cycle is not None}
        self.second_wave_start_evaluable = None

    def known_at(self, day):
        observed = self.rows.get(day, {}).get("known_at") or self.cutoff or day
        known = max(observed, self.state.get("known_at", ""), self.factor_available_from)
        return _joint_input_time(known, self.factor_resumptions)

    def new_fact_day(self, day):
        return day > self.collect_after if self.collect_after else day == self.target

    def receipt_in_report(self, event, known, *, upper_bound=False):
        if known is None:
            return False
        if not self.cutoff:
            return True
        # A published report may have stopped at an unresolved market-date gap.
        # Its checkpoint closes only the sessions the security actually processed.
        if (self.previous_cutoff and event["date"] <= self.previous_cutoff[:10]
                and (not self.collect_after or event["date"] <= (self.processed_through or ""))):
            return False
        if self.deferred_events and wave_event_key(event) in self.deferred_events:
            return True
        if not (self.previous_cutoff or self.target) < known <= self.cutoff:
            return False
        if event["date"] == known[:10]:
            return True
        if upper_bound:
            # The surviving input proves the event by this cutoff, even after an unchecked rewrite.
            return self.warmup_cutoff < known
        # An untracked market-data rewrite cannot establish the first receipt of an affected bar.
        unresolved = self.price_bootstrap.get(known, {}).get(self.symbol)
        # The preceding published ledger proves which starts existed until this stock's next acceptance.
        if (unresolved and event["type"] == "wave_started" and self.start_checkpoint_until
                and known <= self.start_checkpoint_until):
            return True
        return self.warmup_cutoff < known and (not unresolved or event["date"] < unresolved)

    def _first_receipt_gap(self, day, observed):
        if not observed or not (self.acceptance_gaps or self.unverified_receipts):
            return None
        # A later revision can restore an event that disappeared before the gap was checkpointed.
        explicit = []
        for repaired_at, (after, through, scope) in self.unverified_receipts.items():
            if observed < repaired_at:
                continue
            if isinstance(scope, dict):
                bound = scope.get(self.symbol)
                if not bound:
                    continue
                first_day, last_day = bound if isinstance(bound, list) else (bound, through[:10])
                if first_day <= day <= last_day and self.inputs_available_from <= through:
                    explicit.append((after, through))
            elif day <= scope:
                explicit.append((after, through))
        stock = ((after, through) for after, through, first_affected_day in self.acceptance_gaps
                 if observed != self.inputs_available_from and observed > after and self.inputs_available_from <= through
                 and first_affected_day <= day <= through[:10])
        return min(chain(explicit, stock), default=None)

    def check_first_receipt(self, event, *, report=True):
        # A deferred event already proved its first receipt before the acceptance files could expire.
        if self.deferred_events and (deferred := self.deferred_events.get(wave_event_key(event))):
            return deferred
        observed = event["known_at"]
        if gap := self._first_receipt_gap(event["date"], observed):
            if report and observed > (self.previous_cutoff or ""):
                self.encountered_gaps.add(gap)
            return {**event, "known_at": None, "known_by": observed, "acceptance_gap_after": gap[0]}
        return event

    def price(self, day, field, base):
        return self.basis.price(self.rows[day]["raw_" + field], day, base)

    def point_price(self, point, base):
        return self.basis.price(point["raw_price"], point["extreme_date"], base)

    def observe_anchor(self, point, day):
        """Upper bound on the availability of this path under the current factor version."""
        self.state["known_at"] = max(self.known_at(day), point["known_at"],
                                     self.basis.available_at(point["extreme_date"], day))

    def event(self, event_type, day, *, known_at=None, record=True, **fields):
        event = {"event_id": f"{VERSION}:{self.symbol}:{day}:{event_type}",
                 "type": event_type, "date": day, "known_at": known_at or self.known_at(day),
                 "symbol": self.symbol, **fields}
        if self.replay and (event_type == "wave_started" or event_type in STRUCTURE_EVENT_TYPES):
            event = self.replay.event(wave_event_key(event), event)
        if event_type == "candidate_expired":
            event = self.check_first_receipt(event)
        if record and event["known_at"] and (not self.cutoff or self.new_fact_day(day)):
            self.events.append(event)
        return event

    def _point(self, candidate, day):
        extreme = candidate["extreme_date"]
        key = "high" if candidate["kind"] == "H" else "low"
        pivot_id = f'{candidate["candidate_id"]}:{extreme}:{day}'
        known_at = max(candidate["known_at"], self.known_at(day), self.basis.available_at(extreme, day))
        if self.replay:
            known_at = self.replay.time((pivot_id, candidate["raw_price"]), day, known_at)
        point = {
            "pivot_id": pivot_id, "status": "confirmed",
            "kind": candidate["kind"], "extreme_date": extreme, "confirmed_at": day,
            "known_at": known_at, **self.basis.evidence(candidate["raw_price"], key, extreme, day, at=known_at),
        }
        if self.replay:
            self.replay.point(point)
        self.state["density_points"].append({"pivot_id": point["pivot_id"], "confirmed_at": day,
                                             "known_at": point["known_at"], "round_id": None})
        return point

    def _range(self, start, end):
        return self.days[self.index[start] + 1:self.index[end] + 1]

    def _volumes(self, start, end):
        days = self._range(start, end)
        rows = [self.rows.get(day, {}) for day in days if day not in self.halted]
        values = [row.get("volume") for row in rows]
        valid = [value for value in values if finite(value)]
        return {"mean": mean(valid) if valid and len(valid) == len(values) else None,
                "sessions": len(valid), "suspended": len(days) - len(rows)}

    def _impulse_evidence(self, low, high):
        beginning = self.index[low["extreme_date"]]
        ending = self.index[high["extreme_date"]]
        checks = []
        lookback = PARAMETERS["evidence_lookback"]
        for index in range(beginning, ending + 1):
            day = self.days[index]
            if day in self.halted:
                continue
            prior = self.days[max(0, index - lookback):index]
            row = self.rows.get(day, {})
            if len(prior) != lookback or any(p in self.halted or not self.rows.get(p, {}).get("wave_formal") for p in prior):
                checks.append(None)
                continue
            volumes = [self.rows[p].get("volume") for p in prior]
            average = mean(volumes) if all(finite(v) for v in volumes) else None
            close = self.price(day, "close", day)
            bullish = close > self.price(day, "open", day)
            volume_rule = all_of(bullish, compare(ratio(row.get("volume"), average),
                                                  PARAMETERS["first_impulse_volume_multiple"]))
            breakout = compare(close / max(self.price(p, "high", day) for p in prior), 1., "gt")
            if True in (volume_rule, breakout):
                return True
            checks.append(any_of(volume_rule, breakout))
        return any_of(*checks) if checks else None

    def _invalidate(self, day, line):
        state = self.state
        cycle = state["round"]
        raw = self.rows[day]["raw_close"]
        close = {"raw_price": raw}
        event = self.event("structure_invalid", day, record=False, round_id=cycle["round_id"],
                           failure_low=line, close=close)
        # event() can return the original receipt. Price only the newly created close.
        if event["close"] is close:
            event["close"] = self.basis.evidence(raw, "close", day, day, at=event["known_at"])
        previous = self.prior_rounds.get(cycle["round_id"], {}).get("invalidated")
        cycle["invalidated"] = self._collect_structure_event(event, previous)
        state.update(round=None, ended_round=cycle, candidate=None, direction="L",
                     ordinary_low=None, rebuilding=False, saw_decline=True)

    def _update_candidate(self, day):
        state = self.state
        kind = state["direction"]
        source = "high" if kind == "H" else "low"
        raw = self.rows[day]["raw_" + source]
        session = state["session"]
        candidate = state["candidate"]
        if candidate is None:
            candidate = {"candidate_id": f"{VERSION}:{self.symbol}:{kind}:{day}",
                         "kind": kind, "created_at": day, "created_session": session}
            changed = True
        else:
            self.observe_anchor(candidate, day)
            price = self.basis.price(raw, day, day)
            previous = self.point_price(candidate, day)
            changed = compare(price / previous, 1., "gt" if kind == "H" else "lt")
        if changed:
            prior_session = candidate.get("extreme_session", session)
            known_at = self.known_at(day)
            if self.replay:
                known_at = self.replay.time(("candidate", candidate["candidate_id"], day, raw), day, known_at)
            candidate.update(extreme_date=day, raw_price=raw, extreme_session=session,
                             known_at=known_at)
            state["candidate"] = candidate
            self.event("candidate_moved", day, known_at=known_at, candidate_id=candidate["candidate_id"], kind=kind,
                       candidate_age=session - candidate["created_session"], since_last_extreme=session - prior_session,
                       **self.basis.evidence(raw, source, day, day, at=known_at))
        age = session - candidate["extreme_session"]
        relative = self.price(day, "close", day) / self.point_price(candidate, day) - 1
        reversal = PARAMETERS["zigzag_reversal_pct"]
        if compare(relative, -reversal if kind == "H" else reversal, "le" if kind == "H" else "ge"):
            state.update(candidate=None, direction="L" if kind == "H" else "H")
            if not state["saw_decline"]:
                state["saw_decline"] = True
                known_at = self.known_at(day)
                if self.replay:
                    key = "initial_decline", candidate["candidate_id"], candidate["extreme_date"], candidate["raw_price"], day
                    known_at = self.replay.time(key, day, known_at)
                self.event("initial_decline", day, known_at=known_at, candidate_id=candidate["candidate_id"])
                return None, None
            if kind == "H" and state["ordinary_low"] is not None:
                self.observe_anchor(state["ordinary_low"], day)
            point = self._point(candidate, day)
            self.event("pivot_confirmed", day, known_at=point["known_at"], point=point,
                       candidate_id=candidate["candidate_id"])
            return point, None
        if age > PARAMETERS["provisional_max_idle_sessions"]:
            stale = self.event("candidate_expired", day, candidate_id=candidate["candidate_id"],
                               round_id=(state["round"] or {}).get("round_id"),
                               last_extreme_date=candidate["extreme_date"], idle_sessions=age)
            state.update(candidate=None, ordinary_low=None, rebuilding=state["round"] is not None)
            if state["round"] is None:
                state.update(direction="H", saw_decline=False)
            return None, stale
        return None, None

    def _record_low(self, point, cycle):
        if cycle is None:
            self.state["ordinary_low"] = point
            return
        if cycle["low"] is None and self._continues_pullback(point, cycle):
            cycle["low"] = point
            high_day = cycle["high"]["extreme_date"]
            segment_days = self._range(high_day, point["extreme_date"])
            # Keep the admitted closes. A late adjustment can change which session
            # supplies the platform maximum when comparing a future breakout.
            cycle["platform_closes"] = [(day, self.rows[day]["raw_close"])
                                        for day in segment_days if day not in self.halted]
            cycle["pullback_volume"] = self._volumes(high_day, point["extreme_date"])
            if cycle["completed"] == 1:
                cycle["first_pullback_volume"] = cycle["pullback_volume"]
                cycle["first_low"] = point

    def _record_high(self, point, day, cycle):
        if cycle is not None:
            if cycle["current"] > cycle["completed"]:
                segment = cycle["current"]
                point["segment_index"] = segment
                cycle.update(completed=cycle["current"], high=point, low=None,
                             base_low=cycle.pop("next_base_low"), last_completed_at=day)
                cycle["impulse_volume"] = self._volumes(cycle["base_low"]["extreme_date"], point["extreme_date"])
                known_at = max(point["known_at"], known_by(cycle["breakouts"][str(segment)]))
                high = self.event("wave_high_confirmed", day, known_at=known_at, record=False,
                                  round_id=cycle["round_id"], segment_index=segment, pivot_id=point["pivot_id"])
                point["known_at"] = max(point["known_at"], high["known_at"])
                self._collect_structure_event(high)
            return
        low = self.state["ordinary_low"]
        if low is None:
            return
        amplitude = self.point_price(point, day) / self.point_price(low, day) - 1
        known_at = max(self.known_at(day), low["known_at"], point["known_at"])
        if self.replay:
            known_at = self.replay.time(("impulse", low["pivot_id"], point["pivot_id"], amplitude), day, known_at)
        if self.state["warmed"] is None:
            self.state["warmed"] = {"known_at": known_at, "low": low, "high": point,
                                    "first_impulse_return": amplitude}
            self.state.pop("warmup_gap", None)
        self.state["last_impulse"] = {"low": low, "high": point, "return": amplitude}
        if not compare(amplitude, PARAMETERS["first_impulse_min_return"]):
            return
        round_id = f'{VERSION}:{self.symbol}:{low["extreme_date"]}:{point["extreme_date"]}'
        volume = self._volumes(low["extreme_date"], point["extreme_date"])
        prior_close = self.prior_rounds.get(round_id, {}).get("window_close")
        if self.published_close:
            archived_close = self.published_close(round_id, point["extreme_date"])
            if archived_close and (prior_close is None or known_by(archived_close) <= known_by(prior_close)):
                prior_close = archived_close
        self.state["round"] = {
            "round_id": round_id, "current": 1, "completed": 1, "last_completed_at": day,
            "base_low": low, "high": point, "low": None, "first_low": None, "first_high": point,
            "first_start": low, "first_return": amplitude, "first_evidence": self._impulse_evidence(low, point),
            "impulse_volume": volume, "first_impulse_volume": volume, "first_pullback_volume": None,
            "pivots": [low], "starts": {}, "breakouts": {}, "window_close": prior_close,
        }
        self.state["ordinary_low"] = None
        for entry in self.state["density_points"]:
            if entry["pivot_id"] == low["pivot_id"]:
                entry["round_id"] = round_id
        if self.replay:
            known_at = self.replay.time(("round", low["pivot_id"], point["pivot_id"]), day, known_at)
        self.event("round_established", day, known_at=known_at, round_id=round_id, pivots=[low["pivot_id"], point["pivot_id"]])

    def _start_known_at(self, day, evidence_day, cycle, price_branches):
        low, high = cycle["low"], cycle["high"]
        available_at = self.basis.available_at
        branch_times = []
        if "platform" in price_branches:
            branch_times.append(available_at(high["extreme_date"], day))
        if "ma10" in price_branches:
            branch_times.append(max(available_at(self.rows[d].get("ma10_from") or d, d)
                                    for d in (evidence_day, day)))
        return max(high["known_at"], low["known_at"], self.known_at(day), self.known_at(evidence_day),
                   available_at(cycle["base_low"]["extreme_date"], low["extreme_date"]),
                   available_at(low["extreme_date"], evidence_day), min(branch_times))

    def _start_anchor(self, point, day, known_at):
        # Keep the confirmed pivot identity. A later-accepted event effective before
        # its confirmation may change the multiplier already embedded in its price.
        extreme = point["extreme_date"]
        if self.basis.multiplier(extreme, point["adjust_base_date"]) == point["applied_adjustment_multiplier"]:
            return point
        return {**point, **self.basis.evidence(point["raw_price"], point["price_field"], extreme, day, at=known_at)}

    def _above_ma10(self, day):
        row = self.rows[day]
        first = row.get("ma10_from")
        # Replayed prices and post-ST averages must span exactly ten non-halted sessions too.
        if first and (first not in self.index or (self.index[day] - self.index[first] != 9
                      and sum(d not in self.halted for d in self._range(first, day)) != 9)):
            return None
        return compare(ratio(row.get("close"), row.get("ma10")), 1., "gt")

    def _start(self, day, cycle, segment):
        low, high = cycle["low"], cycle["high"]
        if str(segment) in cycle["starts"]:
            return None, True
        platform = max(self.basis.price(close, session, day) for session, close in cycle["platform_closes"])
        current = self.price(day, "close", day)
        branch_platform = compare(current / platform, 1., "gt")
        branch_ma = self._above_ma10(day)
        if any_of(branch_platform, branch_ma) is False:
            return None, True
        pullback, impulse = cycle["pullback_volume"], cycle["impulse_volume"]
        a, b = pullback["mean"], impulse["mean"]
        if pullback["sessions"] < PARAMETERS["pullback_min_sessions"] or not finite(a, positive=True) or not finite(b, positive=True):
            return None, None
        a_min = PARAMETERS["start_volume_vs_pullback"] * a
        b_min = PARAMETERS["start_volume_vs_impulse"] * b
        unknown = False
        clue_days = self._range(low["extreme_date"], day) if low["confirmed_at"] == day else [day]
        for evidence_day in clue_days:
            if evidence_day in self.halted:
                continue
            row = self.rows[evidence_day]
            evidence_close = self.price(evidence_day, "close", day)
            if compare(evidence_close / self.point_price(low, day), 1., "lt"):
                continue
            ma = self._above_ma10(evidence_day)
            past_platform = compare(evidence_close / platform, 1., "gt")
            branches = {"ma10": all_of(branch_ma, ma), "platform": all_of(branch_platform, past_platform)}
            price_rule = any_of(*branches.values())
            guard = all_of(compare(ratio(row.get("volume"), a), PARAMETERS["start_volume_vs_pullback"]),
                           compare(ratio(row.get("volume"), b), PARAMETERS["start_volume_vs_impulse"]), price_rule)
            if guard is None:
                unknown = True
            if guard is not True:
                continue
            start_id = f'{cycle["round_id"]}:{segment}:{high["pivot_id"]}:{low["pivot_id"]}'
            known_starts = self.state["start_known_at"]
            prior_round = self.prior_rounds.get(cycle["round_id"], {})
            previous = prior_round.get("starts", {}).get(str(segment))
            archived = self.published_start((self.symbol, start_id, VERSION), low["confirmed_at"]) if self.published_start else None
            event = archived or (previous if previous and previous["start_id"] == start_id else None)
            already_known = event is not None or start_id in known_starts
            if event is None:
                relative_driver = PARAMETERS["start_volume_vs_pullback"] * (a / b) - PARAMETERS["start_volume_vs_impulse"]
                driver = "equal" if abs(relative_driver) <= 1e-12 else (
                    "pullback_dominant" if relative_driver > 0 else "impulse_dominant")
                price_branches = [key for key, value in branches.items() if value is True]
                receipt = known_starts.get(start_id)
                known_at = receipt.get("known_by") if isinstance(receipt, dict) else receipt
                known_at = known_at or self._start_known_at(day, evidence_day, cycle, price_branches)
                event = self.event(
                    "wave_started", day, record=False, known_at=known_at,
                    security_id=self.symbol, start_id=start_id, rule_version=VERSION, round_id=cycle["round_id"],
                    segment_index=segment, target_high=self._start_anchor(high, day, known_at),
                    failure_low=self._start_anchor(low, day, known_at), evidence_date=evidence_day,
                    evidence_close=self.basis.evidence(row["raw_close"], "close", evidence_day, day, at=known_at),
                    evidence_low=self.basis.evidence(row["raw_low"], "low", evidence_day, day, at=known_at),
                    price_branches=price_branches,
                    volume={"pullback_mean": a, "impulse_mean": b, "pullback_samples": pullback["sessions"],
                            "impulse_samples": impulse["sessions"], "pullback_ratio": a / b,
                            "start_vs_pullback": row["volume"] / a, "start_vs_impulse": row["volume"] / b,
                            "pullback_threshold": a_min, "impulse_threshold": b_min, "threshold_difference": a_min - b_min,
                            "relative_gap": abs(a_min - b_min) / max(a_min, b_min), "volume_constraint_driver": driver},
                    start_amount=row.get("amount"),
                )
                if isinstance(receipt, dict):
                    event = {**event, **receipt, "known_at": None}
                    if not self.collect_after:
                        # Re-entry displays this start again with its previously accepted upper bound.
                        gaps = chain(self.acceptance_gaps, self.unverified_receipts.values())
                        self.encountered_gaps.update((after, through) for after, through, _ in gaps
                                                     if after == receipt["acceptance_gap_after"])
                else:
                    event = self.check_first_receipt(event)
                if event["known_at"] is None:
                    # The surviving version proves a start, but not the price/volume evidence of the first one.
                    for field in ("volume", "start_amount", "price_branches", "evidence_date", "evidence_low", "evidence_close"):
                        event.pop(field)
            known_starts[start_id] = start_receipt(event)
            cycle["starts"][str(segment)] = event
            if not already_known and self.receipt_in_report(event, event["known_at"]):
                self.events.append(event)
                self.starts.append(dict(event))
            return event, True
        return None, not unknown

    def _continues_pullback(self, point, cycle):
        low = cycle["low"] or cycle["base_low"]
        day = point["confirmed_at"]
        return (cycle["current"] == cycle["completed"] and point["extreme_date"] > cycle["high"]["extreme_date"]
                and compare(self.point_price(point, day) / self.point_price(low, day), 1., "gt"))

    def _can_reconnect(self, point, cycle, day):
        """A fresh confirmed swing must connect to the retained structure."""
        if point["kind"] == "L":
            return self._continues_pullback(point, cycle)
        low = cycle["low"]
        if low is None or point["extreme_date"] <= low["extreme_date"]:
            return False
        prior = cycle["high"] if cycle["current"] > cycle["completed"] else low
        return compare(self.point_price(point, day) / self.point_price(prior, day), 1., "gt")

    def _collect_structure_event(self, event, previous=None):
        if previous and structure_event_key(previous) == structure_event_key(event):
            return previous
        day = event["date"]
        event = self.check_first_receipt(event)
        publish = self.receipt_in_report(event, event["known_at"])
        if event["type"] == "wave_high_confirmed":
            if not publish:
                return event
            prior = self.prior_rounds.get(event["round_id"])
            if prior and any(p.get("segment_index") == event["segment_index"] and p["pivot_id"] == event["pivot_id"]
                             for p in prior["pivots"]):
                return event
        could_be_published = day <= self.previous_cutoff[:10] if self.previous_cutoff else day < self.target
        if self.published_structure_event and could_be_published and (
            publish or day < known_by(event)[:10] or known_by(event) > (self.rows[day].get("known_at") or self.cutoff)
        ):
            archived = self.published_structure_event(event)
            if archived:
                return archived
        # Earlier published starts still need a proven invalidation even when its exact receipt expired.
        if publish or (event["type"] == "structure_invalid" and not event["known_at"]
                       and self.receipt_in_report(event, event["known_by"], upper_bound=True)):
            self.events.append(event)
        return event

    def _breakout(self, cycle, segment, day):
        event = self.event("wave_new_high", day, record=False, round_id=cycle["round_id"], segment_index=segment,
                           target_high=cycle["high"]["pivot_id"], after_low=cycle["low"]["pivot_id"])
        prior = self.prior_rounds.get(cycle["round_id"])
        saved = prior["breakouts"].get(str(segment)) if prior else None
        return self._collect_structure_event(event, saved)

    def step(self, day):
        """The caller presents successive market dates, including explicit suspensions."""
        state = self.state
        state["date"] = day
        state["known_at"] = self.known_at(day)
        oldest = self.days[max(0, self.index[day] - 19)]
        state["density_points"] = [p for p in state["density_points"] if p["confirmed_at"] >= oldest]
        state["density_days"] = {d: status for d, status in state["density_days"].items() if d >= oldest}
        state["density_rounds"] = {d: rid for d, rid in state.get("density_rounds", {}).items() if d >= oldest}
        cycle = state["round"]
        if cycle:
            state["density_rounds"][day] = cycle["round_id"]
        closes = []
        self.second_wave_start_evaluable = None
        if day in self.halted:
            state["density_days"][day] = "suspended"
            return
        state["session"] += 1
        if cycle is not None:
            line = cycle["low"] or cycle.get("next_base_low") or cycle["base_low"]
            self.observe_anchor(line, day)
            if compare(self.price(day, "close", day) / self.point_price(line, day), 1., "lt"):
                self._invalidate(day, line)
                cycle = None
            elif cycle["low"] is not None and cycle["current"] == cycle["completed"]:
                self.observe_anchor(cycle["high"], day)
        point, stale = self._update_candidate(day)
        if stale is not None and cycle is not None:
            closes.append(stale)
        if point and state["rebuilding"] and cycle and self._can_reconnect(point, cycle, day):
            state["rebuilding"] = False
        if point and point["kind"] == "L" and not state["rebuilding"]:
            self._record_low(point, cycle)
        if not state["rebuilding"]:
            self.second_wave_start_evaluable = state["warmed"] is not None
            if cycle is not None and cycle["low"] is not None:
                segment = cycle["completed"] + 1
                start, evaluable = self._start(day, cycle, segment)
                if segment == 2:
                    self.second_wave_start_evaluable = evaluable
                    if start:
                        closes.append(start)
                if cycle["current"] < segment and compare(
                    self.price(day, "close", day) / self.point_price(cycle["high"], day), 1., "gt"
                ):
                    cycle.update(current=segment, next_base_low=cycle["low"])
                    breakout = self._breakout(cycle, segment, day)
                    state["known_at"] = max(state["known_at"], known_by(breakout))
                    cycle["breakouts"][str(segment)] = breakout
                    if segment == 2:
                        closes.append(breakout)
            if point and point["kind"] == "H":
                initialized = state["warmed"] is None
                # Structural attribution can be learned later than the ordinary swing confirmation.
                point = point.copy()
                self._record_high(point, day, cycle)
                if initialized and state["warmed"] is not None:
                    self.second_wave_start_evaluable = True
        if point and state["rebuilding"]:
            state["density_points"][-1]["attribution_unknown"] = True
        elif state["round"] is not None and point is not None:
            state["round"]["pivots"].append(point)
            state["density_points"][-1]["round_id"] = state["round"]["round_id"]
        if cycle and closes:
            round_id = cycle["round_id"]
            if not cycle["window_close"] or round_id in self._closing_events:
                # Only this replay's first closure can gain or replace triggers as later bars are visited.
                events = [*self._closing_events.get(round_id, ()), *closes]
                close = window_close_from_events(events)
                cycle["window_close"] = self.window_closes[round_id] = close
                earliest = known_by(close)
                self._closing_events[round_id] = [e for e in events if known_by(e) == earliest
                                                  or e.get("acceptance_gap_after", earliest) < earliest]
        status = "known" if state["warmed"] else "warming_up"
        if state["rebuilding"] or state.get("warmup_gap"):
            status = "rebuilding"
        state["density_days"][day] = status
        if state["round"]:
            state["density_rounds"][day] = state["round"]["round_id"]
        for start in self.starts:
            if start["known_at"][:10] == day:
                start["pivot_density"] = self.density(day)

    def density(self, day):
        window = self.days[max(0, self.index[day] - 19):self.index[day] + 1]
        statuses = self.state["density_days"]
        points = [p for p in self.state["density_points"] if p["confirmed_at"] in window]
        known_rounds = self.state.get("density_rounds", {})
        rounds = list(dict.fromkeys(
            rid for d in window
            for rid in [*(p["round_id"] for p in points if p["confirmed_at"] == d), known_rounds.get(d)]
            if rid))
        continuous = all(statuses.get(d) in ("warming_up", "known", "suspended") for d in window)
        qualified = len(window) == 20 and continuous and self.state["warmed"] is not None
        return {"value": len(points) / 20 if qualified else None, "start": window[0], "end": day,
                "observed_sessions": sum(d in statuses for d in window),
                "confirmed_pivot_count": len(points), "suspended": sum(statuses.get(d) == "suspended" for d in window),
                "missing": sum(d not in statuses for d in window), "continuous": continuous,
                "cross_cycle": len(rounds) > 1 if qualified else None, "round_count": len(rounds) if qualified else None,
                "unassigned_pivot_count": sum(p["round_id"] is None and not p.get("attribution_unknown") for p in points),
                "unknown_pivot_count": sum(bool(p.get("attribution_unknown")) for p in points),
                "cycle_refs": [{"round_id": rid, "rule_version": VERSION,
                                "confirmed_pivot_count": sum(p["round_id"] == rid for p in points)} for rid in rounds]}

    def advance(self, beginning, *, warmup_checkpoint=None):
        state = self.state
        start = bisect_right(self.days, state["date"]) if state["date"] else bisect_left(self.days, beginning)
        self.warmup_checkpoint = warmup_checkpoint
        waiting = {day: at for day, at in state.get("waiting_for_bars", {}).items()
                   if state["date"] and day <= state["date"]}
        missing_days = []
        for day in self.days[start:]:
            formal = self.rows.get(day, {}).get("wave_formal")
            if day not in self.halted and not formal:
                missing_days.append(day)
                continue
            if missing_days:
                cycle = state["round"]
                if cycle is not None:
                    if not formal:
                        continue
                    line = cycle["low"] or cycle.get("next_base_low") or cycle["base_low"]
                    if not compare(self.price(day, "close", day) / self.point_price(line, day), 1., "lt"):
                        continue
                # Crossing a hole can establish a new impulse or prove the old round invalid.
                # Either way, a later fill must resume from the last continuous prefix.
                if self.warmup_checkpoint is None:
                    self.warmup_checkpoint = deepcopy(state)
                waiting.update(dict.fromkeys(missing_days, self.cutoff))
                if cycle is None:
                    state.update(warmup_gap=missing_days[0], direction="H", saw_decline=False, warmed=None,
                                 ordinary_low=None, candidate=None, last_impulse=None)
                else:
                    self.observe_anchor(line, day)
                    self._invalidate(day, line)
                missing_days.clear()
            self.step(day)
        cutoff = self.days[max(0, len(self.days) - 20)]
        state["density_points"] = [p for p in state["density_points"] if p["confirmed_at"] >= cutoff]
        state["density_days"] = {d: status for d, status in state["density_days"].items() if d >= cutoff}
        event_count = len(self.events)
        for events in self._closing_events.values():
            for event in events:
                if event["type"] == "candidate_expired" and self.cutoff and not self.new_fact_day(event["date"]):
                    self._collect_structure_event(event)
        self._closing_events.clear()
        if len(self.events) > event_count:
            self.events.sort(key=lambda event: event["date"])
        waiting.update(dict.fromkeys(missing_days, self.cutoff))
        if waiting:
            state["waiting_for_bars"] = waiting
        else:
            state.pop("waiting_for_bars", None)
        return self.snapshot(missing_days[0] if missing_days else state.get("warmup_gap"))

    def snapshot(self, gap):
        state = self.state
        day = self.target
        cycle = state["round"] or state["ended_round"] or {}
        active = state["round"] is not None
        closed = cycle.get("window_close")
        known = not gap and not state["rebuilding"] and state["warmed"] is not None
        status = ("data_gap" if gap == day else "rebuilding" if gap or state["rebuilding"]
                  else "known" if known else "warming_up")
        suspended = day in self.halted
        first_high = cycle.get("first_high")
        pullback = 1 - self.price(day, "close", day) / self.point_price(first_high, day) if (
            first_high and known and not suspended and active) else None
        phase = "window_closed" if closed else None
        reasons = []
        if closed:
            reasons.append("first_wave_window_closed")
            if "closed_by_stale" in closed["reason_codes"]:
                reasons.append("provisional_stale")
        if suspended:
            reasons.append("suspended")
        if gap:
            reasons.append("missing_rows")
        if cycle.get("invalidated"):
            reasons.append("structure_invalid")
        elif not first_high:
            below_min = known and state.get("last_impulse") and not compare(
                state["last_impulse"]["return"], PARAMETERS["first_impulse_min_return"])
            reasons.append("first_impulse_below_min" if below_min else "first_wave_unconfirmed")
        eligible = False if any(code in reasons for code in ("suspended", "structure_invalid", "first_wave_window_closed", "first_impulse_below_min")) else None
        if not reasons and pullback is not None:
            eligible = compare(pullback, PARAMETERS["first_pullback_min_pct"])
            phase = "pullback_ge_5pct" if eligible else "recovered_lt_5pct"
            if not eligible:
                reasons.append("pullback_below_min")
        first_low = cycle.get("first_low")
        first_start = cycle.get("first_start")
        candidate = state["candidate"]
        provisional = None
        if candidate:
            key = "high" if candidate["kind"] == "H" else "low"
            provisional = {**candidate, "status": "provisional", "idle_sessions": state["session"] - candidate["extreme_session"],
                           **self.basis.evidence(candidate["raw_price"], key, candidate["extreme_date"], day)}
        retracement = None
        provisional_retracement = None
        provisional_volume = None
        if first_high and first_start and active and known:
            high = self.point_price(first_high, day)
            difference = high - self.point_price(first_start, day)
            if first_low:
                retracement = ratio(high - self.point_price(first_low, day), difference)
            elif candidate and candidate["kind"] == "L":
                provisional_retracement = ratio(high - self.point_price(candidate, day), difference)
                provisional_volume = self._volumes(first_high["extreme_date"], day)
        impulse_volume = cycle.get("first_impulse_volume")
        pullback_volume = cycle.get("first_pullback_volume")
        volume_ratio = ratio(pullback_volume["mean"], impulse_volume["mean"]) if pullback_volume and impulse_volume else None
        start2 = cycle.get("starts", {}).get("2")
        density = self.density(day)
        last_impulse = state.get("last_impulse") or {}
        impulse = cycle.get("first_return", last_impulse.get("return"))
        confirmed_pivots = cycle.get("pivots", []) if cycle or not last_impulse else [last_impulse["low"], last_impulse["high"]]
        return {
            "rule_version": VERSION, "data_status": status, "missing_from": gap, "round_id": cycle.get("round_id"),
            "as_of": day, "known_at": self.cutoff, "warmup_exit": state["warmed"],
            "current_wave_index": cycle.get("current") if known and active else None,
            "completed_wave_count": cycle.get("completed", 0 if known or (state["date"] and not gap) else None),
            "count_as_of": cycle.get("last_completed_at", state["date"]), "first_wave_start": first_start,
            "first_wave_high": first_high, "first_pullback_low": first_low, "first_impulse_return": impulse,
            "first_wave_pullback": pullback, "retrace_pct": retracement, "provisional_retrace_pct": provisional_retracement,
            "impulse_volume": impulse_volume, "pullback_volume": pullback_volume,
            "provisional_pullback_volume": provisional_volume, "pullback_volume_ratio": volume_ratio,
            "first_pullback_active": eligible is True or phase == "recovered_lt_5pct" if phase else None,
            "first_pullback_eligible": {"value": eligible, "reason_codes": reasons},
            "pullback_phase_at_time": phase, "window_close_reason_codes": closed["reason_codes"] if closed else [],
            "window_close": closed, "structure_invalidated": cycle.get("invalidated"),
            "confirmed_pivots": confirmed_pivots, "provisional_extreme": provisional,
            "provisional_extreme_history": [e for e in self.events if e["date"] == day and e["type"].startswith("candidate_")],
            "second_wave_started": bool(start2) if (known and active) or start2 else None, "second_wave_start": start2,
            "second_wave_confirmed_by_new_high": cycle.get("breakouts", {}).get("2"),
            "start_evaluable": self.second_wave_start_evaluable is True and known and not suspended,
            "pivot_density": density, "required_checks": RequiredChecks(
                first_impulse_return=compare(impulse, PARAMETERS["first_impulse_min_return"]),
                first_impulse_evidence=cycle.get("first_evidence"),
                retracement=all_of(compare(retracement, PARAMETERS["retrace_min"]),
                                   compare(retracement, PARAMETERS["retrace_max"], "le")),
                volume=compare(volume_ratio, PARAMETERS["pullback_volume_ratio_max"], "lt")
                if pullback_volume and pullback_volume["sessions"] >= PARAMETERS["pullback_min_sessions"] else None,
                support=None),
        }


def _factor_observations(rows, previous_stock, accepted_at, previous_cutoff, accepted_facts):
    previous = dict(previous_stock.get("factors", []))
    current = dict(rows)
    observed = dict(previous_stock.get("factor_known_at", {}))
    for day in previous.keys() | current.keys() | accepted_facts.keys():
        old, new = previous.get(day, 1.), current.get(day, 1.)
        proof = accepted_facts.get(day)
        if proof and proof[0] == new:
            observed[day] = proof[1]
        elif old != new:
            observed[day] = accepted_at
        elif new != 1.:
            observed.setdefault(day, previous_cutoff or accepted_at)
    return observed


def _factor_revisions(previous_stock, previous, history, symbol, basis, cutoff, group, *, replay_prices=False, after=None):
    """Use accepted versions, including market acceptances on dates with no evening report."""
    versions = []
    if previous_stock:
        versions.append((previous["cutoff"], PriceBasis(previous_stock["factors"], previous.get("factor_source"),
                         previous.get("factor_source_known_at") or previous["cutoff"],
                         factor_known_at=previous_stock.get("factor_known_at"))))
    for item in history:
        if previous_stock and item["cutoff"] <= previous["cutoff"]:
            continue
        facts = item["facts"].get(symbol, {})
        if facts is None:
            continue
        if after:
            facts = {day: fact for day, fact in facts.items() if day > after}
        older = PriceBasis.from_accepted_facts(facts, item.get("source"), item.get("source_known_at") or item["cutoff"])
        versions.append((item["cutoff"], older))
    basis.prior_versions = [older for _, older in versions]
    versions.append((cutoff, basis))
    revisions = []
    late = False
    for (previous_at, older), (receipt, newer) in pairwise(versions):
        prior_values, values = dict(older.factors), dict(newer.factors)
        batches = {}
        for day in prior_values.keys() | values.keys() | newer.factor_known_at.keys():
            # Equal endpoint values can be distinct acceptances separated by a lost withdrawal.
            # Replay the older proof before the later clock replaces it.
            changed = prior_values.get(day, 1.) != values.get(day, 1.)
            if changed or newer.factor_known_at.get(day, '') > previous_at:
                known = newer.factor_known_at.get(day, receipt)
                batches.setdefault(known, {})[day] = values.get(day, 1.)
                if day < known[:10]:
                    late = True
                elif not late and day == known[:10]:
                    idx = bisect_left(group["date"], day)
                    if idx < group.height and group["date"][idx] == day:
                        observed = group["known_at"][idx]
                        late = bool(observed and observed < known)
        observations = dict(older.factor_known_at)
        for accepted_at, changes in sorted(batches.items()):
            revisions.append((accepted_at, min(changes), older))
            prior_values.update(changes)
            observations.update(dict.fromkeys(changes, accepted_at))
            # Per-factor proofs can outlive the corresponding full factor file.
            older = PriceBasis(sorted((d, v) for d, v in prior_values.items() if v != 1.), None, accepted_at,
                               factor_known_at=observations.copy())
    return revisions if late or replay_prices else []


def _rows_on_prior_basis(rows, original, current):
    """Rebase MA10 within each affected ten-row window; unaffected ratios stay identical."""
    old, new = dict(original.factors), dict(current.factors)
    dates = sorted(day for day, row in rows.items() if "raw_close" in row)
    affected = set()
    for day in old.keys() | new.keys():
        if old.get(day, 1.) != new.get(day, 1.):
            index = bisect_left(dates, day)
            affected.update(range(index, min(index + 9, len(dates))))
    revised = rows.copy()
    for index in affected:
        day = dates[index]
        row = rows[day]
        if not finite(row.get("ma10")):
            continue
        window = dates[max(0, index - 9):index + 1]
        ma = None
        if len(window) == 10 and all(finite(rows[d].get("close")) for d in window):
            # Keep today's close as the common unit; only earlier terms spanning a changed factor move.
            ma = row["ma10"] + sum(rows[d]["close"] * (original.multiplier(d, day) / current.multiplier(d, day) - 1)
                                   for d in window) / 10
        revised[day] = {**row, "ma10": ma}
    return revised


def _rows_before_corrections(rows, halted, before, basis, current):
    """Rebuild the bars and ten-session average observable under this accepted version."""
    restored = _rows_on_prior_basis(rows, basis, current)
    prior_halts = set(halted)
    recompute_ma = False
    for day, prior in before.items():
        latest = rows.get(day)
        if prior is None or latest is None or prior["halted"] != (day in halted):
            recompute_ma = True
        prior_halts.discard(day)
        if prior is None:
            restored.pop(day, None)
        else:
            row = restored[day] = {**restored.get(day, {}), **prior}
            if prior["halted"]:
                prior_halts.add(day)
            elif not recompute_ma:
                recompute_ma = row["raw_close"] != latest["raw_close"] or row["wave_formal"] != latest["wave_formal"]
    if not recompute_ma:
        return restored, prior_halts

    # WaveRun compares close with MA10 only within the same day. Use that day's raw price
    # as their common unit, exactly equivalent to dividing two forward-adjusted prices.
    sessions = sorted(day for day in restored if day not in prior_halts)
    for i, day in enumerate(sessions):
        row = restored[day]
        window = sessions[max(0, i - 9):i + 1]
        ma10 = (mean(basis.price(restored[d]["raw_close"], d, day) for d in window)
                if len(window) == 10 and all(restored[d]["wave_formal"] for d in window) else None)
        restored[day] = {**row, "close": row["raw_close"], "ma10": ma10,
                         "ma10_from": window[0] if len(window) == 10 else None}
    return restored, prior_halts


def _replay_input_versions(symbol, calendar, index, rows, halted, current, factor_changes, price_changes,
                           beginning, saved, replay, cancel, factor_available_from, factor_resumptions):
    # Rewind sparse before-images to recover the exact joint input just before each receipt.
    factors = {at: (day, basis) for at, day, basis in factor_changes}
    prices = dict(price_changes)
    before = {}
    basis = current
    versions = []
    for at in sorted(factors.keys() | prices.keys(), reverse=True):
        prior = prices.get(at, {})
        for day, old_fields in prior.items():
            before[day] = None if old_fields is None else {**(before.get(day) or {}), **old_fields}
        if at in factors:
            effective, basis = factors[at]
            effective = min((effective, *prior))
        else:
            effective = min(prior)
        versions.append((at, effective, basis, before.copy()))
    for accepted_at, effective, basis, earlier_rows in reversed(versions):
        cancel()
        replay.before = accepted_at
        days = calendar[:bisect_right(calendar, accepted_at[:10])]
        if basis is not current:
            basis.prior_versions = [v for v in current.prior_versions if v.known_at < basis.known_at]
        if earlier_rows:
            prior_rows, prior_halts = _rows_before_corrections(rows, halted, earlier_rows, basis, current)
        else:
            prior_rows, prior_halts = _rows_on_prior_basis(rows, basis, current), halted
        if days and beginning <= days[-1]:
            run = WaveRun(symbol, days, prior_rows, prior_halts, basis,
                          accepted_at, saved, calendar_index=index, replay=replay,
                          factor_available_from=factor_available_from, factor_resumptions=factor_resumptions)
            run.advance(beginning)
        replay.revised_from, replay.revised_at = effective, _joint_input_time(accepted_at, factor_resumptions)
    replay.before = None


def _stock_receipt_gaps(gaps, rows, price_changes, basis, first_session, missing_sessions):
    """Bound unknown first observations within this unpublished interval."""
    if not gaps:
        return [], []
    affected, pending = {}, {}
    clocks = [(day, row["known_at"]) for day, row in rows.items() if row.get("known_at")]
    clocks.extend((day, before["known_at"]) for _, changes in price_changes for day, before in changes.items()
                  if before and before.get("known_at"))
    for version in [*basis.prior_versions, basis]:
        # A factor at/before the first session cancels in every comparison made from these bars.
        clocks.extend((day, at) for day, at in version.factor_known_at.items() if day > first_session)
    later_changes = list(price_changes)
    for after, through in reversed(gaps):
        at_gap_end = {}
        while later_changes and later_changes[-1][0] > through:
            _, changes = later_changes.pop()
            at_gap_end.update(changes)
        days = [day for day, known in clocks if after < known <= through]
        days.extend(day for day, before in at_gap_end.items() if before is None and day <= through[:10])
        if days:
            first_day = min(days)
            affected[after, through] = min(first_day, affected.get((after, through), first_day))
        # Preserve only unresolved dates that could extend this boundary when filled.
        unfilled = sorted(day for day in missing_sessions if day not in at_gap_end and day <= through[:10]
                          and (not days or day < affected[after, through]))
        if unfilled:
            pending[after, through] = unfilled
        missing_sessions = ()  # The gap interrupts the continuous evidence for earlier receipts.
    return ([(after, through, day) for (after, through), day in sorted(affected.items())],
            [(after, through, dates) for (after, through), dates in sorted(pending.items())])


def build_waves(inputs, records, previous, calendar, cancel, *, published_start=None, published_close=None,
                published_structure_event=None):
    """Continue the published state and compare acceptances within the unpublished interval."""
    states, snapshots, events, starts = {}, {}, [], []
    window_closes = {}
    if previous.get("rule_version") != VERSION:
        previous = {}
    reentries = getattr(inputs, "wave_st_reentry", {})
    in_st = {symbol for symbol, since in reentries.items() if since is None}
    prior = {s: stock for s, stock in previous.get("stocks", {}).items()
             if s not in in_st and stock.get("st_reentry") == reentries.get(s)
             and stock["as_of"] >= calendar[0]}
    if inputs.wave_factors is None:
        if previous:
            missing = {*previous.get("missing_factor_receipts", ()), inputs.cutoff}
            previous = {**previous, "missing_factor_receipts": sorted(missing)}
        if previous and reentries:
            previous = {**previous, "stocks": prior}
        return previous, snapshots, events, starts, window_closes
    acceptance_gaps = getattr(inputs, "wave_acceptance_gaps", ())
    unverified_receipts = getattr(inputs, "wave_unverified_receipts", {})
    reported_gaps = set(map(tuple, acceptance_gaps))
    daily_files = daily_partition_versions(inputs.provenance.get("input_versions", {}))
    factors = {symbol[0]: list(group.select("trade_date", "ex_factor").iter_rows())
               for symbol, group in inputs.wave_factors.partition_by("symbol", as_dict=True).items()}
    source = inputs.provenance["wave_factor_source"]
    factor_history = getattr(inputs, "wave_factor_history", ())
    factor_returns = {**previous.get("factor_resumptions", {}), **getattr(inputs, "wave_factor_resumptions", {})}
    global_resumptions = sorted(factor_returns.items())
    price_bootstrap = getattr(inputs, "wave_price_bootstrap", {})
    next_acceptances = getattr(inputs, "wave_next_stock_acceptances", {})
    first_acceptances = inputs.wave_first_acceptances
    price_history = getattr(inputs, "wave_price_revisions", {})
    factor_acceptance = inputs.provenance.get("wave_factor_known_at", inputs.cutoff)
    factor_available_from = inputs.provenance.get("wave_factor_first_accepted") or ""
    factor_facts = inputs.provenance.get("wave_factor_facts", {})
    pending_bad = dict(previous.get("unusable_factor_since", {}))
    recoveries = {}
    for item in [*factor_history, {"facts": factor_facts, "cutoff": inputs.cutoff}]:
        invalid = {s for s, facts in item["facts"].items() if facts is None}
        for s in pending_bad.keys() - invalid:
            recoveries.setdefault(s, {})[pending_bad.pop(s)] = item["cutoff"]
        for s in invalid:
            pending_bad.setdefault(s, item["cutoff"])
    halted = {}
    for row in inputs.provenance.get("suspensions", []):
        if row.get("confirmed") is True:
            halted.setdefault(row["symbol"], {})[str(row["date"])] = row.get("obtained_at")
    eligible = {row["symbol"] for row in records} - in_st
    prefixes = {}
    if prior:
        for at in getattr(inputs, "wave_comparable_prefixes", ()):
            gap = unverified_receipts.get(at)
            if gap and isinstance(gap[2], str) and tuple(gap[:2]) in reported_gaps:
                after, through, end = gap
                # Without comparable stock facts its full trading-day range remains unresolved.
                scope = {s: ["", end] for s in eligible | prior.keys() | first_acceptances.keys()}
                unverified_receipts[at] = after, through, scope
                prefixes[at] = (after, through), end, scope
    # An uncovered security retains its last accepted state until it is observed again.
    states.update(prior)
    symbols = {s for s in eligible.intersection(first_acceptances) if factor_facts.get(s, {}) is not None}
    frame = inputs.prices.filter(pl.col("symbol").is_in(symbols)).select(
        "symbol", pl.col("date").cast(pl.String), "formal", "wave_formal", "raw_open", "raw_close", "raw_high", "raw_low",
        "close", pl.col("wave_volume").alias("volume"), pl.col("wave_amount").alias("amount"),
        pl.when(pl.col("formal")).then(pl.col("ma10")).otherwise(None).alias("ma10"),
        pl.col("known_at") if "known_at" in inputs.prices.columns else pl.lit(inputs.cutoff).alias("known_at"),
    ).filter(pl.col("date").is_between(pl.lit(calendar[0]), pl.lit(inputs.trade_date))).with_columns(
        pl.col("date").shift(9).over("symbol", order_by="date").alias("ma10_from"),
    )
    calendar_index = {day: i for i, day in enumerate(calendar)}
    market_beginning = min(daily_files, default=None)
    listings = {}
    metadata = getattr(inputs, "metadata", None)
    if metadata is not None and "listing_date" in metadata.columns:
        from app.price_limits import parse_listing_date

        listings = {symbol: day.isoformat() for symbol, value in metadata.select("symbol", "listing_date").iter_rows()
                    if (day := parse_listing_date(value))}
    for (symbol,), group in frame.partition_by("symbol", as_dict=True).items():
        cancel()
        previous_stock = prior.get(symbol)
        first_acceptance = first_acceptances[symbol]
        scope_start = reentries.get(symbol)
        factor_resumptions = global_resumptions
        if symbol in recoveries:
            factor_resumptions = sorted((factor_returns | recoveries[symbol]).items())
        factor_rows = factors.get(symbol, [])
        accepted_facts = factor_facts.get(symbol, {})
        price_changes = price_history.get(symbol, ())
        halted_dates = halted.get(symbol, {})
        if scope_start:
            group = group.filter(pl.col("date") >= scope_start)
            if group.is_empty():
                continue
            factor_rows = [(day, value) for day, value in factor_rows if day > scope_start]
            accepted_facts = {day: fact for day, fact in accepted_facts.items() if day > scope_start}
            price_changes = [(at, kept) for at, changes in price_changes
                             if (kept := {day: row for day, row in changes.items() if day >= scope_start})]
            halted_dates = {day: observed for day, observed in halted_dates.items() if day >= scope_start}
        known_factors = _factor_observations(factor_rows, previous_stock or {}, factor_acceptance, previous.get("cutoff"),
                                             accepted_facts)
        basis = PriceBasis(factor_rows, source, factor_acceptance, factor_known_at=known_factors)
        group = group.sort("date")
        if scope_start or not group["formal"].all():
            # Wave MA10 needs ten admitted closes. The general indicator may also
            # be gated by turnover evidence or sessions before ST re-entry.
            closes = [basis.price(close, day, inputs.trade_date) if formal else None
                      for day, close, formal in group.select("date", "raw_close", "wave_formal").iter_rows()]
            group = group.with_columns(pl.Series("close", closes, dtype=pl.Float64)).with_columns(
                pl.col("close").rolling_mean(10).alias("ma10"), pl.col("date").shift(9).alias("ma10_from"))
        factor_changes = _factor_revisions(previous_stock, previous, factor_history, symbol, basis, inputs.cutoff, group,
                                           replay_prices=bool(price_changes), after=scope_start)
        first_bar = group["date"][0]
        beginning = max(calendar[0], market_beginning or first_bar, listings.get(symbol, scope_start or first_bar), scope_start or "")
        available_from = max(first_acceptance or "", factor_available_from, scope_start or "")
        relevant_gaps = [gap for gap in acceptance_gaps if gap[1] >= available_from]
        missing_sessions = ()
        if relevant_gaps:
            end = bisect_right(calendar, relevant_gaps[-1][1][:10])
            missing_sessions = set(calendar[bisect_left(calendar, beginning):end]).difference(group["date"], halted_dates)
        past = previous_stock or {}
        wave = past.get("state")
        checkpoint = past.get("warmup_checkpoint")
        if checkpoint is not None:
            waiting = {day for day in wave["waiting_for_bars"] if day <= wave["date"]}
            if (halted_dates.keys() & waiting
                    or not group.filter(pl.col("date").is_in(list(waiting)) & pl.col("wave_formal")).is_empty()):
                wave, checkpoint = checkpoint, None
        if wave:
            anchors = [wave.get("date"), (wave["candidate"] or {}).get("extreme_date"),
                       (wave["ordinary_low"] or {}).get("extreme_date")]
            cycle = wave["round"]
            if cycle:
                anchors.append((cycle["low"] or cycle["high"])["extreme_date"])
            earliest = min((d for d in anchors if d), default=beginning)
            floor = calendar[max(0, bisect_left(calendar, earliest) - 20)]
            # Include the actual preceding trading bars used by MA10, even
            # across suspensions. Advance still starts at the saved checkpoint.
            trade_days = group["date"].to_list()
            floor = min(floor, trade_days[max(0, bisect_left(trade_days, earliest) - 9)])
            group = group.filter(pl.col("date") >= floor)
            price_changes = [(at, relevant) for at, changes in price_changes
                             if (relevant := {day: row for day, row in changes.items() if day >= floor})]
        changed_days = [day for _, changes in price_changes for day in changes]
        changed_days.extend(day for _, day, _ in factor_changes)
        revised_from = min(changed_days, default=None)
        rows = {row["date"]: row for row in group.iter_rows(named=True)}
        for day, observed in halted_dates.items():
            rows.setdefault(day, {})["known_at"] = observed or rows.get(day, {}).get("known_at")
        start_checkpoint_until = next_acceptances.get(symbol) if previous_stock else None
        stock_gaps, unfilled_gaps = _stock_receipt_gaps(
            relevant_gaps, rows, price_changes, basis, beginning, missing_sessions)
        if previous_stock and prefixes:
            for interval, end, scope in prefixes.values():
                days = [day for after, through, day in stock_gaps if (after, through) == interval]
                days.extend(day for after, through, unfilled in unfilled_gaps if (after, through) == interval
                            for day in unfilled)
                first = min(days, default=None)
                if first and first <= end:
                    scope[symbol] = [first, end]
                else:
                    scope.pop(symbol, None)
        replay = _WaveReplay(previous_stock, revised_from) if revised_from else None
        if revised_from:
            _replay_input_versions(symbol, calendar, calendar_index, rows, halted_dates, basis, factor_changes,
                                   price_changes, beginning, wave, replay, cancel, factor_available_from, factor_resumptions)
        deferred = {wave_event_key(event): event for event in past.get("deferred_events", ())}
        run = WaveRun(symbol, calendar, rows, halted_dates, basis, inputs.cutoff,
                      wave, calendar_index=calendar_index,
                      prior_state=past.get("state"),
                      previous_cutoff=previous.get("cutoff"), published_start=published_start, published_close=published_close,
                      published_structure_event=published_structure_event, replay=replay,
                      first_acceptance=first_acceptance, factor_available_from=factor_available_from,
                      factor_resumptions=factor_resumptions,
                      price_bootstrap=price_bootstrap, acceptance_gaps=stock_gaps,
                      unverified_receipts=unverified_receipts,
                      start_checkpoint_until=start_checkpoint_until, deferred_events=deferred)
        snapshot = run.advance(beginning, warmup_checkpoint=checkpoint)
        snapshots[symbol] = snapshot
        reported_gaps.update(run.encountered_gaps)
        states[symbol] = {"as_of": inputs.trade_date, "factors": factor_rows,
                          "factor_known_at": known_factors, "state": run.state}
        if run.warmup_checkpoint is not None:
            states[symbol]["warmup_checkpoint"] = run.warmup_checkpoint
        # Only an unprocessed session can carry a pending observation past publication.
        # A settled session's withdrawn clue must leave the segment free for a later start.
        unprocessed = [day for day in run.state.get("waiting_for_bars", {}) if day > (run.state["date"] or "")]
        if unprocessed:
            observations = (replay.events if replay else {}) | deferred
            pending = []
            for key, event in observations.items():
                if (event["type"] != "wave_started" or event["date"] < min(unprocessed)
                        or event["start_id"] in run.state["start_known_at"]):
                    continue
                receipt = run.check_first_receipt(event, report=False)
                if key in deferred or run.receipt_in_report(receipt, known_by(receipt), upper_bound=not receipt["known_at"]):
                    pending.append(receipt)
            if pending:
                states[symbol]["deferred_events"] = pending
        if scope_start:
            states[symbol]["st_reentry"] = scope_start
        events.extend(run.events)
        starts.extend(run.starts)
        window_closes.update(run.window_closes)
    return {"rule_version": VERSION, "as_of": inputs.trade_date, "cutoff": inputs.cutoff,
            **({"unusable_factor_since": pending_bad} if pending_bad else {}),
            **({"factor_resumptions": factor_returns} if factor_returns else {}),
            "factor_source": source, "factor_source_known_at": factor_acceptance,
            **({"reported_acceptance_gaps": sorted(reported_gaps)} if reported_gaps else {}),
            "stocks": states}, snapshots, events, starts, window_closes
