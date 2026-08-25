# 项目决策记录

本文记录跨模块、长期有效的重要决定和被放弃方案，帮助外部审查者恢复“为什么这样做”。实现状态、日常进度和运行结果写入 [STATUS.md](STATUS.md) 或对应受控产物，不在这里重复。

## 使用规则

- 使用 `D-YYYYMMDD-NN` 稳定 ID，状态只能是 `proposed`、`accepted`、`rejected` 或 `superseded`。
- 记录架构、数据源、技术栈、接口/Schema 契约、重要 trade-off，以及被放弃的关键方案和原因。
- 已接受的历史记录不静默改写；决定变化时新增记录，并通过 `supersedes`/`superseded_by` 明确关系。
- 每条决定链接到代码、配置、Schema、正式规格、受控产物或测试。决策记录不能自行证明实现完成、来源可信、统计有效或研究准入。
- 不记录命令流水、临时 TODO、聊天推理、秘密或个人账户标识。

## 决策索引

| ID | 日期 | 状态 | 标题 |
|---|---|---|---|
| [D-20260820-01](#d-20260820-01-以仓库作为跨-agent-交接界面) | 2026-08-20 | `accepted` | 以仓库作为跨 Agent 交接界面 |
| [D-20260820-02](#d-20260820-02-按证据价值清理仓库) | 2026-08-20 | `accepted` | 按证据价值清理仓库 |
| [D-20260820-03](#d-20260820-03-隔离自适应仓位-v2-p0不替换质量成长-v1) | 2026-08-20 | `accepted` | 隔离自适应仓位V2 P0，不替换质量成长V1 |
| [D-20260821-01](#d-20260821-01-冻结-v2-p01-执行内核并采用五模块日终信号架构) | 2026-08-21 | `accepted` | 冻结V2 P0.1执行内核并采用五模块日终信号架构 |
| [D-20260821-02](#d-20260821-02-使用固定策略级运行注册表并延后实际仓位事实) | 2026-08-21 | `accepted` | 使用固定策略级运行注册表并延后实际仓位事实 |
| [D-20260821-03](#d-20260821-03-分离llm因子候选与experiment-v3正式冻结证据) | 2026-08-21 | `accepted` | 分离LLM因子候选与Experiment V3正式冻结证据 |
| [D-20260824-01](#d-20260824-01-在daily到next信任边界复验完整语义并拒绝契约子类) | 2026-08-24 | `accepted` | 在Daily到Next信任边界复验完整语义并拒绝契约子类 |
| [D-20260824-02](#d-20260824-02-choice到期后采用tushare-probe-first而不自动迁移) | 2026-08-24 | `accepted` | Choice到期后采用Tushare probe-first而不自动迁移 |
| [D-20260825-01](#d-20260825-01-统一失败后只做双通道单接口诊断) | 2026-08-25 | `accepted` | 统一失败后只做双通道单接口诊断 |

## D-20260820-01 以仓库作为跨 Agent 交接界面

- 日期：2026-08-20
- 状态：`accepted`
- 影响范围：协作流程、外部审查、Git 纪律

### 背景

Codex 负责实现和验证，外部 Agent 负责架构、审查或决策时，聊天 Thread 会更换，也包含大量已经失败或被推翻的尝试。外部审查需要从稳定、可复核的项目材料恢复上下文，而不能依赖复制整段聊天。

### 决策

- 以仓库中的代码、版本化配置、Schema、受控产物、测试和 Git 记录作为跨 Agent 的共享证据，且继续服从 [AGENTS.md](../AGENTS.md) 的真源优先级。
- 用 [STATUS.md](STATUS.md) 提供带日期的当前目标、变更范围、验证证据、阻塞项和建议审查范围；它只是导航快照，不是准入真值。
- 用本文件保存低频、长期的重要选择和取舍；不保存日常执行过程。
- 外部审查按 `STATUS.md -> commit/diff -> 关键代码与配置 -> tests -> DECISIONS.md` 展开。
- Git 提交按功能限定范围；复杂脏工作树不执行 `git add .`，commit、push 和 remote 变更必须得到用户明确授权。

### 证据与真源

- [协作规则](../AGENTS.md)
- [项目架构与数据契约](ARCHITECTURE.md)
- [文档导航](README.md)

### 放弃的方案

- 复制完整 Codex Thread：噪声大、包含过时尝试，难以判定最终事实。
- 把 `STATUS.md` 自动生成为 `git diff` 摘要：diff 不能判断来源、运行结果、统计准入或未跟踪本地产物的语义，且容易产生自引用。
- 复用策略的 `current_status.v6.json` 作为项目交接状态：该文件有严格的策略准入契约，职责与工程交接不同，混用会削弱其语义。
- 立即引入 CI、pre-commit 或自动提交：当前工作树包含多轮未提交工作且没有 remote，应先建立最小协议并拆分审查范围。

### 后果与取舍

- Thread 可以更换，外部 Agent 仍能从仓库恢复必要上下文。
- 状态摘要需要在有意义阶段后人工维护；结构测试只能防止文件和必备章节漂移，不能证明内容真实或最新。
- GitHub 链路只有在相关文件形成 commit 并 push 后才真正成立；本地 dirty 状态不能被外部审查者自动看到。

### 重新评估条件

当仓库建立稳定 remote、PR 流程和 CI 后，可以新增 PR 模板或结构校验任务；即使自动化增强，也不得自动生成或提升数据来源、统计有效性、Paper/交易准入和 LIVE 权限结论。

## D-20260820-02 按证据价值清理仓库

- 日期：2026-08-20
- 状态：`accepted`
- 影响范围：仓库清理、历史证据、兼容模块、用户数据

### 背景

Strategy Workspace 是唯一默认策略主线，但仓库内的研报审计、Factor Lab、市场观察、DeepVan、旧 Paper/Shadow 等模块仍分别拥有独立 CLI、测试、规格或冻结反证。是否被主线直接导入，不能单独判断一个模块是否“无用”。同时，工作树包含大量尚未提交的当前实现，宽泛清理会同时删除无法从 Git 恢复的有效代码。

### 决策

- 直接删除纯字节码、可再生 smoke/test 现场，以及已经由受控归档逐对象哈希覆盖的源缓存。
- 删除零引用、已由当前代码/文档/测试承接的一次性任务书、实施 checklist、拒绝使用的旧 Schema 和未导出的源码孤儿。
- 保留独立能力切片、双跑确定性现场、当前 Choice 环境、受控市场数据/因子证据、非 sample 报告、用户持仓和 Obsidian 内容。
- 不执行 `git clean`、`git add .` 或按“未被默认主线导入”批量删除目录。整项能力退役必须同时处理其 CLI、配置、Schema、文档、测试和历史产物，并单独获得明确范围。

### 证据与真源

- [项目交接状态](STATUS.md)
- [文档导航](README.md)
- [Schema 目录](../schemas/README.md)
- [测试目录](../tests/README.md)

### 放弃的方案

- 只保留 `research/strategy_workspace/`：会丢失仍可复现的独立审计能力、失败样本和永久 LIVE 阻断回归面。
- 对 ignored/untracked 文件执行宽泛清理：当前主线和 Choice 接入的大量实现尚未跟踪，会造成不可恢复的数据与代码丢失。
- 删除内容相同的全部 A/B 双跑目录：虽然内容重复，但双目录并存仍承载确定性审计语义；本轮保留。

### 后果与取舍

- 仓库移除了可证明无价值的噪声，同时保持当前主线、兼容能力、研究反证和安全边界可复核。
- 本地目录仍会较大，因为研报主库、人工审核材料、Choice SDK/环境和受控证据具有复现价值；“磁盘占用大”不会被当作无用的充分条件。
- 清理后测试会重新生成少量 `__pycache__`；交付前再次移除即可，不影响源码验证结果。

### 重新评估条件

当某项兼容能力由用户明确退役、其历史证据已迁移到可校验归档，或当前未提交实现形成可恢复 commit 后，可以按完整 capability slice 再做第二阶段瘦身。

## D-20260820-03 隔离自适应仓位 V2 P0，不替换质量成长 V1

- 日期：2026-08-20
- 状态：`accepted`
- 影响范围：策略版本、研究到执行契约、风险退出、Paper账本、样本外边界

### 背景

质量成长V1冻结Top2、20%最低现金、20日决策记录和既有政策哈希；原地扩展到0%—100%动态仓位、显式现金意图和日频风险退出会破坏历史重放。裸权重映射也无法区分“没有Alpha”“普通调仓”和“风险清仓”，而把所有卖出计入普通换手会在12%回撤后阻断必要退出。

### 决策

- 新建 `a-share-small-account-adaptive-exposure-v2`，保留V1配置、默认入口、Top2/20%现金和历史回测行为不变；历史结果与准入状态不跨版本转移。
- 月净收益10%只作挑战报告，不是保证、模型损失函数、参数优化目标或准入门。
- 使用版本化 `PortfolioIntent` 表达普通调仓、现金和风险退出；普通空目标拒绝，仅明确的现金/回撤退出类型可用空权重表达0%。
- 从意图与账户状态推导订单风险方向。`RISK_REDUCING` / `FORCED_EXIT` 卖单豁免普通换手限制，但不豁免T+1、停牌、跌停、行情时效、账户完整性、可卖数量和幂等。
- 分离稳定 `intent_id` 与逐受控执行时段 `attempt_id`：同一尝试重放幂等，后续交易日重试使用新attempt。
- D日收盘首次达到12%回撤即粘滞锁定，按传入内部受控日历的下一session开盘开始退出，残仓逐session重试；12%是触发值，不是最大亏损保证。日历与执行报价均使用规范化payload哈希绑定，但在官方registry接入前不证明来源或日历无遗漏。该 latch 在当前受控账本和策略运行周期内永久有效，平仓只结束 `exit_pending`，不恢复买入。该语义只进入V2，不回写V1历史回测器。
- 新建独立日频Paper账本V2，绑定政策和受控日历哈希；V1账本不原地扩展。账本只提供对账证据，不授予Paper或交易准入。
- 固定Train 2018—2022、Validation 2023、Locked Test 2024—2025一次受控运行；V2规格冻结前的2026数据视为 `retrospective_consumed`。

### 证据与真源

- [自适应仓位V2规格](ADAPTIVE_EXPOSURE_V2.md)
- [V2政策配置](../configs/strategy_adaptive_exposure.v2.json)
- [PortfolioIntent Schema](../schemas/portfolio_intent.v1.json)
- [Execution Plan Schema](../schemas/portfolio_execution_plan.v1.json)
- [日频Paper账本Schema](../schemas/strategy_paper_ledger_record.v2.json)
- [P0对抗测试](../tests/test_adaptive_exposure_p0.py)

### 放弃的方案

- 原地修改V1政策、历史回测器或账本：会改变既有重放语义。
- 把普通空映射解释成清仓：无法区分缺数据、无Alpha和风险退出，容易误卖。
- 让所有风险退出继续受普通换手上限：会使风险门本身阻止减仓。
- 复用单一decision ID做跨日订单幂等：首日受阻的终态会堵死次日重试。

### 后果与取舍

- P0能表达并审计现金、局部受阻退出、D+1卖出和逐日重试，同时保持V1兼容。
- 当前没有 latch reset、自动换新账本或恢复入场的标准编排；外部新生命周期必须另行形成受控证据，不能改写旧账本或自动提升Paper状态。
- 普通Alpha当前只允许同session执行；D收盘Alpha到下一开盘的正式编排、官方日历/行情registry以及Gate approval对独立费率表的绑定尚未实现，均是Paper准入前的治理债。
- 新Schema、SQLite字段和日频账本增加了契约面；必须保留迁移、防篡改和跨重启回归。
- `p0_runtime_implemented_not_admitted` 只表示安全运行时实现，不代表Alpha/仓位模型、正式PIT回测、统计有效性或Paper准入。

### 重新评估条件

只有在Alpha与exposure engine参数预注册、受控实验V3冻结、完整Choice PIT数据和全收益基准准入、唯一Locked Test运行及前向Paper证据完成后，才能评估研究或Paper状态；任何核心参数变化形成新策略版本。

## D-20260821-01 冻结 V2 P0.1 执行内核并采用五模块日终信号架构

- 日期：2026-08-21
- 状态：`accepted`
- 影响范围：Adaptive Exposure V2执行内核、信号生产、跨进程Schema、日频决策、人工执行与Paper记账
- 关系：扩展 `D-20260820-03`，并替代其中“普通Alpha仅同session、Gate费率bundle尚未绑定”的当时实现状态；不替代其V1/V2隔离和样本外边界

### 背景

P0已经能表达现金与跨日风险退出，但仍存在七个会破坏失败关闭或账户一致性的执行问题：暂停/数据失败可能携带BUY、Gate不能独立证明退出覆盖、部分减仓意图首次D+1受阻、日亏损误伤纯减仓、Paper账户更新缺少CAS、费用/证券规则未形成同一canonical bundle、以及批内预检可能晚于订单进入`SUBMITTING`。另一方面，日终Alpha、目标仓位、整手组合、下一交易日一次性人工适配和日报职责尚未形成清晰的跨进程边界。

### 决策

- 修复并冻结P0.1七项执行语义：双层禁买、Gate独立退出覆盖、四类减仓首次相邻D+1、日亏损只阻断风险增加、账户fingerprint CAS、canonical `FeeSchedule + InstrumentRule + whole-lot policy` bundle、整批预检先于任何`SUBMITTING`。
- 执行内核冻结后，日常信号研究不得直接修改其安全语义；研究到执行只通过版本化结构化契约。
- 将日终信号生产拆为五个单责模块：Alpha Engine、Exposure Engine、Portfolio Constructor、Next-session Adapter和Daily Pipeline。Alpha不下单，Exposure不选生产阈值，Constructor分离目标/可实现/当前，Next-session只生成D+1人工指令，Pipeline只编排证据。
- Daily Pipeline固定为12阶段：更新数据、数据门、Alpha、Exposure、组合、Intent、不可变decision、Markdown/JSON、通知outbox、D+1复核、人工成交证据、Paper Ledger V2收盘追加。每天必须生成决策，但允许零订单。
- 跨进程产物使用严格版本化Schema和自哈希。Daily decision与signal采用create-only、相同字节幂等；D+1 consumption与人工成交/账本追加保留单次或append-only语义。
- D+1单次性以 `signal_sha256` 的受控目录全局CAS为准，而不是调用者指定的文件名；复制、改名、路径别名及取消后重试都不能形成第二次消费。
- Exposure 的普通迟滞只按CST策略日推进，续接前一官方日不可变decision/state；账户回撤由已验证Paper Ledger峰值与D日策略NAV派生。换池后旧持仓使用独立受控收盘引用继续进入exit-only构造，不因离池丢失退出覆盖。
- 预期数据或配置验证失败写 `daily-strategy-decision.v2` 的零订单 `BLOCKED` 分支与failure receipt；不可变碰撞、并发改写等完整性错误仍直接报警，不能被普通失败日报掩盖。
- Paper Ledger V2以typed canonical成本bundle和receipt-bound close-mark bundle重算佣金、印花税、过户费与滑点；调用者自填hash或模块硬编码费率不构成完整成本证据。
- 通知阶段当前只写本地outbox，不声明邮件、飞书、短信或其他外部发送成功。盘前复核和成交记录均无自动提交权限。
- 所有模型、迟滞、entry/hold/no-trade、成本、时效和价格偏离阈值必须来自调用方提供的预注册冻结artifact；代码不选择生产默认值。
- 在Experiment V3正式冻结、外部受控PIT与官方registry接入前，不运行或解释2024—2025 Locked Test。固定`paper_eligibility=false`、`trade_eligibility=false`、`real_money_list_allowed=false`、`live_supported=false`。

### 证据与真源

- [Adaptive Exposure V2正式规格](ADAPTIVE_EXPOSURE_V2.md)
- [Alpha Engine](../research/strategy_workspace/alpha_engine_v2.py)
- [Exposure Engine](../research/strategy_workspace/exposure_engine_v2.py)
- [Portfolio Constructor](../research/strategy_workspace/portfolio_constructor_v2.py)
- [Next-session Adapter](../research/strategy_workspace/next_session_signal.py)
- [Daily Pipeline](../operations/daily_pipeline.py)
- [Execution Plan V2 Schema](../schemas/portfolio_execution_plan.v2.json)
- [Daily Decision Schema](../schemas/daily_strategy_decision.v2.json)
- [P0.1对抗测试](../tests/test_adaptive_exposure_p0.py)
- [Daily Pipeline专项测试](../tests/test_daily_pipeline.py)

### 放弃的方案

- 让Daily Pipeline直接调用券商或Paper Broker自动提交：会混淆信号、人工授权和执行权限，且扩大LIVE风险面。
- 复用风险退出跨session通道承载普通Alpha买入：会绕过Alpha的有效期、价格偏离和风险方向语义。
- 在模块内给entry/hold/迟滞/no-trade阈值设置“合理默认值”：会绕过Experiment V3预注册并增加不可审计自由度。
- 用SHA-256、来源名称或bool认证日历/规则/行情：它们只能证明内容一致，不能证明官方来源或完整性。
- 为保证每日报单而强制交易：日决策与交易是不同事实，零订单必须是合法结果。

### 后果与取舍

- 日终决策、D+1复核、人工成交和收盘账本拥有明确、可重放的责任边界；目标、可实现、当前和实际仓位不会被同一字段覆盖。
- 新增了多个版本化Schema、哈希和create-only产物，集成方必须提供外部冻结artifact与registry，不能只传松散字典。
- 本地实现现在可用于确定性和失败模式验证，但没有生产PIT、官方registry、正式模型/阈值、Locked Test或前向Paper证据；实现状态不得提升为研究有效或准入。

### 重新评估条件

只有在Experiment V3正式冻结并绑定完整外部受控PIT、官方日历/证券规则/行情registry和train-only模型后，才可以启动前向日频Paper决策。2024—2025 Locked Test仍须遵守唯一一次受控消费；任何模型或核心阈值改变都形成新版本，不回写本决策。

## D-20260821-02 使用固定策略级运行注册表并延后实际仓位事实

- 日期：2026-08-21
- 状态：`accepted`
- 影响范围：Next-session一次性消费、人工成交CAS、Exposure跨日连续性、失败恢复、日报事实层与跨进程禁买
- 关系：细化 `D-20260821-01` 的单次消费、状态续接和四类仓位语义

### 背景

调用者自选report/signal路径会把“单次”降级成路径局部约束；同一signal复制到另一目录后可以再次消费。类似地，调用方可以更换日报目录并传入pristine Exposure memory，绕过上一日迟滞或`RISK_OFF`。计划日报若把D日当前仓位复制到`realized_*`，又会在D+1尚未成交前伪造实际结果。Python领域校验之外，早期Schema也没有完整表达所有禁BUY状态。

### 决策

- Next-session consumption以固定仓库/策略级registry中的完整`signal_sha256`为唯一CAS键；signal路径不再决定消费槽。人工成交bundle以`consumption_sha256`形成唯一O_EXCL槽，收盘账本必须重读精确canonical字节。
- Exposure inputs/decision/state写入独立固定策略级registry；report目录不是状态真源。失败日若Exposure policy仍可验证，持久化绑定failure receipt的`IMMEDIATE_RISK_OFF`状态；下一官方日只能从该状态续接。policy无效时保持人工恢复阻断。
- 无账本首次bootstrap只接受冻结政策的空仓`initial_cash=10000`，不能把任意缩水余额重置为新峰值。
- D日daily decision保留`realized_*`字段但固定为`null`；D+1真实成交与收盘mark后，实际仓位只进入Paper Ledger V2。
- `daily_strategy_decision.v2`、`portfolio_execution_plan.v2`、`portfolio_construction_result.v2`和`next_session_signal.v1`均增加跨进程禁BUY条件，不能只依赖同进程dataclass、Planner或Gate。
- 固定registry的单机文件系统ACL是当前writer信任边界；测试必须注入临时registry。直接写内部目录不是正式入口，SHA-256不能认证账户、行情或官方来源。多机运行前必须换成受控的共享CAS/registry adapter。

### 证据与真源

- [Next-session Adapter](../research/strategy_workspace/next_session_signal.py)
- [Daily Pipeline](../operations/daily_pipeline.py)
- [Next-session测试](../tests/test_next_session_signal.py)
- [Daily集成测试](../tests/test_daily_pipeline_integration.py)
- [Schema对抗测试](../tests/test_schema_validation_v2.py)
- [P0.1对抗测试](../tests/test_adaptive_exposure_p0.py)

### 放弃的方案

- 按signal/report父目录发现最近registry：复制到另一受控目录即可制造第二个CAS槽。
- 信任调用者传入的consumption或fill对象：自哈希对象仍可扩大数量或绕过盘前复核。
- 失败日不写Exposure续接证据：第二天要么可被bootstrap重置，要么永久无法受控恢复。
- 在D日计划中把`current_*`复制为`realized_*`：会混淆当前事实与尚未发生的D+1成交。

### 后果与取舍

- 单一仓库/策略运行实例内的重复消费、换目录重置和Stage 11重复记录被create-only CAS关闭；失败恢复保持降仓方向。
- 固定registry是运行状态，不是Git产物；必须由受控调度器持有、备份并限制writer权限。跨主机一致性和外部来源认证仍未实现，因此不提升Paper或交易准入。

### 重新评估条件

生产调度、多机执行或外部人工工作台接入前，必须提供受控共享CAS、官方calendar/account/quote/rule receipt loader和明确的registry迁移/恢复流程；不得用本地路径或哈希自行宣称来源可信。

## D-20260821-03 分离LLM因子候选与Experiment V3正式冻结证据

- 日期：2026-08-21
- 状态：`accepted`
- 影响范围：因子发现、Alpha模型、Exposure/Constructor policy、Next-session Signal、Daily Pipeline固定发布边界
- 关系：细化 `D-20260821-01` 的模型/阈值预注册与外部artifact边界，并以Model/Policy/Signal V2替代 `D-20260821-02` 当时使用的对应V1运行时契约；不改写该历史记录

### 背景

五模块已能消费调用方给出的冻结对象，但旧契约仍可能让候选因子、裸自哈希模型或调用方自报policy越过研究治理边界。金融与非金融子模型的裸分数也不能在没有同目标、同期限train-only校准的情况下直接混排。Next-session若只信任Signal内嵌的receipt内容，则自签JSON仍可能把未获外部控制的政策带到D+1。与此同时，池外旧持仓和仅有相对排名但预测收益不为正的候选必须有明确、可跨进程检查的处理规则。

### 决策

- LLM只能创建永久状态为 `llm_research_candidate_only` 的 `FactorHypothesisV2`。候选必须经过预注册、独立、只使用Validation分区的typed validation receipt，才可进入按ID规范排序和自哈希的approved-factor registry；候选、验证和批准三者不合并。
- Alpha诊断入口只接受 `frozen-alpha-model.v2`：模型feature集合必须与approved registry完全一致，并绑定train-only训练receipt、同一目标/预测期限的金融与非金融校准、候选模型准入receipt、runtime源码manifest及Experiment V3结构绑定。旧V1模型和未批准因子在运行时失败关闭。正式入口还必须通过formal loader；当前固定返回全池 `DATA_FAIL_CLOSED`，不能产生BUY。
- Exposure与Constructor升级为V2 policy并绑定同一admission receipt。Constructor除percentile band外必须设置严格为正的entry预测收益门及hold预测收益门；池外现有持仓固定 `MANDATORY_EXIT`，不能因截面缺行而静默保留。
- `ExperimentV3AdmissionReceiptV1`只表达 `diagnostic_binding_only_not_formally_admitted` 的结构绑定，formal loader固定为 `blocked_not_implemented`。生产代码不提供issuer token或issuer helper；测试直接构造dataclass也不能令formal verifier成功。该receipt不认证外部artifact，不是正式Experiment V3冻结证据。
- Daily Pipeline每天写decision，并以固定本地registry作为D→D+1唯一发布权威。authority枚举只有 `BLOCKED` 和 `RISK_REDUCTION_ONLY`，故不存在Alpha authority：前者恰好发布daily decision、authority receipt、canonical failure receipt、received-input commitments四项且 `next_session_allowed=false`；后者恰好发布17项完整证据且只允许无BUY的四类风险退出。
- 所有publication artifact先做canonical JSON roundtrip再校验和持久化。每个 `YYYY-MM-DD/` 日期槽create-only占用，逐文件排他创建，admission/publication receipt与全部artifact写完后最后写 `COMMITTED`；崩溃留下的部分槽被视为毒化状态，必须失败关闭并人工恢复，不能自动覆盖或补齐。
- Next-session升级为 `next-session-signal.v2`，创建、落盘、重载和消费都必须从固定registry重读精确canonical字节；调用方传入的receipt、路径或hash不能替代registry。当前只有 `RISK_REDUCTION_ONLY` 可进入第一次紧邻D+1，保留 `RISK_OFF`、`DEFENSIVE_REDUCTION`、`NO_ALPHA_CASH`、`ACCOUNT_DRAWDOWN_EXIT` 的首次执行；风险退出不得冒充普通Alpha买入。
- 固定registry和文件ACL只是单机本地writer权限边界，不是外部来源认证、多主机共识或正式准入。Schema、自哈希、测试fixture、diagnostic receipt和逐日publication均不能提升Paper、交易、真实资金或LIVE准入。

### 证据与真源

- [Factor Discovery治理](../research/factor_discovery/README.md)
- [Factor Governance实现](../research/factor_discovery/governance.py)
- [Experiment V3 diagnostic binding契约](../research/strategy_workspace/experiment_v3_admission.py)
- [Alpha Engine](../research/strategy_workspace/alpha_engine_v2.py)
- [Exposure Engine](../research/strategy_workspace/exposure_engine_v2.py)
- [Portfolio Constructor](../research/strategy_workspace/portfolio_constructor_v2.py)
- [Next-session Adapter](../research/strategy_workspace/next_session_signal.py)
- [Daily publication boundary](../research/strategy_workspace/daily_signal_publication.py)
- [Daily Pipeline](../operations/daily_pipeline.py)
- [Daily signal admission Schema](../schemas/daily_signal_admission_receipt.v1.json)
- [Daily signal publication Schema](../schemas/daily_signal_publication_receipt.v1.json)
- [Schema目录说明](../schemas/README.md)
- [因子治理测试](../tests/test_factor_discovery_governance.py)

### 放弃的方案

- 让LLM直接输出“已验证因子”或把候选清单当模型feature registry：这会把提出假设与独立证伪合并为同一权限。
- 只比较factor/model/policy SHA-256：哈希能绑定内容，不能证明issuer、数据源、训练分区或正式冻结流程可信。
- 在生产代码保留issuer token/helper或允许测试fixture签发“正式”receipt：研究调用方会同时成为信任授予方，`blocked_not_implemented`失去意义。
- 允许Next-session从Signal内嵌字段、调用方receipt或任意路径重建信任：同一调用方可以自签闭环或更换目录，绕过逐日唯一发布边界。
- 只写一份publication receipt而不固化全部artifact，或在partial槽上自动续写：无法证明D+1使用的就是D日不可变决策，崩溃恢复还可能拼接两次运行。
- 只按percentile买入或让池外持仓继续HOLD：前者可能买入全截面预期收益为负的相对优胜者，后者会丢失股票池变更后的退出责任。

### 后果与取舍

- 研究候选、独立验证、批准registry、模型训练/校准和政策形成可重放的诊断证据图；正式Alpha在外部loader缺失时明确失败关闭，不能借内部一致性获得BUY权限。
- Daily仍满足“每天有决策”，同时把可执行边界缩窄为零订单阻断或纯风险减仓；四类风险退出不会因Alpha准入阻断而失去第一次D+1。
- Risk publication增加到17项artifact，Blocked保留4项最小证据；canonical roundtrip、完整文件集和`COMMITTED`-last提升本地重放确定性，但partial槽需要人工恢复。
- 当前只能验证单机内部一致性，不能证明外部来源、统计有效性或正式Experiment V3冻结；2024—2025 Locked Test继续不运行、不解释。

### 重新评估条件

只有在独立于研究调用方的正式controlled loader能够认证approved-factor registry、训练/校准产物、两份policy、官方PIT与市场registry，并形成不可变Experiment V3冻结receipt后，才重新评估“正式冻结”状态。之后仍须单独完成唯一Locked Test和前向Paper门，且LIVE永久不支持。

## D-20260824-01 在Daily到Next信任边界复验完整语义并拒绝契约子类

- 日期：2026-08-24
- 状态：`accepted`
- 影响范围：Daily publication、Exposure/Alpha正式artifact、Next-session加载边界、Experiment V3诊断receipt
- 关系：补强`D-20260821-03`的固定发布与失败关闭边界，不改变其formal loader阻断和risk-only authority结论

### 背景

最终红队发现三项阻断：canonical字节和自哈希不能证明正式Daily/Exposure/Alpha artifact符合冻结Schema；分别自洽的authority、状态、failure、安全旗标与Exposure目标仍可能组合成不可达决策图；使用`isinstance`或动态分派还可能让调用方子类覆写验证/序列化方法。Next-session若只依赖loader已经校验过，也缺少独立防御层。

### 决策

- Daily publication在持久化前和固定registry重载时，实际执行`daily_strategy_decision.v2`、`exposure_decision.v2`和`alpha_ranking.v2`正式Schema，并复核Alpha status与eligible count。
- Authority、decision/data status、failure receipt和所有安全旗标必须形成同一分支；blocked不能套risk authority，risk/no-failure分支不能携带blocked字段。
- Exposure固定state/target必须沿流水线真实优先级映射到Intent、Construction和Daily：账户回撤优先，其次数据/Alpha失败、`NO_ALPHA_CASH`、`RISK_OFF`、真实减仓，其他情况为当前禁止发布的Alpha。
- Next-session从固定registry加载后独立重跑完整publication contract；它不把loader返回对象视为已经足够可信。
- Experiment诊断receipt、Daily admission/publication/loaded结果和Next-session Signal均要求exact contract type；关键校验显式调用基类实现，拒绝调用方子类覆写。
- 以上只关闭本地工程绕过，不增加Alpha、Paper、交易、真实资金或LIVE authority；四个Locked/Experiment模块继续不运行、不解释。

### 证据与真源

- [Daily publication boundary](../research/strategy_workspace/daily_signal_publication.py)
- [Next-session Adapter](../research/strategy_workspace/next_session_signal.py)
- [Experiment V3 diagnostic binding](../research/strategy_workspace/experiment_v3_admission.py)
- [Next-session对抗测试](../tests/test_next_session_signal.py)
- [Schema对抗测试](../tests/test_schema_validation_v2.py)

### 后果与取舍

- 同一risk publication须同时通过Schema、hash、authority分支和跨artifact条件图；只重签被修改的字段不再足够。
- Next重复校验增加少量本地计算，但减少loader替换或未来重构削弱语义校验的风险。
- Exact-type边界有意拒绝继承扩展；任何新版本必须显式升级契约和Schema，不能靠子类兼容。

### 重新评估条件

只有在新的版本化契约明确替代现有V1/V2类型、同步Schema和对抗测试后，才重新评估exact-type限制；formal Experiment V3、Locked Test及任何准入仍按既有独立门处理。

## D-20260824-02 Choice到期后采用Tushare probe-first而不自动迁移

- 日期：2026-08-24
- 状态：`accepted`
- 影响范围：Provider访问策略、Choice新采集、Tushare能力验证、未来Market Data V2迁移
- 关系：不改变`D-20260821-03`和`D-20260824-01`的Experiment V3、Daily publication及准入边界

### 背景

用户已确认Choice接口权限到期，而当前`market_data.v1`仍把BaoStock作为默认主源、Choice作为显式许可二级诊断源、Tushare V1作为日线独立核验源。Tushare官方文档列出指数、行业、财务和披露接口及积分门槛，但文档不能证明具体Token有权限，也不能证明历史覆盖、PIT语义、单位或Schema满足项目要求。若在没有真实能力证据时直接迁移，会把SDK可调用性误写为数据准入，并可能越过正式Experiment V3与Daily Alpha的失败关闭。

### 决策

- 以版本化、严格Schema约束的Provider Access Policy记录Choice为`expired`。所有新的Choice网络访问和诊断session都必须在SDK导入、初始化或登录前返回`provider_access_expired`；不靠README、环境变量或调用者布尔值控制。
- Choice历史raw、quarantine、validated、诊断与归档证据不得删除或覆盖。许可证后续使用边界未经人工确认前，策略不得把旧Choice数据作为新的正式研究输入；内容哈希只证明文件一致性。
- BaoStock保持默认主源，不自动切换到Tushare、AKShare或Eastmoney，也不允许字段级、行级或半批次fallback。
- Tushare新增独立capability probe，而不扩展现有`TushareProvider` V1。探针只接受代码内固定白名单，默认plan不读Token、不导入SDK、不联网；显式`--live`才可进行有界只读调用。
- capability receipt固定为`capability_probe_only_not_admitted`及全套false安全字段。endpoint成功、receipt自哈希、Schema通过和重放成功均不能形成正式MarketDataBatch，也不能影响Factor Registry、Experiment V3、Daily publication、Paper、交易、真实资金或LIVE。
- 未获得真实、可重放的本机receipt前，不创建`market_data.v2`，不实现正式Tushare Provider V2，不决定行业体系切换，也不解锁任何Alpha BUY。下一阶段按dataset独立评估权限、覆盖、PIT、单位、基准和失败条件。

### 证据与真源

- [Provider访问策略](../configs/provider_access.v1.json)
- [Provider访问策略Schema](../schemas/provider_access_policy.v1.json)
- [Tushare能力探针配置](../configs/tushare_capability_probe.v1.json)
- [Tushare迁移边界](TUSHARE_MIGRATION.md)
- [Tushare capability contract](../research/market_data/tushare_capability.py)
- [Tushare capability CLI](../agent/tushare_capability_probe.py)
- [Choice到期测试](../tests/test_choice_expired_access.py)
- [Tushare契约测试](../tests/test_tushare_capability_contract.py)
- [Tushare探针测试](../tests/test_tushare_capability_probe.py)

### 放弃的方案

- 直接把Choice的`enabled`改为false：只能挡住Registry的一部分入口，无法保证专用诊断脚本在SDK导入前失败，也没有表达历史证据消费边界。
- 把全部Tushare接口塞进现有V1 Provider：会把能力试探与正式dataset契约合并，并扩大既有日线核验职责。
- 接口成功后自动改默认Provider或回填缺失字段：会制造跨Provider拼接、来源不清和半批次成功。
- 用5000积分或官方文档推断权限与PIT：账户能力、历史覆盖和实际返回字段仍必须由真实Token小样本验证。

### 后果与取舍

- Choice新访问被确定性关闭，但历史证据可继续保存和做受控审计；后续若许可证边界变化，必须通过新版本策略和独立审查恢复，不能临时改布尔值。
- Tushare能力可以逐endpoint形成脱敏、create-only、可重放证据，代价是本轮不会立即获得正式数据主源或Alpha恢复。
- 探针输出只能缩小下一阶段的不确定性；全收益基准、历史成分、SW/CSI行业迁移、财务首披链和正式来源认证仍可能继续阻断。

### 重新评估条件

只有在真实capability receipt明确接口权限与覆盖，并由独立Market Data V2决策逐dataset冻结Provider、Schema、PIT、单位、基准、失败条件及迁移策略后，才重新评估正式Tushare适配范围。该重新评估仍不授予Experiment V3、Paper、交易、真实资金或LIVE权限。

## D-20260825-01 统一失败后只做双通道单接口诊断

- 日期：2026-08-25
- 状态：`accepted`
- 影响范围：Tushare capability probe 诊断、真实请求预算、错误 receipt
- 关系：补强 `D-20260824-02` 的 probe-first 边界，不改变正式 Provider、Experiment V3、Daily、Alpha 或执行准入

### 背景

首次 22-endpoint 探针实际产生 37 次 Tushare 请求，但所有 endpoint 都统一落入 `unexpected`。同构失败只能证明原公共入口没有提取足够的上游结构，不能区分 Token/账户、SDK 客户端、网络传输或探针实现，也不能据此判定 Tushare capability failure。

### 决策

- 停止 22-endpoint 探针，不删除或改写旧 receipt；只新增隔离的 single-endpoint diagnostic。
- 固定先测 `trade_cal`：SDK 一次、直接 HTTP 一次，二者使用相同语义参数。仅在另有必要且仍在同一轮预算内时才可对 `daily` 各一次；全轮上限 4，预算槽 create-only、不可释放或复用。
- 标准live入口固定output与budget同根，并从预算预留前到终态receipt或failure marker发布后全程持有跨进程round lock。`daily`不能以“slot 1存在且failure marker暂时不存在”作为放行条件；必须先重放同一slot 1 run ID对应的终态`trade_cal` receipt，未完成、硬退出或终态发布失败均零请求失败关闭。
- 每通道最多一次发送，无 retry、无 redirect。SDK import/init 的隐式网络在计数发送之外失败关闭。
- 只持久化 `transport_status`、`http_status`、整数或 null 的 `upstream_code`、固定枚举 `sdk_exception_type` 和 `sanitized_message_category`。结构化 code 优先，再按同一 envelope message、HTTP status、transport fallback 映射 permission、rate limit、authentication/account、invalid parameter 和 server/internal。
- Token 只从当前进程环境读取；不得进入配置、Schema、日志、receipt、异常文本、测试夹具，也不得输出或持久化其哈希、长度、前后缀。明显不符合本地安全 envelope 的剪贴板输入必须在预算、SDK import 和网络之前拒绝；该预检不证明凭证真实有效。
- completed receipt 的结论只能是 `token_or_account_problem`、`sdk_client_problem`、`network_transport_problem` 或 `capability_probe_bug`。
- 若 runner 在预算预留后、completed receipt 发布前失败，异常边界必须先写 create-only round-failure marker并关闭整轮；标准入口随后拒绝`trade_cal`和`daily`。sealed postmortem V3必须完整内嵌marker并绑定marker SHA-256、slot与失败实现bundle，交叉验证run ID、endpoint、异常类别和时间顺序；只在固定round根确认completed receipt不存在。实际请求数、runtime语义参数和两通道五项字段均保持`null + unavailable`，不能用封存时当前配置回填失败进程事实。V1未绑定marker、V2回填当前参数的历史形状明确拒绝继续签发。禁止只凭slot抢先宣称runner失败、把预算预留数伪装成真实请求或把顶层异常伪装成SDK异常。此时结论固定为`capability_probe_bug`，只判断runner integrity，不判断Tushare capability，且本轮不得重跑。
- 无论哪类诊断结论，`formal_data_admission`、Experiment V3、Daily authority、Alpha BUY、Paper、交易、真实资金和 LIVE 均保持关闭；不得继续实施 `market_data.v2`。

### 证据与真源

- [single-endpoint diagnostic CLI](../agent/tushare_single_endpoint_diagnostic.py)
- [diagnostic contract](../research/market_data/tushare_diagnostic.py)
- [postmortem contract](../research/market_data/tushare_diagnostic_postmortem.py)
- [completed receipt Schema](../schemas/tushare_single_endpoint_diagnostic_receipt.v1.json)
- [历史unsealed postmortem Schema V1](../schemas/tushare_single_endpoint_diagnostic_postmortem.v1.json)
- [superseded sealed postmortem Schema V2](../schemas/tushare_single_endpoint_diagnostic_postmortem.v2.json)
- [current sealed postmortem Schema V3](../schemas/tushare_single_endpoint_diagnostic_postmortem.v3.json)
- [项目当前状态](STATUS.md)

### 放弃的方案

- 重新运行全部 22 endpoint：会增加预算消耗，却不能修复公共入口统一错误分类。
- 只升级 SDK 或只测 HTTP：无法用相同语义参数定位 SDK 与 wire 层差异。
- 将 reserved count 当实际 request count：进程失败后缺少通道内存证据，这样会制造虚假运行事实。
- 只凭已存在的budget slot签发postmortem：并发finalizer可能在live尚未结束时抢占run directory；必须先有runner异常边界生成的round-failure marker。
- 根据剪贴板形态或顶层 `OtherError` 直接断言 Token/账户问题：本地形态检查不是上游认证，顶层异常也不是通道结果。

### 后果与取舍

- 诊断面缩小到可证伪的两通道对照，并严格限制网络预算和秘密暴露。
- runner 在本轮真实执行中未能封存两个通道，sealed postmortem V3因而诚实保留runtime参数与通道结果不可用；它给出`capability_probe_bug`，但没有完成Tushare capability判断。由于现场先于marker功能发生，当前marker明确标为posthoc；V3完整记录其`evidence_origin`并绑定marker文件哈希，但仍只承担关闭整轮与本地完整性证明，不被提升为上游通道证据。
- 当前预算现场必须保留且不可重跑。未来若需新真实诊断，必须另获授权并建立新预算轮次，不能覆盖当前反证。

### 重新评估条件

只有新的、明确授权的预算轮次通过本地凭证预检，并生成 SDK/HTTP 两通道均有终态且可重放的 completed receipt 后，才可重新评估四类根因。正式数据源迁移仍须另立 Market Data V2 决策。

## D-20260825-02 新授权轮次采用可重放的单HTTP请求记账

- 日期：2026-08-25
- 状态：`accepted`
- 影响范围：Tushare `trade_cal` HTTP诊断、请求预算、崩溃恢复和终态receipt
- 关系：承接`D-20260825-01`的`capability_probe_bug`，不复用或改写旧双通道轮次，不改变正式Provider及研究/执行准入

### 背景

旧轮次只形成marker-bound postmortem，无法证明网络调用是否开始、响应是否收到或两通道结果。用户接受`capability_probe_bug`，但要求在不重跑SDK、`daily`或22接口的前提下，对成功和失败都形成可重放终态，并把预算预留、网络开始、响应收到和终态结果分开记账。

### 决策

- 建立独立、不可复用的新目录和HTTP-only CLI，固定`endpoint=trade_cal`、`channel=http`、`max_requests=1`；不暴露endpoint、channel、预算、SDK、`daily`或全探针选择参数。
- 网络前create-only持久化`REQUEST_RESERVED`和`NETWORK_CALL_STARTED`；收到并完成安全分类后写`RESPONSE_RECEIVED`；分类链结束写`TERMINAL`。事件使用canonical JSON、自哈希和前序哈希形成可重放链。
- 顶层receipt分别记录`reserved_request_count`、`network_call_started_count`、`response_received_count`、`terminal_result_count`、`remote_execution_unknown_count`和`budget_consumed_count`；强制`terminal=1`、`unknown=started-response`、`budget=reserved`。
- 任一崩溃前缀只能离线追加`TERMINAL`和缺失receipt，不能再次进入网络。已存在的terminal receipt必须逐事件、Schema、自哈希和文件canonical字节重放。
- 不保存Token、Token hash、前后缀、原始响应、上游message或异常文本。直接HTTP禁用自动retry和redirect，启用TLS验证和1 MiB响应上限。
- 该receipt只描述HTTP诊断终态；不补造SDK证据，不运行`daily`，不判断全量Tushare capability，也不形成Market Data、Experiment、Alpha、Paper、交易、自动下单或LIVE准入。

### 证据与真源

- [HTTP-only runner](../agent/tushare_http_terminal_diagnostic.py)
- [事件与receipt domain](../research/market_data/tushare_http_terminal.py)
- [事件Schema](../schemas/tushare_http_diagnostic_event.v1.json)
- [终态receipt Schema](../schemas/tushare_http_terminal_diagnostic_receipt.v1.json)
- [故障注入测试](../tests/test_tushare_http_terminal_diagnostic.py)
- [项目当前状态](STATUS.md)

### 放弃的方案

- 修补并重跑旧SDK/HTTP双通道目录：会复用已经关闭的预算现场并增加不被本轮授权的SDK请求。
- 用单一`actual_request_count`概括请求：无法区分已预留、已进入网络、已收到响应和远端执行未知。
- 只生成postmortem：不能满足本轮无论上游成功或失败都必须有terminal diagnostic receipt的要求。
- 崩溃恢复时重发：在`NETWORK_CALL_STARTED`之后无法证明远端未执行，重发会越过一次请求预算。

### 后果与取舍

网络开始后、响应marker前崩溃会保守记录`remote_execution_unknown_count=1`，即使远端事实上已经返回也不猜测；这是避免第二次调用的必要取舍。receipt成功和可重放只证明本地HTTP诊断证据闭合，仍不能提升Tushare数据能力或任何研究/执行状态。

### 重新评估条件

只在本轮唯一真实HTTP调用形成terminal receipt并完成离线replay、文件SHA-256和Token泄漏检查后，才报告该HTTP通道的真实终态。任何正式Tushare Provider、Market Data V2、Experiment V3、Locked Test、Alpha、Paper或执行工作都需要新的独立授权与决策。

## 新增记录模板

```md
## D-YYYYMMDD-NN 标题

- 日期：YYYY-MM-DD
- 状态：`proposed|accepted|rejected|superseded`
- 影响范围：
- supersedes / superseded_by：如适用

### 背景

### 决策

### 证据与真源

### 放弃的方案

### 后果与取舍

### 重新评估条件
```
