# 宏观—行业—个股研报审计

本包把公开可获取的券商研报拆成可证伪主张，并分别评价基本面兑现与市场结果。正式审计、Choice 诊断和人工审核是三条独立链路，不能互相改名或借用准入状态。

当前安全状态固定为 `research_only_not_trade_eligible`。正式基本面榜仍需要通过人工抽取 Manifest、官方首次发布真值与严格时点行业映射；Choice 只提供聚合型 Secondary 诊断，不是官方真值。

## 三条标准链路

| 链路 | 标准入口 | 当前边界 |
|---|---|---|
| 正式审计 | `audit`、`build-factor`、`deep-read` | 固定 11 项产物；门禁不齐时输出空榜、异常与 `not_admitted` |
| Choice 市场诊断 | `diagnostic-market` | 独立 7 项 bundle；只评价市场结果，状态固定为 `diagnostic_choice_secondary_not_admitted` |
| 抽取人工验收 | `prepare-validation`、`finalize-validation` | 确定性抽取每维 30 份、只处理 90 份 PDF；审核不完整或阈值不足时拒绝生成 passing v3 Manifest |

东方财富研报中心仅是 `publicly_retrievable_sample_only` 的公开样本，不代表券商全部报告。研报来源、Choice SDK 与 Eastmoney Legacy 行情互不构成来源认证。

## 正式审计

默认配置为 `configs/broker_report_audit.v2.json`，行情通过 Market Data Registry 读取 BaoStock 主链：

```powershell
python -m research.broker_report_audit audit `
  --dimensions macro,industry,stock `
  --as-of 2026-08-11

python -m research.broker_report_audit build-factor --as-of 2026-08-11
python -m research.broker_report_audit deep-read --as-of 2026-08-11 --limit 20
```

V1 Eastmoney 行情语义只允许显式离线复现：

```powershell
python -m research.broker_report_audit audit `
  --config configs/broker_report_audit.v1.json `
  --dimensions macro,industry,stock `
  --as-of 2026-08-11 `
  --offline
```

正式文件名固定为：

```text
macro_accuracy.csv
industry_accuracy.csv
stock_accuracy.csv
broker_skill_cube.csv
three_layer_factor.csv
factor_walk_forward_report.md
three_layer_dashboard.md
deep_read_queue.md
source_coverage.csv
exceptions.csv
run_manifest.json
```

三张准确率表永久独立。只有已经到期、真值在评价时点可见、来源及抽取均通过门禁的结果才能更新技能；空表或 CLI 退出码 0 不代表已有有效排名。

## 第一阶段：Choice 市场诊断

`diagnostic-market` 必须显式调用 Choice，不改变 BaoStock 默认 Provider，也不允许自动 fallback、半批拼接或正式真值升级：

```powershell
python -m research.broker_report_audit diagnostic-market `
  --as-of 2026-08-11 `
  --db .tmp/broker_report_user_run_20260811/audit.sqlite3 `
  --cache-dir .tmp/choice_diag_cache `
  --output-dir .tmp/broker_report_user_run_20260811/choice_diagnostic_output `
  --max-requests-per-minute 300 `
  --max-pdf-candidates 20 `
  --max-recommendations 5
```

默认冻结 `2024-07-01..2025-06-30` 样本中的 `stock_rating` 与 `target_price`；当前受控样本为 16,670 条、2,291 只股票，实际计数和 population hash 以每次 manifest 为准。股票使用 Choice `qfq`，沪深300使用 `none`，执行窗为受控交易日历确认的起始开盘至第 120 个交易日收盘，并计算几何相对收益。日期只有自然日的报告使用其已记录的候选开盘日，不再重复向后推进。

评级可按方向评价；名义目标价不能直接与事后前复权价格水平比较，因此当前目标价行会带明确排除原因，不产生伪造命中率。券商与分析师/团队分表；同名分析师按券商隔离，同日同标的同方向观点在跨券商层面做共识折扣，只有有效样本量至少 5 的同类 cell 才排名。

独立 bundle 固定为：

```text
choice_claim_market_outcomes.csv
choice_broker_accuracy.csv
choice_analyst_accuracy.csv
choice_source_coverage.csv
choice_exceptions.csv
choice_reading_queue.md
run_manifest.json
```

抓取逐标的持久化并支持断点续跑；连续账户级或网络失败会开启 circuit breaker，剩余主张逐条保留相同失败状态。`--offline` 只重放已校验的 Choice Secondary 批次。阅读候选另取评价时点前 210 天且严格晚于历史技能样本结束日的近期报告，只使用既有历史诊断技能筛选；若两个窗口无可用间隔则为 0。最多下载 20 份 PDF、推荐 5 份。PDF SHA-256、与主张方向/数值一致的证据段、`why_read` 或 `might_change` 任一缺失时减少数量或返回 0。

Choice `10001029 data limit exceeded` 单独记录为 `failed/quota_exhausted`，连续三次后停止继续消耗接口。由于逐代码顺序下的配额截断不是随机样本，该类 run 只保留逐主张 outcome 和覆盖率；技能表中的原始命中率、后验、保守下界、排名及阅读推荐全部置空/关闭，状态为 `partial_quota_truncated_not_rankable`。普通非截断 run 中 ESS 小于 5 的 cell 也只保留覆盖统计，不输出后验或下界。

## 第二阶段：90 份人工审核

先确定性选择宏观、行业、个股各 30 份，只下载这 90 份 PDF 并生成一个完全离线 HTML：

```powershell
python -m research.broker_report_audit prepare-validation `
  --as-of 2026-08-11 `
  --db .tmp/broker_report_user_run_20260811/audit.sqlite3 `
  --cache-dir .tmp/broker_report_user_run_20260811/validation_cache `
  --output-dir .tmp/broker_report_user_run_20260811/validation_review
