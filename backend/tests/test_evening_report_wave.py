from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest
from evening_receipt_fixture import current_receipt

from app.services.evening_report_dip import QUALITY_FIELDS, dip_labels
from app.services.evening_report_wave import PriceBasis, WaveRun, build_waves

SYMBOL = "000001.SZ"
BARS = [
    (10.4, 10.0, 10.30, 100), (10.6, 10.15, 10.55, 100), (10.5, 9.9, 10.07, 100),
    (10.0, 9.8, 9.90, 100), (10.3, 9.84, 10.29, 100),
    (11.3, 10.4, 11.20, 100), (12.0, 11.1, 11.95, 100),
    (11.8, 10.9, 11.40, 65), (11.4, 11.1, 11.20, 65), (11.2, 10.7, 10.90, 65),
    (11.4, 10.9, 11.30, 60), (11.6, 11.2, 11.50, 60), (13.0, 11.4, 12.20, 100),
]


def calendar(count):
    days = []
    day = date(2026, 10, 12)
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def prices(days, bars):
    return {
        day: {"raw_high": high, "raw_low": low, "raw_close": close, "raw_open": low,
              "close": close, "wave_formal": True, "volume": volume, "amount": 750_000_000,
              "ma10": 11.7, "known_at": f"{day}T17:00:00+08:00"}
        for day, (high, low, close, volume) in zip(days, bars, strict=True)
    }


def run_wave(bars, *, state=None, halted=(), exclude=(), dates=None, basis=None):
    days = dates or calendar(len(bars))
    rows = {day: row for day, row in prices(days, bars).items() if day not in {*halted, *exclude}}
    return WaveRun(SYMBOL, days, rows, set(halted), basis or PriceBasis([], "accepted", days[0]), "", state)


def wave_inputs(bars, *, late=()):
    days = calendar(len(bars))
    rows = prices(days, bars)
    cutoff = f"{days[-1]}T17:00:00+08:00"
    for index in late:
        rows[days[index]]["known_at"] = cutoff
    frame = pl.DataFrame([
        {**row, "symbol": SYMBOL, "date": day, "formal": True,
         "wave_volume": row["volume"], "wave_amount": row["amount"]}
        for day, row in rows.items()
    ])
    factors = pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.String, "ex_factor": pl.Float64})
    files = [[f"kline_daily/date={day}/part.parquet", 100, 1] for day in days]
    return SimpleNamespace(prices=frame, calendar=days, cutoff=cutoff, trade_date=days[-1], wave_factors=factors,
                           wave_first_acceptances={SYMBOL: rows[days[0]]["known_at"]},
                           provenance={"input_versions": {"files": files}, "wave_factor_source": ["accepted"]})


def calculate_waves(inputs, previous=None):
    state, snapshots, events, starts, _ = build_waves(inputs, [{"symbol": SYMBOL}], previous or {}, inputs.calendar, lambda: None)
    return state, snapshots, events, starts


def test_confirmed_attribution_is_the_same_in_incremental_and_full_runs():
    first, _, _, _ = calculate_waves(wave_inputs(BARS[:8]))
    current = wave_inputs(BARS)
    _, incremental, _, _ = calculate_waves(current, first)
    _, rebuilt, _, _ = calculate_waves(current)
    known = current.prices["known_at"][7]
    assert incremental[SYMBOL]["first_wave_high"]["known_at"] == known
    assert rebuilt[SYMBOL]["first_wave_high"]["known_at"] == known


@pytest.mark.parametrize("late", [False, True])
def test_start_across_missing_report_is_published_once_with_actual_known_date(tmp_path, monkeypatch, late):
    from datetime import datetime

    from app.services import evening_report_store
    from app.services.evening_report_start_study import run_study

    first, _, _, _ = calculate_waves(wave_inputs(BARS[:12]))
    current = wave_inputs([*BARS, (12.3, 11.7, 12.0, 60)], late=[12] if late else [])
    state, snapshots, events, starts = calculate_waves(current, first)
    assert "closed_by_start" in snapshots[SYMBOL]["window_close_reason_codes"]
    [event] = starts
    assert event["date"] == current.calendar[-2]
    known_day = current.trade_date if late else current.calendar[-2]
    assert event["known_at"] == f"{known_day}T17:00:00+08:00"
    monkeypatch.setattr(evening_report_store, "cn_now", lambda: datetime.fromisoformat(current.cutoff))
    store = evening_report_store.EveningReportStore(tmp_path)
    store.save(current.trade_date, {"trade_date": current.trade_date, "cutoff": current.cutoff,
                                    "audit": {}, "wave_starts": starts, "wave_events": events}, {})
    research = run_study(store, current, evaluated_at=current.cutoff)
    assert research["statistics"]["total"] == 1
    assert research["results"][0]["start_date"] == known_day

    following = wave_inputs([*BARS, (12.3, 11.7, 12.0, 60), (12.4, 11.8, 12.1, 100)])
    assert calculate_waves(following, state)[3] == []


def test_stock_resumes_its_published_wave_after_a_report_without_coverage():
    state, old, _, _ = calculate_waves(wave_inputs(BARS[:12]))
    current = wave_inputs(BARS)
    # One report can cover only other securities before this stock returns.
    absent = wave_inputs(BARS[:12])
    absent.trade_date = absent.calendar[-1]
    state, snapshots, _, _, _ = build_waves(absent, [], state, absent.calendar, lambda: None)
    assert snapshots == {} and state["stocks"][SYMBOL]["as_of"] == absent.trade_date
    _, continued, _, _ = calculate_waves(current, state)
    assert old[SYMBOL]["first_wave_high"]["raw_price"] == 12
    assert continued[SYMBOL]["first_wave_high"] == old[SYMBOL]["first_wave_high"]
    assert continued[SYMBOL]["current_wave_index"] == 2


def test_new_ex_date_dates_the_breakout_window_close_and_structural_high():
    bars = [*BARS[:12], (12.25, 11.4, 11.55, 40), (11.8, 11.3, 11.65, 40)]
    prior, _, _, _ = calculate_waves(wave_inputs(bars[:12]))
    sample = wave_inputs(bars)
    breakout_day = sample.calendar[-2]
    assert calculate_waves(sample, prior)[1][SYMBOL]["second_wave_confirmed_by_new_high"] is None
    sample.wave_factors = pl.DataFrame({"symbol": [SYMBOL], "trade_date": [breakout_day], "ex_factor": [1.1]})
    _, snapshots, events, starts = calculate_waves(sample, prior)
    wave = snapshots[SYMBOL]
    assert starts == []
    assert wave["current_wave_index"] == wave["completed_wave_count"] == 2
    assert wave["window_close_reason_codes"] == ["closed_by_new_high"]
    high = next(event for event in events if event["type"] == "wave_high_confirmed")
    for event in (wave["second_wave_confirmed_by_new_high"], wave["window_close"], high):
        assert (event["date"], event["known_at"]) == (breakout_day, sample.cutoff)
    assert wave["confirmed_pivots"][-1]["known_at"] == sample.cutoff


def test_initial_gap_rewarms_from_new_swings():
    # A low was already confirmed before the hole. It cannot become the start of the new impulse.
    bars = [*BARS[:6], *BARS]
    gap_day = calendar(len(bars))[5]

    def sample(count):
        inputs = wave_inputs(bars[:count])
        inputs.prices = inputs.prices.filter(pl.col("date") != gap_day)
        inputs.provenance["wave_factor_known_at"] = f"{inputs.calendar[0]}T17:00:00+08:00"
        return inputs

    state = None
    for count in range(6, len(bars) + 1):
        inputs = sample(count)
        state, snapshots, _, _ = calculate_waves(inputs, state)
        result = snapshots[SYMBOL]
        rebuilt = calculate_waves(inputs)[1][SYMBOL]
        for key in ("data_status", "missing_from", "round_id", "confirmed_pivots", "current_wave_index"):
            assert result[key] == rebuilt[key]
        if count < 14:
            assert result["data_status"] == ("data_gap" if count == 6 else "rebuilding")
            assert result["missing_from"] == gap_day
            assert result["current_wave_index"] is None
        elif count == 14:
            assert result["data_status"] == "known" and result["missing_from"] is None
            assert result["first_wave_start"]["extreme_date"] == inputs.calendar[9]
            assert result["current_wave_index"] == 1
    assert result["current_wave_index"] == result["completed_wave_count"] == 2
    assert result["pivot_density"]["value"] is None


@pytest.fixture
def wave_publication(tmp_path, monkeypatch):
    from datetime import datetime

    from test_evening_report_observation import observation_inputs

    from app.services import evening_report_jobs, evening_report_store

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    store = evening_report_store.EveningReportStore(tmp_path)

    def publish(sample, evidence=None):
        inputs = observation_inputs()
        template = inputs.prices.head(1)
        inputs.prices = sample.prices.join(template.drop(
            [column for column in sample.prices.columns if column in template.columns]), how="cross")
        inputs.metadata = inputs.metadata.head(1).with_columns(pl.lit(SYMBOL).alias("symbol"))
        inputs.memberships = inputs.memberships.head(1).with_columns(pl.lit(SYMBOL).alias("symbol"))
        inputs.benchmark = pl.DataFrame({"date": sample.calendar, "close": 100., "formal": True})
        inputs.trade_date, inputs.cutoff, inputs.calendar = sample.trade_date, sample.cutoff, sample.calendar
        inputs.wave_factors, inputs.provenance = sample.wave_factors, sample.provenance
        inputs.wave_first_acceptances = sample.wave_first_acceptances
        now = datetime.fromisoformat(sample.cutoff)
        for module in (evening_report_jobs, evening_report_store):
            monkeypatch.setattr(module, "cn_now", lambda: now)
        monkeypatch.setattr(evening_report_jobs, "load_inputs", lambda *_args: inputs)
        monkeypatch.setattr(evening_report_jobs, "require_ready", lambda *_args, **_kwargs: evidence or current_receipt(
            sample.trade_date, sample.cutoff, input_id="a" * 32, versions=sample.provenance["input_versions"],
            active_symbols=[SYMBOL], wave_first_acceptances=sample.wave_first_acceptances,
            wave_factor_facts=sample.provenance.get("wave_factor_facts", {})))
        evening_report_jobs.generate(repo, now.date(), progress=lambda *_args, **_kwargs: None)
        return store.get(sample.trade_date)

    return store, publish


def archived_cycle(store, day):
    return store.state(day)["state"]["wave"]["stocks"][SYMBOL]["state"]["round"]


