from datetime import date
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import regime_builder, sentiment_observations
from app.services.market_sentiment import INPUT_VERSION, classify_sentiment, cohort_performance


def daily(n=60, b=.35, p=.01, j=.2, **overrides):
    return {
        "date": "2026-10-09", "previous_trade_date": "2026-09-30",
        "method_version": sentiment_observations.VERSION,
        "sentiment_inputs_version": INPUT_VERSION, "sentiment_data_version": "fixture-v1",
        "exclude_st": True, "is_complete_day": True, "phase": "rally",
        "limit_up": n, "broken_rate": b, "promo_rate": j, "promo_pool": 12,
        "previous_limit_up_premium": p, "previous_limit_up_pool": 12,
        "previous_limit_up_valid": 12, "previous_limit_up_suspended": 0,
        "previous_limit_up_missing": 0, "previous_limit_up_complete": True,
        **overrides,
    }


@pytest.mark.parametrize(("n", "b", "p", "j", "expected"), [
    (60, .35, None, None, "hot_fragile"),
    (60, .2499, .001, None, "hot"),
    (60, .2499, None, .2, "unknown"),
    (60, .2499, 0, .2, "mixed"),
    (60, .25, None, None, "warm"),
    (60, .3499, None, None, "warm"),
    (39, .35, .01, .2, "mixed"),
    (39, .35, -.001, .2, "cold"),
    (39, .35, None, .149, "cold"),
    (39, .35, None, None, "unknown"),
    (40, .35, -.01, .1, "mixed"),
    (60, None, .01, .2, "unknown"),
])
def test_independent_branches_and_thresholds(n, b, p, j, expected):
    result = classify_sentiment(daily(n, b, p, j), [])
    assert result["sentiment_state"] == expected
    assert result["phase"] == "rally"


def history(n, b, p):
    days = ["2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"]
    return [daily(n, b, p, date=day, previous_trade_date=days[i - 1] if i else "2026-09-23")
            for i, day in enumerate(days)]


def test_direction_is_independent_causal_and_requires_contiguous_complete_history():
    row = daily()
    prior = history(50, .4, 0)
    better = classify_sentiment(row, prior)
    worse = classify_sentiment(row, history(70, .3, .02))
    assert better["sentiment_state"] == worse["sentiment_state"] == "hot_fragile"
    assert better["sentiment_direction"] == "improving"
    assert worse["sentiment_direction"] == "deteriorating"
    assert better["direction_votes"][0]["prior_mean"] == 50
    assert row["date"] not in better["direction_votes"][0]["prior_dates"]
    assert classify_sentiment(row, [*prior, daily(date="2026-10-12", p=1000)]) == better
    assert classify_sentiment(row, prior[1:])["sentiment_direction"] == "unknown"
    assert classify_sentiment(row, [{**r, "exclude_st": False} for r in prior])["sentiment_direction"] == "unknown"
    prior[-1]["previous_trade_date"] = "2026-09-28"
    assert classify_sentiment(row, prior)["sentiment_direction"] == "unknown"


def test_repair_votes_and_small_samples_do_not_create_false_certainty():
    prior = history(40, .24, 0)
    repaired = classify_sentiment(daily(45, .20, .005), prior)
    assert repaired["sentiment_state"] == "repair"
    assert repaired["sentiment_direction"] == "improving"
    small = classify_sentiment(daily(60, .2, .01, previous_limit_up_valid=9), [])
    assert small["evidence"]["previous_limit_up_premium"]["value"] == .01
    assert small["evidence"]["previous_limit_up_premium"]["usable"] is False
    assert small["sentiment_state"] == "unknown"
    small_promo = classify_sentiment(daily(39, .35, .01, 0, promo_pool=9), [])
    assert small_promo["evidence"]["promo_rate"] is None
    assert small_promo["sentiment_state"] == "unknown"
    no_touches = regime_builder._aggregate_daily(pl.DataFrame({
        "date": [date(2020, 1, 2)], "change_pct": [.01],
        "signal_limit_up": [False], "signal_broken_limit_up": [False],
    })).to_dicts()[0]
    assert no_touches["seal_rate"] is None
    assert no_touches["broken_rate"] is None


def cohort_rows():
    previous, current = date(2026, 9, 30), date(2026, 10, 9)
    rows = []
    for day in (previous, current):
        for i in range(12):
            # 除权股票的原始价格下跌，统一复权端点上涨10%。
            rows.append({"symbol": str(i), "date": day, "close": 5. if day == previous else 5.5,
                         "raw_close": 10. if day == previous else 5.5,
                         "raw_high": 10. if day == previous else 5.5,
                         "raw_low": 9. if day == previous else 5.5,
                         "volume": 100., "change_pct": .1,
                         "amount": 1e8, "signal_limit_up": day == previous,
                         "signal_limit_down": False, "signal_broken_limit_up": False})
    rows.append({**rows[-1], "symbol": "new-today", "signal_limit_up": True, "close": 999.})
    return previous, current, rows


