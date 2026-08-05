# Adversarial Review

Run this review before issuing any action recommendation.

## First-Principles Checks

An action is valid only if it improves expected portfolio outcome through at least one of these:

1. Better exposure to a durable earnings or cash-flow driver.
2. Better risk-adjusted exposure than the current fund alternative.
3. Lower drawdown risk without abandoning a still-valid thesis.
4. Better response to changed evidence, not changed emotion.

## Required Questions

### Source Reliability

- Is this an explicit investment action, a research note, a joke, or an emotional update?
- Is the context complete enough to infer an action?
- Is the source newer than the existing rule it appears to change?

### Price-In Risk

- Has the cited theme already moved sharply?
- Is the expected catalyst still ahead, or already reflected?
- Is the action driven by realized performance rather than forward evidence?

### Portfolio Fit

- Does this add a new exposure or just duplicate QD technology risk?
- If it is an A-share stock, why is it better than the quant fund?
- If it is a QD increase, why is it not concentration in US growth?

### Exit Logic

- What specific evidence would prove the action wrong?
- What position size keeps the mistake tolerable?
- Is there a time limit if the expected catalyst does not appear?

## Default Blocks

Block or downgrade the recommendation when:

- The signal is based on one unclear screenshot.
- The action would push QD above the target range without strong confirmation.
- The action would buy an A-share stock with no clear edge over the quant fund.
- The only reason is that the holding has recently gone up.
- The plan lacks an exit condition.

## Output Language

Use plain language:

- "维持" when evidence does not clear action threshold.
- "观察" when signal is relevant but unconfirmed.
- "小幅" when confidence is moderate or portfolio concentration is high.
- "明确动作" only when signal, portfolio fit, and exit logic all pass.
