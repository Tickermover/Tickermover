"""
seo_pages.py — SEO Phase 3 page renderers for TickerMover.

Houses the long-form HTML renderers for:
  • /learn/{slug}              — evergreen educational pillar pages
  • /learn                     — pillar index (hub-and-spoke for crawlers)
  • /sectors/{slug}            — one page per sub-sector with live Quant Scores
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
import html
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
    import sector_intel as _si
    out: dict[str, str] = {}
    for t in universe or []:
        # MUST match sector_intel's grouping exactly. This used to fall back
        # sub_sector -> subsector -> sector, skipping `industry`, while the
        # index is built from sector_intel which prefers industry over sector.
        # Every sector labelled from `industry` — the large majority — was
        # therefore listed on /sectors and 404'd when clicked.
        sub = _si.bucket_of(t) or ""
        if sub and slugify(sub) not in out:
            out[slugify(sub)] = sub
    return out


# ─── Shared chrome ────────────────────────────────────────────────────

import theme as _theme

_BASE_CSS = _theme.THEME_CSS

_FONTS_LINK = _theme.FONTS_LINK

def brand_header() -> str:
    """Deliberately empty. The wordmark now lives in the shared nav
    (theme.nav_html), so a body that also emitted it rendered TWO wordmarks
    stacked - visible on /sectors, one in the nav and one above the crumbs.
    Kept as a no-op rather than deleted: it is called from several body
    builders and this is the one-line fix that cannot miss a call site."""
    return ""


def newsletter_block(source: str, title: str = "", copy: str = "",
                    cta: str = "Subscribe") -> str:
    """Inline newsletter capture block. `source` is recorded server-side
    so we can attribute signups to the page they came from.

    `title`/`copy` let a page offer what that page is actually about. The
    generic "weekly Quant Score digest" is a reasonable ask under an article;
    under a head-to-head it ignores what the reader just came for."""
    safe = (source or "unknown").replace('"', "")
    title = title or "Get the weekly Quant Score digest"
    copy = copy or ("Top-rated US stocks, conflict alerts, and Reverse-DCF reads "
                    "&mdash; straight to your inbox every Sunday. Free.")
    return f"""
<div class="nl">
  <h3>{title}</h3>
  <p>{copy}</p>
  <form id="nl-{safe}" autocomplete="off">
    <input type="email" name="email" placeholder="you@email.com" required>
    <input type="text" name="company" class="nl-honey" tabindex="-1" autocomplete="off">
    <button type="submit">{cta}</button>
  </form>
  <div class="nl-msg" id="nl-msg-{safe}"></div>
  <div style="margin-top:14px;font-size:12.5px;color:#758696">
    Questions or feedback? Email <a href="mailto:support@tickermover.com" style="color:#14587D">support@tickermover.com</a>
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


def cta_block(label: str = "Open the live dashboard", href: str = "/app?signup=1",
              n: int | None = None) -> str:
    """`n` is the scored-universe count. These pages are returned raw, NOT
    through app._with_consent, so the `__UNIV_N__` token the rest of the site
    uses is never substituted here — which is why this box claimed "200+ US
    stocks" while the universe had grown to 545. Callers holding the universe
    pass the real number; the rest get copy that makes no numeric claim,
    because a vague sentence beats a confident wrong one."""
    count = f"{n} US stocks" if n else "Every stock we track"
    return f"""
<div class="cta">
  <div class="cta-brand">{_theme.wordmark(dark=True)}</div>
  <h3>Real research, free during beta</h3>
  <p>{count}, scored daily. Quant Score, conflict detection, Reverse DCF, peer comparison. We do the homework.</p>
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
    # front door, so channel attribution starts here. Same per-site Plausible
    # script as app.py:_analytics_snippet (see config.PLAUSIBLE_SCRIPT_ID;
    # set it to "off" to disable).
    import config as _cfg
    _sid = (getattr(_cfg, "PLAUSIBLE_SCRIPT_ID", "") or "").strip()
    analytics_tag = "" if (not _sid or _sid.lower() == "off") else (
        f'<script async src="https://plausible.io/js/{_sid}.js"></script>\n'
        '<script>window.plausible=window.plausible||function(){(plausible.q='
        'plausible.q||[]).push(arguments)},plausible.init=plausible.init||'
        'function(i){plausible.o=i||{}};plausible.init()</script>'
    )
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
{_theme.nav_html()}
{body_html}
{_theme.footer_html()}
</body>
</html>"""


# ─── Pillar pages ─────────────────────────────────────────────────────

