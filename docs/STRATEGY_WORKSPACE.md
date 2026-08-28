# A股小资金质量成长策略工作区（暂停兼容线）

> 本线已暂停但不删除。当前唯一正式研究主线为 [`a-share-technical-momentum-adaptive-v1`](TECHNICAL_MOMENTUM_FORMAL_P0.md)；本文件只保留质量成长 V1 的历史契约与阻塞状态，不再代表默认研究方向。

`a-share-small-account-quality-growth-v1` 仅作为 `research.strategy_workspace` 的暂停兼容线保留。它面向真实资金决策设计，但只生成研究、Paper 账本和人工复核候选；仓库永久不支持 LIVE 下单。

## 非默认的自适应仓位 V2

`a-share-small-account-adaptive-exposure-v2` 是与本页 V1 主线并存的独立策略版本，规格见[自适应仓位 V2](ADAPTIVE_EXPOSURE_V2.md)，模块索引见[策略工作区代码说明](../research/strategy_workspace/README.md)。P0.1 的七项执行问题已经修复并冻结：暂停/数据失败禁止 BUY、Gate 独立覆盖全部退出持仓、四类减仓支持首次 D+1、日亏损不阻断纯减仓、Paper 账户 fingerprint CAS、canonical 费用/证券规则 bundle，以及整批预检先于 `SUBMITTING`。

V2 现已实现 Alpha Engine、Exposure Engine、Portfolio Constructor、Next-session Adapter 和12阶段Daily Pipeline代码契约；每天可冻结包含BUY/SELL/HOLD/CASH、目标/可实现/当前/实际仓位、整手、成本、取消条件、原因与哈希的JSON/Markdown决策，且允许零订单。Factor Discovery四层治理把LLM候选、独立Validation receipt、批准条目和确定性registry分开；`frozen-alpha-model.v2`绑定train-only训练、同目标同期限校准、模型准入与Experiment V3结构绑定，Exposure/Constructor使用V2 policy并处理正收益门与池外强制退出。

正式Experiment V3 loader仍固定为 `blocked_not_implemented`，生产代码没有issuer token/helper，receipt只提供 `diagnostic_binding_only_not_formally_admitted` 的结构绑定。因此正式Alpha固定输出 `DATA_FAIL_CLOSED`，不能产生BUY；诊断打分也没有发布权限。Daily固定本地registry的authority仅有 `BLOCKED` 与 `RISK_REDUCTION_ONLY`：阻断日写4项最小证据且不能进入D+1；四类风险退出写17项完整证据，可在首次紧邻D+1单次人工复核。风险退出不会被正式Alpha阻断冒充为普通买入，当前不存在Alpha发布authority。

每项发布artifact先做canonical JSON roundtrip，按策略日create-only占槽，全部写完后最后写`COMMITTED`；部分写入会毒化该日期槽并失败关闭，必须人工恢复。Next-session从该固定registry重读精确字节，调用方对象或哈希不能替代。该registry与既有Exposure/consumption registry都是单机文件系统CAS，其ACL只是本地writer权限边界，不是外部来源认证或多机一致性证明。上述变化不改变V1默认入口、Top2/20%最低现金、Experiment v2或既有账本哈希；外部受控PIT、生产官方registry与正式Experiment V3仍未接入，2024—2025 Locked Test未运行、未解释，所有准入继续关闭。

2026-08-24最终红队关闭三项阻断：一是Daily publication现在对正式Daily decision、Exposure decision和Alpha ranking执行对应冻结JSON Schema，而不只检查自哈希；二是authority、decision/data status、failure receipt、安全旗标及Exposure固定state/target到Intent、Construction、Daily的可达条件图必须同时成立，Next-session加载固定字节后再独立复核一次；三是Experiment诊断receipt、Daily admission/publication/loader结果和Next-session Signal均要求exact contract type并绕过动态分派调用基类校验，恶意子类不能覆写`to_dict`或验证方法。

