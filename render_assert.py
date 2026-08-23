#!/usr/bin/env python
"""render_assert.py — render every public page in-process and assert on it.

    python render_assert.py                 # check
    python render_assert.py --baseline      # re-record the CSS rule counts
    python render_assert.py -v              # list every check, not just failures

WHY THIS EXISTS
---------------
Every check below is here because the bug it catches actually shipped. This is
not a generic linter; it is a list of the specific ways this codebase has
broken in production.

  * A SWALLOWED CSS RULE REPORTS ZERO PARSE ERRORS. Deleting a declaration
    block left ".cclk-cool" with no body; the parser then ate the NEXT rule as
    part of its prelude. .cclk-scale vanished, the six band labels ran together
    as "DamagedIgnoredQuietNoticedBusyCrowded", and the error count stayed 0.
    Rule COUNT is the tell, not error count -> CHECK css_rule_count, dangling.

  * A ONE-SHOT str.replace HITS THE WRONG PAGE. "{_theme.footer_html()}\\n
    </body>" exists on /reports as well as /stocks, and earlier in the file, so
    three separate scripts were inserted into the wrong renderer and shipped
    doing nothing. -> CHECK script_placement.

  * A NAME DEFINED IN ONE FUNCTION AND READ IN ANOTHER. `_html` is imported
    locally in several builders but does not exist at module scope, and
    `public` was a parameter of render_card() read inside card_body(). Both
    raised inside a try/except or a 500 handler, so a whole block silently
    disappeared. Three separate occurrences. -> CHECK renders, gated_blocks.

  * A CACHED RENDERING OUTLIVES ITS GATE. The position block was gated in
    card_body(), but the API cached the UNGATED html and the page inlined it,
    so the gate applied to a rendering nobody read. -> CHECK gated_blocks.

  * A FRACTION-VS-PERCENT GUESS. A 1.15 dividend yield printed as 115.0%, and
    the "fix" that matched the other module printed MongoDB's 0.61 as 61.00%.
    -> CHECK number_sanity.

  * A LOSS DRAWN AS A SMALL WIN. Bars clamped to zero, so a -3.6% operating
    margin rendered as a tiny positive stub. -> CHECK number_sanity.

Add a check here the moment something breaks; do not add checks speculatively.
"""

import argparse
import asyncio
import io
import json
import os
import re
import sys

BASELINE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".render_baseline.json")

# Families deleted from the type system, and the pre-August palette.
BANNED_TEXT = [
    ("font", "'Fraunces'"), ("font", "'Instrument Sans'"), ("font", "'Manrope'"),
    ("font", "family=Inter"),
    ("colour", "#2970FF"), ("colour", "#2970ff"), ("colour", "0040c1"),
    ("colour", "#cdeef8"), ("colour", "#f0efe8"), ("colour", "#fafbf7"),
]

# Blocks that must never reach a logged-out reader on /stocks.
PUBLIC_MUST_NOT_CONTAIN = [
    "What a position does",     # position sizing: a portfolio tool
    "Dated events",             # 13G/13D filing rows
    "Your own checks",          # interactive personal checklist
    "Go deeper with Pro",       # there is no Pro tier
    "TickerMover Pro",
]

# script marker -> the ONLY renderer that may carry it
SCRIPT_HOME = {
    "Cold-cache warmer": "stocks",
    "Thesis audit -> bullets": "stocks",
}

FIXTURE = {
    "ticker": "LITE", "name": "Lumentum Holdings Inc.", "sector": "Technology",
    "sub_sector": "Optical Components / Photonics", "price": 214.0, "change_pct": 1.2,
    "smart_score": 73, "pop_score": 73, "grade": "A",
    "rating": "★★★★★ Top Tier",
    "dividend_yield": 0.61, "short_percent_float": 1.4, "beta": 1.05,
    "gross_margin": 0.35, "operating_margin": -0.036, "profit_margin": -0.011,
    "fcf_margin": 0.22, "pe_ratio": 24.9, "forward_pe": 21.4,
    "revenue_growth_yoy": 0.141, "eps_beat_streak": 4,
    "low_52w": 118.30, "high_52w": 341.95,
    "target_low": 110.0, "target_high": 500.0, "target_mean": 321.35,
    "breakdown": {"momentum_1m": .99, "growth_tier": .75, "fundamentals": .62,
                  "rsi_zone": .80, "trend_strength": .71},
    "weighted": {"momentum_1m": 9.0, "growth_tier": 7.5, "fundamentals": 3.1,
                 "rsi_zone": 5.6, "trend_strength": 4.9},
    "eps_quarters": [{"date": "2026-04-30", "surprise_pct": 12.0},
                     {"date": "2026-01-31", "surprise_pct": -4.0}],
}


