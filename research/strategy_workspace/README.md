# Strategy Workspace 代码边界

本目录同时保留默认的质量成长 V1 研究链和非默认的 Adaptive Exposure V2。模块存在、Schema 通过或专项测试通过，只表示工程契约可复核；不表示数据来源已认证、统计有效、Paper 已准入或允许交易。仓库永久不支持 LIVE。

## Adaptive Exposure V2 五模块

| 模块 | 单一职责 | 明确不做 |
|---|---|---|
| [`alpha_engine_v2.py`](alpha_engine_v2.py) | 诊断入口从typed受控PIT截面重算因子并校验approved registry、train-only冻结/校准模型、runtime manifest及Experiment V3结构绑定；正式入口要求formal loader | 不生成订单；不接受候选/未批准因子或裸自哈希模型；当前formal loader阻断，正式结果固定 `DATA_FAIL_CLOSED` 且不可BUY；诊断结果固定not admitted |
| [`exposure_engine_v2.py`](exposure_engine_v2.py) | 只用六类允许输入，消费绑定共享Experiment V3 receipt的 `exposure-hysteresis-policy.v2`，输出 `RISK_OFF=0`、`DEFENSIVE=0.30`、`NEUTRAL=0.60`、`RISK_ON=1.00`；普通变化走预注册迟滞 | 不内置或猜测生产阈值；不读取其他特征；不直接改账户 |
| [`portfolio_constructor_v2.py`](portfolio_constructor_v2.py) | 合并Alpha与目标总仓位；最多3只、单只不超过40%；V2 policy同时应用percentile、严格为正的entry预测收益门、显式hold预测收益门、池外 `MANDATORY_EXIT`、整手、完整成本和no-trade threshold | 不为凑单交易；不让相对排名替代正Alpha门；不让池外旧持仓静默HOLD；不让Alpha no-trade门阻断显式风险减仓 |
| [`next_session_signal.py`](next_session_signal.py) | 生成 `next-session-signal.v2`；创建、重载和消费均从固定Daily publication registry重读完整canonical字节，当前只接受 `RISK_REDUCTION_ONLY` 的17项bundle | 调用方receipt不能替代registry；`BLOCKED`和Alpha不能进入D+1；不自动提交订单 |
| [`operations/daily_pipeline.py`](../../operations/daily_pipeline.py) | 编排阶段1—12并每天写decision；固定registry只发布4项 `BLOCKED` 最小bundle或17项 `RISK_REDUCTION_ONLY` bundle，canonical roundtrip后create-only持久化且最后写`COMMITTED` | 不授予Alpha authority；不把本地ACL当外部认证；不抓取生产数据、不选择阈值、不外发通知、不确认成交或自动提交 |

Alpha Engine 对单股缺失PIT字段的评分契约保留完整排除行，`predicted_return`、分数、percentile和rank均为`null`。金融与非金融子模型的裸分数不能直接混排；`frozen-alpha-model.v2`要求二者先通过同一目标、同一预测期限的train-only冻结校准，再进入全池比较。模型同时绑定实验规格、approved-factor registry、训练receipt、候选模型准入receipt、校准receipt和runtime源码manifest。当前正式入口会因formal V3 admission缺失在评分前全池 `DATA_FAIL_CLOSED`；只有未来loader和Experiment V3正式冻结后，正式“无合格股票”才可输出 `NO_ALPHA_CASH`。诊断入口只能输出 `DIAGNOSTIC_ONLY_NOT_ADMITTED`，不能生成订单。Portfolio Constructor的普通Alpha成本与hold-band语义仍作为未来正式准入后的冻结契约；风险减仓不受Alpha no-trade门阻断。

Exposure Engine 的六类输入固定为：中证800全收益趋势、市场宽度、已实现波动、市场回撤、Alpha 预测分布和账户回撤。任何缺类、多类、未来/陈旧 session、失败状态或哈希漂移都不能被静默忽略；同一 CST 策略日不能重复推进迟滞，正式流水线的状态必须从固定策略级registry续接上一官方交易日不可变inputs/decision/state，换report目录不能bootstrap重置。失败日写可验证的`IMMEDIATE_RISK_OFF`续接状态。账户回撤由已验证 Paper Ledger V2 峰值与 D 日策略 NAV 内部派生，不接受 updater 自报；无账本首次bootstrap只接受冻结政策的空仓初始资金。

## Factor Discovery 与正式冻结边界

[`research/factor_discovery`](../factor_discovery/README.md) 是Alpha前置治理层。`FactorHypothesisV2` 永久为 `llm_research_candidate_only`；只有绑定 `validation_only_not_locked_test` 独立验证receipt的条目才能进入 `ApprovedFactorRegistryV1`。Alpha模型的feature集合必须与registry完整一致，候选或未批准因子不得进入正式信号。

`ExperimentV3AdmissionReceiptV1` 固定为 `diagnostic_binding_only_not_formally_admitted` 和 `formal_loader_status=blocked_not_implemented`。生产代码没有issuer token或issuer helper；测试即使直接构造dataclass也只能调用结构校验，formal verifier仍必然失败。正式Alpha因此 `DATA_FAIL_CLOSED`，但Daily不会因此丢弃风险退出：固定publication authority只允许零订单 `BLOCKED` 或无BUY的 `RISK_REDUCTION_ONLY`，后者为四类风险退出保留第一次紧邻D+1。该receipt不认证外部artifact，不表示Experiment V3已经正式冻结，也不提升Paper、交易或LIVE准入。

