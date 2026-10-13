"""手动采集的重复提示：只核对本地证据，不请求供应商或阻止定时任务。"""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Literal

import polars as pl
import pyarrow.parquet as pq
from pydantic import BaseModel, Field, model_validator

from app.market_time import CN_TZ, cn_now
from app.services import preferences, trading_day
from app.services.financial_sync import FINANCIAL_TABLES


class CollectionSyncRequest(BaseModel):
    kind: Literal["pipeline", "adj_factor", "financial", "index_daily", "minute", "minute_batch", "classifications", "ext", "depth", "quotes"]
    table: str | None = None
    symbol: str | None = None
    days: int | None = Field(default=None, ge=1, le=5000)
    config_id: str | None = None

    @model_validator(mode="after")
    def validate_scope(self):
        if self.kind == "financial" and self.table not in (*FINANCIAL_TABLES, "all"):
            raise ValueError("请选择有效的财务表")
        if self.kind == "index_daily" and (self.days is None or self.days < 30):
            raise ValueError("指数天数必须在 30 到 5000 之间")
        if self.kind == "minute" and (not self.symbol or not self.symbol.strip() or self.days is None or self.days > 30):
            raise ValueError("请提供分钟标的和 1 到 30 个交易日的范围")
        if self.kind == "minute_batch" and (self.days is None or self.days > 30):
            raise ValueError("请提供 1 到 30 个自然日的分钟首次回溯配置")
        if self.kind == "ext" and (not self.config_id or not self.config_id.strip()):
            raise ValueError("请选择扩展数据表")
        return self

    def request_key(self) -> str:
        if self.kind == "financial":
            return f"financial:{self.table}"
        if self.kind == "index_daily":
            return f"index_daily:{self.days}"
        if self.kind == "minute":
            return f"minute:{self.symbol.strip()}:{self.days}"
        if self.kind == "minute_batch":
            return f"minute_batch:{self.days}:False"
        return self.kind


def collection_context(request_key: str | None, repo) -> dict | None:
    """保存实际路由与范围到既有任务结果，避免换源/改范围后误称重复。"""
    if not request_key:
        return None
    kind = request_key.split(":", 1)[0]
    if kind == "pipeline":
        return {
            "daily_provider": preferences.get_daily_data_provider(),
            "adj_provider": preferences.get_adj_factor_provider(),
            "minute_provider": preferences.get_minute_data_provider(),
            **preferences.get_pipeline_pull_types(),
            "index_symbols": preferences.get_pipeline_index_symbols(),
            "minute_enabled": preferences.get_minute_sync_enabled(),
            "minute_days": preferences.get_minute_sync_days(),
            "regime_enabled": preferences.get_pipeline_regime_enabled(),
            "mainline_filter": preferences.get_mainline_filter_config(),
        }
    if kind == "adj_factor":
        earliest = repo.earliest_daily_date()
        return {"provider": preferences.get_adj_factor_provider(), "start_date": str(earliest) if earliest else None}
    if kind == "financial":
        return {"provider": preferences.get_financial_provider()}
    if kind == "index_daily":
        return {"provider": "tickflow", "symbols": preferences.get_pipeline_index_symbols()}
    if kind == "minute":
        return {"provider": preferences.get_minute_data_provider(), "asset_type": repo.resolve_asset_type(request_key.split(":")[1])}
    if kind == "minute_batch":
        _, requested_days, extend = request_key.split(":")
        days = preferences.get_minute_sync_days() if requested_days == "None" else int(requested_days)
        return {"provider": preferences.get_minute_data_provider(), "asset_type": "stock",
                "days": days, "extend_backward": extend == "True" or days >= 365}
    return None


def _has_rows(path: Path) -> bool:
    return path.is_file() and pq.read_metadata(path).num_rows > 0


def _has_partition(data_dir: Path, table: str, day: date) -> bool:
    return any(_has_rows(path) for path in (data_dir / table / f"date={day}").glob("*.parquet"))


