import json
from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest
from evening_receipt_fixture import current_receipt
from test_evening_report_wave import (
    BARS,
    SYMBOL,
    _wave_market_receipts,
    calendar,
    run_wave,
    wave_inputs,
)

from app.services import evening_report_store
from app.services.evening_report_archive import SCHEMA_VERSION
from app.services.evening_report_observation_study import compare_observations
from app.services.evening_report_start_study import (
    evaluate_start,
    run_study,
    start_key,
    summarize_starts,
)
from app.services.evening_report_wave import VERSION, PriceBasis
from app.services.evening_report_wave_context import add_context_refs, reference_status


def event_for(days, *, segment=3, high=15., low=11.):
    basis = PriceBasis([], "accepted", f"{days[2]}T16:01:00+08:00")
    known_at = f"{days[2]}T16:01:00+08:00"
    return {"security_id": SYMBOL, "start_id": f"s{segment}", "round_id": "cycle", "segment_index": segment,
            "known_at": known_at, "evidence_date": days[1], "rule_version": VERSION,
            "target_high": basis.evidence(high, "high", days[0], days[0]) | {"pivot_id": "h"},
            "failure_low": basis.evidence(low, "low", days[0], days[0]) | {"pivot_id": "l"}}


def result_for(closes, *, start_low=13., invalidation=None, gap=None, sessions=10, end_return=.08, high=15., low=11.):
    days = calendar(len(closes) + 2)
    full_calendar = calendar(max(len(days), 13))
    event = event_for(days, high=high, low=low)
    prices = {(SYMBOL, d): {"raw_close": close, "raw_low": start_low, "wave_formal": True}
              for d, close in zip(days[2:], closes, strict=True) if d != (days[gap] if gap is not None else None)}
    return event, evaluate_start(event, calendar=full_calendar, prices=prices,
                                 suspended=set(), basis=PriceBasis([], "accepted", days[-1]),
                                 invalidated=invalidation,
                                 as_of=days[-1], evaluated_at=days[-1], sessions=sessions, end_return=end_return)


@pytest.mark.parametrize(("closes", "trigger", "outcome"), [
    ([15.1, 10.9], 0, "success_at_start"),
    ([14.2, 15.0, 15.1, 10.9], 2, "close_above_target_high"),
    ([14.2, 10.9, 15.1], 1, "close_below_failure_low"),
    ([14.2, 12.9, 15.1], 1, "close_below_start_low_before_new_high"),
    ([14.2, 15.0, 14.8], None, "not_due"),
])
def test_evaluation_uses_known_day_segment_anchors_and_first_terminal(closes, trigger, outcome):
    event, result = result_for(closes)
    assert result["evaluation_reason_code"] == outcome
    assert result["segment_index"] == 3 and result["target_high"]["price"] == 15
    assert result["start_date"] > event["evidence_date"]
    assert result["start_close"]["price"] == closes[0]
    if trigger is not None:
        assert result["forward_sessions"] == trigger and result["is_final"]


def test_window_expiry_and_maturity_denominator():
    event, still_open = result_for([10, 10.8], high=12, low=9, start_low=9.5)
    early = summarize_starts({start_key(event): event}, [still_open],
                             as_of=still_open["as_of"], sessions=10, end_return=.08)
    assert early["segments"][3]["mature_evaluable"] == 0
    _, expired = result_for([10, 10.8, *[10.2] * 9], high=12, low=9, start_low=9.5)
    _, success = result_for([10, 10.8, *[10.2] * 8, 10.8], high=12, low=9, start_low=9.5)
    assert expired["evaluation_reason_code"] == "target_not_reached"
    assert success["evaluation_reason_code"] == "end_return_target_met"
    full = summarize_starts({start_key(event): event}, [success],
                            as_of=success["as_of"], sessions=10, end_return=.08)
    assert full["segments"][3]["success_rate"] == 1
    assert full["segments"][3]["breakout_rate"] == 0


