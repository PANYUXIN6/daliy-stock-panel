"""FastAPI 入口。"""
from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api import (
    abnormal,
    alerts,
    analysis,
    backtest,
    data,
    events,
    ext_data,
    factors,
    financials,
    indices,
    intraday,
    kline,
    lots,
    market_recap,
    mining,
    monitor_rules,
    overview,
    paper,
    pipeline,
    regime,
    rps,
    screener,
    sector_rotation,
    signals,
    stock_analysis,
    strategy,
    watchlist,
)
from app.api import auth as auth_api
from app.api import settings as settings_api
from app.api.routes import router as core_router
from app.config import settings
from app.enriched_generation import EnrichedGenerationUnavailableError
from app.extensions.loader import (
    configure_backend_extensions,
    current_extension_context,
    start_backend_extensions,
)
from app.jobs import daily_pipeline
from app.services.matrix_prewarm_owner import MatrixCachePrewarmOwner
from app.services.mining_process_lock import MiningProcessLock
from app.services.quote_service import QuoteService
from app.tickflow import client as tf_client
from app.tickflow.policy import detect_capabilities
from app.tickflow.repository import DataStore, KlineRepository

logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 追加文件日志: uvicorn (含 --reload 开发模式) 默认只有 StreamHandler, 同步/管道等
# 运行时日志仅出现在 dev 终端, 关掉或滚屏后即丢失, 排查「同步后日志没落」时无处可查。
# 落盘到 data/backend.log 与桌面版 (desktop.py:_setup_logging → desktop.log) 行为对齐,
# 事后可查。桌面版 (frozen) 已由 desktop.py 写 desktop.log, 此处跳过避免重复落盘。
# RotatingFileHandler 防止长期运行/频繁 reload 导致文件无限增长。
if not getattr(sys, "frozen", False):
    try:
        from logging.handlers import RotatingFileHandler

        _log_path = settings.data_dir / "backend.log"
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        _file_handler = RotatingFileHandler(
            _log_path, maxBytes=10 * 1024 * 1024, backupCount=3,
            mode="a", encoding="utf-8", errors="replace",
        )
        _file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        logging.getLogger().addHandler(_file_handler)
    except Exception as _e:  # noqa: BLE001
        logger.warning("文件日志初始化失败, 仅输出到终端: %s", _e)


