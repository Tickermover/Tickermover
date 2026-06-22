"""
TickerMover - AI Alpha Score Engine
Curated-universe aware: uses growth_tier metadata as a prior when live
financial data is unavailable, so scores reflect quality from day one.
Neutral baseline = 0.60 (optimistic for hand-picked high-growth stocks).

v3 (2026-04): Adds caution flag + bottom_line for conflict-aware UX.
v2 (2026-04): Integrates intelligence.py - adds 5 new components
(trend_strength, breakout_proximity, news_sentiment, earnings_acceleration,
score_momentum) and a regime-multiplier "smart_score" output field.
"""
from __future__ import annotations
import logging
import os
from typing import Any, Optional

# Kill switch for Phase 3b sector-aware scoring. Set SECTOR_AWARE_SCORING=0
# on Railway to revert every ticker to the tech-default WEIGHTS instantly,
# without redeploying code.
_SECTOR_AWARE = os.getenv("SECTOR_AWARE_SCORING", "1") != "0"

try:
    from intelligence import (
        score_trend_strength       as _intel_trend_strength,
        score_breakout_proximity   as _intel_breakout_proximity,
        score_news_sentiment       as _intel_news_sentiment,
        score_earnings_acceleration as _intel_earnings_acceleration,
    )
    _INTEL_AVAILABLE = True
except Exception:
    _INTEL_AVAILABLE = False
    def _intel_trend_strength(t):       return 0.60
    def _intel_breakout_proximity(t):   return 0.55
    def _intel_news_sentiment(t):       return 0.55
    def _intel_earnings_acceleration(t):return 0.55


logger = logging.getLogger(__name__)

WEIGHTS: dict[str, int] = {
    "growth_tier":           11, "momentum_1m":           10,
    "rel_strength":           7, "volume_spike":           6,
    "rsi_zone":               7, "dist_52w_high":          5,
    "analyst_cons":           6, "earnings_prox":          3,
    "low_short":              2, "mkt_cap_fit":            3,
    "fundamentals":           5, "social_momentum":        4,
    "insider_bias":           3, "earnings_quality":       5,
    "trend_strength":         7, "breakout_proximity":     6,
    "news_sentiment":         4, "earnings_acceleration":  4,
    "score_momentum":         2,
}
assert sum(WEIGHTS.values()) == 100

