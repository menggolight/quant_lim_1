# 中证行业因子挖掘器 V1

`research/factor_lab/` 是一个只读、低自由度的因子实验室。它固定研究中证一级 11 行业的 `RM20`、`RM60`、`RM120` 相对动量，并稳定给出通过、未通过或数据不足；它不承诺找到 Alpha，也不连接 Paper、Shadow 或交易桥。

## 冻结研究问题

对行业 `i`、交易日 `t` 和窗口 `L`：

```text
RM_L(i,t) = log(C_i,t / C_i,t-L) - log(B_t / B_t-L)
L in {20, 60, 120}
```

每个交易周最后一个正式交易日形成横截面排名。标签是下一交易日收盘至未来第 20 个交易日收盘的行业相对基准收益，只评价方向预测，不代表可成交收益。

两阶段使用不同指数系列，点位和收益不得拼接：

| 阶段 | 行业系列 | 作用 |
|---|---|---|
| Choice Screen | `000986.CSI`–`000991.CSI`、`932075.CSI`、`932076.CSI`、`000993.CSI`–`000995.CSI` | 2023-03-10 以前的长历史筛选 |
| Official Confirm | `932077.CSI`–`932083.CSI`、`931775.CSI`、`932084.CSI`–`932086.CSI` | 当前系列的独立确认 |
| Benchmark | `000985.CSI` | 两阶段共同基准 |

行业只按能源、材料、工业、可选消费、主要消费、医药卫生、金融、信息技术、通信服务、公用事业、房地产做语义对应。代码表、系列版本和有效时间属于 `csi_industry_universe.v1` 证据，不能靠调用者传入的名称或哈希自证。

## 数据边界

- Choice Provider 只允许 `start`、`stop`、`csd`、`tradedates`，指数强制不复权；首次出现 `10001029` 即停止并保存 checkpoint。
- 中证 Provider 只访问固定白名单的 `index-basic-info` 和 `index-perf` 官方 HTTPS 端点；保存最终 URL、响应头哈希、原始响应和规范化哈希。
- 上交所 Provider 只读取年度休市安排。计划周、起止边界和额外日期必须由受控交易日历核验。
- Choice 是许可只读 Secondary，永远不能升级为官方数据，也不能补中证缺口。历史抓取统一标记 `historical_backfill_not_original_capture`。
- 本地文件、URL 字符串、调用者布尔值或 SHA-256 只能证明内容一致性，不能证明官方来源。来源认证未完成时，即使统计门通过，研究准入仍为 `blocked`。

同代码 Choice 与中证数据重叠时，交易日一致率须不低于 99.5%，绝对日收益差中位数不超过 1bp、P99 不超过 5bp；超限状态为 `blocked_source_disagreement`。

## 命令

先捕获或严格重放证据：

```powershell
python -m agent.factor_evidence_probe --source choice --mode online --start-date 2017-01-01 --end-date 2026-08-12 --output-root data/factor_evidence
python -m agent.factor_evidence_probe --source csi --mode online --start-date 2023-03-13 --end-date 2026-08-12 --output-root data/factor_evidence
python -m agent.factor_evidence_probe --source sse --mode online --start-date 2023-03-13 --end-date 2026-08-12 --output-root data/factor_evidence

python -m agent.factor_evidence_probe --source choice --mode offline --start-date 2017-01-01 --end-date 2026-08-12 --evidence-cutoff-at 2026-08-13T09:00:00+08:00 --output-root data/factor_evidence
python -m agent.factor_evidence_probe --source csi --mode offline --start-date 2023-03-13 --end-date 2026-08-12 --evidence-cutoff-at 2026-08-13T09:00:00+08:00 --output-root data/factor_evidence
python -m agent.factor_evidence_probe --source sse --mode offline --start-date 2023-03-13 --end-date 2026-08-12 --evidence-cutoff-at 2026-08-13T09:00:00+08:00 --output-root data/factor_evidence
```

再运行实验室：

```powershell
python -m research.factor_lab inventory
python -m research.factor_lab preregister
python -m research.factor_lab screen
python -m research.factor_lab confirm
python -m research.factor_lab weekly
python -m research.factor_lab verify
```

`confirm` 没有候选、窗口、基准、门槛或 holdout 覆盖参数，只读取 Screen 冻结的唯一赢家和不可覆盖的假设卡。主观卡片通过追加版本表达方向、理由、期限和反证条件；周报把主观观点与客观排名并列展示，不合成分数。

## 固定产物

每次研究运行只生成以下九项：

1. `hypothesis_card.json`
2. `universe_manifest.json`
3. `source_reconciliation.csv`
4. `factor_observations.csv`
5. `weekly_metrics.csv`
6. `window_metrics.csv`
7. `exceptions.csv`
8. `factor_report.md`
9. `run_manifest.json`

`run_manifest.json` 绑定输入和产物哈希、版本、HEAD 与工作树差异哈希，并固定：

```text
paper_eligibility=false
trade_eligibility=false
live_execution_status=live_not_supported
```

未准入报告必须显示“诊断观察”，不得出现买入、卖出或推荐。报告不输出重叠 20 日标签的 Sharpe 或年化收益，也不以交易成本作为 V1 确认门槛。

## 评价门禁

Choice Screen 固定使用完整 20 日标签也已在 2023-03-10 前成熟的最后 260 个计划周、5 个连续 52 周窗口和 120 交易日 warm-up。三个候选共用 complete-case 周；每窗至少 44 周。统计检验为 5 周 moving-block bootstrap 10,000 次、假设卡哈希派生随机种子和三候选 Holm FWER 5% 修正。无候选通过即 `choice_screen_failed`，不现场改公式。

Official Confirm 只评价冻结赢家，使用当前系列最后 104 个标签已成熟计划周并固定切成两个 52 周块，不因缺失向前扩展。两块各至少 44 周且总计至少 88 周；还要通过 leave-one-industry-out、单行业贡献上限和横截面随机置换检查。

完整官方证据链未来通过准入后，回溯确认的最高状态才可为 `research_admitted_retrospective_close_only`。当前上交所日历 source-owned 解析仍未配置，V1 即使统计通过也最高为 `confirmed_not_admitted`。保持卡片不变，再积累至少 52 个新周且至少 44 个标签成熟后，才可在后续阶段评价 `research_admitted_forward_confirmed`。任一数据、来源、覆盖或统计门失败都必须保留其真实状态。

## 后续顺序

1. 积累当前行业系列和主观卡片的前瞻样本。
2. 有 PIT 成分与公司行为契约后，再验证行业广度增量。
3. 至少 52 周主观卡片成熟后，再检验主观观点的增量。
4. 最后单独研究 ETF 映射、成本和 Paper，并重新准入。

指数方法以[当前中证全指行业方法](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/H30199_Index_Methodology_cn.pdf)、[旧行业优选方法](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/20240510102751-000986_cn.pdf)和[中证全指方法](https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/indices/detail/files/zh_CN/20231208175438-000985_Index_Methodology_cn.pdf)为准；Choice 调用遵循 [EmQuantAPI 文档](https://quantapi.eastmoney.com/Upload/EMQuantAPI_Python.html?_=638911758629244253)，交易日证据来自[上交所年度休市安排](https://www.sse.com.cn/disclosure/dealinstruc/closed/)。
