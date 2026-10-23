from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.api import screener as screener_api
from app.market_time import CN_TZ
from app.services import screener_quotes

pytestmark = pytest.mark.usefixtures("private_owner")

class _MonitorEngine:
    def __init__(self, results=None):
        self.results = results or {}

    def latest_strategy_results(self):
        return self.results


def _request(tmp_path, monitor_results=None):
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path))
    state = SimpleNamespace(repo=repo, monitor_engine=_MonitorEngine(monitor_results))
    state.user_runtimes = SimpleNamespace(get=lambda owner: state)
    return SimpleNamespace(app=SimpleNamespace(state=state))


def test_cached_summary_omits_rows_and_counts_realtime_expirations(monkeypatch, tmp_path):
    cached = {
        "as_of": "2026-07-20",
        "results": {
            "strategy_a": {
                "as_of": "2026-07-20",
                "total": 2,
                "rows": [{"symbol": "000001.SZ"}, {"symbol": "000002.SZ"}],
            },
        },
        "today_ever_rows": {
            "strategy_a": {
                "000001.SZ": {"symbol": "000001.SZ"},
                "600000.SH": {"symbol": "600000.SH"},
            },
        },
        "updated_at": 1,
    }
    realtime = {
        "strategy_a": {
            "as_of": "2026-07-20",
            "total": 2,
            "rows": [{"symbol": "000002.SZ"}, {"symbol": "300001.SZ"}],
        },
    }
    path = screener_api.strategy_cache._cache_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json
    path.write_text(json.dumps(cached))

    payload = screener_api.get_cached_summary(_request(tmp_path, realtime))

    assert payload["results"] == {
        "strategy_a": {"total": 2, "as_of": "2026-07-20", "computed_at": None}
    }
    assert payload["today_ever_counts"] == {"strategy_a": 4}
    assert "rows" not in payload["results"]["strategy_a"]


def test_cached_result_returns_only_requested_rows_with_ext_and_strategy_membership(monkeypatch, tmp_path):
    cached = {
        "as_of": "2026-07-20",
        "results": {
            "strategy_a": {
                "as_of": "2026-07-20",
                "total": 1,
                "rows": [{"symbol": "000001.SZ"}],
            },
            "strategy_b": {
                "as_of": "2026-07-20",
                "total": 2,
                "rows": [{"symbol": "000001.SZ"}, {"symbol": "600000.SH"}],
            },
        },
        "today_ever_rows": {
            "strategy_a": {
                "000001.SZ": {"symbol": "000001.SZ"},
                "000002.SZ": {"symbol": "000002.SZ"},
            },
        },
        "updated_at": 1,
    }
    monkeypatch.setattr(screener_api.strategy_cache, "read_cache", lambda *_args: cached)
    monkeypatch.setattr(
        screener_api,
        "_load_ext_value_maps",
        lambda *_args: {"concept.concept": {"000001.SZ": "银行", "000002.SZ": "科技"}},
    )

    payload = screener_api.get_cached_result(
        "strategy_a",
        _request(tmp_path),
        ext_columns="concept.concept",
    )

    assert payload["result"]["strategy"] == "strategy_a"
    assert payload["result"]["rows"] == [{"symbol": "000001.SZ", "concept.concept": "银行"}]
    assert payload["today_ever_rows"]["000002.SZ"]["concept.concept"] == "科技"
    assert payload["strategy_ids_by_symbol"] == {"000001.SZ": ["strategy_a", "strategy_b"]}


