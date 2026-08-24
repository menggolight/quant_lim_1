# 采集与日常编排层

`agent/` 负责把用户有权访问、在当时真实可见的内容或只读 Provider 结果转成受控本地记录。它不判断因子是否统计有效，也没有交易权限。

## 主要入口

| 模块 | 职责 |
|---|---|
| `market_data_probe.py` | 调用 Market Data Registry 做一次真实、只读探针，输出结构化状态和哈希 |
| `tushare_capability_probe.py` | 默认离线plan、显式`--live`的小样本Tushare能力探针；只写`data/tmp`证据，不形成正式MarketDataBatch或策略权限 |
| `choice_candidate_probe.py` | 显式捕获或离线重放 Choice SW2021、sector、EDB 候选证据；永不转正式真值 |
| `factor_evidence_probe.py` | 固定捕获/重放 Choice 23 指数、中证当前 12 指数或上交所交易日证据；输出内容寻址 receipt，始终不自动准入 |
| `choice_evidence_archive.py` | 在读取秘密前排除敏感路径，把现有 Choice raw/receipt/quarantine/checkpoint 复制为只读内容寻址归档；不删除源文件 |
| `choice_quality_growth_batch.py` | 固定中证800历史成分、qfq/none日线与下一交易日资格快照的可续跑Choice批量采集；始终非PIT且不准入Paper/交易 |
| `current_universe_import.py` | 严格导入 Choice 终端两列中证800工作簿，归档原始字节并生成可重放的当前成分诊断 receipt；不生成行业/PIT/Paper资格 |
| `current_industry_import.py` | 严格导入并重放绑定当前成分receipt的16列Choice快照；生成当前行业诊断receipt，并作为冻结60只盲样本的唯一受控入口 |
| `current_sample_snapshot.py` | 固定采集/重放60只盲样本的121个共同交易日与中证800价格指数，生成单截面六因子快照；不生成排名、信号、回测或买入名单 |
| `market_observation_pipeline.py` | 校验、密封三层观察，绑定上一期 observation/manifest 并生成不可变快照 |
| `market_observation_dashboard.py` | 从受控 observation 和 manifest 渲染本地单文件 HTML，不重新计算研究结论 |
| `deepvan_visible_text.py`、`deepvan_capture.py` | 整理复制或 OCR 的可见文本并记录来源与采集时间 |
| `deepvan_daily_pipeline.py` | 串联可见内容、结构化信号和 Obsidian 同步 |
| `eastmoney_source_probe.py` | Eastmoney Legacy 诊断；不进入 V2 默认、fallback 或 validated 市场数据链 |
| `portfolio_snapshot.py` | 维护本地持仓快照；快照不会自动成为策略资产 |

## 市场数据探针

BaoStock 日线示例：

```powershell
python -m agent.market_data_probe `
  --provider baostock `
  --dataset daily_bar `
  --instrument 000333.SZ `
  --start-date 2026-07-01 `
  --end-date 2026-08-05
```

Choice历史许可接口代码与证据仍保留，但当前访问策略固定为`expired`。下列旧诊断入口现在会在SDK导入前返回`provider_access_expired`，不会替换BaoStock或自动fallback：

```powershell
python -m agent.market_data_probe `
  --provider choice `
  --dataset daily_bar `
  --instrument 000333.SZ `
  --adjustment qfq `
  --start-date 2026-08-03 `
  --end-date 2026-08-07
```

Tushare扩展接口使用独立能力探针。plan不读取Token、不导入SDK、不联网：

```powershell
python -m agent.tushare_capability_probe `
  --config configs/tushare_capability_probe.v1.json `
  --plan
```

只有显式`--live`才会读取当前进程的`TUSHARE_TOKEN`并执行有界只读调用；产物固定写入`data/tmp/tushare-capability/<probe_run_id>/`且始终`not_admitted`：

```powershell
$env:TUSHARE_TOKEN = "<仅在本机设置>"
python -m agent.tushare_capability_probe `
  --config configs/tushare_capability_probe.v1.json `
  --live `
  --output-root data/tmp/tushare-capability
```

质量成长历史批次使用另一个固定入口。它把 CSS 按日期和最多50只股票分批，每100项压缩checkpoint，中断后可从已验证artifact恢复；不开放调用者自选板块、字段或回测窗口：

```powershell
python -m agent.choice_quality_growth_batch collect `
  --cutoff-date 2026-08-18 `
  --as-of 2026-08-19T15:30:00+08:00 `
  --output-root data/market_data/archives/strategy_workspace/quality_growth_v1/choice_batch

python -m agent.choice_quality_growth_batch verify `
  --manifest <manifest.json>
```