# ═══════════════════════════════════════════════════════════════════
# Phase 3b — SECTOR-AWARE SCORING
# Per-GICS-sector weight overrides applied on top of the tech-tilted
# default WEIGHTS above. Each delta dict MUST sum to zero so total
# weights stay at 100. Tech / Healthcare / Communication / Industrials
# / Consumer Cyclical use the default (no entry below).
#
# Tilts:
# - Utilities / REITs / Consumer Defensive  -> income (dividend, FCF,
#                                              balance sheet) over growth
# - Financial Services                       -> analyst consensus +
#                                              fundamentals (book value /
#                                              NIM proxies) over momentum
# - Energy / Basic Materials                 -> cyclical: FCF + analyst
#                                              consensus over growth tier
# ═══════════════════════════════════════════════════════════════════
SECTOR_WEIGHT_DELTAS: dict[str, dict[str, int]] = {
    "Utilities": {
        "growth_tier":     -7,   # 11 -> 4   (low-growth by design)
        "momentum_1m":     -5,   # 10 -> 5   (low-vol names)
        "volume_spike":    -3,   # 6  -> 3
        "rsi_zone":        -3,   # 7  -> 4
        "social_momentum": -3,   # 4  -> 1   (utilities don't trend on socials)
        "fundamentals":    +8,   # 5  -> 13  (cash flow + leverage matter)
        "mkt_cap_fit":     +3,   # 3  -> 6
        "low_short":       +2,   # 2  -> 4
        "analyst_cons":    +2,   # 6  -> 8
        "insider_bias":    +3,   # 3  -> 6
        "earnings_quality":+3,   # 5  -> 8   (stable, dividend-supporting earnings)
    },
    "Real Estate": {
        "growth_tier":     -6,   # 11 -> 5
        "momentum_1m":     -4,   # 10 -> 6
        "volume_spike":    -3,
        "rsi_zone":        -2,
        "social_momentum": -2,
        "fundamentals":    +7,   # FFO/balance sheet matter most
        "mkt_cap_fit":     +2,
        "low_short":       +2,
        "analyst_cons":    +2,
        "insider_bias":    +2,
        "earnings_quality":+2,
    },
    "Consumer Defensive": {
        "growth_tier":     -5,
        "momentum_1m":     -3,
        "volume_spike":    -2,
        "social_momentum": -2,
        "fundamentals":    +5,
        "earnings_quality":+3,
        "analyst_cons":    +2,
        "low_short":       +1,
        "mkt_cap_fit":     +1,
    },
    "Financial Services": {
        "growth_tier":     -5,
        "momentum_1m":     -2,
        "volume_spike":    -2,
        "social_momentum": -2,
        "trend_strength":  -2,
        "fundamentals":    +5,   # book value / NIM proxies
        "analyst_cons":    +4,   # consensus matters for cyclical earnings
        "earnings_quality":+2,
        "insider_bias":    +2,
    },
    "Energy": {
        "growth_tier":     -6,   # cyclical, growth isn't structural
        "earnings_acceleration": -2,
        "social_momentum": -1,
        "fundamentals":    +5,   # FCF / breakeven matters
        "analyst_cons":    +2,
        "earnings_quality":+1,
        "low_short":       +1,
    },
    "Basic Materials": {
        "growth_tier":     -5,
        "earnings_acceleration": -2,
        "social_momentum": -1,
        "fundamentals":    +5,
        "analyst_cons":    +1,
        "earnings_quality":+1,
        "low_short":       +1,
    },
}
# Verify every delta sums to zero — guards against silent weight bugs.
for _sec, _delta in SECTOR_WEIGHT_DELTAS.items():
    assert sum(_delta.values()) == 0, f"Sector {_sec} weight deltas don't sum to zero: {_delta}"


def _resolve_weights(ticker: dict) -> dict[str, int]:
    """Return effective scoring weights for this ticker, applying sector
    override if one exists. Falls back to base WEIGHTS for tech / default
    or when SECTOR_AWARE_SCORING env kill-switch is off."""
    if not _SECTOR_AWARE:
        return WEIGHTS
    sector = (ticker.get("sector") or "").strip()
    delta = SECTOR_WEIGHT_DELTAS.get(sector)
    if not delta:
        return WEIGHTS
    out = dict(WEIGHTS)
    for k, dv in delta.items():
        out[k] = out.get(k, 0) + dv
    # Defensive clamp — no negative weights
    return {k: max(0, v) for k, v in out.items()}

_NEUTRAL = 0.60


def _clamp(val: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, val))


def _safe(d: dict, key: str, default=None):
    v = d.get(key)
    return default if v is None else v


def _growth_tier_prior(gtier: str) -> float:
    if not gtier:               return _NEUTRAL
    if "Very High" in gtier:    return 0.90
    if "High"     in gtier:     return 0.75
    if "Moderate" in gtier:     return 0.60
    if "Stable"   in gtier:     return 0.40
    return _NEUTRAL


def _score_growth_tier(t: dict) -> float:
    rev_g = _safe(t, "revenue_growth_yoy")
    eps_g = _safe(t, "eps_growth_yoy")
    if rev_g is None and eps_g is None:
        return _growth_tier_prior(_safe(t, "growth_tier", ""))
    scores = []
    if rev_g is not None:
        if   rev_g >= 0.50: scores.append(1.00)
        elif rev_g >= 0.30: scores.append(0.90)
        elif rev_g >= 0.15: scores.append(0.75)
        elif rev_g >= 0.05: scores.append(0.55)
        elif rev_g >= 0.00: scores.append(0.40)
        else:                scores.append(0.15)
    if eps_g is not None:
        if   eps_g >= 0.60: scores.append(1.00)
        elif eps_g >= 0.30: scores.append(0.85)
        elif eps_g >= 0.10: scores.append(0.65)
        elif eps_g >= 0.00: scores.append(0.45)
        else:                scores.append(0.20)
    return sum(scores) / len(scores)


