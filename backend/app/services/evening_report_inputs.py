"""盘后验收一次，晚报读取当日正式行情与共享指标。"""
from __future__ import annotations

from bisect import bisect_left
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import polars as pl

from app.enriched_generation import EnrichedGenerationUnavailableError, get_enriched_generation
from app.indicators.pipeline import _apply_adj_factor, validated_factor_events
from app.market_time import cn_now
from app.parquet import DAILY_STORAGE_SCHEMA, scan_daily_parquet, scan_enriched_parquet
from app.polars_guard import guarded_collect
from app.services.daily_coverage import DailyCoverageError
from app.services.evening_report_store import EveningReportStore
from app.services.evening_report_wave import WINDOW
from app.services.postmarket import read_suspensions
from app.services.trading_day import calendar_status
from app.tickflow.sector_review_reader import SectorReviewReader


def save_ready(data_dir: Path, day: date, coverage, observations: list[dict], market: dict,
               *, before_commit=None) -> dict:
    from app.services.evening_report_events import sync_events
    from app.services.sentiment_observations import load_observation

    instruments = pl.read_parquet(data_dir / 'instruments' / 'instruments.parquet')
    fields = ('symbol', 'name', 'type', 'exchange', 'board', 'security_type', 'is_st', 'is_current', 'listing_date')
    metadata = instruments.select(c for c in fields if c in instruments.columns).to_dicts()
    expected = {row['symbol'] for row in metadata if _scope(row) is True}
    accepted = {}
    for observation in observations:
        if not observation:
            continue
        kind = observation['kind']
        if (observation.get('complete') is True and observation['obtained_at'][:10] == day.isoformat()
                and not expected.difference(observation['symbols'])):
            accepted[kind] = observation
    if 'concept' not in accepted:
        raise DailyCoverageError('本轮概念分类未完整覆盖目标交易日及范围内证券')
    halted = coverage.halted - coverage.active
    if halted - coverage.halted_evidence.keys():
        raise DailyCoverageError('部分停牌股票缺少逐股来源证据')
    store = EveningReportStore(data_dir)
    calendar = calendar_status(data_dir)
    sessions = [d for d in calendar.get('days', []) if d <= day.isoformat()]
    if not sessions or sessions[-1] != day.isoformat():
        raise DailyCoverageError('交易日历尚未确认目标日')
    previous_day = sessions[-2] if len(sessions) > 1 else None
    previous = store.readiness(previous_day) if previous_day else None
    evidence = {
        'date': day.isoformat(), 'complete': True, 'cutoff': cn_now().isoformat(),
        'stock_generation': get_enriched_generation(data_dir, 'stock', initialize=False),
        'market': {**market, 'sentiment': load_observation(data_dir, day.isoformat())},
        'calendar': calendar, 'metadata': metadata, 'classifications': list(accepted.values()),
        'industry_status': 'available' if 'industry' in accepted else 'missing',
        'active_symbols': sorted(coverage.active), 'halted_symbols': sorted(halted),
        'halted_evidence': [coverage.halted_evidence[s] for s in sorted(halted)],
        'severe_events': sync_events(day, calendar=sessions),
    }
    evidence['classification_comparisons'] = _classification_comparisons(
        evidence, previous if previous and previous.get('classifications') else None)
    store.save_readiness(day.isoformat(), evidence, before_commit=before_commit)
    return evidence


def require_ready(data_dir: Path, day: date) -> dict:
    evidence = EveningReportStore(data_dir).readiness(day.isoformat())
    if not evidence or not evidence.get('complete') or evidence.get('date') != day.isoformat():
        raise DailyCoverageError('等待正式行情、概念分类与市场环境完成本轮验收')
    try:
        generation = get_enriched_generation(data_dir, 'stock', initialize=False)
    except EnrichedGenerationUnavailableError as exc:
        raise DailyCoverageError('指标版本正在发布或不可用，等待正式输入恢复') from exc
    if evidence.get('stock_generation') != generation:
        raise DailyCoverageError('股票指标代次已变化，等待新的盘后验收')
    return evidence


def _scope(record: dict) -> bool | None:
    checks = [record.get('type') == 'stock' if record.get('type') else None,
              record.get('exchange') in ('SH', 'SZ') if record.get('exchange') else None,
              record.get('board') in ('main', 'chinext') if record.get('board') else None,
              record.get('security_type') == 'a_share' if record.get('security_type') else None,
              not record['is_st'] if record.get('is_st') is not None else None,
              record.get('is_current') is not False]
    return False if False in checks else (None if None in checks else True)