即使采集完整，当前仍因 Choice 日历未与交易所真值对账、缺PIT行业与首披财务而返回非零阻塞码。`source_authenticated=false`和 `raw_semantics=canonicalized_sdk_projection`表示可重放不等于官方来源认证。

`dependency_missing`、`not_configured`、`network_blocked`和`failed`只描述访问策略到期前的历史故障分类；当前所有Choice新网络入口都先返回`provider_access_expired`。账号、验证码和`userInfo`不得进入命令、日志或仓库。

Factor Lab 的证据探针只有固定 source 和指数白名单，不接受任意代码或 Provider：

```powershell
python -m agent.factor_evidence_probe `
  --source csi `
  --mode online `
  --start-date 2023-03-13 `
  --end-date 2026-08-12 `
  --output-root data/factor_evidence
```

`choice` 一次请求旧系列、当前对账系列和共同基准，共 23 个唯一 `.CSI` 代码；`csi` 固定当前 11 行业加 `000985.CSI`；`sse` 固定交易日历。`offline` 必须额外给出 `--evidence-cutoff-at`，只复核此前受控捕获，绝不加载 Provider。探针成功仍固定 `not_admitted_probe_only` 和 `formal_truth_eligible=false`。

首次实现或清理缓存前先归档已有 Choice 证据：

```powershell
python -m agent.choice_evidence_archive `
  --source-root .tmp/choice_diag_cache `
  --output-root data/market_data/archives/choice_diag_cache_20260811
```

归档会在打开文件前排除 activation、`userInfo`、credential、token、secret、`.env` 和密钥材料；保存普通证据的逐字节 SHA-256、manifest 和 checkpoint，源文件保持不变。

Choice的分类、指定日期板块成分和EDB发布日期只进入独立候选层。以下在线命令仅保留历史接口形状，当前统一失败为`provider_access_expired`；对应offline模式只可做旧证据完整性诊断，不能进入新的正式研究消费：

```powershell
python -m agent.choice_candidate_probe --mode online `
  --storage-root .tmp/choice_candidates `
  sw2021 --instrument 000333.SZ

python -m agent.choice_candidate_probe --mode online `
  --storage-root .tmp/choice_candidates `
  sector --sector-code 009006039 --membership-date 2024-06-28

python -m agent.choice_candidate_probe --mode online `
  --storage-root .tmp/choice_candidates `
  edb --edb-id EMM00087117
```

三个接口各自判断权限和失败；一个 `passed` 不代表整个账号成功。`SW2021=None`、空分类或形状漂移必须为 `failed`；EDB 行可以缺 `PUBLISHDATE`，但会保留 `first_release_proven=false`，不能冒充首次发布真值。离线时把 `--mode online` 改为 `--mode offline`，请求参数必须完全相同。

Choice 终端导出的两列当前成分工作簿只能进入独立诊断 receipt。导入器锁定单工作表、固定标题/脚注、连续800行、显式 `.SH/.SZ`、无公式/隐藏内容/外链，并拒绝覆盖；命令中的 `received-date` 仅表示本地接收日，不是指数成分生效日：

```powershell
python -m agent.current_universe_import import `
  --source <中证800成份.xlsx> `
  --received-date 2026-08-19 `
  --output-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_membership/<membership-v2-run>

python -m agent.current_universe_import verify `
  --output-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_membership/<membership-v2-run>
```

receipt 固定 `source_authenticated=false`、`membership_basis=current_not_pit`，并明示缺行业、历史成分、PIT、全收益基准和财务能力；文件脚注与 SHA-256 不能证明官方来源。

16列当前快照必须另行绑定已验证的成分artifact导入。导入器锁定2026-08-18市场字段的复权口径、800只完整覆盖、11个中证2021一级行业、无公式/外链及证券代码和名称一致性；它不会把“最新”ST或无有效日期的行业字段升级为历史PIT数据：

```powershell
python -m agent.current_industry_import import `
  --source <中证800成份.xlsx> `
  --membership-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_membership/<membership-v2-run> `
  --received-date 2026-08-19 `
  --output-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_industry/<industry-v2-run>

python -m agent.current_industry_import verify `
  --membership-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_membership/<membership-v2-run> `
  --output-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_industry/<industry-v2-run>

python -m agent.current_industry_import freeze-sample `
  --membership-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_membership/<membership-v2-run> `
  --industry-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_industry/<industry-v2-run> `
  --output-dir data/market_data/archives/strategy_workspace/quality_growth_v1/diagnostic/<sample-v2-run>

python -m agent.current_industry_import verify-sample `
  --membership-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_membership/<membership-v2-run> `
  --industry-dir data/market_data/archives/strategy_workspace/quality_growth_v1/current_industry/<industry-v2-run> `
  --sample-dir data/market_data/archives/strategy_workspace/quality_growth_v1/diagnostic/<sample-v2-run>
