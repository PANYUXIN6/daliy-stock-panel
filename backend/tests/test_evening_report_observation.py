"""Executable acceptance examples for the versioned observation contract (A1–A16)."""
from datetime import date, timedelta
from math import isclose

import polars as pl
import pytest

from app.services.evening_report_engine import EveningInputs, build_report
from app.services.evening_report_observation import (
    allocate,
    apply_observation,
    classify_role,
    dip_labels,
    stock_values,
)


@pytest.mark.parametrize("values,role", [
    ({"return_20d": .50, "identity_score": 45, "high_proximity": .85}, "true_leader"),
    ({"return_20d": .29, "identity_score": 30, "high_proximity": .60}, "fallback_leader"),
    ({"return_20d": .30, "identity_score": 0, "high_proximity": .60}, "fallback_leader"),
    ({"return_20d": .20, "return_5d": .12, "acceleration_5_20": .60, "identity_score": 0}, "emotion"),
    ({"return_20d": .20, "return_5d": .12, "acceleration_5_20": .60, "identity_score": 60}, "fallback_leader"),
    ({"return_20d": None, "identity_score": 45, "high_proximity": None}, "fallback_leader"),
    ({"return_20d": .20, "return_5d": .15, "acceleration_5_20": .75, "identity_score": None}, "emotion"),
])
def test_role_priority_and_independent_or_paths(values, role):
    result = classify_role(values)
    assert result["role"] == result["daily_role"] == role
    if values.get("identity_score") == 60:
        assert result["emotion_attribute"] is True
    if values.get("return_20d") is None or values.get("identity_score") is None:
        assert result["higher_role_pending"] is True


def test_true_leader_mismatch_is_review_not_fake_confirmed_downgrade():
    previous = {"role": "true_leader", "last_true_date": "2026-11-10"}
    result = classify_role({"return_20d": .20, "identity_score": 0, "high_proximity": .6}, previous)
    assert result["role"] == "historical_leader" and result["role_status"] == "review_pending"
    assert result["daily_role"] == "other" and result["conditions"]["true_leader"] is False
    assert result["last_true_date"] == "2026-11-10"


def test_dip_emotion_side_tag_is_reachable_but_unverified_events_never_confirm():
    values = {"return_20d": .25, "return_5d": .15, "acceleration_5_20": .60,
              "identity_score": 60, "high_proximity": .90, "space_score": 31.25,
              "amount_day": 800_000_000, "return_3d": .15, "volume_ratio_2_10": .55,
              "distance_ma10": -.02, "amplitude_ratio_2_10": 1.2,
              "rising_lows_3d": True, "distance_bull_bear": .05}
    role = classify_role(values)
    market = {"phase": "rally", "sentiment": {"classification": {
        "sentiment_state": "hot_fragile", "evidence": {"ge2_count": 5}}}}
    result = dip_labels(values, role, market, "20%")
    assert role["role"] == "fallback_leader"
    assert result["emotion_qualification"] is True
    assert result["leader_qualification"] is False
    assert result["e1"]["value"] is True and result["e2"]["value"] is True
    assert result["status"] == "none"
    assert "t4_unverified" in result["reason_codes"]
    assert result["e1"]["event_applicable"] is None
    assert dip_labels(values, role, {**market, "phase": "ebb"}, "20%")["status"] == "none"
    market["sentiment"]["classification"]["sentiment_state"] = "cold"
    assert dip_labels(values, role, market, "20%")["status"] == "none"


def test_price_derived_exact_boundaries_keep_closed_and_open_semantics():
    values = {"return_20d": .20, "return_5d": 11.2 / 10 - 1,
              "acceleration_5_20": (11.2 / 10 - 1) / .20, "identity_score": 0,
              "return_3d": .15, "volume_ratio_2_10": .55,
              "distance_ma10": 10.6 / 10 - 1, "amplitude_ratio_2_10": 1.2,
              "rising_lows_3d": True, "distance_bull_bear": 10.5 / 10 - 1}
    role = classify_role(values)
    assert role["role"] == "emotion"
    dip = dip_labels(values, role, {}, "20%")
    assert dip["e1"]["value"] is True and dip["e2"]["value"] is True
    values["return_5d"] -= .00001
    assert classify_role(values)["role"] == "other"
    values.update(return_20d=15.5 / 10 - 1, return_5d=.3, acceleration_5_20=.6)
    assert classify_role(values)["emotion_attribute"] is False  # <55% is open.