@asynccontextmanager
async def _application_lifespan(app: FastAPI):
    from app.services import auth as auth_service
    from app.services.user_migration import migrate_legacy_private_data
    auth_service.bootstrap_from_env()
    migrate_legacy_private_data(settings.data_dir, auth_service.get_store().first_admin_id())
    logger.info(
        "Tick Stock Panel v%s starting (mode=%s)",
        __version__, tf_client.current_mode(),
    )

    from app.services.pipeline_jobs import job_store
    job_store.recover_interrupted()

    from app.services.ai_credits import get_credit_store
    get_credit_store(settings.data_dir).recover_interrupted()

    # 数据层
    store = DataStore()
    repo = KlineRepository(store)
    app.state.datastore = store
    app.state.repo = repo
    from app.services.user_runtime import UserRuntimeRegistry
    app.state.user_runtimes = UserRuntimeRegistry(app.state)
    app.state.user_runtimes.recover_interrupted()
    # 在接受回测请求前固定 managed generation，避免首批并发 worker 各自创建版本。
    if settings.backtest_matrix_disk_cache_enabled:
        try:
            repo.get_matrix_data_generation("stock")
        except EnrichedGenerationUnavailableError as exc:
            logger.warning("enriched generation requires a full rebuild: %s", exc)
    # 指标异步预热标志: enriched 缓存在后台线程构建, 完成后置 True
    app.state.indicators_ready = False
    def _warmup_done():
        app.state.indicators_ready = True
        quote = getattr(app.state, 'quote_service', None)
        if quote is not None:
            from app.services.shared_runtime import publish_data_update, publish_quotes
            publish_quotes(quote)
            publish_data_update(store.data_dir, ['enriched'])
    repo._on_warmup_done = _warmup_done

    # Polars 缓存预热 — enriched 的重计算 (107万行 compute_indicators) 推后台,
    # instruments/index/ETF 仍同步 (毫秒级)。应用立即 ready, 指标算完后自动替换。
    repo.refresh_cache(background=True)

    # 自定义数据源配置(可选): 失败只记录错误, 不影响 TickFlow 基准路径。
    try:
        from app.data_providers import custom as custom_sources
        custom_sources.load_all()
        logger.info("custom data sources loaded: %d", len(custom_sources.list_sources()))
    except Exception as e:  # noqa: BLE001
        logger.warning("custom data sources init failed: %s", e)

    # 自定义源必须先注册,能力探测才能补充其数据集能力。
    capset = detect_capabilities()
    app.state.capabilities = capset
    logger.info("ready; %d capabilities active", len(capset.all()))

    # 全局行情服务
    qs = QuoteService()
    app.state.quote_service = qs
    qs.set_repo(repo)
    qs.boot_check()

    # QuoteService 需要访问 strategy_monitor 等单例
    # 先创建 strategy_monitor，再注入 app.state
    from app.strategy.monitor import StrategyMonitorService
    strategy_monitor = StrategyMonitorService()
    app.state.strategy_monitor = strategy_monitor
    qs.set_app_state(app.state)

    # 五档盘口 sealed 服务(真假涨停/跌停, 独立旁路线)
    from app.services.depth_service import DepthService
    depth_service = DepthService()
    depth_service.set_repo(repo)
    depth_service.set_app_state(app.state)
    app.state.depth_service = depth_service

    # 启动调度器(若 enriched 数据为空,首次启动可手动 POST /api/pipeline/run)
    try:
        daily_pipeline.set_app_state(app.state)
        scheduler = daily_pipeline.start_scheduler(repo)
        app.state.scheduler = scheduler
    except Exception as e:  # noqa: BLE001
        logger.warning("scheduler not started: %s", e)
        app.state.scheduler = None

    # depth sealed: 恢复本地数据 + 盘中轮询(有能力时); 盘后仅手动更新。
    try:
        depth_service.boot_check()
        depth_service.start_polling()
    except Exception as e:  # noqa: BLE001
        logger.warning("depth_service init failed: %s", e)

    # 盘中分钟增量刷新 (Expert 专有): 线程常驻, 开关/时段/能力门控在循环内每轮判断
    try:
        from app.services.minute_refresh import MinuteRefreshService
        minute_refresh = MinuteRefreshService(repo)
        minute_refresh.set_app_state(app.state)
        app.state.minute_refresh = minute_refresh
        minute_refresh.start()
    except Exception as e:  # noqa: BLE001 -- 可选分钟服务启动失败不阻断其他服务
        logger.warning("minute_refresh init failed: %s", e, exc_info=True)

    # 企业微信智能机器人长连接(可选通道, 失败不阻断启动)
    try:
        from app.services.wecom_bot_service import WecomBotService
        wecom_bot_service = WecomBotService()
        wecom_bot_service.set_app_state(app.state)
        app.state.wecom_bot_service = wecom_bot_service
        wecom_bot_service.boot_check()
    except Exception as e:  # noqa: BLE001
        logger.warning("wecom_bot_service init failed: %s", e)

    # 内置扩展表 (概念/行业): 先创建 config, 新建默认仅手动拉取。
    # 必须在 pull_scheduler.refresh() 之前执行, 否则全新部署时 scheduler 读不到
    # 刚创建的预设, 定时任务不会启动。
    try:
        from app.services.ext_presets import ensure_builtin_presets
        await ensure_builtin_presets(store.data_dir)
    except Exception as e:  # noqa: BLE001
        logger.warning("内置扩展表初始化失败 (不影响启动): %s", e)

    # 扩展数据定时拉取: 在预设配置就绪后启动, 自动调度 enabled 的预设。
    from app.services.ext_pull import pull_scheduler
    pull_scheduler.start(store.data_dir)
    pull_scheduler.refresh(store.data_dir)
    app.state.pull_scheduler = pull_scheduler

    # 保留旧同步状态供本地画像展示，采集统一由按需服务执行。
    from app.services.financial_sync import financial_scheduler
    financial_scheduler.start(store.data_dir, capset)
    app.state.financial_scheduler = financial_scheduler

    from app.services import financial_demand
    financial_demand.register(repo, capset)

    # 自愈看门狗: 探测 polars 闸与写锁, 僵死时退出交由 supervisor 拉起 (兜底层)。
    from app.watchdog import start_watchdog
    app.state.watchdog = start_watchdog(app.state, repo)

    # 策略引擎
    from app.strategy.engine import StrategyEngine
    strategy_dirs = [
        Path(__file__).resolve().parent / "strategy" / "builtin",
        store.data_dir / "strategies" / "templates",
    ]
    strategy_engine = StrategyEngine(strategy_dirs=strategy_dirs)
    app.state.strategy_engine = strategy_engine
    logger.info("strategy engine loaded: %d strategies", len(strategy_engine.list_strategies()))

    matrix_prewarm_owner = MatrixCachePrewarmOwner()

    def _schedule_matrix_cache_prewarm() -> None:
        if (
            not settings.backtest_matrix_disk_cache_enabled
            or not settings.backtest_matrix_cache_prewarm
        ):
            return

        def _prewarm() -> None:
            from app.backtest.engine import BacktestEngine
            from app.backtest.matrix import MatrixPrewarmCancelledError
            from app.backtest.strategy import prewarm_matrix_cache
            from app.services.heavy_job_limiter import (
                HeavyJobCancelledError,
                shared_heavy_job_limiter,
            )

            try:
                latest = repo.latest_enriched_date("stock")
                if latest is None:
                    logger.info("matrix cache prewarm skipped: no stock enriched data")
                    return

                with shared_heavy_job_limiter.slot(
                    "exclusive",
                    cancel_event=matrix_prewarm_owner.cancel_event,
                ):
                    result = prewarm_matrix_cache(
                        BacktestEngine(repo),
                        strategy_engine,
                        asset_type="stock",
                        latest_date=latest,
                        years=settings.backtest_matrix_cache_prewarm_years,
                        cancel_event=matrix_prewarm_owner.cancel_event,
                    )
                logger.info("matrix cache prewarm done: %s", result)
            except (HeavyJobCancelledError, MatrixPrewarmCancelledError):
                logger.info("matrix cache prewarm cancelled")
            except Exception:
                logger.exception("matrix cache prewarm failed")

        if not matrix_prewarm_owner.schedule(_prewarm):
            logger.info("matrix cache prewarm already running or shutting down, skip")

    def _refresh_done():
        from app.services.shared_runtime import publish_data_update, publish_quotes
        repo.get_enriched_latest_asset('etf')
        repo.get_enriched_latest_asset('index')
        publish_quotes(qs)
        publish_data_update(store.data_dir, ['enriched'])
        _schedule_matrix_cache_prewarm()

    repo._on_refresh_done = _refresh_done
    if repo.enriched_ready:
        _schedule_matrix_cache_prewarm()


    import asyncio

    from app.services import trading_day
    from app.services.ordinary_minute import OrdinaryMinuteService
    async def _sync_calendar():
        try:
            await asyncio.to_thread(trading_day.sync_calendar, store.data_dir)
        except Exception:
            logger.exception('交易日历同步失败，调度保持未就绪')
        try:
            await asyncio.to_thread(daily_pipeline.run_scheduled_instruments_sync, repo)
        except Exception:
            logger.exception('启动时股票基础资料补跑检查失败')
    calendar_task = asyncio.create_task(_sync_calendar())
    ordinary_minute = OrdinaryMinuteService(repo, app_state=app.state)
    app.state.ordinary_minute = ordinary_minute
    ordinary_minute.start()
    from app.services.shared_runtime import publish_quotes
    # 股票预热可能在 quote_service 建立前结束，初次发布仍须包含本地 ETF/指数。
    await asyncio.to_thread(repo.get_enriched_latest_asset, "etf")
    await asyncio.to_thread(repo.get_enriched_latest_asset, "index")
    await asyncio.to_thread(publish_quotes, qs)

    # 源码内二次开发启动钩子: 仅暴露稳定只读上下文, 单个扩展失败不影响核心启动。
    extension_registry = app.state.extension_registry
    start_backend_extensions(
        current_extension_context(data_dir=store.data_dir, repository=repo),
        extension_registry,
    )

    try:
        yield
    finally:
        calendar_task.cancel()
        import contextlib
        with contextlib.suppress(asyncio.CancelledError):
            await calendar_task
        ordinary_minute.stop()
        repo._on_refresh_done = None
        wd = getattr(app.state, "watchdog", None)
        if wd:
            await wd.stop()
        if not matrix_prewarm_owner.shutdown(timeout=5.0):
            logger.warning("matrix cache prewarm did not stop within 5 seconds")
        app.state.user_runtimes.shutdown()
        if app.state.scheduler:
            app.state.scheduler.shutdown(wait=False)
        ps = getattr(app.state, "pull_scheduler", None)
        if ps:
            ps.stop()
        fsc = getattr(app.state, "financial_scheduler", None)
        if fsc:
            fsc.stop()
        financial_demand.unregister(store.data_dir)
        qs = getattr(app.state, "quote_service", None)
        if qs:
            qs.stop()
        dsvc = getattr(app.state, "depth_service", None)
        if dsvc:
            dsvc.stop_polling()
        wbot = getattr(app.state, "wecom_bot_service", None)
        if wbot:
            wbot.stop()
        mrs = getattr(app.state, "minute_refresh", None)
        if mrs:
            mrs.stop()
        # webhook 投递线程池: 放弃排队投递并关闭, 避免通知静默丢失在 daemon 线程里
        from app.services.quote_service import shutdown_webhook_executor

        shutdown_webhook_executor()
        logger.info("shutdown")