@pytest.mark.parametrize('run_day', ['2026-07-17', '2026-07-21'])
def test_single_run_on_other_date_preserves_current_cache(tmp_path, run_day):
    cache = screener_api.strategy_cache
    cache.write_cache(tmp_path, '2026-07-20', {
        'a': {'as_of': '2026-07-20', 'total': 1, 'rows': [{'symbol': 'A'}]},
        'b': {'as_of': '2026-07-20', 'total': 1, 'rows': [{'symbol': 'B'}]},
    })
    cache.write_cache(tmp_path, '2026-07-20', {'a': {'as_of': '2026-07-20', 'total': 0, 'rows': []}})
    before = cache._cache_path(tmp_path).read_bytes()

    screener_api._update_cache_strategy(tmp_path, run_day, 'a', {'total': 1, 'rows': [{'symbol': 'H'}]})

    assert cache._cache_path(tmp_path).read_bytes() == before
    assert cache.read_summary(tmp_path)['today_ever_counts'] == {'a': 1, 'b': 1}
    assert set(cache.read_cache(tmp_path)['today_ever_rows']['a']) == {'A'}


def test_single_run_without_existing_cache_does_not_create_one(tmp_path):
    screener_api._update_cache_strategy(tmp_path, '2026-07-17', 'a', {'total': 0, 'rows': []})
    assert screener_api.strategy_cache.read_cache(tmp_path) is None


def test_same_date_single_run_preserves_other_strategies_and_ever_rows(tmp_path):
    cache = screener_api.strategy_cache
    cache.write_cache(tmp_path, '2026-07-20', {
        'a': {'as_of': '2026-07-20', 'rows': [{'symbol': 'A'}]},
        'b': {'as_of': '2026-07-20', 'rows': [{'symbol': 'B'}]},
    })
    screener_api._update_cache_strategy(tmp_path, '2026-07-20', 'a', {
        'total': 1, 'rows': [{'symbol': 'C'}], 'warnings': ['历史数据不足'],
    })
    cached = cache.read_cache(tmp_path)
    assert set(cached['results']) == {'a', 'b'}
    assert cached['results']['a']['rows'] == [{'symbol': 'C'}]
    assert cached['results']['a']['warnings'] == ['历史数据不足']
    assert set(cached['today_ever_rows']['a']) == {'A', 'C'}
    assert cached['today_ever_rows']['b'] == {'B': {'symbol': 'B'}}


@pytest.fixture
def quote_case(monkeypatch, tmp_path):
    monkeypatch.setattr(screener_quotes, "cn_today", lambda: date(2026, 10, 22))
    row = {
        "symbol": "002543.SZ", "date": "2026-10-22", "close": 8.23,
        "change_pct": 0.0249, "ma20": 7.5, "score": 50, "signal_breakout": True,
    }
    cached = {
        "as_of": "2026-10-22", "updated_at": 123,
        "results": {"a": {"as_of": "2026-10-22", "total": 1, "rows": [row]}},
        "today_ever_rows": {"a": {"002543.SZ": dict(row)}},
    }
    monkeypatch.setattr(screener_api.strategy_cache, "read_cache", lambda *_: cached)
    request = _request(tmp_path)
    live = {
        **row, "close": 8.39, "change_pct": 0.04483, "amount": 3.63e8,
        "score": 99, "ma20": 8.0, "signal_breakout": False,
        "quote_ts": int(datetime(2026, 10, 22, 15, tzinfo=CN_TZ).timestamp() * 1000),
    }
    loads = []

    def load(*, refresh):
        loads.append(refresh)
        return pl.DataFrame([live]), date(2026, 10, 22)

    request.app.state.repo.get_enriched_latest = load
    return request, cached, live, loads


@pytest.mark.parametrize("single", [False, True])
def test_quote_display_updates_without_mutating_signal_cache(quote_case, single):
    request, cached, _, loads = quote_case
    original = deepcopy(cached)
    # 普通账号没有监控引擎，也能看到最新的公开行情。
    request.app.state.monitor_engine = None
    if single:
        payload = screener_api.get_cached_result("a", request, ext_columns=None)
        row = payload["result"]["rows"][0]
        ever = payload["today_ever_rows"]["002543.SZ"]
    else:
        payload = screener_api.get_cached(request, ext_columns=None)
        row = payload["results"]["a"]["rows"][0]
        ever = payload["today_ever_rows"]["a"]["002543.SZ"]
    assert row["close"] == 8.39
    assert row["change_pct"] == 0.04483
    assert row["score"] == 50
    assert row["ma20"] == 7.5
    assert row["signal_breakout"] is True
    assert ever["close"] == 8.39
    assert ever["score"] == 50
    assert payload["updated_at"] == 123
    assert loads == [False]
    assert cached == original


