# 因子计算与研究口径

本文记录当前因子实现及尚未实现的研究设想。当前接口以源码为准，页面入口见 [因子平台](factor-platform-plan.md)；标为“研究设想”的内容不代表已有模块、数据集或 API。

## 1. 模块与计算路径

| 位置 | 当前职责 |
| --- | --- |
| `backend/app/factors/registry.py` | `FactorSpec`、内置目录、依赖与预热声明、用户因子注册 |
| `backend/app/factors/dsl.py` | 公式解析、校验、依赖推导与 Polars 编译 |
| `backend/app/factors/store.py` | 个人自定义／复合因子定义持久化与加载 |
| `backend/app/factors/ext_factors.py` | 将扩展数据字段同步为可用因子 |
| `backend/app/strategy/scoring.py` | 虚拟因子表达式与统一物化，供研究和策略复用 |
| `backend/app/backtest/factor.py` | Rank IC、分层、多空、分年、衰减与分环境检验 |
| `backend/app/backtest/stats_v2.py` | Newey-West、BH-FDR 和 Deflated Sharpe 统计函数 |
| `backend/app/api/factors.py` | 因子目录、公式试算、个人定义和状态接口 |

```text
注册表元数据／个人定义
  → 依赖与预热推导 → materialize_scoring_columns
  → 因子检验／策略评分／矩阵回测
```

注册表统一目录元数据、虚拟依赖和预热声明；内置虚拟因子的计算表达式仍在 `scoring.py`。`FACTOR_COLUMNS` 与 `VIRTUAL_SCORING_DEPENDENCIES` 是兼容视图，不再维护另一份目录。

## 2. 因子注册表

### 2.1 元数据与边界

`FactorSpec` 包含以下字段，完整类型见 [registry.py](../backend/app/factors/registry.py)：

| 字段 | 用途 |
| --- | --- |
| `id`、`version`、`label`、`group`、`formula_text` | 标识、语义版本和展示信息 |
| `kind` | `base`、`virtual`、`custom`、`composite` |
| `dependencies`、`warmup_bars` | 基础依赖与历史预热需求 |
| `direction`、`unit`、`scale_free` | 方向、单位和跨标的可比性；不能凭字段名猜测单位 |
| `pit`、`pit_source`、`asset_types` | 点时依赖及资产适用范围 |
| `incremental_safe`、`null_policy`、`stability`、`tags` | 路径与空值约束、稳定性和标签 |
| `components` | 复合因子的成员与权重 |

