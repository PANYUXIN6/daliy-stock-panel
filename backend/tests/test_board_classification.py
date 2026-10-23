"""策略、矩阵和市场总览对新号段的分类须一致。"""
from __future__ import annotations

import polars as pl
import pytest

from app.api.overview import _board as api_board
from app.backtest.matrix import _symbol_in_boards
from app.services.market_overview_builder import _board as overview_board
from app.strategy.engine import StrategyEngine


@pytest.mark.parametrize(
    ("board", "expected"),
    [
        ("沪主板", ["600000.SH"]),
        ("深主板", ["000001.SZ"]),
        ("创业板", ["300001.SZ", "301001.SZ", "302132.SZ"]),
        ("科创板", ["688001.SH", "689009.SH"]),
        ("北交所", ["920001.BJ"]),
    ],
)
def test_board_filter_and_market_overview_agree(board, expected):
    symbols = [
        "600000.SH", "000001.SZ", "300001.SZ", "301001.SZ", "302132.SZ",
        "688001.SH", "689009.SH", "920001.BJ",
    ]
    frame = pl.DataFrame({"symbol": symbols, "close": [10.0] * len(symbols)})
    selected = StrategyEngine._apply_basic_filter(frame, {"boards": [board]})
    assert selected["symbol"].to_list() == expected
    assert [s for s in symbols if _symbol_in_boards(s, [board])] == expected
    assert [s for s in symbols if api_board(s) == board] == expected
    assert [s for s in symbols if overview_board(s) == board] == expected
