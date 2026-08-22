"""safeguards.py — the five retail safeguards.

The premise, which the Crowd Clock research established the hard way: forward
RETURNS are not predictable from price data, so a product that protects retail
cannot be a tip service. What IS available is disclosure — facts a company has
already filed that institutions read within hours and retail never sees. This
module surfaces five of them.

    1. Dilution        share count growth, and the quarters it jumped
    2. Cash runway     quarters of cash left at the current burn
    3. Dated events    lockup expiry, recent issuance quarters
    4. Insider selling what insiders filed, set against the share's own run
    5. Position size   what this share's own volatility does to a position
    6. Ownership       who holds it, and whether they are adding or cutting

EVIDENCE — read this before adding a number to any of these.
Only (1) carries a validated base rate. 409 volatile US names, 8,352
observations 2020-2025, share growth measured a full quarter after the filing
so it is honestly actionable: heavy diluters (>10%/yr) saw a 30% fall within
six months 37.3% of the time against 20.6% for non-issuers — 1.81x — and the
gradient is monotone in EVERY calendar year 2020-2025 and every era tested.
Rank correlation with the next six months' worst drawdown -0.128, t=-4.93.
Like everything else in this codebase it predicts RISK, not return; the return
gradient was insignificant (t=-0.88).

(2) (3) (4) are reported as FACTS with no base rate attached, because the
available history (about five quarters of statements, and no historical
insider panel) is too thin to backtest. Do not invent rates for them. The
honest link for (2) is that its consequence is (1), which is validated.
(5) is arithmetic on realised volatility, not a prediction.
"""
from __future__ import annotations

import html as _html
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: Validated. See the module docstring before touching these.
DILUTION_BANDS = [
    (-1.00, 0.00, "Shrinking", "Share count is falling — the company is buying back stock.", .195),
    (0.00, 0.02, "Flat", "Share count is broadly flat.", .220),
    (0.02, 0.05, "Mild", "Share count is growing slowly.", .284),
    (0.05, 0.10, "Moderate", "Share count is growing at a pace that meaningfully "
                             "reduces your share of the company each year.", .316),
    (0.10, 0.25, "Heavy", "Share count is growing fast. Existing holders are being "
                          "diluted quickly.", .366),
    (0.25, 99.0, "Severe", "Share count is growing very fast. A holder from a year "
                           "ago owns a materially smaller slice today.", .379),
]
DILUTION_BASELINE = .206      # non-issuers (<=2%/yr): 30%-fall rate within 6m
DILUTION_HEAVY = .373         # >10%/yr


def _f(v):
    try:
        if v is None:
            return None
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def _pick(row: dict, *keys):
    for k in keys:
        v = _f(row.get(k))
        if v is not None:
            return v
    return None


# ── 1. DILUTION ──────────────────────────────────────────────────────────────
def dilution(income_q: list) -> dict | None:
    """Share count growth over the last four reported quarters, plus any single
    quarter where the count jumped — that jump is an issuance event, and it is
    the mechanism by which a company sells into a rally retail created."""
    rows = [r for r in (income_q or []) if isinstance(r, dict)]
    if len(rows) < 5:
        return None
    counts = []
    for r in rows[:9]:
        n = _pick(r, "weightedAverageShsOutDil", "weightedAverageShsOut")
        d = r.get("date") or r.get("fillingDate")
        if n and n > 0 and d:
            counts.append((str(d)[:10], n))
    if len(counts) < 5:
        return None
    counts.sort(key=lambda x: x[0])                      # oldest first
    latest_d, latest = counts[-1]
    year_ago_d, year_ago = counts[-5]
    growth = latest / year_ago - 1.0

    jumps = []
    for i in range(1, len(counts)):
        q = counts[i][1] / counts[i - 1][1] - 1.0
        if q >= 0.05:
            jumps.append({"quarter": counts[i][0], "pct": round(q, 4)})

    label = desc = None
    rate = DILUTION_BASELINE
    for lo, hi, lab, txt, r in DILUTION_BANDS:
        if lo <= growth < hi:
            label, desc, rate = lab, txt, r
            break
    if label is None:
        label, desc, rate = DILUTION_BANDS[-1][2], DILUTION_BANDS[-1][3], DILUTION_BANDS[-1][4]
    return {
        "growth": round(growth, 4), "label": label, "desc": desc,
        "shares_now": latest, "shares_year_ago": year_ago,
        "from_date": year_ago_d, "to_date": latest_d,
        "jumps": jumps[-4:], "fall_rate": rate, "baseline": DILUTION_BASELINE,
        "flag": growth >= 0.10,
    }


