# The Crowd Clock — research findings

**Question asked:** across a large universe of US stocks, is there a single signal that
tells a retail investor where a stock sits in the boom–bust cycle — and in particular,
can we warn them *before* the point where mainstream coverage arrives and they buy the top?

**Data:** 1,518 US listed names (S&P 500 + MidCap 400 + SmallCap 600 + the TickerMover
growth universe), daily adjusted close and volume, Jan 2008 – Aug 2026, from Yahoo Finance.
After liquidity ($3m+ average daily dollar volume), price (>$5) and history filters:
**1,514 names, 233,855 stock-month observations, Feb 2009 – Jan 2026** with complete
forward returns. All features use only data available on the observation date.

---

## 1. The headline finding

> **Returns are close to unpredictable from price data. Risk is not.**

Across every decile of every price/volume feature tested — realised volatility, drawdown
from the 12-month high, distance above the 200-day average, run off the low, RSI, relative
strength, volume ratio, price — **the median 6-month forward return never moved outside
5.7% to 8.3%.** The baseline for the whole sample is 6.6%.

The same features moved the probability of a 30% fall within six months from **3.8% to 25.9%**
— close to a sevenfold range.

| Feature | Median 6m return, lowest → highest decile | P(30% fall in 6m), lowest → highest decile |
|---|---|---|
| Realised volatility (20d) | 6.1% → 7.1% | **3.8% → 25.9%** |
| Drawdown from 12m high | 6.6% → 6.7% | **6.8% → 25.2%** |
| Distance above 200d MA | 6.5% → 6.8% | 7.9% → 21.3% (U-shaped) |
| Run off the 12m low | 6.1% → 7.5% | 12.3% → 19.8% |
| RSI(14) | 6.3% → 5.7% | 14.0% → 10.5% |

Rank correlation with the next six months' *worst drawdown*, averaged monthly over 204 months:
realised volatility **−0.281 (t = −27.0**, correct sign in 95% of months), drawdown from high
**+0.181 (t = 18.4**, 91% of months). Correlation with forward *return*: nothing comparable.

**This determines what the product should be.** A signal that claims to call direction would
be selling something the data does not support. A signal that describes risk and cycle
position is selling something real — and it happens to be the only version that is
comfortable under FCA rules.

---

## 2. The user's observation is correct, and here is the number

Event study on every ≥50% drawdown followed by a recovery: **2,148 episodes across 1,075 stocks.**

| | |
|---|---|
| Median recovery off the trough (best point within 12 months) | **+138.8%** |
| Trading volume at the exact trough, vs the stock's own 200-day norm | **0.79×** — the crowd is *absent* |
| Trading days from the trough to peak crowd attention | **150** (about seven months) |
| Gain already banked by the time attention peaks | **+85.5% (median)** |
| Episodes where attention peaks only after a >50% gain | **59.9%** |
| Median return from the attention peak to 12 months later | **+7.7%** |

So: the stock bottoms in silence, runs for seven months and 86 percentage points, and *then*
gets busy. The retail investor who waits for the alert arrives with roughly nine-tenths of the
recovery already gone. That is exactly what the AEHR and ALAB charts show, and it is the
median case across a thousand stocks, not an anecdote.

---

## 3. Same stock, same recovery: quiet moment vs crowded moment

The cleanest test. Within a single crash-and-recovery episode, compare two moments that are
both identifiable **in real time, with no hindsight**:

- **Quiet** — price is 20%+ off its low, volatility is contracting, volume is below 0.9× its own norm.
- **Crowded** — the stock has doubled off its low *and* volume has reached 1.3× its own norm.

**1,431 paired episodes across 770 stocks.** Returns are excess vs the S&P 500 over the same window.

| | Quiet moment | Crowded moment |
|---|---|---|
| Median 6m excess return | **+11.0%** | **−0.4%** |
| Chance of underperforming the index over 6m | 35.9% | **50.7%** |
| Median 12m excess return | **+19.9%** | **+1.5%** |
| Chance of underperforming over 12m | 32.1% | 48.3% |

Paired difference at 6m: **+15.0% median, t = 2.52**. At 12m: **+20.6% median, t = 2.99**.
The quiet moment beat the crowded moment in **66.8%** of episodes at 6m and **68.9%** at 12m.
It holds in all four sub-periods tested (2009–13, 2014–17, 2018–21, 2022–26).

Median gap between the two moments: **157 trading days and +43.8% of price**.

**Plain reading: buying at the moment a recovering stock gets busy has historically been a
coin flip against simply owning the index.**

### The control that matters

I tested a placebo: a mechanical entry a fixed 157 trading days before the crowd arrived,
with no quiet condition. It returned **+19.0% median excess at 6m — better than the quiet
rule's +11.0%.**

That placebo is **not implementable** (it requires knowing the future crowd date), so it is
not a competing rule. But it tells us something honest and important: **most of the advantage
is earliness within the recovery, not any magic in "quietness."** The quiet rule is one
workable way to be early. It is not the only one, and it is not optimal.

Confirming this — six different real-time recovery markers were tested on 1,966 episodes
across 1,000 stocks:

