# 项目交接状态

> 本文是用于跨 Thread 和外部审查的交接快照，不替代代码、版本化配置、Schema、标准 CLI 产物、manifest 或真实测试证据。策略准入状态以对应受控产物为准。

## 快照元数据

- `as_of`：2026-08-24，Asia/Shanghai
- `branch`：`codex/project-review-20260820`
- `base_commit`：`bd80ca2930564b308e2cdec07562f322bc4f9ee9`
- `review_range`：`bd80ca2..worktree`
- `worktree_state`：dirty；当前工作树包含Factor Discovery、Experiment V3诊断绑定、模型/政策/Signal V2、固定Daily publication registry、2026-08-24红队修复、专项测试与文档；三项阻断已关闭并完成本快照所列受控回归，不得混入或覆盖其他并行修改
- `change_scope`：在已提交的Adaptive Exposure V2执行与日频账本内核之上，增加LLM候选因子治理、approved registry、train-only模型训练/校准契约、Exposure/Constructor Policy V2、正式Alpha fail-closed、Next-session Signal V2与固定本地Daily publication边界；正式外部loader与Experiment V3仍阻塞

## 当前目标

在既有Adaptive Exposure V2执行与日频账本上，补齐“候选因子→独立验证→批准registry→train-only模型/校准→V2 policy→逐日固定发布→风险退出D+1”的可审计链。LLM只能提出候选，任何typed receipt、哈希或本地文件权限都不能自行证明外部来源或正式Experiment V3冻结。正式Alpha在loader缺失时必须 `DATA_FAIL_CLOSED`，但四类风险退出仍保留第一次紧邻D+1。当前阶段只完成工程契约与本地确定性验证，不运行或解释2024—2025 Locked Test，不提升任何准入。

## 本轮完成

> 本节记录当前dirty工作树已经实现并完成本快照受控回归的工程边界。实现完成、测试通过、外部来源接入、统计有效和任何准入仍是不同状态。

