# A股小账户动态仓位 V2

`a-share-small-account-adaptive-exposure-v2` 是独立于质量成长 V1 的新策略版本，不是仓库默认策略。它把 Alpha、总仓位、组合构建和执行风控拆开，并把“允许长期持有现金”提升为显式组合意图。

当前已经完成 P0.1 执行修复并冻结执行内核，同时实现日终信号生产、下一官方交易日盘前人工复核和日频 Paper 决策的代码契约。这里的“完成”只表示实现和专项验证，不表示生产数据、模型或阈值已经接入：外部受控 PIT 数据、官方日历/证券规则 registry、正式 train-only 冻结模型与预注册阈值产物仍缺。Experiment V3 尚未正式冻结，2024—2025 Locked Test 未运行、未解释。`paper_eligibility=false`、`trade_eligibility=false`、`real_money_list_allowed=false`、`live_supported=false`；任何 LIVE 入口仍永久返回 `live_not_supported`。

## 目标的正确含义

月净收益 10% 只是挑战报告指标：

- 不是收益保证；
- 不是模型损失函数；
- 不是准入门；
- 不是参数优化目标；
- 不能用来反复查看 2024—2025 后调参。

正式报告未来需要同时披露未达标月份、最差月份、月度 CVaR、最大回撤、成本、现金月份和平均实际仓位，不能只报告“命中 10%”的月份。

## 冻结组合边界

- 独立策略资金：10,000 元；
- 只做多，不加杠杆，不做空；
- 目标总仓位范围：0%—100%；
- 初始离散状态：`RISK_OFF=0%`、`DEFENSIVE=30%`、`NEUTRAL=60%`、`RISK_ON=100%`；
- 最多 3 只，每只目标权重不超过 40%；
- 最低现金权重为 0%，但整手、费用、候选不足和不可成交造成的现金必须真实保留；
- 没有足够合格或可负担的股票时，不为凑满三只而降低门槛。

政策真源是 [strategy_adaptive_exposure.v2.json](../configs/strategy_adaptive_exposure.v2.json)，结构契约是 [strategy_adaptive_exposure_policy.v2.json](../schemas/strategy_adaptive_exposure_policy.v2.json)。质量成长 V1 的 Top2、20% 最低现金、冻结政策哈希和历史回测行为均保持不变。

## PortfolioIntent

研究输出不能用裸 `{instrument_id: weight}` 表达“没有 Alpha”“风险退出”或“普通调仓”。V2 使用 [portfolio_intent.v1.json](../schemas/portfolio_intent.v1.json) 区分：

- `ALPHA_REBALANCE`
- `NO_ALPHA_CASH`
- `DEFENSIVE_REDUCTION`
- `RISK_OFF`
- `ACCOUNT_DRAWDOWN_EXIT`
- `DATA_FAIL_CLOSED`
- `MANUAL_PAUSE`

只有 `NO_ALPHA_CASH`、`RISK_OFF` 和 `ACCOUNT_DRAWDOWN_EXIT` 明确允许零目标仓位和空权重。普通空权重继续失败关闭；`DATA_FAIL_CLOSED` 和 `MANUAL_PAUSE` 在语义进一步冻结前也不能借空映射隐式清仓。

每个意图绑定 `intent_id`、策略、可见/冻结/决策时点、目标总仓位、目标权重、原因码以及信号、市场数据、模型和风险状态的 SHA-256。Schema 通过不能替代来源准入、PIT 或策略有效性。

## P0.1 执行内核修复与冻结

以下七项 P0.1 问题已经在 Planner、Gate、订单存储、Paper Broker 和日频账本契约中修复，并作为冻结执行内核继续保留：

1. `DATA_FAIL_CLOSED` 与 `MANUAL_PAUSE` 在 Planner 和 Gate 两层均禁止新增 `BUY`，不能靠伪造计划绕过。
2. `RISK_OFF`、`ACCOUNT_DRAWDOWN_EXIT` 等完整退出计划由 Gate 根据受控账户、报价与证券规则独立重建覆盖要求；每个现有持仓必须有正确卖单或可核验的物理阻塞原因。
3. `RISK_OFF`、`DEFENSIVE_REDUCTION`、`NO_ALPHA_CASH` 和 `ACCOUNT_DRAWDOWN_EXIT` 均支持冻结意图后的第一次相邻 D+1 执行；后续跨 session 重试必须携带父 attempt/plan 谱系。
4. 日亏损限制只阻断 `RISK_INCREASING` 订单，不阻断只含安全卖单的纯减仓或强制退出。
5. Paper 成交后的账户更新使用账户 fingerprint compare-and-swap；并发或过期快照不能覆盖已经变化的现金与持仓。
6. Planner、Gate、Approval、Paper Broker 与持久化计划共同绑定 canonical `FeeSchedule + InstrumentRule + whole-lot policy` bundle；Gate 和 Broker 均从结构化 bundle 重算哈希、费用、tick 与整手约束。
7. Paper Broker 对整批订单完成账户、approval、报价、规则、费用、现金和数量预检后，才允许任一订单进入 `SUBMITTING`；批内后续失败不能留下已经提前进入提交态的前序订单。

