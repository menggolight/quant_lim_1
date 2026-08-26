# 项目交接状态

> 本文是用于跨 Thread 和外部审查的交接快照，不替代代码、版本化配置、标准 CLI 产物、manifest 或真实运行证据。

## 快照元数据

- `as_of`：`2026-08-26T13:18:47+08:00`，Asia/Shanghai
- `branch`：`codex/project-review-20260820`
- `base_commit` / 当前 `HEAD`：`10d31bb183912440a84390d146a552626ec03755`
- `review_range`：`10d31bb1..worktree`
- `worktree_state`：dirty；本轮范围为独立 Technical Shadow MVP、BaoStock 最低版本/指数映射修复、业务关键测试与本交接文档
- Git 写操作：本轮未 commit、未 push、未修改 remote

## 当前目标

独立策略 `a-share-technical-shadow-mvp-v1` 已使用真实 BaoStock 数据完成最近10个完整交易日的“数据 → 排名 → 仓位 → 买卖计划 → D+1模拟成交 → 连续账本”业务闭环。结果为冻结 Exposure 连续10日 `RISK_OFF`，因此合法零交易、全现金；这只证明业务链可运行，不构成正式研究、Paper、交易或真实资金准入。

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

## 关键变更文件

- [冻结策略配置](../configs/a_share_technical_shadow_mvp.v1.json)
- [Technical Alpha Shadow](../research/strategy_workspace/technical_alpha_shadow_v1.py)
- [Technical Exposure Shadow](../research/strategy_workspace/technical_exposure_shadow_v1.py)
- [业务回放 CLI](../operations/run_technical_shadow_mvp.py)
- [BaoStock代码映射](../research/market_data/providers/baostock.py)
- [业务关键测试](../tests/test_technical_shadow_mvp.py)
- [决策 D-20260826-01](DECISIONS.md#d-20260826-01-隔离technical-shadow-mvp并冻结启发式业务回放)
- [最终真实运行 manifest](../data/tmp/technical-shadow-mvp/20260826T131026+0800-269b6c81/run_manifest.json)
- [最终真实运行 summary](../data/tmp/technical-shadow-mvp/20260826T131026+0800-269b6c81/run_summary.json)

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