def observation_inputs():
    days = [(date(2026, 6, 1) + timedelta(days=n)).isoformat() for n in range(70)]
    prices, metadata, memberships = [], [], []
    for theme in range(5):
        for kind in range(4):
            symbol = f"{'300' if kind == 2 else '000'}{theme * 4 + kind:03}.SZ"
            metadata.append({"symbol": symbol, "name": f"股票{theme}-{kind}", "board": "chinext" if kind == 2 else "main",
                             "in_scope": True, "trading_normal": True, "regime": "20%" if kind == 2 else "10%",
                             "qualification_pause": False, "suspension_window": False})
            memberships.append({"symbol": symbol, "source": "concept", "sector_id": f"t{theme}", "sector_name": f"产业{theme}"})
            for i, day in enumerate(days):
                progress = max(0, i - 49) / 20
                total = (.51, .20, .20, .35)[kind]
                close = 10 * (1 + total * progress)
                if kind == 2 and i >= 64:
                    close = 12 / 1.15 * (1 + .15 * (i - 64) / 5)
                prices.append({"symbol": symbol, "date": day, "close": close, "open": close * .99,
                               "high": close * 1.01, "low": close * .98, "raw_close": close,
                               "raw_open": close * .99, "raw_high": close * 1.01, "raw_low": close * .98,
                               "ma5": close * .9, "ma10": close * .99, "ma20": close * 1.05,
                               "bull_bear_line": close, "atr_14": close * .01, "high_60d": close * 1.01,
                               "amount": 50_000_000., "volume": 5_000_000., "formal": True,
                               "signal_limit_up": kind in (0, 1) and i in (65, 67, 69),
                               "signal_limit_down": False, "consecutive_limit_ups": 1 if kind in (0, 1) and i == 69 else 0})
    return EveningInputs(trade_date=days[-1], calendar=days, prices=pl.DataFrame(prices), metadata=pl.DataFrame(metadata),
                         memberships=pl.DataFrame(memberships), benchmark=pl.DataFrame({"date": days, "close": [100.] * 70, "formal": [True] * 70}),
                         market={"phase": "ebb", "state": "weak", "ge2_count": 0})


def test_five_themes_fifteen_unique_rows_on_first_report_despite_low_amount_and_broken_position():
    inputs = observation_inputs()
    report = build_report(inputs)
    selected = [r for r in report["records"] if r["selected"]]
    assert len(set(report["selected"])) == len(selected) == 15
    for theme in range(5):
        rows = [r for r in selected if r["observation"]["primary_theme"]["id"] == f"concept:t{theme}"]
        assert [r["observation"]["role"] for r in sorted(rows, key=lambda r: r["observation"]["rank"])] == ["true_leader", "fallback_leader", "emotion"]
    assert all(r["qualification"]["amount_ok"] is False for r in selected)
    assert all(r["position"]["value"] is False for r in selected)
    assert all(r["observation"]["dip"]["status"] == "none" for r in selected)
    assert all(r["observation"]["variables"]["return_20d"]["as_of"] == inputs.trade_date for r in selected)
    from app.services.evening_report_store import report_view
    view = report_view(report)
    assert len(view["records"]) == 15 < len(report["records"])
    assert all("members" not in s["observation"] for s in view["sectors"])


def test_theme_membership_fixed_at_cutoff_full_member_limit_and_independent_missingness():
    inputs = observation_inputs()
    # Only one classification observation is provided; five historical membership snapshots are unnecessary.
    extra = [{"symbol": f"external{n}", "source": "concept", "sector_id": "宽概念", "sector_name": "宽概念"} for n in range(121)]
    extra += [{"symbol": "000000.SZ", "source": "concept", "sector_id": "新疆振兴", "sector_name": "新疆振兴"},
              {"symbol": "missing", "source": "concept", "sector_id": "缺数主题", "sector_name": "缺数主题"}]
    inputs.memberships = pl.concat([inputs.memberships, pl.DataFrame(extra)])
    report = build_report(inputs)
    assert len(report["selected"]) == 15
    assert report["statistics"]["theme_count"] == 5
    by_id = {s["sector_id"]: s["observation"] for s in report["sectors"]}
    assert by_id["宽概念"]["member_count"] == 121 and by_id["宽概念"]["exclusion"]
    assert by_id["新疆振兴"]["exclusion"]
    assert by_id["缺数主题"]["strength"] is None
    assert isclose(by_id["t0"]["strength"], 2 * (1 + 2 ** (-2 / 2.5) + 2 ** (-4 / 2.5)))