PILLARS: dict[str, dict] = {
    "pop-score": {
        "title": "What is Quant Score? TickerMover's 0-100 stock rating explained",
        "desc": "Quant Score is TickerMover's 0-100 composite rating that blends fundamentals, momentum, valuation, analyst signal, and macro regime into a single plain-English verdict.",
        "h1": "What is the Quant Score?",
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
<h1>What is the Quant Score?</h1>
<p class="lede">A 0-100 composite that turns five different stock signals into one number — so you can stop juggling P/E, EPS revisions, momentum charts, and analyst ratings in your head.</p>

<h2>The five inputs</h2>
<p>Every Quant Score is built from five weighted components. Each one is normalized to 0-100 inside its own peer group, so a small-cap quantum stock and a mega-cap chipmaker are graded against their own kind.</p>
<ul>
  <li><strong>Fundamentals (30%)</strong> — revenue growth, gross margins, free cash flow trajectory, return on capital. The bones of the business.</li>
  <li><strong>Valuation (20%)</strong> — forward P/E, PEG, EV/Sales, all benchmarked to the stock's own sub-sector median. Cheap-vs-fair-vs-premium.</li>
  <li><strong>Momentum (20%)</strong> — 1-month and 3-month price action, plus relative strength versus sector. Catches stocks the market is already re-rating.</li>
  <li><strong>Analyst signal (15%)</strong> — consensus rating, target-price upside, EPS revision trend. The wisdom of the wallet-street crowd, weighted by direction-of-change.</li>
  <li><strong>Macro / regime (15%)</strong> — adjusts the score based on whether the broader market is bullish, mixed, or bearish. A 75-score in a bear regime is more meaningful than a 75 in a melt-up.</li>
</ul>

<h2>How to read the number</h2>
<blockquote>The Quant Score isn't a buy signal. It's a "homework checklist completed" signal. It tells you the stock's quantitative story is good. The qualitative judgment — does the thesis make sense to <em>you</em>? — is still yours.</blockquote>
<ul>
  <li><strong>80-100 — ★★★★★ Top Tier zone.</strong> All five inputs are firing. These are the names TickerMover features in the Hot List.</li>
  <li><strong>65-79 — ★★★★ Quality.</strong> Solid composite with at least one minor concern (usually valuation or momentum).</li>
  <li><strong>50-64 — ★★★ Average.</strong> Mixed signals. Often a transition stock — improving fundamentals but lagging momentum, or vice versa.</li>
  <li><strong>35-49 — ★★ Below Average.</strong> Two or more components are weakening.</li>
  <li><strong>0-34 — ★ Weak.</strong> Broad-based weakness across the score components.</li>
</ul>

<h2>Smart Score vs raw Quant Score</h2>
<p>You'll sometimes see two numbers — a raw Quant Score and a Smart Score. The Smart Score is the same composite, but adjusted for the current market regime. In a bullish regime, the Smart Score tilts toward growth and momentum. In a bearish regime, it tilts toward quality, balance-sheet strength, and valuation discipline. The raw Quant Score is regime-blind; the Smart Score adapts.</p>

<h2>The conflict flag</h2>
<p>Numbers can lie when they're averaged. A stock can earn a 78 Quant Score because four components are strong — but if the fifth is screaming "danger" (insider selling spike, EPS estimate crash, margin collapse), TickerMover flags it with a <code>caution</code> badge and rewrites the bottom-line verdict accordingly. <a href="/learn/how-to-read-fundamentals">More on how we read each signal here.</a></p>

<h2>What it doesn't capture</h2>
<p>Quant Score is a quantitative framework. It cannot price in: regulatory risk, executive turnover, accounting irregularities, fraud, geopolitical exposure, or anything that isn't in the public filings yet. Treat it as the starting point of your research, not the conclusion.</p>

{cta_block("See live Quant Scores")}
{newsletter_block("learn-pop-score")}
<div class="legal">TickerMover is a research tool, not financial advice. Quant Score is a composite signal — always do your own research before investing.</div>
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
<p>Rather than scan charts by hand, TickerMover scores all 540+ US large-caps across six research pillars — momentum, quality, growth, valuation, sentiment and risk — and the <a href="/learn/pop-score">Quant Score</a> blends them into one 0&ndash;100 number. The Breakout Picks lens then ranks for exactly the combination above: a strong score, real upside left to the target, and momentum still building, while pushing already-parabolic, above-target names down. It's a research starting point, not a recommendation.</p>

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
<p>Every stock gets an <a href="/learn/pop-score">Quant Score</a> from 0&ndash;100, built from six research pillars — momentum, quality, growth, valuation, sentiment and risk — each normalised inside the stock's own peer group. Normalising within peers matters: a mega-cap chipmaker is graded against other chipmakers, not against a utility. The result is one number per stock, all measured on the same ruler, refreshed through US market hours.</p>

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
<p>Reverse DCF assumes margins, tax rates, and capital intensity stay roughly constant. For companies undergoing big margin shifts (early-stage SaaS scaling to profitability, hardware companies losing pricing power), the implied growth read can be misleading. Use it together with the Quant Score's <a href="/learn/pop-score">Fundamentals component</a>, not in isolation.</p>

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
<p>Numbers tell you what's true. The thesis tells you whether it'll keep being true. Before taking a position, it's worth writing down in one sentence why this company might be bigger and more profitable in five years than it is today. If that sentence won't come, the case rests on the chart, not the business.</p>

<h2>How TickerMover does this for you</h2>
<p>Every stock in our universe is run through this checklist every five minutes during market hours. The output is the <a href="/learn/pop-score">Quant Score</a> — a single 0-100 composite that bakes in all seven signals plus a regime adjustment. You can drill into the underlying components on any stock's detail page.</p>

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
  <p class="lede">Plain-English guides to the methodology behind TickerMover — the Quant Score, the Reverse DCF, and how to read US stock fundamentals.</p>
  <div class="cards">{cards}</div>
  {newsletter_block("learn-index")}
  <div class="legal">TickerMover — research, not advice.</div>
</div>"""
    canonical = f"{site_origin}/learn"
    return page_shell(
        title="Learn — TickerMover methodology, Quant Score, Reverse DCF",
        schema_json=_json.dumps({
            "@context": "https://schema.org", "@type": "ItemList",
            "name": "TickerMover methodology guides",
            "url": canonical, "numberOfItems": len(PILLARS),
            "itemListElement": [
                {"@type": "ListItem", "position": i + 1,
                 "url": f"{site_origin}/learn/{sl}", "name": sl.replace("-", " ").title()}
                for i, sl in enumerate(PILLARS)
            ],
        }, separators=(",", ":")) + "</script>" + chr(10)
        + '<script type="application/ld+json">'
        + _json.dumps({
            "@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Home", "item": site_origin},
                {"@type": "ListItem", "position": 2, "name": "Learn", "item": canonical},
            ],
        }, separators=(",", ":")),
        desc="Plain-English guides to TickerMover's stock-research methodology — Quant Score, Reverse DCF, and the seven-step fundamentals checklist.",
        canonical=canonical, body_html=body,
        og_image=f"{site_origin}/static/icons/icon-512.png",
    )


# ─── Sector landing pages ─────────────────────────────────────────────

def _stock_row(t: dict, rank: int = 99) -> str:
    sym = (t.get("ticker") or "").upper()
    # Was [:30], which severed "MongoDB, Inc. Class A Common Stock" mid-word at
    # "Common S" with no ellipsis to show it had been cut. The cell now clips
    # with a real ellipsis in CSS, so this only needs to stop a pathological
    # name from bloating the HTML.
    name = (t.get("name") or sym)[:48]
    grade = t.get("grade") or "—"
    pop = t.get("smart_score") if t.get("smart_score") is not None else t.get("pop_score")
    try:
        pop_n = round(float(pop)) if pop is not None else "—"
    except (TypeError, ValueError):
        pop_n = "—"
    # NB: module-level `html`, not `_html` — that alias is bound inside one
    # other function only, and reading it here would NameError at request time
    # inside a try/except, silently emptying the column.
    bl = html.escape(t.get("bottom_line_ai") or t.get("bottom_line") or "")
    grade_class = grade if grade in ("A", "B", "C", "D", "F") else ""
    logo = ('<img class="sr-logo" src="https://assets.parqet.com/logos/symbol/'
            + sym + '" alt="" loading="lazy" width="26" height="26" '
            'onerror="this.remove()">')
    cell = ('<td class="' + ("sr-lead" if rank <= 3 else "sr-row")
            + '"><div class="sr-id">' + logo
            + '<div><a href="/stocks/' + sym + '" class="tk">' + sym + "</a><br>"
            + '<span class="sr-nm">' + name + "</span></div></div></td>")
    return (
        "<tr>" + cell
        + f'<td><span class="grade {grade_class}">{grade}</span></td>'
        + f'<td class="pop">{pop_n}</td>'
        + f'<td class="vd"><span>{bl}</span></td></tr>'
    )


def render_sector(slug: str, universe: list[dict], site_origin: str) -> Optional[str]:
    """Render a single sector landing page."""
    smap = sector_slugs(universe)
    label = smap.get(slug)
    if not label:
        return None
    import sector_intel as _si_mod
    rows = [
        t for t in (universe or [])
        if slugify(_si_mod.bucket_of(t) or "") == slug
    ]
    # Sort by Quant Score descending
    def _score(t: dict) -> float:
        s = t.get("smart_score") if t.get("smart_score") is not None else t.get("pop_score")
        try:
            return float(s or 0)
        except (TypeError, ValueError):
            return 0.0
    rows.sort(key=_score, reverse=True)
    table_html = "".join(_stock_row(t, i + 1) for i, t in enumerate(rows[:50]))
    n = len(rows)

    # ── Sector profile ───────────────────────────────────────────────
    # Added 15 Aug 2026. The page opened straight into a 50-row ranking, which
    # answers "which scores highest here" but never "what is this group like".
    # Those are different questions, and the second is the one that tells you
    # whether the ranking is even worth reading: a sub-sector whose names all
    # score alike is a sector call, one with a wide spread is a stock-picking
    # problem. Every figure is measured, and each is shown against the whole
    # scored universe so it can be judged rather than just noted.
    profile_html = ""
    try:
        import sector_intel as _si
        prof = _si.one_sector(slug, universe)
        base = _si.universe_baseline(universe)
        if prof:
            def _stat(lbl, val, base_val, suffix="", signed=False):
                if val is None:
                    return ""
                v = ("+" if signed and val >= 0 else "") + str(val) + suffix
                bv = ("" if base_val is None else
                      ("universe " + ("+" if signed and base_val >= 0 else "")
                       + str(base_val) + suffix))
                return ('<div class="sp-stat"><div class="sp-k">' + lbl
                        + '</div><div class="sp-v">' + v
                        + '</div><div class="sp-b">' + bv + "</div></div>")

            spread = prof["alpha_spread"]
            read = ""
            if spread is not None and base["alpha_spread"] is not None:
                if spread <= base["alpha_spread"] * 0.7:
                    read = ("These names score closely together — the group moves more "
                            "as a block than as individual stories.")
                elif spread >= base["alpha_spread"] * 1.3:
                    read = ("Scores are widely spread here — which name you look at "
                            "matters more than the sub-sector itself.")
                else:
                    read = "Scores are spread about as widely as the universe overall."
            # AI read — CACHE PEEK ONLY. A page render must never block on a
            # model call or pay for one: this page is public, anonymous and
            # crawled, so generating here would bill us per visitor and per
            # bot. The note is produced by the in-app panel hitting
            # /api/sector-intel/{slug}?note=true, and appears here for free
            # once it exists. Absent, the page is simply the stats, which are
            # the substance anyway.
            ai_html = ""
            try:
                import sector_narrative as _sn
                _note = _sn.cached_note(prof)
                if _note:
                    ai_html = ('<p class="sp-ai"><span>Our read</span>' + _note + "</p>")
            except Exception:
                ai_html = ""

            profile_html = (
                '<div class="sp-grid">'
                + _stat("Names scored", prof["count"], base["count"])
                + _stat("Median Quant", prof["alpha_median"], base["alpha_median"])
                + _stat("Score spread", spread, base["alpha_spread"])
                + _stat("Scoring 65+", prof["breadth_strong_pct"], base["breadth_strong_pct"], "%")
                + _stat("Median 3m move", prof["momentum_3m_median"], base["momentum_3m_median"], "%", True)
                + _stat("Median P/E", prof["pe_median"], base["pe_median"], "×")
                + _stat("Median growth", prof["growth_median"], base["growth_median"], "%", True)
                + _stat("Median net margin", prof["margin_median"], base["margin_median"], "%", True)
                + "</div>"
                + ai_html
                + ('<p class="sp-read">' + read + " These are descriptive measures of the "
                   "group as it stands today, not a forecast and not a recommendation.</p>"
                   if read else "")
            )
    except Exception:
        profile_html = ""      # a profile is a bonus; never break the ranking over it
    canonical = f"{site_origin}/sectors/{slug}"
    title = f"Best {label} stocks — live Quant Scores | TickerMover"
    desc = (
        f"TickerMover's live ranking of {n} {label} stocks by Quant Score. "
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
  <h1>{label} stocks, ranked</h1>
  <p class="lede">Live ranking of {n} {label} stocks by TickerMover Quant Score — a 0-100 composite of fundamentals, valuation, momentum, analyst signal, and macro regime. Click any ticker for the full breakdown.</p>
  {profile_html}
  <table class="tbl">
    <thead><tr><th>Ticker</th><th>Grade</th><th>Score</th><th>Bottom line</th></tr></thead>
    <tbody>{table_html or '<tr><td colspan="4">No stocks scored in this sector yet — the universe is warming up.</td></tr>'}</tbody>
  </table>
  <p style="font-size:13px;color:#5d6c7b">Quant Scores update every 5 minutes during US market hours. Grades: <strong>A</strong> Top Tier · <strong>B</strong> Quality · <strong>C</strong> Average · <strong>D</strong> Below Avg · <strong>F</strong> Weak. (Quality descriptors, not buy/sell recommendations.)</p>
  {cta_block("See the full live dashboard", n=len(universe or []) or None)}
  {newsletter_block("sector-" + slug)}
  <div class="legal">TickerMover is a research tool, not financial advice, and not FCA-authorised. Always do your own research before investing. Capital at risk.</div>
</div>
<style>
/* The profile block read as one undifferentiated grey slab: white cards on a
   grey rule, a grey note and a white AI box. Everything had the same weight,
   so nothing led. It now opens on the accent, and the stat grid sits on the
   warm ground the rest of the site uses. */
.sp-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1px;
  background:#E4E7EC;border:1px solid #E4E7EC;border-top:3px solid #FF6100;
  border-radius:10px;overflow:hidden;margin:0 0 8px}}
.sp-stat{{background:#fff;padding:13px 15px;transition:background 140ms cubic-bezier(.2,.7,.2,1)}}
.sp-stat:hover{{background:#FFF9F5}}
.sp-k{{font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;color:#758696;font-weight:700}}
.sp-v{{font-size:20px;font-weight:700;color:#0A2F46;margin-top:5px;font-variant-numeric:tabular-nums}}
.sp-b{{font-size:11px;color:#758696;margin-top:2px;font-variant-numeric:tabular-nums}}
.sp-read{{font-size:13.5px;color:#5d6c7b;line-height:1.6;margin:0 0 22px;padding:11px 15px;
  background:#F2F1EE;border-left:3px solid #FF6100;border-radius:4px}}
.sp-ai{{font-size:14px;color:#10293D;line-height:1.65;margin:14px 0 10px;padding:14px 16px 14px 18px;
  background:#FFF8F4;border:1px solid #F0DDD1;border-left:3px solid #FF6100;border-radius:8px}}
.sp-ai span{{display:block;font-size:10px;letter-spacing:.09em;text-transform:uppercase;
  color:#9A3412;font-weight:800;margin-bottom:6px}}

/* Ranking table — the three highest scorers carry a logo, which is the only
   real colour on the page and marks where the ranking starts. */
.tbl thead th{{background:#FFF2EC;color:#0A2F46;border-bottom:2px solid #E4CFC2}}
.tbl tbody tr:hover td{{background:#FFF6F1}}
.tbl td.vd{{white-space:normal;line-height:1.5}}
.tbl td.vd>span{{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;
  overflow:hidden}}
.sr-nm{{font-size:12.5px;color:#5d6c7b;display:block;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}}
.sr-id{{display:flex;align-items:center;gap:10px;min-width:0}}
.sr-id>div{{min-width:0}}
.sr-logo{{width:26px;height:26px;border-radius:50%;object-fit:contain;background:#fff;
  flex:0 0 26px;box-shadow:0 0 0 1px #E4E7EC}}
.tbl td.sr-lead{{box-shadow:inset 3px 0 0 #FF6100}}
</style>"""
    return page_shell(
        title=title, desc=desc, canonical=canonical, body_html=body,
        schema_json=schema_json,
        og_image=f"{site_origin}/static/icons/icon-512.png",
    )


_SECTOR_INDEX_CSS = """
.si-note{font-size:13px;color:#5d6c7b;line-height:1.6;margin:0 0 20px;padding:12px 16px;
  background:#F2F1EE;border-left:3px solid #FF6100;border-radius:4px}
.si-note b{color:#0A2F46}
.si-wrap{overflow-x:auto;border:1px solid #D6DADD;border-radius:10px;background:#fff;margin:0 0 26px}
table.si{border-collapse:collapse;width:100%;min-width:1080px;font-variant-numeric:tabular-nums}
/* Two-line headers. "MEDIAN REV GROWTH" and "MEDIAN NET MARGIN" were setting
   the column widths on their own, and repeating "Median" five times said the
   same thing five times. The short word carries the column; the qualifier
   underneath keeps it self-describing without the width. */
table.si th{position:sticky;top:0;background:#FFF2EC;text-align:right;font-size:10.5px;
  letter-spacing:.07em;text-transform:uppercase;color:#0A2F46;font-weight:700;
  padding:9px 12px;border-bottom:2px solid #E4CFC2;white-space:nowrap;vertical-align:bottom}
table.si th i{display:block;font-style:normal;font-size:9px;font-weight:400;
  letter-spacing:.04em;color:#8A7A70;margin-top:3px;text-transform:none}
table.si th:first-child,table.si td:first-child{text-align:left}
table.si td{padding:11px 12px;border-bottom:1px solid #F2F1EE;text-align:right;
  font-size:13.5px;color:#10293D;white-space:nowrap}
table.si tbody tr:nth-child(even) td{background:#FCFBFA}
table.si tbody tr:hover td{background:#FFF6F1}
table.si tr:last-child td{border-bottom:0}
/* Baseline row is a reference, not a ranked entry — the left rule marks it as
   the line everything else is read against. */
table.si tr.si-base td{background:#FFF8F2;font-weight:700;color:#0A2F46}
table.si tr.si-base:hover td{background:#FFF8F2}
table.si tr.si-base td:first-child{box-shadow:inset 3px 0 0 #FF6100}
table.si a.si-nm{color:#0A2F46;font-weight:650;text-decoration:none}
table.si a.si-nm:hover{text-decoration:underline}
.si-n{font-size:12px;color:#758696}
.si-bar{display:inline-block;width:52px;height:6px;border-radius:99px;background:#EDEBE8;
  vertical-align:middle;margin-right:7px;overflow:hidden}
.si-bar i{display:block;height:100%;border-radius:99px;background:#AEB9C2}
.si-b2 i{background:#758696}
.si-b3 i{background:#14587D}
.si-b4 i{background:#FF6100}

/* Highest-scoring cell: logo + ticker chips. The logos are the only real
   colour on the page and they make a row identifiable before it is read. */
.si-lead{display:inline-flex;align-items:center;gap:4px;margin:0;
  padding:2px 7px 2px 2px;border:1px solid #E4E7EC;border-radius:99px;background:#fff;
  text-decoration:none;vertical-align:middle;
  transition:border-color 140ms cubic-bezier(.2,.7,.2,1),box-shadow 140ms cubic-bezier(.2,.7,.2,1)}
.si-lead:hover{text-decoration:none;border-color:#0A2F46;
  box-shadow:0 4px 12px -6px rgba(10,47,70,.4)}
.si-lead img{width:17px;height:17px;border-radius:50%;object-fit:contain;
  background:#fff;flex:0 0 17px}
.si-lead span{font-family:var(--mono),ui-monospace,Menlo,monospace;font-size:10.5px;
  font-weight:600;color:#5d6c7b;letter-spacing:.02em}
.si-lead:hover span{color:#0A2F46}
table.si td.si-sec{white-space:normal;min-width:230px;padding:10px 12px}
.si-leads{display:flex;flex-wrap:wrap;gap:5px;margin-top:6px}
.si-pos{color:#12704A;font-weight:650}
.si-neg{color:#B32D23;font-weight:650}
"""


def _si_money(v) -> str:
    """Market cap as $1.2T / $340B / $920M. Two significant-ish digits: the
    column is for comparing sectors at a glance, and $1.24T vs $1.2T does not
    change which is bigger."""
    if not v:
        return '<span class="si-n">&mdash;</span>'
    v = float(v)
    for cut, unit in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if v >= cut:
            n = v / cut
            return f"${n:.1f}{unit}" if n < 100 else f"${n:.0f}{unit}"
    return f"${v:,.0f}"


def _si_cell(v, suffix: str = "", signed: bool = False) -> str:
    if v is None:
        return '<span class="si-n">—</span>'
    if signed:
        cls = "si-pos" if v >= 0 else "si-neg"
        return f'<span class="{cls}">{"+" if v >= 0 else ""}{v}{suffix}</span>'
    return f"{v}{suffix}"


def render_sector_index(universe: list[dict], site_origin: str) -> str:
    """Sub-sector comparison table.

    Rebuilt 15 Aug 2026. This was a list of cards reading "View live Alpha
    Scores for stocks in this sector" seventy-three times — no numbers, nothing
    to compare, and nothing for a search engine to rank on. It is now the
    comparison itself: every sub-sector on one screen, so the differences
    between them are visible without opening anything.

    The universe baseline is pinned as the first row on purpose. A median Alpha
    of 59 is unreadable until you know the universe sits at 64; without the
    comparison a reader supplies their own, and generously.

    Every column is measured, not modelled: counts, medians, an interquartile
    spread and a breadth count. No verdict, no ordering by "best to own" — it
    sorts by size, which is a fact about coverage rather than an opinion.
    """
    import sector_intel as _si

    secs = _si.all_sectors(universe)
    base = _si.universe_baseline(universe)

    def _leader_chip(l: dict) -> str:
        """Logo + ticker for one of a sector's three highest-scoring names.

        The logo host is the one already used by the stock and peer surfaces,
        not a new third party. `onerror` removes the image rather than hiding
        it, so a missing logo collapses to a plain ticker chip instead of
        leaving a hole the width of an image that never arrives."""
        tk = (l.get("ticker") or "").upper()
        src = "https://assets.parqet.com/logos/symbol/" + tk
        return (
            '<a class="si-lead" href="/stocks/' + tk + '">'
            '<img src="' + src + '" alt="" loading="lazy" width="20" height="20" '
            'onerror="this.remove()">'
            '<span>' + tk + "</span></a>"
        )

    def row(s: dict, is_base: bool = False) -> str:
        led = "".join(
            _leader_chip(l) for l in s.get("leaders", [])
        ) if not is_base else '<span class="si-n">—</span>'
        nm = (s["label"] if is_base
              else f'<a class="si-nm" href="/sectors/{s["slug"]}">{s["label"]}</a>')
        med = s["alpha_median"]
        if med is None:
            bar = ""
        else:
            band = ("b4" if med >= 65 else "b3" if med >= 55 else
                    "b2" if med >= 45 else "b1")
            bar = ('<span class="si-bar si-' + band + '"><i style="width:'
                   + str(max(3, min(100, round(med)))) + '%"></i></span>')
        # NB: built outside the f-string. Nested same-type quotes inside an
        # f-string expression need Python 3.12 (PEP 701) and there is no version
        # pin in this repo — Railway picks its own interpreter, so a 3.12-only
        # syntax here would be an ImportError that takes the whole app down.
        tr_open = '<tr class="si-base">' if is_base else "<tr>"
        # The three highest scorers used to be a column of their own on the far
        # right, which pushed the table past the viewport and put a sector's
        # name and its names at opposite ends of a wide row. They belong with
        # the label they describe.
        nm_cell = nm if is_base else (nm + '<div class="si-leads">' + led + "</div>")
        return (
            tr_open
            + f'<td class="si-sec">{nm_cell}</td>'
            f'<td>{s["count"]}</td>'
            f'<td>{_si_money(s.get("market_cap_total"))}</td>'
            f"<td>{bar}{_si_cell(med)}</td>"
            f'<td>{_si_cell(s["alpha_spread"])}</td>'
            f'<td>{_si_cell(s["breadth_strong_pct"], "%")}</td>'
            f'<td>{_si_cell(s["momentum_3m_median"], "%", signed=True)}</td>'
            f'<td>{_si_cell(s["pe_median"], "×")}</td>'
            f'<td>{_si_cell(s["growth_median"], "%", signed=True)}</td>'
            f'<td>{_si_cell(s.get("growth_weighted"), "%", signed=True)}</td>'
            f'<td>{_si_cell(s["margin_median"], "%", signed=True)}</td></tr>'
        )

    body_rows = row(base, True) + "".join(row(s) for s in secs)
    n_sec, n_names = len(secs), sum(s["count"] for s in secs)

    body = f"""
<div class="wrap-wide">
  {brand_header()}
  <div class="crumbs"><a href="/">Home</a> · Sectors</div>
  <h1>Every sub-sector, side by side</h1>
  <p class="lede">{n_sec} sub-sectors covering {n_names} scored US-listed companies, all measured the same way. The first row is the whole scored universe — read every sector against it.</p>
  <div class="si-note">
    <b>What the spread tells you.</b> A narrow mid-50% range means the names in that group
    score alike and tend to move as a block. A wide one means the individual name matters
    more than the theme. Every figure is a median or a count across the group, so no single
    outlier can carry a sector.
    These are quality and characteristic descriptors, not buy or sell signals.
  </div>
  <div class="si-wrap">
    <table class="si">
      <thead><tr>
        <th>Sub-sector</th>
        <th>Names<i>count</i></th>
        <th>Size<i>total mkt cap</i></th>
        <th>Quant<i>median</i></th>
        <th>Spread<i>mid 50%</i></th>
        <th>Strong<i>scoring 65+</i></th>
        <th>3m<i>median move</i></th>
        <th>P/E<i>median</i></th>
        <th>Growth<i>median</i></th>
        <th>Sector growth<i>weighted by size</i></th>
        <th>Margin<i>median net</i></th>
      </tr></thead>
      <tbody>{body_rows}</tbody>
    </table>
  </div>
  <p style="font-size:13px;color:#5d6c7b">Scores refresh every 5 minutes during US market hours. Grades: <strong>A</strong> Top Tier · <strong>B</strong> Quality · <strong>C</strong> Average · <strong>D</strong> Below Avg · <strong>F</strong> Weak — quality descriptors, not recommendations.</p>
  {cta_block("Open the live dashboard", n=n_names or None)}
  {newsletter_block("sectors-index")}
  <div class="legal">TickerMover — research, not advice.</div>
</div>
<style>{_SECTOR_INDEX_CSS}</style>"""
    canonical = f"{site_origin}/sectors"
    _schema = (
        _json.dumps({
            "@context": "https://schema.org", "@type": "ItemList",
            "name": "US stock sub-sectors compared",
            "description": "Every sub-sector TickerMover scores, compared on median Quant "
                           "Score, spread, breadth, size and growth.",
            "url": canonical, "numberOfItems": len(secs),
            "itemListElement": [
                {"@type": "ListItem", "position": i + 1,
                 "url": f"{site_origin}/sectors/{x['slug']}", "name": x["label"]}
                for i, x in enumerate(secs[:60])
            ],
        }, separators=(",", ":"))
        + "</script>" + chr(10) + '<script type="application/ld+json">'
        + _json.dumps({
            "@context": "https://schema.org", "@type": "BreadcrumbList",
            "itemListElement": [
                {"@type": "ListItem", "position": 1, "name": "Home", "item": site_origin},
                {"@type": "ListItem", "position": 2, "name": "Sectors", "item": canonical},
            ],
        }, separators=(",", ":"))
    )
    return page_shell(
        schema_json=_schema,
        title="US stock sectors compared — median Quant, spread and breadth | TickerMover",
        # NB: keep "Quant Score" on one line. This string was previously split as
        # "…median Alpha " / "Score, …", which no find-and-replace could match —
        # it survived the rename and was only caught by rendering the page.
        desc=(f"{n_sec} sub-sectors across {n_names} US-listed stocks, compared on "
              "median Quant Score, score spread, breadth and 3-month momentum. "
              "Updated every 5 minutes."),
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
  <div class="cmp-row"><span class="k">Quant Score</span><span class="v">{pop_n}/100</span></div>
  <div class="cmp-row"><span class="k">Grade</span><span class="v">{grade} · {rating}</span></div>
  <div class="cmp-row"><span class="k">Price</span><span class="v">{price_str}</span></div>
  <div class="cmp-row"><span class="k">Rev growth (YoY)</span><span class="v">{rev_g_str}</span></div>
  <div class="cmp-row"><span class="k">Forward P/E</span><span class="v">{pe_str}</span></div>
  <div class="cmp-row"><span class="k">1-mo momentum</span><span class="v">{mom_str}</span></div>
  <div class="cmp-row"><span class="k">Analyst upside</span><span class="v">{upside_str}</span></div>
  <div style="margin-top:14px;padding-top:12px;border-top:1px solid #F2F1EE;font-size:13.5px;color:#5d6c7b;line-height:1.55">{bl}</div>
</div>
"""


_CMP_CSS = """
.h2h{overflow-x:auto;border:1px solid #D6DADD;border-radius:10px;background:#fff;margin:0 0 24px}
table.h2h-t{border-collapse:collapse;width:100%;min-width:640px;font-variant-numeric:tabular-nums}
table.h2h-t th{background:#F2F1EE;font-size:11px;letter-spacing:.06em;text-transform:uppercase;
  color:#5d6c7b;font-weight:700;padding:12px;border-bottom:1px solid #D6DADD}
table.h2h-t th.sd{font-size:15px;letter-spacing:0;text-transform:none;color:#0A2F46}
table.h2h-t td{padding:12px;border-bottom:1px solid #F2F1EE;font-size:14px;color:#10293D;
  text-align:center;white-space:nowrap}
table.h2h-t td.mk{text-align:left;color:#5d6c7b;font-size:13.5px;white-space:normal}
table.h2h-t td.mk i{display:block;font-style:normal;font-size:11.5px;color:#758696;margin-top:2px}
table.h2h-t tr:last-child td{border-bottom:0}
table.h2h-t td.hi{background:#FFF6EF;font-weight:700;color:#0A2F46}
.h2h-tag{display:inline-block;margin-left:6px;font-size:9.5px;font-weight:800;letter-spacing:.05em;
  color:#9A3412;background:rgba(255,97,0,.1);border-radius:4px;padding:1px 5px;vertical-align:middle}
.h2h-ctx{font-size:13.5px;color:#5d6c7b;line-height:1.65;background:#F2F1EE;border-left:3px solid #FF6100;
  border-radius:4px;padding:14px 16px;margin:0 0 22px}
.h2h-ctx b{color:#0A2F46}
.h2h-sym{font-family:ui-monospace,Menlo,monospace;font-weight:700}
.cs-lede{font-size:14px;color:#5D6C7B;line-height:1.65;max-width:78ch;margin:0 0 18px}
.cs-wrap{display:grid;gap:1px;background:#D6DADD;border:1px solid #D6DADD;
  border-radius:10px;overflow:hidden;margin:0 0 14px}
.cs-sec{background:#fff;padding:16px 18px}
.cs-sec h3{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:#9A3412;
  font-weight:800;margin:0 0 7px}
.cs-sec p{font-size:14.5px;line-height:1.7;color:#10293D;margin:0;max-width:82ch}
.cs-src{font-size:12px;color:#758696;margin:0 0 24px}
"""


def _h2h_logo(sym: str, size: int = 22) -> str:
    """Company logo for the head-to-head. Same host as every other logo on the
    site; `onerror` removes it so a missing file leaves the ticker alone rather
    than a broken-image box in a table header."""
    return ('<img class="h2h-logo" src="https://assets.parqet.com/logos/symbol/'
            + sym + '" alt="" loading="lazy" width="' + str(size) + '" height="'
            + str(size) + '" onerror="this.remove()">')


def render_comparison(a: str, b: str, universe: list[dict], site_origin: str) -> Optional[str]:
    """Head-to-head on measured characteristics.

    Rebuilt 15 Aug 2026. The previous version opened with a verdict — "X edges
    out Y", "the right pick depends on which thesis you find more compelling" —
    and then showed two loose cards you had to read across to compare anything.

    Two problems with that. It declared a winner on a single composite, which
    is a recommendation in all but name and exactly the framing removed from
    the rest of the site this month. And the card layout meant the reader did
    the diffing themselves, which is the entire job of a comparison page.

    It is now one aligned table: every dimension on its own row, both values
    side by side, and the larger side marked. "Higher" is stated as a fact and
    labelled as such — a higher P/E is simply a higher P/E, and whether that is
    good depends on what you think the growth is worth. There is no total, no
    tally of wins, and no conclusion about which to own.
    """
    import sector_intel as _si

    c = _si.compare(a, b, universe)
    if not c:
        return None
    A, B = c["a"], c["b"]
    a, b = A["ticker"], B["ticker"]
    name_a, name_b = A["name"], B["name"]
    canonical = f"{site_origin}/compare/{a}-vs-{b}"

    def cell(row, side):
        val = row["a"] if side == "a" else row["b"]
        tk = a if side == "a" else b
        cls = " class=\"hi\"" if row["higher"] == tk else ""
        tag = '<span class="h2h-tag">HIGHER</span>' if row["higher"] == tk else ""
        return "<td" + cls + ">" + val + tag + "</td>"

    trs = "".join(
        '<tr><td class="mk">' + r["label"] + "<i>" + r["note"] + "</i></td>"
        + cell(r, "a") + cell(r, "b") + "</tr>"
        for r in c["rows"]
    )

    # Sector context. Two names in the same sub-sector are a like-for-like
    # comparison; two from different ones are not, and saying so up front stops
    # the table being read as more equivalent than it is.
    if c["same_sector"]:
        ctx = ("Both sit in <b>" + (A["sector"] or "the same sub-sector")
               + "</b>, so these figures are broadly like-for-like. ")
    else:
        ctx = ("These are in different sub-sectors — <b>" + (A["sector"] or "—")
               + "</b> and <b>" + (B["sector"] or "—")
               + "</b> — so valuation and margin norms differ between them and the "
                 "rows below are not strictly like-for-like. ")
    ctx += ("They differ on <b>" + str(c["differing"]) + " of " + str(c["measured"])
            + "</b> measured dimensions; anything within 2% is shown as level rather "
              "than split. Nothing here totals to a winner — which differences matter "
              "is your call, not ours.")

    # Long-form study — CACHE PEEK ONLY, never generated during a page render.
    # This page is public and crawled; generating here would let a bot trigger
    # research runs. Studies are produced by the admin generate endpoint.
    study_html = ""
    try:
        import compare_study as _cstudy
        _hit = _cstudy.cached(c) or {}
        _sec = _hit.get("sections") or {}
        if _sec:
            _titles = dict(_cstudy.SECTIONS)
            blocks = "".join(
                '<div class="cs-sec"><h3>' + html.escape(str(_titles.get(k, k)).split(" — ")[0])
                + "</h3><p>" + html.escape(v) + "</p></div>"
                for k, v in _sec.items() if isinstance(v, str) and v.strip()
            )
            if blocks:
                srcs = ", ".join(html.escape(s) for s in (_hit.get("sources") or []))
                study_html = (
                    '<h2>The detail</h2>'
                    '<p class="cs-lede">Researched over each company\'s own SEC filing and '
                    'earnings call, its recent coverage and third-party analyst consensus. '
                    'Every claim below traces to one of those or to figures we computed — '
                    'nothing is supplied from memory, and where the sources are silent it '
                    'says so.</p>'
                    '<div class="cs-wrap">' + blocks + "</div>"
                    + ('<p class="cs-src">Sources: ' + srcs + "</p>" if srcs else "")
                )
    except Exception:
        study_html = ""

    title = f"{a} vs {b} — growth, margins, valuation and momentum compared | TickerMover"
    desc = (
        f"{a} ({name_a[:22]}) and {b} ({name_b[:22]}) compared across growth, margins, "
        f"valuation, momentum and size. Live figures, refreshed every 5 minutes."
    )[:160]
    body = f"""
<div class="wrap-wide">
  {brand_header()}
  <div class="crumbs"><a href="/">Home</a> · <a href="/compare">Compare</a> · {a} vs {b}</div>
  <h1><span class="h2h-id">{_h2h_logo(a, 30)}<span class="sym">{a}</span></span>
      <span class="h2h-vs">vs</span>
      <span class="h2h-id">{_h2h_logo(b, 30)}<span class="sym">{b}</span></span></h1>
  <p class="lede">{name_a} and {name_b}, measured on the same ten characteristics.</p>
  <div class="h2h-ctx">{ctx}</div>
  <div class="h2h">
    <table class="h2h-t">
      <thead><tr>
        <th style="text-align:left">Measure</th>
        <th class="sd"><a href="/stocks/{a}" class="h2h-sym">{_h2h_logo(a)}<span>{a}</span></a></th>
        <th class="sd"><a href="/stocks/{b}" class="h2h-sym">{_h2h_logo(b)}<span>{b}</span></a></th>
      </tr></thead>
      <tbody>{trs}</tbody>
    </table>
  </div>
  {study_html}
  <h2>How the Quant Score works</h2>
  <p>It blends fundamentals, valuation, momentum, analyst signal and macro regime into one 0-100 number. It is a quality descriptor, not a buy or sell signal. <a href="/learn/pop-score">Read the methodology →</a></p>
  <p>Full breakdowns: <a href="/stocks/{a}">{a}</a> · <a href="/stocks/{b}">{b}</a>{(' · Both in <a href="/sectors/' + A['slug'] + '">' + (A['sector'] or '') + '</a>') if c['same_sector'] and A['slug'] else ''}</p>
  {cta_block("Open the live dashboard")}
  {newsletter_block(
      "compare-" + a + "-" + b,
      title="Keep this comparison up to date",
      copy=("A table is a snapshot. Get " + a + " vs " + b + " re-measured every week "
            "&mdash; the same ten characteristics, so you can see which way each one "
            "moved rather than re-reading the page. Free, and unsubscribe any time."),
      cta="Track this pair")}
  <div class="legal">TickerMover is a research tool, not financial advice, and not FCA-authorised. Figures are computed from live universe data and refresh every 5 minutes during market hours. Analyst upside is third-party consensus, not our forecast. Capital at risk.</div>
</div>
<style>{_CMP_CSS}
.h2h-id{{display:inline-flex;align-items:center;gap:9px;vertical-align:middle}}
.h2h-vs{{margin:0 10px;color:var(--grey-2);font-weight:300}}
h1 .h2h-logo{{width:30px;height:30px;border-radius:50%;object-fit:contain;
  background:#fff;box-shadow:0 0 0 1px var(--rule)}}
.h2h-logo{{width:22px;height:22px;border-radius:50%;object-fit:contain;background:#fff;
  box-shadow:0 0 0 1px var(--rule);flex:0 0 auto}}
a.h2h-sym{{display:inline-flex;align-items:center;gap:8px;justify-content:flex-end}}
</style>"""
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
    """The /compare hub.

    Rebuilt 15 Aug 2026. It was 14 cards each repeating the same sentence, so
    the page told you nothing you did not already know from the ticker pair.
    It now previews each matchup: both Quant Scores, how many of the ten
    measured dimensions actually separate the two, and whether they sit in the
    same sub-sector — which is what decides whether the comparison is
    like-for-like at all. Enough to choose which pair is worth opening.
    """
    import sector_intel as _si

    rows_html = []
    for a, b in FEATURED_COMPARISONS:
        c = _si.compare(a, b, universe)
        if not c:
            continue           # ticker no longer in the universe
        alpha = next((r for r in c["rows"] if r["key"] == "alpha"), None)
        av = alpha["a"] if alpha else "—"
        bv = alpha["b"] if alpha else "—"
        same = ('<span style="color:#12704A;font-weight:650">same sub-sector</span>'
                if c["same_sector"] else
                '<span style="color:#758696">different sub-sectors</span>')
        rows_html.append(
            '<tr><td><a class="si-nm" href="/compare/' + a + "-vs-" + b + '">'
            + a + " vs " + b + "</a></td>"
            + "<td>" + av + "</td><td>" + bv + "</td>"
            + "<td>" + str(c["differing"]) + " of " + str(c["measured"]) + "</td>"
            + "<td>" + same + "</td></tr>"
        )
    body_rows = "".join(rows_html)

    body = f"""
<div class="wrap-wide">
  {brand_header()}
  <div class="crumbs"><a href="/">Home</a> · Compare</div>
  <h1>Head-to-head comparisons</h1>
  <p class="lede">{len(rows_html)} curated matchups, each measured on the same ten characteristics. You can build any pairing by visiting <code>/compare/&lt;TICKER1&gt;-vs-&lt;TICKER2&gt;</code>.</p>
  <div class="si-note">
    <b>Differ on</b> counts how many of the ten measured dimensions actually separate the two —
    anything inside 2% is treated as level. A low count means the pair are close on the numbers
    and the choice rests on things a table cannot show. <b>Same sub-sector</b> matters because
    valuation and margin norms differ between industries: across sub-sectors the figures are not
    strictly like-for-like. None of these pages picks a winner.
  </div>
  <div class="si-wrap">
    <table class="si">
      <thead><tr><th>Matchup</th><th>Quant (left)</th><th>Quant (right)</th><th>Differ on</th><th>Comparable?</th></tr></thead>
      <tbody>{body_rows}</tbody>
    </table>
  </div>
  {cta_block("Open the live dashboard")}
  {newsletter_block("compare-index")}
  <div class="legal">TickerMover — research, not advice, and not FCA-authorised. Capital at risk.</div>
</div>
<style>{_SECTOR_INDEX_CSS}</style>"""
    canonical = f"{site_origin}/compare"
    return page_shell(
        title="US stock head-to-head comparisons — NVDA vs AMD, AAPL vs MSFT | TickerMover",
        desc=("Head-to-head US stock comparisons on growth, margins, valuation, momentum "
              "and size - see how far apart each pair really is. Updated every 5 minutes."),
        canonical=canonical, body_html=body,
        og_image=f"{site_origin}/static/icons/icon-512.png",
    )


def render_article(art: dict, site_origin: str) -> Optional[str]:
    """One research article as a real HTML page.

    These 14 articles existed only as JSON on /api/blog, yet sitemap.xml had
    been advertising /article/<id> for every one of them — so Google was told
    about 14 pages that returned 404. They are the site's only genuinely
    unique long-form content, so the fix is to serve them, not to hide them.

    DELIBERATELY OMITS the `report` block (rating, conviction, price_target).
    These carry price targets against a price captured when the piece was
    written; publishing a months-old target on an indexable page is both stale
    and the sort of thing the site's own AI prompts already forbid
    ("do NOT give price targets"). The prose and the summary are the value.
    """
    if not art or not art.get("id"):
        return None
    import html as _html

    aid   = str(art.get("id"))
    title = (art.get("title") or "").strip()
    summ  = (art.get("summary") or "").strip()
    body  = art.get("content") or ""          # already HTML, authored in-repo
    tick  = (art.get("ticker") or "").upper()
    cat   = (art.get("category") or "Research").strip()
    date  = (art.get("date") or "").strip()

    canonical = f"{site_origin}/article/{aid}"
    desc = (summ or title)[:180]

    schema = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title[:110],
        "description": desc,
        "url": canonical,
        "datePublished": date,
        "articleSection": cat,
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
            {"@type": "ListItem", "position": 2, "name": "Research", "item": f"{site_origin}/reports"},
            {"@type": "ListItem", "position": 3, "name": title[:70], "item": canonical},
        ],
    }
    schema_json = (
        _json.dumps(schema, separators=(",", ":"))
        + '</script>\n<script type="application/ld+json">'
        + _json.dumps(breadcrumb, separators=(",", ":"))
    )

    tick_chip = (f'<a class="art-tick" href="{site_origin}/stocks/{_html.escape(tick)}">'
                 f'{_html.escape(tick)}</a>') if tick else ""
    # The date is stated plainly and up top: these are point-in-time pieces and
    # a reader arriving from search has no other way to know how old the view is.
    meta_line = " · ".join(x for x in (_html.escape(cat), date) if x)

    body_html = f"""<div class="wrap">{brand_header()}</div>
<article class="wrap art">
  <p class="art-eyebrow">{meta_line}</p>
  <h1 class="art-h1">{_html.escape(title)}</h1>
  {f'<p class="art-sum">{_html.escape(summ)}</p>' if summ else ''}
  {tick_chip}
  <div class="art-body">{body}</div>
  <p class="art-note">Published {date or 'previously'}. This is a point-in-time
  research note and is not updated — figures and any view expressed were current
  when written. Research opinion for information and education only: not
  investment advice, not a personal recommendation, and not FCA-authorised.
  Capital at risk; past performance is not a guide to future results.</p>
  {cta_block("See this sector scored live", "/app?signup=1")}
</article>
<style>
.art{{max-width:760px;padding:26px 22px 60px}}
.art-eyebrow{{font-size:11px;letter-spacing:.12em;text-transform:uppercase;
  color:#758696;margin:0 0 10px;font-weight:700}}
.art-h1{{font-size:clamp(27px,4.4vw,40px);line-height:1.15;letter-spacing:-.022em;
  color:#0A2F46;margin:0 0 14px}}
.art-sum{{font-size:17px;line-height:1.6;color:#5d6c7b;margin:0 0 18px}}
.art-tick{{display:inline-block;font-family:ui-monospace,monospace;font-size:12px;
  font-weight:700;color:#14587D;background:rgba(20,88,125,.08);
  border:1px solid rgba(20,88,125,.18);border-radius:999px;
  padding:5px 12px;text-decoration:none;margin-bottom:20px}}
.art-body{{font-size:16px;line-height:1.75;color:#10293D}}
.art-body p{{margin:0 0 17px}}
.art-body h2{{font-size:22px;line-height:1.25;color:#0A2F46;margin:32px 0 12px;
  letter-spacing:-.015em}}
.art-body h3{{font-size:18px;color:#0A2F46;margin:26px 0 10px}}
.art-body ul,.art-body ol{{margin:0 0 17px;padding-left:22px}}
.art-body li{{margin:0 0 7px}}
.art-note{{font-size:12.5px;line-height:1.6;color:#758696;margin:34px 0 26px;
  padding-top:16px;border-top:1px solid rgba(10,47,70,.09)}}
</style>"""

    return page_shell(title=f"{title} | TickerMover", desc=desc, canonical=canonical,
                      body_html=body_html, schema_json=schema_json,
                      og_image=f"{site_origin}/static/icons/icon-512.png")
