"""财务数据 API — 浏览本地数据，采集命令检查财务数据源能力。"""
from __future__ import annotations

import logging

import polars as pl
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.services import ai_reports
from app.services.financial_analyzer import analyze_financials_stream
from app.services.financial_sync import FINANCIAL_SYNC_TABLES, FINANCIAL_TABLES, get_financial_df
from app.services.ndjson_heartbeat import with_heartbeat
from app.tickflow.capabilities import Cap

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/financials", tags=["financials"])


def _financial_allowed(capset) -> bool:
    """是否有财务数据访问权限 (TickFlow FINANCIAL 套餐 或 custom 财务源)。"""
    if capset.has(Cap.FINANCIAL):
        return True
    from app.services.financial_sync import _financial_is_custom
    return _financial_is_custom()


def _require_financial(capset) -> None:
    """_require_financial(capset) 的 custom 感知版本。"""
    if not _financial_allowed(capset):
        from app.tickflow.capabilities import CapabilityDenied
        raise CapabilityDenied(Cap.FINANCIAL)


@router.get("/status")
def financial_status(request: Request):
    """返回本地财务覆盖画像，采集进程离线也能浏览已有数据。"""
    data_dir = request.app.state.repo.store.data_dir
    tables = {}

    for table in FINANCIAL_TABLES:
        path = data_dir / "financials" / table / "part.parquet"
        if path.exists():
            try:
                df = pl.read_parquet(path, columns=["symbol"])
                tables[table] = {
                    "rows": len(df),
                    "symbols": df["symbol"].n_unique() if not df.is_empty() else 0,
                }
            except Exception:
                logger.exception("financial table statistics failed: %s", table)
                tables[table] = {"rows": 0, "symbols": 0}
        else:
            tables[table] = {"rows": 0, "symbols": 0}

    return {
        "available": any(table["rows"] > 0 for table in tables.values()),
        "tables": tables,
    }


@router.get("/execution-status")
def financial_execution_status(request: Request):
    """执行状态由采集进程回答，与本地数据是否可读分开。"""
    scheduler = getattr(request.app.state, "financial_scheduler", None)
    active_job_id = _active_financial_job()
    return {
        "last_sync": scheduler.last_sync if scheduler else {},
        "syncing": bool(active_job_id),
        "active_job_id": active_job_id,
    }


@router.get("/metrics")
def get_metrics(request: Request, symbol: str | None = None):
    """查询核心财务指标。"""
    df = get_financial_df(request.app.state.repo.store.data_dir, "metrics")
    if df.is_empty():
        return {"data": []}
    if symbol:
        df = df.filter(pl.col("symbol") == symbol)
    return {"data": df.to_dicts()}


@router.get("/income")
def get_income(request: Request, symbol: str | None = None):
    """查询利润表。"""
    df = get_financial_df(request.app.state.repo.store.data_dir, "income")
    if df.is_empty():
        return {"data": []}
    if symbol:
        df = df.filter(pl.col("symbol") == symbol)
    return {"data": df.to_dicts()}


@router.get("/balance-sheet")
def get_balance_sheet(request: Request, symbol: str | None = None):
    """查询资产负债表。"""
    df = get_financial_df(request.app.state.repo.store.data_dir, "balance_sheet")
    if df.is_empty():
        return {"data": []}
    if symbol:
        df = df.filter(pl.col("symbol") == symbol)
    return {"data": df.to_dicts()}


@router.get("/cash-flow")
def get_cash_flow(request: Request, symbol: str | None = None):
    """查询现金流量表。"""
    df = get_financial_df(request.app.state.repo.store.data_dir, "cash_flow")
    if df.is_empty():
        return {"data": []}
    if symbol:
        df = df.filter(pl.col("symbol") == symbol)
    return {"data": df.to_dicts()}


@router.get("/shares")
def get_shares(request: Request, symbol: str | None = None):
    """查询历史股本表。"""
    df = get_financial_df(request.app.state.repo.store.data_dir, "shares")
    if df.is_empty():
        return {"data": []}
    if symbol:
        df = df.filter(pl.col("symbol") == symbol)
    return {"data": df.to_dicts()}


def _active_financial_job() -> str | None:
    from app.services.pipeline_jobs import job_store
    job_id = job_store.active_id()
    job = job_store.get(job_id) if job_id else None
    return job_id if job and str(job.get("request_key") or "").startswith("financial:") else None


