"""正式盘后版本不能被盘中快照、实时 generation 或未完成写入替代。"""
import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import polars as pl

from app.enriched_generation import bump_enriched_generation
from app.services import review_publication as publication

DAY = date(2026, 11, 11)


def bars(root, day, *, live=False):
    for table in ('kline_daily', 'kline_daily_enriched'):
        path = root / table / f'date={day}' / 'part.parquet'
        path.parent.mkdir(parents=True, exist_ok=True)
        pl.DataFrame({'symbol': ['000001.SZ'], 'date': [day], 'close': [10.0],
                      'quote_ts': ['2026-11-11T15:00:00' if live else None]}).write_parquet(path)


def publish(root, day=DAY, version='v1'):
    path = root / 'market_recap' / f'date={day}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'as_of': str(day), 'published': True, 'published_at': version,
                                'strategy_inputs': publication.strategy_inputs(root, day)}))


def test_1600_boundary_and_formal_snapshot_requirement(tmp_path, monkeypatch):
    clock = [datetime(2026, 11, 11, 15, 59, tzinfo=ZoneInfo('Asia/Shanghai'))]
    monkeypatch.setattr(publication, 'cn_now', lambda: clock[0])
    monkeypatch.setattr(publication, 'cn_today', lambda: DAY)
    bars(tmp_path, DAY)
    assert publication.strategy_inputs(tmp_path, DAY) == {}
    clock[0] = clock[0].replace(hour=16, minute=0)
    bars(tmp_path, DAY, live=True)
    publish(tmp_path)
    assert publication.latest_strategy_publication(tmp_path, DAY, 'stock') is None
    bars(tmp_path, DAY)
    publish(tmp_path)
    assert publication.latest_strategy_publication(tmp_path, DAY, 'stock')['version'] == 'v1'


def test_realtime_generation_and_newer_day_do_not_invalidate_formal_history(tmp_path, monkeypatch):
    monkeypatch.setattr(publication, 'cn_now', lambda: datetime(2026, 11, 11, 16, tzinfo=ZoneInfo('Asia/Shanghai')))
    monkeypatch.setattr(publication, 'cn_today', lambda: DAY)
    bars(tmp_path, DAY)
    publish(tmp_path)
    bump_enriched_generation(tmp_path)
    bars(tmp_path, date(2026, 11, 12), live=True)
    expected = publication.latest_strategy_publication(tmp_path, DAY, 'stock')
    assert publication.latest_strategy_publication(tmp_path, DAY, 'stock') == expected
    # 正式输入变更但尚未重新发布时，不以旧发布证据启动新计算。
    bars(tmp_path, DAY)
    assert publication.latest_strategy_publication(tmp_path, DAY, 'stock') is None
    publish(tmp_path, version='v2')
    assert publication.latest_strategy_publication(tmp_path, DAY, 'stock')['version'] == 'v2'


def test_old_publication_can_be_used_without_republishing(tmp_path, monkeypatch):
    monkeypatch.setattr(publication, 'cn_now', lambda: datetime(2026, 11, 11, 16, tzinfo=ZoneInfo('Asia/Shanghai')))
    bars(tmp_path, DAY)
    path = tmp_path / 'market_recap' / f'date={DAY}.json'
    path.parent.mkdir(parents=True)
    old = {'as_of': str(DAY), 'published': True, 'published_at': 'legacy'}
    path.write_text(json.dumps(old))
    assert publication.latest_strategy_publication(tmp_path, DAY, 'stock')['version'] == 'legacy'
    assert json.loads(path.read_text()) == old
