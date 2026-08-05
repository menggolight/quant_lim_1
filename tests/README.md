# 测试目录

测试覆盖采集、行业雷达、研报审计、Paper 执行、风险边界和华泰只读 Shadow。

## 完整回归

```powershell
python -m unittest discover -s tests -v
```

## 研报审计专项

```powershell
python -m unittest discover -s tests -p "test_broker_report_audit*.py" -v
```

## 三层观察专项

```powershell
python -m unittest tests.test_market_observation_dashboard tests.test_market_observation_pipeline -v
```

研报测试按职责覆盖模型、存储与管道、真值与时点、版本隔离、PDF provenance、验证门禁和对抗式绕过。交易测试覆盖费用、整手、风险批准、订单状态、策略资产隔离和只读执行边界。

新增功能至少包含：

- 一个正常路径；
- 一个字段缺失或外部失败路径；
- 一个边界值；
- 涉及时点时的未来数据拒绝；
- 涉及 Schema/缓存时的旧版本或哈希不符拒绝；
- 涉及权限时的绕过尝试。

测试输出应写入临时目录，不能覆盖 `data/` 中的用户输入或历史报告。
