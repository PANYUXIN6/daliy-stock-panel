"""财务页按报告期默认两年、逐次向前一年；历史保存与计算任务隔离。"""
from datetime import date
from unittest.mock import Mock

import polars as pl
import pytest
from test_financial_demand import env as env

from app.data_providers.base import ProviderFinancialDataUnavailableError
from app.services import financial_demand as demand
from app.tickflow.capabilities import CapabilitySet


def history(env, before=None):
    return demand.fetch_stock_history(env.root, CapabilitySet(), "600000.SH", "income", before=before)


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr(demand, "cn_today", lambda: date(2026, 10, 18))


def test_default_two_years_then_each_click_fetches_only_the_previous_year(env):
    def rows(table, symbols, caps, **kwargs):
        start, end = kwargs["start_date"], kwargs["end_date"]
        periods = ["2026-06-30", "2025-06-30", "2024-06-30", "2023-06-30", "2022-06-30"]
        return pl.DataFrame([{"symbol": symbols[0], "source": "fuyao", "period_end": p,
                              "net_income": 100.} for p in periods if start.isoformat() <= p <= end.isoformat()])
    env.fetch.side_effect = rows
    result = history(env)
    assert (result["start_date"], result["end_date"]) == ("2024-10-18", "2026-10-18")
    assert result["rows"] == 2
    assert history(env)["reused"] is True
    for before, start, end in [(date(2024, 10, 18), "2023-10-18", "2024-10-17"),
                                (date(2023, 10, 18), "2022-10-18", "2023-10-17")]:
        result = history(env, before)
        assert (result["start_date"], result["end_date"]) == (start, end)
        assert result["rows"] == 1
        assert history(env, before)["reused"] is True
    assert env.fetch.call_count == 3
    saved = pl.read_parquet(env.root / "financials/income/part.parquet")
    assert set(saved["period_end"]) == {"2026-06-30", "2025-06-30", "2024-06-30", "2023-06-30"}


def test_empty_older_year_advances_checked_cursor_but_failure_keeps_it(env):
    history(env)
    path = env.root / "financials/income/part.parquet"
    before_data = path.read_bytes()
    env.fetch.side_effect = RuntimeError("upstream unavailable")
    with pytest.raises(RuntimeError):
        history(env, date(2024, 10, 18))
    assert path.read_bytes() == before_data
    assert demand.history_start(env.root, "income", "600000.SH") == "2024-10-18"
    env.fetch.side_effect = None
    env.fetch.return_value = pl.DataFrame()
    assert history(env, date(2024, 10, 18))["rows"] == 0
    assert demand.history_start(env.root, "income", "600000.SH") == "2023-10-18"
    assert path.read_bytes() == before_data


def test_recent_window_does_not_satisfy_unbounded_computation_request(env):
    history(env)
    assert demand.fetch_stock(env.root, CapabilitySet(), "600000.SH", "income")["reused"] is False
    assert env.fetch.call_count == 2
    assert "start_date" not in env.fetch.call_args.kwargs
    assert history(env)["reused"] is True


def test_legacy_rows_preserved_and_expansion_cannot_skip_an_unrequested_year(env):
    path = env.root / "financials/income/part.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({"symbol": ["600000.SH"], "source": ["fuyao"],
                  "period_end": ["2020-06-30"], "net_income": [5.]}).write_parquet(path)
    history(env)
    assert "2020-06-30" in pl.read_parquet(path)["period_end"].to_list()
    assert demand.history_start(env.root, "income", "600000.SH") == "2020-06-30"
    env.fetch.reset_mock()
    with pytest.raises(ValueError, match="刷新"):
        history(env, date(2018, 1, 1))
    env.fetch.assert_not_called()


def test_leap_day_recent_window_and_empty_initial_request(env, monkeypatch):
    monkeypatch.setattr(demand, "cn_today", lambda: date(2028, 2, 29))
    env.fetch.side_effect = None
    env.fetch.return_value = pl.DataFrame()
    with pytest.raises(ValueError, match="未返回"):
        history(env)
    assert env.fetch.call_args.kwargs["start_date"] == date(2026, 2, 28)
    assert not (env.root / "financials/demand/income/600000.SH.json").exists()


