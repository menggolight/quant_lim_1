# 项目交接状态

> 本文是用于跨 Thread 和外部审查的交接快照，不替代代码、版本化配置、Schema、标准 CLI 产物、manifest 或真实测试证据。策略准入状态以对应受控产物为准。

## 快照元数据

- `as_of`：2026-08-20，Asia/Shanghai
- `branch`：`codex/project-review-20260820`
- `base_commit`：`6638a0fa74ed0de9c16594bdfaf26809498953f9`
- `review_range`：`6638a0f..HEAD`
- `worktree_state`：本交接 commit 完成后为 `clean`；当前仓库仍没有 Git remote，也尚未 push
- `handoff_scope`：将本轮 176 个实际文件级变更拆为 10 个范围明确的本地 commit；不提交 ignored 运行产物、个人数据或本地 SDK/环境，不改变正式数据、策略准入或执行权限

## 当前目标

把截至 2026-08-20 的有效实现、测试、文档和清理结果形成可由外部 Agent 恢复的本地 Git 历史；下一步是确认 GitHub remote 后单独授权 push。策略工程目标仍是补齐 Choice 单源、完整、受控的历史 PIT 数据链；在此之前必须保持 `blocked_missing_pit_data`。

## 本轮完成

- 从基线 `6638a0f` 建立 `codex/project-review-20260820`，按安全边界、Market Data、市场观察、研报审计、Factor Lab、个股诊断、Strategy Workspace、Choice 采集、导航清理和交接协议拆分提交；每组均使用明确路径暂存，没有执行 `git add .` 或 `git add -A`。
- 将此前仅存在于工作树的源码、配置、Schema、文档和测试纳入 10 个可审查 commit；ignored 的市场数据、因子证据、研报主库、Choice SDK/环境、用户持仓和 Obsidian 内容均未进入 Git。
- 提交前扫描 176 个候选文本文件：未发现真实密钥、Token、账号、个人持仓、二进制或生成物；同时移除交接文档中的本机绝对用户路径，并把脱敏测试中的手机号/身份证号形状演示值改为运行时合成。
- 对源码、文档、配置、Schema、测试、ignored/untracked 生成物和本地证据做三路只读依赖审计；没有把“未被默认主线导入”直接等同于“无用”。
- 删除 55 个精确定位的临时/测试目标和 64 个 `__pycache__`；删除前清单为 4,122 个文件、90,380,238 bytes（86.19 MiB）。完整回归与编译检查重新生成的 12 个 `__pycache__` 也已再次移除，最终复核为 0 个。
- 删除已归档且逐对象校验一致的 `.tmp/choice_diag_cache/`；保留 `data/market_data/archives/choice_diag_cache_20260811/`。
- 删除一次性 `MARKET_DATA_V2_GOAL.md`、两个已完成实施 checklist、已被 CLI 明确拒绝的 `strategy_current_universe_input.v1.json` Schema，以及仓内零引用且未导出的 `trading/brokers/read_port.py`。
- 保留研报主库和人工审核材料、A/B 双跑现场、Choice SDK/环境、市场数据与因子证据、非 sample 报告、用户持仓、Obsidian 内容，以及仍有独立 CLI/测试/规格的兼容能力。
- 更新文档与 Schema 导航，并在 [DECISIONS.md](DECISIONS.md) 记录“按证据价值清理、不做宽泛 `git clean`”的长期边界。

## 审查提交范围

| Commit | 范围 |
|---|---|
| `d983e39` | LIVE 永久不支持与 Paper/Shadow 边界 |
| `346ebf0` | Provider-neutral Market Data 与受控证据层 |
| `2ff0446` | 市场观察绑定 validated data batches |
| `cd2818b` | 研报审计 V2、官方真值 receipt 与人工复核包 |
| `118362c` | 预注册 Factor Lab |
| `75a7a96` | 密封个股诊断观察卡 |
| `df3f353` | 质量成长 Strategy Workspace 核心 |
| `0a52b83` | Choice 诊断采集与当前样本适配器 |
| `e49d395` | 导航同步与已核验废弃文件清理 |
| `HEAD` | 外部审查交接协议、状态和决策记录 |

外部审查使用 `6638a0f..HEAD`；上述短哈希用于定位，不单独证明测试、来源认证、统计有效性或准入状态。

## 关键变更文件

- `research/market_data/`
- `research/factor_lab/`
- `research/strategy_workspace/`
- `research/broker_report_audit/`
- `trading/`
- `configs/` 与 `schemas/` 中对应版本化契约
- `docs/STATUS.md`
- `docs/DECISIONS.md`
- `docs/README.md`
- `schemas/README.md`
- 删除：`MARKET_DATA_V2_GOAL.md`
- 删除：`docs/superpowers/plans/2026-07-03-deepvan-daily-agent.md`
- 删除：`docs/superpowers/plans/2026-07-07-obsidian-quant-loop.md`
- 删除：`schemas/strategy_current_universe_input.v1.json`
- 删除：`trading/brokers/read_port.py`

当前业务真源仍包括：

- [策略工作区说明](STRATEGY_WORKSPACE.md)
- [质量成长政策](../configs/strategy_quality_growth.v1.json)
- [ExperimentSpec v2 Schema](../schemas/strategy_experiment.v2.json)
- `data/tmp/strategy-workspace/quality-growth-v1/current_status.v6.json`（本地受控状态产物，已被 `.gitignore` 忽略）

## 验证证据

