"""
seo_pages.py — SEO Phase 3 page renderers for TickerMover.

Houses the long-form HTML renderers for:
  • /learn/{slug}              — evergreen educational pillar pages
  • /learn                     — pillar index (hub-and-spoke for crawlers)
  • /sectors/{slug}            — one page per sub-sector with live Alpha Scores
  • /sectors                   — sector index
  • /compare/{a}-vs-{b}        — side-by-side ticker comparison

Why a separate module?
    app.py is already ~2700 lines. Putting the SEO chrome (CSS + brand
    header + newsletter footer) plus three new renderer families inline
    would push it over 3500 lines and make every read painful. Keeping
    these here also lets us iterate on copy without touching the API
    layer.

The module is intentionally pure-functional — every renderer takes the
universe snapshot + SITE_ORIGIN as arguments and returns a complete HTML
string. No globals, no FastAPI imports, no I/O. The /app routes in
app.py call into here.
"""
from __future__ import annotations
import json as _json
import re
from typing import Iterable, Optional


# ─── Slugging ─────────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Lowercase, dashes, alphanum-only — used for sector → URL conversion."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "untitled"


def sector_slugs(universe: list[dict]) -> dict[str, str]:
    """Map slug → original sub_sector label for the live universe."""
    out: dict[str, str] = {}
    for t in universe or []:
        sub = t.get("sub_sector") or t.get("subsector") or t.get("sector") or ""
        if sub and slugify(sub) not in out:
            out[slugify(sub)] = sub
    return out


# ─── Shared chrome ────────────────────────────────────────────────────

_BASE_CSS = """
*{margin:0;padding:0;box-sizing:border-box}
button,input,select,textarea{font-family:inherit;font-size:inherit;line-height:inherit}
html{scroll-behavior:smooth}
body{font-family:'Inter',-apple-system,BlinkMacSystemFont,sans-serif;color:#0a0a0a;background:#fafbfc;line-height:1.65;font-size:16px;-webkit-font-smoothing:antialiased}
a{color:#2970ff;text-decoration:none;font-weight:600}
a:hover{text-decoration:underline}
.mono{font-family:'JetBrains Mono',ui-monospace,monospace;font-feature-settings:'tnum' 1}
.wrap{max-width:820px;margin:0 auto;padding:24px 24px 64px}
.wrap-wide{max-width:1100px;margin:0 auto;padding:24px 24px 64px}
.brand{display:inline-flex;align-items:baseline;font-size:16px;font-weight:800;color:#0a0a0a;margin-bottom:24px}
.brand-wordmark{display:inline-flex;align-items:baseline;flex-wrap:nowrap;white-space:nowrap;color:#0a0e22}
.brand-m{height:1.6em;width:auto;flex:none;align-self:baseline;margin:0 .02em}
.crumbs{font-size:12.5px;color:#94a3b8;margin-bottom:8px;letter-spacing:.04em;text-transform:uppercase;font-weight:700}
.crumbs a{color:#94a3b8;font-weight:600}
.crumbs a:hover{color:#2970ff}
h1{font-size:42px;font-weight:900;letter-spacing:-.03em;margin-bottom:10px;color:#0a0a0a;line-height:1.1}
h1 .sym{font-family:'JetBrains Mono',monospace;color:#2970ff}
.lede{font-size:19px;line-height:1.55;color:#475569;margin-bottom:32px;font-weight:500}
h2{font-size:26px;font-weight:800;letter-spacing:-.02em;margin:48px 0 14px;color:#0a0a0a}
h3{font-size:19px;font-weight:800;letter-spacing:-.012em;margin:28px 0 10px;color:#0a0a0a}
p{margin-bottom:16px;color:#1e293b}
ul,ol{margin:8px 0 20px 22px}
li{margin-bottom:8px;color:#1e293b}
.tag{display:inline-block;background:#EAF1FF;color:#1d4ed8;padding:4px 10px;border-radius:5px;font-size:11.5px;font-weight:700;letter-spacing:.03em;text-transform:uppercase;margin-bottom:14px}
blockquote{border-left:3px solid #2970ff;background:#EAF1FF;padding:14px 20px;margin:20px 0;border-radius:0 8px 8px 0;color:#0c2b6b;font-style:italic;font-size:15.5px}
code{background:#f1f5f9;padding:2px 6px;border-radius:4px;font-family:'JetBrains Mono',monospace;font-size:14px;color:#0f172a}
.cta{margin-top:48px;padding:30px 32px;background:linear-gradient(135deg,#0a0e22 0%,#0a2f8f 55%,#2970ff 100%);border-radius:16px;text-align:center;color:#fff}
.cta h3{font-size:22px;font-weight:800;letter-spacing:-.02em;margin-bottom:8px;color:#fff}
.cta p{color:rgba(255,255,255,.7);margin-bottom:18px}
.cta-btn{display:inline-block;background:#fff;color:#0a0a0a;padding:13px 26px;border-radius:10px;font-weight:700;font-size:14.5px}
.cta-btn:hover{background:#f1f5f9;text-decoration:none}
.legal{margin-top:36px;font-size:12px;color:#94a3b8;text-align:center;line-height:1.6}
/* Newsletter footer */
.nl{margin-top:48px;padding:28px 24px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px}
.nl h3{margin:0 0 6px;font-size:18px;font-weight:800}
.nl p{margin:0 0 14px;color:#475569;font-size:14.5px}
.nl form{display:flex;gap:8px;flex-wrap:wrap}
.nl input[type=email]{flex:1;min-width:220px;padding:12px 14px;border:1px solid #cbd5e1;border-radius:8px;font-size:14.5px;font-family:inherit;background:#fff}
.nl input[type=email]:focus{outline:none;border-color:#2970ff;box-shadow:0 0 0 3px rgba(41,112,255,.15)}
.nl button{padding:12px 22px;background:#2970ff;color:#fff;border:none;border-radius:8px;font-weight:700;font-size:14.5px;cursor:pointer;font-family:inherit}
.nl button:hover{background:#0042c5}
.nl .nl-msg{margin-top:10px;font-size:13.5px;font-weight:600;min-height:18px}
.nl .nl-msg.ok{color:#15803d}
.nl .nl-msg.err{color:#b91c1c}
.nl-honey{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}
/* Sector / pillar / compare cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin:18px 0 28px}
.card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:18px 20px;transition:all .15s}
.card:hover{border-color:#2970ff;box-shadow:0 6px 16px rgba(41,112,255,.10);text-decoration:none}
.card a{color:inherit;text-decoration:none;display:block}
.card .ttl{font-size:15.5px;font-weight:800;color:#0a0a0a;margin-bottom:4px}
.card .sub{font-size:13px;color:#64748b;line-height:1.5}
/* Stock list table */
.tbl{width:100%;border-collapse:collapse;margin:16px 0 28px;font-size:14.5px;background:#fff;border:1px solid #e2e8f0;border-radius:10px;overflow:hidden}
.tbl th{background:#f8fafc;text-align:left;padding:11px 14px;font-size:11.5px;font-weight:700;color:#475569;letter-spacing:.04em;text-transform:uppercase;border-bottom:1px solid #e2e8f0}
.tbl td{padding:11px 14px;border-bottom:1px solid #eef0f3;vertical-align:top}
.tbl tr:last-child td{border-bottom:none}
.tbl tr:hover{background:#f8fafc}
.tbl .tk{font-family:'JetBrains Mono',monospace;font-weight:800;color:#2970ff}
.tbl .pop{font-family:'JetBrains Mono',monospace;font-weight:800}
.tbl .grade{display:inline-block;width:24px;text-align:center;padding:3px 0;border-radius:5px;font-weight:800;font-size:12.5px;color:#fff}
.tbl .grade.A{background:#15803d}.tbl .grade.B{background:#2970ff}.tbl .grade.C{background:#D4860A}.tbl .grade.D{background:#ea7317}.tbl .grade.F{background:#dc2626}
.tbl .vd{color:#475569;font-size:13.5px;line-height:1.5}
@media(max-width:640px){h1{font-size:32px}.lede{font-size:17px}.tbl{font-size:13px}.tbl td,.tbl th{padding:9px 8px}}
/* Compare layout */
.cmp-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:18px 0 30px}
.cmp-card{background:#fff;border:1px solid #e2e8f0;border-radius:12px;padding:20px}
.cmp-card .tk{font-family:'JetBrains Mono',monospace;font-size:24px;font-weight:900;color:#2970ff}
.cmp-card .nm{font-size:14px;color:#64748b;margin-bottom:14px}
.cmp-row{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f1f5f9;font-size:14px}
.cmp-row:last-child{border-bottom:none}
.cmp-row .k{color:#64748b;font-weight:600}
.cmp-row .v{font-family:'JetBrains Mono',monospace;font-weight:800;color:#0a0a0a}
.cmp-vs{text-align:center;font-family:'JetBrains Mono',monospace;font-weight:800;color:#94a3b8;font-size:13px;letter-spacing:.06em;margin:4px 0}
@media(max-width:640px){.cmp-grid{grid-template-columns:1fr}}
"""

