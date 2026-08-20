# 研究层

`research/` 的唯一默认量化策略入口是 `python -m research.strategy_workspace`。当前主线为 A 股中证800质量成长 V1：六个首披财务因子、PIT截面残差化、固定线性模型、100万元 Top Decile与1万元Top2成本账本，以及append-only Paper账本内核。后者尚缺受控信号适配和日频盯市，不等于两阶段准入已跑通。研究模块不能调用券商写接口，也不能把诊断产物直接转换成订单。

`research.strategy_workspace.adaptive_exposure` 与 `paper_ledger_v2` 是独立、非默认的自适应仓位V2 P0运行时：前者冻结并校验政策哈希，后者逐受控交易日重算Paper账户与风险退出证据。它们不生成Alpha或仓位状态，不读取锁定测试收益，不替代V1入口，也不授予Paper/交易权限。完整边界见[自适应仓位 V2](../docs/ADAPTIVE_EXPOSURE_V2.md)。

## 默认主线：Strategy Workspace

```powershell
python -m research.strategy_workspace catalog
python -m research.strategy_workspace quality-status `
  --policy configs/strategy_quality_growth.v1.json `
  --daily-bar-probe data/tmp/strategy-workspace/quality-growth-v1/capability/choice_daily_bar.retry4-20260819.json `
  --trade-calendar-probe data/tmp/strategy-workspace/quality-growth-v1/capability/choice_trade_calendar.retry4-20260819.json `
  --historical-sector-probe data/tmp/strategy-workspace/quality-growth-v1/capability/choice_historical_sector.csi800-20260819.json `
  --output data/tmp/strategy-workspace/quality-growth-v1/current_status.v6.json
```

2026-08-19 Choice 只读连接、当前800成分/行业、历史行业日期回显及中证800价格/全收益别名均已真实验证。当前60只降级样本也已完整采集2026-02-24至2026-08-18的121个共同交易日，并生成单截面六个技术诊断因子。它仍是当前成分、当前行业和价格指数口径，不是历史PIT或正式全收益回测；没有排名、Paper准入或买入建议。正式状态因此仍是 `blocked_missing_pit_data`。Choice正式 receipt 还必须枚举完整历史成分 `subject_ids`、全收益基准 open/close、以及 `single_quarter`/`consolidated`/`CNY` 的首披财务口径；当前仍缺把真实响应升级为正式真值的受控适配器。账户成本冻结为佣金 `0.00018`、最低 `5` 元、卖出税 `0.0005`、双边过户费 `0.00001` 和基础单边滑点10bps。标签口径固定为 D+1开盘到D+21开盘的20个收益区间。完整契约、状态与命令见[量化策略工作区](../docs/STRATEGY_WORKSPACE.md)。

`ExperimentSpec v2` 还会冻结 Andrews 自动 HAC 滞后、最少2个可用时段、Holm `alpha=0.05`、验证/锁定测试/审计三段 Rank IC（均值>0、正值占比≥0.5）、因子在锁定测试与审计段同时显著、金融2因子/非金融6因子子模型及带截距 Ridge。降级诊断也只接受恰好800只当前中证800成分和匹配的成分/行业 receipt 与内容哈希。

质量成长V1的append-only Paper账本已能重放费用、成交/未成交、持仓、现金和哈希链；但每决策点 signal/model/source 哈希仍由调用者提供，且缺日频NAV/回撤盯市，所以其 Stage B 固定 `blocked_missing_controlled_paper_signal_adapter` 与 `blocked_missing_daily_paper_risk_marks`。V2独立日频账本不能反向补足V1证据；当前不存在可达的真实资金候选。LIVE 永久不支持。

## 非默认模块的统一边界

下列模块保留用于历史复现、证据适配或防回归，不物理删除，也不再独立发展策略主线：Market Data V2 只提供证据边界；Factor Lab V1 是冻结兼容层；个股诊断、行业雷达、研报审计及 `agent` 的 market observation 只用于观察或审计。新观点、因子、线性检验、成本化回测和归因默认进入 `strategy_workspace`。

## 市场数据 V2（证据适配）

`research/market_data/` 是 Provider 与研究消费者之间的统一边界：

- `contracts.py`：`MarketDataRequest`、`MarketDataBatch`、十进制和确定性序列化；
- `registry.py`：Provider 注册、选择、整批 fallback、校验与准入编排；
- `validation.py`：按数据集做规范化和领域校验；
- `admission.py`：重新计算 dataset-specific admission，不信任调用方自报状态；
- `storage.py`：raw、quarantine、validated 分层和离线回放；
- `providers/`：BaoStock 主源、Choice 股票/沪深300/日历许可只读 Secondary、Tushare 可选核验、AKShare 骨架和 Eastmoney Legacy 元数据。
- `choice_candidates.py`：Choice SW2021、sector、EDB 的隔离候选证据；固定 `diagnostic_current_only`，不进入正式真值或研究读取。

默认 Provider 是 BaoStock。Provider 可调用、批次通过校验、获本地研究准入、真实接口连通和官方真值认证是不同状态。完整说明见 [市场数据 V2](../docs/MARKET_DATA.md)。

## 中证行业 Factor Lab V1（冻结兼容）

入口：`python -m research.factor_lab`

`factor_lab/` 独立于行业雷达和研报审计，只研究中证一级 11 行业的 `RM20`、`RM60`、`RM120`。Choice 旧系列只做长历史 Screen，中证当前系列只做独立 Confirm，两代指数不得拼接；主观假设卡只决定研究方向，与客观排名并列展示。

```powershell
python -m research.factor_lab inventory
python -m research.factor_lab preregister
python -m research.factor_lab screen
python -m research.factor_lab confirm
python -m research.factor_lab weekly
python -m research.factor_lab verify
```

每次研究运行固定生成九项产物。数据来源认证、完整覆盖、预注册统计门和研究准入分别检查；任一层不足都可以得到正确的 `failed`、`blocked` 或 `data_insufficient` 结论。所有状态固定 `paper_eligibility=false`、`trade_eligibility=false`、`live_execution_status=live_not_supported`。详见[中证行业因子挖掘器 V1](../docs/FACTOR_LAB.md)。

## 个股诊断病例观察（冻结观察）

入口：`python -m research.stock_diagnostic`

该入口只密封和验证已经冻结的有限个股筛选，不联网抓行情、不重新排名，也不连接交易桥。原始入选项即使在起点前或中途失败仍保留，禁止换股或删除输家；主要结果固定为 60 个正式交易日后的个股相对行业基准方向。

```powershell
python -m research.stock_diagnostic seal --draft configs/stock_diagnostics/innovation_drug_60td.20260818.v1.json
python -m research.stock_diagnostic verify --observation <sealed.json> --manifest <manifest.json>
```

此产物固定为 `diagnostic_only_not_admitted`、`observe_only`、`trade_action=null`，不能称为已找到的个股因子、买入建议或可交易策略。

## 行业变化雷达 R0（冻结启发式）

入口：`python -m research.industry_radar`

```powershell
python -m research.industry_radar `
  --input data/industry/industry_radar.sample.json `
  --output data/reports/industry/local-run.md `
  --json-output data/reports/industry/local-run.json `
  --config configs/industry_radar.r0.json
