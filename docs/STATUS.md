# 项目交接状态

> 本文是带时点的交接快照，不替代代码、配置、受控状态、标准 CLI 产物或 manifest。

## 2026-08-28 — Technical Momentum 正式数据与验证 P0

### 本轮时点与工作树

- `as_of`：`2026-08-28T12:18:46+08:00`，Asia/Shanghai。
- `branch`：`codex/project-review-20260820`。
- `baseline`：`fe44495e95993147294ba296fe0ba6aa7091e280`，与任务指定基线一致。
- `worktree_state`：dirty；本节所列 P0 文件待形成范围明确的本地 commit。用户既有 `docs/DECISIONS.md` 改动未读取为本轮决策、未修改且明确排除在提交之外。
- `locked_test_status=NOT_RUN`；`locked_test_consumed=false`。本轮未读取、运行或解释 2024—2025 Locked Test 数据或结果。

### 当前目标与完成内容

当前唯一正式研究主线为 `a-share-technical-momentum-adaptive-v1`。质量成长线暂停但保留；Technical Shadow 继续作为既有每日业务闭环观察，本轮没有扩建或修改其历史产物。

- 新增[正式实验配置](../configs/a_share_technical_momentum_adaptive.v1.json)，冻结既有六因子 Alpha、Exposure 阈值、组合参数和 Development/Validation/Locked Test 切分；没有新增因子、权重或阈值。
- 新增[技术正式数据契约与实现说明](TECHNICAL_MOMENTUM_FORMAL_P0.md)、四个 JSON Schema、双价格实现、PIT 中证800 loader、执行状态/成本/回测内核和 fail-closed 报告器。
- `signal_return_series` 只以当日可见的未复权收盘价和当期 `adj_factor` 计算公司行动调整收益；`execution_price_series` 始终使用真实未复权 OHLC。测试覆盖除息、未来因子泄漏、价格通道混用和持仓公司行动会计拒绝。
- PIT loader 要求 `index_weight` 月截面、严格先于决策日、800只唯一成员、Decimal 权重及权重和；日期、重复、缺月、粗精度和当前成分回填均失败关闭。
- 执行内核覆盖停牌、涨跌停、ST 禁新买、上市/退市、T+1、残仓、100股整手、最低佣金、印花税、过户费和滑点；全部输入必须先通过恰止于 split end 的分区元数据门，越界输入在首行迭代前拒绝；缺少受控退市终值或现金/送转股 entitlement 时拒绝回测，不猜测价格或现金。
- 标准入口为 `python -m operations.run_technical_formal`。当前仅生成 create-only coverage、Development/Validation 和 Locked Test readiness 三份自校验报告；调用方布尔值、来源字符串、哈希或自报指标均不能提升准入。

### 当前正式证据与结论

- [dataset coverage report](technical_momentum_p0/20260828T121846+0800/dataset_coverage_report.json)：要求区间 `2018-01-01..2025-12-31`，信号预热从 `2017-07-01`；现有证据只是 Tushare 标准非 VIP 接口能力探针，不是正式批次。PIT 中证800仅有 `2024-01-31` 单截面，缺 `2017-12..2023-12` 与 `2024-02..2025-12`。
- [Development/Validation report](technical_momentum_p0/20260828T121846+0800/development_validation_backtest_report.json)：两个区间均为 `NOT_RUN_BLOCKED`，没有用合成数据生成正式业绩指标。
- [Locked Test readiness report](technical_momentum_p0/20260828T121846+0800/locked_test_readiness_report.json)：`verdict=BLOCKED`、`locked_test_status=NOT_RUN`、`locked_test_consumed=false`。
- 关键阻塞：九类数据尚无完整、受控、标准 CLI 校验的历史批次；PIT 成分历史不完整；仅凭标量 `adj_factor` 无法唯一拆分现金分红与送转/配股 entitlement；退市残仓缺受控终值；正式数据校验器与受控回测运行器尚未接入，因此 Development/Validation 不得运行并声称正式结果。
- 安全状态保持：`paper_eligibility=false`、`trade_eligibility=false`、`real_money_list_allowed=false`、`automatic_order_submission=false`、`live_supported=false`。未运行 Paper，未连接券商，未下单。

### 实际验证