_FONTS_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900'
    '&family=JetBrains+Mono:wght@500;700;800&display=swap" rel="stylesheet">'
)

def brand_header() -> str:
    # Canonical TickerMover wordmark — kept byte-for-byte in sync with the
    # landing page (.brand-wordmark): "Ticker" + blue chart-arrow "M" + "over".
    return (
        '<a href="/" class="brand"><span class="brand-wordmark">Ticker'
        '<svg class="brand-m" viewBox="0 0 90 105" fill="none" aria-hidden="true">'
        '<polyline points="5,100 23,42 45,66 67,26 85,100" stroke="#2970ff" '
        'stroke-width="9" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="67" cy="8" r="7" fill="#2970ff"/></svg>over</span></a>'
    )


def newsletter_block(source: str) -> str:
    """Inline newsletter capture block. `source` is recorded server-side
    so we can attribute signups to the page they came from."""
    safe = (source or "unknown").replace('"', "")
    return f"""
<div class="nl">
  <h3>Get the weekly Alpha Score digest</h3>
  <p>Top-rated US stocks, conflict alerts, and Reverse-DCF reads — straight to your inbox every Sunday. Free.</p>
  <form id="nl-{safe}" autocomplete="off">
    <input type="email" name="email" placeholder="you@email.com" required>
    <input type="text" name="company" class="nl-honey" tabindex="-1" autocomplete="off">
    <button type="submit">Subscribe</button>
  </form>
  <div class="nl-msg" id="nl-msg-{safe}"></div>
  <div style="margin-top:14px;font-size:12.5px;color:#94a3b8">
    Questions or feedback? Email <a href="mailto:support@tickermover.com" style="color:#2970ff">support@tickermover.com</a>
  </div>
</div>
<script>
(function(){{
  var f = document.getElementById('nl-{safe}');
  var m = document.getElementById('nl-msg-{safe}');
  if (!f) return;
  f.addEventListener('submit', async function(e){{
    e.preventDefault();
    var fd = new FormData(f);
    if (fd.get('company')) {{ m.className='nl-msg ok'; m.textContent='Thanks!'; return; }}
    m.className='nl-msg'; m.textContent='Sending…';
    try {{
      var r = await fetch('/api/newsletter/subscribe', {{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{email: fd.get('email'), source: '{safe}'}})
      }});
      var j = await r.json().catch(function(){{return {{}}}});
      if (r.ok) {{ m.className='nl-msg ok'; m.textContent=j.message||'Subscribed — check your inbox!'; f.reset(); }}
      else {{ m.className='nl-msg err'; m.textContent=j.detail||'Something went wrong. Try again.'; }}
    }} catch (err) {{ m.className='nl-msg err'; m.textContent='Network error. Try again.'; }}
  }});
}})();
</script>
"""


def cta_block(label: str = "Open the live dashboard", href: str = "/app?signup=1") -> str:
    return f"""
<div class="cta">
  <h3>Real research, free during beta</h3>
  <p>200+ US stocks. Alpha Score, conflict detection, Reverse DCF, peer comparison. We do the homework.</p>
  <a href="{href}" class="cta-btn">{label} →</a>
</div>
"""


