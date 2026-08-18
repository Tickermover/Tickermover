"""edgar_facts — LAST-RESORT fundamentals from the SEC's own XBRL API.

WHY THIS EXISTS
Alpha Vantage's free tier is ~25 calls/day for the whole key (see av_budget),
and FMP is a paid plan with a per-minute throttle. When either is exhausted the
fundamentals lane returns {} and the stock card goes blank. This module is a
tier UNDER both: it is never consulted while a configured provider is still
answering, and it replaces nothing.

WHY THE SEC
`data.sec.gov/api/xbrl` is free, needs no key and has no daily quota — only the
SEC's ~10 req/s fair-use cap, which edgar._get already throttles for us. It
serves the numbers as filed, so it cannot go over budget and cannot be
rate-limited out of existence.

WHY companyfacts AND NOT companyconcept
`companyconcept` looked cheaper (one small request per tag), but it is not
reliable per-filer: for CIK 21344 (Coca-Cola) every revenue concept comes back
`"units":{"USD":{}}` while the same tag in `companyfacts` carries 106 rows
through 2026. Measured payloads are 3-8 MB and land in ~0.4 s, so one
companyfacts request is both cheaper than ten concept requests AND correct.

TWO TRAPS THIS CODE EXISTS TO AVOID
1. STALE TAGS. Filers migrate between us-gaap tags and leave the old one in
   place. Apple's `Revenues` stops in 2018 while
   `RevenueFromContractWithCustomerExcludingAssessedTax` runs to 2026 — taking
   the first tag in a preference list yields 2018 revenue. `_best` therefore
   picks whichever candidate tag has the NEWEST data, not the first one.
2. MIXED ERAS. Pairing 2018 revenue with 2026 gross profit produced an 83%
   gross margin for Apple. Every field is now checked against the revenue
   anchor date by `_aligned` and dropped if it comes from a different era.

WHAT IT CAN AND CANNOT GIVE
It reads FILINGS, so it knows revenue, margins, cash flow, EPS and leverage.
It knows nothing price-derived — market cap, P/E, beta, moving averages,
52-week range and analyst targets are absent and stay None. That is fine:
get_full_ticker merges on "first non-None wins", so the absent keys leave
whatever yfinance/FMP already supplied untouched.
"""
from __future__ import annotations

import json
import logging
from datetime import date

import edgar

logger = logging.getLogger(__name__)

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

# Refuse anything pathologically large rather than risk the dyno's memory.
# The biggest filer measured (JPM) was 7.9 MB, so this is ~5x headroom.
MAX_BYTES = 40_000_000

# A period counts as a quarter / a year when its length falls in these bands.
# Filed periods are never exactly 91 or 365 days (52/53-week retail calendars,
# transition periods), so both bands are deliberately loose.
_Q_DAYS = (60, 115)
_Y_DAYS = (300, 400)

# How far a field's newest period may sit from the revenue anchor before we
# treat it as a different reporting era and discard it. One reporting lag plus
# slack — wide enough for a balance sheet filed a quarter behind, far too tight
# to let a tag abandoned in 2018 pair with revenue from 2026.
_ERA_TOL_DAYS = 200

