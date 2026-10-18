"""业绩跳空与后续回踩分别选股，形成日只依赖当时利润和完整向上缺口。"""
from __future__ import annotations

from datetime import datetime

import polars as pl
import pytest
from test_earnings_breakout import financial_row
from test_earnings_breakout_strategy import setup_repo as setup_repo
from test_earnings_breakout_strategy import strategy_engine

from app.services import earnings_breakout as service
from app.services.financial_versions import read_versions, record_versions, version_path
from app.strategy import market_data
from app.strategy.engine import StrategyDataContext

GAP_STRATEGY_ID = "earnings_breakout_gap"
PULLBACK_STRATEGY_ID = "earnings_breakout_pullback"


def run_on(engine, strategy_id, repo, index):
    frame = repo.prices.slice(index - 1, 2)
    return engine.run(strategy_id, StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=repo.prices["date"][index],
        current=frame.tail(1), history=frame,
    ))


@pytest.mark.parametrize("kind,pullback_index", [("9", 42), ("42", 45)])
def test_gap_strategy_selects_formation_day_and_pullback_strategy_selects_later_day(
    setup_repo, kind, pullback_index,
):
    repo = setup_repo(kind)
    engine = strategy_engine()
    gap_day = repo.prices["date"][40]
    gap = run_on(engine, GAP_STRATEGY_ID, repo, 40)
    assert gap.total == 1
    assert gap.rows[0]["symbol"] == "600000.SH"
    assert gap.rows[0]["date"] == gap_day
    assert run_on(engine, PULLBACK_STRATEGY_ID, repo, 40).total == 0
    assert run_on(engine, GAP_STRATEGY_ID, repo, pullback_index).total == 0
    assert run_on(engine, PULLBACK_STRATEGY_ID, repo, pullback_index).total == 1
    chart = service.get_earnings_breakout(repo, "600000.SH", gap_day, gap_day)
    assert [event["date"] for event in chart["events"]] == [gap_day.isoformat()]


def test_gap_does_not_require_pullback_psy_trend_or_bullish_candle(setup_repo):
    repo = setup_repo()
    rows = repo.prices.to_dicts()
    for index in range(40):
        close = 20.0 if index < 28 else 10.0 + (index - 28) * .1
        rows[index].update(open=close - .05, high=close + .1, low=close - .1, close=close)
    rows[40].update(open=11.8, high=12.0, low=11.3, close=11.4)
    repo.prices = pl.DataFrame(rows)
    gap_day = repo.prices["date"][40]
    calculated = service.compute_earnings_breakout(repo.prices.head(41), {gap_day})
    # 连涨超过六天、仍低于多空线且收阴，均不妨碍已形成的完整向上缺口。
    assert sum(rows[index]["close"] > rows[index - 1]["close"] for index in range(29, 41)) > 6
    assert calculated["bull_bear_line"][40] > rows[40]["close"]
    assert rows[40]["close"] < rows[40]["open"]
    assert rows[40]["low"] > rows[39]["high"]
    assert not calculated["signal_earnings_9"][40]
    assert not calculated["signal_earnings_42"][40]
    hits = market_data.get_earnings_gap_signals(repo.prices.slice(40, 1))
    assert hits.select("symbol", "date").to_dicts() == [{"symbol": "600000.SH", "date": gap_day}]


@pytest.mark.parametrize("low", [10.2, 10.1])
def test_profit_event_requires_strictly_unfilled_upward_gap_on_formation_day(setup_repo, low):
    repo = setup_repo()
    gap_day = repo.prices["date"][40]
    repo.prices = repo.prices.with_columns(
        pl.when(pl.col("date") == gap_day).then(low).otherwise(pl.col("low")).alias("low"),
    )
    assert service.filter_gap_signals(repo, repo.prices.slice(40, 1)).is_empty()