def page_shell(title: str, desc: str, canonical: str, body_html: str,
               schema_json: Optional[str] = None, robots: str = "index,follow",
               og_image: Optional[str] = None, og_type: str = "article") -> str:
    """Wrap body content in a complete HTML doc with SEO head."""
    img = og_image or ""
    schema_tag = f'<script type="application/ld+json">{schema_json}</script>' if schema_json else ""
    # Cookieless analytics on the SEO pages too — these are the organic-search
    # front door, so channel attribution starts here. Renders nothing until
    # PLAUSIBLE_DOMAIN is configured (see config.py).
    import config as _cfg
    _pd = (getattr(_cfg, "PLAUSIBLE_DOMAIN", "") or "").strip()
    analytics_tag = (f'<script defer data-domain="{_pd}" '
                     f'src="{_cfg.PLAUSIBLE_SRC}"></script>') if _pd else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<meta name="robots" content="{robots}">
<link rel="canonical" href="{canonical}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:type" content="{og_type}">
<meta property="og:url" content="{canonical}">
{f'<meta property="og:image" content="{img}">' if img else ''}
<meta property="og:site_name" content="TickerMover">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{desc}">
<link rel="icon" href="/favicon.ico">
{_FONTS_LINK}
{schema_tag}
{analytics_tag}
<style>{_BASE_CSS}</style>
</head>
<body>
{body_html}
</body>
</html>"""


# ─── Pillar pages ─────────────────────────────────────────────────────

PILLARS: dict[str, dict] = {
    "pop-score": {
        "title": "What is Alpha Score? TickerMover's 0-100 stock rating explained",
        "desc": "Alpha Score is TickerMover's 0-100 composite rating that blends fundamentals, momentum, valuation, analyst signal, and macro regime into a single plain-English verdict.",
        "h1": "What is the Alpha Score?",
        "lede": "A 0-100 composite that turns five different stock signals into one number — so you can stop juggling P/E, EPS revisions, momentum charts, and analyst ratings in your head.",
    },
    "reverse-dcf": {
        "title": "Reverse DCF explained: what growth rate is the stock pricing in?",
        "desc": "Reverse DCF flips the script: instead of guessing growth, it solves for the revenue CAGR baked into today's price. Use it to spot stocks priced for impossible growth.",
        "h1": "Reverse DCF — what growth is the market pricing in?",
        "lede": "A traditional DCF asks 'what is this stock worth?'. A Reverse DCF asks the more useful question: 'what does the market already believe about this company's future, and is that believable?'",
    },
    "how-to-read-fundamentals": {
        "title": "How to read US stock fundamentals — a beginner's checklist",
        "desc": "A practical 7-line checklist for reading a US stock the way a professional does: revenue growth, margins, free cash flow, valuation, balance sheet, momentum, and conviction.",
        "h1": "How to read US stock fundamentals (without a finance degree)",
        "lede": "You don't need a CFA to spot a great business. You need seven numbers, in the right order, and a sense for what \"good\" looks like in each. Here's the checklist we use on TickerMover.",
    },
    "how-to-find-breakout-stocks": {
        "title": "How to find breakout stocks — a 6-point research checklist (2026)",
        "desc": "Breakout stocks pair momentum, relative strength, volume and room to run. Here's the 6-point research checklist TickerMover uses to surface them — and the fakeout traps to avoid.",
        "h1": "How to find breakout stocks",
        "lede": "A breakout is a stock clearing resistance with conviction — but most \"breakouts\" fail. Here's how to separate the real ones from the fakeouts, using signals you can actually check.",
    },
    "every-sp500-stock-scored": {
        "title": "We scored every S&P 500 stock — what the data reveals (2026)",
        "desc": "We run all 500+ S&P 500 names through a 6-pillar quantitative model every day. Here's what scoring the entire index at once reveals about score distribution, sector leadership, and where the upside actually is.",
        "h1": "We scored every S&P 500 stock — here's what the data reveals",
        "lede": "Most analysis looks at one stock at a time. We score the whole index at once, every day. Seeing 500+ names through the same lens surfaces patterns you can't spot one chart at a time.",
    },
}


def _pillar_body(slug: str, site_origin: str) -> str:
    """Return the long-form HTML body for one pillar page."""
    crumbs = '<div class="crumbs"><a href="/">Home</a> · <a href="/learn">Learn</a></div>'
    if slug == "pop-score":
        body = f"""
{crumbs}
<span class="tag">Methodology</span>
<h1>What is the Alpha Score?</h1>
<p class="lede">A 0-100 composite that turns five different stock signals into one number — so you can stop juggling P/E, EPS revisions, momentum charts, and analyst ratings in your head.</p>

<h2>The five inputs</h2>
<p>Every Alpha Score is built from five weighted components. Each one is normalized to 0-100 inside its own peer group, so a small-cap quantum stock and a mega-cap chipmaker are graded against their own kind.</p>
<ul>
  <li><strong>Fundamentals (30%)</strong> — revenue growth, gross margins, free cash flow trajectory, return on capital. The bones of the business.</li>
  <li><strong>Valuation (20%)</strong> — forward P/E, PEG, EV/Sales, all benchmarked to the stock's own sub-sector median. Cheap-vs-fair-vs-premium.</li>
  <li><strong>Momentum (20%)</strong> — 1-month and 3-month price action, plus relative strength versus sector. Catches stocks the market is already re-rating.</li>
  <li><strong>Analyst signal (15%)</strong> — consensus rating, target-price upside, EPS revision trend. The wisdom of the wallet-street crowd, weighted by direction-of-change.</li>
  <li><strong>Macro / regime (15%)</strong> — adjusts the score based on whether the broader market is bullish, mixed, or bearish. A 75-score in a bear regime is more meaningful than a 75 in a melt-up.</li>
</ul>

<h2>How to read the number</h2>
<blockquote>The Alpha Score isn't a buy signal. It's a "homework checklist completed" signal. It tells you the stock's quantitative story is good. The qualitative judgment — does the thesis make sense to <em>you</em>? — is still yours.</blockquote>
<ul>
  <li><strong>80-100 — ★★★★★ Top Tier zone.</strong> All five inputs are firing. These are the names TickerMover features in the Hot List.</li>
  <li><strong>65-79 — ★★★★ Quality.</strong> Solid composite with at least one minor concern (usually valuation or momentum).</li>
  <li><strong>50-64 — ★★★ Average.</strong> Mixed signals. Often a transition stock — improving fundamentals but lagging momentum, or vice versa.</li>
  <li><strong>35-49 — ★★ Below Average.</strong> Two or more components are weakening.</li>
  <li><strong>0-34 — ★ Weak.</strong> Broad-based weakness across the score components.</li>