# Candidate tags per field. Order is a TIE-BREAK only — `_best` prefers
# whichever of these actually has the most recent data.
_TAGS = {
    "revenue": ["Revenues",
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet",
                "SalesRevenueGoodsNet"],
    "gross_profit": ["GrossProfit"],
    "cost_of_revenue": ["CostOfGoodsAndServicesSold", "CostOfRevenue",
                        "CostOfGoodsSold"],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "eps": ["EarningsPerShareDiluted", "EarningsPerShareBasic"],
    "equity": ["StockholdersEquity",
               "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "lt_debt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "st_debt": ["LongTermDebtCurrent"],
}


def _days(start: str, end: str) -> int:
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except Exception:
        return 0


def _load(cik: str) -> tuple[dict, str]:
    """(us-gaap facts, entityName). ({}, "") when unavailable."""
    raw = edgar._get(FACTS_URL.format(cik=cik), cap=MAX_BYTES)
    if not raw:
        return {}, ""
    try:
        doc = json.loads(raw)
    except Exception as exc:
        logger.warning("edgar_facts: companyfacts parse failed for CIK %s: %s", cik, exc)
        return {}, ""
    return (doc.get("facts") or {}).get("us-gaap") or {}, doc.get("entityName") or ""


def _rows(gaap: dict, tag: str, unit_hint: str) -> list[dict]:
    """Normalised, de-duplicated entries for one tag, oldest first."""
    units = (gaap.get(tag) or {}).get("units") or {}
    raw = units.get(unit_hint)
    if not isinstance(raw, list) or not raw:
        # EPS lives under "USD/shares"; some concepts carry an empty dict here.
        raw = next((v for k, v in units.items()
                    if k.startswith(unit_hint) and isinstance(v, list) and v), None)
    if not raw:
        return []

    # A restated period appears twice; the later `filed` date is the live one.
    best: dict[tuple, dict] = {}
    for r in raw:
        val, end = r.get("val"), r.get("end")
        if val is None or not end:
            continue
        start = r.get("start") or ""
        pk = (start, end)
        prev = best.get(pk)
        if prev is None or (r.get("filed") or "") >= (prev.get("filed") or ""):
            best[pk] = {"start": start, "end": end, "val": float(val),
                        "days": _days(start, end) if start else 0}
    return sorted(best.values(), key=lambda x: x["end"])


def _best(gaap: dict, field: str, unit_hint: str = "USD") -> list[dict]:
    """Rows from whichever candidate tag carries the most recent data.

    Filers abandon tags without removing them (Apple's `Revenues` ends in 2018),
    so recency — not list order — decides. List order only breaks ties.
    """
    best_rows: list[dict] = []
    best_end = ""
    for tag in _TAGS[field]:
        rows = _rows(gaap, tag, unit_hint)
        if not rows:
            continue
        if rows[-1]["end"] > best_end:
            best_rows, best_end = rows, rows[-1]["end"]
    return best_rows


def _aligned(rows: list[dict], anchor: str) -> list[dict]:
    """Drop a series whose newest period sits in a different reporting era."""
    if not rows or not anchor:
        return []
    if abs(_days(rows[-1]["end"], anchor)) > _ERA_TOL_DAYS:
        return []
    return rows


def _to_quarters(entries: list[dict]) -> list[dict]:
    """Reduce a series to DISCRETE quarters, differencing cumulative ones.

    Income-statement tags are filed as discrete three-month periods, but
    cash-flow tags are filed CUMULATIVE year-to-date: Walmart's operating
    cash flow arrives as 2026-02-01→04-30, →07-31, →10-31, →01-31, every one
    starting at the fiscal year open. Treating those as quarters made the
    trailing sum add up four different years' FIRST quarters, which is how
    Walmart ended up with negative free cash flow.

    Entries sharing a `start` are therefore a cumulative ladder: differencing
    consecutive rungs recovers the discrete quarter. A genuinely discrete
    series has a unique `start` per period, so each group holds one entry and
    passes through untouched — one code path serves both shapes.
    """
    groups: dict[str, list[dict]] = {}
    for e in entries:
        if e["start"]:
            groups.setdefault(e["start"], []).append(e)

    out: dict[tuple, dict] = {}
    for start, rows in groups.items():
        rows.sort(key=lambda x: x["end"])
        prev_end, prev_val = start, 0.0
        for e in rows:
            span = _days(prev_end, e["end"])
            val = e["val"] - prev_val
            # Only keep the differences that really are one quarter long. This
            # drops the annual-minus-Q1 nine-month residue left by filers who
            # report both a discrete Q1 and a full year off the same start.
            if _Q_DAYS[0] <= span <= _Q_DAYS[1]:
                out[(prev_end, e["end"])] = {"start": prev_end, "end": e["end"],
                                             "val": val, "days": span}
            prev_end, prev_val = e["end"], e["val"]
    return sorted(out.values(), key=lambda x: x["end"])


def _trailing(entries: list[dict], back: int = 0) -> float | None:
    """Sum of four consecutive, non-overlapping quarters.

    `back=0` is the most recent four; `back=1` is the four before those, which
    is what makes a like-for-like year-on-year comparison possible. Falls back
    to a single annual period when a filer reports no usable quarters (foreign
    private issuers on 20-F, mostly).
    """
    quarters = _to_quarters(entries)
    quarters.sort(key=lambda x: x["end"], reverse=True)

    picked: list[dict] = []
    cursor = ""            # start date of the earliest period already consumed
    for e in quarters:
        if cursor and e["end"] > cursor:
            continue       # overlaps a period we already counted
        picked.append(e)
        cursor = e["start"]
        if len(picked) == 4 * (back + 1):
            break

    window = picked[back * 4: back * 4 + 4]
    if len(window) == 4:
        return sum(e["val"] for e in window)

    years = [e for e in entries if _Y_DAYS[0] <= e["days"] <= _Y_DAYS[1]]
    years.sort(key=lambda x: x["end"], reverse=True)
    if len(years) > back:
        return years[back]["val"]
    return None


def _latest_instant(entries: list[dict]) -> float | None:
    """Newest balance-sheet value (instants carry no start date)."""
    instants = [e for e in entries if not e["start"]]
    return instants[-1]["val"] if instants else None


def _ratio(num: float | None, den: float | None, nd: int = 4) -> float | None:
    if num is None or den is None or den == 0:
        return None
    return round(num / den, nd)


def _growth(now: float | None, prior: float | None) -> float | None:
    """Year-on-year change as a DECIMAL (0.35 = +35%), matching the AV shape."""
    if now is None or prior is None or prior == 0:
        return None
    return round((now - prior) / abs(prior), 4)


def fundamentals(ticker: str) -> dict:
    """Best-effort fundamentals from filings. Returns {} when unavailable.

    Never raises — every caller treats {} as "this tier had nothing" and moves
    on, exactly as it already does for Alpha Vantage and FMP.
    """
    try:
        cik = edgar.cik_for(ticker)
        if not cik:
            return {}

        gaap, entity = _load(cik)
        if not gaap:
            return {}

        rev_rows = _best(gaap, "revenue")
        if not rev_rows:
            # This filer tags revenue outside the candidate set (banks and
            # insurers often do). Bail rather than emit a half-empty record
            # with no denominator for any of the margins.
            return {}
        anchor = rev_rows[-1]["end"]

        revenue       = _trailing(rev_rows)
        revenue_prior = _trailing(rev_rows, back=1)

        gross_profit = _trailing(_aligned(_best(gaap, "gross_profit"), anchor))
        if gross_profit is None:
            cor = _trailing(_aligned(_best(gaap, "cost_of_revenue"), anchor))
            if cor is not None and revenue is not None:
                gross_profit = revenue - cor

        net_income = _trailing(_aligned(_best(gaap, "net_income"), anchor))

        ocf   = _trailing(_aligned(_best(gaap, "ocf"), anchor))
        capex = _trailing(_aligned(_best(gaap, "capex"), anchor))
        fcf   = (ocf - capex) if (ocf is not None and capex is not None) else None

        eps_rows  = _aligned(_best(gaap, "eps", unit_hint="USD/shares"), anchor)
        eps       = _trailing(eps_rows)
        eps_prior = _trailing(eps_rows, back=1)

        equity  = _latest_instant(_aligned(_best(gaap, "equity"), anchor))
        lt_debt = _latest_instant(_aligned(_best(gaap, "lt_debt"), anchor))
        st_debt = _latest_instant(_aligned(_best(gaap, "st_debt"), anchor))
        debt    = None
        if lt_debt is not None or st_debt is not None:
            debt = (lt_debt or 0.0) + (st_debt or 0.0)

        # Banks and insurers tag a NET revenue line (JPM's `Revenues` is well
        # below its total revenue), which inflates every margin computed off
        # it. A margin above 1.0 proves the denominator is not a real top line,
        # so drop the ratios and keep the absolute figures, which are sound.
        gm = _ratio(gross_profit, revenue)
        pm = _ratio(net_income, revenue)
        if pm is not None and pm > 1.0:
            gm = pm = None

        out = {
            "ticker":              ticker,
            "name":                entity,
            "revenue_ttm":         revenue,
            "gross_margin":        gm,
            "profit_margin":       pm,
            "revenue_growth_yoy":  _growth(revenue, revenue_prior),
            "eps":                 round(eps, 4) if eps is not None else None,
            "eps_growth_yoy":      _growth(eps, eps_prior),
            "operating_cashflow":  ocf,
            "free_cashflow":       fcf,
            "debt_to_equity":      _ratio(debt, equity),
            "as_of":               anchor,
            "source":              "sec_xbrl",
        }
        # Only worth returning when at least one real number came back.
        if not any(v is not None for k, v in out.items()
                   if k not in ("ticker", "name", "as_of", "source")):
            return {}
        return out
    except Exception as exc:
        logger.warning("edgar_facts fundamentals failed for %s: %s", ticker, exc)
        return {}