冻结表示后续信号生产通过版本化适配器消费该内核，不再为日常模型迭代改写上述安全语义。它不表示 Paper 或交易已准入，也不改变 LIVE 永久不支持。

对应执行计划契约已经升级为 [portfolio_execution_plan.v2.json](../schemas/portfolio_execution_plan.v2.json)，策略政策契约升级为 [strategy_adaptive_exposure_policy.v2.json](../schemas/strategy_adaptive_exposure_policy.v2.json)。旧 V1 只用于既有历史兼容，不作为新 V2 计划的写入格式。

## 计划、尝试与订单

同一组合意图可能因 T+1、停牌或跌停连续多日退出，因此分离：

- `intent_id`：原始组合决定，逐日重试时保持不变；
- `attempt_id`：某一受控执行时段的实际尝试；
- `client_order_id`：绑定策略、intent、attempt、标的和方向。

同 intent、同 attempt 重放必须命中同一订单 ID，不能重复成交；下一受控日重试使用新 attempt ID。计划记录：

- `target_gross_exposure`：策略预算；
- `feasible_gross_exposure`：按整手、预计费用、可卖数量和预计计划后 NAV 重算的可实现仓位；
- `realized_gross_exposure`：仅能在真实成交和日终估值后记录，计划阶段保持 `null`；
- 总换手和普通换手；
- 致命 `rejections` 与非致命 `blocked_exit_reasons`。

跨session风险退出还必须携带完整的内部受控日历payload。Planner与Gate使用和日频账本相同的规范化算法重算日历哈希，并要求前一session与执行session在payload内严格相邻；计划还绑定Planner实际使用的规范化执行报价包哈希，Gate从收到的报价payload重算并复核价格、时点、停牌、买卖封锁和价差。两类哈希都只证明内容一致：没有官方registry时，不能证明日历未遗漏真实交易日，也不能证明报价来自官方来源。

普通 `ALPHA_REBALANCE` 的 D 日收盘结果现在必须通过独立的 [next_session_signal.py](../research/strategy_workspace/next_session_signal.py) 冻结，且只能在结构化日历 receipt 指定的紧邻 D+1 消费一次。Alpha 与风险退出使用不同 factory；`NO_ALPHA_CASH` 与其他风险减仓都走风险通道，不能伪装为普通 Alpha 买入。Signal 文件采用 create-only、同字节幂等语义；消费 CAS 以完整 `signal_sha256` 在固定策略级 registry 全局唯一登记，调用者选择的 signal/report 路径没有另开消费槽的权限，复制、改名、路径别名以及 `CANCELED` 后重试都不能获得第二次执行机会。人工成交 bundle 同样以 `consumption_sha256` 形成唯一 create-only 槽，收盘账本必须重读其精确 canonical 字节。盘前会重新核对策略账户 fingerprint、执行报价和 canonical 费用/证券规则 bundle；BUY 超出冻结最大价格偏离或不再满足当日整手规则即取消。该适配器只返回人工执行指令，不提交订单。固定 registry 的文件系统 ACL 是当前单机 writer 信任边界；Stage 11 没有原始 D+1 account/quote payload，不能仅凭哈希反推并重新认证其来源。生产多机 CAS、官方日历/证券规则/账户/行情 registry 均未接入；调用测试夹具、自建 allowlist 或直接写 registry 不能被描述成正式准入，SHA-256 也只证明内容一致。

订单风险方向为 `RISK_INCREASING`、`RISK_NEUTRAL`、`RISK_REDUCING` 或 `FORCED_EXIT`。方向由受控计划器从 intent 和账户状态推导，不能信任调用者自报。

## 回撤退出

