---
generated_by: repo-map-first
authority_status: observed
---

# 仓库地图

本文用于定位 Tick Stock Panel 的实现、调用方和测试。核对日期为 2026-10-10，描述当前源码中的职责，不新增开发契约。贡献规则以 [CONTRIBUTING.md](../CONTRIBUTING.md) 为准；整体关系见 [架构说明](ARCHITECTURE.md)，启动和验证见 [开发导航](DEVELOPMENT.md)。

## 主要目录

| 位置 | 当前职责与定位入口 |
| --- | --- |
| `backend/app/main.py`、`collector.py` | `create_app` 构建 web／collector 角色；网页仅查询与转发，collector 独占后台服务；实例放在各自 `app.state` |
| `backend/app/api/` | 各领域 HTTP 和流式接口；`routes.py` 只定义健康与能力探测等基础接口，全部核心路由在 `main.py` 接线 |
| `backend/app/services/` | 同步、行情、筛选、扩展数据、任务管理、通知、偏好和认证等编排 |
| `backend/app/tickflow/` | TickFlow SDK、档位策略、限频与调度；`repository.py` 同时承载通用 `DataStore` 和 `KlineRepository` |
| `backend/app/data_providers/` | 标准化模型、Provider 协议、能力矩阵，以及 `custom/` 下的 YAML 源和插件加载 |
| `backend/app/plugins/` | 内置可选数据源插件，当前有 `fuyao/` 和 `stocksdk/` |
| `backend/app/indicators/` | enriched 数据生产、指标和信号计算、关键价位；核心在 `pipeline.py` |
| `backend/app/jobs/` | `daily_pipeline.py` 管理手动盘后管道、盘前基础维护及可选 AI 定时任务 |
| `backend/app/strategy/` | 策略加载执行、评分、信号、监控规则、持仓提醒、模拟盘；内置策略在 `builtin/` |
| `backend/app/backtest/` | 历史撮合、因子研究、矩阵、优化、步进验证、挖掘与独立 worker |
| `backend/app/factors/` | 因子注册、DSL、自定义因子持久化及扩展字段因子 |
| `backend/app/extensions/`、`backend/app/custom/` | 后端二开契约和加载器，以及实际定制模块；`custom/assistant/` 是 AI 助手实现 |
| `frontend/src/pages/` | 页面编排；路由由 `frontend/src/router.tsx` 定义，主外壳在 `components/Layout.tsx` |
| `frontend/src/components/` | 图表、个股详情、表格、弹窗和领域组件；看板组件注册在 `dashboard/registry.tsx` |
| `frontend/src/lib/` | API 客户端及类型、查询键、共享查询、实时订阅和任务状态 |
| `frontend/src/extensions/`、`frontend/src/custom/` | 前端二开注册、插槽、异常隔离，以及 AI 助手定制模块 |
| `backend/tests/`、`frontend/src/**/*.test.*` | 后端 pytest 与前端 Vitest；后端回测测试集中在 `tests/backtest/` |
| `examples/open-api/` | 开放接口客户端示例，通过 HTTP 消费后端能力 |
| `packaging/`、`Dockerfile`、`docker-compose.yml` | 桌面与容器打包；桌面入口为 `backend/app/desktop.py` |
| `.github/workflows/`、`dev.sh`、`dev.ps1`、`scripts/` | CI、发布、开发启动与升级预检；后端专项脚本另在 `backend/scripts/` |
| `docs/`、`brand/`、`screenshots/` | 领域文档、品牌素材与界面示例 |
| `data/` | 默认运行时数据根目录，受 `DATA_DIR` 配置影响；不属于可提交的业务源码 |

## 启动入口

