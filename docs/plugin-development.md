# 数据源插件

Python 插件位于 `backend/app/plugins/<name>/`，与 [HTTP YAML 数据源](custom-data-source.md) 共用注册表和能力路由。现有实现是 `fuyao/` 和 `stocksdk/`；加载入口为 [`custom/loader.py`](../backend/app/data_providers/custom/loader.py)，方法实现可参考 [`GenericHTTPProvider`](../backend/app/data_providers/custom/provider.py)。

## 清单与加载

插件目录提供 `plugin.yaml` 和 provider 实现：

```yaml
name: my_source
display_name: 我的数据源
runtime: none
entry: app.plugins.my_source.provider:MyProvider
check: app.plugins.my_source.provider:availability
datasets: [daily, adj_factor, minute, full_minute, realtime, depth5, financial]
api_key_env: MY_SOURCE_API_KEY
hidden: false
description: 数据覆盖范围
install_hint: 依赖安装说明
homepage: https://example.com
```

`name` 仅允许小写字母、数字和下划线。`entry` 指向可无参构造的 provider 类，`check` 为可选的可用性检测函数，返回 `(bool, reason)`。检测或导入失败只禁用该插件，设置页保留不可用原因；`hidden: true` 直接跳过注册与展示。

`runtime` 为 `none/python/node`，用于展示和依赖安装选择：Python 插件从目录的 `requirements.txt` 安装，Node 插件在本目录安装包，`none` 无需额外安装。实际可用性由 `check` 决定。stock-sdk 的 Docker 镜像默认不包含依赖，需要时通过 `INCLUDE_STOCKSDK=1` 构建，见 [部署说明](deployment.md)。

声明 `api_key_env` 后，设置页支持输入 Key。entry 模块需提供 `probe_api_key(key) -> (ok, reason)`；候选 Key 探测有效后才保存并重载。provider 使用 `secrets_store.get_env_backed_secret("<name>_api_key", api_key_env)` 读取，`data/user_data/secrets.json`（0600 权限）优先于环境变量和 `.env`。

## 路由与内部字段

provider 无需继承基类，需有 `name`、`config.datasets` 和 `close()`；仓库内插件设置 `builtin = True`，使其不能被当作用户 YAML 编辑或删除。loader 重建注册表前会调用 `close()`。

`provider_has_dataset(name, dataset)` 检查 `provider.config.datasets` 的键。只声明真正支持的数据集；未声明时既有通用路径使用 TickFlow，失败后的具体降级行为见下文，盘口不跨源回退。能力矩阵及各数据源偏好由已有 services 路由管理，provider 不自行选择另一个源。

能力声明是数据集级，不能按股票、ETF、指数分别选择。`realtime` 只含股票时，ETF 实时数量为 0，指数由独立补拉或本地日线兜底；应在 `description` 写明覆盖范围。

| 字段 | 内部口径 |
| --- | --- |
| `symbol` | 带交易所后缀，例如 `600519.SH`、`000001.SZ`，ETF、指数同形 |
| `change_pct`、实时入口 `turnover_rate` | 小数制，`0.0366` 表示 `3.66%`；enriched 的换手率另存百分数值 |
| `volume`、盘口买卖数量 | 手，1 手 = 100 股 |
| `amount`、成交额 | 元 |
| 日 K `open/high/low/close` | 不复权原始价，由因子和 enriched 管道处理复权 |
| 分钟 `datetime` | 北京时间墙钟，无时区，例如 `2026-08-28 09:35:00` |

字段和单位在 provider 边界适配，不能用数值大小猜测单位。缺少供应商字段时返回 `None`；可确定的涨跌幅按 `change_amount / prev_close` 推导。响应结构变化到无法识别标的时应记录告警，不能静默当作成功空数据。

## 方法契约

只需实现已声明数据集对应的方法。以下列表中的 `on_chunk_done(done, total)` 表示已完成批次和总批次，空批次也推进，结束时应相等。

