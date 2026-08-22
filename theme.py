"""Single source of truth for the public site's theme.

Every page outside templates/ used to carry its own hand-rolled CSS: 15 separate
<style> blocks across app.py plus one each in seo_pages.py and legal_pages.py.
The 7 Aug colour migration and the 16 Aug font sweep both operated on
templates/ only, so all of them were left on the old electric-blue / Inter
system while the landing page moved to navy+warm / Public Sans. This module
exists so that cannot happen again: change it here, every page follows.

Palette and type are the landing page's, verbatim. Do not "tidy" the values —
they were measured off the reference, and several carry meaning:
  * #16a34a / #ea384c mean up and down. Nothing else.
  * #FF6100 on white is only 3.4:1 — small orange text on a light surface must
    use #C74E00 (--accent-safe).
  * The wordmark's dot is ALWAYS #FF6100. Only the M changes: #4A5BC4 on
    light, #8FA0F0 on dark. See wordmark().
"""

FONTS_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2'
    '?family=Public+Sans:wght@300;400;500;600'
    '&family=JetBrains+Mono:wght@400;500;600'
    '&family=Space+Grotesk:wght@700'
    '&family=Literata:opsz,wght@7..72,400;7..72,500'
    '&display=swap" rel="stylesheet">'
)

def wordmark(dark: bool = False) -> str:
    """Landing's nav wordmark, copied VERBATIM - same markup, same class names,
    same inline SVG values. The colours come from the CSS below, exactly as they
    do on landing.

    Do not try to derive the colours by grepping landing.html. The winning rule
    is a GROUPED selector split across lines:
        html body .brand-m circle, html body .footer .brand-m circle,
        html body .bg-dark .brand-m circle{fill:#FF6100!important}
    A line-oriented grep misses it and reports the earlier #4A5BC4 rule instead.
    Measured with getComputedStyle, the truth is:
        nav / light    M #4A5BC4   dot #FF6100   word #0A0E22
        footer / dark  M #8FA0F0   dot #FF6100   word #FFFFFF
    """
    return (
        '<a class="brand" href="/"><span class="brand-wordmark">Ticker'
        '<svg class="brand-m" viewBox="0 0 90 105" fill="none" aria-hidden="true">'
        '<polyline points="5,100 23,42 45,66 67,26 85,100" stroke="#14587D" '
        'stroke-width="9" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="67" cy="8" r="7" fill="#14587D"/></svg>over</span></a>'
    )


NAV_ITEMS = [
    ("/reports", "Reports"),
    ("/sectors", "Sectors"),
    ("/compare", "Compare"),
    ("/learn", "Methodology"),
]


def nav_html(active: str = "") -> str:
    links = "".join(
        f'<a href="{h}"{" class=\'is-on\'" if h == active else ""}>{t}</a>'
        for h, t in NAV_ITEMS
    )
    return (
        '<header class="tm-nav"><div class="tm-nav-in">'
        + wordmark()
        + f'<nav class="tm-nav-links">{links}</nav>'
        '<div class="tm-nav-cta">'
        '<a class="tm-ghost" href="/login">Sign in</a>'
        '<a class="tm-pill" href="/login?signup"><span>Start free</span>'
        '<span class="tm-arw">&rarr;</span></a>'
        '</div></div></header>'
    )


def footer_html() -> str:
    """Matches the landing page's footer: brand, link columns, then the
    compliance block. The FCA wording is load-bearing - do not reword it."""
    return (
        '<footer class="tm-foot"><div class="tm-foot-in">'
        '<div class="tm-foot-top">'
        '<div class="tm-foot-brand">' + wordmark(dark=True) +
        '<p>An AI research studio for the US market.</p></div>'
        '<div class="tm-foot-cols">'
        '<div><h4>Explore</h4>'
        '<a href="/reports">Reports</a><a href="/sectors">Sectors</a>'
        '<a href="/compare">Compare</a><a href="/learn">Methodology</a></div>'
        '<div><h4>Product</h4>'
        '<a href="/app">Dashboard</a><a href="/weekly">The Weekly</a>'
        '<a href="/login?signup">Start free</a></div>'
        '<div><h4>Legal</h4>'
        '<a href="/privacy">Privacy</a><a href="/terms">Terms</a>'
        '<a href="/disclaimer">Disclaimer</a>'
        '<a href="/editorial-policy">Editorial policy</a></div>'
        '</div></div>'
        '<p class="tm-foot-legal"><strong>Capital at risk.</strong> TickerMover is a '
        'research and data tool. It does not provide investment advice, recommendations, '
        'or any personal recommendation to buy or sell. Scores, signals and reference '
        'levels are information only. The value of investments can go down as well as up, '
        'and you may get back less than you invest. Past performance and historical scores '
        'are not a reliable indicator of future results. TickerMover is not authorised or '
        'regulated by the Financial Conduct Authority. Do your own research, and consider '
        'advice from an FCA-authorised adviser before investing.</p>'
        '<div class="tm-foot-base"><span>&copy; 2026 TickerMover. All rights reserved.</span>'
        '<a href="mailto:support@tickermover.com">support@tickermover.com</a></div>'
        '</div></footer>'
    )


