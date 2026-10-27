# 技术指标与复盘方法的源码及论文核验

核验日期：2026 年 10 月 25 日。参考项目为 `daily_stock_analysis`，吸纳目标为本项目 `daliy-stock-panel`。

**可以吸纳的是可复现的行情特征、结构描述和事件证据；现有文献不足以把参考项目的整套战法、阈值和手工加分直接当作有效交易策略。** 优先复用本项目已有指标、平台突破、市场阶段与主线结果，补齐它们进入复盘的证据。波浪、完整缠论及固定情绪周期买卖法暂不进入自动交易评分；盈余意外和行业相对强度可列为优先研究方向。

这里区分三个用途：

| 用途 | 可以得出的结论 | 采用条件 |
| --- | --- | --- |
| 复盘描述 | 某日满足哪些价格、成交量、结构或事件条件 | 定义明确、输入齐全、计算可复现，标明日期与确认状态 |
| 收益研究 | 某条件是否增加未来收益或降低风险的信息 | 在目标市场做时间外验证，比较基准并计入成本 |
| 自动评分或交易 | 据此排序、分配仓位、触发买卖 | 还需验证成交约束、稳定性及与已有信号的增量作用 |

本次完成源码盘点和公开论文核验，未运行这两套项目的收益回测，也没有修改策略、评分或复盘业务代码。下文“可吸纳”默认指第一层；列为研究候选不表示已通过第二、三层。

## 源码实际实现

参考项目 HEAD 为 `88c4991e4545d9a3eab2a3c19d07f38b4af78f97`，工作树另有未提交内容。因此下述功能以核验日的工作树为准，不能仅用该提交复现全部结论。参考项目的源码位置均相对其仓库根目录；本项目链接则相对本文件。

### 三种机制必须分开

1. **基础计算与识别工具**：`src/stock_analyzer.py` 和 `src/agent/tools/analysis_tools.py` 确定性计算指标、部分交叉和简化形态。
2. **13 个 LLM 分析技能**：根 `strategies/*.yaml` 的自然语言指令会进入真实模型调用，模型按需使用工具并生成意见。这不是 13 套完整的机械交易算法。接线见 `src/agent/skills/base.py:433`、`src/agent/factory.py:510`、`src/agent/executor.py:633`、`src/agent/runner.py:458`；传统分析路径也可加载技能，不能概括成只有 Agent 模式才使用。
3. **7 个确定性筛选预设**：`src/services/screening/strategies/` 配置硬条件和因子权重，经 `pipeline.py` 的快照过滤、日线特征、硬过滤、评分执行；部分路径再加入 LLM 分析。不能因上一层使用提示词，就把整个参考项目说成没有算法。

### 13 个分析技能逐项清单

表内数值是参考项目的规则，均不是本报告认可的最优参数。`+N` 是模型分数调整指令，没有被校准为收益或胜率。