async def trigger_financial_sync(repo, capset, scheduler, table: str = "all", quote_service=None):
    """数据页的财务同步复用统一任务注册、执行槽和取消协议。"""
    from app.api.pipeline import _submit_job

    _require_financial(capset)
    if table not in {*FINANCIAL_SYNC_TABLES, "all", "unfinished"}:
        raise HTTPException(400, "当前财务采集仅支持利润表")
    if table == "unfinished":
        from app.services.financial_sync import unfinished_financial_tables

        try:
            tables = unfinished_financial_tables(repo.store.data_dir)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if not tables:
            raise HTTPException(400, "利润表已完成，更新数据请使用更新利润表")
    else:
        tables = list(FINANCIAL_SYNC_TABLES) if table == "all" else [table]
    labels = {"metrics": "核心指标", "income": "利润表", "balance_sheet": "资产负债表",
              "cash_flow": "现金流量表", "shares": "历史股本"}

    def make_run(progress, cancel_event):
        def run():
            from app.data_providers.base import ProviderRateLimitError

            result = {}
            errors = []
            for index, name in enumerate(tables):
                progress("financials", int(index * 100 / len(tables)), f"同步财务 {name}…")
                rate_limits = 0
                try:
                    while True:
                        try:
                            result.update(scheduler.run_now(name, on_progress=lambda done, total, index=index, name=name: progress(
                                "financials", int((index + done / max(total, 1)) * 95 / len(tables)),
                                f"财务 {name} {done}/{total}",
                            ), cancel_event=cancel_event))
                            break
                        except ProviderRateLimitError:
                            from app.services.financial_sync import wait_financial_rate_limit

                            rate_limits += 1
                            if not wait_financial_rate_limit(rate_limits, lambda remaining, index=index, name=name: progress(
                                "financials", int(index * 95 / len(tables)),
                                f"{labels[name]}限流冷却，{remaining}秒后从已保存批次继续",
                            ), cancel_event):
                                raise
                except ProviderRateLimitError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{labels[name]}: {exc}")
                    progress("financials", int((index + 1) * 95 / len(tables)), f"{labels[name]}取数失败：{exc}")
            if errors:
                raise RuntimeError("财务同步部分或全部取数失败：" + "；".join(errors))
            if not any(result.get(name, 0) > 0 for name in tables):
                raise RuntimeError("本次财务同步未获取可用数据，历史数据保持不变")
            empty = [name for name in tables if result.get(name, 0) == 0]
            if empty:
                progress("financials", 95, "本次未获取可用数据（可能不受当前源支持）：" + "、".join(labels[name] for name in empty))
            progress("done", 100, "财务同步完成")
            return result
        return run

    result = await _submit_job(repo, quote_service, make_run,
                               request_key=f"financial:{table}", datasets=["financials"],
                               with_cancel_event=True, pause_quotes=False)
    return {"status": "ok", "synced": {"started": not result["reused"]}, **result}


@router.post("/sync/{table}")
async def sync_table(request: Request, table: str):
    scheduler = getattr(request.app.state, "financial_scheduler", None)
    if scheduler is None:
        raise HTTPException(503, "FinancialScheduler not available")
    return await trigger_financial_sync(request.app.state.repo, request.app.state.capabilities,
                                        scheduler, table, getattr(request.app.state, "quote_service", None))


class AnalyzeRequest(BaseModel):
    """AI 财务分析请求。"""
    symbol: str
    focus: str = ""  # 可选:用户追加的分析关注点


@router.post("/analyze")
async def analyze_financials(request: Request, req: AnalyzeRequest):
    """AI 财务分析 — SSE 流式返回。

    后端读取该标的财务报表与股本表 → 注入 CFA 分析师级提示词 → 流式调用 LLM →
    逐 chunk 以 SSE 形式推给前端(JSON per line, 非 text/event-stream,
    以便前端用 ReadableStream 逐行解析,更简单可靠)。
    """
    if not req.symbol:
        raise HTTPException(400, "symbol 不能为空")

    data_dir = request.app.state.repo.store.data_dir

    from app.services.analysis_archive import archive_analysis

    async def stream_gen():
        async for chunk in with_heartbeat(archive_analysis(
            analyze_financials_stream(data_dir, req.symbol, req.focus),
            ai_reports.save_report, data_dir, "financials", {"symbol": req.symbol, "focus": req.focus},
        )):
            yield chunk + "\n"

    return StreamingResponse(
        stream_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ================================================================
# AI 报告 CRUD(历史报告持久化)
# ================================================================

class SaveReportRequest(BaseModel):
    """保存一条 AI 财务分析报告。"""
    symbol: str
    name: str = ""
    focus: str = ""
    content: str
    periods: int | None = None
    summary: str = ""


@router.get("/reports")
def list_reports(request: Request):
    """获取全部本地历史报告(按时间降序,后端已裁剪到上限)。"""
    return {"reports": ai_reports.list_reports()}


@router.post("/reports")
def save_report(request: Request, req: SaveReportRequest):
    """保存一条报告。"""
    report = ai_reports.save_report({
        "symbol": req.symbol,
        "name": req.name,
        "focus": req.focus,
        "content": req.content,
        "periods": req.periods,
        "summary": req.summary,
    })
    return {"ok": True, "report": report}


@router.delete("/reports/{report_id}")
def delete_report(request: Request, report_id: str):
    """删除一条报告。"""
    ok = ai_reports.delete_report(report_id)
    return {"ok": ok}
