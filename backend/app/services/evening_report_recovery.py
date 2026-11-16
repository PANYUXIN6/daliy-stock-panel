"""Reaccept retained, dated postmarket facts after the evening input was removed.

This is an explicit administrator operation inside the shared writer slot. It
does not backfill current classifications into a historical date. A successful
dated pipeline, its unchanged stock references, and files written during that
pipeline establish the recoverable boundary; missing bars still need evidence.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

import polars as pl

from app.market_time import CN_TZ, cn_now
from app.services.daily_coverage import DailyCoverage, DailyCoverageError
from app.services.evening_report_inputs import _scope, input_versions, save_ready
from app.services.evening_report_store import read_json
from app.services.postmarket import reconcile_daily_status, require_enriched, stock_universe
from app.services.review_publication import get_publication, strategy_input_files
from app.tickflow.sector_review_reader import SectorReviewReader


def _reference(root: Path, path: Path) -> list:
    stat = path.stat()
    return [str(path.relative_to(root)), stat.st_size, stat.st_mtime_ns]


def _pipeline(root: Path, day: date) -> dict:
    jobs = []
    for path in (root / 'job_store').glob('*.json'):
        job = read_json(path)
        if (job and job.get('request_key') == f'postmarket:{day}' and job.get('status') == 'succeeded'
                and not job.get('result', {}).get('stage_errors')
                and job.get('result', {}).get('postmarket_date') == str(day)):
            jobs.append(job)
    if not jobs:
        raise DailyCoverageError(f'{day} 缺少成功的当日盘后验收记录，无法恢复历史输入')
    return max(jobs, key=lambda job: job['finished_at'])


def _metadata_from_dated_names(root: Path, day: date, frames: list[pl.DataFrame],
                               observations: list[dict], raw: pl.DataFrame) -> list[dict]:
    """Recover mutable qualification only from dated names, never today's status.

    The current table contributes only the historical listing event. It must
    precede an independently retained trade; its actual observation time remains
    separate from the dated name observation.
    """
    from app.price_limits import parse_listing_date
    from app.services.instrument_sync import METADATA_RULE_VERSION, _scope_metadata, _valid_name

    names = []
    for frame in frames:
        if '股票简称' not in frame.columns:
            raise DailyCoverageError('证券资料已被覆盖，保留分类缺少当日证券名称')
        names.append(frame.select('symbol', '股票简称').sort('symbol'))
    if not names[0].equals(names[1]):
        raise DailyCoverageError('当日概念与行业表的证券名称不一致，不能恢复证券资格')
    dated_names = dict(names[0].iter_rows())
    current = pl.read_parquet(root / 'instruments/instruments.parquet')
    if ('listing_date' not in current.columns or current['symbol'].n_unique() != current.height):
        raise DailyCoverageError('缺少唯一的历史上市日期资料')
    listings = {row['symbol']: row for row in current.select(
        'symbol', 'listing_date', 'metadata_source', 'metadata_observed_at').to_dicts()}
    files = SectorReviewReader(root).files('kline_daily', end=day)
    first_trade = dict(pl.scan_parquet(files).select('symbol', 'date').group_by('symbol')
                       .agg(pl.col('date').min()).collect().iter_rows())
    rows = []
    observed = max(o['obtained_at'] for o in observations)
    for symbol in sorted(set(dated_names) | set(raw['symbol'])):
        name = dated_names.get(symbol)
        row = {'symbol': symbol, 'name': name or symbol, 'code': symbol.split('.')[0],
               'exchange': symbol.split('.')[-1], 'type': 'stock', 'is_current': True}
        row.update(_scope_metadata(row, {}))
        # An unclassified instrument may be excluded by a known asset/board
        # boundary. Missing names can never qualify an in-scope instrument.
        if not _valid_name(name, symbol) and _scope(row) is not False:
            raise DailyCoverageError(f'{symbol} 缺少当日证券名称，无法确认历史ST资格')
        source = listings.get(symbol, {})
        listed = parse_listing_date(source.get('listing_date'))
        first = first_trade.get(symbol)
        if listed is None or first is None or listed > first or listed > day:
            raise DailyCoverageError(f'{symbol} 的上市日期与保留交易历史无法核对')
        row.update(listing_date=listed.isoformat(), as_of=day.isoformat(),
                   metadata_source='retained-classification-names', metadata_observed_at=observed,
                   metadata_recovered_at=cn_now().isoformat(), metadata_rule_version=METADATA_RULE_VERSION,
                   is_st_source='retained_classification_name' if name else 'unknown',
                   listing_source=source.get('metadata_source'),
                   listing_observed_at=source.get('metadata_observed_at'),
                   listing_first_retained_trade=first.isoformat())
        rows.append(row)
    return rows


def recover_ready(repo, day: date, *, check_cancel=None, authorize=None) -> dict:
    """Only recover a missing original; the caller owns authorization and the slot."""
    from app.services.evening_report_store import EveningReportStore
    from app.services.regime_builder import load_regime_history
    from app.services.trading_day import calendar_status

    check = check_cancel or (lambda: None)
    root = repo.store.data_dir
    check()
    if day >= cn_now().date():
        raise DailyCoverageError('历史恢复仅用于已结束交易日；今日应执行盘后准备')
    if EveningReportStore(root).has_original(str(day)):
        raise DailyCoverageError('原发布已存在；修订必须使用其保留的完整输入')
    if str(day) not in calendar_status(root).get('days', []):
        raise DailyCoverageError(f'{day} 尚未被交易日历确认为交易日')
    job = _pipeline(root, day)
    started = datetime.fromisoformat(job['started_at']).astimezone(CN_TZ)
    finished = datetime.fromisoformat(job['finished_at']).astimezone(CN_TZ)
    if started.date() != day or finished.date() != day or started.hour < 16:
        raise DailyCoverageError('盘后验收时间不属于目标日收盘后')
    sources = []

    def retained(relative: str) -> Path:
        path = root / relative
        if not path.is_file():
            raise DailyCoverageError(f'{day} 缺少保留数据：{relative}')
        written = datetime.fromtimestamp(path.stat().st_mtime, CN_TZ)
        if not started <= written <= finished:
            raise DailyCoverageError(f'{relative} 已非该日盘后任务保存的版本，不能用当前数据替代')
        sources.append(_reference(root, path))
        return path

    publication = get_publication(root, day)
    original_files = publication.get('strategy_inputs', {}).get('stock')
    if (not publication.get('published') or publication.get('as_of') != str(day)
            or not str(publication.get('published_at', '')).startswith(str(day))
            or not original_files or original_files != strategy_input_files(root, day)):
        raise DailyCoverageError('股票行情或指标与当日复盘引用不一致，不能恢复历史输入')
    retained(f'market_recap/date={day}.json')
    retained('adj_factor/all.parquet')
    factors = pl.read_parquet(root / 'adj_factor/all.parquet')
    if factors.height and (
        factors.select(pl.struct('symbol', 'trade_date').is_duplicated().any()).item()
        or factors.filter(pl.col('trade_date') > day).height
        or not factors.select((pl.col('ex_factor').is_finite() & (pl.col('ex_factor') > 0)).fill_null(False).all()).item()
    ):
        raise DailyCoverageError('复权因子存在重复、非法数值或目标日后的事件')
    observations, classification_frames = [], []
    for name, column, kind in (('ext_gn_ths', '所属概念', 'concept'),
                                ('ext_hy_ths', '所属同花顺行业', 'industry')):
        path = retained(f'ext_data/{name}/part.parquet')
        frame = pl.read_parquet(path)
        classification_frames.append(frame)
        if (not {'symbol', column}.issubset(frame.columns) or frame.is_empty()
                or frame['symbol'].null_count() or frame['symbol'].n_unique() != frame.height
                or frame[column].null_count()):
            raise DailyCoverageError(f'{name} 缺少完整、唯一的证券分类记录')
        memberships = []
        for row in frame.select('symbol', column).iter_rows(named=True):
            value = row[column]
            if not isinstance(value, str):
                raise DailyCoverageError(f'{name} 分类字段格式无效')
            labels = value.split(';') if value else []
            if any(not label.strip() for label in labels):
                raise DailyCoverageError(f'{name} 分类标签无效')
            memberships.extend({'symbol': row['symbol'], 'sector_id': label, 'sector_name': label, 'source': kind}
                               for label in sorted(set(labels)))
        observations.append({
            'id': uuid4().hex, 'source': f'retained:{name}',
            'obtained_at': datetime.fromtimestamp(path.stat().st_mtime, CN_TZ).isoformat(),
            'recovered_at': cn_now().isoformat(), 'source_effective_date': None,
            'source_published_at': None, 'source_version': _reference(root, path),
            'complete': True, 'completeness_basis': '成功盘后任务内保存的完整分类表，恢复时重新核对证券覆盖',
            'membership_semantics': 'retained-labels-v1',
            'limitation': '保留表中的原始分类名称；取得时间为当日落盘时间，恢复时间单独记录',
            'symbols': frame['symbol'].to_list(), 'memberships': memberships,
        })
    retained('regime_history/part.parquet')
    retained('mainline_history/part.parquet')
    market = load_regime_history(root).filter(pl.col('date') == day)
    if market.height != 1 or any(market.row(0, named=True).get(key) is None for key in ('state', 'phase')):
        raise DailyCoverageError('缺少目标日已计算的环境与情绪周期')
    sentiment = publication.get('overview', {}).get('sentiment')
    if not sentiment or sentiment.get('as_of') != str(day):
        raise DailyCoverageError('当日复盘未保留情绪结果')
    before = input_versions(root, day)
    reader = SectorReviewReader(root)
    raw = reader.raw('kline_daily', day, day)
    if raw.select(pl.struct('symbol', 'date').is_duplicated().any()).item():
        raise DailyCoverageError('当日股票行情存在重复记录')
    metadata_path = root / 'instruments/instruments.parquet'
    sources.append(_reference(root, metadata_path))
    metadata = pl.read_parquet(metadata_path)
    written = datetime.fromtimestamp(metadata_path.stat().st_mtime, CN_TZ)
    metadata_method = 'retained-instruments'
    if (started <= written <= finished and 'metadata_observed_at' in metadata.columns
            and metadata['metadata_observed_at'].str.slice(0, 10).eq(str(day)).fill_null(False).all()):
        expected = stock_universe(repo, day)
        recovered_metadata = metadata.to_dicts()
    else:
        recovered_metadata = _metadata_from_dated_names(root, day, classification_frames, observations, raw)
        expected = [row['symbol'] for row in recovered_metadata]
        metadata_method = 'dated-names-with-verified-listing-events'
    coverage = DailyCoverage(day)
    coverage.observe(raw)
    reconcile_daily_status(coverage, expected, check_cancel=check)
    summary = coverage.require(expected)
    if summary != job['result'].get('readiness', {}).get('daily'):
        raise DailyCoverageError('逐股恢复的交易、停牌覆盖与当日验收汇总不一致')
    require_enriched(root, coverage)
    # The original index marker bug must be repaired through formal daily data,
    # never by silently blessing a stored quote as a historical close.
    indices = reader.raw('kline_index_daily', date.fromisoformat(min(calendar_status(root)['days'])), day)
    benchmark = indices.filter(pl.col('symbol') == '000001.SH')
    if benchmark.is_empty() or not benchmark.select(
        (pl.col('quote_ts').is_null() & pl.col('close').is_finite() & (pl.col('close') > 0)).fill_null(False).all()
    ).item() or day not in benchmark['date'].to_list():
        raise DailyCoverageError('上证指数历史仍含未核验行情，请先修复正式指数日线')
    check()
    if authorize:
        authorize()
    if before != input_versions(root, day) or any(_reference(root, root / ref[0]) != ref for ref in sources):
        raise DailyCoverageError('历史恢复期间输入发生变化，请重新验收')
    return save_ready(root, day, coverage, observations,
                      {**market.row(0, named=True), 'sentiment': sentiment},
                      recovery={'method': 'retained-postmarket-v1', 'job_id': job['id'],
                                'metadata_method': metadata_method,
                                'accepted_at': finished.isoformat(), 'recovered_at': cn_now().isoformat(),
                                'sources': sources, 'stock_reference_count': len(original_files)},
                      recovered_metadata=recovered_metadata)
