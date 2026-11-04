from datetime import date, timedelta

import polars as pl

from app.services.evening_report_engine import EveningInputs, build_report


def inputs_at(day=65, *, suspended=(), missing_atr=(), amount_missing=(), weak_amount=()):
    dates = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(80)]
    prices, metadata, members = [], [], []
    for n in range(24):
        symbol = f"stock{n:02}"
        growth = 0.009 if n == 0 else 0.005 if n == 1 else 0.0003 * (24 - n)
        for i, d in enumerate(dates[: day + 1]):
            c = 10 * (1 + growth) ** i
            prices.append(
                dict(
                    symbol=symbol,
                    date=d,
                    close=c,
                    high=c * 1.01,
                    amount=None
                    if symbol in amount_missing and i == day
                    else 50_000_000
                    if symbol in weak_amount
                    else 200_000_000 + n * 1_000_000,
                    ma5=c * 0.99,
                    ma10=c * 0.98,
                    ma20=c * 0.97,
                    atr_14=None if symbol in missing_atr else c * 0.02,
                    high_60d=c,
                    raw_open=c * 0.99,
                    raw_high=c * 1.01,
                    raw_low=c * 0.98,
                    raw_close=c,
                    signal_limit_up=n < 3,
                    signal_limit_down=False,
                    consecutive_limit_ups=3 if n < 3 else 0,
                    formal=True,
                )
            )
        metadata.append(
            dict(
                symbol=symbol,
                name=symbol,
                in_scope=True,
                trading_normal=symbol not in suspended,
                regime="10%",
                suspension_window=symbol in suspended,
                qualification_pause=symbol in suspended,
            )
        )
        members.append(dict(source="concept", sector_id="a", sector_name="产业A", symbol=symbol))
    return EveningInputs(
        trade_date=dates[day],
        next_trade_date=dates[day + 1],
        calendar=dates[: day + 1],
        prices=pl.DataFrame(prices),
        metadata=pl.DataFrame(metadata),
        memberships=pl.DataFrame(members),
        benchmark=pl.DataFrame(
            {"date": dates[: day + 1], "close": [100.0] * (day + 1), "formal": [True] * (day + 1)}
        ),
        market={"state": "strong", "phase": "rally", "promo_rate": 0.3, "seal_rate": 0.8},
    )


def test_full_input_cold_start_then_confirm_and_counts():
    history = []
    for day in (65, 66, 67):
        result = build_report(inputs_at(day), history)
        history.append(result)
    assert history[0]["sectors"][0]["round"]["status"] == "probing"
    assert history[1]["sectors"][0]["round"]["status"] == "active"
    assert history[0]["state"]["paths"].keys() == result["state"]["paths"].keys()
    stock = next(r for r in result["records"] if r["symbol"] == "stock00")
    trend = next(i for i in stock["paths"][0]["identities"] if i["type"] == "trend")
    assert trend["status"] == "confirmed"
    assert trend["intervals"][0]["confirmed_at"] == inputs_at(67).trade_date
    assert (
        sum(result["statistics"]["purpose_counts"].values()) == result["statistics"]["report_count"]
    )
    assert len(result["selected"]) <= 2


def test_amount_coverage_unknown_not_failed():
    result = build_report(inputs_at(amount_missing=("stock23",), weak_amount=("stock22",)))
    count = result["statistics"]["amount_coverage"][0]
    assert (
        count["n_pre"],
        count["n_pass"],
        count["n_fail"],
        count["n_unknown"],
        count["n_decidable"],
    ) == (24, 22, 1, 1, 23)
    assert count["coverage_rate"] == 22 / 24
    assert count["decidable_pass_rate"] == 22 / 23


def test_suspension_keeps_identity_interval_and_membership():
    history = []
    for day in (65, 66, 67):
        history.append(build_report(inputs_at(day), history))
    result = build_report(inputs_at(68, suspended=("stock00",)), history)
    stock = next(r for r in result["records"] if r["symbol"] == "stock00")
    trend = next(i for i in stock["paths"][0]["identities"] if i["type"] == "trend")
    assert trend["valid"] is None and trend["intervals"][0]["ended_at"] is None
    assert stock["purpose"] == "风险暂停"
    assert result["sectors"][0]["member_count"] == 23


