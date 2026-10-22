"""独立交易状态证据只能解释目标日缺行，不能掩盖正式日线失败。"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock

import pytest
from test_postmarket_pipeline import DAY, SYMBOLS, bars
from test_postmarket_pipeline import closing as closing

from app.data_providers import trading_status
from app.data_providers.trading_status import DailyTradingStatus
from app.jobs import daily_pipeline
from app.services import postmarket
from app.services.daily_coverage import DailyCoverage, DailyCoverageError
from app.services.pipeline_jobs import JobCancelledError

HALTED = "000016.SZ"
UPCOMING = "001381.SZ"
UNEXPLAINED = "600999.SH"


@pytest.fixture
def status_fetch(monkeypatch):
    fetch = Mock(return_value=DailyTradingStatus(day=DAY))
    monkeypatch.setattr(trading_status, "get_daily_trading_status", fetch)
    return fetch


def coverage_with_active():
    coverage = DailyCoverage(DAY)
    coverage.observe(bars())
    return coverage


def test_complete_daily_coverage_does_not_query_status(status_fetch):
    coverage = coverage_with_active()
    postmarket.reconcile_daily_status(coverage, SYMBOLS)
    assert coverage.require(SYMBOLS)["active"] == 2
    status_fetch.assert_not_called()


def test_failed_daily_fetch_cannot_be_excused_by_trading_status(status_fetch):
    coverage = coverage_with_active()
    coverage.failed.add(HALTED)
    status_fetch.return_value = DailyTradingStatus(day=DAY, halted=frozenset({HALTED}))
    postmarket.reconcile_daily_status(coverage, [*SYMBOLS, HALTED])
    with pytest.raises(DailyCoverageError, match="取数失败 1 只"):
        coverage.require([*SYMBOLS, HALTED])
    assert coverage.failed == {HALTED}
    assert coverage.halted == set()
    status_fetch.assert_not_called()


def test_status_cannot_make_an_entirely_empty_daily_response_complete(status_fetch):
    coverage = DailyCoverage(DAY)
    status_fetch.return_value = DailyTradingStatus(day=DAY, halted=frozenset(SYMBOLS))
    postmarket.reconcile_daily_status(coverage, SYMBOLS)
    with pytest.raises(DailyCoverageError):
        coverage.require(SYMBOLS)
    status_fetch.assert_not_called()


def test_target_day_halt_and_upcoming_ipo_explain_missing_rows(status_fetch):
    coverage = coverage_with_active()
    status_fetch.return_value = DailyTradingStatus(
        day=DAY, halted=frozenset({HALTED}), not_listed=frozenset({UPCOMING}),
    )
    expected = [*SYMBOLS, HALTED, UPCOMING]
    postmarket.reconcile_daily_status(coverage, expected)
    result = coverage.require(expected)
    assert result["active"] == 2
    assert result["halted"] == 1
    assert result["not_listed"] == 1
    assert coverage.active == set(SYMBOLS)
    assert coverage.halted == {HALTED}
    assert coverage.not_listed == {UPCOMING}
    status_fetch.assert_called_once()
    assert status_fetch.call_args.args[0] == DAY
    assert set(status_fetch.call_args.args[1]) == {HALTED, UPCOMING}


def test_already_explained_halts_and_ipos_are_not_requested_again(status_fetch):
    coverage = coverage_with_active()
    coverage.halted.add(HALTED)
    coverage.not_listed.add(UPCOMING)
    postmarket.reconcile_daily_status(coverage, [*SYMBOLS, HALTED, UPCOMING])
    status_fetch.assert_not_called()


def test_status_from_another_day_is_rejected_before_applying_evidence(status_fetch):
    coverage = coverage_with_active()
    status_fetch.return_value = DailyTradingStatus(
        day=DAY - timedelta(days=1), halted=frozenset({HALTED}),
        not_listed=frozenset({UPCOMING}),
    )
    with pytest.raises((DailyCoverageError, ValueError)):
        postmarket.reconcile_daily_status(coverage, [*SYMBOLS, HALTED, UPCOMING])
    assert coverage.halted == set()
    assert coverage.not_listed == set()


def test_status_outside_missing_request_does_not_change_coverage(status_fetch):
    coverage = coverage_with_active()
    status_fetch.return_value = DailyTradingStatus(
        day=DAY, halted=frozenset({HALTED, SYMBOLS[0], UNEXPLAINED}),
        not_listed=frozenset({SYMBOLS[1], UPCOMING}),
    )
    postmarket.reconcile_daily_status(coverage, [*SYMBOLS, HALTED])
    assert coverage.halted == {HALTED}
    assert coverage.not_listed == set()
    assert coverage.active == set(SYMBOLS)
    assert coverage.require([*SYMBOLS, HALTED])["active"] == 2


def test_unknown_status_keeps_missing_rows_incomplete(status_fetch):
    coverage = coverage_with_active()
    postmarket.reconcile_daily_status(coverage, [*SYMBOLS, UNEXPLAINED])
    with pytest.raises(DailyCoverageError, match=UNEXPLAINED):
        coverage.require([*SYMBOLS, UNEXPLAINED])
    assert coverage.halted == set()
    assert coverage.not_listed == set()


def test_cancel_before_status_fetch_prevents_network(status_fetch):
    coverage = coverage_with_active()
    check_cancel = Mock(side_effect=JobCancelledError("status-test"))
    with pytest.raises(JobCancelledError):
        postmarket.reconcile_daily_status(coverage, [*SYMBOLS, HALTED], check_cancel=check_cancel)
    status_fetch.assert_not_called()
    assert coverage.halted == set()


def test_cancel_after_status_fetch_prevents_applying_evidence(status_fetch):
    coverage = coverage_with_active()
    returned = False

    def fetch(*_args, **_kwargs):
        nonlocal returned
        returned = True
        return DailyTradingStatus(day=DAY, halted=frozenset({HALTED}))

    def check_cancel():
        if returned:
            raise JobCancelledError("status-test")

    status_fetch.side_effect = fetch
    with pytest.raises(JobCancelledError):
        postmarket.reconcile_daily_status(coverage, [*SYMBOLS, HALTED], check_cancel=check_cancel)
    status_fetch.assert_called_once()
    assert coverage.halted == set()


def test_pipeline_with_eight_halts_and_two_upcoming_ipos_completes(closing, monkeypatch):
    repo, caps, steps, controls = closing
    halted = frozenset({
        "000016.SZ", "002388.SZ", "002717.SZ", "002731.SZ", "301314.SZ",
        "301468.SZ", "601326.SH", "688496.SH",
    })
    upcoming = frozenset({"001381.SZ", "301718.SZ"})
    controls.symbols.extend(sorted(halted | upcoming))

    def resolve_status(day, symbols, **_kwargs):
        assert day == DAY
        assert set(symbols) == halted | upcoming
        steps.append("trading_status")
        return DailyTradingStatus(day=day, halted=halted, not_listed=upcoming)

    monkeypatch.setattr(trading_status, "get_daily_trading_status", resolve_status)
    result = daily_pipeline.run_now(repo, caps, postmarket=True)
    daily = result["readiness"]["daily"]
    assert daily["active"] == 2
    assert daily["halted"] == 8
    assert daily["not_listed"] == 2
    assert result["readiness"]["enriched"]["symbols"] == 2
    assert steps.index("daily") < steps.index("trading_status") < steps.index("enriched")
    assert "publication" in steps


@pytest.mark.parametrize("source_failure", [False, True])
def test_unresolved_or_failed_status_stops_pipeline_before_indicators(closing, monkeypatch, source_failure):
    repo, caps, steps, controls = closing
    controls.symbols.append(UNEXPLAINED)
    fetch = Mock(return_value=DailyTradingStatus(day=DAY))
    if source_failure:
        fetch.side_effect = RuntimeError("交易状态来源暂不可用")
    monkeypatch.setattr(trading_status, "get_daily_trading_status", fetch)
    with pytest.raises(RuntimeError):
        daily_pipeline.run_now(repo, caps, postmarket=True)
    fetch.assert_called_once()
    assert "enriched" not in steps
    assert "regime" not in steps
    assert "publication" not in steps