- 网页：`app.main:app`（默认 `SERVICE_ROLE=web`）→ `_web_lifespan` → 只读 `DataStore`、`QuoteMirror`、collector 事件连接与 HTTP 转发。
- 采集：`app.collector:app` → `create_app("collector")` → 独占进程锁与 `_application_lifespan` → 数据源、行情、调度、普通／全量分钟、策略、监控及任务执行。两个进程共享 `DATA_DIR`，网页重启不触发采集任务恢复。
- 前端：`src/main.tsx` → `initializeFrontendExtensions` → 动态导入 `router.tsx` → `Layout` → 页面；首页 `/` 直接呈现 `pages/Dashboard.tsx`，访问认证由现有登录流程处理，共享 `QueryClient` 在 `main.tsx` 创建。
- 桌面：`app/desktop.py` 启动或复用独立 collector，再启动网页线程和 pywebview；关闭窗口仅退出网页，`--stop-collector` 显式停止采集。打包清单在 `packaging/tsp.spec`。

页面进入时读取已配置的凭据与能力路由，不再运行首次配置向导或自动选择数据源。凭据读取归属 `backend/app/secrets_store.py`，路由读取归属 `backend/app/services/preferences.py`；TickFlow / AI 密钥只在后端 `.env` 或历史本地凭据中配置，`api/settings.py` 不返回密钥片段并拒绝网页写入和清除；`pages/settings/Keys.tsx`、`AI.tsx` 仅显示密钥状态。能力路由和 AI 非密钥选项仍通过 settings API 保存。看板展示已有行情，无数据时由各组件显示原有空态；手动同步入口在 `frontend/src/pages/Data.tsx`，不再从看板提供首次取数弹窗或卡片。

## 从任务定位实现

下表后端文件以 `backend/app/` 或 `backend/tests/` 为基准，前端文件以 `frontend/src/` 为基准。测试是定位样例，不能替代检查调用方和实际影响范围。

