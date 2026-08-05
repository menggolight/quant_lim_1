# 采集与日常编排层

`agent/` 负责把用户有权访问、在当时真实可见的内容转成可审计的本地记录，并将结果写入数据目录或 Obsidian。它不负责判断因子是否有效，也不具有交易权限。

## 主要模块

- `deepvan_visible_text.py`：解析复制或 OCR 的可见文本。
- `deepvan_capture.py`：建立带来源与采集时间的结构化 capture。
- `deepvan_daily_pipeline.py`：串联采集、评分和 Obsidian 同步。
- `obsidian_writer.py`、`obsidian_dashboard.py`：生成研究笔记与面板。
- `market_observation_dashboard.py`：将一份三层市场观察 JSON 渲染为本地只读单文件 HTML；不重新计算研究结论，也不产生交易动作。
- `market_observation_pipeline.py`：标准密封入口；校验版本化 Schema、时点和安全状态，绑定上一期 observation/manifest，计算确定性变化并生成受控产物。
- `eastmoney_source_probe.py`：只读检查东方财富历史行情与完整行业榜是否当前可访问；输出独立诊断 JSON，不改写已密封观察。
- `portfolio_snapshot.py`：维护本地持仓快照；持仓不因此自动属于量化策略。

## 运行样例

```powershell
python -m agent.deepvan_daily_pipeline `
  --visible-text data/inbox/deepvan_visible_text.sample.txt `
  --captured-at 2026-07-03T09:30:00+08:00
```

也可以通过 `--capture-json` 读取已经生成的 capture。两种输入互斥。

首次密封三层观察：

```powershell
python -m agent.market_observation_pipeline `
  --input data/inbox/market_observation/2026-08-05-close.draft.json `
  --first-baseline
```

后续运行把 `--first-baseline` 替换为成对的 `--previous` 与 `--previous-manifest`。流水线拒绝自动选择 `latest`、拒绝覆盖内容不同的历史产物；标准 CLI 的实际 `sealed_at` 会与 manifest 绑定，上一期必须在当前决策时点前已经密封。`latest.alias.json` 记录当前稳定入口的 observation、manifest 和快照哈希；只有严格更新且直接承接当前 alias 的观察才能替换 `latest.html`。HTML 完全自包含，不加载 CDN、远程字体或追踪脚本；缺失值保持为“—”。

`market_observation_dashboard.py` 是低层重渲染器，必须同时传入标准 manifest；日常生成应使用 `market_observation_pipeline.py`。标准生成只能证明文件完整性与契约通过，不能把人工或公开整理数据升级为已认证官方来源。

东方财富公开接口连接诊断：

```powershell
python -m agent.eastmoney_source_probe `
  --stock 000333.SZ `
  --start-date 2026-07-01 `
  --end-date 2026-08-05 `
  --expected-last-date 2026-08-05
```

该命令对已知接口域名的直接连接使用 IPv4，仍保留原域名、TLS SNI 与证书校验；若系统配置 HTTP 代理，则由代理决定目标地址族。行业榜只有在 `data.total` 稳定、板块代码无重复、页数完整且全部为本次在线响应时才通过；行情仅检查非空、原始/复权覆盖一致，以及调用者明确给出的最后交易日，不自称核完窗口内每个交易日。默认结果写入 `.tmp/eastmoney_source_probe.json`，不等于官方真值准入，也不会把旧观察里的历史失败状态改成成功。

## 边界

- 不绕过登录、付费、权限、验证码或访问控制。
- 不把采集时间伪装成内容发布时间；字段不确定时保留不确定性。
- 不把作者观点直接当作行情真值、财务真值或可交易信号。
- 生成文件进入 `data/` 或 `obsidian-vault/`，业务规则留在代码和版本化配置中。
