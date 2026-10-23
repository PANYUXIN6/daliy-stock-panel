"""业绩断层选股复用图表信号，并保持当时财务可用性和当天筛选语义。"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest
from test_earnings_breakout import bars, baseline, calendar, financial_row

from app.backtest.strategy import StrategyDependencyResolver
from app.services import earnings_breakout as service
from app.services.financial_versions import read_versions, record_versions, version_path
from app.services.screener import ScreenerService
from app.strategy import market_data
from app.strategy.engine import StrategyDataContext, StrategyEngine, StrategyResult
from app.strategy.monitor import MonitorRuleEngine

STRATEGY_ID = "earnings_breakout_pullback"
SIGNAL_COLUMNS = ["signal_earnings_9", "signal_earnings_42"]


class FakeRepo:
    def __init__(self, data_dir, prices):
        self.store = SimpleNamespace(data_dir=data_dir)
        self.prices = prices
        self.version_reads = []
        self.price_reads = []

    def get_financial_versions(self, table, symbol=None, columns=None):
        self.version_reads.append((table, symbol))
        return read_versions(self.store.data_dir, table, columns, symbol=symbol)

    def get_daily_batch(self, symbols, start, end, columns=None):
        self.price_reads.append((symbols, start, end, columns))
        frame = self.prices.filter(
            pl.col("symbol").is_in(symbols) & pl.col("date").is_between(start, end)
        )
        return frame.select(columns) if columns else frame

    def get_daily_asset(self, asset_type, symbol, start, end, columns=None):
        assert asset_type == "stock"
        return self.get_daily_batch([symbol], start, end, columns)


@pytest.fixture
def setup_repo(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(service.preferences, "get_financial_provider", lambda: "fuyao")

    def make(kind="9", *, missing_prior=False):
        prices = bars(kind).with_columns(pl.lit(1000.0).alias("volume"),
                                        pl.lit(10000.0).alias("amount"))
        baseline(tmp_path, missing_prior=missing_prior)
        folder = tmp_path / "calendar"
        folder.mkdir(exist_ok=True)
        (folder / "trading_days.json").write_text(json.dumps(calendar(prices)))
        repo = FakeRepo(tmp_path, prices)
        market_data._set_repo(repo)
        return repo

    yield make
    market_data._reset_repo()


def strategy_engine():
    return StrategyEngine(strategy_dirs=[Path(__file__).parents[1] / "app/strategy/builtin"])


@pytest.mark.parametrize("kind,index,icon", [("9", 42, 9), ("42", 45, 42)])
def test_real_pullback_signals_match_chart_from_full_preceding_history(setup_repo, kind, index, icon):
    repo = setup_repo(kind)
    day = repo.prices["date"][index]
    # 策略只收到很短的窗口，形态与 EMA 仍必须从完整前序计算。
    frame = repo.prices.slice(index - 1, 2).with_columns(pl.lit("浦发银行").alias("name"))
    hits = service.filter_pullback_signals(repo, frame)
    current = hits.filter(pl.col("date") == day)
    assert current.height == 1
    assert current["name"][0] == "浦发银行"
    assert current[f"signal_earnings_{icon}"][0]
    assert current[f"earnings_{icon}_event_date"][0] == repo.prices["date"][40]
    chart = service.get_earnings_breakout(repo, "600000.SH", frame["date"][0], day)
    expected = {(marker["date"], marker["icon"]) for marker in chart["markers"]}
    actual = {
        (str(row["date"]), signal_icon)
        for row in hits.iter_rows(named=True)
        for signal_icon in (9, 42)
        if row[f"signal_earnings_{signal_icon}"]
    }
    assert actual == expected


def test_strategy_selects_only_as_of_and_live_price_can_invalidate_yesterday_signal(setup_repo):
    repo = setup_repo()
    engine = strategy_engine()
    signal_day, next_day = repo.prices["date"][42], repo.prices["date"][43]
    history = repo.prices.slice(42, 2).with_columns(
        pl.when(pl.col("date") == next_day).then(10.2).otherwise(pl.col("low")).alias("low")
    )
    previous = engine.run(STRATEGY_ID, StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=signal_day,
        current=history.head(1), history=history.head(1),
    ))
    assert previous.total == 1 and previous.rows[0]["symbol"] == "600000.SH"
    result = engine.run(STRATEGY_ID, StrategyDataContext(
        asset_type="stock", timeframe="1d", as_of=next_day,
        current=history.tail(1), history=history,
    ))
    assert result.total == 0 and result.rows == []


def test_screener_context_replaces_cached_today_with_current_candle(setup_repo, monkeypatch):
    repo = setup_repo()
    engine = strategy_engine()
    history = repo.prices.slice(42, 2)
    today = history["date"][-1]
    current = history.tail(1).with_columns(pl.lit(10.2).alias("low"))
    screener = ScreenerService(repo)
    monkeypatch.setattr(screener, "_load_enriched_history", lambda *args: history)

    context = screener.build_strategy_context(
        engine, today, [STRATEGY_ID], current=current,
    )

    assert context.history.filter(pl.col("date") == today)["low"].to_list() == [10.2]
    assert engine.run(STRATEGY_ID, context).total == 0


def test_batch_read_once_and_two_simultaneous_signals_produce_one_candidate(setup_repo, monkeypatch):
    repo = setup_repo()
    second_symbol = "000001.SZ"
    record_versions(repo.store.data_dir, "income", pl.DataFrame([
        {**financial_row("2024-12-31", "2025-03-01", 50.0), "symbol": second_symbol},
        {**financial_row("2025-12-31", "2026-02-09", 100.0), "symbol": second_symbol},
    ]), "fuyao", datetime.fromisoformat("2026-02-09T20:00:00+08:00"))
    repo.prices = pl.concat([
        repo.prices, repo.prices.with_columns(pl.lit(second_symbol).alias("symbol")),
    ])
    target = repo.prices.filter(pl.col("date") == date(2026, 2, 12))

    def both_signals(prices, event_dates):
        assert date(2026, 2, 10) in event_dates
        return prices.with_columns(
            pl.lit(True).alias("signal_earnings_9"),
            pl.lit(True).alias("signal_earnings_42"),
            pl.lit(date(2026, 2, 10)).alias("earnings_9_event_date"),
            pl.lit(date(2026, 2, 10)).alias("earnings_42_event_date"),
        )

    monkeypatch.setattr(service, "compute_earnings_breakout", both_signals)
    hits = market_data.get_earnings_breakout_signals(target)
    assert hits.height == 2
    assert hits.select("symbol", "date").unique().height == 2
    assert hits.select(SIGNAL_COLUMNS).to_dicts() == [
        {"signal_earnings_9": True, "signal_earnings_42": True},
        {"signal_earnings_9": True, "signal_earnings_42": True},
    ]
    # 全市场读取性能契约：财务和前序日线各一次，不为每只股票再次扫描磁盘。
    assert repo.version_reads == [("income", None)]
    assert len(repo.price_reads) == 1
    assert set(repo.price_reads[0][0]) == {"600000.SH", second_symbol}
    assert repo.price_reads[0][1] == date(1990, 1, 1)


@pytest.mark.parametrize("late_comparative", [False, True])
def test_missing_contemporaneous_comparative_is_not_backfilled(setup_repo, late_comparative):
    repo = setup_repo(missing_prior=True)
    if late_comparative:
        record_versions(repo.store.data_dir, "income", pl.DataFrame([
            financial_row("2024-12-31", "2025-03-01", 50.0),
        ]), "fuyao", datetime.fromisoformat("2026-02-16T20:00:00+08:00"))
    assert service.filter_pullback_signals(repo, repo.prices.slice(42, 2)).is_empty()


def test_later_revision_never_erases_previously_formed_gap(setup_repo):
    repo = setup_repo("42")
    frame = repo.prices.slice(45, 2)
    before = service.filter_pullback_signals(repo, frame)
    assert before["signal_earnings_42"].any()
    record_versions(repo.store.data_dir, "income", pl.DataFrame([
        financial_row("2025-12-31", "2026-02-16", -100.0),
        financial_row("2024-12-31", "2026-02-16", 500.0),
    ]), "fuyao", datetime.fromisoformat("2026-02-16T20:00:00+08:00"))
    after = service.filter_pullback_signals(repo, frame)
    assert after.sort("symbol", "date").equals(before.sort("symbol", "date"))


def test_corrupt_financial_versions_raise_instead_of_returning_zero_matches(setup_repo):
    repo = setup_repo()
    version_path(repo.store.data_dir, "income").write_bytes(b"broken parquet")
    with pytest.raises(pl.exceptions.PolarsError):
        market_data.get_earnings_breakout_signals(repo.prices.slice(42, 2))


def test_builtin_registry_and_backtest_dependencies_have_no_extra_selection_rules():
    engine = strategy_engine()
    assert engine.has(STRATEGY_ID)
    strategy = engine.get(STRATEGY_ID)
    assert strategy.meta["name"] == "业绩断层回踩"
    assert strategy.execution_backend == "python_history_legacy"
    assert strategy.meta["realtime_supported"] is False
    assert strategy.basic_filter.get("enabled") is False
    assert not strategy.meta.get("scoring")
    assert engine._result_limit(strategy, None) is None
    assert engine.required_history_bars([STRATEGY_ID]) == 2
    plan = StrategyDependencyResolver().resolve(
        strategy, params={}, basic_filter=strategy.basic_filter,
        entry_signals=strategy.entry_signals, exit_signals=strategy.exit_signals,
    )
    assert not plan.full_feature_fallback
    assert {"open", "high", "low", "close", "date", "symbol"} <= set(plan.base_columns)
    assert not set(SIGNAL_COLUMNS) & set(plan.signal_columns)


def test_manual_strategy_metadata_and_monitor_save_rejection(monkeypatch, tmp_path):
    from fastapi import HTTPException

    from app.api import monitor_rules as rules_api
    from app.api.strategy import _strategy_detail

    engine = strategy_engine()
    assert _strategy_detail(engine.get(STRATEGY_ID))["realtime_supported"] is False
    monkeypatch.setattr(rules_api, "runtime_for", lambda request: SimpleNamespace(strategy_engine=engine))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
    )))
    model = rules_api.RuleModel(
        id="manual_only", name="业绩断层回踩", type="strategy", scope="all",
        strategy_id=STRATEGY_ID,
    )
    with pytest.raises(HTTPException) as error:
        rules_api.save_rule(model, request)
    assert error.value.status_code == 400
    assert "手动日线筛选" in error.value.detail


def test_monitor_never_loads_or_executes_full_history_strategy(setup_repo, private_owner):
    repo = setup_repo()
    strategy = strategy_engine().get(STRATEGY_ID)
    calls = []

    def load_history(*args, **kwargs):
        calls.append("history")
        return repo.prices

    def run(*args, **kwargs):
        calls.append("run")
        return StrategyResult(as_of=repo.prices["date"][-1], strategy_id=STRATEGY_ID)

    engine = MonitorRuleEngine()
    engine.set_strategy_engine(SimpleNamespace(
        get=lambda strategy_id: strategy,
        required_history_bars=lambda *args, **kwargs: 2,
        run=run,
    ))
    engine.set_history_loader(load_history)
    engine.set_rules([{
        "id": "legacy_earnings_monitor", "name": "业绩断层", "enabled": True,
        "type": "strategy", "asset_type": "stock", "scope": "all",
        "strategy_id": STRATEGY_ID, "notify_events": ["pool_entry"],
    }])
    assert engine.evaluate(repo.prices.tail(1)) == []
    assert calls == []
    assert STRATEGY_ID not in engine.latest_strategy_results()


@pytest.mark.parametrize("kind,index", [("9", 42), ("42", 45)])
def test_prepared_command_reads_history_and_computes_prices_once(setup_repo, monkeypatch, kind, index):
    from unittest.mock import Mock

    from app.indicators import earnings_breakout as indicators
    from app.services import financial_demand
    from app.tickflow.capabilities import CapabilitySet

    repo = setup_repo(kind)
    history = repo.prices.slice(index - 1, 2)
    day = history["date"][-1]
    expected = service.filter_pullback_signals(repo, history)
    repo.price_reads.clear()
    repo.version_reads.clear()
    prepared = False
    original_versions = repo.get_financial_versions
    def versions(*args, **kwargs):
        assert prepared, "Final events must use versions available after financial preparation"
        return original_versions(*args, **kwargs)
    def prepare_tables(*args, **kwargs):
        nonlocal prepared
        prepared = True
    monkeypatch.setattr(repo, 'get_financial_versions', versions)
    monkeypatch.setattr(financial_demand, 'prepare_tables', prepare_tables)
    compute = Mock(wraps=indicators._pullback_prices)
    monkeypatch.setattr(indicators, '_pullback_prices', compute)
    svc = ScreenerService(repo)
    monkeypatch.setattr(svc, '_load_enriched_history', lambda *args: history)
    financial_demand.register(repo, CapabilitySet())
    try:
        engine = strategy_engine()
        context = svc.build_strategy_context(engine, day, [STRATEGY_ID],
                                             current=history.tail(1), prepare_financials=True)
        result = engine.run(STRATEGY_ID, context)
        assert len(repo.price_reads) == 1
        assert compute.call_count == 1
        assert repo.version_reads == [("income", None)]
        assert result.total == 1
        assert result.rows[0]['earnings_pullback'] == expected.tail(1)['earnings_pullback'][0]
        assert result.rows[0]['earnings_9_event_date'] == expected.tail(1)['earnings_9_event_date'][0]
        # A standalone chart/strategy read must not retain the previous command's scope.
        market_data.get_earnings_breakout_signals(history)
        assert len(repo.price_reads) == 2
        assert compute.call_count == 2
    finally:
        financial_demand.unregister(repo.store.data_dir)


@pytest.mark.parametrize("changed_index,expected_rows", [(0, 0), (1, 1)])
def test_prepared_prices_recompute_when_history_override_changes(setup_repo, changed_index, expected_rows):
    repo = setup_repo()
    history = repo.prices.slice(42, 2)
    snapshot = service.EarningsPriceSnapshot()
    snapshot.capture(repo, history.tail(1), history['date'][-1])
    snapshot.prepare_pullback_candidates(history.tail(1))
    # Final history may change either the previous candle or preparation's current candle.
    changed = history.with_columns(pl.when(pl.col('date') == history['date'][changed_index])
                                   .then(10.2).otherwise(pl.col('low')).alias('low'))
    reads = len(repo.price_reads)
    result = service.filter_pullback_signals(repo, changed, price_snapshot=snapshot)
    assert len(repo.price_reads) == reads
    assert result.height == expected_rows
    assert result.equals(service.filter_pullback_signals(repo, changed))
    # Reuse must not mutate the snapshot or poison another invocation.
    original = service.filter_pullback_signals(repo, history, price_snapshot=snapshot)
    assert original.height == 2


def test_prepared_prices_do_not_cross_dates_or_stock_pools(setup_repo):
    repo = setup_repo()
    frame = repo.prices.slice(42, 1)
    snapshot = service.EarningsPriceSnapshot()
    snapshot.capture(repo, frame, frame['date'][0])
    snapshot.prepare_pullback_candidates(frame)
    # Different date must read the new cutoff; no reuse of an incomplete old prefix.
    assert service.filter_pullback_signals(repo, repo.prices.slice(43, 1), price_snapshot=snapshot).height == 1
    assert len(repo.price_reads) == 2
    second = '000001.SZ'
    record_versions(repo.store.data_dir, 'income', pl.DataFrame([
        {**financial_row('2024-12-31', '2025-03-01', 50.), 'symbol': second},
        {**financial_row('2025-12-31', '2026-02-09', 100.), 'symbol': second},
    ]), 'fuyao', datetime.fromisoformat('2026-02-09T20:00:00+08:00'))
    other = repo.prices.with_columns(pl.lit(second).alias('symbol'))
    repo.prices = pl.concat([repo.prices, other])
    result = service.filter_pullback_signals(repo, other.slice(42, 1), price_snapshot=snapshot)
    assert result['symbol'].to_list() == [second]
    assert repo.price_reads[-1][0] == [second]


def test_earnings_price_scope_is_nested_and_thread_local():
    from concurrent.futures import ThreadPoolExecutor
    first, second = object(), object()
    with market_data.earnings_prices_scope(first):
        assert market_data._earnings_prices.get() is first
        with pytest.raises(ValueError), market_data.earnings_prices_scope(second):
            assert market_data._earnings_prices.get() is second
            raise ValueError('failed invocation')
        assert market_data._earnings_prices.get() is first
        with ThreadPoolExecutor(1) as executor:
            assert executor.submit(market_data._earnings_prices.get).result() is None
    assert market_data._earnings_prices.get() is None
