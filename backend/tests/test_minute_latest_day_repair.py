"""普通批量分钟同步回补最新数据日，不依赖 Expert 全量分钟能力。"""
from datetime import datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import kline_sync
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet
from app.tickflow.repository import DataStore, KlineRepository


def _bar(symbol: str, stamp: datetime, close: float) -> dict:
    return {
        "symbol": symbol, "datetime": stamp,
        "open": close, "high": close, "low": close, "close": close,
        "volume": 100.0, "amount": close * 100,
    }


@pytest.mark.parametrize("current_day", [20, 21])
def test_pro_minute_sync_repairs_latest_day_and_preserves_history(monkeypatch, tmp_path, current_day):
    """单股尾盘不能把其他股票的早盘挡在增量窗口之外；跨日和重复同步均安全。"""
    first, second = "600000.SH", "000001.SZ"
    yesterday = _bar(first, datetime(2026, 10, 19, 9, 31), 9.0)
    tail = _bar(first, datetime(2026, 10, 20, 14, 59), 10.0)
    minute_dir = tmp_path / "kline_minute"
    kline_sync._write_minute_partition(pl.DataFrame([yesterday, tail]), minute_dir)
    repo = KlineRepository(DataStore(tmp_path))
    now = datetime(2026, 10, current_day, 23, tzinfo=CN_TZ)
    upstream = [
        _bar(second, datetime(2026, 10, 20, 9, 31), 20.0),
        _bar(first, datetime(2026, 10, 20, 14, 59), 11.0),
    ]
    if current_day == 21:
        upstream.append(_bar(second, datetime(2026, 10, 21, 9, 31), 21.0))
    windows = []

    def batch(symbols, *, period, start_time, end_time, **kwargs):
        assert period == "1m"
        windows.append((start_time, end_time))
        result = {}
        for symbol in symbols:
            bars = [
                bar for bar in upstream
                if bar["symbol"] == symbol
                and start_time <= int(bar["datetime"].replace(tzinfo=CN_TZ).timestamp() * 1000) <= end_time
            ]
            if bars:
                result[symbol] = {
                    "timestamp": [int(bar["datetime"].replace(tzinfo=CN_TZ).timestamp() * 1000) for bar in bars],
                    **{column: [bar[column] for bar in bars]
                       for column in ("open", "high", "low", "close", "volume", "amount")},
                }
        return result

    monkeypatch.setattr(kline_sync.preferences, "get_minute_data_provider", lambda: "tickflow")
    monkeypatch.setattr(kline_sync.preferences, "get_minute_sync_segment_days", lambda: 20)
    monkeypatch.setattr(kline_sync, "cn_now", lambda: now)
    monkeypatch.setattr(kline_sync, "get_client", lambda: SimpleNamespace(klines=SimpleNamespace(batch=batch)))
    capset = CapabilitySet({Cap.KLINE_MINUTE_BATCH: CapabilityLimits(batch=100, rpm=30)})
    try:
        assert kline_sync.sync_and_persist_minute([first, second], repo, capset) > 0
        repaired = pl.read_parquet(minute_dir / "date=2026-10-20" / "part.parquet")
        assert repaired.height == 2
        assert repaired.filter(pl.col("symbol") == second)["datetime"].to_list() == [
            datetime(2026, 10, 20, 9, 31),
        ]
        assert repaired.filter(pl.col("symbol") == first)["close"].to_list() == [11.0]
        assert pl.read_parquet(minute_dir / "date=2026-10-19" / "part.parquet").to_dicts() == [yesterday]
        if current_day == 21:
            assert pl.read_parquet(minute_dir / "date=2026-10-21" / "part.parquet")["close"].to_list() == [21.0]

        assert kline_sync.sync_and_persist_minute([first, second], repo, capset) > 0
        repeated = pl.read_parquet(minute_dir / "date=2026-10-20" / "part.parquet")
        assert repeated.to_dicts() == repaired.to_dicts()
        assert repo.execute_one("SELECT count(*) FROM kline_minute")[0] == len(upstream) + 1
        assert datetime.fromtimestamp(windows[0][0] / 1000, CN_TZ) == datetime(2026, 10, 20, tzinfo=CN_TZ)
        assert datetime.fromtimestamp(windows[0][1] / 1000, CN_TZ) == now
    finally:
        repo.db.close()
