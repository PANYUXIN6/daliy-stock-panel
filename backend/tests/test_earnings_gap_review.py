from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.services import earnings_gap_review as review
from app.services.financial_versions import read_versions, record_versions


def financial(symbol, period, available, profit):
    return dict(symbol=symbol, period_end=period, announce_date=available,
                source_update_date=available, date_basis="source_update",
                disclosure_type="formal", net_income=profit)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(review.preferences, "get_financial_provider", lambda: "fuyao")
    days = [date(2026, 10, day) for day in (9, 12, 13, 14)]
    prices = pl.DataFrame([
        dict(symbol=symbol, date=day, open=10.0 if index == 0 else 11.5,
             high=10.5 if index == 0 else 12.0, low=9.5 if index == 0 else 11.0,
             close=10.0 if index == 0 else 11.6, volume=100.0, amount=1000.0)
        for symbol in ("600000.SH", "000001.SZ") for index, day in enumerate(days)
    ])
    folder = tmp_path / "calendar"
    folder.mkdir()
    (folder / "trading_days.json").write_text(json.dumps({
        "start": "2026-10-01", "end": "2026-10-14", "days": list(map(str, days)),
    }))
    return SimpleNamespace(
        store=SimpleNamespace(data_dir=tmp_path), prices=prices,
        get_daily_batch=lambda symbols, start, end, columns: prices.filter(
            pl.col("symbol").is_in(symbols) & pl.col("date").is_between(start, end)
        ).select(columns),
        get_financial_versions=lambda table, symbol=None, columns=None: read_versions(
            tmp_path, table, columns, symbol=symbol,
        ),
    )


def capture(repo, available="2026-10-10", profit=100.0, *, symbol="600000.SH", prior=True,
            observed="2026-10-11T20:00:00+08:00"):
    rows = [financial(symbol, "2026-06-30", available, profit)]
    if prior:
        rows.append(financial(symbol, "2025-06-30", "2025-08-20", 50.0))
    return record_versions(repo.store.data_dir, "income", pl.DataFrame(rows), "fuyao",
                           datetime.fromisoformat(observed))


def candidates(repo):
    return review.find_gap_candidates(repo, repo.prices.filter(pl.col("date") == date(2026, 10, 12)),
                                      date(2026, 10, 12))


def test_price_candidates_are_found_before_any_financial_read(repo):
    repo.get_financial_versions = lambda *args, **kwargs: pytest.fail("筛候选不应读财务")
    found = candidates(repo)
    assert set(found["symbol"]) == {"600000.SH", "000001.SZ"}
    assert found["gap_lower"].to_list() == [10.5, 10.5]
    assert found["gap_upper"].to_list() == [11.0, 11.0]


@pytest.mark.parametrize("available", ["2026-10-09", "2026-10-10", "2026-10-11"])
def test_friday_or_weekend_version_maps_to_first_monday_gap(repo, available):
    capture(repo, available)
    result = review.review_candidates(repo, candidates(repo), date(2026, 10, 12))
    assert result["total"] == 1
    assert result["rows"][0]["symbol"] == "600000.SH"
    verification = result["verification"]
    assert verification["confirmed_count"] == 1 and verification["pending_count"] == 1
    assert {row["verification_status"] for row in verification["candidates"]} == {"confirmed", "pending"}


def test_first_observed_after_monday_gap_remains_pending_and_never_backdates(repo):
    record_versions(repo.store.data_dir, "income", pl.DataFrame([
        financial("600000.SH", "2025-06-30", "2025-08-20", 50.0),
    ]), "fuyao", datetime.fromisoformat("2026-10-01T20:00:00+08:00"))
    capture(repo, observed="2026-10-12T20:00:00+08:00")
    result = review.review_candidates(repo, candidates(repo), date(2026, 10, 12), status="completed")
    assert result["rows"] == [] and result["total"] == 0
    row = next(row for row in result["verification"]["candidates"] if row["symbol"] == "600000.SH")
    assert row["verification_status"] == "pending"
    assert "当时" in row["verification_reason"]
    assert "2026-10-12" in read_versions(repo.store.data_dir, "income")["available_date"].to_list()


@pytest.mark.parametrize("profit,prior,expected", [(-1.0, True, "excluded"),
                                                  (50.0, True, "excluded"),
                                                  (100.0, False, "pending")])
def test_known_profit_failures_are_excluded_and_missing_comparative_is_pending(repo, profit, prior, expected):
    capture(repo, profit=profit, prior=prior)
    result = review.review_candidates(repo, candidates(repo), date(2026, 10, 12))
    row = next(row for row in result["verification"]["candidates"] if row["symbol"] == "600000.SH")
    assert row["verification_status"] == expected
    assert not result["rows"]


def test_single_stock_refresh_failure_retains_pending_reason(repo):
    result = review.review_candidates(repo, candidates(repo), date(2026, 10, 12),
                                      status="completed", errors={"600000.SH": "供应商限流"})
    row = next(row for row in result["verification"]["candidates"] if row["symbol"] == "600000.SH")
    assert row["verification_status"] == "pending" and "限流" in row["verification_reason"]


def test_no_price_candidates_do_not_read_financials(repo):
    repo.get_financial_versions = lambda *args, **kwargs: pytest.fail("空候选不能读财务")
    result = review.review_candidates(repo, repo.prices.head(0), date(2026, 10, 12))
    assert result["total"] == 0 and result["verification"]["candidate_count"] == 0
