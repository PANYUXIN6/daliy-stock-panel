"""显式策略、因子和独立研究 worker 在计算之前准备财务输入。"""
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import polars as pl
import pytest
from test_financial_demand import env as env

from app.backtest.factor import FactorBacktestService, FactorConfig
from app.services import financial_demand as demand
from app.services.screener import ScreenerService
from app.strategy.engine import StrategyEngine


def frame():
    return pl.DataFrame({"symbol": ["600000.SH"], "date": [date(2026, 9, 1)],
                         "open": [10.], "high": [11.], "low": [9.], "close": [10.],
                         "volume": [1000.], "amount": [10000.]})


def test_factor_panel_prepares_before_materializing_and_preserves_time_gate(env):
    engine = SimpleNamespace(repo=env.repo, load_panel=Mock(return_value=frame()))
    service = FactorBacktestService(engine)
    config = FactorConfig("net_margin_latest", ["600000.SH"], date(2026, 9, 1), date(2026, 9, 1))
    panel = service._load_factor_panel(config, ["net_margin_latest"])
    assert panel["net_margin_latest"].item() == 10.
    assert env.fetch.call_count == 1
    engine.load_panel.return_value = frame().with_columns(pl.lit(date(2026, 8, 1)).alias("date"))
    panel = service._load_factor_panel(config, ["net_margin_latest"])
    # 尚未到新一期可用日，仍只能使用上年已公布的 50/800。
    assert panel["net_margin_latest"].item() == 6.25
    assert env.fetch.call_count == 1


def test_strategy_command_prepares_only_selected_pool_and_read_context_does_not_fetch(env, authenticated_actor):
    folder = env.root / "test-strategies"
    folder.mkdir()
    (folder / "financial.py").write_text('''import polars as pl
META = {"id": "financial_test", "name": "financial_test", "scoring": {"net_margin_latest": 1}}
BASIC_FILTER = {"enabled": False}
REQUIRED_FEATURES = {"net_margin_latest"}
LOOKBACK_DAYS = 1
def filter(df, params):
    return pl.lit(True)
''')
    engine = StrategyEngine(strategy_dirs=[folder])
    service = ScreenerService(env.repo)
    current = pl.concat([frame(), frame().with_columns(pl.lit("000001.SZ").alias("symbol"))])
    plain = service.build_strategy_context(engine, date(2026, 9, 1), ["financial_test"], current=current)
    assert "net_margin_latest" not in plain.current.columns
    env.fetch.assert_not_called()
    prepared = service.build_strategy_context(engine, date(2026, 9, 1), ["financial_test"], current=current,
                                               prepare_financials=True, pool=["600000.SH"])
    assert env.fetch.call_args.args[:2] == ("income", ["600000.SH"])
    assert prepared.current.filter(pl.col("symbol") == "600000.SH")["net_margin_latest"].item() == 10.
    assert prepared.current.filter(pl.col("symbol") == "000001.SZ")["net_margin_latest"].item() is None


def test_mining_nested_request_prepared_in_parent_for_selected_symbols(env, monkeypatch, authenticated_actor):
    from app.backtest.engine import BacktestEngine
    from app.services import mining_preflight
    monkeypatch.setattr(mining_preflight, "enriched_partition_dates", lambda *args: [date(2026, 9, 1)])
    monkeypatch.setattr(BacktestEngine, "load_panel", lambda *args, **kwargs: frame())
    progress = []
    demand.prepare_worker({"data_dir": str(env.root), "kind": "mining", "config": {
        "run_id": "test", "request": {"factor_names": ["net_income_yoy_latest"],
                                         "symbols": ["600000.SH"], "start": None, "end": None},
    }}, progress_cb=progress.append)
    assert env.fetch.call_args.args[:2] == ("income", ["600000.SH"])
    assert progress[0]["phase"] == "financials"


@pytest.mark.parametrize("strategy,index", [("earnings_breakout_gap", 40), ("earnings_breakout_pullback", 42)])
def test_earnings_preparation_only_fetches_price_candidates(env, strategy, index):
    from test_earnings_breakout import bars
    first = bars().with_columns(pl.lit(1000.).alias("volume"), pl.lit(10000.).alias("amount"))
    flat = first.with_columns(pl.lit("000001.SZ").alias("symbol"), pl.lit(10.).alias("open"),
                              pl.lit(10.2).alias("high"), pl.lit(9.8).alias("low"), pl.lit(10.).alias("close"))
    prices = pl.concat([first, flat])
    env.repo.get_daily_batch = lambda symbols, start, end, columns=None: prices.filter(pl.col("date") <= end).select(columns)
    engine = StrategyEngine(strategy_dirs=[Path(__file__).parents[1] / "app/strategy/builtin"])
    target = first["date"][index]
    demand.prepare_strategies(env.repo, engine, [strategy], prices.filter(pl.col("date") == target), target)
    assert env.fetch.call_count == 1
    assert env.fetch.call_args.args[:2] == ("income", ["600000.SH"])


def test_formula_trial_and_save_validation_prepare_financial_inputs(env, monkeypatch, authenticated_actor):
    from app.api import backtest, factors
    engine = SimpleNamespace(load_panel=lambda *args, **kwargs: frame())
    monkeypatch.setattr(backtest, "_get_engine", lambda request: engine)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=env.repo)))
    result = factors.trial_formula(factors.FormulaTrialRequest(formula="net_margin_latest"), request)
    assert result["ok"] is True
    assert result["null_ratio"] == 0.
    factors._trial_nonempty(request, "net_margin_latest")
    assert env.fetch.call_count == 1
