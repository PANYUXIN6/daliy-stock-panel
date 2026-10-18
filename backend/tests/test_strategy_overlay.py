from datetime import date

import numpy as np
import polars as pl
import pytest

from app.strategy import overlay
from app.strategy.engine import StrategyDataContext, StrategyDef, StrategyEngine


def condition(**kwargs):
    return dict(left='close', op='>', right='10', **kwargs)


def strategy():
    return StrategyDef(meta={'id': 'overlay_test', 'scoring': {'close': 1}, 'limit': 1,
                             'order_by': 'score', 'asset_types': ['stock'], 'timeframes': ['1d']},
                       basic_filter={}, entry_signals=[], exit_signals=['signal_exit'],
                       stop_loss=None, trailing_stop=None, trailing_take_profit_activate=None,
                       trailing_take_profit_drawdown=None, max_hold_days=None,
                       filter_fn=lambda df, p: pl.lit(True), filter_history_fn=None,
                       lookback_days=1, source='builtin')


def test_overlay_filters_before_score_and_keeps_exits():
    s = strategy()
    engine = StrategyEngine(strategy_dirs=[])
    engine._strategies[s.meta['id']] = s
    frame = pl.DataFrame({'symbol': ['A', 'B', 'C'], 'date': [date(2026, 1, 5)] * 3,
                          'close': [5., 15., 20.], 'signal_exit': [True, False, False]})
    context = StrategyDataContext(asset_type='stock', timeframe='1d', as_of=date(2026, 1, 5), current=frame)
    result = engine.run('overlay_test', context, overrides={'overlay_filter': [condition()]})
    assert [r['symbol'] for r in result.rows] == ['C']
    assert result.rows[0]['score'] == 100.
    assert result.exit_signal_hits == [{'symbol': 'A', 'signals': ['signal_exit']}]
    assert engine.get('overlay_test').source == 'builtin'


def test_shift_uses_symbol_trading_rows_and_preserves_input_order():
    frame = pl.DataFrame({'symbol': ['A', 'B', 'A', 'B'],
                          'date': [date(2026, 1, 5), date(2026, 1, 2), date(2026, 1, 2), date(2026, 1, 5)],
                          'close': [12., 14., 9., 15.]})
    assert overlay.mask(frame, [condition(leftDays=1)]).to_list() == [False, False, False, True]


@pytest.mark.parametrize('value', [None, {}, [dict(left=[], op='>', right=1)],
    [dict(left='close', op='>', right='nan')], [condition(leftDays=1.5)],
    [condition(leftDays=-1)], [condition()] * 9])
def test_invalid_config_fails_closed(value):
    with pytest.raises(ValueError):
        overlay.resolve({'overlay_filter': value})


def test_nonfinite_and_missing_values_cannot_pass():
    frame = pl.DataFrame({'close': [np.nan, np.inf, None, 11.]})
    assert overlay.mask(frame, [condition()]).to_list() == [False, False, False, True]
    with pytest.raises(ValueError, match='不存在'):
        overlay.mask(frame.drop('close'), [condition()])


def test_empty_and_unsupported_backends():
    assert overlay.resolve({}) == []
    assert overlay.resolve({'overlay_filter': []}, 'minute_filter') == []
    with pytest.raises(ValueError, match='不支持'):
        overlay.resolve({'overlay_filter': [condition()]}, 'composite')


def test_matrix_overlay_matches_polars_before_scoring_and_retains_exit():
    from types import SimpleNamespace

    from app.backtest.matrix import (
        MatrixPipelineConfig,
        MatrixStrategyPipeline,
        build_market_data_matrix,
        make_signal_matrix,
    )
    frame = pl.DataFrame({'symbol': ['A', 'B', 'C'], 'date': [date(2026, 1, 5)] * 3,
                          'open': [5., 15., 20.], 'high': [5., 15., 20.], 'low': [5., 15., 20.],
                          'close': [5., 15., 20.], 'volume': [1., 1., 1.]})
    market = build_market_data_matrix(frame)
    raw = make_signal_matrix(market.shape, entry=np.ones(market.shape, dtype=np.uint8),
                             exit=np.ones(market.shape, dtype=np.uint8))
    s = SimpleNamespace(compute_signals=lambda m, p: raw)
    result = MatrixStrategyPipeline().run(s, market, {}, MatrixPipelineConfig(
        basic_filter={}, scoring={'close': 1.}, order_by='score', descending=True,
        overlay_filter=[condition()]))
    assert result.entry.tolist() == [[0, 1, 1]]
    assert result.exit.tolist() == [[1, 1, 1]]
    assert result.score[0, 1:].tolist() == [0., 100.]
    assert raw.entry.tolist() == [[1, 1, 1]]  # 不污染共享策略输出


