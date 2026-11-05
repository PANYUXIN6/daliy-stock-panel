"""固定四只沪深300ETF的场内规模占比；collector每日检查，GET仅读完整快照。"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl

from app.data_providers.csi300_reference import FUNDS, Csi300ReferenceSource
from app.market_time import CN_TZ, cn_now, cn_today
from app.services.fs_utils import atomic_write_text

logger = logging.getLogger(__name__)
JOB_ID = "csi300_etf_reference"
CHECK_HOUR = 16


def _path(data_dir: Path) -> Path:
    return data_dir / "market_reference" / "csi300_etf.json"


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def _checked_today(status: dict, now: datetime) -> bool:
    checked_at = status.get("checked_at") or ""
    if checked_at[:10] != now.date().isoformat():
        return False
    # 盘前手动检查或旧版早间检查，不能抵消当天收盘后的检查。
    cutoff = now.replace(hour=CHECK_HOUR, minute=0, second=0, microsecond=0)
    return now.hour < CHECK_HOUR or checked_at >= cutoff.isoformat()


def build_points(inputs: list[dict], caps: list[dict], days: list[date]) -> list[dict]:
    """同日四只齐全才能计算；比例采用小数制，例如0.02表示2%。"""
    if not caps:
        return []
    cap_frame = pl.DataFrame(caps).with_columns(pl.col("date").str.to_date())
    if (cap_frame["date"].is_duplicated().any()
            or cap_frame.filter(pl.col("float_cap_yuan").is_null()
                                | ~pl.col("float_cap_yuan").is_finite()
                                | (pl.col("float_cap_yuan") <= 0)).height):
        raise ValueError("沪深300流通市值无效或重复")
    expected = {day for day in days if day <= cap_frame["date"].max()}
    if set(cap_frame["date"]) != expected:
        raise ValueError("沪深300流通市值存在交易日缺口")
    if not inputs:
        raise ValueError("ETF计算输入为空")
    frame = pl.DataFrame(inputs).with_columns(pl.col("date").str.to_date())
    if not frame.height or frame.select("symbol", "date").is_duplicated().any():
        raise ValueError("ETF计算输入为空或重复")
    if set(frame["symbol"]) != set(FUNDS) or frame.filter(
        pl.any_horizontal(pl.col("shares", "unit_nav").is_null()
                          | ~pl.col("shares", "unit_nav").is_finite()
                          | (pl.col("shares", "unit_nav") <= 0))
    ).height:
        raise ValueError("ETF计算输入不完整或数值无效")
    counts = frame.group_by("date").agg(pl.len().alias("n"))
    if set(frame["date"]) != expected or counts.filter(pl.col("n") != 4).height:
        raise ValueError("同一交易日的四只ETF未完整对齐")
    return (
        frame.group_by("date").agg(
            (pl.col("shares") * pl.col("unit_nav")).sum().alias("etf_assets_yuan"),
        ).join(cap_frame, on="date", how="inner")
        .with_columns((pl.col("etf_assets_yuan") / pl.col("float_cap_yuan")).alias("ratio"))
        .sort("date").with_columns(pl.col("date").cast(pl.String)).to_dicts()
    )


def read_reference(data_dir: Path, as_of: date | None = None) -> dict:
    snapshot = _read(_path(data_dir))
    cutoff = min(as_of or cn_today(), cn_today())
    start = cutoff - timedelta(days=365)
    rows = [row for row in snapshot.get("rows", []) if start.isoformat() <= row["date"] <= cutoff.isoformat()]
    check = _read(_path(data_dir).with_name("last_check.json"))
    return {
        "state": "ready" if rows else "no_data", "rows": rows,
        "as_of": rows[-1]["date"] if rows else None,
        "checked_at": check.get("checked_at"), "update_failed": check.get("success") is False,
        "failure_stage": check.get("failure_stage") if check.get("success") is False else None,
    }


def sync_reference(data_dir: Path, *, force: bool = False,
                   progress: Callable | None = None, check_cancel: Callable[[], None] | None = None,
                   source: Csi300ReferenceSource | None = None, nav_provider=None) -> dict:
    from app.data_providers import custom as custom_sources
    from app.services import preferences
    from app.services.shared_runtime import publish_data_update

    emit = progress or (lambda *args: None)
    check = check_cancel or (lambda: None)
    now = cn_now()
    end, start = now.date(), now.date() - timedelta(days=365)
    path = _path(data_dir)
    status_path = path.with_name("last_check.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    status = _read(status_path)
    if not force and _checked_today(status, now):
        return {"updated": False, "reason": "今日已检查"}
    owned_source = source is None
    source = source or Csi300ReferenceSource(check)
    stage = "沪深300流通市值"
    try:
        check()
        snapshot = _read(path)
        existing_caps = [r for r in snapshot.get("caps", []) if r["date"] >= start.isoformat()]
        refresh_start = (date.fromisoformat(existing_caps[-1]["date"]).replace(day=1)
                         if existing_caps else start)
        emit("etf_reference", 5, "检查官方沪深300流通市值…")
        caps = {r["date"]: r for r in existing_caps}
        caps.update({r["date"]: r for r in source.index_float_caps(max(start, refresh_start), end)})
        cap_rows = sorted(caps.values(), key=lambda r: r["date"])
        if not cap_rows:
            raise ValueError("官方沪深300流通市值尚未发布")
        last = date.fromisoformat(cap_rows[-1]["date"])
        stage = "沪深300交易日期"
        days = source.trading_days(start, last)
        old = {(r["symbol"], r["date"]): r for r in snapshot.get("inputs", [])
               if start.isoformat() <= r["date"] <= last.isoformat()}
        cap_dates = set(caps)
        wanted = {(symbol, day) for symbol in FUNDS for day in cap_dates}
        missing = wanted - old.keys()
        stage = "四只ETF单位净值"
        if nav_provider is None:
            name = preferences.get_fund_nav_provider()
            if not custom_sources.provider_has_dataset(name, "fund_nav"):
                raise ValueError("基金净值数据源不可用，请在数据源配置中选择可用来源")
            nav_provider = custom_sources.get_provider(name)
        check()
        emit("etf_reference", 10, "读取四只ETF单位净值…")
        nav = nav_provider.get_fund_nav(list(FUNDS), start, last)
        nav_map = {(r["symbol"], r["date"].isoformat()): r["unit_nav"] for r in nav.to_dicts()}
        if wanted - nav_map.keys():
            raise ValueError("四只ETF单位净值未覆盖全部目标交易日")
        old = {key: {**row, "unit_nav": nav_map[key]} for key, row in old.items() if key in wanted}
        if missing:
            sh_days = sorted({day for symbol, day in missing if symbol.endswith(".SH")})
            shares = []
            stage = "上交所ETF历史份额"
            for index, day in enumerate(sh_days):
                check()
                emit("etf_reference", 15 + int(index * 65 / max(1, len(sh_days))),
                     f"同步ETF历史份额 {index + 1}/{len(sh_days)}…")
                shares.extend(source.sse_shares(date.fromisoformat(day)))
            sz_days = sorted(day for symbol, day in missing if symbol.endswith(".SZ"))
            if sz_days:
                stage = "深交所ETF历史份额"
                shares.extend(source.szse_shares(date.fromisoformat(sz_days[0]), date.fromisoformat(sz_days[-1])))
            for row in shares:
                key = (row["symbol"], row["date"])
                if key not in wanted:
                    continue
                if key not in nav_map:
                    raise ValueError("ETF份额与净值日期不一致")
                old[key] = {**row, "unit_nav": nav_map[key]}
        inputs = [old[key] for key in sorted(wanted) if key in old]
        stage = "份额、净值与流通市值的交易日对齐"
        rows = build_points(inputs, cap_rows, days)
        check()
        emit("etf_reference", 95, "核验比例与交易日覆盖…")
        payload = {"rows": rows, "inputs": inputs, "caps": cap_rows,
                   "nav_provider": preferences.get_fund_nav_provider(), "updated_at": now.isoformat()}
        atomic_write_text(path, json.dumps(payload, ensure_ascii=False, allow_nan=False))
        atomic_write_text(status_path, json.dumps({"checked_at": now.isoformat(), "success": True}))
        publish_data_update(data_dir, ["etf_reference"])
        emit("done", 100, "国家队持仓占比参考已更新")
        return {"updated": True, "rows": len(rows), "as_of": rows[-1]["date"] if rows else None}
    except Exception:
        # 对外仅保存固定阶段名称；供应商异常可能包含请求地址或凭据，不能透出到看板。
        atomic_write_text(status_path, json.dumps({
            "checked_at": now.isoformat(), "success": False, "failure_stage": stage,
        }, ensure_ascii=False))
        publish_data_update(data_dir, ["etf_reference"])
        raise
    finally:
        if owned_source:
            source.close()


async def run_scheduled(repo) -> dict | None:
    from fastapi import HTTPException

    from app.api.indices import submit_etf_reference
    from app.jobs.daily_pipeline import _get_app_state
    from app.services.pipeline_jobs import job_store

    now = cn_now()
    if now.hour < CHECK_HOUR:
        return None
    status = _read(_path(repo.store.data_dir).with_name("last_check.json"))
    if _checked_today(status, now):
        return None
    # 复用共享取消端点的持久化终态，亦覆盖尚未进入同步体的排队取消。
    # JobCancelledError 也用于超时退出，只有 cancelled 终态代表手动取消。
    for job in job_store.list_recent(limit=200):
        if job.get("request_key") != "etf_reference" or job.get("outcome") != "cancelled":
            continue
        cancelled_at = datetime.fromisoformat(job["finished_at"]).astimezone(CN_TZ)
        if cancelled_at.date() == now.date():
            return None
    state = _get_app_state()
    if state is None:
        return None
    try:
        return await submit_etf_reference(repo, getattr(state, "quote_service", None), force=False)
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
        logger.info("ETF占比检查等待已有数据任务结束")
        return None
