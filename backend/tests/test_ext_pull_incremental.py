from datetime import date

import polars as pl
import pytest

from app.services import ext_pull
from app.services.ext_data import ExtConfig, ExtField, PullConfig, rows_to_parquet

DAY = date(2026, 11, 2)


@pytest.fixture
def config():
    return ExtConfig(id="ticks", label="时序", mode="timeseries",
                     fields=[ExtField("symbol"), ExtField("ts", "int"), ExtField("v", "float")],
                     pull=PullConfig(url="https://example.test", time_field="ts", since_param="since",
                                     page_param="page", page_size_param="limit", page_size=2))


async def test_incremental_pages_share_saved_cursor_and_merge_existing_rows(tmp_path, config, monkeypatch):
    rows_to_parquet([{"symbol": "600000.SH", "ts": 1, "v": 1.0},
                     {"symbol": "600000.SH", "ts": 2, "v": 2.0}], config, tmp_path, DAY)
    params = []

    async def request(pull, config_id, day=None, extra_params=None):
        params.append(extra_params)
        times = [3, 4] if extra_params["page"] == "1" else [5]
        return [{"symbol": "600000.SH", "ts": ts, "v": float(ts)} for ts in times]

    monkeypatch.setattr(ext_pull, "_request_json", request)
    n, day = await ext_pull.fetch_and_ingest(config, tmp_path, DAY)
    assert (n, day) == (5, "2026-11-02")
    assert params == [{"since": "2", "page": "1", "limit": "2"}, {"since": "2", "page": "2", "limit": "2"}]
    stored = pl.read_parquet(ext_pull._day_partition(tmp_path, config.id, DAY))
    assert stored["ts"].to_list() == [1, 2, 3, 4, 5]


async def test_empty_increment_is_success_without_changing_saved_data_but_first_pull_is_error(tmp_path, config, monkeypatch):
    rows_to_parquet([{"symbol": "600000.SH", "ts": 1, "v": 1.0}], config, tmp_path, DAY)
    part = ext_pull._day_partition(tmp_path, config.id, DAY)
    original = part.read_bytes()
    calls = []

    async def request(pull, config_id, day=None, extra_params=None):
        calls.append(extra_params)
        return []

    monkeypatch.setattr(ext_pull, "_request_json", request)
    assert await ext_pull.fetch_and_ingest(config, tmp_path, DAY) == (0, DAY.isoformat())
    assert part.read_bytes() == original
    with pytest.raises(ValueError, match="行数为 0"):
        await ext_pull.fetch_and_ingest(config, tmp_path, date(2026, 11, 3))
    assert calls == [{"since": "1", "page": "1", "limit": "2"}, {"page": "1", "limit": "2"}]


def test_cursor_never_uses_snapshot_or_another_day(tmp_path, config):
    rows_to_parquet([{"symbol": "600000.SH", "ts": 1, "v": 1.0}], config, tmp_path, DAY)
    assert ext_pull.since_cursor(tmp_path, config, date(2026, 11, 3)) is None
    config.mode = "snapshot"
    assert ext_pull.since_cursor(tmp_path, config, DAY) is None
