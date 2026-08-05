# Portfolio Policy

Use this policy together with the latest point-in-time JSON snapshot under `data/portfolio/`. The JSON snapshot is the position truth source; this document defines constraints rather than mutable balances.

## Current Baseline

Reconciled visible accounts from `data/portfolio/2026-07-13.v3.json`:

| Bucket | Current Weight | Role |
|---|---:|---|
| QD / overseas funds | 25.54% | Global growth, Nasdaq, S&P 500, emerging markets |
| A-share quant fund | 7.67% | Domestic diversified alpha base |
| Defense / cash / low-volatility | 1.94% | Utilities plus account cash |
| A-share single-stock alpha | 64.84% | Midea Group, 200 shares |

These weights are a dated snapshot, not constants. The daily pipeline must resolve the newest snapshot whose `as_of` is no later than the decision time. It must never use a later snapshot to score an earlier decision.

## Target Ranges

| Bucket | Target Range | Notes |
|---|---:|---|
| QD / overseas funds | 50%-70% | Do not keep adding when US tech concentration, valuation, real rates, or QDII premium are elevated. |
| A-share quant fund | 20%-35% | Default A-share exposure. Individual stocks must beat this alternative. |
| Defense / cash / low-volatility | 5%-15% | Raise when QD risk rises and no strong new opportunity is confirmed. |
| A-share single-stock alpha | 0%-15% | Satellite only. Start 3%-5%; extreme conviction still capped near 8%-10% per stock. |

Target ranges are research guardrails, not an instruction to rebalance to every midpoint in one trade.

## Action Constraints

- Prefer funds when the opportunity is broad beta.
- Use A-share single stocks only when there is clear individual alpha that a fund does not capture.
- Do not add to QD only because a single post is enthusiastic.
- Do not reduce risk only because of normal volatility; require logic damage, risk score deterioration, or explicit risk-off signal.
- Keep actions small when source confidence is low or post context is incomplete.
- If a single stock is already above the 15% research ceiling, no signal may recommend adding it. Positive evidence may only affect the pace of concentration reduction.
- When no additional cash is available, do not produce recommendations that assume new external funding; describe any increase as a funded switch and include the source bucket.
- Do not calculate an executable fund trade until product code, dealing cutoff, fees, and redemption constraints are known.

## Replacement Tests

Before buying an A-share stock:

1. Can the same idea be expressed through the existing quant fund or a thematic fund?
2. Is expected upside clearly better than keeping the quant fund?
3. Is downside bounded by a concrete exit condition?
4. Is the position size small enough that being wrong does not damage the full portfolio?

Before adding QD:

1. Is AI capex or overseas growth still being confirmed by market data?
2. Is Nasdaq strength broad enough, not just a few mega caps?
3. Are nominal and real rates not moving against long-duration growth?
4. Is QDII premium or purchase friction not consuming the expected return?
