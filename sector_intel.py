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


# ── head-to-head ─────────────────────────────────────────────────────────
# The dimensions a comparison page reports on. `higher_is` records which
# direction is simply *more* of the thing — it is NOT a judgement about which
# company is preferable. A higher P/E is a higher P/E; whether that is good
# depends on what you think the growth is worth, which is the reader's call and
# not ours to make. `note` is shown next to the row so the direction is never
# silently read as a verdict.
COMPARE_FIELDS: list[dict] = [
    {"key": "alpha",       "label": "Alpha Score",        "keys": ("smart_score", "pop_score"),
     "fmt": "int",  "higher_is": "more", "note": "our composite quality read"},
    {"key": "revenue_growth_yoy", "label": "Revenue growth (YoY)", "keys": ("revenue_growth_yoy",),
     "fmt": "pct100", "higher_is": "more", "note": "faster top-line growth"},
    {"key": "profit_margin",     "label": "Net margin",   "keys": ("profit_margin",),
     "fmt": "pct100", "higher_is": "more", "note": "more of each sale kept"},
    {"key": "gross_margin",      "label": "Gross margin", "keys": ("gross_margin",),
     "fmt": "pct100", "higher_is": "more", "note": "pricing power"},
    {"key": "pe_ratio",   "label": "P/E",                 "keys": ("pe_ratio", "forward_pe"),
     "fmt": "x",    "higher_is": "more", "note": "a richer multiple, not automatically worse"},
    {"key": "momentum_1m", "label": "1-month move",       "keys": ("momentum_1m",),
     "fmt": "pct",  "higher_is": "more", "note": "recent price direction"},
    {"key": "momentum_3m", "label": "3-month move",       "keys": ("momentum_3m",),
     "fmt": "pct",  "higher_is": "more", "note": "medium-term price direction"},
    {"key": "beta",       "label": "Beta",                "keys": ("beta",),
     "fmt": "f2",   "higher_is": "more", "note": "moves more than the market"},
    {"key": "target_upside_pct", "label": "Analyst upside", "keys": ("target_upside_pct",),
     "fmt": "pct",  "higher_is": "more", "note": "third-party consensus, not our view"},
    {"key": "market_cap", "label": "Market cap",          "keys": ("market_cap",),
     "fmt": "money", "higher_is": "more", "note": "company size"},
]


def _fmt(v: Optional[float], fmt: str) -> str:
    if v is None:
        return "—"
    if fmt == "int":
        return str(round(v))
    if fmt == "pct100":
        return f"{v * 100:+.1f}%"
    if fmt == "pct":
        return f"{v:+.1f}%"
    if fmt == "x":
        return f"{v:.1f}×" if v > 0 else "—"
    if fmt == "f2":
        return f"{v:.2f}"
    if fmt == "money":
        a = abs(v)
        if a >= 1e12:
            return f"${v/1e12:.2f}T"
        if a >= 1e9:
            return f"${v/1e9:.1f}B"
        if a >= 1e6:
            return f"${v/1e6:.0f}M"
        return f"${v:,.0f}"
    return str(v)


def compare(a: str, b: str, universe: list[dict]) -> Optional[dict]:
    """Field-by-field comparison of two tickers.

    Reports WHERE the two differ and by how much. It deliberately does not
    total the rows up, score the result, or name a winner: a count of "wins"
    across unweighted, correlated metrics is a meaningless number that reads
    exactly like a recommendation, which is the one thing this page must not
    produce. The reader is given the differences and decides what matters.
    """
    a, b = (a or "").upper(), (b or "").upper()
    if not a or not b or a == b:
        return None
    look = {(t.get("ticker") or "").upper(): t for t in (universe or [])}
    ta, tb = look.get(a), look.get(b)
    if not ta or not tb:
        return None

    rows = []
    for f in COMPARE_FIELDS:
        va, vb = _num(ta, *f["keys"]), _num(tb, *f["keys"])
        lead = None
        if va is not None and vb is not None and va != vb:
            lead = a if va > vb else b
            # A hair's difference is not a difference. Anything inside 2% of
            # the larger value is reported as level, so the page does not
            # manufacture a distinction out of rounding.
            if abs(va - vb) <= abs(max(va, vb, key=abs)) * 0.02:
                lead = None
        rows.append({
            "key": f["key"], "label": f["label"], "note": f["note"],
            "a_raw": va, "b_raw": vb,
            "a": _fmt(va, f["fmt"]), "b": _fmt(vb, f["fmt"]),
            "higher": lead,
        })

    sa, sb = _bucket_of(ta), _bucket_of(tb)
    return {
        "a": {"ticker": a, "name": ta.get("name") or a, "grade": ta.get("grade") or "",
              "sector": sa, "slug": slugify(sa or "")},
        "b": {"ticker": b, "name": tb.get("name") or b, "grade": tb.get("grade") or "",
              "sector": sb, "slug": slugify(sb or "")},
        "same_sector": bool(sa and sb and sa == sb),
        "rows": rows,
        # How many measured dimensions actually separate them. Used only to
        # tell the reader how alike the two are — never as a score.
        "differing": sum(1 for r in rows if r["higher"]),
        "measured": sum(1 for r in rows if r["a_raw"] is not None and r["b_raw"] is not None),
    }