def test_overfetch_is_filtered_and_invalid_report_date_cannot_advance_cursor(env):
    env.fetch.side_effect = None
    env.fetch.return_value = pl.DataFrame({
        "symbol": ["600000.SH"] * 4, "source": ["fuyao"] * 4,
        "period_end": ["2023-06-30", "2025-06-30", "2026-06-30", "2027-06-30"],
        "net_income": [50., 60., 70., 80.],
    })
    assert history(env)["rows"] == 2
    path = env.root / "financials/income/part.parquet"
    assert set(pl.read_parquet(path)["period_end"]) == {"2025-06-30", "2026-06-30"}
    env.fetch.return_value = pl.DataFrame({"symbol": ["600000.SH"], "source": ["fuyao"],
                                         "period_end": ["invalid"], "net_income": [50.]})
    with pytest.raises(ValueError, match="报告期无效"):
        history(env, date(2024, 10, 18))
    assert demand.history_start(env.root, "income", "600000.SH") == "2024-10-18"


def test_read_history_cursor_is_local_and_source_specific(env, monkeypatch):
    history(env)
    env.fetch.reset_mock()
    monkeypatch.setattr(demand, "supported_tables", Mock(side_effect=AssertionError("浏览不能探测供应商")))
    assert demand.history_start(env.root, "income", "600000.SH") == "2024-10-18"
    monkeypatch.setattr(demand.preferences, "get_financial_provider", lambda: "another")
    assert demand.history_start(env.root, "income", "600000.SH") is None
    env.fetch.assert_not_called()


def test_api_default_range_local_cursor_and_one_year_expansion(env, authenticated_actor):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import financials
    app = FastAPI()
    app.include_router(financials.router)
    app.state.repo, app.state.capabilities = env.repo, CapabilitySet()
    with TestClient(app) as client:
        payload = {"symbol": "600000.SH", "table": "income"}
        result = client.post("/api/financials/fetch", json=payload)
        assert result.status_code == 200, result.text
        assert result.json()["start_date"] == "2024-10-18"
        response = client.get("/api/financials/income", params={"symbol": "600000.SH"})
        assert response.json()["history_start"] == "2024-10-18"
        # 空的更早年度也是有效查询，不能删掉本地已有两年数据。
        env.fetch.side_effect = None
        env.fetch.return_value = pl.DataFrame()
        result = client.post("/api/financials/fetch", json={**payload, "before": "2024-10-18"})
        assert result.status_code == 200, result.text
        assert result.json()["end_date"] == "2024-10-17"
        response = client.get("/api/financials/income", params={"symbol": "600000.SH"}).json()
        assert response["history_start"] == "2023-10-18"
        assert len(response["data"]) == 2
        assert client.post("/api/financials/fetch", json={**payload, "before": "bad"}).status_code == 422


def test_range_is_passed_to_custom_source_and_legacy_signature_is_compatible(monkeypatch):
    from types import SimpleNamespace

    from test_financial_demand import records

    from app.data_providers import custom
    from app.services import financial_sync
    monkeypatch.setattr(demand.preferences, "get_financial_provider", lambda: "fuyao")
    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: True)
    calls = []
    def bounded(table, symbols, latest_only, *, start_date, end_date):
        calls.append((start_date, end_date))
        return records(symbols[0])
    monkeypatch.setattr(custom, "get_provider", lambda source: SimpleNamespace(get_financials=bounded))
    result = financial_sync._fetch_table("income", ["600000.SH"], CapabilitySet(), False,
                                         start_date=date(2024, 10, 18), end_date=date(2026, 10, 18))
    assert result.height == 2
    assert calls == [(date(2024, 10, 18), date(2026, 10, 18))]
    def legacy(table, symbols, latest_only):
        return records(symbols[0])
    monkeypatch.setattr(custom, "get_provider", lambda source: SimpleNamespace(get_financials=legacy))
    assert financial_sync._fetch_table("income", ["600000.SH"], CapabilitySet(), False,
                                       start_date=date(2024, 10, 18), end_date=date(2026, 10, 18)).height == 2


