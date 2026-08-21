# Schema 目录

`schemas/` 定义跨进程、跨模块或跨运行环境交换的结构契约。Schema 校验通过只证明形状符合要求，不证明来源真实、批次完整、时点正确或已获研究/交易准入。

## 市场数据 V2

- `market_data_batch.v1.json`：统一批次信封，绑定 Provider、真实上游、请求指纹、可用时间、raw/normalized SHA-256、记录数、准入状态和问题列表。
- `daily_bar.v1.json`：沪深市场日线规范记录；价格和数量使用十进制字符串，复权状态显式记录。
- `trade_calendar.v1.json`：Provider 交易日历记录；工作日近似不能冒充正式日历。
- `security_master.v1.json`：沪深市场证券基础信息记录；北交所未接入，当前快照不能自证历史 point-in-time 股票池。

JSON Schema 负责结构，`research/market_data/validation.py` 在无网络依赖下执行本仓库所需的 Draft 2020-12 约束，包括内部/同目录外部 `$ref`、组合/条件分支、对象与数组上限、唯一性和 RFC3339 日期时间；同时负责请求证券一致性、日期唯一和升序、窗口范围、OHLC、非负成交量/成交额、缺字段和非法数字。`admission.py` 再按数据集重新计算本地准入。

## 其他契约

- `strategy_adaptive_exposure_policy.v2.json`：`a-share-small-account-adaptive-exposure-v2` 的冻结政策形状；锁定0%—100%离散仓位、Top3/单只40%、显式现金意图、P0.1执行语义、风险退出重试和“月净收益10%仅报告”，固定Paper/交易/真实资金为不准入且LIVE永久不支持。V1只保留历史兼容；Schema通过不证明策略有效或任何准入。
- `portfolio_intent.v1.json`：V2研究决策到计划层的显式组合意图，绑定时点和四类输入哈希；普通空目标失败，只有 `NO_ALPHA_CASH`、`RISK_OFF` 与 `ACCOUNT_DRAWDOWN_EXIT` 可以用空权重表达0%目标。权重求和、哈希和时序仍须由领域校验重算。
- `portfolio_execution_plan.v2.json`：意图、账户快照和逐日执行尝试的计划/对账信封；除报价与受控日历外，新增 `execution_rule_bundle_sha256`，绑定完整 `FeeSchedule`、所有相关 `InstrumentRule` 与整手政策。Planner、Gate、Approval 和 Paper Broker 都必须重算；所有现金、减仓、数据失败和暂停意图在Schema层也只能含SELL类订单。V1仅用于历史兼容。相邻性与哈希仍只证明传入内容一致，不证明官方来源。
- `controlled_pit_decision_snapshot.v1.json`、`frozen_alpha_model.v1.json`、`alpha_ranking.v2.json`：分别约束 D 日 PIT 输入、train-only 冻结线性模型和全股票池排名。排除行必须保留并使用 `null` 分数，不能以0填补缺失；未来数据可令整个批次 `DATA_FAIL_CLOSED`。
- `exposure_input_snapshot.v1.json`、`exposure_hysteresis_policy.v1.json`、`exposure_state_memory.v1.json`、`exposure_decision.v2.json`：约束恰好六类Exposure输入、预注册迟滞规则、跨日状态记忆和0/30%/60%/100%决策。引擎没有生产默认阈值。
- `portfolio_constructor_policy.v1.json`、`portfolio_construction_result.v2.json`：约束entry/hold/no-trade/成本/偏离阈值和目标/可实现/当前分离的组合结果；最多3只、单只40%，允许候选不足、整手不可负担和零订单留现金，非Alpha意图在Schema层不得含BUY。
- `official_calendar_receipt.v1.json`、`official_calendar_registry.v1.json`：约束结构化日历payload、自哈希和exact-receipt受控allowlist。bool、来源字符串或哈希本身不能证明官方来源。
- `next_session_signal.v1.json`、`next_session_consumption.v1.json`：约束D日冻结到紧邻D+1的一次性人工信号和单次盘前消费；Alpha/Risk通道与嵌套intent/construction在Schema层分离，风险通道不得承载Alpha BUY；绑定数据、模型、政策、意图、日历registry、账户预期状态、报价与execution-rule bundle，不授予自动提交权限。
- `frozen_daily_data.v2.json`、`daily_strategy_decision.v2.json`、`daily_pipeline_failure_receipt.v1.json`、`manual_fill_bundle.v1.json`：约束Daily Pipeline的数据envelope、标准日报、失败receipt和人工成交证据。Frozen V2 只接收四类市场 Exposure 指标，账户回撤和 Alpha 分布由流水线派生，并为池外旧持仓提供 exit-only 受控收盘引用；日报支持带 failure receipt 的零订单 `BLOCKED` 分支，`DATA_FAIL_CLOSED`/`MANUAL_PAUSE`在Schema层禁BUY，并分离目标/可实现/当前仓位；计划阶段 `realized_*` 固定为null，实际仓位只在D+1收盘账本形成。通知outbox只是本地待投递证据。`frozen_daily_data.v1.json` 保留为未启用的旧 envelope，不被当前流水线消费。
- `strategy_paper_ledger_record.v2.json`：自适应仓位V2独立日频Paper账本的 `header` / `daily_session` 契约；绑定政策与交易日历哈希、canonical FeeSchedule/InstrumentRule 成本 bundle、signal→consumption→manual fill 执行证据和 receipt-bound typed close marks，从前态、真实成交和收盘估值逐项重算现金、佣金、印花税、过户费、滑点、NAV、回撤与仓位。它是对账证据，不是Paper准入或收益证据。
- `strategy_quality_growth_policy.v1.json`：A股小资金质量成长V1的策略政策形状；运行时还会精确校验因子、成本、风险和准入常量，Schema通过不能自行解锁研究。
- `strategy_experiment.v2.json`：append-only正式实验预注册，绑定动态PIT成分、Choice全收益基准、D+1开盘到D+21开盘的20区间标签与锚点、六因子/五控制、带截距的固定Ridge、100万元Top Decile研究本金、基础/压力成本、美的外部持仓、11项历史门及数据/代码/配置哈希。统计契约还冻结 Andrews 自动HAC滞后、最少2个可用时段、Holm `alpha=0.05`、验证/锁定测试/审计Rank IC、锁定测试+审计因子显著性以及金融2因子/非金融6因子子模型。
- `choice_quality_growth_gate.v1.json`：完整Choice单源能力receipt，绑定覆盖区间、行数、完整枚举的 `subject_ids`、字段、内容哈希、中证800全收益 open/close 和 `single_quarter`/`consolidated`/`CNY` 财务口径；聚合数量不能代替主体明细，能力契约通过也不等于实时连通、正式真值、正式回测或Paper准入。
- `choice_quality_growth_batch.v1.json`：固定Choice中证800历史采集manifest；绑定内部20交易日网格、exact 800成分、qfq/none双口径、分批执行日资格快照、raw/normalized重放与checkpoint。固定 `source_authenticated=false`、日历/PIT/行业阻塞，不能解锁Paper或交易。
- `strategy_current_membership_receipt.v1.json`：已知 V1 当前成分诊断receipt的只读兼容契约；不再签发新产物。
- `strategy_current_membership_receipt.v2.json`：Choice终端两列中证800工作簿的当前成分诊断receipt；绑定原始文件、固定模板、800个唯一代码、Schema与生成代码bundle，固定 `source_authenticated=false`、`membership_effective_date=null`。
- `strategy_current_industry_receipt.v1.json`：已知 V1 当前行业诊断receipt的只读兼容契约；不再签发新产物。
- `strategy_current_industry_receipt.v2.json`：绑定已验证 membership V2 artifact的16列Choice当前快照；锁定800只完整映射、exact 11个中证2021一级行业、市场快照日期、信息截止日以及 artifact/payload/content/code-bundle 哈希，安全状态不可提升。
- 调用方自行构造的旧 `strategy_current_universe_input.v1` 已被拒绝且不再保留 Schema；降级诊断必须通过 `agent.current_industry_import freeze-sample` 重放受控 membership 与 industry import 目录，不能用自报 receipt/hash 替代。
- `strategy_current_universe_diagnostic.v1.json`：旧单文件60只样本的只读兼容契约；不再签发新产物。
- `strategy_current_universe_diagnostic.v2.json`：降级路径的 `sample.json + manifest.json` 双文件契约；从两个源artifact重建恰好60只的行业等覆盖轮转样本，明示非中证800比例代表样本，固定 `diagnostic_current_universe_not_pit`、`Paper=false`。
- `index_level.v1.json`：`.CSI` 指数规范 ID、交易日、close、可选 OHLC、币种、指数口径、`available_at` 和来源记录 ID；不能伪装成股票日线。
- `csi_industry_universe.v1.json`：两代中证行业系列、恰好 11 行业、语义映射、共同基准、发布/有效时间与官方文档哈希。
- `cn_equity_session.v1.json`：自然日开闭市状态、开闭市时间、公告来源和内容哈希。
- `factor_hypothesis.v1.json`：冻结候选族、公式、窗口、标签、门槛、失效条件和内容哈希。
- `subjective_thesis.v1.json`：主观方向、理由、期限和反证条件；只能追加新版本，不能覆盖旧版本。
- `stock_diagnostic_observation.v1.json`：有限个股候选的前瞻诊断病例卡；冻结原始候选、价格门、起点前复核、60 交易日标签和安全状态。它不是因子准入或交易信号。

