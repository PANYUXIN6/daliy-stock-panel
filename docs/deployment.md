# 部署指南

本项目的几种运行方式，按推荐程度排序。配置项详解见 [configuration.md](./configuration.md)。

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

**整个 `data/` 目录都不纳入 git** —— 行情 K线、财务、自选、回测、监控记录,乃至概念/行业扩展数据,全部是程序运行时生成/拉取的用户数据,`git pull` 物理上无法影响它们。新用户首次启动只创建概念/行业预设配置，分类数据由维护者在数据页手动同步。

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
