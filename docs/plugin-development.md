# 数据源插件开发指南

数据源插件是可选的行情数据来源(fuyao、stock-sdk、akshare 等),作为独立模块放在
`backend/app/plugins/` 下。services 层(kline_sync / quote_service / financial_sync)
全部通过统一路由点分流:插件声明了某数据集就走插件,未声明自动回退 TickFlow。
因此**一个合格的插件只需要正确实现契约,不需要改动任何 service / API 代码**;
反过来,插件也必须遵守内部数据契约(单位、代码格式、复权口径),框架不会替你转换。

> 无代码接入(纯 HTTP YAML 配置)请看 [custom-data-source.md](./custom-data-source.md),
> 两种方式遵循同一套内部数据契约。

## 快速上手

一个插件 = 一个目录 + 一个 `plugin.yaml` 清单:

```
backend/app/plugins/<your_plugin>/
├── plugin.yaml          # 清单(必需)
├── provider.py          # Provider 实现(必需)
├── ...                  # client/桥接/依赖文件(按需)
```

### plugin.yaml 字段

```yaml
name: my_source                          # 唯一标识, 只允许 [a-z0-9_], 也是 provider name
display_name: "我的数据源"                 # 设置页显示名
runtime: none                            # 运行时类型: node | python | none
entry: app.plugins.my_source.provider:MyProvider   # provider 类的导入路径
check: app.plugins.my_source.bridge:availability   # 可用性检测函数(可选)
datasets: [realtime]                     # 支持: daily/adj_factor/minute/realtime/depth5/financial
api_key_env: MY_SOURCE_API_KEY           # (可选)声明后设置页提供 Key 输入框
hidden: false                            # (可选)true = 已加载但对设置页隐藏,不注册不展示
description: "数据源描述"
install_hint: "pip install xxx"          # 未装依赖时显示的安装提示
homepage: "https://example.com"          # (可选)官网/申请地址, 显示在设置页 Key 配置说明中
```

只声明真实提供的数据集;未声明的数据集 `provider_has_dataset` 返回 False,自动回退
TickFlow。不要声明做不了的数据集(粒度含义见下文"能力声明的粒度")。

#### api_key_env(界面配置 API Key)

声明 `api_key_env` 的插件可以在设置页的数据源卡片中直接填写 Key, 对齐
TickFlow 的「先探后存」语义:

1. entry 模块需提供模块级 `probe_api_key(key) -> (ok, reason)`,
   后端用候选 Key 实探一次, **无效不落盘**
2. 有效则写入 `data/user_data/secrets.json` 的 `{name}_api_key` 字段
   (0600 权限, 优先级高于 `.env` / 环境变量)
3. 保存后自动重载数据源注册表, 插件即刻变为可切换
4. 插件取 Key 用 `secrets_store.get_env_backed_secret("{name}_api_key", api_key_env)`,
   保证 secrets.json 与 .env 两条配置路径一致

### runtime 字段说明

| runtime | 含义 | 典型场景 |
|---|---|---|
| `python` | 纯 Python 依赖, `pip install` | akshare、tushare |
| `node` | 需要 Node.js 运行时, `npm install` | stock-sdk |
| `none` | 无额外依赖 | 纯 HTTP API 源 |

> ⚠️ stock-sdk 在 Docker 中默认不打包(合规考虑:它抓取第三方财经网站接口,存在版权与
> 反爬风险)。如需启用,构建时传 `--build-arg INCLUDE_STOCKSDK=1`,使用风险自负。
> 详见 [deployment.md](./deployment.md)。

`runtime` 字段当前仅用于 UI 展示, 实际依赖检测由 `check` 函数负责。

### check 函数

插件自己负责检测依赖/Key 是否就绪。后端启动时会调用此函数:

```python
# app/plugins/my_source/provider.py (或 bridge.py)
def availability() -> tuple[bool, str]:
    """返回 (是否可用, 原因)。不抛异常。"""
    if not get_api_key():
        return False, "未配置 MY_SOURCE_API_KEY(可在设置页数据源卡片中直接填写)"
    return True, "ok"
```

- **可用** → 插件注册进路由表, 设置页可切换
- **不可用** → 设置页显示插件卡片但灰显, 展示原因/`install_hint`

## 内部数据契约(所有数据集必须遵守)