- P0 定向：`python -m unittest tests.test_technical_formal_data tests.test_technical_formal_backtest tests.test_technical_formal_reporting tests.test_technical_momentum_experiment -v`，`Ran 54 tests`，退出码0，`OK`。
- Technical 全专项：枚举 `test_technical*.py` 模块运行，`Ran 102 tests`，退出码0，`OK`。
- 安全全仓回归：枚举测试模块并在导入前精确排除 `test_strategy_workspace_admission`、`test_strategy_workspace_evaluation`、`test_strategy_workspace_experiment`、`test_strategy_workspace_top_decile_backtest`；补丁前首次 `Ran 1009 tests` 后因 Windows 临时目录 rename 的瞬时 `PermissionError` 退出码1，单测复跑通过，随后同集重跑退出码0；完成全输入物理分区零读取补丁后又从头运行最终86个安全模块，`Ran 1010 tests in 245.787s`，退出码0，`OK (skipped=4)`。四个 Locked Test 相关模块始终未导入、未运行。
- `python -m compileall -q agent research trading operations integrations tests`：退出码0。
- 四个新增 Schema、实验配置和 capability evidence 共6个源 JSON 文件解析：退出码0；三份交付报告的 Schema、自哈希和相互哈希绑定复核：`OK`。
- `git diff --check`：退出码0；仅有既有 Windows 行尾转换提示。

### 建议审查范围与下一步

建议按本节 → [P0 规格与边界](TECHNICAL_MOMENTUM_FORMAL_P0.md) → 配置/Schema → `technical_formal_data.py` → `technical_formal_backtest.py` → `technical_formal_reporting.py` → 四个专项测试 → 三份报告的顺序审查。下一步只能先补齐并由标准 CLI 验证正式历史数据、公司行动 entitlement 和退市终值，再运行 Development/Validation；在 readiness 为 `BLOCKED` 时不得触碰 Locked Test。

## 快照元数据

- `as_of`：`2026-08-27T14:59:47+08:00`，Asia/Shanghai。
- `branch`：`codex/project-review-20260820`。
- `remote_base`：`d7e17302b48d7db79e2456532fe9eb71413fb278`（`review/codex/project-review-20260820`）。
- `local_HEAD_at_snapshot`：`20fc3818fa1a548724144e97a86efa7541c2f3b9`（Commit A：`feat: add persistent technical shadow forward operations`）。
- `worktree_state`：dirty；目标改动仅为 Commit B 候选 `operations/run_technical_shadow_retrospective.py`、`tests/test_technical_shadow_retrospective.py` 和本文增量。`docs/DECISIONS.md` 的一行 Tushare 目录补项是无关遗留，明确不纳入提交。
- 自动任务 `Technical Shadow 0827 前向验收`：`PAUSED`；尚未运行、未启用。

## 当前目标

冻结 `a-share-technical-shadow-mvp-v1` 的 Alpha 公式、Exposure 阈值、组合和成本规则，在不补造历史交易、不自动下单的前提下，把每日运行改成可迁移、可恢复、create-only、轻量 readiness 和有界 `--when-ready` 的前向 Shadow 运营入口。

该策略仍固定为：

- `purpose=business_loop_validation`
- `research_status=heuristic_shadow_baseline`
- `paper_eligibility=false`
- `trade_eligibility=false`
- `real_money_list_allowed=false`
- `automatic_order_submission=false`
- `live_supported=false` / `live_not_supported`

## 本轮完成

### Commit A：每日前向运营化

