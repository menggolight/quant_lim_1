# DeepVan 及时收取 MVP

日期：2026-07-07

## 目标

把 DeepVan 知识星球中用户账号可见的新信息，稳定转成个人投研流水：

`可见内容 -> 去重状态 -> 原始记录 -> 结构化信号 -> 动作建议 -> Obsidian`

第一阶段不直接控制真实页面，不绕过登录、验证码、权限或付费限制。先固定中间数据契约，让浏览器、Computer Use、手工粘贴后续都能接入同一个入口。

## 当前实现

- 可见文本适配器：`agent/deepvan_visible_text.py`
- 采集入口：`agent/deepvan_capture.py`
- 一键流水线：`agent/deepvan_daily_pipeline.py`
- 轻量驾驶舱：`agent/obsidian_dashboard.py`
- 可见文本样例：`data/inbox/deepvan_visible_text.sample.txt`
- 输入样例：`data/inbox/deepvan_capture.sample.json`
- 去重状态：`data/state/deepvan_capture_state.json`
- 原始记录：`data/raw/deepvan/YYYY-MM-DD/*.md`
- 信号文件：`data/signals/YYYY-MM-DD.deepvan.json`

## 输入契约

```json
{
  "captured_at": "2026-07-07T09:10:00+08:00",
  "source_mode": "browser_visible",
  "items": [
    {
      "source_id": "topic-id-if-available",
      "published_at": "2026-07-07T08:58:00+08:00",
      "title": "主题标题",
      "summary": "非逐字摘要",
      "asset": "ai_semiconductor",
      "direction": "watch",
      "strength": 3,
      "confidence": 2,
      "horizon": "days",
      "evidence": "可见证据摘要",
      "counter_evidence": "反证条件"
    }
  ]
}
```

`source_id` 优先用于去重；没有 `source_id` 时使用 `url`；二者都没有时使用发布时间、标题、摘要的哈希。

## 每日使用

推荐使用一键命令：

```powershell
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe agent\deepvan_daily_pipeline.py `
  --visible-text data\inbox\deepvan_visible_text.sample.txt `
  --workspace . `
  --vault obsidian-vault `
  --captured-at 2026-07-07T09:30:00+08:00 `
  --source-mode browser_visible_text
```

一键命令会自动刷新 `obsidian-vault/00-Dashboard/量化投研驾驶舱.md`。

如果只想刷新驾驶舱：

```powershell
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe agent\obsidian_dashboard.py `
  --workspace . `
  --vault obsidian-vault
```

如果已经有标准 capture JSON：

```powershell
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe agent\deepvan_daily_pipeline.py `
  --capture-json data\inbox\deepvan_capture.sample.json `
  --workspace . `
  --vault obsidian-vault
```

排查问题时可以分步运行。

如果拿到的是浏览器、微信桌面版或 OCR 的可见文本，先转换成 capture JSON：

```powershell
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe agent\deepvan_visible_text.py `
  --input data\inbox\deepvan_visible_text.sample.txt `
  --output data\inbox\deepvan_capture.from_text.json `
  --captured-at 2026-07-07T09:30:00+08:00 `
  --source-mode browser_visible_text
```

再入库去重：

```powershell
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe agent\deepvan_capture.py `
  --input data\inbox\deepvan_capture.from_text.json `
  --workspace .
```

然后把生成的信号送入现有评分脚本：

```powershell
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe skills\deepvan-daily-action\scripts\score_daily_action.py `
  --input data\signals\2026-07-07.deepvan.json `
  --output data\actions\2026-07-07.md
```

最后同步到 Obsidian：

```powershell
C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe agent\obsidian_writer.py `
  --action data\actions\2026-07-07.md `
  --vault obsidian-vault
```

一键流水线内部执行的就是以上三步。

## 后续接入顺序

1. 浏览器可见页采集：优先，因为可读性和稳定性最好。
2. 可见文本适配器：浏览器复制、微信桌面版复制、OCR 文本都走同一入口。
3. Computer Use 采集：只在网页不可用、必须读取微信桌面版或手机投屏时使用。
4. 手工粘贴或截图：作为失败兜底。

## 定时建议

- 09:10：A股开盘前。
- 12:40：午盘后。
- 15:20：A股收盘后。
- 21:30：海外资产和夜间观点检查。

## 对抗式审查

- 及时性不等于可交易性。每条新增内容必须先通过去重、置信度、反证条件。
- 星球观点是研究信号，不是交易指令。
- 不保存和传播大段付费原文，只保留个人投研所需的摘要、标签、动作理由和复盘结果。
- 可视化只读取本地结果，不参与采集、评分和交易动作。
