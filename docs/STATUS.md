# 项目交接状态

> 本文是用于跨 Thread 和外部审查的交接快照，不替代代码、版本化配置、Schema、标准 CLI 产物、manifest 或真实测试证据。策略准入状态以对应受控产物为准。

## 快照元数据

- `as_of`：2026-08-20，Asia/Shanghai
- `branch`：`codex/project-review-20260820`
- `base_commit`：`0dce92130b1c5a11cfc84acca89db07e5b4c36fd`
- `review_range`：`0dce921..HEAD`
- `worktree_state`：`clean`；本轮 29 个文件级变更已纳入本地 `HEAD`（`feat: add adaptive exposure v2 p0`）
- `remotes`：`origin=https://github.com/menggolight/quant_lim.git`；`review=https://github.com/menggolight/quant_lim_1.git`
- `remote_visibility`：本轮 Adaptive Exposure V2 P0 已本地 commit、尚未 push；GitHub 网页端仍只能看到 `0dce921` 及此前历史

## 当前目标

完成独立策略 `a-share-small-account-adaptive-exposure-v2` 的 P0 契约、安全执行内核和日频 Paper 对账切片，同时保持质量成长 V1 默认入口和历史行为不变。P0 只要求内部契约可验证、关键失败模式失败关闭；不实现 Alpha/仓位模型，不运行 Locked Test，也不提升 Paper、交易或真实资金准入。

## 本轮完成

- 新增 V2 版本化政策、Policy/PortfolioIntent/ExecutionPlan/PaperLedger Schema 和不可变加载器；冻结政策 SHA-256 为 `bbfb4bf60c92129ca56d0e5473d1ab433ae379ad145444a336f96ef88bc195c1`。
- 固定月净收益 10% 仅为 `reporting_only`；不作为保证、模型损失、调参目标或准入门。
- 实现 0%—100% 目标仓位、最多 3 只、单只不超过 40%、不加杠杆/不做空、候选不足保留现金，以及 `target/feasible/realized` 三类仓位分离。
- 新增显式 `PortfolioIntent`、订单风险方向和 `intent_id/attempt_id`；普通空目标拒绝，仅明确现金/退出意图允许空权重。
- V2 Gate 会重算 intent/order/plan 绑定、规范化执行报价包、内部日历 payload、费用后仓位、停牌/买卖封锁/价差和账户投影；风险减仓豁免普通换手，但不豁免物理与完整性门禁。
- D 日收盘触发 12% 回撤后，按内部受控日历的下一相邻 session 开始退出并逐 session 重试；latch 在当前 ledger 生命周期内永久 no-reentry，平仓只结束 `exit_pending`。
- 新增独立 Paper Ledger V2，区分早盘 `execution_intent` 与收盘 `closing_intent`，按真实成交和收盘估值重算现金、费用、持仓、NAV、峰值、回撤和三类仓位。
- SQLite 订单表新增显式迁移；同 intent/attempt 跨重启幂等，账户对账使用 fingerprint CAS 防止并发覆盖。V1 client order ID、V1 backtest 和 V1 Paper Ledger 保持兼容。
- 更新架构、策略、交易、Schema、测试和决策文档；没有修改正式准入产物或任何 LIVE 边界。

## 关键变更文件

- 政策与规格：[V2 policy](../configs/strategy_adaptive_exposure.v2.json)、[V2 规格](ADAPTIVE_EXPOSURE_V2.md)、[决策记录](DECISIONS.md)
- Schema：[Policy](../schemas/strategy_adaptive_exposure_policy.v1.json)、[PortfolioIntent](../schemas/portfolio_intent.v1.json)、[ExecutionPlan](../schemas/portfolio_execution_plan.v1.json)、[PaperLedger V2](../schemas/strategy_paper_ledger_record.v2.json)
- 策略运行时：[adaptive_exposure.py](../research/strategy_workspace/adaptive_exposure.py)、[paper_ledger_v2.py](../research/strategy_workspace/paper_ledger_v2.py)
- 交易内核：[models.py](../trading/models.py)、[integrity.py](../trading/integrity.py)、[planner.py](../trading/planner.py)、[risk.py](../trading/risk.py)、[order_store.py](../trading/order_store.py)、[paper.py](../trading/paper.py)、[strategy_bridge.py](../trading/strategy_bridge.py)
- 对抗测试：[P0 execution](../tests/test_adaptive_exposure_p0.py)、[policy](../tests/test_strategy_adaptive_exposure_policy.py)、[PaperLedger V2](../tests/test_strategy_workspace_paper_ledger_v2.py)

## 验证证据