@pytest.mark.parametrize("arrives_late", [False, True])
def test_start_uses_the_acceptance_date_of_the_factor_that_enables_it(wave_publication, arrives_late):
    from app.enriched_generation import bump_enriched_generation
    from app.services.evening_report_inputs import input_versions, wave_factor_inputs
    from app.services.evening_report_start_study import run_study

    store, publish = wave_publication
    bump_enriched_generation(store.root.parent)
    bars = [*BARS[:12], (12.1, 11.31, 11.35, 100)]
    first = wave_inputs(bars)
    second = wave_inputs([*bars, (12.15, 11.55, 11.6, 60)])
    path = store.root.parent / "adj_factor" / "all.parquet"
    path.parent.mkdir()
    existing = pl.DataFrame({"symbol": ["000002.SZ"], "trade_date": [first.trade_date], "ex_factor": [1.05]})
    amended = pl.concat([existing, pl.DataFrame({"symbol": [SYMBOL], "trade_date": [first.trade_date], "ex_factor": [1.01]})])

    def publish_with_factors(sample):
        day = date.fromisoformat(sample.trade_date)
        versions = input_versions(store.root.parent, day)
        sample.wave_factors, source = wave_factor_inputs(store.root.parent, day, versions)
        sample.provenance["input_versions"]["files"].append(source)
        sample.provenance["wave_factor_source"] = source
        return publish(sample)

    (existing if arrives_late else amended).write_parquet(path)
    original = publish_with_factors(first)
    if arrives_late:
        amended.write_parquet(path)
    report = publish_with_factors(second)
    if arrives_late:
        assert not original["wave_starts"] and not report["wave_starts"]
        return
    start_day = first
    start_report = original
    assert [len(item["wave_starts"]) for item in (original, report)] == [1, 0]
    [start] = start_report["wave_starts"]
    assert start["price_branches"] == ["platform"]
    assert start["evidence_date"] == first.trade_date
    assert start["known_at"] == start_day.cutoff
    [result] = run_study(store, second, evaluated_at=second.cutoff)["results"]
    assert result["start_date"] == start_day.trade_date
    assert result["report_version"] == start_report["version"]


@pytest.mark.parametrize("changed_symbol,cancelled_retry,missed_factor", [
    ("000002.SZ", False, 1.01), (SYMBOL, False, 1.01), (SYMBOL, False, 1.001),
    ("000002.SZ", True, 1.01), (None, True, None),
])
def test_market_acceptance_survives_missing_report(wave_publication, monkeypatch, changed_symbol, cancelled_retry,
                                                  missed_factor):
    from datetime import datetime

    from app.enriched_generation import bump_enriched_generation
    from app.services import evening_report_inputs, evening_report_store, preferences
    from app.services.daily_coverage import DailyCoverageError
    from app.services.evening_report_price_facts import add_accepted_price_times
    from app.services.evening_report_start_study import run_study

    store, publish = wave_publication
    root = store.root.parent
    bars = [*BARS[:12], (12.1, 11.31, 11.35, 100)]
    evidence_day = calendar(len(bars))[-1]
    next_bar = (12.15, 11.55, 11.60, 60)
    if changed_symbol is None:
        bars = BARS.copy()
        next_bar = (12.3, 11.7, 12.0, 60)
    elif cancelled_retry:
        bars.append(next_bar)
        next_bar = (12.19, 11.57, 11.62, 60)
    baseline, missed, current = wave_inputs(BARS[:12]), wave_inputs(bars), wave_inputs([*bars, next_bar])
    bump_enriched_generation(root)
    evening_report_store.atomic_json(root / "calendar" / "trading_days.json",
                                     {"days": current.calendar, "start": current.calendar[0], "end": current.trade_date})
    instruments = root / "instruments" / "instruments.parquet"
    instruments.parent.mkdir()
    pl.DataFrame([{"symbol": SYMBOL, "type": "stock", "security_type": "a_share", "board": "main",
                   "is_current": True, "is_st": False}]).write_parquet(instruments)
    for day in current.calendar:
        path = root / "kline_daily" / f"date={day}" / "part.parquet"
        path.parent.mkdir(parents=True)
        current.prices.filter(pl.col("date") == day).select(
            "symbol", pl.col("date").str.to_date(), "volume", "amount",
            *[pl.col("raw_" + field).alias(field) for field in ("open", "high", "low", "close")],
        ).write_parquet(path)
    coverage = SimpleNamespace(active={SYMBOL}, halted=set(), halted_evidence={})
    monkeypatch.setattr(preferences, "load", lambda: {})

    def ready(sample):
        now = datetime.fromisoformat(sample.cutoff)
        monkeypatch.setattr(evening_report_inputs, "cn_now", lambda: now)
        observations = [dict(id=source, source=source, complete=True, symbols=[SYMBOL],
                             obtained_at=sample.cutoff, memberships=[]) for source in ("concept", "industry")]
        evening_report_inputs.save_ready(root, now.date(), coverage, observations, {})
        return evening_report_inputs.require_ready(root, now.date())

    def publish_ready(sample, evidence):
        day = date.fromisoformat(sample.trade_date)
        sample.wave_factors, source = evening_report_inputs.wave_factor_inputs(root, day, evidence["versions"])
        sample.prices = add_accepted_price_times(sample.prices, evidence)
        sample.wave_first_acceptances = evidence["wave_first_acceptances"]
        sample.provenance = {"input_versions": evidence["versions"], "wave_factor_source": source,
                             "wave_factor_known_at": evidence["wave_factor_known_at"],
                             "wave_factor_facts": evidence.get("wave_factor_facts", {})}
        return publish(sample, evidence)

    path = root / "adj_factor" / "all.parquet"
    path.parent.mkdir()
    other = pl.DataFrame({"symbol": ["000002.SZ"], "trade_date": [baseline.trade_date], "ex_factor": [1.05]})
    other.write_parquet(path)
    assert publish_ready(baseline, ready(baseline))["wave_starts"] == []
    if changed_symbol is not None:
        factors = pl.concat([other, pl.DataFrame({"symbol": [SYMBOL], "trade_date": [evidence_day],
                                                 "ex_factor": [missed_factor]})])
        factors.write_parquet(path)
    accepted = ready(missed)
    if cancelled_retry:
        store.save_readiness(missed.trade_date, {"date": missed.trade_date, "complete": False,
                                                "reason": "新一轮盘后输入正在验收"})
        assert store.input_evidence(accepted["input_id"]) == accepted
        with pytest.raises(DailyCoverageError, match="本轮验收"):
            evening_report_inputs.require_ready(root, date.fromisoformat(missed.trade_date))
    assert store.get(missed.trade_date) is None
    if changed_symbol is not None:
        factors.with_columns(pl.when(pl.col("symbol") == changed_symbol).then(pl.lit(1.04))
                             .otherwise(pl.col("ex_factor")).alias("ex_factor")).write_parquet(path)
    current_acceptance = ready(current)
    report = publish_ready(current, current_acceptance)
    [start] = report["wave_starts"]
    # 11.35 already beats the 11.40 platform at 1.01, but only the later 1.04 overcomes it after 1.001.
    expected = current if missed_factor == 1.001 else missed
    assert start["evidence_date"] == evidence_day
    assert start["known_at"] == expected.cutoff
    original_factors = evening_report_inputs.factor_file_reference(
        (current_acceptance if expected is current else accepted)["versions"])
    for field in ("evidence_close", "evidence_low"):
        assert start[field]["factor_source"] == original_factors
        assert start[field]["factor_known_at"] <= start["known_at"]
    [result] = run_study(store, current, evaluated_at=current.cutoff)["results"]
    assert result["start_date"] == expected.trade_date
    expected_close = 11.60 if expected is current or cancelled_retry else 11.35
    assert result["start_close"]["raw_price"] == (12.20 if changed_symbol is None else expected_close)


def test_expired_candidate_can_reconnect_to_the_same_round():
    bars = [*BARS[:8], *((11.8, 11.2, 11.3, 30),) * 22,
            (11.1, 10.8, 10.9, 10), (11.6, 10.9, 11.5, 10), (13.0, 11.5, 12.2, 100)]
    days = calendar(len(bars))
    paused = run_wave(bars[:-3])
    before = paused.advance(days[0])
    assert before["data_status"] == "rebuilding"
    resumed = run_wave(bars, state=paused.state).advance(days[0])
    assert resumed["round_id"] == before["round_id"]
    assert resumed["window_close_reason_codes"] == ["closed_by_stale"]
    assert resumed["first_pullback_low"]["extreme_date"] == days[-3]
    assert resumed["current_wave_index"] == resumed["completed_wave_count"] == 2


def test_expired_candidate_accepts_the_same_low_above_the_prior_high_as_an_active_candidate():
    higher_low = [(12.55, 12.05, 12.55, 10), (12.80, 12.20, 12.70, 10)]
    baseline = run_wave([*BARS[:8], *higher_low])
    active = baseline.advance(baseline.days[0])
    bars = [*BARS[:8], *((11.8, 11.2, 11.3, 30),) * 22]
    paused = run_wave(bars)
    before = paused.advance(paused.days[0])
    assert before["data_status"] == "rebuilding"
    resumed = run_wave([*bars, *higher_low], state=paused.state)
    after = resumed.advance(resumed.days[0])
    assert after["round_id"] == before["round_id"]
    assert after["window_close"] == before["window_close"]
    assert after["current_wave_index"] == active["current_wave_index"] == 2
    assert after["first_pullback_low"]["raw_price"] == active["first_pullback_low"]["raw_price"] == 12.05
    assert after["completed_wave_count"] == active["completed_wave_count"] == 1


def test_wave_initial_decline_confirmed_low_qualification_and_same_day_high():
    days = calendar(len(BARS))
    expected = [
        (3, "warming_up", None, 0),
        (7, "warming_up", None, 0),
        (8, "known", 1, 1),
        (13, "known", 2, 2),
    ]
    for n, status, index, completed in expected:
        result = run_wave(BARS[:n]).advance(days[0])
        assert (result["data_status"], result["current_wave_index"], result["completed_wave_count"]) == (status, index, completed)
    low_day = run_wave(BARS[:5])
    low_day.advance(days[0])
    assert low_day.state["ordinary_low"]["extreme_date"] == days[3]
    assert low_day.state["ordinary_low"]["confirmed_at"] == days[4]
    result = run_wave(BARS[:10]).advance(days[0])
    assert result["pullback_phase_at_time"] == "pullback_ge_5pct"
    assert result["required_checks"]["volume"] is None
    assert result["provisional_retrace_pct"] is not None
    result = run_wave(BARS[:11]).advance(days[0])
    assert result["first_pullback_low"]["extreme_date"] == days[9]
    assert result["pullback_volume"] == {"mean": 65, "sessions": 3, "suspended": 0}
    assert result["required_checks"]["volume"] is True
    recovered = run_wave(BARS[:12]).advance(days[0])
    assert recovered["pullback_phase_at_time"] == "recovered_lt_5pct"
    assert recovered["first_pullback_eligible"]["reason_codes"] == ["pullback_below_min"]
    wave = run_wave(BARS)
    result = wave.advance(days[0])
    assert result["window_close_reason_codes"] == ["closed_by_start", "closed_by_new_high"]
    assert result["second_wave_start"]["date"] == result["confirmed_pivots"][-1]["confirmed_at"] == days[-1]
    assert result["second_wave_start"]["target_high"]["price"] == 12
    assert result["provisional_extreme"] is None