def test_missing_atr_keeps_complete_trend_but_blocks_selection():
    history = []
    for day in (65, 66, 67):
        history.append(build_report(inputs_at(day), history))
    result = build_report(inputs_at(68, missing_atr=("stock00",)), history)
    stock = next(r for r in result["records"] if r["symbol"] == "stock00")
    assert stock["purpose"] == "已确认核心跟踪"
    assert stock["risk"]["risk_now"] is None and not stock["selected"]
    assert any("风险证据不足" in reason for reason in stock["limitations"])


def test_historical_snapshot_not_valid_formal_warmup():
    inputs = inputs_at()
    inputs.prices = inputs.prices.with_columns(
        pl.when((pl.col("symbol") == "stock00") & (pl.col("date") == inputs.calendar[0]))
        .then(False)
        .otherwise(pl.col("formal"))
        .alias("formal")
    )
    result = build_report(inputs)
    evidence = next(r for r in result["calculations"] if r["symbol"] == "stock00")
    assert evidence["qualification"]["eligible"] is None
    assert "历史正式性" in evidence["qualification"]["unknown_reasons"]


def test_recovery_first_true_day_is_still_observation_not_selected():
    history = []
    for day in (65, 66, 67):
        history.append(build_report(inputs_at(day), history))
    weak = inputs_at(68, weak_amount=("stock00",))
    history.append(build_report(weak, history))
    recovering = build_report(inputs_at(69), history)
    stock = next(r for r in recovering["records"] if r["symbol"] == "stock00")
    assert any(i["status"] == "divergent" for i in stock["paths"][0]["identities"])
    assert not stock["selected"]
    history.append(recovering)
    recovered = build_report(inputs_at(70), history)
    stock = next(r for r in recovered["records"] if r["symbol"] == "stock00")
    assert any(i["status"] == "confirmed" for i in stock["paths"][0]["identities"])


def test_missing_board_component_preserves_complete_trend_group_ranking():
    inputs = inputs_at()
    inputs.prices = inputs.prices.with_columns(
        pl.when(pl.col("symbol") == "stock23")
        .then(None)
        .otherwise(pl.col("consecutive_limit_ups"))
        .alias("consecutive_limit_ups")
    )
    result = build_report(inputs)
    scores = result["sectors"][0]["scores"]
    assert all(f["rank_b"] is None for f in scores.values())
    assert all(f["rank_t"] is not None for f in scores.values())
    assert len(scores) == 24


def test_suspension_before_group_keeps_six_but_five_is_unknown_not_round_exit():
    for n, expected in ((7, True), (6, False)):
        inputs = inputs_at(suspended=("stock00",))
        inputs.memberships = inputs.memberships.head(n)
        result = build_report(inputs)
        sector = result["sectors"][0]
        assert sector["member_count"] == n - 1
        assert sector["coverage"]["computable"] is expected
        if not expected:
            assert sector["features"]["X20"] is None
            assert sector["round"]["status"] != "ended"


def test_market_percentile_group_minimum_does_not_split_sector_ranking():
    inputs = inputs_at()
    # 5 of a different regime stay in the 24-member comparison group; their market
    # percentile lacks 20 samples, so the complete sector ranking is unavailable.
    inputs.metadata = inputs.metadata.with_columns(
        pl.when(pl.col("symbol") >= "stock19")
        .then(pl.lit("20%"))
        .otherwise(pl.lit("10%"))
        .alias("regime")
    )
    result = build_report(inputs)
    sector = result["sectors"][0]
    assert sector["member_count"] == 24 and sector["regime_counts"]["20%"] == 5
    assert all(row["rank_t"] is None for row in sector["scores"].values())
    assert all(row["L"] is not None for row in sector["scores"].values())


def test_gap_does_not_bridge_three_day_confirmation_and_keeps_original_history():
    first = build_report(inputs_at(65))
    third = build_report(inputs_at(67), [first])
    assert not any(i["status"] == "confirmed" for i in third["state"]["identities"].values())
    assert first["sectors"][0]["round"]["status"] == "probing"
    assert third["statistics"]["publication_frequency"]["report_days"] == 2


