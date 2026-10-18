"""扶摇并行采集的发包限速、数据一致性和取消边界。"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from itertools import pairwise
from threading import Event, Lock, get_ident

import httpx
import polars as pl
import pytest

from app.data_providers import base
from app.plugins.fuyao import client as fc
from app.plugins.fuyao import provider as fp
from app.services import financial_sync, preferences
from app.services.pipeline_jobs import JobCancelledError
from app.tickflow.capabilities import CapabilitySet

_SYMBOLS = ["600000.SH", "600001.SH", "600002.SH", "600003.SH"]
_INCOME = {
    "period_end_ms": 1782748800000,
    "report_date_ms": 1786723200000,
    "fiscal_year": 2026,
    "fiscal_period": "Q2",
    "basic_eps": 2.5,
    "net_profit": -12.5,
}


@pytest.fixture(autouse=True)
def fast_financial_gate(monkeypatch):
    monkeypatch.setattr(fc, "_FINANCIAL_REQUEST_INTERVAL_S", 0)
    monkeypatch.setattr(fc, "_next_financial_request_at", 0.)


def _http_client(handler):
    client = fc.FuyaoClient(api_key="test-key")
    client._http.close()
    client._http = httpx.Client(base_url=fc.BASE_URL, transport=httpx.MockTransport(handler))
    return client


def _provider(monkeypatch, client):
    provider = fp.FuyaoProvider()
    provider._client = client
    monkeypatch.setattr(fp, "_HIST_INTERVAL_S", 0)
    return provider


def _install_financial_provider(monkeypatch, provider):
    from app.data_providers import custom

    monkeypatch.setattr(financial_sync, "_financial_is_custom", lambda: True)
    monkeypatch.setattr(preferences, "get_financial_provider", lambda: "fuyao")
    monkeypatch.setattr(custom, "get_provider", lambda name: provider)


def _old_table(tmp_path, table):
    path = tmp_path / "financials" / table / "part.parquet"
    path.parent.mkdir(parents=True)
    pl.DataFrame({
        "symbol": _SYMBOLS, "period_end": ["2025-12-31"] * len(_SYMBOLS),
        "announce_date": ["2026-03-20"] * len(_SYMBOLS),
        "net_income": [100.] * len(_SYMBOLS),
    }).write_parquet(path)
    return path, path.read_bytes()


@pytest.mark.parametrize("missing_attempts", [1, 2])
def test_confirmed_missing_metrics_are_recorded_and_other_batches_continue(monkeypatch, tmp_path, missing_attempts):
    monkeypatch.setattr(fc, "_REQUEST_INTERVAL_S", 0)
    symbols = ["600817.SH", "600000.SH", "600001.SH"]
    requests = []

    def respond(request):
        symbol = request.url.params.get("thscode")
        if request.url.path.endswith("income-statements"):
            return httpx.Response(200, json={"code": 0, "data": {"item": [dict(_INCOME)]}})
        if request.url.path.endswith("indicators"):
            requests.append(symbol)
            if symbol == symbols[0] and requests.count(symbol) <= missing_attempts:
                return httpx.Response(200, json={"code": 5003, "message": "Data source unavailable"})
            return httpx.Response(200, json={"code": 0, "data": {"abilities": [
                {"indicators": [{"index_id": "index_weighted_avg_roe", "value": "12.5"}]},
            ]}})
        return httpx.Response(200, json={"code": 0, "data": {"list": []}})

    client = _http_client(respond)
    provider = _provider(monkeypatch, client)
    provider.financial_sync_batch_size = 2
    _install_financial_provider(monkeypatch, provider)
    try:
        financial_sync._sync_history_table_for_symbols("metrics", symbols, tmp_path, CapabilitySet())
    finally:
        client.close()
    state = json.loads((tmp_path / "financials/sync_progress/metrics.json").read_text())
    saved = pl.read_parquet(tmp_path / "financials/versions/metrics/part.parquet")
    assert state["status"] == "completed"
    assert requests.count(symbols[0]) == 2
    assert saved["source"].unique().to_list() == ["fuyao"]
    assert saved["roe"].to_list() == [12.5] * saved.height
    if missing_attempts == 2:
        assert set(saved["symbol"]) == set(symbols[1:])
        assert set(state["completed_symbols"]) == set(symbols[1:])
        assert list(state["skipped_symbols"]) == [symbols[0]]
        assert "5003" in state["skipped_symbols"][symbols[0]]
    else:
        assert set(saved["symbol"]) == set(symbols)
        assert not state["skipped_symbols"]


@pytest.mark.parametrize("status,code,message", [
    (502, 0, "Bad gateway"),
    (200, 5003, "Unexpected internal error"),
    (200, 4001, "rate limited"),
])
def test_other_metric_errors_do_not_become_missing_data_skips(monkeypatch, tmp_path, status, code, message):
    monkeypatch.setattr(fc, "_REQUEST_INTERVAL_S", 0)

    def respond(request):
        if request.url.path.endswith("income-statements"):
            return httpx.Response(200, json={"code": 0, "data": {"item": [dict(_INCOME)]}})
        if request.url.path.endswith("indicators"):
            return httpx.Response(status, json={"code": code, "message": message})
        return httpx.Response(200, json={"code": 0, "data": {"list": []}})

    client = _http_client(respond)
    _install_financial_provider(monkeypatch, _provider(monkeypatch, client))
    try:
        with pytest.raises(fc.FuyaoError):
            financial_sync._sync_history_table_for_symbols(
                "metrics", ["600817.SH"], tmp_path, CapabilitySet(),
            )
    finally:
        client.close()
    state = json.loads((tmp_path / "financials/sync_progress/metrics.json").read_text())
    assert not state.get("skipped_symbols")
    assert not state["completed_symbols"]
    assert not (tmp_path / "financials/versions/metrics/part.parquet").exists()


def test_request_spacing_is_shared_across_concurrent_clients(monkeypatch):
    # 同账号多个客户端必须共用节流；慢响应可以重叠，发包不得集中突发。
    interval = .02
    monkeypatch.setattr(fc, "_REQUEST_INTERVAL_S", interval, raising=False)
    lock = Lock()
    starts = []
    active = peak = 0

    def respond(request):
        nonlocal active, peak
        with lock:
            starts.append(time.monotonic())
            active += 1
            peak = max(peak, active)
        time.sleep(.07)
        with lock:
            active -= 1
        return httpx.Response(200, json={"code": 0, "data": {"value": 1}})

    clients = [_http_client(respond), _http_client(respond)]
    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(
                lambda i: clients[i % 2]._get("/test", {}), range(6),
            ))
    finally:
        for client in clients:
            client.close()

    assert results == [{"value": 1}] * 6
    assert peak == 2
    starts.sort()
    # 调度时间存在小幅抖动，保留宽容；缺少共享gate的集中发包仍会失败。
    assert all(b - a >= interval * .75 for a, b in pairwise(starts))


def test_financial_spacing_is_shared_without_slowing_other_endpoint_policy(monkeypatch):
    monkeypatch.setattr(fc, "_REQUEST_INTERVAL_S", 0)
    monkeypatch.setattr(fc, "_FINANCIAL_REQUEST_INTERVAL_S", .02)
    lock, starts = Lock(), []
    def respond(request):
        with lock:
            starts.append(time.monotonic())
        return httpx.Response(200, json={"code": 0, "data": {"item": []}})
    clients = [_http_client(respond), _http_client(respond)]
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(lambda i: clients[i % 2].financial_statements("income", _SYMBOLS[i]), range(4)))
    finally:
        for client in clients:
            client.close()
    assert all(b - a >= .015 for a, b in pairwise(sorted(starts)))


@pytest.mark.parametrize("status,code", [(429, 0), (200, 4001), (200, "4001")])
def test_rate_limit_is_distinguishable_and_does_not_retry(monkeypatch, status, code):
    monkeypatch.setattr(fc, "_REQUEST_INTERVAL_S", 0, raising=False)
    requests = []

    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(status, json={"code": code, "message": "rate limited"})

    client = _http_client(respond)
    try:
        with pytest.raises(fc.FuyaoError) as caught:
            client.financial_statements("income", _SYMBOLS[0])
    finally:
        client.close()

    assert len(requests) == 1
    assert isinstance(caught.value, base.ProviderRateLimitError)


def test_income_overlaps_two_requests_and_preserves_normalized_results(monkeypatch):
    lock = Lock()
    second_finished = Event()
    completed = []
    callbacks = []
    active = peak = 0

    class Client:
        def financial_statements(self, table, symbol, limit, *, cancel_event=None):
            nonlocal active, peak
            assert table == "income" and limit == 20
            with lock:
                active += 1
                peak = max(peak, active)
            if symbol == _SYMBOLS[0]:
                assert second_finished.wait(1), "第二个请求没有并行执行"
            with lock:
                completed.append(symbol)
                active -= 1
            if symbol == _SYMBOLS[1]:
                second_finished.set()
            return [dict(_INCOME, thscode=symbol)]

    provider = _provider(monkeypatch, Client())
    main_thread = get_ident()
    result = provider.get_financials(
        "income", _SYMBOLS, latest_only=False,
        on_chunk_done=lambda done, total: callbacks.append((done, total, get_ident())),
    )

    assert peak == 2
    assert completed.index(_SYMBOLS[1]) < completed.index(_SYMBOLS[0])
    assert result.sort("symbol").select(
        "symbol", "period_end", "announce_date", "net_income", "basic_eps",
    ).rows() == [(symbol, "2026-06-30", "2026-08-15", -12.5, 2.5) for symbol in _SYMBOLS]
    assert callbacks[-1][:2] == (len(_SYMBOLS), len(_SYMBOLS))
    assert all(thread == main_thread for _, _, thread in callbacks)
    assert [done for done, _, _ in callbacks] == sorted(done for done, _, _ in callbacks)


def test_cancel_stops_pending_symbols_and_preserves_previous_table(monkeypatch, tmp_path):
    second_started = Event()
    release = Event()
    calls = []
    finished = []

    class Client:
        def financial_statements(self, table, symbol, limit, *, cancel_event=None):
            calls.append(symbol)
            if symbol == _SYMBOLS[0]:
                assert second_started.wait(1), "第二个请求没有并行执行"
            elif symbol == _SYMBOLS[1]:
                second_started.set()
                assert release.wait(1), "取消后仍在途的请求未能收尾"
            finished.append(symbol)
            return [dict(_INCOME, thscode=symbol)]

    provider = _provider(monkeypatch, Client())
    _install_financial_provider(monkeypatch, provider)
    path, old_bytes = _old_table(tmp_path, "income")

    def progress(done, total):
        if done == 1:
            release.set()
            raise JobCancelledError("test")

    try:
        with pytest.raises(JobCancelledError):
            financial_sync._sync_history_table_for_symbols(
                "income", _SYMBOLS, tmp_path, CapabilitySet(), on_progress=progress,
            )
    finally:
        release.set()

    assert set(calls) == set(_SYMBOLS[:2])
    assert set(finished) == set(calls)
    assert path.read_bytes() == old_bytes


def test_worker_rate_limit_preserves_history_and_stops_following_tables(monkeypatch, tmp_path):
    monkeypatch.setattr(fc, "_REQUEST_INTERVAL_S", .002, raising=False)
    limited = Event()
    calls = []

    def respond(request):
        symbol = request.url.params.get("thscode")
        calls.append((request.url.path, symbol))
        if symbol == _SYMBOLS[0]:
            # 让第二个在途请求的限流先被协调线程发现，第一份结果随后收尾。
            limited.wait(1)
            time.sleep(.02)
        if symbol == _SYMBOLS[1]:
            limited.set()
            return httpx.Response(200, json={"code": 4001, "message": "rate limited"})
        return httpx.Response(200, json={"code": 0, "data": {"item": [dict(_INCOME)]}})

    client = _http_client(respond)
    provider = _provider(monkeypatch, client)
    _install_financial_provider(monkeypatch, provider)
    monkeypatch.setattr(financial_sync, "_get_symbols", lambda data_dir: _SYMBOLS)
    path, old_bytes = _old_table(tmp_path, "metrics")
    try:
        with pytest.raises(fc.FuyaoError) as caught:
            financial_sync.sync_all(tmp_path, CapabilitySet())
    finally:
        client.close()

    assert isinstance(caught.value, base.ProviderRateLimitError)
    assert len(calls) == 2
    assert {symbol for _, symbol in calls} == set(_SYMBOLS[:2])
    assert all(path.endswith("income-statements") for path, _ in calls)
    assert path.read_bytes() == old_bytes
    assert not (tmp_path / "financials" / "income" / "part.parquet").exists()


@pytest.mark.parametrize("stop", ["cancel", "rate_limit"])
def test_stop_during_request_gate_prevents_new_http_and_keeps_old_table(
    monkeypatch, tmp_path, stop,
):
    waiting = Event()
    stopped = Event()
    lock = Lock()
    slots = 0
    wire = []

    def wait_slot(cancel_event=None):
        nonlocal slots
        with lock:
            slots += 1
            index = slots
        if index == 2:
            waiting.set()
            # 确定第二个工作线程仍处于发包门控，等协调线程的取消信号后才返回。
            event = cancel_event if cancel_event is not None else stopped
            assert event.wait(1), "取消未传递给尚未发包的请求"

    def respond(request):
        wire.append((request.url.params["thscode"], stopped.is_set()))
        assert waiting.wait(1), "第二个请求未进入发包门控"
        if stop == "rate_limit":
            stopped.set()
            return httpx.Response(200, json={"code": 4001, "message": "rate limited"})
        return httpx.Response(200, json={"code": 0, "data": {"item": [dict(_INCOME)]}})

    def progress(done, total):
        if stop == "cancel" and done == 1:
            stopped.set()
            raise JobCancelledError("test")

    monkeypatch.setattr(fc, "_wait_request_slot", wait_slot)
    client = _http_client(respond)
    _install_financial_provider(monkeypatch, _provider(monkeypatch, client))
    path, old_bytes = _old_table(tmp_path, "income")
    expected_error = JobCancelledError if stop == "cancel" else fc.FuyaoRateLimitError
    try:
        with pytest.raises(expected_error):
            financial_sync._sync_history_table_for_symbols(
                "income", _SYMBOLS, tmp_path, CapabilitySet(), on_progress=progress,
            )
    finally:
        client.close()

    assert waiting.is_set() and stopped.is_set()
    assert len(wire) == 1
    assert wire[0][0] in _SYMBOLS and wire[0][1] is False
    assert path.read_bytes() == old_bytes