- `broker_report_extractor_review.v1.json`：90 份本地 HTML 导出的逐字段人工审核；浏览器导出不能绕过 CLI 的 PDF、版本、population 与完整性复核。
- `official_truth_receipt.v1.json`：未来 source-owned 官方真值 transport 的 receipt 形状；当前 `admission_status=not_configured`，普通 URL、哈希、本地文件或布尔值不能自签。
- `choice_truth_candidate.v1.json`：Choice 聚合数据的隔离候选形状，固定 `diagnostic_choice_secondary_not_admitted`，不能转换为正式 `TruthObservation`。
- `market_observation.v0.1.json`：宏观—行业—个股三层诊断观察；`overall.trade_action` 必须为 `null`。历史密封文件不因 V2 被重写。
- `htsc_mquant_shadow.v1.json`：华泰 MQuant 只读 Shadow 快照；内容哈希不是券商来源认证。

## 修改规则

- 新增兼容字段可以保留版本；删除、改名、改变类型或语义必须升级主版本。
- Provider 响应必须先规范化，再接受 Schema 和领域校验；不能把 SDK 对象直接交给研究消费者。
- `market_data_batch` 中的 dataset 与 Schema 版本必须一致，正式链固定要求 `synthetic=false`。
- 新版本必须提供迁移、双读窗口或明确拒绝旧版本，并补充正常、负向、边界和旧版本测试。
- 新增的自适应仓位契约与 V1 并存；五模块、Daily Pipeline 和日频账本不能被解释为已替换V1、已接入外部受控PIT/官方registry/正式模型阈值，或已获得Paper执行准入。
- 文件 SHA-256 只证明内容一致性，不能证明来自券商、交易所、监管机构或其他官方来源。