# ── 2. CASH RUNWAY ───────────────────────────────────────────────────────────
def runway(balance_q: list, cashflow_q: list) -> dict | None:
    """Quarters of cash left at the current burn. Reported as a fact — there is
    not enough statement history here to attach a base rate to it. Its
    consequence is dilution, which IS validated."""
    b = [r for r in (balance_q or []) if isinstance(r, dict)]
    c = [r for r in (cashflow_q or []) if isinstance(r, dict)]
    if not b or not c:
        return None
    cash = _pick(b[0], "cashAndShortTermInvestments", "cashAndCashEquivalents")
    if cash is None:
        return None
    debt = _pick(b[0], "totalDebt", "shortLongTermDebtTotal")

    fcfs = []
    for r in c[:4]:
        ocf = _pick(r, "operatingCashFlow", "netCashProvidedByOperatingActivities")
        capex = _pick(r, "capitalExpenditure") or 0.0
        f = _pick(r, "freeCashFlow")
        if f is None and ocf is not None:
            f = ocf + capex                              # capex is negative in FMP
        if f is not None:
            fcfs.append(f)
    if not fcfs:
        return None
    burn = sum(fcfs) / len(fcfs)                          # avg quarterly free cash flow
    out = {"cash": cash, "debt": debt, "burn_q": burn,
           "quarters_sampled": len(fcfs), "as_of": str(b[0].get("date") or "")[:10]}
    if burn >= 0:
        out.update(generating=True, quarters=None, flag=False)
    else:
        q = cash / abs(burn)
        out.update(generating=False, quarters=round(q, 1), flag=q < 8)
    return out


# ── 1b. SHELF / ATM / CONVERTIBLES — from the filings themselves ─────────────
def offerings(edg: dict | None) -> dict | None:
    """What the company has registered to sell, read from EDGAR.

    An at-the-market programme is the point of this whole module: it lets a
    company sell stock straight into a rally, day by day, with no announcement
    on the day it happens. It is disclosed in a 424B5 that essentially no
    private investor reads. This states it plainly, with the price the shares
    were at when the programme was filed."""
    if not edg:
        return None
    atm = edg.get("atm")
    n = int(edg.get("offerings_24m") or 0)
    if (not atm and not n and not edg.get("convertible")
            and not edg.get("warrants") and not edg.get("stakes")):
        return None
    return {
        "atm": atm, "offerings_24m": n, "shelves_24m": int(edg.get("shelves_24m") or 0),
        "convertible": bool(edg.get("convertible")), "warrants": bool(edg.get("warrants")),
        "filings": (edg.get("filings") or [])[:5],
        "stakes": edg.get("stakes") or [],
        "last_raise": edg.get("last_raise"),
        "flag": bool(atm) or n >= 3,
    }


