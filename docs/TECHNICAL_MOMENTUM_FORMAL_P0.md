# Technical Momentum 正式数据与验证 P0

## 当前结论

`a-share-technical-momentum-adaptive-v1` 是当前唯一正式研究主线。质量成长线暂停但保留；Technical Shadow 继续承担每日业务闭环观察，不由本 P0 扩建，也不作为正式数据或历史回测证据。

截至 2026-08-28，本 P0 的工程契约已独立建立，但真实数据与执行会计尚未满足准入条件，状态为 `BLOCKED`。Development 与 Validation 不得在数据门失败时运行；2024—2025 Locked Test 保持：

- `locked_test_status = NOT_RUN`
- `locked_test_consumed = false`

本轮没有读取、运行或解释 Locked Test 策略结果。

## 冻结实验

正式规格为 [`configs/a_share_technical_momentum_adaptive.v1.json`](../configs/a_share_technical_momentum_adaptive.v1.json)，并由 [`schemas/technical_momentum_experiment.v1.json`](../schemas/technical_momentum_experiment.v1.json) 约束。它以内容哈希绑定现有 Technical Shadow 的 Alpha/Exposure 真源，但使用独立运行路径，不修改 Shadow 代码或历史产物。

冻结内容包括：

- 六因子 `RM20/RM60/RM120/TREND_EFF60/DOWNSIDE_VOL60/BREAKOUT60`；
- 1%/99% winsor、population z-score、原方向与原 Entry/Hold 门；
- 原 60/60/20 日 Exposure 输入、阈值、优先级与 0/30%/60%/100% 映射；
- 最多 3 只、单只 40%、100 股整手、候选不足留现金；
- 基础成本与 20 bps + 双倍佣金压力成本；
- Development 2018—2022、Validation 2023、Locked Test 2024—2025。

没有新增因子、权重、Exposure 阈值、午盘逻辑、买卖价格区间或止盈止损。

## 正式数据契约

[`schemas/technical_formal_dataset_manifest.v1.json`](../schemas/technical_formal_dataset_manifest.v1.json) 固定九类输入：

| 数据集 | 研究/执行用途 | 关键失败条件 |
|---|---|---|
| `trade_calendar` | 受控 D/D+1 相邻性与覆盖分母 | 缺日、乱序、重复 |
| `raw_daily_bar` | 原始 OHLC、成交与执行价格 | 非停牌缺行、复权口径或错证券 |
| `adjustment_factor` | 当时有效的公司行动调整信号 | 未来因子、缺因子、日期倒序 |
| `csi800_pit_membership` | 决策日前最新完整中证800截面 | 缺月、非800、重复、权重不闭合、当前回填 |
| `suspension_history` | 停牌双向阻断 | 状态缺失或不可用时点不明 |
| `price_limit_history` | 涨停禁买、跌停禁卖 | 上下限缺失或来源口径漂移 |
| `name_and_st_history` | ST 禁止新买 | 历史区间缺口或当前状态回填 |
| `security_master` | 上市/退市与 100 股规则 | 关键日期缺失或非 PIT 当前快照 |
| `csi800_price_benchmark` | 同期价格基准收益 | 日期不完整或误用全收益/股票日线 |

正式覆盖要求为 2018-01-01 至 2025-12-31；为保证 2018 年首个决策点有 RM120，另要求从 2017-07-01 开始的受控 warm-up，以及 2018 年前可用的成分截面。Locked 分区只允许做 Schema、哈希和 coverage 检查，不允许产生 factor、ranking、signal、trade、NAV 或 return。

允许的数据接口仅限 BaoStock 标准只读接口与 Tushare 标准非 VIP 候选接口。Tushare capability probe 的成功、Token、接口名或文件哈希都不能自行升级为正式数据准入；Tushare VIP、财务、行业、新闻、研报和情绪接口均不在本轮范围。

## 双价格通道

Signal 通道对每个交易日只使用当日及以前有效的因子：

```text
signal_return_t = raw_close_t * adj_factor_t
                  / (raw_close_t-1 * adj_factor_t-1) - 1
```

Signal OHLC 使用同一当日因子并以任意历史起点规范为 1，供原技术公式消费。向输入追加未来 `adj_factor` 不得改变历史 prefix 的值或哈希。

Execution 通道保持真实未复权 `open/close`。D+1 股数、现金、费用、滑点和 NAV 只能接受 Execution 的精确类型；Signal 类型不能进入这些函数。

### 公司行动会计硬门

单一 `adj_factor` 只能确定总财富变化，不能唯一分解现金分红、送转、配股、税和结算后的真实股数/现金。把差额全记现金会错判送转，把差额全变股数等价于假设红利自动再投资，直接用 Signal 价格计 NAV 又违反未复权执行契约。