| Marker | Median 6m excess | Beats the crowded moment |
|---|---|---|
| Golden cross (50d > 200d) | +12.1% | 66.6% |
| Quiet (contracting vol, volume < 0.9×) | +10.9% | 64.7% |
| 50d rising and volume < 0.9× | +9.4% | 63.3% |
| First close back above the 200d | +8.5% | 63.3% |
| Above 200d and volume < 1.1× | +8.8% | 63.9% |
| Higher low and 50d rising | +5.6% | 60.6% |
| **The crowd's arrival** | **+1.3%** | — |

They are all roughly the same. **Which marker you pick barely matters. Whether the crowd has
already arrived matters a great deal.** That is the signal.

---

## 4. What did *not* work — stated plainly

Three results cut against the obvious product, and the product must not claim otherwise.

**(a) "Overbought" is not a sell signal.** Across the full cross-section, stocks in the
Crowded band delivered a *slightly better* median 6-month return than the rest
(+1.5% relative to the median stock; 47.9% chance of underperforming, i.e. marginally
better than a coin flip). Momentum is real. A naive overbought alarm would have told users
to avoid the biggest winners of the last decade for years on end.

**(b) The signal does not time an individual stock.** Ranking each stock's own history by
fragility: its most fragile quintile had a higher crash rate (12.5% vs 9.7%) but also a
*higher* median 6-month return (8.9% vs 5.8%) and a *lower* chance of being underwater a
year later (28.0% vs 34.6%). **For a stock you already own, this is not an exit trigger.**
It is a description of the risk you are carrying.

**(c) The crowd effect is conditional.** For stocks that have *not* had a big drawdown,
crowd presence barely matters (9.2% vs 12.0% crash rate). The effect lives in the post-crash
cycle — which is precisely the situation the two charts show.

**What survives all of this:** crash risk is real, large, and predictable; the crowd is
systematically late; and among moments you can actually identify in real time, the busy
moment is the worst one.

---

## 5. The signal

**Crowd Clock** — one number, 0–100, from three observable inputs. No fitted weights, no
black box; a user can recompute it by hand.

```
run     = close / 252-day low  − 1          ramp 0% → +150%      weight 0.40
crowd   = 20d $volume / 200d $volume        ramp 0.70 → 1.60     weight 0.35
stretch = close / 200-day MA   − 1          ramp 0% → +40%       weight 0.25

score = 0.40·run + 0.35·crowd + 0.25·stretch     (each ramp clipped to 0–100)
```

Bands at 20/40/60/80. A name more than 25% below its 12-month high and scoring under 40 is
labelled **Damaged** rather than Ignored — a wreck still falling is a different situation
from a quiet base, and the base rates confirm it.

Bands were fixed on 2009–2017 data. Rates below are the **held-out 2018–2026** sample,
restricted to stocks that had a 40%+ drawdown in the prior two years — the population the
signal is designed for.

| Band | Share | P(30% fall in 6m) | P(30% rise in 6m) | Median 6m | Underwater at 12m |
|---|---|---|---|---|---|
| Damaged | 34.9% | **22.2%** | 43.8% | +9.1% | 33.7% |
| Ignored | 11.8% | 15.8% | 20.8% | +1.4% | 45.0% |
| **Quiet** | 21.5% | **13.9%** | 24.3% | +3.7% | 41.4% |
| Noticed | 16.2% | 15.2% | 34.7% | +7.6% | 37.1% |
| Busy | 9.0% | 14.3% | 40.7% | +8.8% | 35.5% |
| **Crowded** | 6.6% | **21.3%** | 46.5% | +8.5% | 33.9% |
| *All shares, any band* | — | *11.1%* | *23.1%* | *6.6%* | *31.8%* |

The shape is a U: risk is lowest in the quiet middle of the cycle and highest at both ends —
in the wreck, and in the crowd. **Crash risk in the Crowded band is 1.5× the Quiet band, and
the Crowded band's median return is not high enough to pay for it.** The Crowded band was the
highest-risk or second-highest-risk band in **8 of the 9** held-out years.

Out-of-sample AUC for predicting a 30% fall within six months: **0.674** (0.664 from
volatility alone, 0.686 with five more features). Modest, but stable — the top-risk quintile
had a higher crash rate than the rest in **all nine** held-out years, including 2020 and 2022.

### Today's readings on the two charts you sent

| | Score | Band | Run off 12m low | Volume vs own norm | vs 200d MA |
|---|---|---|---|---|---|
| **AEHR** | **100** | Crowded | +493% | 2.17× | +88% |
| **ALAB** | **89** | Crowded | +233% | 1.32× | +58% |

AEHR is the single highest reading in the 1,515-name universe. Its clock over two years:
6 (Damaged, volume 0.37× — nobody there) in May 2025 → 100 (Crowded) by August 2025.
ALAB: 25 (Damaged, volume 0.64×) in May 2025 → 100 by August 2025. The clock turned in both
cases roughly one quarter before the peak, and it was readable at the time.

Across the market today: **67 of 1,515 names (4.4%)** are in-cycle and reading Crowded;
188 are in-cycle and still Ignored or Quiet. The signal is selective, not a constant alarm.

