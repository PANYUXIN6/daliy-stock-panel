"""按报告期获取历史财务，不把当前估值填入历史指标。"""
from datetime import date
from unittest.mock import Mock

import polars as pl
import pytest
from test_fuyao_financial import _INCOME_ROW, _FakeFinClient, _provider_with

from app.data_providers.base import ProviderFinancialDataUnavailableError
from app.plugins.fuyao.client import FuyaoClient, FuyaoDataUnavailableError
from app.plugins.fuyao.provider import _ms_of_date
from app.services import financial_sync, preferences
from app.tickflow.capabilities import Cap, CapabilitySet


def test_client_sends_range_without_latest_limit_and_keeps_beijing_boundaries(monkeypatch):
    client = FuyaoClient("test-key")
    get = Mock(return_value={"item": []})
    monkeypatch.setattr(client, "_get", get)
    try:
        client.financial_statements("income", "600519.SH", start=1729180800000, end=1792252800000)
        params = get.call_args.args[1]
        assert params == {"thscode": "600519.SH", "period": "quarterly",
                          "start": 1729180800000, "end": 1792252800000}
        assert _ms_of_date(date(2026, 6, 30)) == 1782748800000
    finally:
        client.close()


class HistoryClient(_FakeFinClient):
    def financial_statements(self, stmt, thscode, limit=1, *, start=None, end=None, cancel_event=None):
        self.stmt_calls.append((stmt, thscode, start, end))
        return [r for r in self.statements.get(stmt, [])
                if start is None or start <= r["period_end_ms"] <= end]


def history_client():
    previous = {**_INCOME_ROW, "fiscal_year": 2025, "basic_eps": 20.,
                "period_end_ms": _ms_of_date(date(2025, 6, 30))}
    old = {**_INCOME_ROW, "fiscal_year": 2023, "period_end_ms": _ms_of_date(date(2023, 6, 30))}
    return HistoryClient(statements={"income": [_INCOME_ROW, previous, old]}, indicators={
        "2026-2": [{"indicators": [{"index_id": "index_weighted_avg_roe", "value": "12.5"}]}],
        "2025-2": [{"indicators": [{"index_id": "index_weighted_avg_roe", "value": "10.5"}]}],
    })


def test_statements_range_and_per_period_metrics_preserve_dates_and_avoid_current_bps(monkeypatch):
    from app.plugins.fuyao import provider as fp
    monkeypatch.setattr(fp, "cn_today", lambda: date(2026, 10, 18))
    fake = history_client()
    fake.valuations_snapshot = Mock(side_effect=AssertionError("历史指标不能使用当前估值"))
    provider = _provider_with(monkeypatch, fake)
    start, end = date(2024, 10, 18), date(2026, 8, 31)
    income = provider.get_financials("income", ["600519.SH"], False, start_date=start, end_date=end)
    assert set(income["period_end"]) == {"2026-06-30", "2025-06-30"}
    assert fake.stmt_calls[-1] == ("income", "600519.SH", _ms_of_date(start), _ms_of_date(end))
    metrics = provider.get_financials("metrics", ["600519.SH"], False, start_date=start, end_date=end)
    rows = {r["period_end"]: r for r in metrics.to_dicts()}
    assert rows["2026-06-30"]["roe"] == 12.5
    assert rows["2025-06-30"]["roe"] == 10.5
    assert rows["2025-06-30"]["eps_basic"] == 20.
    assert all(r["date_basis"] == "observed" and "bps" not in r for r in rows.values())
    assert set(fake.ind_calls) == {"600519.SH@2026-2", "600519.SH@2025-2"}


def test_recent_range_keeps_current_bps_estimate_only_on_latest_period(monkeypatch):
    from app.plugins.fuyao import provider as fp
    monkeypatch.setattr(fp, "cn_today", lambda: date(2026, 10, 18))
    provider = _provider_with(monkeypatch, history_client())
    monkeypatch.setattr(provider, "_derive_bps", Mock(return_value={"600519.SH": 8.}))
    result = provider.get_financials("metrics", ["600519.SH"], False,
                                    start_date=date(2024, 10, 18), end_date=date(2026, 10, 18))
    rows = {r["period_end"]: r for r in result.to_dicts()}
    assert rows["2026-06-30"]["bps"] == 8.
    assert rows["2026-06-30"]["bps_basis"] == "valuation_snapshot"
    assert rows["2025-06-30"]["bps"] is None


def test_historical_metric_missing_period_is_an_error_instead_of_partial_success(monkeypatch):
    fake = history_client()
    original = fake.financial_indicators
    def indicators(symbol, report, *, cancel_event=None):
        if report == "2025-2":
            raise FuyaoDataUnavailableError("missing period")
        return original(symbol, report, cancel_event=cancel_event)
    fake.financial_indicators = indicators
    provider = _provider_with(monkeypatch, fake)
    with pytest.raises(ProviderFinancialDataUnavailableError) as caught:
        provider.get_financials("metrics", ["600519.SH"], False,
                               start_date=date(2024, 10, 18), end_date=date(2026, 10, 18))
    assert caught.value.data["period_end"].to_list() == ["2026-06-30"]


@pytest.mark.parametrize("table", financial_sync.FINANCIAL_TABLES)
def test_tickflow_receives_report_date_range_for_each_financial_table(monkeypatch, table):
    from types import SimpleNamespace

    from app.tickflow import client
    fetch = Mock(return_value={"600519.SH": [{"period_end": "2026-06-30", "revenue": 100.}]})
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "tickflow")
    monkeypatch.setattr(client, "get_client", lambda: SimpleNamespace(financials=SimpleNamespace(
        **dict.fromkeys(financial_sync.FINANCIAL_TABLES, fetch))))
    caps = CapabilitySet()
    caps.grant(Cap.FINANCIAL)
    result = financial_sync._fetch_table(table, ["600519.SH"], caps, False,
                                         start_date=date(2024, 10, 18), end_date=date(2026, 10, 18))
    fetch.assert_called_once_with(["600519.SH"], latest=False, start_date="2024-10-18", end_date="2026-10-18")
    assert isinstance(result, pl.DataFrame) and result["source"].item() == "tickflow"