| 要处理的行为 | 先读的入口与责任模块 | 可参考的测试 |
| --- | --- | --- |
| 双服务与更新传播 | `main.py::_web_lifespan`、`collector.py` → `services/collector_proxy.py`、`shared_runtime.py`；前端 `lib/useQuoteStream.ts` | `test_shared_runtime.py`、前端 `lib/useQuoteStream.test.tsx` |
| 行情纯本地查询 | `api/kline.py`、`api/indices.py` → 本机仓库／共享行情快照；`live` 不触发采集 | `test_local_market_reads.py` |
| 普通分时、显式任务与取消 | `services/ordinary_minute.py` → 股票／ETF／指数分钟分区；`services/pipeline_jobs.py`、`api/pipeline.py`；数据页 `components/data/MinuteSyncConfig.tsx`、`FinancialSyncPanel.tsx` 复用现有任务 API 与 `lib/useCollectorJob.ts`；业务分时图只读 | 前端 `components/data/MaintenanceControls.test.tsx`、`components/StockMultiDayIntradayChart.test.tsx`、`pages/Financials.test.tsx` |
| 当日复盘发布与 AI 归档 | `services/review_publication.py`、`analysis_archive.py` → `api/market_recap.py`、`financials.py`、`stock_analysis.py`；前端 `pages/Review.tsx`、分析报告 store | 前端 `pages/Review.test.tsx`、`lib/analysisArchival.test.ts` |
| 数据源路由或能力门控 | `api/settings.py` → `services/preferences.py`、`data_providers/capabilities.py`、`data_providers/custom/loader.py` | `test_capability_matrix.py`、`test_capability_augment.py`、`test_full_minute_capability.py` |
| fuyao 插件取数与归一化 | `plugins/fuyao/plugin.yaml`、`provider.py`、`client.py` → 自定义源路由及专有服务 | `test_fuyao_provider.py`、`test_fuyao_daily_streaming_sync.py`、`test_fuyao_financial.py` |
| 日线、除权、分钟同步 | 前端手动入口 `pages/Data.tsx` → `api/kline.py`、`api/pipeline.py` → `services/kline_sync.py`、`jobs/daily_pipeline.py` | `test_daily_batch_asset.py`、`test_adj_factor_standalone_sync.py`、`test_minute_routing.py`、`test_minute_timezone_contract.py` |
| 指标、历史读取或缓存 | `indicators/pipeline.py`、`tickflow/repository.py`；schema 在 `parquet.py`，版本发布在 `enriched_generation.py` | `test_enriched_generation.py`、`test_parquet_schema_compat.py`、`test_live_enriched_std_parity.py` |
| 盘中行情或图表刷新 | `services/quote_service.py`、`services/minute_refresh.py`、`api/intraday.py` → 前端 `lib/useQuoteStream.ts`、`lib/kline.ts` | `test_realtime_turnover_rate.py`、`test_quote_index_merge.py`、`test_kline_daily_latest.py` |
| 采集时段与盘后手动模式 | `jobs/daily_pipeline.py::start_scheduler` 保留日历、09:10 基础资料及能力维护；`main.py` 在启动日历维护后调用 `run_scheduled_instruments_sync`，以 `instrument_sync.instruments_synced_on` 的本地北京日期去重并补跑；`quote_service.py`、`depth_service.py` 判断交易时段，数据页负责盘后显式同步；周度研究在手动全量同步成功后触发 | `test_instruments_startup.py`、`test_manual_collection_schedule.py`、`test_realtime_polling_schedule.py`、`test_pipeline_capacity.py` |
| 手动重复拉取提示与普通分钟开关 | `api/data.py::sync_check` → `services/collection_status.py` 只核对本地任务与文件；`api/pipeline.py` 在既有任务结果记录路由／范围，`pipeline_jobs.py` 摘要保留 `request_key`；前端 `lib/useCollectionSyncConfirmation.tsx` 确认后调用原接口；`ordinary_minute_enabled` 仅控制普通分钟自动采集 | `test_collection_sync_check.py`、`test_ordinary_minute.py`；前端 `lib/useCollectionSyncConfirmation.test.tsx`、`components/data/MaintenanceControls.test.tsx` |
| 数据页按模块同步 | `pages/Data.tsx` 提供行情与基础数据、分钟历史、财务、板块分类四个主入口；前三项复用原聚合任务，板块走 `api/ext_data.py::sync_classifications` → `ext_presets.sync_classifications`，共用 `api/pipeline.py::_submit_job`；逐表／单股操作保留在维护选项 | `test_classification_sync_jobs.py`、`test_collection_sync_check.py`；前端 `components/data/MaintenanceControls.test.tsx` |
| 自定义财务同步进度与并行 | `services/financial_sync.py::_fetch_table` 接入显式 `on_chunk_done` 原生进度，旧插件仍按 100 股调用；Generic HTTP 保留配置批长与共享 RPM；Fuyao 财务取数有界并行，`client.py::_get` 共用进程请求间距和在途上限，限流向任务传播；取消沿用 JobStore 信号 | `test_financial_progress.py`、`test_fuyao_financial.py`、`test_fuyao_financial_concurrency.py`、`test_collector_data_jobs.py` |
| 财务请求股票池 | `services/financial_sync.py::_get_symbols` 使用可选 `get_financial_universe` 核对并选择供应商当前全市场代码（含本地维表缺少的代码），将双向差异保存到 `financials/universe.json`；Fuyao 从全市场快照提供清单，旧插件沿用本地维表，历史报表继续合并保留 | `test_financial_universe.py` |
| 交易日历或显式历史缺口检查 | `services/trading_day.py::sync_calendar` 由 collector 保存日历，`is_trading_day` 只读；历史检查仍在 `services/data_integrity.py` | `test_trading_day.py`、`test_data_integrity.py` |
| 策略筛选、参数或评分 | `api/screener.py`、`api/strategy.py` → `services/screener.py`、`strategy/engine.py`、`strategy/scoring.py`、`strategy/config.py` | `test_strategy_registry.py`、`test_strategy_saved_params.py`、`test_strategy_scoring.py`、`test_screener_cache_api.py` |
| 策略回测、优化或步进验证 | `api/backtest.py` → `backtest/worker.py` → `backtest/strategy.py`、`engine.py`、`matrix.py`、`optimizer.py`、`walkforward.py`；本地分钟成交研究不受当前供应商采集能力限制 | `test_backtest_local_minute_api.py`、`backtest/test_strategy_backtest_correctness.py`、`backtest/test_worker_process.py`、`backtest/test_walkforward.py` |
| 因子和挖掘 | `api/factors.py`、`api/mining.py` → `factors/`、`backtest/factor.py`、`backtest/mining.py`、`services/mining_manager.py` | `test_factor_dsl.py`、`test_factor_registry.py`、`test_mining_jobs.py`、`test_mining_manager.py` |
| 监控规则、告警与通知 | `api/monitor_rules.py` → `strategy/monitor_rules.py`、`strategy/monitor.py`；行情驱动评估，通知通道在 `services/*_adapter.py` | `test_monitor_group_scope.py`、`test_monitor_etf.py`、`test_monitor_index.py`、`test_strategy_monitor_events.py` |
| 自选、持仓提醒或模拟盘 | `api/watchlist.py`、`api/lots.py`、`api/paper.py` → `services/watchlist.py`、`strategy/lots.py`、`strategy/paper.py`、`strategy/paper_auto.py` | `test_watchlist_groups.py`、`test_lots.py`、`test_paper_trading.py`、`test_paper_t1_available_at_order.py` |
| 扩展数据与分析页面 | 数据页板块分类模块统一同步概念／行业，`components/ext-data/ExtDataStatCard.tsx` 保留单项维护及自定义扩展配置；`api/ext_data.py`、`api/analysis.py` → `services/ext_data.py`、`services/ext_pull.py`；业务 `pages/AnalysisDetail.tsx` 与 `components/ExtDimensionAnalysis.tsx` 只读 | `test_ext_rows_query.py`、`test_ext_sql_safety.py`、`test_ext_pull_refresh.py`、`test_collector_maintenance_publication.py`、`test_classification_sync_jobs.py` |
| 市场环境、板块、异动或复盘 | 对应 `api/regime.py`、`sector_rotation.py`、`abnormal.py`、`market_recap.py` → `services/` 同领域构建器和分析器 | `test_regime_builder.py`、`test_market_phase.py`、`test_sector_rotation.py`、`test_abnormal_moves.py` |
| 龙虎榜、竞价风向标及复盘摘要 | `services/dragon_tiger.py`、`auction_benchmark.py` 由后台获取并保存；`api/market_recap.py` 的 GET 只读，页面在 `pages/Review.tsx`、`AbnormalMoves.tsx`、`Dashboard.tsx` | `test_dragon_tiger.py`、`test_auction_benchmark.py` |
| 财务与个股分析 | 数据页 `FinancialSyncPanel.tsx` 负责五表采集，`pages/Financials.tsx` 读取本地财务与历史报告；`api/financials.py` 的 `/status` 在 web 读取本地覆盖画像，`/execution-status` 转交 collector 查询任务状态；`api/stock_analysis.py`、`services/financial_sync.py`、分析服务沿用原链路，本地读取不受供应商能力门控 | `test_financials_local_api.py`、`test_financial_review_regressions.py`、`test_financial_shares.py`、`test_fuyao_financial.py`、`test_stock_analyzer_index.py` |
| 登录、Token 或开放事件 | `main.py::auth_middleware` → `services/auth.py`、`api_tokens.py`、`api_gateway.py`；`api/events.py` → `event_tickets.py`、`events.py` | `test_auth_env_bootstrap.py`、`test_api_gateway.py`、`test_openapi_contract.py`、`test_events.py` |
| 页面、请求或二开 | 前端 `router.tsx`、`lib/api.ts`、`lib/queryKeys.ts`、`extensions/`；后端 `extensions/` 和 `custom/` | 前端相邻 `*.test.*`；后端 `test_extensions.py`、`test_custom_assistant.py`、`test_assistant_actions.py` |