以下口径是全项目红线(详见 CONTRIBUTING §3)。金融数据错误往往不抛异常,而是生成
**看似合理的错误结果**——单位、代码格式、复权口径错了,页面照样能渲染,只是数字全错。
插件必须在 provider 内完成适配。

### 代码格式

- symbol 统一带交易所后缀: `600519.SH` / `000001.SZ` / `300750.SZ`; ETF、指数同格式。
- 接口返回裸代码(如 `600519`)或异构格式时,在 client 层实测一页并归一,不要直接透传。

### 单位制

| 字段 | 契约 | 说明 |
| --- | --- | --- |
| `change_pct` | **小数制**, `0.0366` = 3.66% | 接口给百分数(3.66)时必须在 provider 内显式 /100 |
| `turnover_rate`(realtime 入口) | **小数制**, `0.05` = 5% | 下游 enriched 管道统一转百分数值存储 |
| `volume` | **手**, `436231` = 43,623,100 股 | 日K 与实时快照均以手计(1手=100股), 股票/ETF/指数一致(与上游 TickFlow 口径一致, 可用 amount÷volume÷100≈当日均价自验); 接口给股时必须在 provider 内显式 /100(参考 fuyao) |
| `amount` / `turnover` | 元 | |
| 日K OHLC | **不复权原始价** | 复权由 adj_factor + enriched 管道处理, provider 不得自行复权 |

### 缺字段与空数据

- 接口不提供的字段返回 `None`,禁止"数值小于 1 就乘 100"之类启发式补全——那会掩盖
  真实的数据错误。
- 可推导字段按固定口径推导: `change_pct = change_amount / prev_close`(小数制,不乘 100)。
- 接口结构整体变化(如所有行都识别不出 symbol)要打明确告警日志,不要静默返回空数据。

## 能力声明的粒度(重要)

`datasets` 声明是**数据集级**的,不是资产类型级的:声明了 `realtime`,整个全市场实时
轮询周期(含指数与 ETF 部分)就全部路由给插件。若你的快照只覆盖 A 股股票:

- 指数行情自动降级为日线推导值(非实时),不报错;
- ETF 实时计数为 0。

这是当前框架的设计行为。要么在数据里尽量覆盖指数/ETF,要么接受降级并在
`description` 里向用户说明覆盖范围。

## Provider 接口契约

Provider 是普通 Python 类(无需继承基类),方法签名对齐 `GenericHTTPProvider`,
services 层零改动即可路由。只实现已声明数据集对应的方法,其余可缺省。