策略 NAV 在 D 日收盘首次达到 12% 回撤时触发 `ACCOUNT_DRAWDOWN_EXIT`。12% 是风险触发值，不是最大亏损保证。

- D 日收盘锁定退出状态；
- 按内部受控日历的下一相邻session开盘开始卖出，不等待下一次 20 日 Alpha 信号；
- 未卖出的持仓以后每个受控交易日继续尝试；
- 持仓真实归零前不得把实际仓位写成 0；
- 一旦触发，`risk_latched` 在当前受控账本和策略运行周期内永久保持；持仓归零只把 `exit_pending` 变为 `false`，不会恢复买入权限；
- T+1、停牌、跌停、可卖数量、行情时效、账户指纹和订单幂等继续生效。

风险减仓和强制退出卖单不受普通换手上限及普通单笔名义额上限阻断；普通 Alpha 换仓的买卖腿仍受原门禁。一个标的物理不可卖时可以记录为 `blocked_exit_reason`，但不能阻止同一计划中其他安全卖单。缺行情、未来行情或过期行情仍是致命错误，因为当前尚未把执行报价和独立估值 mark 拆开。

Gate 不再只信任订单自带预计费用。Planner、Gate、Approval 与 PaperBroker 绑定同一个 canonical `FeeSchedule + InstrumentRule` bundle；Gate 从受控结构重算 bundle hash、逐单费用、tick 和整手约束，PaperBroker 在整批预检中再次核对，并在任何订单进入 `SUBMITTING` 前失败关闭。该绑定修复的是内部一致性，不能证明费率或证券元数据来自官方来源；生产 registry 和账户真实费率人工复核仍是外部阻塞项。

## 五个信号生产模块

1. [alpha_engine_v2.py](../research/strategy_workspace/alpha_engine_v2.py) 只消费 typed、自哈希的受控 PIT 截面，独立重算六个质量成长慢因子与六个预注册价量快因子，再调用 train-only 冻结线性模型。它输出全股票池的预测、质量/时机分数、排名、行业、资格与完整排除码，不生成订单。批次级未来数据或公共来源污染会令全截面 `DATA_FAIL_CLOSED`；单股缺失 PIT 字段则保留该行，所有分数为 `null`，绝不补 0。代码与 Schema 已实现，但生产受控 PIT adapter 和正式冻结模型 artifact 尚未接入。
2. [exposure_engine_v2.py](../research/strategy_workspace/exposure_engine_v2.py) 只接受中证800全收益趋势、市场宽度、已实现波动、市场回撤、Alpha 预测分布和账户回撤六类输入。六类 `OK` 指标必须属于决策时刻换算后的同一 CST 策略日；未来或陈旧 session 都失败关闭。状态固定映射 `RISK_OFF=0`、`DEFENSIVE=0.30`、`NEUTRAL=0.60`、`RISK_ON=1.00`；普通变化依赖预注册迟滞，不能在同一 CST 策略日重复推进，也不能用不可达 pending 次数伪造确认。正式流水线只从固定策略级 registry 续接前一官方 session 的不可变 inputs/decision/state，切换 report 目录不能重置记忆；失败日若 policy 可验证，会持久化绑定 failure receipt 的 `IMMEDIATE_RISK_OFF` 状态供次日安全续接。账户回撤从已验证 Paper Ledger V2 峰值、D 日策略账户和受控收盘价内部派生；数据 updater 不能自报该值。无账本的首次空仓 bootstrap 只接受冻结政策中 `initial_cash=10000` 的账户，不能把任意缩水余额重置成新峰值。数据失败、歧义规则或账户回撤达到12%立即转为 `RISK_OFF`。引擎不内置生产阈值，正式迟滞 policy artifact 尚未冻结接入。
3. [portfolio_constructor_v2.py](../research/strategy_workspace/portfolio_constructor_v2.py) 合并 Alpha 与目标总仓位，最多3只、单只不超过40%，候选不足和整手不可负担均留现金。它先保留仍在 hold band 的原持仓，再考虑 entry band 新候选；普通 Alpha 只有在预期改善严格大于完整预计成本加显式 no-trade threshold 时才交易，风险减仓不受该 Alpha 门阻断。输出始终分离理论目标、整手可实现和当前状态，并给出 `BUY`、`SELL`、`HOLD`、`CASH`、整手数量、费用与原因。所有 band、成本、时效和价格偏离阈值都来自预注册、哈希绑定的 policy 对象，没有生产默认值。
4. [next_session_signal.py](../research/strategy_workspace/next_session_signal.py) 把 D 日 `PortfolioIntent`、构造结果、数据/模型/政策/意图哈希、结构化日历 receipt 和 canonical 执行规则 bundle 冻结到紧邻 D+1。日历必须通过受控 exact-receipt allowlist；bool、来源字符串或自报哈希不能解锁。D+1 仅一次复核账户、报价、规则、费用、整手和 BUY 价格偏离，结果只是人工指令。风险退出有独立通道，不能冒充 Alpha；任何路径都不自动提交。
5. [daily_pipeline.py](../operations/daily_pipeline.py) 把上述模块与日频 Paper 账本编排为明确的收盘、盘前、人工成交和收盘记账边界。调用者必须提供冻结数据 updater、模型、Exposure policy、Constructor policy、官方日历 registry 候选、账户与 canonical 规则；非首日状态必须来自上一官方日不可变产物，非空账户回撤必须由已验证账本派生。股票池变更时，策略旧持仓用独立受控 D 收盘价和规则进入 exit-only 行，绝不因离开 Alpha 池而丢失退出覆盖。流水线不会替调用者选择生产阈值，也无外部消息发送或券商写权限。

