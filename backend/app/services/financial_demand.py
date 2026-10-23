"""显式任务的财务准备；collector 取数、共享单飞，计算 worker 只读。

同源同股同表同范围共享在途结果；页面默认两年并逐年向前查询，计算任务保留
provider 历史窗口。版本可用时间仍由 financial_versions 决定，绝不倒填新版本。
"""
from __future__ import annotations

import calendar
import json
import re
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import Future
from datetime import date, timedelta
from pathlib import Path

import polars as pl

from app.data_providers.base import ProviderFinancialDataUnavailableError
from app.market_time import cn_today
from app.services import financial_sync, preferences
from app.services.financial_snapshot import read_table
from app.services.fs_utils import atomic_write_text
from app.tickflow.capabilities import Cap, CapabilitySet

_guard = threading.Lock()
_flights: dict[tuple, Future] = {}
_runtimes: dict[Path, tuple] = {}
_SYMBOL = re.compile(r"[0-9]{6}\.(SH|SZ|BJ)\Z")
FACTOR_TABLES = {
    "gross_margin_latest": "income", "net_margin_latest": "income",
    "revenue_yoy_latest": "income", "net_income_yoy_latest": "income",
    "roe_latest": "metrics", "pb_latest": "metrics", "debt_ratio_latest": "balance_sheet",
}


def register(repo, capset: CapabilitySet) -> None:
    _runtimes[repo.store.data_dir.resolve()] = (repo, capset)


def unregister(data_dir: Path) -> None:
    _runtimes.pop(data_dir.resolve(), None)


def validate_symbol(symbol: str) -> str:
    symbol = symbol.strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("请提供有效的股票代码，例如 600000.SH")
    return symbol


def supported_tables(capset: CapabilitySet) -> list[str]:
    source = preferences.get_financial_provider()
    if source == "tickflow":
        return list(financial_sync.FINANCIAL_TABLES) if capset.has(Cap.FINANCIAL) else []
    from app.data_providers import custom
    if not financial_sync._financial_is_custom():
        return []
    provider = custom.get_provider(source)
    return list(getattr(provider, "financial_tables", financial_sync.FINANCIAL_TABLES))


def fetch_stock(data_dir: Path, capset: CapabilitySet, symbol: str, table: str, *,
                before_write: Callable[[], None] | None = None, cancel_event=None,
                start_date: date | None = None, end_date: date | None = None,
                allow_empty: bool = False, allow_partial: bool = False) -> dict:
    """获取一股一表的指定范围或源支持历史；复用成功结果，失败可重试。"""
    symbol = validate_symbol(symbol)
    if table not in financial_sync.FINANCIAL_TABLES:
        raise ValueError("不支持的财务表")
    source = preferences.get_financial_provider()
    if table not in supported_tables(capset):
        raise ValueError("当前财务数据源不支持此项数据，请在设置中配置可用数据源")
    key = (data_dir.resolve(), source, table, symbol, start_date, end_date, allow_partial)
    with _guard:
        future = _flights.get(key)
        leader = future is None
        if leader:
            future = Future()
            _flights[key] = future
    if not leader:
        return {**future.result(), "reused": True}
    try:
        result = _fetch_and_merge(data_dir, capset, source, symbol, table, before_write, cancel_event,
                                  start_date, end_date, allow_empty, allow_partial)
        future.set_result(result)
        return result
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _guard:
            _flights.pop(key, None)


def _years_before(day: date, years: int) -> date:
    year = day.year - years
    return day.replace(year=year, day=min(day.day, calendar.monthrange(year, day.month)[1]))


def _receipt(data_dir, table, symbol, source) -> dict:
    path = data_dir / "financials" / "demand" / table / f"{symbol}.json"
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return saved if saved.get("source") == source else {}


def history_start(data_dir: Path, table: str, symbol: str, frame: pl.DataFrame | None = None) -> str | None:
    """浏览只读：返回当前源已检查的最早边界，兼容已有更长的本地历史。"""
    symbol = validate_symbol(symbol)
    source = preferences.get_financial_provider()
    with financial_sync.financial_write_lock:
        saved = _receipt(data_dir, table, symbol, source)
        if frame is None:
            path = data_dir / "financials" / table / "part.parquet"
            frame = read_table(path)
        rows = _source_rows(frame, source, symbol)
        starts = [saved["history_start"]] if saved.get("history_start") else []
        if not rows.is_empty() and "period_end" in rows.columns:
            oldest = rows["period_end"].cast(pl.String).drop_nulls().min()
            if oldest:
                starts.append(oldest[:10])
        if not starts:
            return None
        # 更早年度部分取数时保留补采入口，不能因已保存的旧记录跳过缺失季度。
        pending_before = [before for before in saved.get("pending_periods", {}).values() if before]
        return max(min(starts), max(pending_before)) if pending_before else min(starts)