- 清理前完整回归基线：`<bundled-python> -m unittest discover -s tests -v`，退出码 `0`，`Ran 637 tests in 149.512s`，`OK (skipped=2)`。
- 删除前 PowerShell 精确目标盘点：逐项解析并校验目标仍位于仓库内，退出码 `0`；55 个精确目标、64 个 `__pycache__`、4,122 个文件、90,380,238 bytes。
- 受影响专项：`<bundled-python> -m unittest tests.test_strategy_workspace_quality_cli tests.test_paper_validation_run tests.test_project_handoff_docs -v`，退出码 `0`，`Ran 8 tests in 0.205s`，`OK`。
- 清理后完整回归：`<bundled-python> -m unittest discover -s tests -v`，退出码 `0`，`Ran 637 tests in 151.344s`，`OK (skipped=2)`。
- 提交范围与隐私修正后的最终完整回归：`<bundled-python> -m unittest discover -s tests -v`，退出码 `0`，`Ran 637 tests in 110.852s`，`OK (skipped=2)`。
- 脱敏夹具专项：`<bundled-python> -m unittest tests.test_market_data_choice.ChoiceProviderTests.test_sdk_error_redacts_choice_activation_identifiers -v`，退出码 `0`，`Ran 1 test in 0.049s`，`OK`。
- 编译检查：`<bundled-python> -m compileall -q agent research trading integrations tests`，退出码 `0`；检查后重新移除测试生成的源码目录 `__pycache__`。
- 最终交接结构测试：`<bundled-python> -m unittest tests.test_project_handoff_docs -v`，退出码 `0`，`Ran 4 tests in 0.003s`，`OK`。
- 根 README、文档导航、状态/决策与 Schema README 的本地相对链接检查：退出码 `0`；删除项反向引用检查仅命中本文件的删除记录和被保留的 Obsidian 最近文件历史，不存在运行时依赖。
- `git diff --check`：退出码 `0`；仅报告既有 LF/CRLF 工作区提示，没有空白错误。最终磁盘复核为 `.tmp/` 1,825.66 MiB、`data/tmp/` 12.04 MiB，且 0 个 `__pycache__`。
- 未配置独立 lint 命令，因此不声明 `lint passed`。

## 已知问题与阻塞

- 受控状态产物仍为 `formal_status=blocked_missing_pit_data`，且 `paper_eligibility=false`、`trade_eligibility=false`、`real_money_list_allowed=false`。
- 正式研究仍缺中证 800 全收益基准契约、PIT 一级行业与流通市值、历史 ST/停复牌/涨跌停、首披财务字段，以及受控 Top Decile 与 Top2 价格包。
- 前向 Paper 还缺受控信号适配器、官方受控日历、日频 PIT NAV/回撤、粘滞回撤冻结与退出重试，以及 Stage A 证书标准产物验证。
- `.tmp/` 仍主要保存研报主数据库、人工审核材料、Choice 环境和可复核运行证据；体积大不等于无用。本轮也保留了约 192.27 MiB 的 A/B 或 replay 双目录，因为目录并存仍承载确定性审计语义。
- 两个访问被拒的运行目录未被读取或删除；当前 Choice SDK/venv 仍用于受控数据接入，也未清理。
- 研报审计、Factor Lab、DeepVan、市场观察、旧 Paper/Shadow 等不属于默认策略主线，但仍有独立 CLI、测试、规格或冻结反证；若要进一步瘦身，必须按完整 capability slice 单独退役。
- 当前实现已经形成本地 commits，但仓库没有 Git remote 且尚未 push；GitHub 和 ChatGPT 网页版暂时仍看不到这些内容。

## 安全状态

- 研究状态：`blocked_missing_pit_data`。
- Paper：当前未准入。
- Shadow：只读且必须通过来源、账户绑定与完整性门禁。
- LIVE：永久 `live_not_supported`；配置、枚举、白名单、Token 或 readiness 均不能解锁。
- `data/portfolio/`、非 sample 报告/actions/signals、Obsidian 内容和未跟踪源码均保留；本轮没有使用宽泛通配删除或 `git clean`。

## 待决策

1. 是否明确退役某一整项兼容能力；若是，需要逐项迁移或删除其 CLI、配置、Schema、文档、测试和历史产物。
2. 是否在导出独立、可校验的确定性证明后，去重当前约 192.27 MiB 的 A/B/replay 双目录。
3. 确认 GitHub repo/remote；remote 变更、push 与 Draft PR 必须分别获得明确授权。

## 下一步

1. 若继续瘦身，先由用户选择要退役的 capability slice，再做可恢复备份和成组迁移；不按目录年龄或引用次数猜测。
2. 创建或确认私有 GitHub repo 后，设置 remote，并在用户单独授权后 push `codex/project-review-20260820`；需要网页端集中审查时再创建 Draft PR。
3. 继续按受控适配器顺序补齐 PIT 数据门；数据门通过前不得宣称正式回测、Paper 准入或真实资金候选。

## 建议外部审查范围

按 `STATUS.md -> 6638a0f..HEAD -> 关键配置/Schema -> tests -> DECISIONS.md` 恢复上下文。优先审查：LIVE 是否不可解锁、来源与 PIT 门禁能否被伪造、Strategy Workspace 是否跨级宣称正式准入、被删除项是否已有替代或 Git 历史，以及 637 项完整回归是否覆盖关键失败模式。不要根据 commit、哈希或磁盘空间变化提升任何研究或交易状态。
