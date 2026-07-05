# TickerMover — Backtest Summary
_Generated 2026-07-05T19:31:41_

## Headline numbers

| Metric | Value | Interpretation |
|---|---|---|
| **Information Coefficient (avg)** | +0.0324 | weak / noise — re-tune weights |
| IC consistency (% checkpoints positive) | 61.2% | consistent |
| Total return | +134.3% | over backtest window |
| SPY benchmark | +89.4% | same window |
| **Excess vs SPY** | +44.9% | the only number that really matters |
| Max drawdown | -21.9% | worst peak-to-trough |
| Total closed trades | 209 | sample size |
| Hit rate | 31.6% | trades that ended profitable |
| Avg winner | +61.11% | when right |
| Avg loser  | -9.42% | when wrong |
| Best trade | +586.8% | top winner |
| Worst trade| -38.8% | top loser |

## Per-year performance

| Year | Trades | Hit Rate | Avg Trade |
|---|---|---|---|
| 2022 | 36 | 11.1% | -7.39% |
| 2023 | 44 | 20.5% | -4.20% |
| 2024 | 31 | 45.2% | +2.16% |
| 2025 | 56 | 26.8% | +1.23% |
| 2026 | 42 | 57.1% | +71.45% |

## Caveats — read carefully

- **Universe survivorship bias:** the 187 tickers are today's curated list. Some weren't public 5 years ago and we skip dates with no price. Results are biased upward because we know which IPOs survived.
- **Score reconstruction is partial:** only price-derived components (momentum, RS, RSI, 52w distance, volume spike, trend strength, breakout proximity). Fundamental + analyst + insider + social signals (9 of 19 weights in live app) are NOT included — historical point-in-time data isn't available.
- **No transaction costs / slippage / taxes:** subtract ~1% per round-trip for an honest net-of-fees estimate.
- **Exit validation here:** the exit (8% hard stop + stair-step trail, no take-profit cap) is the well-tested CAN SLIM-derived part. The novel claim that needs the IC to back it up is whether the *score itself* picks better stocks than random.
- **Theme cap applied:** selection enforces max-per-theme (sub-sector) diversification, matching live. Run `--max-per-theme 99` to see the uncapped book for comparison.
- **Regime book-defense NOT modeled:** live raises the entry bar in a risk-off tape via the macro overlay (SPY/QQQ/VIX/^TNX), which isn't reconstructed here. The backtest fills top-N every checkpoint regardless of regime, so it understates the live system's drawdown protection.
- **A higher hit rate doesn't equal a better strategy:** asymmetric payoff (big winners, small losers) drives long-run CAGR. Watch avg winner vs avg loser.

## How to read these results

**The IC is the single most important number.**

- IC ≥ 0.05 average AND ≥ 60% of checkpoints positive → score has real predictive power, you can confidently market the model.
- IC between 0.02 and 0.05 → weak edge, possibly real but easy to lose to costs.
- IC < 0.02 or wildly inconsistent → score is essentially noise. The good performance is coming from the EXIT logic + market beta, not the score itself.

If IC is weak, don't despair — the exit rules alone (cut at -8%, ride winners) on a random selection of Grade A stocks would still beat undisciplined trading.