def catchup_inputs(day, *, suspended=()):
    inputs = inputs_at(day, suspended=suspended)
    rows = []
    for n in range(24):
        symbol = f"stock{n:02}"
        c = 10.0
        for i, d in enumerate(inputs.calendar):
            growth = 0.02 if n == 0 else 0.018 if n == 1 else 0.005
            if n == 23:
                growth = 0.001 if i < 69 else 0.04 if i == 69 else 0.08 if i == 70 else 0.001
            c *= 1 + growth
            rows.append(
                dict(
                    symbol=symbol,
                    date=d,
                    close=c,
                    high=c * 1.01,
                    amount=200_000_000.0,
                    ma5=c * 0.99,
                    ma10=c * 0.98,
                    ma20=c * 0.97,
                    atr_14=c * 0.03,
                    high_60d=c,
                    raw_open=c * 0.99,
                    raw_high=c * 1.01,
                    raw_low=c * 0.98,
                    raw_close=c,
                    signal_limit_up=n < 3,
                    signal_limit_down=False,
                    consecutive_limit_ups=3 if n < 3 else 0,
                    formal=True,
                )
            )
    inputs.prices = pl.DataFrame(rows)
    return inputs


def test_full_chain_catchup_multiple_references_and_suspension_unknown():
    history = []
    for day in range(65, 72):
        history.append(build_report(catchup_inputs(day), history))
    candidate = next(r for r in history[-1]["records"] if r["symbol"] == "stock23")
    path = candidate["paths"][0]
    assert path["catchup"]["value"] is True
    assert path["catchup"]["k"] == catchup_inputs(70).trade_date
    assert path["catchup"]["sustained"] is True
    assert len({r["symbol"] for r in path["catchup"]["references"] if r["value"] is True}) == 2
    one_suspended = build_report(catchup_inputs(72, suspended=("stock00",)), history)
    catchup = next(r for r in one_suspended["records"] if r["symbol"] == "stock23")["paths"][0][
        "catchup"
    ]
    assert catchup["value"] is True
    assert any(r["value"] is None for r in catchup["references"] if r["symbol"] == "stock00")
    both_suspended = build_report(catchup_inputs(72, suspended=("stock00", "stock01")), history)
    catchup = next(r for r in both_suspended["records"] if r["symbol"] == "stock23")["paths"][0][
        "catchup"
    ]
    assert catchup["value"] is None
    assert (
        history[-1]["state"]["paths"][path["id"]]["catchup"]["value"] is True
    )  # Original publication unchanged.


def test_missing_metadata_for_existing_identity_is_not_scope_exit():
    history = []
    for day in (65, 66, 67):
        history.append(build_report(inputs_at(day), history))
    inputs = inputs_at(68)
    inputs.metadata = inputs.metadata.filter(pl.col("symbol") != "stock00")
    result = build_report(inputs, history)
    record = next(r for r in result["records"] if r["symbol"] == "stock00")
    assert record["purpose"] == "证据不足"
    assert not any(
        e["type"] == "scope_exit" and e.get("symbol") == "stock00" for e in result["events"]
    )
    assert any(i["valid"] is None for i in record["paths"][0]["identities"])


def test_missing_limit_history_keeps_trend_computable_and_price_risk_unknown():
    inputs = inputs_at()
    inputs.prices = inputs.prices.drop(
        "signal_limit_up", "signal_limit_down", "consecutive_limit_ups"
    )
    result = build_report(inputs)
    scores = result["sectors"][0]["scores"]
    assert all(f["rank_t"] is not None for f in scores.values())
    assert all(f["rank_b"] is None for f in scores.values())


def test_small_positive_atr_structure_rule_scale_invariant_and_not_daily_drop():
    inputs = inputs_at()
    inputs.prices = inputs.prices.with_columns(
        pl.when(pl.col("symbol") == "stock00")
        .then(pl.col("close") * 0.0001)
        .otherwise(pl.col("atr_14"))
        .alias("atr_14"),
        pl.when(pl.col("symbol") == "stock00")
        .then(pl.col("close") * 1.001)
        .otherwise(pl.col("ma20"))
        .alias("ma20"),
    )
    first = build_report(inputs)
    risk = first["state"]["risks"]["stock00"]["judgement"]
    assert risk["inputs"]["跌破均线2ATR"] is True
    fields = ["close", "high", "ma5", "ma10", "ma20", "atr_14", "high_60d"]
    inputs.prices = inputs.prices.with_columns([pl.col(c) * 0.01 for c in fields])
    second = build_report(inputs)
    assert second["state"]["risks"]["stock00"]["judgement"]["inputs"]["跌破均线2ATR"] is True


def test_atr_zero_unknown_does_not_override_known_limit_down():
    inputs = inputs_at()
    inputs.prices = inputs.prices.with_columns(
        pl.lit(0.0).alias("atr_14"), pl.lit(True).alias("signal_limit_down")
    )
    result = build_report(inputs)
    risk = result["state"]["risks"]["stock00"]["judgement"]
    assert risk["value"] is True and risk["inputs"]["跌破均线2ATR"] is None


