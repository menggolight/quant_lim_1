# 项目交接状态

> 本文是用于跨 Thread 和外部审查的交接快照，不替代代码、版本化配置、Schema、标准 CLI 产物、manifest 或真实测试证据。研究与执行准入仍以各自受控真源为准。

## 快照元数据

- `as_of`：`2026-08-24T16:58:28+08:00`，Asia/Shanghai
- `branch`：`codex/project-review-20260820`
- `base_commit` / 当前 `HEAD`：`b473d62f50c15f635e1aebdf5110eb3e63191e12`
- `review_range`：`b473d62..worktree`
- `worktree_state`：dirty；本轮只包含 Choice 到期访问边界、独立 Tushare capability probe、Schema/配置/测试及对应文档，不清理或覆盖用户既有文件
- `remotes`：`origin=https://github.com/menggolight/quant_lim.git`；`review=https://github.com/menggolight/quant_lim_1.git`
- Git 写操作：用户已明确授权将本轮范围形成一个独立 commit；本快照记录提交前状态，尚未 push、未创建 PR、未修改 remote

## 当前目标

本阶段已由用户验收，开发范围冻结。当前只允许：把已核对的探针准备变更形成独立 commit，重新运行离线 plan，并在用户本机当前进程配置 `TUSHARE_TOKEN` 后执行一次有界 live probe 随即停止。不得继续实现 `market_data.v2`、正式 Tushare Provider、历史回填、模型重训或 Experiment V3 loader；任何结果仍不得进入 Factor Registry、Experiment V3、Daily publication、Alpha、Paper、交易或 LIVE。

## 本轮完成

- 新增严格、版本化的 Provider Access Policy。Choice 固定为 `expired`：新网络访问、historical backfill、diagnostic fetch/session 和 SDK import/start 前均以结构化 `provider_access_expired` 失败关闭；正式离线研究消费同样被阻断。历史 raw、quarantine、validated、诊断和归档证据未删除、未覆盖，诊断重放只保留证据完整性用途。
- Choice 到期或策略损坏是全局停止条件，不触发 Tushare、BaoStock、AKShare 或 Eastmoney 自动替换，也不允许字段级、行级或半批次 fallback。相关 CLI 输出改为 create-only。
- 新增独立 Tushare capability domain 与 CLI；现有 [TushareProvider V1](../research/market_data/providers/tushare.py) 和 `configs/market_data.v1.json` 未改为正式 Tushare 主源。
- 探针代码内固定 22 个只读 endpoint 映射，配置冻结 37 次 Tushare 小样本调用和 1 次 BaoStock 独立日线交叉核验预留，总计 38，低于全局 40 次上限；最小间隔 3 秒，连续 3 次限频或一次全局权限错误即停止。
- 默认 `--plan` 不读 Token、不导入 SDK、不联网；只有 `--live` 读取 `TUSHARE_TOKEN`。缺 Token、SDK 缺失、权限、限频、网络、Schema 与 payload 错误均形成结构化 endpoint 状态。
- SDK、C 标准句柄、文件描述符、子进程和预绑定 logging 输出均在调用边界内捕获；Token 不进入 stdout/stderr、异常、raw、manifest、receipt、配置、测试夹具或实现哈希。
- 真实产物只允许写入 `data/tmp/tushare-capability/<probe_run_id>/`。run 目录和文件 create-only；拒绝路径逃逸、symlink、junction、reparse、Windows 设备名、尾随点与大小写别名；receipt 最后写入，中断目录因缺 receipt 保持 incomplete。
- 每个 endpoint raw 均绑定 SHA-256；receipt 通过严格 Schema、exact-type Python 语义与 raw replay。实现 bundle 同时绑定探针、domain、访问策略、Schema、配置、MarketData contract、错误清洗、BaoStock provider 与验证器。
- BaoStock 对比不合并、不补值、不设自动阈值。其原始 provider bytes 必须由现有 `replay_baostock_raw` 重放，派生 records、单位转换与逐字段差异均从 raw 复算。
- Receipt 新增 exact-type `CrossValidationOutcomeV1`，绑定 daily endpoint result、比较载荷和两侧 raw。`integrity_scope=local_consistency_not_external_authentication`、`admission_effect=none`；本地自哈希不等于外部签名或来源认证。
- 新增迁移决策矩阵和稳定决策 `D-20260824-02`。所有未实测 endpoint 继续标为 `unknown/not_run`，不能写成正式 `primary` 或 `admitted`。