@pytest.mark.parametrize("profit,previous", [(-100., 50.), (0., 50.), (50., 50.), (40., 50.), (100., 0.)])
def test_gap_requires_positive_profit_and_positive_yoy(setup_repo, monkeypatch, profit, previous):
    repo = setup_repo()
    versions = read_versions(repo.store.data_dir, "income").with_columns(
        pl.when(pl.col("period_end") == "2025-12-31")
        .then(profit).otherwise(previous).alias("net_income"),
    )
    monkeypatch.setattr(repo, "get_financial_versions", lambda *args, **kwargs: versions)
    assert service.filter_gap_signals(repo, repo.prices.slice(40, 1)).is_empty()


@pytest.mark.parametrize("late_comparative", [False, True])
def test_missing_contemporaneous_comparative_never_backfills_gap(setup_repo, late_comparative):
    repo = setup_repo(missing_prior=True)
    if late_comparative:
        record_versions(repo.store.data_dir, "income", pl.DataFrame([
            financial_row("2024-12-31", "2025-03-01", 50.),
        ]), "fuyao", datetime.fromisoformat("2026-02-16T20:00:00+08:00"))
    assert service.filter_gap_signals(repo, repo.prices.slice(40, 10)).is_empty()


def test_later_revision_keeps_formed_gap_and_does_not_create_a_new_one(setup_repo):
    repo = setup_repo()
    gap_day, revision_day = repo.prices["date"][40], repo.prices["date"][47]
    repo.prices = repo.prices.with_columns([
        pl.when(pl.col("date") == revision_day).then(value).otherwise(pl.col(column)).alias(column)
        for column, value in [("open", 13.1), ("high", 13.5), ("low", 13.0), ("close", 13.4)]
    ])
    frame = repo.prices.slice(40, 10)
    before = service.filter_gap_signals(repo, frame)
    assert before["date"].to_list() == [gap_day]
    record_versions(repo.store.data_dir, "income", pl.DataFrame([
        financial_row("2025-12-31", "2026-02-16", -100.),
        financial_row("2024-12-31", "2026-02-16", 500.),
    ]), "fuyao", datetime.fromisoformat("2026-02-16T20:00:00+08:00"))
    after = service.filter_gap_signals(repo, frame)
    assert after.equals(before)


def test_positive_revision_cannot_reactivate_initially_ineligible_report(setup_repo):
    repo = setup_repo()
    # 重新建立测试目录中的首版：最初亏损，后来扭亏且修订日再次跳空。
    version_path(repo.store.data_dir, "income").unlink()
    record_versions(repo.store.data_dir, "income", pl.DataFrame([
        financial_row("2024-12-31", "2025-03-01", 50.),
        financial_row("2025-12-31", "2026-02-09", -100.),
    ]), "fuyao", datetime.fromisoformat("2026-02-09T20:00:00+08:00"))
    record_versions(repo.store.data_dir, "income", pl.DataFrame([
        financial_row("2025-12-31", "2026-02-16", 100.),
    ]), "fuyao", datetime.fromisoformat("2026-02-16T20:00:00+08:00"))
    revision_day = repo.prices["date"][47]
    repo.prices = repo.prices.with_columns([
        pl.when(pl.col("date") == revision_day).then(value).otherwise(pl.col(column)).alias(column)
        for column, value in [("open", 13.1), ("high", 13.5), ("low", 13.0), ("close", 13.4)]
    ])
    assert service.filter_gap_signals(repo, repo.prices.slice(40, 10)).is_empty()


def test_gap_strategy_is_separate_and_adds_no_default_filters_or_limit():
    engine = strategy_engine()
    for strategy_id, name in [
        (GAP_STRATEGY_ID, "业绩断层跳空"),
        (PULLBACK_STRATEGY_ID, "业绩断层回踩"),
    ]:
        assert engine.has(strategy_id)
        strategy = engine.get(strategy_id)
        assert strategy.meta["name"] == name
        assert strategy.meta["realtime_supported"] is False
        assert strategy.basic_filter.get("enabled") is False
        assert not strategy.meta.get("scoring")
        assert engine._result_limit(strategy, None) is None