```

正式降级入口会先重放两个artifact，再按证券代码哈希和当前行业等覆盖轮转冻结恰好60只；该样本不代表中证800行业权重。旧的调用者自备universe JSON不再允许。V2 `verify-sample` 会从 membership 与 industry 两个源归档重建 `sample.json`并逐字节比较；行业receipt固定 `membership_basis=current_not_pit`、`source_authenticated=false`、`industry_effective_date=null`，安全状态固定 Paper、trade、real-money均为 `false`，LIVE为 `not_supported`。当前稳定归档位于 `data/market_data/archives/strategy_workspace/quality_growth_v1/`；原 `data/tmp/` 目录只保留为历史工作副本，不再冒充受控归档。

探针结果为 `passed`、`dependency_missing`、`network_blocked`、`not_configured` 或 `failed`。只有当次 `evidence_mode=real_provider` 且状态为 `passed`，才能描述为该次真实读取成功；Mock 结果不能替代网络证据。完整安装、数据集和离线回放说明见 [市场数据 V2](../docs/MARKET_DATA.md)。

Eastmoney 旧探针只用于复核历史诊断路径：

```powershell
python -m agent.eastmoney_source_probe `
  --stock 000333.SZ `
  --start-date 2026-07-01 `
  --end-date 2026-08-05 `
  --expected-last-date 2026-08-05
```

其成功不改变 `eastmoney_legacy=diagnostic_only`，也不覆盖历史密封观察中的失败状态。

## 三层市场观察

以仓库中现有密封观察为基准生成刷新：

```powershell
python -m agent.market_observation_pipeline `
  --input data/inbox/market_observation/2026-08-06-preopen.draft.json `
  --previous data/signals/cn-market-2026-08-05-close.sealed.json `
  --previous-manifest data/actions/cn-market-2026-08-05-close.manifest.json `
  --signals-dir .tmp/market-observation-doc-example/signals `
  --manifest-dir .tmp/market-observation-doc-example/actions `
  --dashboard-dir .tmp/market-observation-doc-example/reports
```

后续观察必须显式传入上一期密封 observation 及其 manifest，不能自动把 `latest` 当真源。标准流水线拒绝未来证据、同名异载荷、非空 `trade_action`、不匹配 manifest 和旧观察回退 latest；历史密封文件不因市场数据 V2 被重写。

若观察需要绑定 Registry 已验证批次，可重复传入 `--market-data-batch`，并用同一个显式根校验 receipt：

```powershell
python -m agent.market_observation_pipeline `
  --input data/inbox/market_observation/2026-08-06-preopen.draft.json `
  --previous data/signals/cn-market-2026-08-05-close.sealed.json `
  --previous-manifest data/actions/cn-market-2026-08-05-close.manifest.json `
  --signals-dir .tmp/market-observation-doc-example/signals `
  --manifest-dir .tmp/market-observation-doc-example/actions `
  --dashboard-dir .tmp/market-observation-doc-example/reports `
  --market-data-batch data/market_data/validated/<provider>/<dataset>/<cache-key>/<batch-id>.json `
  --market-data-storage-root data/market_data
```

路径中的占位符须替换为探针实际生成的路径；自签文件和 `test_injected` receipt 不会被正式读取。

新流水线生成 manifest v0.3；Dashboard 对历史 manifest v0.2 只读兼容。v0.3 在可识别 Git 工作树中生成，记录 commit/dirty 状态，dirty 时绑定 `git_diff_sha256`，状态未知或变化中时失败关闭。现有 observation v0.1 Schema 中的 Eastmoney quality 字段仅用于历史输入兼容，新 v0.3 展示与来源准入不读取这些字段作为 Provider 证明，而是只使用已校验批次元数据。历史密封 observation 和 manifest 不重写。

Dashboard 完全自包含，不加载 CDN 或追踪脚本。文件哈希只证明内容一致，来源准入仍以结构化批次和本地政策为准。

## 可见内容流水线

```powershell
python -m agent.deepvan_daily_pipeline `
  --visible-text data/inbox/deepvan_visible_text.sample.txt `
  --captured-at 2026-07-03T09:30:00+08:00
```

输入必须是用户有权读取且当时真实可见的内容。流水线不会绕过登录、付费、验证码、权限或访问控制。

## 边界

- 不把采集时间伪装成发布时间，字段不确定时保留不确定性。
- 不把作者观点、Provider 名称或一次成功调用当作行情真值、财务真值或交易信号。
- 外部失败必须保留 `dependency_missing`、`network_blocked`、`not_configured` 或 `failed`，不能补造数据。
- 生成物写入受控数据目录，业务规则留在代码和版本化配置中。
- 本层不产生订单；LIVE 永久不支持。