def _metadata(evidence: dict, calendar: list[str]) -> pl.DataFrame:
    from app.price_limits import is_no_limit_day, parse_listing_date, price_limit_pct

    rows = []
    active, halted = set(evidence['active_symbols']), set(evidence['halted_symbols'])
    day = date.fromisoformat(evidence['date'])
    for record in evidence['metadata']:
        symbol = record['symbol']
        in_scope = _scope(record)
        normal = False if symbol in halted else (True if symbol in active else None)
        listing = parse_listing_date(record.get('listing_date'))
        day_rank = len(calendar) - bisect_left(calendar, listing.isoformat()) if listing else None
        regime = None
        if record.get('board') and record.get('is_st') is not None:
            if is_no_limit_day(symbol, listing, day, day_rank=day_rank):
                regime = 'no_limit'
            else:
                regime = f"{price_limit_pct(symbol, day, is_risk_warning=record['is_st']) * 100:g}%"
        rows.append({**record, 'in_scope': in_scope, 'trading_normal': normal, 'regime': regime,
                     'qualification_pause': symbol in halted})
    return pl.DataFrame(rows, infer_schema_length=None)


def _member_keys(observation):
    return {(row['symbol'], row['sector_id']) for row in observation['memberships']}


def _classification_comparisons(evidence, previous):
    previous_by_source = {o['source']: o for o in previous['classifications']} if previous else {}
    result = []
    for observation in evidence['classifications']:
        prior = previous_by_source.get(observation['source'])
        if prior and prior.get('membership_semantics') != observation.get('membership_semantics'):
            prior = None
        current_scope = set(observation['symbols'])
        added = removed = 0
        first = len(current_scope)
        if prior:
            current_members, prior_members = _member_keys(observation), _member_keys(prior)
            prior_scope = set(prior['symbols'])
            added = sum(s in prior_scope for s, _ in current_members - prior_members)
            removed = sum(s in current_scope for s, _ in prior_members - current_members)
            first = len(current_scope - prior_scope)
        result.append({
            'source': observation['source'], 'classification_type': observation['kind'],
            'comparison': 'adjacent_complete_observation' if prior else 'first_observation',
            'observation_id': observation['id'], 'previous_observation_id': prior['id'] if prior else None,
            'previous_obtained_at': prior.get('obtained_at') if prior else None,
            'added_count': added, 'removed_count': removed, 'first_observed_count': first,
        })
    return result


def _raw_halt():
    return ((pl.col('open') == 0) & (pl.col('high') == 0)) | ((pl.col('volume') == 0) & (pl.col('amount') == 0))


def _deduplicate(frame):
    return frame.unique(['symbol', 'date'], keep='last', maintain_order=True)


def _status_suspensions(data_dir, days, quotes):
    proofs = read_suspensions(data_dir, days)
    if not proofs:
        return []
    keys = pl.DataFrame([(p['symbol'], date.fromisoformat(p['date'])) for p in proofs],
                        schema={'symbol': pl.String, 'date': pl.Date}, orient='row')
    restored = quotes.filter(pl.col('formal')).select('symbol', 'date').join(keys, on=['symbol', 'date'], how='semi')
    active = {(s, d.isoformat()) for s, d in restored.iter_rows()}
    return [r for r in proofs if (r['symbol'], r['date']) not in active]


def _factor_events(data_dir, day, symbols=None):
    path = data_dir / 'adj_factor' / 'all.parquet'
    if not path.exists():
        return None
    source = pl.scan_parquet(path)
    if symbols is not None:
        source = source.filter(pl.col('symbol').is_in(list(symbols)))
    return validated_factor_events(guarded_collect(source), through=day)


