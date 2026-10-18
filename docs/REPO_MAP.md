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
| `backend/app/jobs/` | `daily_pipeline.py` 管理交易日 16:00 自动／手动盘后管道、盘前基础维护及可选 AI 定时任务 |
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
| 普通分时、显式任务与取消 | `services/ordinary_minute.py` → 股票／ETF／指数分钟分区；`services/pipeline_jobs.py`、`api/pipeline.py`；数据页 `components/data/MinuteSyncConfig.tsx` 复用现有任务 API 与 `lib/useCollectorJob.ts`；财务按需入口见下文；业务分时图只读 | 前端 `components/data/MaintenanceControls.test.tsx`、`components/StockMultiDayIntradayChart.test.tsx`、`pages/Financials.test.tsx` |
| 当日复盘发布与 AI 归档 | `services/review_publication.py`、`analysis_archive.py` → `api/market_recap.py`、`financials.py`、`stock_analysis.py`；前端 `pages/Review.tsx`、分析报告 store | 前端 `pages/Review.test.tsx`、`lib/analysisArchival.test.ts` |
| 数据源路由或能力门控 | `api/settings.py` → `services/preferences.py`、`data_providers/capabilities.py`、`data_providers/custom/loader.py` | `test_capability_matrix.py`、`test_capability_augment.py`、`test_full_minute_capability.py` |
| fuyao 插件取数与归一化 | `plugins/fuyao/plugin.yaml`、`provider.py`、`client.py` → 自定义源路由及专有服务 | `test_fuyao_provider.py`、`test_fuyao_daily_streaming_sync.py`、`test_fuyao_financial.py` |
| 日线、除权、分钟同步 | 前端手动入口 `pages/Data.tsx` → `api/kline.py`、`api/pipeline.py` → `services/kline_sync.py`、`jobs/daily_pipeline.py` | `test_daily_batch_asset.py`、`test_adj_factor_standalone_sync.py`、`test_minute_routing.py`、`test_minute_timezone_contract.py` |
| 指标、历史读取或缓存 | `indicators/pipeline.py`、`tickflow/repository.py`；schema 在 `parquet.py`，版本发布在 `enriched_generation.py` | `test_enriched_generation.py`、`test_parquet_schema_compat.py`、`test_live_enriched_std_parity.py` |
| 盘中行情或图表刷新 | `services/quote_service.py`、`services/minute_refresh.py`、`api/intraday.py` → 前端 `lib/useQuoteStream.ts`、`lib/kline.ts` | `test_realtime_turnover_rate.py`、`test_quote_index_merge.py`、`test_kline_daily_latest.py` |
| 采集时段与盘后自动顺序 | `jobs/daily_pipeline.py::start_scheduler` → `services/postmarket.py` 判断交易日和当日成功证据 → 既有 pipeline 任务边界 → `run_now(postmarket=True)`；`daily_coverage.py` 验收正式日线，指标落盘核验后才计算环境；顺序约定见 [postmarket-pipeline.md](postmarket-pipeline.md)。09:10 基础资料及启动补跑保留，周度研究在成功并释放执行槽后触发 | `test_postmarket_pipeline.py`、`test_daily_sync_coverage.py`、`test_instruments_startup.py`、`test_manual_collection_schedule.py`、`test_pipeline_capacity.py` |
| 手动重复拉取提示与普通分钟开关 | `api/data.py::sync_check` → `services/collection_status.py` 只核对本地任务与文件；`api/pipeline.py` 在既有任务结果记录路由／范围，`pipeline_jobs.py` 摘要保留 `request_key`；前端 `lib/useCollectionSyncConfirmation.tsx` 确认后调用原接口；`ordinary_minute_enabled` 仅控制普通分钟自动采集 | `test_collection_sync_check.py`、`test_ordinary_minute.py`；前端 `lib/useCollectionSyncConfirmation.test.tsx`、`components/data/MaintenanceControls.test.tsx` |
| 数据页按模块同步 | `pages/Data.tsx` 提供行情与基础数据、分钟历史、板块分类三个同步入口，财务仅展示本地覆盖和单股获取导航；行情与分钟复用原聚合任务，板块走 `api/ext_data.py::sync_classifications` → `ext_presets.sync_classifications`，共用 `api/pipeline.py::_submit_job`；逐表／单股操作保留在维护选项 | `test_classification_sync_jobs.py`、`test_collection_sync_check.py`；前端 `components/data/MaintenanceControls.test.tsx` |
| 财务按需采集与并发复用 | `api/financials.py::fetch_financials` → `services/financial_demand.py::fetch_stock_history` → `fetch_stock`、`_fetch_table`、版本库和浏览表；页面默认近两年，按各表 `history_start` 向前每次补一年；collector 按源／股票／表／日期范围共享在途结果与持久化边界，写入串行重新合并；显式计算任务保留原历史准备；所有登录用户可单股获取，旧 HTTP 全量同步返回 410；Fuyao 支持表由 `financial_tables` 声明 | `test_financial_demand.py`、`test_financial_history.py`、`test_fuyao_financial_history.py`、`FinancialFetch.test.tsx` |
| 策略与因子财务准备 | 显式策略命令通过 `ScreenerService.build_strategy_context(prepare_financials=True)` 准备依赖并附加点时因子；因子面板及公式试算／保存前校验通过 `financial_demand.prepare_factor_frame` 准备对应表并附加时间门控输入；`backtest/worker.py::run_worker_task` 在父 collector 为回测、优化、步进与挖掘准备输入，子进程仅计算；断层先做价格缺口必要条件筛选；GET 与实时监控不采集 | `test_financial_preparation.py`、`test_fundamental_factors.py` |
| 旧内部批次财务股票池（按需入口不调用） | `services/financial_sync.py::_get_symbols` 使用可选 `get_financial_universe` 核对并选择供应商当前全市场代码（含本地维表缺少的代码），将双向差异保存到 `financials/universe.json`；Fuyao 从全市场快照提供清单，旧插件沿用本地维表，历史报表继续合并保留 | `test_financial_universe.py` |
| 财务版本与回测时间 | `services/financial_sync.py` → `financial_versions.py` 保存完整版本与更新日／观察日依据，浏览表每期保留完整记录；`backtest/fundamentals.py` 重建同源指标时间线，共用于面板、矩阵与挖掘，按版本可用日后首个交易日门控；策略评分与排名排除缺失财务输入 | `test_financial_versions.py`、`test_fundamental_factors.py`、`test_strategy_scoring.py`、`backtest/test_factor_rank_research.py` |
| 业绩断层主图指标 | `StockDailyKChart.tsx` 的独立开关 → `api/kline.py::get_earnings_breakout` → `services/earnings_breakout.py` 读取同源利润表版本并固定首次可用财报事件 → `indicators/earnings_breakout.py` 计算断层、9/42；仓库按标的下推版本与前复权价格查询，GET 不采集、不写盘 | `test_earnings_breakout.py`、`StockDailyKChart.test.tsx`、`EChartsCandlestick.test.tsx` |
| 业绩断层回踩选股 | `strategy/builtin/earnings_breakout_pullback.py` → `strategy/market_data.py::get_earnings_breakout_signals` → `services/earnings_breakout.py::filter_pullback_signals` 批量读取利润表版本、完整前序日线并复用主图算法；策略引擎只取目标日9或42，双信号去重。仅手动日线筛选，`realtime_supported=false` 阻止实时监控 | `test_earnings_breakout_strategy.py`、`test_strategy_monitor_events.py` |
| 业绩断层跳空选股 | 独立内置 `earnings_breakout_gap.py` → `strategy/market_data.py::get_earnings_gap_signals` → `services/earnings_breakout.py::filter_gap_signals`；与回踩共享财报版本及批量价格读取，只筛目标日 `earnings_gap`，对应K线“断”标记，不要求回踩附加条件；保留回踩策略与指标 | `test_earnings_gap_strategy.py`、`test_earnings_breakout_strategy.py` |
| 跳空候选财务核验 | 最新交易日手动卡片 → 管理员 `POST /api/screener/earnings-gap/verify` → JobStore → `services/earnings_gap_review.py::find_gap_candidates` 先筛价格 → `financial_sync.sync_candidate_income` 通过共享按需服务更新候选扶摇利润表 → `review_candidates` 复用财报版本时间核验；`verification` 独立保存待核验，确认股才进入命中；不修改全市场断点／成功时间，GET、普通重算和回测不采集 | `test_earnings_gap_review.py`、`test_financial_candidate_sync.py`、`test_earnings_gap_verification_job.py`、`test_strategy_cache.py`、`EarningsGapVerification.test.tsx` |
| 未回补价格缺口区域 | `StockDailyKChart.tsx` 默认展示 → `api/kline.py::get_price_gaps` → `services/price_gaps.py` 读取截止日前全部已存前复权日线及实时尾部 → `indicators/price_gaps.py` 计算向上／向下缺口剩余区间；`EChartsCandlestick.tsx` 用价格带延伸到视图右侧，部分回补收窄、完全回补移除，日线 SSE 失效对应查询 | `test_price_gaps.py`、`StockDailyKChart.test.tsx`、`EChartsCandlestick.test.tsx` |
| 交易日历或显式历史缺口检查 | `services/trading_day.py::sync_calendar` 由 collector 保存日历并累积重叠区间已确认日期（支持财报事件回放），`is_trading_day` 只读；历史检查仍在 `services/data_integrity.py` | `test_trading_day.py`、`test_data_integrity.py` |
| 策略筛选、参数或评分 | `api/screener.py`、`api/strategy.py` → `services/screener.py`、`strategy/engine.py`、`strategy/scoring.py`、`strategy/overlay.py`、`strategy/config.py` | `test_strategy_registry.py`、`test_strategy_saved_params.py`、`test_strategy_overlay.py`、`test_strategy_scoring.py`、`test_screener_cache_api.py` |
| 策略回测、优化或步进验证 | `api/backtest.py` → `backtest/worker.py` → `backtest/strategy.py`、`engine.py`、`matrix.py`、`optimizer.py`、`walkforward.py`；本地分钟成交研究不受当前供应商采集能力限制 | `test_backtest_local_minute_api.py`、`backtest/test_strategy_backtest_correctness.py`、`backtest/test_worker_process.py`、`backtest/test_walkforward.py` |
| 因子和挖掘 | `api/factors.py`、`api/mining.py` → `factors/`、`backtest/factor.py`、`backtest/mining.py`、`services/mining_manager.py` | `test_factor_dsl.py`、`test_factor_registry.py`、`test_mining_jobs.py`、`test_mining_manager.py` |
| 监控规则、告警与通知 | `api/monitor_rules.py` → `strategy/monitor_rules.py`、`strategy/monitor.py`；行情驱动评估，通知通道在 `services/*_adapter.py` | `test_monitor_group_scope.py`、`test_monitor_etf.py`、`test_monitor_index.py`、`test_strategy_monitor_events.py` |
| 自选、持仓提醒或模拟盘 | `api/watchlist.py`、`api/lots.py`、`api/paper.py` → `services/watchlist.py`、`strategy/lots.py`、`strategy/paper.py`、`strategy/paper_auto.py` | `test_watchlist_groups.py`、`test_lots.py`、`test_paper_trading.py`、`test_paper_t1_available_at_order.py` |
| 扩展数据与分析页面 | 数据页板块分类模块统一同步概念／行业，`components/ext-data/ExtDataStatCard.tsx` 保留单项维护及自定义扩展配置；`api/ext_data.py`、`api/analysis.py` → `services/ext_data.py`、`services/ext_pull.py`；业务 `pages/AnalysisDetail.tsx` 与 `components/ExtDimensionAnalysis.tsx` 只读 | `test_ext_rows_query.py`、`test_ext_sql_safety.py`、`test_ext_pull_refresh.py`、`test_collector_maintenance_publication.py`、`test_classification_sync_jobs.py` |
| 市场环境、板块、异动或复盘 | 对应 `api/regime.py`、`sector_rotation.py`、`abnormal.py`、`market_recap.py` → `services/` 同领域构建器和分析器；前端 `pages/Regime.tsx` 在历史为空时提供管理员“环境分析”入口，复用环境重算接口 | `test_regime_builder.py`、`test_market_phase.py`、`test_sector_rotation.py`、`test_abnormal_moves.py`；前端 `pages/Regime.test.tsx` |
| 龙虎榜、竞价风向标及复盘摘要 | `services/dragon_tiger.py`、`auction_benchmark.py` 由后台获取并保存；`api/market_recap.py` 的 GET 只读，页面在 `pages/Review.tsx`、`AbnormalMoves.tsx`、`Dashboard.tsx` | `test_dragon_tiger.py`、`test_auction_benchmark.py` |
| 财务与个股分析 | 数据页 `FinancialSyncPanel.tsx` 展示覆盖并导航至财务页，`pages/Financials.tsx` 即使财务库全空也允许搜索、按股按表获取和浏览历史报告；`api/financials.py` 的 `/status` 在 web 读取本地覆盖画像，`/execution-status` 转交 collector 查询任务状态；`api/stock_analysis.py`、`services/financial_sync.py`、分析服务沿用原链路，本地读取不受供应商能力门控 | `test_financials_local_api.py`、`test_financial_review_regressions.py`、`test_financial_shares.py`、`test_fuyao_financial.py`、`test_stock_analyzer_index.py` |
| 账号、权限与个人归属 | `services/auth_access.py` → `auth.py`、`user_context.py`；`api/users.py` 管理账号；`user_runtime.py`、`private_storage.py`、`user_migration.py` 管用户运行时、路径和旧数据归属；`business_access.py` 管业务权限 | `test_multiuser_auth.py`、`test_multiuser_ai.py`、`test_user_private_stores.py`、`test_user_runtime_boundaries.py`、`test_research_ownership.py` |
| 页面、请求或二开 | 前端 `router.tsx`、`lib/api.ts`、`lib/queryKeys.ts`、`extensions/`；后端 `extensions/` 和 `custom/` | 前端相邻 `*.test.*`；后端 `test_extensions.py`、`test_custom_assistant.py`、`test_assistant_actions.py` |

