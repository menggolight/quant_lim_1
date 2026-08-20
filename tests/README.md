# 测试目录

测试覆盖市场数据契约与 Provider、采集流水线、行业雷达、研报审计、市场观察、Paper 执行、LIVE 永久阻断和华泰只读 Shadow。

## 核心命令

市场数据专项：

```powershell
python -m unittest discover -s tests -p "test_market_data*.py" -v
python -m unittest tests.test_factor_market_data tests.test_factor_evidence_probe tests.test_factor_lab -v
python -m unittest tests.test_choice_quality_growth_batch tests.test_market_data_choice -v
```

研报审计专项：

```powershell
python -m unittest discover -s tests -p "test_broker_report_audit*.py" -v
```

质量成长策略工作区专项：

```powershell
python -m unittest discover -s tests -p "test_strategy_workspace*.py" -v
```

该组覆盖精确六因子与首披时点、动态中证800成分、Choice完整覆盖门、Experiment v2防改写、train-only Ridge、D+1→D+21 open-to-open标签、20交易日 cadence/purge、100万元Top Decile研究账本、成功子样本拒绝、A股整手/停复牌/涨跌停/T+1、账户行业集中度、基础/压力成本、回撤/换手和 append-only Paper 账本重放。Choice 正式适配器、真实数据与正式回测尚未运行；Paper 每决策点 signal/model/source 哈希仍由调用者提供且缺日频 NAV/回撤盯市，因此这些测试不证明历史→12个月的两阶段准入已跑通，Stage B 依然 `blocked_missing_controlled_paper_signal_adapter` 与 `blocked_missing_daily_paper_risk_marks`。

数据门的负向用例还要求 Choice receipt 枚举完整成分 `subject_ids`、中证800全收益 open/close 和 `single_quarter`/`consolidated`/`CNY` 财务口径；Experiment 覆盖 Andrews HAC、Holm、三段Rank IC及金融/非金融子模型的冻结检查。降级路径还验证两列当前成分与16列行业快照的独立导入/重放、800只逐项绑定、无有效日期行业不得升级为PIT、V2 `sample.json + manifest.json` 从双源artifact逐字节重建、恰好60只与exact 11行业、行业等覆盖不得冒充指数代表性，以及调用者自备universe JSON和占位receipt哈希不能绕过；产物始终保持 Paper/trade/real-money为 `false`、LIVE为 `not_supported`。单元测试通过不等于真实数据、统计效力、Paper准入或盈利。

完整回归与编译检查：

```powershell
python -m unittest discover -s tests -v
python -m compileall agent research trading integrations
```

## 市场数据覆盖重点

- Provider 注册、未知或禁用 Provider；
- BaoStock 代码双向映射、日线、交易日历、证券基础信息和 SDK 懒加载；
- Choice SDK 懒加载、逐接口权限分类、股票 `qfq`/沪深300 `none`、独立交易日历、诊断读取与 BaoStock 默认链路隔离；
- Choice SW2021/sector/EDB 候选的固定 SDK 签名、`None`/空值失败关闭、双哈希和严格离线重放；
- Choice `10001029` 配额耗尽的独立分类、三次断路及非随机截断后排名/推荐强制关闭；
- 登录/查询失败、空结果、重复日期、错证券、非法 OHLC、负值、缺字段和非法数字；
- raw、quarantine、validated 分层及研究消费者隔离；
- 整批 fallback，不拼接 Primary 与 Secondary；
- Tushare Token 缺失不影响 BaoStock；
- AKShare 的 Eastmoney/`*_em` 准入拒绝；
- historical backfill 与 offline replay 分离；
- 本地 JSON、调用者布尔值及直接写入 SQLite 的 `evidence_verified=true` 均不能替代逐条官方 receipt 绑定；
- raw/normalized hash 变化和离线确定性。

## 两阶段研报覆盖重点

- `diagnostic-market` 的 16,670 候选一对一覆盖、交易日起点、跨券商共识折扣、同名分析师隔离、ESS 门槛、断点与 circuit breaker；
- 目标价与 `qfq` 绝对价格口径不兼容时明确排除，不伪造命中率；
- 最近报告候选与历史技能样本分离，PDF 上限 20、推荐上限 5，评级方向和目标价数值必须与 PDF 证据一致；
- 90 份审核包的确定性选择、逐 claim 证据绑定、PDF/结构化通道独立计数、未选报告通道漂移、PDF 替换、字段未完成、版本漂移、低于 95% 精确率和导出篡改；
- 官方 receipt 的伪造域名、调用者字节、本地文件、未来时间、修订冒充首次值与 Choice 候选升级均失败关闭。

## 证据边界

Factor Lab 专项覆盖未来可用时间、伪造官方来源、Choice 补中证、两代系列混合、少于 11 行业、非交易日、重复行情、换赢家、第四候选、复用 holdout、错误 IID 推断、单行业驱动、缺字段和 LIVE 绕过。单元测试中的注入 transport 永远不能用于正式离线回放或研究准入。

普通单元测试不得访问网络，Provider 使用受控注入或 Mock。测试通过只证明本地逻辑，不证明 BaoStock/Choice/Tushare/AKShare 真实接口已连通。真实连通必须单独运行：

```powershell
python -m agent.market_data_probe `
  --provider baostock `
  --dataset daily_bar `
  --instrument 000333.SZ `
  --start-date 2026-07-01 `
  --end-date 2026-08-05
```

外部失败必须保留真实状态，不能把 `dependency_missing`、`network_blocked`、`not_configured` 或 `failed` 改写为通过。

新增功能至少包含正常、外部失败、字段缺失、边界值和对抗性绕过测试。测试输出使用临时目录，不能覆盖 `data/` 中的用户输入或历史报告。