def test_bounded_history_matches_full_history_and_publication_counters():
    history = []
    for day in range(60, 74):
        history.append(build_report(catchup_inputs(day), history[-7:]))
    full = build_report(catchup_inputs(74), history)
    bounded = build_report(catchup_inputs(74), history[-7:])
    assert bounded == full
    assert full["statistics"]["publication_frequency"]["report_days"] == 15
    start = next(
        value for key, value in full["state"]["starts"].items() if key.startswith("stock23|")
    )
    assert start["verified"] is True


def test_confirmed_halted_member_without_price_row_is_known_not_limit_up():
    inputs = inputs_at(suspended=("stock23",))
    inputs.prices = inputs.prices.filter(
        ~((pl.col("symbol") == "stock23") & (pl.col("date") == inputs.trade_date))
    )
    result = build_report(inputs)
    assert result["sectors"][0]["ecology"]["complete_ranking"] is True
    assert result["sectors"][0]["today_top5"] is True


def test_observation_association_does_not_lock_out_first_formal_active_sector():
    history = []
    for day in (65, 66, 67, 68):
        inputs = inputs_at(day)
        inputs.prices = inputs.prices.with_columns(
            pl.when(pl.col("symbol") == "stock03")
            .then(True)
            .otherwise(pl.col("signal_limit_up"))
            .alias("signal_limit_up"),
            pl.when(pl.col("symbol") == "stock03")
            .then(3.0)
            .otherwise(pl.col("consecutive_limit_ups"))
            .alias("consecutive_limit_ups"),
        )
        a_symbols = ["stock00", "stock19", "stock20", "stock21", "stock22", "stock23"]
        b_symbols = [f"stock{n:02}" for n in range(1, 19)] + (["stock00"] if day == 68 else [])
        inputs.memberships = pl.DataFrame(
            [
                {"source": "concept", "sector_id": sector, "sector_name": sector, "symbol": symbol}
                for sector, symbols in (("a", a_symbols), ("b", b_symbols))
                for symbol in symbols
            ]
        )
        history.append(build_report(inputs, history))
    before = history[-2]["state"]["associations"]["stock00|concept"]
    after = history[-1]["state"]["associations"]["stock00|concept"]
    assert before["sector"] == "concept:a" and not before["formal"]
    assert after["sector"] == "concept:b" and after["formal"]
    assert before["path_id"] != after["path_id"]
    stock = next(r for r in history[-1]["records"] if r["symbol"] == "stock00")
    assert not any(i["valid"] is True for i in stock["paths"][0]["identities"])


def test_quota_round_robin_industry_supplement_and_catchup_cap():
    from app.services.evening_report_engine import _select

    sectors, records = {}, []
    for source, sid in (("concept", "a"), ("concept", "b"), ("industry", "i")):
        sectors[sid] = {
            "id": sid,
            "top5_count": 1,
            "ecology": {"rank": 1},
            "features": {"X20": 0.1},
        }
        for n in range(3):
            symbol = f"{sid}{n}"
            kind = "board" if n == 0 else "trend" if n == 1 else "catchup"
            path = {
                "source": source,
                "sector_id": sid,
                "selection": {"value": True},
                "selection_role": kind,
                "selection_types": [kind],
                "selection_by_type": {kind: {"role": kind}},
                "features": {"rank_b": n + 1, "rank_t": n + 1, "rank_e": n + 1},
            }
            records.append({"symbol": symbol, "paths": [path]})
    assert _select(records, sectors) == ["a0", "b0", "a1", "b1", "i0"]
    # No formal cores; catch-up candidates remain capped at one across source lists.
    for r in records:
        p = r["paths"][0]
        p.update(
            selection_role="catchup",
            selection_types=["catchup"],
            selection_by_type={"catchup": {"role": "catchup"}},
        )
    assert len(_select(records, sectors)) == 1


def test_failed_probes_do_not_accumulate_unconfirmed_forward_identities():
    import copy

    history = []
    original = None
    for day in range(65, 77):
        inputs = inputs_at(day)
        inputs.benchmark = pl.DataFrame(
            {
                "date": inputs.calendar,
                "close": [1000.0 if n % 2 == 0 else 100.0 for n in range(day + 1)],
                "formal": [True] * (day + 1),
            }
        )
        report = build_report(inputs, history[-7:])
        assert len(report["state"]["identities"]) <= 48
        paths = {a["path_id"] for a in report["state"]["associations"].values()}
        assert all(
            key.rsplit("|", 1)[0] in paths
            for key, item in report["state"]["identities"].items()
            if not item["intervals"]
        )
        history.append(report)
        if original is None:
            original = copy.deepcopy(report)
    assert history[0] == original


