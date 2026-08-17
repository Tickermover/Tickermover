"""
diligence — the hard questions, computed.

WHAT THIS IS FOR
Most stock pages tell you what the numbers ARE. Almost none tell you which
numbers should not be trusted at face value, and that is where the edge is.
This module looks for the specific arithmetic and structural tells that a
careful analyst checks by hand and a retail reader never sees:

  - a "consensus" price target whose high is several times its low, which is
    not a consensus at all but a violent disagreement hidden behind a mean
  - an operating margin above the gross margin, which normal operations cannot
    produce and which therefore means a one-off gain or a bad data feed
  - profit that is not converting to cash
  - growth measured off a collapsed base, which does not annualise
  - institutional ownership so high there is no marginal buyer left
  - price and fundamentals pointing opposite ways

Each finding is a QUESTION with the evidence attached and a note on what would
resolve it. None of them is a verdict, and none says buy, sell, avoid or
prefer. The point is to hand the reader the thing a professional would go and
check, not to check it for them.

WHY IT IS DETERMINISTIC
No AI. Every finding is arithmetic over figures we already hold, so it is free,
instant, reproducible, and cannot hallucinate. A model asked for "hard
questions about NVDA" writes plausible generic ones; this finds the actual
anomaly in the actual filing data, which is the difference between sounding
insightful and being useful.

COMPLIANCE
Questions and observations only. Severity ranks how unusual something is, never
how attractive the stock is. Nothing here is advice or a recommendation.
"""
from __future__ import annotations

import math
from typing import Any, Optional

# Severity is about how ANOMALOUS a reading is, not how good or bad the
# investment is. "high" means the number probably does not mean what it appears
# to mean; it never means "danger, sell".
SEV_HIGH, SEV_MED, SEV_LOW = "high", "medium", "low"


def _n(t: dict, *keys: str) -> Optional[float]:
    for k in keys:
        v = t.get(k)
        if v is None or isinstance(v, bool):
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            return f
    return None


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v * 100:.1f}%"


def _money_short(v: float) -> str:
    n = abs(v or 0)
    if n >= 1e12:
        return f"${n / 1e12:.1f}T"
    if n >= 1e9:
        return f"${round(n / 1e9)}B"
    if n >= 1e6:
        return f"${round(n / 1e6)}M"
    return f"${round(n)}"


def _q(sev, title, evidence, why, resolve) -> dict:
    return {"severity": sev, "title": title, "evidence": evidence,
            "why": why, "resolve": resolve}


