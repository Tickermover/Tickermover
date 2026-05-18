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
    """Return the list of profiles this ticker is eligible for."""
    out: list[str] = ["aggressive"]  # everyone qualifies for aggressive
    sym = (t.get("ticker") or "").upper()

    grade = (t.get("grade") or "").upper()
    score = _f(t.get("smart_score", t.get("pop_score", 0)), 0)
    beta = _f(t.get("beta"))
    pm = _f(t.get("profit_margin"))
    short_pct = _f(t.get("short_percent_float", t.get("short_interest")))
    div_yield = _f(t.get("dividend_yield"))  # already as fraction (e.g. 0.025 = 2.5%)
    market_cap = _f(t.get("market_cap"), 0)

    in_spx = sym in SP500
    in_ndx = sym in NASDAQ_100
    in_dji = sym in DOW_30
    in_any_major = in_spx or in_ndx or in_dji

    # ── Balanced ──
    balanced_ok = True
    if grade in ("D", "F"):                       balanced_ok = False
    if not (beta != beta) and beta >= 2.0:        balanced_ok = False  # beta!=beta -> NaN
    if score and score < 55:                      balanced_ok = False
    if not in_any_major and market_cap < 10e9:    balanced_ok = False  # liquidity floor
    if balanced_ok:
        out.append("balanced")

    # ── Conservative ──
    conservative_ok = True
    if grade not in ("A", "B"):                   conservative_ok = False
    if beta == beta and beta >= 1.3:              conservative_ok = False
    if pm == pm and pm < 0:                       conservative_ok = False  # must be profitable
    if short_pct == short_pct and short_pct >= 5: conservative_ok = False
    if score and score < 60:                      conservative_ok = False
    if not in_spx and not in_dji:                 conservative_ok = False  # S&P/Dow members only
    # Dividend OR Dow 30 (blue-chip exemption)
    has_dividend = div_yield == div_yield and div_yield >= 0.005  # >=0.5%
    if not has_dividend and not in_dji:           conservative_ok = False
    if conservative_ok:
        out.append("conservative")

    return out
