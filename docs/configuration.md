# 配置详解

启动配置可通过根目录 `.env` 提供(复制 `.env.example` 开始),TickFlow 和 AI 密钥只在后端配置，用户偏好及非密钥选项可在面板 **设置** 页面修改。本文件解释每个配置项的作用。

部署相关配置(端口/密码/老 CPU 兼容)的实操见 [deployment.md](./deployment.md)。

登录后进入看板；新建账号或重置密码后须先修改临时密码。TickFlow 和 AI Key 由后端读取，网页仅显示是否配置，不返回密钥片段，也不提供输入或清除入口。历史 `data/user_data/secrets.json` 凭据仍兼容读取，优先于对应环境配置；迁移到 `.env` 时，先写入并核对，再移除该文件中对应的密钥字段。能力路由从 `data/user_data/preferences.json` 读取,未设置的能力沿用后端默认值。数据目录可由 `DATA_DIR` 更改。

进入页面和读取能力矩阵不会自动改写路由。需要切换时在 **设置 → 数据源 → 能力路由** 手动选择;保存插件 Key 与切换路由是两个独立操作。

---

## 服务运行

| 配置项 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `SERVICE_ROLE` | `web` | 网页只读取本地数据；`app.collector:app` 入口显式启动唯一采集服务。 |
| `COLLECTOR_URL` | `http://127.0.0.1:3019` | 网页转交同步、设置及任务操作的采集地址；Compose 使用 `http://collector:3018`。 |
| `AI_MAX_CONCURRENT` | `2` | collector 同时执行模型 API 请求的上限，含流式请求；本地 Codex CLI 已停用。 |

两个服务必须使用同一个 `DATA_DIR`。启动、独立健康检查和备份恢复见 [部署说明](deployment.md)。

## 数据源:TickFlow

```ini
TICKFLOW_API_KEY=              # 留空 = None 模式(历史日K免费);填 Key = 按订阅档位解锁
```

TickFlow 是内置默认数据源;同时支持插件化接入第三方数据源(YAML 声明自有接口见 [custom-data-source.md](./custom-data-source.md),插件开发见 [plugin-development.md](./plugin-development.md)),在面板 **设置 → 数据源** 切换。

- **留空(None 模式)**:通过 free-api 使用历史日 K(当日数据盘后 1-2 小时可用),**无需付费**即可体验核心选股/回测功能
- **填入 API Key**:按你的订阅档位解锁更多能力

### 当前代码的 TickFlow 能力门槛

以下是仓库 [能力注册表](../backend/app/data_providers/capabilities.py) 的 TickFlow 路由门槛,不是供应商套餐报价或权益说明:

| 能力 | 注册表中的最低档位 |
| :--- | :--- |
| 日 K (`daily`) | `none`(无 Key 也可用免费历史通道) |
| 实时行情 (`realtime`)、除权因子 (`adj_factor`) | `starter` |
| 分钟 K (`minute`)、五档盘口 (`depth5`) | `pro` |
| 财务 (`financial`)、全量分钟 (`full_minute`) | `expert` |

当前 `pro` 不满足财务和全量分钟的 TickFlow 门槛。保留这些功能不代表当前源可以取数;是否可用以设置页能力矩阵的 `usable` 为准。在 **设置 → 凭据与能力** 点「重新检测」可查看当前档位标签。

档位门槛只适用于 TickFlow。插件和 YAML 自定义源按声明数据集及可用状态进入候选名单,每项能力独立路由;内置 stock-sdk 参考插件声明日 K、除权、分钟 K 和实时行情,不提供财务、五档盘口或全量分钟。

### fuyao 补充数据（当前接入方案）

当前项目以 TickFlow 为主要行情源,fuyao 用于补充龙虎榜、竞价风向标和交易日历。在 **设置 → 数据源** 的 fuyao 卡片配置 Key,或在根目录 `.env` 配置 `FUYAO_API_KEY`。配置 Key 不会自动把日 K、实时行情等路由切换为 fuyao。

龙虎榜、竞价风向标与交易日历由独立服务直接调用已就绪的 fuyao 插件,不依赖主行情路由选中它。fuyao 还声明 `daily`、`adj_factor`、`realtime`、`financial`,这些标准数据集按设置中的对应路由使用。它未接入分钟、五档盘口或全量分钟;财务三大报表支持多期,指标仅取最近一期,股本表返回空,能力声明不能替代这些数据覆盖限制。插件契约与实现参考见 [plugin-development.md](./plugin-development.md)。