财务因子按可用版本时间附加，不是 enriched 磁盘列；原始价、复权价和百分比口径见 [维护约定](../CONTRIBUTING.md#3-不可混用的数据契约)。PB、ROE、资产负债率已退出当前目录，历史策略引用缺列时保持不可计算，不重分权重。

### 2.2 注册与持久化

主要入口为 `register_factor`、`get_factor`、`all_factors`、`factor_dependencies`、`factor_columns_view`、`virtual_dependencies` 和 `scoring_warmups`。重复 ID 未提升版本时拒绝注册。

自定义 ID 为 `uf_[a-z0-9_]{1,40}`，复合 ID 为 `cf_[a-z0-9_]{1,40}`。定义存于 `DATA_DIR/users/<user_id>/user_data/custom_factors/*.json`，注册表按当前用户隔离。启动与按需装载时，损坏文件只跳过该因子；复合定义多轮加载以解析成员依赖。

公式编辑提升版本并回到 `draft`，仅改名称或分组保留状态。`draft/active/watch/retired` 已支持手动设置；当前状态主要映射为 `experimental/stable`，没有自动完整检验晋升或后台衰减巡检。删除前检查引用，有引用时返回清单并拒绝普通删除。

## 3. 因子表达式层

### 3.1 语法与算子

公式只接受常量、白名单字段、已注册因子、算术／比较／逻辑和函数调用，不执行用户 Python。基础字段为 `open/high/low/close/volume/amount/turnover_rate/prev_close/raw_close`；其他字段通过注册表解析。

| 类别 | 算子与语义 |
| --- | --- |
| 时序 | `ts_mean/std/sum/max/min`、`ts_delay/delta/rank/zscore`、`ts_corr/cov/quantile`、`decay_linear`；按 `symbol` 分组、只向历史取值 |
| 截面 | `rank` 为百分位排名；`zscore` 为 `(x−μ)/σ`；`winsorize` 截尾至 `μ±kσ`；按 `date` 分组 |
| 工具 | `log/abs/sign/sqrt/min/max/power/clamp/if_else` |
| 运算 | `+ - * /`、`> >= < <= == !=`、`and or`；运行期除零输出 null |

普通时序窗口为 `[2,512]`；`ts_delay` 为 `[1,512]`，`ts_delta` 为 `[0,512]`，均拒绝负值。`power` 指数绝对值不超过 4，分位 q 在 `(0,1)`，winsorize 的 k 在 `[1,6]`。AST 深度上限 12，token 上限 200。

### 3.2 校验与求值

编译推导基础依赖、引用因子、预热与截面标记；当前预热取引用因子要求及各正窗口 `n+1` 的最大值。静态除零、常量公式、未知字段和越界参数均拒绝。

时序运算挂 `over("symbol")`，截面运算挂 `over("date")`。截面运算消费带窗口的子树时，先按依赖顺序物化临时列再计算，避免嵌套窗口生成全 null；当前不支持截面运算嵌入时序窗口，如 `ts_mean(rank(close), 5)`，返回 E009。

引用因子先由共享物化路径补齐，DSL 不内联另一套因子计算。运行时依赖列缺失返回不可计算，不用零值补齐。

### 3.3 编译流水线与缓存

`text → tokenizer → 递归下降解析 → AST → 语义校验 → 依赖／预热 → frame_transform`。结构化错误包含 `code/message/position/detail`。`compile_formula_cached` 的 LRU 键包括公式文本和个人注册表修订信息，避免用户间定义串用。

保存自定义公式前，服务端重新编译并对最近 40 个交易日试算；输出全空则拒绝保存。交互试算支持 20–120 个交易日，复用研究面板和物化路径。

### 3.4 文法与错误码

```ebnf
expr        = or_expr ;
or_expr     = and_expr { "or" and_expr } ;
and_expr    = cmp_expr { "and" cmp_expr } ;
cmp_expr    = add_expr { (">" | ">=" | "<" | "<=" | "==" | "!=") add_expr } ;
add_expr    = mul_expr { ("+" | "-") mul_expr } ;
mul_expr    = unary { ("*" | "/") unary } ;
unary       = "-" unary | primary ;
primary     = NUMBER | IDENT | func_call | "(" expr ")" ;
func_call   = IDENT "(" [ arglist ] ")" ;
arglist     = expr { "," expr } ;
```

| 错误码 | 含义 |
| --- | --- |
| E001 / E002 | 未知标识符／函数 |
| E003 / E004 / E005 | 参数错误／窗口或分位越界／负 shift |
| E006 / E007 / E008 | 深度超限／token 超限／静态除零 |
| E009 / E010 / E011 | 非法类型或窗口嵌套／power 越界／winsorize 越界 |
| E012 / E013 / E014 | 循环引用／依赖列缺失／语法错误 |
| E015 / E016 | 预热不足／常量表达式 |

错误码结构与具体触发以 `dsl.py`、API 校验和 `test_factor_dsl.py` 为准。

## 4. 研究宇宙（研究设想，尚未实现）

拟将研究股票池描述为按日生效的配置，候选条件包括停牌、调仓时点一字涨停、ST、上市天数、成交额分位、市值分位和标的数量上限。每个条件独立计数，报告展示筛选漏斗；市值需历史股本，排序键由研究任务明确指定。

所有判定只使用当日及以前可获得的信息。当前名称不能证明历史 ST 状态；当前行业快照不能证明历史归属；仅有当前上市与本地历史数据不能消除幸存者偏差。

| 缺失素材 | 拟采用的明确行为 |
| --- | --- |
| 停牌／涨停锁死基础列 | 报数据异常 |
| 历史 ST 或点时行业 | 跳过对应严格过滤／调整，并报告降级 |
| 上市日 | 缺失标的不能通过上市天数过滤，展示数量 |
| 历史股本 | 不参与市值分位，展示降级 |
| 退市股历史 | 报告保留幸存者偏差说明 |

若实现配置化宇宙，生效配置需同时进入研究结果、缓存键和候选保存字段；`backtest/candidates.py` 当前字段白名单尚不包含这些配置。

## 5. 风险调整与基准（研究设想，尚未实现）

拟支持原始收益、基准超额收益以及行业／规模调整后的并列结果：

- 超额收益：`r_ex = r_stock − r_bench`，基准日期与研究收益轴对齐；缺失行情的处理需在实现时明确。
- 行业去均值：`f' = f − mean_ind(f)`。
- 行业内标准化：`f' = (f−μ_ind)/σ_ind`；σ 为零时不可计算。
- 行业与规模回归：逐日拟合 `f ~ 1 + 行业哑变量 + log_float_mv`，使用残差；小样本或共线时明确回退与原因。
- 候选顺序：宇宙过滤 → 截尾 → 中性化 → 标准化，所有生效参数随报告保存。

行业历史缺失时只能使用带降级标记的快照研究。拟增加三口径并列、单调性和筛选漏斗；当前没有 `UniverseSpec`、`NeutralizationSpec` 或 `/api/factor-research/*` 接口。

## 6. 统计检验

### 6.1 当前实现

| 统计量 | 口径与实现 |
| --- | --- |
| Rank IC | 每日因子值与下一交易日收益的 Spearman 相关；均值、IR、胜率及 IC 序列由 `factor.py` 计算 |
| 朴素 t | `mean(IC) / (std(IC, ddof=1)/√N)`；样本不足或标准差为零返回 null |
| Newey-West t | Bartlett 核 HAC；当前批量检验与公式试算均使用 `lag=1` |
| 双侧 p | `erfc(abs(t)/√2)` 的标准正态近似；批量优先 NW，缺失时使用朴素 t |
| BH-FDR q | 在同批可检验因子中按 p 排序，`q_i=min_{j≥i}(m·p_j/j)`，从后向前单调回填并限制至 1；空值不计入 m |
| 分年／衰减／分环境 | `factor.py` 保留年度 IC、1/3/5 日衰减与市场环境分组 |

NW 计算中，`γ_l = Σ[(x_t−μ)(x_{t+l}−μ)]/N`，
`S = γ_0 + 2Σ_{l=1..L}(1−l/(L+1))γ_l`，`SE=√(max(S,0)/N)`，`t=μ/SE`。
样本 `N≤L+2` 或 SE 为零时返回 null。扩展至 h 日重叠前瞻收益时，原研究设想取 `L=h`，须与实际调用对齐。

`stats_v2.py` 使用 NumPy 和标准库实现，并不意味着所有研究报告已升级为统一绩效口径。`FACTOR_METHODOLOGY_VERSION` 当前仍是 `factor_v2`。

### 6.2 Deflated Sharpe

`expected_max_sharpe` 与 `deflated_sharpe_psr` 已存在；当前未接入挖掘排行榜或晋升条件。

```text
EM = sqrt(Var[SR]) × [(1−γ)Φ⁻¹(1−1/N) + γΦ⁻¹(1−1/(Ne))]
DSR = Φ((SR−EM)√(T−1) / √(1−skew×SR + (kurtosis−1)SR²/4))
```

γ 为欧拉常数，N 为试验数，SR 与其方差必须采用一致尺度。单次试验或非正方差的 EM 为零；样本不足、非有限 SR 或非法分母的 DSR 为 null。研究报告接入前需明确试验数、相关试验与样本区间的含义。

### 6.3 尚未实现的统计增强

- IC 自相关 ACF(1..10)；半衰期拟取 ACF 首次低于 0.5 的滞后并插值，不收敛为 null。
- 分层单调性拟使用 `Spearman(组序号, 组均超额收益)` 及趋势斜率 t。
- 多空收益拟增加 NW t，滞后与调仓收益重叠一致。
- 原始／超额／中性化三口径并列，报告记录宇宙、调整配置和降级原因。
- h 日前瞻、NW 滞后与方法论版本分开记录；因子公式版本独立于研究方法论版本。

这些扩展会改变研究输出，不能用现有批量 t/q 字段推断它们已落地。

## 7. 绩效指标统一（研究设想，尚未实现）

当前引擎仍存在逐笔、出场日聚合和净值日收益等统计路径。因子分层 Sharpe 根据调仓频率采用 252／52／12，年化收益按自然日区间折算；不能将不同路径的数字直接视为同一口径。

原统一方案保留如下待评估定义：

| 指标 | 拟统一定义 |
| --- | --- |
| Sharpe | 日净值收益 `r_t=nav_t/nav_(t−1)−1`；`mean(r−rf_daily)/std(r−rf_daily,ddof=1)×√A` |
| 年化收益 | `(nav_T/nav_0)^(A/n_bars)−1` |
| 最大回撤 | 相对历史峰值，初始净值纳入峰值 |
| Tracking error | `std(r−r_b)×√A` |
| Information ratio | `mean(r−r_b)/std(r−r_b)×√A` |
| Alpha／Beta | 同一无风险利率与收益轴上的 OLS |

原提案建议 `A=243`、标记为 `metrics_v2`，尚未取代当前 252 等口径。实施时需明确口径变更、历史结果可读性和报告版本，不能只改一个入口。

## 8. 复合因子与策略接入

当前复合定义使用 `members: {factor_id: weight}`，支持 2–8 个成员；依赖和预热从成员展开，未知成员、自引用和循环引用会被拒绝。

`scoring.py::_composite_value_expr` 按日期计算：

```text
composite(date,symbol) = Σ weight_i × zscore(member_i(date,symbol))
zscore(x) = (x − 截面均值) / 截面样本标准差
```

成员不可计算或截面标准差为零时保留 null，不用部分成员凑分。前端提供均等／手动权重；实际计算使用定义中的权重，没有 `FactorCompositeSpec` 的 transform、截尾或自动权重配置。

策略通过 `scoring` 引用因子，例如：

```json
{
  "scoring": {"cf_hotmom": 0.6, "vol_ratio_5d": 0.4},
  "scoring_directions": {"cf_hotmom": "high"}
}
```

因子检验与策略评分复用物化路径。自定义定义不写入共享 enriched Parquet；修改计算逻辑时仍需核对回测矩阵、策略结果缓存和监控消费，不能由接口相同推断全部时段逐位一致。

**研究设想**：ICIR 自动权重可按 `w_i∝max(ICIR_i,0)`，窗口原建议 504 个交易日；t 日权重只能使用截至 t−1 的数据。当前未实现自动权重、独立复合规范或挖掘结果自动导出复合因子；四端数值一致性仍需按实际执行路径验证。

## 9. 点时数据扩展（研究设想，尚未实现）

下列名称尚未注册为当前 provider 数据集；字段用于保留数据需求，不是插件可直接声明的现成能力。

| 数据需求 | 核心字段 | 查询语义与限制 |
| --- | --- | --- |
| `st_history` | `symbol/flag_date/st_flag/flag_type/source` | 取 `flag_date≤t` 的最后状态，状态日应是公告后首个生效交易日；按代码和生效日幂等合并 |
| 退市日 K 与维表 | 日 K 标准字段；`symbol/name/list_date/delist_date/delist_reason` | 按 `list_date≤t<delist_date` 重建当时可交易池；补回已退市标的历史 |
| `industry_pit` | `symbol/effective_date/industry_l1/industry_l2/source` | 按生效日 backward asof；无历史归属日期时只能作为快照并明确降级 |
| 扩展基准日 K | 与指数日 K 一致的代码／日期／OHLC／量额 | 按供应商能力和研究用途明确标的范围，不由核心四只的展示范围推断所有历史基准能力 |

可继续研究的因子包括特质动量（个股对基准回归残差累计）、基于点时公告股本的规模因子、E/P、SUE（同比盈利差除以历史差分波动）、应收／存货及商誉质量指标。财报必须遵循当前版本可用时间，所需字段、报告频率、股本与源覆盖先核实。EWMA 条件波动、波动的波动等公式可在实际研究需要时评估；不把原候选清单当作已注册目录。

## 10. 当前 API

| 端点 | 方法与用途 |
| --- | --- |
| `/api/factors` | GET，目录及元数据，可按资产类型过滤 |
| `/api/factors/validate` | POST，公式校验、依赖与预热 |
| `/api/factors/trial` | POST，最近交易日公式试算与 IC |
| `/api/factors/custom` | POST，保存自定义公式 |
| `/api/factors/composite` | POST，保存成员权重复合因子 |
| `/api/factors/custom/{id}/update` | POST，更新自定义公式与信息 |
| `/api/factors/custom/{id}/status`、`/group` | POST，更新状态或分组 |
| `/api/factors/custom/{id}` | DELETE，引用检查后删除，支持显式 force |
| `/api/backtest/factor/columns`、`/run`、`/batch` | 目录、单因子与批量研究 |

请求和响应以 `api/factors.py`、`api/backtest.py` 与 `frontend/src/lib/api.ts` 为准。鉴权继承当前 SQLite 账号会话与业务权限，个人定义经 `private_storage` 限定归属；旧部署口令不构成当前身份。没有原提案的 `/catalog`、`/preview`、命名宇宙或 `/api/factor-research/*` API。

## 11. 页面与研究流转

当前入口为 `/factors` 的检验、因子库、编辑器、组合、挖掘五个 tab；页面行为、策略桥接与未完成事项见 [因子平台](factor-platform-plan.md)。原来的 Settings 因子库、ResearchProfile 和独立巡检页线框不代表已实现界面。

## 12. 缓存与数据更新

当前 DSL 编译缓存按公式及个人注册表修订分区；研究和评分通过共享物化路径计算，不将个人公式列落入 enriched 存储。行情、财务版本及扩展表更新仍需沿各自既有失效链核对。

若接入宇宙、中性化、自动权重或新方法论，所有影响数值的配置与数据 generation 都需进入相应缓存身份，并随候选保存与恢复。原提案中的具体 `fr:*` 键、spec 哈希和性能数字尚未落地，不作为当前缓存接口或性能承诺。

## 13. 定向验证入口

| 范围 | 现有测试 |
| --- | --- |
| 注册表与目录 | `test_factor_registry.py`、`test_factor_expansion.py`、`test_factor_library_v2.py` |
| DSL | `test_factor_dsl.py` |
| API、存储与删除 | `test_factor_api.py`、`test_factor_store.py`、`test_factor_delete_fail_closed.py` |
| 评分与条件信号 | `test_strategy_scoring.py`、`test_custom_signals_factor_conditions.py` |
| 统计函数 | `test_stats_v2.py` |
| 研究与矩阵 | `backtest/test_factor_batch.py`、`backtest/test_factor_metrics.py`、`backtest/test_factor_rank_research.py` |
| 前端检验 | `frontend/src/pages/backtest/FactorDiscovery.test.tsx` |

修改公式或统计口径时，用固定样本数值断言覆盖 null、零方差、预热、缺列和时间边界；扩展研究设想时再补对应行为验证。
