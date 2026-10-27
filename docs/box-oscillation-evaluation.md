# 箱体震荡的结构核对

目标是让用户看清箱体上下沿和当前价格所处位置，自行判断仓位。工具只核对边界是否合理、是否有明显误识别或漏识别，不提供仓位建议，也不评价交易收益。

当前已有离线核对工具和[合成回归基准](box-oscillation-baseline-report.md)，尚无独立人工标注，因此真实识别质量仍待人工核对。人工集不作为开发诊断字段的前置门槛；合成测试通过也不能说明真实行情识别可靠。在线识别算法、默认参数和图表默认关闭行为沿用[第一版](box-oscillation-design.md)。

## 保留的工具

- 完整回归快照：检查代码改动是否意外改变箱体集合、边界、确认日或事件。
- 独立人工参考：核对检测框与人工认为合理的箱体是否一致。
- 简单结构评价：一对一匹配、边界误差、时间重叠度、明显误识别与漏识别。
- 两段时间切分：calibration 用于观察问题和调整，validation 用于核对调整后的表现。

不设置最终保留集、规则冻结编号或固定样本数门槛；不再单列拆分、合并、重复诊断，也不计算突破后 H 根回箱率、收益或存续分析。这些都不是当前看图用途的必要条件。

离线工具不被行情 API、指标流水线、采集或前端调用，不增加缓存、供应商访问或线上运行开销。

## 怎样准备参考样本

先用 8–10 个指数和个股做人工核对即可开始发现系统性偏差，尽量兼顾不同市值、流动性和波动程度。数量不是质量验收门槛；发现某类问题再补相应案例。

先选标的和时间范围，再看其中的结构，避免只挑引擎已经识别出的漂亮案例。人工正样只需记录起止日、下沿和上沿，价格基准与 K 线一致。建议先隐藏引擎检测框再标注。`earliest_confirmable_date` 可留空，仅作可选记录，不参与当前评价，不要求按严格规则计算。

明显单边趋势、通道可作为负样，但需人工确认其确实不包含水平旋转结构。波动扩张本身不能自动判为非箱体。正负样的时间范围不能冲突；大箱体内部的趋势段不能同时标为该 case 的纯负样。

标签来源仍应分开，避免用引擎自己的结果证明自己正确：

| `label_source` | 用途 |
| --- | --- |
| `human` | 人工核对的真实行情参考；`annotator` 可选 |
| `synthetic` | 手工构造的工程样本，与人工评价分别输出 |
| `review_candidate` | 引擎检测或多周期一致性产生的待复核线索，不进入质量统计 |

`annotation_status=complete` 需指定 `evaluation_interval`，表示这个区间已完整看过，正样清单完整。仅标出几个箱体、尚未检查其余行情时应保持 pending，或把复核区间缩小到已看过的连续范围。否则未匹配的检测框可能只是尚未标注，不能计作误识别。

## 输入格式

从[空模板](examples/box-evaluation-template.json)开始。模板日期和匹配阈值只是初始示例，可按实际核对范围调整。`cases=[]` 合法，执行时输出待人工标注状态，质量统计分母为空。

每个 case 对应一个标的、资产类型、周期和连续有效的完整 K 线快照。日期严格递增且无重复，OHLC 有限、为正且价格关系正确，标注端点需对应实际 K 线。股票／ETF 使用前复权价 `qfq`，指数使用自身价格基准 `index`。

`bars_are_completed=true` 表示提供者确认这些 K 线已完成。输入应沿用在线服务相同的清洗和完成规则；异常或未知缺口分段，不补零、不跨缺口拼接。周／月输入为已经聚合完成的周期 K，评估工具不重新聚合。

待标注 case 示例；两根 K 只演示格式，不构成箱体参考：

```json
{
  "id": "replace-case-id",
  "symbol": "replace-symbol",
  "asset_type": "stock",
  "period": "daily",
  "price_basis": "qfq",
  "bars_are_completed": true,
  "label_source": "human",
  "annotation_status": "pending",
  "annotator": null,
  "annotation_note": "可记录边界选择及有争议的结构",
  "evaluation_interval": null,
  "bars": [
    {"date": "2024-01-02", "open": 10.0, "high": 10.2, "low": 9.8, "close": 10.1, "volume": null},
    {"date": "2024-01-03", "open": 10.1, "high": 10.3, "low": 9.9, "close": 10.0, "volume": null}
  ],
  "references": [],
  "negative_intervals": []
}
```

