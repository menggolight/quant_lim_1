# 小资金交易执行层

本目录提供 1 万元 ETF 低频策略的 Paper 执行内核，并保留华泰 MQuant 只读 Shadow 适配器。仓库永久不支持 LIVE，不会提交真实订单；本机尚未安装或获权使用官方客户端，因此也不能声称真实账户已经连通。

## 组成

- `models.py`：Decimal 领域模型、执行模式和订单状态；
- `costs.py`：最低佣金、按比例佣金和卖出税费模型；
- `planner.py`：整手、现金储备、仓位和换手感知的计划器；
- `risk.py`：Paper / Shadow fail-closed 门禁，并签发绑定计划与账户快照的短时批准；任何 LIVE 输入立即返回 `live_not_supported`；
- `strategy_bridge.py`：阻止行业雷达、合成或未准入信号直接触发订单；
- `order_store.py`：SQLite 订单状态机、Paper 资金持仓账本、日内用量与跨重启幂等；
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

LIVE 的统一错误码为 `live_not_supported`，错误信息为 `LIVE execution is not supported by this repository`；不存在配置或令牌解锁路径。

`huatai_shadow_probe` 当前应返回 `blocked`，直到用户通过华泰正式渠道开通并安装 MATIC/MQuant、导入 `integrations/htsc_mquant/htsc_shadow_exporter.py` 并产生完整快照。读到一次快照只表示只读数据可见，不会改变 LIVE 永久阻断。