## 每日 12 阶段职责

| 阶段 | 责任与失败边界 |
|---|---|
| 1. 更新数据 | `DailyDataUpdaterV2.update_and_freeze` 返回不可变 `frozen-daily-data.v2` D 收盘 envelope；只允许四类市场 Exposure 指标，账户回撤和 Alpha 分布由流水线派生；流水线本身不抓取或认证外部源。 |
| 2. 数据门 | 校验 PIT 时点、完整性、哈希、账户时效、日历 receipt 与 execution-rule bundle；失败关闭且禁止 BUY。 |
| 3. Alpha 排名 | 生成全股票池排名和完整排除原因；无合格股票输出 `NO_ALPHA_CASH`，不为产生交易而降门槛。 |
| 4. Exposure 状态 | 读取恰好六类输入并应用冻结迟滞状态；数据失败与账户回撤可以立即降为 `RISK_OFF`。 |
| 5. 目标组合 | 输出理论目标、整手可实现组合、当前组合、成本比较及 BUY/SELL/HOLD/CASH。 |
| 6. PortfolioIntent | 把组合结果转成显式 Alpha、现金或风险意图，并绑定数据、模型、风险状态和构造哈希。 |
| 7. 不可变 daily decision | 写入 `daily-strategy-decision.v2`；同一日期同内容重放字节一致，不同内容发生 immutable collision。数据更新或预期验证失败也写零订单 `BLOCKED` decision 与 failure receipt；不可变碰撞仍直接报警，不被吞掉。 |
| 8. Markdown/JSON 计划 | 生成供人工复核的标准 JSON 和 Markdown，同时写出各阶段证据 JSON；它们不是下单文件。 |
| 9. 通知 | 只写本地 `local-notification-outbox.v1` 文件作为待投递证据；当前没有邮件、飞书、短信或其他外部发送器，不能声称已经通知成功。 |
| 10. D+1 盘前复核 | 单次消费冻结 signal，重新核对账户 fingerprint、执行报价、规则 bundle、费用、整手及 BUY 偏离上限；只输出人工指令。 |
| 11. 人工成交记录 | 操作员必须逐项覆盖所有 `READY_FOR_MANUAL_EXECUTION` 指令，记录 FILLED/PARTIAL/UNFILLED 和证据哈希；代码重读中央 consumption、逐项绑定冻结 action/数量/整手/参考价/偏离，并以 `consumption_sha256` 单次CAS写人工成交 bundle，不替人确认成交。 |
| 12. 收盘 Paper Ledger V2 | 只从中央 immutable 人工成交 bundle、canonical 执行成本 bundle、receipt-bound typed close-mark bundle 与 closing intent 追加日频账本，逐项重算佣金、印花税、过户费、滑点及成本后现金、持仓、NAV、回撤和实际仓位。 |

## 标准日报

每个策略日都必须生成一条决策，即使订单数为零。标准日报至少记录 `strategy_date`、`execution_date`、`data_status`、`market_regime`、`portfolio_intent_type`、`target_gross_exposure`、`feasible_gross_exposure`、`current_gross_exposure`、`realized_gross_exposure`、四套对应股票权重与整手数量、`buy_orders`、`sell_orders`、`hold_positions`、`cash_weight`、最大执行价格偏离、取消条件、完整预计成本、模型/风险/no-trade 原因，以及 `data_sha256`、`model_sha256`、`policy_sha256`、`intent_sha256` 和 daily decision 自哈希。零订单是合法决策，不得为了输出 `BUY` 或 `SELL` 而强制交易。

