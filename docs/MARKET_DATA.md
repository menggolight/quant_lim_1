# 市场数据 V2

市场数据 V2 为研究层提供 Provider-Neutral、只读、失败关闭的数据边界。默认免费主源是 BaoStock；Provider 名称、SDK 返回成功和文件 SHA-256 都不能自动证明数据是官方真值、已获研究准入或可以用于交易。

本页是市场数据实现、安装和限制的主文档。其他 README 只保留入口和链接。

## 数据链路

```text
Provider 原始响应
  -> raw evidence
  -> 规范化与 Schema 校验
  -> 领域校验
  -> dataset-specific admission
  -> validated snapshot
  -> research consumer

任一校验失败
  -> raw + quarantine
  -> 不进入 research consumer
```

`research/market_data/` 中的 Registry 负责选择 Provider、执行整批校验和本地准入。Provider 可调用与数据可准入是两个状态；Primary 失败后不得用 Secondary 补齐半批，也不会生成默认价格或合成数据。

## Provider 状态

| Provider | 当前代码能力 | 角色 | 外部状态与限制 |
|---|---|---|---|
| BaoStock | 沪深市场不复权日线、交易日历、证券基础信息 | 默认主源；完整合格批次可标为 `validated_research_only` | SDK 为可选依赖；是否安装、网络是否可达必须以本机真实探针为准。不是官方真值认证 |
| Choice | 历史代码支持`EmQuantAPI`沪深A股`qfq`日线、白名单`000300.SH`不复权日线、交易日历和隔离候选证据 | 当前访问已到期；新网络、诊断session及新的正式离线研究消费均失败关闭 | 在SDK导入、初始化或登录前固定返回`provider_access_expired`；旧raw/quarantine/validated/诊断证据保留但不自动消费，也不触发其他Provider fallback |
| Tushare | 现有V1只支持指定证券不复权日线独立核验；扩展接口位于隔离的capability probe与诊断runner | 可选核验/能力候选，不阻塞BaoStock且不形成正式dataset | 37/37统一失败只证明公共诊断不足；旧single-endpoint runner以`capability_probe_bug`封存；新授权链只允许一次HTTP `trade_cal`并必须形成可重放终态receipt，运行前Tushare capability继续`unknown` |
| AKShare | 受控扩展骨架，没有已配置数据集 | 禁用、未准入 | 不提供任意函数执行；Eastmoney 上游或 `*_em` 接口只能是 `diagnostic_only/not_admitted` |
| Eastmoney Legacy | 既有历史行情和行业榜探针 | 仅 Legacy 诊断；不是默认源、验证源或 fallback | Registry 不把旧缓存适配为 validated batch。东方财富研报公开样本来源与行情诊断是两条独立链路 |

“代码能力”不等于真实接口已经连通。普通单元测试使用注入对象或Mock，不构成网络证据；正式V1日线状态只看`agent.market_data_probe`当次输出。旧双通道轮次的marker-bound sealed postmortem V3把未封存的runtime参数和通道字段保持为不可用，只证明runner完整性失败。新授权的HTTP-only轮次固定`trade_cal/max_requests=1`，以create-only的`REQUEST_RESERVED`、`NETWORK_CALL_STARTED`、`RESPONSE_RECEIVED`和`TERMINAL`事件拆分六类计数；其receipt即使成功也只形成HTTP通道诊断，不作正式Tushare能力或数据准入判断。

Provider网络许可由[版本化访问策略](../configs/provider_access.v1.json)独立控制。`market_data.v1`中的`enabled=true`只表示该适配器仍在历史注册表内，不得覆盖`access_status=expired`；调用者布尔值、环境变量、SDK对象或fallback列表均不能恢复Choice访问。迁移判断见[Choice到期后的Tushare迁移边界](TUSHARE_MIGRATION.md)。

## 数据集与准入

| 数据集 | 主源 | 时点与准入边界 |
|---|---|---|
| `daily_bar` | BaoStock | 当前仅覆盖沪深市场，北交所未接入；BaoStock 默认仅 `adjustment=none`。Choice V2 的股票诊断强制 `qfq`，白名单指数 `000300.SH` 强制 `none`，两者均不得自动回退；其 `csd` 会用同次 `tradedates` 结果做完整性检查。只有调用者显式指定 Choice 诊断入口才会调用，不能与 BaoStock 拼接。当前抓取历史数据是 `historical_backfill_not_original_capture`；上游无精确发布时间时为 `policy_estimated` |
| `trade_calendar` | BaoStock | 工作日近似不能替代 Provider 交易日历。Choice 也实现了显式 Secondary 诊断日历，但两者都不是交易所官方签名真值 |
| `security_master` | BaoStock | 当前仅覆盖沪深市场，北交所未接入；查询是 `current_snapshot_not_pit`，不能据此重建历史股票池 |
| `industry_classification` | 配置预留 BaoStock | 当前 Provider 尚未实现该数据集；即使接入当前分类，也只能是 `diagnostic_current_only` |
| `financial_indicator` | 未配置 | 缺少可靠首次披露时间时只能是 `research_only_not_pit` |

