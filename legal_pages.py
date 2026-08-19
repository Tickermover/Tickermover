"""
TickerMover - Legal pages (Terms, Privacy, Disclaimer)

Three plain-English legal documents. The business is relocating to the UK,
so the REGULATORY DISCLAIMER has been converted to FCA framing (generic
research / not a personal recommendation under FSMA + the FCA Handbook).

⚠️  DRAFT — STILL NEEDS A UK SOLICITOR SIGN-OFF BEFORE PAID LAUNCH / ADS.
  UK conversion pass (Aug 2026) done in-house:
    * Terms §13 governing law: India/arbitration -> law & courts of England
      and Wales.
    * Terms §10 liability cap: INR -> GBP (£50 free-tier) + non-excludable
      carve-outs.
    * Terms §6: added UK Consumer Contracts Regs 2013 14-day cancellation.
    * Terms §1: trading-entity identification block (env-driven; shows a
      visible "pending" note until LEGAL_ENTITY_NAME / _COMPANY_NUMBER /
      _ADDRESS are set — REQUIRED before ads).
    * Privacy: rewritten cookie section (essential/analytics/advertising
      categories + consent), removed the false "we do not run ads / no ad
      cookies / never share with advertisers" absolutes, added Google/Meta
      as consent-gated partners. Pairs with static/consent.js banner.
  STILL OUTSTANDING for the solicitor: confirm the FCA financial-promotions
  position (the model portfolio / Outperform-Avoid verdicts vs the Art.20
  media exemption) BEFORE spending on ads; verify data-processor list and
  cross-border transfer basis; fill the trading-entity details.

  * FCA generic-research framing — TickerMover is a research/educational
    tool, NOT authorised or regulated by the FCA, and gives no personal
    recommendation to any specific user.
  * Data-feed providers (Polygon, FMP, Alpha Vantage, Finnhub, Alpaca,
    yfinance, SEC EDGAR) in the data-flow disclosure.
  * Limitation of liability sized for a low-cost SaaS (cap = 12 months
    of fees paid).

Drafted in-house; reviewed by no lawyer yet. Do NOT rely on this as legal
advice — a UK solicitor must review before prod launch for paid users.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

# Effective date is regenerated on each cold start so users always see
# a date that reflects the latest deploy. To pin a date, override via env.
_EFFECTIVE_DATE = os.environ.get(
    "LEGAL_EFFECTIVE_DATE",
    datetime.utcnow().strftime("%B %d, %Y"),
)
_COMPANY = os.environ.get("LEGAL_COMPANY_NAME", "TickerMover")
_DOMAIN  = os.environ.get("LEGAL_DOMAIN", "tickermover.com")
_CONTACT = os.environ.get("LEGAL_CONTACT_EMAIL", "support@tickermover.com")
# NOTE: the governing-law / arbitration body text further down STILL references
# India and must be rewritten by a UK solicitor. This default reflects the
# INTENDED UK posture only.
_JURIS   = os.environ.get("LEGAL_JURISDICTION", "England and Wales")
# UK e-commerce / consumer law requires you to identify the trading entity.
# These MUST be set before paid launch / running ads — fill in the registered
# company name, its Companies House number, and a geographic contact address.
_ENTITY     = os.environ.get("LEGAL_ENTITY_NAME", "")     # e.g. "TickerMover Ltd"
_COMPANY_NO = os.environ.get("LEGAL_COMPANY_NUMBER", "")  # Companies House number
_ADDRESS    = os.environ.get("LEGAL_ADDRESS", "")         # registered/trading address


def _business_details_html() -> str:
    """Renders the trading-entity identification block (Companies Act / UK
    e-commerce regs). Shows a visible 'to be completed' note until the env
    vars are set, so it is never silently missing."""
    if _ENTITY or _COMPANY_NO or _ADDRESS:
        rows = [f"<strong>{_ENTITY or _COMPANY}</strong>"]
        if _COMPANY_NO:
            rows.append(f"Registered in {_JURIS}, company no. {_COMPANY_NO}")
        if _ADDRESS:
            rows.append(_ADDRESS)
        rows.append(f'Contact: <a href="mailto:{_CONTACT}" class="body-link">{_CONTACT}</a>')
        return "<p>" + "<br>".join(rows) + "</p>"
    # Reader-facing wording ONLY. This block used to print the env var names to
    # set — LEGAL_ENTITY_NAME and friends — on the LIVE public Terms page, so
    # visitors were reading our deployment instructions inside a legal document.
    # The reminder for us belongs in the startup log, not in the contract.
    logger.warning(
        "legal_pages: no registered entity configured — /terms shows a "
        "'business details pending' notice. Set LEGAL_ENTITY_NAME, "
        "LEGAL_COMPANY_NUMBER and LEGAL_ADDRESS before paid launch."
    )
    return (
        '<div class="callout callout-amber"><strong>Business details pending.</strong> '
        'Our registered company name, company number and contact address will be '
        'published here. In the meantime you can reach us at '
        f'<a href="mailto:{_CONTACT}" class="body-link">{_CONTACT}</a>.</div>'
    )


# ── Shared styling + header / footer ────────────────────────────────

def _shell(title: str, slug: str, body_html: str) -> str:
    """Wrap body content with the real TickerMover nav + footer."""
    active = {s: ' aria-current="page"' for s in ("terms", "privacy", "disclaimer")}
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} | {_COMPANY}</title>
<meta name="description" content="{title} for {_COMPANY} — research tool, not financial advice.">
<meta name="robots" content="index, follow">
<meta property="og:site_name" content="{_COMPANY}">
<link rel="icon" type="image/svg+xml" href="/static/brand/tickermover-mark.svg">
<link rel="icon" type="image/png" sizes="32x32" href="/static/icons/favicon-32.png">
<link rel="icon" type="image/png" sizes="192x192" href="/static/icons/icon-192.png">
<link rel="apple-touch-icon" href="/static/icons/icon-192.png">
<meta name="theme-color" content="#0040c1">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600;700&family=Space+Grotesk:wght@700&display=swap" rel="stylesheet">
<style>
:root{{
  --primary:#0040c1;--blue-light:#2970ff;--blue-grad:linear-gradient(120deg,#2970ff 0%,#0042c5 55%,#00359e 100%);
  --alice-blue:#cdeef8;--text-dark:#090909;--eerie:#111827;--raisin:#212121;--grey:#758696;
  --r-pill:6.25rem;--wrap:1240px;--ease:cubic-bezier(.2,.7,.2,1);
}}
*{{box-sizing:border-box;margin:0;padding:0}}
html{{scroll-behavior:smooth}}
body{{background:var(--alice-blue);color:var(--text-dark);font-family:'Instrument Sans',system-ui,-apple-system,sans-serif;font-weight:400;line-height:1.6;-webkit-font-smoothing:antialiased;overflow-x:hidden}}
a{{color:inherit;text-decoration:none}}
img,svg{{display:block;max-width:100%}}

/* ── nav ── */
@property --btn-fill{{syntax:'<percentage>';inherits:false;initial-value:0%}}
.site-nav{{position:fixed;top:0;left:0;right:0;z-index:200;display:flex;align-items:center;justify-content:space-between;padding:20px 28px;max-width:1400px;margin:0 auto;transition:padding .4s var(--ease)}}
.nav-inner{{display:flex;align-items:center;justify-content:space-between;width:100%;background:rgba(255,255,255,.94);backdrop-filter:blur(20px) saturate(180%);-webkit-backdrop-filter:blur(20px) saturate(180%);border:1px solid rgba(255,255,255,.7);border-radius:var(--r-pill);padding:10px 10px 10px 22px;box-shadow:0 10px 34px rgba(9,20,60,.16),inset 0 1px 0 rgba(255,255,255,.6);transition:box-shadow .4s,background .4s}}
.site-nav.scrolled .nav-inner{{box-shadow:0 14px 44px rgba(9,20,60,.18),inset 0 1px 0 rgba(255,255,255,.6);background:rgba(255,255,255,.97)}}
.brand{{display:flex;align-items:center;gap:11px;font-family:'Space Grotesk','Instrument Sans',sans-serif;font-weight:700;font-size:20px;letter-spacing:-.02em;color:#0a0e22}}
.brand-wordmark{{display:inline-flex;align-items:baseline;flex-wrap:nowrap;white-space:nowrap;color:#0a0e22}}
.brand-m{{height:1.6em;width:auto;flex:none;align-self:baseline;margin:0 .02em}}
.foot-brand .brand-wordmark{{color:#fff}}
.nav-links{{display:flex;gap:30px;align-items:center}}
.nav-links a{{font-size:15px;color:var(--raisin);font-weight:500;font-family:'Instrument Sans',sans-serif}}
.nav-links a:hover,.nav-links a[aria-current]{{color:var(--primary)}}
.nav-right{{display:flex;align-items:center;gap:14px}}
.nav-signin{{font-family:'Instrument Sans',sans-serif;font-weight:600;font-size:15px;color:var(--raisin)}}
.nav-signin:hover{{color:var(--primary)}}
.btn{{display:inline-flex;align-items:center;gap:10px;cursor:pointer;border:none;font-family:'Instrument Sans',sans-serif;font-weight:600;font-size:14px;padding:10px 8px 10px 20px;border-radius:var(--r-pill);white-space:nowrap}}
.btn .arrow{{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;flex:0 0 30px}}
.btn .arrow svg{{width:14px;height:14px}}
.btn-primary{{--btn-fill:0%;color:#fff;background:radial-gradient(circle at calc(100% - 24px) 50%,#0a0e22 var(--btn-fill),#2453ff calc(var(--btn-fill) + 1%));box-shadow:0 0 0 2px #2453ff,0 8px 20px rgba(36,83,255,.28);transition:--btn-fill .85s cubic-bezier(.19,1,.22,1),box-shadow .55s}}
.btn-primary .arrow{{background:#0a0e22;color:#fff}}
.btn-primary:hover{{--btn-fill:135%}}
@media(max-width:768px){{.nav-links{{display:none}}.site-nav{{padding:12px 14px}}}}

/* ── page body ── */
.legal-page{{max-width:780px;margin:0 auto;padding:108px 24px 80px}}
.legal-crumbs{{font-size:12px;color:#94a3b8;margin-bottom:8px;letter-spacing:.05em;text-transform:uppercase;font-weight:600}}
h1{{font-size:34px;font-weight:700;letter-spacing:-.02em;margin-bottom:6px;color:var(--text-dark);font-family:'Instrument Sans',sans-serif}}
.legal-sub{{font-size:14px;color:#64748b;margin-bottom:32px}}
.legal-tab-nav{{display:flex;gap:4px;margin-bottom:32px;padding:4px;background:#fff;border:1px solid rgba(9,20,60,.08);border-radius:10px;width:fit-content;box-shadow:0 2px 8px rgba(9,20,60,.06)}}
.legal-tab-nav a{{font-size:13.5px;font-weight:600;color:#475569;padding:7px 18px;border-radius:7px;font-family:'Instrument Sans',sans-serif;transition:background .2s,color .2s}}
.legal-tab-nav a:hover{{color:var(--primary)}}
.legal-tab-nav a[aria-current]{{background:var(--primary);color:#fff}}
h2{{font-size:18px;font-weight:700;margin:32px 0 10px;color:var(--text-dark);padding-top:14px;border-top:1px solid rgba(9,20,60,.08);font-family:'Instrument Sans',sans-serif}}
h2:first-of-type{{border-top:none;padding-top:0;margin-top:0}}
h3{{font-size:15px;font-weight:600;margin:18px 0 6px;color:var(--primary);font-family:'Instrument Sans',sans-serif}}
p{{margin-bottom:12px;color:#1e293b;font-size:15px}}
ul,ol{{margin:8px 0 14px 22px;color:#1e293b;font-size:15px}}
li{{margin-bottom:6px}}
strong{{font-weight:700;color:var(--text-dark)}}
.callout{{background:#eff6ff;border-left:4px solid var(--primary);padding:14px 18px;border-radius:8px;margin:16px 0;font-size:14.5px}}
.callout-amber{{background:#fffbeb;border-left-color:#f59e0b}}
.callout-green{{background:#ecfdf5;border-left-color:#15803d}}
a.body-link{{color:var(--primary);font-weight:600}}
a.body-link:hover{{text-decoration:underline}}
table{{width:100%;border-collapse:collapse;margin:12px 0;font-size:14px}}
th{{padding:8px;border:1px solid rgba(9,20,60,.1);text-align:left;background:#f8fafc;font-weight:600}}
td{{padding:8px;border:1px solid rgba(9,20,60,.1)}}

/* ── footer ── */
.site-footer{{background:var(--text-dark);color:#fff;padding:0 0 28px;margin-top:64px}}
.foot-wrap{{max-width:var(--wrap);margin:0 auto;padding:0 28px}}
.foot-top{{display:flex;justify-content:space-between;align-items:flex-start;padding:48px 0 32px;gap:32px;flex-wrap:wrap}}
.foot-brand p{{font-size:14px;color:#94a3b8;margin-top:8px}}
.foot-right{{display:flex;gap:48px;align-items:flex-start;flex-wrap:wrap}}
.foot-nav{{display:flex;gap:24px;align-items:center;flex-wrap:wrap}}
.foot-nav a{{font-size:14px;color:#94a3b8;font-weight:500;transition:color .2s}}
.foot-nav a:hover{{color:#fff}}
.foot-meta{{display:flex;flex-direction:column;gap:8px;align-items:flex-end}}
.foot-email{{font-size:14px;color:#94a3b8;font-weight:500}}
.foot-email:hover{{color:#fff}}
.foot-risk{{border-top:1px solid rgba(255,255,255,.08);padding:16px 0 4px}}
.foot-risk p{{font-size:12px;line-height:1.6;color:#94a3b8;margin:0}}
.foot-bottom{{display:flex;justify-content:space-between;align-items:center;padding-top:20px;flex-wrap:wrap;gap:12px}}
.foot-bottom span{{font-size:12.5px;color:#64748b}}
.foot-legal{{display:flex;gap:20px}}
.foot-legal a{{font-size:13px;color:#64748b;font-weight:500;transition:color .2s}}
.foot-legal a:hover,.foot-legal a[aria-current]{{color:#fff}}
.disc{{font-size:12px;color:#475569}}
</style>
<script src="/static/consent.js" defer></script>
</head>
<body>

<!-- ── NAV ── -->
<nav class="site-nav" id="site-nav">
  <div class="nav-inner">
    <a class="brand" href="/"><span class="brand-wordmark">Ticker<svg class="brand-m" viewBox="0 0 90 105" fill="none" aria-hidden="true"><polyline points="5,100 23,42 45,66 67,26 85,100" stroke="#2970ff" stroke-width="9" fill="none" stroke-linecap="round" stroke-linejoin="round"/><circle cx="67" cy="8" r="7" fill="#2970ff"/></svg>over</span></a>
    <div class="nav-links">
      <a href="/#engine">The engine</a>
      <a href="/app">Dashboard</a>
      <a href="/#ai-engine">How it works</a>
      <a href="/#insights">Daily desk</a>
      <a href="/#pricing">Pricing</a>
    </div>
    <div class="nav-right">
      <a class="nav-signin" href="/login">Sign in</a>
      <a class="btn btn-primary" href="/login?signup">Start free <span class="arrow"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg></span></a>
    </div>
  </div>
</nav>

<!-- ── CONTENT ── -->
<div class="legal-page">
  <nav class="legal-tab-nav">
    <a href="/terms"{active['terms'] if slug == 'terms' else ''}>Terms</a>
    <a href="/privacy"{active['privacy'] if slug == 'privacy' else ''}>Privacy</a>
    <a href="/disclaimer"{active['disclaimer'] if slug == 'disclaimer' else ''}>Disclaimer</a>
  </nav>
  <div class="legal-crumbs">Legal · {title}</div>
  <h1>{title}</h1>
  <p class="legal-sub">Effective {_EFFECTIVE_DATE} · Last updated {_EFFECTIVE_DATE}</p>

  {body_html}

  <p style="font-size:12.5px;color:#94a3b8;margin-top:48px;padding-top:20px;border-top:1px solid rgba(9,20,60,.08)">
    © {datetime.utcnow().year} {_COMPANY} · Questions? <a href="mailto:{_CONTACT}" class="body-link">{_CONTACT}</a>
  </p>
</div>

<!-- ── FOOTER ── -->
<footer class="site-footer">
  <div class="foot-wrap">
    <div class="foot-top">
      <div class="foot-brand">
        <a class="brand" href="/"><span class="brand-wordmark">Ticker<svg class="brand-m" viewBox="0 0 90 105" fill="none" aria-hidden="true"><polyline points="5,100 23,42 45,66 67,26 85,100" stroke="#6aabff" stroke-width="9" fill="none" stroke-linecap="round" stroke-linejoin="round"/><circle cx="67" cy="8" r="7" fill="#6aabff"/></svg>over</span></a>
        <p>An AI research studio for the US market.</p>
      </div>
      <div class="foot-right">
        <nav class="foot-nav">
          <a href="/#engine">The engine</a>
          <a href="/app">Dashboard</a>
          <a href="/#ai-engine">How it works</a>
          <a href="/#insights">Daily desk</a>
          <a href="/#pricing">Pricing</a>
        </nav>
        <div class="foot-meta">
          <a class="foot-email" href="mailto:{_CONTACT}">{_CONTACT}</a>
        </div>
      </div>
    </div>
    <div class="foot-risk">
      <p><strong style="color:#cbd5e1">Capital at risk.</strong> {_COMPANY} is a research and data tool. It does not provide investment advice, recommendations, or any personal recommendation to buy or sell. Scores, signals and reference levels are information only. The value of investments can go down as well as up, and you may get back less than you invest. Past performance and historical scores are not a reliable indicator of future results. {_COMPANY} is not authorised or regulated by the Financial Conduct Authority. Do your own research, and consider advice from an FCA-authorised adviser before investing.</p>
    </div>
    <div class="foot-bottom">
      <span>© {datetime.utcnow().year} {_COMPANY}. All rights reserved.</span>
      <nav class="foot-legal">
        <a href="/privacy"{active['privacy'] if slug == 'privacy' else ''}>Privacy</a>
        <a href="/terms"{active['terms'] if slug == 'terms' else ''}>Terms</a>
        <a href="/disclaimer"{active['disclaimer'] if slug == 'disclaimer' else ''}>Disclaimer</a>
        <a href="#" data-cc-open>Cookie settings</a>
      </nav>
      <span class="disc">Not investment advice. {_COMPANY} is a research tool; do your own due diligence before any trade.</span>
    </div>
  </div>
</footer>

<script>
const n=document.getElementById('site-nav');
addEventListener('scroll',()=>n.classList.toggle('scrolled',scrollY>20),{{passive:true}});
</script>
</body>
</html>"""