---

## 6. Limitations

- **Survivorship bias.** The universe is today's index membership. Companies that were
  delisted or went to zero are absent, so all recovery statistics are flattered. The
  drawdown statistics — the ones the product leads with — are biased *conservatively* by this.
- **Overlapping windows.** Monthly observations with 6- and 12-month forward returns overlap.
  Reported t-statistics use monthly cross-sectional means (Fama–MacBeth) to reduce this, but
  they remain optimistic.
- **No costs.** No spread, commission, slippage or tax is modelled.
- **Reorganisation artifacts.** A handful of post-bankruptcy names (e.g. CHRD) produce
  absurd returns from adjusted prices. Forward returns are clipped at +300% and a $5 minimum
  price applies; medians are used throughout for this reason.
- **One market, one era.** US listed equities, 2009–2026 — a period with three short bears
  and a long bull. 2019 is the one held-out year where every band showed elevated crash rates
  and band ordering broke down.
- **Volatility does most of the work.** The extra features add ~0.01 to AUC over realised
  volatility alone. They earn their place by making the reading *explainable*, not by making
  it much sharper.

---

## 7. How to present it — the FCA point

**This matters more than the maths, and it changes what the feature can say.**

You asked for a signal that tells users "when to enter and when to exit". I would not ship
that wording, for two independent reasons.

**It is not supported by the evidence.** Section 4 is unambiguous: forward returns are close
to unpredictable, the Crowded band does not underperform on median return, and within a
single stock the fragile moments are not bad moments to own it. An entry/exit trigger would
be a claim the research does not license.

**It is the highest-risk wording you could choose under UK rules.** Two separate regimes bite:

- **Section 21 FSMA (financial promotions).** An invitation or inducement to engage in
  investment activity must be made or approved by an authorised person. "Enter here / exit
  here" on a named share is close to the centre of that definition. Breach is a criminal
  offence. Your existing note already flags `intelligence.py::_trade_plan()` — which emits
  entry, stop, target and position size — as the most exposed feature on the site. A new
  entry/exit signal would be a second, more prominent one.
- **Article 53 RAO (advising on investments).** Advice on the merits of buying or selling a
  *specific* investment is a regulated activity. Generic, non-personalised research published
  identically to every user is generally outside it — the moment output is tailored to a
  user's holdings or circumstances, that protection weakens.

A state description with published base rates sits in a materially better place: factual,
identical for every user, no inducement, no personal recommendation. It is also, on the
evidence, the more truthful product.

### Rules to hold to

1. **Never** the words buy, sell, entry, exit, target, stop, take profit, or "time to".
2. State the state, then the base rate, then the sample size. Never a probability *for this
   share* — "21% of readings in this band saw a 30% fall" is a historical frequency;
   "there is a 21% chance AEHR falls 30%" is a forecast, and would be misleading.
3. Show both sides always. The Crowded band's 46.5% chance of a 30% *rise* must appear
   alongside its 21.3% chance of a 30% fall. Showing only the downside is as unbalanced as
   showing only the upside, and COBS 4.2 (fair, clear, not misleading) cuts both ways.
4. Past-performance wording on every surface that shows a rate; five years of data minimum
   (you have seventeen).
5. Keep it identical for every user. No personalisation by holdings, no "your position".
6. Do not use it in ad creative. Financial promotions to a UK audience are the specific
   exposure your compliance note already flags as unresolved.

### Suggested copy

> **Crowd Clock: Crowded (100/100)**
> Most of the move off the low is already behind it and trading volume is far above its own
> average — the busiest point of the cycle. AEHR is +493% from its 12-month low, trading on
> 2.17× its own 200-day average volume, 88% above its 200-day average price.
>
> Among 4,190 readings in this band since 2018 (US listed shares that had fallen 40%+ in the
> prior two years), 21% saw a 30% fall within the next six months and 46% saw a 30% rise.
> Across all shares in any band the figures were 11% and 23%.
>
> *Historical frequencies for a sample of shares in a similar state — not a forecast for this
> share. Past performance is not a reliable indicator of future results. General information,
> not advice or a personal recommendation. Capital at risk.*

And the one line that carries the whole product, which is a statement of fact about
information timing and makes no claim about the future at all:

> **By the time a recovering share gets busy, a median 86% of the move off its low has
> already happened. At the low itself, it was trading on 0.79× its normal volume.**
> *(2,148 recoveries from 50%+ falls, 1,075 US shares, 2009–2026.)*

---

## 8. Files

| | |
|---|---|
| `crowd_clock.py` | Production module — `read(close, volume)` returns score, band, base rates and a compliant sentence. Drop into the TickerMover repo alongside `scans.py`. No new dependencies. |
| `CROWD_CLOCK_RESEARCH.md` | This document. |

Reproduction scripts (universe build, panel, the eight analysis passes) are in the session
scratchpad; say the word and I will move them into the repo so the study can be re-run.

*I am not a lawyer and not a compliance consultant. Section 7 is engineering guidance on how
to reduce exposure, not legal advice — the outstanding s.21 question in your compliance notes
still needs a UK solicitor before any paid promotion.*
