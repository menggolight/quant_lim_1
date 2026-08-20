# A股小账户动态仓位 V2

`a-share-small-account-adaptive-exposure-v2` 是独立于质量成长 V1 的新策略版本，不是仓库默认策略。它把 Alpha、总仓位、组合构建和执行风控拆开，并把“允许长期持有现金”提升为显式组合意图。

当前只实现 P0 契约和安全执行内核。没有开始收益调参，没有运行正式样本外回测，也没有提升 Paper、交易或真实资金准入。LIVE 仍永久 `live_not_supported`。

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

政策真源是 [strategy_adaptive_exposure.v2.json](../configs/strategy_adaptive_exposure.v2.json)，结构契约是 [strategy_adaptive_exposure_policy.v1.json](../schemas/strategy_adaptive_exposure_policy.v1.json)。质量成长 V1 的 Top2、20% 最低现金、冻结政策哈希和历史回测行为均保持不变。

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

普通 `ALPHA_REBALANCE` 当前只允许同session计划与审批。D日收盘Alpha到下一受控开盘的一次性有效期、受控信号registry和标准编排尚未实现，不能用风险退出的跨session通道替代。

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

Gate使用订单中的预计费用校验计划后现金和仓位；最终PaperBroker会用实际注入的 `FeeSchedule` 重算，并在任何成交前拒绝费用不一致。不过费率表本身尚未绑定到Gate approval，低报费用可能得到随后无法执行的approval并留下待恢复订单状态，因此正式Paper前仍必须补齐费率配置绑定与恢复流程。

## 日频 Paper 账本

V1 Paper 账本固定为 20 日决策记录和 Top2/20% 现金语义，不能原地扩展。V2 使用独立的 [`paper_ledger_v2.py`](../research/strategy_workspace/paper_ledger_v2.py) 与 [日频账本Schema](../schemas/strategy_paper_ledger_record.v2.json)，绑定策略政策哈希和受控日历；每个 `daily_session` 区分早盘尝试所执行的 `execution_intent` 与当日收盘后生效的 `closing_intent`，再从前一日持仓、当日真实成交和收盘估值重算现金、持仓、费用、NAV、峰值、回撤和三种总仓位。这样同一天可以先执行前序Alpha意图，再因收盘回撤切换为次日开始执行的退出意图。

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

P0 不实现 Alpha 或仓位模型，不读取 locked test 收益，也不据此调参。任何核心参数变化必须形成新策略版本。

## 当前完成与阻塞

P0 的完成定义只包括：版本化政策/Schema、显式 PortfolioIntent、风险方向、普通换手豁免的安全减仓、跨日 attempt 幂等、内部受控日历相邻session回撤退出、规范化报价/日历完整性绑定、日频对账契约和对抗性测试。

仍阻塞：

- 完整 Choice 单源 PIT 历史成分、全收益基准、行业/市值/交易状态和首披财务；
- Alpha 模型和 exposure engine 的预注册参数；
- 受控实验 V3、PBO/DSR 与唯一一次 Locked Test；
- 前向 Paper 的官方日历与行情registry、受控信号适配器、Gate费率配置绑定和足够观察期；
- latch reset / 新账本生命周期的外部治理与标准编排；
- 任何真实资金候选。

因此当前状态必须保持 `blocked_missing_pit_data`、`paper_eligibility=false`、`trade_eligibility=false`、`real_money_list_allowed=false` 和 `live=not_supported`。