- 修复并冻结P0.1七项：`DATA_FAIL_CLOSED`/`MANUAL_PAUSE`禁买；Gate独立覆盖全部退出持仓及部分可卖残余；四类减仓首次相邻D+1；日亏仅阻断风险增加；Paper fill账户fingerprint CAS；canonical费用/证券规则/整手bundle；整批预检早于任何`SUBMITTING`。
- 新增Alpha、Exposure、Portfolio Constructor、Next-session Adapter与Daily Pipeline。Alpha诊断入口输出全池预测/排名/排除码且不下单，正式入口在loader阻断时全池失败；Exposure固定0/30%/60%/100%；Constructor执行Top3、40%、entry/hold、整手、完整成本与no-trade；Pipeline编排12阶段且无自动提交权限。
- 统一CST策略日，拒绝未来或陈旧session；同一CST日不能重复推进迟滞，不可达pending计数失败。正式流水线只从固定策略级registry续接上一官方日不可变Exposure inputs/decision/state，切换report目录不能重置记忆；失败日写绑定failure receipt的`IMMEDIATE_RISK_OFF`续接状态。
- `frozen-daily-data.v2`只接收四类市场Exposure指标；Alpha分布由排名派生，账户回撤由已验证Paper Ledger V2峰值、D日策略账户和受控收盘价内部派生。非空账户缺账本时生成零订单`BLOCKED`日报；无账本首次bootstrap仅允许冻结政策的空仓`initial_cash=10000`，缩水余额不能重置峰值。
- 股票池变更时，策略旧持仓必须附D日受控收盘引用和InstrumentRule，以`not_in_current_alpha_universe`进入exit-only构造；不会因离开Alpha池而失去HOLD/SELL覆盖。
- `daily-strategy-decision.v2`分离目标、可实现、当前和实际事实：D日计划的`realized_*`固定为`null`，实际仓位只在D+1收盘账本形成。日报输出BUY/SELL/HOLD/CASH、执行偏离上限、取消条件、完整成本、模型/风险/no-trade原因及数据/模型/政策/意图哈希。预期数据或验证失败写create-only failure receipt与零订单`BLOCKED`日报；不可变碰撞继续直接报警。
- Next-session消费使用固定仓库/策略级registry，以完整`signal_sha256`全局CAS；复制、改名、换目录、路径别名、并发消费及`CANCELED`后重试不能形成第二次执行。风险退出与`NO_ALPHA_CASH`不能冒充普通Alpha BUY。
- Stage 11重读中央consumption并逐项绑定冻结action、数量、整手、参考价和偏离上限；人工成交bundle以`consumption_sha256`唯一CAS。Paper Ledger V2只接受中央精确fill字节、`CanonicalExecutionCostBundleV1`、`PaperCloseExecutionEvidenceV1`和`ControlledCloseMarkBundleV1`，逐项重算佣金、印花税、过户费、滑点及成本后NAV/回撤。
- Constructor门禁使用未量化完整成本比率，展示精度不能制造交易夹缝。JSON Schema执行器补齐本轮契约使用的Draft 2020-12关键语义；日报、执行计划、组合构造和Next-session signal在跨进程Schema层同样关闭风险/失败状态BUY。
- 新增 `research/factor_discovery/` 四层治理：`FactorHypothesisV2`固定为LLM研究候选；独立Validation receipt只允许Validation分区；批准条目必须绑定typed receipt；approved registry按factor ID规范排序并拒绝重复公式/receipt、未来时点和字段矛盾。
- Alpha升级为 `frozen-alpha-model.v2`，绑定approved registry、train-only训练receipt、同目标/同预测期限的金融/非金融校准、候选模型准入receipt、runtime源码manifest及Experiment V3诊断绑定；旧V1模型、未批准因子或任一语义/时序/hash漂移均失败关闭。
- Exposure与Constructor升级为V2 policy并绑定同一admission receipt。Constructor增加严格为正的entry预测收益门、显式hold预测收益门及池外 `MANDATORY_EXIT`；不能只凭相对排名买入全截面负收益股票，也不能因股票离池而丢失退出责任。
- `ExperimentV3AdmissionReceiptV1`固定为 `diagnostic_binding_only_not_formally_admitted` 和 `formal_loader_status=blocked_not_implemented`。生产代码没有issuer token/helper；测试直接构造dataclass也只能做结构校验，formal verifier始终失败。正式Alpha因此返回全池 `DATA_FAIL_CLOSED` 且不可BUY，诊断打分固定not admitted。
- Daily Pipeline以固定本地registry作为D→D+1发布权威，authority仅有 `BLOCKED` 与 `RISK_REDUCTION_ONLY`，不存在Alpha authority。Blocked日期槽恰好含daily decision、authority receipt、canonical failure receipt、received-input commitments四项且 `next_session_allowed=false`；risk槽恰好含17项完整artifact并只允许四类无BUY风险退出。
- Publication先把每个artifact做canonical JSON roundtrip，再按 `YYYY-MM-DD/` create-only占槽并逐文件排他写入；admission/publication receipt与全部artifact完成后最后写`COMMITTED`。partial槽失败关闭且必须人工恢复，不能自动覆盖或拼接两次运行。
- Next-session创建、落盘、重载和消费均从固定publication registry重读精确canonical字节；调用方receipt、路径或hash不能替代。`RISK_OFF`、`DEFENSIVE_REDUCTION`、`NO_ALPHA_CASH`、`ACCOUNT_DRAWDOWN_EXIT`继续支持第一次紧邻D+1，`BLOCKED`与Alpha不能进入。
- **红队阻断1已关闭**：Daily publication在发布和重载时实际执行`daily_strategy_decision.v2`、`exposure_decision.v2`、`alpha_ranking.v2`正式Schema，并绑定authority、decision/data status、failure receipt、安全旗标及Alpha status/eligible count；自哈希但不合Schema或语义的artifact不能发布。
- **红队阻断2已关闭**：固定Exposure state/target必须沿同一可达条件图映射到Intent、Construction和Daily；`RISK_OFF`不能伪造0.30目标，`DEFENSIVE`不能伪造全退出，blocked decision不能套risk authority。Next-session从固定registry加载后独立重跑完整publication contract，不能只信任loader第一次校验。
- **红队阻断3已关闭**：Experiment V3诊断receipt、Daily admission/publication/loaded对象和Next-session Signal的信任边界均要求exact type，关键校验显式调用基类实现；调用方子类不能覆写`require_structural_valid()`或`to_dict()`绕过失败关闭。

## 关键变更文件

