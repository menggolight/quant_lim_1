# 测试目录

测试覆盖市场数据契约与 Provider、采集流水线、行业雷达、研报审计、市场观察、Paper 执行、LIVE 永久阻断和华泰只读 Shadow。

## 核心命令

市场数据专项：

```powershell
python -m unittest discover -s tests -p "test_market_data*.py" -v
python -m unittest tests.test_factor_market_data tests.test_factor_evidence_probe tests.test_factor_lab -v
python -m unittest tests.test_choice_quality_growth_batch tests.test_market_data_choice -v
python -m unittest tests.test_choice_expired_access tests.test_tushare_capability_contract tests.test_tushare_capability_probe -v
python -m unittest tests.test_tushare_single_endpoint_diagnostic tests.test_tushare_single_endpoint_diagnostic_postmortem -v
python -m unittest tests.test_tushare_http_terminal_diagnostic -v
```

研报审计专项：

```powershell
python -m unittest discover -s tests -p "test_broker_report_audit*.py" -v
```

质量成长策略工作区专项：

```powershell
python -m unittest discover -s tests -p "test_strategy_workspace*.py" -v
```

自适应仓位 V2 P0 专项：

```powershell
python -m unittest tests.test_strategy_adaptive_exposure_policy tests.test_adaptive_exposure_p0 tests.test_strategy_workspace_paper_ledger_v2 -v
```

该组验证政策哈希、普通空意图拒绝、显式现金意图、风险退出普通换手豁免、物理受阻不连坐、intent/attempt跨重启幂等、旧SQLite迁移、账户CAS、规范化报价包、内部日历相邻性、费用后仓位上限、D日收盘触发后下一受控session退出、逐日重试、永久no-reentry latch，以及日频现金/持仓/NAV/回撤/三类仓位重放。它不包含Alpha/仓位模型、正式PIT数据、官方日历/行情registry或Locked Test运行；Gate approval也尚未直接绑定独立费率表，因此这些测试不能证明策略有效或Paper准入。

该组覆盖精确六因子与首披时点、动态中证800成分、Choice完整覆盖门、Experiment v2防改写、train-only Ridge、D+1→D+21 open-to-open标签、20交易日 cadence/purge、100万元Top Decile研究账本、成功子样本拒绝、A股整手/停复牌/涨跌停/T+1、账户行业集中度、基础/压力成本、回撤/换手和 append-only Paper 账本重放。Choice 正式适配器、真实数据与正式回测尚未运行；Paper 每决策点 signal/model/source 哈希仍由调用者提供且缺日频 NAV/回撤盯市，因此这些测试不证明历史→12个月的两阶段准入已跑通，Stage B 依然 `blocked_missing_controlled_paper_signal_adapter` 与 `blocked_missing_daily_paper_risk_marks`。

数据门的负向用例还要求 Choice receipt 枚举完整成分 `subject_ids`、中证800全收益 open/close 和 `single_quarter`/`consolidated`/`CNY` 财务口径；Experiment 覆盖 Andrews HAC、Holm、三段Rank IC及金融/非金融子模型的冻结检查。降级路径还验证两列当前成分与16列行业快照的独立导入/重放、800只逐项绑定、无有效日期行业不得升级为PIT、V2 `sample.json + manifest.json` 从双源artifact逐字节重建、恰好60只与exact 11行业、行业等覆盖不得冒充指数代表性，以及调用者自备universe JSON和占位receipt哈希不能绕过；产物始终保持 Paper/trade/real-money为 `false`、LIVE为 `not_supported`。单元测试通过不等于真实数据、统计效力、Paper准入或盈利。

当前Experiment V3仍未正式冻结。涉及市场数据基础设施的安全全仓回归必须精确排除四个Locked/Experiment模块，不能先用`discover`加载后再跳过：

```powershell
$excluded = @(
  "test_strategy_workspace_admission",
  "test_strategy_workspace_evaluation",
  "test_strategy_workspace_experiment",
  "test_strategy_workspace_top_decile_backtest"
)
$modules = Get-ChildItem tests/test_*.py |
  Where-Object { $_.BaseName -notin $excluded } |
  ForEach-Object { "tests.$($_.BaseName)" }
python -m unittest @modules -v
python -m compileall -q agent research trading operations integrations tests
```

上述四个模块保持`not run`；不能读取或解释Locked Test结果。正式Experiment冻结后的唯一Locked运行须走独立受控流程。

## 市场数据覆盖重点

- Provider 注册、未知或禁用 Provider；
- BaoStock 代码双向映射、日线、交易日历、证券基础信息和 SDK 懒加载；
- Choice访问到期时在SDK导入/start前确定性返回`provider_access_expired`，诊断session、live capture、historical backfill和新的正式offline消费均被拒绝；旧文件保留且不触发自动fallback；
- Choice SW2021/sector/EDB 候选的固定 SDK 签名、`None`/空值失败关闭、双哈希和严格离线重放；
- Choice `10001029` 配额耗尽的独立分类、三次断路及非随机截断后排名/推荐强制关闭；
- 登录/查询失败、空结果、重复日期、错证券、非法 OHLC、负值、缺字段和非法数字；
- raw、quarantine、validated 分层及研究消费者隔离；
- 整批 fallback，不拼接 Primary 与 Secondary；
- Tushare capability plan不读Token/不导入SDK/不联网；live缺Token、SDK缺失、权限、限频、网络、字段漂移、重复主键、非有限数、create-only和重放均结构化失败且Token不落盘；
- Tushare single-endpoint诊断固定SDK/HTTP同语义参数、每通道一次、全轮预算4、无retry/redirect、五项安全错误结构和四类结论；明显异常的本地凭证输入在预算/SDK/网络前拒绝；标准live入口全程持有跨进程round lock且output/budget固定同根，`daily`必须先重放已完成的`trade_cal` receipt；runner failure marker关闭整轮，sealed postmortem V3完整绑定marker与slot，并把实际请求数、runtime参数和两通道结果保持为未知，不伪造能力证据；
- 新授权的Tushare HTTP终态诊断固定`trade_cal/http/max_requests=1`，覆盖reserve前、reserve后network前、network进入后、response后receipt前、receipt写中断和terminal写中断；所有残留前缀离线replay且不得触发第二次网络请求，六类计数逐项复核；
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