## 关键调用链

```text
盘后同步：web api/pipeline → collector_proxy → collector pipeline_jobs → daily_pipeline.run_now
          → instrument_sync / kline_sync → Parquet
          → indicators.run_pipeline → enriched 分区 → 仓库视图与缓存刷新
          → review_publication / shared_runtime.publish_data_update → web 缓存与页面失效

筛选：api/screener → ScreenerService.build_strategy_context
      → StrategyEngine.run → 过滤、评分、排序 → 策略结果缓存 / 响应

盘中：collector QuoteService → 当前实时 Provider → 标准行情
      → 当日日线与 enriched 更新 → 监控评估 / 模拟盘撮合
      → shared_runtime.publish_quotes → 共享快照 → web QuoteMirror
      → 页面 SSE → useQuoteStream → 查询失效或 K 线尾部更新

普通分时：collector OrdinaryMinuteService → 自选 / 启用监控 / 核心四指数
          → 资产分类分钟分区 → data_updated(minute) → 本地查询

复盘：盘后管道成功 → review_publication.publish_review → 当日快照
      → GET /api/market-recap/publication → 已发布摘要或未更新提示
AI报告：显式分析请求 → collector analysis_archive → 报告保存 → done(archived, report_id)

策略研究：api/backtest → spawn worker → 独立 DataStore / Repository / StrategyEngine
          → StrategyBacktestService → BacktestEngine → 任务进度 / 结果

扩展数据：上传 / HTTP 拉取 / ingest → ExtConfig 映射 → 扩展 Parquet
          → 派生因子和缓存失效 → 分析接口 / 声明式页面
```

