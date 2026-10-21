# 本地开发与验证

维护约定和数据契约见 [CONTRIBUTING.md](../CONTRIBUTING.md)；实现与测试定位见 [仓库地图](REPO_MAP.md)，数据流与状态归属见 [架构说明](ARCHITECTURE.md)。

## 分支与环境

| 分支 | 对应环境 | 用途 |
| --- | --- | --- |
| `develop` | 本地与测试 | 日常开发、回归验证和数据实验 |
| `main` | 服务器预发 | 接收已验证的改动，作为服务器发布版本来源 |

日常流程为 `develop` 开发与验证 → 合入 `main` → GitHub CI 检查 → 自动发布同版本的 web 与 collector。CI 在两个分支的 push 和目标分支为它们的 PR 上运行检查，只有 `push main` 且后端测试、前端构建均成功时部署预发。Docker 镜像发布独立，服务器部署不依赖 `latest`。

本地与服务器各自持有 `DATA_DIR` 和配置，账号、会话、自选、个人策略、通知、研究产物、数据源与 AI 配置不互相同步。公共行情、财报、交易日历仅在功能发布需要时按白名单单向同步，常规采集由服务器独立运行。发布和数据同步边界见 [部署指南](deployment.md#本项目的环境与发布约定)。

## 环境与启动

项目仅维护 Linux 服务器版，通过浏览器访问；本地开发支持 macOS / Linux。后端要求 Python 3.11 以上，使用 `uv`；CI 使用 Python 3.12。前端使用 Node 20 和 pnpm 9，`package.json` 声明 pnpm 9.10.0。依赖以各子项目 manifest 与锁文件为准，配置见 [configuration.md](configuration.md)。

需要本地配置且根目录尚无 `.env` 时，从 `.env.example` 复制；已有配置按需编辑，保留密钥和运行数据。根目录运行 `./dev.sh`。启动器检查依赖并启动 web、collector 与前端；遇到已有 collector 端口占用会停止并提示。默认端口为 web 3018、collector 3019、前端 3011。

分开启动时，在三个终端执行。两个后端必须使用相同 `DATA_DIR`，以下都从同一 `backend` 目录启动：

```bash
# 终端一，从项目根开始
cd backend
uv sync --extra dev --frozen
SERVICE_ROLE=collector uv run uvicorn app.collector:app --no-proxy-headers --host 127.0.0.1 --port 3019
```

```bash
# 终端二，从项目根开始；只让 web 热重载
cd backend
SERVICE_ROLE=web COLLECTOR_URL=http://127.0.0.1:3019 uv run uvicorn app.main:app --no-proxy-headers --reload --port 3018
```

```bash
# 终端三，从项目根开始
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

访问前端 `http://localhost:3011`，Vite 将 `/api` 和 `/health` 代理到后端。更改后端地址时同步设置 `BACKEND_PORT`、`BACKEND_HOST`，见 [vite.config.ts](../frontend/vite.config.ts)。分别用 `http://127.0.0.1:3018/health` 和 `http://127.0.0.1:3019/health` 检查双服务；collector 仅供本机或容器私网访问。

空账号库默认创建管理员 `pyxadmin`，初始凭据与账号规则见 [deploy-password.md](deploy-password.md)。登录后直接进入看板，页面只读取已有 Key 和数据源路由；密钥由后端配置，能力路由在“设置 → 数据源”修改。基础数据从“数据”页准备，已有本地数据的浏览与刷新不触发采集。

collector 启动会创建或使用数据目录、恢复任务并启动调度、日历和行情服务，可能访问数据源。web 只加载共享结果并连接 collector。隔离验证时为两者设置同一独立 `DATA_DIR`，并核对 `.env` 和数据源配置；没有付费 Key 不代表 collector 离线。只重启 web 可保持采集任务运行，停止开发启动器会停止它启动的全部进程。

实时行情、普通分钟采集和盘后任务的开关、时段及本地读取边界见 [架构说明](ARCHITECTURE.md#实时行情与事件流)；交易日 16:00 盘后同步的依赖顺序、验收、重试与取消见 [盘后同步约定](postmarket-pipeline.md)。手动同步和任务进度集中在数据页，财务按股按表获取，计算任务由 collector 准备所需财务输入。

自动盘后管道在股票日线、因子就绪后允许两路轻量网络预取，与股票指标计算重叠；核心指数只预取不超过 14 个自然日的窗口，深历史、指标计算、仓库写入与刷新仍按依赖串行执行。分类模块独立同步时也并发取数、顺序写入；分钟历史保持后置。不要将含 `repo` 写入或视图刷新的整段函数直接加入线程池，也不要并行写概念／行业主线共用的历史文件。TickFlow 首批请求同样需要等待共享预算，预算边界见 [请求限流](tickflow-pro-shared-rate-limit.md)。

数据页显示独立子任务状态、阶段进度、批次日志、耗时及最后更新时间，刷新后可恢复活跃任务；进度获取失败可重试，结束详情由用户关闭，结果仍在同步历史中。同页关闭的任务不因陈旧的 `active_id` 自动重开，仍可从历史显式查看；保留终态详情不触发高频数据状态轮询，也不停止发现后续新任务。侧边栏分别显示成功、失败与取消。总进度不是剩余时间估算；优化效果应以阶段耗时和真实同步记录衡量，不预设提速比例。验证并发改动时覆盖取数重叠、写入顺序、首批限流、失败不发布、取消后不继续提交与旧任务兼容。

数据页显式启动修正、向前扩展、指标重算或分钟／除权任务后，统一按响应中的 `job_id` 恢复实时跟踪并切换到新进度，不能仅关闭弹窗或等待任务列表刷新。指数范围、单位和批次参数的禁用条件依据实际任务与请求繁忙状态，保留已结束或历史详情不影响配置。

数据页「同步分钟历史数据」使用普通分钟能力；首次回溯配置的自然日数，已有数据时从北京时间最新数据日的零点回补到现在。这样单股尾盘数据不会令全市场早盘被跳过，重叠记录按股票和分钟合并。行业／概念轮动读取这份本地数据，不要求盘中全量分钟能力；供应商提供完整窗口后即可展示。此操作只回补最新数据日，不修复更早日期的历史缺口；向前扩展仍从最早数据继续向前取数。

行业／概念轮动的自动榜保留当天曾有分钟数据的板块，最新时段仅更新少数股票时，其余板块已有曲线仍可查看，未更新时段和当前涨幅、排名、综合分留空。强弱切换优先从具有当前值与历史对照值的板块中抽样，不把缺数据当作走弱。页面分别提示当日与最新时段的行业／概念覆盖数，覆盖数不代表全部成分股已经齐全。新一天仅有单股分钟时继续使用新日期并提示覆盖不足，不把昨天的数据当作今天行情；可通过上述普通分钟历史同步补采。

可选依赖按需安装：`backtest` 提供 vectorbt，`legacy-cpu` 提供兼容 Polars 内核。矩阵回测与 vectorbt 信号回测是不同路径。容器发布和 CPU 兼容见 [deployment.md](deployment.md)。

## 验证入口

只改文档时核对链接、源码位置和最终差异。代码修改按 [维护约定](../CONTRIBUTING.md#9-验证矩阵) 选择受影响模块；类型检查和构建不能代替业务行为验证。

```bash
# 后端，从项目根开始；路径换成受影响模块
cd backend
uv run --frozen pytest tests/test_capability_matrix.py -q
uv run --frozen ruff check app/data_providers/capabilities.py

# 需要时运行后端全量测试
uv run --frozen pytest tests -q
```

```bash
# 前端，从项目根开始
cd frontend
pnpm build
pnpm exec vitest run src/pages/Review.test.tsx src/pages/Financials.test.tsx

# 指数页专用脚本
pnpm test:indices
```

```bash
# 在项目根检查最终变更
git diff --check
git diff --stat
git status --short
```

后端测试位于 `backend/tests/`，回测测试集中在 `backend/tests/backtest/`。双服务和本地查询可先看 `test_shared_runtime.py`、`test_local_market_reads.py`；财务按需路径可看 `test_financial_demand.py`、`test_financial_preparation.py`、`test_financial_candidate_sync.py`、`test_fundamental_factors.py`。

前端测试靠近模块，使用 `*.test.ts` 或 `*.test.tsx`。部分测试使用 `node:test`，先检查导入再选择运行器；例如支持类型剥离的 Node 可运行 `node --experimental-strip-types --test src/lib/neighborPrefetch.test.ts`。测试是否联网以 fixture 和测试体为准。

[ci.yml](../.github/workflows/ci.yml) 当前执行后端 `uv sync --extra dev --frozen`、`uv run pytest tests -q`，前端 `pnpm install --frozen-lockfile`、`pnpm build`。Ruff、Vitest、真实数据源探针和浏览器交互验证不在该 CI 范围内。Ruff 配置见 `backend/pyproject.toml`。

## 专项入口

- 多用户与个人数据：`test_multiuser_auth.py`、`test_multiuser_ai.py`、`test_user_private_stores.py`、`test_user_runtime_boundaries.py`、`test_research_ownership.py` 使用独立临时目录，验证账号与存储隔离。
- 定制模块：后端 `test_extensions.py`、`test_custom_assistant.py`、`test_assistant_actions.py`；前端结合类型构建和界面验证，入口见 [扩展说明](secondary-development.md)。
- Polars 并发：`backend/scripts/stress_polars_concurrency.py`；供应商能力：`backend/scripts/probe_tickflow_pro.py`，其中 `--live` 会发真实请求。
- 预发部署：[ci_deploy.sh](../scripts/ci_deploy.sh) 为 GitHub runner 入口，[deploy_preproduction.sh](../scripts/deploy_preproduction.sh) 在服务器构建、切换和验收。`test_ci_deploy.py` 与 `test_preproduction_deploy.py` 使用临时目录和命令替身，不访问真实服务器。设置见 [部署指南](deployment.md#github-自动发布预发)。

## 领域资料

| 工作内容 | 资料 |
| --- | --- |
| 部署、配置、账号 | [deployment.md](deployment.md)、[configuration.md](configuration.md)、[deploy-password.md](deploy-password.md) |
| 数据源接入 | [custom-data-source.md](custom-data-source.md)、[plugin-development.md](plugin-development.md) |
| 策略与信号 | [strategy.md](strategy.md)、[策略开发规范](../backend/app/strategy/prompts/strategy-guide.md)、[strategy-iteration.md](strategy-iteration.md) |
| 因子与研究 | [factor-system-design.md](factor-system-design.md)、[factor-platform-plan.md](factor-platform-plan.md)、[mining.md](mining.md) |
| 市场环境与模拟盘 | [market-phase.md](market-phase.md)、[paper-trading-plan.md](paper-trading-plan.md) |
| 源码定制 | [secondary-development.md](secondary-development.md) |
