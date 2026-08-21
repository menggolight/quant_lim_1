# 项目交接状态

> 本文是用于跨 Thread 和外部审查的交接快照，不替代代码、版本化配置、Schema、标准 CLI 产物、manifest 或真实测试证据。策略准入状态以对应受控产物为准。

## 快照元数据

- `as_of`：2026-08-21，Asia/Shanghai
- `branch`：`codex/project-review-20260820`
- `base_commit`：`bf6e2665d6bdab09f635c81ab778fd6a57abe489`
- `review_range`：`bf6e266..HEAD`
- `worktree_state`：clean；本轮56个文件已形成当前HEAD提交；远端发布状态必须以Git核验，不由本文档声明替代
- `change_scope`：Adaptive Exposure V2 P0.1执行内核、五个信号模块、跨进程Schema、Daily Pipeline、D+1人工复核、人工成交证据、Paper Ledger V2、专项测试与文档

## 当前目标

在既有Adaptive Exposure V2执行与日频账本上，完成“D日收盘决策、紧邻D+1开盘人工执行”的可审计信号生产代码契约；每天允许零订单，数据/模型/配置失败必须关闭。当前阶段只完成工程实现和每日Paper决策内核，不运行或解释2024—2025 Locked Test，不提升任何Paper、交易、真实资金或LIVE准入。

## 本轮完成

- 修复并冻结P0.1七项：`DATA_FAIL_CLOSED`/`MANUAL_PAUSE`禁买；Gate独立覆盖全部退出持仓及部分可卖残余；四类减仓首次相邻D+1；日亏仅阻断风险增加；Paper fill账户fingerprint CAS；canonical费用/证券规则/整手bundle；整批预检早于任何`SUBMITTING`。
- 新增Alpha、Exposure、Portfolio Constructor、Next-session Adapter与Daily Pipeline。Alpha输出全池预测/排名/排除码且不下单；Exposure固定0/30%/60%/100%；Constructor执行Top3、40%、entry/hold、整手、完整成本与no-trade；Pipeline编排12阶段且无自动提交权限。
- 统一CST策略日，拒绝未来或陈旧session；同一CST日不能重复推进迟滞，不可达pending计数失败。正式流水线只从固定策略级registry续接上一官方日不可变Exposure inputs/decision/state，切换report目录不能重置记忆；失败日写绑定failure receipt的`IMMEDIATE_RISK_OFF`续接状态。
- `frozen-daily-data.v2`只接收四类市场Exposure指标；Alpha分布由排名派生，账户回撤由已验证Paper Ledger V2峰值、D日策略账户和受控收盘价内部派生。非空账户缺账本时生成零订单`BLOCKED`日报；无账本首次bootstrap仅允许冻结政策的空仓`initial_cash=10000`，缩水余额不能重置峰值。
- 股票池变更时，策略旧持仓必须附D日受控收盘引用和InstrumentRule，以`not_in_current_alpha_universe`进入exit-only构造；不会因离开Alpha池而失去HOLD/SELL覆盖。
- `daily-strategy-decision.v2`分离目标、可实现、当前和实际事实：D日计划的`realized_*`固定为`null`，实际仓位只在D+1收盘账本形成。日报输出BUY/SELL/HOLD/CASH、执行偏离上限、取消条件、完整成本、模型/风险/no-trade原因及数据/模型/政策/意图哈希。预期数据或验证失败写create-only failure receipt与零订单`BLOCKED`日报；不可变碰撞继续直接报警。
- Next-session消费使用固定仓库/策略级registry，以完整`signal_sha256`全局CAS；复制、改名、换目录、路径别名、并发消费及`CANCELED`后重试不能形成第二次执行。风险退出与`NO_ALPHA_CASH`不能冒充普通Alpha BUY。
- Stage 11重读中央consumption并逐项绑定冻结action、数量、整手、参考价和偏离上限；人工成交bundle以`consumption_sha256`唯一CAS。Paper Ledger V2只接受中央精确fill字节、`CanonicalExecutionCostBundleV1`、`PaperCloseExecutionEvidenceV1`和`ControlledCloseMarkBundleV1`，逐项重算佣金、印花税、过户费、滑点及成本后NAV/回撤。
- Constructor门禁使用未量化完整成本比率，展示精度不能制造交易夹缝。JSON Schema执行器补齐本轮契约使用的Draft 2020-12关键语义；日报、执行计划、组合构造和Next-session signal在跨进程Schema层同样关闭风险/失败状态BUY。

