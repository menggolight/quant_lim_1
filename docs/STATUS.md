# 项目交接状态

> 本文是用于跨 Thread 和外部审查的交接快照，不替代代码、版本化配置、标准 CLI 产物、manifest 或真实运行证据。

## 快照元数据

- `as_of`：`2026-08-26T15:00:50+08:00`，Asia/Shanghai
- `branch`：`codex/project-review-20260820`
- `base_commit` / 当前 `HEAD`：`86d4c6b1d868914010bde0d76d3595c40087f154`
- `review_range`：`86d4c6b1..worktree`
- `worktree_state`：dirty；当前未提交范围为 Exposure 诊断、自然 BUY/SELL 路径、成交可重算修复及测试。本轮开始前遗留的 `docs/DECISIONS.md` 一行 Tushare 目录补项不属于该范围，继续保留且未混入提交。
- Git 写操作：已创建独立业务闭环提交 `86d4c6b1`（`feat: run real BaoStock technical shadow business loop`）；未 push、未修改 remote

## 当前目标

独立策略 `a-share-technical-shadow-mvp-v1` 已解释原10日连续 `RISK_OFF`，完成真实120日 Exposure 只读诊断、真实自然 BUY/SELL 路径验收，以及最近20个决策日的连续业务回放。这只证明冻结启发式策略的业务和模拟执行链可复核，不构成正式研究、Paper、交易或真实资金准入。

## 本轮完成

- 冻结既有60只当前成分诊断样本，明确 `current_not_pit`，不冒充历史PIT中证800。
- 新增固定六因子横截面排名：`RM20/RM60/RM120/TREND_EFF60/DOWNSIDE_VOL60/BREAKOUT60`，1%/99%去极值、总体Z-score、固定方向等权；逐日输出完整60只股票的因子、Z-score、综合分、排名、percentile、eligibility和排除码。
- 新增只使用真实中证800价格指数趋势、样本宽度、基准已实现波动和账户回撤的冻结Exposure；数据失败固定 `RISK_OFF=0`。
- 新增 BaoStock-only Shadow runner：D收盘决策、D+1真实开盘模拟成交、D+1真实收盘估值，账户、持仓和现金连续传递。
- 组合固定1万元、最多3只、单只最多40%、100股整手、无杠杆、无做空；佣金、最低佣金、卖出税、双边过户费和10bps单边滑点进入成交模型。
- 输出固定为 `data/tmp/technical-shadow-mvp/<run_id>/` create-only；Mock/合成数据不能标记为 `real_provider`，CLI无订单或LIVE入口。
- 修复真实运行阻塞：项目可选依赖最低版本改为 `baostock>=0.9.3`，使用官方新入口；BaoStock代码映射补入冻结基准 `000906.SH -> sh.000906`。
- 修复零交易原因码：存在Alpha候选但Exposure为0时记录 `RISK_OFF_CASH`，只有确无候选时才记录 `NO_ALPHA_CASH`。
- 未改 Tushare probe、Market Data V2、Adaptive Exposure V2、Experiment V3、Daily Signal、Paper或执行桥。
- Exposure诊断按最近120个已结束交易日输出每日输入、阈值、条件、规则、无迟滞状态和汇总分布；平坦现金账户被明确标为反事实诊断，不冒充自然账户路径。
- 修复冻结Exposure实现忽略四个窗口配置字段的问题；旧硬编码恰好等于当前冻结的`60/60/20/252`，因此该修复不改变当前输出，也不是连续RISK_OFF根因。
- 修复成交receipt无法精确复算现金的问题：成交价和显式费用先按分量化，持久化`execution_notional`、`explicit_fees`和`cash_delta`，滑点只通过成交价进入现金且不重复扣除。
- 修复单股共同日缺口被误报为全局`DATA_FAIL_CLOSED`的问题；缺失股票现在逐日排除并单独披露，只有Exposure整体输入失败才进入全局失败关闭。

## 关键变更文件