因此当前规则固定为：持仓跨越非单位因子变化且缺少可核验的公司行动权益分解时立即失败关闭。该分解不在用户本轮九类数据白名单内，所以它是当前 Locked readiness 的独立 blocker，不能用估算法绕过。

## PIT 股票池

每个决策日只选择严格早于决策日的最新完整截面。Loader 逐月检查 2017-12 前置截面及 2018-01 至 2025-12：

- 每截面恰好 800 个唯一 A 股代码；
- 唯一键为 `snapshot_date + instrument_id`；
- 日期严格升序，不得按成员拼接截面；
- 权重必须为正且有限，保留源值，不自动归一化；
- `index_weight` 规范化值必须至少保留候选接口已观察到的三位小数；权重和容差再由每个源 Decimal 的原始精度逐行推导，不允许用粗精度把大幅缺口包装成舍入误差；
- 任一缺月、重复、数量、权重、来源、哈希或时点问题使整个正式数据门失败。

## 执行与报告

独立 Technical backtest 只接受 `development` 或 `validation`。它在读取任何输入元数据或行前拒绝 `locked_test`；Development/Validation 的日历、Signal、Execution、状态、公司行动和 PIT loader 都必须先提供恰止于各自 `split_end` 的物理分区元数据，任一越界会在首行迭代前失败关闭。执行内核实现 D 收盘到紧邻 D+1 未复权开盘、停牌、涨跌停、ST、上市/退市、T+1、失败卖出残仓、100 股和完整成本。

基础与压力情景分别完整重放，因为费用导致的 NAV/回撤可能改变后续 Exposure 路径。报告契约为 [`schemas/technical_momentum_backtest_report.v1.json`](../schemas/technical_momentum_backtest_report.v1.json)，Locked readiness 契约为 [`schemas/technical_locked_test_readiness.v1.json`](../schemas/technical_locked_test_readiness.v1.json)。

当前真实覆盖不足且公司行动会计门未通过，所以 Development/Validation 报告必须为 `NOT_RUN_BLOCKED`，不能用合成测试结果替代。

标准报告命令只接受当前能力证据并 create-only 发布三份产物：

```powershell
python -m operations.run_technical_formal `
  --output-directory "docs/technical_momentum_p0/20260828T121846+0800" `
  --dataset-evidence "docs/technical_momentum_p0/current_capability_evidence.json" `
  --generated-at "2026-08-28T12:18:46+08:00"
```

当前命令按约定以退出码 `1` 表示 `BLOCKED`，并保持 `locked_test_status=NOT_RUN`、`locked_test_consumed=false`。它不会因为调用者写入 `complete`、布尔 check、来源名、哈希或任意 metrics JSON 而提升准入；九类原始数据的标准 CLI 逐行验证器和把正式引擎结果绑定到数据/config 哈希的受控 runner 尚未实现，分别以固定 blocker 明示。

## 当前真实证据与剩余阻塞

仓库当前正式 BaoStock validated 数据只有 2026 年的小样本日线、短日历和一个 current-only 证券主表记录。最新 Tushare 标准接口 capability 证据只证明若干小样本接口可响应：`index_weight` 仅有 2024-01-31 单截面；`stk_limit`、`stock_basic` 有字段漂移，`namechange` 返回无效载荷；整个 receipt 明示 `formal_data_admission=false`。

本轮固化的可复核链为：

- [当前 capability evidence](technical_momentum_p0/current_capability_evidence.json)
- [dataset coverage report](technical_momentum_p0/20260828T121846+0800/dataset_coverage_report.json)
- [Development/Validation report](technical_momentum_p0/20260828T121846+0800/development_validation_backtest_report.json)
- [Locked Test readiness report](technical_momentum_p0/20260828T121846+0800/locked_test_readiness_report.json)

因此当前至少仍缺：

1. 九类数据的 2018—2025 完整、受控、可重放批次及 2017 warm-up；
2. 2017-12 至 2025-12 无缺月的中证800 PIT 截面；
3. 完整价格限制、名称/ST、上市退市与停牌历史；
4. 可支持 raw NAV 的公司行动权益分解，或用户明确改变本轮数据范围；
5. 退市残仓的受控终止估值/结算证据；缺失时引擎保留失败关闭，不能沿用最后收盘价或擅自记零；
6. 九类原始文件标准 CLI 逐行验证与受控 manifest receipt；
7. 标准 runner 对引擎要求的物理 split metadata、数据/config/源码哈希和正式指标的受控绑定，以及数据门通过后的 Development 与 Validation 基础/压力双跑。

在这些条件满足前，结论固定为 `BLOCKED`，不会运行 Locked Test、Paper、券商连接或任何订单。
