# 研究层

`research/` 负责宏观、行业和个股层面的可解释中低频研究。目前包含行业变化雷达 R0 与研报审计 V1。

## 行业变化雷达 R0

入口：`python -m research.industry_radar`

它使用趋势、广度、参与度、基本面和拥挤度特征生成行业状态与冲突提示。配置状态明确为 `heuristic_baseline_not_alpha`；输出是研究诊断，不是经过样本外验证的买卖建议。

```powershell
python -m research.industry_radar `
  --input data/industry/industry_radar.sample.json `
  --output data/reports/industry/local-run.md `
  --json-output data/reports/industry/local-run.json `
  --config configs/industry_radar.r0.json
```

## 宏观—行业—个股研报审计 V1

入口：`python -m research.broker_report_audit`

详细的数据流、固定产物和信任边界见 [broker_report_audit/README.md](broker_report_audit/README.md)。

内部模块：

- `sources.py`：报告、行情、交易日历和真值来源边界。
- `extractors.py`：本地确定性文本/PDF claim 抽取。
- `models.py`、`storage.py`：版本化领域模型与 SQLite 存储。
- `evaluation.py`：基本面与市场真值评价。
- `skills.py`：券商、分析师和团队的收缩技能估计。
- `factors.py`：B0/B1/B2/M1 和滚动样本外研究。
- `reporting.py`：固定 11 项产物。
- `validation.py`：抽取精度、manifest、来源和完整性门禁。
- `cli.py`：标准受控入口。

```powershell
python -m research.broker_report_audit audit `
  --dimensions macro,industry,stock `
  --as-of 2026-08-04

python -m research.broker_report_audit build-factor --as-of 2026-08-04
python -m research.broker_report_audit deep-read --as-of 2026-08-04 --limit 20
```

当前状态是安全的诊断框架：代码、产物契约、时点门禁和离线复现路径已实现，但受控官方真值、正式交易日历、客观因子和行业映射尚未接齐。因此不能把空表、诊断表或测试夹具解释为真实券商准确率，也不能宣称 M1 已提供增量 Alpha。

## 研究规则

- 三个维度的准确率独立保存；基本面命中与市场有效性独立保存。
- 只有到期、可证伪且真值已在评价时点可用的 claim 才能更新技能。
- 无变量、无期限的空泛表述进入异常或不可评分集合。
- 只信任标准 CLI 和可验证 manifest；内部 Python API 不是正式数据准入边界。
- 新模型必须先有无研报 B0 基线、固定样本外窗口、交易成本和行业集中度检查。
