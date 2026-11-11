"""盘后市场复盘快照，与可选 AI 文字报告独立。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.market_time import cn_now, cn_today


def get_publication(data_dir: Path, day: date | None = None) -> dict:
    day = day or cn_today()
    path = data_dir / "market_recap" / f"date={day.isoformat()}.json"
    if not path.exists():
        return {"as_of": day.isoformat(), "published": False, "published_at": None, "overview": None}
    return json.loads(path.read_text(encoding="utf-8"))


def publish_review(repo, day: date, quote_service=None, depth_service=None) -> dict:
    from app.services.market_overview_builder import _json_safe, build_market_overview
    from app.services.shared_runtime import publish_data_update

    overview = build_market_overview(repo, quote_service, depth_service, day)
    payload = {"as_of": day.isoformat(), "published": True,
               "published_at": cn_now().isoformat(), "overview": _json_safe(overview),
               "strategy_inputs": strategy_inputs(repo.store.data_dir, day)}
    path = repo.store.data_dir / "market_recap" / f"date={day.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str), encoding="utf-8")
    tmp.replace(path)
    publish_data_update(repo.store.data_dir, ["review"])
    return payload


def publish_review_if_ready(repo, quote_service=None, depth_service=None) -> dict | None:
    """仅发布已经落盘的交易日期；今日盘中同步不提前发布盘后复盘。"""
    from datetime import time
    _, day = repo.get_enriched_latest()
    now = cn_now()
    if day is None or day > now.date() or (day == now.date() and now.time() < time(15)):
        return None
    return publish_review(repo, day, quote_service, depth_service)


def strategy_input_files(data_dir: Path, day: date, asset_type: str = 'stock') -> list[list]:
    """引用正式日线与指标的既有文件版本；不复制行情，不使用实时 generation。"""
    from app.tickflow.sector_review_reader import SectorReviewReader
    reader = SectorReviewReader(data_dir)
    tables = ('kline_daily', 'kline_daily_enriched') if asset_type == 'stock' else ('kline_etf_daily', 'kline_etf_enriched')
    references = []
    for table in tables:
        for item in reader.files(table, end=day):
            stat = item.stat()
            references.append([str(item.relative_to(data_dir)), stat.st_size, stat.st_mtime_ns])
    return references


def strategy_inputs(data_dir: Path, day: date) -> dict:
    """发布时确认当日为正式日线，再记录可被手动选股消费的版本。"""
    import polars as pl

    from app.enriched_generation import enriched_publication_incomplete
    ready = {}
    now = cn_now()
    if day > now.date() or (day == now.date() and now.hour < 16):
        return ready
    for asset, table, enriched in (
        ('stock', 'kline_daily', 'kline_daily_enriched'),
        ('etf', 'kline_etf_daily', 'kline_etf_enriched'),
    ):
        files = list((data_dir / table / f'date={day}').glob('*.parquet'))
        if not files or enriched_publication_incomplete(data_dir, asset):
            continue
        if not list((data_dir / enriched / f'date={day}').glob('*.parquet')):
            continue
        raw = pl.read_parquet(files)
        # 旧格式无 quote_ts 的历史正式日线继续兼容。
        if raw.is_empty() or ('quote_ts' in raw.columns and raw['quote_ts'].is_not_null().any()):
            continue
        enriched_files = list((data_dir / enriched / f'date={day}').glob('*.parquet'))
        frame = pl.read_parquet(enriched_files)
        if frame.is_empty() or ('quote_ts' in frame.columns and frame['quote_ts'].is_not_null().any()):
            continue
        ready[asset] = strategy_input_files(data_dir, day, asset)
    return ready


def latest_strategy_publication(data_dir: Path, through: date, asset_type: str) -> dict | None:
    """只读发布记录；未正式就绪时沿用最近的历史数据，不启动采集。"""
    for filename in sorted((data_dir / 'market_recap').glob('date=*.json'), reverse=True):
        try:
            day = date.fromisoformat(filename.stem.removeprefix('date='))
        except ValueError:
            continue
        if day > through:
            continue
        payload = json.loads(filename.read_text(encoding='utf-8'))
        if not payload.get('published'):
            continue
        references = (payload.get('strategy_inputs') or {}).get(asset_type)
        # 升级前的发布没有版本引用：只在手动点击时读取本地正式分区，
        # 不回写历史发布，不要求用户为了升级重跑数据管道。
        if 'strategy_inputs' not in payload:
            references = strategy_inputs(data_dir, day).get(asset_type)
        if references:
            # 原始数据部分写入或指标尚未完成时不能使用旧发布宣称就绪。
            from app.enriched_generation import enriched_publication_incomplete
            if not enriched_publication_incomplete(data_dir, asset_type) and references == strategy_input_files(data_dir, day, asset_type):
                return {'as_of': day.isoformat(), 'version': payload['published_at'], 'files': references}
    return None