固定Daily publication registry按策略日create-only占槽。`BLOCKED`恰好持久化daily decision、authority receipt、failure receipt与received-input commitments四项；`RISK_REDUCTION_ONLY`恰好持久化17项完整证据。所有artifact先经canonical JSON roundtrip，文件使用排他创建，`COMMITTED`最后写入；崩溃或部分写入留下的日期槽失败关闭并要求人工恢复。Next-session只信任从该registry重读的精确字节，调用方receipt/路径/hash不能另建权威。该机制是单机本地文件系统权限边界，不是外部来源认证或多机共识。

发布合约不再把“canonical+自哈希”当成完整语义验证。写入前和固定registry重载时都会校验`daily_strategy_decision.v2`、`exposure_decision.v2`、`alpha_ranking.v2`正式Schema，并绑定authority、decision/data status、failure receipt、安全旗标、Alpha status/eligible count与Exposure固定state/target。Risk bundle还必须通过`ExposureDecision -> Intent选择 -> Construction -> Daily decision`的同一条件图；`RISK_OFF`不能改写为0.30，`DEFENSIVE`不能伪造全退出，blocked decision不能套用risk authority。Next-session加载后再次调用完整发布合约，防止替换loader返回弱校验对象。

所有可授予信任的dataclass边界拒绝子类：Experiment V3诊断receipt、Daily admission/publication/loaded对象和Next-session Signal均要求exact type，关键结构校验显式调用基类实现而非动态分派。调用方覆写`require_structural_valid()`或`to_dict()`不能把诊断receipt、发布receipt或Signal升级为权限。

## P0.1 冻结执行边界

V2 执行链已经修复并冻结以下行为：

- `DATA_FAIL_CLOSED` 与 `MANUAL_PAUSE` 在 Planner 和 Gate 均不能产生 BUY；
- Gate 独立验证完整退出计划覆盖全部现有持仓；
- `RISK_OFF`、`DEFENSIVE_REDUCTION`、`NO_ALPHA_CASH`、`ACCOUNT_DRAWDOWN_EXIT` 支持第一次相邻 D+1，后续重试需要谱系；
- 日亏损限制只阻断风险增加，不阻断纯减仓；
- Paper 成交账户更新使用 fingerprint CAS；
- 费用、InstrumentRule、tick 与整手规则绑定 canonical execution-rule bundle；
- 整批预检全部通过后，订单才可进入 `SUBMITTING`。

执行内核位于 [`trading/`](../../trading/README.md)。上述冻结不代表已接入生产官方元数据或已获得 Paper/交易准入。

## Daily Pipeline 12 阶段

1. 外部 `DailyDataUpdaterV2` 更新并冻结 `frozen-daily-data.v2` D 日数据；池外策略旧持仓必须附独立受控收盘价和规则，且只能 HOLD/SELL。
2. 校验 PIT、哈希、账户、日历 receipt 和规则 bundle；失败关闭。
3. 正式Alpha先要求formal admission；当前输出全池 `DATA_FAIL_CLOSED`，诊断入口仅生成not-admitted排名和排除原因。
4. 用V2 policy及共享admission receipt生成Exposure状态与迟滞记忆。
5. 应用正预测收益门和池外 `MANDATORY_EXIT`，构建理论目标和整手可实现组合。
6. 冻结显式 `PortfolioIntent`。
7. 写入不可变 `daily-strategy-decision.v2`；正式Alpha受阻或其他预期验证异常仍产出零订单 `BLOCKED` 日报和failure receipt，不可变碰撞直接报警。
8. 生成Markdown/JSON计划；对Daily/Exposure/Alpha正式Schema、authority/failure/safety及Exposure条件图校验后，把4项 `BLOCKED` 或17项 `RISK_REDUCTION_ONLY` bundle经canonical roundtrip写入固定create-only registry，`COMMITTED`最后写。
9. 只写本地 `local-notification-outbox.v1`；没有外部投递器。
10. 仅对固定registry中的 `RISK_REDUCTION_ONLY` 做D+1盘前单次复核；加载后独立重跑完整publication contract并拒绝子类对象，随后复核账户/报价/规则。四类风险退出支持第一次紧邻D+1，只返回无执行权限的人工指令。
11. 操作员逐项记录 FILLED/PARTIAL/UNFILLED 及证据哈希。
12. 收盘使用人工成交、canonical 成本 bundle 和 receipt-bound typed close-mark bundle 追加 Paper Ledger V2，逐项重算佣金、印花税、过户费、滑点及成本后状态。

每天都生成决策，但允许零订单。日报分离目标、可实现、当前和实际事实；D日计划的`realized_*`固定为`null`，实际仓位只由D+1成交后收盘账本形成。日报记录 BUY、SELL、HOLD、CASH、价格偏离上限、取消条件、完整成本、模型/风险/no-trade 原因以及数据、模型、政策、意图和决策哈希。

## 当前外部阻塞

- 没有接入生产级外部受控 PIT updater；当前诊断数据不能转换为正式输入。
- 没有接入生产官方日历、证券规则/费率和行情 registry；内容哈希不证明官方来源。
- Experiment V3正式外部loader仍为 `blocked_not_implemented`；生产代码没有issuer/helper，诊断receipt、测试fixture、Schema、自哈希和本地publication registry只证明内部绑定，均不是正式train-only Alpha model或政策artifact的来源/冻结证据。
- 2024—2025 Locked Test 未运行、未解释；Daily Pipeline 对该日期范围显式失败关闭。
- 通知只有本地 outbox，没有外部发送成功证据。
- `paper_eligibility=false`、`trade_eligibility=false`、`real_money_list_allowed=false`、`live_supported=false`；LIVE入口仍返回`live_not_supported`。

完整规格见 [`docs/ADAPTIVE_EXPOSURE_V2.md`](../../docs/ADAPTIVE_EXPOSURE_V2.md)，当前工程交接状态以 [`docs/STATUS.md`](../../docs/STATUS.md) 为导航，但状态文档本身不构成准入真值。
