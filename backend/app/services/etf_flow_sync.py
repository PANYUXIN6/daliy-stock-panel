"""重点ETF看板的显式采集与独立盘后补充任务，复用共享写入槽。"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, time, timedelta

import polars as pl

from app.data_providers import custom
from app.data_providers.base import ProviderRateLimitError
from app.data_providers.etf_public import EtfPublicSource
from app.market_time import CN_TZ, cn_now
from app.services import etf_flow, index_sync, ordinary_minute, preferences, trading_day
from app.services.etf_flow_universe import FOCUS_FUNDS, FOCUS_SYMBOLS
from app.services.fs_utils import atomic_write_text
from app.services.pipeline_jobs import JobCancelledError
from app.services.shared_runtime import publish_data_update
from app.tickflow.capabilities import Cap

logger = logging.getLogger(__name__)
JOB_KEY = "etf_flow"
CHECK_HOUR = 20


def _merge(old: list[dict], new: list[dict], start: str) -> list[dict]:
    rows = {(r["symbol"], r["date"]): r for r in old if r["date"] >= start}
    rows.update({(r["symbol"], r["date"]): r for r in new if r["date"] >= start})
    return [rows[key] for key in sorted(rows)]


def _daily_provider(capset):
    name = preferences.get_daily_data_provider()
    if name == "tickflow":
        if not capset.has(Cap.KLINE_DAILY_BATCH):
            raise ValueError("日线数据能力不可用，请检查数据源配置")
        from app.data_providers.registry import get_provider
        return get_provider(name)
    if not custom.provider_has_dataset(name, "daily"):
        raise ValueError("所选日线数据源不可用，请检查数据源配置")
    return custom.get_provider(name)


def sync_board(repo, capset, *, days: int = 22, full_history: bool = False, progress=None, check_cancel=None,
               source=None, daily_provider=None, nav_provider=None) -> dict:
    emit = progress or (lambda *args: None)
    check = check_cancel or (lambda: None)
    now = cn_now()
    data_dir = repo.store.data_dir
    path = etf_flow.snapshot_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    status_path = path.with_name("etf_flow_status.json")
    snapshot = etf_flow.read_snapshot(data_dir)
    work_path = path.with_name("etf_flow_work.json")
    if work_path.exists():
        snapshot = json.loads(work_path.read_text(encoding="utf-8"))
    shares = list(snapshot.get("shares", []))
    nav = list(snapshot.get("nav", []))
    prices = list(snapshot.get("prices", []))
    profiles = dict(snapshot.get("profiles", {}))
    errors: list[str] = []

    def report(percent, message):
        check()
        emit(JOB_KEY, percent, message)

    def attempt(label, action, default):
        check()
        try:
            value = action()
            check()
            return value
        except JobCancelledError:
            raise
        except ProviderRateLimitError:
            raise ValueError("数据源要求限流，本次同步已停止，请稍后重试") from None
        except Exception as exc:  # noqa: BLE001 - 单源失败保留其他输入，边界错误集中返回。
            # 对外不传供应商响应、请求地址或配置；只保留可定位的固定阶段。
            logger.warning("ETF看板 %s 失败 (%s)", label, type(exc).__name__)
            errors.append(label)
            return default

    owned = source is None
    source = source or EtfPublicSource(check)
    try:
        calendar = trading_day.calendar_status(data_dir)
        if (calendar.get("end") or "") < now.date().isoformat():
            report(1, "更新过期交易日历…")
            attempt("交易日历更新", lambda: trading_day.sync_calendar(data_dir), {})
            calendar = trading_day.calendar_status(data_dir)
            if (calendar.get("end") or "") < now.date().isoformat():
                errors.append("交易日历未覆盖今天，仅更新已确认日期")
        available = sorted(day for day in calendar.get("days", []) if day <= now.date().isoformat())
        if len(available) < 2:
            raise ValueError("本地交易日历尚未就绪，请先同步交易日历")
        if full_history and not snapshot.get("history_calendar_start"):
            report(2, "核对2024年以来的历史交易日期…")
            historical = source.trading_days(date(2023, 12, 1), date.fromisoformat(available[-1]))
            baseline = [d.isoformat() for d in historical if d < etf_flow.HISTORY_START]
            if not baseline or not any(d.year == 2024 for d in historical):
                raise ValueError("历史交易日历未覆盖2024年，未开始回补")
            snapshot = {**snapshot, "history_calendar_start": baseline[-1]}
            available = sorted(set(available) | {d.isoformat() for d in historical})
        available = sorted(set(available) | set(snapshot.get("sessions", [])))
        requested = ([d for d in available if d >= snapshot["history_calendar_start"]]
                     if full_history else available[-(days + 1):])
        old_start = (snapshot.get("sessions") or requested)[0]
        sessions = [d for d in available if d >= min(old_start, requested[0])]
        start = date.fromisoformat(requested[0])
        closed = [day for day in sessions if day < now.date().isoformat() or now.time() >= time(15)]
        published = [day for day in sessions if day < now.date().isoformat() or now.hour >= CHECK_HOUR]
        if not closed or not published:
            raise ValueError("目标区间尚无已结束交易日")

        report(2, "核对交易所宽基ETF目录…")
        funds = snapshot.get("funds", [])
        catalog_date = snapshot.get("catalog_as_of")
        if not funds or not catalog_date or catalog_date < (now.date() - timedelta(days=7)).isoformat():
            fetched = attempt("交易所基金目录", source.catalog, [])
            if fetched:
                funds, catalog_date = fetched, now.date().isoformat()
        if not funds:
            raise ValueError("交易所基金目录不可用，尚不能确定统计范围")
        funds = [dict(row) for row in funds]
        known = {row["symbol"] for row in funds}
        for symbol, name, category in FOCUS_FUNDS:
            if symbol not in known:
                funds.append({"symbol": symbol, "name": name, "category": category,
                              "list_date": "", "classification": "固定重点样本，上市日待核验"})
        report(8, f"已识别 {len(funds)} 只ETF，读取重点ETF与上证指数日线…")
        def checkpoint():
            # 保存原始输入与已完成分钟段；取消或重启后复用，不把未完成聚合冒充完整结果。
            payload = {**snapshot, "funds": funds, "catalog_as_of": catalog_date,
                       "sessions": sessions, "shares": shares, "nav": nav,
                       "prices": prices, "profiles": profiles}
            atomic_write_text(work_path, json.dumps(payload, ensure_ascii=False, allow_nan=False))

        checkpoint()
        daily_provider = daily_provider or attempt("日线能力", lambda: _daily_provider(capset), None)
        if daily_provider is not None:
            for asset, symbols in (("etf", list(FOCUS_SYMBOLS)), ("index", ["000001.SH"])):
                raw = attempt(f"{asset}日线", lambda asset=asset, symbols=symbols: daily_provider.get_daily(
                    symbols, datetime.combine(start, time.min, tzinfo=CN_TZ),
                    datetime.combine(date.fromisoformat(closed[-1]), time(23, 59), tzinfo=CN_TZ), asset,
                ), pl.DataFrame())
                if not raw.is_empty():
                    raw = raw.filter(pl.col("symbol").is_in(symbols)
                                     & pl.col("date").is_between(start, date.fromisoformat(closed[-1])))
                    if raw.is_empty():
                        continue
                    check()
                    if asset == "etf":
                        # 共享指标会重建完整本地历史，因子不能仅取本次日线窗口。
                        # 严格同步失败时保留原共享日K/指标，看板仍可使用原始成交额。
                        report(9, "更新ETF除权因子…")
                        prepared = attempt("ETF除权因子", lambda raw=raw: index_sync.sync_etf_adj_factor(
                            sorted(raw["symbol"].unique().to_list()), repo, capset,
                            end_time=datetime.combine(date.fromisoformat(closed[-1]), time(23, 59), tzinfo=CN_TZ),
                            on_chunk_done=lambda *_: report(9, "更新ETF除权因子…"),
                            require_complete=True,
                        ), None)
                        check()
                        if prepared is not None:
                            index_sync.persist_etf_daily(raw, repo)
                    else:
                        index_sync._commit_index_daily_batch(raw, repo)
                    rows = [{"symbol": r["symbol"], "date": r["date"].isoformat(),
                             "amount": etf_flow.finite(r.get("amount")),
                             "close": etf_flow.finite(r.get("close"), positive=True)} for r in raw.to_dicts()]
                    prices = _merge(prices, rows, sessions[0])
            repo.refresh_index_views()

        shares = [r for r in shares if r["date"] >= sessions[0]]
        share_keys = {(r["symbol"], r["date"]) for r in shares}
        sh_symbols = tuple(r["symbol"] for r in funds if r["symbol"].endswith(".SH"))
        sh_days = [day for day in published if day >= requested[0] and ((not full_history and day in published[-2:]) or any(
            (row["symbol"], day) not in share_keys for row in funds
            if row["symbol"].endswith(".SH") and row["list_date"] <= day))]
        sz_funds = [row for row in funds if row["symbol"].endswith(".SZ")]
        work_count = max(1, len(sh_days) + len(sz_funds))
        for index, day in enumerate(sh_days):
            report(15 + int(30 * index / work_count), f"同步沪市份额 {day}…")
            rows = attempt(f"沪市份额 {day}", lambda day=day: source.sse_shares(
                date.fromisoformat(day), sh_symbols, require_complete=False), [])
            shares = _merge(shares, rows, sessions[0])
            if index % 20 == 0:
                checkpoint()
        checkpoint()
        for index, fund in enumerate(sz_funds):
            report(15 + int(30 * (len(sh_days) + index) / work_count), f"同步深市份额 {index + 1}/{len(sz_funds)}…")
            missing = [day for day in published if day >= requested[0] and fund["list_date"] <= day
                       and ((fund["symbol"], day) not in share_keys or (not full_history and day in published[-2:]))]
            if not missing:
                continue
            rows = attempt(f"深市份额 {fund['symbol']}", lambda missing=missing, fund=fund: source.szse_shares(
                date.fromisoformat(missing[0]), date.fromisoformat(missing[-1]), fund["symbol"]), [])
            shares = _merge(shares, rows, sessions[0])
            checkpoint()

        nav_keys = {(r["symbol"], r["date"]) for r in nav}
        nav_name = preferences.get_fund_nav_provider()
        if nav_provider is None and custom.provider_has_dataset(nav_name, "fund_nav"):
            nav_provider = custom.get_provider(nav_name)
        if nav_provider is None:
            errors.append("基金单位净值能力不可用")
        else:
            for index, fund in enumerate(funds):
                report(45 + int(25 * index / len(funds)), f"同步单位净值 {index + 1}/{len(funds)}…")
                missing = [d for d in published if d >= requested[0] and fund["list_date"] <= d
                           and ((fund["symbol"], d) not in nav_keys or (not full_history and d in published[-2:]))]
                if not missing:
                    continue
                result = attempt(f"单位净值 {fund['symbol']}", lambda fund=fund, missing=missing: nav_provider.get_fund_nav(
                    [fund["symbol"]], date.fromisoformat(missing[0]), date.fromisoformat(published[-1])), pl.DataFrame())
                rows = [{"symbol": r["symbol"], "date": r["date"].isoformat(), "unit_nav": r["unit_nav"]}
                        for r in result.to_dicts() if r["symbol"] == fund["symbol"]]
                nav = _merge(nav, rows, sessions[0])
                if index % 10 == 0:
                    checkpoint()
        checkpoint()

        minute_days = [day for day in requested[1:] if not profiles.get(day, {}).get("complete")
                       or any(p["index_close"] is None for p in profiles.get(day, {}).get("points", []))
                       or day in sessions[-2:]]
        # 以不超过20个交易日分段，复用现有分钟路由及写入互斥；空响应保留缺口。
        if ordinary_minute.minute_allowed(capset):
            with ordinary_minute.MINUTE_COLLECTION_LOCK:
                batches = []
                for day in minute_days:
                    if (not batches or len(batches[-1]) >= 20
                            or (date.fromisoformat(day) - date.fromisoformat(batches[-1][0])).days > 27):
                        batches.append([])
                    batches[-1].append(day)
                for offset, batch in enumerate(batches):
                    report(70 + int(25 * offset / max(1, len(batches))),
                           f"同步分钟行情 {batch[0]} 至 {batch[-1]}…")
                    begin = datetime.combine(date.fromisoformat(batch[0]), time(9, 30), tzinfo=CN_TZ)
                    end = min(now, datetime.combine(date.fromisoformat(batch[-1]), time(15), tzinfo=CN_TZ))
                    if begin >= end:
                        continue
                    attempt(f"分钟行情 {batch[0]}至{batch[-1]}", lambda begin=begin, end=end, offset=offset, batch=batch: ordinary_minute.collect_minutes(
                        repo, capset, {"etf": list(FOCUS_SYMBOLS), "index": ["000001.SH"]}, begin, end,
                        on_progress=lambda *_: report(70 + int(25 * offset / max(1, len(batches))),
                                                      f"采集分钟段 {batch[0]} 至 {batch[-1]}…"),
                        should_collect=lambda: (check() is None),
                    ), 0)
                    for day in batch:
                        check()
                        session_date = date.fromisoformat(day)
                        profile = etf_flow.minute_profile(
                            repo.get_minute_batch(list(FOCUS_SYMBOLS), session_date, asset_type="etf", adjust=False),
                            repo.get_minute("000001.SH", session_date, asset_type="index"), day)
                        # 没有任何行情的日期不伪造分钟覆盖；已有有效结果保留。
                        if profile["as_of"] is not None:
                            profiles[day] = profile
                    checkpoint()
        else:
            errors.append("分钟数据能力不可用")
        report(97, "核验份额、净值和交易日覆盖…")
        daily = etf_flow.build_daily(funds, sessions, shares, nav, prices)
        last_complete = next((row["date"] for row in reversed(daily) if row["net_flow_yuan"] is not None), None)
        last_daily = next((row for row in daily if row["date"] == closed[-1]), None)
        if last_daily is None or last_daily["amount_yuan"] is None:
            errors.append("最近已收盘交易日的重点ETF日线不完整")
        latest_profile = profiles.get(sessions[-1], {})
        minute_complete = latest_profile.get("complete") is True
        if sessions[-1] in closed and not minute_complete:
            errors.append("最近已收盘交易日分钟行情未覆盖20只ETF全天")
        elif sessions[-1] not in closed and latest_profile.get("covered", 0) < len(FOCUS_SYMBOLS):
            errors.append("最新交易日分钟行情尚未完整覆盖20只ETF")
        payload = {"history_calendar_start": snapshot.get("history_calendar_start"), "funds": funds, "catalog_as_of": catalog_date, "sessions": sessions,
                   "shares": shares, "nav": nav, "prices": prices, "daily": daily,
                   "profiles": profiles, "updated_at": now.isoformat(),
                   "sources": {"daily": preferences.get_daily_data_provider(),
                               "minute": preferences.get_minute_data_provider(), "nav": nav_name,
                               "shares": "沪深交易所公开份额"}}
        check()
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, allow_nan=False))
        status = {"checked_at": now.isoformat(), "state": "partial" if errors or last_complete != published[-1] else "ready",
                  "errors": errors, "flow_as_of": last_complete, "target_date": published[-1]}
        atomic_write_text(status_path, json.dumps(status, ensure_ascii=False))
        work_path.unlink(missing_ok=True)
        report(100, f"看板已更新：{len(daily)} 个交易日，完整净申购截至 {last_complete or '暂无'}")
        return {"days": len(daily), "funds": len(funds), "flow_as_of": last_complete,
                "minute_complete": minute_complete, "stage_errors": errors}
    except JobCancelledError:
        raise
    except Exception:
        atomic_write_text(status_path, json.dumps({"checked_at": now.isoformat(), "state": "failed",
                                                  "errors": ["本次同步未完成，保留上一份快照及分段采集进度"]}, ensure_ascii=False))
        raise
    finally:
        if owned:
            source.close()


async def submit(repo, capset, quote_service=None, *, days: int = 22, full_history: bool = False):
    from app.api.pipeline import _submit_job

    def make_run(progress, cancel_event):
        def check():
            if cancel_event.is_set():
                raise JobCancelledError(JOB_KEY)
        return lambda: sync_board(repo, capset, days=days, full_history=full_history, progress=progress, check_cancel=check)

    def finalize():
        repo.refresh_index_views()
        publish_data_update(repo.store.data_dir, ["etf_flow", "daily", "minute"])

    return await _submit_job(repo, quote_service, make_run, request_key=JOB_KEY,
                             with_cancel_event=True, finalize=finalize)


async def run_scheduled(repo):
    from fastapi import HTTPException

    from app.jobs.daily_pipeline import _get_app_state
    from app.services.pipeline_jobs import job_store

    now = cn_now()
    if (now.hour < CHECK_HOUR or not etf_flow.enabled_symbols(repo.store.data_dir)
            or trading_day.is_trading_day(now, data_dir=repo.store.data_dir) is not True):
        return None
    for job in job_store.list_recent(limit=200):
        if job.get("request_key") != JOB_KEY or not job.get("finished_at"):
            continue
        ended = datetime.fromisoformat(job["finished_at"]).astimezone(CN_TZ)
        if ended.date() != now.date():
            continue
        if job.get("outcome") == "cancelled":
            return None
        result = job.get("result") or {}
        if (job.get("status") == "succeeded" and not result.get("stage_errors")
                and result.get("flow_as_of") == now.date().isoformat()
                and result.get("minute_complete") is True):
            return None
    state = _get_app_state()
    if state is None:
        return None
    try:
        return await submit(repo, state.capabilities, getattr(state, "quote_service", None))
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        return None
