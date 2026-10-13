"""采集进程发布快照，网页进程通过单条 SSE 连接安装本机结果。"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import queue
import threading
import time
import zipfile
from datetime import date
from pathlib import Path

import httpx
import polars as pl
from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from app.services.events import EventBus, bus
from app.services.fs_utils import atomic_write_text
from app.services.quote_service import QuoteSubscriber
from app.tickflow.capabilities import Cap, CapabilityLimits, CapabilitySet

logger = logging.getLogger(__name__)
runtime_bus = EventBus()
_publish_lock = threading.Lock()
_FRAME_FIELDS = (
    '_enriched_cache', '_etf_enriched_cache', '_index_enriched_cache',
    '_live_agg_cache', '_etf_live_agg_cache', '_instruments_cache',
    '_index_instruments_cache', '_etf_instruments_cache',
)
_DATE_FIELDS = (
    '_enriched_cache_date', '_etf_enriched_cache_date', '_index_enriched_cache_date',
    '_live_agg_cache_date', '_etf_live_agg_cache_date',
)


def _directory(data_dir: Path) -> Path:
    return Path(data_dir) / 'runtime'


def publish_quotes(quote_service) -> None:
    """原子保存一份有 schema 的行情快照；不会重新计算指标。"""
    repo = quote_service._repo
    if repo is None:
        return
    target = _directory(repo.store.data_dir)
    target.mkdir(parents=True, exist_ok=True)
    with _publish_lock:
        app_state = getattr(quote_service, '_app_state', None)
        capset = getattr(app_state, 'capabilities', CapabilitySet())
        metadata = {
            'status': quote_service.status(),
            'capabilities': capset.to_dict(),
            'dates': {key: str(value) if (value := getattr(repo, key, None)) else None
                      for key in _DATE_FIELDS},
        }
        depth = getattr(app_state, 'depth_service', None)
        if depth is not None:
            with depth._lock:
                metadata['depth'] = {'cache': dict(depth._sealed_cache), 'ready': depth._sealed_ready,
                                     'date': str(depth._sealed_date) if depth._sealed_date else None,
                                     'fetched_at': depth._sealed_fetched_at}
        frames = {key: value if (value := getattr(repo, key, None)) is not None else pl.DataFrame()
                  for key in _FRAME_FIELDS}
        frames['indices'] = quote_service.get_index_quotes()
        temporary = target / 'quotes.zip.tmp'
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('metadata.json', json.dumps(metadata))
            for key, frame in frames.items():
                output = io.BytesIO()
                frame.write_ipc(output)
                archive.writestr(key + '.arrow', output.getvalue())
        os.replace(temporary, target / 'quotes.zip')


def publish_data_update(data_dir: Path, datasets: list[str], *, private: bool = False) -> None:
    """保存更新通知以便重连补读；实时事件使用现有有界总线。"""
    target = _directory(data_dir)
    target.mkdir(parents=True, exist_ok=True)
    payload = {'datasets': sorted(set(datasets)), 'updated_at': time.time_ns()}
    if private:
        from app.services.user_context import current_user
        payload['owner_id'] = current_user().id
    else:
        atomic_write_text(target / 'data_update.json', json.dumps(payload))
    runtime_bus.publish('data_updated', payload, '*')


class QuoteMirror:
    """仅查询和广播的网页行情接口，不包含供应商或监控执行能力。"""

    def __init__(self, repo) -> None:
        self._repo = repo
        self._status = {'enabled': False, 'running': False, 'symbol_count': 0,
                        'index_symbol_count': 0, 'last_fetch_ms': None,
                        'is_trading_hours': False, 'interval_s': 30}
        self._indices = pl.DataFrame()
        self._subscribers: set[QuoteSubscriber] = set()
        self._lock = threading.Lock()
        self._symbol_count = 0
        self._interval = 30

    def status(self) -> dict:
        result = dict(self._status)
        stamp = result.get('last_fetch_ms')
        result['quote_age_ms'] = max(0, time.time() * 1000 - stamp) if stamp else None
        return result

    def get_index_quotes(self, symbols=None) -> pl.DataFrame:
        frame = self._indices
        return frame.filter(pl.col('symbol').is_in(symbols)) if symbols and not frame.is_empty() else frame

    def get_enriched_today(self) -> tuple[pl.DataFrame, date | None]:
        return self._repo.get_enriched_latest()

    def get_quotes_compat(self) -> pl.DataFrame:
        from app.services.quote_service import QuoteService
        return QuoteService.get_quotes_compat(self)

    def subscribe(self) -> QuoteSubscriber:
        subscriber = QuoteSubscriber()
        from app.services.user_context import current_user
        subscriber.owner_id = current_user().id
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def dispatch(self, event: str, payload: dict) -> None:
        with self._lock:
            subscribers = tuple(self._subscribers)
        for subscriber in subscribers:
            if (event in {'strategy_alert', 'review_progress', 'strategy_results_updated'}
                    and payload.get('owner_id') != getattr(subscriber, 'owner_id', None)):
                continue
            if event == 'quotes_updated':
                subscriber.notify_quote()
            elif event == 'strategy_results_updated':
                subscriber.notify_strategy_results()
            elif event == 'depth_updated':
                subscriber.notify_depth()
            elif event == 'strategy_alert':
                subscriber.push_alerts(payload.get('alerts', []))
            elif event == 'review_progress':
                subscriber.push_review(json.dumps(payload, ensure_ascii=False))
        if event == 'data_updated':
            runtime_bus.publish(event, payload, '*')

    def load_snapshot(self, state) -> bool:
        path = _directory(self._repo.store.data_dir) / 'quotes.zip'
        if not path.exists():
            return False
        with zipfile.ZipFile(path) as archive:
            metadata = json.loads(archive.read('metadata.json'))
            frames = {name[:-6]: pl.read_ipc(io.BytesIO(archive.read(name)))
                      for name in archive.namelist() if name.endswith('.arrow')}
        self._indices = frames.pop('indices', pl.DataFrame())
        dates_changed = any(
            str(getattr(self._repo, key, None)) != str(value)
            for key, value in metadata['dates'].items()
        )
        if dates_changed:
            with self._repo._lock:
                self._repo.store._register_views()
        self._repo.install_shared_snapshot(frames, metadata['dates'])
        depth = getattr(state, 'depth_service', None)
        saved_depth = metadata.get('depth')
        if depth is not None and saved_depth:
            with depth._lock:
                depth._sealed_cache = saved_depth['cache']
                depth._sealed_ready = saved_depth['ready']
                depth._sealed_date = date.fromisoformat(saved_depth['date']) if saved_depth['date'] else None
                age = max(0, time.time() - saved_depth['fetched_at'])
                depth._sealed_fetched_ts = time.perf_counter() - age
        self._status = metadata['status']
        self._symbol_count = self._status.get('symbol_count', 0)
        self._interval = self._status.get('interval_s', 30)
        state.capabilities = CapabilitySet({Cap(key): CapabilityLimits(**value)
                                           for key, value in metadata['capabilities'].items()})
        state.indicators_ready = True
        return True


def refresh_web_data(state, datasets: list[str]) -> None:
    """刷新文件视图和相关缓存；行情热路径只安装已经计算好的最新日快照。"""
    from app.api.data import invalidate_data_cache, invalidate_storage_cache
    from app.api.ext_data import invalidate_dimension_intraday_cache
    from app.api.overview import invalidate_overview_cache
    from app.api.regime import invalidate_regime_cache
    from app.services.screener import ScreenerService

    repo = state.repo
    with repo._lock:
        repo.store._register_views()
    if any(name in {'daily', 'enriched', 'pipeline', 'all'} for name in datasets):
        repo._enriched_history_cache = None
        repo._enriched_history_start = None
        repo._enriched_history_generation = None
        ScreenerService.clear_history_cache()
    invalidate_data_cache()
    invalidate_storage_cache()
    invalidate_overview_cache()
    invalidate_regime_cache()
    if any(name in {'ext_data', 'minute', 'daily', 'pipeline', 'all'} for name in datasets):
        invalidate_dimension_intraday_cache()
    state.quote_service.load_snapshot(state)


async def bridge_events(app, collector_url: str) -> None:
    """每个网页进程唯一连接；断线后重读共享快照，再恢复事件广播。"""
    from app.services.internal_auth import credential
    state = app.state
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=None), trust_env=False) as client:
        while True:
            try:
                async with client.stream('GET', collector_url + '/internal/runtime/events', headers={'X-Internal-Key': credential(state.repo.store.data_dir)}) as response:
                    response.raise_for_status()
                    state.collector_connected = True
                    event = ''
                    data: list[str] = []
                    async for line in response.aiter_lines():
                        if line.startswith('event:'):
                            event = line[6:].strip()
                        elif line.startswith('data:'):
                            data.append(line[5:].strip())
                        elif not line and data:
                            payload = json.loads('\n'.join(data))
                            state.collector_last_event_at = time.time()
                            data.clear()
                            if event == 'hello':
                                await asyncio.to_thread(refresh_web_data, state, ['all'])
                                state.quote_service.dispatch('data_updated', {'datasets': ['all']})
                            elif event == 'quotes_updated':
                                await asyncio.to_thread(state.quote_service.load_snapshot, state)
                                from app.api.overview import invalidate_overview_cache
                                invalidate_overview_cache()
                                state.quote_service.dispatch(event, payload)
                            elif event == 'data_updated':
                                await asyncio.to_thread(refresh_web_data, state, payload['datasets'])
                                state.quote_service.dispatch(event, payload)
                            elif event == 'runtime_status':
                                state.quote_service._status = payload['quotes']
                                state.capabilities = CapabilitySet({
                                    Cap(key): CapabilityLimits(**value)
                                    for key, value in payload['capabilities'].items()
                                })
                            elif event == 'public_event':
                                bus.publish(payload['type'], payload['data'], payload['scope'])
                            else:
                                if event == 'depth_updated':
                                    await asyncio.to_thread(state.quote_service.load_snapshot, state)
                                state.quote_service.dispatch(event, payload)
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, OSError, ValueError, zipfile.BadZipFile):
                logger.warning('采集事件连接中断，等待重新连接')
            state.collector_connected = False
            await asyncio.sleep(3)


router = APIRouter()


@router.get('/internal/runtime/events')
async def internal_events(request: Request):
    import secrets

    from app.services.internal_auth import credential
    if not secrets.compare_digest(request.headers.get('X-Internal-Key', ''), credential(request.app.state.repo.store.data_dir)):
        raise HTTPException(status_code=403, detail='内部服务凭据无效')
    qs = request.app.state.quote_service
    subscriber = qs.subscribe()
    updates = runtime_bus.subscribe()
    public_events = bus.subscribe()

    async def stream():
        try:
            await asyncio.to_thread(publish_quotes, qs)
            yield {'event': 'hello', 'data': '{}'}
            next_status = 0.0
            while True:
                snapshot = subscriber.pop()
                events = []
                if time.monotonic() >= next_status:
                    events.append(('runtime_status', {
                        'quotes': qs.status(),
                        'capabilities': request.app.state.capabilities.to_dict(),
                    }))
                    next_status = time.monotonic() + 5
                for flag, event in [('quote_updated', 'quotes_updated'),
                                    ('strategy_results_updated', 'strategy_results_updated'),
                                    ('depth_updated', 'depth_updated')]:
                    if snapshot[flag]:
                        events.append((event, {}))
                if snapshot['alerts']:
                    events.append(('strategy_alert', {'alerts': snapshot['alerts']}))
                for review in snapshot['reviews']:
                    events.append(('review_progress', json.loads(review)))
                while not updates.empty():
                    with contextlib.suppress(queue.Empty):
                        update = updates.get_nowait()
                        events.append((update['type'], update['data']))
                while not public_events.empty():
                    with contextlib.suppress(queue.Empty):
                        events.append(('public_event', public_events.get_nowait()))
                if snapshot['depth_updated']:
                    await asyncio.to_thread(publish_quotes, qs)
                for event, payload in events:
                    yield {'event': event, 'data': json.dumps(payload, ensure_ascii=False)}
                await asyncio.sleep(0.25)
        finally:
            qs.unsubscribe(subscriber)
            runtime_bus.unsubscribe(updates)
            bus.unsubscribe(public_events)
    return EventSourceResponse(stream())