def test_matrix_and_polars_rolling_factor_and_trading_gap_agree():
    from datetime import timedelta

    from app.backtest.matrix import build_market_data_matrix
    frame = pl.DataFrame([{'symbol': symbol, 'date': date(2026, 1, 1) + timedelta(days=i),
                          'open': float(i+1), 'high': float(i+1), 'low': float(i+1),
                          'close': float(i+1), 'volume': float(10+i)}
                         for i in range(25) for symbol in ['A', 'B'] if not (symbol == 'A' and i == 23)])
    market = build_market_data_matrix(frame)
    conditions = [dict(left='ma20_bias', op='>', right='0', leftDays=1)]
    actual = overlay.matrix_mask(market, conditions)
    assert actual[-1].tolist() == [True, True]
    assert not actual[23, 0]
    expected = frame.filter(overlay.mask(frame, conditions))
    assert actual.sum() == expected.height
    assert overlay.history_bars(conditions) >= 21


def test_user_config_roundtrip_clear_and_monitor_isolation(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.api import strategy as api
    from app.config import settings
    from app.services import strategy_cache
    from app.services.user_context import UserContext, user_scope
    from app.strategy import config, monitor_rules
    from app.strategy.monitor import MonitorRuleEngine

    monkeypatch.setattr(settings, 'data_dir', tmp_path)
    day = date(2026, 1, 5)
    monkeypatch.setattr('app.strategy.monitor.cn_today', lambda: day)
    s = strategy()
    s.meta['limit'] = 100
    engine = StrategyEngine(strategy_dirs=[])
    engine._strategies['overlay_test'] = s
    owners = [UserContext('a' * 32, 'admin', tmp_path), UserContext('b' * 32, 'guest', tmp_path)]
    states = {}
    for owner in owners:
        monitor = MonitorRuleEngine()
        monitor.set_strategy_engine(engine)
        monitor.set_data_dir(tmp_path)
        monitor.set_rules([monitor_rules.normalize(dict(id='test', name='测试', type='strategy',
            asset_type='stock', scope='all', strategy_id='overlay_test', notify_events=['buy_signal', 'sell_signal']))])
        states[owner.id] = SimpleNamespace(strategy_engine=engine, monitor_engine=monitor)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
        user_runtimes=SimpleNamespace(get=lambda owner: states[owner.id]))))
    frame = pl.DataFrame({'symbol': ['A', 'B'], 'name': ['A', 'B'], 'date': [day, day],
                          'close': [5., 15.], 'change_pct': [0., 0.], 'signal_exit': [True, False]})
    for owner, threshold in zip(owners, ['10', '0'], strict=True):
        with user_scope(owner):
            saved = [dict(left='close', op='>', right=threshold)]
            api.save_config(api.SaveConfigRequest(strategy_id='overlay_test', overrides={'overlay_filter': saved}), request)
            assert api._strategy_detail(s, config.load_override(tmp_path, 'overlay_test'))['overlay_filter'] == saved
            states[owner.id].monitor_engine.evaluate(frame)
            strategy_cache.write_cache(tmp_path, str(day), {'overlay_test': {'rows': [{'symbol': threshold}]}})
    assert [r['symbol'] for r in states[owners[0].id].monitor_engine.latest_strategy_results()['overlay_test']['rows']] == ['B']
    assert [r['symbol'] for r in states[owners[1].id].monitor_engine.latest_strategy_results()['overlay_test']['rows']] == ['B', 'A']
    with user_scope(owners[0]):
        api.patch_config(api.SaveConfigRequest(strategy_id='overlay_test', overrides={'overlay_filter': []}), request)
        assert config.load_override(tmp_path, 'overlay_test')['overlay_filter'] == []
        assert states[owners[0].id].monitor_engine.latest_strategy_results() == {}
        assert not strategy_cache.read_cache(tmp_path).get('results')
    with user_scope(owners[1]):
        assert config.load_override(tmp_path, 'overlay_test')['overlay_filter'][0]['right'] == '0'
        assert strategy_cache.read_cache(tmp_path)['results']['overlay_test']['rows'] == [{'symbol': '0'}]
        assert states[owners[1].id].monitor_engine.latest_strategy_results()['overlay_test']['total'] == 2


