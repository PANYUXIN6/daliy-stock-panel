# 部署指南

本项目的几种运行方式，按推荐程度排序。配置项详解见 [configuration.md](./configuration.md)。

## 本项目的环境与发布约定

`develop` 用于本地开发和测试，`main` 对应服务器预发。改动在 `develop` 验证后合入 `main`，GitHub CI 的后端测试与前端构建通过后，自动从这次提交构建并发布同一版本的 web 与 collector。

### 代码发布

更新代码或镜像时保留目标环境的整个 `data/`、`.env`、`tiers.yaml` 和 `docker-compose.override.yml`。源码发布包只包含版本控制内的代码；其中 `tiers.yaml` 虽然受 Git 跟踪，仍须保留远端配置，不能直接用本地版本覆盖。需要调整配置结构时，应单独说明变更，并在目标环境迁移已有值。

账号、会话、自选、个人策略、通知、报告、回测与挖掘产物，以及数据源、AI、采集开关和其他配置，各自由本地和远端维护。首次部署的数据迁移不代表后续继续同步这些内容。

### GitHub 自动发布预发

入口在 [`.github/workflows/ci.yml`](../.github/workflows/ci.yml)。合并到 `main` 后产生的 push 会先执行后端测试和前端构建，全部成功后才进入 `preproduction` 部署任务；PR、`develop` 及失败检查均不会触发部署。直接推送 `main` 也会触发同一流程。部署任务检查待发布 SHA 是否仍为 `main` 最新提交，跳过过时任务；预发发布串行执行，不因后续提交自动取消正在运行的发布。

在 GitHub 仓库的 **Settings → Environments → preproduction** 设置部署分支为仅 `main`，并保存以下 Environment secrets。服务器使用独立 SSH 部署密钥，不能把私钥或登录密码写入工作流。

| Secret | 内容 |
| --- | --- |
| `DEPLOY_HOST` | 服务器 IP 或主机名 |
| `DEPLOY_USER` | 已获准运行 Docker 和管理部署目录的 SSH 用户 |
| `DEPLOY_SSH_KEY` | 该服务器部署专用私钥 |
| `DEPLOY_KNOWN_HOSTS` | 通过可信连接核验后的 SSH 主机公钥记录，严格校验，不在运行时自动接受新指纹 |

同一 Environment 的 Variables 为 `DEPLOY_DIR`（默认 `/opt/stock-panel`）、`DEPLOY_PORT`（默认 `22`）、`DEPLOY_URL`（公网 HTTPS 地址，用于额外健康检查）。目标机需已完成双服务初始化，并具备 Bash、Python 3.12、OpenSSH、Docker Compose 和 `flock`；运行根目录必须已有 `.env`、`tiers.yaml`、`docker-compose.yml`、`docker-compose.override.yml` 与运行数据。密钥须能以非交互方式登录，服务器需启用公钥认证。

[CI 入口](../scripts/ci_deploy.sh) 只上传 `git archive` 生成的本次提交源码。[服务器入口](../scripts/deploy_preproduction.sh) 将其放入运行根目录的 `releases/<SHA>/`，先构建新镜像，再用该版本的 Compose 定义、远端已有覆盖配置和精确镜像版本启动双服务。数据目录、环境文件及挂载配置始终从远端运行根解析，镜像版本覆盖不会改写原部署配置。首次自动发布可接管现有手动部署；后续版本通过 `current` 指向已验收的源码目录。发布不会上传金融数据或个人数据。

新镜像继承远端现有 Compose 中 web 服务已解析的全部 `build.args`，包括 `BACKEND_EXTRAS`、`INCLUDE_STOCKSDK` 和镜像源选项；发布脚本不另行覆盖这些设置。

服务器文件锁覆盖发布和恢复过程。切换前若检测到未完成的持久化采集／挖掘任务，或仍运行的独立策略计算 worker，发布会失败，待任务结束后重跑 GitHub 工作流。进程内的信号／因子回测及尚未启动 worker 的排队任务不在这项检查范围内，发布前应结束这些任务；状态检查与实际切换之间也存在短暂竞态。若需要在持续运行研究任务时发布，应增加 collector 的维护准入机制，当前脚本不提供这项能力。

更新涉及两服务重建，会有短暂不可用，不提供零停机保证。健康验收要求两个容器正常、镜像正确、collector 未暴露宿主端口，并且 web 的 `/health` 显示已连接 collector；失败时尝试按旧版本 Compose 和旧镜像恢复双服务。成功后才更新 `current` 和 `.deployed-commit`。镜像恢复不回滚运行数据，涉及持久化格式变更的版本应单独安排兼容迁移与备份。