def _score_momentum_1m(t: dict) -> float:
    mom = _safe(t, "momentum_1m")
    if mom is None: return _NEUTRAL
    if   mom >= 25:  return 1.00
    elif mom >= 15:  return 0.90
    elif mom >= 8:   return 0.75
    elif mom >= 3:   return 0.62
    elif mom >= 0:   return 0.50
    elif mom >= -5:  return 0.30
    else:            return 0.15


def _score_rel_strength(t: dict) -> float:
    dist = _safe(t, "dist_52w_high")
    if dist is None: return _NEUTRAL
    if   dist >= -5:   return 1.00
    elif dist >= -10:  return 0.85
    elif dist >= -20:  return 0.65
    elif dist >= -35:  return 0.45
    elif dist >= -50:  return 0.25
    else:              return 0.10


def _score_volume_spike(t: dict) -> float:
    vr = _safe(t, "volume_ratio")
    if vr is None: return _NEUTRAL
    if   vr >= 3.0:  return 1.00
    elif vr >= 2.0:  return 0.88
    elif vr >= 1.5:  return 0.75
    elif vr >= 1.2:  return 0.62
    elif vr >= 0.9:  return 0.50
    else:            return 0.25


def _score_rsi_zone(t: dict) -> float:
    rsi = _safe(t, "rsi_14")
    if rsi is None: return _NEUTRAL
    if   50 <= rsi <= 65: return 1.00
    elif 65 <  rsi <= 70: return 0.85
    elif 45 <= rsi <  50: return 0.70
    elif 70 <  rsi <= 80: return 0.55
    elif 35 <= rsi <  45: return 0.35
    elif rsi > 80:        return 0.30
    else:                 return 0.20


def _score_dist_52w_high(t: dict) -> float:
    target_up = _safe(t, "target_upside_pct")
    if target_up is None:
        rs = _score_rel_strength(t)
        return _clamp((rs + _NEUTRAL) / 2)
    if   target_up >= 40: return 1.00
    elif target_up >= 25: return 0.88
    elif target_up >= 15: return 0.75
    elif target_up >= 8:  return 0.60
    elif target_up >= 0:  return 0.45
    else:                 return 0.25


def _score_analyst_consensus(t: dict) -> float:
    sb_pct = _safe(t, "strong_buy_pct")
    if sb_pct is None: return _NEUTRAL
    if   sb_pct >= 60: return 1.00
    elif sb_pct >= 45: return 0.88
    elif sb_pct >= 30: return 0.70
    elif sb_pct >= 15: return 0.50
    else:              return 0.30


def _score_earnings_proximity(t: dict) -> float:
    days = _safe(t, "days_to_earnings")
    if days is None: return _NEUTRAL
    if   days < 0:    return 0.35
    elif days <= 7:   return 1.00
    elif days <= 14:  return 0.92
    elif days <= 30:  return 0.78
    elif days <= 60:  return 0.60
    else:             return 0.42


def _score_low_short(t: dict) -> float:
    si = _safe(t, "short_percent_float")
    if si is None: return _NEUTRAL
    if   si <= 2:   return 1.00
    elif si <= 5:   return 0.82
    elif si <= 10:  return 0.63
    elif si <= 20:  return 0.42
    else:           return 0.22