def _history(repo, day, calendar, check):
    reader = SectorReviewReader(repo.store.data_dir)
    start = date.fromisoformat(calendar[-WINDOW:][0])
    paths = reader.files('kline_daily_enriched', start, day)
    if not paths:
        raise DailyCoverageError('正式历史指标为空')
    stored = _deduplicate(guarded_collect(scan_enriched_parquet(paths, hive_partitioning=True)
                                          .filter(pl.col('date').is_between(start, day))))
    raw = _deduplicate(reader.raw('kline_daily', start, day)).filter(pl.col('quote_ts').is_null())
    halts = raw.filter(_raw_halt()).select('symbol', 'date').to_dicts()
    raw = raw.filter(~_raw_halt().fill_null(False)).select(
        'symbol', 'date', pl.col('open').alias('raw_open'), pl.col('close').alias('raw_close'),
        pl.col('high').alias('raw_high'), pl.col('low').alias('raw_low'), 'amount', 'volume')
    stored = stored.filter(pl.col('quote_ts').is_null()).drop('quote_ts', 'raw_close', 'raw_high', 'raw_low', 'amount', 'volume')
    frame = raw.join(stored, on=['symbol', 'date'], how='left')
    # 历史连板来自相应交易日的正式指标，均线等优先使用共享缓存。
    indicators = ['ma5', 'ma10', 'ma20', 'bull_bear_line', 'atr_14', 'high_60d', 'change_pct', 'vol_ratio_5d']
    span = repo.get_enriched_history_span()
    cached = (repo.get_enriched_range(max(start, span[0]), day, columns=[*indicators, 'quote_ts'])
              if span and span[0] <= day <= span[1] else None)
    if cached is None or cached.is_empty():
        raise DailyCoverageError('当日共享指标缓存尚未就绪，待预热完成后重试')
    cached = _deduplicate(cached)
    if 'quote_ts' in cached.columns:
        cached = cached.filter(pl.col('quote_ts').is_null()).drop('quote_ts')
    frame = frame.join(cached, on=['symbol', 'date'], how='left')
    frame = frame.with_columns(pl.lit(None, dtype=pl.Float64).alias(c) for c in indicators if c not in frame.columns)
    if frame['ma10'].null_count():
        # 波次窗口外只取前 30 个市场交易日的正式成交收盘，用于预热个股 MA10。
        warmup_paths = [p for d in calendar[-(WINDOW + 30):-WINDOW]
                        for p in sorted((repo.store.data_dir / 'kline_daily_enriched' / f'date={d}').glob('*.parquet'))]
        if warmup_paths:
            warmup = _deduplicate(scan_enriched_parquet(warmup_paths, hive_partitioning=True)).filter(
                pl.col('quote_ts').is_null() & ~_raw_halt().fill_null(False)).select('symbol', 'date', 'close')
            frame = pl.concat([guarded_collect(warmup), frame], how='diagonal')
        local_ma10 = pl.col('close').rolling_mean(10).over('symbol', order_by='date')
        frame = frame.with_columns(pl.coalesce('ma10', local_ma10).alias('ma10')).filter(pl.col('date') >= start)
    check()
    factors = _factor_events(repo.store.data_dir, day)
    if factors is not None:
        invalid = factors.filter(pl.col('ex_factor').is_null())['symbol'].unique()
        if len(invalid):
            frame = frame.with_columns(pl.when(~pl.col('symbol').is_in(invalid.implode())).then(c).alias(c)
                                       for c in ['open', 'high', 'low', 'close', *indicators])
    frame = frame.with_columns(
        (pl.col('raw_close').is_finite() & (pl.col('raw_close') > 0)).fill_null(False).alias('formal'),
        (pl.col('consecutive_limit_ups') > 0).alias('signal_limit_up'),
        (pl.col('consecutive_limit_downs') > 0).alias('signal_limit_down'))
    return frame.sort('symbol', 'date'), halts


def load_inputs(repo, day: date, evidence: dict, cancel_check=None):
    from app.services.evening_report_engine import EveningInputs

    check = cancel_check or (lambda: None)
    check()
    sessions = evidence['calendar']['days']
    today = day.isoformat()
    calendar = [d for d in sessions if d <= today]
    window = calendar[-WINDOW:]
    prices, raw_halts = _history(repo, day, calendar, check)
    suspensions = {(r['symbol'], str(r['date'])): {**r, 'source': 'formal_daily_history', 'confirmed': True}
                   for r in raw_halts}
    data_dir = repo.store.data_dir
    for row in [*_status_suspensions(data_dir, window, prices), *evidence['halted_evidence']]:
        suspensions[row['symbol'], str(row['date'])] = row
    metadata = _metadata(evidence, calendar)
    observations = evidence['classifications']
    memberships = pl.DataFrame([row for o in observations for row in o['memberships']],
                                schema=dict.fromkeys(['symbol', 'sector_id', 'sector_name', 'source'], pl.String))
    benchmarks = _deduplicate(SectorReviewReader(data_dir).raw('kline_index_daily', date.fromisoformat(window[0]), day))
    benchmarks = benchmarks.with_columns(
        (pl.col('quote_ts').is_null() & pl.col('close').is_finite() & (pl.col('close') > 0)).alias('formal'))
    check()
    return EveningInputs(
        trade_date=today, next_trade_date=next((d for d in sessions if d > today), None), cutoff=evidence['cutoff'],
        calendar=calendar, prices=prices, memberships=memberships, metadata=metadata, market=evidence['market'],
        abnormal_benchmarks=benchmarks,
        provenance={'industry_status': evidence['industry_status'], 'suspensions': list(suspensions.values()),
                    'classifications': observations, 'membership_changes': evidence['classification_comparisons'],
                    'severe_events': evidence.get('severe_events'), 'stock_generation': evidence['stock_generation']})