日报中的目标仓位、可实现仓位、当前仓位和成交后实际仓位是四个不同事实，不得互相覆盖。D 日计划阶段三个 `realized_*` 字段固定为 `null`；成交后实际仓位只由 D+1 收盘 Paper Ledger V2 记录。任何收益或 NAV 报告只允许使用人工确认成交及完整成本后的账本结果；计划收益、毛收益或未成交目标不能写成实际收益。

## 日频 Paper 账本

V1 Paper 账本固定为 20 日决策记录和 Top2/20% 现金语义，不能原地扩展。V2 使用独立的 [`paper_ledger_v2.py`](../research/strategy_workspace/paper_ledger_v2.py) 与 [日频账本Schema](../schemas/strategy_paper_ledger_record.v2.json)，绑定策略政策哈希和受控日历；每个 `daily_session` 区分早盘尝试所执行的 `execution_intent` 与当日收盘后生效的 `closing_intent`，并持久化可重算的 `CanonicalExecutionCostBundleV1`、`PaperCloseExecutionEvidenceV1` 和 `ControlledCloseMarkBundleV1`。账本从前一日持仓、当日真实成交、逐项费用与 receipt-bound 收盘估值重算现金、持仓、NAV、峰值、回撤和三种总仓位；任意调用者自填 mark hash 或硬编码费率不能再充当证据。这样同一天可以先执行前序Alpha意图，再因收盘回撤切换为次日开始执行的退出意图。

账本只能证明记录完整性和内部对账，不证明信号来源可信、研究有效或 Paper 已准入。

P0 不提供 latch reset、自动换新账本或恢复入场的标准编排。未来即使由外部受控流程决定开启新账本，也必须形成新的、可审查的生命周期证据；不能修改旧记录、翻转旧 latch 或据此声称已获得正式 Paper 准入。

## 严格样本外边界

未来研究固定分段：

| 区间 | 用途 |
|---|---|
| 2018—2022 | Train |
| 2023 | Validation |
| 2024—2025 | Locked Test，仅一次受控正式运行后标记 consumed |
| 2026 冻结前 | `retrospective_consumed`，不得伪装新鲜样本外 |
| V2 规格冻结后的下一受控交易日 | 前向观察起点 |

Alpha、Exposure 和组合构造的生产代码契约已实现，但正式 Experiment V3、train-only 模型参数、迟滞/entry/hold/no-trade 阈值及受控 PIT 输入尚未冻结接入。流水线对 2024—2025 日期显式拒绝，当前没有读取或解释 Locked Test 收益，也不能据此调参。任何核心参数变化必须形成新策略版本。

## 当前完成与阻塞

当前实现完成项包括：P0.1 七项执行修复并冻结；五个信号生产模块；版本化政策、Schema 与不可变 daily decision；显式 PortfolioIntent；目标/可实现/当前/实际仓位分离；D→紧邻D+1一次性人工适配；本地通知 outbox；人工成交证据；独立日频 Paper 对账；以及确定性、未来数据、缺字段、零 Alpha、hold/no-trade、成本、D+1一次性、价格偏离和准入边界的专项测试。这些是工程能力，不是一次生产日跑或统计有效性证明。

仍阻塞：

- 完整外部受控 Choice 单源 PIT 历史成分、中证800全收益序列、行业/市值/交易状态和首披财务；
- 可由生产流水线读取并独立认证的数据 updater、官方交易日历 receipt registry、证券规则/费率 registry 与执行行情 registry；
- Experiment V3 正式冻结，以及与之绑定的 train-only Alpha 模型、Exposure 迟滞 policy、Constructor entry/hold/no-trade/偏离阈值；
- PBO/DSR、唯一一次 2024—2025 Locked Test 和任何对该区间的结果解释；
- 外部通知发送器、足够的前向 Paper 观察期、latch reset/新账本生命周期治理；
- 任何 Paper、交易或真实资金候选。

因此当前状态必须保持 `blocked_missing_pit_data`、`paper_eligibility=false`、`trade_eligibility=false`、`real_money_list_allowed=false` 和 `live_supported=false`。本地 outbox、通过测试或生成一份日报均不能提升这些状态。
