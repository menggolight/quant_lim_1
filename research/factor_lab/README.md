# CSI 11 行业相对动量 Factor Lab V1

这是独立、只读、仅研究的因子实验引擎。它固定比较 `RM20`、`RM60`、`RM120` 三个候选，公式为
`log(C_t/C_t-L)-log(B_t/B_t-L)`；不会导入交易桥或生成订单。

标准入口：

```powershell
python -m research.factor_lab inventory
python -m research.factor_lab preregister --output-dir data/tmp/factor-lab/preregister
python -m research.factor_lab screen --index-receipt <choice-receipt.json> --calendar-receipt <sse-receipt.json> --evidence-root <probe-root> --output-dir <run-dir>
python -m research.factor_lab confirm --index-receipt <csi-receipt.json> --calendar-receipt <sse-receipt.json> --evidence-root <probe-root> --screen-index-receipt <choice-receipt.json> --screen-calendar-receipt <sse-receipt.json> --screen-evidence-root <probe-root> --screen-run <screen-run> --output-dir <run-dir>
python -m research.factor_lab weekly --index-receipt <csi-receipt.json> --calendar-receipt <sse-receipt.json> --evidence-root <probe-root> --confirmed-run <confirm-run> --output-dir <run-dir>
python -m research.factor_lab verify --run-dir <run-dir>
```

`screen` 只用 Choice 旧 11 行业系列；Choice 当前 11 系列只与 CSI 同代码序列对账。`confirm` 只评价
Screen 已冻结的赢家，不查看另外两个候选。所有运行目录恰有九个固定产物，且不可覆盖；`verify` 校验完整集合、
逐文件 SHA-256 与 `run_id`。

CLI 直接消费 `agent.factor_evidence_probe` 的内容寻址 receipt、normalized evidence 和 SSE 日历 receipt，并逐层核验
路径、内容哈希、固定请求、Provider 身份和 23/12 指数白名单。`engine.py` 模块头还定义了 Provider 无关的严格 JSON
bundle 形状，供测试或受控适配器使用；未知字段一律拒绝。

重要边界：Probe receipt、URL、来源字符串、布尔值和哈希只能证明受控采集与内容完整性，不能自证“官方”。当前
`official_transport_status=not_configured`，因此 Confirm 即使统计通过，也保持
`confirmed_not_admitted` 并记录 `source_authentication_not_configured`；Weekly 也只能是诊断产物。
