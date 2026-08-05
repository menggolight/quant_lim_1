# 宏观—行业—个股研报审计 V1

本包审计公开可获取的券商研报，并研究经过历史验证的来源技能是否能为中低频模型提供增量信息。它严格分开三件事：研报说了什么、后来基本面是否兑现、市场价格是否给出超额收益。

当前状态为 `research_only_not_trade_eligible`。代码框架和固定产物已实现，但真实数据准入尚未闭环，M1 必须保持 `diagnostic/not_admitted`。

## 数据流

```text
report metadata / PDF
  -> immutable source record and content hash
  -> deterministic claim extraction
  -> episode deduplication
  -> point-in-time truth and market outcome
  -> mature-only broker / analyst / team skill
  -> macro, industry and stock report factors
  -> B0 / B1 / B2 / M1 walk-forward comparison
  -> fixed report bundle and deep-read queue
```

## 模块职责

| 模块 | 职责 |
|---|---|
| `models.py` | `ResearchClaim`、`ClaimOutcome`、`SkillSnapshot`、`FactorObservation` 等领域类型 |
| `sources.py` | 公开报告、行情、交易日历与真值来源边界 |
| `extractors.py` | 本地文本/PDF 确定性抽取和版本化缓存键 |
| `storage.py` | SQLite Schema、append-only 版本和内容寻址缓存 |
| `evaluation.py` | 基本面误差/命中与市场超额收益的独立评价 |
| `skills.py` | 时间衰减、经验贝叶斯收缩、保守下界和共识折扣 |
| `factors.py` | 三层因子、两个相邻交互、滚动样本外和准入判断 |
| `reporting.py` | 固定文件输出、仪表盘和深读清单 |
| `validation.py` | 人工抽查、抽取精度、manifest、来源和时点门禁 |
| `cli.py` | `audit`、`build-factor`、`deep-read` 的标准受控入口 |

## 命令

从仓库根目录运行：

```powershell
python -m research.broker_report_audit --help

python -m research.broker_report_audit audit `
  --dimensions macro,industry,stock `
  --as-of 2026-08-04

python -m research.broker_report_audit audit `
  --dimensions macro,industry,stock `
  --as-of 2026-08-04 `
  --offline

python -m research.broker_report_audit build-factor --as-of 2026-08-04
python -m research.broker_report_audit deep-read --as-of 2026-08-04 --limit 20
```

三个子命令共同支持 `--config`、`--db`、`--cache-dir` 和 `--output-dir`。`audit` 可重复传入 `--truth-input`；`build-factor` 使用 `--factor-input` 与 `--trading-calendar`；`audit` 和 `deep-read` 支持 `--offline`。

在线东方财富请求经共享缓存客户端执行。标准 CLI 对精确接口域名启用 IPv4 直连策略，但仍使用原域名完成 Host、SNI 和证书验证；其他域名以及代理端点保持系统默认网络路径。研报分页会先完整缓冲并验证稳定 `TotalPage`、非空页和唯一报告 ID，之后才向调用者返回；分页异常不会交付半批结果。`EastmoneyIndustryBoardSource` 同样只返回总数稳定、代码唯一、完整且同一采集批次的行业榜。

默认路径由 `configs/broker_report_audit.v1.json` 定义：

```text
database:  data/research_reports/broker_report_audit.sqlite3
cache:     data/cache/broker_report_audit/
outputs:   data/reports/broker_report_audit/
```

## 固定产物

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

三张准确率表始终独立存在。综合因子、技能值或人工阅读结果都不能反向修改原始评价结果。输出为空不一定是错误：预测未成熟、真值未准入或时点证据不足时，空表加异常与 manifest 是预期的安全结果。

## 正式准入缺口

当前必须继续明确披露：

- 东方财富数据只是公开可抓取样本，不能代表券商全部报告。
- 三个维度的 30 份人工抽查与至少 95% 字段精确率尚未通过正式 manifest 验证。
- 本地 JSON/CSV 只能证明导入字节和版本，不能自证为国家统计局、央行、巨潮、交易所或协会的官方首次发布响应。
- 日期型报告需要受控的真实交易所日历；工作日近似只能用于诊断。
- 客观宏观/行业/个股因子、时点行业映射和标签来源尚未形成不可伪造的准入链。
- M1 的正式 admission 当前被代码 fail-closed；单元测试或合成 Rank IC 不能解除门禁。

因此不得把当前结果描述为“已得到真实券商准确率排名”“研报因子已产生稳定 Alpha”或“可以进入自动交易”。

## 修改与验证

修改 claim 语义、抽取器、存储版本、因子或报告契约时，先阅读根 [AGENTS.md](../../AGENTS.md)。至少运行：

```powershell
python -m unittest discover -s tests -p "test_broker_report_audit*.py" -v
python -m unittest discover -s tests -v
```

涉及正式产物时还要从相同缓存离线运行两次，逐项核对 11 个文件哈希一致。不要手改 CSV 或 manifest 来制造通过结果。