## 当前真实状态（2026-08-24）

| 层级 | 状态 | 结论 |
|---|---|---|
| 策略与代码契约 | 已实现 | 六因子、PIT 时点校验、截面残差化、Fama–MacBeth/Newey–West、固定 Ridge、Top Decile/Top2 成本账本及 append-only Paper 账本内核已实现；这不代表数据已接入或准入闭环已跑通 |
| Choice 连接 | `passed`（诊断） | 2026-08-19 qfq 日线、交易日历、指定日期板块成分、历史行业日期回显及中证800价格/全收益别名均完成真实只读探针；它们仍不是正式PIT真值或官方来源认证 |
| 正式 PIT 数据 | `blocked_missing_pit_data` | 尚未取得完整中证800历史成分、全收益基准、PIT行业/市值/交易状态及首披财务受控批次 |
| 历史统计 | 未运行 | 没有正式质量成长回测结果，不得声称有效或盈利 |
| 降级诊断 | 60只六因子截面已运行 | 当前中证800成分与16列Choice快照已经独立验证并绑定；Choice真实采集完整覆盖60只股票与中证800价格指数的121个共同交易日，并生成2026-08-18单截面。样本采用当前行业等覆盖轮转，不代表中证800行业权重；没有排名、回测或买入名单，状态仍为 `diagnostic_current_universe_not_pit` |
| Adaptive Exposure V2 日频信号 | 正式Alpha阻断；风险减仓通道保留 | 五模块、固定Daily publication registry、正式Schema与跨artifact条件图双重复核、exact-type信任边界、人工成交bundle和日频账本已实现；formal loader为 `blocked_not_implemented`。当前只发布 `BLOCKED` 或 `RISK_REDUCTION_ONLY`，后者仅支持四类风险退出首次D+1；正式PIT、官方registry及Experiment V3仍缺，Locked Test未运行 |
| Paper / 真实资金 | 不准入 | V1 append-only账本与V2日频账本均只提供内部对账证据；没有完成外部来源准入、正式Experiment V3与足够前向观察，`paper_eligibility=false`、`trade_eligibility=false`、`real_money_list_allowed=false` |
| LIVE | `live_not_supported` | 配置、白名单、Token 或枚举均不能解锁 |

真实探针保存在 `data/tmp/strategy-workspace/quality-growth-v1/capability/`，当前受控状态为 `data/tmp/strategy-workspace/quality-growth-v1/current_status.v6.json`。日线返回6条、日历返回8条；Choice终端板块主表的精确记录为 `009006039;中证800成份;...`，按该代码查询2024-06-28返回800只。用户提供的当前成分工作簿也已由 `agent.current_universe_import` 验证为800个唯一代码（沪市469、深市331），原始文件 SHA-256 为 `a1dfd62c437777778e43c3c248282faeed671ee95a43c297c29bf71ee3bd7a8c`。当前集合与2024-06-28集合重合648只、各自独有152只，直接反证了当前成分回填历史。

随后导出的16列Choice当前快照已由 `agent.current_industry_import` 绑定上述成分receipt并受控归档。红队发现旧验证器允许攻击者改写manifest状态后自行重签，因此 membership、industry 与 sample 均已升级为真正的 V2 契约，并保留已知 V1 的只读重放。稳定本地归档位于 `data/market_data/archives/strategy_workspace/quality_growth_v1/`，不再依赖可清理的 `data/tmp/`。原始文件 SHA-256 为 `60f636c3e8297e42ec33ff06d6c0bf345986c5cd4708c3b2a4a861b9c5cf581c`，行业receipt artifact SHA-256 为 `605d6b3b361fa5b7cfa0df31cc11c3a7786894ffd9850659559654c31dd91512`，manifest 自哈希为 `0a77d89f6456728f6dfe8860993f0544cebd3e7c85b6659bb5f9f6ac7cdb1168`。该文件完整覆盖800只、11个中证2021一级行业，市场字段日期为2026-08-18；但行业字段没有有效日期、ST字段是“最新”、上市日期全缺失，且 `source_authenticated=false`、`industry_effective_date=null`。因此它只能是 `current_not_pit` 诊断证据，不能解锁正式PIT、Paper、交易或真实资金清单；LIVE仍为 `not_supported`。