class Report:
    def __init__(self, verbose):
        self.rows, self.failed, self.verbose = [], 0, verbose

    def check(self, page, name, ok, detail=""):
        if not ok:
            self.failed += 1
        if not ok or self.verbose:
            self.rows.append(("FAIL" if not ok else "ok  ", page, name, detail))

    def dump(self):
        for tag, page, name, detail in self.rows:
            print("%s %-10s %-18s %s" % (tag, page, name, detail))
        print("-" * 66)
        print("FAILED %d check(s)" % self.failed if self.failed else "all checks passed")
        return 1 if self.failed else 0


# ── helpers ──────────────────────────────────────────────────────────────
def stylesheets(html, html5lib):
    doc = html5lib.parse(html, namespaceHTMLElements=False)
    return [e.text for e in doc.iter("style") if e.text and e.text.strip()]


def css_stats(html, html5lib, tinycss2):
    """Return (qualified rule count, [suspicious preludes]).

    A prelude carrying a newline but NO comma is two selectors jammed
    together - the signature of a rule that swallowed the one after it.
    """
    total, bad = 0, []
    for text in stylesheets(html, html5lib):
        if "'+" in text[:400]:
            continue
        for rule in tinycss2.parse_stylesheet(text, skip_comments=True, skip_whitespace=True):
            if rule.type != "qualified-rule":
                continue
            total += 1
            prelude = tinycss2.serialize(rule.prelude)
            if "\n" in prelude.strip() and "," not in prelude:
                bad.append(" ".join(prelude.split())[:70])
    return total, bad


def bad_scripts(html, html5lib, esprima):
    """Inline scripts that do not parse. application/ld+json is JSON, not JS -
    counting it as a broken script is what made earlier numbers look alarming."""
    out = []
    doc = html5lib.parse(html, namespaceHTMLElements=False)
    for el in doc.iter("script"):
        if el.get("src") or not (el.text or "").strip():
            continue
        if (el.get("type") or "").endswith("json"):
            continue
        try:
            esprima.parseScript(el.text)
        except Exception as exc:
            out.append(str(exc)[:60])
    return out


def render_pages():
    """{name: html} for every public renderer."""
    import app, seo_pages as S, legal_pages as L
    app._universe_data = [FIXTURE] + [
        dict(FIXTURE, ticker=t, name=n) for t, n in
        (("AAOI", "Applied Optoelectronics"), ("VIAV", "Viavi Solutions"),
         ("COHR", "Coherent Corp."), ("MTSI", "MACOM Technology"))]
    O = "https://tickermover.com"
    U = app._universe_data
    return {
        "stocks":     asyncio.run(app.stock_page("LITE")).body.decode(),
        "reports":    asyncio.run(app.reports_index()).body.decode(),
        "learn":      S.render_pillar_index(O),
        "sectors":    S.render_sector_index(U, O),
        "compare":    S.render_compare_index(U, O),
        "terms":      L.render_terms(),
        "privacy":    L.render_privacy(),
        "disclaimer": L.render_disclaimer(),
    }