`api/backtest.py` 还保留 `services/backtest.py::BacktestService` 的信号回测路径，不应把所有回测入口都认作同一个策略 worker 流程。数据源同样有两条解析入口：`data_providers/registry.py::get_provider` 当前处理 TickFlow，YAML 源和插件由 `data_providers/custom/loader.py` 管理。

## 契约与扩展入口

| 契约 | 定义位置 |
| --- | --- |
| Provider 方法与字段 | [base.py](../backend/app/data_providers/base.py)、[schemas.py](../backend/app/data_providers/schemas.py)、[自定义数据源文档](custom-data-source.md)、[插件文档](plugin-development.md) |
| 能力路由 | [capabilities.py](../backend/app/data_providers/capabilities.py) 的 `CAPABILITY_REGISTRY` 与 `build_capability_matrix`；偏好由 settings API 注入 |
| 原始与 enriched 存储字段 | [parquet.py](../backend/app/parquet.py)、[pipeline.py](../backend/app/indicators/pipeline.py) 的 `ENRICHED_STORAGE_COLS` |
| 策略文件与执行上下文 | [engine.py](../backend/app/strategy/engine.py)、[market_data.py](../backend/app/strategy/market_data.py)、[策略开发规范](../backend/app/strategy/prompts/strategy-guide.md) |
| 前后端请求与类型 | `backend/app/api/` 的路由及请求模型、[frontend/src/lib/api.ts](../frontend/src/lib/api.ts)；类型在两端分别维护 |
| 对外开放面 | [api_gateway.py](../backend/app/services/api_gateway.py) 的规则表、`/api/openapi.json?tier=a`、[开放契约测试](../backend/tests/test_openapi_contract.py) |
| 前端二开 | [extensions/types.ts](../frontend/src/extensions/types.ts)、[registry.ts](../frontend/src/extensions/registry.ts)、[前端模板](../frontend/src/custom/_template/extension.tsx.example) |
| 后端二开 | [extensions/contracts.py](../backend/app/extensions/contracts.py)、[registry.py](../backend/app/extensions/registry.py)、[后端模板](../backend/app/custom/_template.py.example) |

源码扩展、数据源插件、用户策略文件、扩展数据表是不同机制，不能互换注册入口。能力与数据单位的长期要求仍以贡献规范和对应领域文档为准。