def test_primary_theme_is_stable_and_overlap_does_not_borrow_other_quota():
    inputs = observation_inputs()
    shared = inputs.memberships.filter(pl.col("sector_id") == "t0").with_columns(
        pl.lit("t1").alias("sector_id"), pl.lit("产业1").alias("sector_name"))
    inputs.memberships = pl.concat([inputs.memberships, shared])
    report = build_report(inputs)
    assert len(set(report["selected"])) == len(report["selected"]) == 12
    first = next(r for r in report["records"] if r["symbol"] == "000000.SZ")
    assert first["observation"]["primary_theme"]["id"] == "concept:t1"
    assert [t["id"] for t in first["observation"]["related_themes"]] == ["concept:t0"]
    assert not any(r["selected"] and r["observation"]["primary_theme"]["id"] == "concept:t0" for r in report["records"])


def test_main_board_emotion_is_not_selected_and_same_role_tie_prefers_chinext():
    def record(symbol, role, board):
        return {"symbol": symbol, "selected": False, "observation": {"role": role, "daily_role": role, "role_status": "qualified", "values": {"identity_score": 30},
                "board": board, "eligibility": {"value": True}, "primary_theme": {"id": "t"}}}
    records = [record("a", "fallback_leader", "main"), record("c", "fallback_leader", "chinext"),
               record("b", "fallback_leader", "main"), record("e", "emotion", "main")]
    assert allocate(records, [{"id": "t"}]) == ["c", "a"]


def test_window_endpoints_halts_and_nonoverlapping_volume_ratios():
    inputs = observation_inputs()
    rows = {r["date"]: r for r in inputs.prices.filter(pl.col("symbol") == "000000.SZ").to_dicts()}
    for day in inputs.calendar[-12:-2]:
        rows[day]["volume"] = 100
    for day in inputs.calendar[-2:]:
        rows[day]["volume"] = 50
    values = stock_values(rows, inputs.calendar, set())
    assert values["volume_ratio_2_10"] == .5
    assert values["identity_score"] == 68
    assert values["return_20d"] == pytest.approx(.51)
    assert stock_values(rows, inputs.calendar, {inputs.calendar[-4]})["volume_ratio_2_10"] is None
    assert stock_values(rows, inputs.calendar, {inputs.calendar[-21]})["return_20d"] is None
    short = stock_values(rows, inputs.calendar[-12:], set())
    assert short["return_20d"] is None and short["high_proximity"] is None
    rows[inputs.calendar[-1]]["close"] = rows[inputs.calendar[-21]]["close"] * .9
    assert stock_values(rows, inputs.calendar, set())["acceleration_5_20"] is None


def test_theme_strength_needs_five_price_days_but_no_prior_classifications():
    inputs = observation_inputs()
    inputs.calendar = inputs.calendar[-5:]
    # Every member seals up each of the five days: no prior close needed for U.
    inputs.prices = inputs.prices.filter(pl.col("date").is_in(inputs.calendar)).with_columns(pl.lit(True).alias("signal_limit_up"))
    result = apply_observation(inputs, [], [], {}, inputs.calendar, lambda: None)
    assert len(result["themes"]) == 5
    assert all(t["complete"] for t in result["themes"])
    report = build_report(inputs)
    assert report["statistics"]["theme_count"] == 5
    assert report["selected"] == []  # 只有5日历史不足以证明20日角色条件。


def test_resumed_member_contributes_verified_daily_return_without_moving_window_endpoints():
    inputs = observation_inputs()
    symbol = "000001.SZ"
    peer = pl.col("symbol") == symbol
    # The last traded close lies outside the regular 60-day indicator window.
    halted = inputs.calendar[-62:-1]
    inputs.prices = inputs.prices.filter(~(peer & pl.col("date").is_in(halted))).with_columns(
        pl.when(peer).then(pl.when(pl.col("date") == inputs.trade_date).then(10.7).otherwise(10.0))
        .otherwise("close").alias("close"),
        pl.when(peer).then(False).otherwise("signal_limit_up").alias("signal_limit_up"),
    )
    inputs.provenance["suspensions"] = [{"symbol": symbol, "date": day, "confirmed": True} for day in halted]
    report = build_report(inputs)
    theme = next(s["observation"] for s in report["sectors"] if s["sector_id"] == "t0")
    rows = {row["date"]: row for row in inputs.prices.filter(peer).to_dicts()}
    values = stock_values(rows, inputs.calendar, set(halted))
    assert theme["complete"] and theme["daily"][-1]["strong"] == 1
    assert "000000.SZ" in report["selected"]
    assert values["change_pct"] == pytest.approx(.07)
    assert values["return_5d"] is None and values["close_vs_previous_non_decreasing"] is None

    inputs.provenance["suspensions"].pop()
    report = build_report(inputs)
    theme = next(s["observation"] for s in report["sectors"] if s["sector_id"] == "t0")
    assert not theme["complete"] and "000000.SZ" not in report["selected"]
    assert {"symbol": symbol, "date": inputs.trade_date, "reason": "日涨幅端点缺失"} in theme["missing"]