| 数据集／方法 | 返回与要求 |
| --- | --- |
| `get_daily(symbols, start_time, end_time, asset_type="stock", on_chunk_done=None)` | Polars DataFrame：`symbol, date, open, high, low, close, volume, amount` |
| 可选 `iter_daily(...)` | 与 `get_daily` 同形的有界分批迭代器，日线同步优先消费；流正常结束后才提交 staging，异常丢弃 staging |
| `get_adj_factors(symbols, start_time, end_time, asset_type="stock", on_chunk_done=None)` | DataFrame：`symbol, trade_date, ex_factor`；可选 `dividend, bonus, allot, allot_price, prev_close`，用于等差投影与全精度因子链重建 |
| `get_minute(symbols, start_time, end_time, asset_type="stock", on_chunk_done=None, freq="1m")` | DataFrame：`symbol, datetime, open, high, low, close, volume, amount`，成交额可空 |
| `get_realtime()` | 全市场快照 `list[dict]`，字段见下表 |
| 可选 `get_realtime_indices(symbols)` | 与实时快照同形；失败 `None` 保留上轮缓存，成功无数据 `[]` |
| `get_depth_batch(symbols)` | 以 symbol 为键的五档盘口字典，结构见下文 |
| `get_financials(table, symbols, latest_only=False, on_chunk_done=None, *, cancel_event=None, start_date=None, end_date=None)` | 财务 DataFrame；可选参数按显式签名接入，详见财务契约 |
| 可选 `get_instruments(asset_type="stock")` | TickFlow Instrument 形状的字典列表，由 `instrument_sync` 复用 flatten |
| 可选 `test_dataset(dataset, symbols=None)` | 设置页试拉结果：`provider, dataset, rows, columns, preview`，失败加 `error` |

`iter_daily` 每批必须有明确大小上界，不能先收集全部结果再切批。未实现时继续使用 `get_daily`。同步流程已有的单标的软失败行为不因分批接口改变。

### 实时快照

| 字段 | 要求 |
| --- | --- |
| `symbol` | 必填，标准代码 |
| `last_price`、`prev_close`、`open/high/low`、`volume` | 必填，当前价、昨收、当日 OHLC、成交量 |
| `amount`、`change_pct`、`change_amount` | 建议提供，单位见上表 |
| `timestamp` | 建议使用行情服务端的毫秒时间戳，缺失时退回本地时间 |
| `name` | 可选，缺失时关联本地标的维表 |
| `amplitude`、`turnover_rate`、`session` | 可选，比例为小数制，缺失留空 |

自定义源分支会按鸭子类型调用 `get_realtime_indices`。未实现时指数使用本地日 K 兜底。fuyao 指数接口混入不支持代码可能整批失败，插件需先过滤不支持的后缀。

实时默认约 6 秒轮询一轮，fetch 锁防止重叠，实际周期包含拉取耗时。分页取数需设置空页终止、最大页数和限频，避免全量快照持续堆积请求。

### 分钟与全量分钟

分钟入口的 `_enforce_minute_beijing_wallclock` 会转换带时区数据，并对整体具有 UTC 特征的 naive 时间作 +8 小时纠偏；无法识别时拒收并回退 TickFlow。provider 仍应直接返回北京墙钟。缺可靠成交额时返回空值，前端停止绘制后续累计均价并显示 `—`。

可用类属性 `minute_history_days` 声明 1 分钟历史交易日深度；浅源会收窄前端可选范围，未声明按深历史处理。stock-sdk 当前为最近 5 个交易日。

声明 `full_minute` 后，在设置页将全量分钟路由到该源，由 `minute_refresh` 调度：

- `get_intraday_batch(symbols, count=300, asset_type="stock") -> pl.DataFrame` 提供当日 1 分钟 K，返回同分钟接口的 8 列；服务在冷启动、覆盖断档或连续空轮时调用，provider 自行分块限速。缺少该方法时兼容调用 `get_minute`。
- 可选 `get_intraday_latest(symbols=None, count=3)` 提供全市场每只最新几根，用于稳态增量轮；未实现时仅运行修复轮，间隔下限为 60 秒。

两种结果均经过时区守卫。异常或空结果按空轮处理，连续空轮触发修复。YAML 源只提供修复轮。

### 五档盘口

价格和数量数组按一至五档排列，数量为手，`timestamp` 为毫秒时间戳：