- 将账户状态真源与临时报表分离：默认持久状态根为 `data/portfolio/technical-shadow-daily/`，临时报表根为 `data/tmp/technical-shadow-daily/`；两者必须互斥。
- 提供显式一次性迁移入口，只接受已验证的 `2026-08-25` 旧槽，逐文件哈希复制并保留来源 lineage；不自动迁移、删除或覆盖用户数据。
- 持久槽固定包含 `state.json`、`next_session_plan.json`、`prior_plan_application.json`、`lineage.json` 和最后写入的 `manifest.json`；状态、计划、前驱和报告 manifest 全部互相绑定。
- readiness 阶段只读取交易日历和中证800基准，输出 `DATA_READY`、`DATA_NOT_READY` 或 `ALREADY_PROCESSED`；只有 ready 后才允许一次完整60股采集。
- `--when-ready` 使用有界轮询、显式 deadline、正数轮询间隔和最大次数；deadline 或次数用尽后停止，不进入无限循环。
- 同日同输入返回 `idempotent_existing`，不改变字节、mtime 或状态；同日不同输入、部分槽、篡改、非相邻前驱和采集期间 head 变化均失败关闭。
- 对 `2026-08-25` 空仓 `NO_ACTION_CASH` 到 `2026-08-27` 的唯一允许缺口，不创建 `2026-08-26` 正式状态槽；在 `2026-08-27/prior_plan_application.json` 记录：
  - `status=MISSED_SESSION_CARRY_FORWARD`
  - `missed_session_date=2026-08-26`
  - `orders=[]`、`fills=[]`、`ledger_fills=[]`
  - `generated_late=true`
  - `forward_evidence=false`
  - `state_carry_forward=true`
  - 现金、持仓、NAV及累计成本原样延续
- carry-forward 不执行旧计划、不重新初始化1万元账户、不补造开盘成交；随后允许 `2026-08-27` 使用真实收盘数据生成绑定下一官方交易日且窗口为 `OPEN` 的新前向人工计划。
- 会计口径保持：`reference_price_gross_pnl=77.00`、`slippage_cost=4.00`、`execution_price_gross_pnl=73.00`、`explicit_fee=15.80`、`net_pnl=57.20`；只允许 `77.00-4.00-15.80=57.20` 或 `73.00-15.80=57.20`，不得重复扣滑点。

### Commit B 候选：隔离回溯决策

- 新增独立 `retrospective_replay` 入口，固定 `strategy_date=2026-08-26`、`execution_date=2026-08-27`，从已验证的 `2026-08-25` 正式状态读取账户和 Exposure 迟滞状态；不创建账户、不写正式状态、不并入前向账本或策略绩效。
- 决策采集截止日物理限制为 `2026-08-26`；Alpha、Exposure、组合和人工计划完成并固化哈希后，才允许独立查询 `2026-08-27` 真实开盘用于 retrospective execution，禁止把 D+1 信息反馈到决策。
- 输出到 create-only 的 `data/tmp/technical-shadow-retrospective/2026-08-26/<run_id>/`，固定包含 data receipt、完整 ranking、Exposure、portfolio decision、独立 execution、Markdown report 和 manifest。
- 所有产物固定 `strategy_signal=false`、`alpha_evidence=false`、`forward_evidence=false`、`trade_recommendation=false`、`paper_eligibility=false`、`trade_eligibility=false`、`real_money_list_allowed=false`、`automatic_order_submission=false`、`state_mutation_allowed=false`、`live_supported=false`。
- 本次真实 BaoStock 回溯运行结果：Exposure `RISK_OFF`、目标总仓位 `0.0`、`BUY/SELL/HOLD/CASH=0/0/0/1`；虽有6只 Alpha entry 候选，但 `benchmark_trend=-0.036352` 命中 `risk_off_rule`，因此 `simulated_fills=[]`、`execution_result=NO_ACTION`。
- 真实输出目录为 `data/tmp/technical-shadow-retrospective/2026-08-26/20260827T114520+0800-7f6daa74/`，manifest 文件 SHA-256 为 `b16b38fa32ea276c8ff26a96178896eee7397b0092469b885f42220c8c89894a`。正式状态树运行前后 SHA-256 均为 `97349797e8a88cbea6221d880005e0b729a5c1d19b7a0898846bafcf7bac19bd`，`formal_state_chain_modified=false`。
- 产物在 `2026-08-27T11:45:20+08:00` 生成时，完整收盘数据尚不可用，因此 `close_valuation_status=PENDING`；这是生成时点证据，不外推为当前行情结论。
- 无需新增共享模块；Commit B 复用 Commit A 的受控状态验证入口及已有 daily 纯计算辅助函数，避免扩大变更面。

## 关键变更文件

- [每日前向运营入口](../operations/run_technical_shadow_daily.py)
- [每日前向运营专项测试](../tests/test_technical_shadow_daily.py)
- [隔离回溯入口](../operations/run_technical_shadow_retrospective.py)
- [隔离回溯专项测试](../tests/test_technical_shadow_retrospective.py)
- [冻结 Technical Shadow 配置](../configs/a_share_technical_shadow_mvp.v1.json)