## 关键变更文件

- 访问边界：[Provider Access Policy](../configs/provider_access.v1.json)、[Policy Schema](../schemas/provider_access_policy.v1.json)、[加载与门禁](../research/market_data/provider_access.py)
- Choice 接线：[Provider base](../research/market_data/providers/base.py)、[Choice Provider](../research/market_data/providers/choice.py)、[Choice index](../research/market_data/providers/choice_index.py)、[Registry](../research/market_data/registry.py)
- Tushare 探针：[配置](../configs/tushare_capability_probe.v1.json)、[domain contract](../research/market_data/tushare_capability.py)、[CLI](../agent/tushare_capability_probe.py)
- Schema：[endpoint result](../schemas/tushare_endpoint_result.v1.json)、[capability receipt](../schemas/tushare_capability_receipt.v1.json)
- 测试：[Choice expiry](../tests/test_choice_expired_access.py)、[Tushare contract](../tests/test_tushare_capability_contract.py)、[Tushare probe](../tests/test_tushare_capability_probe.py)
- 文档：[Market Data](MARKET_DATA.md)、[Tushare 迁移边界](TUSHARE_MIGRATION.md)、[决策记录](DECISIONS.md)

## 验证证据

- Git 起点检查：分支为 `codex/project-review-20260820`，HEAD 为完整 `b473d62f...`；`git status --short` 为本轮 dirty 范围；两个 remote 如快照元数据所列。
- 三个新增专项：bundled Python 执行 `python -m unittest tests.test_choice_expired_access tests.test_tushare_capability_contract tests.test_tushare_capability_probe -v`；`Ran 79 tests in 53.377s`，退出码 0，`OK (skipped=1)`。唯一 skip 是当前 Windows 环境不能创建目录 symlink；junction/reparse 负向测试已实际通过。
- 离线计划：`python -m agent.tushare_capability_probe --config configs/tushare_capability_probe.v1.json --plan`，退出码 0；22 endpoint、37 次 Tushare + 1 次 BaoStock 预留、总计 38/40，且 `credential_accessed=false`、`sdk_imported=false`、`network_accessed=false`。
- Token 检查：`TUSHARE_TOKEN_STATUS=not_configured`；真实 `--live` 为 `not_run_token_missing`，没有网络请求或本地真实 receipt。
- 安全全仓回归精确排除 `test_strategy_workspace_admission.py`、`test_strategy_workspace_evaluation.py`、`test_strategy_workspace_experiment.py`、`test_strategy_workspace_top_decile_backtest.py` 四个模块。首次运行 `Ran 861 tests in 142.487s`，因 Windows 临时目录 `os.replace` 的一次 `WinError 5` 退出码 1；该单项随后 `Ran 1 test`、退出码 0。原命令完整复跑 `Ran 861 tests in 144.959s`，退出码 0，`OK (skipped=3)`。
- `python -m compileall -q agent research trading operations integrations tests`：退出码 0。
- `git diff --check`：退出码 0；只有 Windows LF→CRLF 提示，无 whitespace error。
- 交接文档专项：`python -m unittest tests.test_project_handoff_docs -v`，`Ran 4 tests in 0.004s`，退出码 0，`OK`。
- 本轮 10 份 Markdown 相对链接检查：`ALL_RELATIVE_LINK_TARGETS_EXIST`，退出码 0。
- 最终范围与秘密结构检查：禁止修改的 Experiment/Daily/Alpha/TushareProvider V1/`market_data.v1` 文件命中 0，`data/` 变更 0，配置与 Schema 的 Token/Token hash/长度/前后缀字段命中 0；`market_data.v2` 与正式 `tushare_pro_v2.py` 均不存在。
- **not run**：上述四个 Locked/Experiment 模块及包含它们的无排除 discover；未读取、未运行、未解释真实 2024—2025 Locked Test 结果。
- **not run**：真实 Tushare 网络探针、历史回填、正式 MarketData admission、Alpha/组合生成、Paper、券商、真实资金和 LIVE。

