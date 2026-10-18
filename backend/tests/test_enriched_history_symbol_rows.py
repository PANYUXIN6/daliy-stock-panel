"""选股历史窗口按各标的实际交易行计数，停牌不能截掉偏移输入。"""
from datetime import date, timedelta

import polars as pl
import pytest

from app.services.screener import ScreenerService
from app.strategy import overlay
from app.tickflow.repository import DataStore, KlineRepository


def _history():
    days = [date(2024, 1, 1) + timedelta(days=i) for i in range(900)]
    days = [day for day in days if day.weekday() < 5]
    target = days[-6]
    rows = []
    for symbol in ("A", "B", "NEW"):
        for i, day in enumerate(days):
            if symbol == "A" and 100 <= i < len(days) - 6:
                continue
            if symbol == "NEW" and day < target:
                continue
            rows.append({"symbol": symbol, "date": day, "open": 12., "high": 13.,
                         "low": 11., "close": 12., "volume": 100., "amount": 1200.})
    return pl.DataFrame(rows).sort(["symbol", "date"]), target


@pytest.mark.parametrize("cached", [False, True], ids=["cold", "cached"])
@pytest.mark.parametrize("field", ["close", "ma20"], ids=["offset", "rolling_offset"])
def test_screener_backfills_suspended_symbol_and_bounds_each_window(tmp_path, monkeypatch, cached, field):
    frame, target = _history()
    root = tmp_path / "kline_daily_enriched" / "sample"
    root.mkdir(parents=True)
    frame.write_parquet(root / "part.parquet")
    repo = KlineRepository(DataStore(tmp_path))
    if cached:
        repo._enriched_history_cache = (
            frame.with_columns(pl.col("close").rolling_mean(20).over("symbol").alias("ma20"))
            .filter(pl.col("date") >= target - timedelta(days=300))
            .with_columns(pl.when((pl.col("symbol") == "A") & (pl.col("date") == target))
                          .then(14.).otherwise(pl.col("close")).alias("close"))
        )
    monkeypatch.setattr("app.services.screener._history_cache", {})
    service = ScreenerService(repo)

    history = service._load_enriched_history(target, 2)
    conditions = [{"left": field, "op": ">", "right": "10", "leftDays": 1}]
    selected = history.filter(overlay.mask(history, conditions)).filter(pl.col("date") == target)

    assert selected["symbol"].to_list() == ["A", "B"]
    assert history.group_by("symbol").len().sort("symbol")["len"].to_list() == [3, 3, 1]
    assert history.filter(pl.col("symbol") == "A")["date"].to_list() == (
        frame.filter((pl.col("symbol") == "A") & (pl.col("date") <= target))["date"].tail(3).to_list()
    )
    assert history["date"].max() == target
    assert history.filter((pl.col("symbol") == "A") & (pl.col("date") == target))["close"].item() == (
        14. if cached else 12.
    )
    monkeypatch.setattr("app.services.screener.scan_enriched_parquet", lambda _path: pytest.fail(
        "缺行标的补读必须复用历史 TTL 缓存",
    ))
    assert service._load_enriched_history(target, 2).equals(history)


def test_repository_keeps_previous_actual_rows_without_expanding_output(tmp_path):
    frame, target = _history()
    repo = KlineRepository(DataStore(tmp_path))
    repo._enriched_history_cache = frame

    history = repo.get_enriched_history(target, 2)

    assert history is not None
    expected = frame.filter(pl.col("date") <= target).group_by("symbol", maintain_order=True).tail(3)
    assert history.equals(expected)


def test_complete_repository_window_does_not_read_parquet(tmp_path, monkeypatch):
    frame, target = _history()
    repo = KlineRepository(DataStore(tmp_path))
    repo._enriched_history_cache = frame.filter(pl.col("symbol") != "NEW")
    monkeypatch.setattr("app.services.screener.scan_enriched_parquet", lambda _path: pytest.fail(
        "完整缓存窗口不应读取磁盘",
    ))

    history = ScreenerService(repo)._load_enriched_history(target, 2)

    assert history.group_by("symbol").len()["len"].to_list() == [3, 3]
