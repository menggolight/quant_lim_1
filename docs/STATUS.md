# 项目交接状态

> 本文是带时点的交接快照，不替代代码、配置、受控状态、标准 CLI 产物或 manifest。

## 快照元数据

- `as_of`：`2026-08-27T14:46:46+08:00`，Asia/Shanghai。
- `branch`：`codex/project-review-20260820`。
- `remote_base`：`d7e17302b48d7db79e2456532fe9eb71413fb278`（`review/codex/project-review-20260820`）。
- `local_HEAD_at_snapshot`：`d7e17302b48d7db79e2456532fe9eb71413fb278`；原未推送文档提交 `d8a3821` 已解提交，以便严格重组为 Commit A/B。
- `worktree_state`：dirty；Commit A 候选仅为 `operations/run_technical_shadow_daily.py`、`tests/test_technical_shadow_daily.py` 和本文。Commit B 候选为独立 retrospective runner、测试及后续本文增量。`docs/DECISIONS.md` 的一行 Tushare 目录补项是无关遗留，明确不纳入两次提交。
- 自动任务 `Technical Shadow 0827 前向验收`：`PAUSED`；尚未运行、未启用。

## 当前目标与能力边界

冻结 `a-share-technical-shadow-mvp-v1` 的 Alpha 公式、Exposure 阈值、组合和成本规则，在不补造历史交易、不自动下单的前提下，把每日运行改成可迁移、可恢复、create-only、轻量 readiness 和有界 `--when-ready` 的前向 Shadow 运营入口。

该策略仍固定为：

- `purpose=business_loop_validation`
- `research_status=heuristic_shadow_baseline`
- `paper_eligibility=false`
- `trade_eligibility=false`
- `real_money_list_allowed=false`
- `automatic_order_submission=false`
- `live_supported=false` / `live_not_supported`

## Commit A 候选：每日前向运营化

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

## 真实状态与运行证据

- 正式持久状态当前只有 `data/portfolio/technical-shadow-daily/2026-08-25/`：现金/NAV `10000.00/10000.00`、持仓 `{}`、Exposure `RISK_OFF`；manifest SHA-256 `84f9cb34757c15131047cdd27dc1ad4972dfe58975b4c462b3e2e34b039037d7`。
- `2026-08-26` 没有正式状态槽，也没有及时前向计划；只存在隔离 `retrospective_replay`，其 `forward_evidence=false`、`state_mutation_allowed=false`，不得并入前向账户或策略绩效。
- `2026-08-27` 前向验收：`not run`。没有 `2026-08-27` 正式状态槽；一次性任务仍为 `PAUSED`。
- 已有10/20日真实 BaoStock 回放、120日 Exposure 诊断及自然 BUY/SELL 路径保持不变；它们仅证明业务闭环和模拟执行可复核，不证明正式 Alpha 或可交易性。

## 当前验证证据

- 状态缺口定向测试：`test_missed_session_carry_forward_preserves_account_and_allows_current_plan`，`Ran 1 test`，退出码0，`OK`。
- Technical Shadow 全专项：`python -m unittest discover -s tests -p "test_technical_shadow*.py" -v`，`Ran 48 tests in 3.623s`，退出码0，`OK`。
- 安全全仓回归：先枚举86个 `test_*.py` 模块，精确排除 `test_strategy_workspace_admission`、`test_strategy_workspace_evaluation`、`test_strategy_workspace_experiment`、`test_strategy_workspace_top_decile_backtest`，实际运行82个模块；`Ran 947 tests in 158.123s`，退出码0，`OK (skipped=4)`。四个禁止模块未导入、未运行。
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

## 下一步与审查范围

Commit A 仅审查 daily runner、daily tests 和本文对应部分；Commit B 再单独审查 retrospective runner、retrospective tests、隔离产物契约和本文 retrospective 增量。推送前逐 commit 执行 cached diff 检查，并确认 `data/tmp/`、`data/portfolio/`、任务卡、缓存及无关 `DECISIONS.md` 均未进入 Git。
