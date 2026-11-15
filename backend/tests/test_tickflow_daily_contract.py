"""Historical bar dates must not become live quote timestamps."""

from datetime import date, datetime
from types import SimpleNamespace

import polars as pl
import pytest

from app.data_providers import tickflow_provider
from app.market_time import CN_TZ
from app.services import kline_sync


@pytest.mark.parametrize("bounded", [False, True])
def test_historical_daily_matches_sync_without_snapshot_marker(monkeypatch, bounded):
    days = [date(2026, 11, 2), date(2026, 11, 3)]
    raw = {"000001.SH": {
        "timestamp": [int(datetime.combine(d, datetime.min.time(), CN_TZ).timestamp() * 1000)
                      for d in days],
        "open": [4000., 4010.], "high": [4020., 4030.], "low": [3990., 4000.],
        "close": [4010., 4020.], "volume": [1000., 1200.], "amount": [4_000_000., 4_800_000.],
    }}
    client = SimpleNamespace(klines=SimpleNamespace(batch=lambda *args, **kwargs: raw))
    monkeypatch.setattr(tickflow_provider, "get_client", lambda: client)
    monkeypatch.setattr(kline_sync, "get_client", lambda: client)
    window = {"start_time": datetime(2026, 11, 2, tzinfo=CN_TZ),
              "end_time": datetime(2026, 11, 3, tzinfo=CN_TZ)} if bounded else {}
    daily = tickflow_provider.TickFlowProvider().get_daily(
        ["000001.SH"], window.get("start_time"), window.get("end_time"), "index")
    synced = kline_sync.sync_daily_batch(["000001.SH"], **window)
    assert daily["date"].to_list() == days
    assert "quote_ts" not in daily.columns or daily["quote_ts"].null_count() == daily.height
    assert daily.select(synced.columns).equals(synced)
    assert daily["close"].to_list() == [4010., 4020.]


def test_realtime_daily_keeps_snapshot_marker():
    timestamp = int(datetime(2026, 11, 3, 11, tzinfo=CN_TZ).timestamp() * 1000)
    from app.data_providers.normalizer import normalize_daily

    daily = normalize_daily(pl.DataFrame({
        "symbol": ["000001.SH"], "date": [date(2026, 11, 3)], "timestamp": [timestamp],
        "open": [4000.], "high": [4020.], "low": [3990.], "close": [4010.],
        "volume": [1000.], "amount": [4_000_000.],
    }))
    assert daily["quote_ts"].to_list() == [timestamp]