- 执行内核：[models.py](../trading/models.py)、[integrity.py](../trading/integrity.py)、[planner.py](../trading/planner.py)、[risk.py](../trading/risk.py)、[order_store.py](../trading/order_store.py)、[paper.py](../trading/paper.py)
- 信号模块：[Alpha](../research/strategy_workspace/alpha_engine_v2.py)、[Exposure](../research/strategy_workspace/exposure_engine_v2.py)、[Constructor](../research/strategy_workspace/portfolio_constructor_v2.py)、[Next-session](../research/strategy_workspace/next_session_signal.py)、[Daily Pipeline](../operations/daily_pipeline.py)
- 治理与发布模块：[Factor Discovery](../research/factor_discovery/README.md)、[治理实现](../research/factor_discovery/governance.py)、[Experiment V3 diagnostic binding](../research/strategy_workspace/experiment_v3_admission.py)、[Daily publication boundary](../research/strategy_workspace/daily_signal_publication.py)
- 账本与政策：[Paper Ledger V2](../research/strategy_workspace/paper_ledger_v2.py)、[冻结配置](../configs/strategy_adaptive_exposure.v2.json)、[政策加载器](../research/strategy_workspace/adaptive_exposure.py)
- 主要Schema：[Factor Hypothesis V2](../schemas/factor_hypothesis.v2.json)、[Approved Registry](../schemas/approved_factor_registry.v1.json)、[Frozen Alpha Model V2](../schemas/frozen_alpha_model.v2.json)、[Experiment Diagnostic Binding](../schemas/experiment_v3_admission_receipt.v1.json)、[Daily Signal Admission](../schemas/daily_signal_admission_receipt.v1.json)、[Daily Signal Publication](../schemas/daily_signal_publication_receipt.v1.json)、[Exposure Policy V2](../schemas/exposure_hysteresis_policy.v2.json)、[Constructor Policy V2](../schemas/portfolio_constructor_policy.v2.json)、[Next-session Signal V2](../schemas/next_session_signal.v2.json)、[Daily Decision V2](../schemas/daily_strategy_decision.v2.json)
- 主要测试：[Factor治理](../tests/test_factor_discovery_governance.py)、[Alpha](../tests/test_alpha_engine_v2.py)、[Exposure](../tests/test_exposure_engine_v2.py)、[Constructor](../tests/test_portfolio_constructor_v2.py)、[Next-session](../tests/test_next_session_signal.py)、[Integration](../tests/test_daily_pipeline_integration.py)、[Schema](../tests/test_schema_validation_v2.py)

## 验证证据

- Next-session专项：`Ran 27 tests`，退出码0，`OK`；包含正式Schema/条件图二次复核与exact-type子类攻击。
- 11组合并专项（Factor/Alpha/Exposure/Constructor/Next-session/Daily/P0/Paper Ledger/Schema）：`Ran 178 tests in 24.592s`，退出码0，`OK`。
- 安全全仓回归：精确排除`test_strategy_workspace_admission`、`test_strategy_workspace_evaluation`、`test_strategy_workspace_experiment`、`test_strategy_workspace_top_decile_backtest`四个Locked/Experiment模块后，`Ran 782 tests in 157.325s`，退出码0，`OK (skipped=2)`。
- 仓库59个Schema JSON全部解析成功。
- bundled Python `compileall`：退出码0。
- 文档交接测试：系统PATH中的`python`不可用；改用bundled Python执行`-m unittest tests.test_project_handoff_docs -v`，最终复跑`Ran 4 tests in 0.005s`，退出码0，`OK`。
- 本轮8份Markdown相对链接检查：`ALL_RELATIVE_LINK_TARGETS_EXIST`，退出码0。
- `git diff --check`全工作树退出码0；只有Windows LF→CRLF提示，无whitespace error。
- **not run**：上述四个Locked/Experiment模块及包含它们的无排除全仓命令；未运行、未读取、未解释任何真实2024—2025 Locked Test结果。
- **not run**：生产外部数据更新、真实官方registry、外部通知发送、券商/Paper自动提交、真实资金与LIVE。

## 已知问题与阻塞