# ── 3. DATED EVENTS ──────────────────────────────────────────────────────────
def dated_events(profile: dict | None, dil: dict | None, off: dict | None = None,
                 today=None) -> dict:
    """Things with a date attached, so nothing has to be predicted. Lockup
    expiry is IPO + 180 days, the market convention. Convertible and warrant
    terms are NOT machine-readable from our data sources — they live in filing
    prose — so this deliberately does not guess at them; recent issuance
    quarters from (1) are shown instead, which are observed, not inferred."""
    today = today or datetime.now(timezone.utc).date()
    items = []
    ipo = None
    for k in ("ipoDate", "ipo_date", "ipo"):
        v = (profile or {}).get(k)
        if v:
            try:
                ipo = datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
                break
            except ValueError:
                pass
    if ipo:
        age = (today - ipo).days
        if 0 <= age <= 550:
            lock = ipo.toordinal() + 180
            lockd = datetime.fromordinal(lock).date()
            days = (lockd - today).days
            items.append({
                "kind": "Lockup expiry",
                "date": lockd.isoformat(),
                "days": days,
                "note": ("Insiders and early backers are typically free to sell from "
                         "this date — the market convention is 180 days after listing. "
                         + ("Still ahead." if days > 0 else "Already passed.")),
                "flag": 0 < days <= 60,
            })
        if age <= 550:      # only interesting while it is still a recent listing
            items.append({"kind": "Listed", "date": ipo.isoformat(), "days": -age,
                          "note": f"Came to market {age // 30} months ago.",
                          "flag": False})
    for j in (dil or {}).get("jumps", []):
        items.append({
            "kind": "Share count jumped",
            "date": j["quarter"], "days": None,
            "note": f"Shares outstanding rose {j['pct'] * 100:.0f}% in this quarter — "
                    f"an issuance landed. Observed in the filings, not inferred.",
            "flag": j["pct"] >= 0.10,
        })
    # Registration filings, read straight from EDGAR — each 424B5 is a real sale
    # off a shelf, not an intention to sell.
    FORM_NOTE = {
        "424B5": "A prospectus supplement — shares priced and sold off an existing shelf.",
        "424B3": "A prospectus supplement — shares registered for resale.",
        "424B4": "A prospectus supplement — an offering priced.",
        "S-3": "A shelf registration — the company registered stock it may sell later.",
        "S-3ASR": "An automatic shelf registration — the company can sell registered "
                  "stock at will, with no further approval.",
        "S-1": "A registration statement — new stock registered for sale.",
    }
    for f in (off or {}).get("filings", [])[:4]:
        form = (f.get("form") or "").upper()
        base = next((v for k, v in FORM_NOTE.items() if form.startswith(k)),
                    "A registration filing.")
        items.append({"kind": f"Filed {form}", "date": f.get("date") or "",
                      "days": f.get("days"), "note": base,
                      "flag": form.startswith("424B")})
    for k in ((off or {}).get("stakes") or [])[:3]:
        items.append({
            "kind": ("Activist stake (13D)" if k.get("activist")
                     else f'>5% holder ({k.get("kind")})'),
            "date": k.get("date") or "", "days": k.get("days"),
            "note": ("A holder crossing 5% filed a Schedule 13D — they intend to "
                     "influence the company." if k.get("activist")
                     else "A >5% holder filed" + (" an amendment to their stake."
                                                  if k.get("amended") else " a new stake.")),
            "flag": bool(k.get("activist")),
        })
    if (off or {}).get("convertible"):
        items.append({"kind": "Convertible notes", "date": "in recent filings", "days": None,
                      "note": "Convertible notes are referenced in a recent prospectus. "
                              "Notes that convert become shares, which dilutes holders. "
                              "The terms sit in the filing text — read the document.",
                      "flag": False})
    if (off or {}).get("warrants"):
        items.append({"kind": "Warrants", "date": "in recent filings", "days": None,
                      "note": "Warrants are referenced in a recent prospectus. Exercised "
                              "warrants become new shares. Strike and expiry sit in the "
                              "filing text — read the document.",
                      "flag": False})
    # real dates newest-first, then the undated "in recent filings" notes
    items.sort(key=lambda x: (x["date"][:1].isdigit(), x["date"]), reverse=True)
    return {"items": items[:8], "flag": any(i["flag"] for i in items)}


# ── 4. INSIDER SELLING ───────────────────────────────────────────────────────
def insiders(ins: dict | None, run_90d: float | None) -> dict | None:
    """What insiders filed, set against the share's own 90-day move. Selling
    into strength is the case worth seeing; selling on its own is routine —
    most of it is scheduled compensation, and this says so."""
    if not ins:
        return None
    buys = int(ins.get("insider_buys_90d") or 0)
    sells = int(ins.get("insider_sells_90d") or 0)
    detail = [d for d in (ins.get("insider_detail") or []) if isinstance(d, dict)][:6]
    if buys == 0 and sells == 0 and not detail:
        return None
    total = buys + sells
    return {
        "buys": buys, "sells": sells, "detail": detail,
        "sell_share": round(sells / total, 3) if total else None,
        "run_90d": run_90d,
        "into_strength": bool(run_90d is not None and run_90d > 0.30
                              and sells >= 3 and sells > buys * 2),
        "flag": bool(sells >= 3 and sells > buys * 2),
    }


# ── 5. POSITION SIZE ─────────────────────────────────────────────────────────
def position(vol_annual: float | None, worst_1y: float | None,
             stake: float = 1000.0) -> dict | None:
    """What this share's own volatility does to a stake. Arithmetic on realised
    volatility — not a recommendation, and deliberately not a suggested size."""
    if not vol_annual or vol_annual <= 0:
        return None
    monthly = vol_annual / (12 ** 0.5)
    return {
        "vol_annual": round(vol_annual, 4),
        "typical_month": round(stake * monthly, 0),
        "stake": stake,
        "worst_1y": worst_1y,
        "worst_cash": round(stake * abs(worst_1y), 0) if worst_1y else None,
        "band": ("very high" if vol_annual > 0.80 else "high" if vol_annual > 0.55
                 else "moderate" if vol_annual > 0.35 else "ordinary"),
        "flag": vol_annual > 0.55,
    }


def summarise(blocks: dict) -> dict:
    flags = [k for k, v in blocks.items() if isinstance(v, dict) and v.get("flag")]
    return {"flags": flags, "n_flags": len(flags),
            "checked": [k for k, v in blocks.items() if v]}


