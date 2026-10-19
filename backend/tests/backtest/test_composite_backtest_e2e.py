"""叠加策略回测端到端集成测试 (M2 验证)。

用真实的内置 matrix_native 子策略(ma_golden_cross + macd_golden) + 合成行情 panel,
完整跑通 StrategyBacktestService.run() 的 composite 分支:
特征计划合并 → 超集矩阵加载 → 逐子策略信号 → 合并(退出投影) → 撮合 → 结果归因。

验证两种模式(position/full)和两种合并模式(union/intersect)的关键路径。
合成数据在评分暖机完成后用放量上涨制造金叉信号。
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import polars as pl
import pytest

from app.backtest.engine import BacktestEngine
from app.backtest.matrix import build_market_data_matrix
from app.backtest.strategy import StrategyBacktestConfig, StrategyBacktestService
from app.strategy.engine import StrategyEngine

pytestmark = pytest.mark.usefixtures("private_owner")


def _synthetic_panel() -> pl.DataFrame:
    """先横盘 80 根, 再放量上涨, 使两子策略金叉时均有完整评分输入。"""
    n_days = 130
    symbols = ["000001.SZ", "000002.SZ", "600000.SH"]
    rows = []
    for sym_idx, sym in enumerate(symbols):
        base = 10.0 + sym_idx * 5
        for t in range(n_days):
            d = date(2024, 11, 1) + timedelta(days=int(t * 1.5))
            close = base * (1 + max(t - 80, 0) * (0.015 + sym_idx * 0.001))
            open_p = close
            high = close + 0.2
            low = close - 0.2
            vol = 5e6 if t == 81 else 1e6
            rows.append({
                "symbol": sym, "date": d, "open": open_p, "high": high,
                "low": low, "close": close, "volume": vol,
                "raw_close": close, "raw_high": high, "raw_low": low,
            })
    return pl.DataFrame(rows).sort(["symbol", "date"])


class _RepoStub:
    def get_index_daily(self, *args, **kwargs) -> pl.DataFrame:
        return pl.DataFrame()


def _make_service(panel: pl.DataFrame, builtin: Path, comp_dir: Path):
    """构造 StrategyBacktestService, 用 monkeypatch 的数据加载。"""
    bt_engine = BacktestEngine(_RepoStub())

    def _load_mdm(self, symbols, start, end, feature_plan, asset_type="stock", **kw):
        df = panel.filter((pl.col("date") >= start) & (pl.col("date") <= end))
        if symbols:
            df = df.filter(pl.col("symbol").is_in(symbols))
        cols = [c for c in sorted(set(feature_plan.base_columns)) if c in df.columns]
        return build_market_data_matrix(df.select(cols), field_columns=feature_plan.matrix_columns)

    bt_engine.load_market_data_matrix_for_backtest = _load_mdm.__get__(bt_engine)
    strategy_engine = StrategyEngine(strategy_dirs=[builtin, comp_dir])
    return StrategyBacktestService(bt_engine, strategy_engine)


def _write_composite(
    comp_dir: Path, sid: str, children: list, *, merge_mode="union", min_confirm=0
):
    children_repr = ", ".join(
        f'{{"strategy_id": "{c}", "weight": {w}}}' for c, w in children
    )
    opts = '["union", "intersect"]'
    params_block = (
        f'{{"id": "merge_mode", "type": "select", '
        f'"options": {opts}, "default": "{merge_mode}"}}, '
        f'{{"id": "min_confirm", "type": "int", "default": {min_confirm}}}'
    )
    (comp_dir / f"{sid}.py").write_text(
        f'''META = {{"id": "{sid}", "name": "{sid}", "asset_types": ["stock"], "timeframes": ["1d"],
        "params": [{params_block}], "children": [{children_repr}]}}
EXECUTION_BACKEND = "composite"
''',
        encoding="utf-8",
    )


@pytest.fixture
def composite_setup(tmp_path):
    builtin = Path(__file__).resolve().parents[2] / "app" / "strategy" / "builtin"
    comp_dir = tmp_path / "composite"
    comp_dir.mkdir(parents=True)
    _write_composite(comp_dir, "custom_e2e_blend", [("ma_golden_cross", 0.5), ("macd_golden", 0.5)])
    panel = _synthetic_panel()
    svc = _make_service(panel, builtin, comp_dir)
    return svc, comp_dir


@pytest.mark.parametrize("strategy_id", ["ma_golden_cross", "macd_golden"])
def test_composite_panel_supports_each_child(composite_setup, strategy_id):
    """每个子策略都能独立成交, 防止合并测试仅因另一个子策略有信号而通过。"""
    svc, _ = composite_setup
    result = svc.run(StrategyBacktestConfig(
        strategy_id=strategy_id, symbols=None,
        start=date(2025, 2, 1), end=date(2025, 5, 1),
        mode="position", max_positions=5, holding_days=10,
        overrides={"basic_filter": {"enabled": False}},
    ))
    assert result.error is None, f"{strategy_id} 回测失败: {result.error}"
    assert {trade["symbol"] for trade in result.trades} == {"000001.SZ", "000002.SZ", "600000.SH"}


def test_composite_backtest_position_mode(composite_setup):
    """position 模式: composite 回测产出交易、净值曲线和归因。"""
    svc, _ = composite_setup
    cfg = StrategyBacktestConfig(
        strategy_id="custom_e2e_blend", symbols=None,
        start=date(2025, 2, 1), end=date(2025, 5, 1),
        mode="position", max_positions=5, holding_days=10,
        overrides={"basic_filter": {"enabled": False}},
    )
    result = svc.run(cfg)
    assert result.error is None, f"回测失败: {result.error}"
    assert len(result.trades) > 0, "应产出交易"
    assert len(result.equity_curve) > 0, "应有净值曲线"
    assert result.strategy_info is not None
    children = result.strategy_info.get("composite_children")
    assert children is not None and len(children) == 2
    assert {c["id"] for c in children} == {"ma_golden_cross", "macd_golden"}


def test_composite_backtest_full_mode(composite_setup):
    """full 模式(独立候选): composite 回测能跑通。"""
    svc, _ = composite_setup
    cfg = StrategyBacktestConfig(
        strategy_id="custom_e2e_blend", symbols=None,
        start=date(2025, 2, 1), end=date(2025, 5, 1),
        mode="full", holding_days=10,
        overrides={"basic_filter": {"enabled": False}},
    )
    result = svc.run(cfg)
    assert result.error is None, f"full 模式失败: {result.error}"
    assert len(result.trades) > 0


def test_composite_backtest_intersect_min1_equals_union(composite_setup):
    """门槛 1 与 union 交易一致; 门槛 2 排除仅由 MA 命中的标的。"""
    _, comp_dir = composite_setup
    # 首只标的的量比为 1.3, 满足 MA 的 1.2 门槛但不满足 MACD 的 1.5。
    panel = _synthetic_panel().with_columns(
        pl.when((pl.col("symbol") == "000001.SZ") & (pl.col("volume") == 5e6))
        .then(1.3e6).otherwise(pl.col("volume")).alias("volume")
    )
    builtin = Path(__file__).resolve().parents[2] / "app" / "strategy" / "builtin"
    svc = _make_service(panel, builtin, comp_dir)
    cfg = StrategyBacktestConfig(
        strategy_id="custom_e2e_blend", symbols=None,
        start=date(2025, 2, 1), end=date(2025, 5, 1),
        mode="position", max_positions=5, holding_days=10,
        overrides={"basic_filter": {"enabled": False}},
    )
    union = svc.run(cfg)
    assert union.error is None, f"union 模式失败: {union.error}"
    assert {trade["symbol"] for trade in union.trades} == {"000001.SZ", "000002.SZ", "600000.SH"}

    _write_composite(
        comp_dir, "custom_e2e_blend",
        [("ma_golden_cross", 0.5), ("macd_golden", 0.5)],
        merge_mode="intersect", min_confirm=1,
    )
    svc.strategy_engine.reload()
    result = svc.run(cfg)
    assert result.error is None, f"intersect 模式失败: {result.error}"
    assert result.trades == union.trades

    strict_result = svc.run(replace(cfg, params={"min_confirm": 2}))
    assert strict_result.error is None, f"min_confirm=2 模式失败: {strict_result.error}"
    assert {trade["symbol"] for trade in strict_result.trades} == {"000002.SZ", "600000.SH"}


def test_worker_strategy_dirs_includes_composite(tmp_path, private_owner):
    """回归: worker 加载受信任策略和当前用户的声明式 composite。

    缺陷历史: worker._strategy_dirs 曾遗漏 composite 目录, 导致回测子进程
    找不到 composite 策略(报 unknown strategy)。个人 custom/ai Python 不得加载。
    """
    from app.backtest.worker import _strategy_dirs

    dirs = _strategy_dirs(tmp_path)
    assert set(dirs) == {
        Path(__file__).resolve().parents[2] / "app" / "strategy" / "builtin",
        tmp_path / "strategies" / "templates",
        tmp_path / "users" / private_owner.id / "strategies" / "composite",
    }
