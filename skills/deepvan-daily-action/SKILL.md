---
name: deepvan-daily-action
description: Use when Codex needs to run or maintain the user's personal DeepVan daily investment-decision workflow: ingesting user-provided Knowledge Planet posts, screenshots, or summaries; converting them into structured signals; applying the user's QD/QDII plus quant-fund portfolio policy; performing first-principles and adversarial review; and producing a daily non-trading action memo. Do not use for automatic trading or bulk copying paid source text.
---

# DeepVan Daily Action

## Purpose

Run the user's personal daily investment-decision workflow. Treat DeepVan posts as high-priority research signals, not direct orders. Produce structured signals, a portfolio-aware action memo, and a short adversarial review.

## Hard Boundaries

- Do not place trades, submit orders, move money, or automate brokerage actions.
- Do not bypass login, CAPTCHA, paywalls, platform restrictions, or security prompts.
- Do not bulk-save paid source text. Save only time, title, link if available, keywords, non-verbatim summary, structured signal, and action rationale.
- If using Computer Use or browser control, only read visible content from the user's logged-in session. Stop if login, CAPTCHA, account permissions, or unexpected sensitive prompts appear.
- If source text is incomplete or screenshot OCR is uncertain, mark the signal as low confidence.

## Workflow

1. **Collect input**
   - Prefer pasted text or user-provided screenshots.
   - If the user asks for desktop automation, use the browser or Computer Use only to read visible pages and screenshots.
   - When structured captures are available, ingest them with `agent/deepvan_capture.py` before scoring.
   - Record the source mode: `pasted_text`, `screenshot`, `browser_visible`, `computer_use_visible`, or `manual_summary`.

2. **Extract signals**
   - Read `references/signal_schema.md`.
   - Convert each relevant post into one or more signal objects.
   - Separate source meaning from model interpretation.

3. **Apply portfolio policy**
   - Read `references/portfolio_policy.md`.
   - Resolve the latest immutable `data/portfolio/YYYY-MM-DD[.vN].json` whose `as_of` is not later than the decision time.
   - Use explicit signal-file weights only for isolated experiments; never let a later portfolio snapshot leak into an earlier decision.
   - Treat reconciliation failures as blocking data errors rather than silently repairing them.
   - Compare any A-share idea against the quant-fund alternative.
   - Never recommend adding a single stock that is already above the 15% research ceiling.

4. **Run adversarial review**
   - Read `references/adversarial_review.md`.
   - Identify over-interpretation, price-in risk, concentration risk, and exit conditions.

5. **Score and draft action**
   - When structured JSON signals are available, run `scripts/score_daily_action.py`.
   - When only text is available, manually produce the same output sections.
   - Output action recommendations only; do not imply certainty.

6. **Save daily memo**
   - Save final memos under `data/actions/YYYY-MM-DD.md` when the user asks to persist the result.
   - Keep source notes under `data/raw/` and structured signals under `data/signals/`.

## Daily Output Format

```markdown
# DeepVan Daily Action - YYYY-MM-DD

## 今日结论

- QD / 海外：
- A股量化：
- 防守仓：
- A股强因子：

## 星球信号

| 信号 | 资产 | 方向 | 强度 | 置信度 | 时效 |
|---|---|---|---:|---:|---|

## 动作建议

| 模块 | 当前暴露 | 建议 | 幅度 | 理由 |
|---|---:|---|---:|---|

## 对抗式审查

- 是否过度解读：
- 是否已被价格反映：
- 是否有基金替代：
- 如果错了怎么退出：

## 今日禁止动作

- （待填写）

## 明日反证

- （待填写）
```

## Script Usage

Recommended one-command daily run:

```powershell
python agent/deepvan_daily_pipeline.py `
  --visible-text data/inbox/deepvan_visible_text.sample.txt `
  --workspace . `
  --vault obsidian-vault `
  --captured-at 2026-07-07T09:30:00+08:00 `
  --source-mode browser_visible_text
```

The daily pipeline also refreshes `obsidian-vault/00-Dashboard/量化投研驾驶舱.md`. When a valid point-in-time portfolio snapshot exists, it injects the reconciled weights and risk flags into the action memo automatically.

Refresh only the Obsidian dashboard:

```powershell
python agent/obsidian_dashboard.py `
  --workspace . `
  --vault obsidian-vault
```

For debugging, ingest visible capture JSON first:

```powershell
python agent/deepvan_visible_text.py `
  --input data/inbox/deepvan_visible_text.sample.txt `
  --output data/inbox/deepvan_capture.from_text.json `
  --captured-at 2026-07-07T09:30:00+08:00 `
  --source-mode browser_visible_text

python agent/deepvan_capture.py `
  --input data/inbox/deepvan_capture.from_text.json `
  --workspace .
```

Use the scoring script only after signal extraction:

```powershell
python skills/deepvan-daily-action/scripts/score_daily_action.py `
  --input data/signals/2026-07-03.deepvan.json `
  --output data/actions/2026-07-03.md
```

If `python` is unavailable, use the bundled Codex Python runtime.