def missing_periods(data_dir: Path, table: str, symbol: str) -> list[str]:
    """只读当前源的待补报告期，刷新页面后仍明确展示数据缺口。"""
    symbol = validate_symbol(symbol)
    with financial_sync.financial_write_lock:
        saved = _receipt(data_dir, table, symbol, preferences.get_financial_provider())
        return sorted(saved.get("pending_periods", {}), reverse=True)


def fetch_stock_history(data_dir: Path, capset: CapabilitySet, symbol: str, table: str, *,
                        before: date | None = None, before_write=None, cancel_event=None) -> dict:
    """财务页首次取近两年，按已展示游标仅向前补一年；失败不移动游标。"""
    symbol = validate_symbol(symbol)
    if table not in financial_sync.FINANCIAL_TABLES:
        raise ValueError("不支持的财务表")
    today = cn_today()
    if before is None:
        start, end = _years_before(today, 2), today
    else:
        cursor = history_start(data_dir, table, symbol)
        if not cursor or before.isoformat() < cursor or before > _years_before(today, 2) or before.year <= 1:
            raise ValueError("历史范围已变化，请刷新财务表后重试")
        start, end = _years_before(before, 1), before - timedelta(days=1)
    return fetch_stock(data_dir, capset, symbol, table, before_write=before_write,
                       cancel_event=cancel_event, start_date=start, end_date=end,
                       allow_empty=before is not None, allow_partial=True)