# ╔════════════════════════════════════════════════════════════════════╗
# ║  Page 1: Terms of Service                                          ║
# ╚════════════════════════════════════════════════════════════════════╝
def render_terms() -> str:
    body = f"""
<div class="callout callout-green">
  <strong>TL;DR.</strong> {_COMPANY} is a research tool that scores publicly-listed
  US stocks using public data and AI. It is <strong>not authorised or regulated by
  the Financial Conduct Authority (FCA)</strong> and provides generic research only —
  not a personal recommendation or personalised investment advice. You make your
  own decisions; we provide information to help you make them.
</div>

<h2>1. Who we are</h2>
<p>{_COMPANY} (&ldquo;we&rdquo;, &ldquo;us&rdquo;, &ldquo;our&rdquo;) operates the
website <strong>{_DOMAIN}</strong> (&ldquo;Service&rdquo;), a research and educational
platform that aggregates public market data, computes quantitative scores, and
produces editorial commentary on US-listed equities. Use of the Service is governed
by these Terms.</p>
{_business_details_html()}

<h2>2. What the Service is &mdash; and is not</h2>
<h3>What it is</h3>
<ul>
  <li>A research and educational tool that scores ~200 US-listed stocks every few minutes using a quantitative model.</li>
  <li>A presentation of public information &mdash; SEC filings, exchange data, analyst targets, news headlines, social mentions, etc.</li>
  <li>A platform-generated &ldquo;Quant Score&rdquo; that ranks stocks on a 0&ndash;100 scale based on a composite signal we have designed.</li>
  <li>Plain-English summaries of public earnings releases, generated using third-party language models.</li>
</ul>

<h3>What it is NOT</h3>
<ul>
  <li><strong>Not personalised investment advice.</strong> We do not know your age, risk tolerance, financial situation, time horizon, or tax status. Nothing on the Service should be interpreted as a recommendation tailored to your circumstances.</li>
  <li><strong>Not FCA-authorised; not a personal recommendation.</strong> {_COMPANY} is not authorised or regulated by the Financial Conduct Authority (FCA). The Service provides generic research / journalism / commentary on publicly-traded securities and does not give a &ldquo;personal recommendation&rdquo; within the meaning of the FCA Handbook (COBS). We do not solicit funds, manage portfolios, place orders, or hold securities on your behalf.</li>
  <li><strong>Not a brokerage or trading platform.</strong> You cannot buy, sell, or hold securities through {_COMPANY}. To act on any insight, you must use a separate broker.</li>
  <li><strong>Not a guarantee of accuracy or future performance.</strong> Past results do not predict future outcomes. Markets carry risk. The data we display can be delayed, incomplete, or wrong &mdash; we make best-effort updates but offer no warranty.</li>
</ul>

<h2>3. Eligibility</h2>
<p>You must be at least 18 years old and legally capable of entering into a
binding contract in your jurisdiction. By creating an account, you confirm that
the information you provide is accurate and that you understand the
&ldquo;What it is NOT&rdquo; section above.</p>

<h2>4. Your account</h2>
<ul>
  <li>You are responsible for maintaining the security of your login credentials. Notify us at <a href="mailto:{_CONTACT}">{_CONTACT}</a> if you suspect unauthorised access.</li>
  <li>You may not create multiple accounts to evade rate limits, abuse free-tier features, or circumvent paid subscriptions.</li>
  <li>We may suspend or terminate accounts that violate these Terms, abuse the Service, or engage in fraudulent payment activity.</li>
</ul>

<h2>5. Acceptable use</h2>
<p>You agree NOT to:</p>
<ul>
  <li>Scrape, mass-download, or republish our data, scores, or analysis without written permission.</li>
  <li>Reverse-engineer the scoring methodology to produce a competing service.</li>
  <li>Use the Service to make recommendations to third parties for compensation, unless you yourself are an FCA-authorised adviser using {_COMPANY} purely as a research input (in which case our content is just one of many sources you would consider).</li>
  <li>Upload, share, or input information that violates intellectual property rights, third-party privacy, or applicable law.</li>
  <li>Use bots, headless browsers, or automation to interact with the Service in ways that mimic human use, beyond ordinary RSS / API access we explicitly enable.</li>
</ul>

<h2>6. Free and paid plans</h2>
<p>The Service offers a free tier with reasonable usage limits and may, from time
to time, offer a paid &ldquo;Pro&rdquo; tier with enhanced features. Pricing for paid
features is displayed before purchase and processed by our payment processor.
Refunds for paid plans are governed by the Refund Policy linked from
the checkout page; in the absence of a separate Refund Policy, paid subscriptions
are non-refundable except where required by applicable consumer law.</p>
<p>If you are a consumer in the UK, you have a statutory right under the Consumer
Contracts (Information, Cancellation and Additional Charges) Regulations 2013 to
cancel within 14 days of purchase. Because the Service is digital content
supplied immediately, by starting to use a paid feature within that period you
acknowledge that you consent to immediate supply and lose the 14-day right to
cancel, to the extent permitted by law. This does not affect your other
statutory rights.</p>

<h2>7. Intellectual property</h2>
<p>All software, scoring algorithms, editorial commentary, screenshots, branding,
logos, and the Quant Score methodology are the intellectual property of
{_COMPANY}. We grant you a personal, non-exclusive, non-transferable licence to
view and interact with the Service for your own research and decision-making.
Public market data displayed within the Service remains the property of the
respective exchanges and data providers.</p>

<h2>8. Third-party services</h2>
<p>The Service depends on third-party providers including (but not limited to):
Polygon.io, Financial Modeling Prep, Alpha Vantage, Finnhub, Alpaca, Yahoo
Finance, SEC EDGAR, Supabase, Resend, Cloudflare, Railway, and Groq /
Anthropic for language-model output. Outages or data errors at these providers
may affect the Service. {_COMPANY} is not responsible for losses caused by
third-party failures.</p>

<h2>9. Disclaimer of warranties</h2>
<p>The Service is provided <strong>&ldquo;AS IS&rdquo; and &ldquo;AS AVAILABLE&rdquo;</strong>
without warranties of any kind, express or implied, including but not limited to
implied warranties of merchantability, fitness for a particular purpose, accuracy,
or non-infringement. We do not warrant that the Service will be uninterrupted,
error-free, or secure, or that the data displayed will be accurate, timely, or
complete.</p>

<h2>10. Limitation of liability</h2>
<p>To the maximum extent permitted by applicable law, {_COMPANY}'s aggregate
liability arising out of or related to your use of the Service shall not exceed
the amount you have paid us in the twelve (12) months preceding the claim, or
&pound;50 if you are on the free tier. Under no circumstances will we be liable
for indirect, incidental, consequential, special, or punitive damages, including
lost profits, lost trading opportunities, or losses arising from investment
decisions you make. Nothing in these Terms limits or excludes our liability for
death or personal injury caused by our negligence, for fraud, or for anything
that cannot be limited or excluded under applicable law.</p>

<div class="callout">
  <strong>Critical:</strong> Trading and investing carry substantial risk, including
  loss of principal. You may lose more than you invest in leveraged positions.
  Decisions you take based on information shown on {_COMPANY} are entirely your
  own. We do not refund losses, do not compensate for missed opportunities, and
  do not guarantee any outcome.
</div>

<h2>11. Indemnification</h2>
<p>You agree to indemnify and hold harmless {_COMPANY}, its directors, employees,
and affiliates, from any claim, damage, or expense (including reasonable legal
fees) arising from (a) your use of the Service in breach of these Terms, (b) your
trading activity, (c) any third party who suffers loss because they relied on
information you republished from the Service, or (d) your violation of any law
or third-party right.</p>

<h2>12. Modifications to the Service or Terms</h2>
<p>We may modify the Service, pricing, or these Terms from time to time. For
material changes (e.g. price increases on active subscriptions, expanded data
collection, changes to the limitation of liability), we will email registered
users with at least 14 days' notice. Continued use after such notice constitutes
acceptance.</p>

<h2>13. Governing law and disputes</h2>
<p>These Terms, and any dispute or claim arising out of or in connection with
them or the Service (including non-contractual disputes), are governed by the
law of <strong>{_JURIS}</strong>. We will first try to resolve any dispute with
you by good-faith negotiation. If that fails, the courts of {_JURIS} shall have
exclusive jurisdiction, except that if you are a consumer resident elsewhere in
the UK you may also bring proceedings in the courts of your home nation. Nothing
in this clause affects any mandatory statutory rights you have as a consumer.</p>

<h2>14. Severability and entire agreement</h2>
<p>If any provision of these Terms is held unenforceable, the remaining
provisions remain in full effect. These Terms, together with the Privacy Policy
and Disclaimer, constitute the entire agreement between you and {_COMPANY}.</p>

<h2>15. Contact</h2>
<p>Questions about these Terms? Email <a href="mailto:{_CONTACT}">{_CONTACT}</a>.</p>
"""
    return _shell("Terms of Service", "terms", body)