首次自动发布成功后，使用当前版本的管理入口，避免根目录旧 Compose 定义把服务切回最初镜像：

```bash
bash /opt/stock-panel/current/scripts/preproduction_compose.sh /opt/stock-panel ps
bash /opt/stock-panel/current/scripts/preproduction_compose.sh /opt/stock-panel logs --tail 100 web collector
bash /opt/stock-panel/current/scripts/preproduction_compose.sh /opt/stock-panel restart web
```

发布失败可在 GitHub Actions 中查看失败步骤；修复后重新运行时仍会检查 `main` 当前提交，历史任务不能覆盖新版本。服务验收与命令替身测试不替代第一次 GitHub runner 到服务器的真实发布验收。

### 按发布需要同步公共数据

常规采集由服务器独立执行；只有本地新增公共数据、修复数据，或功能发布确需配套数据时，才随发布同步明确选定的数据集。代码发布不自动复制本地数据。自动任务覆盖已启用的盘中行情、基础资料、交易日历，以及默认在交易日北京时间 16:00 执行的盘后管道。collector 必须持续运行且本地日历确认当天为交易日；正式行情未齐或任务繁忙时当天每 15 分钟重试。财务仍按需求获取，ETF／分钟和 AI 保持各自开关，不能把“服务已启动”视为所有数据都会每日全量更新。具体顺序与验收见 [盘后同步顺序约定](postmarket-pipeline.md)。

同步范围以文件白名单确定，不能只排除 `users/` 后复制其余内容，因为根目录仍可能保留旧版个人数据。

| 可选公共数据 | 同步要求 |
| --- | --- |
| 股票、指数、ETF 的原始日线、分钟线、复权因子和基础维表 | 只处理对应数据文件，按记录主键核对并合并，不能用本地旧整表覆盖远端新增记录；分钟数据必须核对 `.raw_basis` 价格口径，不一致时停止导入 |
| 财报浏览表及 `financials/versions/` 版本记录 | 保留远端独有版本及来源、可用日和首次观察时间；发现同一版本事实冲突时停止处理，不推测历史日期，也不只复制最新浏览表 |
| `calendar/trading_days.json` | 保留两边已有的确认日期及覆盖证据，只合并重叠覆盖；不连续的区间停止合并，不能把中间未知日期视为休市，也不能用较短的本地日历替换远端历史 |
| 公共概念／行业分类结果 | 仅在明确选定时处理预设的数据文件，不携带同目录配置；自定义扩展数据不默认纳入 |

不传输 `auth/`、`users/`、旧个人目录、策略、研究产物、`user_data/`、数据源配置、密钥、任务记录、财报同步断点、运行快照、日志、锁或缓存。`ext_data/` 和 `financials/` 均含混合用途文件，不能整体复制。enriched、矩阵等派生结果由目标版本与目标环境数据重新生成，不从本地搬运。

执行顺序为：确定发布提交和所需数据集 → 检查口径与合并差异 → 备份远端将改动的数据 → 在维护窗口内停止相关读写服务并完成安装及必要重算 → 启动双服务 → 核对数据覆盖、服务健康和业务读取。存在活动采集或研究任务时先妥善结束，不能在 collector 写入期间覆盖文件；失败时用本次远端备份恢复。本地缺少某条数据不代表需要删除远端数据，未选定的数据集保持原状。

当前仓库没有跨机器公共数据导入工具；同机双服务的运行时更新机制和 `/api/data/refresh-cache` 不提供跨机合并或磁盘 enriched 重算。实际随版同步应按具体数据集实施并验证，不能用裸目录覆盖替代。下文“备份与恢复”用于同一环境的整站恢复，不用于本地向预发同步。

