"""手动选股：逐策略任务、正式盘后版本和个人结果复用。"""
from __future__ import annotations

import copy
import math
import time
from dataclasses import asdict
from datetime import date

from app.market_time import cn_now
from app.services import review_publication, screener_snapshots, strategy_run_queue
from app.services.path_locks import path_lock
from app.services.screener import ScreenerService
from app.strategy import config

NAMESPACE = 'manual-screener'


def _key(asset: str, timeframe: str, sid: str) -> tuple:
    # 参数或数据变更也不叠加在途任务；旧任务退出后，下次点击再更新。
    return NAMESPACE, asset, timeframe, sid


def _spec(engine, root, sid: str) -> dict:
    definition = engine.get(sid)
    override = config.load_override(root, sid)
    source = getattr(definition, 'file_path', None)
    stat = source.stat() if source and source.exists() else None
    spec = {'meta': definition.meta, 'basic_filter': definition.basic_filter,
            'source': [stat.st_size, stat.st_mtime_ns] if stat else None,
            'override': override}
    composite = getattr(definition, 'composite', None)
    if composite:
        children = override.get('children') or [asdict(child) for child in composite.children]
        spec['children_weights'] = children
        spec['children'] = {child['strategy_id']: _spec(engine, root, child['strategy_id']) for child in children}
    return copy.deepcopy(spec)


def _frozen_engine(engine, spec: dict, sid: str):
    # 等待资源时用户可编辑参数；本次计算只使用提交时的定义和子策略参数。
    frozen = copy.copy(engine)
    frozen._strategies = dict(engine._strategies)
    overrides = {sid: spec['override'], **{cid: child['override'] for cid, child in spec.get('children', {}).items()}}
    frozen._override_loader = lambda cid: copy.deepcopy(overrides.get(cid, {}))
    return frozen


def results(root, asset: str, through: date | None = None, *, summary_only: bool = False) -> dict:
    records = screener_snapshots.read(root, summary_only=summary_only)
    merged = {}
    for namespace, entries in records.items():
        if not namespace.startswith(f'{asset}:'):
            continue
        timeframe = namespace.split(':')[1]
        for sid, result in entries.items():
            if (result.get('as_of') and (through is None or result['as_of'] <= through.isoformat())
                    and (sid not in merged or result['as_of'] >= merged[sid]['as_of'])):
                merged[sid] = {**result, 'timeframe': timeframe}
    return merged


def summary(root, asset: str, through: date | None = None) -> dict:
    saved = results(root, asset, through, summary_only=True)
    tasks = {}
    for status in sorted(strategy_run_queue.MANAGER.list_statuses(NAMESPACE), key=lambda s: (s['started_at_ms'], not s['done'])):
        meta = status['metadata']
        if meta['asset_type'] == asset:
            tasks[meta['strategy_id']] = status
    for item in saved.values():
        if isinstance(item.get('verification'), dict):
            item['verification'] = {k: v for k, v in item['verification'].items() if k != 'candidates'}
    return {
        'as_of': max((item['as_of'] for item in saved.values()), default=None),
        'results': {sid: {k: v for k, v in item.items() if k in ('as_of', 'total', 'computed_at', 'timeframe', 'invalidated', 'verification')}
                    for sid, item in saved.items()},
        'tasks': tasks, 'today_ever_counts': {}, 'updated_at': max(((item.get('computed_at') or 0) for item in saved.values()), default=0),
    }


def _safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe(v) for v in value]
    if isinstance(value, date):
        return value.isoformat()
    return value


def _minute_files(root, day: date, asset: str) -> list[list]:
    from app.tickflow.sector_review_reader import SectorReviewReader
    table = 'kline_minute' if asset == 'stock' else 'kline_etf_minute'
    return [[str(p.relative_to(root)), p.stat().st_size, p.stat().st_mtime_ns]
            for p in SectorReviewReader(root).files(table, start=day, end=day)]