## 关键变更文件

- 执行内核：[models.py](../trading/models.py)、[integrity.py](../trading/integrity.py)、[planner.py](../trading/planner.py)、[risk.py](../trading/risk.py)、[order_store.py](../trading/order_store.py)、[paper.py](../trading/paper.py)
- 信号模块：[Alpha](../research/strategy_workspace/alpha_engine_v2.py)、[Exposure](../research/strategy_workspace/exposure_engine_v2.py)、[Constructor](../research/strategy_workspace/portfolio_constructor_v2.py)、[Next-session](../research/strategy_workspace/next_session_signal.py)、[Daily Pipeline](../operations/daily_pipeline.py)
- 账本与政策：[Paper Ledger V2](../research/strategy_workspace/paper_ledger_v2.py)、[冻结配置](../configs/strategy_adaptive_exposure.v2.json)、[政策加载器](../research/strategy_workspace/adaptive_exposure.py)
- 主要Schema：[Execution Plan V2](../schemas/portfolio_execution_plan.v2.json)、[Frozen Daily V2](../schemas/frozen_daily_data.v2.json)、[Daily Decision V2](../schemas/daily_strategy_decision.v2.json)、[Next-session Signal](../schemas/next_session_signal.v1.json)、[Paper Ledger V2](../schemas/strategy_paper_ledger_record.v2.json)
- 主要测试：[P0.1](../tests/test_adaptive_exposure_p0.py)、[Alpha](../tests/test_alpha_engine_v2.py)、[Exposure](../tests/test_exposure_engine_v2.py)、[Constructor](../tests/test_portfolio_constructor_v2.py)、[Next-session](../tests/test_next_session_signal.py)、[Daily](../tests/test_daily_pipeline.py)、[Integration](../tests/test_daily_pipeline_integration.py)、[Ledger](../tests/test_strategy_workspace_paper_ledger_v2.py)、[Schema](../tests/test_schema_validation_v2.py)

## 验证证据

- 核心专项：P0.1、政策、Alpha、Exposure、Constructor、Next-session、Daily、端到端、Paper Ledger与Schema共`Ran 127 tests in 1.737s`，退出码0，`OK`；此前跨进程禁BUY定向回归`Ran 31 tests`与`Ran 28 tests`也均为`OK`。
- 全仓安全回归：枚举全部`tests/test_*.py`，明确排除`test_strategy_workspace_admission`、`test_strategy_workspace_evaluation`、`test_strategy_workspace_experiment`、`test_strategy_workspace_top_decile_backtest`四个会触达Locked/Experiment路径的模块；最终`Ran 731 tests in 62.897s`，退出码0，`OK (skipped=2)`。
- 编译：`<bundled-python> -m compileall -q agent research trading operations integrations tests`，退出码0。
- 文档交接测试：`tests.test_project_handoff_docs`，`Ran 4 tests`，退出码0，`OK`。
- `git diff --check`退出码0；仅Windows LF→CRLF提示，无whitespace error。
- **not run**：完整`python -m unittest discover -s tests -v`，因为它会包含上述Locked/Experiment路径；未运行、未读取、未解释任何真实2024—2025 Locked Test结果。
- **not run**：生产外部数据更新、真实官方registry、外部通知发送、券商/Paper自动提交、真实资金与LIVE。

## 已知问题与阻塞