# ╔════════════════════════════════════════════════════════════════════╗
# ║  Page 2: Privacy Policy                                            ║
# ╚════════════════════════════════════════════════════════════════════╝
def render_privacy() -> str:
    body = f"""
<div class="callout callout-green">
  <strong>TL;DR.</strong> We collect the minimum needed to run the Service:
  your email + auth credentials, your watchlist, payment metadata, and standard
  server logs. We do not sell your data. We use essential cookies to run the
  site, and &mdash; only with your consent &mdash; analytics and advertising
  cookies, which you can accept, reject or change at any time via
  <a href="#" data-cc-open class="body-link">Cookie settings</a>.
</div>

<h2>1. Who we are</h2>
<p>{_COMPANY} operates the website <strong>{_DOMAIN}</strong>. Questions about
this Privacy Policy or requests to exercise your rights should be directed to
<a href="mailto:{_CONTACT}" class="body-link">{_CONTACT}</a>.</p>

<h2>2. What we collect</h2>

<h3>2.1 Information you provide directly</h3>
<ul>
  <li><strong>Email address</strong> &mdash; for account creation, password reset, and product updates.</li>
  <li><strong>Authentication credentials</strong> &mdash; password (hashed and salted by Supabase, never visible to us in plain text), or third-party OAuth tokens if you sign in via Google etc.</li>
  <li><strong>Watchlist</strong> &mdash; the tickers you save.</li>
  <li><strong>Subscription metadata</strong> &mdash; if you upgrade to a paid plan: subscription ID, plan, status (active/cancelled). Card details are handled directly by our payment processor; we never see or store full card numbers.</li>
  <li><strong>Optional profile fields</strong> &mdash; name, trading experience, primary goal &mdash; collected during onboarding to tailor what we show you. You can leave these blank.</li>
  <li><strong>Support correspondence</strong> &mdash; emails you send to us for assistance.</li>
</ul>

<h3>2.2 Information we collect automatically</h3>
<ul>
  <li><strong>Server logs</strong> &mdash; IP address, browser user-agent, page URL, timestamp, response code. Used for debugging, abuse prevention, and aggregate analytics. Retained 30&ndash;90 days then deleted.</li>
  <li><strong>Cookies and similar technologies</strong> &mdash; see the dedicated section below. Essential cookies are always used; analytics and advertising cookies are used only if you consent.</li>
  <li><strong>Aggregate usage data</strong> &mdash; how many users hit which pages, used in anonymous form to improve the product.</li>
</ul>

<h3>2.4 Cookies and similar technologies</h3>
<p>We ask for your consent before setting any non-essential cookie, using the
banner shown on your first visit. You can change or withdraw your choice at any
time via <a href="#" data-cc-open class="body-link">Cookie settings</a>. The
categories we use:</p>
<table>
<thead><tr><th>Category</th><th>Purpose</th><th>Consent</th></tr></thead>
<tbody>
<tr><td><strong>Essential</strong></td><td>Sign-in/session, security, and remembering your cookie choice. The site cannot function without these.</td><td>Always on (no consent needed)</td></tr>
<tr><td><strong>Analytics</strong></td><td>Understand how the site is used so we can improve it. Our default analytics (Plausible) is privacy-friendly and sets no cookies; any cookie-based analytics we add will sit in this category.</td><td>Only with your consent</td></tr>
<tr><td><strong>Advertising</strong></td><td>Measure and target our advertising (for example Google Ads and the Meta/Facebook pixel), including conversion tracking. These set cookies and share limited event data with the ad platform.</td><td>Only with your consent</td></tr>
</tbody></table>
<p>Advertising and cookie-based analytics tags do not load at all until you opt
in. If you reject them, they stay off. You can also block or delete cookies in
your browser settings.</p>

<h3>2.3 Information we do NOT collect</h3>
<ul>
  <li>Your trading account, brokerage credentials, or trade history. (We are not a broker; we have no way to access these.)</li>
  <li>Bank account or full credit card numbers.</li>
  <li>Physical address (unless required for a future invoiced enterprise plan, with your consent).</li>
  <li>Government identifiers (national insurance number, passport, etc.).</li>
</ul>

<h2>3. Why we collect it (Purpose &amp; Legal Basis)</h2>
<table>
<thead><tr>
  <th>What</th><th>Why</th><th>Legal basis</th>
</tr></thead><tbody>
<tr><td>Email + password</td><td>Authenticate you; recover lost access</td><td>Necessary for the contract you have with us</td></tr>
<tr><td>Watchlist</td><td>Show you stocks you care about</td><td>Necessary for the contract</td></tr>
<tr><td>Server logs</td><td>Detect abuse, fix bugs, plan capacity</td><td>Legitimate interest</td></tr>
<tr><td>Payment metadata</td><td>Process subscriptions; comply with tax</td><td>Legal obligation + contract</td></tr>
<tr><td>Product update emails</td><td>Tell you about new features</td><td>Your consent (opt out anytime)</td></tr>
</tbody></table>

<h2>4. Who we share it with (Data Processors)</h2>
<p>We use industry-standard third parties to run the Service. Each acts as a
Data Processor on our behalf and is contractually obligated to handle your data
only for the agreed purpose:</p>
<ul>
  <li><strong>Supabase</strong> (database + auth) &mdash; stores your email, password hash, watchlist. Servers in Asia/Singapore.</li>
  <li><strong>Railway</strong> (hosting) &mdash; runs our application servers; sees IP addresses in logs.</li>
  <li><strong>Cloudflare</strong> (CDN + WAF) &mdash; sees IP and request metadata; provides DDoS protection.</li>
  <li><strong>Stripe</strong> (payments) &mdash; receives payment info directly from you when you upgrade. Card details go directly to Stripe; we never see or store them. Stripe is a PCI-DSS Level 1 certified processor.</li>
  <li><strong>Resend</strong> (email delivery) &mdash; delivers transactional emails; sees your email and the message body.</li>
  <li><strong>Groq</strong> &mdash; processes text from public press releases through their language model. We do <em>not</em> send your personal data to Groq, only public earnings text.</li>
  <li><strong>Anthropic / OpenAI</strong> (optional, for editorial features) &mdash; same as above; only public market commentary, never your personal data.</li>
  <li><strong>Plausible Analytics</strong> &mdash; privacy-friendly, cookieless site analytics; does not identify you individually.</li>
  <li><strong>Advertising &amp; measurement partners</strong> (only if you consent to advertising cookies) &mdash; e.g. Google (Google Ads / Google Analytics) and Meta Platforms (the Facebook/Instagram pixel). When enabled, these receive limited event data (such as page views and conversions) and identifiers to measure and target our ads. They act as independent controllers for that data under their own privacy policies. If you do not consent, none of these are loaded.</li>
</ul>

<p><strong>We do not sell your personal data</strong> for money. We do not
share your account data with data brokers. If you consent to advertising
cookies, limited activity data is shared with our advertising partners (above)
so we can measure and target our ads &mdash; you can withdraw that consent at
any time via <a href="#" data-cc-open class="body-link">Cookie settings</a>,
and it will stop.</p>

<h2>5. Cross-border transfers</h2>
<p>Some of our processors (Cloudflare, Stripe, Resend, Groq, Anthropic) operate
servers outside the UK. Where this happens, transfers are made under standard
contractual clauses or equivalent safeguards as required by UK GDPR and the
Data Protection Act 2018.</p>

<h2>6. How long we keep it</h2>
<ul>
  <li><strong>Account data</strong>: as long as your account is active, plus 90 days after deletion (to handle disputes).</li>
  <li><strong>Server logs</strong>: 30&ndash;90 days, then deleted.</li>
  <li><strong>Payment metadata</strong>: 7 years (required by HMRC record-keeping rules).</li>
  <li><strong>Email correspondence</strong>: 2 years from last reply, then archived.</li>
</ul>

<h2>7. Your rights</h2>
<p>Under UK GDPR you have the right to:</p>
<ul>
  <li><strong>Access</strong> &mdash; request a copy of the data we hold about you.</li>
  <li><strong>Correction</strong> &mdash; ask us to fix inaccurate data.</li>
  <li><strong>Erasure</strong> &mdash; delete your account and associated personal data (subject to retention obligations above for billing records).</li>
  <li><strong>Withdraw consent</strong> &mdash; opt out of product update emails anytime via the unsubscribe link.</li>
  <li><strong>Complaint</strong> &mdash; if you are unhappy with how we handle your data you may lodge a complaint with the ICO (ico.org.uk).</li>
</ul>

<p>To exercise any of these rights, email
<a href="mailto:{_CONTACT}" class="body-link">{_CONTACT}</a> from the address
associated with your account. We will respond within 30 days.</p>

<h2>8. Security</h2>
<p>We use industry-standard practices: HTTPS everywhere, password hashing
(bcrypt via Supabase), database row-level security so users can only see their
own data, environment-isolated production credentials, and rate limiting on
authentication endpoints. No system is bullet-proof; if we discover a personal
data breach we will notify affected users and the ICO within the timelines
required by UK GDPR.</p>

<h2>9. Children</h2>
<p>The Service is not intended for users under 18. We do not knowingly collect
personal data from children. If you believe a child has provided us their
data, contact us and we will delete it.</p>

<h2>10. Changes to this Policy</h2>
<p>If we materially change this Policy (e.g. expand the categories of data we
collect, add new processors, change retention periods), we will notify
registered users by email at least 14 days before the change takes effect.</p>

<h2>11. Contact</h2>
<p>Privacy questions or access requests:
<a href="mailto:{_CONTACT}" class="body-link">{_CONTACT}</a>.</p>
"""
    return _shell("Privacy Policy", "privacy", body)


