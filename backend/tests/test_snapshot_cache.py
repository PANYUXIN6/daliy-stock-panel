from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.services.snapshot_cache import SnapshotCache


def test_concurrent_cold_requests_share_one_build_and_failure_is_retryable():
    cache = SnapshotCache(60, 2)
    entered, release = Event(), Event()
    def build():
        entered.set()
        assert release.wait(2)
        return object()
    build = Mock(side_effect=build)
    with ThreadPoolExecutor(8) as pool:
        futures = [pool.submit(cache.get_or_compute, 'same', build) for _ in range(8)]
        assert entered.wait(2)
        release.set()
        results = [f.result(2) for f in futures]
    assert build.call_count == 1
    assert all(result is results[0] for result in results)
    with pytest.raises(ValueError):
        cache.get_or_compute('error', lambda: (_ for _ in ()).throw(ValueError()))
    assert cache.get_or_compute('error', lambda: 42) == 42


def test_clear_during_build_cannot_republish_old_snapshot():
    cache = SnapshotCache(60, 2)
    entered, release = Event(), Event()
    def old():
        entered.set()
        assert release.wait(2)
        return 'old'
    with ThreadPoolExecutor(1) as pool:
        result = pool.submit(cache.get_or_compute, 'key', old)
        assert entered.wait(2)
        cache.clear()
        assert cache.get_or_compute('key', lambda: 'new') == 'new'
        release.set()
        assert result.result(2) == 'old'
    assert cache.get_or_compute('key', lambda: 'wrong') == 'new'


def test_ttl_eviction_and_uncacheable_fallback(monkeypatch):
    from app.services import snapshot_cache
    clock = [0.0]
    monkeypatch.setattr(snapshot_cache.time, 'monotonic', lambda: clock[0])
    cache = SnapshotCache(5, 2)
    cache.get_or_compute('a', lambda: 1)
    cache.get_or_compute('b', lambda: 2)
    assert cache.get_or_compute('a', lambda: 0) == 1
    cache.get_or_compute('c', lambda: 3)
    assert cache.get_or_compute('b', lambda: 4) == 4
    clock[0] = 6
    assert cache.get_or_compute('b', lambda: 5) == 5
    assert cache.get_or_compute('fallback', lambda: None, cache_if=bool) is None
    assert cache.get_or_compute('fallback', lambda: 'recovered') == 'recovered'


def test_overview_dates_have_independent_bounded_cache(monkeypatch, tmp_path):
    from datetime import date

    from app.api import overview
    overview.invalidate_overview_cache()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)))))
    build = Mock(side_effect=lambda req, day: {'day': day})
    monkeypatch.setattr(overview, '_build_overview', build)
    for _ in range(2):
        for day in (date(2026, 1, 1), date(2026, 1, 2)):
            assert overview.market_overview(request, day) == {'day': day}
    assert build.call_count == 2