不复权日线是 V1 规范输入。复权数据必须使用独立 `adjustment` 标记、方法和来源，不能覆盖不复权原始数据。

Choice历史诊断榜对股票收益强制使用`qfq`，对沪深300强制使用`none`。当前访问到期后这些入口只用于保留代码与历史重放审计，不再发起新请求。报告中的名义目标价与事后前复权价格水平没有受控换算桥，因此当前目标价主张只生成明确排除原因，不计算虚假的绝对达成率。

SDK 返回 `10001029 data limit exceeded` 时适配器记录 `failed/quota_exhausted`。诊断采集连续三次遇到该全局配额错误即停止请求并保留已有检查点；由于按证券代码顺序截断会产生系统性选择偏差，该 run 的技能表不输出原始命中率、后验、下界或排名，也不生成研报推荐。

## Choice 候选证据

`research/market_data/choice_candidates.py` 是独立、内容寻址的候选层，不写入 `MarketDataStorage` validated 正式消费路径，也不创建 `TruthObservation`。它只有三个固定调用组合：

| query_type | 固定 SDK 方法 | 保存内容 | 固定边界 |
|---|---|---|---|
| `sw2021_classification` | `css(..., SW2021, ...)` | 当前分类原值 | `diagnostic_current_only`；`None/null/nan/--` 必须失败 |
| `historical_sector_membership` | `sector(code, date, ...)` | 指定日期返回的成分及名称 | 仅 Choice 候选，不证明官方历史 PIT |
| `edb_publish_dates` | `edbquery` + `edb(..., IsPublishDate=1, ...)` | 指标元数据、观测值和可用的 `PUBLISHDATE` | 缺失发布日期逐条保留 `null`；`first_release_proven=false` |

历史产物曾按接口独立输出`passed`、`dependency_missing`、`network_blocked`、`not_configured`或`failed`并绑定exact request与双哈希；这些状态只描述当次旧访问。当前所有新在线入口统一先返回`provider_access_expired`。Choice Provider和候选层保留的历史函数白名单不能越过访问策略，组合、下单和账户函数始终不可达。

## 统一批次契约

`MarketDataBatch` 同时绑定请求、Provider、真实上游和两层内容哈希，核心字段包括：

```text
batch_id
provider_id / upstream_source
dataset_type / schema_version / adapter_version
request_fingerprint / request_payload / retrieval_mode
requested_at / fetched_at / available_at_min / available_at_max
raw_content_sha256 / normalized_content_sha256
record_count / completeness_status / freshness_status
admission_status / point_in_time_status / synthetic / issues / records
```

时间戳必须带时区；交易日使用 ISO 日期；价格、金额和比率以可精确恢复的十进制字符串持久化。`fetched_at` 不能代替 `available_at`，当前回填也不能冒充当时已经保存的快照。

JSON Schema 负责结构，Python 领域校验还会检查请求证券、日期窗口和唯一性、严格升序、OHLC 关系、非负成交量/成交额、缺字段和非法数字。空结果、错证券、HTML 错误页、错误结构和不完整批次均失败关闭。

## 存储与离线回放

默认根目录是 `data/market_data/`：

```text
raw/         原始响应证据
quarantine/  失败批次和问题说明
validated/   结构与领域校验通过、按本地政策标记的批次
```

缓存键绑定 `provider_id`、`dataset_type`、`request_fingerprint`、`adapter_version` 和 `schema_version`。`request_payload` 允许读取时重建请求并重新计算指纹，不能只信任调用者给出的哈希。研究消费者只能读取 `validated/`，不能读取 quarantine；`synthetic=true` 或未获本地研究准入的批次也会被拒绝。

validated 文件必须同时带有 Registry receipt，绑定批次文件哈希、raw hash、当前准入政策哈希和适配器身份。只有 `MarketDataRegistry.configured()` 创建的 `configured_runtime` receipt 才能进入正式研究读取；单测注入 Provider 生成的 `test_injected` receipt 默认拒绝。BaoStock 读取时还会从 canonical raw envelope 确定性重放规范化并与 records 比对，不能用无关 raw 配合同步重算哈希通过校验。

receipt 不是外部数字签名，也不证明数据来自官方。`--market-data-storage-root` 只是显式选择本地受控目录；本地文件作者理论上仍能伪造一套结构自洽的 SDK 形态证据，因此 Provider 名称、receipt 和哈希都不得升级为官方来源认证。

