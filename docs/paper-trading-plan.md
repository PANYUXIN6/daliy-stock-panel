# 模拟盘维护说明

模拟盘已实现多账户、手动下单、自动跟单、净值和账户对比，用虚拟资金按真实行情模拟成交。页面为 `frontend/src/pages/Paper.tsx`，账务实现位于 [`strategy/paper.py`](../backend/app/strategy/paper.py)，自动规则在 [`paper_auto.py`](../backend/app/strategy/paper_auto.py)，HTTP 入口为 [`api/paper.py`](../backend/app/api/paper.py)。

## 用户与账户存储

所有个人文件通过 `UserContext` 和 `private_storage` 定位到：

```text
<DATA_DIR>/users/<user_id>/paper/accounts/<account_id>/
  account.json             # 初始资金、现金、费用、active/frozen 状态
  orders/order_*.json       # pending/filled/cancelled/expired 订单
  fills.jsonl              # 追加成交与 corp_action 台账
  positions.json           # 物化持仓与买入批次
  nav/daily.jsonl           # 按日期保存的定版净值
  auto_rules/arule_*.json   # 自动跟单规则
```

`account_id` 默认为 `default`，仅允许 1–32 个字母、数字、下划线或短横线；它选择当前用户的账户，不选择用户身份。同 ID 创建返回已有账户，不覆盖余额。用户目录内的旧单账户布局由首次访问迁入 `accounts/default/`。

共享行情仍从根数据目录读取。行情轮询在各用户的 `user_scope` 内遍历账户；盘后由 [`settle_user_accounts`](../backend/app/services/user_runtime.py) 对启用用户逐账户结算，个人事件写入对应用户告警记录。

## 账务与写入

初始资金加 `fills.jsonl` 是持仓、现金重放的依据：买入扣成交额和费用，平均成本含买入费；卖出增加扣费后的现金并按 FIFO 消耗批次。`replay_positions` 统一重放，`rebuild_positions` 重建现金和物化持仓，不修改历史成交行。净值还需要对应日期的行情，不能只由成交台账恢复。

API 下单、盘中撮合、盘后结算共用进程内 `PAPER_LOCK`。账户、订单、持仓和净值使用 `atomic_write_text` 替换文件；成交台账采用追加。一次成交依次追加台账、更新订单、按台账重算现金和持仓。这是进程内互斥与单文件原子写，不是跨文件事务或跨进程锁；维护时不能据此假设进程崩溃后的多文件一致性已自动保证。

同日重跑的保护分别是：已成交订单不再匹配、同标的同日 `corp_action` 不重复追加、净值按日期替换。缺行情与涨跌停排队的顺延计数仍按处理次数递增，不能把整个结算过程视为按日期完全幂等。

## 订单、价格与费用

撮合、持仓估值和结算使用不复权原始价格；策略计算继续使用原有前复权口径。下单数量为 100 股整数倍，金额模式按参考价向下取整到百股，费用另扣；资金不足不会自动缩量。只处理整单成交，没有盘口排队量、成交量约束或部分成交模拟。

| 类型 | 当前撮合方式 |
| --- | --- |
| `market` | 连续竞价时段由下一次行情评估使用股票快照 `raw_close` 撮合；ETF 下单时转为 `next_open` |
| `next_open` | 盘后结算使用当日原始开盘价；下单或排队时当日开盘价已打印的，留到下一交易日 |
| `close` | 盘后按当日原始收盘价；收盘时刻及之后下单的，留到下一交易日 |
| 尚未成交的 `market` | 盘后可用原始收盘价兜底，同样不能用下单前已打印的价格回填 |

价格时点按北京时间判断，开盘为 09:30、收盘为 15:00。盘中交易日／连续竞价门控由行情服务执行；`settle_day` 本身按传入日期处理，自动盘后入口的日历和数据验收见 [盘后管道](postmarket-pipeline.md)。

当前模拟费用默认值由 `paper.py` 定义，属于账户参数：

| 参数 | 默认值与计算 |
| --- | --- |
| `commission_pct` | `0.00025`，买卖双边佣金，每笔最低 5 元 |
| `stamp_tax_pct` | `0.001`，只对卖出计入 |
| `slippage_bps` | `5`，买价上调、卖价下调 `5/10000` |

