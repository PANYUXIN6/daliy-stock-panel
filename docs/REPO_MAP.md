---
generated_by: repo-map-first
authority_status: observed
---

# 仓库地图

本文用于定位实现、调用方和测试；维护规则与数据契约见 [CONTRIBUTING.md](../CONTRIBUTING.md)，整体关系见 [架构说明](ARCHITECTURE.md)，启动和验证见 [开发导航](DEVELOPMENT.md)。

## 主要目录

| 位置 | 当前职责与定位入口 |
| --- | --- |
| `backend/app/main.py`、`collector.py` | `create_app` 构建 web／collector 角色；网页仅查询与转发，collector 独占后台服务；实例放在各自 `app.state` |
| `backend/app/api/` | 各领域 HTTP 和流式接口；`routes.py` 只定义健康与能力探测等基础接口，全部核心路由在 `main.py` 接线 |
| `backend/app/services/` | 同步、行情、筛选、扩展数据、任务管理、通知、偏好和认证等编排 |
| `backend/app/tickflow/` | TickFlow SDK、档位策略与请求限速；`repository.py` 同时承载通用 `DataStore` 和 `KlineRepository` |
| `backend/app/data_providers/` | 标准化模型、Provider 协议、能力矩阵，以及 `custom/` 下的 YAML 源和插件加载 |
| `backend/app/plugins/` | 内置可选数据源插件，当前有 `fuyao/` 和 `stocksdk/` |
| `backend/app/indicators/` | enriched 数据生产、指标和信号计算、关键价位；核心在 `pipeline.py` |
| `backend/app/jobs/` | `daily_pipeline.py` 管理交易日 16:00 自动／手动盘后管道、盘前基础维护及可选 AI 定时任务 |
| `backend/app/strategy/` | 策略加载执行、评分、信号、监控规则、持仓提醒、模拟盘；内置策略在 `builtin/` |
| `backend/app/backtest/` | 历史撮合、因子研究、矩阵、优化、步进验证、挖掘与独立 worker |
| `backend/app/factors/` | 因子注册、DSL、自定义因子持久化及扩展字段因子 |
| `backend/app/extensions/`、`backend/app/custom/` | 后端扩展契约和加载器，以及实际定制模块；`custom/assistant/` 是 AI 助手实现 |
| `frontend/src/pages/` | 页面编排；路由由 `frontend/src/router.tsx` 定义，主外壳在 `components/Layout.tsx` |
| `frontend/src/components/` | 图表、个股详情、表格、弹窗和领域组件；看板组件注册在 `dashboard/registry.tsx` |
| `frontend/src/lib/` | API 客户端及类型、查询键、共享查询、实时订阅和任务状态 |
| `frontend/src/extensions/`、`frontend/src/custom/` | 前端扩展注册、插槽、异常隔离，以及 AI 助手定制模块 |
| `backend/tests/`、`frontend/src/**/*.test.*` | 后端 pytest 与前端 Vitest；后端回测测试集中在 `tests/backtest/` |
| `Dockerfile`、`docker-compose.yml` | Linux 服务器镜像与 web / collector 双服务运行定义 |
| `.github/workflows/`、`dev.sh`、`scripts/` | CI、服务器发布与 macOS / Linux 开发启动；后端专项脚本另在 `backend/scripts/` |
| `docs/`、`brand/`、`screenshots/` | 领域文档、品牌素材与界面示例 |
| `data/` | 默认运行时数据根目录，受 `DATA_DIR` 配置影响；不属于可提交的业务源码 |

## 启动入口

- 网页：`app.main:app`（默认 `SERVICE_ROLE=web`）→ `_web_lifespan` → 只读 `DataStore`、`QuoteMirror`、collector 事件连接与 HTTP 转发。
- 采集：`app.collector:app` → `create_app("collector")` → 独占进程锁与 `_application_lifespan` → 数据源、行情、调度、普通／全量分钟、策略、监控及任务执行。两个进程共享 `DATA_DIR`，网页重启不触发采集任务恢复。
- 前端：`src/main.tsx` → `initializeFrontendExtensions` → 动态导入 `router.tsx` → `Layout` → 页面；首页 `/` 直接呈现 `pages/Dashboard.tsx`，访问认证由现有登录流程处理，共享 `QueryClient` 在 `main.tsx` 创建。