</ul>

<h2>Smart Score vs raw Alpha Score</h2>
<p>You'll sometimes see two numbers — a raw Alpha Score and a Smart Score. The Smart Score is the same composite, but adjusted for the current market regime. In a bullish regime, the Smart Score tilts toward growth and momentum. In a bearish regime, it tilts toward quality, balance-sheet strength, and valuation discipline. The raw Alpha Score is regime-blind; the Smart Score adapts.</p>

<h2>The conflict flag</h2>
<p>Numbers can lie when they're averaged. A stock can earn a 78 Alpha Score because four components are strong — but if the fifth is screaming "danger" (insider selling spike, EPS estimate crash, margin collapse), TickerMover flags it with a <code>caution</code> badge and rewrites the bottom-line verdict accordingly. <a href="/learn/how-to-read-fundamentals">More on how we read each signal here.</a></p>

<h2>What it doesn't capture</h2>
<p>Alpha Score is a quantitative framework. It cannot price in: regulatory risk, executive turnover, accounting irregularities, fraud, geopolitical exposure, or anything that isn't in the public filings yet. Treat it as the starting point of your research, not the conclusion.</p>

{cta_block("See live Alpha Scores")}
{newsletter_block("learn-pop-score")}
<div class="legal">TickerMover is a research tool, not financial advice. Alpha Score is a composite signal — always do your own research before investing.</div>
"""
    elif slug == "how-to-find-breakout-stocks":
        body = f"""
{crumbs}
<span class="tag">Strategy</span>
<h1>How to find breakout stocks</h1>
<p class="lede">A breakout is a stock clearing resistance with conviction — but most "breakouts" fail. Here's how to separate the real ones from the fakeouts, using signals you can actually check.</p>

<h2>What a breakout actually is</h2>
<p>A breakout happens when a stock pushes through a level that previously capped it — a prior high, a consolidation range, or a chart base — on rising participation. The thesis is simple: once the overhead supply is gone, the path of least resistance is up. The catch is that price clearing a line is necessary but not sufficient. Without the right supporting signals, you're buying a fakeout.</p>

<h2>The 6-point breakout checklist</h2>
<ul>
  <li><strong>Relative strength.</strong> Is the stock outperforming the S&amp;P 500 over the last 1&ndash;3 months? Real breakouts come from leaders, not laggards. A stock making a new high while the index chops is a strong tell.</li>
  <li><strong>Volume confirmation.</strong> The breakout bar should show above-average volume. Price up on light volume is a shrug; price up on heavy volume is conviction.</li>
  <li><strong>A proper base.</strong> The best breakouts emerge from weeks of tight consolidation, not a vertical spike. Tight, orderly bases mean supply and demand are balanced, so the break resolves cleanly.</li>
  <li><strong>Room to run.</strong> Check the distance to the analyst consensus target and the prior high. A stock breaking out with 20%+ of headroom has somewhere to go; one already above its target is extended.</li>
  <li><strong>Fundamental support.</strong> Momentum without earnings is a candle in the wind. The most durable breakouts pair the chart with accelerating revenue or an EPS surprise — the news that justifies the re-rating.</li>
  <li><strong>Not already parabolic.</strong> A stock up 120% in three months "breaking out" again is usually late, not early. The best entries are early in the move, not after the crowd has piled in.</li>
</ul>

<h2>The three traps</h2>
<p><strong>The fakeout</strong> — price clears the line, then snaps back below it within days, usually on weak volume. <strong>The exhaustion gap</strong> — a breakout so late and so vertical it marks the top, not the start. <strong>Chasing</strong> — buying 15% above the breakout level, where your risk is huge and your edge is gone. The discipline is to act early, on confirmation, with a level where you know you're wrong.</p>

<h2>How TickerMover surfaces breakouts</h2>
<p>Rather than scan charts by hand, TickerMover scores all 540+ US large-caps across six research pillars — momentum, quality, growth, valuation, sentiment and risk — and the <a href="/learn/pop-score">Alpha Score</a> blends them into one 0&ndash;100 number. The Breakout Picks lens then ranks for exactly the combination above: a strong score, real upside left to the target, and momentum still building, while pushing already-parabolic, above-target names down. It's a research starting point, not a recommendation.</p>

{cta_block("See today's Breakout Picks")}
{newsletter_block("learn-breakouts")}
<div class="legal">TickerMover is a research tool, not financial advice, and is not authorised or regulated by the FCA. Breakout signals can and do fail — always do your own research before investing.</div>
"""
    elif slug == "every-sp500-stock-scored":
        body = f"""
{crumbs}
<span class="tag">Data study</span>
<h1>We scored every S&P 500 stock — here's what the data reveals</h1>
<p class="lede">Most analysis looks at one stock at a time. We score the whole index at once, every day — 540+ US large-caps through the same six-pillar lens. Seeing every name together surfaces patterns you can't spot one chart at a time.</p>

<h2>The method, in one paragraph</h2>
<p>Every stock gets an <a href="/learn/pop-score">Alpha Score</a> from 0&ndash;100, built from six research pillars — momentum, quality, growth, valuation, sentiment and risk — each normalised inside the stock's own peer group. Normalising within peers matters: a mega-cap chipmaker is graded against other chipmakers, not against a utility. The result is one number per stock, all measured on the same ruler, refreshed through US market hours.</p>

<h2>1. The distribution has fat, interesting tails</h2>
<p>Because each pillar is normalised, the bulk of the index clusters in the middle of the range — most large-caps are, by definition, average large-caps. The story is in the tails. The top decile is where momentum, quality and growth line up at the same time; the bottom decile is where two or more pillars are breaking down together. The middle is noise; the edges are signal. <a href="/reports">See the live distribution across every scored stock &rarr;</a></p>

<h2>2. Leadership is concentrated — and it rotates</h2>
<p>At any moment, the high scorers are not spread evenly across the market. They cluster — in whichever sectors the tape is re-rating right now. Six months later the cluster has often moved. Scoring the whole index makes that rotation visible in a way single-stock research never does: you can watch leadership migrate sector to sector. <a href="/sectors">Browse the current sector leaderboard &rarr;</a></p>

