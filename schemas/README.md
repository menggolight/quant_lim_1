# Schema 目录

`schemas/` 定义跨进程、跨模块或跨运行环境交换的结构契约。Schema 校验通过只证明形状符合要求，不证明来源真实、批次完整、时点正确或已获研究/交易准入。

## 市场数据 V2

- `market_data_batch.v1.json`：统一批次信封，绑定 Provider、真实上游、请求指纹、可用时间、raw/normalized SHA-256、记录数、准入状态和问题列表。
- `daily_bar.v1.json`：沪深市场日线规范记录；价格和数量使用十进制字符串，复权状态显式记录。
- `trade_calendar.v1.json`：Provider 交易日历记录；工作日近似不能冒充正式日历。
- `security_master.v1.json`：沪深市场证券基础信息记录；北交所未接入，当前快照不能自证历史 point-in-time 股票池。
- `provider_access_policy.v1.json`：Provider访问许可的版本化失败关闭策略；当前Choice固定`expired`且禁止网络、诊断session和新的正式离线研究消费，Tushare扩展固定为capability-probe-only。它不保存秘密，也不能授予正式Provider或LIVE权限。
- `tushare_endpoint_result.v1.json`、`tushare_capability_receipt.v1.json`：约束固定白名单endpoint的小样本能力结果、raw哈希/manifest绑定和整次探针receipt；安全字段固定为`capability_probe_only_not_admitted`及全false，Schema、receipt哈希或接口成功均不能形成MarketDataBatch、Experiment V3、Paper或交易准入。
- `tushare_single_endpoint_diagnostic_receipt.v1.json`：约束同一endpoint的SDK/HTTP双通道诊断；每通道最多一次，安全持久化transport/HTTP/upstream code/固定异常类型/消息类别，结论仅限四类根因且不产生任何准入。
- `tushare_single_endpoint_diagnostic_postmortem.v1.json`：保留首次未绑定failure marker的历史unsealed形状；当前sealed verifier明确拒绝V1，不能把旧文件当作完整封存证据。
- `tushare_single_endpoint_diagnostic_postmortem.v2.json`：保留首次marker-bound sealed形状；因仍用封存时当前配置回填runtime参数，已由V3替代，不再由当前verifier签发。
- `tushare_single_endpoint_diagnostic_postmortem.v3.json`：完整内嵌并哈希绑定create-only budget slot与round-failure marker，三方复核run ID、endpoint、失败代码bundle、异常类别和时间顺序；实际请求数、runtime语义参数与两通道字段均明确为`null + unavailable`，并只在固定round根确认completed receipt不存在。结论只表示`capability_probe_bug`，不能判断Tushare能力或授权重跑。
- `tushare_http_diagnostic_event.v1.json`：新授权HTTP-only轮次的create-only哈希链事件；固定`trade_cal`、`channel=http`、`max_requests=1`和SDK不运行，只允许`RUN_CREATED -> REQUEST_RESERVED -> NETWORK_CALL_STARTED -> [RESPONSE_RECEIVED] -> TERMINAL`及其提前终态，不保存凭证、原始响应、上游消息或异常文本。
- `tushare_http_terminal_diagnostic_receipt.v1.json`：从上述已持久化事件链重建的终态receipt，拆分六类请求计数并固定`terminal_result_count=1`、`remote_execution_unknown=started-response`、`budget_consumed=reserved`；它只证明本地诊断链完整性，不作Tushare能力判断或任何准入。

JSON Schema 负责结构，`research/market_data/validation.py` 在无网络依赖下执行本仓库所需的 Draft 2020-12 约束，包括内部/同目录外部 `$ref`、组合/条件分支、对象与数组上限、唯一性和 RFC3339 日期时间；同时负责请求证券一致性、日期唯一和升序、窗口范围、OHLC、非负成交量/成交额、缺字段和非法数字。`admission.py` 再按数据集重新计算本地准入。

## 其他契约