页面读取已配置的凭据与能力路由。凭据归属 `backend/app/secrets_store.py`，路由归属 `backend/app/services/preferences.py`；TickFlow / AI 密钥在后端配置，`api/settings.py` 仅返回配置状态并拒绝网页写入和清除。能力路由和 AI 非密钥选项通过 settings API 保存；手动数据同步集中在 `frontend/src/pages/Data.tsx`。

## 从任务定位实现

下表后端文件以 `backend/app/` 或 `backend/tests/` 为基准，前端文件以 `frontend/src/` 为基准。测试是定位样例，不能替代检查调用方和实际影响范围。

| 要处理的行为 | 先读的入口与责任模块 | 可参考的测试 |
| --- | --- | --- |
| 双服务与更新传播 | `main.py::_web_lifespan`、`collector.py` → `services/collector_proxy.py`、`shared_runtime.py`；前端 `lib/useQuoteStream.ts` | `test_shared_runtime.py`、前端 `lib/useQuoteStream.test.tsx` |
| 行情纯本地查询 | `api/kline.py`、`api/indices.py` → 本机仓库／共享行情快照；`live` 不触发采集 | `test_local_market_reads.py` |
| 普通分时、显式任务与取消 | `services/ordinary_minute.py` → 股票／ETF／指数分钟分区；`services/pipeline_jobs.py`、`api/pipeline.py`；数据页 `components/data/MinuteSyncConfig.tsx` 复用现有任务 API 与 `lib/useCollectorJob.ts`；单日分时空态 `components/StockIntradayChart.tsx`／`MinuteCollectionButton.tsx` → `POST /api/kline/minute/fetch` 显式补采所选标的和日期，在途共享任务、锁内复查已有数据，专用进度 GET 仅暴露分钟补采任务；浏览与刷新仍只读；财务按需入口见下文 | `test_minute_fetch_api.py`、`test_minute_fetch_access.py`；前端 `components/StockIntradayChart.test.tsx`、`components/data/MaintenanceControls.test.tsx`、`components/StockMultiDayIntradayChart.test.tsx`、`lib/minuteBatchIncremental.test.ts`、`pages/Financials.test.tsx` |
| 当日复盘发布与 AI 归档 | `services/review_publication.py`、`analysis_archive.py` → `api/market_recap.py`、`financials.py`、`stock_analysis.py`；前端 `pages/Review.tsx`、分析报告 store | 前端 `pages/Review.test.tsx`、`lib/analysisArchival.test.ts` |
| 板块复盘：事件共振／调整逆势 | `api/sector_review.py` → `services/sector_review.py`、`sector_review_rules.py` → `tickflow/sector_review_reader.py`；`KlineRepository.sector_review_reader` 只读正式原始日线与同版前复权收盘，不预热指标；前端独立 `pages/SectorReview.tsx`、`components/sector-review/` | `test_sector_review.py`；`pages/SectorReview.test.tsx`；规模脚本 `scripts/benchmark_sector_review.py`；[实现与验证记录](sector-review-implementation.md) |
| 个人复盘提示词 | `api/market_recap.py::get_recap_prompt/update_recap_prompt` → `services/recap_prompt.py`、个人 preferences；`market_recap.py` 生成时固定模板快照；前端 `ReviewPromptDialog.tsx`，Pro／admin 可编辑 | `test_market_recap_prompt.py`；复盘页面、提示词弹窗与归档测试 |
| 指标发布中断恢复 | `enriched_generation.py` 的发布锁和写入标记 → `indicators/pipeline.py::run_pipeline` 异常释放与完整重建，collector 启动恢复未写入状态 | `test_enriched_generation.py`、`test_enriched_full_rebuild.py`、`test_data_clear_generation.py` |
| 盘中与重算量比一致性 | `indicators/volume_ratio.py` → `compute_indicators` 与 `compute_enriched_today`，按行行情时间确定已交易分钟数 | `test_vol_ratio_intraday_rebuild.py`、`test_live_enriched_std_parity.py` |
| 列表实时价格与迷你图 | `services/screener_quotes.py` 在选股响应副本叠加同日展示行情；`stock-table/MiniIntraday.tsx` 绘制量带；`api/kline.py::get_daily_batch` 为根数不足的标的有界补读本地历史 | `test_screener_cache_api.py`、`test_kline_daily_batch_backfill.py`、`MiniIntraday.test.tsx` |
| 数据源路由或能力门控 | `api/settings.py` → `services/preferences.py`、`data_providers/capabilities.py`、`data_providers/custom/loader.py` | `test_capability_matrix.py`、`test_capability_augment.py`、`test_full_minute_capability.py` |
| fuyao 插件取数与归一化 | `plugins/fuyao/plugin.yaml`、`provider.py`、`client.py` → 自定义源路由及专有服务 | `test_fuyao_provider.py`、`test_fuyao_daily_streaming_sync.py`、`test_fuyao_financial.py` |
| 日线、除权、分钟同步 | 前端手动入口 `pages/Data.tsx` → `api/kline.py`、`api/pipeline.py` → `services/kline_sync.py`、`jobs/daily_pipeline.py` | `test_daily_batch_asset.py`、`test_adj_factor_standalone_sync.py`、`test_minute_routing.py`、`test_minute_timezone_contract.py` |
| 自动盘后缺行状态核对 | `jobs/daily_pipeline.py` 在正式日线验收前 → `services/postmarket.py::reconcile_daily_status` → `data_providers/trading_status.py` 读取独立的按日停复牌及 IPO 日程 → `DailyCoverage.halted/not_listed`；仅自动任务缺行时联网，详见 [盘后约定](postmarket-pipeline.md) | `test_trading_status_provider.py`、`test_postmarket_trading_status.py`、`test_daily_sync_coverage.py` |
| 指标、历史读取或缓存 | `indicators/pipeline.py`、`tickflow/repository.py`；schema 在 `parquet.py`，版本发布在 `enriched_generation.py` | `test_enriched_generation.py`、`test_parquet_schema_compat.py`、`test_live_enriched_std_parity.py` |
| 盘中行情或图表刷新 | `services/quote_service.py`、`services/minute_refresh.py`、`api/intraday.py` → 前端 `lib/useQuoteStream.ts`、`lib/kline.ts` | `test_realtime_turnover_rate.py`、`test_quote_index_merge.py`、`test_kline_daily_latest.py` |
| 采集时段与盘后自动顺序 | `jobs/daily_pipeline.py::start_scheduler` → `services/postmarket.py` 判断交易日和当日成功证据 → 既有 pipeline 任务边界 → `run_now(postmarket=True)`；`instrument_sync.py` 在 TickFlow 元数据边界获取本轮完整 A 股成员名单、标记维表 `is_current`，自动股票池排除历史非当前成员；`daily_coverage.py` 验收正式日线，指标落盘核验后才计算环境；顺序约定见 [postmarket-pipeline.md](postmarket-pipeline.md)。09:10 基础资料及启动补跑保留，周度研究在成功并释放执行槽后触发 | `test_postmarket_pipeline.py`、`test_postmarket_universe.py`、`test_daily_sync_coverage.py`、`test_instruments_startup.py`、`test_manual_collection_schedule.py`、`test_pipeline_capacity.py` |
| 手动重复拉取提示与普通分钟开关 | `api/data.py::sync_check` → `services/collection_status.py` 只核对本地任务与文件；`api/pipeline.py` 在既有任务结果记录路由／范围，`pipeline_jobs.py` 摘要保留 `request_key`；前端 `lib/useCollectionSyncConfirmation.tsx` 确认后调用原接口；`ordinary_minute_enabled` 仅控制普通分钟自动采集 | `test_collection_sync_check.py`、`test_ordinary_minute.py`；前端 `lib/useCollectionSyncConfirmation.test.tsx`、`components/data/MaintenanceControls.test.tsx` |
| 数据页按模块同步 | `pages/Data.tsx` 提供行情与基础数据、分钟历史、板块分类三个同步入口，财务仅展示本地覆盖和单股获取导航；行情与分钟复用原聚合任务，板块走 `api/ext_data.py::sync_classifications` → `ext_presets.sync_classifications`，共用 `api/pipeline.py::_submit_job`；逐表／单股操作保留在维护选项 | `test_classification_sync_jobs.py`、`test_collection_sync_check.py`；前端 `components/data/MaintenanceControls.test.tsx` |
| 财务按需采集与并发复用 | `api/financials.py::fetch_financials` → `services/financial_demand.py::fetch_stock_history` → `fetch_stock`、`_fetch_table`、版本库和浏览表；页面默认近两年，按各表 `history_start` 向前每次补一年；collector 按源／股票／表／日期范围共享在途结果与持久化边界，写入串行重新合并；显式计算任务保留原历史准备；所有登录用户可单股获取，旧 HTTP 全量同步返回 410；Fuyao 支持表由 `financial_tables` 声明 | `test_financial_demand.py`、`test_financial_history.py`、`test_fuyao_financial_history.py`、`FinancialFetch.test.tsx` |
| 策略与因子财务准备 | 显式策略命令通过 `ScreenerService.build_strategy_context(prepare_financials=True)` 准备依赖并附加点时因子；因子面板及公式试算／保存前校验通过 `financial_demand.prepare_factor_frame` 准备对应表并附加时间门控输入；`backtest/worker.py::run_worker_task` 在父 collector 为回测、优化、步进与挖掘准备输入，子进程仅计算；断层先做价格缺口必要条件筛选；GET 与实时监控不采集 | `test_financial_preparation.py`、`test_fundamental_factors.py` |
| 旧内部批次财务股票池（按需入口不调用） | `services/financial_sync.py::_get_symbols` 使用可选 `get_financial_universe` 核对并选择供应商当前全市场代码（含本地维表缺少的代码），将双向差异保存到 `financials/universe.json`；Fuyao 从全市场快照提供清单，旧插件沿用本地维表，历史报表继续合并保留 | `test_financial_universe.py` |
| 财务版本与回测时间 | `services/financial_sync.py` → `financial_versions.py` 保存完整版本与更新日／观察日依据，浏览表每期保留完整记录；`backtest/fundamentals.py` 重建同源指标时间线，共用于面板、矩阵与挖掘，按版本可用日后首个交易日门控；策略评分与排名排除缺失财务输入 | `test_financial_versions.py`、`test_fundamental_factors.py`、`test_strategy_scoring.py`、`backtest/test_factor_rank_research.py` |
| 业绩断层主图指标 | `StockDailyKChart.tsx` 的独立开关 → `api/kline.py::get_earnings_breakout` → `services/earnings_breakout.py` 读取同源利润表版本并固定首次可用财报事件 → `indicators/earnings_breakout.py` 计算断层、9/42；仓库按标的下推版本与前复权价格查询，GET 不采集、不写盘 | `test_earnings_breakout.py`、`StockDailyKChart.test.tsx`、`EChartsCandlestick.test.tsx` |
| 业绩断层回踩选股 | `strategy/builtin/earnings_breakout_pullback.py` → `strategy/market_data.py::get_earnings_breakout_signals` → `services/earnings_breakout.py::filter_pullback_signals` 批量读取利润表版本、完整前序日线并复用主图算法；通过任务内 `EarningsPriceSnapshot` 复用财务候选准备的价格与指标，输入变化时重算；策略引擎只取目标日9或42，双信号去重。仅手动日线筛选，`realtime_supported=false` 阻止实时监控 | `test_earnings_breakout_strategy.py`、`test_strategy_monitor_events.py` |
| 业绩断层跳空选股 | 独立内置 `earnings_breakout_gap.py` → `strategy/market_data.py::get_earnings_gap_signals` → `services/earnings_breakout.py::filter_gap_signals`；与回踩共享财报版本及批量价格读取，只筛目标日 `earnings_gap`，对应K线“断”标记，不要求回踩附加条件；保留回踩策略与指标 | `test_earnings_gap_strategy.py`、`test_earnings_breakout_strategy.py` |
| 跳空候选财务核验 | 策略卡片优先读取缓存；最新交易日独立「核验／重新核验」按钮 → 管理员 `POST /api/screener/earnings-gap/verify` → JobStore → `services/earnings_gap_review.py::find_gap_candidates` 先筛价格 → `financial_sync.sync_candidate_income` 通过共享按需服务更新候选扶摇利润表 → `review_candidates` 复用财报版本时间核验；`verification` 独立保存待核验，确认股才进入命中；不修改全市场断点／成功时间，GET、普通重算和回测不采集 | `test_earnings_gap_review.py`、`test_financial_candidate_sync.py`、`test_earnings_gap_verification_job.py`、`test_strategy_cache.py`、`EarningsGapVerification.test.tsx` |
| 未回补价格缺口区域 | `StockDailyKChart.tsx` 默认展示 → `api/kline.py::get_price_gaps` → `services/price_gaps.py` 读取截止日前全部已存前复权日线及实时尾部 → `indicators/price_gaps.py` 计算向上／向下缺口剩余区间；`EChartsCandlestick.tsx` 用价格带延伸到视图右侧，部分回补收窄、完全回补移除，日线 SSE 失效对应查询 | `test_price_gaps.py`、`StockDailyKChart.test.tsx`、`EChartsCandlestick.test.tsx` |
| 交易日历或显式历史缺口检查 | `services/trading_day.py::sync_calendar` 由 collector 保存日历并累积重叠区间已确认日期（支持财报事件回放），`is_trading_day` 只读；历史检查仍在 `services/data_integrity.py` | `test_trading_day.py`、`test_data_integrity.py` |
| 策略筛选、参数或评分 | `api/screener.py`、`api/strategy.py` → `services/screener.py`、`strategy/engine.py`、`strategy/scoring.py`、`strategy/overlay.py`、`strategy/config.py` | `test_strategy_registry.py`、`test_strategy_saved_params.py`、`test_strategy_overlay.py`、`test_strategy_scoring.py`、`test_screener_cache_api.py` |
| 多用户任务排队与限流 | 选股各命令 → `services/strategy_run_queue.py`；回测／优化／步进／挖掘 → `services/task_scheduler.py` → `heavy_job_limiter.py`；按用户轮转、有界待处理数量、在途选股去重、排队取消释放名额；渐进任务返回 `run_id`，`GET /api/screener/run-status/{run_id}` → `useScreenerRunStatus` 传递终态与逐策略错误，状态仅本用户可读 | `test_strategy_run_queue.py`、`test_screener_run_all_progressive.py`、`useScreenerRunStatus.test.tsx`、`test_task_scheduler.py`、`test_research_ownership.py`、`test_mining_manager.py` |
| 公共快照与个人摘要缓存 | `services/snapshot_cache.py` 合并总览和历史的同键构建；`financial_snapshot.py` 按文件版本复用财务表；`strategy_cache.py` 以 `path_locks.py` 隔离文件锁并缓存摘要；`user_runtime.py::background_active` 避免空闲账号参与实时计算 | `test_snapshot_cache.py`、`test_strategy_cache.py`、`test_financial_demand.py`、`test_user_runtime_boundaries.py` |
| 策略回测、优化或步进验证 | `api/backtest.py` → `backtest/worker.py` → `backtest/strategy.py`、`engine.py`、`matrix.py`、`optimizer.py`、`walkforward.py`；本地分钟成交研究不受当前供应商采集能力限制 | `test_backtest_local_minute_api.py`、`backtest/test_strategy_backtest_correctness.py`、`backtest/test_worker_process.py`、`backtest/test_walkforward.py` |
| 因子和挖掘 | `api/factors.py`、`api/mining.py` → `factors/`、`backtest/factor.py`、`backtest/mining.py`、`services/mining_manager.py` | `test_factor_dsl.py`、`test_factor_registry.py`、`test_mining_jobs.py`、`test_mining_manager.py` |
| 趋势强度梯队 | `api/screener.py::strength_ladder` → `services/strength_ladder.py` → `ScreenerService._load_enriched_for_date(required_bars=60)`；最新日复用指标快照，历史逐股补足实际交易记录，覆盖长期停牌后的20日动量及60日极值；前端 `pages/LimitUpLadder.tsx` 独立趋势标签 → `components/StrengthLadder.tsx`，查询键与刷新开关复用连板页前缀，动量和连板字段分离 | `test_strength_ladder.py`；`pages/LimitUpLadder.test.tsx` |
| 监控规则、告警与通知 | `api/monitor_rules.py` → `strategy/monitor_rules.py`、`strategy/monitor.py`；`services/user_runtime.py` 在装载、重载与角色变化时限制普通监控，`services/ordinary_minute.py` 同步过滤监控采集标的；`matched_conditions` 记录数值与布尔条件的实际命中子集，经 `quote_service.rule_event_to_alert` 及告警存储进入 `components/monitor/AlertConditionDetails.tsx`；通知通道在 `services/*_adapter.py` | `test_monitor_condition_attribution.py`、`test_monitor_runtime_permissions.py`、`test_monitor_group_scope.py`、`test_monitor_etf.py`、`test_monitor_index.py`、`test_strategy_monitor_events.py`；`AlertConditionDetails.test.tsx` |
| 自选、持仓提醒或模拟盘 | `api/watchlist.py`、`api/lots.py`、`api/paper.py` → `services/watchlist.py`、`strategy/lots.py`、`strategy/paper.py`、`strategy/paper_auto.py`；持仓提醒页面、接口及派生提醒仅管理员可用，`services/user_runtime.py` 与 `quote_service.py` 为模拟盘策略跟单维护独立内部事件，不向普通监控结果与告警输出 | `test_monitor_runtime_permissions.py`、`test_watchlist_groups.py`、`test_lots.py`、`test_paper_trading.py`、`test_paper_t1_available_at_order.py` |
| 扩展数据与分析页面 | 数据页板块分类模块统一同步概念／行业，`components/ext-data/ExtDataStatCard.tsx` 保留单项维护及自定义扩展配置；`api/ext_data.py`、`api/analysis.py` → `services/ext_data.py`、`services/ext_pull.py`；业务 `pages/AnalysisDetail.tsx` 与 `components/ExtDimensionAnalysis.tsx` 只读 | `test_ext_rows_query.py`、`test_ext_sql_safety.py`、`test_ext_pull_refresh.py`、`test_collector_maintenance_publication.py`、`test_classification_sync_jobs.py` |
| 市场环境、板块、异动或复盘 | 对应 `api/regime.py`、`sector_rotation.py`、`abnormal.py`、`market_recap.py` → `services/` 同领域构建器和分析器；前端 `pages/Regime.tsx` 在历史为空时提供管理员“环境分析”入口，复用环境重算接口 | `test_regime_builder.py`、`test_market_phase.py`、`test_sector_rotation.py`、`test_abnormal_moves.py`；前端 `pages/Regime.test.tsx` |
| 龙虎榜、竞价风向标及复盘摘要 | `services/dragon_tiger.py`、`auction_benchmark.py` 由后台获取并保存；`api/market_recap.py` 的 GET 只读，页面在 `pages/Review.tsx`、`AbnormalMoves.tsx`、`Dashboard.tsx` | `test_dragon_tiger.py`、`test_auction_benchmark.py` |
| 财务与个股分析 | 数据页 `FinancialSyncPanel.tsx` 展示覆盖并导航至财务页，`pages/Financials.tsx` 即使财务库全空也允许搜索、按股按表获取和浏览历史报告；`api/financials.py` 的 `/status` 在 web 读取本地覆盖画像，`/execution-status` 转交 collector 查询任务状态；`api/stock_analysis.py`、`services/financial_sync.py`、分析服务沿用原链路，本地读取不受供应商能力门控 | `test_financials_local_api.py`、`test_financial_review_regressions.py`、`test_financial_shares.py`、`test_fuyao_financial.py`、`test_stock_analyzer_index.py` |
| 账号、权限与个人归属 | `services/auth_access.py` → `auth.py`、`user_context.py`；`api/users.py` 管理账号；`user_runtime.py`、`private_storage.py`、`user_migration.py` 管用户运行时、路径和旧数据归属；`business_access.py` 管业务权限 | `test_multiuser_auth.py`、`test_multiuser_ai.py`、`test_user_private_stores.py`、`test_user_runtime_boundaries.py`、`test_research_ownership.py` |
| AI 助手工具权限与动作反馈 | `custom/assistant/permissions.py` 保存工具元数据与角色配置；`routes.py` 提供管理员配置 API；`tools.py`、`chat_service.py`、`prompt.py` 共用策略过滤模型工具并在确认和执行时重验；前端 `components/settings/AssistantToolPermissions.tsx` 挂载于设置 AI 页，`custom/assistant/AssistantLauncher.tsx` 订阅成功添加自选事件并刷新页面缓存 | `test_assistant_tool_permissions.py`、`test_assistant_tool_access.py`、`test_assistant_actions.py`、`AssistantToolPermissions.test.tsx`、`AssistantLauncher.test.tsx` |
| AI 点数与用量统计 | `api/ai_credits.py` → `services/ai_credit_policy.py`、`ai_credits.py` 保存运行规则与事务账本；六类生成和连接测试由 `ai_credit_stream.py` 包装，`ai_metering.py` 在核心 provider 与助手流逐次采集 usage，默认仅点数限制使用；`api/users.py` 批量返回各用户当月点数、tokens 与金额估算，前端 `pages/settings/Users.tsx` 展示；`ai_prompt_data.py` 无损压缩个股/财务输入；前端 `lib/aiGeneration.ts`、`useAiCredits.ts` 与 `components/AiCreditHint.tsx` 共用价格/余额；[规则说明](ai-credits.md) | `test_ai_credits.py`、`test_ai_credit_routes.py`、`test_ai_metering.py`、`test_ai_provider_metering.py`、`test_ai_prompt_budget.py`、`test_user_ai_usage.py`；前端 `aiGeneration.test.ts`、`AiCreditHint.test.tsx`、`pages/settings/Users.test.tsx` |
| 页面、请求或扩展 | 前端 `router.tsx`、`lib/api.ts`、`lib/queryKeys.ts`、`extensions/`；后端 `extensions/` 和 `custom/` | 前端相邻 `*.test.*`；后端 `test_extensions.py`、`test_custom_assistant.py`、`test_assistant_actions.py` |