<h2>3. A high score is not the same as high upside</h2>
<p>The most counter-intuitive finding: the highest-scoring stocks often have the <em>least</em> room left to run. A name can earn a 90 because momentum, quality and growth are all firing — but if it's already well above its analyst target and up triple digits in three months, the easy money is behind it. That's why our Breakout Picks lens ranks for <em>headroom</em> — strong score <em>plus</em> real upside to target <em>plus</em> momentum still building — rather than raw score. <a href="/learn/how-to-find-breakout-stocks">More on that here &rarr;</a></p>

<h2>4. The pillars disagree more than you'd think</h2>
<p>The single best use of scoring everything at once is spotting <em>conflict</em>: strong momentum but collapsing margins; a cheap valuation but deteriorating sentiment; great growth but an insider-selling spike. Averaged into one number, those tensions hide — so the model flags them separately. The disagreements are usually where the real research begins.</p>

<h2>What this can't tell you</h2>
<p>A quantitative score is a starting point, not a verdict. It can't price in a pending regulatory decision, an accounting irregularity, a key-person departure, or a shock that isn't in the public data yet. Treat the score as the question, not the answer.</p>

{cta_block("Explore every scored S&P 500 stock")}
{newsletter_block("learn-sp500-study")}
<div class="legal">TickerMover is a research tool, not financial advice, and is not authorised or regulated by the FCA. Scores are quantitative research signals, not recommendations — always do your own research before investing.</div>
"""
    elif slug == "reverse-dcf":
        body = f"""
{crumbs}
<span class="tag">Valuation</span>
<h1>Reverse DCF — what growth is the market pricing in?</h1>
<p class="lede">A traditional DCF asks "what is this stock worth?". A Reverse DCF asks the more useful question: "what does the market already believe about this company's future, and is that believable?"</p>

<h2>The trick in one sentence</h2>
<p>Take today's market cap, hold the discount rate and terminal multiple constant, and solve for the only unknown: the 10-year revenue CAGR that justifies the current price. That's the implied growth rate. If it's higher than what the company has ever delivered, the stock is priced for a miracle.</p>

<h2>Worked example</h2>
<blockquote>If a stock trades at $100 with a market cap of $50B and the Reverse DCF spits out an implied 10-year revenue CAGR of <strong>+38%</strong>, the market is saying: "we believe this company will grow revenue 38% per year for a decade." For most companies, that's a bet, not a forecast. For some — say, an early-stage AI infrastructure leader — it might be conservative.</blockquote>

<h2>How TickerMover categorizes the verdict</h2>
<ul>
  <li><strong>Implied CAGR &lt; 8%</strong> — the market is pricing in mature-company growth. Often a value setup if you believe the business can re-accelerate.</li>
  <li><strong>8-15%</strong> — fair-to-modest growth assumptions. Most established large caps land here.</li>
  <li><strong>15-25%</strong> — premium growth expectations. The company needs to keep delivering above-trend results just to justify today's price.</li>
  <li><strong>25-40%</strong> — exceptional growth baked in. This is the "show me" zone — the company is on the hook for meaningful out-performance.</li>
  <li><strong>&gt; 40%</strong> — heroic assumptions. Either the market knows something we don't, or it's getting ahead of itself.</li>
</ul>

<h2>What it tells you that a P/E ratio doesn't</h2>
<p>A P/E of 60× is meaningless without context. 60× could be cheap for a company growing 50% per year and expensive for one growing 10%. The Reverse DCF removes the ambiguity by translating the multiple into a growth assumption you can argue with. You stop asking "is 60× expensive?" and start asking "do I believe this company can grow 30% per year for a decade?". That's a much more useful question.</p>

<h2>What it doesn't capture</h2>
<p>Reverse DCF assumes margins, tax rates, and capital intensity stay roughly constant. For companies undergoing big margin shifts (early-stage SaaS scaling to profitability, hardware companies losing pricing power), the implied growth read can be misleading. Use it together with the Alpha Score's <a href="/learn/pop-score">Fundamentals component</a>, not in isolation.</p>

{cta_block("See Reverse DCF on every stock")}
{newsletter_block("learn-reverse-dcf")}
<div class="legal">TickerMover is a research tool, not financial advice. Reverse DCF is one input among many — verify the assumptions on each company you analyse.</div>
"""
    elif slug == "how-to-read-fundamentals":
        body = f"""
{crumbs}
<span class="tag">Beginner</span>
<h1>How to read US stock fundamentals (without a finance degree)</h1>
<p class="lede">You don't need a CFA to spot a great business. You need seven numbers, in the right order, and a sense for what "good" looks like in each. Here's the checklist we use on TickerMover.</p>

<h2>1 · Revenue growth (year over year)</h2>
<p>The single most important question you can ask: <em>is the top line getting bigger?</em> Look at the most recent quarter's revenue versus the same quarter last year. For mature large-caps, anything above 10% is healthy. For high-growth names, you want to see 25% or more. A two-quarter deceleration is a yellow flag.</p>

<h2>2 · Gross margin trend</h2>
<p>Revenue growth means little if the company is buying that growth at a loss. Gross margin (revenue minus cost of goods, divided by revenue) tells you how much pricing power the business has. Software companies often run 70-85% gross margins; hardware companies 30-50%. What matters most is the <em>trend</em> — flat or expanding margins are fine, contracting margins on growing revenue is a quiet warning.</p>

<h2>3 · Free cash flow</h2>
<p>Net income can be massaged. Free cash flow (operating cash flow minus capex) is much harder to fake — it's the actual cash the business produces. Look at trailing-12-month FCF and the FCF margin (FCF / revenue). For mature businesses, 15-25% FCF margins are excellent. For early-stage growth companies, a clear path to positive FCF within 2-3 years is the bar.</p>

<h2>4 · Valuation versus growth</h2>
<p>Forget P/E by itself. Use PEG (P/E divided by growth rate), or even better, the <a href="/learn/reverse-dcf">Reverse DCF implied CAGR</a>. A 50× P/E on a stock growing 40% (PEG ~1.25) is more attractive than a 20× P/E on a stock growing 5% (PEG 4.0). Multiples are a function of growth, not a verdict on it.</p>

<h2>5 · Balance sheet strength</h2>
<p>Two ratios matter: net debt / EBITDA (how many years of operating profit it would take to pay off the debt — under 2× is comfortable for most companies, under 1× is great), and the current ratio (current assets / current liabilities — above 1.5 is healthy). Companies with weak balance sheets get punished disproportionately in downturns.</p>

