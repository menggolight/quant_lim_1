# DeepVan Daily Agent Design

## Goal

Build a personal daily investment-decision agent that turns user-visible DeepVan Knowledge Planet content and accumulated rules into a structured, portfolio-aware action memo.

## Non-Goals

- Do not automate trades or brokerage actions.
- Do not bypass platform permissions, CAPTCHA, login, paywalls, or security prompts.
- Do not bulk-copy paid original text.
- Do not treat DeepVan posts as commands to follow blindly.

## Current Constraints

- The `C:\Users\Admin\Documents\quant` workspace currently contains only DeepVan summary/export notes and no existing codebase.
- Existing zsxq CLI access cannot read this star's topic list because the group has not enabled Skill permissions.
- The user currently holds mostly QD/QDII funds plus one A-share quant fund and a small utilities/defense fund.
- The first version must support semi-automatic use: pasted text, screenshots, visible browser pages, or Computer Use snapshots.

## Architecture

The agent has three layers:

1. **Capture layer**: accept pasted text, screenshots, browser-visible content, or Computer Use visible content. This layer records source mode and uncertainty.
2. **Decision layer**: use the `deepvan-daily-action` skill to convert source material into structured signals, apply portfolio policy, and run adversarial review.
3. **Persistence layer**: save raw notes, structured JSON signals, and action memos under `data/raw`, `data/signals`, and `data/actions`.

## Skill Boundary

The `skills/deepvan-daily-action` skill is the reusable operating manual. It contains:

- `SKILL.md`: daily workflow and safety boundaries.
- `references/signal_schema.md`: signal JSON format and extraction rules.
- `references/portfolio_policy.md`: current portfolio ranges and replacement tests.
- `references/adversarial_review.md`: first-principles and adversarial review checks.
- `scripts/score_daily_action.py`: deterministic scoring of structured signals into a markdown memo.

## Daily Workflow

1. Gather today's DeepVan source material.
2. Convert source material into the JSON schema.
3. Run scoring script when JSON is available.
4. Review output manually with first-principles and adversarial checks.
5. Save the action memo.
6. Do not trade automatically; the user decides any real transaction.

## Default Portfolio Policy

Target ranges:

- QD / overseas funds: 50%-70%
- A-share quant fund: 20%-35%
- Defense / cash / low-volatility: 5%-15%
- A-share single-stock alpha: 0%-15%

Single-stock A-share exposure is a satellite module. It should activate only when a stock-specific signal clearly beats the quant-fund alternative and has an explicit exit condition.

## Safety Model

Computer Use or browser control may be used only to read visible content. The agent must stop for:

- Login or authorization prompts.
- CAPTCHA.
- Platform permission or paywall barriers.
- Requests to post, comment, upload, trade, or transmit sensitive data.
- Unexpected account or security settings.

## Success Criteria

- A future Codex session can use the skill without reconstructing the whole conversation.
- Daily output has consistent sections: conclusion, signals, scores/actions, adversarial review, forbidden actions, next-day counter-evidence.
- The workflow works with manually structured JSON before any UI automation is added.
- The script can produce a markdown memo from sample structured signals.

## Open Extension Points

- Add a Computer Use capture guide after the first successful visible-page run.
- Add OCR-assisted screenshot extraction if screenshots become the main input.
- Add market-data checks only after the decision workflow is stable.
- Add a scheduled reminder/automation only after the user confirms the daily operating time and source window.