THEME_CSS = """
:root{
  --primary:#0A2F46; --blue-light:#14587D; --deep:#001C31;
  --peach:#FFF2EC; --alt:#F2F1EE; --surface:#FFFFFF; --rule:#D6DADD;
  --ink:#10293D; --grey:#5d6c7b; --grey-2:#758696;
  --accent:#FF6100; --accent-safe:#C74E00; --on-dark:#8FBFDD;
  --up:#16a34a; --down:#ea384c;
  --font:'Public Sans',-apple-system,BlinkMacSystemFont,system-ui,sans-serif;
  --mono:'JetBrains Mono',ui-monospace,monospace;
  --read:'Literata',Georgia,serif;
  --wrap:1240px;
  --t-fast:140ms; --t-base:240ms; --t-slow:420ms;
  --e-out:cubic-bezier(.2,.7,.2,1); --e-spring:cubic-bezier(.34,1.56,.64,1);
}
*{margin:0;padding:0;box-sizing:border-box}
button,input,select,textarea{font-family:inherit;font-size:inherit;line-height:inherit}
html{scroll-behavior:smooth}
body{font-family:var(--font);font-weight:400;color:var(--ink);background:var(--peach);
  line-height:1.65;font-size:16px;-webkit-font-smoothing:antialiased}
a{color:var(--blue-light);text-decoration:none;font-weight:500}
a:hover{text-decoration:underline}
.mono{font-family:var(--mono);font-feature-settings:'tnum' 1}

/* ---------- nav ---------- */
.tm-nav{position:sticky;top:0;z-index:60;background:rgba(255,242,236,.86);
  backdrop-filter:saturate(150%) blur(10px);border-bottom:1px solid var(--rule)}
.tm-nav-in{max-width:var(--wrap);margin:0 auto;padding:12px 24px;display:flex;
  align-items:center;justify-content:space-between;gap:22px}
/* ---- wordmark: landing.html's rules, verbatim. .tm-foot stands in for
   landing's .footer / .bg-dark. Order matters - the grouped rule is last. ---- */
.brand{display:flex;align-items:center;gap:11px;
  font-family:'Space Grotesk','Public Sans',sans-serif;font-weight:700;font-size:20px;
  letter-spacing:-.02em;color:#0A2F46}
.brand:hover{text-decoration:none}
.brand-wordmark{display:inline-flex;align-items:baseline;flex-wrap:nowrap;
  white-space:nowrap;color:#0A2F46}
.brand-m{height:1.6em;width:auto;flex:none;align-self:baseline;margin:0 .02em}
html body .brand,html body .brand-wordmark{color:#0A0E22!important}
html body .tm-foot .brand,html body .tm-foot .brand-wordmark{color:#FFFFFF!important}
html body .brand-m polyline{stroke:#4A5BC4!important}
html body .tm-foot .brand-m polyline{stroke:#8FA0F0!important}
html body .brand-m circle,html body .tm-foot .brand-m circle{fill:#FF6100!important}
@media(max-width:640px){.brand{font-size:17px;gap:8px}}
.tm-nav-links{display:flex;gap:26px;font-size:15px;font-weight:500}
.tm-nav-links a{color:var(--ink);transition:color var(--t-fast) var(--e-out)}
.tm-nav-links a:hover{color:var(--primary);text-decoration:none}
.tm-nav-links a.is-on{color:var(--primary);font-weight:500}
.tm-nav-cta{display:flex;align-items:center;gap:14px}
.tm-ghost{font-size:15px;color:var(--ink);font-weight:600;
  transition:color var(--t-fast) var(--e-out)}
.tm-ghost:hover{color:var(--primary);text-decoration:none}
.tm-pill{display:inline-flex;align-items:center;gap:10px;background:var(--primary);
  color:#fff;border-radius:100px;padding:8px 8px 8px 18px;font-size:14px;font-weight:500;
  transition:background var(--t-base) var(--e-out)}
.tm-pill:hover{text-decoration:none}
.tm-arw{width:26px;height:26px;border-radius:50%;background:var(--accent);display:grid;
  place-items:center;font-size:13px;transition:transform var(--t-base) var(--e-spring)}
.tm-pill:hover .tm-arw{transform:translateX(5px)}
@media(max-width:820px){.tm-nav-links{display:none}.tm-ghost{display:none}}

/* ---------- page frame ---------- */
.wrap{max-width:900px;margin:0 auto;padding:44px 24px 72px}
.wrap-wide{max-width:var(--wrap);margin:0 auto;padding:44px 24px 72px}
.crumbs{font-family:var(--mono);font-size:10.5px;color:var(--grey-2);margin-bottom:12px;
  letter-spacing:.14em;text-transform:uppercase;font-weight:500}
.crumbs a{color:var(--grey-2);font-weight:500}
.crumbs a:hover{color:var(--accent-safe)}
h1{font-size:clamp(34px,4.4vw,50px);font-weight:500;letter-spacing:-.02em;
  margin-bottom:14px;color:var(--primary);line-height:1.1}
h1 .sym{font-family:var(--mono);font-weight:500;color:var(--blue-light)}
.lede{font-size:19px;line-height:1.6;color:var(--grey);margin-bottom:36px;font-weight:300;
  max-width:78ch}
h2{font-size:27px;font-weight:500;letter-spacing:-.01em;margin:52px 0 14px;color:var(--primary)}
h3{font-size:19px;font-weight:500;letter-spacing:-.01em;margin:30px 0 10px;color:var(--primary)}
p{margin-bottom:16px;color:var(--ink);font-weight:300}
ul,ol{margin:8px 0 22px 22px}
li{margin-bottom:8px;color:var(--ink);font-weight:300}
strong,b{font-weight:500;color:var(--primary)}
.tag{display:inline-block;background:var(--alt);color:var(--blue-light);padding:4px 11px;
  border-radius:100px;font-family:var(--mono);font-size:10px;font-weight:500;
  letter-spacing:.14em;text-transform:uppercase;margin-bottom:16px}
blockquote{border-left:3px solid var(--accent);background:#FFF7F2;padding:16px 22px;
  margin:22px 0;border-radius:0 12px 12px 0;color:var(--ink);font-size:15.5px;font-weight:300}
code{background:var(--alt);padding:2px 6px;border-radius:5px;font-family:var(--mono);
  font-size:13.5px;color:var(--blue-light)}

/* Long-form reading column gets the serif, per the site's type rule:
   sans interface, serif reading column. */
.prose p,.prose li{font-family:var(--read);font-size:16.5px;line-height:1.68;font-weight:400}

/* ---------- cards ---------- */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:16px;
  margin:20px 0 32px}
.card{position:relative;background:var(--surface);border:1px solid var(--rule);
  border-radius:16px;padding:20px 22px;overflow:hidden;
  transition:transform var(--t-base) var(--e-spring),box-shadow var(--t-base) var(--e-out),
             border-color var(--t-base) var(--e-out)}
.card::after{content:"";position:absolute;inset:0;border-radius:inherit;pointer-events:none;
  opacity:0;background:linear-gradient(115deg,transparent 38%,rgba(255,255,255,.38) 50%,transparent 62%);
  background-size:260% 100%;background-position:118% 0;
  transition:opacity var(--t-fast) linear,background-position var(--t-slow) var(--e-out)}
.card:hover{transform:translateY(-3px);border-color:var(--primary);
  box-shadow:0 12px 30px rgba(10,47,70,.12);text-decoration:none}
.card:hover::after{opacity:1;background-position:-38% 0}
.card a{color:inherit;text-decoration:none;display:block}
.card .ttl{font-size:16px;font-weight:500;color:var(--primary);margin-bottom:5px}
.card .sub{font-size:13.5px;color:var(--grey);line-height:1.55;font-weight:300}

/* ---------- table ---------- */
.tbl{width:100%;border-collapse:collapse;margin:18px 0 32px;font-size:14.5px;
  background:var(--surface);border:1px solid var(--rule);border-radius:14px;overflow:hidden}
.tbl th{background:var(--alt);text-align:left;padding:12px 15px;font-family:var(--mono);
  font-size:10px;font-weight:500;color:var(--grey-2);letter-spacing:.14em;
  text-transform:uppercase;border-bottom:1px solid var(--rule)}
.tbl td{padding:12px 15px;border-bottom:1px solid #ECEEF0;vertical-align:top;font-weight:300}
.tbl tr:last-child td{border-bottom:none}
.tbl tr:hover{background:#FBFAF8}
.tbl .tk{font-family:var(--mono);font-weight:600;color:var(--blue-light)}
.tbl .pop{font-family:var(--mono);font-weight:600;font-variant-numeric:tabular-nums}
.tbl .vd{color:var(--grey);font-size:13.5px;line-height:1.55;font-weight:300}
/* Grade ramp: one good-to-bad scale. Green and red keep their only meaning. */
.tbl .grade{display:inline-block;min-width:26px;text-align:center;padding:3px 0;
  border-radius:6px;font-family:var(--mono);font-weight:600;font-size:12px;color:#fff}
.tbl .grade.A{background:#16a34a}
.tbl .grade.B{background:#14587D}
.tbl .grade.C{background:#b45309}
.tbl .grade.D{background:#C74E00}
.tbl .grade.F{background:#ea384c}
.up{color:var(--up)} .dn{color:var(--down)}

/* ---------- compare ---------- */
.cmp-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:20px 0 32px}
.cmp-card{background:var(--surface);border:1px solid var(--rule);border-radius:16px;padding:22px}
.cmp-card .tk{font-family:var(--mono);font-size:25px;font-weight:600;color:var(--blue-light)}
.cmp-card .nm{font-size:14px;color:var(--grey);margin-bottom:16px;font-weight:300}
.cmp-row{display:flex;justify-content:space-between;gap:12px;padding:9px 0;
  border-bottom:1px solid #F1EFEC;font-size:14px}
.cmp-row:last-child{border-bottom:none}
.cmp-row .k{color:var(--grey);font-weight:400}
.cmp-row .v{font-family:var(--mono);font-weight:600;color:var(--primary)}
.cmp-vs{text-align:center;font-family:var(--mono);font-weight:500;color:var(--grey-2);
  font-size:11px;letter-spacing:.16em;text-transform:uppercase;margin:6px 0}
@media(max-width:640px){.cmp-grid{grid-template-columns:1fr}}

/* ---------- CTA ---------- */
.cta{margin-top:52px;padding:34px 34px;border-radius:20px;text-align:center;color:#fff;
  background-image:radial-gradient(circle at 0% 0%,#001C31 -55%,#0A2F47 38%,#0A2F47 55%,#001C31 100%)}
.cta h3{font-size:23px;font-weight:500;letter-spacing:-.01em;margin-bottom:8px;color:#fff}
.cta p{color:#CFE0EA;margin-bottom:20px;font-weight:300}
.cta-btn{display:inline-flex;align-items:center;gap:10px;background:#fff;color:var(--primary);
  padding:11px 24px;border-radius:100px;font-weight:500;font-size:14.5px;
  transition:background var(--t-base) var(--e-out)}
.cta-btn:hover{text-decoration:none}

/* ---------- newsletter ---------- */
.nl{margin-top:52px;padding:30px 26px;background:var(--surface);border:1px solid var(--rule);
  border-radius:18px}
.nl h3{margin:0 0 6px;font-size:19px;font-weight:500}
.nl p{margin:0 0 16px;color:var(--grey);font-size:14.5px;font-weight:300}
.nl form{display:flex;gap:9px;flex-wrap:wrap}
.nl input[type=email]{flex:1;min-width:220px;padding:12px 15px;border:1px solid var(--rule);
  border-radius:100px;font-size:14.5px;font-family:inherit;background:#fff}
.nl input[type=email]:focus{outline:none;border-color:var(--blue-light);
  box-shadow:0 0 0 3px rgba(20,88,125,.15)}
.nl button{padding:12px 24px;background:var(--primary);color:#fff;border:none;
  border-radius:100px;font-weight:500;font-size:14.5px;cursor:pointer;font-family:inherit;
  transition:background var(--t-base) var(--e-out)}

.nl .nl-msg{margin-top:10px;font-size:13.5px;font-weight:500;min-height:18px}
.nl .nl-msg.ok{color:var(--up)} .nl .nl-msg.err{color:var(--down)}
.nl-honey{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}

/* ---------- footer ---------- */
.tm-foot{background-image:radial-gradient(circle at 0% 0%,#001C31 -55%,#0A2F47 35%,#0A2F47 50%,#001C31 100%);
  color:#CFE0EA;margin-top:0}
.tm-foot-in{max-width:var(--wrap);margin:0 auto;padding:52px 24px 34px}
.tm-foot-top{display:flex;justify-content:space-between;gap:40px;flex-wrap:wrap;
  padding-bottom:30px;border-bottom:1px solid rgba(255,255,255,.12)}
.tm-foot-brand .brand{margin-bottom:12px}
.tm-foot-brand p{color:rgba(255,255,255,.62);font-size:14.5px;margin-top:10px;font-weight:300}
.tm-foot-cols{display:flex;gap:56px;flex-wrap:wrap}
.tm-foot-cols h4{font-family:var(--mono);font-size:10px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--on-dark);font-weight:500;margin-bottom:12px}
.tm-foot-cols a{display:block;color:rgba(255,255,255,.78);font-size:14.5px;font-weight:300;
  margin-bottom:9px;transition:color var(--t-fast) var(--e-out)}
.tm-foot-cols a:hover{color:var(--accent);text-decoration:none}
.tm-foot-legal{font-size:12.5px;line-height:1.62;color:rgba(255,255,255,.5);
  padding:26px 0;border-bottom:1px solid rgba(255,255,255,.12);font-weight:300}
.tm-foot-legal strong{color:rgba(255,255,255,.8);font-weight:500}
.tm-foot-base{display:flex;justify-content:space-between;gap:18px;flex-wrap:wrap;
  padding-top:22px;font-size:13px;color:rgba(255,255,255,.5);font-weight:300}
.tm-foot-base a{color:rgba(255,255,255,.68);font-weight:300}

@media(max-width:640px){
  .wrap,.wrap-wide{padding:32px 18px 56px}
  .tbl{font-size:13px}.tbl td,.tbl th{padding:10px 9px}
  .tm-foot-cols{gap:32px}
}
@media(prefers-reduced-motion:reduce){
  *{animation:none!important;transition:none!important}
  .card::after{display:none}
}

/* ---------- button hover: the inset orange flood ----------
   A plain darken (#0A2F46 -> #0D3A56, or white -> #F2F1EE) is invisible on
   these two buttons because, unlike landing's .btn, they have no arrow to move
   - so there was no perceptible feedback at all. This is the site's own
   "vibrant" treatment: an inset box-shadow flooding 0 -> 60px, painted UNDER
   the label. The flood is --accent-safe (#C74E00), not #FF6100: the label goes
   white, and white on #FF6100 is only ~3.1:1. On #C74E00 it is ~5.4:1. Deliberately NOT an @property animation - forcing --btn-fill with
   !important pins it at 0% and the sweep never runs. */
.tm-pill,.cta-btn,.nl button{position:relative;z-index:0;overflow:hidden}
.tm-pill::before,.cta-btn::before,.nl button::before{content:"";position:absolute;
  inset:0;border-radius:inherit;z-index:-1;box-shadow:inset 0 0 0 0 var(--accent-safe);
  transition:box-shadow var(--t-slow) cubic-bezier(.22,.9,.28,1)}
.tm-pill:hover::before,.cta-btn:hover::before,.nl button:hover::before{
  box-shadow:inset 0 0 0 60px var(--accent-safe)}
.tm-pill:hover,.nl button:hover{color:#fff}
.cta-btn:hover{color:#fff}
/* the pill's arrow is already orange - invert it so it stays visible in the flood */
.tm-pill:hover .tm-arw{background:#fff;color:var(--accent-safe)}
@media(prefers-reduced-motion:reduce){
  .tm-pill::before,.cta-btn::before,.nl button::before{transition:none}
}
"""
