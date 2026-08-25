# 项目交接状态

> 本文是用于跨 Thread 和外部审查的交接快照，不替代代码、版本化配置、Schema、标准 CLI 产物、manifest 或真实测试证据。研究与执行准入仍以各自受控真源为准。

## 快照元数据

- `as_of`：`2026-08-25T18:04:12+08:00`，Asia/Shanghai
- `branch`：`codex/project-review-20260820`
- `base_commit` / 当前 `HEAD`：`3571b417ccbb7bdb8707e95c0372e1024f60fdd9`
- `review_range`：`3571b417..worktree`
- `worktree_state`：dirty；本轮范围为前序single-endpoint P0诊断/postmortem及新授权HTTP-only终态记账、故障恢复测试与对应文档
- Git 写操作：本轮未 commit、未 push、未修改 remote

## 当前目标

接受前序`capability_probe_bug`，但不复用已经关闭的双通道预算。新授权轮次只实现并验证一次可崩溃重放的直接HTTP `trade_cal`：`channel=http`、`max_requests=1`；先提交代码，再只运行一次真实请求，随后仅离线replay并立即停止。禁止SDK、`daily`、22-endpoint probe、`market_data.v2`、正式Tushare Provider、Experiment V3 loader、Locked Test、Alpha、Paper或执行链。

## 本轮完成

- 新增独立HTTP-only runner，生产CLI不提供endpoint、channel、预算、SDK、`daily`或全探针参数；生产live/replay固定到唯一目录`data/tmp/tushare-capability/http-terminal-once/`，目录一旦认领不能通过换根重发。
- create-only canonical事件链固定为`RUN_CREATED -> REQUEST_RESERVED -> NETWORK_CALL_STARTED -> [RESPONSE_RECEIVED] -> TERMINAL`；首事件绑定实际代码/config/policy bundle、Git状态、runtime参数和expected fields，后续事件逐项继承并以前序hash连接。
- 六类计数独立持久化并由Schema/domain共同约束：`response <= started <= reserved`、`remote_execution_unknown=started-response`、`budget_consumed=reserved`、`terminal_result=1`。
- live与replay使用同一跨进程非阻塞锁；busy或已认领的live不自动replay。`session.post`前在锁内从磁盘重载事件链，要求head精确为`NETWORK_CALL_STARTED`且不存在receipt，防止并发分叉形成虚假零请求终态。
- HTTP层固定零retry、禁redirect、TLS验证、30秒timeout和1 MiB响应上限。Python/进程stdout与stderr直接丢弃到OS null device，不使用磁盘临时捕获；Token只在内存wire payload中出现，raw body、upstream message、exception text和Token派生值均不持久化。
- 六个故障点均通过离线恢复：reserve前、reserve后network前、network进入后、response后receipt前、receipt写中断、terminal marker写中断；replay时socket/session被测试显式封锁且HTTP调用数不增加。
- 对抗审查首次发现并修复live/replay并发竞态、可变生产root、回放时元数据漂移、磁盘临时输出捕获及Schema lifecycle漂移；复审确认原5项均关闭且无残余P0。
- 当前只完成离线验证；真实HTTP请求在本commit前明确为`not run`，不得把fake response或测试receipt写成真实接口结果。

### 前序single-endpoint P0与postmortem