```python
{
    "600519.SH": {
        "bid_prices": [1500.0, 1499.9, 1499.8, 1499.7, 1499.6],
        "bid_volumes": [10, 20, 30, 40, 50],
        "ask_prices": [1500.1, 1500.2, 1500.3, 1500.4, 1500.5],
        "ask_volumes": [12, 22, 32, 42, 52],
        "timestamp": 1788505200000,
    },
}
```

服务按能力的 `batch/rpm` 分片限速，单批异常隔离并保留其他批次，不切换数据源。

## 财务版本时间契约

`symbol`、`period_end` 标识股票和报告期，财务数值使用各表现有 canonical 字段。默认正式报表 `disclosure_type=formal`；预告、快报必须显式标为 `forecast/express` 并分别保留，当前因子只消费正式报表。供应商 `announce_date` 不能直接当作首次披露或当前数值版本日期。

只有确认日期对应当前返回数值版本，才声明 `date_basis=source_update` 和 `source_update_date=YYYY-MM-DD`。首次采集同源同股同表时可用该日期保守回填；之后新增报告期、修订和日期变化均取源版本日期与本地实际观察日的较晚者。没有可信版本日期时只从实际观察日使用。

版本服务保存 `source`、`observed_at`、`available_date`、`availability_basis`；provider 不得用文件时间或报告期伪造本地证据。`available_date` 是保守可用日，日频和分钟消费均从其后首个交易日开始。

- `financials/{table}/part.parquet` 保存每期最新完整浏览记录，`financials/versions/{table}/part.parquet` 追加完整供应商版本。同内容复用首次观察时间；变化另存，不跨源、跨版本逐列补空。
- 旧浏览文件没有来源和观察证据，继续可读但不能用于回测历史；通过单股获取或策略／因子按需任务建立版本记录。版本库损坏时停止写入，不能当空历史覆盖。
- 因子按当时可用的最新报告期选完整同源记录，旧季度晚更新不替代新季度；同比使用同源上年同期、比较期绝对值作分母，比较期修订只影响生效后结果。缺财务输入不入选、不参与排名，也不触发因子分数离场；独立卖出与风控沿用原规则。
- 财务因子每次从版本库附加，不写入行情矩阵磁盘缓存。业绩断层也只读该版本库，不另取数；业务规则见 [架构说明](ARCHITECTURE.md)。

### 取数、取消与支持范围

应用通过共享按需服务逐股逐表调用 `get_financials(..., latest_only=False)`，不提供页面全市场同步。财务页默认请求近两年报告期，“采集更多”每次向前一年，股票／表／源分别持久化边界；成功空年度可继续向前，失败不移动边界，范围外已存数据保留。策略与研究任务按各自依赖准备历史，不扫描 provider 全市场池或修改旧批次断点。

同源同股同表的在途任务共享结果，当日成功后复用，跨日再取；失败可重试且保留已有文件。`financial_tables` 可声明当前支持的表名元组，未声明沿用 `income/balance_sheet/cash_flow/metrics/shares` 五表。

- 显式声明 `on_chunk_done` 的源自行分批、并行、控频，并由协调线程报告 `(done, total)`；旧签名由服务按 100 股调用。回调抛出的取消向上传播，未完成表不能返回部分结果。
- 显式声明 `cancel_event` 时传入任务取消信号，应在限频等待和请求阶段响应；内部停止信号需与任务取消区分，不能将取数失败记为取消。
- `start_date/end_date` 为可选 `datetime.date`，两端包含，限制报告期而非公告日。显式声明时在上游限界；旧签名由服务过滤返回结果，上游量与历史深度仍受源限制。
- 可选 `get_financial_universe()` 返回 canonical 股票代码范围，供旧内部批次使用并记录到 `financials/universe.json`；按需入口不调用。未实现用本地维表，异常或空清单停止旧批次，不删除基础维表及历史报表。
- 缺数、限流、网络错误必须传播，不能生成占位版本或记为当日成功。上游限流使用 `ProviderRateLimitError`，保留当前表原文件。
- 已复核的单期缺数可抛 `ProviderFinancialDataUnavailableError(data, missing_symbols, missing_periods={symbol: ["YYYY-MM-DD"]})`，其中 `data` 仅含成功报告，报告期按 canonical 股票代码分组。页面按需获取保存这些完整报告并返回 `complete=false`、`missing_periods`，本地 GET 也返回待补报告期；缺期阻止对应范围命中完整缓存，较早年度保持原补采游标。计算入口仍传播该异常。未提供结构化报告期、网络失败或限流均不走部分成功路径。

