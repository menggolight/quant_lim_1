# Strategy Workspace 代码边界

本目录同时保留默认的质量成长 V1 研究链和非默认的 Adaptive Exposure V2。模块存在、Schema 通过或专项测试通过，只表示工程契约可复核；不表示数据来源已认证、统计有效、Paper 已准入或允许交易。仓库永久不支持 LIVE。

## Adaptive Exposure V2 五模块

| 模块 | 单一职责 | 明确不做 |
|---|---|---|
| [`alpha_engine_v2.py`](alpha_engine_v2.py) | 从 typed、自哈希的受控 PIT 截面重算质量成长慢因子和预注册价量快因子，使用 train-only 冻结模型输出全股票池预测、排名与排除码 | 不生成订单；不接受调用方预算因子；缺失值不补0；未来数据令批次失败关闭 |
| [`exposure_engine_v2.py`](exposure_engine_v2.py) | 只用六类允许输入，输出 `RISK_OFF=0`、`DEFENSIVE=0.30`、`NEUTRAL=0.60`、`RISK_ON=1.00`；普通变化走预注册迟滞，数据失败和账户回撤可立即降仓 | 不内置或猜测生产阈值；不读取其他特征；不直接改账户 |
| [`portfolio_constructor_v2.py`](portfolio_constructor_v2.py) | 合并 Alpha 与目标总仓位；最多3只、单只不超过40%；应用 entry/hold band、整手、完整成本和 no-trade threshold，输出目标/可实现/当前分离及 BUY/SELL/HOLD/CASH | 不为凑单交易；不把排除行缺失预测补0；不让 Alpha no-trade 门阻断显式风险减仓 |
| [`next_session_signal.py`](next_session_signal.py) | 将 D 日 Intent、构造结果、数据/模型/政策哈希、结构化日历 receipt 和 canonical 执行规则 bundle 冻结到紧邻 D+1；单次复核账户、报价、费用、整手和 BUY 偏离 | 不把 bool/来源字符串当官方证明；不让风险退出走 Alpha 通道；不自动提交订单 |
| [`operations/daily_pipeline.py`](../../operations/daily_pipeline.py) | 编排收盘后阶段1—9、盘前阶段10、人工成交阶段11和收盘账本阶段12；生成不可变 JSON/Markdown 决策和本地通知 outbox | 不抓取或认证生产数据；不选择模型/阈值；不外发通知；不确认成交；不授予执行权限 |

Alpha Engine 对单股缺失 PIT 字段保留完整排除行，`predicted_return`、分数、percentile 和 rank 均为 `null`。当没有任何合格股票时明确输出 `NO_ALPHA_CASH`。Portfolio Constructor 只有在普通 Alpha 的预期改善严格高于完整预计成本加显式 no-trade threshold 时才换仓；仍在 hold band 的原持仓优先保留。

Exposure Engine 的六类输入固定为：中证800全收益趋势、市场宽度、已实现波动、市场回撤、Alpha 预测分布和账户回撤。任何缺类、多类、未来/陈旧 session、失败状态或哈希漂移都不能被静默忽略；同一 CST 策略日不能重复推进迟滞，正式流水线的状态必须从固定策略级registry续接上一官方交易日不可变inputs/decision/state，换report目录不能bootstrap重置。失败日写可验证的`IMMEDIATE_RISK_OFF`续接状态。账户回撤由已验证 Paper Ledger V2 峰值与 D 日策略 NAV 内部派生，不接受 updater 自报；无账本首次bootstrap只接受冻结政策的空仓初始资金。

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
3. 生成全股票池 Alpha 排名和排除原因。
4. 生成 Exposure 状态与迟滞记忆。
5. 构建理论目标和整手可实现组合。
6. 冻结显式 `PortfolioIntent`。
7. 写入不可变 `daily-strategy-decision.v2`；预期数据/验证异常也产出零订单 `BLOCKED` 日报和 failure receipt，不可变碰撞仍直接报警。
8. 生成 Markdown/JSON 计划及分阶段证据 JSON。
9. 只写本地 `local-notification-outbox.v1`；没有外部投递器。
10. D+1 盘前单次复核，返回无执行权限的人工指令。
11. 操作员逐项记录 FILLED/PARTIAL/UNFILLED 及证据哈希。
12. 收盘使用人工成交、canonical 成本 bundle 和 receipt-bound typed close-mark bundle 追加 Paper Ledger V2，逐项重算佣金、印花税、过户费、滑点及成本后状态。

每天都生成决策，但允许零订单。日报分离目标、可实现、当前和实际事实；D日计划的`realized_*`固定为`null`，实际仓位只由D+1成交后收盘账本形成。日报记录 BUY、SELL、HOLD、CASH、价格偏离上限、取消条件、完整成本、模型/风险/no-trade 原因以及数据、模型、政策、意图和决策哈希。

## 当前外部阻塞

- 没有接入生产级外部受控 PIT updater；当前诊断数据不能转换为正式输入。
- 没有接入生产官方日历、证券规则/费率和行情 registry；内容哈希不证明官方来源。
- Experiment V3 尚未正式冻结，因此没有正式 train-only Alpha model、Exposure hysteresis policy 或 Constructor entry/hold/no-trade/偏离阈值 artifact。
- 2024—2025 Locked Test 未运行、未解释；Daily Pipeline 对该日期范围显式失败关闭。
- 通知只有本地 outbox，没有外部发送成功证据。
- `paper_eligibility=false`、`trade_eligibility=false`、`real_money_list_allowed=false`、`live_supported=false`；LIVE入口仍返回`live_not_supported`。

完整规格见 [`docs/ADAPTIVE_EXPOSURE_V2.md`](../../docs/ADAPTIVE_EXPOSURE_V2.md)，当前工程交接状态以 [`docs/STATUS.md`](../../docs/STATUS.md) 为导航，但状态文档本身不构成准入真值。