正样字段示例为 `{"id":"ref-1","start_date":"…","end_date":"…","lower":10,"upper":12,"earliest_confirmable_date":null,"note":"…"}`；负样为 `{"id":"neg-1","start_date":"…","end_date":"…","reason":"人工核对为单边通道"}`。替换为实际有效日期。完整机器格式通过 `schema` 命令导出，校验实现见 [box_evaluation.py](../backend/app/services/box_evaluation.py)。

## 匹配和评价口径

同一 case、同一时间集合内，检测与参考满足以下四个条件才配对：

1. 有效 K 日期集合的交并比 `time_iou >= min_time_iou`，含首尾。
2. `max(|Ldet−Lref|, |Udet−Uref|) / (Uref−Lref) <= max_boundary_error`。
3. 起点相差有效 K 根数不超过 `max_start_error_bars`。
4. 终点相差有效 K 根数不超过 `max_end_error_bars`。

默认值依次为 0.5、0.2、5、5，是待核对的初始匹配口径，不是已经证明可靠的识别参数。配对先最大化匹配数，再最小化 `1−IoU + boundary_error + (start_error+end_error)/reference_bars`；每个检测和参考最多配对一次。

首次确认日非空的检测纳入评价，包括历史已突破箱体；候选单列。配对为 TP，未匹配检测为 FP，未匹配参考为 FN。一个大参考被识别成相邻小框时，简单匹配仍可能产生 FP／FN；应回看具体图形，不把这些数字直接等同于看图功能是否可用，也不另建碎片错误分类。

| 输出 | 含义 |
| --- | --- |
| 边界误差、时间 IoU | 匹配框的边界偏差与覆盖程度，给出明细及汇总 |
| `negative_segment_false_positive_rate` | 被已确认检测覆盖的负样段数／负样段数；有至少一根有效 K 重叠即命中，一段多框只算一次 |
| `precision` / `false_discovery_rate` | TP／(TP+FP) 与 FP／(TP+FP) |
| `recall` / `false_negative_rate` | TP／(TP+FN) 与 FN／(TP+FN) |

分母为 0 时返回 null。结果按 **human／synthetic → calibration／validation → daily／weekly／monthly** 分组，保留 case 明细与排除原因；不要把合成结果当成人工质量结果。

## 两段切分与跨界结构

输入依次声明互不重叠的 calibration、validation 起止日期。`gap_bars` 是当前 case 周期的有效 K 根数，默认 5，可按需要设为 5–10。

短隔离带可减少边界附近的相似样本，但不能保证长箱体不跨界。工具仍按结构的实际起止日排除跨越边界或隔离带的参考；同标的、同标签来源下跨周期时间重叠的参考作为一组处理，跨界时整组排除。无需靠拉长隔离带替代这个判断。

各集合只识别到本集合完整复核区间末日，历史数据可作为已知上下文。检测和负样区间需完整位于复核核心范围；跨界部分保留排除说明，不裁剪为完整标签。排除比例较高时，应调整人工复核范围或补案例，避免仅凭剩余几个数字判断。

validation 可在修改后重复核对；反复据它调整会降低独立验证含义，但当前工具不增加保留集纪律或额外命令开关。

## 执行方法

在 `backend/` 下运行，输出路径应尚不存在，父目录须已建立。工具仅读取显式快照或版本库内的合成 fixture，不访问 `data/`、供应商或启动采集。

```bash
uv run python scripts/evaluate_box_oscillation.py baseline --output /tmp/box-baseline.json
uv run python scripts/evaluate_box_oscillation.py schema --output /tmp/box-evaluation-schema.json
uv run python scripts/evaluate_box_oscillation.py evaluate --dataset ../docs/examples/box-evaluation-template.json --output /tmp/box-awaiting-labels.json
```

填好本地 JSON 后替换 dataset 路径即可。`baseline` 支持显式 `--baseline` 指定完整回归快照；不一致退出码 1，输入／输出错误退出码 2，正常完成为 0。已有输出拒绝覆盖，避免静默替换报告或输入。用户行情与报告留在本地，不提交 Git；版本库 fixture 只含合成数据。

## 后续改动怎么判断

可以继续增加可解释的结构诊断，让边界偏差和误识别原因更容易看清，不必等待大量人工标注。诊断字段首先保持现有识别结果，利用完整回归快照核对；再结合少量真实案例判断是否需要修改成立条件或参数。没有人工核对时，只报告工程行为与观察到的问题，不宣称识别质量提升。

验收以边界合理、结构可复现、明显误识别／漏识别可解释为主。参数或过滤器变更一次聚焦一个问题，保留前后图形与简单指标对比即可；不以收益显著或仓位推荐为验收目标。