@asynccontextmanager
async def _web_lifespan(app: FastAPI):
    import asyncio
    import contextlib

    import httpx
    import polars as pl

    from app.services import auth as auth_service
    from app.services.shared_runtime import QuoteMirror, bridge_events
    from app.tickflow.capabilities import CapabilitySet
    auth_service.bootstrap_from_env()
    from app.services.user_migration import migrate_legacy_private_data
    migrate_legacy_private_data(settings.data_dir, auth_service.get_store().first_admin_id())
    store = DataStore(read_only=True)
    repo = KlineRepository(store)
    # 缺采集快照时返回空行情，不触发 repository 的历史重算/修复分支。
    repo.install_shared_snapshot({name: pl.DataFrame() for name in (
        '_enriched_cache', '_etf_enriched_cache', '_index_enriched_cache',
        '_live_agg_cache', '_etf_live_agg_cache',
    )}, {})
    app.state.datastore = store
    app.state.repo = repo
    from app.services.user_runtime import UserRuntimeRegistry
    app.state.user_runtimes = UserRuntimeRegistry(app.state)
    app.state.capabilities = CapabilitySet()
    app.state.indicators_ready = False
    app.state.collector_connected = False
    app.state.collector_last_event_at = None
    from app.services.depth_service import DepthService
    app.state.depth_service = DepthService()
    app.state.depth_service.set_repo(repo)
    app.state.quote_service = QuoteMirror(repo)
    app.state.quote_service.load_snapshot(app.state)
    app.state.collector_url = settings.collector_url.rstrip('/')
    async with httpx.AsyncClient(timeout=httpx.Timeout(15, read=None), trust_env=False) as client:
        app.state.collector_client = client
        bridge = asyncio.create_task(bridge_events(app, app.state.collector_url))
        try:
            yield
        finally:
            bridge.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await bridge
            app.state.user_runtimes.shutdown()
            store.db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    role = getattr(getattr(app, 'state', None), 'service_role', 'collector')
    if role == 'web':
        async with _web_lifespan(app):
            yield
        return
    # 复用原有跨平台锁：collector 同时拥有采集、挖掘和配置写入。
    owner_lock = MiningProcessLock(settings.data_dir)
    owner_lock.acquire()
    try:
        async with _application_lifespan(app):
            yield
    finally:
        owner_lock.release()


