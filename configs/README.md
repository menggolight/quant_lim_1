# 配置目录

`configs/` 保存可以审查和版本控制的研究政策。配置描述“允许什么”和“当前处于什么状态”，不能用来伪造外部授权或绕过代码门禁。

## 当前配置

- `broker_report_audit.v1.json`：研报审计、技能估计、因子研究、数据源和固定产物契约。当前为 `research_only_not_trade_eligible`。
- `industry_radar.r0.json`：行业雷达特征、权重和阈值。当前为 `heuristic_baseline_not_alpha`。
- `small_account_trading.v1.json`：1 万元 ETF Paper 执行与阶段门禁。当前为 `paper_only`。
- `htsc_mquant_shadow.example.json`：华泰只读快照探针示例。当前为 `blocked_pending_client_authorization`。

## 修改规则

- 影响历史解释、输入字段、产物或安全边界的变更必须升级配置或 Schema 版本。
- 路径可以指向本地文件，但不得写入密码、账户、Token、Cookie 或绑定秘密。
- `research_only=false`、`orders_enabled=true` 等单一布尔值不能解锁实盘；权限来自独立代码门禁、官方授权和可验证证据。
- 修改配置后运行对应专项测试及完整测试，并确认 README 中的状态没有漂移。
