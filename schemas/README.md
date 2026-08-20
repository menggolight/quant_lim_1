# Schema 目录

`schemas/` 定义跨进程、跨模块或跨运行环境交换的结构契约。Schema 校验通过只证明形状符合要求，不证明来源真实、批次完整、时点正确或已获研究/交易准入。

## 市场数据 V2

- `market_data_batch.v1.json`：统一批次信封，绑定 Provider、真实上游、请求指纹、可用时间、raw/normalized SHA-256、记录数、准入状态和问题列表。
- `daily_bar.v1.json`：沪深市场日线规范记录；价格和数量使用十进制字符串，复权状态显式记录。
- `trade_calendar.v1.json`：Provider 交易日历记录；工作日近似不能冒充正式日历。
- `security_master.v1.json`：沪深市场证券基础信息记录；北交所未接入，当前快照不能自证历史 point-in-time 股票池。

JSON Schema 负责结构，`research/market_data/validation.py` 负责请求证券一致性、日期唯一和升序、窗口范围、OHLC、非负成交量/成交额、缺字段和非法数字。`admission.py` 再按数据集重新计算本地准入。

## 其他契约

- `strategy_quality_growth_policy.v1.json`：A股小资金质量成长V1的策略政策形状；运行时还会精确校验因子、成本、风险和准入常量，Schema通过不能自行解锁研究。
- `strategy_experiment.v2.json`：append-only正式实验预注册，绑定动态PIT成分、Choice全收益基准、D+1开盘到D+21开盘的20区间标签与锚点、六因子/五控制、带截距的固定Ridge、100万元Top Decile研究本金、基础/压力成本、美的外部持仓、11项历史门及数据/代码/配置哈希。统计契约还冻结 Andrews 自动HAC滞后、最少2个可用时段、Holm `alpha=0.05`、验证/锁定测试/审计Rank IC、锁定测试+审计因子显著性以及金融2因子/非金融6因子子模型。
- `choice_quality_growth_gate.v1.json`：完整Choice单源能力receipt，绑定覆盖区间、行数、完整枚举的 `subject_ids`、字段、内容哈希、中证800全收益 open/close 和 `single_quarter`/`consolidated`/`CNY` 财务口径；聚合数量不能代替主体明细，能力契约通过也不等于实时连通、正式真值、正式回测或Paper准入。
- `choice_quality_growth_batch.v1.json`：固定Choice中证800历史采集manifest；绑定内部20交易日网格、exact 800成分、qfq/none双口径、分批执行日资格快照、raw/normalized重放与checkpoint。固定 `source_authenticated=false`、日历/PIT/行业阻塞，不能解锁Paper或交易。
- `strategy_current_membership_receipt.v1.json`：已知 V1 当前成分诊断receipt的只读兼容契约；不再签发新产物。
- `strategy_current_membership_receipt.v2.json`：Choice终端两列中证800工作簿的当前成分诊断receipt；绑定原始文件、固定模板、800个唯一代码、Schema与生成代码bundle，固定 `source_authenticated=false`、`membership_effective_date=null`。
- `strategy_current_industry_receipt.v1.json`：已知 V1 当前行业诊断receipt的只读兼容契约；不再签发新产物。
- `strategy_current_industry_receipt.v2.json`：绑定已验证 membership V2 artifact的16列Choice当前快照；锁定800只完整映射、exact 11个中证2021一级行业、市场快照日期、信息截止日以及 artifact/payload/content/code-bundle 哈希，安全状态不可提升。
- 调用方自行构造的旧 `strategy_current_universe_input.v1` 已被拒绝且不再保留 Schema；降级诊断必须通过 `agent.current_industry_import freeze-sample` 重放受控 membership 与 industry import 目录，不能用自报 receipt/hash 替代。
- `strategy_current_universe_diagnostic.v1.json`：旧单文件60只样本的只读兼容契约；不再签发新产物。
- `strategy_current_universe_diagnostic.v2.json`：降级路径的 `sample.json + manifest.json` 双文件契约；从两个源artifact重建恰好60只的行业等覆盖轮转样本，明示非中证800比例代表样本，固定 `diagnostic_current_universe_not_pit`、`Paper=false`。
- `index_level.v1.json`：`.CSI` 指数规范 ID、交易日、close、可选 OHLC、币种、指数口径、`available_at` 和来源记录 ID；不能伪装成股票日线。
- `csi_industry_universe.v1.json`：两代中证行业系列、恰好 11 行业、语义映射、共同基准、发布/有效时间与官方文档哈希。
- `cn_equity_session.v1.json`：自然日开闭市状态、开闭市时间、公告来源和内容哈希。
- `factor_hypothesis.v1.json`：冻结候选族、公式、窗口、标签、门槛、失效条件和内容哈希。
- `subjective_thesis.v1.json`：主观方向、理由、期限和反证条件；只能追加新版本，不能覆盖旧版本。
- `stock_diagnostic_observation.v1.json`：有限个股候选的前瞻诊断病例卡；冻结原始候选、价格门、起点前复核、60 交易日标签和安全状态。它不是因子准入或交易信号。

- `broker_report_extractor_review.v1.json`：90 份本地 HTML 导出的逐字段人工审核；浏览器导出不能绕过 CLI 的 PDF、版本、population 与完整性复核。
- `official_truth_receipt.v1.json`：未来 source-owned 官方真值 transport 的 receipt 形状；当前 `admission_status=not_configured`，普通 URL、哈希、本地文件或布尔值不能自签。
- `choice_truth_candidate.v1.json`：Choice 聚合数据的隔离候选形状，固定 `diagnostic_choice_secondary_not_admitted`，不能转换为正式 `TruthObservation`。
- `market_observation.v0.1.json`：宏观—行业—个股三层诊断观察；`overall.trade_action` 必须为 `null`。历史密封文件不因 V2 被重写。
- `htsc_mquant_shadow.v1.json`：华泰 MQuant 只读 Shadow 快照；内容哈希不是券商来源认证。

## 修改规则

- 新增兼容字段可以保留版本；删除、改名、改变类型或语义必须升级主版本。
- Provider 响应必须先规范化，再接受 Schema 和领域校验；不能把 SDK 对象直接交给研究消费者。
- `market_data_batch` 中的 dataset 与 Schema 版本必须一致，正式链固定要求 `synthetic=false`。
- 新版本必须提供迁移、双读窗口或明确拒绝旧版本，并补充正常、负向、边界和旧版本测试。
- 文件 SHA-256 只证明内容一致性，不能证明来自券商、交易所、监管机构或其他官方来源。
