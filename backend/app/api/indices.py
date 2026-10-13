"""指数 API (核心四只固定清单, 浏览/搜索全量指数已下线; 仅保留详情读数与同步)。"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Annotated

import polars as pl
from fastapi import APIRouter, HTTPException, Query, Request

from app.market_time import cn_today
from app.services import index_sync, trading_day
from app.tickflow.capabilities import Cap

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/index", tags=["index"])

def _index_info(repo, symbol: str) -> dict:
    df = repo.get_index_instruments()
    if df.is_empty() or "symbol" not in df.columns:
        return {}
    hit = df.filter(pl.col("symbol") == symbol).head(1)
    if hit.is_empty():
        return {}
    return hit.to_dicts()[0]



@router.get("/daily")
def get_index_daily(
    request: Request,
    symbol: str = Query(..., description="指数代码, 如 000001.SH"),
    days: int = Query(120, ge=10, le=2000),
    start_date: str | None = Query(None, description="起始日期 YYYY-MM-DD, 优先于 days"),
    end_date: str | None = Query(None, description="截止日期 YYYY-MM-DD, 默认今天"),
):
    """读取指数日 K。指数数据使用独立 kline_index_* parquet。"""
    repo = request.app.state.repo
    # 未传 end_date 时用北京今天。实时注入只在内存缓存命中时补当日 K,
    # 缓存冷时 parquet 当日行能否进结果取决于这个窗口右端。
    end = date.fromisoformat(end_date) if end_date else cn_today()
    start = date.fromisoformat(start_date) if start_date else end - timedelta(days=days)
    info = _index_info(repo, symbol)

    df = repo.get_index_daily(symbol, start, end)
    if not df.is_empty():
        from app.api.kline import _maybe_inject_live_candle
        rows = _maybe_inject_live_candle(request, symbol, df.to_dicts(), "index", start=start, end=end)
        return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": rows, "source": "index_enriched"}

    return {"symbol": symbol, "name": info.get("name"), "index_info": info, "rows": [], "source": "none"}


@router.get("/minute")
def get_index_minute(
    request: Request,
    symbol: str = Query(..., description="指数代码, 如 000001.SH"),
    trade_date: Annotated[date | None, Query(alias="date", description="交易日期, 休市时默认最近本地指数交易日")] = None,
):
    """读取后台保存的指数分钟K，不触发供应商请求。"""
    repo = request.app.state.repo
    info = _index_info(repo, symbol)
    today = cn_today()
    day = trade_date or today
    if trade_date is None and trading_day.is_trading_day() is not True:
        day = repo.latest_minute_date(symbol, asset_type="index") or today
    df = repo.get_minute(symbol, day, asset_type="index") if day <= today else pl.DataFrame()
    return {"symbol": symbol, "name": info.get("name"), "index_info": info,
            "date": str(day), "rows": df.to_dicts(),
            "source": "future" if day > today else "local" if not df.is_empty() else "none"}


@router.post("/sync_instruments")
async def sync_index_instruments(request: Request):
    from app.api.pipeline import _submit_job
    repo = request.app.state.repo

    def make_run(progress):
        def run():
            progress("sync_index", 0, "同步指数名单…")
            count = index_sync.sync_index_instruments(repo)
            progress("done", 100, "指数名单完成")
            return {"count": count}
        return run
    return await _submit_job(repo, getattr(request.app.state, "quote_service", None), make_run,
                             request_key="index_instruments", datasets=["daily"])


@router.post("/sync_daily")
async def sync_index_daily(request: Request, days: int = Query(365, ge=30, le=5000)):
    from app.api.pipeline import _submit_job
    from app.market_time import cn_now
    repo = request.app.state.repo
    capset = request.app.state.capabilities
    if not capset.has(Cap.KLINE_DAILY_BATCH):
        raise HTTPException(status_code=403, detail="指数日线能力不可用")

    def make_run(progress):
        def run():
            progress("sync_index", 0, "同步指数日线…")
            end = cn_now()
            count = index_sync.sync_index_instruments(repo)
            rows = index_sync.sync_and_persist_index_daily(
                repo, capset, start_date=end - timedelta(days=days), end_date=end,
                on_chunk_done=lambda cur, total: progress("sync_index", int(cur * 95 / max(total, 1)), f"指数日线 {cur}/{total}"),
            )
            progress("done", 100, "指数日线完成")
            return {"index_count": count, "rows_written": rows}
        return run
    return await _submit_job(repo, getattr(request.app.state, "quote_service", None), make_run,
                             request_key=f"index_daily:{days}", datasets=["daily"])