## 关键调用链

预发自动发布入口（2026-10-18）：`.github/workflows/ci.yml` 在 `push main` 的后端测试与前端构建成功后调用 `scripts/ci_deploy.sh`，上传指定提交源码并通过 SSH 执行 `scripts/deploy_preproduction.sh`。后者在远端 `releases/<SHA>/` 构建镜像，保留运行根目录的数据与配置，完成双服务健康验收或尝试恢复旧版本；`scripts/preproduction_compose.sh` 管理当前已发布版本。回归入口为 `backend/tests/test_ci_deploy.py` 和 `backend/tests/test_preproduction_deploy.py`，操作说明见 [部署指南](deployment.md#github-自动发布预发)。

```text
盘后同步：web api/pipeline → collector_proxy → collector pipeline_jobs → daily_pipeline.run_now
          → instrument_sync / kline_sync → Parquet
          → indicators.run_pipeline → enriched 分区 → 仓库视图与缓存刷新
          → review_publication / shared_runtime.publish_data_update → web 缓存与页面失效

盘后自动：collector scheduler(默认北京时间16:00；本地日历确认交易日)
          → postmarket.run_scheduled → 复用 pipeline 任务与写入槽 → run_now(postmarket=True)
          → 正式日线／因子／指标／核心指数验收 → 分类 → 环境／情绪／主线
          → 模拟盘／可选分钟／盘口／榜单 → 复盘发布 → 释放槽后按开关触发周度研究

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

### 页面缩放与模拟盘移植入口

- 页面缩放：`frontend/src/lib/pageScale.ts` 负责根节点缩放与坐标比例；`pageZoom.ts` 和 `sessionRuntime.ts` 共用该实现，偏好仍按登录账户隔离。看板网格、助手及分析气泡按此比例换算拖拽坐标。
- 模拟盘：`frontend/src/pages/Paper.tsx` 保持正式路由，复用 `api/paper.py` 与用户私有台账；账户删除、回合与实时对比覆盖于 `test_paper_account_delete.py`，持仓上限与自动方向覆盖于 `test_paper_auto_max_positions.py`。
