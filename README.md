# Quant Strategy Workspace

这是一个面向真实资金决策、但严格停留在研究、Paper 与人工复核边界内的中低频量化策略工作区。默认模型是可解释的 OLS、Ridge 和 Fama–MacBeth，不以复杂度代替证据，也不承诺盈利。仓库永久不支持 LIVE 下单。

## 当前唯一默认主线

```text
A股中证800动态PIT股票池
  -> 六个冻结质量成长因子（首披财务）
  -> 同日去极值 / 行业与风格残差化 / Z-score
  -> Fama–MacBeth + Newey–West / train-only Ridge alpha=1
  -> D+1开盘到D+21开盘的20日标签、20日调仓、Top2整手执行
  -> 账户成本 / 美的外部持仓 / 行业集中度 / 回撤与换手门
  -> 历史全门通过后12个月Paper
  -> 仅人工真实资金候选
```

主入口是 `research.strategy_workspace`，完整契约和命令见[策略工作区说明](docs/STRATEGY_WORKSPACE.md)。冻结账户成本为佣金 `0.00018`、单笔最低 `5` 元、卖出税 `0.0005`、双边过户费 `0.00001`、基础单边滑点 10 bps；压力情景为20 bps和双倍佣金。

[自适应仓位 V2](docs/ADAPTIVE_EXPOSURE_V2.md) 是独立、非默认的日频信号生产系统：P0.1 七项执行问题已经修复并冻结，Alpha、Exposure、Portfolio Constructor、Next-session Adapter 与 12 阶段 Daily Pipeline 的代码契约已经实现。它每天可产生允许零订单的不可变 BUY/SELL/HOLD/CASH 决策；预期数据/验证失败也生成零订单 `BLOCKED` 日报。紧邻 D+1 的人工复核按 signal 全局CAS只允许一次，账户回撤由已验证账本派生，池外旧持仓保持exit-only覆盖，Paper Ledger使用canonical成本与receipt-bound收盘mark重算完整成本；通知当前仅为本地 outbox。外部受控 PIT、生产官方日历/证券规则/行情 registry、Experiment V3、正式冻结模型和预注册阈值尚未接入，2024—2025 Locked Test 未运行、未解释；`paper_eligibility=false`、`trade_eligibility=false`、`real_money_list_allowed=false`、`live_supported=false`。质量成长 V1 的默认入口、Top2/20%现金规则和既有账本语义保持不变。

当前真实状态是 `blocked_missing_pit_data`，不是“策略已跑完”。2026-08-19 Choice 只读链已真实完成当前800成分、当前一级行业、历史行业日期回显、中证800价格/全收益别名及60只股票价量采集。降级样本完整覆盖2026-02-24至2026-08-18的121个共同交易日，60只均生成六个技术诊断因子；但它使用当前成分与当前行业，不是历史PIT，且相对收益使用价格指数而非正式全收益序列。当前只有单截面，没有排名、历史回测、Paper证书或股票清单。

查看当前因子目录，以及把真实探针绑定成状态产物：

```powershell
python -m research.strategy_workspace catalog

python -m research.strategy_workspace quality-status `
  --policy configs/strategy_quality_growth.v1.json `
  --daily-bar-probe data/tmp/strategy-workspace/quality-growth-v1/capability/choice_daily_bar.retry4-20260819.json `
  --trade-calendar-probe data/tmp/strategy-workspace/quality-growth-v1/capability/choice_trade_calendar.retry4-20260819.json `
  --historical-sector-probe data/tmp/strategy-workspace/quality-growth-v1/capability/choice_historical_sector.csi800-20260819.json `
  --output data/tmp/strategy-workspace/quality-growth-v1/current_status.v6.json