def _fetch_and_merge(data_dir, capset, source, symbol, table, before_write, cancel_event,
                     start_date, end_date, allow_empty, allow_partial):

    # 源名称不作为路径组成部分，避免可配置源名越过数据目录。
    receipt = data_dir / "financials" / "demand" / table / f"{symbol}.json"
    path = data_dir / "financials" / table / "part.parquet"
    today = cn_today().isoformat()
    window = ({"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
              if start_date is not None and end_date is not None else {})
    with financial_sync.financial_write_lock:
        existing = read_table(path)
        financial_sync.read_versions(data_dir, table)
        rows = _source_rows(existing, source, symbol)
        saved = _receipt(data_dir, table, symbol, source)
        if not rows.is_empty():
            if window:
                reusable = (saved.get("history_start", "9999") <= window["start_date"]
                            and saved.get("history_end", "") >= window["end_date"]
                            and (allow_empty or saved.get("recent_checked_on") == today))
            else:
                reusable = saved.get("checked_on") == today
            pending = saved.get("pending_periods", {})
            reusable = reusable and not any(
                not window or window["start_date"] <= period <= window["end_date"] for period in pending
            )
            if reusable:
                if window and "period_end" in rows.columns:
                    rows = rows.filter(pl.col("period_end").cast(pl.String).str.slice(0, 10)
                                       .is_between(pl.lit(window["start_date"]), pl.lit(window["end_date"])))
                return {"symbol": symbol, "table": table, "rows": len(rows), "reused": True,
                        "complete": True, "missing_periods": [], **window}
    _check_cancel(cancel_event)
    range_args = {"start_date": start_date, "end_date": end_date} if window else {}
    missing = []
    try:
        fetched = financial_sync._fetch_table(table, [symbol], capset, latest_only=False,
                                             cancel_event=cancel_event, **range_args)
    except ProviderFinancialDataUnavailableError as exc:
        if not allow_partial or not exc.missing_periods.get(symbol):
            raise
        missing = sorted(set(exc.missing_periods[symbol]), reverse=True)
        for period in missing:
            if (date.fromisoformat(period).isoformat() != period
                    or (window and not window["start_date"] <= period <= window["end_date"])):
                raise ValueError("财务缺失报告期无效或超出请求范围，未保存") from exc
        fetched = exc.data
    _check_cancel(cancel_event)
    if preferences.get_financial_provider() != source:
        raise ValueError("财务数据源已改变，请重新获取")
    if fetched.is_empty() and not allow_empty and not missing:
        raise ValueError("数据源未返回该股票的可用财务数据")
    if not fetched.is_empty() and not {"symbol", "period_end", "source"} <= set(fetched.columns):
        raise ValueError("财务数据缺少股票代码、报告期或来源，未保存")
    if not fetched.is_empty() and (set(fetched["symbol"].to_list()) != {symbol}
                                  or set(fetched["source"].to_list()) != {source}):
        raise ValueError("财务数据超出请求的股票或数据源范围，未保存")
    if window and not fetched.is_empty():
        if fetched.select(pl.col("period_end").cast(pl.String).str.slice(0, 10)
                          .str.to_date("%Y-%m-%d", strict=False).is_null().any()).item():
            raise ValueError("财务数据报告期无效，未保存")
        fetched = fetched.filter(pl.col("period_end").cast(pl.String).str.slice(0, 10)
                                 .is_between(pl.lit(window["start_date"]), pl.lit(window["end_date"])))
        if fetched.is_empty() and not allow_empty and not missing:
            raise ValueError("数据源未返回该范围的可用财务数据")
    # 网络请求不占写锁；提交时重新读取，防不同股票同时获取丢失彼此的数据。
    with financial_sync.financial_write_lock:
        existing = read_table(path)
        if preferences.get_financial_provider() != source:
            raise ValueError("财务数据源已改变，请重新获取")
        if before_write:
            before_write()
        _check_cancel(cancel_event)
        if not fetched.is_empty():
            captured = financial_sync._record_table(table, fetched, data_dir)
            merged = financial_sync._merge_report_history(existing, captured)
            if before_write:
                before_write()
            _check_cancel(cancel_event)
            financial_sync._write_table(table, merged, data_dir, publish=False)
        saved = _receipt(data_dir, table, symbol, source)
        saved["source"] = source
        pending = saved.get("pending_periods", {})
        if window:
            pending = {period: before for period, before in pending.items()
                       if not window["start_date"] <= period <= window["end_date"]}
        else:
            # 不限日期的计算准备仍受源历史深度约束，只清除本轮实际返回的报告期。
            returned = set(fetched["period_end"].cast(pl.String).str.slice(0, 10).to_list())
            pending = {period: before for period, before in pending.items() if period not in returned}
        retry_before = (end_date + timedelta(days=1)).isoformat() if allow_empty and end_date else None
        pending.update(dict.fromkeys(missing, retry_before))
        saved["pending_periods"] = pending
        if window:
            saved["history_start"] = min(saved.get("history_start", window["start_date"]), window["start_date"])
            saved["history_end"] = max(saved.get("history_end", window["end_date"]), window["end_date"])
            if not allow_empty:
                if missing:
                    saved.pop("recent_checked_on", None)
                else:
                    saved["recent_checked_on"] = today
        else:
            saved["checked_on"] = today
        receipt.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(receipt, json.dumps(saved))
    from app.api.data import invalidate_data_cache
    from app.services.shared_runtime import publish_data_update
    invalidate_data_cache("financials", storage=True)
    publish_data_update(data_dir, ["financials"])
    return {"symbol": symbol, "table": table, "rows": len(fetched), "reused": False,
            "complete": not missing, "missing_periods": missing, **window}


def _check_cancel(event):
    if event is not None and event.is_set():
        from app.services.pipeline_jobs import JobCancelledError
        raise JobCancelledError("financial_demand")


def _source_rows(frame, source, symbol):
    if frame.is_empty() or not {"symbol", "source"} <= set(frame.columns):
        return pl.DataFrame()
    return frame.filter((pl.col("symbol") == symbol) & (pl.col("source") == source))


def financial_factors(names: Iterable[str]) -> set[str]:
    from app.factors.registry import get_factor
    pending, visited = list(names), set()
    while pending:
        name = pending.pop()
        if name in visited:
            continue
        visited.add(name)
        spec = get_factor(name)
        if spec:
            pending.extend(spec.dependencies)
    return visited & FACTOR_TABLES.keys()


def prepare_factors(data_dir: Path, frame: pl.DataFrame, names: Iterable[str], *,
                    cancel_event=None, before_write=None) -> None:
    tables = {FACTOR_TABLES[name] for name in financial_factors(names)}
    prepare_tables(data_dir, frame, tables, cancel_event=cancel_event, before_write=before_write)


def prepare_factor_frame(data_dir: Path | None, frame: pl.DataFrame, names: Iterable[str]) -> pl.DataFrame:
    """显式因子计算共用：准备财务依赖，再按版本可用日附加输入。"""
    financial_names = financial_factors(names)
    if not financial_names or frame.is_empty():
        return frame
    if data_dir is not None:
        prepare_factors(data_dir, frame, financial_names)
    from app.backtest.fundamentals import attach_fundamental_factors, load_fundamental_snapshot
    return attach_fundamental_factors(frame, load_fundamental_snapshot(data_dir), financial_names)


def prepare_tables(data_dir: Path, frame: pl.DataFrame, tables: Iterable[str], *,
                   cancel_event=None, before_write=None) -> None:
    tables = sorted(set(tables))
    if not tables or frame.is_empty():
        return
    runtime = _runtimes.get(data_dir.resolve())
    if runtime is None:
        # spawned worker 只消费 collector 预先准备的数据；不自行加载供应商客户端。
        return
    _repo, capset = runtime
    if before_write is None:
        from app.services.auth import require_current_session
        from app.services.user_context import current_user
        owner = current_user(required=False)
        if owner and owner.session_token:
            before_write = require_current_session
    supported = supported_tables(capset)
    failures = []
    symbols = sorted(frame["symbol"].unique().to_list())
    for table in tables:
        # Whole-table readiness is evaluated once per batch, not once per stock.
        # Network misses still go through the shared flights and re-merge under the write lock.
        ready = set()
        if table in supported:
            source = preferences.get_financial_provider()
            with financial_sync.financial_write_lock:
                existing = read_table(data_dir / "financials" / table / "part.parquet")
                financial_sync.read_versions(data_dir, table)
            present = set(existing.filter(pl.col("source") == source)["symbol"].unique().to_list()) \
                if {"symbol", "source"} <= set(existing.columns) else set()
            today = cn_today().isoformat()
            for symbol in present.intersection(symbols):
                saved = _receipt(data_dir, table, symbol, source)
                if saved.get("checked_on") == today and not saved.get("pending_periods"):
                    ready.add(symbol)
        for symbol in symbols:
            _check_cancel(cancel_event)
            if symbol in ready:
                continue
            try:
                if table not in supported:
                    saved = financial_sync.read_versions(data_dir, table, symbol=symbol)
                    if not _source_rows(saved, preferences.get_financial_provider(), symbol).is_empty():
                        continue
                fetch_stock(data_dir, capset, symbol, table,
                            before_write=before_write, cancel_event=cancel_event)
            except ValueError as exc:
                failures.append(f"{symbol}: {exc}")
    if failures:
        raise ValueError(f"财务数据准备未完成（{len(failures)}项）：" + "；".join(failures[:3]))


def strategy_requirements(engine, strategy_ids, overrides_map=None, params_map=None) -> tuple[set[str], set[str]]:
    from app.backtest.strategy import StrategyDependencyResolver
    from app.strategy.engine import StrategyDef, _parse_composite_children
    from app.strategy.scoring import effective_scoring, scoring_dependencies
    names, events = set(), set()
    visited = set()

    def visit(sid):
        if sid in visited:
            return
        visited.add(sid)
        strategy = engine.get(sid)
        loader = getattr(engine, "_override_loader", None)
        overrides = (overrides_map or {}).get(sid)
        if overrides is None:
            overrides = (loader(sid) if loader else {}) or {}
        scoring = effective_scoring(strategy.meta.get("scoring"), overrides)
        names.update(scoring_dependencies(scoring))
        names.update(getattr(strategy, "required_features", set()))
        if isinstance(strategy, StrategyDef) and strategy.execution_backend != "composite":
            plan = StrategyDependencyResolver().resolve(
                strategy, params=engine.resolve_params(strategy, (params_map or {}).get(sid), overrides),
                basic_filter={**strategy.basic_filter, **overrides.get("basic_filter", {})},
                entry_signals=overrides.get("entry_signals", strategy.entry_signals),
                exit_signals=overrides.get("exit_signals", strategy.exit_signals), overrides=overrides,
            )
            names.update(plan.fundamental_columns)
        if sid in {"earnings_breakout_gap", "earnings_breakout_pullback"}:
            events.add(sid)
        composite = getattr(strategy, "composite", None)
        if composite:
            children = _parse_composite_children(overrides["children"]).children if overrides.get("children") else composite.children
            for child in children:
                visit(child.strategy_id)
    for sid in strategy_ids:
        visit(sid)
    return financial_factors(names), events


def prepare_strategies(repo, engine, strategy_ids, frame, as_of: date, *,
                       overrides_map=None, params_map=None, cancel_event=None, before_write=None,
                       price_snapshot=None) -> set[str]:
    if repo.store.data_dir.resolve() not in _runtimes:
        return set()
    names, events = strategy_requirements(engine, strategy_ids, overrides_map, params_map)
    prepare_factors(repo.store.data_dir, frame, names, cancel_event=cancel_event, before_write=before_write)
    if events and not frame.is_empty():
        # 两种断层策略均要求历史价格出现缺口；先做独立于财务的必要条件筛选。
        from app.indicators.earnings_breakout import pullback_price_candidates, upward_gap_expr
        from app.services.earnings_breakout import EarningsPriceSnapshot
        snapshot = price_snapshot if price_snapshot is not None else EarningsPriceSnapshot()
        prices = snapshot.capture(repo, frame, as_of)
        if not prices.is_empty():
            gaps = prices.with_columns(upward_gap_expr().alias("_gap")).filter(pl.col("_gap"))
            candidates = []
            if "earnings_breakout_gap" in events:
                candidates.append(gaps.join(frame.select("symbol", "date").unique(),
                                             on=["symbol", "date"], how="semi"))
            if "earnings_breakout_pullback" in events:
                eligible = (snapshot.prepare_pullback_candidates(frame) if price_snapshot is not None
                            else pullback_price_candidates(prices, frame))
                candidates.append(gaps.filter(pl.col("symbol").is_in(eligible["symbol"].unique().implode())))
            prepare_tables(repo.store.data_dir, pl.concat(candidates, how="diagonal_relaxed"), ["income"],
                           cancel_event=cancel_event, before_write=before_write)
    return names


def prepare_worker(task, cancel_event=None, progress_cb=None) -> None:
    """在父 collector 内、spawn 之前准备回测/优化/挖掘共同输入。"""
    runtime = _runtimes.get(Path(task["data_dir"]).resolve())
    if runtime is None:
        return
    config = task["config"]
    if task["kind"] == "mining":
        config = config["request"]
    asset_type = config.get("asset_type") or config.get("backtest_kwargs", {}).get("asset_type", "stock")
    if asset_type != "stock":
        return
    from app.backtest.engine import BacktestEngine
    from app.services.user_context import user_data_dir
    from app.services.user_runtime import strategy_directories
    from app.strategy import config as strategy_config
    from app.strategy.engine import StrategyEngine
    repo, _ = runtime
    engine = StrategyEngine(strategy_dirs=strategy_directories(repo.store.data_dir),
                            declarative_dirs=[user_data_dir(repo.store.data_dir) / "strategies" / "composite"],
                            override_loader=lambda sid: strategy_config.load_override(repo.store.data_dir, sid))
    ids = config.get("strategy_ids", []) or ([config["strategy_id"]] if config.get("strategy_id") else [])
    overrides = {sid: (strategy_config.load_override(repo.store.data_dir, sid) if task["kind"] == "mining"
                      else config.get("overrides")) or {} for sid in ids}
    params = config.get("params") or config.get("base_params") or {}
    names, events = strategy_requirements(engine, ids, overrides, {sid: params for sid in ids})
    if config.get("param_grid"):
        from app.backtest.optimizer import expand_param_grid
        for sid in ids:
            for combo in expand_param_grid(engine.get(sid).meta.get("params", []), config["param_grid"]):
                extra, extra_events = strategy_requirements(engine, [sid], overrides, {sid: {**params, **combo}})
                names.update(extra)
                events.update(extra_events)
    names |= financial_factors(config.get("factor_names", []))
    if not names and not events:
        return
    if progress_cb:
        progress_cb({"phase": "financials", "label": "准备任务所需财务数据", "done": 0, "total": 1}
                    if task["kind"] == "mining"
                    else {"day": 0, "total": 1, "date": "准备任务所需财务数据"})
    from app.services.mining_preflight import enriched_partition_dates
    days = enriched_partition_dates(repo.store.data_dir, asset_type)
    if not days:
        return
    start = date.fromisoformat(config["start"]) if config.get("start") else days[0]
    end = date.fromisoformat(config["end"]) if config.get("end") else days[-1]
    frame = BacktestEngine(repo).load_panel(config.get("symbols"), start, end, columns=["symbol", "date"])
    from app.services.auth import require_current_session
    from app.services.user_context import current_user
    owner = current_user(required=False)
    before_write = require_current_session if owner and owner.session_token else None
    prepare_factors(repo.store.data_dir, frame, names, cancel_event=cancel_event, before_write=before_write)
    prepare_strategies(repo, engine, ids, frame, end, overrides_map=overrides,
                       params_map={sid: params for sid in ids}, cancel_event=cancel_event, before_write=before_write)