@pytest.mark.parametrize(("first_factor", "expected"), [(1.04, "expired"), (1.01, "failed")])
def test_maturity_competes_with_the_first_accepted_structure_event(tmp_path, private_owner, monkeypatch,
                                                                  first_factor, expected):
    from app.services.evening_report_inputs import load_inputs
    from app.services.kline_sync import _merge_adj_factor_store

    bars = [*BARS[:-1], (12.1, 10.49, 11.70, 100), *([(11.72, 11.45, 11.60, 80)] * 9), (11.71, 10.30, 10.53, 80)]
    stocks, peer, accept = _wave_market_receipts(tmp_path, monkeypatch, bars)
    seed, start, *window, _ = stocks["date"].unique().sort().to_list()[-13:]
    last = window[-1]
    symbols = [SYMBOL, peer]

    def factor(value):
        _merge_adj_factor_store(tmp_path / "adj_factor" / "all.parquet",
                                pl.DataFrame({"symbol": [SYMBOL], "trade_date": [start], "ex_factor": [value]}))

    accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    factor(1.02)
    for day in [start, *window[:-1]]:
        accept(day, 17, stocks.filter(pl.col("date") == day), symbols, publish=True)
    factor(first_factor)
    accept(last, 16, stocks.filter(pl.col("date") == last), symbols)
    factor(1.01)
    report, store, _ = accept(last, 17, stocks.head(0), symbols, publish=True)
    receipt = store.readiness(str(last))
    study = run_study(store, load_inputs(tmp_path, last, receipt), evaluated_at=receipt["cutoff"])
    result, = study["results"]
    breach, = [e for e in report["wave_events"] if e["type"] == "structure_invalid"]
    assert breach["known_at"] == f"{last}T{17 if expected == 'expired' else 16}:00:00+08:00"
    assert (result["status"], result["outcome_known_at"]) == (expected, f"{last}T16:00:00+08:00")
    group = study["statistics"]["segments"][2]
    assert group["counts"] == {"total": 1, expected: 1}
    assert group["failure_rate"] == (1.0 if expected == "failed" else 0.0)


def test_structure_failure_prices_the_archived_close_on_its_fact_day():
    days = calendar(7)
    start, missing, fact, reported, filled = days[2:]

    def at(day):
        return f"{day}T17:00:00+08:00"

    basis = PriceBasis([(fact, 1.02), (reported, 1.03)], "accepted", at(start))
    event = event_for(days)
    breach = {"event_id": "breach", "date": fact, "known_at": at(reported),
              "close": basis.evidence(10.55, "close", fact, fact)}
    prices = {(SYMBOL, day): {"raw_close": 14., "raw_low": 13., "wave_formal": True,
                              "known_at": at(filled if day == missing else day)} for day in days[2:]}
    result = evaluate_start(event, calendar=days, prices=prices, suspended=set(), basis=basis, invalidated=breach,
                            as_of=filled, evaluated_at=at(filled))
    assert result["failure_event"]["source_event_id"] == breach["event_id"]
    assert result["fact_date"] == result["failure_event"]["fact_date"] == fact
    assert result["outcome_known_at"] == at(filled)
    quote = result["trigger_close"]
    assert (quote["raw_price"], quote["adjust_base_date"], quote["applied_adjustment_multiplier"]) == (10.55, start, 1.02)
    assert result["return"] == pytest.approx(10.55 * 1.02 / 14. - 1)


@pytest.mark.parametrize(("close", "reason"), [(12.10, "close_above_target_high"),
                                              (11.25, "close_below_start_low_before_new_high")])
def test_study_quote_times_follow_the_consumed_fields(tmp_path, private_owner, monkeypatch, close, reason):
    from app.services.evening_report_inputs import load_inputs

    stocks, peer, accept = _wave_market_receipts(
        tmp_path, monkeypatch, [*BARS[:-1], (12.1, 11.4, 11.85, 100), (12.2, 11.1, close, 80)])
    seed, start, trigger, _ = stocks["date"].unique().sort().to_list()[-4:]
    symbols = [SYMBOL, peer]
    accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    _, store, _ = accept(start, 17, stocks.filter(pl.col("date") == start), symbols, publish=True)
    bar = stocks.filter(pl.col("date") == trigger)
    accept(trigger, 16, bar, symbols)
    bar = bar.filter(pl.col("symbol") == SYMBOL).with_columns((pl.col("volume") + 1).alias("volume"))
    accept(trigger, 17, bar, symbols)

    def verify():
        receipt = store.readiness(str(trigger))
        result, = run_study(store, load_inputs(tmp_path, trigger, receipt), evaluated_at=receipt["cutoff"])["results"]
        assert (result["evaluation_reason_code"], result["outcome_known_at"]) == (reason, f"{trigger}T16:00:00+08:00")
        if result["status"] == "failed":
            assert result["failure_event"]["known_at"] == result["outcome_known_at"]

    verify()
    report, _, _ = accept(trigger, 18, bar.with_columns(pl.lit(11.05).alias("low")), symbols, publish=True)
    assert report["study_quote_times"][str(trigger)][SYMBOL] == [f"{trigger}T16:00:00+08:00", report["cutoff"]]
    verify()


def test_price_gap_does_not_change_anchor_or_skip_to_a_later_success():
    _, transient = result_for([14., 14.1, 15.2], gap=2)
    _, final = result_for([14., 14.1, 15.2, *[14.] * 8], gap=2)
    assert transient["evaluation_reason_code"] == final["evaluation_reason_code"] == "missing_rows"
    assert not transient["is_final"] and final["is_final"]
    assert final["start_date"] == transient["start_date"]
    _, contradictory = result_for([14., 15.2], start_low=15.3)
    assert contradictory["evaluation_reason_code"] == "price_basis_unavailable"
    _, invalid_anchor = result_for([14.], high=0.)
    assert invalid_anchor["evaluation_reason_code"] == "price_basis_unavailable"


