"""财务股票池与供应商覆盖核对，不修改历史维表。"""
import json
from types import SimpleNamespace

import polars as pl
import pytest

from app.data_providers import custom
from app.plugins.fuyao.provider import FuyaoProvider
from app.services import financial_sync, preferences
from app.tickflow.capabilities import CapabilitySet


def test_scope_records_differences_and_preserves_instruments(tmp_path, monkeypatch):
    path = tmp_path / 'instruments' / 'instruments.parquet'
    path.parent.mkdir()
    pl.DataFrame({'symbol': ['600000.SH', '600005.SH', '000001.SZ']}).write_parquet(path)
    original = path.read_bytes()
    monkeypatch.setattr(financial_sync, '_financial_is_custom', lambda: True)
    monkeypatch.setattr(preferences, 'get_financial_provider', lambda: 'fuyao')
    provider = FuyaoProvider()
    provider._client = SimpleNamespace(snapshot_all=lambda: ([
        {'thscode': '000001.SZ'}, {'thscode': '600000.SH'}, {'thscode': '001381.SZ'},
    ], 0))
    monkeypatch.setattr(custom, 'get_provider', lambda name: provider)
    assert financial_sync._get_symbols(tmp_path) == ['600000.SH', '000001.SZ', '001381.SZ']
    assert path.read_bytes() == original
    report = json.loads((tmp_path / 'financials' / 'universe.json').read_text())
    assert report['provider'] == 'fuyao'
    assert report['excluded_symbols'] == ['600005.SH']
    assert report['provider_only_symbols'] == ['001381.SZ']
    assert report['requested_symbols'] == 3


@pytest.mark.parametrize('outcome', ['empty', 'error'])
def test_universe_failure_does_not_use_unverified_pool(tmp_path, monkeypatch, outcome):
    path = tmp_path / 'instruments' / 'instruments.parquet'
    path.parent.mkdir()
    pl.DataFrame({'symbol': ['600000.SH']}).write_parquet(path)

    def universe():
        if outcome == 'error':
            raise RuntimeError('supplier unavailable')
        return []

    monkeypatch.setattr(financial_sync, '_financial_is_custom', lambda: True)
    monkeypatch.setattr(preferences, 'get_financial_provider', lambda: 'test')
    monkeypatch.setattr(custom, 'get_provider', lambda name: SimpleNamespace(get_financial_universe=universe))
    with pytest.raises(RuntimeError):
        financial_sync._get_symbols(tmp_path)
    assert not (tmp_path / 'financials' / 'universe.json').exists()


def test_legacy_provider_preserves_existing_pool(tmp_path, monkeypatch):
    path = tmp_path / 'instruments' / 'instruments.parquet'
    path.parent.mkdir()
    pl.DataFrame({'symbol': ['600000.SH']}).write_parquet(path)
    monkeypatch.setattr(financial_sync, '_financial_is_custom', lambda: True)
    monkeypatch.setattr(custom, 'get_provider', lambda name: SimpleNamespace())
    assert financial_sync._get_symbols(tmp_path) == ['600000.SH']


def test_sync_preserves_out_of_scope_history_and_adds_provider_only_stock(tmp_path, monkeypatch):
    instruments = tmp_path / 'instruments' / 'instruments.parquet'
    instruments.parent.mkdir()
    pl.DataFrame({'symbol': ['600000.SH', '600005.SH']}).write_parquet(instruments)
    history = tmp_path / 'financials' / 'income' / 'part.parquet'
    history.parent.mkdir(parents=True)
    pl.DataFrame({'symbol': ['600005.SH'], 'period_end': ['2016-12-31'],
                  'net_income': [12.]}).write_parquet(history)
    requested = []

    def fetch(table, symbols, latest_only):
        requested.extend(symbols)
        return pl.DataFrame({'symbol': symbols, 'period_end': ['2026-06-30'] * len(symbols),
                             'net_income': [25.] * len(symbols)})

    provider = SimpleNamespace(get_financial_universe=lambda: ['600000.SH', '001381.SZ'],
                               get_financials=fetch)
    monkeypatch.setattr(financial_sync, '_financial_is_custom', lambda: True)
    monkeypatch.setattr(preferences, 'get_financial_provider', lambda: 'test')
    monkeypatch.setattr(custom, 'get_provider', lambda name: provider)

    assert financial_sync.sync_income(tmp_path, CapabilitySet()) == 3
    assert requested == ['600000.SH', '001381.SZ']
    saved = pl.read_parquet(history)
    legacy = saved.filter(pl.col('symbol') == '600005.SH')
    assert legacy.select('symbol', 'period_end', 'net_income').row(0, named=True) == {
        'symbol': '600005.SH', 'period_end': '2016-12-31', 'net_income': 12.,
    }
    assert legacy['observed_at'].item() is None  # 不给旧文件伪造观察证据
    assert legacy['source'].item() is None
    assert saved.filter(pl.col('symbol') == '001381.SZ')['net_income'].item() == 25.
