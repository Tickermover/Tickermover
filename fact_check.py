"""fact_check.py — per-stock fundamentals fact-check card for /report pages.

Renders a due-diligence card modelled on the "prime stock" fact-check:
  • a plain-English business line,
  • a data-derived fundamentals checklist (profitable, revenue growing,
    valuation vs sector, returns capital, earnings schedule),
  • a short "your own checks" prompt (self-assessment items),
  • a "what management said" section loaded client-side from
    /api/event-intel/{ticker} (AI earnings-call summary, cached, degrades
    gracefully when unavailable),
  • a key-catalyst line.

FCA-safe by design: the checklist is factual data checks and the management
section is clearly-labelled commentary — framed as research, never as a
personal recommendation or a buy/sell call. Non-superlative header.

The whole card is returned as a self-contained string (scoped CSS + HTML +
a small script), so app.py only needs one injection point.
"""
from __future__ import annotations

import html as _html
from statistics import median


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return f
    except (TypeError, ValueError):
        return None


def _sector_pe_median(t: dict, universe) -> float | None:
    sec = (t.get("sector") or "").strip()
    if not sec or not universe:
        return None
    pes = []
    for r in universe:
        if (r.get("sector") or "").strip() != sec:
            continue
        pe = _num(r.get("forward_pe")) or _num(r.get("pe_ratio"))
        if pe and pe > 0:
            pes.append(pe)
    return median(pes) if len(pes) >= 3 else None


def build_checklist(t: dict, universe) -> list[dict]:
    """Data-derived checks. state: True=pass, False=miss, None=unknown
    (unknown renders neutral, never as a false negative)."""
    checks: list[dict] = []

    # Profitable
    ni = _num(t.get("net_income"))
    eps = _num(t.get("eps")) or _num(t.get("eps_ttm"))
    pm = _num(t.get("profit_margin"))
    if ni is not None or eps is not None or pm is not None:
        profitable = (ni is not None and ni > 0) or (eps is not None and eps > 0) or (pm is not None and pm > 0)
        checks.append({"label": "Profitable",
                       "detail": "Positive earnings on the latest reported data" if profitable
                                 else "Not profitable on the latest reported earnings",
                       "state": bool(profitable)})
    else:
        checks.append({"label": "Profitable", "detail": "Earnings data unavailable", "state": None})

    # Revenue growing
    # The renderer and every metric panel read revenue_growth_yoy; reading only
    # revenue_growth made this check report "Growth data unavailable" on the same
    # page that printed +1.5% twice.
    rg = _num(t.get("revenue_growth"))
    if rg is None:
        rg = _num(t.get("revenue_growth_yoy"))
    if rg is not None:
        rgp = rg * 100 if abs(rg) <= 1.5 else rg
        checks.append({"label": "Revenue growing",
                       "detail": f"{'+' if rgp >= 0 else ''}{rgp:.1f}% latest reported growth",
                       "state": rgp > 0})
    else:
        checks.append({"label": "Revenue growing", "detail": "Growth data unavailable", "state": None})

    # Valuation vs sector
    pe = _num(t.get("forward_pe")) or _num(t.get("pe_ratio"))
    med = _sector_pe_median(t, universe)
    sec = (t.get("sector") or "its sector").strip() or "its sector"
    if pe and pe > 0 and med:
        checks.append({"label": "Reasonably valued vs sector",
                       "detail": f"P/E {pe:.1f} vs {sec} median {med:.1f}",
                       "state": pe <= med})
    elif pe and pe > 0:
        checks.append({"label": "Valuation", "detail": f"P/E {pe:.1f} (no sector benchmark)", "state": None})
    else:
        checks.append({"label": "Valuation", "detail": "P/E unavailable (often means no positive earnings)", "state": None})

    # Returns capital
    dy = _num(t.get("dividend_yield"))
    if dy is not None:
        # Stored as a percent already - see app.py::_sppct_pct. The old
        # "dy <= 1 -> x100" rule printed MA as 61.00% and AAPL as 34.00%.
        dyp = dy
        if dyp > 0:
            checks.append({"label": "Returns capital to shareholders",
                           "detail": f"Dividend yield {dyp:.2f}%", "state": True})
        else:
            checks.append({"label": "Returns capital to shareholders",
                           "detail": "No dividend — may reinvest for growth", "state": False})
    else:
        checks.append({"label": "Returns capital to shareholders", "detail": "Dividend data unavailable", "state": None})

    # Earnings schedule
    ed = t.get("earnings_date") or t.get("next_earnings_date")
    if ed:
        checks.append({"label": "Earnings schedule known",
                       "detail": f"Next report around {_html.escape(str(ed))}", "state": True})
    else:
        checks.append({"label": "Earnings schedule known", "detail": "Next report date to be confirmed", "state": None})

    return checks


