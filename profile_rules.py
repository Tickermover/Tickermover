"""
AlphaHunt — Risk profile assignment rules.

Maps a ticker (with its loaded fundamentals/risk metrics) to a set of
profile memberships: {"aggressive", "balanced", "conservative"}.

Used by app.py to enrich /api/universe with a `profiles` field per ticker
so the frontend can filter the universe without reapplying heuristics.

DESIGN:
- Aggressive: everyone qualifies (the current default).
- Balanced: drops F-grade, drops very-high-beta names, requires a real
  score, requires membership in a major index (SPX / NDX / DJI) as a
  liquidity / institutional-coverage floor.
- Conservative: A or B grade, beta < 1.3, profitable, low short interest,
  pays a dividend (yield > 0.5%) OR is on the Dow 30 (which is the
  traditional 'blue chip' bar), AND is an S&P 500 member.
"""
from __future__ import annotations

from index_constituents import DOW_30, NASDAQ_100, SP500


def _f(v, default: float = float("nan")) -> float:
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return default


def assign_profile(t: dict) -> list[str]:
    """Return the SINGLE primary profile this ticker fits.

    Mutually exclusive bucketing based on the stock's real risk profile —
    primarily BETA, with quality / fundamentals as secondary gates.
    Every ticker lands in exactly one of {conservative, balanced, aggressive}.

    Rationale (May 19 user feedback): the prior 'everyone is aggressive +
    maybe also balanced + maybe also conservative' design made the
    Aggressive bucket meaningless (it was just the full universe).
    Categorization should reflect the stock's actual character.
    """
    sym = (t.get("ticker") or "").upper()

    grade     = (t.get("grade") or "").upper()
    score     = _f(t.get("smart_score", t.get("pop_score", 0)), 0)
    beta      = _f(t.get("beta"))
    pm        = _f(t.get("profit_margin"))
    short_pct = _f(t.get("short_percent_float", t.get("short_interest")))
    div_yield = _f(t.get("dividend_yield"))   # fraction (0.025 = 2.5%)
    market_cap= _f(t.get("market_cap"), 0)

    in_spx = sym in SP500
    in_ndx = sym in NASDAQ_100
    in_dji = sym in DOW_30
    in_any_major = in_spx or in_ndx or in_dji

    has_beta = beta == beta  # NaN check (NaN != NaN)
    has_pm   = pm   == pm
    has_short= short_pct == short_pct

    # ─── 1. AGGRESSIVE: high-risk / high-volatility / high-conviction ───
    # Any of these flags puts a ticker in the Aggressive bucket regardless
    # of other criteria. These are the names that swing meaningfully.
    if (has_beta and beta >= 1.5):                  return ["aggressive"]
    if (has_short and short_pct >= 10):             return ["aggressive"]
    if (has_pm and pm < 0):                         return ["aggressive"]  # loss-making
    if grade in ("D", "F"):                         return ["aggressive"]
    if not in_any_major and market_cap < 10e9:      return ["aggressive"]  # illiquid

    # ─── 2. CONSERVATIVE: low-beta, profitable quality, income-eligible ───
    # Must clear ALL of these gates. Roughly the universe of names a
    # widow-and-orphans fund would own.
    cons_ok = (
        has_beta and beta < 1.0                    # actually low-beta
        and grade in ("A", "B")
        and has_pm and pm > 0                       # profitable
        and (not has_short or short_pct < 5)
        and score >= 55
        and (in_spx or in_dji)                      # S&P or Dow only
    )
    has_dividend = div_yield == div_yield and div_yield >= 0.005
    large_cap_a  = market_cap >= 50e9 and grade == "A"
    income_exempt = has_dividend or in_dji or large_cap_a
    if cons_ok and income_exempt:
        return ["conservative"]

    # ─── 3. BALANCED: everything else (the middle ground) ───
    # Mainstream names — moderate beta, decent quality, no red flags.
    return ["balanced"]
