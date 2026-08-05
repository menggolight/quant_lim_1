# Signal Schema

Use this schema when converting DeepVan posts, screenshots, or user summaries into structured signals.

## JSON Shape

```json
{
  "date": "2026-07-03",
  "source_mode": "pasted_text",
  "signals": [
    {
      "title": "short title or post topic",
      "asset": "qd",
      "direction": "bullish",
      "strength": 4,
      "confidence": 3,
      "horizon": "days",
      "summary": "non-verbatim summary",
      "evidence": "brief source-derived reason",
      "counter_evidence": "what would weaken this signal"
    }
  ]
}
```

`current_weights` is optional. In the integrated daily pipeline it is injected from the latest immutable portfolio snapshot legally available at `captured_at`. A manually supplied `current_weights` remains supported for isolated experiments, but its provenance must be documented. Cash is combined into `defense` for the B3 four-bucket scorer.

## Enumerations

`source_mode`:

- `pasted_text`
- `screenshot`
- `browser_visible`
- `computer_use_visible`
- `manual_summary`

`asset`:

- `qd`
- `quant`
- `defense`
- `a_share_alpha`
- `ai_semiconductor`
- `us_growth`
- `sp500`
- `nasdaq`
- `emerging_market`
- `gold_resource`
- `utilities`
- `cash`

`direction`:

- `bullish`
- `bearish`
- `risk_off`
- `risk_on`
- `rotate_to`
- `rotate_from`
- `hold`
- `watch`

`horizon`:

- `intraday`
- `days`
- `week`
- `earnings_event`
- `macro_event`
- `structural`

## Strength and Confidence

Use `strength` for signal intensity:

| Value | Meaning |
|---:|---|
| 1 | Weak mention, no action |
| 2 | Watchlist signal |
| 3 | Relevant but needs confirmation |
| 4 | Strong action candidate |
| 5 | Explicit or repeated high-conviction signal |

Use `confidence` for source clarity:

| Value | Meaning |
|---:|---|
| 1 | OCR or context uncertain |
| 2 | Partial screenshot or incomplete quote |
| 3 | Clear summary but no full source context |
| 4 | Clear source text with context |
| 5 | Explicit action language plus confirmed context |

## Extraction Rules

- Map broad overseas technology or Nasdaq signals to `qd` and optionally `nasdaq`.
- Map S&P 500, SCHD-like, or broad US allocation signals to `qd` and optionally `sp500`.
- Map domestic market breadth or quant-fund comments to `quant`.
- Map utilities, dividend, cash, or low-volatility comments to `defense`.
- Map specific A-share opportunities to `a_share_alpha` only when the post implies stock-specific alpha.
- Use `watch`, not `bullish`, when the post only says to wait for earnings, capex guidance, or a future trigger.
- Preserve uncertainty. A single emotional post should have lower confidence than a clear adjustment post.
