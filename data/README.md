# 数据目录

`data/` 保存输入证据和生成产物，不保存业务逻辑。原始响应、规范记录、研究输入、评价结果和展示文件必须分层，不能覆盖或循环引用。

## 目录职责

| 子目录 | 用途 | 默认版本控制策略 |
|---|---|---|
| `market_data/raw/` | Provider 原始响应证据 | 不提交 |
| `market_data/quarantine/` | Provider/校验失败批次和问题记录 | 不提交；研究消费者禁止读取 |
| `market_data/validated/` | 结构、领域校验通过并带本地准入状态的批次 | 不提交；读取时仍检查 admission 与 `synthetic=false` |
| `market_data/archives/` | 现有许可数据证据的内容寻址只读归档；保留 raw、receipt、quarantine 与 checkpoint | 不提交；归档哈希不等于官方认证 |
| `factor_evidence/` | Factor Lab 的 Choice/CSI/SSE raw、normalized、transport receipt、receipt 与 checkpoint | 不提交；只消费受控 CLI 产物 |
| `inbox/` | 人工可见文本或待处理 capture | 仅提交 `*.sample.*` |
| `raw/` | 其他原始抓取材料和正文 | 不提交 |
| `cache/` | 内容寻址的下载与解析缓存 | 不提交，可重建 |
| `actions/` | 日常行动摘要和受控 manifest | 仅提交脱敏样例或明确审查的密封产物 |
| `signals/` | 结构化研究信号和密封 observation | 仅提交脱敏样例或明确审查的密封产物 |
| `industry/` | 行业雷达时点输入 | 仅提交 `*.sample.*` |
| `portfolio/` | 用户本地持仓快照 | 不提交，不自动归属策略 |
| `research_reports/` | 研报审计 SQLite 数据库 | 不提交 |
| `reports/` | 模块化 Markdown/CSV/JSON/HTML 结果 | 默认只保留脱敏样例或明确密封产物 |
| `broker/` | 券商只读 Shadow 快照 | 不提交 |
| `trading/` | Paper 账本和验证报告 | 不提交 |
| `tmp/` | 测试与临时输出 | 不提交，可删除 |

## 市场数据证据链

Market Data Registry 先保存原始字节，再做规范化、Schema、领域和本地准入检查：

```text
raw -> validated
raw -> quarantine
```

同一失败响应可以保留在 raw/quarantine 用于诊断，但 HTML、错误 JSON、空批次、重复日期或非法数据不能进入 validated。quarantine 不能成为离线 fallback 或研究输入。

validated 批次分别记录 `raw_content_sha256` 和 `normalized_content_sha256`。缓存键绑定 Provider、数据集、请求指纹、适配器版本和 Schema 版本；相同请求的 `offline_replay` 仍会核对两层哈希。

## 文件约定

Factor Lab 的一次运行固定生成 `hypothesis_card.json`、`universe_manifest.json`、`source_reconciliation.csv`、`factor_observations.csv`、`weekly_metrics.csv`、`window_metrics.csv`、`exceptions.csv`、`factor_report.md` 和 `run_manifest.json`。增删或改名必须升级契约并同步 CLI、manifest、离线确定性校验、文档和测试。

- 可提交样例使用 `name.sample.ext`，并移除真实姓名、账号、Cookie、Token、持仓和订单标识。
- 时间使用带时区的 ISO 8601；交易日使用 ISO 日期并记录日历来源。
- 价格、金额和比率使用可精确恢复的十进制字符串。
- 原始证据尽量只追加；同路径异载荷拒绝覆盖。
- 生成物至少记录生产器、配置、Schema、输入哈希、运行时点、研究截止日和准入状态。
- 工作区不干净时，正式 manifest 记录 `git_diff_sha256`；commit 与 dirty 布尔值不足以证明可复现。
- 市场数据批次的 SHA-256 证明内容一致性，不证明 Provider 或官方来源身份。

三层市场观察的 `signals/<observation_id>.sealed.json`、`actions/<observation_id>.manifest.json` 和历史 HTML 是受控产物；`latest.html`/`latest.alias.json` 只是可变入口，不能反向修改历史文件或研究数据。

个股诊断病例卡同样使用 `signals/<observation_id>.sealed.json` 与 `actions/<observation_id>.manifest.json`，但采用独立的 `stock_diagnostic_observation.v1` 契约。卡片只冻结候选和评价规则；起点收盘、周度复核和到期结果必须以后续只追加产物记录，不能覆盖原卡。

`.gitignore` 只阻止新的未跟踪文件。已经进入版本历史的敏感信息必须单独清理，不能依赖新增忽略规则。
