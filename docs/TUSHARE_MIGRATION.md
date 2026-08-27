# Choice 到期后的 Tushare 数据迁移边界

> 状态：`probe_diagnostic_inconclusive_not_admitted`。本文记录候选迁移规则，不是数据准入、来源认证、Experiment V3、Paper 或交易授权。

## 当前结论

- 用户已确认 Choice 接口权限到期。新的 Choice 网络访问必须在 SDK 导入、初始化和登录前以 `provider_access_expired` 失败关闭。
- 既有 Choice raw、quarantine、validated、诊断和归档证据继续保留；在许可证后续使用边界经人工确认前，不得把旧数据送入新的正式研究消费。保留文件和校验哈希不等于仍拥有新的使用授权。
- BaoStock 继续是 `market_data.v1` 的默认主源。Tushare 仍只保留现有 `daily_bar` 独立核验职责；本轮不创建 `market_data.v2`，也不把任何探针接口注册为正式 dataset。
- capability receipt 固定为 `capability_probe_only_not_admitted`；成功、自哈希或重放通过均不能授予正式 MarketDataBatch、Factor Registry、Experiment V3、Daily Alpha、Paper、交易或 LIVE 权限。
- 2000 积分账户已观察到部分核心接口可访问；标准财务权限因 probe normalization 缺陷仍未判定；显式 `fields` 与合法缺失值兼容正在修复；`formal_data_admission=false` 保持不变。

## Probe 与正式 Provider 的区别

| 边界 | Capability probe | 正式 Provider / admission |
|---|---|---|
| 目的 | 验证某个 Token 在小样本、固定参数下能否调用接口及返回何种结构 | 形成可被版本化 dataset 契约消费的完整数据链 |
| 网络入口 | 22-endpoint probe已停止重跑；新授权入口固定一次HTTP `trade_cal`，默认plan与replay不读Token、不导入SDK、不联网 | 尚未实现 |
| 方法范围 | 代码内固定 enum 与 SDK 映射，配置不能提供任意函数名 | 未来必须逐 dataset 独立实现和审查 |
| 产物位置 | 只允许`data/tmp/tushare-capability/<probe_run_id>/`安全子树 | 不写入`raw/quarantine/validated`、策略registry、配置或其他正式目录 |
| 结论 | endpoint capability evidence | 还需来源、PIT、完整性、Schema、版本和准入门 |
| 策略影响 | 固定为 `none` | 本轮禁止实现 |

正式 [Tushare V1 Provider](../research/market_data/providers/tushare.py) 保持 `daily_bar validation only`，能力探针不扩展它的 `supported_datasets`。

## 固定探测范围

代码白名单只包括以下只读接口：

- 基础行情与交易资格：`trade_cal`、`stock_basic`、`daily`、`daily_basic`、`adj_factor`、`suspend_d`、`stk_limit`、`namechange`。
- 指数与行业：`index_basic`、`index_daily`、`index_weight`、`index_classify`、`index_member_all`。
- 财务与披露：`income`、`income_vip`、`balancesheet`、`balancesheet_vip`、`cashflow`、`cashflow_vip`、`disclosure_date`、`fina_indicator`、`dividend`。

明确禁止预计算因子库、新闻、公告全文、实时/分钟行情及任何交易接口。`fina_indicator` 最多是 `cross_validation_candidate`，不能成为正式质量因子的唯一原始输入。

官方文档当前说明：`index_weight` 是月度指数成分和权重接口且最低 2000 积分；`index_member_all` 是申万分级行业成分，包含 `in_date/out_date` 且需 2000 积分；`cashflow_vip` 需要 5000 积分；`disclosure_date` 提供 `actual_date`。这些只是探针候选依据，真实权限与覆盖仍以本机 receipt 为准：