def checks(t: dict) -> list[dict]:
    """Hard questions for one company. Ordered most anomalous first."""
    out: list[dict] = []
    tk = (t.get("ticker") or "").upper()

    # ── 1. The consensus that isn't ──────────────────────────────────
    lo, hi = _n(t, "target_low"), _n(t, "target_high")
    mean, na = _n(t, "target_mean", "street_target"), _n(t, "total_analysts", "analyst_count")
    if lo and hi and mean and lo > 0 and hi / lo >= 2.0:
        out.append(_q(
            SEV_HIGH,
            "Is there actually a consensus here?",
            f"{int(na) if na else 'The'} analysts' targets run ${lo:,.0f} to ${hi:,.0f} — "
            f"the high is {hi/lo:.1f}x the low — behind a mean of ${mean:,.0f}.",
            "A mean target implies agreement. A high several times the low means the "
            "people paid to model this company cannot agree what it is worth, usually "
            "because the outcome hinges on one assumption. The average of two "
            "incompatible views describes neither.",
            "Read the bull and bear notes rather than the mean, and find the single "
            "assumption they disagree on. That assumption is the investment case.",
        ))

    # ── 2. Margin stack that cannot happen ───────────────────────────
    gm, om, pm = _n(t, "gross_margin"), _n(t, "operating_margin"), _n(t, "profit_margin")
    if gm is not None and om is not None and om > gm + 0.005:
        out.append(_q(
            SEV_HIGH,
            "Why is operating margin above gross margin?",
            f"Operating margin {_pct(om)} exceeds gross margin {_pct(gm)}.",
            "Operating profit is gross profit minus operating costs, so it cannot "
            "normally exceed it. This reading means either a one-off gain is sitting "
            "in operating income — a disposal, a legal settlement, a revaluation — or "
            "the data feed is mixing periods or definitions.",
            "Check the income statement in the latest filing. If it is a one-off, the "
            "margin, the P/E and every ratio built on earnings are flattered.",
        ))
    if gm is not None and pm is not None and pm > gm + 0.005:
        out.append(_q(
            SEV_HIGH,
            "Why is net margin above gross margin?",
            f"Net margin {_pct(pm)} exceeds gross margin {_pct(gm)}.",
            "Net profit sits below gross profit in the income statement, so exceeding "
            "it requires income from outside normal operations — a tax benefit, an "
            "investment gain, a divestiture — or a data error.",
            "Find the line that bridges the two in the filing. If it is non-recurring, "
            "this year's earnings are not a run rate.",
        ))

    # ── 3. Profit that is not becoming cash ──────────────────────────
    fcf = _n(t, "fcf_margin")
    if pm is not None and fcf is not None and pm > 0.05 and fcf < pm * 0.5:
        out.append(_q(
            SEV_MED,
            "Where is the profit going?",
            f"Net margin {_pct(pm)} but free-cash-flow margin only {_pct(fcf)} — "
            f"about {fcf/pm*100:.0f}p of cash per pound of reported profit.",
            "Accounting profit and cash diverge for legitimate reasons — a capex "
            "cycle, inventory build, receivables growth — and for unwelcome ones. "
            "Sustained divergence is the single most common early tell that reported "
            "earnings flatter the business.",
            "Compare the cash-flow statement against net income. Working capital and "
            "capex explain most gaps; if neither does, ask what does.",
        ))

    # ── 4. Growth measured off a hole ────────────────────────────────
    g = _n(t, "revenue_growth_yoy")
    qi = t.get("quarterly_income")
    if g is not None and g >= 1.0:
        base_note = ""
        if isinstance(qi, list) and len(qi) >= 2:
            revs = [x.get("revenue") for x in qi if isinstance(x, dict)]
            revs = [float(r) for r in revs if isinstance(r, (int, float))]
            if len(revs) >= 2 and min(revs) > 0 and max(revs) / min(revs) >= 2:
                base_note = (f" Quarterly revenue in the last four prints ranges "
                             f"{min(revs)/1e9:.1f}bn to {max(revs)/1e9:.1f}bn.")
        out.append(_q(
            SEV_MED,
            "Is this growth, or is it a recovery?",
            f"Revenue growth of {g*100:.0f}% year on year.{base_note}",
            "A number this size is usually measured against a collapsed prior year — a "
            "cycle trough, a lost customer, a shutdown. Recovery growth and structural "
            "growth look identical in a percentage and behave nothing alike: one "
            "decelerates hard as the base normalises, the other does not.",
            "Compare revenue to the same quarter TWO years ago, not one. If the "
            "two-year line is flat, this is a round trip, not expansion.",
        ))

    # ── 5. No marginal buyer left ────────────────────────────────────
    inst = _n(t, "held_pct_institutions")
    if inst is not None and inst >= 0.90:
        out.append(_q(
            SEV_MED,
            "Who is left to buy this?",
            f"Institutions already hold {_pct(inst)} of the shares.",
            "Ownership this concentrated means the natural buyers are already in. "
            "Upside then depends on existing holders adding rather than new money "
            "arriving, and crowded positions exit through the same door when the "
            "thesis changes.",
            "Look at the direction of institutional holdings over recent quarters, not "
            "the level. Rising into a high number is different from stalling at one.",
        ))

    # ── 6. Price and fundamentals disagreeing ────────────────────────
    dist = _n(t, "dist_52w_high")
    if dist is not None and dist <= -20 and g is not None and g >= 0.20:
        out.append(_q(
            SEV_MED,
            "Why is the price ignoring the growth?",
            f"Revenue growing {g*100:.0f}% while the shares sit {abs(dist):.0f}% below "
            f"their 52-week high.",
            "The market is discounting something the trailing figures do not show — a "
            "peak-earnings fear, a pricing cycle turning, a customer concentration "
            "risk, or a multiple that simply got ahead of itself. The gap is "
            "information, not an oversight.",
            "Read the most recent guidance and the risk factors. The market is usually "
            "reacting to the forward statement, not the reported quarter.",
        ))

    # ── 7. Leverage ──────────────────────────────────────────────────
    de = _n(t, "debt_to_equity")
    if de is not None and de >= 2.0:
        out.append(_q(
            SEV_MED,
            "Can the balance sheet take a bad year?",
            f"Debt to equity of {de:.2f}.",
            "Leverage amplifies both directions. What matters is not the ratio but "
            "whether cash generation covers interest through a downturn, and when the "
            "debt has to be refinanced.",
            "Check the maturity schedule and interest cover in the filing. Debt due "
            "inside two years at higher rates is the part that bites.",
        ))

    # ── 8. Insider ownership ─────────────────────────────────────────
    # Only where it is actually anomalous. This check used to fire on any name
    # under 0.5% and then open by admitting "low insider ownership is normal in
    # a large company and says nothing on its own" — which is true, and is a
    # reason not to print it. On a $840B company like AMD, 0.4% is simply what
    # large-cap ownership looks like. Below ~$20B it is a real signal: founders
    # and operators usually still hold something, and a near-zero reading says
    # the alignment runs entirely through the pay plan.
    ins = _n(t, "held_pct_insiders")
    mc = _n(t, "market_cap")
    if ins is not None and ins < 0.005 and mc is not None and mc < 20e9:
        out.append(_q(
            SEV_LOW,
            "Who is aligned with the shares here?",
            f"Insiders hold {_pct(ins)} of a {_money_short(mc)} company.",
            "At this size you would normally still find founders or long-tenured "
            "operators on the register. Near-zero insider ownership means the people "
            "running it are aligned through the compensation plan rather than through "
            "the shares themselves.",
            "Read the proxy statement for what the bonus actually rewards — revenue, "
            "EPS, TSR or something else. That is the behaviour you are underwriting.",
        ))

    # ── 9. Short interest ────────────────────────────────────────────
    sh = _n(t, "short_percent_float")
    if sh is not None and sh >= 0.10:
        out.append(_q(
            SEV_MED,
            "What do the short sellers see?",
            f"Short interest is {_pct(sh)} of the float.",
            "A double-digit short position means a body of professional money is "
            "positioned against the story, and they publish their reasoning more often "
            "than the longs do. It also makes the shares prone to violent moves in "
            "both directions.",
            "Find a published short thesis and read it against the filing. Either it "
            "identifies something real, or knowing why it is wrong strengthens the case.",
        ))

    order = {SEV_HIGH: 0, SEV_MED: 1, SEV_LOW: 2}
    out.sort(key=lambda q: order.get(q["severity"], 3))
    return out


def for_pair(ta: dict, tb: dict) -> dict:
    """Hard questions for both sides of a comparison."""
    return {
        (ta.get("ticker") or "").upper(): checks(ta),
        (tb.get("ticker") or "").upper(): checks(tb),
    }
