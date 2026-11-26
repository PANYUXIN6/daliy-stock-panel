
import polars as pl
import pytest

from app.services.evening_report_engine import build_report


def test_leading_score_uses_all_eligible_members_including_small_and_short_histories():
    from test_evening_report_observation import observation_inputs

    inputs = observation_inputs()
    inputs.memberships = inputs.memberships.with_columns(pl.lit("all").alias("sector_id"))
    # Exclude a high-return but paused member. Keep a member missing one unrelated mid-window close
    # and another with less than 60 days: both fixed return endpoints still exist.
    inputs.metadata = inputs.metadata.with_columns((pl.col("symbol") == "000000.SZ").alias("qualification_pause"))
    inputs.prices = inputs.prices.filter(
        ~((pl.col("symbol") == "000001.SZ") & (pl.col("date") == inputs.calendar[-12]))
        & ~((pl.col("symbol") == "300002.SZ") & (pl.col("date") < inputs.calendar[-21])))
    report = build_report(inputs)
    theme, = report["observation_themes"]
    assert len(theme["lead_source_members"]) == 19
    assert "000000.SZ" not in theme["lead_source_members"]
    assert {"000001.SZ", "300002.SZ"} <= set(theme["lead_source_members"])
    leader = next(r["observation"] for r in report["records"] if r["symbol"] == "000004.SZ")
    lead = leader["theme_lead_evidence"]
    # The 18 other peers consist of 3 at 51%, 5 at 35% and 10 at 20%.
    benchmark = (3 * .51 + 5 * .35 + 10 * .20) / 18
    assert lead["components"]["X20"] == pytest.approx(1.51 / (1 + benchmark) - 1)
    # Four tied high-return peers occupy ranks 16..19 out of 19.
    assert lead["components"]["_p20"] == pytest.approx(100 * 16.5 / 18)
    assert lead["metric"] == "relative_lead_score" and lead["source"]["sample_count"] == 19


def test_only_adjacent_published_roles_survive_for_five_trading_days():
    from test_evening_report_observation import observation_inputs

    inputs = observation_inputs()
    inputs.memberships = inputs.memberships.with_columns(pl.lit("all").alias("sector_id"))
    first = build_report(inputs)
    unseen = next(s for s in first["role_state"] if s not in first["selected"])
    assert first["role_state"][unseen] == inputs.trade_date
    # On D the stock qualifies neither as current true leader nor fallback.
    inputs.prices = inputs.prices.with_columns(
        *[pl.when(pl.col("symbol") == unseen).then(value).otherwise(key).alias(key)
          for key, value in {"close": 10., "raw_close": 10., "signal_limit_up": False, "consecutive_limit_ups": 0}.items()])
    def at(previous):
        result = build_report(inputs, previous)
        row = next(r for r in result["calculations"] if r["symbol"] == unseen)
        return row["observation"]["role"], result["role_state"].get(unseen)
    previous = {"trade_date": inputs.calendar[-2], "role_state": {unseen: inputs.calendar[-5]}}
    assert at(previous) == ("historical_leader", inputs.calendar[-5])
    assert at({**previous, "trade_date": inputs.calendar[-3]}) == ("other", None)
    assert at({**previous, "role_state": {unseen: inputs.calendar[-6]}}) == ("other", None)


def test_published_candidate_uses_event_day_and_open_pullback_for_dip_labels():
    from test_evening_report_observation import observation_inputs

    inputs = observation_inputs()
    symbol = "000000.SZ"
    inputs.market = {"phase": "rally", "ge2_count": 3, "classification": {"sentiment_state": "warm"}}
    target = inputs.prices.filter(pl.col("symbol") == symbol).to_dicts()
    # A rise from 7, followed by a downward reversal, L0=10, H1=12 and two equal lows at 11.2.
    tail = [(7.7, 7.1, 7.7), (8.47, 7.75, 8.47), (9.32, 8.5, 9.32),
            (10.25, 9.4, 10.25), (11.15, 10.3, 11.1), (11.2, 10.5, 10.55),
            (10.7, 10., 10.55), (11.6, 10.6, 11.5), (12., 11.5, 11.9),
            (11.8, 11.2, 11.32), (11.5, 11.2, 11.30)]
    for i, row in enumerate(target):
        j = i - (len(target) - len(tail))
        high, low, close = tail[j] if j >= 0 else (7.05, 6.95, 7.)
        row.update(high=high, low=low, open=low, close=close, raw_high=high, raw_low=low,
                   raw_open=low, raw_close=close, ma10=close, bull_bear_line=close,
                   signal_limit_up=0 <= j < 4, consecutive_limit_ups=j+1 if 0 <= j < 4 else 0,
                   amount=1e9, volume=50. if i == len(target)-2 else 60. if i == len(target)-1 else 100.)
    inputs.prices = pl.concat([inputs.prices.filter(pl.col("symbol") != symbol), pl.DataFrame(target)])
    table = {"trade_date": inputs.trade_date, "status": "complete", "events": {symbol: {"e2": {"source": "test"}}}}
    inputs.provenance["severe_events"] = table

    def snapshot():
        report = build_report(inputs)
        assert symbol in report["selected"]
        return next(r["observation"] for r in report["records"] if r["symbol"] == symbol)

    obs = snapshot()
    assert obs["wave"]["first_pullback_low"] is None
    assert obs["values"]["retrace_pct"] == pytest.approx(.4)
    assert obs["values"]["pullback_volume_mean"] == 55
    assert obs["values"]["first_wave_pullback"] == pytest.approx(1-11.3/12.)
    assert obs["values"]["severe_event"] is True and obs["dip"]["status"] == "full"
    assert obs["dip"]["e2"]["event"] == table["events"][symbol]["e2"]

    inputs.prices = inputs.prices.with_columns(
        pl.when((pl.col("symbol") == symbol) & (pl.col("date") == inputs.trade_date))
        .then(11.19).otherwise("low").alias("low"))
    assert snapshot()["dip"]["status"] == "partial"
    inputs.provenance["severe_events"] = {**table, "events": {}}
    absent = snapshot()["dip"]
    assert absent["status"] == "none" and absent["e2"]["event_applicable"] is False
    del inputs.provenance["severe_events"]
    unknown = snapshot()["dip"]
    assert unknown["status"] == "none" and unknown["e2"]["event_applicable"] is None