def test_uncertain_invalidation_cannot_turn_a_published_start_into_a_success():
    days = calendar(7)
    invalidation = {"event_id": "invalid", "known_at": None, "date": days[1], "close": {"raw_price": 14.},
                    "acceptance_gap_after": f"{days[3]}T17:00:00+08:00", "known_by": f"{days[4]}T18:00:00+08:00"}
    _, uncertain = result_for([14., 14.2, 15.2, 15.1], invalidation=invalidation)
    assert uncertain["evaluation_reason_code"] == "structure_invalidation_time_unknown"
    assert uncertain["status"] == "unavailable" and uncertain["is_final"]
    _, earlier = result_for([15.2, 15., 15.2, 15.1], invalidation=invalidation)
    assert earlier["evaluation_reason_code"] == "success_at_start"
    # With both bounds on the same market day its calendar date is established.
    _, same_day = result_for([14., 14.2, 15.2, 15.1], invalidation={**invalidation, "date": days[4]})
    assert same_day["evaluation_reason_code"] == "cycle_structure_invalid" and same_day["terminal_date"] == days[4]


def test_study_only_uses_structure_failure_after_publication(tmp_path, monkeypatch):
    days = calendar(9)
    store = evening_report_store.EveningReportStore(tmp_path)
    start = event_for(days)

    def at(day, hour=17):
        return f"{day}T{hour}:00:00+08:00"

    def publish(day, **fields):
        monkeypatch.setattr(evening_report_store, "cn_now", lambda: datetime.fromisoformat(at(day, 18)))
        return store.save(day, {"trade_date": day, "cutoff": at(day), "audit": {}, **fields}, {})

    def evaluate(day, cutoff=17, evaluated=18):
        inputs = wave_inputs([(14.5, 13., 14., 100)] * (days.index(day) + 1))
        inputs.cutoff = at(day, cutoff)
        result, = run_study(store, inputs, evaluated_at=at(day, evaluated))["results"]
        return result

    publish(days[2], wave_starts=[start])
    day = days[4]
    breach = {"type": "structure_invalid", "round_id": start["round_id"], "event_id": "breach", "date": day,
              "known_at": at(day), "failure_low": start["failure_low"], "close": {"raw_price": 14.}}
    publish(day, wave_events=[breach])
    assert evaluate(day, cutoff=16)["status"] == "not_due"
    assert evaluate(day, evaluated=17)["status"] == "not_due"
    for as_of in (day, days[5]):
        result = evaluate(as_of)
        assert (result["start_invalidated"], result["evaluation_reason_code"], result["terminal_date"]) == (
            breach["event_id"], "cycle_structure_invalid", day)


def test_gap_up_confirmed_by_waves_counts_as_success_at_start():
    run = run_wave([*BARS[:8], (12.55, 12.05, 12.55, 70), (12.80, 12.20, 12.70, 130)])
    run.advance(run.days[0])
    [event] = run.starts
    assert event['target_high']['raw_price'] == 12.
    assert event['failure_low']['raw_price'] == 12.05
    result = evaluate_start(event, calendar=run.days, prices={(SYMBOL, day): row for day, row in run.rows.items()},
                            suspended=set(), basis=run.basis, invalidated=None, as_of=run.target,
                            evaluated_at=event['known_at'])
    assert result['evaluation_reason_code'] == 'success_at_start'
    assert result['is_final'] and result['forward_sessions'] == 0


@pytest.mark.parametrize(("closes", "reason", "offset"), [
    ([13., 12., 14.], "success_at_start", 0),
    ([12., 13., 14.], "close_above_target_high", 1),
    ([12., 12., 14.], "price_basis_unavailable", 2),
])
def test_factor_interruption_only_blocks_uncovered_days(tmp_path, monkeypatch, closes, reason, offset):
    days = calendar(5)
    start, covered, missing = days[2:]
    store = evening_report_store.EveningReportStore(tmp_path)
    event = event_for(days)
    monkeypatch.setattr(evening_report_store, "cn_now", lambda: datetime.fromisoformat(event["known_at"]))
    store.save(start, {"trade_date": start, "cutoff": event["known_at"], "audit": {}, "wave_starts": [event],
                       "study_quotes": {start: {SYMBOL: [closes[0], 11.]}}}, {})
    inputs = wave_inputs([(14., 11., c, 100.) for c in [12., 12., *closes]])
    sources = inputs.provenance["input_versions"]["files"]
    source = {"files": [*sources[:-1], ["adj_factor/all.parquet", 80, 10]]}
    facts = {SYMBOL: {start: [1.25, event["known_at"]]}}
    cutoff = covered + "T17:00:00+08:00"
    proof = current_receipt(covered, cutoff, versions=source, wave_factor_facts=facts,
                            active_symbols=[SYMBOL], study_quotes={covered: {SYMBOL: [closes[1], 11.]}})
    store.save_readiness(covered, proof)
    store.save_readiness(missing, current_receipt(missing, missing + "T17:00:00+08:00", versions=source))
    # A later successful retry is already on disk, but cannot extend coverage at 17:00.
    store.save_readiness(missing, current_receipt(missing, missing + "T18:00:00+08:00",
                                                 versions=source, wave_factor_facts=facts))
    inputs.wave_factors = None
    study = run_study(store, inputs, evaluated_at=inputs.cutoff)
    result, = study["results"]
    assert study["factor_input_reference"] == {"date": covered, "cutoff": cutoff, "input_id": proof["input_id"]}
    assert result["target_high"]["price"] == 12.
    assert (result["evaluation_reason_code"], result["fact_date"], result["is_final"]) == (
        reason, days[2 + offset], offset < 2)


