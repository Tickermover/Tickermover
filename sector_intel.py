"""
sector_intel — one shared, deterministic view of the universe by sub-sector.

WHY THIS EXISTS
The /sectors and /compare pages were link indexes: a list of names and nothing
to read. Making them useful needs real comparative numbers, and those numbers
must be identical whether they are rendered into a public SEO page or a panel
inside the app. This codebase already carries two stock-page renderers and,
until recently, two "Key dependencies" cards — the second copy is where the
dead links and the stale figures came from. So there is exactly one place that
computes sector aggregates: here.

WHAT IT IS NOT
No AI, no provider calls, no I/O. Pure arithmetic over rows that are already in
memory, so it is cheap enough to call on any request and costs nothing per
visitor. The AI narrative is a separate, durably-cached layer on top; this
module is what grounds it, so the model is never asked to invent a number.

COMPLIANCE
Everything here is descriptive: counts, medians, spreads, breadth. It ranks
sub-sectors by measured characteristics and says what is true of a group today.
It deliberately produces no verdict, no ranking of "best to own", and nothing
forward-looking. Read the field names as a checklist of that intent.
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional


# ── small numeric helpers ────────────────────────────────────────────────
def _num(t: dict, *keys: str) -> Optional[float]:
    """First finite numeric value among `keys`, else None."""
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


def _median(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _pct(xs: list[float], p: float) -> Optional[float]:
    """Simple percentile (linear interpolation), p in 0..100."""
    if not xs:
        return None
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    idx = (len(s) - 1) * (p / 100.0)
    lo, hi = int(math.floor(idx)), int(math.ceil(idx))
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def _alpha(t: dict) -> Optional[float]:
    """The score we show as 'Alpha'. smart_score is the regime-adjusted one."""
    return _num(t, "smart_score", "pop_score")


def slugify(text: str) -> str:
    """Kept byte-compatible with seo_pages.slugify so existing URLs still resolve."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "other"


# ── grouping ─────────────────────────────────────────────────────────────
def _bucket_of(t: dict) -> Optional[str]:
    """The label a row is grouped under.

    Prefers the curated `sub_sector` theme tag, falls back to `industry`, then
    `sector`. Same precedence used across the app since 14 Aug: sub_sector is
    richer but present on only ~a third of the universe, while industry covers
    almost all of it, so preferring one without the other leaves most rows
    unlabelled.
    """
    for k in ("sub_sector", "subsector", "industry", "sector"):
        v = t.get(k)
        if v and str(v).strip():
            return str(v).strip()
    return None


def _group(universe: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for t in universe or []:
        if _alpha(t) is None:
            continue          # unscored rows would drag every median toward nothing
        label = _bucket_of(t)
        if not label:
            continue
        out.setdefault(label, []).append(t)
    return out


# ── the aggregate ────────────────────────────────────────────────────────
def summarise(label: str, rows: list[dict]) -> dict[str, Any]:
    """Descriptive stats for one sub-sector. Every value is measured, not modelled."""
    alphas = [a for a in (_alpha(t) for t in rows) if a is not None]
    grades = [str(t.get("grade") or "").upper() for t in rows]
    gdist = {g: grades.count(g) for g in ("A", "B", "C", "D", "F") if grades.count(g)}

    mom1 = [v for v in (_num(t, "momentum_1m") for t in rows) if v is not None]
    mom3 = [v for v in (_num(t, "momentum_3m") for t in rows) if v is not None]
    pes = [v for v in (_num(t, "pe_ratio", "forward_pe") for t in rows) if v and v > 0]
    growth = [v for v in (_num(t, "revenue_growth_yoy") for t in rows) if v is not None]
    margin = [v for v in (_num(t, "profit_margin") for t in rows) if v is not None]

    def _top(key_fn, reverse=True):
        cand = [(key_fn(t), t) for t in rows]
        cand = [(v, t) for v, t in cand if v is not None]
        if not cand:
            return None
        v, t = sorted(cand, key=lambda x: x[0], reverse=reverse)[0]
        return {"ticker": (t.get("ticker") or "").upper(),
                "name": t.get("name") or "", "value": round(v, 2)}

    leaders = sorted(
        [t for t in rows if _alpha(t) is not None],
        key=lambda t: _alpha(t) or 0, reverse=True,
    )[:3]

    n = len(rows)
    strong = sum(1 for a in alphas if a >= 65)
    return {
        "label": label,
        "slug": slugify(label),
        "count": n,
        # central tendency + spread: the spread is the point. A sub-sector whose
        # names all score alike is a different proposition from one with the
        # same median and a 40-point gap between its best and worst.
        "alpha_median": round(_median(alphas), 1) if alphas else None,
        "alpha_p25": round(_pct(alphas, 25), 1) if alphas else None,
        "alpha_p75": round(_pct(alphas, 75), 1) if alphas else None,
        "alpha_spread": (round(_pct(alphas, 75) - _pct(alphas, 25), 1) if len(alphas) > 1 else None),
        "breadth_strong_pct": round(strong / n * 100) if n else None,
        "grades": gdist,
        "momentum_1m_median": round(_median(mom1), 1) if mom1 else None,
        "momentum_3m_median": round(_median(mom3), 1) if mom3 else None,
        "pe_median": round(_median(pes), 1) if pes else None,
        "growth_median": round(_median(growth) * 100, 1) if growth else None,
        "margin_median": round(_median(margin) * 100, 1) if margin else None,
        "best_momentum": _top(lambda t: _num(t, "momentum_3m")),
        "worst_momentum": _top(lambda t: _num(t, "momentum_3m"), reverse=False),
        "leaders": [
            {"ticker": (t.get("ticker") or "").upper(),
             "name": t.get("name") or "",
             "alpha": round(_alpha(t) or 0),
             "grade": (t.get("grade") or "")}
            for t in leaders
        ],
    }


def all_sectors(universe: list[dict], min_count: int = 3) -> list[dict]:
    """Every sub-sector with at least `min_count` scored names, richest first.

    `min_count` exists because a "sub-sector" of one name has a median, a
    spread of zero and a 100% breadth reading, all of which are arithmetically
    true and completely meaningless. Publishing them would put noise at the top
    of a table people are meant to read down.
    """
    groups = _group(universe)
    out = [summarise(label, rows) for label, rows in groups.items() if len(rows) >= min_count]
    out.sort(key=lambda s: (-(s["count"] or 0), -(s["alpha_median"] or 0)))
    return out


def one_sector(slug: str, universe: list[dict]) -> Optional[dict]:
    """Full detail for a single sub-sector, including its member rows."""
    for label, rows in _group(universe).items():
        if slugify(label) == slug:
            s = summarise(label, rows)
            s["rows"] = sorted(rows, key=lambda t: _alpha(t) or 0, reverse=True)
            return s
    return None


def universe_baseline(universe: list[dict]) -> dict[str, Any]:
    """The whole scored universe as one pseudo-sector.

    Every sector number on the page is read against this. A median Alpha of 58
    means nothing until you know the universe sits at 54 — without the
    baseline a reader has to guess whether a figure is good, and most will
    guess generously.
    """
    rows = [t for t in (universe or []) if _alpha(t) is not None]
    s = summarise("All scored stocks", rows)
    s["slug"] = "_all"
    return s
