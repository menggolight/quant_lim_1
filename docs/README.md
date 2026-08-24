# 文档导航

`docs/` 保存当前架构、数据边界、正式规格和外部接入记录。文档不能覆盖代码、版本化配置、Schema、标准 CLI 产物或真实探针结果。

## 当前入口

- [量化策略工作区](STRATEGY_WORKSPACE.md)：唯一默认策略主线；A股质量成长六因子、Choice PIT门、Experiment v2、Top Decile/Top2账本、两阶段Paper与当前阻塞状态。
- [自适应仓位 V2](ADAPTIVE_EXPOSURE_V2.md)：独立、非默认的日终信号系统；P0.1执行内核、五模块、12阶段编排、固定Daily publication registry、D+1人工复核与日频Paper对账已实现。2026-08-24红队补齐正式Daily/Exposure/Alpha Schema校验、authority/failure/safety与Exposure条件图绑定、Next二次复核及exact-type防子类覆写。formal loader仍阻断，正式Alpha固定 `DATA_FAIL_CLOSED`，当前只发布 `BLOCKED` 或无BUY的 `RISK_REDUCTION_ONLY`，全部准入保持关闭。
- [市场数据 V2](MARKET_DATA.md)：Strategy Workspace 的证据适配层；说明 Provider、时点、校验、准入、真实探针和离线回放。
- [Choice到期后的Tushare迁移边界](TUSHARE_MIGRATION.md)：Choice新访问失败关闭、历史证据保留、Tushare 5000能力探针与下一阶段dataset迁移门。
- [中证行业因子挖掘器 V1](FACTOR_LAB.md)：冻结兼容文档；旧候选、双轨指数和历史统计产物不再独立发展策略主线。
- [项目架构与数据契约](ARCHITECTURE.md)：目录职责、依赖方向、时点、证据层和执行边界。
- [根 README](../README.md)：项目定位、最短安装和快速开始。

## 协作与交接

- [项目交接状态](STATUS.md)：带日期的当前目标、变更范围、真实验证证据、阻塞项和建议审查范围；不替代受控状态产物。
- [项目决策记录](DECISIONS.md)：跨模块的重要选择、取舍、放弃方案与重新评估条件。

## 模块说明

- [采集与编排](../agent/README.md)
- [研究层](../research/README.md)
- [因子发现治理](../research/factor_discovery/README.md)：LLM候选、独立Validation receipt、批准因子与确定性registry的四层边界。
- [Strategy Workspace代码边界](../research/strategy_workspace/README.md)
- [研报审计](../research/broker_report_audit/README.md)
- [数据目录](../data/README.md)
- [配置目录](../configs/README.md)
- [Schema 目录](../schemas/README.md)
- [测试目录](../tests/README.md)
- [交易执行层](../trading/README.md)
- [外部集成](../integrations/README.md)

Factor Lab V1、研报审计、行业雷达、个股诊断以及 `agent` 的 market observation/dashboard 保留用于证据、复现或兼容，不物理删除，也不能直接形成交易信号。当前质量成长主线虽已完成Choice只读诊断探针，但正式PIT批次仍缺失，因而保持 `blocked_missing_pit_data` 且未运行正式回测；旧 CSI `RM20` 负结果仅为反证。Paper 尚未准入，LIVE 永久不支持。

## 历史版本化规格与接入记录（冻结）

- [宏观—行业—个股研报审计 V1](superpowers/specs/2026-08-04-broker-report-audit-v1.md)：历史 V1 语义；当前默认市场数据路径以 V2 配置和 `MARKET_DATA.md` 为准。
- [小账户自动交易研究 V1](superpowers/specs/2026-07-14-small-account-auto-trading-v1.md)
- [行业变化雷达 V1](superpowers/specs/2026-07-13-industry-change-radar-v1.md)
- [量化模型 V1](superpowers/specs/2026-07-13-quant-model-v1.md)
- [DeepVan 采集 MVP](superpowers/specs/2026-07-07-deepvan-capture-mvp.md)
- [DeepVan 日常 Agent 设计](superpowers/specs/2026-07-03-deepvan-daily-agent-design.md)
- [华泰 API 接入调研](huatai/2026-07-14-huatai-api-integration.md)：只读接入记录；当前 SDK 契约和真实运行结果优先。

已完成的一次性实施 checklist 不保留在当前文档树中；需要追溯时使用 Git 历史。新增或整理文档时，一个概念只在一个主文档完整解释；其他位置使用链接，不追加重复警告或过时 TODO。
