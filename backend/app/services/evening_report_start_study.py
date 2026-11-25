"""从定稿读取完整启动队列，按固定交易日窗口独立评价。"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime
from functools import lru_cache

from app.market_time import CN_TZ
from app.services.evening_report_receipt import daily_partition_versions
from app.services.evening_report_rules import finite
from app.services.evening_report_wave import VERSION as WAVE_VERSION
from app.services.evening_report_wave import (
    PriceBasis,
    compare,
    known_by,
    start_key,
)
from app.services.evening_report_wave_context import study_context

VERSION = "start-evaluation-rule-v1"
DEFAULT_SESSIONS = 10
DEFAULT_END_RETURN = .08
NEAR_TIE_RELATIVE_GAP_MAX = .01


def _local_date(timestamp):
    return datetime.fromisoformat(timestamp).astimezone(CN_TZ).date().isoformat()


def _first_possible_invalidation_day(event):
    if event["known_at"]:
        return _local_date(event["known_at"])
    after = event["acceptance_gap_after"]
    return max(event["date"], _local_date(after)) if after else event["date"]


def published_on_trade_date(report):
    """只将能定位到当日盘后输入及发布时点的原件纳入前向研究。"""
    if report.get("kind") != "original" or not report.get("generated_at") or not report.get("cutoff"):
        return False
    generated = datetime.fromisoformat(report["generated_at"])
    cutoff = datetime.fromisoformat(report["cutoff"])
    if generated.tzinfo is None or cutoff.tzinfo is None:
        return False
    local_cutoff = cutoff.astimezone(CN_TZ)
    return (report["trade_date"] == local_cutoff.date().isoformat() == _local_date(report["generated_at"])
            and local_cutoff.hour >= 15 and generated >= cutoff)


def _window_days(known_day, calendar, sessions, saved=(), coverage_start=None):
    """Extend a confirmed window only where the new calendar connects to it."""
    if saved:
        window = list(saved[:sessions + 1])
        if len(window) == sessions + 1:
            return window
        last = bisect_left(calendar, window[-1])
        if last == len(calendar) or calendar[last] != window[-1]:
            return window
        remaining = sessions + 1 - len(window)
        return window + calendar[last + 1:last + 1 + remaining]
    if not calendar or known_day < (coverage_start or calendar[0]):
        return []
    start = bisect_left(calendar, known_day)
    return calendar[start:start + sessions + 1]


def evaluate_start(event, *, calendar, prices, suspended, basis, invalidated, as_of, evaluated_at,
                   sessions=DEFAULT_SESSIONS, end_return=DEFAULT_END_RETURN,
                   basis_through=None, published_bases=None, window_days=None, accepted_through=None):
    """The original starting date and first terminal outcome are invariant within this run."""
    date_known = _local_date(event["known_at"])
    window = _window_days(date_known, calendar, sessions) if window_days is None else window_days
    start_day = window[0] if window else None
    target_day = window[sessions] if len(window) > sessions else None
    result = {
        "security_id": event["security_id"], "start_id": event["start_id"], "rule_version": event["rule_version"],
        "segment_index": event["segment_index"], "known_at": event["known_at"], "evidence_date": event["evidence_date"],
        "start_date": start_day, "target_date": target_day, "evaluated_at": evaluated_at, "as_of": as_of,
        "status": "not_due", "evaluation_reason_code": "not_due", "is_final": False,
        "success_at_start": False, "evaluation_rule_version": VERSION,
        "evaluation_sessions": sessions, "end_return_threshold": end_return, "forward_sessions": 0,
    }

    def finish(status, reason, day, *, final=True, **evidence):
        return {**result, "status": status, "evaluation_reason_code": reason,
                "is_final": final, "success_at_start": reason == "success_at_start",
                "terminal_date": day if final else None, "fact_date": day, **evidence}

    if not window and calendar and date_known < calendar[0] and date_known <= as_of:
        return finish("unavailable", "calendar_unconfirmed", date_known, final=False)
    if start_day is None or start_day > as_of:
        return result
    end = min(as_of, target_day or as_of)
    invalidation_day = _local_date(known_by(invalidated)) if invalidated else None
    uncertain_from = None
    if invalidated and start_day < invalidation_day:
        earliest = _first_possible_invalidation_day(invalidated)
        if earliest < invalidation_day:
            uncertain_from = earliest
    maturity = min(day for day in (target_day, invalidation_day, "9999-12-31") if day)
    round_breach = invalidated
    if round_breach and accepted_through and known_by(round_breach) > accepted_through:
        round_breach = None
    symbol = event["security_id"]

    def gap(reason, day):
        return finish("unavailable", reason, day, final=maturity <= as_of)

    def day_basis(day):
        if published_bases and day in published_bases:
            return published_bases[day]
        return basis if not basis_through or day <= basis_through else None

    def factor_time(basis, day, base, quote_day):
        # The dated receipt admits this security's factor series. Only adjustments
        # inside the comparison interval can subsequently change its multiplier.
        row = prices.get((symbol, quote_day)) or {}
        covered = row.get("factor_covered_at")
        adjustment = basis.available_at(day, base)
        return max(covered, adjustment) if covered else adjustment or basis.known_at

    def study_close(day, raw, basis):
        quote = basis.evidence(raw, "close", day, start_day)
        quote["factor_known_at"] = factor_time(basis, day, start_day, day)
        return quote

    start_basis = day_basis(start_day)
    if start_basis is None:
        return gap("price_basis_unavailable", start_day)
    high, low = event["target_high"], event["failure_low"]
    def rebase(point):
        raw, applied = point["raw_price"], point["applied_adjustment_multiplier"]
        if (not finite(raw, positive=True) or not finite(applied, positive=True)
                or (basis_through and start_basis is basis and point["adjust_base_date"] > basis_through)):
            return None
        multiple = applied * start_basis.multiplier(point["adjust_base_date"], start_day)
        price = raw * multiple
        if not finite(price, positive=True):
            return None
        return {**point, "price": price, "applied_adjustment_multiplier": multiple,
                "adjust_base_date": start_day, "continuation_factor_source": start_basis.source,
                "continuation_factor_known_at": factor_time(start_basis, point["adjust_base_date"], start_day, start_day)}

    high_ref, low_ref = rebase(high), rebase(low)
    if high_ref is None or low_ref is None:
        return gap("price_basis_unavailable", start_day)
    ceiling, floor = high_ref["price"], low_ref["price"]
    result.update(adjust_base_date=start_day, target_high=high_ref | {"pivot_id": high["pivot_id"]},
                  failure_low=low_ref | {"pivot_id": low["pivot_id"]})
    known_at = max(event["known_at"], high_ref["continuation_factor_known_at"], low_ref["continuation_factor_known_at"])

    def failed(reason, day, trigger, evidence, *, fact_day=None):
        fact_day = fact_day or day
        failure = {"type": "start_invalidated", "start_id": event["start_id"], "fact_date": fact_day,
                   "known_at": evidence["outcome_known_at"], "evaluation_reason_code": reason, "source_event_id": trigger}
        return finish("failed", reason, day, fact_date=fact_day, start_invalidated=trigger, failure_event=failure, **evidence)

    for offset, day in enumerate(window):
        if day > end:
            break
        result["forward_sessions"] = offset
        current_basis = day_basis(day)
        if current_basis is None:
            return gap("price_basis_unavailable", day)
        if (symbol, day) in suspended:
            return gap("suspended", day)
        row = prices.get((symbol, day))
        if not row or not row["wave_formal"]:
            return gap("missing_rows", day)
        if not finite(row["raw_close"], positive=True):
            return gap("price_basis_unavailable", day)
        current = study_close(day, row["raw_close"], current_basis)
        close = current["price"]
        if not finite(close, positive=True):
            return gap("price_basis_unavailable", day)
        if offset == 0:
            if not finite(row["raw_low"], positive=True):
                return gap("price_basis_unavailable", day)
            anchor = start_basis.evidence(row["raw_low"], "low", day, start_day)
            anchor["factor_known_at"] = current["factor_known_at"]
            start_close, start_low = close, anchor["price"]
            if compare(start_low / start_close, 1., "gt"):
                return gap("price_basis_unavailable", day)
            result.update(start_close=current, start_low=anchor)
            known_at = max(known_at, row.get("low_known_at") or "")
        known_at = max(known_at, row.get("known_at") or "", current["factor_known_at"] or "")
        evidence = {"outcome_known_at": known_at, "trigger_close": current, "return": close / start_close - 1}
        if uncertain_from and day >= uncertain_from:
            return gap("structure_invalidation_time_unknown", day)
        price_result = None
        if compare(close / floor, 1., "lt"):
            price_result = failed("close_below_failure_low", day, event["start_id"], evidence)
        elif compare(close / ceiling, 1., "gt"):
            price_result = finish("success", "close_above_target_high" if offset else "success_at_start", day, **evidence)
        elif compare(close / start_low, 1., "lt"):
            price_result = failed("close_below_start_low_before_new_high", day, event["start_id"], evidence)
        elif day == target_day:
            if compare(evidence["return"], end_return):
                price_result = finish("success", "end_return_target_met", day, **evidence)
            else:
                price_result = finish("expired", "target_not_reached", day, **evidence)

        # The archive supplies the triggering fact. The study path and its factor
        # basis determine when this start could use that fact.
        if round_breach:
            at = known_by(round_breach)
            fact_day = round_breach["date"]
            applicable = day >= invalidation_day or (day == fact_day and at <= known_at)
            if applicable and (not price_result or at <= known_at):
                fact_basis = day_basis(fact_day)
                if fact_basis is None:
                    return gap("price_basis_unavailable", day)
                trigger = study_close(fact_day, round_breach["close"]["raw_price"], fact_basis)
                when = max(at, known_at, trigger["factor_known_at"] or "")
                if not price_result or when <= known_at:
                    proof = {"outcome_known_at": when, "trigger_close": trigger, "return": trigger["price"] / start_close - 1}
                    return failed("cycle_structure_invalid", day, round_breach["event_id"], proof, fact_day=fact_day)
        if price_result:
            return price_result
    if target_day is None and as_of > window[-1]:
        return gap("calendar_unconfirmed", window[-1])
    return result


def summarize_starts(queue, results, *, as_of, sessions, end_return, near_tie=NEAR_TIE_RELATIVE_GAP_MAX):
    coverage = defaultdict(Counter)
    mature = defaultdict(Counter)
    groups = defaultdict(Counter)
    context_groups = defaultdict(Counter)
    density_groups = {key: defaultdict(Counter) for key in ("cross_cycle", "round_count", "all")}
    indexed = {(r["security_id"], r["start_id"], r["rule_version"]): r for r in results
               if r["evaluation_rule_version"] == VERSION and r["evaluation_sessions"] == sessions
               and r["end_return_threshold"] == end_return and r["as_of"] == as_of}
    for key, event in queue.items():
        segment = event["segment_index"]
        coverage[segment]["total"] += 1
        result = indexed.get(key)
        category = "uncovered"
        if result:
            category = result["evaluation_reason_code"] if result["status"] == "success" else result["status"]
            if category == "unavailable":
                category = "unavailable_final" if result["is_final"] else "unavailable_temporary"
            if result["target_date"] and result["target_date"] <= as_of:
                mature[segment][category] += 1
        coverage[segment][category] += 1
        for when in ("evidence", "known"):
            refs = result.get("context_refs", {}).get(when, []) if result else []
            market = next((r for r in refs if r["source_snapshot_id"] == "market"), {})
            theme = next((r for r in refs if r["source_snapshot_id"].startswith("audit/themes/")), {})
            for field, reference in (("phase", market), ("heat_level", market), ("quality_level", market),
                                     ("sentiment_state", market), ("theme_id", theme)):
                status = reference.get("current_reference_status", "missing")
                value = reference.get("values", {}).get(field)
                bucket = context_groups[(segment, when, field, status, value)]
                bucket["samples"] += 1
                bucket[category] += 1
        volume = event.get("volume", {})
        gap = volume.get("relative_gap")
        group = "unknown" if gap is None else "near_tie" if compare(gap, near_tie, "le") else volume["volume_constraint_driver"]
        groups[segment][group] += 1
        density = event.get("pivot_density", {})
        for key, label in (("cross_cycle", density.get("cross_cycle")), ("round_count", density.get("round_count")),
                           ("all", "all")):
            bucket = density_groups[key][(segment, "unknown" if label is None else str(label).lower())]
            bucket["samples"] += 1
            bucket["density_known" if density.get("value") is not None else "density_unknown"] += 1
            bucket[category] += 1
            if (category in ("close_above_target_high", "end_return_target_met", "failed", "expired")
                    and result["target_date"] and result["target_date"] <= as_of):
                bucket["mature_evaluable"] += 1

    def percentage(n, d):
        return n / d if d else None

    by_segment = {}
    for segment, counts in coverage.items():
        done = mature[segment]
        successes = done["close_above_target_high"] + done["end_return_target_met"]
        denominator = successes + done["failed"] + done["expired"]
        volume_counts = groups[segment]
        volume_denominator = counts["total"] - volume_counts["unknown"]
        by_segment[segment] = {
            "counts": dict(counts), "mature_counts": dict(done),
            "mature_evaluable": denominator, "success_rate": percentage(successes, denominator),
            "breakout_rate": percentage(done["close_above_target_high"], denominator),
            "failure_rate": percentage(done["failed"], denominator),
            "target_missed_rate": percentage(done["failed"] + done["expired"], denominator),
            "volume_driver": {"counts": dict(volume_counts), "known": volume_denominator,
                              "rates": {key: percentage(volume_counts[key], volume_denominator)
                                        for key in ("near_tie", "pullback_dominant", "impulse_dominant")}},
        }
    return {
        "total": len(queue), "segments": by_segment, "near_tie_relative_gap_max": near_tie,
        "volume_constraint_formula": "max(1.3 × pullback_mean, 0.8 × impulse_mean)",
        "context_groups": [{"segment_index": segment, "as_of_event": when, "field": field,
                            "reference_status": status, "value": value, **counts}
                           for (segment, when, field, status, value), counts in context_groups.items()],
        "density_views": {key: [{"segment_index": segment, "group": label, **counts}
                                for (segment, label), counts in entries.items()]
                          for key, entries in density_groups.items()},
    }


def _retained_factor_bases(store, symbols, as_of, cutoff):
    """Return the accepted factors together with the receipt that pins their raw prices."""
    from app.services.evening_report_inputs import factor_file_reference

    references = store.acceptance_references()
    for day in sorted((day for day in references if day <= as_of), reverse=True):
        for at, input_id in reversed(references[day]):
            accepted_at = datetime.fromisoformat(at)
            if accepted_at.tzinfo is None or accepted_at > cutoff:
                continue
            receipt = store.accepted_input(input_id, at, day=day)
            if not receipt:
                continue
            facts = receipt["wave_factor_facts"]
            source = factor_file_reference(receipt["versions"])
            if facts is not None and source:
                bases = {s: PriceBasis.from_accepted_facts(facts.get(s, {}), source, receipt["wave_factor_known_at"])
                         for s in symbols if facts.get(s, {}) is not None}
                return bases, receipt
    return {}, None


def _study_quotes(store, inputs, needed, receipt):
    """Read admitted quotes for dates that have not yet entered a publication."""
    import polars as pl

    if not needed:
        return {}, {}, {}
    symbols = set().union(*needed.values())
    current = daily_partition_versions(inputs.provenance.get("input_versions", {}))
    pinned = daily_partition_versions(receipt["versions"]) if receipt else current
    covered = set(receipt["active_symbols"]) | set(receipt["halted_symbols"]) if receipt else symbols
    live = {day for day in needed if pinned.get(day) == current.get(day)}
    frame = inputs.prices.filter(pl.col("symbol").is_in(symbols & covered) & pl.col("date").cast(pl.String).is_in(live)
                                 & pl.col("wave_formal")).select("symbol", pl.col("date").cast(pl.String), "raw_close", "raw_low")
    prices = {(s, day): [close, low] for s, day, close, low in frame.iter_rows() if s in needed[day]}
    times = {}
    low_times = {}

    def record_time(batch, symbol, day):
        key = symbol, day
        if batch and (pair := batch.get("study_quote_clocks", {}).get(day, {}).get(symbol)):
            times[key] = pair[0]
            if pair[1] != pair[0]:
                low_times[key] = pair[1]
        elif batch:
            clocks = batch["wave_price_clocks"]
            times[key] = clocks["rows"].get(day, {}).get(symbol) or max(
                clocks["dates"].get(day, batch["cutoff"]), batch["wave_first_acceptances"].get(symbol, batch["cutoff"]))
        else:
            times[key] = inputs.cutoff

    halts = ([*receipt["historical_suspensions"], *receipt["halted_evidence"]] if receipt else
             inputs.provenance.get("suspensions", []))
    for halt in halts:
        symbol, day = halt["symbol"], str(halt["date"])
        if halt.get("confirmed") and day in live and symbol in needed[day]:
            prices.setdefault((symbol, day), "suspended")

    waiting = {day: stocks - covered if day in live else stocks.copy() for day, stocks in needed.items()}
    # The actual accepted observation takes precedence even if the warehouse now differs.
    if receipt:
        for day in needed.keys() & receipt["study_quotes"].keys():
            for symbol in needed[day].intersection(receipt["study_quotes"][day]):
                prices[symbol, day] = receipt["study_quotes"][day][symbol]
                waiting[day].discard(symbol)
    waiting = {day: stocks for day, stocks in waiting.items() if stocks}
    if waiting:
        cutoff = receipt["cutoff"] if receipt else inputs.cutoff
        references = store.acceptance_references()
        batches = sorted(((at, ref, day) for day, entries in references.items() if min(waiting) <= day <= inputs.trade_date
                          for at, ref in entries if at <= cutoff), reverse=True)
        for at, ref, day in batches:
            if receipt and ref == receipt["input_id"]:
                continue
            older = store.accepted_input(ref, at, day=day)
            for trading_day in list(waiting):
                if trading_day > day:
                    continue
                if not older:
                    del waiting[trading_day]
                    continue
                quotes = older["study_quotes"].get(trading_day, {})
                for symbol in waiting[trading_day].intersection(quotes):
                    prices[symbol, trading_day] = quotes[symbol]
                    record_time(older, symbol, trading_day)
                    waiting[trading_day].remove(symbol)
                if not waiting[trading_day]:
                    del waiting[trading_day]
            if not waiting:
                break
    for symbol, day in prices.keys() - times.keys():
        record_time(receipt, symbol, day)
    return prices, times, low_times


def _study_acceptances(store, receipt, quotes, times, low_times, *, frozen_quotes=()):
    """Find usable factor coverage and earlier joint observations of the open study prices."""
    from app.services.evening_report_inputs import factor_file_reference

    if not receipt or receipt["wave_factor_facts"] is None:
        return {}, {}
    cutoff = receipt["cutoff"]
    latest = receipt["wave_factor_facts"]
    latest_source = factor_file_reference(receipt["versions"])
    covered = {key: cutoff for key, value in quotes.items()
               if isinstance(value, list) and times[key] <= cutoff and key[1] <= receipt["date"]
               and latest.get(key[0], {}) is not None}
    if not covered:
        return {}, {}

    # Original receipts save a day's admission and subsequent changes. Carry those observed
    # raw values forward to each factor acceptance; the two must be valid at the same cutoff.
    earliest_day = min(day for _, day in covered)
    refs = sorted((at, day, ref) for day, entries in store.acceptance_references().items()
                  if earliest_day <= day <= receipt["date"] for at, ref in entries if at < cutoff)
    observed = {}
    versions = defaultdict(list)
    symbols = {symbol for symbol, _ in covered}
    last = {}
    for at, day, ref in refs:
        batch = store.accepted_input(ref, at, day=day)
        if not batch:
            observed.clear()
            last.clear()
            continue
        facts = batch["wave_factor_facts"]
        source = factor_file_reference(batch["versions"])
        same_basis = set()
        if source == latest_source and facts is not None:
            same_basis = {s for s in symbols if facts.get(s, {}) == latest.get(s, {})}
        changed = defaultdict(dict)
        missing = defaultdict(dict)
        for key in covered:
            symbol, trading_day = key
            if trading_day > day:
                continue
            admitted = batch["study_quotes"].get(trading_day, {})
            if symbol in admitted and key not in frozen_quotes:
                observed[key] = admitted[symbol]
            quote = observed.get(key)
            if key not in observed and times[key] <= at:
                close, low = quotes[key]
                quote = [close, low if low_times.get(key, times[key]) <= at else None]
            if not isinstance(quote, list):
                missing[symbol][trading_day] = None
                continue
            if facts is None or facts.get(symbol, {}) is None:
                continue
            # A coverage time is used with the current file. Keep earlier files
            # with their own observations, including rewrites affecting only a peer.
            if symbol in same_basis and quote[0] == quotes[key][0] and times[key] <= at:
                covered[key] = min(covered[key], at)
            if (symbol not in same_basis or quote != quotes[key]
                    or at < max(times[key], low_times.get(key, times[key]))):
                changed[symbol][trading_day] = quote
        consecutive = {}
        for symbol, earlier_quotes in changed.items():
            earlier_quotes.update(missing.get(symbol, {}))
            prior = last.get(symbol)
            if (prior and prior["source"] == source and prior["facts"] == facts.get(symbol, {})
                    and prior["quotes"] == earlier_quotes):
                prior["through"] = at
            else:
                prior = {"at": at, "source": source,
                         "facts": facts.get(symbol, {}), "quotes": earlier_quotes}
                versions[symbol].append(prior)
            consecutive[symbol] = prior
        last = consecutive
    return covered, versions


def archive_study_market(store, inputs, report, history):
    """Publish one interval of observations for securities with archived starts."""
    prior = history[-1] if history else {}
    source = inputs.provenance.get("calendar", {})
    calendar = source.get("days", inputs.calendar)
    needed = defaultdict(set)
    pending = prior.get("state", {}).get("study_windows", {})
    active = {}
    new_schedules = {}

    def include(known_day, symbols, since):
        saved = pending.get(known_day, {}).get("days", [])
        days = _window_days(known_day, calendar, DEFAULT_SESSIONS, saved, source.get("start"))
        observed = days[:bisect_right(days, inputs.trade_date)]
        for day in observed[bisect_right(observed, since):]:
            needed[day].update(symbols)
        if len(days) <= DEFAULT_SESSIONS or days[-1] > inputs.trade_date:
            window = active.setdefault(known_day, {"days": days, "through": observed[-1] if observed else since,
                                                   "symbols": set()})
            window["symbols"].update(symbols)
        if days != saved:
            new_schedules[known_day] = days

    for known, window in pending.items():
        for day, symbols in window.get("missing", {}).items():
            needed[day].update(symbols)
        include(known, window["symbols"], window["through"])
    new_starts = defaultdict(set)
    for event in report["wave_starts"]:
        new_starts[_local_date(event["known_at"])].add(event["security_id"])
    for known, symbols in new_starts.items():
        include(known, symbols, "")
    if active:
        report["state"]["study_windows"] = {k: {**w, "symbols": sorted(w["symbols"])} for k, w in active.items()}
    if new_schedules:
        report["study_calendars"] = new_schedules
    if not needed:
        return
    quotes, times, low_times = _study_quotes(store, inputs, needed, inputs.study_price_receipt)
    market = {day: {s: quotes.get((s, day)) for s in symbols} for day, symbols in needed.items()}
    report["study_quotes"] = market
    # The publication cutoff already dates same-batch quotes. Keep earlier acceptances explicitly.
    other_times = defaultdict(dict)
    for (symbol, day), at in times.items():
        low_at = low_times.get((symbol, day), at)
        if quotes[symbol, day] is not None and (at != inputs.cutoff or low_at != at):
            other_times[day][symbol] = at if at == low_at else [at, low_at]
    if other_times:
        report["study_quote_times"] = dict(other_times)
    for window in report["state"].get("study_windows", {}).values():
        missing = {day: [s for s in window["symbols"] if s in market[day] and market[day][s] is None]
                   for day in window["days"] if day in market}
        if unresolved := {day: stocks for day, stocks in missing.items() if stocks}:
            window["missing"] = unresolved
    if inputs.wave_factors is not None and inputs.study_price_receipt:
        receipt = inputs.study_price_receipt
        facts = receipt["wave_factor_facts"]
        earlier = defaultdict(dict)
        coverage, versions = _study_acceptances(store, receipt, quotes, times, low_times)
        for (symbol, day), at in coverage.items():
            if at != receipt["cutoff"]:
                earlier[day][symbol] = at
        report["study_factors"] = {
            "source": inputs.provenance["wave_factor_source"], "known_at": receipt["wave_factor_known_at"],
            "facts": {s: facts[s] for s in set().union(*needed.values()) if s in facts},
            **({"earlier_coverage": dict(earlier)} if earlier else {}),
            **({"earlier_inputs": dict(versions)} if versions else {}),
        }


def _first_terminal_proof(event, versions, prices, day_bases, window, **options):
    """Compare complete accepted paths, including the round's archived cause at each cutoff."""
    symbol = event["security_id"]
    options["window_days"] = window
    result = evaluate_start(event, prices=prices, published_bases=day_bases, **options)
    cutoff = result.get("outcome_known_at")
    if not versions or (not cutoff and not options["invalidated"]):
        return result
    candidates = [result] if cutoff else []
    for version in sorted(versions, key=lambda v: v["at"]):
        at = version["at"]
        through = version.get("through", at)
        if cutoff and at > cutoff:
            break
        if through < event["known_at"] or not any(day in version["quotes"] for day in window):
            continue
        earlier_prices = {(symbol, d): prices[symbol, d] for d in window if (symbol, d) in prices}
        earlier_bases = dict(day_bases)
        prior_basis = PriceBasis.from_accepted_facts(version["facts"], version["source"], at)
        for day, quote in version["quotes"].items():
            if quote is None:
                earlier_prices.pop((symbol, day), None)
                continue
            earlier_prices[symbol, day] = {"raw_close": quote[0], "raw_low": quote[1], "wave_formal": True,
                                           "known_at": at, "factor_covered_at": at}
            earlier_bases[day] = prior_basis
        prior = evaluate_start(event, prices=earlier_prices, published_bases=earlier_bases, accepted_through=through, **options)
        when = prior.get("outcome_known_at")
        if when and when <= through and (not cutoff or when <= cutoff):
            candidates.append(prior)

    # A confirmed structure event competes with every already proven terminal
    # outcome. Ordinary price revisions must still support the same terminal fact.
    if not any(row["evaluation_reason_code"] == "cycle_structure_invalid" for row in candidates):
        candidates = [row for row in candidates if row["fact_date"] == result.get("fact_date")
                      and row["evaluation_reason_code"] == result["evaluation_reason_code"]]
    return min(candidates, key=lambda r: (r["outcome_known_at"], r["fact_date"],
                                          r["evaluation_reason_code"] != "cycle_structure_invalid"), default=result)