```

该状态命令在数据门阻塞时返回非零退出码。旧中证行业 `RM20` 2017—2023 负结果保留为历史反证和兼容测试，不再是默认策略主线，也不能通过调参复活。

## 冻结兼容区

原有 Factor Lab、研报审计、市场观察、行业雷达和合成执行内核保留为证据适配器、历史复现或安全兼容层，不再作为默认策略开发入口。它们不会被物理删除，以免破坏已有产物、测试或用户未提交改动；新功能只进入 `research/strategy_workspace/`。

## 兼容能力

| 模块 | 当前边界 |
|---|---|
| Strategy Workspace | A股质量成长六因子、Experiment v2、Choice数据门、PIT截面、线性检验、100万元Top Decile/1万元Top2成本账本和append-only Paper账本内核已实现；当前60只非PIT价量诊断已真实运行，正式质量成长PIT数据与正式回测仍未跑，Stage B因 `blocked_missing_controlled_paper_signal_adapter` 和 `blocked_missing_daily_paper_risk_marks` 保持阻塞 |
| 自适应仓位 V2（非默认） | P0.1执行内核、五模块、不可变日报、本地outbox、紧邻D+1一次性人工复核、人工成交证据与独立日频账本已实现；外部受控PIT、生产官方registry、正式冻结模型/阈值、Experiment V3、Locked Test与Paper准入均未接入或未运行 |
| 市场数据 V2 | Provider Registry、BaoStock 日线/交易日历/证券基础信息、raw/quarantine/validated、离线回放和结构化探针；真实连通状态以当次探针为准 |
| 中证行业 Factor Lab V1 | 引擎、Provider、固定 `RM20/RM60/RM120`、预注册 Screen/Confirm、主观假设卡和每周诊断报告已实现；尚无真实统计通过或研究准入结论 |
| DeepVan 采集 | 整理人工有权读取的可见文本或 OCR 内容，不绕过登录、权限或付费限制 |
| 行业变化雷达 R0 | `heuristic_baseline_not_alpha`，只做启发式研究排序 |
| 研报审计 | V2 默认使用 Market Data Registry；V1 可显式复现。整体仍为 `research_only_not_trade_eligible` |
| 三层市场观察 | 生成诊断观察、不可变历史快照和本地 Dashboard，不产生交易动作 |
| 小资金执行层 | 新质量成长 Top2 账本处理整手、费用、停复牌/涨跌停、外部美的持仓和风险门；append-only Paper账本可重放费用/持仓，但不能自证信号来源或日频回撤，仍是研究/Paper、不能下单 |
| 华泰 MQuant | 只读 Shadow，未完成官方客户端授权和人工核对时保持 `blocked` |

## 明确不能做什么

- 不支持真实下单、撤单或自动授权；LIVE 永久返回 `live_not_supported`。
- 不把单元测试、Mock、SDK 调用成功或 SHA-256 写成真实接口已连通或官方来源认证。
- 不把当前行业分类、缺少首次披露时间的财务数据或历史回填冒充严格 point-in-time 数据。
- 不把启发式排序、空审计表、合成 Paper 数据或通过测试称为 Alpha、正式排名或可交易策略。
- 不为“挖到因子”自动海选公式、改门槛、换 holdout 或让主观观点修改客观因子分数。
- 不用默认价格、合成行情或不同 Provider 的半批拼接掩盖外部失败。
- 不把密码、验证码、账户标识、Token、Cookie 或其他绑定秘密写入仓库、日志、测试夹具和聊天；协作与修改遵守 [AGENTS.md](AGENTS.md)。

## 架构

```mermaid
flowchart LR
    A["可见材料 / Provider 原始响应"] --> B["agent：采集与编排"]
    A --> C["research：市场数据、雷达与研报审计"]
    C --> D["raw / quarantine / validated"]
    B --> E["版本化研究记录"]
    D --> E
    E --> F["Obsidian / Markdown / Dashboard"]
    E --> G["显式信号桥与风控"]
    G --> H["Paper / Read-only Shadow"]
```

详细依赖方向、时点规则和失败关闭路径见 [架构说明](docs/ARCHITECTURE.md)。

Factor Lab 与行业雷达、研报因子和执行层隔离；使用方法、固定门槛和九项产物见[中证行业因子挖掘器 V1](docs/FACTOR_LAB.md)。

## 安装与快速开始

`pyproject.toml` 要求 Python 3.10 或更高版本。基础导入不要求安装全部市场数据 SDK；BaoStock 按 extra 安装：

```powershell
python -m pip install -e ".[market-baostock]"
```

运行市场数据专项与完整测试：

```powershell
python -m unittest discover -s tests -p "test_market_data*.py" -v
python -m unittest discover -s tests -v
```

运行一次真实、只读的 BaoStock 日线探针：

```powershell
python -m agent.market_data_probe `
  --provider baostock `
  --dataset daily_bar `
  --instrument 000333.SZ `
  --start-date 2026-07-01 `
  --end-date 2026-08-05
```