```python
class MyProvider:
    name = "my_source"
    builtin = True  # 标记为内置(不可被用户编辑/删除)

    def __init__(self):
        self.config = MyConfig()  # 需有 .datasets 属性(dict, key 是数据集名)

    def close(self) -> None:
        """清理资源(load_all 重建注册表时会调)。"""

    def get_daily(self, symbols, start_time, end_time, asset_type="stock",
                  on_chunk_done=None) -> pl.DataFrame:
        """日K: [symbol, date, open, high, low, close, volume, amount]; 不复权"""

    def iter_daily(self, symbols, start_time, end_time, asset_type="stock",
                   on_chunk_done=None) -> Iterator[pl.DataFrame]:
        """(可选)有界分批返回与 get_daily 同形的日K; 全市场历史同步优先消费。"""

    def get_adj_factors(self, symbols, start_time, end_time, asset_type="stock",
                        on_chunk_done=None) -> pl.DataFrame:
        """除权因子: [symbol, trade_date, ex_factor]
        (+可选明细列 dividend/bonus/allot/allot_price/prev_close, 均可空 —
        提供时用于等差显示投影与全精度因子链重建)"""

    def get_minute(self, symbols, start_time, end_time, asset_type="stock",
                   on_chunk_done=None, freq="1m") -> pl.DataFrame:
        """分钟K: [symbol, datetime(北京墙钟), open, high, low, close, volume, amount(元, 可空)]"""

    def get_intraday_batch(self, symbols, count=300, asset_type="stock") -> pl.DataFrame:
        """(声明 full_minute 数据集时实现) 全量分钟修复轮: 给定标的当日 1 分钟K,
        canonical 8 列同 get_minute; 内部自行分块/限速。"""

    def get_intraday_latest(self, symbols=None, count=3) -> pl.DataFrame:
        """(可选, full_minute 稳态增量轮) 尽量单请求返回全市场每只最新 count 根;
        未实现则服务降级为仅修复轮 (节奏下限 60s)。"""

    def get_realtime(self) -> list[dict]:
        """全市场实时快照 → list[dict]。失败软返回 [], 不抛异常(不阻断轮询线程)。"""

    def get_realtime_indices(self, symbols: list[str]) -> list[dict] | None:
        """(可选)指数实时快照 → list[dict], 行字段与 get_realtime 一致。
        A 股快照普遍不含指数(fuyao 的指数在独立端点); 声明 realtime 的源
        强烈建议实现本方法, 否则指数行情冻结在本地日K兜底。失败返回 None,
        成功但无数据返回 []。"""

    def get_depth_batch(self, symbols: list[str]) -> dict[str, dict]:
        """(声明 depth5 时实现)五档盘口, 返回以 symbol 为键的标准盘口字典。"""

    def get_financial_universe(self) -> list[str]:
        """(可选)当前财务请求可用的股票代码范围，返回 canonical symbol。
        旧内部批次同步使用此范围（按需入口不调用，包括本地维表尚未收录的代码），并在 financials/universe.json 保存核对时间、
        源名称、数量及双向差异。未实现时仍使用本地完整维表；异常或空清单停止同步。
        清单不保证每股每期都有财务数据，后续取数异常不能因此忽略。
        只影响财务请求范围，不修改本地基础维表或删除已有历史报表。
        """

    def get_financials(self, table, symbols, latest_only=False,
                       on_chunk_done=None, *, cancel_event=None,
                       start_date=None, end_date=None) -> pl.DataFrame:
        """财务数据；可选原生进度回调为 (done, total)，由协调线程调用。

        显式声明 on_chunk_done 的源自行分批/并行与控频；旧签名继续由服务按
        100 股调用。回调抛出的取消必须向上传播，未完成表不得返回部分结果。
        显式声明 cancel_event 的源会收到任务取消信号，应传入限流等待及请求
        阶段；并发轮次内部停止信号应独立于任务取消信号，避免将取数失败标记为取消。
        start_date/end_date 为可选 datetime.date，限定报告期（两端包含），不是公告日。
        显式声明这两个参数的源收到财务页近两年或向前一年的范围，应在上游请求中限界。
        旧签名继续可用，由按需服务过滤返回数据后保存；其上游取数量及可回溯范围仍受源限制。
        """

    def get_instruments(self, asset_type="stock") -> list[dict]:
        """(可选)标的维表: 返回 tickflow Instrument 形状的行, 供 instrument_sync 复用 flatten"""

    def test_dataset(self, dataset: str, symbols=None) -> dict:
        """(强烈建议)设置页"试拉"按钮。
        返回 {provider, dataset, rows, columns, preview, error?}; 未支持的数据集
        返回 error 字段说明会回退 TickFlow。"""
```

财务五表的版本时间契约：`symbol`、`period_end` 标识股票和报告期，数值使用
各表已有 canonical 字段。默认属于正式财务数据；若返回预告或快报，必须显式
标注 `disclosure_type=forecast/express`（正式为 `formal`），三类分别保留，当前
财务因子仅使用正式报表。`announce_date` 沿用供应商原字段，不能直接视作首次披露。

只有确认日期对应当前返回数值版本时，provider 才可声明
`date_basis=source_update` 与 `source_update_date=YYYY-MM-DD`。首次采集该源该股票的
一张表时，允许以此日期保守回填历史；此后新增报告期、修订、日期改变均按源版本日期
与本地实际观察日的较晚者生效。无法确认版本日期（包括缺列、空值、日期无效）时，
不回填历史，按实际观察日使用。服务保存 `source`、`observed_at`、`available_date`、
`availability_basis`，provider 不得用文件时间或报告期伪造这些本地证据。
`available_date` 为保守可用日期，日频和分钟消费均从其后首个交易日开始使用。