```

当前状态固定为 `heuristic_baseline_not_alpha`。当前行业分类和成分若没有决策时点版本，只能用于诊断，不能进入严格历史回测。

## 宏观—行业—个股研报审计（冻结证据）

入口：`python -m research.broker_report_audit`

标准默认配置是 `configs/broker_report_audit.v2.json`：研报公开样本来源与市场数据来源分离，行情通过 Market Data Registry 获取。历史 V1 仍可显式复现：

```powershell
python -m research.broker_report_audit audit `
  --dimensions macro,industry,stock `
  --as-of 2026-08-04

python -m research.broker_report_audit audit `
  --config configs/broker_report_audit.v1.json `
  --dimensions macro,industry,stock `
  --as-of 2026-08-04 `
  --offline
```

研报审计保留三张独立准确率表、来源技能、三层因子、滚动样本外研究、深读清单和固定 manifest；另提供独立 Choice 7 文件诊断 bundle 与 90 份 PDF 人工审核。当前整体仍是 `research_only_not_trade_eligible`：受控官方真值、严格 PIT 行业映射和统计准入没有因为市场数据 V2 或 Choice 许可自动完成。

详细命令、V1/V2 边界和固定产物见 [broker_report_audit/README.md](broker_report_audit/README.md)。

## 研究规则

- 三个维度的原始准确率独立保存；综合因子不得反向更新它们。
- 基本面兑现与市场超额收益分开评价。
- 只有到期且真值在评价时点可用的 claim 才能更新来源技能。
- 报告来源、市场数据 Provider、真实上游和真值来源分别记录。
- 研究消费者只能读取完整且本地准入的 validated 市场数据批次；quarantine 不可读取。
- 新模型必须先定义预测对象、基准、期限、交易成本、样本外窗口和失败条件。
- `validated_research_only` 不等于 Alpha、Paper 准入或交易资格；LIVE 永久不支持。
