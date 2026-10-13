# 开发导航

本文帮助后续维护者进入当前仓库：先找到实现和已有约定，再启动环境、选择验证。命令与入口核对日期为 2026-10-10；它们是操作参考，贡献规则以 [CONTRIBUTING.md](../CONTRIBUTING.md) 为准。

## 阅读顺序

1. [AGENTS.md](../AGENTS.md) 与 [CONTRIBUTING.md](../CONTRIBUTING.md)：协作规则、数据契约和验证矩阵。
2. [仓库地图](REPO_MAP.md)：从任务找入口、责任模块和测试。
3. [架构说明](ARCHITECTURE.md)：数据流、状态归属、运行与扩展边界。
4. 目标模块、调用方和现有测试；涉及二开时阅读 [secondary-development.md](secondary-development.md)，并核对接口是否真实存在。

## 环境与启动

后端要求 Python 3.11 以上，使用 `uv`；当前 CI 使用 Python 3.12。前端使用 Node 20 和 pnpm 9，`package.json` 声明 pnpm 9.10.0。依赖来源为各子项目 manifest 与锁文件，完整配置见 [configuration.md](configuration.md)。

开发前先查看 `git status --short`。需要本地配置且根目录尚无 `.env` 时，再从 `.env.example` 复制；已存在时保留并按需编辑。不要把密钥或运行时数据写进文档、测试样例或提交。

根目录可以运行 `./dev.sh`，Windows 使用 `./dev.ps1`。启动器会检查并在需要时下载或安装依赖、处理网页／前端端口占用，再启动 web、collector 与前端；已有 collector 端口占用时停止并提示，避免终止运行中的采集任务。默认 web 端口为 3018、collector 为 3019、前端为 3011；只阅读或整理文档时无需启动它。

分开启动时，在三个终端运行（两个后端进程必须使用相同 `DATA_DIR`，以下都从同一 `backend` 目录启动）：

```bash
# 终端一，从项目根开始
cd backend
uv sync --extra dev --frozen
SERVICE_ROLE=collector uv run uvicorn app.collector:app --host 127.0.0.1 --port 3019
```

```bash
# 终端二，从项目根开始；只让 web 热重载
cd backend
SERVICE_ROLE=web COLLECTOR_URL=http://127.0.0.1:3019 uv run uvicorn app.main:app --reload --port 3018
```

```bash
# 终端三，从项目根开始
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

浏览器访问前端 `http://localhost:3011`，Vite 将 `/api` 和 `/health` 代理到后端。后端端口或主机改变时，同步设置前端使用的 `BACKEND_PORT`、`BACKEND_HOST`，见 [vite.config.ts](../frontend/vite.config.ts)。

首页直接进入看板；已启用访问认证时先登录。首次配置向导已移除，页面沿用现有 Key 和数据源路由，进入页面不会自动改写路由。需要变更时在“设置 → 数据源”手动操作。看板不再提供首次获取弹窗或一键取数卡片，无数据时显示组件原有空态；维护者上线前在“数据”页手动准备基础数据，后续手动同步也从数据页发起。

collector 启动会创建或使用数据目录、恢复任务并启动调度、日历和行情后台服务，可能访问数据源。web 启动只加载共享结果并连接 collector，不启动采集。隔离验证时为两个服务设置同一个独立 `DATA_DIR`，并核对 `.env` 和数据源配置；未配置付费 Key 不代表 collector 离线。只重启网页进程可保持采集任务运行；停止整个开发启动器则会停止它启动的全部进程。