def test_restart_and_full_replay_match_and_future_does_not_change_prior_snapshot():
    days = calendar(len(BARS))
    resume = None
    for end in range(1, len(BARS) + 1):
        fresh = run_wave(BARS[:end])
        incremental = run_wave(BARS[:end], state=resume)
        assert incremental.advance(days[0]) == fresh.advance(days[0])
        resume = incremental.state


def test_volume_missing_does_not_stop_warmup_and_price_gap_requires_recovery():
    days = calendar(len(BARS))
    changed = [(*row[:3], None) for row in BARS[:8]]
    warm = run_wave(changed).advance(days[0])
    assert warm["data_status"] == "known"
    assert warm["current_wave_index"] == 1
    unavailable = run_wave(BARS[:11], exclude=[days[9]])
    snapshot = unavailable.advance(days[0])
    assert snapshot["data_status"] == "rebuilding"
    assert snapshot["current_wave_index"] is None and snapshot["completed_wave_count"] == 1
    assert snapshot["first_pullback_eligible"]["reason_codes"] == ["missing_rows"]
    restored = run_wave(BARS[:12], state=unavailable.state).advance(days[0])
    assert restored["completed_wave_count"] == 1
    assert restored["first_pullback_low"]["extreme_date"] == days[9]


def test_reversal_stays_relative_to_extreme_during_suspension_and_expiry_does_not_reset_count():
    days = calendar(33)
    bars = [*BARS[:8], *((11.8, 11.2, 11.3, 30),) * 24]
    wave = run_wave(bars[:29], halted=[days[10]])
    result = wave.advance(days[0])
    assert result["window_close_reason_codes"] == []
    wave = run_wave(bars[:32], halted=[days[10]], state=wave.state)
    result = wave.advance(days[0])
    assert result["window_close_reason_codes"] == ["closed_by_stale"]
    assert result["current_wave_index"] is None and result["completed_wave_count"] == 1

    resume_bars = [(20, 19, 19.9, 10), (19.8, 19., 19.2, 10), (19.3, 18., 18.8, 10), (19.1, 18.2, 18.9, 10)]
    wave = run_wave(resume_bars, halted=[days[2]])
    wave.advance(days[0])
    assert wave.state["saw_decline"] and wave.state["direction"] == "L"


def dip_inputs():
    day = "2026-11-18"
    cutoff = f"{day}T17:00:00+08:00"
    values = {"space_score": 60, "identity_score": 70, "high_proximity": .9, "return_20d": .5,
              "amount_day": 500_000_000, "return_3d": .1, "volume_ratio_2_10": .55,
              "distance_ma10": .06, "amplitude_ratio_2_10": 1.2, "rising_lows_3d": True,
              "distance_bull_bear": -.05, "return_10d": .30, "return_5d": .03}
    wave = {"first_pullback_eligible": {"value": True, "reason_codes": []}, "first_wave_pullback": .06,
            "first_pullback_active": True, "required_checks": dict.fromkeys(QUALITY_FIELDS, True)}
    events = {"e1": {"applicable": True, "valid_from": day, "valid_until": day, "known_at": cutoff}}
    market = {"phase": "rally", "classification": {"sentiment_state": "hot_fragile"}, "ge2_count": 2}
    return values, wave, events, market, day, cutoff


@pytest.mark.parametrize(("distance", "status", "reason"), [
    (.20, "none", "path_not_met"), (.06, "partial", "gate_passed_path_partial"),
    (-.02, "partial", "gate_passed_path_partial"), (None, "none", "required_input_unknown"),
])
def test_e1_interval_is_one_check_and_missing_event_does_not_vote(distance, status, reason):
    values, wave, events, market, day, cutoff = dip_inputs()
    values.update(return_3d=.20, volume_ratio_2_10=.9, distance_ma10=distance, amplitude_ratio_2_10=1.4)
    events["e2"] = {**events["e1"], "applicable": False}
    result = dip_labels(values, {}, market, "10%", wave=wave, events=events, day=day, cutoff=cutoff)
    assert len(result["e1"]["checks"]) == 4
    assert result["status"] == status and result["primary_reason_code"] == reason


def test_full_then_closed_and_wide_amount_branch_is_independent():
    values, wave, events, market, day, cutoff = dip_inputs()
    result = dip_labels(values, {}, market, "10%", wave=wave, events=events, day=day, cutoff=cutoff)
    assert result["status"] == "full"
    assert result["primary_reason_code"] == "all_gates_and_path_passed"
    assert result["risk_hot_fragile"]
    wave["first_pullback_eligible"] = {"value": False, "reason_codes": ["first_wave_window_closed"]}
    wave["first_pullback_active"] = False
    closed = dip_labels(values, {}, market, "10%", wave=wave, events=events, day=day, cutoff=cutoff)
    assert closed["status"] == "none" and closed["primary_reason_code"] == "first_wave_window_closed"
    assert closed["gates"]["inputs"]["amount"] is True

    values.update(space_score=3, identity_score=20, amount_day=200_000_000)
    wave["first_pullback_eligible"] = {"value": True, "reason_codes": []}
    wave["first_pullback_active"] = True
    limited = dip_labels(values, {}, market, "10%", wave=wave, events=events, day=day, cutoff=cutoff)
    assert [(item["applicable"], item["passed"]) for item in limited["branches"]] == [(True, True), (True, False)]
    assert limited["gates"]["inputs"]["identity"] is True
    assert limited["primary_reason_code"] == "amount_below_threshold"


def test_later_segment_uses_its_own_high_and_low_after_weak_second_wave():
    bars = [*BARS[:12], (*BARS[12][:3], 50),
            (12., 11.7, 11.8, 65), (11.9, 11.6, 11.7, 65), (12.5, 11.8, 12.3, 135),
            (13.2, 12.3, 13.1, 100), (13.6, 12.75, 12.8, 100)]
    days = calendar(len(bars))
    before = run_wave(bars[:16])
    at_start = before.advance(days[0])
    assert at_start["second_wave_started"] is False
    assert at_start["current_wave_index"] == at_start["completed_wave_count"] == 2
    assert at_start["window_close_reason_codes"] == ["closed_by_new_high"]
    [third] = before.starts
    assert third["segment_index"] == 3 and third["target_high"]["price"] == 13
    assert third["failure_low"]["price"] == pytest.approx(11.6)
    final = run_wave(bars, state=before.state).advance(days[0])
    assert final["current_wave_index"] == final["completed_wave_count"] == 3
    assert final["window_close_reason_codes"] == ["closed_by_new_high"]


def test_three_percent_has_own_reason_and_does_not_hide_independent_missing_support():
    values, wave, events, market, day, cutoff = dip_inputs()
    wave["first_pullback_eligible"] = {"value": False, "reason_codes": ["pullback_below_min"]}
    wave["required_checks"]["support"] = None
    wave["first_wave_pullback"] = .03
    result = dip_labels(values, {}, market, "10%", wave=wave, events=events, day=day, cutoff=cutoff)
    assert result["status"] == "none" and result["primary_reason_code"] == "pullback_below_min"
    assert "support_missing" in result["reason_codes"]
    prohibited = dip_labels(values, {}, market | {"phase": "ebb"}, "10%", wave=wave, events=events, day=day, cutoff=cutoff)
    assert prohibited["primary_reason_code"] == "phase_ebb"
    assert "pullback_below_min" in prohibited["reason_codes"]


def test_real_archive_persists_wave_start_for_candidate_and_reuses_same_final(tmp_path):
    import polars as pl
    from test_evening_report_observation import observation_inputs

    from app.services.evening_report_engine import build_report
    from app.services.evening_report_store import EveningReportStore

    inputs = observation_inputs()
    symbol = inputs.metadata["symbol"][0]
    days = inputs.calendar
    by_date = dict(zip(days[-len(BARS):], BARS, strict=True))
    rows = []
    for row in inputs.prices.iter_rows(named=True):
        day = row["date"]
        if row["symbol"] == symbol:
            high, low, close, volume = by_date.get(day, (10.2, 10., 10.1, 100))
            row.update(raw_high=high, raw_low=low, raw_open=low, raw_close=close,
                       wave_formal=True, wave_volume=volume, wave_amount=500_000_000)
        else:
            row.update(raw_open=row["raw_close"], wave_formal=True, wave_volume=row["volume"], wave_amount=row["amount"])
        rows.append(row)
    inputs.prices = pl.DataFrame(rows)
    inputs.wave_factors = pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.String, "ex_factor": pl.Float64})
    inputs.cutoff = f"{days[-1]}T17:00:00+08:00"
    inputs.wave_first_acceptances = {symbol: f"{days[0]}T17:00:00+08:00"}
    inputs.provenance.update(wave_factor_source=["accepted"], calendar={"days": days})
    payload = build_report(inputs)
    assert payload["wave_starts"][0]["security_id"] == symbol
    store = EveningReportStore(tmp_path)
    first = store.save(days[-1], payload, {})
    assert store.save(days[-1], {"selected": []}, {})["version"] == first["version"]
    saved = next(row for row in first["records"] if row["symbol"] == symbol)
    assert saved["observation"]["variables"]["completed_wave_count"]["value"] == 2
    assert saved["observation"]["wave"]["window_close_reason_codes"] == ["closed_by_start", "closed_by_new_high"]
    assert first["wave_starts"][0]["start_id"] == payload["wave_starts"][0]["start_id"]
    cycle = store.state(days[-1])["state"]["wave"]["stocks"][symbol]["state"]["round"]
    assert cycle["completed"] == 2
    assert cycle["first_impulse_volume"]["mean"] > 0
    assert cycle["first_pullback_volume"]["mean"] == 65