def metric_rows(periods):
    return pl.DataFrame([{"symbol": "600000.SH", "source": "fuyao", "period_end": period,
                          "roe": 12.3, "date_basis": "observed"} for period in periods])


def missing_metrics(frame, periods):
    return ProviderFinancialDataUnavailableError(
        frame, {"600000.SH": "private upstream error"}, missing_periods={"600000.SH": periods},
    )


@pytest.mark.parametrize("empty", [False, True])
def test_partial_metrics_are_saved_and_reported_then_retry_can_complete_same_day(env, authenticated_actor, empty):
    import json

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import financials

    frame = pl.DataFrame() if empty else metric_rows(["2026-06-30", "2025-12-31"])
    env.fetch.side_effect = missing_metrics(frame, ["2026-09-30"])
    app = FastAPI()
    app.include_router(financials.router)
    app.state.repo, app.state.capabilities = env.repo, CapabilitySet()
    payload = {"symbol": "600000.SH", "table": "metrics"}
    receipt = env.root / "financials/demand/metrics/600000.SH.json"
    with TestClient(app) as client:
        partial = client.post("/api/financials/fetch", json=payload)
        assert partial.status_code == 200, partial.text
        assert partial.json()["complete"] is False
        assert partial.json()["rows"] == len(frame)
        assert partial.json()["missing_periods"] == ["2026-09-30"]
        assert "private upstream" not in partial.text
        saved = json.loads(receipt.read_text())
        assert "recent_checked_on" not in saved and "checked_on" not in saved
        # 浏览或刷新只读本地缺期信息；不能再次访问数据源。
        local = client.get("/api/financials/metrics", params={"symbol": "600000.SH"}).json()
        assert len(local["data"]) == len(frame)
        assert local["missing_periods"] == ["2026-09-30"]
        assert local["history_start"] == "2024-10-18"
        assert env.fetch.call_count == 1
        # 部分结果不能让同日重试误命中完整缓存。
        assert client.post("/api/financials/fetch", json=payload).json()["complete"] is False
        assert env.fetch.call_count == 2
        env.fetch.side_effect = None
        env.fetch.return_value = metric_rows(["2026-09-30", "2026-06-30", "2025-12-31"])
        completed = client.post("/api/financials/fetch", json=payload).json()
        assert completed["complete"] is True and completed["missing_periods"] == []
        assert completed["reused"] is False
        assert json.loads(receipt.read_text())["recent_checked_on"] == "2026-10-18"
        local = client.get("/api/financials/metrics", params={"symbol": "600000.SH"}).json()
        assert len(local["data"]) == 3 and local["missing_periods"] == []
        assert client.post("/api/financials/fetch", json=payload).json()["reused"] is True
        assert env.fetch.call_count == 3


def test_partial_older_year_keeps_cursor_until_missing_quarter_is_filled(env):
    demand.fetch_stock_history(env.root, CapabilitySet(), "600000.SH", "metrics")
    env.fetch.side_effect = missing_metrics(metric_rows(["2023-12-31"]), ["2024-03-31"])
    before = date(2024, 10, 18)
    result = demand.fetch_stock_history(env.root, CapabilitySet(), "600000.SH", "metrics", before=before)
    assert result["complete"] is False
    assert demand.history_start(env.root, "metrics", "600000.SH") == before.isoformat()
    stored = pl.read_parquet(env.root / "financials/metrics/part.parquet")
    assert "2023-12-31" in stored["period_end"].to_list()
    # 近期或无范围的计算取数不应清除其范围外的旧缺期。
    env.fetch.side_effect = None
    env.fetch.return_value = metric_rows(["2026-06-30"])
    demand.fetch_stock_history(env.root, CapabilitySet(), "600000.SH", "metrics")
    demand.fetch_stock(env.root, CapabilitySet(), "600000.SH", "metrics")
    assert demand.missing_periods(env.root, "metrics", "600000.SH") == ["2024-03-31"]
    assert demand.history_start(env.root, "metrics", "600000.SH") == before.isoformat()
    # 原游标下继续补采同一年，成功后才允许向更早年度前进。
    env.fetch.side_effect = None
    env.fetch.return_value = metric_rows(["2024-03-31", "2023-12-31"])
    result = demand.fetch_stock_history(env.root, CapabilitySet(), "600000.SH", "metrics", before=before)
    assert result["complete"] is True and not result["reused"]
    assert demand.history_start(env.root, "metrics", "600000.SH") == "2023-10-18"
    assert demand.missing_periods(env.root, "metrics", "600000.SH") == []