修改费用只影响之后成交，历史台账不重算。买入按参考价预检，撮合时按实际模拟价和费用再验；更早挂单优先。同标的待成交卖单占用可卖额度，防止多单超卖。

## 成交约束与结算

- T+1 按买入批次日期判断：只有 `buy_date < 当日` 的份额可卖；下单和撮合均重算，不依赖物化文件上次保存的可卖数。账户最多持有 50 个标的，新标的买入受此限制。
- 基于上一有数据日的原始收盘价检查涨跌停。默认触及涨停的买单、跌停的卖单过期；开启 `queue_limit_orders` 则转 `next_open`，记录排队时刻并计入最多 3 次顺延，之后过期。
- 当日无日线不成交，顺延计数超过 3 时过期并保留原因。资金或可卖数量不足也过期；仅 `pending` 可撤。
- 冻结账户拒绝新单，已有挂单仍可撮合和结算。删除账户会删除该账户全部文件；有持仓或待成交订单时，必须显式 `force=true`。
- 结算先撮合，再将当日 `ex_factor` 记为 `corp_action`：批次数量乘因子、平均成本除因子，保留买入日期。当前不处理现金分红。
- 定版净值为现金加当日原始收盘价市值，缺行情的持仓按成本计并记录警告；有沪深 300 当日行情时保存 `benchmark_close`。

涨跌停模型仍是简化规则：ETF 统一 10%，创业板／科创板 20%，指定北交所代码前缀 30%，其余股票 10%；未识别 ST、上市初期等特殊规则，无上一收盘数据时跳过涨跌停检查。它不能等同真实交易所可成交性。

## 自动跟单与展示

`paper_auto` 支持匹配策略 ID 或监控规则 ID，按固定金额或最近定版权益百分比决定数量。策略跟单使用用户独立的内部监控事件，不依赖可见监控规则；下单价格映射为当日原始快照价，复用手动单校验。

`side=auto` 仅用于策略：入场／进池买入，出场／出池尝试平仓；自动退出绕过入场冷却，但仍受 T+1 和可卖额度限制。旧规则缺方向时保持 `buy`。固定卖出按可卖余量扣除待成交卖单后封顶。规则的 `max_positions` 计当前持仓与待成交买单的不同标的，0 表示不限制该规则；已有标的加仓与卖出不受该规则的开仓槽位限制。

冷却按同规则、同标的的历史自动订单创建日期计算，当前使用自然日，非交易日。单批事件按规则／标的／方向去重。下单和盘中成交事件复用告警、SSE 和通知链路，通知失败不撤销账务。

总览和账户对比在有当日股票快照时展示 `estimating` 实时估算；未提供价格的持仓使用平均成本。最大回撤取定版净值序列，实时估值不写入历史净值。FIFO 回合包含买卖费用，持有天数为自然日；当前回合统计只读取普通成交，不纳入 `corp_action`，发生除权后的回合结果需结合该限制解读。

## API 与验证入口

单账户接口通过 `?account=<id>` 选择账户。当前路由包括账户创建／删除／列表、`overview`、`orders`、`trades`、`positions`、`nav`、`stats`、`rounds`、`compare`、`settings`、`freeze`、`rebuild` 和 `auto_rules`；具体方法以 [`api/paper.py`](../backend/app/api/paper.py) 为准。`POST /api/paper/arena/batch_create` 可用同一资金、费用参数创建至多 20 个对比账户并各绑定一条规则。

现有测试：`test_paper_trading.py`（账务重放、费用、FIFO、多账户、正常重跑）、`test_paper_settle_price_time.py`（已知价格不得回填）、`test_paper_t1_available_at_order.py`、`test_paper_auto_sell_cap.py`、`test_paper_auto_max_positions.py`、`test_paper_daily_scan_schema.py`、`test_paper_api.py`、`test_paper_account_delete.py`（含用户隔离）和 `test_paper_arena.py`。维护撮合或写入时，应在这些入口验证具体账务结果，不能只检查 HTTP 成功。