def _closed_session(data_dir: Path, now: datetime) -> date | None:
    calendar = trading_day.calendar_status(data_dir)
    if trading_day.is_trading_day(now, data_dir=data_dir) is None:
        return None
    # 交易日盘中仍会变化，不对行情类弹出“重复更新”确认。
    if now.date().isoformat() in calendar["days"] and time(9, 30) <= now.time() < time(15):
        return None
    days = [date.fromisoformat(day) for day in calendar["days"]
            if day < now.date().isoformat() or (day == now.date().isoformat() and now.time() >= time(15))]
    return max(days) if days else None


def _response(message: str | None = None, last_synced_at: str | None = None) -> dict:
    return {"requires_confirmation": message is not None, "message": message, "last_synced_at": last_synced_at}


def _ext_saved_today(data_dir: Path, config_id: str, now: datetime) -> datetime | None:
    paths = (data_dir / "ext_data" / config_id).glob("**/*.parquet")
    latest = max((path.stat().st_mtime for path in paths if _has_rows(path)), default=None)
    if latest is not None:
        saved_at = datetime.fromtimestamp(latest, CN_TZ)
        if saved_at.date() == now.date():
            return saved_at
    return None


def check_collection_sync(request: CollectionSyncRequest, repo, *, quote_service=None, job_store=None) -> dict:
    """已知相同范围已同步时提示；未知、空结果、失败和历史旧记录不伪装成最新。"""
    now = cn_now()
    data_dir = repo.store.data_dir
    if request.kind == "classifications":
        saved = [_ext_saved_today(data_dir, config_id, now) for config_id in ("ext_gn_ths", "ext_hy_ths")]
        if all(saved):
            return _response("概念与行业分类已有今天保存的本地数据。数据源可能修订内容，是否重新拉取？", max(saved).isoformat())
        return _response()
    if request.kind == "ext":
        from app.services.ext_data import ExtConfigStore
        config = ExtConfigStore(data_dir).get(request.config_id)
        if config is None:
            raise ValueError("扩展数据表不存在")
        # 扩展表没有供应商版本信息，提示只描述本地文件，不声称上游已无变化。
        saved_at = _ext_saved_today(data_dir, config.id, now)
        if saved_at is not None:
            return _response(f"{config.label}已有今天保存的本地数据。数据源可能修订内容，是否重新拉取？", saved_at.isoformat())
        return _response()

    session = _closed_session(data_dir, now) if request.kind in {"pipeline", "index_daily", "minute", "minute_batch", "depth", "quotes"} else None
    if request.kind in {"depth", "quotes"}:
        if session is None:
            return _response()
        if request.kind == "quotes":
            state = quote_service.status() if quote_service else {}
            fetched_ms = state.get("last_fetch_ms")
            if state.get("final_sync_done") and not state.get("final_sync_failed") and fetched_ms:
                fetched_at = datetime.fromtimestamp(fetched_ms / 1000, CN_TZ)
                if fetched_at.date() == session and fetched_at.time() >= time(15):
                    return _response(f"{session} 的收盘行情已确认保存，是否重新拉取？", fetched_at.isoformat())
        elif _has_partition(data_dir, "depth5", session):
            # 盘口 fetched_at 为 perf_counter，不能当作收盘墙钟时间。
            return _response(f"本地已有 {session} 的五档盘口快照，快照可能来自盘中。是否重新拉取以更新盘口？")
        return _response()

    if request.kind in {"pipeline", "index_daily", "minute", "minute_batch"} and session is None:
        return _response()
    if job_store is None:
        from app.services.pipeline_jobs import job_store
    key = request.request_key()
    context = collection_context(key, repo)
    jobs = job_store.list_recent(limit=50)
    latest = next((job for job in jobs if job.get("request_key") == key), None)
    if not latest or latest["status"] != "succeeded":
        return _response()
    result = latest.get("result") or {}
    if result.get("collection_context") != context or result.get("stage_errors"):
        return _response()
    skipped = set(result.get("skipped_stages") or [])
    if request.kind == "pipeline":
        # 未勾选的可选步骤本就不属于本次范围；缺能力导致的跳过不能算已完成。
        if not context["minute_enabled"]:
            skipped.discard("sync_minute")
        if not context["regime_enabled"]:
            skipped.difference_update({"regime", "mainline"})
        if not (context["pipeline_pull_index"] or context["pipeline_pull_etf"]):
            skipped.discard("sync_index")
    if skipped:
        return _response()
    started_at = datetime.fromisoformat(latest["started_at"]).astimezone(CN_TZ)
    finished_at = latest["finished_at"]
    if request.kind in {"financial", "adj_factor"}:
        if started_at.date() != now.date():
            return _response()
    elif started_at < datetime.combine(session, time(15), tzinfo=CN_TZ):
        return _response()

    if request.kind == "financial":
        tables = FINANCIAL_TABLES if request.table == "all" else (request.table,)
        if request.table == "all" and not all(table in result for table in tables):
            return _response()
        available = [table for table in tables
                     if result.get(table, 0) > 0 and _has_rows(data_dir / "financials" / table / "part.parquet")]
        if not available:
            return _response()
        message = "所选财务表今天已完成过同源同步任务，本地已有结果。财务公告仍可能新增或修订，是否重新拉取？"
        if len(available) != len(tables):
            labels = {"metrics": "核心指标", "income": "利润表", "balance_sheet": "资产负债表",
                      "cash_flow": "现金流量表", "shares": "历史股本"}
            available_names = "、".join(labels[table] for table in available)
            unavailable_names = "、".join(labels[table] for table in tables if table not in available)
            message = (f"所选五张财务表今天已执行过同源同步任务。当前有本地可用结果：{available_names}；"
                       f"本次任务未确认有可用结果：{unavailable_names}。财务公告仍可能新增或修订，是否重新拉取？")
    elif request.kind == "adj_factor":
        if not result.get("adj_written") or not any(_has_rows(path) for path in (data_dir / "adj_factor").glob("**/*.parquet")):
            return _response()
        message = "当前数据源与历史窗口的除权因子今天已成功同步，是否重新拉取？"
    elif request.kind == "index_daily":
        if result.get("rows_written", 0) <= 0 or not _has_partition(data_dir, "kline_index_daily", session):
            return _response()
        message = f"指数日线相同的 {request.days} 天范围已在 {session} 收盘后成功同步，是否重新拉取？"
    elif request.kind == "minute":
        table = {"stock": "kline_minute", "etf": "kline_etf_minute", "index": "kline_index_minute"}[context["asset_type"]]
        paths = list((data_dir / table / f"date={session}").glob("*.parquet"))
        if result.get("rows", 0) <= 0 or not paths:
            return _response()
        if pl.scan_parquet(paths).filter(pl.col("symbol") == request.symbol.strip()).select(pl.len()).collect().item() == 0:
            return _response()
        message = f"{request.symbol.strip()} 按 {request.days} 个交易日配置的分钟任务已在 {session} 收盘后完成，是否重新拉取？"
    elif request.kind == "minute_batch":
        if result.get("minute_rows", 0) <= 0 or not _has_partition(data_dir, "kline_minute", session):
            return _response()
        message = (f"按“首次回溯 {request.days} 个自然日、已有数据从最新时间增量更新”的配置，"
                   f"同源同范围配置的分钟任务已在 {session} 收盘后完成，本地已有结果。上游仍可能修订数据，是否重新拉取？")
    else:
        tables = [table for enabled, table in ((context["pipeline_pull_a_share"], "kline_daily"),
                  (context["pipeline_pull_index"], "kline_index_daily"), (context["pipeline_pull_etf"], "kline_etf_daily")) if enabled]
        if not tables or not all(_has_partition(data_dir, table, session) for table in tables):
            return _response()
        message = f"当前数据源与同步范围已在 {session} 收盘后完成一次全量同步。上游仍可能修订数据，是否重新拉取？"
    return _response(message, finished_at)