def test_page_partial_success_never_satisfies_strict_computation_and_source_switch(env, monkeypatch):
    error = missing_metrics(metric_rows(["2026-06-30"]), ["2026-09-30"])
    env.fetch.side_effect = error
    demand.fetch_stock_history(env.root, CapabilitySet(), "600000.SH", "metrics")
    # 即使日期范围相同，计算入口仍保留缺数失败语义。
    with pytest.raises(ProviderFinancialDataUnavailableError):
        demand.fetch_stock(env.root, CapabilitySet(), "600000.SH", "metrics",
                           start_date=date(2024, 10, 18), end_date=date(2026, 10, 18))
    assert env.fetch.call_count == 2
    monkeypatch.setattr(demand.preferences, "get_financial_provider", lambda: "another")
    assert demand.missing_periods(env.root, "metrics", "600000.SH") == []


def test_concurrent_page_and_strict_request_do_not_share_partial_success(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)
    def fetch(*args, **kwargs):
        barrier.wait(timeout=5)
        raise missing_metrics(metric_rows(["2026-06-30"]), ["2026-09-30"])
    env.fetch.side_effect = fetch
    with ThreadPoolExecutor(max_workers=2) as pool:
        page = pool.submit(demand.fetch_stock_history, env.root, CapabilitySet(), "600000.SH", "metrics")
        strict = pool.submit(demand.fetch_stock, env.root, CapabilitySet(), "600000.SH", "metrics",
                             start_date=date(2024, 10, 18), end_date=date(2026, 10, 18))
        assert page.result(timeout=5)["complete"] is False
        with pytest.raises(ProviderFinancialDataUnavailableError):
            strict.result(timeout=5)
    assert env.fetch.call_count == 2


def test_unstructured_missing_error_is_not_silently_accepted_as_partial_success(env):
    env.fetch.side_effect = ProviderFinancialDataUnavailableError(
        metric_rows(["2026-06-30"]), {"600000.SH": "missing report"},
    )
    with pytest.raises(ProviderFinancialDataUnavailableError):
        demand.fetch_stock_history(env.root, CapabilitySet(), "600000.SH", "metrics")
    assert not (env.root / "financials/metrics/part.parquet").exists()


@pytest.mark.parametrize("failure", ["network", "cancel", "auth", "foreign", "invalid_period"])
def test_partial_fetch_still_checks_failures_cancellation_auth_and_scope_before_writes(env, failure):
    from threading import Event

    from app.services.pipeline_jobs import JobCancelledError

    cancel = Event()
    frame = metric_rows(["2026-06-30"])
    periods = ["2026-09-30"]
    if failure == "foreign":
        frame = frame.with_columns(pl.lit("600999.SH").alias("symbol"))
    if failure == "invalid_period":
        periods = ["2027-09-30"]
    error = missing_metrics(frame, periods)

    def fetch(*args, **kwargs):
        if failure == "cancel":
            cancel.set()
        raise RuntimeError("network failed") if failure == "network" else error

    env.fetch.side_effect = fetch
    check = Mock(side_effect=PermissionError("session revoked")) if failure == "auth" else None
    with pytest.raises((RuntimeError, ValueError, PermissionError, JobCancelledError)):
        demand.fetch_stock_history(env.root, CapabilitySet(), "600000.SH", "metrics",
                                   before_write=check, cancel_event=cancel)
    assert not (env.root / "financials/metrics/part.parquet").exists()
    assert not (env.root / "financials/versions/metrics/part.parquet").exists()
    assert not (env.root / "financials/demand/metrics/600000.SH.json").exists()