def _score_mkt_cap_fit(t: dict) -> float:
    mc = _safe(t, "market_cap")
    if mc is None:
        tier = _safe(t, "market_cap_tier", "")
        tier_map = {"Micro Cap":0.95,"Small Cap":1.00,"Mid Cap":0.92,"Large Cap":0.72,"Mega Cap":0.35}
        for k, v in tier_map.items():
            if k in tier: return v
        return _NEUTRAL
    B = 1e9
    tier = _safe(t, "market_cap_tier", "")
    if mc < 0.5*B and tier in ("Small Cap", "Micro Cap"): return 0.70
    if   mc <  0.25*B: return 0.05
    elif mc <  0.5*B:  return 0.10
    elif mc <  1*B:    return 0.70
    elif mc <= 5*B:    return 1.00
    elif mc <= 20*B:   return 0.88
    elif mc <= 100*B:  return 0.70
    elif mc <= 200*B:  return 0.55
    else:              return 0.35


def _score_fundamentals(t: dict) -> float:
    pe     = _safe(t, "pe_ratio")
    peg    = _safe(t, "peg_ratio")
    margin = _safe(t, "gross_margin")
    scores: list[float] = []
    if pe is not None:
        if   pe <= 0:    scores.append(0.25)
        elif pe <= 15:   scores.append(1.00)
        elif pe <= 25:   scores.append(0.85)
        elif pe <= 40:   scores.append(0.65)
        elif pe <= 60:   scores.append(0.45)
        else:            scores.append(0.25)
    if peg is not None and peg > 0:
        if   peg <= 0.5:  scores.append(1.00)
        elif peg <= 1.0:  scores.append(0.88)
        elif peg <= 1.5:  scores.append(0.68)
        elif peg <= 2.5:  scores.append(0.48)
        else:             scores.append(0.28)
    if margin is not None:
        if   margin >= 0.60: scores.append(1.00)
        elif margin >= 0.40: scores.append(0.82)
        elif margin >= 0.25: scores.append(0.63)
        elif margin >= 0.10: scores.append(0.43)
        else:                scores.append(0.22)
    return sum(scores) / len(scores) if scores else _NEUTRAL


def _score_social_momentum(t: dict) -> float:
    vel = _safe(t, "mention_velocity")
    if vel is None: return _NEUTRAL - 0.05
    if   vel >= 200:  return 1.00
    elif vel >= 100:  return 0.92
    elif vel >= 50:   return 0.78
    elif vel >= 20:   return 0.64
    elif vel >= 0:    return 0.52
    elif vel >= -20:  return 0.35
    else:             return 0.20


def _score_earnings_quality(t: dict) -> float:
    streak  = _safe(t, "eps_beat_streak")
    avg_sur = _safe(t, "avg_eps_surprise_pct")
    last_beat = _safe(t, "eps_last_beat")
    if streak is None and avg_sur is None: return _NEUTRAL
    scores: list[float] = []
    if streak is not None:
        if   streak == 4:  scores.append(1.00)
        elif streak == 3:  scores.append(0.85)
        elif streak == 2:  scores.append(0.65)
        elif streak == 1:  scores.append(0.45)
        else:              scores.append(0.20)
    if avg_sur is not None:
        if   avg_sur >= 20:  scores.append(1.00)
        elif avg_sur >= 10:  scores.append(0.88)
        elif avg_sur >= 5:   scores.append(0.72)
        elif avg_sur >= 0:   scores.append(0.55)
        elif avg_sur >= -5:  scores.append(0.35)
        else:                scores.append(0.15)
    if last_beat is True:    scores.append(0.80)
    elif last_beat is False: scores.append(0.30)
    return sum(scores) / len(scores) if scores else _NEUTRAL


def _score_insider_bias(t: dict) -> float:
    buys  = _safe(t, "insider_buys_90d",  0)
    sells = _safe(t, "insider_sells_90d", 0)
    total = buys + sells
    if total == 0: return _NEUTRAL
    buy_ratio = buys / total
    if   buy_ratio >= 0.90: return 1.00
    elif buy_ratio >= 0.70: return 0.85
    elif buy_ratio >= 0.50: return 0.65
    elif buy_ratio >= 0.30: return 0.45
    elif buy_ratio >= 0.10: return 0.28
    else:                   return 0.12