def create_app(role: str | None = None) -> FastAPI:
    """显式构建运行角色，测试可独立创建 web 和 collector。"""
    role = role or settings.service_role
    if role not in {'web', 'collector'}:
        raise ValueError('service role must be web or collector')
    app = FastAPI(
        title="Tick Stock Panel",
        version=__version__,
        description="A 股选股 + 回测面板 — TickFlow 适配",
        lifespan=lifespan,
    )

    app.state.service_role = role
    if role == 'collector':
        from app.services.shared_runtime import router as runtime_router
        app.include_router(runtime_router)

    # Browser writes use same-origin cookies; authentication enforces Origin/Referer.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )


    from app.services.collector_proxy import proxy_middleware
    app.middleware("http")(proxy_middleware)

    from app.services.auth_access import install_auth_middleware
    from app.services.business_access import business_middleware
    app.middleware("http")(business_middleware)
    install_auth_middleware(app)

    # 路由
    app.include_router(core_router)
    app.include_router(auth_api.router)
    from app.api.ai_credits import router as ai_credits_router
    app.include_router(ai_credits_router)
    from app.api.users import router as users_router
    app.include_router(users_router)
    app.include_router(kline.router)
    app.include_router(watchlist.router)
    app.include_router(screener.router)
    app.include_router(backtest.router)
    app.include_router(factors.router)
    app.include_router(mining.router)
    app.include_router(intraday.router)
    app.include_router(indices.router)
    app.include_router(overview.router)
    app.include_router(paper.router)
    app.include_router(abnormal.router)
    app.include_router(regime.router)
    app.include_router(analysis.router)
    app.include_router(pipeline.router)
    app.include_router(data.router)
    app.include_router(ext_data.router)
    app.include_router(financials.router)
    app.include_router(stock_analysis.router)
    app.include_router(market_recap.router)
    app.include_router(settings_api.router)
    app.include_router(strategy.router)
    app.include_router(signals.router)
    app.include_router(monitor_rules.router)
    app.include_router(lots.router)
    app.include_router(alerts.router)
    app.include_router(events.router)
    app.include_router(rps.router)
    app.include_router(sector_rotation.router)

    # 二次开发路由与小粒度策略在所有核心路由后注册, 禁止覆盖核心路径。
    extension_registry, extension_load_errors = configure_backend_extensions(app)
    app.state.extension_registry = extension_registry
    app.state.extension_load_errors = extension_load_errors


    # 能力门控异常 → 403(而非默认 500)
    # 业务代码用 capset.require(Cap.X) 断言能力,缺失时抛 CapabilityDenied;
    # 若不注册 handler 会冒泡成 500 Internal Server Error,对前端不友好且语义错误。
    from fastapi.responses import JSONResponse

    from app.services.ai_credits import CreditError

    @app.exception_handler(CreditError)
    async def handle_credit_error(request, exc):
        return JSONResponse(
            {"detail": exc.detail, "code": exc.code}, status_code=exc.status,
            headers={"Cache-Control": "no-store"},
        )

    from app.tickflow.capabilities import CapabilityDenied


    @app.get("/api/openapi.json", include_in_schema=False)
    async def openapi_contract_view(tier: str = "a"):
        """Tier A 契约视图: 只保留对外开放 (Token 可达) 的端点 = 稳定承诺面。

        开放清单的权威来源是 api_gateway 的规则表 — 规则表即契约, 单源维护。
        二开方以此生成客户端; 未出现在此视图的端点属内部实现, 随时变化。
        """
        spec = app.openapi()
        if tier == "a":
            from app.services import api_gateway

            kept_paths: dict = {}
            for path, ops in spec.get("paths", {}).items():
                kept_ops = {}
                for method, op in ops.items():
                    if method in ("get", "post", "put", "delete", "patch") and api_gateway.required_scope(
                        method.upper(), path,
                    ):
                        kept_ops[method] = op
                if kept_ops:
                    kept_paths[path] = kept_ops
            spec["paths"] = kept_paths
            spec["x-tier"] = "a"
        return JSONResponse(spec)


    @app.exception_handler(CapabilityDenied)
    async def capability_denied_handler(request: Request, exc: CapabilityDenied) -> JSONResponse:
        return JSONResponse(
            status_code=403,
            content={"detail": str(exc), "suggestion": exc.suggestion},
        )

    from app.services.pipeline_jobs import JobBusyError

    @app.exception_handler(JobBusyError)
    async def job_busy_handler(request: Request, exc: JobBusyError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    # 生产期静态文件(前端 dist)
    _static = Path(settings.static_dir)
    if _static.exists():
        if (_static / "assets").exists():
            app.mount("/assets", StaticFiles(directory=_static / "assets"), name="assets")

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa_fallback(full_path: str):
            """所有未匹配路径回退到 index.html — React Router 接管。

            index.html 禁止缓存 (Cache-Control: no-store), 确保浏览器每次拿到
            最新版本引用的 JS/CSS 文件名 (assets 带 hash, 可长缓存)。
            """
            index = _static / "index.html"
            if index.exists():
                return FileResponse(
                    index,
                    headers={"Cache-Control": "no-store, must-revalidate"},
                )
            return {"error": "frontend not built"}

    return app


app = create_app()