`financials/{table}/part.parquet` 是每报告期的最新完整浏览记录；
`financials/versions/{table}/part.parquet` 是追加版本库，每行保留完整供应商记录。
相同内容只复用首次观察时间，变化保存新记录，不跨源、跨版本逐列补空。
扶摇只读 GET 的连接、超时和远端断流最多尝试三次，每次经过共享限速（请求间隔 0.5 秒）；HTTP／业务限流不重试，取消在等待中生效。该间隔用于降低持续采集的请求频率；财报与指标端点另共用 2 秒发包间隔，不保证避开供应商的动态限流。
财报同步每轮重查供应商可返回的历史窗口（扶摇为最近 20 期），指标接口如果只能
提供最近一期，不虚构历史指标；股本表仍沿用原增量同步。旧浏览文件继续可读，
但没有来源和观察证据，不能作为回测历史；升级后通过单股获取或策略／因子任务按需建立版本记录。

扶摇三表的 `report_date_ms` 实测与更新日期一致，作为 `source_update_date` 使用；
首次披露至更新之间的原版本仍可能缺失。扶摇指标接口无版本日期，ROE 等仅从本地
采集后可用，加权 ROE 不用净利润除权益替代。行情与估值反推的 BPS 标记
`bps_basis=valuation_snapshot`，只供浏览，不作为回测财报 BPS。PB 使用不复权价格
与可用财报 BPS，缺任一输入则为空；尚不提供股本变动后财报 BPS 的重算。

共享财务因子按当时可用的最新报告期选择完整同源记录；旧季度晚更新不会替代新季度。
毛利率、净利率、资产负债率由同一期报表计算，同比使用同源上年同期，分母为比较期
绝对值；缺输入时为空，修订比较期只能影响修订生效后的结果。缺财务输入不入选或
参与截面排名，也不触发因子分数离场；止损、最长持有期和独立卖出信号仍沿用原规则。
当前应用通过共享按需服务逐股逐表调用 `get_financials(..., latest_only=False)`；财务页默认传近两年的报告期范围，底部“采集更多”每次只请求当前历史边界之前的一年，边界按股票／表／数据源持久化。范围外的已存历史保留，失败不移动边界；成功空年度明确显示无返回并可继续向前查询。显式策略与研究任务保留其依赖历史准备范围。按需入口不扫描 provider 全市场股票池，不修改全市场同步断点。版本库损坏时停止写入，不能当作空历史覆盖；保存沿用原子写入与 `financials` 数据更新通知。财务因子每次从版本库附加，不写入行情矩阵磁盘缓存。旧批次同步与异常协议仍保留供内部兼容。

Provider 可声明 `financial_tables`（表名元组），表示当前可获取的表；未声明时沿用五表协议。Fuyao 声明 `income`、`balance_sheet`、`cash_flow`、`metrics`，不声明无上游接口的 `shares`。明确缺数、限流或网络错误均向需求任务传播，不生成占位版本，也不将失败记录为当日成功。

### 当前应用的财务采集范围

财务五表统一按需求获取并持久化，页面不再提供全市场同步。所有登录用户可获取单股单表；有效策略与因子任务在执行前准备自身股票池、依赖表和源支持的历史窗口。利润表也不再默认全市场采集。旧 HTTP `/api/financials/sync/{table}` 已停用，`FINANCIAL_SYNC_TABLES` 仅供旧内部批次代码兼容。

同源同股同表的在途请求共享结果；当日成功获取后复用，跨日再次使用时更新。获取失败可重试，不删除已有数据。新增版本沿用原来的观察时间与可用日规则，后采集到的数据不能绕过历史门控。源仅有有限历史窗口时，仍明确保留不可计算的历史缺口。

PB、ROE、资产负债率三项已退出可选因子目录；旧财务时间线计算保留用于兼容已有
研究格式，旧策略若仍引用已下线且缺失的财务评分，不会静默重分权重。毛利率、净利率、
营收增速和归母净利润增速仍从利润表版本计算。现金流等缺少数据时，财务分析应保留缺失。

### 正式财报版业绩断层

K 线工具栏的「业绩断层」复用上述版本库及当前财务源，不额外取数或新增供应商。
「断」表示财务条件满足后的向上缺口，箭头 9 表示缺口回踩，菱形 42 表示断层中点回踩。
图标采用项目现有图表形状和数字标签，不依赖通达信的位图资源。

- 每个报告期仅在首个保存版本的 `available_date` 之后第一个市场交易日尝试形成事件。
  首次历史基线沿用版本库的保守供应商更新日；无法恢复的首次披露版本不向前推断。