@pytest.mark.parametrize("factor_outage", [False, True])
def test_published_start_keeps_price_and_terminal_after_source_loss(tmp_path, private_owner, monkeypatch, factor_outage):
    from app.services import evening_report_inputs as source

    stocks, other, accept = _wave_market_receipts(tmp_path, monkeypatch, BARS)
    seed, signal, following = stocks["date"].unique().sort().to_list()[-3:]
    symbols = [SYMBOL, other]
    accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    published, store, _ = accept(signal, 17, stocks.filter(pl.col("date") == signal), symbols, publish=True)
    proof = store.readiness(str(signal))
    bar = stocks.filter((pl.col("symbol") == SYMBOL) & (pl.col("date") == signal))
    if factor_outage:
        monkeypatch.setattr(source, "wave_factor_inputs", lambda root, day, versions: (None, source.factor_file_reference(versions)))
    (store.root / "inputs" / (proof["input_id"] + ".json.gz")).unlink()
    accept(following, 17, stocks.filter(pl.col("date") == following), symbols, publish=True)
    receipt = store.readiness(str(following))
    inputs = source.load_inputs(tmp_path, following, receipt)
    assert (inputs.wave_factors is None) == factor_outage
    result, = run_study(store, inputs, evaluated_at=receipt["cutoff"])["results"]
    assert (result["evaluation_reason_code"], result["terminal_date"]) == ("success_at_start", str(signal))
    assert result["trigger_close"]["raw_price"] == bar["close"].item()
    assert result["start_low"]["raw_price"] == bar["low"].item()
    assert store.get(str(signal)) == published


@pytest.mark.parametrize(("fill", "reason"), [(12.10, "close_above_target_high"),
                                           (11.25, "close_below_start_low_before_new_high")])
def test_open_study_accepts_late_quote_with_its_first_known_time(tmp_path, private_owner, monkeypatch, fill, reason):
    from app.services.evening_report_inputs import load_inputs

    bars = [*BARS[:-1], (12.1, 11.4, 11.85, 100), (12.2, 11.1, fill, 80), (12., 11.4, 11.8, 80)]
    stocks, other, accept = _wave_market_receipts(tmp_path, monkeypatch, bars)
    seed, start, missing, observed, today = stocks["date"].unique().sort().to_list()[-5:]
    symbols = [SYMBOL, other]
    accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    original, store, _ = accept(start, 17, stocks.filter(pl.col("date") == start), symbols, publish=True)
    assert len(original["wave_starts"]) == 1
    before, _, _ = accept(observed, 17, stocks.filter(pl.col("date") == observed), symbols, publish=True)
    assert before["study_quotes"][str(missing)][SYMBOL] is None
    update = stocks.filter(pl.col("date").is_in([missing, today]))
    accept(today, 16, update, symbols)
    first_fill = store.readiness(str(today))
    live, = run_study(store, load_inputs(tmp_path, today, first_fill), evaluated_at=first_fill["cutoff"])["results"]
    assert (live["evaluation_reason_code"], live["outcome_known_at"]) == (reason, first_fill["cutoff"])
    after, _, _ = accept(today, 17, update, symbols, publish=True)
    proof = store.readiness(str(today))
    inputs = load_inputs(tmp_path, today, proof)
    result, = run_study(store, inputs, evaluated_at=proof["cutoff"])["results"]
    assert (result["evaluation_reason_code"], result["fact_date"]) == (reason, str(missing))
    assert after["study_quote_times"][str(missing)][SYMBOL] == result["outcome_known_at"] == f"{today}T16:00:00+08:00"
    if result["status"] == "failed":
        assert result["failure_event"]["start_id"] == original["wave_starts"][0]["start_id"]
        assert result["failure_event"]["known_at"] == result["outcome_known_at"]
    assert store.get(str(observed)) == before


