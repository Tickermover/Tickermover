# AlphaHunt — Backtest Summary
_Generated 2026-05-09T16:46:47_

## Headline numbers

| Metric | Value | Interpretation |
|---|---|---|
| **Information Coefficient (avg)** | +0.0836 | real edge |
| IC consistency (% checkpoints positive) | 72.0% | consistent |
| Total return | +127.9% | over backtest window |
| SPY benchmark | +87.1% | same window |
| **Excess vs SPY** | +40.8% | the only number that really matters |
| Max drawdown | -31.5% | worst peak-to-trough |
| Total closed trades | 59 | sample size |
| Hit rate | 33.9% | trades that ended profitable |
| Avg winner | +88.62% | when right |
| Avg loser  | -12.65% | when wrong |
| Best trade | +546.4% | top winner |
| Worst trade| -58.5% | top loser |

## Per-year performance

| Year | Trades | Hit Rate | Avg Trade |
|---|---|---|---|
| 2024 | 16 | 31.2% | -5.19% |
| 2025 | 29 | 20.7% | -5.77% |
| 2026 | 14 | 64.3% | +109.23% |

## Caveats — read carefully

- **Universe survivorship bias:** the 187 tickers are today's curated list. Some weren't public 5 years ago and we skip dates with no price. Results are biased upward because we know which IPOs survived.
- **Score reconstruction is partial:** only price-derived components (momentum, RS, RSI, 52w distance, volume spike, trend strength, breakout proximity). Fundamental + analyst + insider + social signals (9 of 19 weights in live app) are NOT included — historical point-in-time data isn't available.
- **No transaction costs / slippage / taxes:** subtract ~1% per round-trip for an honest net-of-fees estimate.
- **Exit-only validation here:** the 4-rule exit (8% stop + stair-step trail) is the well-tested CAN SLIM-derived part. The novel claim that needs the IC to back it up is whether the *score itself* picks better stocks than random.
- **A higher hit rate doesn't equal a better strategy:** asymmetric payoff (big winners, small losers) drives long-run CAGR. Watch avg winner vs avg loser.

## How to read these results

**The IC is the single most important number.**

- IC ≥ 0.05 average AND ≥ 60% of checkpoints positive → score has real predictive power, you can confidently market the model.
- IC between 0.02 and 0.05 → weak edge, possibly real but easy to lose to costs.
- IC < 0.02 or wildly inconsistent → score is essentially noise. The good performance is coming from the EXIT logic + market beta, not the score itself.

If IC is weak, don't despair — the exit rules alone (cut at -8%, ride winners) on a random selection of Grade A stocks would still beat undisciplined trading.
