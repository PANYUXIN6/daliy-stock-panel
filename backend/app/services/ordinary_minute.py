"""普通分时后台采集：自选、启用监控标的与核心指数共用持久化结果。"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time

from app.market_time import cn_now, in_continuous_session
from app.services import kline_sync, minute_adjust, preferences, trading_day, watchlist
from app.services.index_const import CORE_INDEX_SYMBOLS
from app.strategy import monitor_rules
from app.tickflow.capabilities import Cap
from app.tickflow.rate_limits import resolve_limit

logger = logging.getLogger(__name__)
MINUTE_COLLECTION_LOCK = threading.Lock()
MINUTE_TABLES = {"stock": "kline_minute", "etf": "kline_etf_minute", "index": "kline_index_minute"}


def minute_allowed(capset) -> bool:
    """是否有分钟K权限 (TickFlow Pro+ 或 custom minute 源)。"""
    from app.tickflow.capabilities import Cap
    if capset.has(Cap.KLINE_MINUTE_BATCH):
        return True
    provider = preferences.get_minute_data_provider()
    _, fallback, error = kline_sync._resolve_minute_provider(provider)
    if error is not None:
        logger.warning("minute provider resolution failed while checking access: %s", error)
    return not fallback


def collection_pool(repo) -> dict[str, list[str]]:
    """只依据共享工作区配置选池，不接受浏览行为添加订阅。"""
    symbols = set(CORE_INDEX_SYMBOLS)
    symbols.update(row["symbol"] for row in watchlist.list_symbols())
    for rule in monitor_rules.load_all(repo.store.data_dir):
        if rule.get("enabled", True):
            symbols.update(rule.get("symbols", []))
    groups = {asset: [] for asset in MINUTE_TABLES}
    for symbol in sorted(symbols):
        groups[repo.resolve_asset_type(symbol)].append(symbol)
    return groups


def collect_minutes(repo, capset, groups: dict[str, list[str]], start: datetime, end: datetime, on_progress=None, *, should_collect=None) -> int:
    """按资产与provider路由采集并写现有分钟分区；调用方持采集锁。"""
    from app.jobs.daily_pipeline import _refresh_single_view
    from app.services.shared_runtime import publish_data_update

    raw_basis = minute_adjust.minute_basis_is_raw(repo.store.data_dir)
    limits = resolve_limit(capset, Cap.KLINE_MINUTE_BATCH, default_batch=100, default_rpm=30, default_rpm_when_unset=False)
    total = 0
    for asset, symbols in groups.items():
        if not symbols:
            continue
        if should_collect is not None and not should_collect():
            break
        def progress(cur, count, segment="", asset=asset):
            if on_progress:
                on_progress("sync_minute", int(95 * cur / max(count, 1)), f"{asset} 分钟K {cur}/{count} {segment}")
        if on_progress:
            on_progress("sync_minute", 0, f"获取 {len(symbols)} 只{asset}分钟K…")
        df = kline_sync.sync_minute_batch(
            symbols, start_time=start, end_time=end, asset_type=asset,
            batch_size=limits.batch, rpm=limits.rpm, raw_basis=raw_basis,
            on_chunk_done=progress,
        )
        if df.is_empty():
            continue
        with repo._write_lock:
            total += kline_sync._write_minute_partition(df, repo.store.data_dir / MINUTE_TABLES[asset])
        _refresh_single_view(repo, MINUTE_TABLES[asset])
    if total:
        publish_data_update(repo.store.data_dir, ["minute"])
    return total


def sync_symbol_minutes(repo, capset, symbol: str, days: int, on_progress=None) -> dict:
    end = cn_now()
    calendar = trading_day.calendar_status(repo.store.data_dir)
    sessions = sorted(date.fromisoformat(day) for day in calendar.get("days", []) if day <= end.date().isoformat())
    if not sessions:
        raise ValueError("交易日历尚未就绪，无法确定分钟历史区间")
    start = datetime.combine(sessions[-days:][0], time(9, 30), tzinfo=end.tzinfo)
    with MINUTE_COLLECTION_LOCK:
        rows = collect_minutes(repo, capset, {repo.resolve_asset_type(symbol): [symbol]}, start, end, on_progress)
    if on_progress:
        on_progress("done", 100, f"分钟K完成，写入 {rows} 行")
    return {"symbol": symbol, "rows": rows}


class OrdinaryMinuteService:
    def __init__(self, repo, *, app_state):
        self._repo = repo
        self._app_state = app_state
        self._stop = threading.Event()
        self._thread = None
        self._last_update = None
        self._last_error = None

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ordinary-minute", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def status(self):
        return {"enabled": preferences.get_ordinary_minute_enabled(),
                "running": bool(self._thread and self._thread.is_alive()),
                "interval_s": 30, "last_update": self._last_update, "error": self._last_error}

    def run_round(self):
        from app.services.pipeline_jobs import job_store

        if not preferences.get_ordinary_minute_enabled():
            return 0
        now = cn_now()
        if not in_continuous_session(now) or trading_day.is_trading_day(now, data_dir=self._repo.store.data_dir) is not True:
            return 0
        if job_store.active_id() or not minute_allowed(self._app_state.capabilities):
            return 0
        with MINUTE_COLLECTION_LOCK:
            if not preferences.get_ordinary_minute_enabled() or job_store.active_id():
                return 0
            groups = collection_pool(self._repo)
            full = getattr(self._app_state, "minute_refresh", None)
            full_status = full.status() if full is not None else {}
            if full_status.get("enabled") and full_status.get("running") and full.capability_ok():
                groups["stock"] = []
            start = datetime.combine(now.date(), time(9, 30), tzinfo=now.tzinfo)
            rows = collect_minutes(
                self._repo, self._app_state.capabilities, groups, start, now,
                should_collect=preferences.get_ordinary_minute_enabled,
            )
        self._last_update = now.isoformat()
        self._last_error = None
        return rows

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_round()
            except Exception as exc:
                self._last_error = str(exc)
                logger.exception("普通分时采集失败")
            self._stop.wait(30)
