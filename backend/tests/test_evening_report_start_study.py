from datetime import date, timedelta

import polars as pl
import pytest
from test_evening_report_observation import observation_inputs

from app.parquet import DAILY_STORAGE_SCHEMA
from app.services.evening_report_engine import build_report
from app.services.evening_report_observation_study import compare_observations
from app.services.evening_report_start_study import evaluate_start, run_study
from app.services.evening_report_store import EveningReportStore, atomic_json

SYMBOL = "300001.SZ"
DAYS = [str(date(2026, 11, 2) + timedelta(days=n)) for n in range(15)
        if (date(2026, 11, 2) + timedelta(days=n)).weekday() < 5]
EVENT = {"date": DAYS[0], "evidence_date": DAYS[0], "start_id": "second", "rule_version": "wave-rule-v2",
         "security_id": SYMBOL, "segment_index": 2, "target_high": {"price": 12.}, "failure_low": {"price": 9.},
         "start_close": 10., "start_low": 9.5}


@pytest.mark.parametrize(("closes", "expected"), [
    ([12., 12.01, 8.9], "close_above_target_high"),
    ([9.5, 9.49, 12.01], "close_below_start_low_before_new_high"),
    ([8.99, 12.01], "close_below_failure_low"),
    ([10.9] + [10.] * 8 + [10.8], "end_return_target_met"),
    ([10.9] + [10.] * 9, "target_not_reached"),
    ([None, 12.01], "missing_price_or_factor"),
    ([10.], "not_due"),
])
def test_first_decisive_close_and_fixed_maturity(closes, expected):
    prices = {(SYMBOL, day): close for day, close in zip(DAYS[1:], closes, strict=False)}
    result = evaluate_start(EVENT, window=DAYS, prices=prices, factors=dict.fromkeys(DAYS, 1.),
                            suspended=set(), as_of=DAYS[len(closes)])
    assert result["evaluation_reason_code"] == expected


def test_calendar_pending_suspension_and_breakout_at_start():
    options = dict(window=DAYS[:2], prices={(SYMBOL, DAYS[1]): 10.}, factors=dict.fromkeys(DAYS, 1.),
                   suspended=set(), as_of=DAYS[-1])
    assert evaluate_start(EVENT, **options)["status"] == "not_due"
    assert evaluate_start(EVENT, **{**options, "suspended": {(SYMBOL, DAYS[1])}})["status"] == "unavailable"
    assert evaluate_start({**EVENT, "start_close": 12.1}, **options)["status"] == "success_at_start"


def test_offline_run_deduplicates_nonselected_starts_and_converts_using_local_factors(tmp_path):
    store = EveningReportStore(tmp_path)
    atomic_json(tmp_path / "calendar" / "trading_days.json", {"days": DAYS})
    for day in DAYS[:2]:
        store.save(day, {"trade_date": day, "records": [], "selected": [], "audit": {},
                         "wave_starts": [{**EVENT, "date": day}, {**EVENT, "segment_index": 3, "start_id": "third"}]}, {})
    first = store.get(DAYS[0])
    folder = tmp_path / "adj_factor"
    folder.mkdir()
    pl.DataFrame({"symbol": [SYMBOL], "trade_date": [date.fromisoformat(DAYS[1])],
                  "ex_factor": [2.]}).write_parquet(folder / "all.parquet")
    day = date.fromisoformat(DAYS[1])
    path = tmp_path / "kline_daily" / f"date={day}"
    path.mkdir(parents=True)
    pl.DataFrame([{"symbol": SYMBOL, "date": day, "open": 5., "high": 6.1, "low": 5., "close": 6.1,
                   "volume": 100., "amount": 600., "quote_ts": None}], schema=DAILY_STORAGE_SCHEMA).write_parquet(path / "part.parquet")
    for as_of, rate in [(DAYS[1], None), (DAYS[-1], 1.)]:
        output = run_study(store, date.fromisoformat(as_of), evaluated_at=as_of)
        assert output["summary"]["total"] == 1 and output["summary"]["success_rate"] == rate
        assert output["results"][0]["report_version"] == first["version"]
        assert output["results"][0]["evaluation_reason_code"] == "close_above_target_high"
        assert store.get(DAYS[0]) == first


def test_baseline_uses_frozen_candidates_and_prices_on_the_same_sessions(tmp_path):
    inputs = observation_inputs()
    store = EveningReportStore(tmp_path)
    report = store.save(inputs.trade_date, build_report(inputs), {})
    start = date.fromisoformat(inputs.trade_date)
    end = start + timedelta(days=1)
    assert compare_observations(store, end)["uncovered_reports"][0]["date"] == inputs.trade_date
    atomic_json(tmp_path / "calendar" / "trading_days.json", {"days": [str(start), str(end)]})
    symbol = report["selected"][0]
    folder = tmp_path / "adj_factor"
    folder.mkdir()
    pl.DataFrame({"symbol": [symbol], "trade_date": [end], "ex_factor": [2.]}).write_parquet(folder / "all.parquet")
    for on_date, close in [(start, 10.), (end, 5.5)]:
        path = tmp_path / "kline_daily" / f"date={on_date}"
        path.mkdir(parents=True)
        pl.DataFrame([{"symbol": symbol, "date": on_date, "open": close, "high": close, "low": close,
                       "close": close, "volume": 100., "amount": 1000., "quote_ts": None}],
                     schema=DAILY_STORAGE_SCHEMA).write_parquet(path / "part.parquet")
    comparison, = compare_observations(store, end)["comparison"]
    row = next(r for r in comparison["observations"] if r["symbol"] == symbol)
    assert comparison["baseline_selected"] == comparison["selected"] == 15
    assert row["baseline"] and row["selected"]
    assert row["performance"]["1"]["return"] == pytest.approx(.10)
    assert row["performance"]["3"]["status"] == "not_due"