def _score_trend_strength(t):       return _intel_trend_strength(t)
def _score_breakout_proximity(t):   return _intel_breakout_proximity(t)
def _score_news_sentiment(t):       return _intel_news_sentiment(t)
def _score_earnings_acceleration(t):return _intel_earnings_acceleration(t)


def _score_momentum_history(t: dict) -> float:
    sm = _safe(t, "score_momentum")
    if sm is None: return _NEUTRAL
    try:    return _clamp(float(sm))
    except (TypeError, ValueError): return _NEUTRAL


def compute_pop_score(
    ticker_data: dict,
    regime: Optional[dict] = None,
) -> dict[str, Any]:
    t = ticker_data
    # M2: a NaN/inf numeric field (e.g. a bad upstream quote) makes every pillar
    # comparison silently False, routing the stock to a WRONG bucket rather than
    # the neutral fallback. Coerce NaN/inf -> None up front so the pillars' existing
    # missing-data handling (-> neutral) kicks in instead.
    import math as _math
    for _k, _v in list(t.items()):
        if isinstance(_v, float) and (_math.isnan(_v) or _math.isinf(_v)):
            t[_k] = None
    component_fns: dict[str, callable] = {
        "growth_tier":            _score_growth_tier,
        "momentum_1m":            _score_momentum_1m,
        "rel_strength":           _score_rel_strength,
        "volume_spike":           _score_volume_spike,
        "rsi_zone":               _score_rsi_zone,
        "dist_52w_high":          _score_dist_52w_high,
        "analyst_cons":           _score_analyst_consensus,
        "earnings_prox":          _score_earnings_proximity,
        "low_short":              _score_low_short,
        "mkt_cap_fit":            _score_mkt_cap_fit,
        "fundamentals":           _score_fundamentals,
        "social_momentum":        _score_social_momentum,
        "insider_bias":           _score_insider_bias,
        "earnings_quality":       _score_earnings_quality,
        "trend_strength":         _score_trend_strength,
        "breakout_proximity":     _score_breakout_proximity,
        "news_sentiment":         _score_news_sentiment,
        "earnings_acceleration":  _score_earnings_acceleration,
        "score_momentum":         _score_momentum_history,
    }

    # Phase 3b: resolve per-sector weights. Tech / Healthcare / etc. use
    # the default WEIGHTS unchanged; Utilities / REITs / Financials /
    # Energy / etc. get an income- or cyclical-tilted override.
    eff_weights = _resolve_weights(t)
    scoring_profile = "default"
    sector = (t.get("sector") or "").strip()
    if _SECTOR_AWARE and sector in SECTOR_WEIGHT_DELTAS:
        scoring_profile = f"sector:{sector}"

    breakdown: dict[str, float] = {}
    weighted:  dict[str, float] = {}
    total_score  = 0.0
    data_present = 0
    for comp, fn in component_fns.items():
        raw = _clamp(fn(t))
        w   = eff_weights.get(comp, WEIGHTS[comp])
        contribution = raw * w
        breakdown[comp] = round(raw, 4)
        weighted[comp]  = round(contribution, 4)
        total_score    += contribution
        if abs(raw - _NEUTRAL) > 0.07:
            data_present += 1

    confidence = round(data_present / len(component_fns), 3)
    pop_score  = round(total_score, 2)

    regime_mult  = 1.0
    regime_label = "Mixed"
    if regime:
        try:
            regime_mult  = float(regime.get("regime_multiplier") or 1.0)
            regime_label = regime.get("regime_label", "Mixed")
        except (TypeError, ValueError):
            regime_mult, regime_label = 1.0, "Mixed"
    smart_score = round(pop_score * regime_mult, 2)

    grading_score = smart_score
    if   grading_score >= 68: grade = "A"
    elif grading_score >= 60: grade = "B"
    elif grading_score >= 50: grade = "C"
    elif grading_score >= 40: grade = "D"
    else:                     grade = "F"

    signals: list[str] = []
    _add_signals(t, breakdown, signals)

    # Conflict detection -> caution flag + plain-English bottom_line.
    # We only fire on REAL conflicts (analyst disagrees, macro hostile, badly
    # overbought) plus genuinely-sparse data. Healthy 60-70% confidence on a
    # high-momentum stock is NOT a conflict.
    caution_reasons: list[tuple[str, str]] = []   # (kind, message)
    analyst_upside = _safe(t, "target_upside_pct")
    if analyst_upside is not None and analyst_upside < -2 and grade in ("A", "B"):
        # Only flag when the gap is meaningful (>2%), not rounding noise
        caution_reasons.append((
            "analyst",
            f"trades {abs(analyst_upside):.1f}% above analyst mean target",
        ))
    if confidence < 0.40 and grade in ("A", "B"):
        # Only fire when data is REALLY sparse — not on healthy 60-70% coverage
        caution_reasons.append((
            "data",
            f"only {int(confidence*100)}% of fundamentals are loaded",
        ))
    if regime_label == "Bearish" and grade in ("A", "B"):
        caution_reasons.append(("regime", "market regime is Bearish"))
    rsi_val = _safe(t, "rsi_14")
    if rsi_val is not None and rsi_val > 78 and grade in ("A", "B"):
        # Tightened from 75 → 78 — RSI 75-78 is hot but not screaming bubble
        caution_reasons.append(("rsi", f"RSI {rsi_val:.0f} is overbought"))

    caution_msgs = [m for _, m in caution_reasons]

    caution = bool(caution_reasons)
    bottom_line = _build_bottom_line(t, grade, smart_score, regime_label, caution_reasons)

    return {
        "pop_score":         pop_score,
        "smart_score":       smart_score,
        "regime_multiplier": round(regime_mult, 3),
        "regime_label":      regime_label,
        "confidence":        confidence,
        "grade":             grade,
        "caution":           caution,
        "caution_reasons":   caution_msgs,
        "bottom_line":       bottom_line,
        "breakdown":         breakdown,
        "weighted":          weighted,
        "signals":           signals,
        "scoring_profile":   scoring_profile,
    }


