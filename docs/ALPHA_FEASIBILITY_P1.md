# Tushare 历史回填与 Alpha Feasibility P1

本轮只回答一个前置问题：在暂不扩建九类正式执行数据和小账户成交仿真的情况下，既有六因子 Technical Alpha 与冻结 Exposure 是否值得继续建设完整执行层。它是独立的 `research_alpha_feasibility_only` 研究门，不替代现有正式小账户框架，不授予 Paper、交易或 LIVE 权限。

## 冻结边界

- 市场、策略、请求参数和消费者数据日期只允许 `2017-07-01..2023-12-31`；Development 为 `2018-01-01..2022-12-31`，Validation 为 `2023-01-01..2023-12-31`。`generated_at` 仅记录真实审计生成时点，不是市场数据、信号日期或 Locked Test 日期，任何消费者不得将其解释为特征。
- 2024—2025 Locked Test 在配置、请求、缓存、DataFrame、消费者和报告层均不可达，固定为 `NOT_ACCESSED / NOT_DOWNLOADED / NOT_RUN`、`locked_test_consumed=false`。
- Tushare 仅允许标准只读 `trade_cal`、`index_weight`、`daily`、`adj_factor`、`index_daily`、`suspend_d`、`stock_basic`；不用 `pro_bar`、`*_vip`、Choice、券商、账户或订单接口，也不做字段级 BaoStock fallback。
- 六因子、权重、entry/hold 门槛、Exposure 阈值、最多3只、单只40%及0/30/60/100%总仓位均由源文件 SHA-256 绑定，运行时再次核验实际 ranker 与 Exposure 源码。

## 数据门

标准入口先逐月请求 `000906.SH` 的 `2017-12..2023-12` 共73个 `index_weight` 窗口。每月必须存在合法截面、代码唯一、权重非负且权重和落在逐行末位精度推导的容差内。成分数不是800时，只有绑定中证指数公司正式证据、月份、截面日、实际数量、原因和源文件哈希的受控说明才可放行；否则阶段状态为 `BLOCKED_PIT_MEMBERSHIP`，最终状态为 `BLOCKED_DATA`，不会规划股票历史任务。

PIT 通过后，只对73个月合法截面的成员并集回填。所有请求在本地形成不含 Token 的参数指纹、started claim、规范化响应哈希和 create-only 产物；完整任务离线重放，已开始但没有持久化响应的远端调用按歧义失败关闭，不自动重发。上游响应或错误字段一旦出现 `2024-01-01` 及以后日期，在进入消费者前隔离且不保存原始正文。

历史完整性要求：

- `trade_cal` 覆盖每个自然日并校验 `pretrade_date`、年度开市日数量和窗口内下一交易日映射；末日不跨到2024。
- `stock_basic` 分别请求 `L/D/P` 并逐行核对响应状态，只用于上市日、退市日、代码和交易所校验。
- `daily` 保存未复权 OHLCV 及单位；`adj_factor` 只做当日或之前 as-of；`index_daily` 完整覆盖每个开市日。
- 上市日至退市日之间的每个开市日必须有 `daily`，或有同日全日 `suspend_d` 且已有前一经济价值；其他缺口失败关闭。本轮不实现退市终值，也不因已知退市日提前卖出。

正式数据产物位于忽略提交的 `data/tmp/alpha-feasibility/tushare-p1-v1/`，其中至少包括：

- `pit_membership_coverage_report.json`
- `pit_membership_manifest.json`
- `history_coverage_report.json`
- `history_manifest.json`
- `alpha_feasibility_report.json`

最终报告把 collection plan、PIT manifest、history manifest、冻结实验 canonical config、Alpha Feasibility 引擎源码及终态 Gate 源码的 SHA-256 一并纳入自哈希。标准 loader 会从 create-only task store 重建历史 coverage/manifest 并逐字比对，不能只改 manifest 后重签自哈希。若在任何网络调用前因 Token 或本地证据失败，CLI 仍发布无指标的 `BLOCKED_DATA`，并用独立 create-only evidence 固定时间戳以支持字节一致重放。

## 收益、时序与成本

股票信号经济价值为 `raw_close_t * latest_adj_factor_available_on_or_before_t`；开盘执行参考为 `raw_open_t * 同一因果因子`。停牌日无论供应商是否同时返回 bar，信号 open/high/close 均沿用前一经济价值，并要求同日 `suspend_d` 证据。内部非停牌缺口会在排名前阻断，不能静默变成横截面排除。

时序固定为 D 收盘计算 Alpha/Exposure，D+1 未复权开盘的调整后同口径值进行小数权重换仓：旧仓承担 D 收盘到 D+1 开盘段，新目标只承担 D+1 开盘到收盘段。该模型扣除基础/压力双情景的比例佣金、卖出税、过户费和滑点，`annualized_turnover` 使用单边口径；最低5元佣金、整手、精确分红/送股股数、涨跌停、ST、退市终值和券商成交均未模拟，因此始终 `execution_realism=INCOMPLETE`。

## 运行与终态

```powershell
python -m operations.run_alpha_feasibility all `
  --config configs/a_share_technical_alpha_feasibility.v1.json `
  --output-root data/tmp/alpha-feasibility/tushare-p1-v1
```

`data` 子命令只完成数据门；默认 `all` 在数据验签通过后运行 Development/Validation 的 base/stress。禁止参数搜索、按2023结果重训或调整阈值。终态只有：

- `ALPHA_FEASIBILITY_GO_CANDIDATE`：Validation base 主动净收益大于0、stress 不小于0、两情景最大回撤均不超过12%，且单股与最佳10日收益集中度均不超过预注册50%门。
- `ALPHA_FEASIBILITY_NO_GO`：数据完整，但冻结 Alpha 在 Validation 不值得继续扩建完整执行层。
- `BLOCKED_DATA`：PIT、历史、回放或消费者完整性未通过；报告不含 Development/Validation 指标。

即使为 GO candidate，也只是后续工程候选，不是实盘收益、Paper 准入、股票推荐或交易授权。

为避免 IEEE-754 舍入把极小越界误判为通过，报告中的收益率、回撤、换手、成本、Exposure 和集中度均以 canonical decimal string 持久化；门禁使用 `Decimal` 精确比较。
