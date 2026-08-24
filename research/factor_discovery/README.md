# Factor Discovery Governance

本目录只治理“候选因子如何成为可被研究代码引用的冻结因子”，不负责生成 Alpha、回测、订单或准入结论。

## 四层边界

1. `FactorHypothesisV2`：状态永久为 `llm_research_candidate_only`。它冻结公式、输入 Schema、预测对象、期限、方向、基准、信息截止时点和反证条件，但不能自报验证通过。
2. `FactorValidationReceiptV1`：绑定候选哈希、公式哈希、实现代码哈希、输入 Schema 哈希、验证规格、验证数据和验证代码。固定只允许 `validation_only_not_locked_test`，不读取 Locked Test。
3. `ApprovedFactorV1`：只能绑定 typed validation receipt；候选对象不能直接写入批准条目。
4. `ApprovedFactorRegistryV1`：按 `factor_id` 规范排序并计算 `registry_sha256`，拒绝重复 `factor_id`、重复公式、重复 receipt、未来时点和字段间矛盾。

```python
from research.factor_discovery.governance import (
    ApprovedFactorRegistryV1,
    ApprovedFactorV1,
    FactorHypothesisV2,
    FactorValidationReceiptV1,
)
```

Registry 的稳定消费接口为：

- `registry.registry_sha256`
- `registry.approved_factor_ids`
- `registry.get(factor_id)`
- `registry.to_dict()`
- `registry.require_valid(as_of=...)`
- `ApprovedFactorRegistryV1.from_dict(payload, as_of=...)`

`as_of` 必须是带时区的显式调用时点，不读取机器当前时间，保证重放确定性。`from_dict` 会重算候选、receipt、批准条目和 registry 的全部自哈希。

## 不提供的能力

- 不接受 `source_authenticated=True`、`validation_passed=True` 等调用方布尔认证；
- 不把 Schema 或 SHA-256 当作 receipt 来源认证；生产环境仍须由受控写入路径和文件权限保护；
- 不授予 Paper、交易、真实资金清单或 LIVE 能力，相关状态固定关闭；
- 不运行或解释 2024—2025 Locked Test，也不代表 Experiment V3 已冻结。