- P0/V1 组合回归：`<bundled-python> -m unittest tests.test_adaptive_exposure_p0 tests.test_strategy_adaptive_exposure_policy tests.test_strategy_workspace_paper_ledger_v2 tests.test_strategy_workspace_paper_ledger tests.test_strategy_workspace_a_share_backtest tests.test_strategy_bridge tests.test_small_account_trading tests.test_execution_boundary tests.test_trading_order_store -v`，退出码 `0`，`Ran 101 tests in 1.001s`，`OK`。
- 最终完整回归：`<bundled-python> -m unittest discover -s tests -v`，退出码 `0`，`Ran 675 tests in 107.102s`，`OK (skipped=2)`。
- 编译检查：`<bundled-python> -m compileall -q agent research trading integrations tests`，退出码 `0`。
- 新增 1 个配置与 4 个 Schema 均由 PowerShell `ConvertFrom-Json` 解析，退出码 `0`。
- 交接结构测试：`<bundled-python> -m unittest tests.test_project_handoff_docs -v`，退出码 `0`，`Ran 4 tests in 0.007s`，`OK`。
- 根 README、受影响目录 README 与 `docs/` 共 21 个 Markdown 文件的本地相对链接检查，退出码 `0`。
- 高置信秘密模式与本轮曾出现的 GitHub OAuth device code 仓库扫描，退出码 `0`，未命中。
- `git diff --check`：退出码 `0`；仅有既有 LF/CRLF 工作区提示，无 whitespace error。
- 独立只读对抗复审：P0 PASS；日历/报价篡改、费用后 40%、残余集中退出、SQLite 迁移、时区、永久 latch 和 V1 兼容均通过。复审同时确认下述两项 P1 尚未关闭。
- 未配置独立 lint 命令，因此不声明 `lint passed`。

## 已知问题与阻塞

- **P1—费率配置未绑定 approval**：Gate 当前使用订单中的 `estimated_fee`；真实 `PaperBroker` 会用注入的 `FeeSchedule` 重算并在成交前拒绝不一致费用，账户不变且无 fill，但会产生错误 approval 并可能遗留 `SUBMITTING` 状态。正式 Paper 前必须绑定 canonical fee/rule bundle 并实现异常状态恢复。
- **P1—日历来源与完整性未闭环**：Planner/Gate 能验证 payload 哈希及 payload 内严格相邻，但截断后的自洽日历仍可能遗漏真实交易日；`official_trading_calendar_proven=false`。正式 Paper 前必须接入受控官方日历 registry。
- 执行报价包哈希能绑定规范化内容，但没有受控行情 registry，不能证明来源认证或 PIT。
- 普通 `ALPHA_REBALANCE` 当前只允许同 session；D 日收盘 Alpha 到下一受控开盘的一次性有效期、受控 signal registry 和标准编排尚未实现。
- latch reset、自动新建下一 ledger 生命周期和恢复入场的标准编排尚未实现；当前策略周期触发后永久 no-reentry。
- V2 仍没有 Alpha engine、exposure engine、受控 Experiment V3、PBO/DSR、唯一 Locked Test、正式回测或前向 Paper 运行证据。
- 质量成长 V1 的正式状态仍为 `blocked_missing_pit_data`；完整中证 800 PIT 成分、全收益基准、PIT 行业/市值/交易状态和首披财务等数据门尚未通过。V2 不能补写或提升 V1 状态。

## 安全状态

- V2 契约状态：`p0_runtime_implemented_not_admitted`。
- 研究状态：`blocked_missing_pit_data`。
- Paper：`paper_eligibility=false`，尚未准入。
- 交易：`trade_eligibility=false`。
- 真实资金：`real_money_list_allowed=false`。
- LIVE：永久 `live_not_supported`；配置、枚举、Token、白名单或 readiness 均不能解锁。
- 规范化哈希只证明内部内容一致，不证明官方来源、统计有效性、策略盈利或执行授权。

## 待决策

1. Gate、Planner、PaperBroker 与持久化订单如何共同绑定 canonical `FeeSchedule` / instrument fee rule，并在拒绝前避免或恢复 `SUBMITTING` 状态。
2. 官方交易日历、执行行情和受控 signal registry 采用哪个现有 Provider/receipt 契约接入；普通 Alpha 的 next-session 有效期如何冻结。
3. Alpha engine、exposure engine 和 Experiment V3 的最低自由度预注册方案，以及何时允许唯一一次 2024—2025 Locked Test。
4. 何时 push 当前 `HEAD` 并创建 Draft PR；本轮只获得本地提交授权，未获得当前结果的 push/PR 授权。

## 下一步

1. 先由外部 Agent 按下述范围审查当前 worktree，重点复核两项 P1、V1 兼容和准入措辞。
2. 实现 fee/rule bundle 绑定、官方日历/行情/signal registry 和 next-session Alpha 编排，并重新做对抗测试。
3. 冻结 Experiment V3 与 Alpha/exposure 参数后，补齐正式 Choice PIT 数据门；数据门通过前不运行或解释 Locked Test。
4. 只有历史门通过后才进入新的前向 Paper 生命周期；无用户明确授权时不 push、不创建 PR。

## 建议外部审查范围

按 `STATUS.md -> 0dce921..worktree -> V2 config/Schema -> adaptive_exposure.py -> trading planner/risk/order_store/paper -> PaperLedger V2 -> tests -> DECISIONS.md` 恢复上下文。优先攻击：低报费用与 FeeSchedule 替换、截断日历 payload、篡改报价 flags/价格/时间后重算公开哈希、跨 session Alpha、退出残仓大于 3 只或单只 40%、CAS 并发覆盖、旧 SQLite 迁移、latch 后重新买入、V1 order ID/账本/backtest 漂移，以及任何把 P0 实现写成 Paper/交易准入的越级表述。
