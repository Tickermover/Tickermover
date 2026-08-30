"""
TickerMover - Legal pages (Terms, Privacy, Disclaimer)

Three plain-English legal documents. The business is relocating to the UK,
so the REGULATORY DISCLAIMER has been converted to FCA framing (generic
research / not a personal recommendation under FSMA + the FCA Handbook).

⚠️  DRAFT — NOT REVIEWED BY A SOLICITOR.
   Scope narrowed 30 Aug 2026: TickerMover is run as a free, non-commercial
   project. No payments, no subscription, no advertising, no sponsored
   coverage. That removes the paid-launch and financial-promotion pressure
   these pages were originally drafted against, but it does NOT make them
   solicitor-approved. Reinstate a legal review before charging for
   anything, running ads, or taking money from covered companies.
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
  REGULATORY POSTURE (Aug 2026). The disclaimer used to rest on a "generic
  commentary / journalism / educational content" characterisation. The
  journalism half of that has been REMOVED, here and on /terms. Art.20 FPO
  carries a principal-purpose test — the publication's principal purpose must
  not be to lead or enable people to buy or sell securities — and a ranked
  database of scored securities does not pass it. Claiming an exemption we
  would fail is worse than not claiming one.

  What replaces it is the posture an unauthorised UK research site can
  actually hold: a research and screening tool, and an educational resource.
  Not a broker or dealer, not an investment adviser, not a tip sheet or
  advisory service, no specific buy or sell recommendations, no personalised
  recommendations. Those exact statements now appear on /terms, /disclaimer
  and /editorial-policy. This mirrors how the closest UK comparable operates
  unauthorised at scale.

  The content rules that make that characterisation true — rather than merely
  asserted in small print — are published at /editorial-policy, and the
  advice-shaped features are switched off at config.MODEL_PORTFOLIO_ENABLED /
  TRADE_PLAN_ENABLED / TRACK_RECORD_PUBLIC.

  STILL OUTSTANDING for the solicitor: confirm the financial-promotions
  position under s.21 FSMA on the content that remains (quality scores on
  named securities, published free to the general public) BEFORE spending on
  ads; verify data-processor list and cross-border transfer basis; fill the
  trading-entity details.

  * FCA generic-research framing — TickerMover is a research/educational
    tool, NOT authorised or regulated by the FCA, and gives no personal
    recommendation to any specific user.
  * Data-feed providers (Polygon, FMP, Alpha Vantage, Finnhub, Alpaca,
    yfinance, SEC EDGAR) in the data-flow disclosure.
  * Limitation of liability: flat £50 cap, since the Service is free and
    there are no fees to measure against.

Drafted in-house; reviewed by no lawyer. Do NOT rely on this as legal advice.
It is written for a free, non-commercial site; charging, advertising or taking
payment from covered companies would all require a solicitor to look first.
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
# UK e-commerce / consumer law requires a TRADING ENTITY to identify itself.
# TickerMover is not trading — it is a free non-commercial project — so these
# are deliberately unset and _business_details_html() prints a non-commercial
# statement instead. Set them only if it ever becomes a business, at which
# point the Companies Act Part 41 disclosure duty starts to apply.
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
    # No entity is configured and none is expected: TickerMover is run as a
    # non-commercial personal project, not a trading business. The Companies
    # Act Part 41 disclosure duty applies to businesses trading under a name,
    # so it does not bite here. Setting LEGAL_ENTITY_NAME would switch this
    # back to the trading-entity block above.
    return (
        '<div class="callout"><strong>Who runs this.</strong> TickerMover is a '
        'free, non-commercial research project run by an individual. It is not a '
        'registered company and not a business: there is no subscription, no paid '
        'tier, no advertising and no sponsored coverage. Nothing on the site is '
        'sold, and we take no payment from anyone &mdash; readers or the companies '
        'covered. You can reach us at '
        f'<a href="mailto:{_CONTACT}" class="body-link">{_CONTACT}</a>.</div>'
    )


# ── Shared styling + header / footer ────────────────────────────────

def _shell(title: str, slug: str, body_html: str) -> str:
    """Wrap legal body content in the shared site theme.

    This shell used to carry its own CSS: Instrument Sans (deleted from the
    type system in Aug), the pre-warm icy #cdeef8 ground, and an
    @property --btn-fill sweep that is a known trap. It now renders through
    theme.py like every other public page.

    Legal copy stays in the interface sans, not the reading serif: these are
    reference documents people scan for a clause, not prose they read start to
    finish. Not one word of the body copy is touched here.
    """
    import theme as _theme
    tabs = [("/terms", "Terms"), ("/privacy", "Privacy"),
            ("/disclaimer", "Disclaimer"),
            ("/editorial-policy", "Editorial policy")]
    tab_html = "".join(
        f'<a href="{h}"{' class="is-on"' if h.strip("/") == slug else ''}>{t}</a>'
        for h, t in tabs
    )
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
<meta name="theme-color" content="#0A2F46">
{_theme.FONTS_LINK}
<style>{_theme.THEME_CSS}
/* ---- legal-specific ---- */
.legal-page{{max-width:860px;margin:0 auto;padding:44px 24px 72px}}
.legal-crumbs{{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--grey-2);font-weight:500;margin-bottom:12px}}
.legal-crumbs a{{color:var(--grey-2)}}
.legal-sub{{font-size:18px;line-height:1.6;color:var(--grey);font-weight:300;
  margin:0 0 28px;max-width:74ch}}
.legal-tab-nav{{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 36px;
  padding-bottom:22px;border-bottom:1px solid var(--rule)}}
.legal-tab-nav a{{padding:8px 18px;border-radius:100px;border:1px solid var(--rule);
  background:var(--surface);color:var(--ink);font-size:14px;font-weight:400;
  transition:border-color var(--t-fast) var(--e-out),color var(--t-fast) var(--e-out)}}
.legal-tab-nav a:hover{{text-decoration:none;border-color:var(--primary)}}
.legal-tab-nav a.is-on{{background:var(--primary);color:#fff;border-color:var(--primary)}}
.legal-page h2{{font-size:22px;margin:44px 0 12px}}
.legal-page h3{{font-size:17px;margin:26px 0 8px}}
.legal-page p,.legal-page li{{font-size:15.5px;line-height:1.7}}
.body-link{{color:var(--blue-light);font-weight:400;text-decoration:underline;
  text-underline-offset:2px}}
.callout{{border-left:3px solid var(--accent);background:#FFF7F2;padding:16px 20px;
  border-radius:0 12px 12px 0;margin:22px 0}}
.callout p:last-child{{margin-bottom:0}}
.callout-green{{border-left-color:var(--up);background:#F0F8F2}}
</style>
</head>
<body>
{_theme.nav_html()}
<main class="legal-page">
  <div class="legal-crumbs"><a href="/">TickerMover</a> &rsaquo; {title}</div>
  <h1>{title}</h1>
  <div class="legal-tab-nav">{tab_html}</div>
  {body_html}
</main>
{_theme.footer_html()}
</body>
</html>"""



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
  <li><strong>Not FCA-authorised; not a personal recommendation.</strong> {_COMPANY} is not authorised or regulated by the Financial Conduct Authority (FCA). We are not a broker or a dealer and we are not an investment adviser. The Service is a research and screening tool providing generic research and commentary on publicly-traded securities; it is not a tip sheet or an advisory service, makes no specific buy or sell recommendations, and does not give a &ldquo;personal recommendation&rdquo; within the meaning of the FCA Handbook (COBS). We do not solicit funds, manage portfolios, place orders, or hold securities on your behalf.</li>
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
  <li>You may not create multiple accounts to evade rate limits or abuse the Service.</li>
  <li>We may suspend or terminate accounts that violate these Terms or abuse the Service.</li>
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