# ── the checks ───────────────────────────────────────────────────────────
def run(update_baseline, verbose, pages=None, quiet=False):
    import html5lib, tinycss2, esprima
    rep = Report(verbose)

    if pages is None:
        try:
            pages = render_pages()
        except Exception as exc:
            print("FAIL render — %r" % (exc,))
            return 1

    base = {}
    if os.path.exists(BASELINE):
        base = json.load(io.open(BASELINE, encoding="utf-8"))
    fresh = {}

    for name, html in sorted(pages.items()):
        rep.check(name, "renders", bool(html) and len(html) > 4000, "%d bytes" % len(html or ""))

        # --- structure ---
        rep.check(name, "closes body", "</body>" in html and "</html>" in html)
        rep.check(name, "one h1", html.count("<h1") == 1, "found %d" % html.count("<h1"))

        # --- CSS: count, not error count ---
        rules, dangling = css_stats(html, html5lib, tinycss2)
        fresh[name] = rules
        rep.check(name, "no dangling sel", not dangling, "; ".join(dangling[:2]))
        if not update_baseline and name in base:
            rep.check(name, "css_rule_count", rules >= base[name],
                      "%d (baseline %d)" % (rules, base[name]))

        # --- JS ---
        bad = bad_scripts(html, html5lib, esprima)
        rep.check(name, "scripts parse", not bad, "; ".join(bad[:2]))

        # --- fonts and palette ---
        hits = ["%s:%s" % (k, v) for k, v in BANNED_TEXT if v in html]
        rep.check(name, "no legacy theme", not hits, "; ".join(hits[:3]))
        rep.check(name, "loads Public Sans", "Public+Sans" in html)

        # --- a script may only appear on the page it was written for ---
        for marker, home in SCRIPT_HOME.items():
            present, belongs = marker in html, (name == home)
            if present == belongs:
                detail = "%r %s" % (marker, "present" if present else "absent")
            else:
                detail = "%r %s" % (marker, "MISSING" if belongs else "LEAKED onto this page")
            rep.check(name, "script placement", present == belongs, detail)

    # --- /stocks only: gating and number sanity -------------------------
    st = pages["stocks"]
    for phrase in PUBLIC_MUST_NOT_CONTAIN:
        rep.check("stocks", "gated block", phrase not in st, phrase)

    # a percentage that large is a fraction-vs-percent bug, not a company
    # EVERY occurrence, not the first. The original bug was precisely that the
    # same field printed two different scales in two places on one page - a
    # re.search stops at the good one and never sees the bad one.
    for label in ("Dividend yield", "Short % of float"):
        for m in re.finditer(re.escape(label) + r"[^%]{0,160}?([0-9]+\.[0-9]+)%", st):
            v = float(m.group(1))
            rep.check("stocks", "number_sanity", v < 100.0, "%s = %.2f%%" % (label, v))

    # a negative margin must be drawn as negative, never clamped to a stub
    neg = re.findall(r'class="ch-val neg"', st)
    has_neg_input = FIXTURE["operating_margin"] < 0 or FIXTURE["profit_margin"] < 0
    rep.check("stocks", "signed bars", bool(neg) == has_neg_input,
              "%d signed labels" % len(neg))

    # every section must say something the others do not
    caps = re.findall(r"<figcaption><b>(.*?)</b>", st)
    rep.check("stocks", "no dup charts", len(caps) == len(set(caps)), ", ".join(caps))
    # Wordmark count, per page. This exists because seo_pages emitted the
    # wordmark from BOTH the shared nav and a body builder, so /sectors showed
    # two stacked. /stocks legitimately carries three: nav, footer, and the
    # sign-off card. An exact expectation is what makes a duplicate fail.
    for pg, want in (("stocks", 3), ("sectors", 2), ("learn", 2), ("compare", 2),
                     ("terms", 2), ("privacy", 2), ("disclaimer", 2), ("reports", 2)):
        got = pages[pg].count('class="brand"')
        rep.check(pg, "wordmark count", got == want, "found %d, want %d" % (got, want))

    # --- every sector the index links to must actually render ------------
    # 54 of 73 sector pages were 404 in production. The index is built from
    # sector_intel, which groups sub_sector -> subsector -> INDUSTRY -> sector,
    # while seo_pages resolved the slug with its own copy that skipped
    # `industry`. Only ~a third of the universe carries sub_sector and ~all of
    # it carries industry, so three quarters of the links pointed at nothing —
    # and every one of them was in sitemap.xml. Rendering the index alone
    # never caught it: the page was perfect, the destinations were not.
    try:
        import seo_pages as _S
        _O = "https://tickermover.com"
        _mixed = [
            dict(FIXTURE, ticker="AA1", name="Curated One", sub_sector="AI Semiconductors"),
            dict(FIXTURE, ticker="AA2", name="Curated Two", sub_sector="AI Semiconductors"),
            dict(FIXTURE, ticker="AA3", name="Curated Three", sub_sector="AI Semiconductors"),
        ]
        # industry-only rows: the majority case, and the one that broke
        for _t, _n in (("BB1", "Industry One"), ("BB2", "Industry Two"), ("BB3", "Industry Three")):
            _r = dict(FIXTURE, ticker=_t, name=_n, industry="Medical Devices")
            _r.pop("sub_sector", None)
            _r.pop("subsector", None)
            _mixed.append(_r)
        _idx = _S.render_sector_index(_mixed, _O)
        _links = re.findall(r'<a class="si-nm" href="/sectors/([^"]+)"', _idx)
        rep.check("sectors", "index links exist", len(_links) >= 2,
                  "%d links" % len(_links))
        for _slug in _links:
            _pg = _S.render_sector(_slug, _mixed, _O)
            _rows = len(re.findall(r'class="tk"', _pg or ""))
            rep.check("sectors", "sector link resolves", _pg is not None and _rows > 0,
                      "/sectors/%s -> %s" % (_slug, "404" if _pg is None else "%d rows" % _rows))
    except Exception as _exc:
        rep.check("sectors", "sector link resolves", False, "raised %r" % (_exc,))

    if update_baseline:
        io.open(BASELINE, "w", encoding="utf-8").write(json.dumps(fresh, indent=1, sort_keys=True))
        print("baseline written: " + ", ".join("%s=%d" % kv for kv in sorted(fresh.items())))
        return 0
    if quiet:
        return rep
    return rep.dump()