正式研报 bundle 和市场观察必须在可识别的 Git 工作树中生成。clean/dirty 状态与 commit 写入 manifest；dirty 工作树还绑定 tracked diff 与未跟踪文件内容形成的 `git_diff_sha256`。两次 Git 快照不一致或状态未知时生成失败关闭；这仍不等于外部来源认证。

`offline_replay` 只回放此前相同请求指纹下、且 `fetched_at <= evidence_cutoff_at` 的最新 validated 批次，并重新核对 raw/normalized hash；未显式给 cutoff 时以本次请求时点为上限。研报审计把研究 `decision_time` 作为独立 evidence cutoff，不会因较新的同指纹缓存而跳过一份本可使用的旧证据。回放批次顶层 `requested_at` 保留源批次的采集请求时间，源 `batch_id`、`retrieval_mode`、`requested_at`、`fetched_at` 和 PIT 状态写入 lineage issue；本次本地回放的墙钟时间不进入确定性数据身份。调用时点早于源 `fetched_at` 会被拒绝。它不访问网络，也不把历史回填改写成原始时点采集。

## 安装

项目基础导入不要求安装全部 Provider SDK。下文 `python` 表示运行本项目的同一个 Python 解释器；若它不在 `PATH`，请用该解释器的绝对路径替代。BaoStock、Tushare 和 AKShare 按需安装 extra：

```powershell
python -m pip install -e ".[market-baostock]"
python -m pip install -e ".[market-tushare]"
python -m pip install -e ".[market-akshare]"
```

Choice Python SDK由厂商下载站分发，不存在可由本项目声明的PyPI extra。下列安装信息仅保留为历史环境说明；当前访问策略禁止新SDK导入/初始化/登录，不应为本项目运行这些步骤：

```powershell
$choiceSdk = "<包含 installEmQuantAPI.py 的 python3 目录>"
python "$choiceSdk\installEmQuantAPI.py"
python -c "import EmQuantAPI; print(EmQuantAPI.__file__)"
Get-ChildItem -LiteralPath $choiceSdk -Recurse -Filter LoginActivator.exe
```

API 激活与 Choice 终端登录不是同一状态。`LoginActivator.exe` 通常会在 `$choiceSdk\libs\windows\` 生成 `userInfo` 登录令牌；稳定 SDK 与该令牌应放在仓库外。它和手机号、验证码、账号、密码都属于本地秘密，不得读取到报告或复制进仓库、日志、测试夹具和聊天。运行厂商可执行文件前应核对官方来源、版本、哈希和数字签名；未获用户明确授权时不得代为激活。

仓库没有把未实际核验的 SDK 版本写成“已验证版本”。Tushare Token 只放在当前进程环境中，不写入配置、日志或仓库：

```powershell
$env:TUSHARE_TOKEN = "<仅在本机设置>"
```

不要把真实 Token 复制到聊天、测试夹具或提交记录中。

## 真实只读探针

BaoStock 日线：

```powershell
python -m agent.market_data_probe `
  --provider baostock `
  --dataset daily_bar `
  --instrument 000333.SZ `
  --start-date 2026-07-01 `
  --end-date 2026-08-05
```

Choice旧股票日线命令保留为失败关闭示例。它不会导入SDK，当前固定输出`provider_access_expired`，且不会改变BaoStock默认主源或尝试其他Provider：

```powershell
python -m agent.market_data_probe `
  --provider choice `
  --dataset daily_bar `
  --instrument 000333.SZ `
  --adjustment qfq `
  --start-date 2026-08-03 `
  --end-date 2026-08-07
```

访问策略检查优先于SDK存在性、激活和网络分类，因此当前Choice新访问只会得到`provider_access_expired`。历史批次即使曾为`validated_secondary_not_primary`，也不能经新的正式`offline_replay`进入research consumer；独立归档和完整性审计仍可保留字节。本地receipt与哈希不能恢复许可证或升级为官方真值。

Tushare扩展先运行默认离线plan；它不读取Token、不导入SDK、不访问网络：

```powershell
python -m agent.tushare_capability_probe `
  --config configs/tushare_capability_probe.v1.json `
  --plan
```

真实探针必须显式`--live`，只读取当前进程的`TUSHARE_TOKEN`；输出根严格限制在被Git忽略的`data/tmp/tushare-capability`安全子树，不能指向MarketDataStorage、策略publication、portfolio、配置或其他正式目录。成功也不进入MarketDataStorage：

```powershell
$env:TUSHARE_TOKEN = "<仅在本机设置>"
python -m agent.tushare_capability_probe `
  --config configs/tushare_capability_probe.v1.json `
  --live `
  --output-root data/tmp/tushare-capability