# ── 6. OWNERSHIP ─────────────────────────────────────────────────────────────
def ownership(major: dict | None, holders: list | None,
              stakes: list | None = None) -> dict | None:
    """Who actually owns the company, and whether the big holders are adding or
    cutting.

    Two things here that a private investor almost never sees. First, how much
    management owns: a board with no stake is not wrong, but it is worth
    knowing. Second, the direction of the big positions — 13F filings are
    public and quarterly, and a majority of the top holders cutting is a fact
    about supply, not a prediction.

    Reported as fact, with no base rate: 13F data is a quarter stale by the
    time it is filed and there is no historical panel here to measure it
    against. Do not attach a percentage to it."""
    major = major or {}
    ins = _f(major.get("insidersPercentHeld"))
    inst = _f(major.get("institutionsPercentHeld"))
    cnt = _f(major.get("institutionsCount"))
    top = []
    for h in (holders or [])[:6]:
        if not isinstance(h, dict):
            continue
        top.append({"holder": str(h.get("Holder") or h.get("holder") or "")[:38],
                    "pct": _f(h.get("pctHeld")), "change": _f(h.get("pctChange")),
                    "date": str(h.get("Date Reported") or h.get("date") or "")[:10]})
    if ins is None and inst is None and not top:
        return None

    chg = [t["change"] for t in top if t["change"] is not None]
    cutting = sum(1 for c in chg if c < 0)
    st = [x for x in (stakes or []) if isinstance(x, dict)]
    activist = next((x for x in st if x.get("activist")), None)
    return {
        "insiders": ins, "institutions": inst, "count": int(cnt) if cnt else None,
        "top": top, "cutting": cutting, "n_changed": len(chg),
        "stakes": st[:4], "activist": activist,
        # Flag only on things a holder would want to look at, not on a view.
        "flag": bool((ins is not None and ins < 0.01)
                     or (chg and cutting > len(chg) / 2)
                     or activist),
    }


# ── universe-wide dilution watch ─────────────────────────────────────────────
def scan_offerings(tickers: list[str], sectors: dict | None = None,
                   progress=None) -> list[dict]:
    """Rank a universe by how often each company has actually priced stock off a
    shelf, straight from EDGAR.

    This deliberately does NOT rank on a share-count percentage. The free
    share-count feeds are too noisy to publish per name — one of them reads
    KLAC at 1,307m shares against a true ~130m, and ON at -42% — and a wrong
    number on a page that exists to protect people is worse than no number.
    A 424B5 count is a filing count: no estimation, nothing to get wrong.
    Accurate share growth is added afterwards, from the statement API, for the
    names at the top of this list only. One SEC request per name.
    """
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone
    import edgar as ed

    sectors = sectors or {}
    syms = sorted({(s or "").upper().strip() for s in tickers if s})
    today = datetime.now(timezone.utc).date()
    done = [0]

    def one(sym):
        try:
            cik = ed.cik_for(sym)
            if not cik:
                return None
            n424 = nshelf = 0
            last = None
            dates = []
            for r in ed._recent(cik):
                form = (r.get("form") or "").upper()
                d = r.get("filingDate") or ""
                if not d or not form.startswith(ed.RAISE_FORMS):
                    continue
                try:
                    age = (today - datetime.strptime(d, "%Y-%m-%d").date()).days
                except ValueError:
                    continue
                if age > 730:
                    continue
                if form.startswith("424B5"):
                    n424 += 1
                    dates.append(d)
                if form.startswith("S-3"):
                    nshelf += 1
                if last is None or d > last:
                    last = d
            recent12 = sum(1 for d in dates
                           if (today - datetime.strptime(d, "%Y-%m-%d").date()).days <= 365)
            return {"ticker": sym, "sector": sectors.get(sym) or "Other",
                    "offerings_24m": n424, "offerings_12m": recent12,
                    "shelves_24m": nshelf, "last_raise": last,
                    "growth": None, "label": None, "fall_rate": None,
                    "flag": n424 >= 3}
        except Exception:
            return None
        finally:
            done[0] += 1
            if progress and done[0] % 10 == 0:
                try:
                    progress(done[0], len(syms))
                except Exception:
                    pass

    with ThreadPoolExecutor(max_workers=5) as ex:      # gentle on sec.gov
        rows = [r for r in ex.map(one, syms) if r]
    rows.sort(key=lambda x: (-x["offerings_24m"], -x["shelves_24m"], x["ticker"]))
    return rows