- 外部受控Choice PIT历史成分、中证800全收益序列、PIT行业/交易状态/首披财务尚未接入；现有诊断快照不能作为正式信号输入。
- 生产官方日历、InstrumentRule/真实费率、执行行情和收盘mark registry尚未认证。typed receipt与SHA-256只证明内部内容一致，不证明官方来源真实性或完整性。
- 固定Daily publication、Next-session与Exposure registry目前都是单机文件系统CAS；ACL仅是本地writer权限边界，不是外部来源认证或多机共识。partial Daily日期槽必须人工恢复；生产多机共享CAS、受控loader、备份与恢复流程尚未实现。Stage 11没有原始D+1 account/quote payload，不能从哈希反推来源。
- Experiment V3尚未正式冻结；本轮新增的是模型/校准/policy/Signal的V2类型与diagnostic structural binding，正式外部controlled loader仍为 `blocked_not_implemented`。仓库没有经过该loader认证的正式train-only Alpha模型、Exposure迟滞policy或Constructor entry/hold/正收益/no-trade/偏离阈值artifact。
- 当前formal Alpha路径只能生成 `DATA_FAIL_CLOSED`/`BLOCKED`，因此不能用本轮代码生产普通Alpha BUY；风险通道只允许纯减仓。未来loader实现后仍须重新对Alpha authority、BUY价格偏离取消和正式 `NO_ALPHA_CASH` 做独立准入审查，不能沿用诊断结果自动解锁。
- 通知阶段只有`local-notification-outbox.v1`待投递证据，没有外部发送器或送达证明。
- `frozen-daily-data.v1`不被当前流水线消费；当前入口严格要求V2。Paper Ledger V2严格拒绝缺少成本、执行证据或typed close-mark bundle的旧草案记录；仓库未迁移或覆盖任何用户账本数据。
- latch reset、新账本生命周期与恢复入场仍没有标准治理；当前ledger生命周期触发风险锁后永久no-reentry。
- 尚无生产日跑、正式回测、统计有效性、前向Paper观察或收益结论；通过测试不能跨级解释为准入。
- 早期红队测试曾误写一条被Git忽略的假消费记录：`data/portfolio/a-share-small-account-adaptive-exposure-v2/.next-session-registry.v1/consumptions/022a50bd0d2d86c07346f432e708f93f9885f3b5f976fe7eb21b61cb6a941df0.consumed.json`，并留下同registry的空`manual-fills/`。当前测试已全部使用临时registry且复跑不再修改它；因`data/portfolio/`删除纪律和本轮未获明确删除授权，该已知生成物尚未清理，不能当作真实执行证据。

## 安全状态

- 工程状态：`v3_signal_governance_engineering_verified_formal_admission_blocked`
- 研究状态：`blocked_missing_pit_data_formal_loader_and_experiment_v3_freeze`
- Paper：`paper_eligibility=false`
- 交易：`trade_eligibility=false`
- 真实资金：`real_money_list_allowed=false`
- LIVE：永久`live_not_supported`；配置、枚举、Token、白名单或readiness均不能解锁
- 自动提交：`automatic_order_submission=false`；D+1仅生成人工复核指令

## 待决策

1. 生产受控PIT updater以及官方日历、证券规则/费率、执行行情和收盘mark registry采用何种外部适配与来源认证机制。
2. 哪个独立受控系统负责正式Experiment V3 loader，并认证approved-factor registry、train-only训练/校准、迟滞、entry/hold/正收益/no-trade、成本和价格偏离阈值。
3. 外部通知器、前向Paper运行目录和账本生命周期由哪个受控调度器持有；当前本地outbox不等于发送成功。
4. 外部交付必须显式确认具体GitHub仓库与分支；存在多个remote时禁止默认选择`origin`。

## 下一步

1. 按`bd80ca2..worktree`做最终独立只读审查，重点攻击LLM候选越权、formal receipt伪造、runtime/registry/model/policy错配、publication缺文件/重复action/超卖/partial槽、调用方receipt替换固定字节及任何Alpha BUY越权。
2. 接入并认证生产PIT、官方registry、正式模型/阈值和外部controlled loader后，才冻结Experiment V3；在此之前正式Alpha、生产日跑与Paper准入保持关闭，但已冻结的四类风险退出首次D+1语义继续作为执行内核能力保留。
3. Experiment V3正式冻结后，才讨论唯一一次2024—2025 Locked Test；任何核心参数变化形成新版本，不能用Locked结果回调。
4. 推送前核对当前HEAD、干净工作树、目标remote URL与目标分支；禁止默认选择第一个remote。

## 建议外部审查范围

按`STATUS.md -> bd80ca2..worktree -> factor_discovery/experiment_v3_admission -> daily_signal_publication -> V2 Schema -> 五模块与daily_pipeline.py -> tests -> DECISIONS.md`恢复上下文。优先复现：候选因子直接进入模型、未来验证、train-only/校准/runtime时点或语义漂移、无formal loader仍输出非失败Alpha、publication artifact自重签、同标的重复action、聚合SELL超仓/超可卖、canonical类型漂移、缺`COMMITTED`或partial槽续写、调用方receipt替换固定registry、`BLOCKED`进入D+1、风险退出被Alpha门阻断，以及任何用diagnostic receipt、Schema、哈希或本地ACL提升正式Experiment/Paper/交易/LIVE状态的表述。