> 📌 前置依赖(仅方式 D 需要):Python ≥ 3.11 · Node ≥ 20 · [`uv`](https://docs.astral.sh/uv/) · `pnpm`（`npm i -g pnpm`）

---

## 方式 A：已有镜像

使用包含双服务入口 `app.collector:app` 的同一版本镜像运行网页与采集服务。复制根目录 `docker-compose.yml`，将共享 `x-service.build` 改为该版本的 `image`，再执行：

```bash
docker compose pull
docker compose up -d
```

不要只运行一个 `app.main:app` 容器：默认角色为网页，仅提供本地查询，显式同步需要采集服务。既有单容器部署应先停止旧容器，再启动双服务，保留原 `data/` 挂载。

镜像默认不含 stock-sdk 插件或 `legacy-cpu` / `backtest` extras；需要这些能力时使用本地构建。

---

## 方式 B:Docker Compose(本地构建,全套挂载)

```bash
cp .env.example .env
docker compose up --build
# 打开 http://localhost:3018
```

Docker 使用同一镜像启动 `web` 和唯一的 `collector`，共享 `./data:/app/data`。只有 `web` 映射宿主机端口；`collector` 仅在 Compose 网络内可达，外部反向代理也只能指向 `web`。两个服务均配置健康检查和 `restart: unless-stopped`。

> ⚠️ **stock-sdk 插件默认不打包(合规考虑)**
>
> stock-sdk 数据源本质是抓取第三方财经网站(如东方财富)的行情接口,未经对方授权,可能违反其服务条款并涉及交易所行情版权问题。**出于合规考虑,Docker 默认构建不再内置 stock-sdk 插件依赖**。
>
> - **默认行为**:`docker compose up --build` 构建出的镜像**不含** stock-sdk,插件不可用。
> - **如确需启用**(自行承担合规责任):
>   ```bash
>   docker compose build --build-arg INCLUDE_STOCKSDK=1
>   docker compose up -d
>   ```
> - 启用后镜像会额外内置 Node.js 运行时并预装 stock-sdk 依赖,插件开箱即用。
> - **建议优先使用 TickFlow 等正规授权数据源。**

更新到新版本:

```bash
git pull
docker compose up --build -d
```

---

## 方式 C:本机 AI 代部署(小白推荐)

装一个本机 AI 编程助手(Trae / Codex / OpenCode / ZCode / WorkBuddy 等,任选其一),把 [README · 快速开始](../README.md#-快速开始) 里方式 C 的提示词原样发给它,AI 会自动完成克隆、装依赖、启动服务。适合完全不想碰命令行的用户;AI 最终执行的仍是方式 A / B / D 之一。

---


## 方式 D:Dev 模式(二次开发推荐)

由于刚开源近期更新频繁,推荐开发模式运行,可随时 `git pull` 同步最新代码。

```bash
git clone https://github.com/shy3130/tick-stock-panel.git
cd tick-stock-panel
cp .env.example .env       # 按需填 TICKFLOW_API_KEY(留空 = None 模式)
./dev.sh                   # Windows: .\dev.ps1
```

`dev.sh` / `dev.ps1` 同时启动网页、采集和前端开发服务器，Ctrl-C 一并关闭。采集服务不启用代码热重载，网页 reload 不会终止采集任务；采集端口已占用时会报错，不强杀现有采集进程。默认:

- 网页后端 → <http://localhost:3018> · 前端 → <http://localhost:3011> · 采集 → `127.0.0.1:3019`
- 自定义端口:`BACKEND_PORT=8000 FRONTEND_PORT=5173 ./dev.sh`

### 手动分别启动(不想用 dev.sh)

```bash
# 后端
cd backend && uv sync --extra backtest   # 含回测依赖
# 老 CPU: uv sync --extra legacy-cpu
# 老 CPU + 回测: uv sync --extra legacy-cpu --extra backtest
# 第一个终端：唯一采集服务
uv run uvicorn app.collector:app --no-proxy-headers --host 127.0.0.1 --port 3019

# 第二个终端，同一个 backend/ 和 DATA_DIR：网页服务
uv run uvicorn app.main:app --no-proxy-headers --host 127.0.0.1 --reload --port 3018

# 前端
cd frontend && pnpm install && pnpm dev   # http://localhost:3011
```

---

## 运行配置、健康与恢复

交易日历复用已配置的 fuyao 插件，由 collector 启动、定时及数据源保存后同步到共享目录。日历未知或未覆盖当日时，实时与分钟采集保持未就绪；先在设置中配置 fuyao，再查看服务日志中的同步结果，不按周一至周五猜测交易日。

- `SERVICE_ROLE`：默认 `web`；独立入口 `app.collector:app` 显式选择采集角色。
- `COLLECTOR_URL`：网页内部请求地址，源码默认 `http://127.0.0.1:3019`；Compose 为 `http://collector:3018`。
- `DATA_DIR`：两个服务必须使用同一目录。唯一采集进程通过原跨平台进程锁持有写入/任务执行权，第二个采集实例启动会失败。
- `AI_MAX_CONCURRENT`：默认 2，所有 AI 执行入口共享并发上限，流结束、关闭和取消均释放槽位。

`GET /health` 返回自身 `service_role`。网页还返回 `collector_connected` 和 `collector_last_event_at`，用于区分网页可用与采集连接就绪；采集暂不可用时仍可浏览已保存数据，执行请求返回明确的 503。

```bash
docker compose ps
docker compose logs --tail 100 web collector
docker compose restart web       # 不影响 collector 正在执行的任务
```

采集服务重启后，未完成任务会标记为中断，用户可显式重试；不会由网页启动自动补齐。行情与指数使用已保存快照，网页每个进程仅保持一条采集事件连接，连接恢复后重新加载本地快照和视图。

## 桌面版

桌面窗口连接网页服务。首次启动会在本机启动独立采集监护进程；窗口关闭后采集继续运行，重开窗口复用原进程。采集异常退出由监护进程自动重启。日志在 `data/runtime/collector-launch.log` 与后端日志中。

需要彻底停止后台采集时，关闭窗口后运行：

```bash
# 源码模式（backend/ 目录）
uv run python -m app.desktop --stop-collector
# 打包版：对安装目录中的可执行文件传同一参数
TSP --stop-collector
```

下次打开桌面程序会重新启动采集。不要同时运行桌面采集和另一个使用同一 `DATA_DIR` 的 collector。

## 备份与恢复

备份整个 `data/`（包括行情、用户配置、策略、报告和任务记录），同时保存 `.env` 与 `tiers.yaml`。为获得一致备份，先停止两个服务，拷贝到独立备份目录，再启动：

```bash
docker compose stop
# 将 data/、.env、tiers.yaml 复制到你选择的备份位置
# 确认备份完成后：
docker compose up -d
```

恢复时停止两服务，用备份替换目标目录并确认挂载路径和文件权限，再启动同版本镜像。不要在 collector 写入时覆盖数据。备份包含密钥与个人数据，应保存在受控位置。

---

## 老 CPU 兼容(avx2/fma 缺失)

如果运行时报 `avx2`/`fma` 缺失,或进程 `exit 132`,说明 CPU 不支持 AVX2 指令集(常见于老 VPS)。解决:

- **Dev 源码启动**:在根目录 `.env` 设置后运行 `./dev.sh` 或 Windows 的 `.\dev.ps1`;即使已有 `.venv`,启动器也会同步兼容内核
- **Docker**:在根目录 `.env` 设置后执行 `docker compose up --build`

```ini
BACKEND_EXTRAS=legacy-cpu          # 兼容老 CPU
BACKEND_EXTRAS=legacy-cpu backtest # 兼容老 CPU + 回测依赖
```

手动启动源码时，也可以在 `backend/` 目录直接执行 `uv sync --extra legacy-cpu`。不要设置 `POLARS_SKIP_CPU_CHECK`，它只会隐藏警告，实际执行不支持的指令时仍可能崩溃。

### 回测依赖说明

vectorbt → numba 体积较大,作为可选 extras(`uv sync --extra backtest`)。macOS / Intel 无预构建 wheel 时需 `brew install cmake` 现场编译。

---

## 更新代码(已部署用户必读)

拉取新版本只需一条命令(Dev / Compose 本地构建用户):

```bash
git pull
```

> 镜像部署执行 `docker compose pull && docker compose up -d`；两个服务始终使用相同版本。

**整个 `data/` 目录都不纳入 git** —— 行情 K线、财务、自选、回测、监控记录,乃至概念/行业扩展数据,全部是程序运行时生成/拉取的用户数据,`git pull` 物理上无法影响它们。新用户首次启动只创建概念/行业预设配置，分类数据由盘后自动管道或数据页手动同步。

> ⚠️ **切勿使用以下命令"解决冲突"或"清理",它们会一次性删光 `data/` 下所有未被 git 跟踪的数据:**
> - `git clean -fdx`(最危险,会删掉所有 `.gitignore` 忽略的文件)
> - `git reset --hard`
> - 直接删除整个项目文件夹重新 `git clone`
>
> 若 `git pull` 报冲突,通常是本地误改了被跟踪的文件,请先 `git stash` 暂存再 pull,或单独联系作者,不要直接执行上面的命令。

---

## 账号初始化与访问控制

空账号库默认创建内置管理员 `pyxadmin`，可直接登录，无需首次配置或强制改密；默认密码见[账号初始化与部署](deploy-password.md)。首次启动前可同时设置 `AUTH_ADMIN_USERNAME` 与 `AUTH_ADMIN_PASSWORD`（8–128 位）覆盖初始凭据；已有账号时不会追加内置管理员，也不会覆盖现有账号或密码。服务登录后使用，管理员通过设置页管理其他账号。web 与 collector 必须共享 `DATA_DIR` 和账号配置。

升级会保留旧个人文件，并将其复制给账号库记录的初始管理员。备份时包含整个数据目录，不能删除认证数据库来重置密码。完整的会话规则、代理配置与恢复边界见 [账号初始化与部署](deploy-password.md)。
