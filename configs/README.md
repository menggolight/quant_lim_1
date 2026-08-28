# 配置目录

`configs/` 保存可版本控制的研究和执行政策。配置描述允许的 Provider、数据集和安全状态，不能证明外部来源真实，也不能绕过代码门禁。

## 当前配置

| 文件 | 职责 |
|---|---|
| `a_share_technical_momentum_adaptive.v1.json` | 当前唯一正式研究主线的冻结实验：既有六因子/Exposure参数、九类正式数据、双价格、Development/Validation/Locked物理边界与全关闭安全状态；当前数据门为 `BLOCKED` |
| `a_share_technical_alpha_feasibility.v1.json` | 独立P1可行性门：只允许7个Tushare标准只读接口与2017-07至2023-12，哈希绑定既有Alpha实际ranker/Exposure，冻结小数权重、D+1开盘时序、base/stress成本和三种终态；不授予正式数据、Paper或交易准入 |
| `strategy_adaptive_exposure.v2.json` | A股1万元自适应仓位V2的P0契约：月净收益10%仅作挑战报告，目标总仓位0%—100%、显式现金意图、风险退出优先和严格样本外边界；P0安全运行时已实现，但未实现Alpha/仓位模型且不具备Paper或交易准入 |
| `strategy_quality_growth.v1.json` | A股1万元质量成长V1的六因子、PIT数据门、Top2仓位、成本、风险、历史门、Paper与降级诊断真源；当前 `blocked_missing_pit_data` |
| `market_data.v1.json` | BaoStock 默认主源、Provider allowlist、dataset-specific admission、整批 fallback、三层存储和可选核验政策 |
| `provider_access.v1.json` | Provider访问许可边界；Choice固定为`expired`并在SDK导入前阻断新访问，历史证据保留但不进入新的正式研究消费；Tushare只允许capability probe |
| `tushare_capability_probe.v1.json` | Tushare固定endpoint、小样本参数、字段/主键/日期/单位、调用顺序和请求上限；不含Token、SDK函数名或正式准入 |
| `factor_hypotheses/csi11_relative_momentum.v1.json` | Factor Lab V1 的三候选、公式、双轨指数、标签、Screen/Confirm 窗口与不可变门槛 |
| `broker_report_audit.v2.json` | 研报审计标准默认配置；研报来源与市场数据 Registry 解耦 |
| `broker_report_audit.v1.json` | 历史兼容配置；只在调用方显式指定时复现原 Eastmoney 行情语义 |
| `industry_radar.r0.json` | 行业雷达特征、权重和阈值，状态为 `heuristic_baseline_not_alpha` |
| `small_account_trading.v1.json` | 旧1万元ETF Paper执行兼容配置；佣金已同步为万1.8/最低5元，但不是质量成长策略真源 |
| `htsc_mquant_shadow.example.json` | 华泰只读快照探针示例，未授权时保持 `blocked_pending_client_authorization` |

## 市场数据默认政策

- `default_provider=baostock`；Provider 身份不能自证官方真值。
- `daily_bar`、`trade_calendar` 和 `security_master` 的主源为 BaoStock。
- Choice代码与历史证据保留，但当前访问策略为`expired`：所有新网络入口在SDK导入前返回`provider_access_expired`，旧validated数据也不能进入新的正式研究消费。它不会触发自动fallback或与其他Provider拼接。
- Tushare V1在默认Market Data Registry中仍仅为可选日线核验源；本轮另有用户明确授权的P1独立回填配置，只能服务 `research_alpha_feasibility_only`，不能形成正式MarketDataBatch、Paper或交易准入。缺少`TUSHARE_TOKEN`时失败关闭。
- AKShare 是禁用的扩展骨架；没有显式数据集、真实上游和准入声明时不能调用。
- Eastmoney 行情固定为 `diagnostic_only`，`default_provider=false`，不能作为 fallback。
- Primary 与 Secondary 只能形成独立完整批次，不允许补行、默认数据或 synthetic 降级。

完整字段和运行方式见 [市场数据 V2](../docs/MARKET_DATA.md)。

## 修改规则

- 影响历史解释、输入字段、准入、缓存键或产物的变更必须升级配置或 Schema 版本。
- Factor Lab `confirm` 只能读取冻结卡；增加候选、修改窗口、门槛、标签、基准或 holdout 均须新建版本，不能覆盖 V1。
- 配置中只能记录环境变量名，不得保存密码、账户、Token、Cookie、验证码或绑定秘密。
- Provider `enabled=true` 只表示允许 Registry 选择，不表示 SDK 已安装、网络可达或数据已准入。
- V1 配置保留历史语义；V2 只能通过新增配置演进，且在运行时、迁移和回归证据完成前不能成为默认，更不能静默改写 V1。
- 自适应仓位 V2 的 `10%` 月收益目标只能进入报告，不能成为收益保证、模型损失函数、参数优化目标或布尔准入门；P0安全运行时存在也不代表策略有效、数据已准入或Paper已获准。
- LIVE 永久不支持。任何 `execution_status="live"`、令牌、白名单或 readiness 都不能由配置打开。
- 修改后运行对应专项测试和完整测试，并核对 README、Schema 与 CLI 默认值一致。