def _caution_observation(kind: str, t: dict) -> str:
    """Observational note about a caution flag.  Describes the data condition,
    NOT what the user should do.  TickerMover is not an FCA-authorised adviser;
    it surfaces signals, the user decides what (if anything) to do."""
    if kind == "analyst":
        mean = _safe(t, "target_mean") or _safe(t, "target_price")
        if mean and mean > 0:
            return f"Stock is trading above the analyst mean target (${mean:.0f}) — limited consensus headroom."
        return "Stock is trading above the analyst mean target — limited consensus headroom."
    if kind == "data":
        return "Some fundamental fields are missing — score has lower data confidence."
    if kind == "regime":
        return "Macro regime is not currently supportive of growth equities."
    if kind == "rsi":
        return "RSI is in stretched territory — momentum is extended."
    return "One or more thesis components are unconfirmed."

# Backward-compat shim: existing call-sites use the old name. Keep the old
# function pointing to the new neutral implementation.
_action_for_caution = _caution_observation


def _build_bottom_line(t, grade, score, regime_label, caution_reasons):
    """
    One short paragraph: drivers (specific numbers) + caveat (if any) + action
    (concrete, with price levels where possible). The verdict lives in the
    UI heading; this prose explains *why* and *what to do*.
    """
    # Collect the three strongest drivers with specific numbers
    drivers = []
    rev_g = _safe(t, "revenue_growth_yoy")
    if rev_g is not None and rev_g >= 0.20:
        drivers.append(f"{rev_g*100:.0f}% revenue growth")
    mom = _safe(t, "momentum_1m")
    if mom is not None and mom >= 10:
        drivers.append(f"{mom:+.0f}% 30-day momentum")
    streak = _safe(t, "eps_beat_streak")
    if streak is not None and streak >= 3:
        drivers.append(f"{streak}/4 EPS beats")
    sb_pct = _safe(t, "strong_buy_pct")
    if sb_pct is not None and sb_pct >= 60:
        drivers.append(f"{int(sb_pct)}% analyst strong-buy")
    margin = _safe(t, "profit_margin")
    if margin is not None and margin >= 0.20:
        drivers.append(f"{margin*100:.0f}% net margin")

    drivers = drivers[:3]  # readability cap

    # Build the lead sentence — quality + specific drivers
    if grade == "A" and drivers:
        lead = "Top-tier setup — " + ", ".join(drivers) + "."
    elif grade == "B" and drivers:
        lead = "Solid setup — " + ", ".join(drivers) + "."
    elif grade in ("A", "B"):
        lead = "Setup looks favourable on the available data."
    elif grade == "C":
        lead = "Mixed signals — neither side has the edge here."
    elif grade == "D":
        lead = "Risk is rising; momentum is fading or fundamentals are weakening."
    elif grade == "F":
        lead = "The setup is unfavourable — multiple signals are negative."
    else:
        lead = ""

    # No conflicts → neutral observation by grade
    if not caution_reasons:
        if grade in ("A", "B"):
            return lead + " Momentum, fundamentals and macro indicators are aligned."
        if grade == "C":
            return lead + " The thesis lacks a clear edge from current data."
        if grade == "D":
            return lead + " Multiple risk indicators are firing."
        if grade == "F":
            return lead + " Setup looks unfavourable across the score components."
        return lead

    # With conflicts → caveat + tactical instruction
    primary_kind, primary_msg = caution_reasons[0]
    extra = ""
    if len(caution_reasons) > 1:
        n = len(caution_reasons) - 1
        extra = f" Also {n} other caveat{'s' if n > 1 else ''} flagged."

    action = _action_for_caution(primary_kind, t)
    return f"{lead} Caveat: {primary_msg}.{extra} {action}"


