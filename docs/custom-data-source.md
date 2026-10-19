# 自定义 HTTP 数据源

外部 HTTP 服务返回 JSON，本项目按 YAML 映射字段，复用已有存储、指标、策略和展示流程。配置解析及取数实现位于 `backend/app/data_providers/custom/`；需要自定义 Python 取数逻辑时使用 [数据源插件](plugin-development.md)。

## 配置与加载

配置保存在 `<DATA_DIR>/data_sources/*.yaml`，默认即仓库的 `data/data_sources/`。修改后在「设置 → 数据源」重新加载，用「试拉测试」确认字段和数据，再保存各能力的数据源选择。试拉使用当前表单内容，未保存的配置也可测试。

完整本地样例为 [`mock_source.yaml`](examples/custom-data-source/mock_source.yaml)，运行方法见 [联调说明](examples/custom-data-source/README.md)。最小日线配置：

```yaml
name: my_source
display_name: 我的数据源
auth:
  type: none

datasets:
  daily:
    url: https://api.example.com/kline
    method: POST
    batch: 100
    rpm: 200
    timeout: 30
    response_path: data.list
    field_map:
      ts_code: symbol
      trade_date: date
      open: open
      high: high
      low: low
      close: close
      vol: volume
      amt: amount
    transforms:
      date: "parse_date(value, '%Y%m%d')"
```

`field_map` 的方向是「接口字段 → 内部字段」；`transforms` 使用映射后的内部字段名。`response_path` 指向响应中的记录数组，空字符串表示顶层数组。

## 数据集与字段

只配置实际提供的数据集，能力选择和可用状态以设置页路由矩阵为准。

| 数据集 | 必填内部字段 | 说明 |
| --- | --- | --- |
| `daily` | `symbol`、`date`、`open/high/low/close`、`volume`、`amount` | 批量区间日 K，不复权原始价 |
| `adj_factor` | `symbol`、`trade_date`、`ex_factor` | 除权因子 |
| `realtime` | `symbol`、`last_price`、`prev_close`、`open/high/low`、`volume` | 全市场实时快照，不支持逐股轮询 |
| `minute` | `symbol`、`datetime`、`open/high/low/close`、`volume` | 1 分钟 K，`datetime` 为北京时间墙钟，无时区 |
| `full_minute` | 同 `minute` | 当日全市场分钟批量修复，节奏下限 60 秒 |
| `depth5` | `symbol`、`asks`、`bids` | 按标的批量取五档盘口，每档为 `[价格, 数量（手）]` |
| `financial` | 校验仅要求 `symbol` | 同一配置接收不同表名；业务字段与版本时间见 [插件财务契约](plugin-development.md#财务版本时间契约) |

统一代码带交易所后缀，例如 `600519.SH`、`000001.SZ`；`volume` 为手，成交额 `amount` 为元。供应商按股返回的成交量必须显式换算，不能直接透传。

- `adj_factor` 可提供 `dividend`、`bonus`、`allot`、`allot_price`、`prev_close`，用于等差显示投影和全精度因子链重建，缺省为空。
- `realtime` 建议补充 `amount`、`change_pct`、`change_amount`、`amplitude`、`turnover_rate`、`name`、`timestamp`。涨跌额为元，时间戳为毫秒。
- `minute/full_minute` 的 `amount` 可缺省；无法提供可靠成交额时留空，前端累计均价线停止绘制后续值并显示 `—`。
- `full_minute` 的低成本稳态增量接口 `get_intraday_latest` 仅由 Python 插件提供，YAML 源只提供修复轮。
- `depth5` 映射后派生买卖价格、数量数组，数量单位不自动猜测或换算；单批失败不影响其他批次，不跨源回退。

### 实时比例单位

内部 `change_pct`、`amplitude`、`turnover_rate` 均为小数制，`0.0366` 表示 `3.66%`。新配置应显式声明：

```yaml
datasets:
  realtime:
    url: https://api.example.com/snapshot
    pct_unit: percent   # 接口以 3.66 表示 3.66%；以 0.0366 表示时用 decimal
```

| 配置 | 当前处理 |
| --- | --- |
| `pct_unit: percent` | 三列无条件除以 100，包含已配置转换的列；不要重复换算 |
| `pct_unit: decimal` | 三列原样传递 |
| 旧配置未声明 | 未经 `transforms` 处理的 `change_pct` 沿用统计阈值兼容；`amplitude/turnover_rate` 置空交下游重算并记警告 |
| 未声明单位，但该列已有 `transforms` | 认为该列单位已由配置接管，不再额外换算 |

旧配置的统计兼容取绝对值中位数（少于 5 个有效值时取最大值），大于 `0.31` 则整列除以 100。它不能可靠区分全部合法行情的单位；维护配置时应以实际接口单位声明 `pct_unit`。

## 请求参数

| 配置项 | 默认值与用途 |
| --- | --- |
| `method` | `GET`；非 GET 请求发送 JSON body |
| `batch` | 每批标的数，用于日线、因子、分钟、盘口和财务请求 |
| `rpm` | 批次限频 |
| `timeout` | 单次请求 30 秒，允许 `0 < timeout <= 300`；同步和试拉均生效 |
| `symbols_param` | `symbols`，GET 为逗号分隔字符串，JSON body 为数组 |
| `start_param` / `end_param` | `start_time` / `end_time`，区间起止时间 |
| `params` / `body` | 固定 query 参数／JSON 字段 |
| `asset_type_param` / `freq_param` | 分钟源可选参数名，配置后传 `stock/etf/index`、`1m`；省略则不发送 |

`financial` 请求额外传入 `table`；`shares` 表额外传入 `latest`。HTTP 映射层不替代财务版本服务的来源、观察日与历史可用性校验。

## 鉴权

Token 来自系统环境变量或项目 `.env`，YAML 用 `token_env` 指定变量名。

| `auth.type` | 其他配置 | 请求形式 |
| --- | --- | --- |
| `none` | 无 | 无鉴权 |
| `bearer` | `token_env: MY_DATA_TOKEN` | `Authorization: Bearer ...` |
| `header` | `token_env: MY_DATA_TOKEN`、`header: X-Token` | 自定义请求头 |
| `query` | `token_env: MY_DATA_TOKEN`、`param: token` | URL query 参数 |

## 排查

| 现象 | 检查位置 |
| --- | --- |
| 重新加载后没有数据源 | YAML 位置、数据集字段映射及设置页错误信息 |
| `missing mapped fields` | `field_map` 是否覆盖必填内部字段 |
| 试拉为 0 行 | `response_path` 是否指向数组，接口是否实际有数据 |
| 日期为空 | `parse_date` 格式是否与响应一致 |
| 实时未更新 | 实时路由是否已保存、接口是否返回全市场快照 |

## 用 AI 生成映射配置

提供接口地址、请求参数、实际 JSON 样例和单位，让 AI 依据本页字段生成 YAML 即可。配置仍需用「试拉测试」检查日期、代码、比例及成交量单位，不能仅凭示例响应格式判断正确。