@pytest.mark.parametrize(("factor_for", "close", "before", "after", "hour"), [
    ("peer", 12.10, "close_above_target_high", "close_above_target_high", 16),
    ("peer", 11.25, "close_below_start_low_before_new_high", "close_below_start_low_before_new_high", 16),
    ("before_window", 12.10, "close_above_target_high", "close_above_target_high", 16),
    ("corrected", 12.10, "close_above_target_high", "close_above_target_high", 16),
    ("corrected", 11.05, "close_below_start_low_before_new_high", "close_below_start_low_before_new_high", 16),
    ("reversed", 11.85, "close_below_start_low_before_new_high", "close_above_target_high", 17),
    ("target", 11.85, "not_due", "close_above_target_high", 17),
    ("factor_outage", 12.10, "price_basis_unavailable", "close_above_target_high", 17),
])
def test_study_times_only_the_adjustments_used_by_its_quote(tmp_path, private_owner, monkeypatch,
                                                           factor_for, close, before, after, hour):
    from app.services import evening_report_inputs
    from app.services.kline_sync import _merge_adj_factor_store

    bars = [*BARS[:-1], (12.1, 11.4, 11.85, 100), (12.2, 11., close, 80)]
    stocks, peer, accept = _wave_market_receipts(tmp_path, monkeypatch, bars)
    seed, start, trigger, _ = stocks["date"].unique().sort().to_list()[-4:]
    symbols = [SYMBOL, peer]
    accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    _, store, _ = accept(start, 17, stocks.filter(pl.col("date") == start), symbols, publish=True)

    def study():
        receipt = store.readiness(str(trigger))
        inputs = evening_report_inputs.load_inputs(tmp_path, trigger, receipt)
        result, = run_study(store, inputs, evaluated_at=receipt["cutoff"])["results"]
        return result

    path = tmp_path / "adj_factor" / "all.parquet"

    def factor(symbol, value, day=trigger):
        _merge_adj_factor_store(path, pl.DataFrame({"symbol": [symbol], "trade_date": [day], "ex_factor": [value]}))

    if factor_for in ("corrected", "before_window"):
        factor(SYMBOL, 1.02)
    elif factor_for == "reversed":
        factor(SYMBOL, .95)
    with monkeypatch.context() as initial:
        if factor_for == "factor_outage":
            initial.setattr(evening_report_inputs, "wave_factor_inputs", lambda root, day, versions: (None, None))
        accept(trigger, 16, stocks.filter(pl.col("date") == trigger), symbols)
    original = study()
    assert original["evaluation_reason_code"] == before
    factor(peer if factor_for in ("peer", "factor_outage") else SYMBOL, 1.03 if factor_for == "corrected" else 1.02,
           day=seed if factor_for == "before_window" else trigger)
    accept(trigger, 17, stocks.head(0), symbols)
    live = study()
    if factor_for == "corrected":
        if close > 12:
            factor(SYMBOL, .93)
            accept(trigger, 18, stocks.head(0), symbols)
            interim = study()
            assert (interim["status"], interim["outcome_known_at"]) == ("failed", f"{trigger}T18:00:00+08:00")
        factor(SYMBOL, 1.02)
    accept(trigger, 19, stocks.head(0), symbols, publish=True)
    for result in (live, study()):
        assert (result["evaluation_reason_code"], result["outcome_known_at"]) == (after, f"{trigger}T{hour}:00:00+08:00")
        assert result["trigger_close"]["factor_known_at"] == result["outcome_known_at"]
        if factor_for in ("corrected", "peer", "before_window"):
            assert result["trigger_close"] == original["trigger_close"]
        if result["status"] == "failed":
            assert result["failure_event"]["known_at"] == result["outcome_known_at"]


def test_late_close_uses_structure_receipt_for_the_same_failure(tmp_path, private_owner, monkeypatch):
    from app.services.evening_report_inputs import load_inputs
    from app.services.kline_sync import _merge_adj_factor_store

    bars = [*BARS[:-1], (12.1, 11.4, 11.70, 100), (12.2, 10.3, 10.45, 80), (12.3, 10.4, 11.71, 80)]
    stocks, peer, accept = _wave_market_receipts(tmp_path, monkeypatch, bars)
    seed, start, trigger, following, _ = stocks["date"].unique().sort().to_list()[-5:]
    symbols = [SYMBOL, peer]
    rows = stocks.filter(pl.col("date") == trigger)

    def set_factor(value):
        _merge_adj_factor_store(tmp_path / "adj_factor" / "all.parquet",
                                pl.DataFrame({"symbol": [SYMBOL], "trade_date": [start], "ex_factor": [value]}))

    accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    set_factor(1.02)
    _, store, _ = accept(start, 17, stocks.filter(pl.col("date") == start), symbols, publish=True)
    accept(trigger, 16, rows.filter(pl.col("symbol") == peer), [peer])
    set_factor(1.01)
    fill = rows.filter(pl.col("symbol") == SYMBOL)
    accept(following, 16, pl.concat([stocks.filter(pl.col("date") == following), fill]), symbols)

    def study():
        receipt = store.readiness(str(following))
        result, = run_study(store, load_inputs(tmp_path, following, receipt), evaluated_at=receipt["cutoff"])["results"]
        return result

    assert study()["evaluation_reason_code"] == "close_below_failure_low"
    report, _, _ = accept(following, 17, stocks.head(0), symbols, publish=True)
    breach, = [e for e in report["wave_events"] if e["type"] == "structure_invalid"]
    result = study()
    assert result["evaluation_reason_code"] == "cycle_structure_invalid"
    assert result["fact_date"] == breach["date"] == str(trigger)
    assert result["trigger_close"]["raw_price"] == breach["close"]["raw_price"] == 10.45
    assert result["outcome_known_at"] == breach["known_at"] == f"{following}T16:00:00+08:00"
    assert result["failure_event"]["source_event_id"] == result["start_invalidated"] == breach["event_id"]