- 事件当天取该期及同源上年同期当时已可用的完整利润表；使用 `net_income > 0`，
  且 `100 * (本期净利润 - 上年同期净利润) / abs(上年同期净利润) > 0`。
  比较值为零、缺失或非有限值均不计算，归母口径因子不混入这里的总净利润判断。
- 仅当该期为当天最新可用报告期且当日最低价高于上一根最高价时形成断层。
  停牌不把事件顺延到复牌日，交易日历未覆盖的事件日期保持缺失。
  持续日历同步保留重叠区间的已确认历史；长期停采导致的不相连未知区间不合并。
- 后续当前期或比较期修订不撤销已形成的断层，不给原先缺数据或业绩不达标的旧缺口补信号。
  9/42 使用原断层锚点，继续按原式 PSY12、EMA10、MA20、阳线和回踩条件计算，
  保留确认后的 125 根、10 根有效期，不包含 2023 年日期截止限制。
- 行情按项目统一前复权口径，完整前序用于 EMA 和动态 REF/COUNT；缺失、读取失败、
  无财务版本、无日历及非股票有明确提示。尚未建立版本库时，可在财务页获取该股利润表或显式运行相关策略。

`get_depth_batch` 返回结构如下。价格和数量数组均按一档到五档排列;数量单位为“手”,
`timestamp` 为毫秒 Unix 时间戳。服务层按 capability 的 `batch` / `rpm` 统一分片限速,
provider 不应自行切换或回退到其他数据源。

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

### get_minute 的 datetime 时区契约

`datetime` 必须是**北京时间墙钟**（naive，如 `2026-08-28 09:35:00`），与日K的
`date` 语义对齐；不要返回 UTC 或带时区的时间。前端分时图按交易时段时轴
（09:30–11:30 / 13:00–15:00）映射每根K线，UTC 口径的帧会导致全部点位落在时轴外、
分时图空白。

`amount` 单位为元；数据源无法提供可靠的分钟成交额时应返回 `null`，不得伪造。
成交额缺失后无法继续计算累计成交均价，前端会停止绘制后续均价线并显示 `—`。

入口守卫（`kline_sync._enforce_minute_beijing_wallclock`）对所有分钟源强制归一：
带时区 → 自动换算成北京墙钟；naive 但整体呈 UTC 特征（如 01:30）→ 自动 +8 纠偏并
记日志；完全无法识别的口径 → 拒收并回退 TickFlow。契约仍要求源头写对，守卫只是兜底。

可选类属性 `minute_history_days = 5` 声明 1 分钟历史深度（交易日）；未声明视为
深历史（TickFlow 基准）。浅源（如 stock-sdk 免费分时仅保留最近 5 个交易日）声明后，
个股分时档位自动收窄为可行选项并默认 5 日，深源默认 20 日。

> **全量分钟 (full_minute) 数据集契约**:声明 `full_minute` 数据集并把
> `full_minute_data_provider` 路由到你的源,即接入「全量分钟」能力(盘中全市场
> 当日分钟K增量落盘,由内置服务 `minute_refresh` 调度,与 TickFlow Expert 同一
> 能力键 `intraday.universe`)。需实现:
>
> - `get_intraday_batch(symbols, count=300, asset_type="stock") -> pl.DataFrame`
>   — **必须**(或已有 `get_minute` 自动回退,但强烈建议实现批量端点)。
>   返回给定标的当日 1 分钟K,canonical 8 列
>   `[symbol, datetime(北京墙钟 naive), open, high, low, close, volume, amount]`,
>   内部自行分块/限速。服务在冷启动、覆盖断档、连续空轮时调用(修复轮)。
> - `get_intraday_latest(symbols=None, count=3) -> pl.DataFrame` — **可选**,
>   稳态增量轮专用:尽量单请求返回全市场每只标的最新 `count` 根。未实现时
>   服务自动降级为仅修复轮,节奏下限抬到 60s(全天批量打不住 6s 节奏)。
>
> 两个方法的返回帧都过 `_enforce_minute_beijing_wallclock` 时区守卫(与
> `get_minute` 同纪律);失败抛异常或返回空 df 均按空轮处理,连续空轮触发
> 修复轮自愈。声明方式:插件在 `plugin.yaml` 的 `datasets:` 列表加入
> `full_minute`。YAML 声明式源同样支持(数据集配置与 `minute` 同形,仅提供
> 修复轮语义,见 [custom-data-source.md](./custom-data-source.md))。

