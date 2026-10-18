"""盘后管道 API — 异步触发 + 进度跟踪。"""
from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import logging
from collections.abc import Callable

from fastapi import APIRouter, HTTPException, Request

from app.api.data import invalidate_storage_cache
from app.jobs import daily_pipeline
from app.services.collection_status import collection_context
from app.services.pipeline_jobs import (
    JobBusyError,
    JobCancelledError,
    job_store,
    release_run_slot,
    run_with_capacity,
    try_acquire_run_slot,
)

# 长时间任务专用线程池（隔离于 FastAPI 默认线程池，防止阻塞请求处理）
_long_task_executor = _cf.ThreadPoolExecutor(max_workers=2, thread_name_prefix="long-task")

logger = logging.getLogger(__name__)
_tasks: set[asyncio.Task] = set()

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])


@router.post("/run")
async def run_now(request: Request) -> dict:
    """异步触发盘后管道,立即返回 job_id。客户端轮询 /jobs/{id} 拿进度。"""
    return await trigger_pipeline_job(
        request.app.state.repo,
        request.app.state.capabilities,
        getattr(request.app.state, "quote_service", None),
    )


async def trigger_pipeline_job(repo, capset, quote_service=None, *, postmarket: bool = False) -> dict:
    """触发数据页的手动盘后管道后台任务。

    若已有任务在跑,**返回该任务 id 而不是开新任务**(防止并发拉数据撞限流)。
    卡死判定按「进度停滞」而非总时长(慢带宽下长任务不会被误杀), 见 reap_stale。
    """
    from app.market_time import cn_today

    day = cn_today()
    options = {"postmarket": True, "target_date": day} if postmarket else {}
    finalize = None
    if postmarket:
        def finalize():
            # 自动任务的部分写入也必须通知读取方；任务终态仍由异常决定。
            from app.services import preferences
            from app.services.shared_runtime import publish_data_update, publish_quotes

            repo.refresh_cache()
            if quote_service is not None:
                publish_quotes(quote_service)
            datasets = ["daily", "enriched", "regime", "mainline", "ext_data"]
            if preferences.get_minute_sync_enabled():
                datasets.append("minute")
            publish_data_update(repo.store.data_dir, datasets)

    return await _submit_job(
        repo, quote_service,
        lambda progress: (lambda: daily_pipeline.run_now(repo, capset, on_progress=progress, **options)),
        request_key=f"postmarket:{day}" if postmarket else "pipeline",
        finalize=finalize,
    )