<h2>6 · Momentum and relative strength</h2>
<p>The market is forward-looking. A stock outperforming its sector for 3-6 months usually means analysts and institutional money are seeing something in the numbers that hasn't fully shown up in the public commentary yet. We treat 1-month and 3-month price action as a confirming signal — never the lead signal.</p>

<h2>7 · Conviction — does the thesis make sense?</h2>
<p>Numbers tell you what's true. The thesis tells you whether it'll keep being true. Before you buy, write down in one sentence why this company will be bigger and more profitable in five years than it is today. If you can't, you're trading the chart, not the business.</p>

<h2>How TickerMover does this for you</h2>
<p>Every stock in our universe is run through this checklist every five minutes during market hours. The output is the <a href="/learn/pop-score">Alpha Score</a> — a single 0-100 composite that bakes in all seven signals plus a regime adjustment. You can drill into the underlying components on any stock's detail page.</p>

{cta_block("Open the dashboard and try it")}
{newsletter_block("learn-fundamentals")}
<div class="legal">TickerMover is a research tool, not financial advice. The fundamentals checklist is a starting framework — adapt it to the specific industry and company you're studying.</div>
"""
    else:
        return ""

    return f'<div class="wrap">{body}</div>'


def render_pillar(slug: str, site_origin: str) -> Optional[str]:
    """Render a single pillar page; return None if slug is unknown."""
    meta = PILLARS.get(slug)
    if not meta:
        return None
    canonical = f"{site_origin}/learn/{slug}"
    schema = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": meta["title"][:110],
        "description": meta["desc"],
        "url": canonical,
        "image": f"{site_origin}/static/icons/icon-512.png",
        "author": {"@type": "Organization", "name": "TickerMover", "url": site_origin},
        "publisher": {
            "@type": "Organization", "name": "TickerMover",
            "logo": {"@type": "ImageObject", "url": f"{site_origin}/static/icons/icon-512.png"},
        },
    }
    breadcrumb = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": site_origin},
            {"@type": "ListItem", "position": 2, "name": "Learn", "item": f"{site_origin}/learn"},
            {"@type": "ListItem", "position": 3, "name": meta["h1"], "item": canonical},
        ],
    }
    schema_json = (
        _json.dumps(schema, separators=(",", ":")) + '</script>\n<script type="application/ld+json">'
        + _json.dumps(breadcrumb, separators=(",", ":"))
    )
    body_html = f'<div class="wrap">{brand_header()}</div>{_pillar_body(slug, site_origin)}'
    return page_shell(
        title=meta["title"], desc=meta["desc"], canonical=canonical,
        body_html=body_html, schema_json=schema_json,
        og_image=f"{site_origin}/static/icons/icon-512.png",
    )


def render_pillar_index(site_origin: str) -> str:
    """The /learn hub page — links to all pillar pages."""
    cards = "".join(
        f'<div class="card"><a href="/learn/{slug}"><div class="ttl">{m["h1"]}</div>'
        f'<div class="sub">{m["desc"][:140]}</div></a></div>'
        for slug, m in PILLARS.items()
    )
    body = f"""
<div class="wrap">
  {brand_header()}
  <div class="crumbs"><a href="/">Home</a> · Learn</div>
  <h1>Learn</h1>
  <p class="lede">Plain-English guides to the methodology behind TickerMover — the Alpha Score, the Reverse DCF, and how to read US stock fundamentals.</p>
  <div class="cards">{cards}</div>
  {newsletter_block("learn-index")}
  <div class="legal">TickerMover — research, not advice.</div>
</div>"""
    canonical = f"{site_origin}/learn"
    return page_shell(
        title="Learn — TickerMover methodology, Alpha Score, Reverse DCF",
        desc="Plain-English guides to TickerMover's stock-research methodology — Alpha Score, Reverse DCF, and the seven-step fundamentals checklist.",
        canonical=canonical, body_html=body,
        og_image=f"{site_origin}/static/icons/icon-512.png",
    )


# ─── Sector landing pages ─────────────────────────────────────────────

def _stock_row(t: dict) -> str:
    sym = (t.get("ticker") or "").upper()
    name = (t.get("name") or sym)[:30]
    grade = t.get("grade") or "—"
    pop = t.get("smart_score") if t.get("smart_score") is not None else t.get("pop_score")
    try:
        pop_n = round(float(pop)) if pop is not None else "—"
    except (TypeError, ValueError):
        pop_n = "—"
    bl = (t.get("bottom_line_ai") or t.get("bottom_line") or "")[:130]
    grade_class = grade if grade in ("A", "B", "C", "D", "F") else ""
    return (
        f'<tr><td><a href="/stocks/{sym}" class="tk">{sym}</a><br>'
        f'<span style="font-size:12.5px;color:#64748b">{name}</span></td>'
        f'<td><span class="grade {grade_class}">{grade}</span></td>'
        f'<td class="pop">{pop_n}</td>'
        f'<td class="vd">{bl}</td></tr>'
    )


def render_sector(slug: str, universe: list[dict], site_origin: str) -> Optional[str]:
    """Render a single sector landing page."""
    smap = sector_slugs(universe)
    label = smap.get(slug)
    if not label:
        return None
    rows = [
        t for t in (universe or [])
        if slugify(t.get("sub_sector") or t.get("subsector") or t.get("sector") or "") == slug
    ]
    # Sort by Alpha Score descending
    def _score(t: dict) -> float:
        s = t.get("smart_score") if t.get("smart_score") is not None else t.get("pop_score")
        try:
            return float(s or 0)
        except (TypeError, ValueError):
            return 0.0
    rows.sort(key=_score, reverse=True)
    table_html = "".join(_stock_row(t) for t in rows[:50])
    n = len(rows)
    canonical = f"{site_origin}/sectors/{slug}"
    title = f"Best {label} stocks — live Alpha Scores | TickerMover"
    desc = (
        f"TickerMover's live ranking of {n} {label} stocks by Alpha Score. "
        f"Plain-English verdict, grade, and bottom line for each. Updated every 5 minutes."
    )[:160]
    schema = {
        "@context": "https://schema.org",
        "@type": "ItemList",
        "name": f"Best {label} stocks",
        "description": desc,
        "url": canonical,
        "numberOfItems": min(n, 50),
        "itemListElement": [
            {
                "@type": "ListItem", "position": i + 1,
                "url": f"{site_origin}/stocks/{(t.get('ticker') or '').upper()}",
                "name": (t.get("name") or t.get("ticker") or ""),
            }
            for i, t in enumerate(rows[:20])
        ],
    }
    breadcrumb = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": site_origin},
            {"@type": "ListItem", "position": 2, "name": "Sectors", "item": f"{site_origin}/sectors"},
            {"@type": "ListItem", "position": 3, "name": label, "item": canonical},
        ],
    }
    schema_json = (
        _json.dumps(schema, separators=(",", ":")) + '</script>\n<script type="application/ld+json">'
        + _json.dumps(breadcrumb, separators=(",", ":"))
    )
    body = f"""