def attach_growth(row: dict, income_q: list) -> dict:
    """Add an accurate share-count change to a scan row, from the statement API
    — the same source the per-stock card uses, which is clean."""
    d = dilution(income_q)
    if d:
        row.update(growth=d["growth"], label=d["label"], fall_rate=d["fall_rate"],
                   shares_now=d["shares_now"], shares_then=d["shares_year_ago"],
                   as_of=d["to_date"])
        row["flag"] = row["flag"] or d["flag"]
    return row


# ── the card ─────────────────────────────────────────────────────────────────
_CSS = """
<style>
.sfg{margin:36px 0}
.sfg-box{border:1px solid rgba(10,10,10,.1);border-radius:16px;overflow:hidden;background:#fff;
  box-shadow:0 10px 30px -24px rgba(10,10,10,.25);margin:0 0 16px}
.sfg-box:last-of-type{margin-bottom:0}
.sfg-head{display:flex;align-items:center;gap:10px;padding:11px 16px;position:relative;
  background:linear-gradient(105deg,#5DC0AC,#0E7C66)}
.sfg-head::after{content:"";position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(180deg,rgba(255,255,255,.22),rgba(255,255,255,0) 62%)}
.sfg-head>*{position:relative;z-index:1}
.sfg-ico{width:27px;height:27px;border-radius:9px;background:rgba(255,255,255,.2);
  display:grid;place-items:center;font-size:14px;flex:0 0 auto}
.sfg-h{font-family:'Public Sans',system-ui,sans-serif;font-size:15px;font-weight:500;
  color:#fff;margin:0;letter-spacing:-.005em;text-shadow:0 1px 1px rgba(0,0,0,.12)}
.sfg-bd{padding:20px 24px}
.sfg-pill{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11.5px;font-weight:700;
  padding:5px 11px;border-radius:999px;white-space:nowrap;margin-left:auto;color:#fff;
  background:rgba(255,255,255,.18);border:1px solid rgba(255,255,255,.38)}
/* The "worth a look" count is the one thing on the banner that carries a
   reading, so it inverts rather than just tinting. */
.sfg-pill.on{color:#0E7C66;background:#fff;border-color:#fff}
.sfg-sub{font-size:12.5px;color:#5d6c7b;margin:0 0 18px;line-height:1.55}
.sfg-item{padding:16px 0;border-top:1px solid rgba(10,10,10,.07)}
.sfg-item:first-of-type{border-top:none;padding-top:4px}
.sfg-top{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin-bottom:7px}
.sfg-k{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11px;font-weight:700;letter-spacing:.11em;text-transform:uppercase;color:#758696}
.sfg-v{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:20px;font-weight:800;color:#0A2F46;letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.sfg-v.warn{color:#C74E00}
.sfg-v.ok{color:#0E7C66}
.sfg-tag{margin-left:auto;font-family:'JetBrains Mono',monospace;font-size:9.5px;font-weight:700;letter-spacing:.06em;padding:3px 8px;border-radius:5px;color:#8A97A4;background:rgba(10,10,10,.05)}
.sfg-tag.warn{color:#C74E00;background:rgba(199,78,0,.09)}
.sfg-txt{font-size:13.5px;line-height:1.6;color:#5d6c7b;margin:0}
.sfg-txt b{color:#0A2F46}
.sfg-ev{font-size:12px;line-height:1.55;color:#5d6c7b;margin:8px 0 0;padding:9px 12px;border-radius:8px;background:#FBFAF8;border:1px solid rgba(10,10,10,.06)}
.sfg-list{list-style:none;margin:9px 0 0;padding:0;display:grid;gap:6px}
.sfg-list li{display:grid;grid-template-columns:96px 1fr;gap:10px;font-size:12.5px;color:#5d6c7b;align-items:baseline}
.sfg-list time{font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;color:#0A2F46}
.sfg-list li.warn time{color:#C74E00}
.sfg-note{font-size:11.5px;line-height:1.55;color:#758696;margin:18px 0 0;padding-top:14px;border-top:1px solid rgba(10,10,10,.07)}
.sfg-off{font-size:13.5px;color:#758696;margin:0}
@media (max-width:520px){.sfg-list li{grid-template-columns:1fr}}
</style>
"""


def _box(title: str, icon: str, inner: str, pill=None) -> str:
    """One ribboned card. The banner metrics are copied from _sp_ribbon's
    .rep-rib in app.py rather than invented, so these sit in the same family as
    "The picture" and "The audit" instead of looking like a different product."""
    p = ""
    if pill:
        txt, on = pill
        p = f'<span class="sfg-pill{" on" if on else ""}">{_html.escape(txt)}</span>'
    return (f'<section class="sfg-box"><div class="sfg-head">'
            f'<span class="sfg-ico">{icon}</span><h3 class="sfg-h">{title}</h3>{p}</div>'
            f'<div class="sfg-bd">{inner}</div></section>')