def _add_signals(t: dict, breakdown: dict, signals: list) -> None:
    vel = _safe(t, "mention_velocity")
    if vel is not None and vel >= 50:
        signals.append(f"Social mentions +{vel:.0f}% vs 24h ago")
    days = _safe(t, "days_to_earnings")
    if days is not None and 0 <= days <= 14:
        signals.append(f"Earnings in {days}d")
    buys  = _safe(t, "insider_buys_90d",  0)
    sells = _safe(t, "insider_sells_90d", 0)
    if buys > 0 and sells == 0:
        signals.append(f"Pure insider buying ({buys} txns, 90d)")
    elif buys > sells * 2:
        signals.append(f"Insider buys outpace sells ({buys}:{sells})")
    sb = _safe(t, "strong_buy_pct")
    if sb is not None and sb >= 60:
        signals.append(f"{sb:.0f}% analyst strong-buy consensus")
    rsi = _safe(t, "rsi_14")
    if rsi is not None:
        if 50 <= rsi <= 65: signals.append(f"RSI {rsi:.1f} - ideal momentum zone")
        elif rsi > 75:      signals.append(f"RSI {rsi:.1f} - overbought")
    vr = _safe(t, "volume_ratio")
    if vr is not None and vr >= 2.0:
        signals.append(f"Volume {vr:.1f}x 20-day avg")
    mom = _safe(t, "momentum_1m")
    if mom is not None and mom >= 15:
        signals.append(f"+{mom:.1f}% price momentum (30d)")
    dist = _safe(t, "dist_52w_high")
    if dist is not None and dist >= -5:
        signals.append(f"Within {abs(dist):.1f}% of 52-week high")
    tup = _safe(t, "target_upside_pct")
    if tup is not None and tup >= 25:
        signals.append(f"Analyst target +{tup:.0f}% upside")
    streak = _safe(t, "eps_beat_streak")
    if streak is not None and streak >= 3:
        signals.append(f"EPS beat {streak}/4 last quarters")
    avg_sur = _safe(t, "avg_eps_surprise_pct")
    if avg_sur is not None and avg_sur >= 10:
        signals.append(f"Avg EPS surprise +{avg_sur:.1f}% (4Q)")
    gmt = _safe(t, "gross_margin_trend", "")
    if gmt == "expanding":   signals.append("Gross margin expanding")
    elif gmt == "contracting": signals.append("Gross margin contracting")
    gtier = _safe(t, "growth_tier", "")
    if "Very High" in str(gtier): signals.append("Very High growth tier (>25% CAGR)")
    elif "High" in str(gtier):    signals.append("High growth tier (15-25% CAGR)")
    ts = breakdown.get("trend_strength")
    if ts is not None and ts >= 0.85:   signals.append("Multi-timeframe uptrend")
    elif ts is not None and ts <= 0.20: signals.append("Multi-timeframe downtrend")
    bp = breakdown.get("breakout_proximity")
    if bp is not None and bp >= 0.85:   signals.append("Breakout setup - near highs on volume")
    ns = breakdown.get("news_sentiment")
    if ns is not None and ns >= 0.80:   signals.append("News flow leans bullish")
    elif ns is not None and ns <= 0.30: signals.append("News flow leans bearish")
    ea = breakdown.get("earnings_acceleration")
    if ea is not None and ea >= 0.85:   signals.append("Revenue growth-rate accelerating QoQ")
    sv = _safe(t, "score_velocity")
    if sv is not None and sv >= 3:    signals.append(f"Alpha Score climbing +{sv:.1f} pts in 24h")
    elif sv is not None and sv <= -3: signals.append(f"Alpha Score falling {sv:.1f} pts in 24h")