## 关键调用链

预发自动发布入口（2026-10-19）：`.github/workflows/ci.yml` 在 `push main` 检查成功后调用 `scripts/ci_deploy.sh`，上传指定源码并传递 HTTPS 域名到 `scripts/deploy_preproduction.sh`。后者在远端 `releases/<SHA>/` 构建应用镜像，通过 `docker-compose.https.yml` 接入 Caddy；`deploy/Caddyfile` 只反代 web，发布生成的 `.deploy-https.json` 固定域名、Caddy 镜像及版本配置挂载。证书保存在稳定持久卷，服务器数据和配置独立保留；服务与本机 TLS 验收失败时恢复旧应用及代理。`scripts/preproduction_compose.sh` 选择已发布版本的全部覆盖文件。回归入口为 `backend/tests/test_ci_deploy.py`、`test_preproduction_deploy.py`、`test_https_deployment.py` 与 `test_proxy_trust_runtime.py`，操作说明见 [部署指南](deployment.md#docker-https-与-caddy)。

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
| 前端扩展 | [extensions/types.ts](../frontend/src/extensions/types.ts)、[registry.ts](../frontend/src/extensions/registry.ts)、[前端模板](../frontend/src/custom/_template/extension.tsx.example) |
| 后端扩展 | [extensions/contracts.py](../backend/app/extensions/contracts.py)、[registry.py](../backend/app/extensions/registry.py)、[后端模板](../backend/app/custom/_template.py.example) |

源码扩展、数据源插件、用户策略文件、扩展数据表是不同机制，不能互换注册入口。能力与数据单位见维护约定和对应领域文档。

### 页面缩放与模拟盘

- 页面缩放：`frontend/src/lib/pageScale.ts` 负责根节点缩放与坐标比例；`pageZoom.ts` 和 `sessionRuntime.ts` 共用该实现，偏好仍按登录账户隔离。看板网格、助手及分析气泡按此比例换算拖拽坐标。
- 模拟盘：`frontend/src/pages/Paper.tsx` 保持正式路由，复用 `api/paper.py` 与用户私有台账；账户删除、回合与实时对比覆盖于 `test_paper_account_delete.py`，持仓上限与自动方向覆盖于 `test_paper_auto_max_positions.py`。