### 分钟历史范围

分钟查询与采集按不同用途显示单位，不能把同一个数字都理解成“最近 N 天”：

| 入口 | 实际范围 |
| :--- | :--- |
| 个股／ETF 多日分时 | 最多显示最近 N 个已落库交易日，不补采缺失数据；覆盖栏列出实际日期。 |
| 数据页单标的分钟历史 | 按本地交易日历选最近 N 个交易日，范围 1～30；日历未就绪时报错。 |
| 分钟历史大模块、随行情与基础数据同步 | 首次从当前时间回溯 N 个自然日；已有数据从本地最新分钟时间增量更新到当前时间。N 不代表每次都重拉或补齐最近 N 个交易日。 |
| 向前扩展一段／约 365 个交易日 | 从本地最早分钟时间向前扩展；无本地数据则从当前时间回溯。沿用现有自然日估算，节假日会影响实际交易日数，365 不表示一个自然年。 |
| 分段大小 | 按交易日数量估算的自然日区间，只控制批次大小，不承诺每段恰好 N 个交易日。 |
| 指定日期分钟查询／批量分时 | 读取指定单日；未指定日期时读取当前或最近本地日期。 |
| 分钟回测与历史回放 | 按所选起止日期读取本地数据，运行前检查所需区间覆盖。 |

数据源声明的 `minute_history_days` 是可获取的历史深度，单位为交易日；它限制详情的周期选项，不代表本地已有完整覆盖。所有页面查询只读取后台已采集的分钟数据。

### 全量分钟 (full_minute)

「全量分钟」是一项**独立能力**(能力键 `full_minute`,探测名 `intraday.universe`),与其他能力同样**可路由**:盘中把全市场当日 1 分钟 K 持续增量落盘到本地 `data/kline_minute/` 当日分区,分钟策略(`minute_filter`)与分时视图即可读到新鲜数据。接入方式二选一:

- **TickFlow 路径**:当前代码要求检测档位达到 `expert`,由修复轮 `intraday.batch` + 稳态 `intraday.universe` 单请求增量供数;以能力矩阵 `usable` 确认可用
- **插件/自定义源**:声明 `full_minute` 数据集并在 **设置 → 数据源 → 全量分钟** 路由到该源 — Python 插件实现 `get_intraday_batch`(必需)/`get_intraday_latest`(可选,未实现自动降级仅修复轮、节奏下限 60s);YAML 声明式源数据集配置与 `minute` 同形(仅修复轮语义)。契约细节见 [plugin-development.md](./plugin-development.md) 与 [custom-data-source.md](./custom-data-source.md)

接入步骤:

1. 在后端 `.env` 配置 `TICKFLOW_API_KEY` 并重启网页与采集服务，在 **设置 → 数据源** 重新检测能力，或在 **设置 → 数据源** 声明/安装提供 `full_minute` 的源并路由;确认能力矩阵中「全量分钟」可用
2. **开启实时行情**后落盘服务自动启动;仅连续竞价时段(9:30–11:30 / 13:00–15:00)运行,午休/收盘自动暂停与恢复
3. 冷启动(如 10 点才开服务)自动触发**全天修复轮**,一次批量补齐 9:30 起的全部缺口;稳态走**增量轮**(默认 6 秒一轮,可配 3–120 秒),幂等合并滚出全天
4. 与盘后分钟同步写同一分区(`unique(symbol, datetime)` 幂等合并),互不冲突

说明:标的池为 A 股股票(CN_Equity_A),ETF 不在内(分时走批量补拉路径);覆盖滞后超阈值或连续空轮会自动再跑修复轮自愈。

---

## AI(可选)

用于声明式条件生成、数据问答、分析与复盘。未配置时跳过对应 AI 功能；支持 OpenAI 兼容的模型接口。

```ini
AI_PROVIDER=openai_compat              # openai_compat | ollama
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=                            # 留空 = 关闭 AI
AI_MODEL=deepseek-chat
AI_DAILY_TOKEN_BUDGET=500000           # 每日 token 预算上限
```

