# 采集与日常编排层

`agent/` 负责把用户有权访问、在当时真实可见的内容转成可审计的本地记录，并将结果写入数据目录或 Obsidian。它不负责判断因子是否有效，也不具有交易权限。

## 主要模块

- `deepvan_visible_text.py`：解析复制或 OCR 的可见文本。
- `deepvan_capture.py`：建立带来源与采集时间的结构化 capture。
- `deepvan_daily_pipeline.py`：串联采集、评分和 Obsidian 同步。
- `obsidian_writer.py`、`obsidian_dashboard.py`：生成研究笔记与面板。
- `portfolio_snapshot.py`：维护本地持仓快照；持仓不因此自动属于量化策略。

## 运行样例

```powershell
python -m agent.deepvan_daily_pipeline `
  --visible-text data/inbox/deepvan_visible_text.sample.txt `
  --captured-at 2026-07-03T09:30:00+08:00
```

也可以通过 `--capture-json` 读取已经生成的 capture。两种输入互斥。

## 边界

- 不绕过登录、付费、权限、验证码或访问控制。
- 不把采集时间伪装成内容发布时间；字段不确定时保留不确定性。
- 不把作者观点直接当作行情真值、财务真值或可交易信号。
- 生成文件进入 `data/` 或 `obsidian-vault/`，业务规则留在代码和版本化配置中。