- [冻结策略配置](../configs/a_share_technical_shadow_mvp.v1.json)
- [Technical Alpha Shadow](../research/strategy_workspace/technical_alpha_shadow_v1.py)
- [Technical Exposure Shadow](../research/strategy_workspace/technical_exposure_shadow_v1.py)
- [业务回放 CLI](../operations/run_technical_shadow_mvp.py)
- [Exposure 120日诊断 CLI](../operations/diagnose_technical_shadow_exposure.py)
- [自然 BUY/SELL 路径 CLI](../operations/run_technical_shadow_natural_path.py)
- [BaoStock代码映射](../research/market_data/providers/baostock.py)
- [业务关键测试](../tests/test_technical_shadow_mvp.py)
- [决策 D-20260826-01](DECISIONS.md#d-20260826-01-隔离technical-shadow-mvp并冻结启发式业务回放)
- [最终真实运行 manifest](../data/tmp/technical-shadow-mvp/20260826T131026+0800-269b6c81/run_manifest.json)
- [最终真实运行 summary](../data/tmp/technical-shadow-mvp/20260826T131026+0800-269b6c81/run_summary.json)
- [120日 Exposure 诊断](../data/tmp/technical-shadow-exposure-diagnostic/20260826T142649+0800-60e42303/exposure_summary.json)
- [自然 BUY/SELL 验收 summary](../data/tmp/technical-shadow-natural-path/20260826T145605+0800-4d61044f/run_summary.json)
- [20日连续回放 summary](../data/tmp/technical-shadow-mvp/20260826T143744+0800-d3d2607b/run_summary.json)

## 真实运行证据

- 标准命令：`python -m operations.run_technical_shadow_mvp --recent-completed-sessions 10 --initial-cash 10000`，退出码0；真实 `baostock==0.9.3` 登录、查询、登出成功。
- 最终目录：`data/tmp/technical-shadow-mvp/20260826T131026+0800-269b6c81/`；manifest文件SHA-256为 `8125dfd4f1d45f21b9db041246a89612bd2e598cca4a07d0fdfcde679e24e7ae`。
- 决策日期：`2026-08-11` 至 `2026-08-24`；执行日期：`2026-08-12` 至 `2026-08-25`。
- 股票池 / 完整股票：`60 / 60`；真实数据receipt：`62`（日历1、基准1、股票60）。
- 决策 / BUY / SELL / HOLD / CASH日：`10 / 0 / 0 / 0 / 10`。
- 10日市场状态均为 `RISK_OFF`、目标总仓位均为0；原因码均为 `exposure_risk_off + RISK_OFF_CASH`，不是 `NO_ALPHA_CASH`。
- 最终持仓 `{}`；最终现金 / NAV：`10000.00 / 10000.00`；总交易成本 `0.00`；最大回撤 `0.0`。
- `DATA_FAIL_CLOSED=false`；`provider_kind=real_provider`、`synthetic=false`。
- 目录共96个文件：10个decision JSON、10个decision Markdown、10个各含60行的完整ranking、10行连续ledger、62个receipt、summary两份和manifest；manifest列出的95个artifact逐文件SHA-256复核零不匹配。
- ledger序号为1至10且 `previous_event_sha256` 连续；订单命名文件为0，所有日报 `automatic_order_submission=false`。

## 验证证据

- 业务专项与基准映射：`python -m unittest tests.test_technical_shadow_mvp tests.test_market_data_core.MarketDataCoreTest.test_baostock_code_mapping_is_strict_and_bidirectional -v`，`Ran 13 tests`，退出码0，`OK`。
- 安全全仓回归精确排除 `test_strategy_workspace_admission`、`test_strategy_workspace_evaluation`、`test_strategy_workspace_experiment`、`test_strategy_workspace_top_decile_backtest`：`Ran 911 tests in 125.813s`，退出码0，`OK (skipped=3)`；四个 Locked/Experiment 模块保持 `not run`。
- `python -m compileall -q agent research trading operations integrations tests`：退出码0。
- `git diff --check`：退出码0；仅有Windows LF→CRLF提示，无whitespace error。

## Exposure 与真实 BUY/SELL 路径验收

- 120日只读诊断命令：`python -m operations.diagnose_technical_shadow_exposure`；最终目录 `data/tmp/technical-shadow-exposure-diagnostic/20260826T142649+0800-60e42303/`，日期 `2026-03-04` 至 `2026-08-25`，manifest SHA-256 `ae74484a0bff8a2e101a98587fc4085f72974966371e1f89263e064c3f8e67e2`。
- 120日平坦现金反事实状态分布：`RISK_OFF=102 (85.00%)`、`DEFENSIVE=16 (13.33%)`、`NEUTRAL=2 (1.67%)`、`RISK_ON=0`；切换9次，最长连续RISK_OFF为69日。窗口内未观察到RISK_ON，但冻结规则四态均有构造性输入，因此没有结构性不可达状态。
- 原10日业务回放决策日期 `2026-08-11` 至 `2026-08-24` 已逐日对齐：10/10日`benchmark_trend <= 0`首先触发RISK_OFF；其中4日另有`market_breadth < 0.40`。账户回撤和数据失败均未触发，窗口配置映射修复不是根因，阈值未修改。
- 自然路径以120日诊断最早非零目标`2026-03-04`为选择锚点，并从此前5个交易日开始恢复账户；前导期本身已在`2026-02-25`自然进入NEUTRAL 60%。前导期交易用于恢复账户但不触发停止门；运行在锚点后的首笔SELL完成后停止，共9个决策日，未覆盖Alpha或Exposure。
- BUY：`2026-02-25`收盘决策，`2026-02-26`开盘模拟买入`600583.SH` 200股，市场开盘价`7.34`、成交价`7.35`、总成本`7.01`、现金变化`-1475.01`。
- SELL 1：`2026-03-03`收盘因Exposure从60%降至30%，`2026-03-04`开盘模拟卖出同股100股，市场开盘价`8.35`、成交价`8.34`、总成本`6.43`（含卖出税`0.42`）、现金变化`+828.57`。
- SELL 2：锚点后的`2026-03-09`收盘自然转为RISK_OFF，`2026-03-10`开盘卖出剩余100股，市场开盘价`7.10`、成交价`7.09`、总成本`6.36`（含卖出税`0.35`）、现金变化`+703.64`；两次卖出均晚于买入执行日，T+1通过。
- 自然路径最终目录 `data/tmp/technical-shadow-natural-path/20260826T145605+0800-4d61044f/`，最终现金/NAV`10057.20/10057.20`、持仓`{}`、累计成本`19.80`、最大回撤`-2.062423%`、`DATA_FAIL_CLOSED=false`；`601112.SH`因共同日缺口明确排除。manifest SHA-256 `e7244a0a172d9645712f09b3a2de18b8a99d3872ef59d0d4df63f5f685fe89bf`。
- 最近20个决策日标准自然回放日期 `2026-07-28` 至 `2026-08-24`，目录 `data/tmp/technical-shadow-mvp/20260826T143744+0800-d3d2607b/`；20日均全现金、零自然交易、最终现金/NAV`10000.00/10000.00`、`DATA_FAIL_CLOSED=false`。manifest SHA-256 `95652171b39a28d486f925b3b0dfae31048980eda462b7876e866533e1b05908`。
- 三个真实目录的manifest artifacts分别为185、92、125项，逐文件SHA-256零不匹配；summary canonical digest、ledger自哈希链、逐日现金/持仓/NAV独立复算均通过，订单命名文件为0。
- 新增/相关专项测试30项全部通过；安全全仓回归精确排除四个Locked/Experiment模块后最终干净运行`Ran 929 tests`、`OK (skipped=3)`。首次同范围运行曾有既有`current_sample_snapshot`在Windows临时目录`os.replace`遇到一次ACL拒绝，原用例独立重跑及随后全量重跑均通过。`compileall`通过。

## 已知问题与阻塞

- 当前样本不是历史PIT中证800；该零交易结果不证明Alpha有效、统计显著或可交易，也不得据此调参。
- `market_data.v2=not implemented`
- `Experiment V3 loader=blocked`
- `2024-2025 Locked Test=not run`

## 安全状态

- `purpose=business_loop_validation`
- `research_status=heuristic_shadow_baseline`
- `paper_eligibility=false`
- `trade_eligibility=false`
- `real_money_list_allowed=false`
- `automatic_order_submission=false`
- `live_supported=false` / `live_not_supported`
- 该Shadow runner只写本地模拟产物，不导入交易桥、不连接券商、不生成或提交真实订单。

## 待决策

- 无新的策略参数决策；禁止根据本次零交易结果修改冻结方向、权重、Entry/Hold门槛或Exposure阈值。

## 下一步

本阶段业务闭环已完成。后续若要进入正式研究准入、Paper或交易验证，必须单独立项并满足PIT、样本外和执行门禁，不能由本Shadow结果自动升级。

## 建议外部审查范围

按 `STATUS.md → config → Alpha/Exposure → runner → final manifest/summary/ledger/ranking/receipts → tests → DECISIONS.md`，重点尝试推翻未来数据隔离、缺数据排除、D/D+1错位、现金透支、成本重算、Mock冒充、create-only与任何订单路径。