## 已知问题与阻塞

- `TUSHARE_TOKEN` 当前未配置，所以 22 个 endpoint 的账户权限、真实字段、历史覆盖、行数限制、PIT 语义和 BaoStock 差异均仍是 `unknown/not_run`。
- 中证 800 全收益候选、历史指数成分完整性、SW2021 与 CSI 行业体系映射、财务首披链、修订版本和单位口径均未被真实数据证明。
- 本地 receipt 可证明当前证据包内部闭合；没有外部签名或不可变日志时，不能证明具有工作区写权限的人从未全量改写整个历史证据包。该限制不影响 fail-closed：任何 capability 或失败声明都不产生准入效力。
- Choice 旧证据的许可证后续使用边界仍待人工确认；在此之前禁止新的正式研究消费。
- `market_data.v2 not implemented`
- `Tushare formal provider not implemented`
- `Experiment V3 loader blocked`
- `Locked Test not run`
- `Paper not admitted`
- `trade not admitted`
- `real money not allowed`
- `LIVE not supported`

## 安全状态

- 工程状态：`choice_expired_tushare_probe_engineering_verified_live_not_run`
- 数据状态：`capability_unknown_not_run_token_missing`
- 正式数据准入：`formal_data_admission=false`
- Experiment V3 影响：`none`；formal loader 仍为 `blocked_not_implemented`
- Daily signal authority：`none`；不产生 Alpha BUY、Next-session 或订单
- Paper：`paper_eligibility=false`
- 交易：`trade_eligibility=false`
- 真实资金：`real_money_list_allowed=false`
- 自动下单：`automatic_order_submission=false`
- LIVE：永久 `live_not_supported`

## 待决策

1. 一次配置内固定的 38 次有界只读 live probe 已获用户授权；当前唯一执行前置条件是用户在本机当前进程配置 `TUSHARE_TOKEN`，不得通过聊天传递或写入仓库。
2. 只有真实 receipt 存在后，才逐 dataset 决定哪些接口进入未来 `market_data.v2`、哪些只作独立核验、哪些继续阻断。
3. SW2021 是否替代或仅补充 CSI 行业体系，以及全收益基准缺失是否继续全局阻断，均需基于真实覆盖证据另立版本化决策。
4. 若要提高历史证据的不可变性，需要选择外部签名、受控对象存储或不可变日志；当前本地自哈希不承担该证明。

## 下一步

1. 先以逐路径暂存形成独立 commit `feat: add fail-closed Tushare capability probe`，明确排除 `data/tmp/tushare-capability/`、真实 Token 和真实原始响应；然后复跑离线 plan。
2. 用户本机当前进程设置 Token 后执行一次 `--live --output-root data/tmp/tushare-capability`；到 receipt 生成并重放通过即停止，不自动进入 Provider 改造。
3. 按真实 receipt 更新迁移矩阵：逐 endpoint 报告 passed、denied、partial 或 unknown，并检查 PIT、主键、版本、单位、全收益基准及行业体系。
4. 另立 Market Data V2 决策和实现范围；在此之前 BaoStock 默认主源、Tushare V1 日线核验职责、Experiment V3、Paper、交易和 LIVE 状态全部保持不变。

## 建议外部审查范围

按 `STATUS.md -> b473d62..worktree -> provider_access.v1 + Schema -> Choice guards/Registry -> Tushare config/domain/CLI + receipt Schema -> 三个新增测试 -> TUSHARE_MIGRATION.md -> DECISIONS.md` 恢复上下文。重点核对 SDK 前置阻断、无 fallback、Token 输出与持久化边界、固定 endpoint/参数/请求预算、raw replay、create-only/reparse 防护、receipt 的本地一致性语义，以及任何把 capability evidence 表述成正式 admission 的越级路径。