<h2>6. The Service is free</h2>
<p>Every feature is free to every user. There is no paid tier, no subscription,
no trial that converts to a charge, and no payment method is collected. We do not
take payment from readers, and we do not take payment from the companies we write
about &mdash; there is no sponsored or promoted coverage.</p>
<p>Some features are labelled &ldquo;Pro&rdquo; in the interface. That labelling is
left over from an earlier plan and is inactive: those features are available to
everyone. If that ever changes we will say so here and by email before it takes
effect, and nothing will begin charging without you actively agreeing to it.</p>
<p>Because nothing is sold, the consumer cancellation rules that apply to paid
digital content do not arise. Your statutory rights are unaffected.</p>

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
&pound;50. The Service is provided free of charge and you pay us nothing, so
there is no larger sum to measure a claim against. Under no circumstances will we be liable
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
material changes (e.g. expanded data
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
  your email + auth credentials, your watchlist, and standard
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
investment objectives. Nothing on the Service is tailored to you. Where a
score or a summary reads like a view on a company, it is a quantitative
summary of public information, not advice meant for your specific
circumstances.</p>

<h2>2. Not authorised by the FCA &mdash; no personal recommendation</h2>
<p>{_COMPANY} is not authorised or regulated by the Financial Conduct
Authority (FCA). We are not a broker or a dealer, and we are not an
investment adviser. The Service is a research and screening tool, and an
educational resource for analysing and discussing general and generic
information about publicly-traded US equities.</p>

<p>{_COMPANY} is not a tip sheet or an advisory service, and does not make
specific buy or sell recommendations. We do not provide personalised
recommendations, or any view as to whether a particular stock or investment
approach is suited to the financial needs of any particular person. Nothing
on the Service is a &ldquo;personal recommendation&rdquo; or &ldquo;investment
advice&rdquo; as defined under the Financial Services and Markets Act 2000
(FSMA) and the FCA Handbook (COBS) — it is not based on, and does not take
account of, your individual circumstances.</p>

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
<p>Backtests of our scoring methodology and any historical examples are
exactly that &mdash; historical. They are not a guarantee of future returns.
A stock that has gained 80% in three months can fall 80% in the next three.
A high score can be wrong. Hot lists, screens and watchlists are illustrative
only. Past performance is not a reliable indicator of future results, and is
not a guide to future performance.</p>

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


def render_editorial_policy() -> str:
    """Published content standards.

    Modelled on the content guidelines an unauthorised research site needs in
    order to stand behind the characterisation its disclaimer relies on. The
    disclaimer asserts that nothing here is a recommendation; this page is the
    standing rule that makes that assertion true of the content itself, rather
    than only of the small print. It is written to be enforceable against our
    own output, including the AI-generated parts.
    """
    body = f"""
<div class="callout">
  <strong>Why this page exists.</strong> {_COMPANY} is not authorised by the
  FCA, and does not give advice or make recommendations. That is not only a
  statement in our <a href="/disclaimer" class="body-link">Disclaimer</a> —
  it is a rule we hold our own content to. This page sets out what we will and
  will not publish, and how to hold us to it.
</div>

<h2>1. What the Service is for</h2>
<p>{_COMPANY} exists to help people research publicly-traded US companies
using public information: company filings, reported financials, prices, and
quantitative scores computed from them. It is a research and screening tool
and an educational resource.</p>

<p>We are not a broker or a dealer, and we are not an investment adviser. It
is not a tip sheet, an advisory service, a newsletter of share tips, or a
signal service. Its purpose is not to lead or enable anyone to buy or sell
a particular security. If you are looking for someone to tell you what to
buy, this is the wrong site, and we would rather say so plainly than take
your attention under a misunderstanding.</p>

<h2>2. What we will not publish</h2>
<p>These are standing prohibitions on our own content, including content
generated by our AI models:</p>
<ul>
  <li><strong>No buy, sell or hold instructions</strong> on any named
      security, and no wording that functions as one.</li>
  <li><strong>No entry prices, price targets, stop losses, position sizes or
      trade plans.</strong> A score describes a company; an entry price and a
      position size tell someone what to do with their own money.</li>
  <li><strong>No personalised content.</strong> We do not know your
      circumstances and will not publish anything that presumes them.</li>
  <li><strong>No urgency or pressure devices</strong> — no countdowns, no
      &ldquo;act now&rdquo;, no manufactured scarcity, no flashing or
      animated prompts to trade.</li>
  <li><strong>No performance claim as a marketing hook</strong> — no
      &ldquo;beat the market&rdquo;, no hit rates or benchmark-relative
      records used to sell the Service.</li>
  <li><strong>No promise or projection of returns</strong>, and no
      presentation of a forecast as though it were a fact.</li>
</ul>

<h2>3. Balance</h2>
<p>Where we publish a view on a company, it carries the case against as well
as the case for. We avoid excessive promotion or excessive criticism of any
specific stock, company or strategy. A page that only argues one side is a
defect, and we treat it as one.</p>

<h2>4. Evidence and sourcing</h2>
<p>Factual claims about a company should be traceable to a public source —
an SEC filing, a company release, a reported financial statement, or market
data from one of our named providers. Scores are computed from that data by a
published method, described in our <a href="/learn" class="body-link">methodology</a>
pages. Where a number is an estimate, a consensus or a third-party opinion,
we attribute it rather than presenting it as our own finding.</p>

<h2>5. AI-generated content</h2>
<p>Much of the written commentary on the Service is produced by large language
models working from our data. We hold that content to the same rules as
everything else: the prohibitions in section 2 are built into the prompts
themselves, not applied afterwards.</p>

<p>AI output can still be wrong, and can be confidently wrong. It is not a
substitute for reading the filing. Where a decision matters, verify the
underlying source. We do not warrant the accuracy of model-generated text.</p>

<h2>6. Disclosure of interests</h2>
<ul>
  <li>{_COMPANY}, its founders and anyone writing for it may hold positions
      in securities covered on the Service.</li>
  <li>We do <strong>not</strong> accept payment from companies, investor
      relations firms, or any third party in exchange for coverage, for a
      score, or for favourable treatment. No security appears on this site
      because someone paid for it to.</li>
  <li>We do <strong>not</strong> receive referral payments from brokers or
      exchanges for users who view a stock page.</li>
  <li>If any of the above changes, we will disclose it prominently and on the
      page where it applies — not only here.</li>
  <li>Any sponsored or paid content, if we ever run it, will be labelled as
      such at the point of display.</li>
</ul>

<h2>7. Corrections</h2>
<p>We publish a large amount of computed and generated content, and some of it
will be wrong. If you find an error — a misstated figure, a broken
calculation, a claim that is not supported by the filing it cites — tell us at
<a href="mailto:{_CONTACT}" class="body-link">{_CONTACT}</a> and we will
correct it. Corrections to substantive factual errors are made on the page
itself rather than silently.</p>

<h2>8. Holding us to this</h2>
<p>If you believe something on the Service reads as advice, as a
recommendation, or as pressure to trade, that is a defect in our content and
we want to know. Write to
<a href="mailto:{_CONTACT}" class="body-link">{_CONTACT}</a> quoting the page.
We would rather remove a sentence than blur the line this page draws.</p>

<p class="legal-sub" style="margin-top:36px">Last updated {_EFFECTIVE_DATE}.</p>
"""
    return _shell("Editorial policy", "editorial-policy", body)