| 方法与源码 | 参考项目定义 | 实际支撑及缺口 | 吸纳结论 |
| --- | --- | --- | --- |
| 多头趋势 `strategies/bull_trend.yaml:17` | MA5≥MA10≥MA20、MA20 向上，偏好回踩与放量突破；强多头 +12 | 均线可计算；底层严格多头排列及发散度与提示词不完全相同 | 趋势描述可用；固定分数待验证 |
| 均线金叉 `ma_golden_cross.yaml:19` | 近 3 日 MA5 上穿 MA10，或 MA10 上穿 MA20；量比>1.2、乖离<5%，可叠 MACD | 核心工具没有完整返回这组三日组合事件；模型需另算 | 交叉事件可用；联合条件作为研究候选 |
| 放量突破 `volume_breakout.yaml:20` | 过 20 日前高或平台，量>5 日均量 2 倍，收盘处于振幅顶部 30%，乖离<5% | 图形工具另有前 20 日高点突破，但量门槛是前 5 日均量的 1.5 倍 | 优先研究价格突破及量能的增量作用 |
| 热点题材 `hot_theme.yaml:24` | 板块强势、扩散、公司实质受益，个股量价强于板块；启动扩散 +12 | 新闻与当日板块排行可取；历史扩散、公司受益程度主要由模型判断 | 板块相对强度与已核实事件可用；不照搬题材分数 |
| 缩量回踩 `shrink_pullback.yaml:21` | MA5>MA10>MA20，距 MA5≤1% 或 MA10≤2%，量<5 日均量 70%，守均线 | 核心工具支撑距离均为 2%；下跌且缩量就得量能最高分，没有验证洗盘 | 展示价跌、量缩、距均线；交易组合待研究 |
| 事件驱动 `event_driven.yaml:23` | 事件分类、发布时间、可信度、兑现程度、量价确认；未充分反映的利好 +14 | 新闻检索可用；没有确定性的预期差模型与一致预期数据 | 优先采用事件时间线；“未充分反映”需独立证据 |
| 箱体震荡 `box_oscillation.yaml:25` | 60—120 日历史、多次触碰，箱底顶附近 5%，两倍量突破、连续两日确认 | 图形工具仅计算近 10 日高低区间<8%，不验证触碰和两日确认 | 区间与突破描述优先；箱底买箱顶卖须回测 |
| 底部放量 `bottom_volume.yaml:19` | 20 日高点至近期低点回撤>15%，空头，量>5 日均量 3 倍，收阳守低点 | 有基础量价；没有专用序列识别，前期极度缩量也未精确定义 | 可标“低位异常放量候选”，不标“见底确认” |
| 缠论 `chan_theory.yaml:24` | 分型、笔、线段、中枢、MACD 面积背驰、一二三买及仓位 | 此技能没有对应包含处理、笔和中枢算法，也没有 MACD 面积序列工具 | 可研究因果确认的结构特征；不直接采用完整战法 |
| 波浪理论 `wave_theory.yaml:24` | 120 日日线、五浪三浪、固定斐波那契回撤延伸、模型数浪置信度 | 无确定性转折点、数浪、浪级及规则校验器 | 可显示候选波段和回撤；暂不作为自动买卖依据 |
| 龙头策略 `dragon_head.yaml:20` | 板块强，率先上涨或涨停，换手通常>5%、量比>1.5，跑赢板块>2 个百分点 | 此技能没有完整龙头检测器；独立 hotspot 服务另有角色算法，见后文 | 行业相对强度与客观领涨排序可用；接力战法待研究 |
| 情绪周期 `emotion_cycle.yaml:24` | 个股换手、量脉冲、新闻、均线和背离；底顶各五项满足≥3 项调整分数 | 是个股情绪提示词，非全市场六阶段状态机；一年换手、机构历史等数据不保证齐全 | 采用可测量的活跃度与市场状态；固定顶底分数不采用 |
| 一阳夹三阴 `one_yang_three_yin.yaml:18` | **五根** K 线：阳线、三根小阴或小 K、确认阳线；首阳实体>2%，中间量比<0.8，末收高于首收，加多头均线 | 无专用检测器；近似 Rising Three Methods，不能等同任意“一阳三阴”或一阳吞三阴 | 精确定义后可作形态标签与研究候选；不照搬 +15 分 |

**预期重估不在当前 13 个技能中。** `strategies/README.md:99` 明确已移除成长质量和预期重估技能；当前基本面聚合也不能提供完整财务和一致预期能力。`src/sentiment/` 只有占位文件，`docs/sentiment.md` 是尚未实现的观点采集设计，不是另一套已上线情绪模型。

### 基础指标及口径

| 指标或特征 | 实际公式与位置 | 迁移时需要注意 |
| --- | --- | --- |
| SMA | 收盘价滚动算术均值，5/10/20/60 日；`stock_analyzer.py:265` | 不足 60 根时将 MA20 填入 MA60，不能照搬这种替代 |
| MACD | DIF=EMA12−EMA26；DEA=EMA9(DIF)；柱=2×(DIF−DEA)；`:277` | 当日交叉和零轴状态已编码；没有背离和柱面积序列识别 |
| RSI | 上涨与下跌幅以 `alpha=1/n` 指数平滑，周期 6/12/24，主要按 RSI12 分组；`:305` | screening 另算简单均值 RSI14；两者不是同一口径 |
| 乖离率 | `(C/MA−1)×100`；`:394` | 5%、强趋势 7.5% 及负乖离奖励均为经验规则 |
| 日成交量比 | 当日量/此前 5 日均量，排除当日；`:411` | `get_volume_analysis` 的近 5/20 日均量却含当日；供应商实时量比又是另一概念 |
| 支撑压力 | 价在 MA5/MA10 上且距离≤2%，另取 MA20、含今日的 20 日最高价；`:449` | 不是反复触碰的结构支撑，更不等于已确认反弹 |
| 突破和区间 | 前 20 日最高价突破、近 10 日高低区间<8%；`analysis_tools.py:456` | 突破排除当日，箱体包含当日；缺量时也会返回“放量突破”，不宜照搬 |
| K 线形态 | 十字、锤子类、吞没、晨暮星、简化双底；`analysis_tools.py:330` | 部分缺少前置趋势，双底无颈线突破确认；没有上述五日形态 |
| KDJ 告警 | 9 日 RSV，经两次 `alpha=1/3` 平滑得到 K/D，检测交叉；`alert_indicators.py:295` | 这段只算 K/D，没有 J；不在核心 Agent 工具链 |
| CCI 告警 | `TP=(H+L+C)/3`；`(TP−SMA14)/(0.015×14日平均绝对偏差)`；`:327` | 用户阈值穿越告警，不能自动解释成买卖优势 |
| ATR20 百分比 | 近 20 日真实波幅 TR 的算术均值/收盘价；`screening/daily.py:1023` | 不是 Wilder 递推 ATR；本项目已有 ATR14，不能同名替换 |
| 年化波动 | 近 20 个收盘形成的简单收益样本标准差×√252×100 | 描述历史波动；不是未来风险上限 |
| 最大回撤 | 窗口内 `min(C/此前累计最高C−1)×100` | 描述历史路径；不能把 20 日回撤阈值当未来最大亏损 |
| 量价相关 | 成交量水平与收盘价水平的 Pearson 相关；`analysis_tools.py:206` | 不是成交量变化与未来收益的预测检验，不能据此推断主力意图 |