```

页面显示 PDF、元数据、完整 claim set、全部抽取字段以及每条 claim 的 `evidence_source_kind/hash`、extractor、parser、prompt 和 bundle hash。审核者必须填写 `reviewer_id`，并对每个字段明确选择 `pass`、`correct` 或 `reject`；90 份未全部完成时浏览器不能导出 review JSON。

导出后由 CLI 重新读取当前 PDF、population、claim 载荷和版本，不能只信任浏览器文件：

```powershell
python -m research.broker_report_audit finalize-validation `
  --as-of 2026-08-11 `
  --db .tmp/broker_report_user_run_20260811/audit.sqlite3 `
  --output-dir .tmp/broker_report_user_run_20260811/validation_review `
  --review <浏览器导出的 extractor-review.json>
```

v3 要求每维恰好 30 份、元数据匹配率 100%、完整 claim set，并把 `textual/pdf` 与 `structured/source_record` 分开验收：PDF 通道必须对每个 extraction field 至少有 30 个决策且精确率不低于 95%，完整 population 中出现的其他证据通道也必须独立达到同一门槛，不能用结构化记录给 PDF parser 背书。Manifest 逐 claim 绑定 source kind/hash、extractor、parser、prompt 与 bundle hash；低召回、PDF 被替换、版本变化、篡改、不完整审核或样本不足都会失败关闭。生成 passing Manifest 后无需修改版本化配置，可显式锚定到正式命令；路径与外部 SHA-256 必须同时提供，CLI 不会替调用者自算哈希来冒充外部锚点：

```powershell
python -m research.broker_report_audit audit `
  --as-of 2026-08-11 `
  --validation-manifest <extractor_validation.v3.json> `
  --validation-manifest-sha256 <文件SHA-256>
```

人工 Manifest 只解决抽取验证，不能替代官方真值。

## 官方真值与 Choice 候选

`official_truth.py` 已定义 receipt、来源域名、时点、版本、单位和修订的失败关闭契约，但当前没有 source-owned 官方网络 transport；所有调用者字节、本地 JSON、布尔值、URL 字符串、Choice 候选，以及直接写入旧 SQLite 的 `evidence_verified=true` 行都返回或按 `not_configured` 拒绝，不会解锁正式评分。未来实施顺序仍是 CNINFO 首次披露、交易所/中证/申万历史映射与指数、统计局/央行/ChinaMoney 首次发布。

Choice `SW2021`、`sector` 与 EDB 发布日期证据进入独立 `diagnostic_current_only` 候选存储。它们可以发现或交叉核验，但不能写入正式真值表；具体探针见 [市场数据 V2](../../docs/MARKET_DATA.md)。

## 模块职责

| 模块 | 职责 |
|---|---|
| `sources.py` | 东方财富公开样本、Legacy 来源与本地诊断真值导入 |
| `extractors.py` | 结构化记录及 PDF 文本的确定性抽取和版本化缓存键 |
| `models.py`、`storage.py` | append-only 领域模型、版本迁移与 SQLite 存储 |
| `evaluation.py`、`skills.py` | 基本面/市场独立评价、时间衰减、收缩、保守下界与共识折扣 |
| `choice_diagnostic.py` | Choice Secondary 市场结果、技能表、阅读候选与 7 文件 bundle |
| `validation_review.py`、`validation.py` | 90 份 HTML 审核、v3 Manifest 与运行时门禁 |
| `official_truth.py` | 官方 receipt 契约及 Choice 候选隔离；真实 transport 尚未配置 |
| `reporting.py` | 正式 11 文件 bundle、Dashboard 与深读清单 |
| `cli.py` | 六个标准子命令及受控运行参数 |

## 验证

```powershell
python -m unittest discover -s tests -p "test_market_data*.py" -v
python -m unittest discover -s tests -p "test_broker_report_audit*.py" -v
python -m unittest discover -s tests -v
python -m compileall agent research trading integrations tests
git diff --check
```

涉及 bundle 时必须在代码和 Git 状态稳定后从相同输入离线双跑，核对 run ID 与全部文件 SHA-256；网络成功、Mock、哈希或测试通过都不能跨级写成统计有效或可交易。