- 保留旧 capability probe 代码与真实 receipt，不改写其实现 bundle。旧 receipt 的 37/37 统一失败只判为 `public_entry_diagnostic_insufficient`，不判为 Tushare capability failure。
- 新增独立 single-endpoint diagnostic。默认 plan 不读凭证、不导入 SDK、不联网；live 只允许 `trade_cal` 或可选 `daily`，SDK/HTTP 使用相同 endpoint、字段和语义参数。
- 每通道固定最多一次发送，无 retry、无 redirect，TLS 校验开启，响应体上限 1 MiB；SDK import/init 阶段的隐式 socket/DNS 被进程级 gate 阻断。
- 通道 receipt 只保留 `transport_status`、`http_status`、整数或 null 的 `upstream_code`、固定枚举 `sdk_exception_type`、`sanitized_message_category` 及受控字段结构。结构化 code 优先于 message/HTTP fallback；覆盖 permission、rate limit、authentication/account、invalid parameter、server/internal。
- 结论域固定为 `token_or_account_problem`、`sdk_client_problem`、`network_transport_problem`、`capability_probe_bug` 四类；任何结论都不产生正式数据、Experiment、Daily、Paper 或交易准入。
- 全局 P0 预算采用 create-only 两槽：slot 1 固定 `trade_cal`、slot 2 仅可选 `daily`，每槽保守预留 2 次，总上限 4。标准live入口全程持有跨进程round lock；`daily`还必须先重放slot 1对应的终态`trade_cal` receipt，只有未完成slot或进程硬退出时保持零请求失败关闭。重复或并发调用不能释放、覆盖或重用预算。
- 预算预留后的未捕获 runner failure 会立即写 create-only round-failure marker，并关闭整轮；标准入口在 marker 存在时拒绝 `trade_cal` 与 `daily`。postmortem 必须绑定该 marker，不能只凭 slot 抢先宣称正在运行的进程已失败。
- 真实运行前的离线 plan 通过：`trade_cal` 两通道计划请求数 2、全轮上限 4，`credential_accessed=false`、`sdk_imported=false`、`network_accessed=false`。
- 本轮仅启动一次 `trade_cal` live CLI。slot 1 已保守预留 2 次，进程随后在正式 receipt 发布前返回顶层 `OtherError`；无 retry、无第二次 CLI、无 `daily`、无 22-endpoint probe。由于 `_WireObservation` 只在内存中，实际请求数不能从现场恢复，必须保持 `null`，可信边界仅为 `0..2`。
- 随后的纯本地保守凭证形态预检拒绝了剪贴板输入；这不能证明 Token 或账户失效，也不能回填两个通道的响应。P0 已补为在预算预留、SDK import 和网络之前失败关闭，且不输出任何凭证派生信息。
- 新增独立sealed postmortem V3 contract，完整内嵌并哈希绑定create-only slot、round-failure marker、失败实现bundle和Git状态；实际请求数、runtime语义参数及SDK/HTTP两通道的五项结果均固定为`null + unavailable`，不会用封存时当前配置回填失败进程事实，也不会把预算预留伪造成实际请求。slot的`reserved_request_count`必须精确为正式writer固定值2；V1未绑定marker、V2回填当前参数的历史形状不再由当前verifier接受。
- 当前有效sealed receipt已生成并重放通过：[diagnostic_postmortem.sealed.v3.json](../data/tmp/tushare-capability/diagnostics/20260825T071648409093Z/diagnostic_postmortem.sealed.v3.json)，文件SHA-256为`3bba44a58eca20914d30842d7812c0666656d01dd93f6be24230616a637d6f27`；状态为`runner_failed_sealed`，结论为`capability_probe_bug`，`tushare_capability_judgment=not_made`、`rerun_permitted=false`。固定round根下已确认不存在`diagnostic_receipt.json`。
- [diagnostic_postmortem.sealed.v2.json](../data/tmp/tushare-capability/diagnostics/20260825T071648409093Z/diagnostic_postmortem.sealed.v2.json)作为superseded V2原样保留，SHA-256为`5b9ce1b2e264c20f450c93035a57301f718a7744d3f4ac03536d400535734abc`；原[diagnostic_postmortem.json](../data/tmp/tushare-capability/diagnostics/20260825T071648409093Z/diagnostic_postmortem.json)作为legacy V1原样保留，SHA-256仍为`06408a2f5d70672d0fd1665c5fbb94817ad84b437f4cd4180326e209461424d0`。二者均不单独承担当前封存真值。
- 由于真实失败发生在marker功能加入前，当前现场补写了明确标为`posthoc_observed_cli_failure`的[round-failure marker](../data/tmp/tushare-capability/diagnostics/.p0-round-failure.json)，文件SHA-256为`b9b73eba3fefa3ba3243e1cad4dab1343ef484207f7aa9adc8c7655422a9d9d5`；sealed V3完整记录该origin并与slot、失败bundle交叉绑定，且已实测阻断`daily`预算预留。该posthoc标记不提升上游证据强度。

## 关键变更文件