```

Choice Secondary历史市场诊断、90份人工审核和固定7文件bundle见[研报审计](../research/broker_report_audit/README.md)。下列三个旧在线命令当前同样在SDK导入前返回`provider_access_expired`，仅保留接口形状供历史审计：

```powershell
python -m agent.choice_candidate_probe --mode online `
  --storage-root .tmp/choice_candidates `
  sw2021 --instrument 000333.SZ

python -m agent.choice_candidate_probe --mode online `
  --storage-root .tmp/choice_candidates `
  sector --sector-code 009006195 --membership-date 2024-06-28

python -m agent.choice_candidate_probe --mode online `
  --storage-root .tmp/choice_candidates `
  edb --edb-id EMM00087117
```

把 `--mode online` 改为 `--mode offline` 只会核验完全相同请求的本地 raw、规范化记录、evidence ID 和双哈希，不加载 SDK。接口参数依据 [Choice EmQuantAPI 官方文档](https://quantapi.eastmoney.com/Upload/EMQuantAPI_Python.html) 固定在代码中；文档列出功能不等于当前账号拥有对应权限。

交易日历和证券基础信息：

```powershell
python -m agent.market_data_probe `
  --provider baostock `
  --dataset trade_calendar `
  --start-date 2026-07-01 `
  --end-date 2026-08-05

python -m agent.market_data_probe `
  --provider baostock `
  --dataset security_master `
  --instrument 000333.SZ
```

探针始终输出结构化 JSON。成功批次为 `passed`；失败会如实区分 `dependency_missing`、`network_blocked`、`not_configured` 或 `failed`，并以非零退出码结束。空结果不会写成 `passed`。

已有相同请求的 validated 批次后，可以显式离线回放：

```powershell
python -m agent.market_data_probe `
  --provider baostock `
  --dataset daily_bar `
  --instrument 000333.SZ `
  --start-date 2026-07-01 `
  --end-date 2026-08-05 `
  --retrieval-mode offline_replay
```

## Factor Lab 指数证据

`research/market_data/index_evidence.py` 独立承载 `index_level.v1`、`csi_industry_universe.v1` 与
`cn_equity_session.v1`，不把指数伪装成股票日线。标准捕获入口是
`python -m agent.factor_evidence_probe`：Choice 固定 23 个研究/对账指数，中证固定当前 11 行业加
`000985.CSI`，上交所固定交易日历；调用者不能传任意指数或 Provider。

当前真实状态必须分层阅读：中证 `index-perf` 的单交易日 12/12 最小探针及离线哈希重放已经通过，但
标准探针产物仍固定 `not_admitted_probe_only`；Choice SDK 登录成功后，`.CSI/.CNI` 别名未通过代码校验，随后
立即命中 `10001029 data limit exceeded`，所以 Choice 长历史 Screen 仍被额度和别名契约阻塞。上交所
`sse-calendar-adapter-v2` 只访问固定的 2017–2026 年度休市详情页，并叠加 2019 劳动节调整和 2020 春节延长
公告；它严格校验正文结构、日期星期、七类节假日、重开日、HTTPS host/path，并保存每页 raw 与 receipt。
历史日历的 `available_at` 明确标为 capture 时点，状态为 `historical_backfill_not_original_capture`，不能伪装成
当年已知的 PIT 证据。三种来源都不能互相补洞，也不能因为测试、URL 或 SHA-256 自动升级为正式真值。完整运行方法与统计边界见
[中证行业因子挖掘器 V1](FACTOR_LAB.md)。

## 研报审计中的来源分离

研报审计标准路径使用 `configs/broker_report_audit.v2.json`，行情通过 Market Data Registry 获取；V1 仅用于显式兼容复现：

```powershell
python -m research.broker_report_audit audit `
  --config configs/broker_report_audit.v1.json `
  --dimensions macro,industry,stock `
  --as-of 2026-08-04
```

东方财富仍可作为 `publicly_retrievable_sample_only` 的研报元数据/PDF 来源；这不允许东方财富行情进入 V2 默认主链，也不能证明覆盖了券商全部报告。在线研报不可用时，本地 PDF、JSON 和离线审计仍应独立工作。

## 状态术语

| 状态 | 只代表什么 |
|---|---|
| 代码已实现 | 本地模块和接口存在 |
| 单元测试通过 | 受控输入下行为符合断言 |
| 真实接口已连通 | 当次真实探针成功；不能由 Mock 代替 |
| `validated_research_only` | 完整批次通过本地结构、领域和准入政策；不是官方签名真值 |
| 可用于交易 | 本仓库不存在该状态 |

本仓库固定为 `research_only`、`paper_only` 和只读 Shadow；永久不支持 LIVE。任何 `execution_status="live"`、令牌、白名单或伪造 readiness 都只能得到 `live_not_supported`。
