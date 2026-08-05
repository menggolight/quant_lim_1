# 文档导航

`docs/` 保存架构、正式规格、实施计划和外部接入记录。文档说明意图和证据，但不能覆盖代码、配置、Schema 或真实运行状态。

## 架构

- [项目架构与数据契约](ARCHITECTURE.md)

## 当前规格

- [宏观—行业—个股研报审计 V1](superpowers/specs/2026-08-04-broker-report-audit-v1.md)
- [小账户自动交易研究 V1](superpowers/specs/2026-07-14-small-account-auto-trading-v1.md)
- [行业变化雷达 V1](superpowers/specs/2026-07-13-industry-change-radar-v1.md)
- [量化模型 V1](superpowers/specs/2026-07-13-quant-model-v1.md)
- [DeepVan 采集 MVP](superpowers/specs/2026-07-07-deepvan-capture-mvp.md)
- [DeepVan 日常 Agent 设计](superpowers/specs/2026-07-03-deepvan-daily-agent-design.md)

## 实施计划与接入记录

- `superpowers/plans/`：按日期保留的实施计划；完成情况以当前代码和测试为准。
- [华泰 API 接入调研](huatai/2026-07-14-huatai-api-integration.md)：历史调研材料；实际接入必须以当前官方客户端契约为准。

新增规格时应写清：研究问题、真值、可执行时点、样本范围、基线、失败条件、产物、验收和明确不做的范围。
