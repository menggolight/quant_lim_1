# Schema 目录

`schemas/` 定义跨进程、跨运行环境或跨模块交换的结构化数据契约。Schema 是兼容性边界，不是来源认证机制。

当前契约：

- `htsc_mquant_shadow.v1.json`：华泰 MQuant 只读 Shadow 快照结构。
- `market_observation.v0.1.json`：宏观—行业—个股三层市场观察结构；固定为诊断状态，`overall.trade_action` 必须为 `null`；可选 `comparison` 记录与前一期的结构化变化，可选 `pipeline` 记录标准 CLI 的实际密封时点及输入、Schema 哈希。

修改规则：

- 新增可选字段可以保持兼容；删除、改名、改变类型或语义必须升级主版本。
- 生产者和消费者都要校验 Schema、能力标志、完整性、时效和业务约束。
- Schema 校验通过只证明形状符合要求，不证明数据真实、完整或来自官方系统。
- 新版本必须提供迁移、双读窗口或明确拒绝旧版，并补充负向和旧版本测试。