def submit(repo, engine, sid: str, asset: str, through: date | None = None) -> dict:
    definition = engine.get(sid)
    if definition.meta.get('research_only') or asset not in definition.meta.get('asset_types', ['stock']):
        raise ValueError('该策略不支持当前资产类型')
    timeframe = '1m' if '1m' in definition.meta.get('timeframes', ['1d']) else '1d'
    root = repo.store.data_dir
    key = _key(asset, timeframe, sid)
    # 检查结果、提交与发布共用个人文件锁，避免完成瞬间的重复提交竞态。
    with path_lock(screener_snapshots.path(root)):
        existing = strategy_run_queue.MANAGER.active(key)
        if existing:
            return {'task': existing, 'notice': None}
        now = cn_now()
        if now.hour < 16:
            return {'task': None, 'notice': '每天 16:00 后可手动更新，当前展示上次结果。'}
        target = min(through or now.date(), now.date())
        publication = review_publication.latest_strategy_publication(root, target, asset)
        if publication is None:
            return {'task': None, 'notice': '正式盘后数据尚未就绪，当前保留上次结果，请稍后点击更新。'}
        day = date.fromisoformat(publication['as_of'])
        spec = _spec(engine, root, sid)
        version = {'publication': publication, 'strategy': spec}
        if timeframe == '1m':
            version['minute_files'] = _minute_files(root, day, asset)
            if not version['minute_files']:
                return {'task': None, 'notice': '该交易日分钟数据尚未就绪，当前保留上次结果。'}
        records = screener_snapshots.read(root)
        namespace = f'{asset}:{timeframe}:{day}'
        previous = records.get(namespace, {}).get(sid)
        if previous and not previous.get('invalidated') and previous.get('input_version') == version:
            return {'task': None, 'notice': None}
        frozen = _frozen_engine(engine, spec, sid)

        def job(handle):
            svc = ScreenerService(repo, asset_type=asset)
            # 发布可能在排队期间更新；拒绝以新行情写出旧版本的结果。
            if review_publication.latest_strategy_publication(root, day, asset) != publication:
                raise ValueError('正式盘后数据已变化，请重新点击策略更新')
            params = {sid: spec['override'].get('params', {})}
            overrides = {sid: spec['override']}
            context = svc.build_strategy_context(frozen, day, [sid], timeframe=timeframe,
                                                 params_map=params, overrides_map=overrides, prepare_financials=True)
            handle.checkpoint()
            if timeframe == '1m':
                import polars as pl
                if context.history.is_empty() or context.history.filter(pl.col('datetime').dt.date() != day).height:
                    raise ValueError('分钟数据日期与所选交易日不一致，请更新该日分钟数据')
            result = frozen.run_all(context, params_map=params, overrides_map=overrides,
                                    strategy_ids=[sid], parallel=False)[sid]
            handle.checkpoint()
            payload = {'as_of': day.isoformat(), 'total': result.total, 'rows': _safe(result.rows),
                       'computed_at': int(time.time() * 1000), 'input_version': version}
            with path_lock(screener_snapshots.path(root)):
                if _spec(engine, root, sid) != spec or review_publication.latest_strategy_publication(root, day, asset) != publication:
                    raise ValueError('参数、权重或正式盘后数据已变化，请重新点击策略更新')
                if timeframe == '1m' and _minute_files(root, day, asset) != version['minute_files']:
                    raise ValueError('分钟数据已变化，请重新点击策略更新')
                handle.checkpoint()
                latest = screener_snapshots.read(root)
                # 普通重算不重新核验财务；在发布锁内保留最新的同日核验记录（含旧缓存迁移）。
                previous_result = (latest.get(namespace, {}).get(sid)
                                   or latest.get(f'{asset}:{timeframe}', {}).get(sid, {}))
                if previous_result.get('as_of') == payload['as_of'] and 'verification' in previous_result:
                    payload['verification'] = previous_result['verification']
                latest.setdefault(namespace, {})[sid] = payload
                screener_snapshots.write(root, latest)
                handle.complete(sid, {k: v for k, v in payload.items() if k not in ('rows', 'input_version', 'verification')})

        handle = strategy_run_queue.MANAGER.get_or_submit(key, [sid], job, metadata={
            'namespace': NAMESPACE, 'asset_type': asset, 'timeframe': timeframe,
            'strategy_id': sid, 'as_of': day.isoformat(),
        })
        return {'task': handle.snapshot(), 'notice': None}