def test_dual_identity_second_slot_reports_actual_complementary_type():
    from app.services.evening_report_engine import _select

    sectors = {"a": {"id": "a", "top5_count": 1, "ecology": {"rank": 1}, "features": {"X20": 0.1}}}
    records = []
    for n in range(2):
        path = {
            "source": "concept",
            "sector_id": "a",
            "selection": {"value": True},
            "selection_role": "board",
            "selection_types": ["board", "trend"],
            "selection_by_type": {"board": {"role": "board"}, "trend": {"role": "trend"}},
            "features": {"rank_b": n + 1, "rank_t": n + 1},
        }
        records.append({"symbol": str(n), "paths": [path]})
    assert _select(records, sectors) == ["0", "1"]
    assert records[1]["paths"][0]["selection_type"] == "trend"
    assert records[1]["paths"][0]["selection_role"] == "trend"


def test_candidate_unknown_does_not_restart_candidate_stage():
    first = build_report(inputs_at(65))
    missing = inputs_at(66)
    missing.prices = missing.prices.with_columns(
        pl.when(pl.col("symbol") == "stock00").then(None).otherwise(pl.col("ma20")).alias("ma20")
    )
    second = build_report(missing, [first])
    third = build_report(inputs_at(67), [first, second])
    key = next(key for key in third["state"]["candidate_stages"] if key.startswith("stock00|"))
    stage = third["state"]["candidate_stages"][key]
    assert stage["candidate_first_entered_at"] == first["trade_date"]
    assert stage["new_today"] is False
    assert second["state"]["candidate_stages"][key]["unverified"] is True


def test_scope_version_change_does_not_borrow_previous_confirmation():
    history = []
    for day in (65, 66):
        history.append(build_report(inputs_at(day), history))
    changed = inputs_at(67)
    changed.provenance["scope_version"] = "different-scope-v2"
    result = build_report(changed, history)
    assert result["scope_version"] == "different-scope-v2"
    assert result["sectors"][0]["round"]["status"] == "probing"
    assert not any(i["valid"] is True for i in result["state"]["identities"].values())
    assert result["statistics"]["publication_frequency"]["report_days"] == 1


def test_observation_hints_do_not_block_complete_core_paths_or_reset_identity():
    from app.services.evening_report_rules import SELECTION_POLICY_VERSION
    from app.services.evening_report_store import report_view

    history = []
    for day in (65, 66, 67):
        previous = build_report(inputs_at(day), history)
        if day == 65:
            previous.pop("selection_policy_version")
        else:
            previous["selection_policy_version"] = "observation-v2"
        history.append(previous)
    baseline = build_report(inputs_at(68), history)
    assert "stock00" in baseline["selected"]
    baseline_identity = baseline["state"]["identities"]
    for market, phase_reason, ongoing, overheated in (
        ({}, "no_rule_match", False, False),
        ({}, "pending_confirmation", False, False),
        ({}, None, True, False),
        ({}, None, False, True),
        ({}, "pending_confirmation", True, True),
        ({"state": "weak"}, None, False, False),
        ({"state": "lean_weak"}, None, False, False),
        ({"promo_rate": 0.13, "seal_rate": 0.57}, None, False, False),
        ({"phase": "ebb"}, None, False, False),
        ({"phase": "ice"}, None, False, False),
        ({"state": "weak", "promo_rate": 0.13, "seal_rate": 0.57}, "pending_confirmation", True, True),
        ({"state": "lean_weak", "phase": "ebb"}, None, True, True),
        ({"state": "weak", "phase": "ice"}, None, True, True),
        ({"promo_rate": None, "seal_rate": 0.8}, None, False, False),
    ):
        value = inputs_at(68)
        value.market.update(market)
        if phase_reason:
            value.market.update(phase="unclassified", phase_reason=phase_reason)
        if ongoing:
            value.benchmark = value.benchmark.with_columns(
                pl.when(pl.col("date") == value.trade_date).then(98.0).otherwise(pl.col("close")).alias("close")
            )
        if overheated:
            value.prices = value.prices.with_columns(
                pl.when((pl.col("symbol") == "stock00") & (pl.col("date") == value.trade_date))
                .then(pl.col("close") * 0.9).otherwise(pl.col("ma5")).alias("ma5")
            )
        result = build_report(value, history)
        stock = next(row for row in result["records"] if row["symbol"] == "stock00")
        assert stock["selected"] and "stock00" in result["selected"]
        assert stock["purpose"] == "已确认核心跟踪"
        assert result["state"]["identities"] == baseline_identity
        assert result["selection_policy_version"] == SELECTION_POLICY_VERSION
        assert stock["position"]["observation_eligibility"]["value"] is True
        assert result["market"]["policy"]["value"] is True
        expected_hints = []
        if market.get("state") in {"weak", "lean_weak"}:
            expected_hints.append("市场弱势" if market["state"] == "weak" else "市场偏弱")
        if market.get("phase") in {"ebb", "ice"}:
            expected_hints.append("短线退潮" if market["phase"] == "ebb" else "市场冰点")
        if market.get("promo_rate") == 0.13:
            expected_hints.append("原始晋级率≤13%且封板率≤57%")
        for hint in expected_hints:
            assert any(hint in reason for reason in result["market"]["policy"]["observation_hints"])
            assert all(any(hint in reason for reason in row["limitations"])
                       for row in result["records"] if row["selected"])
        if phase_reason:
            assert any("阶段" in reason for reason in stock["limitations"])
        if ongoing:
            assert stock["paths"][0]["resilience"]["ongoing"] is True
            assert any("压力进行中" in reason for reason in stock["paths"][0]["limitations"])
        if overheated:
            assert stock["position"]["value"] is False
            assert any("位置过热" in reason for reason in stock["limitations"])
        view = report_view(result)
        assert view["selection_policy_version"] == SELECTION_POLICY_VERSION
        assert next(row for row in view["records"] if row["symbol"] == "stock00") == stock


