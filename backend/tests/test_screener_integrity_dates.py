"""历史筛选只阻断目标日及之前的异常，覆盖真实仓库的缓存探测路径。"""
from datetime import date

import polars as pl
import pytest

from app.services.daily_health import DailyIntegrityError, record_issues
from app.services.data_integrity import IntegrityIssue
from app.services.screener import ScreenerService
from app.tickflow.repository import DataStore, KlineRepository


@pytest.mark.parametrize("asset,symbol,table", [
    ("stock", "600000.SH", "kline_daily"),
    ("etf", "510300.SH", "kline_etf_daily"),
    ("index", "000001.SH", "kline_index_daily"),
])
@pytest.mark.parametrize("issue_is_index", [False, True])
def test_history_before_issue_remains_usable_but_affected_dates_fail(tmp_path, asset, symbol, table, issue_is_index):
    repo = KlineRepository(DataStore(tmp_path))
    earlier, broken = date(2026, 11, 2), date(2026, 11, 4)
    frame = pl.DataFrame({"symbol": [symbol], "date": [earlier], "close": [10.0]})
    cache_name = {"stock": "_enriched", "etf": "_etf_enriched", "index": "_index_enriched"}[asset]
    setattr(repo, cache_name + "_cache", frame)
    setattr(repo, cache_name + "_cache_date", earlier)
    try:
        service = ScreenerService(repo, asset_type=asset)
        record_issues(tmp_path, [IntegrityIssue(
            broken, "kline_index_daily" if issue_is_index else table, "invalid",
            symbols=("000001.SH" if issue_is_index else symbol,),
        )])
        result = service._load_enriched_for_date(earlier)
        assert result["close"].to_list() == [10.0]
        for target in [broken, date(2026, 11, 5)]:
            with pytest.raises(DailyIntegrityError):
                service._load_enriched_for_date(target)
        with pytest.raises(DailyIntegrityError):
            repo.get_enriched_latest_asset(asset)
    finally:
        repo.db.close()