| 配置项 | 说明 |
| :--- | :--- |
| `AI_PROVIDER` | `openai_compat`(OpenAI 兼容,支持 DeepSeek / 通义 / OpenAI 等)或 `ollama`(本地模型) |
| `AI_BASE_URL` | 接口地址,如 DeepSeek `https://api.deepseek.com/v1` |
| `AI_API_KEY` | 留空则关闭 AI 功能 |
| `AI_MODEL` | 模型名,如 `deepseek-chat` |
| `AI_DAILY_TOKEN_BUDGET` | 每日 token 预算,超限后当日不再调用 |

在后端 `.env` 配置 `AI_API_KEY`，重启网页与采集服务后生效。AI 页面保留模型、接口地址等非密钥选项与连通测试；原密钥写入和清空接口返回 403。旧客户端读取的 `tickflow_api_key_masked`、`ai_api_key_masked` 字段保留为空字符串。

条件信号和受信任策略的使用边界见 [策略指南](strategy.md)；网页 Python 策略代码入口已关闭。

---

## 服务

```ini
HOST=0.0.0.0          # 开发服务监听地址 / Docker 主机绑定地址
PORT=3018             # 开发后端端口 / Docker 主机映射端口
LOG_LEVEL=INFO        # DEBUG | INFO | WARNING | ERROR
```

- `HOST`:`0.0.0.0` 监听所有网卡(容器/公网部署需要);仅本机用可设 `127.0.0.1`
- `PORT`:默认 `3018`;开发模式兼容显式的 `BACKEND_PORT` 覆盖,改端口后 SSH 转发命令也要同步改
- `LOG_LEVEL`:排查问题时改 `DEBUG`

---

## 数据

```ini
DATA_DIR=./data       # Parquet / DuckDB 数据存储目录
```

整个 `data/` 目录都不纳入 git —— 行情 K线、财务、自选、回测、监控记录,乃至概念/行业扩展数据,全部是程序运行时生成/拉取的用户数据。

同一环境的整站备份与恢复见 [部署指南](deployment.md#备份与恢复)。本地向预发只按 [公共数据同步约定](deployment.md#按发布需要同步公共数据) 处理选定数据集，账号、个人数据和配置由各环境独立维护。

---

## 账号与认证

```ini
# 可选：同时填写，仅覆盖空账号库的首次管理员；留空使用内置 pyxadmin
AUTH_ADMIN_USERNAME=
AUTH_ADMIN_PASSWORD=
AUTH_COOKIE_SECURE=false
AUTH_TRUSTED_PROXIES=
AUTH_ALLOWED_ORIGINS=
```

初始管理员仅在账号库为空时创建；两项自定义凭据都留空时使用内置 `pyxadmin`，可直接登录，无需首次配置或强制改密；自定义须同时填写，空账号库仅配置一项会报错。密码 8–128 位。账号保存在 `data/auth/auth.sqlite3`，已有账号时不会追加内置管理员，环境变量不会重置已有账号或密码。`AUTH_PASSWORD` 与旧 `auth.json` 已不参与认证。HTTPS 部署可强制 Secure Cookie；代理 IP/CIDR 与额外允许来源均需明确配置，默认不信任转发头。默认密码及详细说明见 [账号初始化与部署](deploy-password.md)。

---

## 后端依赖 Extras(可选)

```ini
BACKEND_EXTRAS=             # 留空默认;legacy-cpu 兼容老 CPU
```

老 CPU 无 AVX2/FMA 支持时设为 `legacy-cpu`,会给 Polars 切到 `rtcompat` 运行时;需回测则 `legacy-cpu backtest`。Docker 构建和 `./dev.sh` 都会读取此值并同步依赖。详见 [deployment.md → 老 CPU 兼容](./deployment.md#老-cpu-兼容avx2fma-缺失)。

---

## 配置优先级

TickFlow / AI 凭据及 AI 选项：历史 `data/user_data/secrets.json` > 系统环境变量 > 根目录 `.env` > 默认值。TickFlow 和 AI 密钥只能从后端配置；网页保存的 AI 非密钥选项仍写入 `secrets.json` 并立即生效。

启动配置由系统环境变量优先于 `.env` 提供。个人偏好通过设置页保存到 `data/users/<user_id>/user_data/preferences.json`，系统共享配置仍在 `data/user_data/preferences.json`。更改 `.env` 后需重启网页与采集服务，两者必须读取相同配置。

`.env` 不提交到 Git，建议文件权限为 `600`。迁移历史凭据时只清理对应字段，保留其他用户配置。
