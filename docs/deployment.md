# 部署指南

本地开发、Docker 与服务器预发的运行和恢复方式。配置项详解见 [configuration.md](./configuration.md)。

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

同一 Environment 的 Variables 为 `DEPLOY_DIR`（默认 `/opt/stock-panel`）、`DEPLOY_PORT`（默认 `22`）、`DEPLOY_URL`（默认 `https://shuaihub.cn`，同时用于配置 Caddy 域名与公网健康检查）。`DEPLOY_URL` 只接受 HTTPS 根域名地址，可有末尾 `/` 或显式 `:443`，不支持子路径、其他端口、账号密码、查询或片段。目标机需已完成双服务初始化，并具备 Bash、Python 3.12、OpenSSH、Docker Compose 2.24.4+ 和 `flock`；运行根目录必须已有 `.env`、`tiers.yaml`、`docker-compose.yml`、`docker-compose.override.yml` 与运行数据。密钥须能以非交互方式登录，服务器需启用公钥认证。

[CI 入口](../scripts/ci_deploy.sh) 只上传 `git archive` 生成的本次提交源码。[服务器入口](../scripts/deploy_preproduction.sh) 将其放入运行根目录的 `releases/<SHA>/`，先构建新镜像，再用该版本的 Compose 定义、远端已有覆盖配置和精确镜像版本启动双服务。数据目录、环境文件及挂载配置始终从远端运行根解析，镜像版本覆盖不会改写原部署配置。首次自动发布可接管现有手动部署；后续版本通过 `current` 指向已验收的源码目录。发布不会上传金融数据或个人数据。

新镜像继承远端现有 Compose 中 web 服务已解析的全部 `build.args`，包括 `BACKEND_EXTRAS`、`INCLUDE_STOCKSDK` 和镜像源选项；发布脚本不另行覆盖这些设置。

服务器文件锁覆盖发布和恢复过程。切换前若检测到未完成的持久化采集／挖掘任务，或仍运行的独立策略计算 worker，发布会失败，待任务结束后重跑 GitHub 工作流。进程内的信号／因子回测及尚未启动 worker 的排队任务不在这项检查范围内，发布前应结束这些任务；状态检查与实际切换之间也存在短暂竞态。若需要在持续运行研究任务时发布，应增加 collector 的维护准入机制，当前脚本不提供这项能力。

更新涉及服务重建，会有短暂不可用，不提供零停机保证。健康验收要求应用与 Caddy 容器正常、镜像正确、collector 未暴露宿主端口，web 的 `/health` 已连接 collector，并通过域名 SNI 与可信证书校验的本机 HTTPS 请求；失败时尝试按旧版本 Compose、镜像与代理配置恢复。成功后才更新 `current` 和 `.deployed-commit`，runner 再从公网检查 HTTPS。镜像恢复不回滚运行数据，涉及持久化格式变更的版本应单独安排兼容迁移与备份。

首次自动发布成功后，使用当前版本的管理入口，避免根目录旧 Compose 定义把服务切回最初镜像：

```bash
bash /opt/stock-panel/current/scripts/preproduction_compose.sh /opt/stock-panel ps
bash /opt/stock-panel/current/scripts/preproduction_compose.sh /opt/stock-panel logs --tail 100 web collector
bash /opt/stock-panel/current/scripts/preproduction_compose.sh /opt/stock-panel restart web
```

发布失败可在 GitHub Actions 中查看失败步骤；修复后重新运行时仍会检查 `main` 当前提交，历史任务不能覆盖新版本。服务验收与命令替身测试不替代第一次 GitHub runner 到服务器的真实发布验收。

### Docker HTTPS 与 Caddy

自动发布将 `DEPLOY_URL` 的域名交给 Caddy，无需手动申请腾讯云证书或配置 DNS API 密钥。以当前默认域名为例，部署验收成功后访问 `https://shuaihub.cn`，HTTP 自动跳转到 HTTPS。

发布前确认：

- 域名 A 记录指向服务器公网 IPv4；若存在 AAAA 记录，也须指向这台服务器可用的 IPv6。
- 服务器安全组和系统防火墙允许公网 TCP 80、443，且没有其他程序占用这些端口。旧 `web` 的 80 端口映射会由发布流程接管。
- 服务器可访问镜像仓库和 ACME 证书服务。首次证书签发可能需要等待数分钟；签发失败不会被当作健康发布。

[docker-compose.https.yml](../docker-compose.https.yml) 在服务器覆盖文件之后加载，用 `!override` 替换旧公网 HTTP 端口，而非叠加映射。Caddy 对外提供 80/443；`web` 仅保留 `127.0.0.1:3018` 的本机健康入口，`collector` 继续只在内部网络可达。日常本地开发仍使用原 `docker-compose.yml`，不会启动 Caddy。

Caddy 与 `web` 使用专用代理网络；默认子网 `172.30.255.0/24`、Caddy 地址 `172.30.255.2`，自动分配池限制为 `172.30.255.128/25`，避免 web 先占用代理地址。HTTPS 覆盖层为 `web` 设置 `AUTH_COOKIE_SECURE=true`，并只信任该代理的 `/32`，因此登录、同源写请求、真实访问者 IP 和 SSE 可沿原有实现工作，不需要放宽允许来源。