### 盘后自动同步的覆盖与停牌证据

交易日盘后管道按 [盘后同步顺序约定](postmarket-pipeline.md) 验收本轮正式日线；不能把请求成功、旧分区或“返回少量当天数据”当作全市场完整。

若 provider 的 `get_daily` / `iter_daily` 已过滤停牌行，可选实现 `get_daily_halted_symbols(day: date) -> set[str]`，返回目标交易日正式数据中明确停牌的完整股票代码。只允许按现有停牌口径提供证据；缺行、缺字段、上一交易日数据和请求失败不能推断成停牌。未实现此方法仍兼容原同步；自动任务遇到无法解释的缺行时会停止环境计算并稍后重试。

`get_adj_factors` 可选接受 `require_complete: bool = False`。自动管道在方法支持时传入 `True`，此时上游请求失败必须抛出，不能用空表伪装为“没有除权事件”；合法的无事件结果仍返回空表。旧 provider 无须新增参数，但必须遵守原有错误与无数据区分的契约。默认调用保持原行为。

长驻 provider 的近期日线、因子缓存必须及时检查上游版本；不能因为进程未重启而永久返回前一交易日的文件。扶摇实现以五分钟为进程缓存检查间隔，磁盘文件仍按 release 复用。

### 异常语义

| 方法 | 失败行为 |
| --- | --- |
| `get_realtime` | **软失败**: 返回 `[]` + warning 日志, 保证轮询线程不中断 |
| `get_realtime_indices` | **软失败**: 返回 `None` + warning 日志, 保留上轮有效缓存; 成功无数据返回 `[]` |
| `get_depth_batch` | 单批异常由服务隔离并保留其他批次; 不跨数据源回退 |
| `get_minute` | 抛异常时调用方自动回退 TickFlow 重试 |
| `get_daily` / `get_adj_factors` / `get_financials` | 异常由上层同步流程捕获记录; 无数据返回空 DataFrame |
| 财务上游限流 | `data_providers.base.ProviderRateLimitError` 向任务传播，保留当前表原文件；不按成功空结果处理 |
| `iter_daily` | 可选; 每批必须符合 `get_daily` 契约。流正常结束后才提交 staging; 未捕获异常会丢弃 staging。provider 内已定义的单标的软失败语义保持不变 |

`iter_daily` 用于避免大范围日K同步在 provider 内累积完整 DataFrame。实现该方法后,
`kline_sync` 会优先消费它; 未实现的 provider 继续调用 `get_daily`,保持兼容。批次大小应有
明确上界,不得先把全部结果放入列表再 `concat`。`on_chunk_done(cur, total)` 必须覆盖空批次,
确保最终 `cur == total`。

### get_realtime 行字段

| 字段 | 必需 | 契约 |
| --- | --- | --- |
| `symbol` | ✅ | 标准代码带后缀 |
| `last_price` | ✅ | 最新价 |
| `prev_close` | ✅ | 昨收, 涨跌幅推导基准 |
| `open` / `high` / `low` | ✅ | 当日 OHLC |
| `volume` | ✅ | **手**(1手=100股) |
| `amount` | 建议 | 成交额(元) |
| `change_pct` | 建议 | **小数制**; 缺失时下游按 change_amount/prev_close 推导 |
| `change_amount` | 建议 | 涨跌额(元) |
| `timestamp` | 建议 | 毫秒; 优先用服务端时间(行情归属), 缺失退本地时间 |
| `name` | 可选 | 快照无名称时置 None, 下游用标的维表关联 |
| `amplitude` / `turnover_rate` / `session` | 可选 | 缺失置 None, 不启发式伪造; turnover_rate 入口为小数制 |

### config.datasets 的作用

`provider_has_dataset(name, dataset)` 通过 `dataset in provider.config.datasets` 判断。
这是 services 层路由的关键: 用户在设置页选了插件, 但某数据集未声明时, 该数据集
自动回退 TickFlow。