`stock_analyzer.py:585` 的综合分由趋势 30、乖离 20、量能 15、支撑 10、MACD 15、RSI 10 构成；缩量下跌给量能最高分，RSI 超卖也加分。这些是手工设定，**80 分不代表 80% 胜率**。多头排列、MACD、乖离重复使用价格信息，也不能未经验证直接叠加成独立证据。

### 7 个真正的筛选预设

以下来自 `src/services/screening/strategies/`。为便于比较，表中列主要硬条件；非 ST、价格及流动性门槛也参与实际筛选。评分权重是配置值而非论文估计。

| 预设 | 主要硬条件 | 评分侧重及判断 |
| --- | --- | --- |
| `balanced_alpha` | 成交额≥1 亿、市值≥50 亿、日涨幅 −4% 至 8.5% | 动量、流动性、活跃和稳定；名字中的 alpha 不证明超额收益 |
| `momentum_quality` | 成交额≥2 亿、市值≥80 亿、日涨幅 −3.5% 至 7.5% | 更重动量；这里的 quality 不是盈利质量因子 |
| `volume_breakout` | 实时量比≥2、换手≥3%、价≥MA20、技术分≥60、距前 20 日高点≥−1%、20 日量比≥1.3、整理≥8 日等 | 可以尚未突破前高，与根目录同名技能不同 |
| `shrink_pullback` | MA5≥MA20≥MA60、价≥MA20、技术分≥65、20 日量比≤1.5、偏 MA20≤8%、波动≤45%、回撤≥−12%、ATR 比≤6.5% | 围绕 MA20，既非 MA5/10 回踩，也不要求量低至 0.7 倍 |
| `capital_heat` | 成交额≥3 亿、换手≥2%、实时量比≥1.5、日涨幅 1% 至 9.5% | 偏动量和活跃，再用热点及情绪技能分析 |
| `oversold_reversal` | 当日跌幅 −8% 至 −1%、成交额≥0.8 亿、换手≥1% | 偏反转；不要求三倍量、收阳或阶段跌幅>15%，与“底部放量”不同 |
| `gap_limit_up` | 排除部分板块，近 20 交易日有完整上跳缺口，价≥MA20 且 MA20 上行，关键交易日完整 | 另评缺口、整理及市场风险；没有财报条件，不等于业绩断层 |

执行入口见 `screening/strategy.py:184`、`pipeline.py:118`、`filter.py:347`、`scorer.py:90`。日线硬过滤对缺列和空值有处理，但部分指标仍用短历史替代长窗口或将不足数据记为 neutral；吸纳时应保留“不可计算”的真实状态。

`screening/hotspot.py:157` 另有初次异动、扩散、加速、分歧、退潮标签；`:229` 用涨幅、成交额、换手、量比、净流入、涨停和持续天数等评分，再分核心龙头、助攻、补涨等角色。这是真实规则，但分数和标签本身仍未证明接力收益，也不是根目录龙头技能的同一算法。

## 论文证据与可用边界

优先使用正式期刊、原始会议论文、作者稿和官方研究。下文分别说明样本及规则是否匹配、成本与样本外信息；只取得摘要或出版预览的来源不承担全文级结论。“没有找到直接验证”指本次公开检索的边界，不等于证明该方法永远无效。

### 均线 金叉 MACD RSI 乖离与价格突破