def test_identity_only_role_survives_missing_20day_return_endpoint_and_60day_high():
    inputs = observation_inputs()
    inputs.calendar = inputs.calendar[-20:]
    report = build_report(inputs)
    selected = [r for r in report["records"] if r["selected"]]
    assert len(selected) == 10
    assert all(r["observation"]["role"] == "fallback_leader" for r in selected)
    assert all(r["observation"]["higher_role_pending"] for r in selected)
    assert all(r["observation"]["values"]["return_20d"] is None for r in selected)


def test_untrusted_current_price_excludes_only_affected_path_and_future_does_not_change_report():
    inputs = observation_inputs()
    baseline = build_report(inputs)
    future = inputs.prices.filter(pl.col("date") == inputs.trade_date).with_columns(
        pl.lit("2027-01-01").alias("date"), pl.lit(99999.).alias("high"), pl.lit(.01).alias("low"))
    inputs.prices = pl.concat([inputs.prices, future])
    assert build_report(inputs) == baseline
    inputs.prices = inputs.prices.with_columns(pl.when((pl.col("symbol") == "000000.SZ") & (pl.col("date") == inputs.trade_date))
                                               .then(False).otherwise(pl.col("formal")).alias("formal"))
    result = build_report(inputs)
    assert "000000.SZ" not in result["selected"]
    assert result["statistics"]["theme_count"] == 4


def test_missing_historical_fill_bar_does_not_invent_a_known_open_gap():
    from app.services.evening_report_observation import _position_details

    inputs = observation_inputs()
    symbol = "000000.SZ"
    inputs.prices = inputs.prices.filter(pl.col("symbol") == symbol).with_columns(
        pl.when(pl.col("date") < inputs.calendar[40]).then(12.).otherwise(17.).alias("high"),
        pl.when(pl.col("date") < inputs.calendar[40]).then(9.)
        .when(pl.col("date") == inputs.calendar[-7]).then(11.).otherwise(14.).alias("low"),
        pl.when(pl.col("date") < inputs.calendar[40]).then(10.).otherwise(15.).alias("close"))
    row = {"symbol": symbol, "name": symbol, "selected": True, "observation": {"values": {}, "board": "main"},
           "risk": {"judgement": {"inputs": {}}}, "position": {}}
    _position_details(inputs, [row], inputs.calendar)
    assert row["observation"]["price_gaps"]["status"] == "known"
    assert row["observation"]["price_gaps"]["intervals"] == []
    inputs.prices = inputs.prices.filter(pl.col("date") != inputs.calendar[-7])
    _position_details(inputs, [row], inputs.calendar)
    assert row["observation"]["price_gaps"]["status"] == "unknown"
    assert row["observation"]["price_gaps"]["intervals"] == []


def test_same_role_lead_metric_fallback_is_frozen_for_whole_competition_group():
    def row(symbol, r20, score, source):
        return {"symbol": symbol, "selected": False, "observation": {
            "role": "fallback_leader", "daily_role": "fallback_leader", "role_status": "qualified", "board": "main",
            "values": {"return_20d": r20, "identity_score": 30}, "primary_theme": {"id": "t"},
            "eligibility": {"value": True}, "theme_lead_evidence": {"score": score, "source": source},
        }}

    records = [row("A", .40, None, None), row("B", .35, 80, {"version": "v1"}), row("C", .30, 95, {"version": "v2"})]
    assert allocate(records, [{"id": "t", "as_of": "2026-11-18"}]) == ["A", "B"]
    for rank, record in enumerate(records, 1):
        lead = record["observation"]["theme_lead_evidence"]
        assert lead["metric"] == "return_20d_rank"
        assert lead["reason_codes"] == ["lead_fallback_missing", "lead_fallback_inconsistent"]
        assert lead["return_20d_rank"] == rank