当前项目以 TickFlow 为主要行情源，fuyao 补充龙虎榜、竞价风向标与交易日历；保存 fuyao Key 不会自动切换主行情路由，接入方式见 [配置说明](configuration.md#fuyao-补充数据当前接入方案)。插件和专有服务的定向测试入口包括 `test_fuyao_provider.py`、`test_fuyao_financial.py`、`test_dragon_tiger.py`、`test_auction_benchmark.py`。

交易调度只读 `calendar/trading_days.json`；collector 启动及每日定时更新日历，未覆盖日期返回未知并暂停交易时段任务。09:10 股票基础资料仍自动更新。collector 启动完成交易日历维护后，若已超过配置时点、当天明确为交易日且维表 `as_of` 尚未更新为当天，则通过原任务执行槽补跑一次；启动与定时任务共用本地成功证据，当天已完成时跳过。实时行情需在数据页开启自动采集，之后 collector 启动会读取已保存开关，在交易日 09:30–11:30、13:00–15:00 自动取数；午休与休市暂停，15:00–15:30 仅完成既有收盘确认，成功即停止。盘后全量、榜单及盘口定版不自动执行，改由数据页手动同步；盘口启动只恢复本地文件。历史完整性检查仍是数据页修复／同步和手动盘后管道的能力，浏览行情、切换股票及启用实时不再自动启动缺口修复。各源支持的数据集见 [配置说明](configuration.md#当前代码的-tickflow-能力门槛)。

验证双服务时分别检查 `http://127.0.0.1:3018/health` 和 `http://127.0.0.1:3019/health`；collector 仅供本机或容器私网访问。网页行情读走本机仓库，数据页的显式获取、同步、配置和任务操作经代理转交 collector。业务页刷新仅重新查询本地数据。进度与取消沿用 pipeline API，网页重启后数据页从任务列表、财务维护面板从 `active_job_id` 恢复状态。

数据页的“普通分钟采集”开关独立控制自选／启用监控标的／核心指数的自动分钟落盘，关闭不影响显式分钟同步。手动同步先只读检查本地重复证据；发现相同范围已完成任务或已有当日本地结果时可取消或确认重新拉取。提示不等于保证上游绝无修订，盘中、失败、无数据或证据未知时不会宣称最新。检查接口与原同步接口分开，未来定时触发无需经过浏览器弹窗。

复盘页通过 `/api/market-recap/publication` 读取当日发布快照；后台尚未发布时显示“当日复盘尚未更新”。手动 AI 和历史报告仍可用，复盘／个股／财务 AI 由 collector 归档。跨服务 `data_updated` 与行情 SSE 在后台缓存更新之后通知页面，浏览器连接恢复时补读状态。

可选依赖按需要安装：`backtest` 提供 vectorbt，`desktop` 提供 pywebview，`legacy-cpu` 提供兼容 Polars 内核。矩阵回测与 vectorbt 信号回测是不同路径；无需为每项开发任务安装全部 extras。Docker、桌面发布及 CPU 兼容详情沿用 [deployment.md](deployment.md) 与 `packaging/`。

## 验证入口

只改文档时，核对链接、源码位置和最终差异即可。代码修改按贡献规范选受影响模块，不把构建或静态检查当作业务行为验证。

```bash
# 后端，从项目根开始；路径换成受影响模块
cd backend
uv run --frozen pytest tests/test_capability_matrix.py -q
uv run --frozen ruff check app/data_providers/capabilities.py

# 后端全量测试（需要时）
uv run --frozen pytest tests -q
```

```bash
# 前端，从项目根开始
cd frontend
pnpm build
pnpm exec vitest run src/pages/Review.test.tsx src/pages/Financials.test.tsx src/components/StockMultiDayIntradayChart.test.tsx src/lib/useQuoteStream.test.tsx src/lib/analysisArchival.test.ts

# 现有专用脚本
pnpm test:indices
```

```bash
# 在项目根检查最终变更
git diff --check
git diff --stat
git status --short
```

后端测试位于 `backend/tests/`，回测专门测试位于 `backend/tests/backtest/`；双服务和本地行情读取可先看 `test_shared_runtime.py`、`test_local_market_reads.py`。前端测试靠近模块，使用 `*.test.ts` 或 `*.test.tsx`；部分文件使用 `node:test` 而非 Vitest，先检查导入再选择运行器，例如支持类型剥离的 Node 可运行 `node --experimental-strip-types --test src/lib/neighborPrefetch.test.ts`。文件名含 `live` 的测试也可能通过 mock 模拟实时路径，是否访问网络应读 fixture 和测试体，不按名称推断。

当前 [ci.yml](../.github/workflows/ci.yml) 的实际范围如下：

| 作业 | 安装与执行 | 未由该作业执行的检查 |
| --- | --- | --- |
| 后端 | `uv sync --extra dev --frozen`；`uv run pytest tests -q` | Ruff、mypy、真实数据源探针 |
| 前端 | `pnpm install --frozen-lockfile`；`pnpm build`（TypeScript 与 Vite） | Vitest、浏览器交互验证 |

Ruff 规则定义在 `backend/pyproject.toml`。后端源码扩展和助手已有 `test_extensions.py`、`test_custom_assistant.py`、`test_assistant_actions.py`；当前未找到对应的前端扩展注册或助手 store 专门测试，因此这些改动还需要类型构建和必要的界面验证。

## 对外接入与专项脚本

- [examples/open-api/README.md](../examples/open-api/README.md)：行情读取、扩展写入、回测与事件流示例；写入与计算示例会改变面板状态，运行前将 `TSP_BASE` 指向用于验证的面板，并按 README 配置 Token 与 scope。
- `backend/scripts/stress_polars_concurrency.py`：Polars 并发专项验证；`probe_tickflow_pro.py`：供应商能力探针，`--live` 会发真实请求。它们服务特定问题，不能替代定向测试。
- [scripts/upgrade_check.py](../scripts/upgrade_check.py)：源码二开升级的只读 Git 预检，目标 ref 须已在本地可解析，未提交修改不参与预演；详情见二开指南。

开放端点以 `services/api_gateway.py` 的规则表和 `/api/openapi.json?tier=a` 为准；`test_openapi_contract.py` 保存 method/path 快照。README 中的接口数量是介绍信息，后续接入不应依赖这些数字。

## 领域文档导航

| 工作内容 | 现有资料 |
| --- | --- |
| 部署、配置、密码 | [deployment.md](deployment.md)、[configuration.md](configuration.md)、[deploy-password.md](deploy-password.md) |
| 功能与页面行为 | [features.md](features.md)、根目录 [README.md](../README.md) |
| 数据源接入 | [custom-data-source.md](custom-data-source.md)、[plugin-development.md](plugin-development.md)、[mock 联调示例](examples/custom-data-source/README.md) |
| 策略、信号与迭代 | [strategy.md](strategy.md)、[策略开发规范](../backend/app/strategy/prompts/strategy-guide.md)、[strategy-iteration.md](strategy-iteration.md) |
| 因子与研究 | [factor-system-design.md](factor-system-design.md)、[factor-platform-plan.md](factor-platform-plan.md)、[mining.md](mining.md) |
| 市场环境与模拟盘 | [market-phase.md](market-phase.md)、[paper-trading-plan.md](paper-trading-plan.md) |
| 对外接口与二开 | [open-platform-plan.md](open-platform-plan.md)、[secondary-development.md](secondary-development.md)、前后端 `custom/README.md` |

名称含 `plan` 或 `design` 的文档，以及二开指南中的按需扩展示例，可能同时包含已实现行为与目标设计。实施前用源码定义、注册入口和测试确认，避免据此创建不存在的调用路径。

## 后续工作定位方法

先从页面请求或 API 入口追到服务、领域计算、仓库和数据源；写入问题继续追到内存缓存、generation、事件和前端查询。金融结果需核对单位、原始价与复权价、交易日、北京时间及资产类型，详细要求集中在贡献规范中。

开始实施时记录目标行为、改动责任和最小验证方式。完成后报告实际结果与尚未验证的范围；如果入口、职责或主要数据流改变，同步仓库地图与架构说明。
