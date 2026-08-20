# 小资金交易执行层

本目录提供小资金低频策略的 Paper 执行内核，并保留华泰 MQuant 只读 Shadow 适配器。V1 ETF/质量成长兼容入口保持不变；自适应仓位V2通过新增显式意图入口演进。仓库永久不支持 LIVE，不会提交真实订单；本机尚未安装或获权使用官方客户端，因此也不能声称真实账户已经连通。

## 组成

- `models.py`：Decimal 领域模型、执行模式、显式 `PortfolioIntent`、订单风险方向和订单状态；
- `costs.py`：最低佣金、按比例佣金和卖出税费模型；
- `planner.py`：整手、费用、现金、仓位和换手感知的计划器；V2区分目标/可实现/实际仓位，把 `intent_id` 与逐日 `attempt_id` 分离，并绑定规范化执行报价包与内部受控日历；
- `risk.py`：Paper / Shadow fail-closed 门禁，并签发绑定计划与账户快照的短时批准；V2会从实际报价与日历 payload 重算哈希、验证相邻 session、报价状态、价差及费用后仓位。仅受控推导的 `RISK_REDUCING` / `FORCED_EXIT` 卖单豁免普通换手，T+1、停牌、跌停、行情时效、账户绑定和幂等仍生效；任何 LIVE 输入立即返回 `live_not_supported`；
- `strategy_bridge.py`：阻止行业雷达、合成或未准入信号直接触发订单；
- `order_store.py`：带显式迁移的SQLite订单状态机、Paper资金持仓账本、总/普通日内用量与跨重启幂等；
- `paper.py`：仅供内核验证的立即成交 Paper Broker；
- `paper_run.py`：生成明确标注为合成数据的 1 万元验证报告。
- `brokers/htsc_mquant_shadow.py`：严格校验供 MQuant 桥接使用的只读本地快照；无下单、撤单方法。文件校验不等于官方来源认证；
- `brokers/reconcile.py`：把完整券商账户与策略所有权账本隔离；在持久化成交来源链实现前固定拒绝转换，美的等长期持仓不会被调用方自报为策略资产；
- `huatai_shadow_probe.py`：客户端运行后的只读健康检查命令。

## 验证命令

```powershell
python -m unittest discover -s tests -v
python -m trading.paper_run
python -m trading.huatai_shadow_probe --config configs/htsc_mquant_shadow.example.json
```

`paper_run` 只检验资金、费用、整手、门禁和订单审计。输出不是收益回测，也不是 ETF 推荐。

自适应仓位V2的普通空目标继续拒绝；只有显式 `NO_ALPHA_CASH`、`RISK_OFF` 或 `ACCOUNT_DRAWDOWN_EXIT` 可以表达0%目标。同一 intent 的同一 attempt 重放不重复下单，下一受控日重试必须使用新 attempt。独立日频账本位于 `research/strategy_workspace/paper_ledger_v2.py`，它只对账，不授予执行权限；详见[自适应仓位V2规格](../docs/ADAPTIVE_EXPOSURE_V2.md)。

V2 当前把普通 `ALPHA_REBALANCE` 限制在同一 session；跨 session 只开放显式现金、减仓和退出意图，并要求与账本相同算法的日历 payload 哈希及严格相邻关系。该哈希只能证明传入 payload 内部一致，不能证明它是完整或官方交易所日历。执行报价包同样有规范化哈希，但尚无受控行情 registry 证明来源。Gate 仍使用订单携带的预计费用，最终 `PaperBroker` 会用其真实 `FeeSchedule` 重算并在成交前拒绝不一致费用；费率表尚未直接绑定到 Gate approval，因此正式 Paper 准入仍阻塞。

LIVE 的统一错误码为 `live_not_supported`，错误信息为 `LIVE execution is not supported by this repository`；不存在配置或令牌解锁路径。

`huatai_shadow_probe` 当前应返回 `blocked`，直到用户通过华泰正式渠道开通并安装 MATIC/MQuant、导入 `integrations/htsc_mquant/htsc_shadow_exporter.py` 并产生完整快照。读到一次快照只表示只读数据可见，不会改变 LIVE 永久阻断。