def test_previous_set_uses_adjusted_endpoints_with_halts_missing_rows_and_limits():
    previous, current, rows = cohort_rows()
    rows[12].update(volume=0., change_pct=0.)
    frame = pl.DataFrame(rows)
    result = cohort_performance(frame, [previous, current]).to_dicts()[-1]
    assert result["previous_limit_up_pool"] == 12
    assert result["previous_limit_up_valid"] == 11
    assert result["previous_limit_up_suspended"] == 1
    assert result["previous_limit_up_limited"] == 11
    assert result["previous_limit_up_premium"] == pytest.approx(.1)
    assert result["previous_limit_up_complete"] is True
    missing = cohort_performance(frame.filter(~((pl.col("symbol") == "1") & (pl.col("date") == current))), [previous, current]).to_dicts()[-1]
    assert missing["previous_limit_up_missing"] == 1
    assert missing["previous_limit_up_valid"] == 10
    assert missing["previous_limit_up_complete"] is False
    assert classify_sentiment(daily(b=.2, **missing), [])["sentiment_state"] == "unknown"
    assert cohort_performance(frame, [previous, date(2026, 10, 8), current]).to_dicts()[-1]["previous_limit_up_pool"] is None
    assert cohort_performance(frame, []).to_dicts()[-1]["previous_limit_up_premium"] is None


def test_broken_cohort_excludes_closed_limit_and_incomplete_sources_are_not_validated():
    previous, current, rows = cohort_rows()
    for row in rows[:2]:
        row.update(signal_broken_limit_up=True, signal_limit_up=False)
    rows[2].update(signal_broken_limit_up=True)  # 不计入未封住集合
    frame = pl.DataFrame(rows)
    result = cohort_performance(frame, [previous, current]).to_dicts()[-1]
    assert result["previous_broken_pool"] == result["previous_broken_valid"] == 2
    assert result["previous_broken_performance"] == pytest.approx(.1)
    rows[3]["signal_limit_up"] = None
    result = cohort_performance(pl.DataFrame(rows), [previous, current]).to_dicts()[-1]
    assert result["previous_limit_up_complete"] is False
    assert result["previous_broken_complete"] is False


def test_incremental_persistence_and_api_share_one_classification(tmp_path, monkeypatch):
    import json

    from app.api.regime import regime_latest, regime_observations
    from app.services import preferences

    previous, current, rows = cohort_rows()
    calendar_path = tmp_path / "calendar" / "trading_days.json"
    calendar_path.parent.mkdir()
    calendar_path.write_text(json.dumps({"days": [str(previous), str(current)]}))
    monkeypatch.setattr(preferences, "get_sentiment_exclude_st", lambda: False)
    monkeypatch.setattr(regime_builder, "_load_index_pct", lambda *_, **__: {})
    requested = []

    def enriched(start, end):
        requested.append((start, end))
        return pl.DataFrame(rows).filter(pl.col("date").is_between(start, end))

    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path), get_enriched_range=enriched)
    result = regime_builder.run_regime_batch(repo, current, current)
    assert requested == [(previous, current)]
    assert result["date"].to_list() == [current]
    assert result["previous_limit_up_premium"][0] == pytest.approx(.1)
    assert result["limit_up_amount"][0] == 1e8
    regime_builder.upsert_regime_history(tmp_path, result)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))
    stored = sentiment_observations.load_observation(tmp_path, str(current))
    assert stored["classification"] == regime_latest(request)["row"]["sentiment"]["classification"]
    assert stored == regime_observations(request, current)["sentiment"]


def test_old_and_unclosed_data_remain_unknown_without_removing_phase():
    row = daily(b=.2)
    del row["sentiment_inputs_version"]
    old = classify_sentiment(row, [])
    assert old["sentiment_state"] == "unknown"
    assert "旧版" in old["evidence"]["previous_limit_up_premium"]["missing_reason"]
    pending = classify_sentiment(daily(is_complete_day=False), [])
    assert pending["heat_level"] == pending["sentiment_state"] == "unknown"
    assert pending["phase"] == "rally"


@pytest.mark.parametrize("preserve_warmup", [False, True])
def test_missing_whole_market_day_cannot_bridge_promotion_and_trigger_cold(tmp_path, preserve_warmup):
    import json

    from app.services.market_phase import with_prev_consecutive

    days = [date(2026, 10, n) for n in (12, 13, 14)]
    calendar = tmp_path / "calendar" / "trading_days.json"
    calendar.parent.mkdir()
    calendar.write_text(json.dumps({"days": [str(day) for day in days]}))
    rows = [{"date": day, "symbol": str(i), "close": 10., "volume": 100.,
             "change_pct": .01, "signal_limit_up": day == days[0],
             "signal_broken_limit_up": day == days[2],
             "consecutive_limit_ups": 1 if day == days[0] else 0}
            for day in (days[0], days[2]) for i in range(12)]
    frame = pl.DataFrame(rows)
    if preserve_warmup:
        # 磁盘慢路径已经在预热阶段带入_prev_*列，也必须重新对照真实日历。
        frame = with_prev_consecutive(frame).filter(pl.col("date") == days[2])
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    result = regime_builder._aggregate_with_cohorts(frame, repo, days[2], days[2], {}).to_dicts()[0]
    assert result["previous_trade_date"] == str(days[1])
    assert result["previous_limit_up_complete"] is None
    assert result["promo_pool"] is None and result["promo_rate"] is None
    assert classify_sentiment(result, [])["sentiment_state"] == "unknown"