# ╔════════════════════════════════════════════════════════════════════╗
# ║  Page 3: Disclaimer                                                ║
# ╚════════════════════════════════════════════════════════════════════╝
def render_disclaimer() -> str:
    body = f"""
<div class="callout">
  <strong>Read this carefully.</strong> {_COMPANY} is not your financial adviser.
  Nothing on this site &mdash; not the Quant Score, not the verdicts, not the
  guidance summaries, not the AI commentary &mdash; is an instruction to buy,
  sell, or hold any security. You are 100% responsible for any trading or
  investment decision you make. You can lose money. Markets are risky.
</div>

<h2>1. No financial advice</h2>
<p>{_COMPANY} provides general research and educational content about
publicly-traded US equities. We do not know your personal financial situation,
risk tolerance, age, time horizon, tax situation, existing portfolio, or
investment objectives. Nothing on the Service is tailored to you. Even when our
output reads like a recommendation (&ldquo;STRONG BUY&rdquo;, &ldquo;Avoid&rdquo;),
it is a quantitative summary of public information, not advice meant for
your specific circumstances.</p>

<h2>2. Not authorised by the FCA &mdash; no personal recommendation</h2>
<p>{_COMPANY} is not authorised or regulated by the Financial Conduct
Authority (FCA). The Service operates as generic market commentary /
journalism / educational content on publicly available information about
US-listed securities. Nothing on the Service is a &ldquo;personal
recommendation&rdquo; or &ldquo;investment advice&rdquo; as defined under the
Financial Services and Markets Act 2000 (FSMA) and the FCA Handbook (COBS) —
it is not based on, and does not take account of, your individual
circumstances.</p>

<p>If you require <em>personalised</em> investment advice (advice tailored to
your specific financial situation), you must consult an independent financial
adviser authorised and regulated by the FCA. {_COMPANY} cannot and does not
provide such advice.</p>

<h2>3. Our methodology has limits</h2>
<p>The Quant Score is a composite of multiple quantitative signals (momentum,
fundamentals, valuation, social sentiment, etc.) weighted using an internal
algorithm. By design, it cannot capture:</p>
<ul>
  <li>Regulatory or legal risks not yet in public filings</li>
  <li>Executive turnover or culture issues</li>
  <li>Accounting irregularities or fraud</li>
  <li>Geopolitical exposure that is not yet reflected in public news</li>
  <li>Product cycles, customer concentration, or competitive dynamics that are not in the structured data we ingest</li>
  <li>Anything that happened in the last few minutes (we refresh data every ~5 minutes during US market hours)</li>
</ul>

<h2>4. Data may be wrong, late, or incomplete</h2>
<p>Market data on the Service comes from third-party providers (Polygon, FMP,
Alpha Vantage, Finnhub, Yahoo Finance, SEC EDGAR, etc.). These providers
occasionally have outages, lag, or feed errors. Prices may be delayed by up
to 15 minutes on the free plan. EPS estimates and analyst targets are
sourced from public consensus and may be stale. We make best-effort updates
but offer no warranty of accuracy.</p>

<h2>5. AI-generated content disclaimer</h2>
<p>Some content on the Service &mdash; in particular forward-guidance summaries,
earnings highlights, positives/concerns, and Q&amp;A summaries &mdash; is generated
by third-party large language models (currently Llama 3.3 70B via Groq, with
optional Anthropic Claude refinement). LLM output can contain factual errors,
hallucinations, or misleading framing. Always verify critical information
against the source (the SEC filing or company press release). We do not
warrant the accuracy of LLM-generated text and are not responsible for
decisions based on it.</p>

<h2>6. Past performance does not predict future results</h2>
<p>Backtests of our scoring methodology, model-portfolio performance, and any
historical examples are exactly that &mdash; historical. They are not a
guarantee of future returns. A stock that has gained 80% in three months can
fall 80% in the next three. A &ldquo;Strong Buy&rdquo; verdict can be wrong.
Hot lists, watchlists, and the model portfolio are illustrative only.</p>

<h2>7. Forward-looking statements</h2>
<p>Any AI-extracted forward guidance from company press releases is a
restatement of what the company said about itself. Forward-looking statements
issued by companies are not guarantees; companies frequently miss or
withdraw their own guidance.</p>

<h2>8. No solicitation</h2>
<p>Information on the Service is not an offer to sell or a solicitation to
buy any security. Securities laws in the UK and the US restrict who may
publicly recommend specific securities; we do not do so.</p>

<h2>9. You bear all risk</h2>
<p>Trading and investing in equities, options, futures, or any market
instrument involves substantial risk, including total loss of principal and,
in leveraged products, losses exceeding deposit. You should:</p>
<ul>
  <li>Only trade with money you can afford to lose</li>
  <li>Diversify your holdings</li>
  <li>Understand the products you trade</li>
  <li>Consult an FCA-authorised financial adviser for personalised guidance</li>
  <li>Read the offer documents and disclosures of any product before investing</li>
</ul>

<h2>10. Conflicts of interest</h2>
<p>{_COMPANY}, its founders, and its employees may hold positions in
securities mentioned on the Service. We do not currently take payment from
companies in exchange for coverage. If our policy changes, we will disclose
it prominently. We do not receive referral payments from brokers or
exchanges for users who view stock pages.</p>

<h2>11. Accept these terms by using the Service</h2>
<p>By creating an account or continuing to use the Service, you acknowledge
that you have read this Disclaimer, you understand it, and you accept that
all decisions made on the basis of information shown on {_COMPANY} are your
own.</p>

<p>If you do not agree with any part of this Disclaimer, please do not use
the Service.</p>
"""
    return _shell("Disclaimer", "disclaimer", body)