def _row(key, value, cls, text, tag=None, extra=""):
    t = (f'<span class="sfg-tag{" warn" if tag[1] else ""}">{_html.escape(tag[0])}</span>'
         if tag else "")
    return (f'<div class="sfg-item"><div class="sfg-top"><span class="sfg-k">{key}</span>'
            f'<span class="sfg-v {cls}">{value}</span>{t}</div>'
            f'<p class="sfg-txt">{text}</p>{extra}</div>')


def card_body(sym: str, d: dict, public: bool = False) -> str:
    pct = lambda v, p=0: "&mdash;" if v is None else f"{v * 100:.{p}f}%"
    out, own_out, s = [], [], d.get("summary") or {}
    n = s.get("n_flags", 0)
    esym = _html.escape(sym)

    dil = d.get("dilution")
    if dil:
        out.append(_row(
            "Dilution", f'{dil["growth"] * 100:+.1f}%', "warn" if dil["flag"] else "ok",
            f'Shares outstanding went from {dil["shares_year_ago"] / 1e6:,.0f}m to '
            f'{dil["shares_now"] / 1e6:,.0f}m over the four quarters to {dil["to_date"]}. '
            f'<b>{_html.escape(dil["desc"])}</b>',
            ("SHARE COUNT, YEAR ON YEAR", dil["flag"]),
            # The measured sample is volatile US shares, and the gradient only
            # separates once issuance is material. Quoting "20% vs 21%" at a
            # buyback name would be noise dressed as evidence, so don't.
            (f'<p class="sfg-ev">Among 409 volatile US shares over 8,352 observations '
             f'(2020&ndash;2025), those diluting at this pace saw a 30% fall within the next '
             f'six months <b>{pct(dil["fall_rate"])}</b> of the time, against '
             f'{pct(dil["baseline"])} for shares not issuing. The gradient held in every '
             f'calendar year tested. Historical frequencies for other shares, not a forecast '
             f'for {esym}.</p>') if dil["growth"] >= 0.05 else ''))

    off = d.get("offerings")
    if off:
        atm = off.get("atm")
        if atm:
            px_then = atm.get("price_at_filing")
            size = atm.get("size")
            headline = "Live ATM programme"
            body = (f'On {atm["date"]} the company filed a prospectus supplement for an '
                    f'<b>at-the-market equity programme</b>'
                    + (f' of up to <b>${size / 1e6:,.0f}m</b>' if size else '')
                    + (f', with the shares at <b>${px_then:,.2f}</b>' if px_then else '')
                    + '. An ATM lets a company sell stock into the open market day by day, '
                      'at prevailing prices, with no announcement on the day it happens. '
                      '<b>A rising price is what makes it easy to use.</b>')
        else:
            headline = f'{off["offerings_24m"]} in 24 months'
            body = ('Prospectus supplements filed — each one is stock priced and sold off a '
                    'shelf registration, not merely an intention to sell.'
                    if off["offerings_24m"] else
                    'No shares priced off a shelf in the last two years.')
        extra = (f'<p class="sfg-ev">Read from the company&rsquo;s own SEC filings: '
                 f'{off["offerings_24m"]} prospectus supplement'
                 f'{"" if off["offerings_24m"] == 1 else "s"} and {off["shelves_24m"]} shelf '
                 f'registration{"" if off["shelves_24m"] == 1 else "s"} in the last 24 months'
                 + (', with convertible notes referenced' if off["convertible"] else '')
                 + (' and warrants referenced' if off["warrants"] else '')
                 + '. Filing dates are listed under dated events below.</p>')
        out.append(_row("Share offerings", headline, "warn" if off["flag"] else "ok",
                        body, ("REGISTERED WITH THE SEC", off["flag"]), extra))

    rw = d.get("runway")
    if rw:
        if rw["generating"]:
            out.append(_row(
                "Cash runway", "Self-funding", "ok",
                f'The business generated cash over the last {rw["quarters_sampled"]} reported '
                f'quarters, so it is not dependent on raising money to keep going. Cash and '
                f'short-term investments ${rw["cash"] / 1e6:,.0f}m as of {rw["as_of"]}.',
                ("FREE CASH FLOW", False)))
        else:
            debt = (f', with ${rw["debt"] / 1e6:,.0f}m of debt' if rw.get("debt") else '')
            # past ~5 years the burn is so small next to the cash pile that a
            # precise quarter count is false precision
            headline = ("5+ years" if rw["quarters"] >= 20
                        else f'{rw["quarters"]:.0f} quarters')
            out.append(_row(
                "Cash runway", headline, "warn" if rw["flag"] else "",
                f'${rw["cash"] / 1e6:,.0f}m of cash against an average burn of '
                f'${abs(rw["burn_q"]) / 1e6:,.0f}m a quarter over the last '
                f'{rw["quarters_sampled"]} quarters{debt}. <b>A company that runs low on cash '
                f'raises more, and raising more is what the dilution figure above measures.</b>',
                ("AT THE CURRENT BURN", rw["flag"]),
                '<p class="sfg-ev">Reported as a fact. There is too little statement history '
                'to attach a reliable historical frequency to runway itself, so none is shown '
                '&mdash; unlike the dilution figure above, which is measured.</p>'))

    ev = None if public else d.get("events")
    if ev and ev.get("items"):
        li = "".join(
            f'<li class="{"warn" if i["flag"] else ""}"><time>{_html.escape(i["date"])}</time>'
            f'<span><b>{_html.escape(i["kind"])}</b> &mdash; {_html.escape(i["note"])}</span></li>'
            for i in ev["items"])
        out.append(_row(
            "Dated events", f'{len(ev["items"])} on record', "warn" if ev["flag"] else "",
            "Things with a date attached, so nothing has to be predicted.",
            ("FILED OR SCHEDULED", ev["flag"]),
            f'<ul class="sfg-list">{li}</ul>'
            '<p class="sfg-ev">Registration filings come straight from EDGAR. Convertible and '
            'warrant <em>terms</em> — strike, expiry, conversion price — sit in filing prose '
            'and are not extracted here, so their presence is reported and their terms are '
            'not guessed at. Share-count jumps are observed in the reported accounts.</p>'))

    ins = d.get("insiders")
    if ins:
        run = ins.get("run_90d")
        ctx = f' The share is {run * 100:+.0f}% over the same 90 days.' if run is not None else ''
        strength = '<b> Those sales landed into a rising price.</b>' if ins["into_strength"] else ''
        own_out.append(_row(
            "Insider filings", f'{ins["sells"]} sold / {ins["buys"]} bought',
            "warn" if ins["flag"] else "",
            f'Form 4 filings over the last 90 days.{ctx}{strength} Most insider selling is '
            f'scheduled compensation and routine &mdash; it is the pattern against the price '
            f'that is worth seeing, not any single sale.',
            ("LAST 90 DAYS", ins["flag"]),
            '<p class="sfg-ev">Reported as filed. No historical frequency is attached: there '
            'is no insider panel here deep enough to measure one honestly.</p>'))

    own = d.get("ownership")
    if own:
        bits = []
        if own["institutions"] is not None:
            bits.append(f'<b>{own["institutions"] * 100:.0f}%</b> held by institutions'
                        + (f' across {own["count"]} of them' if own["count"] else ''))
        if own["insiders"] is not None:
            bits.append(f'insiders hold <b>{own["insiders"] * 100:.1f}%</b>')
        lead = ' &middot; '.join(bits) if bits else 'Holdings on file.'

        moves = ''
        if own["top"]:
            li = "".join(
                '<li class="{c}"><time>{p}</time><span><b>{h}</b> &mdash; {m}</span></li>'.format(
                    c="warn" if (t["change"] is not None and t["change"] < 0) else "",
                    p=(f'{t["pct"] * 100:.1f}%' if t["pct"] is not None else "&mdash;"),
                    h=_html.escape(t["holder"]),
                    m=("position unchanged" if t["change"] in (None, 0) else
                       f'{"added" if t["change"] > 0 else "cut"} '
                       f'{abs(t["change"]) * 100:.0f}% in the quarter to {t["date"]}'))
                for t in own["top"])
            moves = f'<ul class="sfg-list">{li}</ul>'

        note = ''
        if own["activist"]:
            note = (f'<p class="sfg-ev"><b>A Schedule 13D was filed on '
                    f'{_html.escape(own["activist"]["date"])}.</b> A holder crossing 5% files '
                    f'13D when they intend to influence the company, and 13G when they are '
                    f'passive. Both are public within days; neither usually reaches a private '
                    f'investor until it appears in a news story weeks later.</p>')
        elif own["stakes"]:
            k = own["stakes"][0]
            note = (f'<p class="sfg-ev">Most recent &gt;5% stake filing: {_html.escape(k["form"])} '
                    f'on {_html.escape(k["date"])}. Anyone crossing 5% of the shares must file '
                    f'within days &mdash; these are public the moment they land.</p>')
        note += ('<p class="sfg-ev">13F holdings are filed quarterly and are already a quarter '
                 'old when published. Reported as filed, with no historical frequency attached '
                 'because there is no panel here deep enough to measure one.</p>')

        headline = (f'{own["cutting"]} of {own["n_changed"]} cutting'
                    if own["n_changed"] else 'On file')
        own_out.append(_row(
            "Ownership", headline, "warn" if own["flag"] else "",
            f'{lead}. Among the largest holders, positions moved as below in the last '
            f'reported quarter.',
            ("WHO OWNS IT", own["flag"]), moves + note))

    ps = None if public else d.get("position")
    if ps:
        worst = (f' In its worst stretch of the last year, $1,000 would have been down '
                 f'${ps["worst_cash"]:,.0f} at the low point.' if ps.get("worst_cash") else '')
        own_out.append(_row(
            "What a position does", f'&plusmn;${ps["typical_month"]:,.0f}',
            "warn" if ps["flag"] else "",
            f'on every $1,000 held, in a typical month. Realised volatility is '
            f'{ps["vol_annual"] * 100:.0f}% a year &mdash; <b>{ps["band"]}</b>.{worst}',
            # NOTE: literal U+2019, not &rsquo; - _row() runs html.escape() on this
            # label, so an entity here becomes &amp;rsquo; and prints raw.
            ("THIS SHARE’S OWN VOLATILITY", ps["flag"]),
            '<p class="sfg-ev">Arithmetic on this share&rsquo;s own price history, shown so '
            'the size of a position can be judged against what it actually does. It is not a '
            'suggested position size and not advice.</p>'))

    if not out and not own_out:
        return (_box("Dilution Check", "🛡",
                     f'<p class="sfg-off">No filing data available for {esym}.</p>'))

    box1 = _box(
        "Dilution Check", "🛡",
        f'<p class="sfg-sub">What the company is doing to its own share count &mdash; '
        f'things already filed that rarely reach a private investor in time. Facts about '
        f'{esym}, not a view on where the price goes.</p>' + "".join(out),
        pill=(f'{n} of {len(s.get("checked") or [])} worth a look', bool(n)),
    ) if out else ""

    box2 = _box(
        "Who owns it — insiders and institutions", "👥",
        '<p class="sfg-sub">What the people who already hold the share are doing with it, '
        'as filed. Insider sales are mostly routine; it is the pattern that reads, not any '
        'single line.</p>' + "".join(own_out),
    ) if own_out else ""

    return (box1 + box2 +
            '<p class="sfg-note">General information and research, not advice and not a '
            'personal recommendation. Figures are drawn from company filings and market data '
            'and can lag or contain gaps. Where a historical frequency is shown it describes a '
            'sample of other shares in a similar state &mdash; it is not a forecast or a '
            'probability for this share. Past performance is not a reliable indicator of '
            'future results. TickerMover is not authorised by the Financial Conduct Authority. '
            'Capital at risk.</p>')