- 外部受控Choice PIT历史成分、中证800全收益序列、PIT行业/交易状态/首披财务尚未接入；现有诊断快照不能作为正式信号输入。
- 生产官方日历、InstrumentRule/真实费率、执行行情和收盘mark registry尚未认证。typed receipt与SHA-256只证明内部内容一致，不证明官方来源真实性或完整性。
- 固定Next-session/Exposure registry目前是单机文件系统CAS，ACL是writer信任边界；Stage 11没有原始D+1 account/quote payload，不能从哈希反推来源。生产多机共享CAS、receipt loader、备份与恢复流程尚未实现。
- Experiment V3尚未正式冻结；仓库没有正式train-only Alpha模型、Exposure迟滞policy或Constructor entry/hold/no-trade/偏离阈值artifact。
- 通知阶段只有`local-notification-outbox.v1`待投递证据，没有外部发送器或送达证明。
- `frozen-daily-data.v1`不被当前流水线消费；当前入口严格要求V2。Paper Ledger V2严格拒绝缺少成本、执行证据或typed close-mark bundle的旧草案记录；仓库未迁移或覆盖任何用户账本数据。
- latch reset、新账本生命周期与恢复入场仍没有标准治理；当前ledger生命周期触发风险锁后永久no-reentry。
- 尚无生产日跑、正式回测、统计有效性、前向Paper观察或收益结论；通过测试不能跨级解释为准入。
- 早期红队测试曾误写一条被Git忽略的假消费记录：`data/portfolio/a-share-small-account-adaptive-exposure-v2/.next-session-registry.v1/consumptions/022a50bd0d2d86c07346f432e708f93f9885f3b5f976fe7eb21b61cb6a941df0.consumed.json`，并留下同registry的空`manual-fills/`。当前测试已全部使用临时registry且复跑不再修改它；因`data/portfolio/`删除纪律和本轮未获明确删除授权，该已知生成物尚未清理，不能当作真实执行证据。

## 安全状态

- 工程状态：`signal_runtime_implemented_not_admitted`
- 研究状态：`blocked_missing_pit_data_and_experiment_v3`
- Paper：`paper_eligibility=false`
- 交易：`trade_eligibility=false`
- 真实资金：`real_money_list_allowed=false`
- LIVE：永久`live_not_supported`；配置、枚举、Token、白名单或readiness均不能解锁
- 自动提交：`automatic_order_submission=false`；D+1仅生成人工复核指令

## 待决策

1. 生产受控PIT updater以及官方日历、证券规则/费率、执行行情和收盘mark registry采用何种外部适配与来源认证机制。
2. Experiment V3的正式train-only模型、迟滞、entry/hold/no-trade、成本和价格偏离阈值何时预注册冻结。
3. 外部通知器、前向Paper运行目录和账本生命周期由哪个受控调度器持有；当前本地outbox不等于发送成功。
4. 外部交付必须显式确认具体GitHub仓库与分支；存在多个remote时禁止默认选择`origin`。

## 下一步

1. 先按本快照与`bf6e266..worktree`做独立只读审查，重点攻击来源伪造、未来/陈旧时点、复制signal二次消费、状态记忆伪造、换池残仓、成本/mark漂移和failure日报。
2. 接入并认证生产PIT、官方registry、正式模型与预注册阈值后，冻结Experiment V3；在此之前继续保持每日正式运行与Paper准入关闭。
3. Experiment V3正式冻结后，才讨论唯一一次2024—2025 Locked Test；任何核心参数变化形成新版本，不能用Locked结果回调。
4. 推送前核对当前HEAD、干净工作树、目标remote URL与目标分支；禁止默认选择第一个remote。

## 建议外部审查范围

按`STATUS.md -> bf6e266..HEAD -> config/Schema -> 五模块与daily_pipeline.py -> trading P0.1 -> Paper Ledger V2 -> tests -> DECISIONS.md`恢复上下文。优先复现：DATA_FAIL/MANUAL_PAUSE BUY、退出遗漏/部分可卖残余、首次D+1、日亏纯减仓、账户CAS、提交前预检、+14时区、同日迟滞、伪造pending、陈旧指标、账本回撤被自报覆盖、换池旧持仓、signal复制/改名/取消后再消费、BUY价格偏离、规则漂移取消、任意mark hash、费用分量缺失、异常日无日报，以及任何越级准入表述。