def run_study(store, inputs, *, evaluated_at, sessions=DEFAULT_SESSIONS, end_return=DEFAULT_END_RETURN):
    """Research uses the original archives and an accepted as-of input, never republishes."""
    import polars as pl

    calendar = sorted(set(inputs.provenance.get("calendar", {}).get("days", inputs.calendar)))
    schedules = {}
    queue = {}
    publications = []
    published_quotes = {}
    published_bases = defaultdict(dict)
    published_times = {}
    published_factor_times = {}
    versions = defaultdict(list)
    published_low_times = {}
    invalidations = {}
    evaluation_time = datetime.fromisoformat(evaluated_at)
    input_cutoff = datetime.fromisoformat(inputs.cutoff)
    for entry in reversed(store.dates()):
        if entry["date"] > inputs.trade_date:
            continue
        report = store.get(entry["date"])
        if (not report or not published_on_trade_date(report) or datetime.fromisoformat(report["cutoff"]) > input_cutoff
                or datetime.fromisoformat(report["generated_at"]) > evaluation_time):
            continue
        keys = []
        for event in report.get("wave_starts", ()):
            if event["rule_version"] == WAVE_VERSION and event["known_at"] <= report["cutoff"]:
                key = start_key(event)
                keys.append({"event_key": list(key), "known_at": event["known_at"]})
                queue.setdefault(key, event | {"report_date": report["trade_date"], "report_version": report["version"]})
        schedules.update(report.get("study_calendars", {}))
        factors = report.get("study_factors")
        report_bases = {}
        admitted = defaultdict(set)
        for day, quotes in report.get("study_quotes", {}).items():
            stocks = {s for s in quotes if published_quotes.get((s, day)) is None and quotes[s] is not None}
            for symbol in stocks:
                at = report.get("study_quote_times", {}).get(day, {}).get(symbol, report["cutoff"])
                if isinstance(at, list):
                    at, published_low_times[symbol, day] = at
                published_times[symbol, day] = at
            if factors:
                for symbol in stocks:
                    facts = factors["facts"].get(symbol, {})
                    if facts is None:
                        continue
                    published_factor_times[symbol, day] = factors.get("earlier_coverage", {}).get(day, {}).get(symbol, report["cutoff"])
                    if symbol not in report_bases:
                        report_bases[symbol] = PriceBasis.from_accepted_facts(facts, factors["source"], factors["known_at"])
                    published_bases[symbol][day] = report_bases[symbol]
                    admitted[symbol].add(day)
            published_quotes.update({(s, day): quotes[s] for s in stocks})
        for symbol, dates in admitted.items():
            for version in factors.get("earlier_inputs", {}).get(symbol, ()):
                earlier = {day: quote for day, quote in version["quotes"].items() if day in dates}
                if earlier:
                    versions[symbol].append(version | {"quotes": earlier})
        gaps = (report.get("wave_rule") or {}).get("acceptance_gaps")
        publications.append({"report_date": report["trade_date"], "report_version": report["version"],
                             "input_cutoff": report["cutoff"], "starts": keys,
                             **({"acceptance_gaps": gaps} if gaps else {})})
        for event in report.get("wave_events", ()):
            if event["type"] == "structure_invalid" and known_by(event) <= report["cutoff"]:
                # The first published breach closes this round.
                invalidations.setdefault(event["round_id"], event)
    active = {symbol for symbol, _, _ in queue}
    bases = {}
    factor_reference = None
    retained_cutoff = None
    receipt = getattr(inputs, "study_price_receipt", None)
    if inputs.wave_factors is not None:
        factors = {symbol[0]: list(group.select("trade_date", "ex_factor").iter_rows())
                   for symbol, group in inputs.wave_factors.filter(pl.col("symbol").is_in(active))
                   .partition_by("symbol", as_dict=True).items()}
        factor_facts = inputs.provenance.get("wave_factor_facts", {})
        for symbol in active:
            facts = factor_facts.get(symbol, {})
            if facts is not None:
                bases[symbol] = PriceBasis(factors.get(symbol, []), inputs.provenance.get("wave_factor_source"),
                                           inputs.provenance.get("wave_factor_known_at") or inputs.cutoff,
                                           factor_known_at={day: at for day, (_, at) in facts.items()})
    elif active:
        bases, receipt = _retained_factor_bases(store, active, inputs.trade_date, min(evaluation_time, input_cutoff))
        if receipt:
            factor_reference = {k: receipt[k] for k in ("date", "cutoff", "input_id")}
            retained_cutoff = datetime.fromisoformat(receipt["cutoff"])
    needed = defaultdict(set)
    through = receipt["date"] if receipt else inputs.trade_date
    windows = {}
    coverage_start = inputs.provenance.get("calendar", {}).get("start")
    for event in queue.values():
        known = _local_date(event["known_at"])
        if known not in windows:
            windows[known] = _window_days(known, calendar, sessions, schedules.get(known, ()), coverage_start)
        symbol = event["security_id"]
        for day in windows[known]:
            if day <= through and (symbol, day) not in published_quotes:
                needed[day].add(symbol)
    quotes, times, low_times = _study_quotes(store, inputs, needed, receipt)
    quotes.update(published_quotes)
    times.update(published_times)
    low_times.update(published_low_times)
    missing_bases = {key: quotes[key] for key in quotes.keys() - published_factor_times.keys()}
    factor_times, open_versions = _study_acceptances(store, receipt, missing_bases, times, low_times,
                                                     frozen_quotes=published_quotes)
    factor_times.update(published_factor_times)
    for symbol, observations in open_versions.items():
        versions[symbol].extend(observations)
    prices = {key: {"raw_close": value[0], "raw_low": value[1], "wave_formal": True, "known_at": times[key],
                    "factor_covered_at": factor_times.get(key), "low_known_at": low_times.get(key)}
              for key, value in quotes.items() if isinstance(value, list)}
    suspended = {key for key, value in quotes.items() if value == "suspended"}

    @lru_cache(maxsize=8)
    def context_source(day, version):
        return store.referenced_report(day, version) if version else None

    results = []
    for event in queue.values():
        basis = bases.get(event["security_id"])
        if retained_cutoff and datetime.fromisoformat(event["known_at"]) > retained_cutoff:
            basis = None
        window = windows[_local_date(event["known_at"])]
        symbol = event["security_id"]
        result = _first_terminal_proof(
            event, versions.get(symbol, ()), prices, published_bases.get(symbol, {}), window,
            calendar=calendar, basis=basis, invalidated=invalidations.get(event["round_id"]), suspended=suspended,
            basis_through=factor_reference["date"] if factor_reference else None,
            as_of=inputs.trade_date, evaluated_at=evaluated_at,
            sessions=sessions, end_return=end_return)
        if sessions > DEFAULT_SESSIONS:
            coverage = Counter()
            for day in window[DEFAULT_SESSIONS + 1:bisect_right(window, inputs.trade_date)]:
                key = event["security_id"], day
                if quotes.get(key) is None:
                    coverage["missing"] += 1
                elif key in published_quotes:
                    coverage["archived"] += 1
                else:
                    coverage["accepted"] += 1
            result["extended_window_quote_coverage"] = dict(coverage)
        result["context_refs"] = study_context(event.get("context_refs", {}), context_source, evaluated_at)
        results.append(result | {"report_date": event["report_date"], "report_version": event["report_version"]})
    return {"as_of": inputs.trade_date, "evaluated_at": evaluated_at, "input_cutoff": inputs.cutoff,
            "data_version": inputs.provenance.get("input_versions"), "wave_rule_version": WAVE_VERSION,
            **({"factor_input_reference": factor_reference} if factor_reference else {}),
            "evaluation_rule_version": VERSION, "evaluation_sessions": sessions, "end_return_threshold": end_return,
            "publications": publications, "results": results,
            "statistics": summarize_starts(queue, results, as_of=inputs.trade_date, sessions=sessions, end_return=end_return)}
