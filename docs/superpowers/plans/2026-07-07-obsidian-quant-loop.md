# Obsidian Quant Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Obsidian-backed decision log and review loop to the local Quant Agent project.

**Architecture:** Keep Codex as the execution layer under `agent/`, keep structured data under `data/`, and add an Obsidian vault under `obsidian-vault/` for human review. A Python writer converts generated action memos into Obsidian daily notes with YAML properties and stable sections.

**Tech Stack:** Markdown, YAML-compatible frontmatter, Python standard library, Obsidian Properties, optional Dataview queries.

## Global Constraints

- Do not automate trades, brokerage actions, money movement, or account changes.
- Do not bypass Knowledge Planet permissions, paywalls, login prompts, CAPTCHA, or security prompts.
- Do not bulk-copy paid source text into Obsidian.
- Store only personal research summaries, structured signals, action suggestions, execution decisions, and review outcomes.
- Use standard-library Python only for MVP scripts.

---

## File Structure

- Create: `agent/obsidian_writer.py` for converting action memo Markdown into Obsidian daily notes.
- Create: `tests/test_obsidian_writer.py` for writer behavior.
- Create: `obsidian-vault/00-Dashboard/量化投研驾驶舱.md` for the review dashboard.
- Create: `obsidian-vault/01-Daily/.gitkeep` to preserve daily-note directory.
- Create: `obsidian-vault/02-Signals/.gitkeep` to preserve signal-note directory.
- Create: `obsidian-vault/03-Portfolio/当前持仓.md` for portfolio baseline.
- Create: `obsidian-vault/03-Portfolio/仓位纪律.md` for portfolio discipline.
- Create: `obsidian-vault/04-Rules/DeepVan经验规则.md` for source interpretation rules.
- Create: `obsidian-vault/04-Rules/第一性原理.md` for decision first principles.
- Create: `obsidian-vault/04-Rules/对抗式审查.md` for checklist reuse.
- Create: `obsidian-vault/05-Reviews/周复盘模板.md` for weekly review.

### Task 1: Obsidian writer

**Files:**
- Create: `tests/test_obsidian_writer.py`
- Create: `agent/obsidian_writer.py`

**Interfaces:**
- Consumes: `write_daily_note(action_path: Path, vault_root: Path) -> Path`
- Produces: an Obsidian daily note at `vault_root / "01-Daily" / "<date>.md"`

- [x] **Step 1: Write failing tests**

Tests assert that `write_daily_note` creates a daily note from an action memo, includes YAML properties, preserves memo content, and avoids overwriting user execution fields.

- [x] **Step 2: Run tests and verify RED**

Expected failure: `ModuleNotFoundError: No module named 'agent.obsidian_writer'`.

- [x] **Step 3: Implement minimal writer**

Implement date extraction, YAML frontmatter generation, and non-destructive note updates.

- [x] **Step 4: Run tests and verify GREEN**

Expected: all writer tests pass.

### Task 2: Obsidian vault MVP

**Files:**
- Create dashboard, portfolio, rules, and review template Markdown files under `obsidian-vault/`.

**Interfaces:**
- Consumes: daily note properties created by `agent/obsidian_writer.py`
- Produces: human-readable dashboard and reusable review templates.

- [x] **Step 1: Add vault directories and dashboard**

Dashboard includes Dataview blocks but remains readable without the plugin.

- [x] **Step 2: Add portfolio and rule notes**

Notes mirror the current policy and adversarial review already used by the agent.

- [x] **Step 3: Add weekly review template**

Template captures signal, action, execution, result, and rule update.

### Task 3: Verify integration

**Files:**
- Existing: `data/actions/2026-07-03.sample.md`
- Generated: `obsidian-vault/01-Daily/2026-07-03.md`

**Interfaces:**
- Consumes: existing sample action memo.
- Produces: an Obsidian daily note.

- [x] **Step 1: Run writer on sample memo**

Expected: `obsidian-vault/01-Daily/2026-07-03.md` is created.

- [x] **Step 2: Inspect generated note**

Expected: note contains frontmatter, original action memo, and execution/review sections.

- [x] **Step 3: Run tests**

Expected: all tests pass with standard-library Python.
