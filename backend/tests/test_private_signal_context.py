from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from app.strategy import custom_signals
from app.strategy.engine import StrategyDataContext, StrategyEngine

pytestmark = pytest.mark.usefixtures('private_owner')
DAY = date(2026, 10, 13)


def _save_signal(root, *, shift=False):
    custom_signals.save_one(root, {
        'id': 'positive', 'conditions': [{
            'left': 'close', 'op': '>', 'right': 'field:close' if shift else '1',
            'rightDays': 1 if shift else 0,
        }],
    })


@pytest.mark.parametrize('history_day,history_close', [(DAY - timedelta(days=1), 4.), (DAY, 0.)])
def test_strategy_uses_live_signal_over_absent_or_stale_today(private_owner, history_day, history_close):
    _save_signal(private_owner.data_dir)
    custom_dir = private_owner.data_dir / 'strategy_fixture'
    custom_dir.mkdir()
    (custom_dir / 'live.py').write_text('''import polars as pl
META = {"id": "live", "name": "live", "asset_types": ["stock"], "timeframes": ["1d"]}
def filter(df, params):
    return pl.col("csg_positive")
''')
    engine = StrategyEngine(strategy_dirs=[custom_dir])
    history = pl.DataFrame({'symbol': ['X'], 'date': [history_day], 'close': [history_close]})
    current = pl.DataFrame({'symbol': ['X'], 'date': [DAY], 'close': [5.]})
    context = StrategyDataContext('stock', '1d', DAY, current=current, history=history)
    result = engine.run('live', context, overrides={'basic_filter': {'enabled': False}})
    assert [row['symbol'] for row in result.rows] == ['X']
    assert result.rows[0]['csg_positive'] is True
    assert history.columns == ['symbol', 'date', 'close']
    assert history['close'].to_list() == [history_close]
    assert current.columns == ['symbol', 'date', 'close']


def test_shift_signal_aligns_live_symbols_and_preserves_source_frames(private_owner):
    _save_signal(private_owner.data_dir, shift=True)
    history = pl.DataFrame({
        'symbol': ['A', 'B', 'A', 'B'],
        'date': [DAY - timedelta(days=1), DAY - timedelta(days=1), DAY, DAY],
        'close': [2., 8., 100., 0.],
    })
    current = pl.DataFrame({'symbol': ['B', 'C', 'A'], 'date': [DAY] * 3, 'close': [7., 5., 3.]})
    original = history.clone()
    result = custom_signals.apply_user_context(StrategyDataContext('stock', '1d', DAY, current=current, history=history))
    assert result.current['symbol'].to_list() == ['B', 'C', 'A']
    assert result.current['csg_positive'].to_list() == [False, None, True]
    today = result.history.filter(pl.col('date') == DAY).sort('symbol')
    assert today['close'].to_list() == [3., 7., 5.]
    assert history.equals(original)
    assert 'csg_positive' not in current.columns


def _matrix_engine(root: Path):
    directory = root / 'matrix_fixture'
    directory.mkdir()
    (directory / 'live_matrix.py').write_text('''import numpy as np
from app.backtest.matrix import make_signal_matrix
META = {"id": "live_matrix", "name": "live", "asset_types": ["stock"], "timeframes": ["1d"]}
EXECUTION_BACKEND = "matrix_native"
class LiveMatrix:
    def required_fields(self): return frozenset({"csg_positive"})
    def required_warmup_bars(self, params): return 1
    def compute_signals(self, market, params):
        hit = np.nan_to_num(market.fields["csg_positive"]) > 0
        return make_signal_matrix(market.shape, entry=hit.astype(np.uint8))
MATRIX_STRATEGY = LiveMatrix()
''')
    return StrategyEngine(strategy_dirs=[directory])


def test_matrix_shift_survives_second_quote_and_next_day_without_history_reload(private_owner):
    _save_signal(private_owner.data_dir, shift=True)
    engine = _matrix_engine(private_owner.data_dir)
    def ohlc(frame):
        return frame.with_columns(*(pl.col('close').alias(name) for name in ('open', 'high', 'low')))
    history = ohlc(pl.DataFrame({'symbol': ['A', 'B'], 'date': [DAY - timedelta(days=1)] * 2, 'close': [2., 8.]}))
    def context(day, prices, historical=None):
        return StrategyDataContext('stock', '1d', day,
            current=ohlc(pl.DataFrame({'symbol': ['B', 'A'], 'date': [day] * 2, 'close': prices})),
            history=historical, cache_key='live')
    first = context(DAY, [7., 3.])
    with pytest.raises(ValueError, match='requires history data'):
        engine.prepare_realtime_matrix(first, ['live_matrix'])
    first.history = history
    market = engine.prepare_realtime_matrix(first, ['live_matrix'])
    assert np.asarray(market.fields['csg_positive'][-1]).tolist() == [1., 0.]
    second = context(DAY, [9., 1.])
    market = engine.prepare_realtime_matrix(second, ['live_matrix'])
    assert np.asarray(market.fields['csg_positive'][-1]).tolist() == [0., 1.]
    second.market = market
    result = engine.run('live_matrix', second, overrides={'basic_filter': {'enabled': False}})
    assert [row['symbol'] for row in result.rows] == ['B']
    tomorrow = context(DAY + timedelta(days=1), [8., 2.])
    market = engine.prepare_realtime_matrix(tomorrow, ['live_matrix'])
    assert np.asarray(market.fields['csg_positive'][-1]).tolist() == [1., 0.]
    tomorrow.market = market
    result = engine.run('live_matrix', tomorrow, overrides={'basic_filter': {'enabled': False}})
    assert [row['symbol'] for row in result.rows] == ['A']
    assert engine.realtime_matrix_stats('live')['build_count'] == 1
    assert engine.realtime_matrix_stats('live')['update_count'] == 2
    assert history['close'].to_list() == [2., 8.]
    retained = engine._realtime_matrices['live'].signal_history
    assert retained.group_by('symbol').len()['len'].max() == 2
    assert retained['date'].min() == DAY


@pytest.mark.parametrize('stale_columns', [False, True])
def test_context_without_daily_expressions_reuses_frames_or_only_drops_old_columns(private_owner, stale_columns):
    # An intraday-only owner must use the same daily fast path as an empty owner.
    custom_signals.save_one(private_owner.data_dir, {
        'id': 'minute_only', 'timeframe': 'intraday',
        'conditions': [{'left': 'close', 'op': '>', 'right': '1'}],
    })
    history = pl.DataFrame({'symbol': ['A', 'A'], 'date': [DAY - timedelta(days=1), DAY], 'close': [2., 0.]})
    current = pl.DataFrame({'symbol': ['A'], 'date': [DAY], 'close': [3.]})
    if stale_columns:
        history = history.with_columns(pl.lit(True).alias('csg_old_owner'))
        current = current.with_columns(pl.lit(False).alias('csg_old_owner'))
    result = custom_signals.apply_user_context(StrategyDataContext('stock', '1d', DAY, current=current, history=history))
    if not stale_columns:
        assert result.history is history
        assert result.current is current
    else:
        assert result.history.equals(history.drop('csg_old_owner'))
        assert result.current.equals(current.drop('csg_old_owner'))
        assert 'csg_old_owner' in history.columns
        assert 'csg_old_owner' in current.columns