@pytest.mark.parametrize("same_session", [True, False], ids=["same-bar", "start-on-following-bar"])
def test_later_breakout_evidence_does_not_postpone_a_window_already_closed_by_start(wave_publication, same_session):
    store, publish = wave_publication
    later = [(12.5, 11.40, 11.8, 140), (12.35, 11.5, 12.1, 60)] if same_session else [
        (12.05, 11.4, 11.8, 40), (12.12, 11.55, 11.95, 140), (12.3, 11.6, 11.95, 60),
    ]
    bars = [*BARS[:12], *later]
    publish(wave_inputs(bars[:12]))
    inputs = wave_inputs(bars)
    start_day = inputs.calendar[-2]
    breakout_day = inputs.calendar[12]
    inputs.wave_factors = pl.DataFrame({"symbol": [SYMBOL], "trade_date": [breakout_day], "ex_factor": [1.1]})
    report = publish(inputs)

    [start] = report["wave_starts"]
    breakout = next(e for e in report["wave_events"] if e["type"] == "wave_new_high")
    assert start["date"] == start_day and breakout["date"] == breakout_day
    assert start["known_at"] == f"{start_day}T17:00:00+08:00"
    assert breakout["known_at"] == inputs.cutoff
    assert archived_cycle(store, inputs.trade_date)["window_close"] == {
        "date": start_day, "known_at": start["known_at"], "reason_codes": ["closed_by_start"],
        "event_refs": [start["event_id"]],
    }


@pytest.mark.parametrize(("kind", "bars"), [
    ("candidate_expired", [*BARS[:8], *([(11.8, 11.2, 11.3, 30)] * 23)]),
    ("structure_invalid", [*BARS[:12], (10.5, 10., 10.1, 50), (10.7, 10.2, 10.5, 55)]),
])
def test_unrelated_factor_preserves_expiry_and_invalidation_clocks(kind, bars):
    original = wave_inputs(bars[:-2])
    original.provenance["wave_factor_source"] = ["prior-factor-file"]
    prior = calculate_waves(original)[0]
    current = wave_inputs(bars)
    current.wave_factors = pl.DataFrame({"symbol": [SYMBOL], "trade_date": [current.calendar[1]], "ex_factor": [1.01]})
    _, waves, events, _ = calculate_waves(current, prior)
    [event] = [e for e in events if e["type"] == kind]
    assert event["date"] == current.calendar[-2]
    assert event["known_at"] == f"{event['date']}T17:00:00+08:00"
    if kind == "candidate_expired":
        assert waves[SYMBOL]["window_close"]["known_at"] == event["known_at"]
    else:
        assert event["close"]["factor_source"] == original.provenance["wave_factor_source"]
        assert event["close"]["factor_known_at"] <= event["known_at"]


def test_unpublished_invalidation_keeps_the_first_accepted_close():
    from app.services.evening_report_wave import _WaveReplay

    bars = [*BARS[:12], (13., 10.3, 10.6, 100)]
    days = calendar(len(bars))
    factor_time = f"{days[-1]}T16:00:00+08:00"
    first = run_wave(bars, basis=PriceBasis([], ["original"], factor_time))
    first.rows[days[-1]]["known_at"] = factor_time
    first.advance(days[0])
    original, = [event for event in first.events if event["type"] == "structure_invalid"]
    assert (original["close"]["raw_price"], original["failure_low"]["raw_price"]) == (10.6, 10.7)
    assert original["close"]["factor_source"] == ["original"]
    assert original["close"]["factor_known_at"] == factor_time

    later = f"{days[-1]}T17:00:00+08:00"
    repriced = [*bars[:-1], (13., 10.3, 10.55, 100)]
    replay = _WaveReplay({"state": first.state}, first_day=days[-1])
    replay.before = later
    checkpoint = WaveRun(SYMBOL, days, prices(days, repriced), set(), PriceBasis([], ["revised"], later), later,
                         replay=replay)
    checkpoint.advance(days[0])
    assert checkpoint.state["ended_round"]["invalidated"] == original


@pytest.mark.parametrize(("close", "known_hour"), [(12.2, 16), (11.95, 17)])
def test_factor_accepted_after_prices_on_the_same_day_dates_only_a_new_breakout(close, known_hour):
    prior = calculate_waves(wave_inputs(BARS[:12]))[0]
    current = wave_inputs([*BARS[:12], (12.6, 11.4, close, 40)])
    day = current.trade_date
    earlier = f"{day}T16:00:00+08:00"
    current.prices = current.prices.with_columns(
        pl.when(pl.col("date") == day).then(pl.lit(earlier)).otherwise(pl.col("known_at")).alias("known_at"))
    current.wave_factor_history = [{"cutoff": earlier, "facts": {}, "source": ["first acceptance"]}]
    current.wave_factors = pl.DataFrame({"symbol": [SYMBOL], "trade_date": [day], "ex_factor": [1.01]})
    _, waves, events, _ = calculate_waves(current, prior)
    [breakout] = [e for e in events if e["type"] == "wave_new_high"]
    assert breakout["known_at"] == f"{day}T{known_hour}:00:00+08:00"
    assert waves[SYMBOL]["window_close"]["known_at"] == breakout["known_at"]


@pytest.mark.parametrize(("material_revision", "reaccepted_prices"), [(False, False), (True, False), (True, True)])
def test_first_report_archives_only_starts_and_structure_first_learned_today(wave_publication, material_revision,
                                                                           reaccepted_prices):
    from app.services.evening_report_start_study import run_study

    store, publish = wave_publication
    bars = [*BARS, (12.3, 11.7, 12., 60)]
    if material_revision:
        bars[5:] = [(high * .94, low * .94, close * .94, volume) for high, low, close, volume in bars[5:]]
    missed = wave_inputs(bars[:-1])
    store.save_readiness(missed.trade_date, current_receipt(
        missed.trade_date, missed.cutoff, active_symbols=[SYMBOL], wave_factor_first_accepted=missed.cutoff,
        versions={"files": [["adj_factor/all.parquet", 431, 1]]}, wave_factor_facts={}))
    current = wave_inputs(bars)
    if reaccepted_prices:
        current.prices = current.prices.with_columns(pl.lit(current.cutoff).alias("known_at"))
    factor_day = current.calendar[5 if material_revision else 1]
    current.wave_factors = pl.DataFrame({"symbol": [SYMBOL], "trade_date": [factor_day],
                                         "ex_factor": [1.1 if material_revision else 1.01]})
    report = publish(current)
    cycle = archived_cycle(store, current.trade_date)
    known = current.cutoff if material_revision else missed.cutoff
    assert cycle["breakouts"]["2"]["known_at"] == cycle["window_close"]["known_at"] == known
    kinds = ("wave_started", "wave_new_high", "wave_high_confirmed")
    facts = [(e["type"], e["date"], e["known_at"]) for e in report["wave_events"] if e["type"] in kinds]
    assert facts == ([(kind, missed.trade_date, current.cutoff) for kind in kinds] if material_revision else [])
    results = run_study(store, current, evaluated_at=current.cutoff)["results"]
    assert [r["start_date"] for r in results] == ([current.trade_date] if material_revision else [])
    assert publish(current) == report


@pytest.mark.parametrize("previous_coverage", [None, {"active_symbols": ["000002.SZ"], "halted_symbols": []}],
                         ids=["no-acceptance", "only-other-security"])
def test_first_bulk_acceptance_keeps_its_historical_events_in_warmup(wave_publication, previous_coverage):
    store, publish = wave_publication
    if previous_coverage is not None:
        earlier = wave_inputs(BARS)
        store.save_readiness(earlier.trade_date, current_receipt(
            earlier.trade_date, earlier.cutoff, versions={"files": [["adj_factor/all.parquet", 431, 1]]},
            wave_factor_facts={}, wave_factor_first_accepted=earlier.cutoff, **previous_coverage))
    current = wave_inputs([*BARS, (12.3, 11.7, 12., 60)])
    current.prices = current.prices.with_columns(pl.lit(current.cutoff).alias("known_at"))
    current.wave_first_acceptances[SYMBOL] = current.cutoff
    report = publish(current)
    assert archived_cycle(store, current.trade_date)["starts"]["2"]["date"] == current.calendar[-2]
    assert not report["wave_starts"]
    assert not any(e["type"] in ("wave_new_high", "wave_high_confirmed") for e in report["wave_events"])


@pytest.fixture
def wave_market_receipts(tmp_path, private_owner, monkeypatch):
    return _wave_market_receipts(tmp_path, monkeypatch, BARS)


def _wave_market_receipts(tmp_path, monkeypatch, bars):
    from datetime import datetime

    from app import market_time
    from app.indicators.pipeline import run_pipeline
    from app.services import (
        evening_report_inputs,
        evening_report_jobs,
        evening_report_store,
        preferences,
    )
    from app.services.daily_coverage import DailyCoverage
    from app.services.evening_report_start_study import run_study
    from app.tickflow.repository import DataStore, KlineRepository

    bars = [*bars, (12.3, 11.7, 12., 60)]
    days = calendar(len(bars))
    other_symbol = "000002.SZ"
    stocks = pl.DataFrame([
        {"symbol": symbol, "date": date.fromisoformat(day), "open": low, "high": high, "low": low, "close": close,
         "volume": float(volume), "amount": 750_000_000., "quote_ts": None}
        for symbol, series in [(SYMBOL, bars), (other_symbol, [(10.2, 9.98, 10.16, 50)] * len(bars))]
        for day, (high, low, close, volume) in zip(days, series, strict=True)
    ])
    repo = KlineRepository(DataStore(tmp_path))
    store = evening_report_store.EveningReportStore(tmp_path)
    repo.append_index_daily(stocks.filter(pl.col("symbol") == other_symbol).with_columns(pl.lit("000001.SH").alias("symbol")))
    evening_report_store.atomic_json(tmp_path / "calendar" / "trading_days.json",
                                     {"days": days, "start": days[0], "end": days[-1]})
    pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.Date, "ex_factor": pl.Float64}).write_parquet(
        tmp_path / "adj_factor" / "all.parquet")
    monkeypatch.setattr(preferences, "load", lambda: {})

    def accept(day, hour, updates, symbols, *, publish=False, listing_dates=None, halted=(), st=()):
        now = datetime.fromisoformat(f"{day}T{hour}:00:00+08:00")
        obtained_at = f"{day}T{hour}:00:00+08:00"
        for module in (market_time, evening_report_inputs, evening_report_jobs, evening_report_store):
            monkeypatch.setattr(module, "cn_now", lambda: now)
        metadata = [{"symbol": s, "code": s[:6], "name": s, "exchange": "SZ", "type": "stock", "board": "main",
                     "security_type": "a_share", "is_st": s in st, "is_current": True, "as_of": day,
                     "listing_date": (listing_dates or {}).get(s, "2010-01-01"),
                     "metadata_source": "test", "metadata_rule_version": "v1",
                     "metadata_observed_at": obtained_at} for s in symbols]
        pl.DataFrame(metadata).write_parquet(tmp_path / "instruments" / "instruments.parquet")
        repo.append_daily(updates)
        run_pipeline(tmp_path)
        coverage = DailyCoverage(day)
        coverage.observe(pl.read_parquet(tmp_path / "kline_daily" / f"date={day}" / "*.parquet"))
        if halted:
            coverage.record_halted(halted, source="trading_status", basis="confirmed_all_day")
        coverage.require(symbols)
        observations = [{"id": source, "source": source, "complete": True, "symbols": symbols,
                         "obtained_at": obtained_at, "memberships": []} for source in ("industry", "concept")]
        evidence = evening_report_inputs.save_ready(tmp_path, day, coverage, observations, {})
        if publish:
            evening_report_jobs.generate(repo, day, progress=lambda *args, **kwargs: None)
            inputs = evening_report_inputs.load_inputs(tmp_path, day, evidence)
            results = run_study(store, inputs, evaluated_at=now.isoformat())["results"]
            return store.get(str(day)), store, [result["start_date"] for result in results]

    return stocks, other_symbol, accept