- `technical_alpha_feasibility_experiment.v1.json`：冻结P1的7接口、绝对日期截止、实际ranker/Exposure源码哈希、PIT规则、总回报、D+1开盘时序、小数仓位、成本与GO/NO_GO门。
- `pit_membership_coverage_report.v1.json`、`pit_membership_manifest.v1.json`：分别约束73次逐月请求检查和合法截面/成员并集；非800截面必须携带受控指数公司调整说明，缺月或哈希/权重失败不得进入Alpha。
- `tushare_alpha_feasibility_manifest.v1.json`：汇总7接口真实请求计数、各数据集覆盖/哈希和固定安全状态；它只为P1数据门服务。
- `technical_alpha_feasibility_report.v1.json`：只允许Development/Validation的base/stress 16项指标及三种终态；数值指标用canonical decimal string避免门槛舍入，并绑定plan、PIT/history manifest、实验、引擎和Gate源码哈希；`BLOCKED_DATA`时指标必须为`null`，Locked Test和全部执行权限固定关闭。
- `technical_momentum_experiment.v1.json`：当前唯一正式研究主线 `a-share-technical-momentum-adaptive-v1` 的独立冻结规格；哈希绑定既有 Technical Alpha/Exposure 真源，锁定九类数据、双价格、最多3只/40%、基础与压力成本，以及 Development/Validation/Locked 日期。Locked 固定 `NOT_RUN`、`consumed=false`，安全权限全关闭。
- `technical_formal_dataset_manifest.v1.json`：九类 Technical 正式数据的 coverage/完整性 manifest；逐数据集记录来源接口、行数、日期、缺口、内容哈希和问题，并把 PIT、adjustment、双价格、执行状态及公司行动权益分解作为关键门。Schema通过不代表数据完整或来源官方。
- `technical_momentum_backtest_report.v1.json`：只允许 Development 与 Validation 的基础/压力双情景报告；数据门失败时指标必须为 `null` 且状态为 `NOT_RUN_BLOCKED`，不得用单测或合成结果补值。
- `technical_locked_test_readiness.v1.json`：只汇总正式数据 manifest 与 Development/Validation 报告的 readiness；不允许包含 2024—2025 factor、ranking、signal、trade、NAV 或 return，Locked 状态固定未运行/未消费。
- `strategy_adaptive_exposure_policy.v2.json`：`a-share-small-account-adaptive-exposure-v2` 的冻结政策形状；锁定0%—100%离散仓位、Top3/单只40%、显式现金意图、P0.1执行语义、风险退出重试和“月净收益10%仅报告”，固定Paper/交易/真实资金为不准入且LIVE永久不支持。V1只保留历史兼容；Schema通过不证明策略有效或任何准入。
- `portfolio_intent.v1.json`：V2研究决策到计划层的显式组合意图，绑定时点和四类输入哈希；普通空目标失败，只有 `NO_ALPHA_CASH`、`RISK_OFF` 与 `ACCOUNT_DRAWDOWN_EXIT` 可以用空权重表达0%目标。权重求和、哈希和时序仍须由领域校验重算。
- `portfolio_execution_plan.v2.json`：意图、账户快照和逐日执行尝试的计划/对账信封；除报价与受控日历外，新增 `execution_rule_bundle_sha256`，绑定完整 `FeeSchedule`、所有相关 `InstrumentRule` 与整手政策。Planner、Gate、Approval 和 Paper Broker 都必须重算；所有现金、减仓、数据失败和暂停意图在Schema层也只能含SELL类订单。V1仅用于历史兼容。相邻性与哈希仍只证明传入内容一致，不证明官方来源。
- `factor_hypothesis.v2.json`、`factor_validation_receipt.v1.json`、`approved_factor_registry.v1.json`：依次约束LLM候选、只使用Validation分区的独立验证receipt，以及可由Alpha消费的确定性批准registry。候选固定 `llm_research_candidate_only`，不能自报验证或直接成为批准因子；registry拒绝重复ID/公式/receipt与未来时点。`factor_hypothesis.v1.json`仅保留历史研究形状，不能进入该批准链。
- `experiment_v3_admission_receipt.v1.json`：把实验规格、approved registry、train-only模型训练/准入/校准及Exposure/Constructor policy source绑定到同一诊断证据图；状态固定 `diagnostic_binding_only_not_formally_admitted`、`formal_loader_status=blocked_not_implemented`，并固定关闭Paper、交易、真实资金和LIVE。生产代码没有issuer token/helper；runtime还要求exact dataclass type并直接调用基类结构校验，子类覆写不能升级权限。正式Alpha因此 `DATA_FAIL_CLOSED`，receipt与SHA-256均不是外部来源认证。
- `controlled_pit_decision_snapshot.v1.json`、`frozen_alpha_model.v2.json`、`alpha_ranking.v2.json`：分别约束 D 日 PIT 输入、Experiment V3绑定的train-only冻结/校准/准入模型和全股票池排名。模型v2内嵌同一目标与期限的金融/非金融校准、训练receipt和候选模型准入receipt，并绑定approved-factor registry及共享V3准入receipt；`frozen_alpha_model.v1.json`仅保留历史形状，当前Alpha运行时明确拒绝。排除行必须保留并使用 `null` 分数，不能以0填补缺失；未来数据或准入证据不完整可令整个批次 `DATA_FAIL_CLOSED`。
- `exposure_input_snapshot.v1.json`、`exposure_hysteresis_policy.v2.json`、`exposure_state_memory.v1.json`、`exposure_decision.v2.json`：约束恰好六类Exposure输入、绑定共享V3 receipt的预注册迟滞规则、跨日状态记忆和0/30%/60%/100%决策。V2 policy没有生产默认阈值；旧`exposure_hysteresis_policy.v1.json`仅保留历史结构，当前runtime拒绝。
- `portfolio_constructor_policy.v2.json`、`portfolio_construction_result.v2.json`：约束percentile entry/hold、严格为正的entry预测收益门、显式hold预测收益门、`MANDATORY_EXIT`池外退出、no-trade/成本/偏离阈值和目标/可实现/当前分离；最多3只、单只40%，允许候选不足、整手不可负担和零订单留现金，非Alpha意图在Schema层不得含BUY。旧`portfolio_constructor_policy.v1.json`仅保留历史结构，当前runtime拒绝。
- `official_calendar_receipt.v1.json`、`official_calendar_registry.v1.json`：约束结构化日历payload、自哈希和exact-receipt受控allowlist。bool、来源字符串或哈希本身不能证明官方来源。
- `daily_signal_admission_receipt.v1.json`、`daily_signal_publication_receipt.v1.json`：约束固定本地Daily publication registry的authority和完整文件集合。authority仅允许 `BLOCKED` 与 `RISK_REDUCTION_ONLY`，没有Alpha状态；前者恰好绑定4项最小artifact且 `next_session_allowed=false`，后者恰好绑定17项完整artifact且只允许无BUY风险退出。runtime在发布与重载时还执行Daily/Exposure/Alpha各自正式Schema，并复核authority/status/failure/safety、固定Exposure state/target和Intent/Construction/Daily条件图；admission/publication/loaded对象必须是exact type。每个 `YYYY-MM-DD/` 日期槽create-only，最后写`COMMITTED`；部分槽失败关闭并人工恢复。Schema和本地ACL不构成外部认证。
- `next_session_signal.v2.json`、`next_session_consumption.v1.json`：约束D日冻结到紧邻D+1的一次性人工信号和单次盘前消费；创建、落盘、重载和消费必须从固定Daily publication registry重读精确canonical字节，调用方receipt不能替代registry。Next加载后独立重跑完整Daily publication contract，并对Experiment/Daily publication/Next Signal使用exact-type门禁，防止子类覆写。当前只接受17项 `RISK_REDUCTION_ONLY` bundle，四类风险退出支持第一次紧邻D+1；`BLOCKED`和Alpha/BUY均不能进入，也不授予自动提交权限。`next_session_signal.v1.json`仅保留历史结构，当前runtime显式拒绝。
- `frozen_daily_data.v2.json`、`daily_strategy_decision.v2.json`、`daily_pipeline_failure_receipt.v1.json`、`manual_fill_bundle.v1.json`：约束Daily Pipeline的数据envelope、标准日报、canonical失败receipt和人工成交证据。Frozen V2只接收四类市场Exposure指标，账户回撤和Alpha分布由流水线派生，并为池外旧持仓提供exit-only受控收盘引用；日报支持零订单 `BLOCKED` 分支，`DATA_FAIL_CLOSED`/`MANUAL_PAUSE`禁BUY并分离四种仓位事实。Daily decision正式Schema现在由publication runtime实际执行，且其status/data-status/failure/safety必须与authority receipt及failure receipt一致；普通sidecar、typed对象或hash不能替代该验证。通知outbox只是本地待投递证据。`frozen_daily_data.v1.json`不被当前流水线消费。
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
- `factor_hypothesis.v1.json`：旧Factor Lab候选族的历史冻结形状；不含V2的LLM候选状态、独立validation receipt或approved registry语义，不能进入Adaptive Exposure V2正式信号链。
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
