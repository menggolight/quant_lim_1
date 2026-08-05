# DeepVan Daily Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the first local version of a personal DeepVan daily action agent.

**Architecture:** A workspace-local skill stores the decision workflow and rules. A deterministic Python script scores structured daily signals and writes a markdown action memo. Raw source collection stays manual or visible-session based until platform access is reliable.

**Tech Stack:** Markdown skill files, JSON signal files, Python standard library.

---

## File Structure

- Create: `skills/deepvan-daily-action/SKILL.md` for the reusable daily workflow.
- Create: `skills/deepvan-daily-action/references/signal_schema.md` for signal extraction rules.
- Create: `skills/deepvan-daily-action/references/portfolio_policy.md` for current holding constraints and target ranges.
- Create: `skills/deepvan-daily-action/references/adversarial_review.md` for mandatory review questions.
- Create: `skills/deepvan-daily-action/scripts/score_daily_action.py` for scoring structured signals.
- Create: `skills/deepvan-daily-action/agents/openai.yaml` for local skill UI metadata.
- Create: `data/signals/2026-07-03.sample.json` for a representative sample input.
- Create: `docs/superpowers/specs/2026-07-03-deepvan-daily-agent-design.md` for the design record.

### Task 1: Create the skill skeleton

- [x] **Step 1: Initialize skill folder**

Run the skill creator init script with resources `scripts,references`.

- [x] **Step 2: Replace template SKILL.md**

Write a concise workflow with safety boundaries, daily output format, and script usage.

- [x] **Step 3: Add UI metadata**

Create `agents/openai.yaml` with display name, short description, and default prompt.

### Task 2: Add decision references

- [x] **Step 1: Add portfolio policy**

Document current weights, target ranges, and replacement tests.

- [x] **Step 2: Add signal schema**

Define JSON structure, enum values, and extraction rules.

- [x] **Step 3: Add adversarial review**

Define first-principles checks, source reliability checks, price-in checks, portfolio-fit checks, and exit logic.

### Task 3: Add deterministic scoring script

- [x] **Step 1: Implement input loading**

Read JSON from `--input` with UTF-8.

- [x] **Step 2: Normalize portfolio weights**

Use the current baseline weights when JSON omits `current_weights`.

- [x] **Step 3: Score bucket signals**

Map source assets into QD, quant, defense, and A-share alpha buckets.

- [x] **Step 4: Render markdown memo**

Write conclusion, score table, signal table, adversarial review, forbidden actions, and next-day counter-evidence.

### Task 4: Verify first version

- [x] **Step 1: Validate skill metadata**

Attempted official validation:

```powershell
& 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  'C:\Users\Admin\.codex\skills\.system\skill-creator\scripts\quick_validate.py' `
  'C:\Users\Admin\Documents\quant\skills\deepvan-daily-action'
```

Result: blocked because the bundled Python environment lacks the `yaml` package required by `quick_validate.py`.

Fallback validation:

```powershell
& 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  -c "from pathlib import Path; p=Path(r'C:\Users\Admin\Documents\quant\skills\deepvan-daily-action\SKILL.md'); s=p.read_text(encoding='utf-8'); assert s.startswith('---\nname: deepvan-daily-action\ndescription: '); assert '\n---\n' in s[4:]; print('manual skill frontmatter ok')"
```

Result: manual skill frontmatter validation passed.

- [x] **Step 2: Run sample scoring**

Run:

```powershell
& 'C:\Users\Admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' `
  'C:\Users\Admin\Documents\quant\skills\deepvan-daily-action\scripts\score_daily_action.py' `
  --input 'C:\Users\Admin\Documents\quant\data\signals\2026-07-03.sample.json' `
  --output 'C:\Users\Admin\Documents\quant\data\actions\2026-07-03.sample.md'
```

Result: output markdown file was written.

- [x] **Step 3: Inspect generated memo**

Open the generated memo and verify it contains the standard sections: 今日结论, 分数, 星球信号, 对抗式审查, 今日禁止动作, 明日反证.

Result: all standard sections were found.

### Task 5: Next extension after user review

- [ ] **Step 1: Run one real daily session from pasted text or screenshots**

Convert today's visible source material into `data/signals/YYYY-MM-DD.json`.

- [ ] **Step 2: Compare action memo with user judgment**

Record where the rules were too aggressive, too conservative, or unclear.

- [ ] **Step 3: Only then add capture automation**

Add a Computer Use or browser-visible capture guide after one successful manual run.