def pointers(c: dict, baseline: Optional[dict] = None) -> list[dict]:
    """"What to look at next" for a comparison. NOT a recommendation.

    A comparison table shows the reader ten differences and leaves them to work
    out which ones mean something. This does the obvious analytical legwork:
    it names the widest divergence, flags figures that are unusual enough to
    probably have a one-off behind them, and says where in the product the
    answer lives.

    Every item is a QUESTION or a PLACE TO LOOK, never a course of action. The
    line is deliberate and worth holding: "check whether that margin includes a
    one-off" is research, "prefer the higher-margin one" is advice. Nothing
    here ranks the two companies or suggests owning either.
    """
    A, B = c["a"]["ticker"], c["b"]["ticker"]
    rows = {r["key"]: r for r in (c.get("rows") or [])}
    out: list[dict] = []

    def both(k):
        r = rows.get(k) or {}
        return r.get("a_raw"), r.get("b_raw"), r

    # 1. The widest relative gap — the single thing most worth understanding.
    widest, widest_score = None, 0.0
    for k in ("revenue_growth_yoy", "profit_margin", "gross_margin", "pe_ratio",
              "momentum_3m", "target_upside_pct"):
        va, vb, r = both(k)
        if va is None or vb is None:
            continue
        denom = max(abs(va), abs(vb))
        if not denom:
            continue
        rel = abs(va - vb) / denom
        if rel > widest_score:
            widest, widest_score = r, rel
    if widest is not None and widest_score >= 0.25:
        out.append({
            "kind": "gap",
            "title": f"Start with {widest['label'].lower()}",
            "body": (f"It is the widest divergence here — {A} {widest['a']} against "
                     f"{B} {widest['b']}. Whatever explains that gap probably explains "
                     f"most of the difference between these two."),
        })

    # 2. Figures far above the universe median. Deliberately does NOT assert a
    #    cause: NVDA really does sustain a 60%+ net margin, so telling a reader
    #    "a one-off is the likeliest explanation" would be confidently wrong.
    #    It reports the distance from normal and says where the answer lives.
    #    Falls back to fixed thresholds only when no baseline is supplied.
    for k, what, fallback in (("revenue_growth_yoy", "revenue growth", 1.00),
                              ("profit_margin", "net margin", 0.45)):
        va, vb, r = both(k)
        med = None
        if baseline:
            raw = baseline.get("growth_median" if k == "revenue_growth_yoy"
                               else "margin_median")
            if raw is not None:
                med = float(raw) / 100.0        # baseline medians are percentages
        trigger = (med * 3.0) if med not in (None, 0) else fallback
        for tk, v in ((A, va), (B, vb)):
            if v is None or v < trigger:
                continue
            shown = r["a"] if tk == A else r["b"]
            ref = (f", against a universe median of {round(med * 100, 1)}%"
                   if med is not None else "")
            out.append({
                "kind": "check",
                "title": f"Understand {tk}'s {what}",
                "body": (f"At {shown} it is well above the rest of the market{ref}. "
                         f"That can be structural or it can be a cycle turn, a disposal "
                         f"or a tax item — the income statement and the latest filing "
                         f"distinguish the two, and which it is changes how much weight "
                         f"this row deserves."),
            })

    # 3. Cross-sector comparisons are not like-for-like and the table cannot
    #    show that on its own.
    if not c.get("same_sector"):
        out.append({
            "kind": "context",
            "title": "These are not like-for-like",
            "body": (f"{A} sits in {c['a'].get('sector') or 'a different sub-sector'} and "
                     f"{B} in {c['b'].get('sector') or 'another'}. Valuation and margin "
                     f"norms differ between industries, so the multiple and margin rows "
                     f"are comparing against different backdrops."),
        })

    # 4. Very close overall — the table is not the deciding evidence.
    if (c.get("differing") or 0) <= 3 and (c.get("measured") or 0) >= 6:
        out.append({
            "kind": "context",
            "title": "The numbers barely separate these",
            "body": (f"They differ on only {c['differing']} of {c['measured']} measured "
                     f"dimensions. Whatever distinguishes them is not in this table — "
                     f"the filings, the product lines and the customer concentration are "
                     f"where to look."),
        })

    # 5. Beta gap — a risk characteristic, stated as one.
    va, vb, r = both("beta")
    if va is not None and vb is not None and abs(va - vb) >= 0.4:
        hi = A if va > vb else B
        out.append({
            "kind": "context",
            "title": "They carry different volatility",
            "body": (f"{hi} has the higher beta ({r['a']} vs {r['b']}), so it has "
                     f"historically moved more than the other for the same market move. "
                     f"That is a description of past variability, not of risk overall."),
        })

    # Order and cap. A pair of high-margin, fast-growing names can trip four
    # "understand this figure" items and push the context off the end, which
    # would leave the reader with a list of caveats and no orientation. The gap
    # leads, context follows, and at most two figure checks come last.
    gap = [p for p in out if p["kind"] == "gap"]
    ctx = [p for p in out if p["kind"] == "context"]
    chk = [p for p in out if p["kind"] == "check"][:2]
    return (gap + ctx + chk)[:5]


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