## 真实状态与运行证据

- 正式持久状态当前只有 `data/portfolio/technical-shadow-daily/2026-08-25/`：现金/NAV `10000.00/10000.00`、持仓 `{}`、Exposure `RISK_OFF`；manifest SHA-256 `84f9cb34757c15131047cdd27dc1ad4972dfe58975b4c462b3e2e34b039037d7`。
- `2026-08-26` 没有正式状态槽，也没有及时前向计划；只存在隔离 `retrospective_replay`，其 `forward_evidence=false`、`state_mutation_allowed=false`，不得并入前向账户或策略绩效。
- `2026-08-27` 前向验收：`not run`。没有 `2026-08-27` 正式状态槽；一次性任务仍为 `PAUSED`。
- 已有10/20日真实 BaoStock 回放、120日 Exposure 诊断及自然 BUY/SELL 路径保持不变；它们仅证明业务闭环和模拟执行可复核，不证明正式 Alpha 或可交易性。

## 验证证据

- 状态缺口定向测试：`test_missed_session_carry_forward_preserves_account_and_allows_current_plan`，`Ran 1 test`，退出码0，`OK`。
- Technical Shadow 全专项：`python -m unittest discover -s tests -p "test_technical_shadow*.py" -q`，`Ran 48 tests in 3.873s`，退出码0，`OK`。
- 安全全仓回归：先枚举86个 `test_*.py` 模块，精确排除 `test_strategy_workspace_admission`、`test_strategy_workspace_evaluation`、`test_strategy_workspace_experiment`、`test_strategy_workspace_top_decile_backtest`，实际运行82个模块；首次运行发现本文缺少固定交接标题并失败，修复标题契约后从头重跑，最终 `Ran 947 tests in 167.028s`，退出码0，`OK (skipped=4)`。四个禁止模块在两次运行中均未导入、未运行。
- `python -m compileall -q agent research trading operations integrations tests`：退出码0。
- `git diff --check`：退出码0，仅有 LF→CRLF 提示，无 whitespace error。
- Markdown 本地链接检查：43个 tracked/nonignored Markdown 文件，`MARKDOWN_LINKS_OK`；检查标准 inline 本地目标是否存在，外链、纯锚点和 Obsidian wikilink不在此检查范围。

## 已知问题与阻塞

- 当前60只样本是冻结的当前诊断样本，不是历史 PIT 中证800。
- `market_data.v2=not implemented`。
- `Experiment V3 loader=blocked`。
- `2024-2025 Locked Test=not run`，本轮不得运行或解释。
- Tushare 只有单 HTTP `trade_cal` 诊断成功，整体 capability 未判定、未正式准入；本轮未修改 Tushare。
- 正式 PIT、样本外统计、Paper admission、交易和真实资金准入均未完成。

## 安全状态

- `paper_eligibility=false`
- `trade_eligibility=false`
- `real_money_list_allowed=false`
- `automatic_order_submission=false`
- `live_supported=false` / `live_not_supported`
- 本轮没有接入券商、生成自动订单、修改 Alpha/Exposure/组合规则、运行 Locked Test 或提升任何准入。

## 待决策

- 无策略参数或准入决策；`2026-08-27` 前向真实运行仍须等待代码推送固定及一次性任务界面确认“不重复”。

## 下一步

逐 commit 核对、推送至 `review/codex/project-review-20260820` 并验证远端与本地 ahead/behind 为 `0/0`。仅在这些条件满足且任务界面确认“不重复”后，才允许启用一次性前向验收任务。

## 建议外部审查范围

Commit A 已固定为 `20fc3818fa1a548724144e97a86efa7541c2f3b9`，仅审查 daily runner、daily tests 和本文对应部分。Commit B 仅审查 retrospective runner、retrospective tests、隔离产物契约和本文增量。推送前执行 cached diff 检查，并确认 `data/tmp/`、`data/portfolio/`、任务卡、缓存及无关 `DECISIONS.md` 均未进入 Git。远端推送并核验 ahead/behind 为 `0/0` 之前，一次性前向验收任务继续保持 `PAUSED`；`2026-08-27` 前向真实运行仍为 `not run`。