fuyao 当前提供 `income/balance_sheet/cash_flow/metrics`，无 `shares` 接口。三大报表回查源可返回的窗口（最近 20 期），`report_date_ms` 按实测更新日期保存；首次披露至更新之间的原版本仍可能缺失。指标接口仅提供最新期且无可信版本日期，从本地观察后使用，不虚构历史或用净利润除权益替代加权 ROE。估值反推 BPS 标为 `bps_basis=valuation_snapshot`，只供浏览；财报 PB 使用不复权价和可用财报 BPS，缺输入留空，不重算股本变动后的财报 BPS。

fuyao 只读 GET 的连接、超时、远端断流最多尝试 3 次，共用 0.5 秒间隔；财报与指标端点另共用 2 秒发包间隔。HTTP／业务限流不重试，等待期间响应取消，固定间隔不保证避开供应商动态限流。

当前可选财务因子保留毛利率、净利率、营收增速和归母净利润增速；PB、ROE、资产负债率已退出目录。旧研究格式的时间线仍可读，旧策略缺失评分不静默重分权重，缺少现金流等输入时保留缺失。

## 盘后完整性与异常语义

交易日盘后验收遵循 [盘后管道](postmarket-pipeline.md)。请求成功、旧分区和少量当天行均不等于全市场完整。

- `get_instruments` 的 `ext.is_current` 可声明 Boolean 当前 A 股成员状态，`instrument_sync` 将其保存在维表。自动股票池只排除明确的 `false`；缺字段或 `None` 保持兼容，不根据名称、缺日线或历史最后交易日推测退市。名单中停牌股仍需目标日停牌证据。未实现 `get_instruments` 时沿用 TickFlow 元数据边界，自动任务会额外获取本轮 `CN_Equity_A` 完整成员名单；历史代码仍保留在维表。
- 若日线已过滤停牌行，可实现 `get_daily_halted_symbols(day: date) -> set[str]`，只返回目标日有明确停牌证据的完整代码。缺行、旧数据或请求失败不能推断为停牌。无此方法仍兼容，无法解释的缺行会停止环境计算并稍后重试。
- 自动任务会在正式取数之后，对仍未解释的缺行核对独立的东方财富按日停复牌与 IPO 日程（`data_providers/trading_status.py`）；无需插件新增方法。只接收覆盖全天的停牌区间、晚于目标日的上市日期或日程明确的“待上市”状态；复牌／上市当天仍需日线。此补充只提供状态证据，不跨源替代行情，不能掩盖插件取数异常或缺少能力。状态源失败或不完整时任务继续失败并重试，见盘后管道约定。
- `get_adj_factors` 可选接受 `require_complete=False`。自动管道在支持时传 `True`，上游失败必须抛出，合法无事件才返回空表。旧签名仍支持。
- 长驻 provider 的近期日线、因子缓存需检查上游版本。fuyao 每 5 分钟检查进程缓存，磁盘仍按 release 复用。

| 方法 | 失败行为 |
| --- | --- |
| `get_realtime` | 返回 `[]` 并记 warning，轮询线程继续 |
| `get_realtime_indices` | 返回 `None` 并记 warning，保留有效缓存；成功空数据用 `[]` |
| `get_depth_batch` | 服务隔离单批异常，不跨源回退 |
| `get_minute` | 抛异常后调用方可回退 TickFlow |
| `get_daily/get_adj_factors/get_financials` | 异常由同步流程记录，无数据返回空 DataFrame；完整性和财务失败要求仍适用 |
| `iter_daily` | 异常丢弃 staging，完整结束才提交 |

## 现有验证入口

`backend/tests/test_fuyao_provider.py`、`test_stocksdk_provider.py`、`test_custom_depth_provider.py`、`test_custom_minute_config.py` 和 `test_custom_loader_install.py` 覆盖现有实现。修改 provider 时使用假 client 或桥接，不依赖真实 Key／网络，验证实际字段及单位、日期、空数据、失败／取消、分页和能力声明；更改 Key 或加载机制时检查探测、保存和失败隔离。