```python
class MyConfig:
    datasets = {"daily": ..., "realtime": ...}  # key 是数据集名, value 任意
```

## 限频与性能

- realtime 默认 6s 轮询一轮。优先确认服务端单次 limit 上限: fuyao 实测单页
  limit=6000 可一次拉完全市场(~5600 只), 1 请求/轮; 若服务端强制小页, 必须做
  页间隔/自限速(参考 fuyao 的 0.15s 页间隔兜底), 并建议用户把轮询间隔调大(15-30s)。
- 分页必须有页数上限(防 count 异常导致死循环)和空页终止条件。
- 拉取由 fetch 锁串行化, 慢不会并发重叠; 实际刷新周期 = 轮询间隔 + 拉取耗时,
  串行分页的全量快照本身就需要数秒, 不要按"6s 内必须完成"设计。

## 测试要求

插件 PR 必须带契约测试(CONTRIBUTING §9), **不依赖真实网络与 API Key**——用假
Client/桥接注入。以 `backend/tests/test_fuyao_provider.py` 为范本, 至少覆盖:

1. 字段映射与单位转换: 百分数→小数制、volume 股→手、*ms 零点戳时区换算、缺失字段按口径推导、缺失字段置 None 不伪造
2. 接口响应结构变体: 实测结构 vs 官方文档示例双兼容(供应商文档与实际不一致是常态)
3. 分页: 多页合并、空页终止、页数上限
4. 软失败: 接口报错返回 []; 整页 schema 变化有告警而非静默空数据
5. 能力声明: 未声明数据集 `provider_has_dataset` 为 False
6. Key 语义: 先探后存(无效不落盘)、secrets.json > .env 优先级、availability 两态
7. loader 集成: 清单解析后正确注册(或 hidden 时正确跳过)

```bash
cd backend && uv run --extra dev python -m pytest tests/test_<your_plugin>_provider.py -q
uv run --extra dev python -m ruff check app/plugins/<your_plugin>/ tests/test_<your_plugin>_provider.py
```

## 现有插件参考

- **`backend/app/plugins/fuyao/`** — 同花顺官方 REST 数据源(runtime: none, 纯 HTTP 零依赖)
  - 提供 `realtime`(A 股全市场快照, 分页拉取)、`daily`(原始价日K三档: 近端窗口走 daily-k-10d dump, 深窗口走 daily-k 10 年全量 dump(172MB 一次下载、缓存复用、10d 补尾), 兜底单标的接口按 10 年自动分片)、`adj_factor`(事件 dump + 前收盘价从本地日K dump 一次取齐、缺价标的回退单标的接口, 按交易所公式推导单事件比值, 涨跌停自检; 全市场配价从逐标的 ~13 分钟降为秒级); Key 在设置页卡片直接配置(先探后存), 或 `.env` 配 `FUYAO_API_KEY`
  - `client.py` — httpx 客户端(X-api-key 认证 + 统一信封解包 + 分页 + 页间隔限频 + 单标的日K + dump 预签名下载, S3 下载不带 Key 头)
  - `provider.py` — Provider 实现(实测/文档双字段名映射、百分数→小数制、volume 股→手、上海零点戳 +8h 时区、dump 按 release 版本缓存、软失败、Key 探测)
  - `tests/test_fuyao_provider.py` — 73 个契约测试, 是新插件的测试范本
- **`backend/app/plugins/stocksdk/`** — Node 型插件, 通过 subprocess 桥接调用 stock-sdk
  - `bridge.py` — Python↔Node 桥接 + availability 检测
  - `bridge.mjs` — Node 端(并发池、重试、SDK 解析)
  - `provider.py` — Provider 实现(归一化、分批、错误降级)

## 路由机制(无需关心, 仅参考)

后端启动时, `loader.py` 扫描 `plugins/` 目录:
1. 读每个子目录的 `plugin.yaml`
2. `hidden: true` → 跳过(不注册不展示); 否则调 `check` 函数检测可用性
3. 可用 → 动态 import `entry` 指向的 Provider 类 → 注册进 `_PROVIDERS`
4. 不可用 → 记录状态, 设置页显示但不可切换

注册后, 插件和用户 YAML 自定义源走**完全相同的路由路径**(services 层的
`provider_has_dataset` / `get_provider` 调用), 无需额外集成代码。