输出会如实区分 `passed`、`dependency_missing`、`network_blocked`、`not_configured` 和 `failed`。Mock 测试不能替代该探针。安装、离线回放和限制见 [市场数据 V2](docs/MARKET_DATA.md)。

研报审计标准路径使用 V2 默认配置：

```powershell
python -m research.broker_report_audit audit `
  --dimensions macro,industry,stock `
  --as-of 2026-08-04
```

如需复现历史 V1 行情语义，必须显式指定配置：

```powershell
python -m research.broker_report_audit audit `
  --config configs/broker_report_audit.v1.json `
  --dimensions macro,industry,stock `
  --as-of 2026-08-04 `
  --offline
```

## 数据源状态

| 来源 | 角色 | 当前声明 |
|---|---|---|
| BaoStock | 默认市场数据主源 | Provider 已实现；SDK/网络/真实响应须由本机探针确认，不是官方真值认证 |
| Choice | 质量成长正式链的许可只读单源候选 | 2026-08-19 三项真实只读连接探针已通过，正确中证800板块查询为800只；当前工作簿也已验证800只，但均仅为 Secondary/diagnostic。完整历史PIT面板、行业/交易状态、首披财务及正式适配器仍缺，不能用 BaoStock 补缺 |
| Tushare | 可选日线核验源 | 需要 `market-tushare` extra 和 `TUSHARE_TOKEN`；不影响 BaoStock 主流程 |
| AKShare | 受控扩展骨架 | 当前没有已配置数据集；Eastmoney/`*_em` 接口不得进入准入路径 |
| Eastmoney 行情 | Legacy 诊断 | `default_provider=false`，不进入 V2 默认、fallback 或 validated 主链 |
| Eastmoney 研报 | 公开可获取样本 | 仅 `publicly_retrievable_sample_only`，不代表券商全部研报，与行情来源解耦 |

## Paper、Shadow 与 LIVE

- 质量成长 V1 Paper 账本可验证 append-only 哈希链、费用、成交、未成交、持仓和现金重放；当前每决策点的 signal/model/source 哈希仍由调用者提供，且缺日频NAV/回撤盯市，因此准入固定 `blocked_missing_controlled_paper_signal_adapter` 与 `blocked_missing_daily_paper_risk_marks`。
- 自适应仓位 V2 使用独立日频账本，能从人工成交和收盘 mark 对账成本后 NAV、回撤、风险锁定和退出重试；五模块与 Daily Pipeline 的实现不能反向补足 V1 的 Stage B 证据，也不代表生产日跑、收益回测、券商授权或真实成交。所有 `paper_eligibility`、`trade_eligibility`、`real_money_list_allowed` 和 LIVE 权限保持关闭。
- Shadow 只读取并校验本地快照；未认证来源、账户绑定或不完整快照一律拒绝。
- LIVE 不在仓库能力范围内。枚举即使为兼容保留，所有入口也必须统一拒绝，令牌、白名单和 readiness 均不能解锁。

## 文档导航

- [项目交接状态](docs/STATUS.md)
- [项目决策记录](docs/DECISIONS.md)
- [自适应仓位 V2](docs/ADAPTIVE_EXPOSURE_V2.md)
- [市场数据 V2](docs/MARKET_DATA.md)
- [中证行业因子挖掘器 V1](docs/FACTOR_LAB.md)
- [项目架构与数据契约](docs/ARCHITECTURE.md)
- [采集与编排层](agent/README.md)
- [研究层](research/README.md)
- [研报审计](research/broker_report_audit/README.md)
- [数据目录](data/README.md)
- [配置目录](configs/README.md)
- [Schema 目录](schemas/README.md)
- [测试目录](tests/README.md)
- [外部集成](integrations/README.md)
- [交易执行层](trading/README.md)
- [项目文档索引](docs/README.md)