Choice 的固定 `HISCSIND` 最小探针在2024-06-28和2026-08-18均得到精确日期回显，三只样本分别为平安银行“金融”、美的集团“可选消费”、贵州茅台“主要消费”；artifact SHA-256 为 `b6e611ae59e8ce2da355e417029736790a380cf5c81d55b6f6a9d38575bf7da8`。固定基准探针确认本机 Choice 映射与真实响应中的 `000906.SH=中证800价格指数`、`H00906.CSI=中证800全收益指数`，artifact SHA-256 为 `aefde945c0e37335cb746fed3ac0df9e4170151ab9577494a4d28d86bea132f8`。两个探针都固定 `source_authenticated=false`；三只日期回显不能替代800只历史PIT行业，全收益单日响应也不能替代正式全收益时间序列。

## 冻结策略

- 独立策略资金为 10,000 元；美的集团 `000333.SZ` 100 股是 `unmanaged_external`，策略不能卖出、补仓或认领。
- 最多持有 2 只策略股票，每只不超过 40%，现金不低于 20%。
- 两只策略股票必须属于不同中证一级行业。
- 合并美的后，新增仓位不得令其所属一级行业超过账户总资产的 45%；美的自身上涨导致超限时，只禁止继续增加该行业，不阻止买入其他行业以降低集中度。
- 信号在收盘后冻结，下一受控交易日开盘执行；相邻决策点严格相隔 20 个受控交易日。
- 新仓要求预测收益为正且进入前 5%；原持仓预测仍为正且位于前 20%时保留。
- 人工否决后对应仓位留现金，不递补下一名。没有合格股票时允许一只或全现金。
- 回撤达到 12%后停止新开仓，并在下一个可交易决策点转向现金；卖出受 T+1、停牌和跌停约束，不能假设成交。

政策真源是 `configs/strategy_quality_growth.v1.json`；运行时加载器会拒绝放宽持仓、成本、因子、预处理、历史门或 LIVE 边界的配置漂移。

## 六个质量成长因子

| 因子 | 冻结公式 | 金融行业 |
|---|---|---|
| `QG_ROE_STABILITY` | 最新可用季度 ROE − 过去12季度 ROE 样本标准差 | 适用 |
| `QG_EARNINGS_TREND_DEVIATION` | 当前季度净利润相对前8季度 OLS 趋势预测的标准化偏离 | 适用 |
| `QG_CASH_EARNINGS_QUALITY` |（TTM经营现金流 − TTM营业利润）/ 最新资产 | 不适用 |
| `QG_CASH_DEBT_COVERAGE` | TTM经营现金流 / 最新负债 | 不适用 |
| `QG_GROSS_PROFITABILITY` | TTM毛利润 / 最新与四季前资产均值 | 不适用 |
| `QG_REVENUE_GROWTH_STABILITY` | 过去8个季度收入同比增速均值 − 样本标准差 | 不适用 |

收入同比增速由同一批首次披露收入序列计算，不接受调用者直接塞入后验增长率。金融行业不适用项明确为 `not_applicable`，任何缺失值都不补 0。

每个决策截面固定执行：1%/99%去极值；对行业哑变量、对数流通市值、盈利收益率、120日动量和60日波动做线性残差化；残差 Z-score。单因子使用 Fama–MacBeth，HAC 滞后固定为 Andrews 自动规则 `floor(4*(T/100)^(2/9))`，至少需要2个可用时段，多重检验使用 Holm、族错误率 `0.05`。多因子仅使用带截距的 `Ridge alpha=1`，金融业使用2因子子模型，非金融业使用6因子子模型，并保留预注册方向等权基线。