def score_and_rank(universe: list[dict], regime: Optional[dict] = None) -> list[dict]:
    for t in universe:
        try:
            result = compute_pop_score(t, regime=regime)
            t.update(result)
        except Exception as exc:
            logger.error(f"Scoring error for {t.get('ticker','?')}: {exc}")
            t.update({
                "pop_score":0.0,"smart_score":0.0,"regime_multiplier":1.0,
                "regime_label":"Mixed","confidence":0.0,"grade":"F",
                "caution":False,"caution_reasons":[],"bottom_line":"No data.",
                "signals":[],"breakdown":{},"weighted":{},
            })
    universe.sort(key=lambda x: x.get("smart_score", x.get("pop_score", 0)), reverse=True)
    return universe


def _fmt_dollar(val):
    if val is None: return None
    abs_val = abs(val); sign = "-" if val < 0 else ""
    if   abs_val >= 1e12: return f"{sign}${abs_val/1e12:.2f}T"
    elif abs_val >= 1e9:  return f"{sign}${abs_val/1e9:.1f}B"
    elif abs_val >= 1e6:  return f"{sign}${abs_val/1e6:.0f}M"
    return f"{sign}${abs_val:,.0f}"


def derive_recent_wins(t: dict) -> list[str]:
    wins: list[str] = []
    rev = _safe(t, "revenue_ttm")
    if rev: wins.append(f"Revenue: {_fmt_dollar(rev)} TTM")
    fcf = _safe(t, "free_cashflow")
    if fcf:
        label = "FCF" if fcf >= 0 else "FCF (negative)"
        wins.append(f"{label}: {_fmt_dollar(fcf)}")
    op_cf = _safe(t, "operating_cashflow")
    if op_cf: wins.append(f"Op. CF: {_fmt_dollar(op_cf)}")
    eps = _safe(t, "eps_last_actual")
    if eps is not None: wins.append(f"EPS (last Q): ${eps:.2f}")
    rev_g = _safe(t, "revenue_growth_yoy")
    if rev_g is not None: wins.append(f"Rev growth YoY: {rev_g*100:.1f}%")
    mom = _safe(t, "momentum_1m")
    if mom is not None: wins.append(f"30d price: {mom:+.1f}%")
    return wins
