# 采集与日常编排层

`agent/` 负责把用户有权访问、在当时真实可见的内容转成可审计的本地记录，并将结果写入数据目录或 Obsidian。它不负责判断因子是否有效，也不具有交易权限。

## 主要模块

- `deepvan_visible_text.py`：解析复制或 OCR 的可见文本。
- `deepvan_capture.py`：建立带来源与采集时间的结构化 capture。
- `deepvan_daily_pipeline.py`：串联采集、评分和 Obsidian 同步。
- `obsidian_writer.py`、`obsidian_dashboard.py`：生成研究笔记与面板。
- `market_observation_dashboard.py`：将一份三层市场观察 JSON 渲染为本地只读单文件 HTML；不重新计算研究结论，也不产生交易动作。
- `market_observation_pipeline.py`：标准密封入口；校验版本化 Schema、时点和安全状态，绑定上一期 observation/manifest，计算确定性变化并生成受控产物。
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

## 边界

- 不绕过登录、付费、权限、验证码或访问控制。
- 不把采集时间伪装成内容发布时间；字段不确定时保留不确定性。
- 不把作者观点直接当作行情真值、财务真值或可交易信号。
- 生成文件进入 `data/` 或 `obsidian-vault/`，业务规则留在代码和版本化配置中。
