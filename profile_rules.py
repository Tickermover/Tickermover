"""
AlphaHunt — Risk profile assignment rules.

Maps a ticker (with its loaded fundamentals/risk metrics) to a single
profile membership: {"aggressive", "balanced", "conservative"}.

Used by app.py to enrich /api/universe with a `profiles` field per ticker
so the frontend can filter the universe without reapplying heuristics.
"""
from __future__ import annotations

from index_constituents import DOW_30, NASDAQ_100, SP500


def _f(v, default: float = float("nan")) -> float:
    try:
        f = float(v)
        return f
    except (TypeError, ValueError):
        return default


_DEFENSIVE_SECTORS = {
    "Utilities", "Real Estate", "Consumer Defensive", "Energy", "Healthcare",
}


def assign_profile(t: dict) -> list[str]:
    """Return the SINGLE primary profile this ticker fits.

    Beta-driven, mutually-exclusive bucketing. Categorization reflects the
    stock's actual risk character — not just model grade (which is
    tech-tilted and unfairly penalises slow-growth defensives).

    May 19 audit (live dev) found:
    - dividend_yield is null for the ENTIRE universe (data pipeline gap),
      so the original income gate cut Conservative from ~120 down to 38.
    - Grade-D names with beta < 1 were landing in Aggressive (e.g. MCD
      at beta 0.44, score 47.9). Grade alone should not trigger Aggressive
      for genuinely low-beta defensives.

    Updated rules below address both. Defensive sectors (Utilities, REITs,
    Staples, Healthcare, Energy) get an alternate Conservative path that
    does NOT require populated dividend data.
    """
    sym = (t.get("ticker") or "").upper()

    sector    = (t.get("sector") or "").strip()
    grade     = (t.get("grade") or "").upper()
    score     = _f(t.get("smart_score", t.get("pop_score", 0)), 0)
    beta      = _f(t.get("beta"))
    pm        = _f(t.get("profit_margin"))
    short_pct = _f(t.get("short_percent_float", t.get("short_interest")))
    div_yield = _f(t.get("dividend_yield"))   # fraction (0.025 = 2.5%)
    market_cap= _f(t.get("market_cap"), 0)

    in_spx = sym in SP500
    in_dji = sym in DOW_30
    in_any_major = in_spx or sym in NASDAQ_100 or in_dji

    has_beta = beta == beta  # NaN check
    has_pm   = pm   == pm
    has_short= short_pct == short_pct
    low_beta = has_beta and beta < 1.0
    is_defensive_sector = sector in _DEFENSIVE_SECTORS

    # ─── 1. AGGRESSIVE: real risk indicators ───
    if has_beta and beta >= 1.5:                    return ["aggressive"]
    if has_short and short_pct >= 10:               return ["aggressive"]
    if has_pm and pm < 0:                           return ["aggressive"]  # loss-making
    # Weak grade triggers Aggressive ONLY if beta is also elevated. A
    # low-beta utility / staple with grade D is just under-scored by
    # our tech-tilted model — not actually risky.
    if grade in ("D", "F") and not low_beta:        return ["aggressive"]
    if not in_any_major and market_cap < 10e9:      return ["aggressive"]

    # ─── 2. CONSERVATIVE: two parallel gates ───
    has_dividend  = div_yield == div_yield and div_yield >= 0.005
    large_cap_a   = market_cap >= 50e9 and grade == "A"

    # Strict tier — high-grade S&P/Dow blue chip with income / blue-chip proof
    cons_strict = (
        low_beta
        and grade in ("A", "B")
        and has_pm and pm > 0
        and (not has_short or short_pct < 5)
        and score >= 55
        and (in_spx or in_dji)
        and (has_dividend or in_dji or large_cap_a)
    )
    # Defensive-sector tier — utilities/staples/REITs/healthcare/energy with
    # low beta + profitability. Skips the dividend gate (live data has
    # dividend_yield null for the entire universe) and accepts C grade
    # since the tech-tilted model under-scores these names.
    cons_defensive = (
        has_beta and beta < 0.9
        and is_defensive_sector
        and grade in ("A", "B", "C")
        and has_pm and pm > 0
        and (not has_short or short_pct < 5)
        and (in_spx or in_dji)
    )
    if cons_strict or cons_defensive:
        return ["conservative"]

    # ─── 3. BALANCED: everything else ───
    return ["balanced"]
