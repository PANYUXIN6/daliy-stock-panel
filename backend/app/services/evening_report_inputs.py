"""Accepted postmarket evidence and strictly local, formal evening-report inputs.

File references detect a raw-only failed rewrite that does not advance enriched
publication generation. They never establish formalness or classification coverage.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import polars as pl

from app.enriched_generation import EnrichedGenerationUnavailableError, get_enriched_generation
from app.market_time import cn_now
from app.parquet import scan_enriched_parquet
from app.polars_guard import guarded_collect
from app.services.daily_coverage import DailyCoverageError
from app.services.evening_report_store import EveningReportStore
from app.services.trading_day import calendar_status
from app.tickflow.sector_review_reader import SectorReviewReader


def input_versions(data_dir: Path, day: date) -> dict:
    reader = SectorReviewReader(data_dir)
    paths = []
    for table in ('kline_daily', 'kline_daily_enriched', 'kline_index_daily'):
        paths.extend(reader.files(table, end=day))
    factor_path = data_dir / 'adj_factor' / 'all.parquet'
    if factor_path.exists():
        paths.append(factor_path)
    references = []
    for path in sorted(paths):
        try:
            stat = path.stat()
        except FileNotFoundError as exc:
            raise DailyCoverageError('晚报输入文件已变化，请重新等待盘后验收') from exc
        references.append([str(path.relative_to(data_dir)), stat.st_size, stat.st_mtime_ns])
    # The benchmark consumes raw index daily bars, not index enriched data.
    # Index writes have no enriched publication marker; their actual input
    # partitions are covered by the file references above.
    return {'stock_generation': get_enriched_generation(data_dir, 'stock', initialize=False),
            'files': references}


def save_ready(data_dir: Path, day: date, coverage, observations: list[dict], market: dict,
               *, recovery: dict | None = None, recovered_metadata: list[dict] | None = None) -> dict:
    """Called only after required stages 1–8 succeeded, inside the pipeline slot."""
    if len(observations) != 2 or any(not o or o.get('complete') is not True for o in observations):
        raise DailyCoverageError('本轮概念及行业完整分类观察尚未就绪')
    if any(date.fromisoformat(o['obtained_at'][:10]) != day for o in observations):
        raise DailyCoverageError('分类观察并非目标交易日取得，不能作为当晚原发布输入')
    if recovered_metadata is not None and not recovery:
        raise DailyCoverageError('恢复的证券资格必须附带历史核验依据')
    metadata = (recovered_metadata if recovered_metadata is not None else
                pl.read_parquet(data_dir / 'instruments' / 'instruments.parquet').to_dicts())
    expected = {row['symbol'] for row in metadata if _scope(row) is True}
    for observation in observations:
        missing = expected - set(observation['symbols'])
        if missing:
            raise DailyCoverageError(f"本轮分类响应缺少 {len(missing)} 只范围内证券，不能由合并旧表补齐")
    from app.services import evening_report_classification
    from app.services.preferences import get_mainline_filter_config
    from app.services.sentiment_observations import load_observation

    # Freeze the same dated domain result displayed by the environment page.
    sentiment = market.get('sentiment') if recovery else load_observation(data_dir, day.isoformat())
    market = {**market, 'sentiment': sentiment}

    store = EveningReportStore(data_dir)
    calendar = calendar_status(data_dir)
    previous_days = [value for value in calendar.get('days', []) if value < day.isoformat()]
    historical_suspensions = []
    previous_classification_input_id = None
    for prior_day in previous_days[-20:]:
        original = store.original_metadata(prior_day)
        prior = (store.input_evidence(original['input_id']) if original and original.get('input_id')
                 else store.readiness(prior_day))
        if prior and prior.get('complete'):
            historical_suspensions.extend({**item, 'source_input_id': prior['input_id']}
                                          for item in prior.get('halted_evidence', []))
            if prior_day == previous_days[-1]:
                previous_classification_input_id = prior['input_id']
    # 关系源/行业语义变化后不续接旧来源的核心身份与连续轮次；历史证据不回写。
    membership_semantics = sorted({o['membership_semantics'] for o in observations if o.get('membership_semantics')})
    scope_version = 'main-chinext-nonst-v1/' + evening_report_classification.RULE_VERSION
    if membership_semantics:
        scope_version += '/' + '+'.join(membership_semantics)
    evidence = {
        'date': day.isoformat(), 'cutoff': cn_now().isoformat(),
        'versions': input_versions(data_dir, day), 'classifications': observations,
        'active_symbols': sorted(coverage.active), 'halted_symbols': sorted(coverage.halted),
        'halted_evidence': list(coverage.halted_evidence.values()),
        'market': market, 'complete': True, 'metadata': metadata,
        'calendar': calendar, 'sector_filter_config': get_mainline_filter_config(),
        'filter_rule_version': evening_report_classification.RULE_VERSION,
        'scope_version': scope_version,
        'historical_suspensions': historical_suspensions,
        'previous_classification_input_id': previous_classification_input_id,
    }
    if coverage.halted - coverage.halted_evidence.keys():
        raise DailyCoverageError('部分停牌股票缺少逐股来源证据')
    if recovery:
        evidence['recovery'] = recovery
    EveningReportStore(data_dir).save_readiness(day.isoformat(), evidence)
    return evidence


def require_ready(data_dir: Path, day: date, *, input_id: str | None = None) -> dict:
    store = EveningReportStore(data_dir)
    evidence = store.input_evidence(input_id) if input_id else store.readiness(day.isoformat())
    if input_id and evidence is None:
        raise DailyCoverageError(f'{day} 的完整输入证据已过保留期或不存在；可核验原快照，不能完整重演历史筛选')
    if not evidence or not evidence.get('complete'):
        raise DailyCoverageError('等待正式行情、分类与市场环境完成本轮验收')
    if evidence.get('date') != day.isoformat() or not evidence.get('metadata') or not evidence.get('calendar'):
        raise DailyCoverageError(f'{day} 缺少可恢复的当时证券资格或交易日历证据')
    try:
        current = input_versions(data_dir, day)
    except EnrichedGenerationUnavailableError as exc:
        raise DailyCoverageError('指标版本正在发布或不可用，等待正式输入恢复') from exc
    if evidence['versions']['files'] != current['files']:
        raise DailyCoverageError('就绪后输入版本已变化，等待新的盘后验收记录')
    return evidence


def _prices(reader: SectorReviewReader, day: date, instruments: pl.DataFrame, cancel_check=None,
            suspension_evidence: list[dict] | None = None) -> pl.DataFrame:
    paths = reader.files('kline_daily', end=day)
    if not paths:
        raise DailyCoverageError('正式历史日线为空')
    start = date.fromisoformat(paths[0].parent.name[5:])
    raw = reader.raw('kline_daily', start, day).sort('symbol', 'date')
    if raw.select(pl.struct('symbol', 'date').is_duplicated().any()).item():
        raise DailyCoverageError('正式历史日线存在重复证券日期')
    halt = (((pl.col('open') == 0) & (pl.col('high') == 0))
            | ((pl.col('volume') == 0) & (pl.col('amount') == 0))).fill_null(False)
    valid = (pl.all_horizontal(pl.col(c).is_finite() & (pl.col(c) > 0)
                              for c in ('open', 'high', 'low', 'close'))
             & (pl.col('high') >= pl.max_horizontal('open', 'close', 'low'))
             & (pl.col('low') <= pl.min_horizontal('open', 'close'))
             & pl.all_horizontal(pl.col(c).is_finite() & (pl.col(c) >= 0)
                                 for c in ('volume', 'amount')))
    raw = raw.with_columns(halt.alias('_halt'),
                           (pl.col('quote_ts').is_null() & (halt | valid)).fill_null(False).alias('_formal_raw'))
    # ATR is recursive: an earlier snapshot cannot silently become a formal seed.
    if suspension_evidence is not None:
        suspension_evidence.extend(raw.filter(pl.col('_halt') & pl.col('_formal_raw')).select('symbol', 'date').to_dicts())
    raw = raw.with_columns(pl.col('_formal_raw').cum_min().over('symbol').alias('_formal_dependencies'))
    enriched_paths = reader.files('kline_daily_enriched', start, day)
    if not enriched_paths:
        raise DailyCoverageError('正式历史指标为空')
    lf = scan_enriched_parquet(enriched_paths, hive_partitioning=True)
    names = set(lf.collect_schema().names())
    wanted = ['symbol', 'date', 'open', 'low', 'close', 'high', 'ma5', 'ma10', 'ma20', 'bull_bear_line', 'atr_14', 'high_60d',
              'signal_limit_up', 'signal_limit_down', 'consecutive_limit_ups',
              'raw_close', 'raw_high', 'raw_low', 'amount', 'volume', 'quote_ts']
    enriched = guarded_collect(lf.select([pl.col(c) if c in names else pl.lit(None).alias(c)
                                         for c in wanted]))
    if enriched.select(pl.struct('symbol', 'date').is_duplicated().any()).item():
        raise DailyCoverageError('历史指标存在重复证券日期')
    raw = raw.select('symbol', 'date', '_halt', '_formal_dependencies',
                     *[pl.col(c).alias('_source_' + c) for c in ('open', 'high', 'low', 'close', 'amount', 'volume')])
    frame = enriched.join(raw, on=['symbol', 'date'], how='left')
    equality = pl.all_horizontal(
        (pl.col(left) - pl.col('_source_' + right)).abs() < 0.005
        for left, right in [('raw_close', 'close'), ('raw_high', 'high'), ('raw_low', 'low'),
                            ('amount', 'amount'), ('volume', 'volume')])
    frame = frame.filter(~pl.col('_halt').fill_null(False)).with_columns(
        (pl.col('_formal_dependencies') & ~pl.col('_halt') & pl.col('quote_ts').is_null()
         & equality).fill_null(False).alias('formal'),
        pl.col('_source_open').alias('raw_open'),
    ).drop([c for c in frame.columns if c.startswith('_')]).sort('symbol', 'date')
    frame = frame.with_columns(pl.col('formal').cum_min().over('symbol').alias('formal'))
    # Disk enriched partitions intentionally contain only storage columns. Reuse
    # the indicator pipeline in bounded batches, including the entire recursive
    # seed history checked above; never substitute missing disk columns with zero.
    from app.indicators.pipeline import compute_indicators, compute_limit_signals

    symbols = frame['symbol'].unique().sort().to_list()
    parts = []
    for offset in range(0, len(symbols), 256):
        if cancel_check:
            cancel_check()
        batch = symbols[offset:offset + 256]
        part = compute_indicators(frame.filter(pl.col('symbol').is_in(batch)),
                                  needed={'ma5', 'ma10', 'ma20', 'bull_bear_line', 'atr_14', 'high_60d', 'change_pct', 'vol_ratio_5d'})
        part = compute_limit_signals(part, instruments.filter(pl.col('symbol').is_in(batch)),
                                     needed={'signal_limit_up', 'signal_limit_down', 'consecutive_limit_ups'})
        keep = [column for column in [*wanted, 'formal', 'raw_open'] if column in part.columns]
        parts.append(part.select(keep))
    return pl.concat(parts, how='diagonal_relaxed')


def _scope(record: dict) -> bool | None:
    checks = [record.get('type') == 'stock' if record.get('type') else None,
              record.get('exchange') in ('SH', 'SZ') if record.get('exchange') else None,
              record.get('board') in ('main', 'chinext') if record.get('board') else None,
              record.get('security_type') == 'a_share' if record.get('security_type') else None,
              not record['is_st'] if record.get('is_st') is not None else None,
              record.get('is_current') is not False]
    return False if False in checks else (None if None in checks else True)


def _metadata(data_dir: Path, evidence: dict, calendar: list[str], historical_suspensions: list[dict]) -> pl.DataFrame:
    from app.price_limits import is_no_limit_day, parse_listing_date, price_limit_pct

    rows = []
    active, halted = set(evidence['active_symbols']), set(evidence['halted_symbols'])
    recent = set(calendar[-21:])
    historical_halts = {r['symbol'] for r in [*historical_suspensions, *evidence.get('historical_suspensions', [])]
                        if str(r['date']) in recent}
    day = date.fromisoformat(evidence['date'])
    for record in evidence['metadata']:
        symbol = record['symbol']
        in_scope = _scope(record)
        normal = False if symbol in halted else (True if symbol in active else None)
        listing = parse_listing_date(record.get('listing_date'))
        day_rank = sum(d >= listing.isoformat() for d in calendar) if listing else None
        regime = None
        if record.get('board') and record.get('is_st') is not None:
            if is_no_limit_day(symbol, listing, day, day_rank=day_rank):
                regime = 'no_limit'
            else:
                regime = f"{price_limit_pct(symbol, day, is_risk_warning=record['is_st']) * 100:g}%"
        rows.append({**record, 'in_scope': in_scope, 'trading_normal': normal, 'regime': regime,
                     'suspension_window': symbol in historical_halts and symbol not in halted,
                     'qualification_pause': symbol in halted})
    return pl.DataFrame(rows, infer_schema_length=None)


def _classification_changes(data_dir: Path, evidence: dict, calendar: list[str]) -> list[dict]:
    prior_id = evidence.get('previous_classification_input_id')
    previous = EveningReportStore(data_dir).input_evidence(prior_id) if prior_id else None
    previous_by_source = {o['source']: o for o in previous.get('classifications', [])} if previous and previous.get('complete') else {}
    changes = []
    for observation in evidence['classifications']:
        prior = previous_by_source.get(observation['source'])
        current_members = {(r['symbol'], r['sector_id']) for r in observation['memberships']}
        prior_members = {(r['symbol'], r['sector_id']) for r in prior['memberships']} if prior else set()
        changes.append({'source': observation['source'], 'comparison': 'adjacent_complete_observation' if prior else 'first_observation',
                        'previous_observation_id': prior['id'] if prior else None,
                        'added': sorted(current_members - prior_members) if prior else [],
                        'removed': sorted(prior_members - current_members) if prior else [],
                        'first_observed': sorted(current_members) if prior is None else [],
                        'interpretation': '比较口径发生变化，排名变化原因尚未分离' if prior and current_members != prior_members else None})
    return changes


def _memberships(evidence: dict) -> tuple[pl.DataFrame, dict]:
    from collections import Counter, defaultdict

    from app.services import evening_report_classification

    rows = [row for observation in evidence['classifications'] for row in observation['memberships']]
    cfg = evidence['sector_filter_config']
    counts = defaultdict(set)
    for row in rows:
        if row['source'] == 'concept':
            counts[row['sector_id']].add(row['symbol'])
    excluded = []
    for sector_id, symbols in sorted(counts.items()):
        declared = evening_report_classification.exclusion(sector_id)
        reasons = [declared[1]] if declared else []
        if len(symbols) > 120:
            reasons.append('分类完整成员数超过120只')
        if reasons:
            excluded.append({'sector_id': sector_id, 'members': len(symbols), 'reasons': reasons,
                             'category': declared[0] if declared else 'configured_filter'})
    blocked = {entry['sector_id'] for entry in excluded}
    kept = [row for row in rows if row['source'] != 'concept' or row['sector_id'] not in blocked]
    frame = pl.DataFrame(kept, schema={'symbol': pl.String, 'sector_id': pl.String,
                                      'sector_name': pl.String, 'source': pl.String})
    return frame, {'config': {'max_members': 120}, 'previous_config': cfg,
                   'rule_version': evening_report_classification.RULE_VERSION, 'excluded': excluded,
                   'excluded_counts': dict(Counter(item['category'] for item in excluded)),
                   'limitation': '明确非产业标签按版本化完整名称排除；成员上限按本轮完整分类统计，未列入语义映射的标签不凭名称猜测'}


def load_inputs(data_dir: Path, day: date, evidence: dict, cancel_check=None):
    from app.services.evening_report_engine import EveningInputs

    check = cancel_check or (lambda: None)
    check()
    calendar = evidence.get('calendar', {}).get('days', [])
    if day.isoformat() not in calendar:
        raise DailyCoverageError('交易日历尚未确认目标日')
    before = [d for d in calendar if d <= day.isoformat()]
    future = [d for d in calendar if d > day.isoformat()]
    reader = SectorReviewReader(data_dir)
    instruments = pl.DataFrame(evidence['metadata'], infer_schema_length=None)
    historical_suspensions = []
    prices = _prices(reader, day, instruments, check, historical_suspensions)
    for suspension in historical_suspensions:
        suspension.update(confirmed=True, source='formal_daily_history', basis='formal_daily_zero_price_or_volume_amount',
                          source_version=evidence['versions']['stock_generation'], obtained_at=evidence['cutoff'])
    check()
    all_benchmarks = reader.raw('kline_index_daily', date.fromisoformat(before[0]), day).with_columns(
        (pl.col('quote_ts').is_null() & pl.col('close').is_finite() & (pl.col('close') > 0)).alias('formal'))
    benchmark = all_benchmarks.filter(pl.col('symbol') == '000001.SH')
    memberships, sector_filter = _memberships(evidence)
    check()
    return EveningInputs(trade_date=day.isoformat(), next_trade_date=future[0] if future else None,
                         cutoff=evidence['cutoff'], calendar=before, prices=prices,
                         metadata=_metadata(data_dir, evidence, before, historical_suspensions), memberships=memberships,
                         benchmark=benchmark, abnormal_benchmarks=all_benchmarks, market=evidence['market'],
                         provenance={'classifications': evidence['classifications'], 'sector_filter': sector_filter,
                                     'calendar': evidence['calendar'],
                                     'scope_version': evidence.get('scope_version'),
                                     'recovery': evidence.get('recovery'),
                                     'suspensions': evidence['halted_evidence'] + historical_suspensions,
                                     'membership_changes': _classification_changes(data_dir, evidence, before),
                                     'input_versions': evidence['versions']})


def prepare_evaluation_inputs(data_dir: Path, inputs) -> None:
    """Recompute both endpoints from raw closes at one explicit factor cutoff.

    Runs only in the post-publication evaluator, never while reading a report.
    Report-date enriched values are not reused as evaluation prices.
    """
    from app.indicators.pipeline import _apply_adj_factor

    path = data_dir / 'adj_factor' / 'all.parquet'
    if not path.exists():
        raise DailyCoverageError('客观回看缺少可核验复权因子文件')
    try:
        factors = pl.read_parquet(path).select(
            pl.col('symbol'), pl.col('trade_date').cast(pl.Date, strict=False),
            pl.col('ex_factor').cast(pl.Float64, strict=False))
    except (OSError, pl.exceptions.PolarsError) as exc:
        raise DailyCoverageError('客观回看复权因子不可读') from exc
    if factors.select(pl.any_horizontal(pl.all().is_null()).any()).item():
        raise DailyCoverageError('客观回看复权因子包含未知字段')
    factors = factors.filter(pl.col('trade_date') <= date.fromisoformat(inputs.trade_date))
    if (factors.select((~pl.col('ex_factor').is_finite() | (pl.col('ex_factor') <= 0)).any()).item()
            or factors.select(pl.struct('symbol', 'trade_date').is_duplicated().any()).item()):
        raise DailyCoverageError('客观回看复权因子无效或重复')
    dates = (pl.col('date').str.to_date() if inputs.prices.schema['date'] == pl.String else pl.col('date').cast(pl.Date))
    frame = inputs.prices.with_columns(dates)
    raw = frame.select('symbol', 'date', pl.col('raw_close').alias('close'))
    adjusted = _apply_adj_factor(raw, factors).select('symbol', 'date', 'close')
    inputs.prices = frame.drop('close').join(adjusted, on=['symbol', 'date'], how='left')
    if 'is_st' in inputs.metadata.columns:
        inputs.prices = inputs.prices.join(inputs.metadata.select('symbol', pl.col('is_st').alias('_is_st_at_cutoff')),
                                          on='symbol', how='left').with_columns(
            pl.when(pl.col('date') == date.fromisoformat(inputs.trade_date))
            .then(pl.col('_is_st_at_cutoff')).otherwise(None).alias('is_st')).drop('_is_st_at_cutoff')
    # The current instrument table cannot establish historical ST status.
    # Keep historical tradability unknown instead of applying today's regime.
    inputs.prices = inputs.prices.with_columns([
        pl.when(pl.col('date') == date.fromisoformat(inputs.trade_date)).then(pl.col(key)).otherwise(None).alias(key)
        for key in ('signal_limit_up', 'signal_limit_down') if key in inputs.prices.columns])
    calendar = inputs.provenance.get('calendar', {})
    source = next((ref for ref in inputs.provenance['input_versions']['files']
                   if ref[0] == 'adj_factor/all.parquet'), None)
    inputs.provenance['evaluation_context'] = {
        'adjust_method': 'qfq', 'adjust_base_date': inputs.trade_date,
        'adjust_rule_version': 'raw-times-event-cumfactor-over-base-v1',
        'adjustment_source': source,
        'calendar_version': {key: calendar.get(key) for key in ('start', 'end', 'updated_at')},
        'suspension_source': sorted({row.get('source', 'unspecified') for row in inputs.provenance.get('suspensions', [])}),
    }