| 原始研究 | 样本与主要发现 | 对本项目的意义及限制 |
| --- | --- | --- |
| Brock、Lakonishok、LeBaron，1992，Journal of Finance，[Simple Technical Trading Rules and the Stochastic Properties of Stock Returns](https://doi.org/10.1111/j.1540-6261.1992.tb04681.x) | DJIA 1897—1986；均线和区间突破有统计预测信息 | 类别正证据；本次核对摘要，不能据此证明 MA5/10/20、两倍量或今天的 A 股净收益 |
| Bajgrowicz、Scaillet，2012，JFE，[Technical trading revisited](https://scaillet.ch/pdfs/BajSca.pdf) | DJIA 1897—2011、7,846 条规则；控制假发现，按月检验事前选优持久性，优势缺乏延续且易被成本抵消 | 已核对作者全文；直接反对“挑历史最高收益参数就有效”，不等于所有技术描述无用 |
| Zhu 等，2015，Physica A，[Profitability of simple technical trading rules of Chinese stock exchange indexes](https://arxiv.org/abs/1504.04254) | 上证 1992—2013、深证综指 1991—2013；48 个均线及突破变体，加入其单边 0.3%/0.5%/1% 成本后不再通过相应显著性检验 | 有 A 股指数证据；已核对全文。历史成本假设不能当当前费率，指数结果也不等于个股联合规则 |
| Wang 等，2015，Physica A，[Testing the performance of technical trading rules in the Chinese market](https://arxiv.org/abs/1504.06397) | 上证、沪深 300，超过 7,000 条规则；SPA 检验和移动窗口显示市场及时期差异，牛市区段影响明显 | 已核对作者全文；未见成本模型，分段检验不是严格独立选模测试 |
| Chong、Ng、Liew，2014，JRFM，[Revisiting the Performance of MACD and RSI Oscillators](https://www.mdpi.com/1911-8074/7/1/1) | 五个发达市场指数，1976—2002；部分 MACD、RSI 规则有正结果，部分市场计入 1% 往返成本仍正 | 已核对全文；显著的 DIF 穿零不等于“零轴上金叉”，RSI 回穿 30 不等于低于 30 就买，参数与平滑也不同 |
| Ko、Wang、Yang，2025，Financial Analysts Journal，[Short-Term Moving Average Distance and the Cross-Section of Stock Returns](https://doi.org/10.1080/0015198X.2025.2533099) | 月末价格相对 10 日均线的距离与随后横截面收益相关，高距离对应较低收益 | 仅核对正式摘要与作者稿索引；成本和最终样本外未核实。可研究连续距离，不支持 MA5 乖离 5% 硬门槛 |

**判断**：MA、MACD、RSI、乖离和突破均可作为复盘事实。要成为交易信号，应分别检验交叉、趋势延续、超卖反转，不能将它们混合成一个未经校准的“越多命中越好”分数。参考项目的 3 日交叉、5% 乖离、零轴上金叉加分没有在上述研究中得到直接验证。

### 放量突破 缩量回踩与底部放量

| 原始研究 | 样本与主要发现 | 可支持和不能支持的结论 |
| --- | --- | --- |
| Gervais、Kaniel、Mingelgrin，2001，Journal of Finance，[The High-Volume Return Premium](https://doi.org/10.1111/0022-1082.00349)；[作者早期全文](https://rodneywhitecenter.wharton.upenn.edu/wp-content/uploads/2014/04/9901.pdf) | NYSE 1963—1996，异常日或周成交量与随后约一月收益；美国有高量溢价 | 支持异常量值得研究。作者稿普通组合可能被成本吞没，部分限价模拟不能视为现实无摩擦收益；不是三倍五日量的底部形态 |
| Campbell、Grossman、Wang，1993，QJE，[Trading Volume and Serial Correlation in Stock Returns](https://web.mit.edu/wangj/www/pap/CampbellGrossmanWang93.pdf) | 美国指数及个股；模型和数据表明流动性交易造成的放量下跌可以伴随后续反转 | 已核对全文；反对把“放量下跌=最坏、缩量下跌=最好”当普遍规律，也不保证任何一次放量下跌都会反转 |
| Llorente 等，2002，RFS，[Dynamic Volume-Return Relation of Individual Stocks](https://web.mit.edu/wangj/www/pap/LlorenteMichaelySaarWang02.pdf) | NYSE/AMEX，1993—1998；风险分担型交易较易反转，信息型交易较易延续 | 已核对全文；支持量价关系需要条件化，不能凭量柱识别“主力洗盘” |
| Wang、Cheng，2004，Pacific-Basin Finance Journal，[Extreme volumes and expected stock returns](https://doi.org/10.1016/j.pacfin.2004.04.002) | 沪深 A 股 1994—2000；高量后较低、低量后较高的结果与美国相反，受股票特征影响 | 核对出版摘要及预览；成本细节未核实，未检验本项目底部组合 |
| Wang、Wen、Singh，2017，同刊，[The high-volume return premium: Does it exist in the Chinese stock market?](https://doi.org/10.1016/j.pacfin.2017.10.003) | 沪深 A 股 1991—2015；没有普遍的美股式高量溢价，投机特征较强股票出现高量折价 | 核对出版摘要及预览；完整成本和严格样本外未核实，足以阻止把量比单调加分当既定事实 |

**判断**：优先拆成“价格突破”“历史量分位”“量价交互”“前期趋势”四个可检验变量。分别比较仅价格突破和加量能过滤后的表现。底部放量、缩量回踩都可进入事件研究，但尚不能称可靠买点；参考项目的 15% 回撤、3 倍量、0.7 倍缩量和最高 15 分均不应直接采用。

### 箱体震荡与支撑阻力

Osler（2000）在纽约联储研究 [Support for Resistance](https://www.newyorkfed.org/research/epr/00v06n2/0007osle.html) 中，用 1996—1998 年六家机构事前发布的外汇支撑阻力和分钟行情检验，发现这些位置对趋势中断有预测信息，但明确未证明可获交易利润。该研究有[全文](https://www.newyorkfed.org/medialibrary/media/research/epr/00v06n2/0007osle.pdf)，市场和频率均不同于 A 股日线。

Zapranis、Tsinaslanidis（2012），Applied Financial Economics，[Identifying and evaluating horizontal support and resistance levels](https://www.tandfonline.com/doi/abs/10.1080/09603107.2012.663469)，用近 20 年美股复权日收盘和局部极值定义水平支撑阻力；出版摘要报告支撑的趋势中断识别较好，但未超过买入持有收益。全文未取得，精确年份、成本和独立样本外尚不能确认。

**判断**：可优先用于复盘描述，但要区分“窄幅区间”和“反复触碰形成的箱体”。输出上下沿、箱宽、触碰与突破确认日期；不把 5% 箱底区、3% 止损或两倍量当学术验证参数。本项目已有平台突破算法，应先复用它，后续再判断是否需要独立的多次触碰箱体定义。

### 一阳夹三阴与其他 K 线形态

Duvinage、Mazza、Petitjean（2013），Quantitative Finance，[The intra-day performance of market timing strategies and trading systems based on Japanese candlesticks](https://doi.org/10.1080/14697688.2013.768774)，研究 30 只 DJIA 股票 2010—2011 年五分钟数据，83 个规则明确包括 `RISEFALL3METHODS`。[作者全文](https://research.dial.uclouvain.be/bitstreams/31c13a2c-b7fd-4be0-8d87-14265a10ced4/download) 的成本及 SSPA 检验显示，在其 double-or-out 策略设定下，加入单边 5bp 后单规则未显著超过持有基准。它是相近形态家族证据，不能直接断言 A 股日线五日组合一定无效。

另一项正式研究有正结果：Lu、Chen、Hsu（2015），Journal of Banking & Finance，[Trend definition or holding strategy](https://doi.org/10.1016/j.jbankfin.2015.09.009)，使用 1992—2012 年美股的八种三日反转形态，部分退出方法在 0.5% 成本与 Step-SPA 后仍有效。[作者全文](https://www.econ.sinica.edu.tw/~econ/pdfPaper/14-A010.pdf) 也显示结果依赖持有规则；样本偏向完整存续股票，且它并非五日延续形态。

**判断**：K 线标签可采用，交易效果须把形态、前置趋势、量能和退出方法一起定义。用户提到的“一阳三阴”在参考项目中实际是“阳线—三根小 K—确认阳线”，先固定这个定义，才有可回测对象。首阳 2%、量比 0.8 和固定 +15 分没有被这些论文验证。

### 波浪理论与斐波那契

| 原始研究 | 证据与限制 |
| --- | --- |
| Tsinaslanidis、Guijarro、Voukelatos（2022），Expert Systems with Applications，[Automatic identification and evaluation of Fibonacci retracements](https://doi.org/10.1016/j.eswa.2021.115893) | 三个股票市场的自动回撤检验，出版摘要和预览报告斐波那契区域不优于随机区域。全文未取得，成本与严格样本外未核实；直接削弱固定比率的特殊性，未穷尽检验所有数浪法 |
| Batchelor、Ramyar（2006），大学讨论稿，[Magic numbers in the Dow](https://openaccess.city.ac.uk/id/eprint/16276/) | DJIA 1915—2003，原文用 bootstrap 检验趋势幅度与时间比率，少量显著现象不超出多次检验偶然范围；不是可交易策略实验 |
| Atsalakis 等（2011），Expert Systems with Applications，[The WASP system](https://doi.org/10.1016/j.eswa.2011.01.068) | 波浪启发特征加神经模糊模型，400 个交易日，作者报告优于持有；仅取得出版预览，成本和最终独立测试不明，不能归因于手工数浪本身 |
| Shanaev、Gibson（2022），[Can Returns Breed Like Rabbits?](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4215708) | 工作论文摘要报告斐波那契正结果；未取得全文或确认正式期刊版本，成本与样本外不明。说明存在不同结果，不能声称文献一致否定 |

**判断**：波段幅度和回撤比例可如实展示；自动浪号、目标价和交易加分暂不采用。关键困难是数浪规则、确认时点和可重复性，而非是否能画出符合理论的历史图。任何事后确认的波峰波谷都不能提前写回历史信号。

### 缠论

Chen 等（2022）的 [DDPG based on multi-scale strokes for financial time series trading strategy](https://doi.org/10.1145/3543712.3543716) 是正式 ACM ICCTA 会议论文，亦有 [arXiv 作者稿](https://arxiv.org/abs/2207.10071)。它从包含、分型及多尺度笔构造强化学习输入，测试沪深 300、上证、DJIA 和 S&P 500，包含按时间分开的训练与测试，作者报告中国指数表现较好。

这是真实相关研究，不能写成“缠论没有论文”。但作者稿未清晰报告手续费、滑点、涨跌停及笔端点在线确认的完整审计，模型也不是参考 YAML 中的中枢、MACD 面积背驰、一二三买和仓位规则。

**判断**：可将可因果确认的分型、笔或多尺度结构列为较低优先级研究特征；完整缠论战法目前缺乏与参考实现相匹配的可交易验证。不能让 LLM 口头判断“一买”再给确定的买入分数。

### 情绪周期 热点与龙头

| 原始研究 | 主要证据 | 对参考实现的约束 |
| --- | --- | --- |
| Baker、Wurgler（2006），Journal of Finance，[Investor Sentiment and the Cross-Section of Stock Returns](https://onlinelibrary.wiley.com/doi/full/10.1111/j.1540-6261.2006.00885.x) | 美国情绪代理与特定股票的后续横截面收益有关 | 已核对原文；不是个股换手 5% 判顶，也没有验证 A 股六阶段或连板周期 |
| Leong 等（2026，2025 年在线发表），Economic Inquiry，[Re-examining investor sentiment and stock returns](https://onlinelibrary.wiley.com/doi/full/10.1111/ecin.13290) | 扩展至美国和中国、更新期至 2023；美国总体结论可复现，部分代理的预测方向会变化，中国未呈现同样显著的横截面结果 | 已核对原文；说明跨市场迁移不稳，不能扩大成所有情绪变量都无效 |
| Moskowitz、Grinblatt（1999），Journal of Finance，[Do Industries Explain Momentum?](https://doi.org/10.1111/0022-1082.00146) | 美国 20 个行业、1963—1995，行业动量解释部分个股动量 | 已核对作者全文；月度行业组合不等于日内龙头接力，原文未完成实际成本后的全面盈利验证 |
| 段丙蕾、汪荣飞、张然（2022），金融研究，[南橘北枳 A股市场的经济关联与股票回报](https://www.jryj.org.cn/CN/abstract/abstract1000.shtml) | 原刊摘要报告 A 股月度行业动量和科技关联的短期预测性 | 仅核对原刊摘要，样本期与成本未核实；属于相关本土证据，不能替换龙头阈值验证 |
| Liu、Wu、Zhu（2022），Economic Modelling，[Price overreaction to up-limit events and revised momentum strategies in the Chinese stock market](https://www.sciencedirect.com/science/article/pii/S0264999322001560) | A 股 2000—2020；涨停过度反应侵蚀常规动量，调整涨停相关收益后动量更显著 | 核对出版摘要和预览；成本与独立样本外未核实。支持将涨停事件单独处理，不证明所有接力有效或无效 |

**判断**：把情绪拆成市场宽度、封板率、晋级率、连板高度及其历史变化；把龙头拆成板块内超额收益、成交额、领涨持续性和扩散程度。它们可以支持复盘解释，标签仍是项目规则。行业动量的文献基础比“换手>5%就是龙头”更直接；应优先研究连续的行业与个股相对强度，而非先固化一套龙头仓位法。

### 事件驱动与预期重估

“利润增长”“消息利好”“低估值”与“超出此前预期”是不同变量。预期重估至少需要当时可得的预期基线、实际公告及公告时间；拿事后修订预测或报表期代替公告时间会泄露未来信息。

| 原始研究 | 主要证据与边界 |
| --- | --- |
| Chan、Jegadeesh、Lakonishok（1996），Journal of Finance，[Momentum Strategies](https://doi.org/10.1111/j.1540-6261.1996.tb05222.x)；同作者（1999），FAJ，[The Profitability of Momentum Strategies](https://rpc.cfainstitute.org/research/financial-analysts-journal/1999/the-profitability-of-momentum-strategies) | 出版摘要支持盈余消息、预测修正和价格动量有不同的信息。可研究标准化未预期盈余、预测修正；本次未完成正文级成本及严格样本外复核，不能据此保证 A 股收益 |
| Ng、Rusticus、Verdi（2008），JAR，[Implications of Transaction Costs for the Post–Earnings Announcement Drift](https://doi.org/10.1111/j.1475-679X.2008.00290.x) | 正式摘要指出成本会明显削弱盈余公告后漂移收益，毛收益高的标的往往也更难交易；不能只报告事件后平均涨幅 |
| Cai 等（2021），Accounting and Business Research，[The role of institutional investors in post-earnings announcement drift](https://digitalcommons.uri.edu/cba_facpubs/357/) | 大学作者记录及摘要提供中国 PEAD 证据，并涉及机构持有与公告后第四个季度的反转；成本、样本期和样本外未完成核实 |
| Tetlock、Saar-Tsechansky、Macskassy（2008），Journal of Finance，[More Than Words](https://doi.org/10.1111/j.1540-6261.2008.01362.x) | 已核对作者全文；S&P 500 相关新闻 1980—2004 的负面语言与基本面及短期价格反应有关，合理成本可消掉高频收益。支持新闻内容有信息，不支持 LLM “利好+14” |

**判断**：事件时间线和已发生的业绩事实可优先接入复盘；盈余意外、分析师修正是值得验证的研究方向。没有一致预期时，可以另定义基于历史盈利预测的意外，但须明确其模型，不能沿用“市场预期差”的名字。本项目现有“业绩断层跳空”是利润条件加缺口，不能被重新解释成已测得预期差。

### KDJ CCI ATR 波动率与回撤

| 特征 | 相关原始研究 | 结论 |
| --- | --- | --- |
| KDJ | Chen、Zhou、Wang（2018），Physica A，[Profitability of simple stationary technical trading rules with high-frequency data of Chinese Index Futures](https://doi.org/10.1016/j.physa.2017.11.088)，[作者全文](https://arxiv.org/abs/1710.07470) | 沪深 300 期货 2012—2016，15/30/60 秒、平稳化 MA/KDJ/BOLL，成本与 SPA 后优势消失。是异频率变体的证据，不能直接推论日线 K/D 告警 |
| CCI | Chaysiri 等（2020），BU Academic Review，[Tests of Technical Trading Strategies with Different Asset Conditions](https://so01.tci-thaijo.org/index.php/buacademicreview/article/view/240412) | 正式审稿期刊全文；19 只持续入选泰股、2007—2017，CCI20 有部分正结果，但采用 3bp 的入离场成本假设，作者承认偏低。不能验证 CCI14 与 A 股净收益 |
| ATR | 本次未找到直接验证参考项目 ATR20/close≤6.5% 筛选条件的可靠论文 | 可作为波幅事实和风险尺度；阈值收益效果须单独验证 |
| 波动率 | Moreira、Muir（2017），Journal of Finance，[Volatility-Managed Portfolios](https://doi.org/10.1111/jofi.12513)；Cederburg 等（2020），JFE，[On the performance of volatility-managed portfolios](https://www.lehigh.edu/~xuy219/research/COWY.pdf) | 前者报告按历史方差调节风险的优势；后者对 103 个策略发现可实时执行的样本外版本无系统优势。均已核对作者全文，不能为 45% 筛选阈值背书 |
| 最大回撤 | Chekhlov、Uryasev、Zabarankin（2005），IJTAF，[Drawdown Measure in Portfolio Optimization](https://www.math.columbia.edu/~chekhlov/IJTheoreticalAppliedFinance.8.1.2005.pdf) | 原文研究回撤风险度量与组合优化，不是“20 日回撤优于 −12% 会涨”的证明 |

这些风险特征可以直接用于描述历史行情；它们并不需要先证明可预测上涨。只有当用于筛选、排序或调仓时，才需要证明对应规则的增量效果。参考项目未发现 BOLL 实现，本项目已有的布林带不计入“从该参考项目吸纳”的清单。

## 在本项目中的吸纳顺序

### 先复用现有能力补齐复盘证据

| 现有位置 | 已有能力 | 本次研究导出的下一步 |
| --- | --- | --- |
| [指标流水线](../backend/app/indicators/pipeline.py) | MA、MACD、RSI6/14/24、KDJ、BOLL、ATR14、量比、动量、波动等 | 维持现有统一口径；不为迁移参考项目再建 pandas 指标链 |
| [个股分析](../backend/app/services/stock_analyzer.py) | 最多 90 根 K 线及白名单指标进入模型，包含部分交叉信号 | 补充已计算的形态条件、确认日期及未命中原因；不能假设全部策略命中已进上下文 |
| [平台整理突破](../backend/app/strategy/builtin/platform_consolidation_breakout.py) | 默认前 10 日区间、区间宽度/当日收盘≤8%、突破前高、5 日量比≥1.5 | 优先作为现有复盘证据；它仍是经验规则，不因存在算法就等于学术验证 |
| [新低反转](../backend/app/strategy/builtin/n_day_low_reversal.py) | 默认 `close≤low_60d`、收阳、量比≥1.5 | 与参考“回撤>15%后3倍量”不同；先核对实际事件语义，再决定是否新增低位量价事件 |
| [市场阶段](../backend/app/services/market_phase.py) | 根据高度、宽度、晋级率和封板率等产生六阶段序列 | 可接入复盘解释，保留原始驱动和规则性质，不另复制个股换手情绪模型 |
| [市场主线](../backend/app/services/market_mainline.py) | 主线、连板与成交额等维度的领涨角色 | 先把板块历史与相对强度证据用于复盘；不把“龙头”标签当收益承诺 |
| [大盘复盘](../backend/app/services/market_recap.py) | 当前指数、盘面、情绪分、板块排行及可选龙虎榜、竞价背景 | 当前提示组装未直接纳入阶段和主线的历史序列；这是比增加理论名称更明确的补强点 |
| [业绩断层跳空](../backend/app/strategy/builtin/earnings_breakout_gap.py) | 当时可用正式利润表为正且同比增长，首次可用日后首个交易日形成完整向上缺口 | 保留现有事件定义；预期差研究需要新增可证明的预期基线，不能只改标签 |

上述为后续实施建议，本次没有改动这些文件。

### 研究优先级

1. **优先验证已有、容易精确定义的变量**：平台突破、异常量分位与量价交互、行业和个股相对强度、连续均线距离。用已有算法作为起点，比较新变量是否比现有信号多提供信息。
2. **事件数据具备时验证预期差**：盈余意外、盈利预测修正、公告后漂移；先检验当时可得数据，再研究收益。普通新闻和利润增长不能代替预期基线。
3. **有明确需求再添加形态研究**：多次触碰箱体、五日一阳夹三阴、精确定义的缩量回踩与低位放量。先做事件统计，避免一开始叠加多组均线和阈值。
4. **暂缓完整理论自动决策**：波浪自动数浪、完整缠论买卖点、固定情绪周期仓位及龙头接力。主要缺口是定义和在线确认、匹配实证及交易可执行性。

## 后续收益验证的最小设计

这部分适用于拟进入排序、交易或收益预测的候选，单纯显示事实无需承担整套策略验证流程。

1. **先固定事件和时间**：窗口按交易日；收盘确认信号最早在下一可交易时点使用。次日确认、未来形成的笔端点和事后新闻不得回填信号日。财报按首次可得公告版本，历史板块与股本按当时信息。
2. **比较简单基准与增量**：例如“价格突破”对比“价格突破+放量”，五日形态对比相同趋势、波动及行业背景的普通股票。记录样本量、分组收益、风险与换手，不只报告命中率。
3. **按时间留出数据**：训练、参数选择与最终测试分开，使用现有步进优化边界；保留试过的规则以评估多重检验。不能反复看最终测试段后再称其为样本外。
4. **使用可成交收益**：沿用本项目 T+1、停牌、涨跌停、手续费、滑点及信号与成交时间隔离；测试成本变化和低流动性敏感度。全市场样本应包含退市和历史成分，避免只选今天还活着的股票。
5. **检验稳定性与已有信息重复**：区分时期、规模、流动性、行业和波动环境，比较指数与个股、不同持有期。新特征只有在留出样本仍有稳定增量时，才考虑进入正式评分；失败也可保留为清晰标注的行情描述。

实现时继续遵守 [维护约定](../CONTRIBUTING.md)：技术指标使用既有复权口径，涨跌停使用原始价及历史规则，百分数与小数显式转换，缺数据不伪装成中性或低风险。参考项目的短窗代长窗、同名量比不同分母及手工评分不应成为新的长期契约。

## 检索范围与证据限制

逐项检索覆盖：Elliott wave、Fibonacci retracement、Chan theory 和 multi-scale strokes、support/resistance、candlestick 与 rising three methods、MA crossover、MACD、RSI、moving-average distance、volume breakout、shrink-volume pullback、bottom-volume reversal、investor sentiment、industry momentum、price limits、earnings surprise、analyst revisions、PEAD、KDJ、CCI、ATR、volatility 和 drawdown；同时使用相应中文关键词与论文题名查找原刊及作者稿。

没有把营销博客、公开视频胜率、策略 Prompt 或无法核对研究设计的总结当作有效性证据。部分正式研究只能读取摘要或出版预览，已经在相应条目注明；公开检索不能替代穷尽付费数据库或独立复现。本次核验足以决定哪些内容可先进入复盘、哪些值得研究，尚不足以宣布任何一套参考项目策略已经在本项目 A 股样本上有效。