def render_card(t: dict, public: bool = False) -> str:
    """`public=True` is the /stocks SEO page: it drops the position-sizing
    block, which is a portfolio tool and belongs to a logged-in reader."""
    """Shell only; the reading loads client-side from /api/safeguards/{ticker}
    so the page render stays network-free."""
    sym = _html.escape((t.get("ticker") or "").upper())
    if not sym:
        return ""
    return _CSS + """
<div class="sfg" id="sfg-card" data-sym="SYMBOL" data-public="PUBFLAG">
  <section class="sfg-box"><div class="sfg-head">
    <span class="sfg-ico">🛡</span><h3 class="sfg-h">Dilution Check</h3></div>
    <div class="sfg-bd"><p class="sfg-off" id="sfg-status">Reading SYMBOL&rsquo;s filings&hellip;</p></div>
  </section>
</div>
<script>
(function(){
  var el = document.getElementById('sfg-card');
  if (!el) return;
  var PUBLIC = el.getAttribute('data-public') === '1';
  fetch('/api/safeguards/' + encodeURIComponent(el.getAttribute('data-sym')))
    .then(function(r){ return r.ok ? r.json() : null; })
    .then(function(d){
      var _h = (PUBLIC && d && d.html_public) || (d && d.html);
      if (!d || !_h) {
        document.getElementById('sfg-status').textContent =
          'Filing data is not available for this share.';
        return;
      }
      el.innerHTML = _h;
    })
    .catch(function(){
      document.getElementById('sfg-status').textContent =
        'Safeguards are unavailable right now.';
    });
})();
</script>""".replace("SYMBOL", sym).replace("PUBFLAG", "1" if public else "0")