def test_price_failure_precedes_later_same_day_structure_event(tmp_path, private_owner, monkeypatch):
    from app.services.evening_report_inputs import load_inputs
    from app.services.kline_sync import _merge_adj_factor_store

    bars = [*BARS[:-1], (12.1, 11.4, 11.70, 100), (12.2, 11.45, 11.68, 80), (12.2, 10.3, 10.45, 80)]
    stocks, peer, accept = _wave_market_receipts(tmp_path, monkeypatch, bars)
    seed, start, following, trigger, _ = stocks["date"].unique().sort().to_list()[-5:]
    symbols = [SYMBOL, peer]

    def rows(day):
        return stocks.filter(pl.col("date") == day)

    def factor(value):
        _merge_adj_factor_store(tmp_path / "adj_factor" / "all.parquet",
                                pl.DataFrame({"symbol": [SYMBOL], "trade_date": [start], "ex_factor": [value]}))

    accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    factor(1.02)
    original, store, _ = accept(start, 17, rows(start), symbols, publish=True)
    wave_start, = original["wave_starts"]
    accept(following, 16, rows(following), symbols)
    factor(1.04)
    accept(trigger, 16, rows(trigger), symbols)
    factor(1.01)
    report, _, _ = accept(trigger, 17, stocks.head(0), symbols, publish=True)
    breach, = [e for e in report["wave_events"] if e["type"] == "structure_invalid"]
    receipt = store.readiness(str(trigger))
    result, = run_study(store, load_inputs(tmp_path, trigger, receipt), evaluated_at=receipt["cutoff"])["results"]
    assert breach["known_at"] == f"{trigger}T17:00:00+08:00"
    assert result["fact_date"] == breach["date"] == str(trigger)
    assert (result["evaluation_reason_code"], result["outcome_known_at"], result["trigger_close"]["raw_price"],
            result["failure_event"]["source_event_id"]) == (
                "close_below_failure_low", f"{trigger}T16:00:00+08:00", 10.45, wave_start["start_id"])


def test_retained_snapshot_must_cover_the_start_time_even_on_the_same_day(tmp_path, monkeypatch):
    inputs = wave_inputs([(16., 12., 15.5, 100.)] * 3)
    day = inputs.trade_date
    store = evening_report_store.EveningReportStore(tmp_path)
    versions = {"files": [*inputs.provenance["input_versions"]["files"], ["adj_factor/all.parquet", 80, 10]]}
    store.save_readiness(day, current_receipt(day, day + "T16:00:00+08:00", versions=versions, wave_factor_facts={}))
    event = event_for(inputs.calendar)  # First known at 16:01.
    monkeypatch.setattr(evening_report_store, "cn_now", lambda: datetime.fromisoformat(inputs.cutoff))
    store.save(day, {"trade_date": day, "cutoff": inputs.cutoff, "audit": {}, "wave_starts": [event]}, {})
    inputs.wave_factors = None
    result, = run_study(store, inputs, evaluated_at=inputs.cutoff)["results"]
    assert (result["evaluation_reason_code"], result["is_final"]) == ("price_basis_unavailable", False)