Ridge 只使用 2018—2022 训练标签拟合。2023 验证、2024—2025 锁定测试和 2026 审计标签可以评价，但永远不能回流到模型拟合。Rank IC 同时检查验证、锁定测试和审计分段，要求总体均值大于0且正值占比至少 `0.5`；因子显著性必须同时在锁定测试与审计段成立。相邻决策点必须严格相隔20个受控交易日，跨分段标签执行20日 purge。

## Choice 数据门

正式面板必须是完整 Choice 单源、受控留档的 PIT 批次，并同时覆盖：

- 决策时点有效的中证800历史成分，receipt 必须枚举历史成分并集的完整 `subject_ids`，不接受仅填报聚合数量；
- qfq OHLCV、成交额和受控交易日历；
- 由 Choice 返回并绑定 receipt 的中证800全收益基准代码、`total_return_open` 与 `total_return_close` 序列；
- PIT 中证一级行业、流通市值、ST、停复牌和涨跌停；
- 财务首次披露时间及六因子所需的收入、利润、经营现金流、ROE、资产和负债；流量口径必须是 `single_quarter`，报表范围是 `consolidated`，币种是 `CNY`。

禁止当前成分回填历史、只保留查询成功股票、Choice缺失后拼 BaoStock、用修订值覆盖首披值，或把 qfq 成功写成公司行为证据完整。任一核心能力或字段缺失即 `blocked_missing_pit_data`。