<div class="wrap-wide">
  {brand_header()}
  <div class="crumbs"><a href="/">Home</a> · <a href="/sectors">Sectors</a> · {label}</div>
  <h1>Best {label} stocks</h1>
  <p class="lede">Live ranking of {n} {label} stocks by TickerMover Alpha Score — a 0-100 composite of fundamentals, valuation, momentum, analyst signal, and macro regime. Click any ticker for the full breakdown.</p>
  <table class="tbl">
    <thead><tr><th>Ticker</th><th>Grade</th><th>Score</th><th>Bottom line</th></tr></thead>
    <tbody>{table_html or '<tr><td colspan="4">No stocks scored in this sector yet — the universe is warming up.</td></tr>'}</tbody>
  </table>
  <p style="font-size:13px;color:#64748b">Alpha Scores update every 5 minutes during US market hours. Grades: <strong>A</strong> Top Tier · <strong>B</strong> Quality · <strong>C</strong> Average · <strong>D</strong> Below Avg · <strong>F</strong> Weak. (Quality descriptors, not buy/sell recommendations.)</p>
  {cta_block("See the full live dashboard")}
  {newsletter_block("sector-" + slug)}
  <div class="legal">TickerMover is a research tool, not financial advice. Always do your own research before investing.</div>
</div>"""
    return page_shell(
        title=title, desc=desc, canonical=canonical, body_html=body,
        schema_json=schema_json,
        og_image=f"{site_origin}/static/icons/icon-512.png",
    )


def render_sector_index(universe: list[dict], site_origin: str) -> str:
    smap = sector_slugs(universe)
    items = sorted(smap.items(), key=lambda kv: kv[1].lower())
    cards = "".join(
        f'<div class="card"><a href="/sectors/{slug}"><div class="ttl">{label}</div>'
        f'<div class="sub">View live Alpha Scores for stocks in this sector.</div></a></div>'
        for slug, label in items
    )
    body = f"""
<div class="wrap">
  {brand_header()}
  <div class="crumbs"><a href="/">Home</a> · Sectors</div>
  <h1>Sectors</h1>
  <p class="lede">{len(smap)} sub-sectors covered, all scored on the same 0-100 Alpha Score. Pick one to see a live ranking.</p>
  <div class="cards">{cards}</div>
  {newsletter_block("sectors-index")}
  <div class="legal">TickerMover — research, not advice.</div>
</div>"""
    canonical = f"{site_origin}/sectors"
    return page_shell(
        title="Stock sectors — live Alpha Scores by sub-sector | TickerMover",
        desc="Browse TickerMover's stock universe by sub-sector — AI semiconductors, cybersecurity, quantum computing, photonics, and more. Live Alpha Scores updated every 5 minutes.",
        canonical=canonical, body_html=body,
        og_image=f"{site_origin}/static/icons/icon-512.png",
    )


# ─── Comparison pages ─────────────────────────────────────────────────

# Curated high-traffic head-to-head comparisons added to the sitemap.
# These are the queries with real search volume; users can hit any
# /compare/{A}-vs-{B} URL but only these show up in sitemap.xml.
FEATURED_COMPARISONS: list[tuple[str, str]] = [
    ("NVDA", "AMD"), ("AAPL", "MSFT"), ("GOOGL", "META"),
    ("PLTR", "AI"),  ("IONQ", "RGTI"), ("AMD", "INTC"),
    ("CRWD", "PANW"), ("TSLA", "NVDA"), ("MU", "WDC"),
    ("AVGO", "MRVL"), ("SMCI", "DELL"), ("COHR", "LITE"),
    ("ASML", "AMAT"), ("CRWV", "APLD"),
]


def _cmp_card(t: dict) -> str:
    sym = (t.get("ticker") or "").upper()
    name = t.get("name") or sym
    grade = t.get("grade") or "—"
    pop = t.get("smart_score") if t.get("smart_score") is not None else t.get("pop_score")
    try:
        pop_n = round(float(pop)) if pop is not None else "—"
    except (TypeError, ValueError):
        pop_n = "—"
    rating = {"A": "★★★★★ Top Tier", "B": "★★★★ Quality", "C": "★★★ Average", "D": "★★ Below Avg", "F": "★ Weak"}.get(grade, "Under Review")
    price = t.get("price")
    try:
        price_str = f"${float(price):.2f}" if price else "—"
    except (TypeError, ValueError):
        price_str = "—"
    rev_g = t.get("revenue_growth_yoy")
    rev_g_str = f"{float(rev_g)*100:+.1f}%" if rev_g is not None else "—"
    pe = t.get("pe_ratio") or t.get("forward_pe")
    pe_str = f"{float(pe):.1f}×" if pe and float(pe) > 0 else "—"
    mom = t.get("momentum_1m")
    mom_str = f"{float(mom):+.1f}%" if mom is not None else "—"
    upside = t.get("target_upside_pct")
    upside_str = f"{float(upside):+.1f}%" if upside is not None else "—"
    bl = (t.get("bottom_line_ai") or t.get("bottom_line") or "")[:200]
    return f"""
