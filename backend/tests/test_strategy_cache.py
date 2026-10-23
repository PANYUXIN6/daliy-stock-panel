from __future__ import annotations

import pytest

from app.services import strategy_cache

pytestmark = pytest.mark.usefixtures("private_owner")



def _result(*symbols: str) -> dict:
    return {
        "total": len(symbols),
        "as_of": "2026-07-20",
        "rows": [{"symbol": symbol, "close": index + 1.0} for index, symbol in enumerate(symbols)],
    }


def test_summary_reuses_parse_and_detects_external_replace(tmp_path, monkeypatch):
    import json
    from unittest.mock import Mock

    strategy_cache.write_cache(tmp_path, "2026-07-20", {"a": _result("A")})
    loads = Mock(wraps=json.loads)
    monkeypatch.setattr(strategy_cache.json, 'loads', loads)
    first = strategy_cache.read_summary(tmp_path)
    first['results']['a']['total'] = 999
    full = strategy_cache.read_cache(tmp_path)
    full['results']['a']['rows'].clear()
    assert strategy_cache.read_summary(tmp_path)['results']['a']['total'] == 1
    assert strategy_cache.read_cache(tmp_path)['results']['a']['rows']
    assert loads.call_count == 1
    path = strategy_cache._cache_path(tmp_path)
    replacement = path.with_suffix('.external')
    replacement.write_text(json.dumps({'as_of': '2026-07-20', 'results': {'b': _result('B', 'C')}}))
    replacement.replace(path)
    assert strategy_cache.read_summary(tmp_path)['results']['b']['total'] == 2
    assert loads.call_count == 2


def test_different_owners_do_not_share_file_lock_or_results(tmp_path, private_owner):
    from concurrent.futures import ThreadPoolExecutor

    from app.services.path_locks import path_lock
    from app.services.user_context import UserContext, user_scope

    def other_user():
        with user_scope(UserContext('b' * 32, 'admin', tmp_path)):
            strategy_cache.write_cache(tmp_path, '2026-07-20', {'other': _result('B')})
            return strategy_cache.read_summary(tmp_path)
    with ThreadPoolExecutor(1) as pool, path_lock(strategy_cache._cache_path(tmp_path)):
        result = pool.submit(other_user).result(5)
    assert set(result['results']) == {'other'}
    assert strategy_cache.read_summary(tmp_path)['results'] == {}


def test_same_day_partial_writes_merge_strategy_results(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("000001.SZ")})
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_b": _result("600000.SH")})

    cached = strategy_cache.read_cache(tmp_path)

    assert set(cached["results"]) == {"strategy_a", "strategy_b"}
    assert cached["results"]["strategy_a"]["rows"][0]["symbol"] == "000001.SZ"
    assert cached["results"]["strategy_b"]["rows"][0]["symbol"] == "600000.SH"


def test_same_day_update_replaces_only_target_strategy_and_keeps_ever_rows(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {
        "strategy_a": _result("000001.SZ"),
        "strategy_b": _result("600000.SH"),
    })
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("000002.SZ")})

    cached = strategy_cache.read_cache(tmp_path)

    assert [row["symbol"] for row in cached["results"]["strategy_a"]["rows"]] == ["000002.SZ"]
    assert [row["symbol"] for row in cached["results"]["strategy_b"]["rows"]] == ["600000.SH"]
    assert set(cached["today_ever_rows"]["strategy_a"]) == {"000001.SZ", "000002.SZ"}


def test_new_date_resets_results_and_ever_rows(tmp_path):
    strategy_cache.write_cache(tmp_path, "2026-07-20", {"strategy_a": _result("000001.SZ")})
    next_day = _result("600000.SH")
    next_day["as_of"] = "2026-07-21"

    strategy_cache.write_cache(tmp_path, "2026-07-21", {"strategy_b": next_day})
    cached = strategy_cache.read_cache(tmp_path)

    assert cached["as_of"] == "2026-07-21"
    assert set(cached["results"]) == {"strategy_b"}
    assert set(cached["today_ever_rows"]) == {"strategy_b"}