@pytest.mark.parametrize("as_of", ["2026-10-21", "2026-10-23", None, "invalid"])
def test_non_current_or_undated_results_do_not_receive_today_quotes(quote_case, as_of):
    request, cached, _, loads = quote_case
    cached["as_of"] = as_of
    cached["results"]["a"]["as_of"] = as_of
    cached["results"]["a"]["rows"][0].pop("date")
    cached["today_ever_rows"]["a"]["002543.SZ"].pop("date")
    payload = screener_api.get_cached(request, ext_columns=None)
    assert payload == cached
    assert not loads


@pytest.mark.parametrize("field,value", [
    ("date", "2026-10-21"), ("date", "2026-10-23"),
    ("quote_ts", 1), ("quote_ts", float("inf")),
    ("close", 0), ("close", float("nan")),
])
def test_stale_or_invalid_quote_rows_do_not_replace_valid_cached_prices(quote_case, field, value):
    request, cached, live, _ = quote_case
    live[field] = value
    assert screener_api.get_cached(request, ext_columns=None) == cached


def test_previous_day_frame_and_cold_cache_leave_results_unchanged(quote_case):
    request, cached, live, _ = quote_case
    for frame, day in [(pl.DataFrame(), None), (pl.DataFrame([live]), date(2026, 10, 21))]:
        request.app.state.repo.get_enriched_latest = lambda *, refresh, frame=frame, day=day: (frame, day)
        assert screener_api.get_cached(request, ext_columns=None) == cached


def test_historical_row_inside_today_result_is_not_overwritten(quote_case):
    request, cached, _, _ = quote_case
    cached["results"]["a"]["rows"][0]["date"] = "2026-10-21"
    payload = screener_api.get_cached(request, ext_columns=None)
    assert payload["results"]["a"]["rows"][0]["close"] == 8.23
    assert payload["today_ever_rows"]["a"]["002543.SZ"]["close"] == 8.39


def test_single_result_does_not_project_other_strategies(quote_case):
    request, cached, _, _ = quote_case
    cached["results"]["b"] = deepcopy(cached["results"]["a"])
    projected = screener_quotes.with_live_quotes(cached, request.app.state.repo, strategy_id="a")
    assert projected["results"]["b"] is cached["results"]["b"]
    assert projected["results"]["a"]["rows"][0]["close"] == 8.39


def test_older_same_day_quote_does_not_roll_back_newer_scan(quote_case):
    request, cached, live, _ = quote_case
    cached["results"]["a"]["rows"][0]["quote_ts"] = live["quote_ts"] + 1000
    cached["today_ever_rows"]["a"]["002543.SZ"]["quote_ts"] = live["quote_ts"] + 1000
    assert screener_api.get_cached(request, ext_columns=None) == cached


def test_partial_quote_does_not_mix_new_price_with_old_display_fields(quote_case):
    request, cached, live, _ = quote_case
    cached["results"]["a"]["rows"][0]["raw_close"] = 8.23
    cached["results"]["a"]["rows"][0]["quote_ts"] = live["quote_ts"] - 1000
    live.pop("change_pct")
    live.pop("quote_ts")  # 正式日线无盘中快照时刻。
    row = screener_api.get_cached_result("a", request, ext_columns=None)["result"]["rows"][0]
    assert row["close"] == 8.39
    assert row["change_pct"] is None
    assert row["raw_close"] is None
    assert row["quote_ts"] is None
    assert row["score"] == 50


def test_etf_rows_are_not_replaced_by_stock_snapshot(quote_case):
    request, cached, _, _ = quote_case
    cached["results"]["a"]["rows"][0]["symbol"] = "510300.SH"
    cached["today_ever_rows"] = {}
    assert screener_api.get_cached(request, ext_columns=None) == cached