<div class="cmp-card">
  <a href="/stocks/{sym}" style="text-decoration:none">
    <div class="tk">{sym}</div>
    <div class="nm">{name}</div>
  </a>
  <div class="cmp-row"><span class="k">Alpha Score</span><span class="v">{pop_n}/100</span></div>
  <div class="cmp-row"><span class="k">Grade</span><span class="v">{grade} · {rating}</span></div>
  <div class="cmp-row"><span class="k">Price</span><span class="v">{price_str}</span></div>
  <div class="cmp-row"><span class="k">Rev growth (YoY)</span><span class="v">{rev_g_str}</span></div>
  <div class="cmp-row"><span class="k">Forward P/E</span><span class="v">{pe_str}</span></div>
  <div class="cmp-row"><span class="k">1-mo momentum</span><span class="v">{mom_str}</span></div>
  <div class="cmp-row"><span class="k">Analyst upside</span><span class="v">{upside_str}</span></div>
  <div style="margin-top:14px;padding-top:12px;border-top:1px solid #f1f5f9;font-size:13.5px;color:#475569;line-height:1.55">{bl}</div>
</div>
"""


def render_comparison(a: str, b: str, universe: list[dict], site_origin: str) -> Optional[str]:
    a, b = a.upper(), b.upper()
    if a == b:
        return None
    lookup = {(t.get("ticker") or "").upper(): t for t in (universe or [])}
    ta, tb = lookup.get(a), lookup.get(b)
    if not ta or not tb:
        return None
    name_a = (ta.get("name") or a)
    name_b = (tb.get("name") or b)
    canonical = f"{site_origin}/compare/{a}-vs-{b}"
    # Compute the verdict — which one wins on Alpha Score
    pa = ta.get("smart_score") if ta.get("smart_score") is not None else ta.get("pop_score") or 0
    pb = tb.get("smart_score") if tb.get("smart_score") is not None else tb.get("pop_score") or 0
    try:
        pa_f, pb_f = float(pa or 0), float(pb or 0)
    except (TypeError, ValueError):
        pa_f, pb_f = 0.0, 0.0
    if abs(pa_f - pb_f) < 3:
        verdict = f"{a} and {b} score within 3 points of each other on Alpha Score — effectively tied. The right pick depends on which thesis you find more compelling."
    elif pa_f > pb_f:
        verdict = f"{a} edges out {b} on TickerMover's composite Alpha Score ({round(pa_f)} vs {round(pb_f)}). The breakdown below shows where each stock leads."
    else:
        verdict = f"{b} edges out {a} on TickerMover's composite Alpha Score ({round(pb_f)} vs {round(pa_f)}). The breakdown below shows where each stock leads."
    title = f"{a} vs {b} — Alpha Score, valuation, growth compared | TickerMover"
    desc = (
        f"Side-by-side comparison of {a} ({name_a[:24]}) and {b} ({name_b[:24]}) — "
        f"Alpha Score, growth, valuation, momentum and analyst upside. Updated every 5 minutes."
    )[:160]
    body = f"""
<div class="wrap-wide">
  {brand_header()}
  <div class="crumbs"><a href="/">Home</a> · <a href="/compare">Compare</a> · {a} vs {b}</div>
  <h1><span class="sym">{a}</span> vs <span class="sym">{b}</span></h1>
  <p class="lede">{verdict}</p>
  <div class="cmp-grid">
    {_cmp_card(ta)}
    {_cmp_card(tb)}
  </div>
  <h2>How Alpha scores both</h2>
  <p>The Alpha Score blends fundamentals, valuation, momentum, analyst signal and macro regime into one 0-100 number. <a href="/learn/pop-score">Read the methodology →</a></p>
  <p>For deeper analysis on each name, open the live dashboard: <a href="/stocks/{a}">{a} full breakdown</a> · <a href="/stocks/{b}">{b} full breakdown</a>.</p>
  {cta_block("Open the live dashboard")}
  {newsletter_block(f"compare-{a}-{b}")}
  <div class="legal">TickerMover is a research tool, not financial advice. Comparisons are computed from live universe data and refresh every 5 minutes during market hours.</div>
</div>"""
    schema = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": f"{a} vs {b} — stock comparison",
        "description": desc,
        "url": canonical,
        "image": f"{site_origin}/static/icons/icon-512.png",
        "author": {"@type": "Organization", "name": "TickerMover", "url": site_origin},
        "publisher": {
            "@type": "Organization", "name": "TickerMover",
            "logo": {"@type": "ImageObject", "url": f"{site_origin}/static/icons/icon-512.png"},
        },
        "about": [
            {"@type": "Corporation", "name": name_a, "tickerSymbol": a},
            {"@type": "Corporation", "name": name_b, "tickerSymbol": b},
        ],
    }
    schema_json = _json.dumps(schema, separators=(",", ":"))
    return page_shell(
        title=title, desc=desc, canonical=canonical, body_html=body,
        schema_json=schema_json,
        og_image=f"{site_origin}/static/icons/icon-512.png",
    )


def render_compare_index(universe: list[dict], site_origin: str) -> str:
    """The /compare hub — links to featured comparisons."""
    lookup = {(t.get("ticker") or "").upper() for t in (universe or [])}
    valid = [(a, b) for a, b in FEATURED_COMPARISONS if a in lookup and b in lookup]
    cards = "".join(
        f'<div class="card"><a href="/compare/{a}-vs-{b}"><div class="ttl">{a} vs {b}</div>'
        f'<div class="sub">Side-by-side Alpha Score, growth, valuation and momentum.</div></a></div>'
        for a, b in valid
    )
    body = f"""
<div class="wrap">
  {brand_header()}
  <div class="crumbs"><a href="/">Home</a> · Compare</div>
  <h1>Stock comparisons</h1>
  <p class="lede">Curated head-to-head pages for the most-asked-about US stocks. Alpha Score, growth, valuation and analyst signal — all on one page. You can also build any comparison by visiting <code>/compare/&lt;TICKER1&gt;-vs-&lt;TICKER2&gt;</code>.</p>
  <div class="cards">{cards}</div>
  {newsletter_block("compare-index")}
  <div class="legal">TickerMover — research, not advice.</div>
</div>"""
    canonical = f"{site_origin}/compare"
    return page_shell(
        title="Stock comparisons — NVDA vs AMD, AAPL vs MSFT, and more | TickerMover",
        desc="Side-by-side US stock comparisons — Alpha Score, growth, valuation, analyst upside. Curated head-to-head pages updated every 5 minutes.",
        canonical=canonical, body_html=body,
        og_image=f"{site_origin}/static/icons/icon-512.png",
    )