# ── self-test: prove each check actually fires ───────────────────────────
# A harness that only ever passes is worthless. Every fault below is a bug
# that really shipped; if one of these stops failing, the check guarding it
# has rotted and the next occurrence will go out silently.
# NOTE: "sector link resolves" is deliberately NOT in this list. Every fault
# here works by mutating a rendered page's HTML, and that check does not read
# a page — it re-renders each sector the index links to. It was proven to fire
# the only way that matters: against the real bug, where /sectors/medical-devices
# returned None while the index happily linked to it.
FAULTS = [
    ("dangling CSS rule", "stocks", "no dangling sel",
     lambda h: h.replace(".cclk-scale{display:grid",
                         ".cclk-orphan" + chr(10) + ".cclk-scale{display:grid", 1)),
    ("script on wrong page", "reports", "script placement",
     lambda h: h.replace("</body>", "<script>/* Cold-cache warmer */</script></body>", 1)),
    ("ungated position block", "stocks", "gated block",
     lambda h: h.replace("</body>", "<p>What a position does</p></body>", 1)),
    ("115% dividend yield", "stocks", "number_sanity",
     lambda h: h.replace("</body>", "<span>Dividend yield</span><span>115.00%</span></body>", 1)),
    ("legacy font returns", "stocks", "no legacy theme",
     lambda h: h.replace("</body>", "<style>b{font-family:'Fraunces'}</style></body>", 1)),
    ("duplicate chart", "stocks", "no dup charts",
     lambda h: h.replace("</body>",
                         "<figcaption><b>Margin profile</b></figcaption></body>", 1)),
]


def selftest():
    try:
        clean = render_pages()
    except Exception as exc:
        print("FAIL render — %r" % (exc,))
        return 1
    bad = 0
    for label, page, expect, corrupt in FAULTS:
        pages = dict(clean)
        pages[page] = corrupt(pages[page])
        if pages[page] == clean[page]:
            print("FAIL %-24s fault could not be injected" % label)
            bad += 1
            continue
        rep = run(False, False, pages=pages, quiet=True)
        names = [r[2] for r in rep.rows]
        caught = expect in names
        print("%s %-24s -> %s" % ("ok  " if caught else "FAIL", label,
                                  expect if caught else "NOT CAUGHT (%s)" % (names or "nothing")))
        if not caught:
            bad += 1
    print("-" * 66)
    print("self-test: %d check(s) failed to fire" % bad if bad else
          "self-test: every check fired on its own fault")
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true", help="re-record CSS rule counts")
    ap.add_argument("-v", "--verbose", action="store_true", help="print passing checks too")
    ap.add_argument("--selftest", action="store_true", help="prove each check fires")
    a = ap.parse_args()
    sys.exit(selftest() if a.selftest else run(a.baseline, a.verbose))