[Choice EmQuantAPI 官方文档](https://quantapi.eastmoney.com/Upload/EMQuantAPI_Python.html?_=637077181105957032)说明登录后才能调用数据函数；图形界面环境可用 `LoginActivator.exe` 生成令牌。激活属于用户本机账户操作，仓库不读取、保存或索要账号、密码、验证码和令牌。

固定历史批量采集器已实现，但尚未运行真实全量网络批次。它固定板块 `009006039`、内部20交易日网格、qfq/none双口径与下一交易日资格快照，CSS每批最多50只，支持孤儿artifact恢复和周期checkpoint压缩。即使采集完整，也因 `source_authenticated=false`、Choice日历未与交易所真值对账、缺PIT行业/首披财务而保持 `blocked`。首次真实运行前先用3只股票做最小权限与参数探针，不直接消耗全量额度。

绑定本轮三个真实探针并生成不可覆盖状态文件：

```powershell
python -m research.strategy_workspace quality-status `
  --policy configs/strategy_quality_growth.v1.json `
  --daily-bar-probe data/tmp/strategy-workspace/quality-growth-v1/capability/choice_daily_bar.retry4-20260819.json `
  --trade-calendar-probe data/tmp/strategy-workspace/quality-growth-v1/capability/choice_trade_calendar.retry4-20260819.json `
  --historical-sector-probe data/tmp/strategy-workspace/quality-growth-v1/capability/choice_historical_sector.csi800-20260819.json `
  --output data/tmp/strategy-workspace/quality-growth-v1/current_status.v6.json
```

该命令在阻塞状态返回非零退出码，这是失败关闭，不是程序异常。

当未来受控适配器生成完整 capability receipt 后，先独立评估：

```powershell
python -m research.strategy_workspace choice-gate `
  --receipt <choice-capability-receipt.json> `
  --output <choice-gate-evaluation.json>
```

调用者自报布尔值、字段名和哈希不能替代受控 evidence receipt；`contract_satisfied` 也不等于实时连通、统计有效或 Paper 准入。

## ExperimentSpec v2

正式运行必须在读取未来标签前创建 append-only `ExperimentSpec v2`。它精确冻结：动态PIT成分面板、Choice全收益基准、从决策日后第1个交易日开盘到第21个交易日开盘（D+1→D+21，20个收益区间）的 open-to-open 标签、交易日历与再平衡锚点、六因子、五个控制变量、固定 Ridge与上述统计契约、基础/压力成本、100万元 Top Decile 研究本金、1万元 Top2 规则、美的外部持仓、全部11项历史门，以及数据/代码/配置哈希和已消费测试区间。

中证800价格指数代码 `000906` 以[中证指数官方事实表](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/000906factsheet.pdf)为依据；全收益代码仍必须由 Choice 返回并绑定 receipt，不能手工猜测。前四项质量成长因子的公开基线来自[中证800质量成长指数 V1.3 编制方案](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/931156_Index_Methodology_cn.pdf)，`931156` 只作挑战者比较，不替代我们的样本外检验。

```powershell
python -m research.strategy_workspace freeze-experiment `
  --input <experiment-v2-input.json> `
  --output <new-experiment-v2.json>
```

目标文件已存在时拒绝覆盖。2024—2025 或 2026 区间若此前已被查看，必须登记为 `retrospective_consumed`，不能重新称为新鲜 holdout。

## 成本与A股成交

- 佣金：成交额 `0.00018`，单笔最低 5 元；
- 卖出税：`0.0005`；
- 双边过户费：`0.00001`；
- 基础单边滑点：10 bps；
- 压力：20 bps 滑点和双倍佣金。

以上是当前统一研究配置，不冒充逐日历史费率。每只证券的交易单位来自受控元数据，不把所有A股硬编码为100股；一手超出单仓预算时按冻结排名寻找下一只可负担股票。新仓还要求非ST、非停牌、非封板、上市至少250个交易日且20日平均成交额不少于1亿元。

卖出印花税、双边过户费和券商最低5元佣金的公开起点见[上海证券交易所投资者费用说明](https://one.sse.com.cn/onething/gptz/)；用户确认的万1.8佣金作为账户配置，仍应在真实投入前用券商费率页或交割单人工复核。

## 历史门、Paper 与人工候选

历史研究必须同时满足完整PIT数据、100万元 Top Decile 研究账本和1万元Top2执行账本的成本后绝对/主动收益、正且稳定的OOS Rank IC、至少2个Holm校正后方向正确因子、4个半年窗至少3个主动收益为正、压力成本主动收益不负、最大回撤不超过12%及年化单边换手不超过4倍。两个账本均按 D+1→D+21 open-to-open 口径与中证800全收益基准对齐。任一失败即淘汰，不通过换因子、换测试区间或放宽门槛挽救。

理论准入路径是：通过历史门后只能得到 `paper_admitted` 证书，之后再用绑定同一配置和证书的独立前向账本运行至少12个完整月、12个不同月份决策点；期间配置不可变，达到12%回撤立即冻结。现有 append-only Paper 账本已能重放费用、成交、未成交、持仓、现金和哈希链，但它尚不是受控的策略信号来源，也没有日频 NAV/回撤盯市记录，所以 Stage B 准入保持 `blocked_missing_controlled_paper_signal_adapter` 与 `blocked_missing_daily_paper_risk_marks`，当前不存在可达的 `manual_real_money_candidate`。`current_status.v6.json` 还明示列出 Stage A 的 `controlled_top_decile_price_bar_bundle` 与 `controlled_top2_execution_bar_bundle` 仍缺，以及待补的受控 Paper 日历、粘滞回撤冻结/退出重试和 Stage A 证书标准产物验证，不得用裸 bar 或调用者自报字段绕过。即使未来补齐并通过，也只能由用户在券商端人工复核和执行，不会自动下单。

## 数据不足时的降级

如果正式PIT门失败，可在恰好800只的完整“当前中证800成分”上按证券代码 SHA-256 和行业分层冻结60只结果盲样本，仅计算 `RM20/RM60/RM120/TREND_EFF60/DOWNSIDE_VOL60/BREAKOUT60`。输入必须显式声明 `current_not_pit`，同时绑定并重放成分与行业两个受控artifact；数量不足800、成分不一致、哈希不匹配或价格日历不一致都失败关闭。调用者提供的任意universe JSON或占位receipt哈希不再受支持。

本轮两个受控输入可分别重放：

```powershell
python -m agent.current_universe_import verify `
  --output-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_membership/choice-current-csi800-20260819-v2-a1dfd62c

python -m agent.current_industry_import verify `
  --membership-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_membership/choice-current-csi800-20260819-v2-a1dfd62c `
  --output-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_industry/choice-current-csi800-industry-20260819-v2-60f636c3
```

只有两个artifact都通过重放，才可由受控入口冻结样本：

```powershell
python -m agent.current_industry_import freeze-sample `
  --membership-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_membership/choice-current-csi800-20260819-v2-a1dfd62c `
  --industry-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_industry/choice-current-csi800-industry-20260819-v2-60f636c3 `
  --output-dir data/market_data/archives/strategy_workspace/quality_growth_v1/diagnostic/current-csi800-60-sample-20260819-v2

python -m agent.current_industry_import verify-sample `
  --membership-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_membership/choice-current-csi800-20260819-v2-a1dfd62c `
  --industry-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_industry/choice-current-csi800-industry-20260819-v2-60f636c3 `
  --sample-dir data/market_data/archives/strategy_workspace/quality_growth_v1/diagnostic/current-csi800-60-sample-20260819-v2
```

本轮已冻结60只样本，`sample_content_sha256=f5cd2725ef42906889e7ae23336fc4b1e355d7ada5b47d44ab0c4b42e3ad1723`，`sample.json` artifact SHA-256 为 `08965230d7c5691fdc83d5b1cc12c87bb79c178bfaa8e534e90d8add59343b31`。样本是“11行业等覆盖轮转”的接口诊断集，不是中证800比例代表样本。

真实价量快照已由 `agent.current_sample_snapshot` 读取固定60只 qfq 日线和 `000906.SH` 未复权价格指数，严格要求2026-02-24至2026-08-18共121个共同交易日。稳定归档为 `data/market_data/archives/strategy_workspace/quality_growth_v1/diagnostic/current-csi800-60-factor-snapshot-20260818-v1/`，共128个文件；manifest文件 SHA-256 为 `421379d5b34d6fdcc9a3bbd1263bb56d6fc7953b1fdf877889cb983b9b2bf8a9`，manifest payload SHA-256 为 `edba5a1bbbcaf69befa0df397fb1ce518eda0eac0240266efba92f6cf6819e0c`，因子截面内容 SHA-256 为 `5db65a2577bb985196b3f242901c87095fc2a6c6a0e1e624c16353aaa010c4dc`。60只均生成 `RM20/RM60/RM120/TREND_EFF60/DOWNSIDE_VOL60/BREAKOUT60`，但相对收益口径是“个股 qfq 减中证800价格指数”，不是正式全收益基准；产物没有排名、信号、回测或买入名单。状态固定 `diagnostic_current_universe_not_pit`、`paper_eligibility=false`、`trade_eligibility=false`、`real_money_list_allowed=false`、`live=not_supported`。

离线重放命令：

```powershell
python -m agent.current_sample_snapshot verify `
  --membership-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_membership/choice-current-csi800-20260819-v2-a1dfd62c `
  --industry-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_industry/choice-current-csi800-industry-20260819-v2-60f636c3 `
  --sample-dir data/market_data/archives/strategy_workspace/quality_growth_v1/diagnostic/current-csi800-60-sample-20260819-v2 `
  --output-dir data/market_data/archives/strategy_workspace/quality_growth_v1/diagnostic/current-csi800-60-factor-snapshot-20260818-v1
```

## 历史兼容证据

旧中证行业 `RM20` 2017—2023 诊断为负，且行业指数不可交易。它保留为反证和兼容测试，不再是策略主线，也不能通过调参复活。Factor Lab、研报审计、行业雷达、市场观察与合成执行模块继续保留，避免破坏历史证据，但不得越级进入本策略。