若现有服务器网络或端口冲突，可在服务器已有 `.env` 中单独调整下列配置。自动分配池须包含于所选子网，代理 IP 须处于该子网内、自动分配池外：

```ini
HTTPS_PROXY_SUBNET=172.30.255.0/24
HTTPS_PROXY_IP=172.30.255.2
HTTPS_PROXY_RANGE=172.30.255.128/25
HTTPS_BACKEND_PORT=3018
```

证书与 ACME 账号保存在 Compose 项目 `stock-panel` 的 `caddy_data` 持久卷，运行配置保存在 `caddy_config` 卷，正常发版和回滚均保留。Caddyfile 来自本次 release 的 [deploy/Caddyfile](../deploy/Caddyfile)；发布生成 `.deploy-https.json`，记录域名、实际 Caddy 镜像 ID 和该版本配置的绝对挂载路径，不改写服务器 `.env` 或原有覆盖文件。同一提交重跑复用已记录的代理版本；调整域名时使用下一次发布提交。

首次 HTTPS 发布失败时，脚本先停止新增 Caddy 释放端口，再恢复旧 HTTP 服务；已有 HTTPS 发布失败时恢复旧代理版本、旧 Caddyfile 和应用镜像。公网二次检查发生在服务器事务完成后，其失败会使 CI 失败，但不会自动回滚已通过本机 TLS 验收的版本，应继续检查公网 DNS、防火墙和链路。

发布后查看代理日志：

```bash
bash /opt/stock-panel/current/scripts/preproduction_compose.sh /opt/stock-panel logs --tail 100 caddy
```

直接执行服务器发布脚本时，原有三参数调用仍支持无代理 HTTP 部署；可增加第四参数域名启用 HTTPS。已有 HTTPS 版本的三参数更新会沿用原域名，不会降级为 HTTP。

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

stock-sdk 插件默认不打包。需要时显式启用构建参数；镜像会额外安装 Node.js 与插件依赖：

```bash
docker compose build --build-arg INCLUDE_STOCKSDK=1
docker compose up -d
```

更新到新版本:

```bash
git pull
docker compose up --build -d
```

---

## 本地开发

需要 Python 3.11+、Node.js 20+、`uv` 与 `pnpm`。在已有仓库根目录启动：

```bash
# 首次启动且尚无 .env 时，从 .env.example 复制并配置
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

备份整个 `data/`（包括行情、用户配置、策略、报告和任务记录），同时保存 `.env`、`tiers.yaml` 与服务器覆盖配置。为获得一致备份，先停止服务，拷贝到独立备份目录，再启动。已接入自动发布的服务器预发使用当前版本入口，确保同时加载应用版本与 HTTPS 配置：

```bash
bash /opt/stock-panel/current/scripts/preproduction_compose.sh /opt/stock-panel stop
# 复制运行数据与配置到备份位置，确认完成后：
bash /opt/stock-panel/current/scripts/preproduction_compose.sh /opt/stock-panel up -d --no-build
```

本地或手动 Compose 部署使用原入口：

```bash
docker compose stop
# 将 data/、.env、tiers.yaml 复制到你选择的备份位置
# 确认备份完成后：
docker compose up -d
```

恢复时停止服务，用备份替换目标目录并确认挂载路径和文件权限，再启动同版本镜像。不要在 collector 写入时覆盖数据。备份包含密钥与个人数据，应保存在受控位置。

目录备份不包含 Caddy 的 `stock-panel_caddy_data` 与 `stock-panel_caddy_config` Docker 卷。整站迁移若需保留证书和 ACME 账号，还须在 Caddy 停止期间单独备份并恢复这两个卷。

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

## 更新代码

本地开发更新代码和依赖后重启启动脚本；Compose 本地构建更新后执行 `docker compose up --build -d`；已有镜像部署使用 `docker compose pull` 后再启动两个同版本服务。服务器预发按本文的自动发布流程管理。

更新前保存运行数据与环境配置，遇到代码冲突逐文件处理。`data/` 不受 Git 跟踪，`git clean -fdx` 会连同被忽略的数据和密钥文件一起删除，不能用于整理运行目录。分类预设只初始化配置，数据由盘后管道或数据页采集。

## 账号初始化与访问控制

空账号库默认创建内置管理员 `pyxadmin`，可直接登录，无需首次配置或强制改密；默认密码见[账号初始化与部署](deploy-password.md)。首次启动前可同时设置 `AUTH_ADMIN_USERNAME` 与 `AUTH_ADMIN_PASSWORD`（8–128 位）覆盖初始凭据；已有账号时不会追加内置管理员，也不会覆盖现有账号或密码。服务登录后使用，管理员通过设置页管理其他账号。web 与 collector 必须共享 `DATA_DIR` 和账号配置。

升级会保留旧个人文件，并将其复制给账号库记录的初始管理员。备份时包含整个数据目录，不能删除认证数据库来重置密码。完整的会话规则、代理配置与恢复边界见 [账号初始化与部署](deploy-password.md)。
