"""多日分时 API 契约。"""

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl
import pytest
from fastapi import HTTPException

from app.api import kline as kline_api


def _request(repo=None, capset=None):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                repo=repo or MagicMock(),
                capabilities=capset or MagicMock(),
            )
        )
    )


def test_minute_range_returns_latest_sessions_with_previous_closes():
    repo = MagicMock()
    repo.resolve_asset_type.return_value = "stock"
    # _get_stock_info 走 instruments 内存缓存 (不再走 execute_one DuckDB 查询)
    repo.get_instruments.return_value = pl.DataFrame({
        "symbol": ["600000.SH"],
        "name": ["浦发银行"],
        "total_shares": [1.0],
        "float_shares": [1.0],
    })
    repo.get_minute_range.return_value = pl.DataFrame({
        "symbol": ["600000.SH"] * 3,
        "datetime": [
            datetime(2026, 8, 5, 1, 30),
            datetime(2026, 8, 6, 1, 30),
            datetime(2026, 8, 7, 1, 30),
        ],
        "open": [10.0, 11.0, 12.0],
        "high": [10.2, 11.2, 12.2],
        "low": [9.8, 10.8, 11.8],
        "close": [10.1, 11.1, 12.1],
        "volume": [100.0, 110.0, 120.0],
        "amount": [101_000.0, 122_100.0, 145_200.0],
    })
    repo.get_daily_asset.return_value = pl.DataFrame({
        "date": [
            date(2026, 8, 4),
            date(2026, 8, 5),
            date(2026, 8, 6),
            date(2026, 8, 7),
        ],
        "close": [9.9, 10.1, 11.1, 12.1],
    })

    result = kline_api.get_minute_range(_request(repo), "600000.SH", 2)

    assert result["name"] == "浦发银行"
    assert result["requested_days"] == 2
    assert result["source"] == "local"
    assert [session["date"] for session in result["sessions"]] == [
        "2026-08-06",
        "2026-08-07",
    ]
    assert [session["prev_close"] for session in result["sessions"]] == [
        10.1,
        11.1,
    ]
    assert result["sessions"][0]["rows"][0]["close"] == 11.1


def test_minute_range_does_not_read_stock_store_for_index():
    repo = MagicMock()
    repo.resolve_asset_type.return_value = "index"
    repo.get_instruments_asset.return_value = pl.DataFrame()

    result = kline_api.get_minute_range(_request(repo), "000001.SH", 10)

    assert result["asset_type"] == "index"
    assert result["sessions"] == []
    repo.get_minute_range.assert_not_called()


async def test_sync_minute_single_uses_requested_days(monkeypatch, tmp_path):
    from app.api import pipeline
    from app.market_time import CN_TZ
    from app.services import ordinary_minute, pipeline_jobs
    from app.tickflow.repository import DataStore, KlineRepository

    repo = KlineRepository(DataStore(tmp_path))
    monkeypatch.setattr(repo, 'resolve_asset_type', lambda symbol: 'stock')
    jobs = pipeline_jobs.JobStore(store_dir=tmp_path / 'jobs')
    monkeypatch.setattr(pipeline_jobs, 'job_store', jobs)
    monkeypatch.setattr(pipeline, 'job_store', jobs)
    monkeypatch.setattr(pipeline_jobs, '_run_slot_owner', None)
    monkeypatch.setattr(kline_api, '_minute_allowed', lambda _: True)
    monkeypatch.setattr(ordinary_minute, 'cn_now', lambda: datetime(2026, 8, 10, 14, tzinfo=CN_TZ))
    monkeypatch.setattr(ordinary_minute.trading_day, 'calendar_status', lambda root: {
        'days': ['2026-08-06', '2026-08-07', '2026-08-10'],
    })
    collect = MagicMock(return_value=480)
    monkeypatch.setattr(ordinary_minute, 'collect_minutes', collect)
    try:
        result = await kline_api.sync_minute_single(_request(repo), {'symbol': '600000.SH', 'days': 2})
        assert result['status'] == 'started'
        await asyncio.gather(*list(pipeline._tasks))
        job = jobs.get(result['job_id'])
        assert job['status'] == 'succeeded'
        assert job['result'] == {
            'symbol': '600000.SH', 'rows': 480,
            'collection_context': {'provider': 'tickflow', 'asset_type': 'stock'},
        }
        args = collect.call_args.args
        assert args[2] == {'stock': ['600000.SH']}
        assert args[3] == datetime(2026, 8, 7, 9, 30, tzinfo=CN_TZ)
        assert args[4] == datetime(2026, 8, 10, 14, tzinfo=CN_TZ)
    finally:
        repo.db.close()


def test_sync_minute_single_rejects_invalid_days():
    with pytest.raises(HTTPException, match="days 必须在 1 到 30 之间"):
        asyncio.run(kline_api.sync_minute_single(
            _request(),
            {"symbol": "600000.SH", "days": 0},
        ))