- [Tushare 权限说明](https://tushare.pro/document/1?doc_id=108)
- [指数成分和权重](https://tushare.pro/document/2?doc_id=96)
- [申万行业成分构成](https://tushare.pro/document/2?doc_id=335)
- [现金流量表与 VIP 说明](https://tushare.pro/document/2?doc_id=44)
- [财报披露计划](https://tushare.pro/document/2?doc_id=162)

## PIT 与行业风险

- 财务报表中的 `ann_date`、`f_ann_date`、`update_flag` 和披露计划中的 `actual_date` 必须分别统计覆盖率；任何一个字段存在都不能自动证明首次披露链完整。修订版本需按股票、报告期、报表类型和公司类型保留，缺失值不能补 0。
- `index_weight` 返回的某月截面需检查唯一成分、重复代码、权重和及实际返回日期；当前不能把“能返回一批数据”解释为中证800历史 PIT 成分完整。
- `index_member_all` 是申万行业接口。其 SW2021 分类不得伪装为 CSI 行业；切换行业体系会改变历史行业中性化、暴露控制和因子可比性，必须另立版本化决策。
- `index_basic` 只能搜索中证800价格/全收益候选。不能预先硬编码最终正确代码；若真实 receipt 找不到可验证的全收益基准，正式研究继续阻断。
- endpoint 文档、SDK 返回成功和文件 SHA-256 均不能证明官方来源认证、历史无缺口或在决策时点已经可得。

## BaoStock 独立交叉核验

仅对固定少量 `daily` 样本与现有 BaoStock `daily_bar` 做独立批次比较。比较前必须显式换算 Tushare `vol`（手）和 `amount`（千元）到项目单位，再统计 `trade_date/open/high/low/close/pre_close/volume/amount` 的逐字段差异。两侧不得合并、互补或触发字段级 fallback；本阶段不冻结自动通过阈值。BaoStock 当次不可用时只能记录 `cross_validation_not_configured`。

## 迁移决策矩阵

当前虽存在真实full-probe receipt与single-endpoint postmortem，但二者都没有形成可信endpoint capability结果；能力与PIT状态因此继续为`unknown/not_run`，不能改成正式`primary`或`admitted`。下表角色与探针配置一致；它们只是未来候选，不改变现有Tushare V1的`daily_bar validation only`职责。

| 策略数据集 | 候选接口 | 当前能力状态 | 当前 PIT 状态 | 允许的建议角色 | 当前阻塞原因 |
|---|---|---|---|---|---|
| `daily_bar` | `daily` | `unknown/not_run` | `unknown` | `phase2_validation_candidate` | Token 权限、历史覆盖及 BaoStock 独立差异尚未实测 |
| `daily_basic` | `daily_basic` | `unknown/not_run` | `unknown` | `phase2_primary_candidate` | 可用时点、单位和历史完整性尚未实测 |
| `index_membership` | `index_weight` | `unknown/not_run` | `unknown` | `phase2_primary_candidate` | 多月截面、重复、权重和及全收益基准尚未实测 |
| `industry_membership` | `index_member_all` | `unknown/not_run` | `unknown` | `diagnostic_only` | SW/CSI 体系不一致，历史 `in_date/out_date` 覆盖未知 |
| `financial_income` | `income/income_vip` | `unknown/not_run` | `unknown` | `phase2_primary_candidate` | 四类公司、版本和首披字段覆盖未知 |
| `financial_balance` | `balancesheet/balancesheet_vip` | `unknown/not_run` | `unknown` | `phase2_primary_candidate` | 四类公司、版本和首披字段覆盖未知 |
| `financial_cashflow` | `cashflow/cashflow_vip` | `unknown/not_run` | `unknown` | `phase2_primary_candidate` | 5000 权限和首披字段覆盖未知 |
| `disclosure` | `disclosure_date` | `unknown/not_run` | `unknown` | `phase2_validation_candidate` | `actual_date` 覆盖不等于首次披露链完整 |
| `corporate_action` | `dividend/adj_factor` | `unknown/not_run` | `unknown` | `phase2_validation_candidate` | 除权事件、可用时点与复权口径尚未对账 |

允许的角色全集固定为：`phase2_primary_candidate`、`phase2_validation_candidate`、`diagnostic_only`、`blocked`、`unknown`。

## 下一阶段进入条件

下一阶段只能在一次有界真实探针形成可重放 receipt 后再定范围，并至少满足：

1. required endpoint 的权限、Schema、字段覆盖、主键/版本和历史边界已有真实结果；
2. Token、错误文本、raw、manifest 和 receipt 的秘密扫描均通过；
3. 全收益基准候选、历史指数成分、行业体系及财务首披链分别有明确通过或阻断结论；
4. BaoStock 独立小样本差异已记录且未发生拼接或 fallback；
5. 新的 `market_data.v2` 决策逐 dataset 指定 provider、Schema、单位、PIT 规则、失败条件和迁移/拒绝旧版策略；
6. Experiment V3、Daily publication、Paper、交易和 LIVE 继续由各自独立门禁控制。

即使所有 endpoint 均 `passed`，本轮仍保持：`market_data.v2 not implemented`、`Tushare formal provider not implemented`、`Experiment V3 loader blocked`、`Paper not admitted`、`trade not admitted`、`real money not allowed`、`LIVE not supported`。