- 新授权HTTP终态链：[runner](../agent/tushare_http_terminal_diagnostic.py)、[event/receipt domain](../research/market_data/tushare_http_terminal.py)
- 新授权Schema：[journal event](../schemas/tushare_http_diagnostic_event.v1.json)、[terminal receipt](../schemas/tushare_http_terminal_diagnostic_receipt.v1.json)
- 新授权故障注入测试：[HTTP terminal tests](../tests/test_tushare_http_terminal_diagnostic.py)
- 运行器：[single-endpoint diagnostic](../agent/tushare_single_endpoint_diagnostic.py)
- 诊断契约：[channel/receipt domain](../research/market_data/tushare_diagnostic.py)、[postmortem domain](../research/market_data/tushare_diagnostic_postmortem.py)
- Schema：[completed receipt](../schemas/tushare_single_endpoint_diagnostic_receipt.v1.json)、[legacy unsealed postmortem V1](../schemas/tushare_single_endpoint_diagnostic_postmortem.v1.json)、[superseded sealed postmortem V2](../schemas/tushare_single_endpoint_diagnostic_postmortem.v2.json)、[current sealed postmortem V3](../schemas/tushare_single_endpoint_diagnostic_postmortem.v3.json)
- 测试：[single-endpoint tests](../tests/test_tushare_single_endpoint_diagnostic.py)、[postmortem tests](../tests/test_tushare_single_endpoint_diagnostic_postmortem.py)
- 真实 P0 现场：[budget slot](../data/tmp/tushare-capability/diagnostics/.p0-round-budget-slot-1.json)、[round-failure marker](../data/tmp/tushare-capability/diagnostics/.p0-round-failure.json)、[current sealed V3](../data/tmp/tushare-capability/diagnostics/20260825T071648409093Z/diagnostic_postmortem.sealed.v3.json)、[superseded V2](../data/tmp/tushare-capability/diagnostics/20260825T071648409093Z/diagnostic_postmortem.sealed.v2.json)、[legacy V1](../data/tmp/tushare-capability/diagnostics/20260825T071648409093Z/diagnostic_postmortem.json)
- 决策：[D-20260825-01](DECISIONS.md#d-20260825-01-统一失败后只做双通道单接口诊断)

## 验证证据

- 新授权专项：`python -m unittest tests.test_tushare_http_terminal_diagnostic -v`，`Ran 2 tests`（内含6个故障子场景），退出码0，`OK`；覆盖固定HTTP/`trade_cal`/max1、同语义payload、跨进程锁busy失败关闭、六类计数、canonical/hash篡改拒绝及合成secret/raw+SHA+前后缀全树/stdout/stderr扫描。
- Tushare/Choice联合离线回归：`python -m unittest tests.test_choice_expired_access tests.test_tushare_capability_contract tests.test_tushare_capability_probe tests.test_tushare_single_endpoint_diagnostic tests.test_tushare_single_endpoint_diagnostic_postmortem tests.test_tushare_http_terminal_diagnostic -v`，`Ran 117 tests in 56.921s`，退出码0，`OK (skipped=1)`；无真实网络。
- 安全全仓回归精确排除4个Locked/Experiment模块后：`Ran 899 tests in 145.073s`，退出码0，`OK (skipped=3)`；被排除模块继续`not run`，没有读取或解释Locked Test。
- `python -m compileall -q agent research trading operations integrations tests`退出码0；`git diff --check`无whitespace error，仅Windows LF→CRLF提示。
- 新HTTP plan：`python -m agent.tushare_http_terminal_diagnostic --plan`，退出码0；固定`endpoint=trade_cal`、`channel=http`、`max_requests=1`、`credential_accessed=false`、`sdk_imported=false`、`network_accessed=false`。
- **not run（commit前）**：新授权HTTP `trade_cal` live；其真实结果只能由commit后的固定目录terminal receipt及离线replay证明。
- 旧 37-call receipt 原实现 bundle 重放：`old_receipt_replay=passed`；旧 receipt 文件未修改。
- 新 P0 专项：`python -m unittest tests.test_tushare_single_endpoint_diagnostic tests.test_tushare_single_endpoint_diagnostic_postmortem -v`，`Ran 36 tests in 18.686s`，退出码0，`OK`；包含跨进程round lock、固定同根、未完成slot 1阻断`daily`、终态receipt前置门、completed receipt冲突拒绝、完整failure-marker绑定和参数不可用语义。
- sealed postmortem V3 replay：退出码0，`sealed_postmortem_v3_replay=passed`；marker/slot文件SHA-256、run ID、endpoint、失败bundle、请求数/参数未知状态和两通道`unavailable`均与Schema/domain一致。V1/V2文件哈希复核未变化。
- Choice到期、旧capability contract/probe、新诊断及交接文档联合离线回归：`Ran 119 tests in 102.522s`，退出码0，`OK (skipped=1)`；未发起真实网络请求。
- 真实运行：只启动一次 `trade_cal` diagnostic CLI；slot 1 SHA-256 为 `f953888d30a2713474f0d35ed8d542c989d5f76c0206f07275bab20ac6752eba`，slot 2 与 completed receipt 均不存在，无自动重试；round-failure marker 下的离线 reservation 复核返回 `daily_after_failure_marker=blocked`。
- 排除四个 Locked/Experiment 模块的安全全仓回归首次运行 `Ran 890 tests in 191.255s`，仅因 Windows 临时目录 `os.replace` 的一次 `WinError 5` 退出码 1；该单项随后 `Ran 1 test`、退出码 0。原命令完整复跑 `Ran 890 tests in 232.426s`，退出码 0，`OK (skipped=3)`。
- `python -m compileall -q agent research trading operations integrations tests`：退出码 0；`git diff --check` 无 whitespace error，只有 Windows LF→CRLF 提示；新增文件尾随空白扫描命中 0。
- P0与交接文档合并复核：`Ran 40 tests in 7.811s`，退出码0，`OK`；离线plan继续报告2次计划、4次全轮上限和三项访问标志为false。
- 凭证派生字段扫描：`token/credential hash|sha|prefix|suffix|length` 命中0；sealed postmortem V3不含`token`字段或任何Token派生值。
- **not run**：`daily` live、22-endpoint probe 重跑、正式 Provider、历史回填、Experiment V3、Locked Test、Alpha、Daily signal、Paper、券商、真实资金和 LIVE。

## 已知问题与阻塞

- 新授权HTTP-only链在本快照仍无真实terminal receipt；Tushare HTTP响应、upstream code和分类均未知。代码/Mock/离线测试不构成接口连通证据。
- 两个真实通道的 `transport_status`、`http_status`、`upstream_code`、`sdk_exception_type` 和 `sanitized_message_category` 未被原失败进程封存，不能事后重建或猜测。
- slot 1 已按最坏情况消耗本轮 2 次预算且不可复用；round-failure marker 已关闭整轮，标准入口不能再创建 slot 2。
- 当前 receipt 只证明诊断运行器完整性失败；Tushare Token、账户权限、网络通路、SDK 客户端和 endpoint capability 均未得到可信判断。
- 中证800历史成分、财报 `f_ann_date`、全收益指数、行业体系、PIT、历史覆盖、主键与重复质量继续未证明。
- `market_data.v2 not implemented`
- `Tushare formal provider not implemented`
- `Experiment V3 loader blocked`
- `Alpha BUY blocked`
- `Paper not admitted`
- `trade not admitted`
- `real money not allowed`
- `LIVE not supported`

## 安全状态

- 工程状态：`http_terminal_diagnostic_offline_verified_live_pending`
- 本轮唯一结论：`capability_probe_bug`
- Tushare capability：`unknown / judgment_not_made`
- 正式数据准入：`formal_data_admission=false`
- Experiment V3：`blocked_not_implemented`
- Daily signal authority：`none`
- Alpha BUY：`blocked`
- Paper：`paper_eligibility=false`
- 交易：`trade_eligibility=false`
- 真实资金：`real_money_list_allowed=false`
- 自动下单：`automatic_order_submission=false`
- LIVE：永久 `live_not_supported`

## 待决策

1. 当前唯一获权执行项是：精确暂存本轮文件并提交`fix: harden Tushare probe diagnostics and request accounting`，随后从当前进程环境读取Token，只运行一次固定HTTP `trade_cal`，再离线replay并停止。
2. 不能删除或复用前序slot、marker、sealed V3、superseded V2、legacy V1；新HTTP receipt也不能改写前序双通道postmortem。
3. 新HTTP终态只诊断直接HTTP通道；无论成功或失败，都不能据此决定正式迁移、SDK状态或全量Tushare capability。

## 下一步

先完成精确commit；commit后只运行固定HTTP `trade_cal`一次，产生terminal receipt后立即停止网络。只允许离线replay、文件SHA-256、Token泄漏检查和Git状态核对；不运行SDK、`daily`或22 endpoint，不实施`market_data.v2`。

## 建议外部审查范围

按 `STATUS.md -> 3571b417..worktree -> HTTP terminal runner/domain/Schema -> 故障注入测试 -> D-20260825-02 -> 前序single-endpoint/postmortem链 -> D-20260825-01` 恢复上下文。重点核对固定production root、live/replay同锁、发送前磁盘head、六计数不变量、首事件runtime context绑定、恢复绝不重发、秘密/原始响应不持久化，以及所有准入状态继续失败关闭。