def _constraint_history(data_dir, prices, symbols, days):
    paths = [p for day in days for p in sorted((data_dir / 'kline_daily_enriched' / f'date={day}').glob('*.parquet'))]
    if paths:
        flags = _deduplicate(guarded_collect(scan_enriched_parquet(paths, hive_partitioning=True)
            .filter(pl.col('symbol').is_in(list(symbols)) & pl.col('date').cast(pl.String).is_in(days))
            .select('symbol', 'date', 'quote_ts', 'consecutive_limit_ups', 'consecutive_limit_downs')))
        flags = flags.filter(pl.col('quote_ts').is_null()).select('symbol', 'date',
            (pl.col('consecutive_limit_ups') > 0).alias('signal_limit_up'),
            (pl.col('consecutive_limit_downs') > 0).alias('signal_limit_down'))
        prices = prices.join(flags, on=['symbol', 'date'], how='left')
    # 维表逐条带 as_of，仅附到同日的观察端点。
    path = data_dir / 'instruments' / 'instruments.parquet'
    if path.is_file() and {'symbol', 'as_of', 'is_st'} <= set(pl.read_parquet_schema(path)):
        metadata = guarded_collect(pl.scan_parquet(path).filter(
            pl.col('as_of').cast(pl.String).is_in(days) & pl.col('symbol').is_in(list(symbols))
        ).select('symbol', pl.col('as_of').cast(pl.Date).alias('date'), 'is_st'))
        prices = prices.join(_deduplicate(metadata), on=['symbol', 'date'], how='left')
    return prices


def load_evaluation_inputs(data_dir: Path, as_of: date, *, symbols: set[str], days: set[str],
                           include_multipliers=False, include_constraints=False):
    """Read only requested securities/sessions and use one cumulative adjustment basis."""
    calendar = calendar_status(data_dir)
    if not calendar.get('days'):
        raise DailyCoverageError('客观回看缺少本地标准交易日历')
    dates = sorted(set(calendar['days']).intersection(days))
    past = [d for d in dates if d <= as_of.isoformat()]
    paths = [path for d in past for path in sorted((data_dir / 'kline_daily' / f'date={d}').glob('*.parquet'))]
    raw = (_deduplicate(guarded_collect(scan_daily_parquet(paths, hive_partitioning=True)
            .filter(pl.col('symbol').is_in(list(symbols)) & pl.col('date').cast(pl.String).is_in(past))))
           if paths and symbols else pl.DataFrame(schema=DAILY_STORAGE_SCHEMA))
    raw = raw.filter(pl.col('quote_ts').is_null())
    halts = raw.filter(_raw_halt()).select('symbol', 'date').to_dicts()
    raw = raw.filter(~_raw_halt().fill_null(False)).with_columns(
        pl.col('close').alias('raw_close'), pl.col('high').alias('raw_high'), pl.col('low').alias('raw_low'),
        (pl.col('close').is_finite() & (pl.col('close') > 0)).alias('formal'))
    factors = _factor_events(data_dir, as_of, symbols)
    prices = (_apply_adj_factor(raw, factors) if factors is not None else
              raw.with_columns(pl.lit(None, dtype=pl.Float64).alias('close')))
    if include_constraints and not prices.is_empty():
        prices = _constraint_history(data_dir, prices, symbols, past)
    ctx = {'adjust_method': 'qfq', 'adjust_rule_version': 'raw-times-event-cumfactor-over-base-v1',
           'adjust_base_date': as_of.isoformat(), 'adjustment_source': 'adj_factor' if factors is not None else None,
           'calendar_version': {k: calendar.get(k) for k in ('start', 'end', 'updated_at')},
           'suspension_source': ['formal_daily_history', 'daily_trading_status']}
    proofs = [r for r in _status_suspensions(data_dir, past, prices) if r['symbol'] in symbols]
    provenance = {'calendar': calendar, 'evaluation_context': ctx, 'suspensions': [*halts, *proofs]}
    if include_multipliers:
        # 独立于 K 日是否仍有成交记录，从事件累计因子求 F(t)/F(as_of)。
        multipliers = {}
        if factors is not None and past and symbols:
            units = pl.DataFrame({'symbol': sorted(symbols)}).join(
                pl.DataFrame({'date': [date.fromisoformat(d) for d in past]}), how='cross'
            ).with_columns(pl.lit(1.0).alias('close'))
            for symbol, frame in _apply_adj_factor(units, factors).partition_by('symbol', as_dict=True).items():
                multipliers[symbol[0]] = dict(frame.select(pl.col('date').cast(pl.String), 'close').iter_rows())
        provenance['evaluation_multipliers'] = multipliers
    return SimpleNamespace(trade_date=as_of.isoformat(), cutoff=cn_now().isoformat(), calendar=calendar['days'],
                           prices=prices, provenance=provenance)