@pytest.mark.parametrize("gap_kind", ["partition", "stock", "intraday"])
def test_first_acceptance_of_missing_partition_archives_start_on_known_date(wave_market_receipts, gap_kind):
    stocks, other, accept = wave_market_receipts
    days = stocks["date"].unique().sort().to_list()
    gap, signal, today = days[9], days[-2], days[-1]
    symbols = [SYMBOL, other]
    before = stocks.filter(pl.col("date") <= signal)
    missing = pl.col("date") == gap
    if gap_kind != "partition":
        missing &= pl.col("symbol") == SYMBOL
    if gap_kind == "intraday":
        before = before.with_columns(pl.when(missing).then(pl.lit(1, dtype=pl.Int64)).alias("quote_ts"))
    else:
        before = before.filter(~missing)
    original, store, _ = accept(signal, 17, before, symbols, publish=True)
    assert not original["wave_starts"]
    waiting = store.waiting_wave_bars(str(signal))
    assert {day for day, rows in waiting.items() if SYMBOL in rows} == {str(gap)}
    update = stocks.filter(pl.col("date").is_in([gap, today]))
    accept(today, 16, update, symbols)
    report, _, research = accept(today, 17, update, symbols, publish=True)
    start, = report["wave_starts"]
    assert (start["date"], start["known_at"]) == (str(signal), f"{today}T16:00:00+08:00")
    assert start["start_id"] in [e.get("start_id") for e in report["wave_events"]]
    assert research == [str(today)]
    assert store.get(str(signal)) == original


@pytest.mark.parametrize("bad_symbol", ["outside", SYMBOL])
def test_bad_factor_is_local_to_its_security(wave_market_receipts, tmp_path, bad_symbol):
    stocks, other, accept = wave_market_receipts
    seed, signal, following = stocks["date"].unique().sort().to_list()[-3:]
    symbols = [SYMBOL, other]
    accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    path = tmp_path / "adj_factor" / "all.parquet"
    empty = pl.read_parquet(path)
    pl.DataFrame({"symbol": [bad_symbol], "trade_date": [signal], "ex_factor": [0.]}).write_parquet(path)
    report, store, results = accept(signal, 17, stocks.filter(pl.col("date") == signal), symbols, publish=True)
    assert store.readiness(str(signal))["wave_factor_facts"][bad_symbol] is None
    assert len(report["wave_starts"]) == len(results) == (bad_symbol != SYMBOL)
    if bad_symbol == SYMBOL:
        empty.write_parquet(path)
        report, _, results = accept(following, 17, stocks.filter(pl.col("date") == following), symbols, publish=True)
        assert results == [str(following)]
        assert report["wave_starts"][0]["known_at"] == report["cutoff"]


def test_publication_keeps_a_proven_start_waiting_on_a_missing_bar(tmp_path, private_owner, monkeypatch):
    from app.tickflow.repository import DataStore, KlineRepository

    bars = [*BARS[:7], (11.8, 10.9, 11.25, 65), *BARS[8:12], (11.55, 11., 11.06, 100), (11.6, 11., 11.5, 100)]
    stocks, peer, accept = _wave_market_receipts(tmp_path, monkeypatch, bars)
    days = stocks["date"].unique().sort().to_list()
    symbols = [SYMBOL, peer]
    accept(days[10], 17, stocks.filter(pl.col("date") <= days[10]), symbols, publish=True)
    prior, signal, following = days[11:14]
    accept(signal, 16, stocks.filter(pl.col("date").is_in([prior, signal])), symbols)
    repo = KlineRepository(DataStore(tmp_path))
    earlier = stocks.filter((pl.col("date") == prior) & (pl.col("symbol") == SYMBOL))
    repo.replace_daily_rows("stock", prior, earlier.with_columns(pl.lit(None, dtype=pl.Float64).alias("close")),
                            symbols=[SYMBOL], backup_dir=tmp_path / "backups")
    frozen, store, study = accept(signal, 17, stocks.head(0), symbols, publish=True)
    assert frozen["wave_starts"] == study == []
    updates = stocks.filter(pl.col("date") == following)
    updates = pl.concat([updates, earlier])
    report, _, study = accept(following, 17, updates, symbols, publish=True)
    start, = report["wave_starts"]
    assert (start["date"], start["known_at"], study) == (
        str(signal), f"{signal}T16:00:00+08:00", [str(signal)])
    assert store.get(str(signal)) == frozen


@pytest.mark.parametrize("effective_index", [7, 11])
def test_newly_accepted_prior_day_factor_rebases_published_high(wave_market_receipts, tmp_path, effective_index):
    from app.services.kline_sync import _merge_adj_factor_store

    stocks, other, accept = wave_market_receipts
    seed, signal, _ = stocks["date"].unique().sort().to_list()[-3:]
    symbols = [SYMBOL, other]
    original, store, _ = accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    _merge_adj_factor_store(tmp_path / "adj_factor" / "all.parquet", pl.DataFrame(
        {"symbol": [SYMBOL], "trade_date": [stocks["date"].unique().sort()[effective_index]], "ex_factor": [1.02]}))
    updated = stocks.filter(pl.col("date") == signal).with_columns(
        pl.when(pl.col("symbol") == SYMBOL).then(11.90).otherwise("close").alias("close"))
    report, _, _ = accept(signal, 17, updated, symbols, publish=True)
    event, = [e for e in report["wave_events"] if e["type"] == "wave_new_high" and e["symbol"] == SYMBOL]
    assert (event["date"], event["known_at"]) == (str(signal), report["cutoff"])
    assert store.get(str(seed)) == original


def test_equal_rebased_high_keeps_original_candidate_clock():
    days = calendar(2)
    run = run_wave([(1.17, 1.15, 1.17, 60), (.9, .89, .9, 60)],
                   basis=PriceBasis([(days[1], 1.3)], "accepted", days[0]))
    result = run.advance(days[0])
    assert result["provisional_extreme"]["extreme_date"] == days[0]
    assert result["provisional_extreme"]["idle_sessions"] == 1


def test_confirmed_swing_below_minimum_is_known_and_not_a_first_wave():
    bars = [*BARS[:6], (11.5, 11.1, 11.45, 100), (11.3, 10.8, 10.90, 65)]
    run = run_wave(bars)
    result = run.advance(run.days[0])
    assert result["data_status"] == "known" and result["completed_wave_count"] == 0
    assert result["first_pullback_eligible"] == {"value": False, "reason_codes": ["first_impulse_below_min"]}
    assert [p["kind"] for p in result["confirmed_pivots"]] == ["L", "H"]


def test_rebuilding_confirmed_low_without_reconnection_has_unknown_attribution():
    bars = [*BARS[:8], *((11.8, 11.2, 11.3, 30),) * 22, (10.3, 9.7, 10.3, 30)]
    run = run_wave(bars)
    wave = run.advance(run.days[0])
    assert wave["data_status"] == "rebuilding"
    density = wave["pivot_density"]
    assert density["unknown_pivot_count"] == 1 and density["unassigned_pivot_count"] == 0
    assert density["confirmed_pivot_count"] == 1 and density["cycle_refs"][0]["confirmed_pivot_count"] == 0
    assert wave["confirmed_pivots"][-1]["confirmed_at"] != run.days[-1]