def test_single_update_checks_date_after_waiting_for_concurrent_rollover(tmp_path):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from contextvars import copy_context

    from app.services.path_locks import path_lock

    strategy_cache.write_cache(tmp_path, '2026-07-20', {'old': _result('A')})
    started = threading.Event()
    def update_previous_day():
        started.set()
        strategy_cache.write_cache(tmp_path, '2026-07-20', {'old': _result('H')}, same_date_only=True)
    with ThreadPoolExecutor(1) as pool:
        with path_lock(strategy_cache._cache_path(tmp_path)):
            future = pool.submit(copy_context().run, update_previous_day)
            assert started.wait(5)
            strategy_cache.write_cache(tmp_path, '2026-07-21', {
                'new': {**_result('B'), 'as_of': '2026-07-21'},
            })
            after_rollover = strategy_cache.read_cache(tmp_path)
        future.result(5)
    assert strategy_cache.read_cache(tmp_path) == after_rollover


def _verified_result(confirmed: str, pending: str, checked_at: str) -> dict:
    result = _result(confirmed)
    result["verification"] = {
        "status": "completed", "checked_at": checked_at, "candidate_count": 2,
        "confirmed_count": 1, "pending_count": 1, "excluded_count": 0,
        "candidates": [
            {"symbol": confirmed, "verification_status": "confirmed"},
            {"symbol": pending, "verification_status": "pending"},
        ],
    }
    return result


def test_same_day_readonly_recompute_preserves_verification_without_admitting_pending(tmp_path):
    strategy_id = "earnings_breakout_gap"
    explicit = _verified_result("600000.SH", "600001.SH", "2026-07-20T08:00:00Z")
    strategy_cache.write_cache(tmp_path, "2026-07-20", {strategy_id: explicit})
    recomputed = _result("600000.SH")
    recomputed["rows"][0]["close"] = 12.5

    strategy_cache.write_cache(tmp_path, "2026-07-20", {strategy_id: recomputed})
    cached = strategy_cache.read_cache(tmp_path)

    assert cached["results"][strategy_id]["verification"] == explicit["verification"]
    assert cached["results"][strategy_id]["rows"] == recomputed["rows"]
    assert cached["results"][strategy_id]["total"] == 1
    assert set(cached["today_ever_rows"][strategy_id]) == {"600000.SH"}
    assert cached["today_ever_matched"][strategy_id] == ["600000.SH"]


def test_same_day_explicit_verification_replaces_old_candidates_without_admitting_pending(tmp_path):
    strategy_id = "earnings_breakout_gap"
    previous = _verified_result("600000.SH", "600001.SH", "2026-07-20T08:00:00Z")
    latest = _verified_result("000001.SZ", "600002.SH", "2026-07-20T09:00:00Z")
    strategy_cache.write_cache(tmp_path, "2026-07-20", {strategy_id: previous})

    strategy_cache.write_cache(tmp_path, "2026-07-20", {strategy_id: latest})
    cached = strategy_cache.read_cache(tmp_path)

    assert cached["results"][strategy_id]["verification"] == latest["verification"]
    assert cached["results"][strategy_id]["rows"] == latest["rows"]
    assert cached["results"][strategy_id]["total"] == 1
    assert set(cached["today_ever_rows"][strategy_id]) == {"600000.SH", "000001.SZ"}
    assert set(cached["today_ever_matched"][strategy_id]) == {"600000.SH", "000001.SZ"}


def test_new_day_same_strategy_drops_old_verification_and_pending_candidates(tmp_path):
    strategy_id = "earnings_breakout_gap"
    previous = _verified_result("600000.SH", "600001.SH", "2026-07-20T08:00:00Z")
    strategy_cache.write_cache(tmp_path, "2026-07-20", {strategy_id: previous})
    next_day = _result("000001.SZ")
    next_day["as_of"] = "2026-07-21"

    strategy_cache.write_cache(tmp_path, "2026-07-21", {strategy_id: next_day})
    cached = strategy_cache.read_cache(tmp_path)

    assert "verification" not in cached["results"][strategy_id]
    assert cached["results"][strategy_id]["rows"] == next_day["rows"]
    assert cached["results"][strategy_id]["total"] == 1
    assert set(cached["today_ever_rows"][strategy_id]) == {"000001.SZ"}
    assert cached["today_ever_matched"][strategy_id] == ["000001.SZ"]