def test_published_queue_includes_nonselected_event(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import polars as pl

    days = calendar(15)
    day = days[2]
    monkeypatch.setattr(evening_report_store, "cn_now", lambda: datetime.fromisoformat(f"{day}T17:00:00+08:00"))
    event = event_for(days)
    store = evening_report_store.EveningReportStore(tmp_path)
    original = store.save(day, {"trade_date": day, "cutoff": event["known_at"], "selected": [],
                                "wave_starts": [event], "audit": {}, "records": [],
                                "wave_rule": {"version": VERSION}, "provenance": {"calendar": {"days": days}},
                                "market": {"classification": {"sentiment_state": "mixed"}}}, {})
    source = SimpleNamespace(trade_date=days[-1], cutoff=f"{days[-1]}T17:00:00+08:00", calendar=days,
                             provenance={}, wave_factors=wave_inputs(BARS).wave_factors, prices=pl.DataFrame({
                                 "symbol": [SYMBOL] * 2, "date": [day, days[-1]], "wave_formal": [True] * 2,
                                 "raw_close": [14.] * 2, "raw_low": [13.] * 2}))
    study = run_study(store, source, evaluated_at=source.cutoff, sessions=12)
    assert study["statistics"]["total"] == 1
    assert study["results"][0]["report_version"] == original["version"]
    assert study["results"][0]["start_id"] == event["start_id"]
    result = study["results"][0]
    assert result["extended_window_quote_coverage"] == {"accepted": 1, "missing": 1}
    evidence, known = (result["context_refs"][when][0] for when in ("evidence", "known"))
    assert evidence["current_reference_status"] == "missing" and "values" not in evidence
    assert known["current_reference_status"] == "valid" and known["source_report_id"] == original["version"]
    assert known["checked_at"] == source.cutoff
    groups = [g for g in study["statistics"]["context_groups"] if g["field"] == "sentiment_state"]
    assert [(g["as_of_event"], g["value"], g["reference_status"], g["samples"]) for g in groups] == [
        ("evidence", None, "missing", 1), ("known", "mixed", "valid", 1)]
    assert store.get(day)["wave_starts"] == original["wave_starts"]
    assert "checked_at" not in original["wave_starts"][0]["context_refs"]["known"][0]


def test_observation_baseline_keeps_dated_constraints_and_one_shared_manifest(tmp_path, monkeypatch):
    inputs = wave_inputs(BARS[:2])
    day, target = inputs.calendar
    cutoff = f"{day}T17:00:00+08:00"
    monkeypatch.setattr(evening_report_store, "cn_now", lambda: datetime.fromisoformat(cutoff))
    store = evening_report_store.EveningReportStore(tmp_path)
    observation = {"role": "emotion", "variables": {"return_5d": {"value": .1}},
                   "decision": {"comparison_eligible": True, "final_theme": "A", "competed_quota_type": "emotion",
                                  "all_reason_codes": [], "steps": [], "comparison_group": "selected",
                                  "rank_within_theme_role": 1, "rank_global_role": 1}}
    store.save(day, {"trade_date": day, "cutoff": cutoff, "selection_policy_version": "theme-observation-v2",
                     "audit": {}, "selected": [SYMBOL], "records": [{"symbol": SYMBOL, "selected": True, "observation": observation}],
                     "sectors": []}, {})
    inputs.benchmark = inputs.prices.select("date", "formal", "close")
    inputs.prices = inputs.prices.with_columns(
        pl.Series("raw_high", [10.4, 10.55]), pl.Series("raw_low", [10., 10.55]),
        pl.Series("signal_limit_up", [None, True]), pl.Series("signal_limit_down", [None, False]),
        pl.Series("is_st", [None, False]),
    )
    inputs.provenance["input_id"] = "b" * 32
    result = run_study(store, inputs, evaluated_at=inputs.cutoff)
    baseline = result["observation_baseline"] = compare_observations(store, inputs)
    [case] = baseline["comparison"]
    [item] = case["observations"]
    outcome = item["performance"]["1"]
    assert outcome["return"] == pytest.approx(10.55 / 10.30 - 1)
    assert outcome["trading_constraints"] == ["目标日涨停", "目标日一字价格"]
    assert outcome["constraint_coverage"][target]["limit_down_status"] == "known"
    assert outcome["constraint_coverage"][day]["limit_up_status"] == "unknown"
    assert outcome["data_version"]["input_id"] == inputs.provenance["input_id"]
    assert outcome["constraint_coverage"][target]["source_version"] == outcome["data_version"]
    assert result["data_version"] == inputs.provenance["input_versions"]
    first_partition = result["data_version"]["files"][0][0]
    assert json.dumps(result).count(first_partition) == 1


def test_context_changes_projection_without_changing_archive_evidence():
    report = {"version": "v1", "trade_date": "2026-11-18", "cutoff": "2026-11-18T17:00:00+08:00",
              "market": {"classification": {"sentiment_state": "mixed"}}}
    ref = {"version": "v1", "as_of": report["trade_date"], "source_snapshot_id": "market",
           "available_by": report["cutoff"], "retention_until": "2026-11-19T00:00:00+08:00",
           "reference_status": "valid"}
    assert reference_status(ref, report, report["cutoff"])[0] == "valid"
    assert reference_status(ref, report | {"version": "v2"}, report["cutoff"])[0] == "missing"
    assert reference_status(ref, None, ref["retention_until"])[0] == "expired"
    assert ref["reference_status"] == "valid"


@pytest.mark.parametrize('has_evidence_observation', [True, False])
def test_start_context_uses_each_source_dates_archived_theme(has_evidence_observation):
    evidence_day, known_day = calendar(2)

    def report(day, theme, score):
        cutoff = day + 'T17:00:00+08:00'
        return {'trade_date': day, 'version': day, 'cutoff': cutoff, 'generated_at': cutoff,
                'market': {'classification': {'sentiment_state': 'mixed'}},
                'audit': {'themes': [{'id': item, 'selected_theme': True} for item in ('concept:A', 'concept:B')]},
                'records': [{'symbol': SYMBOL, 'observation': {'primary_theme': {'id': theme},
                                                               'variables': {'relative_lead_score': {'value': score}}}}]}

    evidence = report(evidence_day, 'concept:A', 90)
    if not has_evidence_observation:
        evidence['records'] = []
    current = report(known_day, 'concept:B', 65)
    current['storage_version'] = SCHEMA_VERSION
    current['calculations'] = current.pop('records')
    current['calculations'][0]['selected'] = False
    current['calculations'][0]['observation']['decision'] = {'comparison_eligible': True}
    current['provenance'] = {'calendar': {'days': [evidence_day, known_day]}}
    current['wave_starts'] = [{'security_id': SYMBOL, 'known_at': current['cutoff'], 'evidence_date': evidence_day}]
    add_context_refs(current, SimpleNamespace(referenced_report=lambda day: evidence if day == evidence_day else None))
    refs = {key: {ref['source_snapshot_id']: ref['reference_status'] for ref in values}
            for key, values in current['wave_starts'][0]['context_refs'].items()}
    score = f'observation/{SYMBOL}/relative_lead_score'
    assert refs['known'] == {'market': 'valid', 'audit/themes/concept:B': 'valid', score: 'valid'}
    expected = ({'audit/themes/concept:A': 'valid', score: 'valid'} if has_evidence_observation
                else {f'observation/{SYMBOL}/primary_theme': 'missing'})
    assert refs['evidence'] == {'market': 'valid', **expected}

    from app.services.evening_report_wave_context import study_context

    later = known_day + 'T19:00:00+08:00'
    original = current['wave_starts'][0]['context_refs']
    original['known'][0]['retention_until'] = later
    checked = study_context(original, lambda day, version: evidence if day == evidence_day else current, later)
    assert checked['evidence'][0]['values']['sentiment_state'] == 'mixed'
    assert checked['known'][0]['current_reference_status'] == 'expired' and 'values' not in checked['known'][0]
    assert checked['known'][0]['checked_at'] == later and original['known'][0]['reference_status'] == 'valid'


@pytest.mark.parametrize(("close", "expected"), [(16., "success_at_start"), (14., "calendar_unconfirmed")])
def test_study_preserves_known_window_across_disjoint_calendar(tmp_path, monkeypatch, close, expected):
    from app.services.evening_report_start_study import archive_study_market

    inputs = wave_inputs(BARS)
    days = inputs.calendar
    k = days[2]
    inputs.trade_date = k
    inputs.cutoff = f"{k}T17:00:00+08:00"
    inputs.calendar = days[:3]
    inputs.provenance["calendar"] = {"days": inputs.calendar}
    inputs.study_price_receipt = None
    inputs.prices = inputs.prices.with_columns(pl.lit(close).alias("raw_close"), pl.lit(13.).alias("raw_low"))
    store = evening_report_store.EveningReportStore(tmp_path)
    monkeypatch.setattr(evening_report_store, "cn_now", lambda: datetime.fromisoformat(inputs.cutoff))
    first = {"trade_date": k, "cutoff": inputs.cutoff, "wave_starts": [event_for(days)], "state": {},
             "audit": {}, "records": []}
    archive_study_market(store, inputs, first, [])
    store.save(k, first, {})
    inputs.calendar = days[7:]
    inputs.provenance["calendar"] = {"days": inputs.calendar}
    inputs.trade_date = inputs.calendar[-1]
    inputs.cutoff = f"{inputs.trade_date}T17:00:00+08:00"
    later = {"trade_date": inputs.trade_date, "wave_starts": [], "state": {}}
    archive_study_market(store, inputs, later, [evening_report_store.history_view(first)])
    assert not later.get("study_quotes")
    result, = run_study(store, inputs, evaluated_at=inputs.cutoff)["results"]
    assert (result["start_date"], result["target_date"], result["evaluation_reason_code"]) == (k, None, expected)
    inputs.calendar = days
    inputs.provenance["calendar"] = {"days": days}
    resumed = {"trade_date": inputs.trade_date, "wave_starts": [], "state": {}}
    archive_study_market(store, inputs, resumed, [evening_report_store.history_view(later)])
    assert list(resumed["study_quotes"]) == days[3:13]


def test_study_archives_only_open_session_windows(tmp_path):
    from app.services.evening_report_start_study import archive_study_market

    inputs = wave_inputs([BARS[0]] * 17)
    inputs.study_price_receipt = None
    store = evening_report_store.EveningReportStore(tmp_path)
    days = inputs.calendar
    history = []
    for i, events, expected in [(2, [event_for(days)], days[2:3]), (14, [], days[3:13]),
                                (15, [], []), (16, [event_for(days[14:])], days[16:])]:
        report = {"trade_date": days[i], "wave_starts": events, "state": {}}
        inputs.trade_date = days[i]
        inputs.calendar = days[:i + 1]
        archive_study_market(store, inputs, report, history)
        assert list(report.get("study_quotes", {})) == expected
        assert bool(report["state"].get("study_windows")) == (i in (2, 16))
        history.append(report)
