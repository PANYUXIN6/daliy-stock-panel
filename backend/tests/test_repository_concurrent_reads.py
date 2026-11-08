from concurrent.futures import ThreadPoolExecutor
from threading import Event

import duckdb
import pytest

from app.tickflow.repository import DataStore, KlineRepository


def test_slow_query_does_not_block_an_unrelated_read_and_results_stay_separate(tmp_path):
    repo = KlineRepository(DataStore(tmp_path))
    entered, release = Event(), Event()

    def slow_value() -> int:
        entered.set()
        assert release.wait(5)
        return 17

    repo.db.create_function("slow_value", slow_value)
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(repo.execute_all, "SELECT slow_value()")
        try:
            assert entered.wait(2)
            quick = pool.submit(repo.execute_one, "SELECT ?", [23])
            assert quick.result(timeout=2) == (23,)
        finally:
            release.set()
        assert slow.result(timeout=2) == [(17,)]
    with pytest.raises(duckdb.Error):
        repo.execute_all("SELECT unknown_column")
    assert repo.execute_one("SELECT 31") == (31,)
    repo.db.close()