async def _submit_job(repo, quote_service, make_run, *, request_key=None, datasets=None,
                      finalize: Callable[[], None] | None = None,
                      with_cancel_event: bool = False, pause_quotes: bool = True) -> dict:
    """任务化执行公共机制: 单飞 create → 重任务槽 → 行情暂停 → 缓存失效。

    make_run(progress) 返回实际执行闭包(阻塞 IO + CPU, 在 executor 里跑)。
    盘后管道与独立除权因子同步共用 —— 两者写同一批 parquet, 必须共享
    单飞与重任务槽防并发覆写。
    finalize 用于板块聚合任务: 执行体退出时统一刷新并发布实际写入,
    包括部分失败/取消, 替代默认缓存刷新及成功发布。
    with_cancel_event 将既有任务信号作为 make_run 的第二参传入，供待发请求停止。
    pause_quotes=False 用于只写独立财务表的长任务，避免盘中行情整轮停采。
    """
    # 检测卡死的 running job (如 reload 后孤儿 task / 网络读无限阻塞)。
    # reap_stale 会在 /run 和 /jobs/{id} 轮询端点都调用,保证卡死后能自愈。
    job_store.reap_stale()
    context = collection_context(request_key, repo)

    # 单飞: 复用任何活跃 (pending∨running) 任务, is_new=False 时不再调度新任务
    try:
        job_id, is_new = job_store.create(request_key=request_key)
    except JobBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not is_new:
        return {"job_id": job_id, "reused": True}

    def progress(stage: str, pct: int, msg: str, stage_pct: int | None = None,
                 skip_log: bool = False) -> None:
        job_store.progress(job_id, stage, pct, msg, stage_pct=stage_pct, skip_log=skip_log)

    if with_cancel_event:
        from app.services.pipeline_jobs import get_cancel_event

        run = make_run(progress, get_cancel_event(job_id))
    else:
        run = make_run(progress)

    # 在 executor 里跑同步任务(pipeline 内部都是阻塞 IO + CPU)
    async def task() -> None:
        # 重任务执行槽: 防僵尸并发(reap 后线程仍活时新任务不得并行写 parquet)
        if not try_acquire_run_slot(job_id):
            job_store.fail(job_id, "已有数据任务在运行(或上一次任务卡死未结束),请稍后再试")
            return
        # 任务运行期间暂停实时行情取数, 防止覆写同一批 parquet 竞态
        qs = quote_service
        try:
            loop = asyncio.get_event_loop()

            def _run() -> dict:
                try:
                    if qs and pause_quotes:
                        with qs.paused():
                            return run()
                    return run()
                finally:
                    if finalize is None:
                        repo.refresh_cache()
                    else:
                        finalize()

            result = await loop.run_in_executor(_long_task_executor, run_with_capacity, job_id, _run)
            # 记录在任务创建时读取的路由/范围；执行中改了配置则不提供重复证据。
            if context is not None and context == collection_context(request_key, repo):
                result = {**result, "collection_context": context}
            if finalize is None:
                from app.services.shared_runtime import publish_data_update, publish_quotes
                if qs:
                    await asyncio.to_thread(publish_quotes, qs)
                publish_data_update(repo.store.data_dir, datasets or ['daily', 'enriched'])
            job_store.succeed(job_id, result)
            invalidate_storage_cache()
        except JobCancelledError:
            # 已被 reap/手动取消终止: job 状态已由 terminate() 写为 failed,
            # 拉取线程在分块回调处自行退出, 这里无需(也无法)再写状态。
            logger.warning("pipeline job %s cancelled", job_id)
        except Exception as e:
            logger.exception("pipeline job failed")
            job_store.fail(job_id, str(e))
            invalidate_storage_cache()
        finally:
            release_run_slot(job_id)

        # 周度研究沿用原全量同步后触发的语义, 不依赖已停用的盘后采集定时器。
        if (request_key == "pipeline" or (request_key or "").startswith("postmarket:")) and job_store.get(job_id)["status"] == "succeeded":
            await asyncio.to_thread(daily_pipeline.run_weekly_mining_after_sync)

    running = asyncio.create_task(task())
    _tasks.add(running)
    running.add_done_callback(_tasks.discard)
    return {"job_id": job_id, "reused": False}


@router.post("/adj-factor/run")
async def run_adj_factor_now(request: Request) -> dict:
    """独立同步除权因子(全历史 + 受影响个股 enriched 局部重算), 任务化+进度轮询。

    与盘后管道共用单飞与重任务槽 (两者写同一批 parquet); 门控与矩阵
    usable 同口径: TickFlow 档位不足且未路由到其他可用源时 400。
    """
    from app.services import preferences
    from app.tickflow.capabilities import Cap

    capset = request.app.state.capabilities
    if not (capset.has(Cap.ADJ_FACTOR) or preferences.get_adj_factor_provider() != "tickflow"):
        raise HTTPException(
            status_code=400,
            detail="除权因子能力不可用: 请先在 设置 → 数据源 中配置可用数据源",
        )
    repo = request.app.state.repo
    return await _submit_job(
        repo,
        getattr(request.app.state, "quote_service", None),
        lambda progress: (lambda: daily_pipeline.run_adj_factor_sync(repo, capset, on_progress=progress)),
        request_key="adj_factor",
    )


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    # 每次轮询都检查卡死 job — 前端持续轮询, 进度停滞超阈值后必定自愈,
    # 无需用户再次手动点「同步」。
    job_store.reap_stale()
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    return j


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    """手动取消一个 running 的 job(协作式: 拉取线程在当前分块完成后自行退出)。"""
    j = job_store.get(job_id)
    if not j:
        raise HTTPException(status_code=404, detail="job not found")
    if j["status"] not in ("running", "pending"):
        raise HTTPException(status_code=400, detail=f"job status is {j['status']}, cannot cancel")
    job_store.terminate(job_id, "用户手动取消")
    return {"cancelled": job_id}


@router.get("/jobs")
def list_jobs(limit: int = 20) -> dict:
    return {
        "active_id": job_store.active_id(),
        "jobs": job_store.list_recent(limit=limit),
    }
