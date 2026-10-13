"""Legacy signal summaries follow the same private ownership as research jobs."""
import polars as pl
import pytest

from app.config import settings
from app.services.backtest import BacktestResult, BacktestService
from app.services.user_context import UserContext, user_scope


def result(value):
    return BacktestResult(
        run_id="same-run", config={}, stats={"total_return": value},
        equity_curve=[], trades=[{"symbol": "600000.SH"}], per_symbol_stats=[],
    )


def test_same_run_id_persists_separate_user_summaries(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    service = BacktestService(None)
    for marker, value in (("a", 0.1), ("b", 0.2)):
        with user_scope(UserContext(marker * 32, "admin", tmp_path)):
            service._persist(result(value))
    for marker, value in (("a", 0.1), ("b", 0.2)):
        path = tmp_path / "users" / (marker * 32) / "backtest_results" / "run_id=same-run.parquet"
        assert pl.read_parquet(path).to_dicts() == [{
            "run_id": "same-run", "stats_json": str({"total_return": value}), "n_trades": 1,
        }]
    assert not (tmp_path / "backtest_results").exists()


def test_summary_persistence_without_owner_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    with user_scope(None), pytest.raises(RuntimeError, match="身份"):
        BacktestService(None)._persist(result(0.1))
    assert not list(tmp_path.rglob("*.parquet"))