def test_monitor_loads_overlay_history_for_plain_strategy(tmp_path, monkeypatch, private_owner):
    from app.strategy import config, monitor_rules
    from app.strategy.monitor import MonitorRuleEngine
    day = date(2026, 1, 5)
    monkeypatch.setattr('app.strategy.monitor.cn_today', lambda: day)
    engine = StrategyEngine(strategy_dirs=[])
    s = strategy()
    s.meta['limit'] = 100
    engine._strategies['overlay_test'] = s
    history = pl.DataFrame({'symbol': ['A', 'B'], 'date': [date(2026, 1, 2)] * 2, 'close': [5., 15.]})
    current = pl.DataFrame({'symbol': ['A', 'B'], 'date': [day] * 2, 'close': [20., 20.]})
    config.save_override(tmp_path, 'overlay_test', {'overlay_filter': [condition(leftDays=1)]})
    monitor = MonitorRuleEngine()
    monitor.set_strategy_engine(engine)
    monitor.set_data_dir(tmp_path)
    monitor.set_history_loader(lambda target, bars: history if bars >= 2 else pl.DataFrame())
    monitor.set_rules([monitor_rules.normalize(dict(id='test', name='测试', type='strategy',
        asset_type='stock', scope='all', strategy_id='overlay_test', notify_events=['buy_signal']))])
    monitor.evaluate(current)
    assert [r['symbol'] for r in monitor.latest_strategy_results()['overlay_test']['rows']] == ['B']


def test_overlay_api_rejects_invalid_conditions_without_overwriting(tmp_path, private_owner):
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.api import strategy as api
    from app.strategy import config
    engine = StrategyEngine(strategy_dirs=[])
    engine._strategies['overlay_test'] = strategy()
    state = SimpleNamespace(strategy_engine=engine)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        repo=SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path)),
        user_runtimes=SimpleNamespace(get=lambda _: state))))
    original = {'overlay_filter': [condition()]}
    config.save_override(tmp_path, 'overlay_test', original)
    for action in [api.save_config, api.patch_config]:
        with pytest.raises(HTTPException) as error:
            action(api.SaveConfigRequest(strategy_id='overlay_test', overrides={'overlay_filter': {}}), request)
        assert error.value.status_code == 400
        assert config.load_override(tmp_path, 'overlay_test') == original


def test_matrix_overlay_includes_ohlc_inputs_of_derived_indicators():
    from datetime import timedelta

    from app.backtest.matrix import build_market_data_matrix
    frame = pl.DataFrame({'symbol': ['A'] * 30,
        'date': [date(2026, 1, 1) + timedelta(days=i) for i in range(30)],
        'open': [10.] * 30, 'high': [11.] * 30, 'low': [9.] * 30,
        'close': [10.] * 30, 'volume': [100.] * 30})
    conditions = [{'left': 'atr_pct', 'op': '>', 'right': '0.1'}]
    actual = overlay.matrix_mask(build_market_data_matrix(frame), conditions)
    assert actual[-1, 0]
    assert actual[:, 0].tolist() == overlay.mask(frame, conditions).to_list()
    with pytest.raises(ValueError, match='依赖字段不足'):
        overlay.mask(frame.drop('high'), conditions)


def test_composite_history_includes_saved_child_overlay():
    from dataclasses import replace

    from app.strategy.engine import CompositeChild, CompositeSpec

    child = strategy()
    parent = replace(child, meta={**child.meta, 'id': 'parent'}, execution_backend='composite',
                     composite=CompositeSpec((CompositeChild('overlay_test', 1.),)))
    engine = StrategyEngine(strategy_dirs=[], override_loader=lambda sid: {
        'overlay_filter': [condition(leftDays=40)]} if sid == 'overlay_test' else {})
    engine._strategies.update(overlay_test=child, parent=parent)
    assert engine.required_history_bars(['parent']) == 41


def test_long_overlay_factor_history_is_not_capped_at_180_calendar_days(tmp_path, monkeypatch):
    from datetime import timedelta
    from types import SimpleNamespace

    from app.services.screener import ScreenerService

    monkeypatch.setattr('app.services.screener._history_cache', {})
    days = [date(2025, 1, 1) + timedelta(days=i) for i in range(500)]
    days = [day for day in days if day.weekday() < 5]
    frame = pl.DataFrame({'symbol': ['A'] * len(days), 'date': days,
        'open': [10.] * len(days), 'high': [11.] * len(days), 'low': [9.] * len(days),
        'close': [10.] * len(days), 'volume': [100.] * len(days), 'amount': [1000.] * len(days)})
    root = tmp_path / 'kline_daily_enriched' / 'sample'
    root.mkdir(parents=True)
    frame.write_parquet(root / 'sample.parquet')
    repo = SimpleNamespace(store=SimpleNamespace(data_dir=tmp_path),
        get_enriched_history=lambda *args: None, get_instruments_asset=lambda *args: pl.DataFrame())
    result = ScreenerService(repo)._load_enriched_history(days[-1], 301)
    assert result['date'].n_unique() >= 301
