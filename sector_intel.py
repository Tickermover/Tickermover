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


def bucket_of(t: dict) -> Optional[str]:
    """Public name for _bucket_of. THE single definition of which sub-sector a
    row belongs to — every surface that groups, links or filters by sub-sector
    must call this and nothing else. seo_pages carried its own copy that
    omitted `industry`, and since only ~a third of the universe has
    `sub_sector` while `industry` covers ~all of it, most sector pages 404'd."""
    return _bucket_of(t)


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
    {"key": "alpha",       "label": "Quant Score",        "keys": ("smart_score", "pop_score"),
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
        # The raw universe rows, for pointers() only. NOT part of the response:
        # api_compare pops them before serialising, because shipping two full
        # rows to the client would double the payload for data the panel does
        # not render. Kept here so _corroborate can reach revisions, margin
        # trend and beat history without a second lookup.
        "a_row": ta,
        "b_row": tb,
    }


def _corroborate(c: dict, lead: str) -> tuple[list[str], list[str]]:
    """Which OTHER readings move the same way as the widest gap, and which do not.

    This is the analytical step the comparison used to leave to the reader.
    Naming the widest divergence is easy and worth almost nothing on its own;
    the useful question is whether anything else in the data agrees with it.

    When several operating readings line up behind the leader, the gap has a
    visible cause and the reader should start there. When NOTHING lines up —
    AMD +14.7% against INTC -15.0% while Intel carries the better estimate
    revisions and both expanded gross margin — the honest finding is that the
    price move is not coming from the figures on this page, which is a far more
    useful thing to be told than "whatever explains that gap probably explains
    most of the difference".

    Returns (agrees_with_lead, points_the_other_way) as finished clauses.
    Deliberately describes only; nothing here ranks the two companies.
    """
    A, B = c["a"]["ticker"], c["b"]["ticker"]
    ra, rb = c.get("a_row") or {}, c.get("b_row") or {}
    agree: list[str] = []
    against: list[str] = []
    neither: list[str] = []      # true of BOTH — does not separate them

    def _num(d, *keys):
        for k in keys:
            v = d.get(k)
            try:
                if v is not None:
                    return float(v)
            except (TypeError, ValueError):
                continue
        return None

    def note(better: str, text: str):
        (agree if better == lead else against).append(text)

    # Estimate revisions — the cleanest read on whether the street is moving.
    va, vb = ra.get("eps_revisions_30d") or {}, rb.get("eps_revisions_30d") or {}
    if va.get("ups") is not None and vb.get("ups") is not None:
        na = (va.get("ups") or 0) - (va.get("downs") or 0)
        nb = (vb.get("ups") or 0) - (vb.get("downs") or 0)
        if na != nb:
            hi, lo = (A, B) if na > nb else (B, A)
            hv, lv = (va, vb) if na > nb else (vb, va)
            note(hi, (f"{hi} carries the better estimate revisions "
                      f"({hv.get('ups', 0)} up, {hv.get('downs', 0)} down against "
                      f"{lv.get('ups', 0)} up, {lv.get('downs', 0)} for {lo})"))

    # Gross-margin direction — structural, and moves slowly.
    ta, tb = ra.get("gross_margin_trend"), rb.get("gross_margin_trend")
    if ta and tb and ta != tb:
        hi = A if ta == "expanding" else B
        note(hi, f"{A} gross margin is {ta} and {B}'s is {tb}")
    elif ta and tb and ta == tb and ta != "stable":
        neither.append(f"both are {ta} gross margin")

    # Most recent quarter against expectations.
    ba, bb = ra.get("eps_last_beat"), rb.get("eps_last_beat")
    if ba is not None and bb is not None and ba != bb:
        hi = A if ba else B
        note(hi, f"{hi} beat on its last quarter and the other missed")
    elif ba and bb:
        neither.append("both beat on their last quarter")

    # Quarterly revenue growth — the near-term operating trend.
    qa, qb = _num(ra, "rev_growth_qyoy"), _num(rb, "rev_growth_qyoy")
    if qa is not None and qb is not None and abs(qa - qb) >= 0.05:
        hi = A if qa > qb else B
        note(hi, (f"{hi} grew revenue faster last quarter "
                  f"({round(max(qa, qb) * 100, 1)}% against {round(min(qa, qb) * 100, 1)}%)"))

    # Beat streak — consistency rather than a single print.
    sa, sb = _num(ra, "eps_beat_streak"), _num(rb, "eps_beat_streak")
    if sa is not None and sb is not None and abs(sa - sb) >= 2:
        hi = A if sa > sb else B
        note(hi, f"{hi} has the longer run of beats ({int(max(sa, sb))} against {int(min(sa, sb))})")
    elif sa is not None and sa == sb and sa >= 3:
        neither.append(f"both have beaten {int(sa)} quarters running")

    return agree, against, neither


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
        # Naming the widest gap and stopping there was circular — the table
        # already marks it, and "whatever explains that gap probably explains
        # most of the difference" tells the reader nothing they did not know.
        # So test it: does anything else in the data move the SAME way?
        lead = A if (widest.get("a_raw") or 0) >= (widest.get("b_raw") or 0) else B
        agree, against, neither = _corroborate(c, lead)
        head = f"{A} {widest['a']} against {B} {widest['b']} — the widest gap here. "
        if agree:
            body = (head + f"{len(agree)} other reading"
                    + ("s point" if len(agree) > 1 else " points")
                    + " the same way: " + "; ".join(agree[:3]) + ".")
            if against:
                body += " Against it: " + "; ".join(against[:2]) + "."
        elif against:
            # The finding the old copy could never reach: a price gap that the
            # operating figures do not support. Worth far more to a reader than
            # being told the biggest difference is the biggest difference.
            body = (head + "Nothing else here moves with it — "
                    + "; ".join(against[:3]) + ". "
                    + "Whatever is driving it is not in these rows.")
        elif neither:
            body = (head + "The operating readings do not separate them: "
                    + "; ".join(neither[:3]) + ". "
                    + "The gap is not coming from these rows.")
        else:
            body = (head + "We hold no revisions, margin-trend or beat history for "
                    "both sides, so there is nothing here to corroborate it against.")
        if neither and (agree or against):
            body += " Neither is distinguished by: " + "; ".join(neither[:2]) + "."
        out.append({
            "kind": "gap",
            "title": f"Start with {widest['label'].lower()}",
            "body": body,
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
            # The old body was one fixed sentence — "structural or a cycle
            # turn, a disposal or a tax item" — printed identically for every
            # company, which is a definition of the problem rather than a read
            # on it. We hold the quarterly run-rate and the gross-margin
            # direction, and those two are exactly what separate a structural
            # figure from a flattering one. Use them.
            row = (c.get("a_row") if tk == A else c.get("b_row")) or {}
            tail = (f"The income statement and the latest filing distinguish a "
                    f"structural figure from a one-off.")
            if k == "revenue_growth_yoy":
                q = row.get("rev_growth_qyoy")
                try:
                    q = float(q) if q is not None else None
                except (TypeError, ValueError):
                    q = None
                if q is not None:
                    qp = round(q * 100, 1)
                    if q < v * 0.6:
                        tail = (f"The most recent quarter ran +{qp}%, well under the "
                                f"annual figure — the twelve-month number is carrying "
                                f"an easier comparison base than the business is "
                                f"currently growing at.")
                    elif q > v * 1.2:
                        tail = (f"The most recent quarter ran +{qp}%, ahead of the "
                                f"annual figure, so the run-rate is still climbing "
                                f"rather than lapping a weak base.")
                    else:
                        tail = (f"The most recent quarter ran +{qp}%, in line with the "
                                f"annual figure, so the rate is holding rather than "
                                f"resting on one unusual period.")
            else:
                trend = row.get("gross_margin_trend")
                if trend in ("expanding", "contracting", "stable"):
                    tail = (f"Gross margin is {trend}, so the net figure is "
                            + ("supported by the line above it."
                               if trend == "expanding" else
                               "not being helped by the line above it — check what "
                               "below the gross line is doing the work."
                               if trend == "contracting" else
                               "not moving with the gross line; the difference is "
                               "below it."))
            out.append({
                "kind": "check",
                "title": f"Understand {tk}'s {what}",
                "body": f"At {shown}{ref}. {tail}",
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
    # ONE figure check, not two: on a pair where both names are growing hard
    # (MU vs WDC, NVDA vs AMD) two of these render as near-identical
    # paragraphs, which is padding rather than analysis. Keep the more
    # extreme one.
    chk = [p for p in out if p["kind"] == "check"][:1]
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
