"""指数分钟读取后台结果，重复浏览不请求数据源。"""
from datetime import date, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import polars as pl

from app.api import indices
from app.services import kline_sync


def test_historical_and_current_minute_reads_local(monkeypatch):
    monkeypatch.setattr(indices, "cn_today", lambda: date(2026, 9, 14))
    repo = MagicMock()
    repo.get_index_instruments.return_value = pl.DataFrame({"symbol": ["000001.SH"], "name": ["上证指数"]})
    repo.get_minute.return_value = pl.DataFrame({"datetime": [datetime(2026, 9, 14, 10)], "close": [3000.]})
    forbidden = MagicMock(side_effect=AssertionError("read cannot fetch"))
    monkeypatch.setattr(kline_sync, "fetch_minute_single", forbidden)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=repo)))
    for day in [date(2026, 9, 11), date(2026, 9, 14), date(2026, 9, 14)]:
        result = indices.get_index_minute(request, symbol="000001.SH", trade_date=day)
        assert result["date"] == day.isoformat()
        assert result["source"] == "local"
        assert result["rows"][0]["close"] == 3000.
        assert result["name"] == "上证指数"
    forbidden.assert_not_called()