def _business_line(t: dict) -> str:
    desc = (t.get("description") or t.get("long_business_summary") or "").strip()
    if desc:
        # first 2 sentences, capped
        cut = desc[:360]
        if len(desc) > 360:
            cut = cut.rsplit(" ", 1)[0] + "…"
        return _html.escape(cut)
    name = _html.escape(t.get("name") or t.get("ticker") or "This company")
    sector = _html.escape(t.get("sector") or "")
    industry = _html.escape(t.get("industry") or t.get("sub_industry") or "")
    tail = f" in {industry}" if industry else (f" in the {sector} sector" if sector else "")
    return f"{name} is a US-listed company{tail}. A fuller business description isn't available yet."


def _key_catalyst_line(t: dict) -> str:
    ed = t.get("earnings_date") or t.get("next_earnings_date")
    cat = (t.get("catalyst") or t.get("key_catalyst") or "").strip()
    if cat:
        return _html.escape(cat)
    if ed:
        return f"Next scheduled catalyst is the earnings report around {_html.escape(str(ed))}."
    return "No dated near-term catalyst on file — watch the next earnings report and analyst revisions."


def render_card(t: dict, universe, public: bool = False) -> str:
    """`public=True` is the /stocks SEO page: it drops "Your own checks",
    an interactive personal checklist that only means anything to a
    logged-in reader with a portfolio."""
    sym = _html.escape((t.get("ticker") or "").upper())
    checks = build_checklist(t, universe)
    n_pass = sum(1 for c in checks if c["state"] is True)
    n_total = len(checks)
    biz = _business_line(t)
    catalyst = _key_catalyst_line(t)

    def _row(c):
        st = c["state"]
        cls = "on" if st is True else ("off" if st is False else "na")
        mark = "✓" if st is True else ("" if st is False else "–")
        return (f'<li class="fchk-item fchk-{cls}"><span class="fchk-box">{mark}</span>'
                f'<span class="fchk-txt"><b>{_html.escape(c["label"])}</b>'
                f'<span class="fchk-det">{c["detail"]}</span></span></li>')

    checklist_html = "".join(_row(c) for c in checks)

    self_items = [
        "I can explain in one sentence why this business makes money",
        "This position fits my own risk limits (e.g. a sector cap)",
        "I've checked how it fits the rest of my holdings",
    ]
    self_html = "".join(
        f'<li class="fchk-item fchk-self"><span class="fchk-box"></span>'
        f'<span class="fchk-txt">{_html.escape(s)}</span></li>' for s in self_items)

    self_block = "" if public else (
        '<div class="fchk-sub">Your own checks</div>'
        f'<ul class="fchk-list">{self_html}</ul>')
    return f"""
<style>
.fchk{{border:1px solid rgba(10,10,10,.1);border-radius:16px;padding:24px 26px;margin:36px 0;background:#fff;box-shadow:0 10px 30px -24px rgba(10,10,10,.25)}}
.fchk-head{{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:14px}}
.fchk-h{{font-family:'Public Sans',system-ui,sans-serif;font-size:20px;font-weight:500;color:#0A2F46;margin:0}}
.fchk-score{{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:12.5px;font-weight:700;letter-spacing:.02em;color:#116B35;background:rgba(21,128,61,.1);border:1px solid rgba(21,128,61,.28);padding:6px 12px;border-radius:999px;white-space:nowrap}}
.fchk-biz{{font-size:14.5px;line-height:1.6;color:#4A5563;margin:0 0 18px}}
.fchk-biz b{{color:#0A2F46}}
.fchk-list{{list-style:none;margin:0 0 10px;padding:0}}
.fchk-item{{display:flex;gap:12px;align-items:flex-start;padding:9px 0;border-top:1px solid rgba(10,10,10,.06)}}
.fchk-item:first-child{{border-top:none}}
.fchk-box{{flex:0 0 20px;width:20px;height:20px;border-radius:6px;border:1.5px solid rgba(10,10,10,.22);display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:800;margin-top:1px}}
.fchk-on .fchk-box{{background:#15803d;border-color:#15803d;color:#fff}}
.fchk-na .fchk-box{{color:#5B6673;border-style:dashed}}
.fchk-txt{{font-size:14px;color:#0A2F46;display:flex;flex-direction:column;gap:2px}}
.fchk-det{{font-size:12.5px;color:#4A5563;font-weight:500}}
.fchk-self .fchk-txt{{color:#4A5563;font-size:13.5px;padding-top:1px}}
.fchk-sub{{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#5B6673;margin:18px 0 6px}}
.fchk-mgmt-row{{display:grid;grid-template-columns:150px 1fr;gap:14px;padding:10px 0;border-top:1px solid rgba(10,10,10,.06);font-size:13.5px}}
.fchk-mgmt-row .k{{color:#4A5563;font-weight:600}}
.fchk-mgmt-row .v{{color:#10293D;line-height:1.55}}
.fchk-note{{font-size:12px;line-height:1.6;color:#5B6673;margin:16px 0 0;border-top:1px solid rgba(10,10,10,.06);padding-top:12px}}
.fchk-cat{{background:#FBFAF8;border:1px solid rgba(10,10,10,.08);border-radius:10px;padding:12px 14px;margin-top:14px;font-size:13.5px;color:#4A5563}}
.fchk-cat b{{color:#0A2F46}}
/* Thesis audit — four areas chosen for this business, two cited checks each. */
.fca-area{{border-top:1px solid rgba(10,10,10,.07);padding:15px 0 13px}}
.fca-area:first-child{{border-top:none}}
.fca-head{{display:flex;justify-content:space-between;align-items:baseline;gap:12px}}
.fca-name{{font-size:15px;font-weight:800;color:#0A2F46;line-height:1.3}}
.fca-v{{flex:none;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:10.5px;font-weight:800;letter-spacing:.08em;padding:3px 10px;border-radius:999px;border:1px solid;white-space:nowrap}}
.fca-pass .fca-v,.fca-pass .fca-cv{{color:#116B35;border-color:rgba(21,128,61,.35);background:rgba(21,128,61,.08)}}
.fca-fail .fca-v,.fca-fail .fca-cv{{color:#b91c1c;border-color:rgba(185,28,28,.35);background:rgba(185,28,28,.07)}}
.fca-mixed .fca-v,.fca-mixed .fca-cv{{color:#b45309;border-color:rgba(180,83,9,.35);background:rgba(180,83,9,.08)}}
.fca-note{{font-size:12px;color:#5B6673;font-weight:600;margin-top:3px}}
.fca-chk{{display:flex;gap:10px;align-items:flex-start;padding:8px 0}}
.fca-cv{{flex:none;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:9.5px;font-weight:800;letter-spacing:.06em;padding:2px 7px;border-radius:5px;border:1px solid;margin-top:3px}}
.fca-ct{{font-size:14px;line-height:1.6;color:#10293D}}
.fca-cn{{font-weight:700;color:#0A2F46}}
.fca-cite{{font-size:11px;font-weight:700;color:#14587D;text-decoration:none;vertical-align:super;margin-left:3px}}
.fca-cite:hover{{text-decoration:underline}}
@media(max-width:560px){{.fchk-mgmt-row{{grid-template-columns:1fr}}}}
</style>
<section class="fchk" id="fchk" data-sym="{sym}">
  <div class="fchk-head">
    <h3 class="fchk-h">Fundamentals fact-check</h3>
    <span class="fchk-score">{n_pass} / {n_total} data checks met</span>
  </div>
  <p class="fchk-biz"><b>The business.</b> {biz}</p>
  <ul class="fchk-list">{checklist_html}</ul>
{self_block}
  <div class="fchk-sub">The audit · what has to hold for this thesis</div>
  <div id="fchk-audit"><div class="fchk-mgmt-row"><span class="k">Working…</span><span class="v">Auditing the areas that decide this thesis.</span></div></div>
  <div class="fchk-sub">What management said · latest earnings call</div>
  <div id="fchk-mgmt"><div class="fchk-mgmt-row"><span class="k">Loading…</span><span class="v">Fetching the latest earnings-call summary.</span></div></div>
  <div class="fchk-cat"><b>Key catalyst.</b> {catalyst}</div>
  <p class="fchk-note">Data checks are computed from the latest reported fundamentals and can lag or contain gaps. The audit and the management summary are AI-generated from the company's filing and recent coverage and may be incomplete or wrong — a PASS is a reading of the cited evidence, not a verified fact and not a reason to buy; follow each citation to the source. This card is research and opinion for information only — not investment advice, not a personal recommendation, and not FCA-authorised. Capital at risk.</p>
</section>
<script>
(function(){{
  var sym=document.getElementById('fchk').getAttribute('data-sym');
  var box=document.getElementById('fchk-mgmt');
  if(!sym||!box)return;
  function esc(s){{return String(s==null?'':s).replace(/[&<>"]/g,function(c){{return{{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];}});}}

  /* Thesis audit — its own endpoint and cache, loaded independently so a slow
     or failed audit never blocks the management summary below it. */
  (function(){{
    var abox=document.getElementById('fchk-audit');
    if(!abox)return;
    var VC={{pass:'fca-pass',fail:'fca-fail',mixed:'fca-mixed'}};
    var VL={{pass:'PASS',fail:'FAIL',mixed:'MIXED'}};
    fetch('/api/thesis-audit/'+encodeURIComponent(sym)).then(function(r){{return r.json();}}).then(function(d){{
      var areas=(d&&Array.isArray(d.audit))?d.audit:[];
      if(!areas.length){{
        abox.innerHTML='<div class="fchk-mgmt-row"><span class="k">Not available yet</span><span class="v">We don\\'t have an audit for '+esc(sym)+' right now.</span></div>';
        return;
      }}
      abox.innerHTML=areas.map(function(a){{
        var checks=(a.checks||[]).map(function(c){{
          /* Footnotes are numbered per check, matching how they were cited. */
          var cites=(c.sources||[]).map(function(s,i){{
            if(!s||!s.url)return '';
            return '<a class="fca-cite" href="'+esc(s.url)+'" target="_blank" rel="noopener nofollow" title="'+esc(s.label||s.url)+'">['+(i+1)+']</a>';
          }}).join('');
          return '<div class="fca-chk '+(VC[c.verdict]||'fca-mixed')+'"><span class="fca-cv">'+(VL[c.verdict]||'MIXED')+
                 '</span><span class="fca-ct"><span class="fca-cn">'+esc(c.name||'')+'.</span> '+esc(c.finding||'')+cites+'</span></div>';
        }}).join('');
        return '<div class="fca-area '+(VC[a.verdict]||'fca-mixed')+'"><div class="fca-head"><span class="fca-name">'+
               esc(a.area||'')+'</span><span class="fca-v">'+(VL[a.verdict]||'MIXED')+'</span></div>'+
               (a.verdict_note?'<div class="fca-note">'+esc(a.verdict_note)+'</div>':'')+checks+'</div>';
      }}).join('');
    }}).catch(function(){{
      abox.innerHTML='<div class="fchk-mgmt-row"><span class="k">Unavailable</span><span class="v">Could not load the audit.</span></div>';
    }});
  }})();
  function row(k,items){{
    if(!items||!items.length)return '';
    var v=items.slice(0,3).map(esc).join(' ');
    return '<div class="fchk-mgmt-row"><span class="k">'+esc(k)+'</span><span class="v">'+v+'</span></div>';
  }}
  fetch('/api/event-intel/'+encodeURIComponent(sym)).then(function(r){{return r.json();}}).then(function(d){{
    if(!d||d.available===false){{
      box.innerHTML='<div class="fchk-mgmt-row"><span class="k">Not available yet</span><span class="v">We don\\'t have a summarised earnings call for '+esc(sym)+' right now. Check the primary transcript.</span></div>';
      return;
    }}
    var html='';
    if(d.event_title||d.event_date){{html+='<div class="fchk-mgmt-row"><span class="k">Latest call</span><span class="v">'+esc(d.event_title||'')+(d.event_date?(' · '+esc(d.event_date)):'')+'</span></div>';}}
    html+=row('Key updates',d.key_updates);
    html+=row('Operations',d.operations);
    html+=row('Outlook',d.outlook);
    html+=row('Risks flagged',d.risks);
    box.innerHTML=html||'<div class="fchk-mgmt-row"><span class="k">Summary</span><span class="v">No structured highlights returned for the latest call.</span></div>';
  }}).catch(function(){{
    box.innerHTML='<div class="fchk-mgmt-row"><span class="k">Unavailable</span><span class="v">Could not load the earnings-call summary.</span></div>';
  }});
}})();
</script>
"""