def test_global_quota_rank_keeps_theme_order_instead_of_rescoring_between_themes():
    records = [{"symbol": symbol, "selected": False, "observation": {
        "role": "fallback_leader", "daily_role": "fallback_leader", "board": "main",
        "values": {"return_20d": score, "identity_score": score},
        "primary_theme": {"id": theme}, "eligibility": {"value": True},
    }} for symbol, theme, score in [("A", "t1", .3), ("B", "t2", .4), ("C", "t3", .5)]]
    assert allocate(records, [{"id": theme} for theme in ("t1", "t2", "t3")]) == ["A", "B", "C"]
    assert [row["observation"]["rank_global_role"] for row in records] == [1, 2, 3]


def test_second_wave_concentration_only_uses_volume_for_the_second_segment():
    from test_evening_report_wave import BARS

    inputs = observation_inputs()
    third_pending, second_started, second_pending = "000000.SZ", "000001.SZ", "000004.SZ"
    paths = {
        third_pending: [*BARS[:12], (*BARS[12][:3], 50), (12., 11.7, 11.8, 65),
                        (11.9, 11.6, 11.7, 65), (12.5, 11.8, 12.3, None)],
        second_started: BARS,
        second_pending: [*BARS[:-1], (*BARS[-1][:3], None)],
    }
    inputs.metadata = inputs.metadata.filter(pl.col("symbol").is_in(list(paths)))
    inputs.memberships = inputs.memberships.filter(pl.col("symbol").is_in(list(paths)))
    rows = []
    for symbol, bars in paths.items():
        by_date = dict(zip(inputs.calendar[-len(bars):], bars, strict=True))
        for row in inputs.prices.filter(pl.col("symbol") == symbol).iter_rows(named=True):
            high, low, close, volume = by_date.get(row["date"], (10.2, 10., 10.1, 100))
            rows.append({**row, "high": high, "low": low, "open": low, "close": close, "ma10": 11.7,
                         "raw_high": high, "raw_low": low, "raw_open": low, "raw_close": close,
                         "volume": volume, "wave_volume": volume, "wave_amount": row["amount"], "wave_formal": True})
    inputs.prices = pl.DataFrame(rows)
    inputs.wave_factors = pl.DataFrame(schema={"symbol": pl.String, "trade_date": pl.String, "ex_factor": pl.Float64})
    inputs.cutoff = f"{inputs.trade_date}T17:00:00+08:00"
    inputs.wave_first_acceptances = dict.fromkeys(paths, inputs.cutoff)
    inputs.provenance["wave_factor_source"] = ["accepted"]

    report = build_report(inputs)
    assert [(event["security_id"], event["segment_index"]) for event in report["wave_starts"]] == [(second_started, 2)]
    waves = {record["symbol"]: record["observation"]["wave"] for record in report["records"]}
    assert waves[third_pending]["second_wave_started"] is False
    assert waves[third_pending]["second_wave_confirmed_by_new_high"]["date"] < inputs.trade_date
    assert waves[third_pending]["start_evaluable"] is True
    assert waves[second_pending]["start_evaluable"] is False
    assert report["audit"]["start_concentration"] == {
        "segment_index": 2, "pool_count": 3, "start_evaluable_count": 2, "start_unknown_count": 1,
        "new_start_count": 1, "known_new_starts_per_pool": 1 / 3, "known_new_starts_per_evaluable": 1 / 2,
    }


def test_unknown_streak_preserves_limit_count_and_theme_strength():
    inputs = observation_inputs()
    extra = inputs.memberships.filter(pl.col("sector_id") == "t4").with_columns(pl.lit("t5").alias("sector_id"))
    inputs.memberships = pl.concat([inputs.memberships, extra])
    baseline = build_report(inputs)
    inputs.prices = inputs.prices.with_columns(
        pl.when((pl.col("symbol") == "000000.SZ") & (pl.col("date") == inputs.trade_date))
        .then(None).otherwise("consecutive_limit_ups").alias("consecutive_limit_ups"))
    report = build_report(inputs)
    before, after = (next(t for t in r["observation_themes"] if t["id"] == "concept:t0") for r in (baseline, report))
    assert after["daily"][-1] == {**before["daily"][-1], "max_consecutive": None}
    assert after["daily"][-1]["limit_up"] == 2 and after["strength"] == before["strength"]
    assert before["selected_theme"] and after["rank"] == before["rank"] and after["selected_theme"]