@pytest.mark.parametrize("failed_write", ["acceptances", "ready"])
@pytest.mark.parametrize("retry", [False, True])
def test_failed_acceptance_never_enters_replay(tmp_path, private_owner, monkeypatch, failed_write, retry):
    from app.services import evening_report_store as storage

    stocks, other, accept = _wave_market_receipts(tmp_path, monkeypatch, [*BARS, (12.3, 11.7, 12., 60)])
    signal, withdrawal, final = stocks["date"].unique().sort().to_list()[-3:]
    symbols = [SYMBOL, other]
    target = (pl.col("symbol") == SYMBOL) & (pl.col("date") == signal)
    bar = stocks.filter(target)
    low = bar.with_columns(pl.lit(60.).alias("volume"))
    before = stocks.filter(pl.col("date") <= signal).with_columns(
        pl.when(target).then(60.).otherwise(pl.col("volume")).alias("volume"))
    original, store, _ = accept(signal, 16, before, symbols, publish=True)
    first = store.readiness(str(signal))
    atomic = storage.atomic_json

    def fail(path, value, **kwargs):
        if path.parent.name == failed_write:
            raise OSError("injected acceptance failure")
        return atomic(path, value, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(storage, "atomic_json", fail)
        with pytest.raises(OSError, match="injected acceptance failure"):
            accept(signal, 17, bar, symbols)
    restarted = storage.EveningReportStore(tmp_path)
    assert restarted.readiness(str(signal)) == first
    assert restarted.acceptance_references()[str(signal)] == [(first["cutoff"], first["input_id"])]
    if retry:
        accept(signal, 18, low, symbols)
        assert store.readiness(str(signal))["wave_previous_acceptance"] == first["cutoff"]
    accept(withdrawal, 17, stocks.filter(pl.col("date") == withdrawal), symbols, publish=True)
    report, _, samples = accept(final, 17, stocks.filter(pl.col("date") == final), symbols, publish=True)
    assert report["wave_starts"] == samples == []
    assert store.get(str(signal)) == original


@pytest.mark.parametrize("rewrite_all", [False, True], ids=["only-stock", "shared-rewrite"])
def test_same_day_volume_acceptance_only_publishes_the_new_start(wave_market_receipts, rewrite_all):
    stocks, other, accept = wave_market_receipts
    today = stocks["date"].max()
    previous = calendar(len(BARS))[-1]
    evidence_bar = (pl.col("symbol") == SYMBOL) & (pl.col("date") == date.fromisoformat(previous))
    before = stocks.with_columns(pl.when(evidence_bar).then(60.).otherwise(pl.col("volume")).alias("volume"))
    accept(today, 16, before, [SYMBOL, other])
    updates = stocks.filter(evidence_bar)
    if rewrite_all:
        other_revisions = stocks.filter(pl.col("symbol") == other).with_columns(pl.col("volume") + 1)
        updates = pl.concat([updates, other_revisions])
    report, store, start_dates = accept(today, 17, updates, [SYMBOL, other], publish=True)
    [start] = report["wave_starts"]
    assert (start["date"], start["known_at"]) == (previous, f"{today}T17:00:00+08:00")
    assert start_dates == [str(today)]
    # The price breakout was already established by the first receipt, before the volume correction.
    assert archived_cycle(store, str(today))["breakouts"]["2"]["known_at"] == f"{today}T16:00:00+08:00"
    assert not any(event["type"] in ("wave_new_high", "wave_high_confirmed") for event in report["wave_events"])


@pytest.mark.parametrize(("previously_covered", "previous_report"), [
    pytest.param(True, False, id="previously-covered"),
    pytest.param(False, False, id="uncovered-old-row"),
    pytest.param(False, True, id="uncovered-old-row-with-previous-report"),
])
def test_first_wave_report_uses_accepted_history_for_each_stock(wave_market_receipts, previously_covered, previous_report):
    stocks, other, accept = wave_market_receipts
    today = stocks["date"].max()
    previous = date.fromisoformat(calendar(len(BARS))[-1])
    before = stocks.filter(pl.col("date") <= previous)
    updates = stocks.filter(pl.col("date") == today)
    symbols = [SYMBOL, other]
    if previously_covered:
        accept(previous, 16, before, symbols)
    else:
        # One old row remains in an untouched partition even though only the other stock was accepted.
        first_day = before["date"].min()
        before = before.filter((pl.col("symbol") == other) | (pl.col("date") == first_day))
        accept(previous, 16, before, [other], publish=previous_report)
        imported = stocks.filter((pl.col("symbol") == SYMBOL) & pl.col("date").is_between(first_day, today, closed="none"))
        updates = pl.concat([updates, imported])
    report, store, start_dates = accept(today, 17, updates, symbols, publish=True)
    start = archived_cycle(store, str(today))["starts"]["2"]
    assert start["date"] == str(previous)
    assert start["known_at"] == (f"{previous}T16:00:00+08:00" if previously_covered else f"{today}T17:00:00+08:00")
    assert not report["wave_starts"] and not start_dates
    assert not any(e["type"] in ("wave_new_high", "wave_high_confirmed") for e in report["wave_events"])


@pytest.mark.parametrize("history", ["intact", "missing", "unrelated"])
def test_unreported_day_factor_versions_bound_the_first_start(tmp_path, private_owner, monkeypatch, history):
    from app.services.evening_report_store import EveningReportStore
    from app.services.kline_sync import _merge_adj_factor_store

    bars = [*BARS, (12.3, 11.7, 12., 60)]
    bars[5:] = [(h * .94, low * .94, close * .94, volume) for h, low, close, volume in bars[5:]]
    stocks, other, accept = _wave_market_receipts(tmp_path, monkeypatch, bars)
    warmup, signal, interim, final = stocks["date"].unique().sort().to_list()[-4:]
    symbols = [SYMBOL, other]
    effective = stocks["date"].unique().sort()[5]
    factors = tmp_path / "adj_factor" / "all.parquet"
    store = EveningReportStore(tmp_path)

    def update(symbol, value):
        events = pl.DataFrame({"symbol": [symbol], "trade_date": [effective], "ex_factor": [value]})
        _merge_adj_factor_store(factors, events)

    def rows(day):
        return stocks.filter(pl.col("date") == day)

    affected = other if history == "unrelated" else SYMBOL
    update(affected, 1.)
    seed = stocks["date"].unique().sort()[4]
    original, _, _ = accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    accept(warmup, 17, stocks.filter(pl.col("date").is_between(seed, warmup, closed="right")), symbols)
    accept(signal, 16, rows(signal), symbols)
    update(affected, 1.1)
    accept(signal, 17, stocks.clear(), symbols)
    latest = store.readiness(str(signal))
    path = store.root / "inputs" / f"{latest['input_id']}.json.gz"
    if history != "intact":
        path.unlink()
        for folder in ("acceptances", "ready"):
            (store.root / folder / f"{signal}.json").unlink()

    update(affected, 1.)
    accept(interim, 17, rows(interim), symbols, publish=False)
    lower = store.readiness(str(interim))
    if history != "intact":
        assert lower["wave_missing_predecessor_day"] == str(signal)
        assert not lower.get("wave_previous_acceptance")
        assert lower["wave_unverified_price_days"] == {affected: str(effective)}
    else:
        assert "wave_missing_predecessor_day" not in lower

    update(SYMBOL, 1.1)
    report, _, samples = accept(final, 17, rows(final), symbols, publish=True)
    if history == "intact":
        assert [s["known_at"] for s in report["wave_starts"]] == [latest["cutoff"]]
        assert samples == [str(signal)]
    elif history == "unrelated":
        assert [s["known_at"] for s in report["wave_starts"]] == [report["cutoff"]]
        assert samples == [str(final)]
    else:
        start = archived_cycle(store, str(final))["starts"]["2"]
        assert start["known_at"] is None and start["known_by"] == report["cutoff"]
        assert report["wave_starts"] == samples == []
    assert store.get(str(seed)) == original


@pytest.mark.parametrize("interim_unreadable", [False, True])
def test_unwitnessed_factor_read_recovery_keeps_the_receipt_gap(
        tmp_path, private_owner, monkeypatch, interim_unreadable):
    from app.services import evening_report_inputs as inputs_api
    from app.services.evening_report_store import EveningReportStore

    stocks, other, accept = _wave_market_receipts(tmp_path, monkeypatch, [*BARS, (12.3, 11.7, 12., 60)])
    warmup, signal, interim, final = stocks["date"].unique().sort().to_list()[-4:]
    symbols = [SYMBOL, other]
    store = EveningReportStore(tmp_path)
    accept(warmup, 17, stocks.filter(pl.col("date") <= warmup), symbols, publish=True)

    def failed_read(*args):
        return None, inputs_api.factor_file_reference(args[-1])

    with monkeypatch.context() as fault:
        fault.setattr(inputs_api, "wave_factor_inputs", failed_read)
        accept(signal, 16, stocks.filter(pl.col("date") == signal), symbols)
    accept(signal, 17, stocks.clear(), symbols)
    latest = store.readiness(str(signal))
    path = store.root / "inputs" / f"{latest['input_id']}.json.gz"
    path.unlink()
    for folder in ("acceptances", "ready"):
        (store.root / folder / f"{signal}.json").unlink()
    with monkeypatch.context() as fault:
        if interim_unreadable:
            fault.setattr(inputs_api, "wave_factor_inputs", failed_read)
        accept(interim, 17, stocks.filter(pl.col("date") == interim), symbols, publish=True)
    lower = store.readiness(str(interim))
    assert lower.get("wave_missing_predecessor_day") == str(signal)
    assert inputs_api.factor_file_reference(lower["versions"]) == inputs_api.factor_file_reference(latest["versions"])
    report, _, samples = accept(final, 17, stocks.filter(pl.col("date") == final), symbols, publish=True)
    start = archived_cycle(store, str(final))["starts"]["2"]
    assert start["known_at"] is None and report["wave_starts"] == samples == []


@pytest.mark.parametrize("revoked", [False, True])
def test_unreadable_last_acceptance_leaves_first_start_unverified(wave_market_receipts, revoked):
    stocks, other, accept = wave_market_receipts
    day, today = stocks["date"].unique().sort().to_list()[-2:]
    symbols = [SYMBOL, other]
    target = (pl.col("symbol") == SYMBOL) & (pl.col("date") == day)
    before = stocks.filter(pl.col("date") <= day).with_columns(
        pl.when(target).then(60.).otherwise(pl.col("volume")).alias("volume"))
    warm = stocks["date"].unique().sort().to_list()[-3]
    original, store, samples = accept(warm, 17, before.filter(pl.col("date") <= warm), symbols, publish=True)
    accept(day, 16, before.filter(pl.col("date") == day), symbols)
    baseline = store.readiness(str(day))
    assert original["wave_starts"] == samples == []
    accept(day, 17, stocks.filter(target), symbols)
    latest = store.readiness(str(day))
    original_path = store.root / "inputs" / f"{latest['input_id']}.json.gz"
    original_path.unlink()
    if revoked:
        store.save_readiness(str(day), {"complete": False})

    report, _, samples = accept(today, 17, stocks.filter(pl.col("date") == today), symbols, publish=True)
    receipt = store.readiness(str(today))
    assert receipt.get("wave_previous_acceptance") == latest["cutoff"]
    assert "wave_price_compared_after" not in receipt
    if changes := receipt.get("wave_price_changes"):
        old = changes[SYMBOL][str(day)]
        assert (old["volume"], old["known_at"]) == (60., baseline["cutoff"])
        assert receipt["wave_unverified_price_days"][SYMBOL] == str(day)
    start = archived_cycle(store, str(today))["starts"]["2"]
    assert start["known_at"] is None and start["known_by"] == receipt["cutoff"]
    assert report["wave_starts"] == samples == []
    assert store.get(str(warm)) == original


def test_missing_reacceptance_still_allows_a_start_on_a_new_session(wave_market_receipts):
    stocks, other, accept = wave_market_receipts
    previous, today = stocks["date"].unique().sort().to_list()[-3:-1]
    symbols = [SYMBOL, other]
    _, store, samples = accept(previous, 16, stocks.filter(pl.col("date") <= previous), symbols, publish=True)
    assert samples == []
    accept(previous, 17, stocks.filter(pl.col("date") == previous), symbols)
    lost = store.last_accepted_input_id(str(previous))
    (store.root / "inputs" / f"{lost}.json.gz").unlink()
    updates = stocks.filter(pl.col("date") == today)
    report, _, samples = accept(today, 17, updates, symbols, publish=True)
    start, = report["wave_starts"]
    assert (start["date"], start["known_at"]) == (str(today), report["cutoff"])
    assert samples == [str(today)]


def test_st_reentry_starts_a_fresh_wave_after_a_factor_change(tmp_path, private_owner, monkeypatch):
    from app.services.evening_report_store import EveningReportStore, atomic_json
    from app.tickflow.repository import DataStore, KlineRepository

    bars = BARS[:5] + [(high * .94, low * .94, close * .94, volume) for high, low, close, volume in BARS[5:]]
    stocks, other, accept = _wave_market_receipts(tmp_path, monkeypatch, bars)
    symbols = [SYMBOL, other]
    days = [date.fromisoformat(day) for day in calendar(15)]
    signal, later_st, reentry = days[12:]
    atomic_json(tmp_path / "calendar" / "trading_days.json", {"days": list(map(str, days)),
                                                             "start": str(days[0]), "end": str(reentry)})
    tail = stocks.filter(pl.col("date") == later_st)
    signal_bars = stocks.filter(pl.col("date") == signal)
    original, _, samples = accept(days[11], 17, stocks.filter(pl.col("date") < signal), symbols, publish=True)
    assert original["wave_starts"] == samples == []

    def factor(value):
        pl.DataFrame({"symbol": [SYMBOL], "trade_date": [days[5]], "ex_factor": [value]}).write_parquet(
            tmp_path / "adj_factor" / "all.parquet")

    factor(1.10)
    accept(signal, 16, signal_bars, symbols, st=[SYMBOL])
    factor(1.00)
    st_report, _, _ = accept(signal, 17, signal_bars, symbols, st=[SYMBOL], publish=True)
    assert all(row["symbol"] != SYMBOL for row in st_report["records"])
    accept(later_st, 17, tail, symbols, st=[SYMBOL], publish=True)
    updates = tail.with_columns(pl.lit(reentry).alias("date"))
    factor(1.12)

    repo = KlineRepository(DataStore(tmp_path))
    repo.append_index_daily(updates.filter((pl.col("date") == reentry) & (pl.col("symbol") == other))
                            .with_columns(pl.lit("000001.SH").alias("symbol")))
    report, store, samples = accept(reentry, 17, updates, symbols, publish=True)
    assert report["wave_starts"] == samples == []
    assert all(event["date"] == str(reentry) for event in report["wave_events"] if event["symbol"] == SYMBOL)
    state = store.state(str(reentry))["state"]["wave"]["stocks"][SYMBOL]
    assert state["st_reentry"] == state["state"]["date"] == str(reentry)
    assert state["state"]["session"] == 1
    assert state["state"]["round"] is None
    assert store.get(str(signal)) == st_report
    assert EveningReportStore(tmp_path).get(str(days[11])) == original


def test_post_st_wave_starts_with_first_accepted_non_st_day_even_without_reports(wave_market_receipts, tmp_path):
    import os
    from datetime import datetime

    from app.services.evening_report_store import EveningReportStore, atomic_json, read_json
    from app.tickflow.repository import DataStore, KlineRepository

    stocks, other, accept = wave_market_receipts
    repo = KlineRepository(DataStore(tmp_path))
    symbols = [SYMBOL, other]
    days = [date.fromisoformat(day) for day in calendar(50)]
    signal, st_day, reentry, target = days[12], days[13], days[36], days[48]
    atomic_json(tmp_path / "calendar" / "trading_days.json", {"days": list(map(str, days)),
                                                             "start": str(days[0]), "end": str(days[-1])})
    initial, _, samples = accept(signal, 17, stocks.filter(pl.col("date") <= signal), symbols, publish=True)
    assert len(initial["wave_starts"]) == 1 and samples == [str(signal)]

    tail = stocks.filter(pl.col("date") == st_day)
    accept(st_day, 17, tail, symbols, st=[SYMBOL])
    store = EveningReportStore(tmp_path)
    st_receipt = store.readiness(str(st_day))["input_id"]
    # No qualification evidence on these sessions even though the raw price history is complete.
    repo.append_daily(pl.concat([tail.with_columns(pl.lit(day).alias("date")) for day in days[14:36]]))
    shifted = stocks.with_columns(pl.col("date").replace(dict(zip(days[:14], days[36:], strict=True))))
    repo.append_index_daily(shifted.filter(pl.col("symbol") == other).with_columns(pl.lit("000001.SH").alias("symbol")))
    accept(reentry, 17, shifted.filter(pl.col("date") == reentry), symbols)
    for path in (store.root / "inputs").glob("*.json.gz"):
        stamp = datetime.fromisoformat(read_json(path)["cutoff"]).timestamp()
        os.utime(path, (stamp, stamp))
    assert store.prune(target)["removed_files"] > 0
    assert store.input_evidence(st_receipt) is None
    report, store, samples = accept(target, 17, shifted.filter((pl.col("date") > reentry) & (pl.col("date") <= target)),
                                    symbols, publish=True)
    start, = report["wave_starts"]
    assert start["known_at"] == f"{target}T17:00:00+08:00"
    assert start["start_id"] != initial["wave_starts"][0]["start_id"]
    state = store.state(str(target))["state"]["wave"]["stocks"][SYMBOL]
    assert state["st_reentry"] == str(reentry) and state["state"]["session"] == 13
    assert samples == [str(signal), str(target)]
    assert store.get(str(signal)) == initial


def test_post_st_wave_ma10_uses_only_the_new_formal_history():
    inputs = wave_inputs([(50., 50., 50., 100.), *BARS[:-1], (12., 11.3, 11.4, 100.)])
    inputs.wave_st_reentry = {SYMBOL: inputs.calendar[1]}
    # The general indicators still carry the earlier ST period's invalid history.
    inputs.prices = inputs.prices.with_columns(pl.lit(False).alias("formal"),
        pl.when(pl.col("date") != inputs.calendar[1]).then(pl.col("wave_amount")).alias("wave_amount"))
    _, _, _, starts = calculate_waves(inputs)
    start, = starts
    assert start["price_branches"] == ["ma10"]
    assert start["known_at"] == inputs.cutoff


@pytest.mark.parametrize("new_stock", [False, True], ids=["new-session-after-gap", "first-stock-acceptance-in-gap"])
def test_first_availability_and_new_sessions_bound_a_missing_receipt(new_stock):
    prior = calculate_waves(wave_inputs(BARS[:-2]))[0]
    current = wave_inputs(BARS)
    earlier = f"{current.calendar[-2]}T18:00:00+08:00"
    if new_stock:
        prior["stocks"] = {}
        first = f"{current.trade_date}T16:00:00+08:00"
        current.prices = current.prices.with_columns(pl.lit(first).alias("known_at"))
        current.wave_first_acceptances = {SYMBOL: first}
        # The stored price clocks and its first coverage coincide, with no possible earlier stock acceptance.
        current.wave_acceptance_gaps = [(prior["cutoff"], first)]
    else:
        first = current.cutoff
        current.wave_acceptance_gaps = [(prior["cutoff"], earlier)]
    _, _, _, starts = calculate_waves(current, prior)
    assert [event["known_at"] for event in starts] == [first]


@pytest.mark.parametrize("factor_available", [True, False])
def test_same_day_start_is_dated_when_price_and_factor_are_both_accepted(wave_market_receipts, tmp_path,
                                                                        factor_available):
    stocks, peer, accept = wave_market_receipts
    seed, signal, following = stocks["date"].unique().sort().to_list()[-3:]
    symbols = [SYMBOL, peer]
    accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    bars = stocks.filter(pl.col("date") == signal)
    accept(signal, 15, bars.with_columns(pl.lit(60.).alias("volume")), symbols)
    factor = tmp_path / "adj_factor" / "all.parquet"
    held = tmp_path / "unavailable.parquet"
    if not factor_available:
        factor.rename(held)
    accept(signal, 16, bars, symbols)
    if factor_available:
        factor.rename(held)
    accept(signal, 17, bars.clear(), symbols)
    held.rename(factor)
    accept(signal, 18, bars.clear(), symbols)
    report, _, samples = accept(following, 17, stocks.filter(pl.col("date") == following), symbols, publish=True)
    start, = report["wave_starts"]
    hour = 16 if factor_available else 18
    assert start["known_at"] == f"{signal}T{hour}:00:00+08:00"
    assert samples == [str(signal)]


def test_first_accepted_factor_dates_events_with_previously_accepted_prices(wave_market_receipts, tmp_path):
    stocks, other, accept = wave_market_receipts
    today = stocks["date"].max()
    previous = date.fromisoformat(calendar(len(BARS))[-1])
    factor_path = tmp_path / "adj_factor" / "all.parquet"
    unavailable = factor_path.rename(tmp_path / "unavailable.parquet")
    accept(today, 16, stocks, [SYMBOL, other])
    unavailable.rename(factor_path)
    bar = stocks.filter((pl.col("symbol") == SYMBOL) & (pl.col("date") == previous))
    report, _, dates = accept(today, 17, bar.with_columns(pl.col("volume") + 1), [SYMBOL, other], publish=True)
    kinds = ("wave_started", "wave_new_high", "wave_high_confirmed")
    assert [(e["type"], e["date"], e["known_at"]) for e in report["wave_events"] if e["type"] in kinds] == [
        (kind, str(previous), f"{today}T17:00:00+08:00") for kind in kinds]
    assert dates == [str(today)]


@pytest.mark.parametrize("recovery", ["same-file", "new-file", "withdrawn-while-unavailable"])
def test_wave_requires_an_accepted_factor_at_the_price_observation(wave_market_receipts, tmp_path, recovery):
    stocks, other, accept = wave_market_receipts
    prior, signal, restored = stocks["date"].unique().sort().to_list()[-3:]
    symbols = [SYMBOL, other]
    accept(prior, 17, stocks.filter(pl.col("date") <= prior), symbols, publish=True)
    factor = tmp_path / "adj_factor" / "all.parquet"
    saved = factor.rename(tmp_path / "unavailable.parquet")
    bars = stocks.filter(pl.col("date") == signal)
    interim, store, samples = accept(signal, 17, bars, symbols, publish=True)
    assert interim["wave_starts"] == samples == []
    if recovery == "withdrawn-while-unavailable":
        accept(signal, 18, bars.with_columns(pl.lit(60.).alias("volume")), symbols)
    if recovery == "new-file":
        pl.DataFrame({"symbol": [other], "trade_date": [signal], "ex_factor": [1.01]}).write_parquet(factor)
    else:
        saved.rename(factor)
    report, _, samples = accept(restored, 17, stocks.filter(pl.col("date") == restored), symbols, publish=True)
    kinds = ("wave_started", "wave_new_high", "wave_high_confirmed")
    expected = kinds[1:] if recovery == "withdrawn-while-unavailable" else kinds
    assert [(e["type"], e["date"], e["known_at"]) for e in report["wave_events"] if e["type"] in kinds] == [
        (kind, str(signal), report["cutoff"]) for kind in expected]
    assert samples == ([] if recovery == "withdrawn-while-unavailable" else [str(restored)])
    assert store.get(str(signal)) == interim


def test_factor_read_recovery_requires_a_new_acceptance(wave_market_receipts, tmp_path, monkeypatch):
    from app.services import evening_report_inputs, evening_report_jobs
    from app.tickflow.repository import DataStore, KlineRepository

    stocks, other, accept = wave_market_receipts
    prior, signal, restored = stocks["date"].unique().sort().to_list()[-3:]
    symbols = [SYMBOL, other]
    _, store, _ = accept(prior, 17, stocks.filter(pl.col("date") <= prior), symbols, publish=True)
    with monkeypatch.context() as unavailable:
        unavailable.setattr(evening_report_inputs, "wave_factor_inputs", lambda *args: (None, None))
        accept(signal, 17, stocks.filter(pl.col("date") == signal), symbols)
    receipt = store.readiness(str(signal))
    assert receipt["wave_factor_facts"] is None and evening_report_inputs.factor_file_reference(receipt["versions"])
    evening_report_jobs.generate(KlineRepository(DataStore(tmp_path)), signal, progress=lambda *a, **kw: None)
    assert not store.get(str(signal))["wave_starts"]
    report, _, samples = accept(restored, 17, stocks.filter(pl.col("date") == restored), symbols, publish=True)
    start, = report["wave_starts"]
    assert start["known_at"] == report["cutoff"] and samples == [str(restored)]


@pytest.mark.parametrize("confirmed", [True, False], ids=["confirmed-no-row-halt", "unexplained-gap"])
def test_replay_of_backfilled_bar_preserves_prior_halt_evidence(tmp_path, private_owner, monkeypatch, confirmed):
    # The extra session between the pullback pivots leaves the round and breakout unchanged if suspended.
    bars = [*BARS[:9], (11.35, 11.05, 11.18, 65), *BARS[9:]]
    stocks, other, accept = _wave_market_receipts(tmp_path, monkeypatch, bars)
    dates = stocks["date"].unique().sort().to_list()
    gap, signal, today = dates[9], dates[-2], dates[-1]
    missing = (pl.col("symbol") == SYMBOL) & (pl.col("date") == gap)
    symbols = [SYMBOL, other]
    accept(gap, 17, stocks.filter((pl.col("date") <= gap) & ~missing), symbols if confirmed else [other],
           halted=[SYMBOL] if confirmed else [])
    accept(signal, 17, stocks.filter(pl.col("date").is_between(gap, signal, closed="right")), symbols)
    report, store, study = accept(today, 17, stocks.filter(missing | (pl.col("date") == today)), symbols, publish=True)
    cycle = archived_cycle(store, str(today))
    first_known = f"{signal if confirmed else today}T17:00:00+08:00"
    assert cycle["starts"]["2"]["known_at"] == cycle["breakouts"]["2"]["known_at"] == first_known
    kinds = ("wave_started", "wave_new_high", "wave_high_confirmed")
    assert [e["type"] for e in report["wave_events"] if e["symbol"] == SYMBOL and e["type"] in kinds] == (
        [] if confirmed else list(kinds))
    assert study == ([] if confirmed else [str(today)])


def test_halts_survive_long_acceptance_gap_and_same_day_retries(tmp_path, private_owner, monkeypatch):
    from app.services.evening_report_inputs import load_inputs
    from app.services.evening_report_store import EveningReportStore

    bars = [*BARS[:9], (11.35, 11.05, 11.18, 65), *BARS[9:], *[(12.3, 11.7, 12., 60)] * 24]
    stocks, other, accept = _wave_market_receipts(tmp_path, monkeypatch, bars)
    dates = stocks["date"].unique().sort().to_list()
    first, last, today = dates[8], dates[9], dates[-1]
    symbols = [SYMBOL, other]
    first_halt = (pl.col("symbol") == other) & (pl.col("date") == first)
    last_halt = (pl.col("symbol") == SYMBOL) & (pl.col("date") == last)
    today_halt = (pl.col("symbol") == other) & (pl.col("date") == today)
    accept(first, 16, stocks.filter((pl.col("date") <= first) & ~first_halt), symbols, halted=[other])
    accept(last, 17, stocks.filter((pl.col("date") == last) & ~last_halt), symbols, halted=[SYMBOL])

    store = EveningReportStore(tmp_path)
    expected = {(other, str(first)): store.last_accepted_input_id(str(first)),
                (SYMBOL, str(last)): store.last_accepted_input_id(str(last))}
    accept(today, 16, stocks.filter((pl.col("date") > last) & ~today_halt), symbols, halted=[other])
    first_recovery = store.readiness(str(today))
    accept(today, 17, stocks.filter(today_halt), symbols)
    retry = store.readiness(str(today))
    for receipt in (first_recovery, retry):
        assert {(s["symbol"], s["date"]): s["source_input_id"] for s in receipt["historical_suspensions"]} == expected
    assert retry["halted_symbols"] == []
    report, _, study = accept(today, 18, stocks.filter(last_halt), symbols, publish=True)
    final = store.readiness(str(today))
    assert final["wave_price_changes"][SYMBOL][str(last)] == {
        "halted": True, "wave_formal": False, "raw_absent": True, "known_at": f"{last}T17:00:00+08:00"}
    suspended = {(s["symbol"], str(s["date"])) for s in load_inputs(tmp_path, today, final).provenance["suspensions"]}
    assert suspended == {(other, str(first))}
    kinds = ("wave_started", "wave_new_high", "wave_high_confirmed")
    assert [(e["type"], e["known_at"]) for e in report["wave_events"] if e["symbol"] == SYMBOL and e["type"] in kinds
            ] == [(kind, f"{today}T16:00:00+08:00") for kind in kinds]
    assert study == [str(today)]


def test_missing_acceptance_can_cover_a_previously_deferred_bar(tmp_path, private_owner, monkeypatch):
    """A late bar becomes knowable only when a receipt covers its stock."""
    from app.services.evening_report_store import EveningReportStore

    stocks, other, accept = _wave_market_receipts(tmp_path, monkeypatch, [*BARS, (12.3, 11.7, 12., 60.)])
    seed, signal, retry, final = stocks["date"].unique().sort().to_list()[-4:]
    symbols = [SYMBOL, other]
    def rows(day):
        return stocks.filter(pl.col("date") == day)

    accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    accept(signal, 16, rows(signal).filter(pl.col("symbol") == other), [other])
    missing = rows(signal).filter(pl.col("symbol") == SYMBOL)
    accept(retry, 16, pl.concat([rows(retry).filter(pl.col("symbol") == other), missing]), [other])
    accept(retry, 17, rows(retry).filter(pl.col("symbol") == SYMBOL), symbols)
    store = EveningReportStore(tmp_path)
    latest = store.readiness(str(retry))
    (store.root / "inputs" / f"{latest['input_id']}.json.gz").unlink()

    report, _, samples = accept(final, 17, rows(final), symbols, publish=True)
    assert store.readiness(str(final))["wave_unverified_price_days"] == {SYMBOL: str(signal)}
    start = archived_cycle(store, str(final))["starts"]["2"]
    assert start["known_at"] is None and start["known_by"] == report["cutoff"]
    assert report["wave_starts"] == samples == []


@pytest.mark.parametrize("missing_indices", [(4,), (0, 1, 2)])
def test_filling_initial_warmup_restores_start_from_continuous_prefix(wave_market_receipts, missing_indices):
    stocks, peer, accept = wave_market_receipts
    dates = stocks["date"].unique().sort().to_list()
    gaps = [dates[index] for index in missing_indices]
    prior, today = dates[11:13]
    absent = pl.col("date").is_in(gaps) & (pl.col("symbol") == SYMBOL)
    symbols = [SYMBOL, peer]
    original, store, _ = accept(prior, 17, stocks.filter((pl.col("date") <= prior) & ~absent), symbols, publish=True)
    assert {day for day, stocks in store.waiting_wave_bars(str(prior)).items() if SYMBOL in stocks} == set(map(str, gaps))
    report, _, study = accept(today, 17, stocks.filter((pl.col("date") == today) | absent), symbols, publish=True)
    start, = report["wave_starts"]
    assert (start["date"], start["known_at"], study) == (str(today), report["cutoff"], [str(today)])
    assert store.get(str(prior)) == original


@pytest.mark.parametrize("published", [True, False])
def test_withdrawn_clue_cannot_date_another_days_start(tmp_path, private_owner, monkeypatch, published):
    bars = [*BARS[:12], (11.85, 11.30, 11.70, 100), (11.95, 11.4, 11.75, 100)]
    stocks, peer, accept = _wave_market_receipts(tmp_path, monkeypatch, bars)
    seed, first, next_day = stocks["date"].unique().sort().to_list()[11:14]
    symbols = [SYMBOL, peer]
    accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    earlier = stocks.filter(pl.col("date") == first)
    accept(first, 16, earlier, symbols)
    withdrawn = earlier.with_columns(pl.when(pl.col("symbol") == SYMBOL).then(60.).otherwise("volume").alias("volume"))
    accept(first, 17, withdrawn, symbols, publish=published)
    report, _, study = accept(next_day, 17, stocks.filter(pl.col("date") == next_day), symbols, publish=True)
    start, = report["wave_starts"]
    assert (start["evidence_date"], start["known_at"], study) == (str(next_day), report["cutoff"], [str(next_day)])


def test_old_round_invalidation_keeps_missing_bars_for_the_next_round(tmp_path, private_owner, monkeypatch):
    bars = [*BARS[:12], *((high * .9, low * .9, close * .9, volume) for high, low, close, volume in BARS)]
    stocks, peer, accept = _wave_market_receipts(tmp_path, monkeypatch, bars)
    days = stocks["date"].unique().sort().to_list()
    symbols = [SYMBOL, peer]
    seed, prior, today = days[11], days[23], days[24]
    gap = (pl.col("symbol") == SYMBOL) & pl.col("date").is_in(days[12:15])
    accept(seed, 17, stocks.filter(pl.col("date") <= seed), symbols, publish=True)
    original, store, _ = accept(prior, 17, stocks.filter(pl.col("date").is_between(seed, prior, closed="right") & ~gap),
                                symbols, publish=True)
    assert any(e["type"] == "structure_invalid" for e in original["wave_events"])
    assert set(store.waiting_wave_bars(str(prior))) == set(map(str, days[12:15]))
    report, _, study = accept(today, 17, stocks.filter((pl.col("date") == today) | gap), symbols, publish=True)
    start, = report["wave_starts"]
    assert start["round_id"].endswith(f":{days[16]}:{days[18]}")
    assert start["known_at"] == report["cutoff"] and study == [str(today)]
    assert store.get(str(prior)) == original