def test_observation_policy_retains_unknown_and_known_risk_gates():
    history = []
    for day in (65, 66, 67):
        history.append(build_report(inputs_at(day), history))
    for case in ("unknown", "unavailable", "state_missing", "climax", "promo_missing", "seal_missing", "core_cold_start", "ma20", "one_price", "ma5_missing", "atr_missing", "limit_down", "pressure_missing", "local_weak"):
        value = inputs_at(68)
        value.market.update(state="weak", phase="unclassified", phase_reason="pending_confirmation")
        if case in {"unknown", "climax"}:
            value.market["phase"] = case
        elif case == "unavailable":
            value.market["state"] = case
        elif case == "state_missing":
            value.market["state"] = None
        elif case == "promo_missing":
            value.market.update(phase="ebb", promo_rate=None, seal_rate=0.57)
        elif case == "seal_missing":
            value.market.update(phase="ice", promo_rate=0.13, seal_rate=None)
        elif case == "core_cold_start":
            result = build_report(value)
            assert not result["selected"]
            assert result["market"]["policy"]["value"] is True
            continue
        elif case == "local_weak":
            value.prices = value.prices.with_columns(
                pl.when((pl.col("symbol") == "stock00") & (pl.col("date") == value.calendar[-10]))
                .then(pl.col("close") * 0.8).otherwise(pl.col("close")).alias("close")
            )
        elif case == "pressure_missing":
            value.benchmark = value.benchmark.with_columns(
                pl.when(pl.col("date") == value.trade_date).then(None).otherwise(pl.col("close")).alias("close")
            )
        else:
            changes = {
                "ma20": {"ma20": pl.col("close") * 1.01},
                "one_price": {key: pl.col("raw_close") for key in ("raw_open", "raw_high", "raw_low")},
                "ma5_missing": {"ma5": pl.lit(None)},
                "atr_missing": {"atr_14": pl.lit(None)},
                "limit_down": {"signal_limit_down": pl.lit(True)},
            }[case]
            value.prices = value.prices.with_columns([
                pl.when((pl.col("symbol") == "stock00") & (pl.col("date") == value.trade_date))
                .then(expression).otherwise(pl.col(key)).alias(key) for key, expression in changes.items()
            ])
        result = build_report(value, history)
        stock = next(row for row in result["records"] if row["symbol"] == "stock00")
        assert not stock["selected"], case
        if case in {"promo_missing", "seal_missing"}:
            assert result["market"]["policy"]["value"] is None
            assert any("市场政策证据不足" in reason for reason in stock["limitations"])
        if case == "local_weak":
            assert stock["paths"][0]["resilience"]["local_weak"] is True
            assert stock["paths"][0]["selection"]["inputs"]["韧性无观察限制"] is False
