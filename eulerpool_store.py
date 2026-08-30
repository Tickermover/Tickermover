"""
TickerMover — Eulerpool statements, the fallback for symbols FMP will not serve.

WHY THIS EXISTS
The FMP plan refuses roughly 45% of the universe outright, per-symbol:

    HTTP 402  "Premium Query Parameter: 'Special Endpoint : This value set for
               'symbol' is not available under your current subscription"

Measured on a 20-name sample: mega-caps answered, DXCM / SNOW / CRWD / NET /
DDOG / ANET / SMCI / VRT / CEG did not. That is a different wall from the
`limit` cap fixed in financials_store (which was universal and is now cleared),
and no amount of parameter tuning gets past it. So the Financials tab was dark
for every one of those names with nothing in the code to blame.

Eulerpool answers all of them on a free tier, by TICKER (no ISIN lookup needed),
and goes far deeper than the FMP plan's 5 periods — AAPL income statements run
back to 1983.

SHAPE
Everything here is translated into FMP's field names before it is returned, so
`financials_store` hands the UI the same contract it always did and no renderer
changes. Two conversions matter and are easy to get wrong:

  * VALUES ARE IN MILLIONS. Eulerpool reports DXCM FY2024 revenue as `4033`;
    FMP reports `4033000000`. Every monetary field is scaled by 1e6 on the way
    out. `diluted_eps` is per-share and must NOT be scaled; `shares` is in
    millions like everything else and must be.
  * FORWARD ESTIMATES SHARE THE ARRAY. A period ending in "e" ("2026-12-31e")
    is a forecast, not a filing. Those rows are pulled OUT of the statements —
    letting one through would put an invented year in the income statement —
    and re-emitted as `estimates`, which is a small bonus: the Estimates tab
    was empty for these same symbols for the same reason.

ORDER: Eulerpool returns oldest-first, FMP newest-first. The UI reads FMP's
order, so these are reversed.

SCOPE: annual only. Eulerpool exposes a separate quarterly income endpoint but
no matching quarterly balance/cash-flow, and a Financials tab whose quarterly
toggle showed an income statement with two blank neighbours would read as more
broken than the honest empty state. Quarterly stays FMP-only for now.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_KEY = os.environ.get("EULERPOOL_API_KEY", "").strip()
_BASE = "https://api.eulerpool.com/api/1/equity"
_TIMEOUT = float(os.environ.get("EULERPOOL_TIMEOUT", "20"))
# Periods kept per statement. The plan does not cap this — the table does, since
# each period is a COLUMN. Ten is roughly twice what the FMP plan allows and
# still readable inside the pane's horizontal scroll.
_PERIODS = int(os.environ.get("EULERPOOL_PERIODS", "10"))

LAST_STATUS: dict = {}

# Eulerpool name -> FMP name the renderer already reads.
_INCOME_MAP = {
    "revenue": "revenue",
    "costOfGoodsSold": "costOfRevenue",
    "grossIncome": "grossProfit",
    "researchDevelopment": "researchAndDevelopmentExpenses",
    "sgaExpense": "sellingGeneralAndAdministrativeExpenses",
    "totalOperatingExpense": "operatingExpenses",
    "ebit": "operatingIncome",
    "depreciationAmortization": "depreciationAndAmortization",
    "interestIncomeExpense": "interestExpense",
    "pretaxIncome": "incomeBeforeTax",
    "provisionforIncomeTaxes": "incomeTaxExpense",
    "netIncome": "netIncome",
    "nonRecurringItems": "totalOtherIncomeExpensesNet",
}
_BALANCE_MAP = {
    "cashShortTermInvestments": "cashAndShortTermInvestments",
    "receivables": "netReceivables",
    "inventory": "inventory",
    "otherCurrentAssets": "otherCurrentAssets",
    "currentAssets": "totalCurrentAssets",
    "propertyPlantEquipment": "propertyPlantEquipmentNet",
    "goodwill": "goodwill",
    "intangiblesAssets": "intangibleAssets",
    "longTermInvestments": "longTermInvestments",
    # NOT `assets` and NOT `allAssets`. Both are subtotals and naming either
    # one totalAssets is wrong in a way the page renders without complaint:
    # for DXCM FY2025 `assets` is 242.2 and `allAssets` 2985.6, against equity
    # of 2630.6 — a balance sheet claiming less in assets than in equity.
    # `capital` is the real total, and the accounting identity proves it:
    # equity 2630.6 + liabilities 3391.8 = capital 6022.4 exactly.
    # _translate re-checks that identity per row rather than trusting this.
    "capital": "totalAssets",
    "assets": "otherNonCurrentAssets",
    "accountsPayable": "accountPayables",
    "accruedLiability": "otherCurrentLiabilities",
    "shortTermDebt": "shortTermDebt",
    "currentLiabilities": "totalCurrentLiabilities",
    "longTermDebt": "longTermDebt",
    "fixedLiabilities": "totalNonCurrentLiabilities",
    "otherLiabilities": "otherNonCurrentLiabilities",
    "liabilities": "totalLiabilities",
    "intangibles": "intangibleAssets",
    "commonStock": "commonStock",
    "additionalPaidInCapital": "additionalPaidInCapital",
    "retainedEarnings": "retainedEarnings",
    "equity": "totalStockholdersEquity",
}
_CASHFLOW_MAP = {
    "netIncomeStartingLine": "netIncome",
    "amortization": "depreciationAndAmortization",
    "changesinWorkingCapital": "changeInWorkingCapital",
    "nonCashItems": "otherNonCashItems",
    "netOperatingCashFlow": "netCashProvidedByOperatingActivities",
    "capex": "capitalExpenditure",
    "netInvestingCashFlow": "netCashUsedForInvestingActivites",  # FMP's own typo
    "issuanceReductionCapitalStock": "commonStockIssuance",
    "issuanceReductionDebtNet": "debtRepayment",
    "cashDividendsPaid": "commonDividendsPaid",
    "netCashFinancingActivities": "netCashUsedProvidedByFinancingActivities",
    "fcf": "freeCashFlow",
}
# Per-share or already-absolute: must not be multiplied by 1e6.
_UNSCALED = {"epsdiluted", "eps"}


def available() -> bool:
    return bool(_KEY)


def _is_estimate(period: str) -> bool:
    """A trailing 'e' marks a forecast row ('2026-12-31e')."""
    return str(period or "").strip().lower().endswith("e")


def _date(period: str) -> str:
    return str(period or "").strip().rstrip("eE")


def _scale(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v) * 1_000_000
    except (TypeError, ValueError):
        return None


async def _get(path: str, root: bool = False) -> list:
    """GET one endpoint. `root=True` addresses /api/1/<path> directly, for the
    non-equity families (earning-calls/…, market/…) that do not sit under the
    equity base."""
    import httpx
    if not _KEY:
        return []
    base = _BASE.rsplit("/", 1)[0] if root else _BASE
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{base}/{path}", params={"token": _KEY})
        LAST_STATUS[path.split("/")[0]] = r.status_code
        if r.status_code != 200:
            LAST_STATUS[path.split("/")[0] + ":body"] = r.text[:140]
            logger.warning("eulerpool %s HTTP %s: %s", path, r.status_code, r.text[:140])
            return []
        data = r.json()
        return data if isinstance(data, list) else []
    except Exception as exc:
        LAST_STATUS[path.split("/")[0]] = f"exception: {exc}"[:120]
        logger.warning("eulerpool %s failed: %s", path, exc)
    return []


def _translate(rows: list, fmap: dict) -> tuple[list, list]:
    """(filed periods, forecast periods), newest first, in FMP field names."""
    filed, est = [], []
    for r in rows:
        if not isinstance(r, dict):
            continue
        period = r.get("period")
        if not period:
            continue
        out = {"date": _date(period), "symbol": r.get("ticker"),
               "period": "FY", "reportedCurrency": "USD"}
        for src, dst in fmap.items():
            if src in r:
                out[dst] = _scale(r.get(src))
        # EPS is per-share; shares are in millions like every other figure.
        if "diluted_eps" in r and r.get("diluted_eps") is not None:
            try:
                out["epsdiluted"] = float(r["diluted_eps"])
                out["eps"] = float(r["diluted_eps"])
            except (TypeError, ValueError):
                pass
        if r.get("shares") is not None:
            out["weightedAverageShsOutDil"] = _scale(r.get("shares"))
            out["weightedAverageShsOut"] = _scale(r.get("shares"))
        # Balance-sheet guard. A vendor's field NAMES are a guess about meaning;
        # the accounting identity is not. `assets` looked like the total and was
        # a subtotal, which renders as a plausible number rather than an error,
        # so check A = L + E per row and repair from the identity when the
        # mapped total contradicts it.
        eq, li = out.get("totalStockholdersEquity"), out.get("totalLiabilities")
        ta = out.get("totalAssets")
        if eq is not None and li is not None:
            implied = eq + li
            if ta is None or (implied and abs(ta - implied) / abs(implied) > 0.02):
                if ta is not None:
                    logger.warning("eulerpool %s %s: totalAssets %.0f contradicts "
                                   "equity+liabilities %.0f — using the identity",
                                   out.get("symbol"), out.get("date"), ta, implied)
                out["totalAssets"] = implied

        (est if _is_estimate(period) else filed).append(out)
    filed.reverse()
    est.reverse()
    return filed, est


def _as_estimates(rows: list) -> list:
    """Forecast income rows in the shape the Estimates tab reads.

    Forecast rows are SPARSE — revenue and netIncome are present, eps and
    shares are not — so epsAvg/analyst counts stay absent rather than being
    invented. A partly-filled estimates table beats the "no forward analyst
    estimates published" empty state these symbols show today.
    """
    out = []
    for r in rows:
        row = {"date": r.get("date"), "symbol": r.get("symbol")}
        if r.get("revenue") is not None:
            row["revenueAvg"] = r["revenue"]
        if r.get("netIncome") is not None:
            row["netIncomeAvg"] = r["netIncome"]
        if r.get("operatingIncome") is not None:
            row["ebitdaAvg"] = r["operatingIncome"]
        if r.get("epsdiluted") is not None:
            row["epsAvg"] = r["epsdiluted"]
        if len(row) > 2:
            out.append(row)
    return out


async def last_earnings_ms(ticker: str) -> int | None:
    """Epoch-millis of the most recent earnings CALL, or None.

    This is the REPORT date, which is the whole point: the `date` on an
    eps_quarters row is the QUARTER END, months earlier, and refreshing off that
    would either fire before the numbers exist or never fire at all. One cheap
    call, used as the staleness trigger so the expensive fetches only run when
    something has actually been reported.
    """
    rows = await _get(f"earning-calls/list/{(ticker or '').upper()}", root=True)
    best = None
    for r in rows:
        if not isinstance(r, dict):
            continue
        ts = r.get("datePublished")
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            continue
        if best is None or ts > best:
            best = ts
    return best


async def enrich(ticker: str) -> dict:
    """Universe fields FMP's plan refuses, from Eulerpool. {} when unavailable.

    ONLY the fields validated against known values are returned. Deliberately
    absent, having been checked and rejected:
      * beta — risk-metrics reports NVDA 0.16 and AAPL 0.43. NVIDIA's beta is
        ~2 and the detail page shows LITE at 1.51 against Eulerpool's 1.09.
        Wrong numbers are worse than blank ones on a risk field.
      * pe / ps — `valuation.pe` returns 0 for AAPL and NVDA, 0.02 for INTC.
      * target_upside_pct — no price-target endpoint; the estimates endpoint
        carries EPS and revenue, not analyst price targets.

    SCALES DIFFER WITHIN THE SAME PAYLOAD, which is exactly how a percent bug
    gets shipped: `grossMargin` is 46.91 (PERCENT) while `roe` is -0.0023 and
    `revenueGrowth3Y` -0.16 (FRACTIONS). The universe stores margins as
    fractions, so the percents are divided by 100 here and nowhere else.
    """
    sym = (ticker or "").upper().strip()
    if not sym or not _KEY:
        return {}
    import asyncio as _aio
    prof, met = await _aio.gather(_get(f"profile/{sym}"), _get(f"metrics/{sym}"))
    p = (prof[-1] if isinstance(prof, list) and prof else {}) or {}
    m = (met[-1] if isinstance(met, list) and met else {}) or {}
    if not isinstance(p, dict) or not isinstance(m, dict):
        return {}
    prof_ability = m.get("profitability") or {}

    def _num(v):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return f if f == f else None          # drop NaN

    out: dict = {}
    mcap = _num(p.get("mcap"))
    if mcap:                                   # reported in MILLIONS
        out["market_cap"] = mcap * 1_000_000
    shares = _num(p.get("shares"))
    if shares:
        out["shares_outstanding"] = shares * 1_000_000
    for src, dst in (("grossMargin", "gross_margin"), ("netMargin", "profit_margin"),
                     ("operatingMargin", "operating_margin")):
        v = _num(prof_ability.get(src))
        if v is not None:
            out[dst] = v / 100.0               # percent -> fraction

    # THE REPORT DATE. `last_earnings_date` is null across most of the universe,
    # and the "Recent earnings" panel falls back to the QUARTER END when it is —
    # which is why that panel showed NVDA at 31 Jul on 31 Aug, a month stale,
    # the day after NVDA actually reported. The panel is honest about it (it
    # prints "Quarter end ·"), but a feed headed "What's happening now" listing
    # month-old dates reads as broken data.
    ms = await last_earnings_ms(sym)
    if ms:
        import datetime as _dt
        try:
            out["last_earnings_date"] = _dt.datetime.fromtimestamp(
                ms / 1000, _dt.timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            pass
    return out


_ENRICH_NS = "eulerpool_enrich"
_ENRICH_TTL = int(os.environ.get("EULERPOOL_ENRICH_TTL_DAYS", "30")) * 86400
# In-process overlay so the 5-minute scoring loop can re-apply these for free.
# Nothing here does I/O — the network work happens only in the prewarm loop.
_ENRICH_MEM: dict[str, dict] = {}
# Fields this module is allowed to fill. It only ever fills a BLANK — a value
# the universe already computed always wins, so two sources can never disagree
# on screen and a Eulerpool outage cannot blank a populated row.
FILLS = ("market_cap", "gross_margin", "profit_margin",
         "operating_margin", "shares_outstanding", "last_earnings_date")


async def enrich_cached(ticker: str, force: bool = False) -> dict:
    """Enrichment for one ticker, cached 30 days, refreshed on a new earnings call.

    NETWORK. Call this from the background sweep, never from a request path.

    Two triggers, both needed. The 30-day TTL alone would leave a stale market
    cap and margins for weeks after results; an earnings check alone would never
    pick up a restatement or a fixed field. On a cache hit only the cheap
    earning-calls lookup runs, so a quarter with no results costs one call.
    """
    import asyncio as _aio
    import time as _t
    sym = (ticker or "").upper().strip()
    if not sym or not _KEY:
        return {}
    try:
        from kv_store import store as _kv
    except Exception:
        return await enrich(sym)

    doc = None
    if not force:
        try:
            doc = await _aio.to_thread(_kv.get, _ENRICH_NS, sym, _ENRICH_TTL)
        except Exception:
            doc = None

    if isinstance(doc, dict) and doc.get("fields"):
        seen = doc.get("earnings_ms") or 0
        latest = await last_earnings_ms(sym)
        if latest is None or latest <= seen:
            _ENRICH_MEM[sym] = doc["fields"]
            return doc["fields"]
        logger.info("eulerpool enrich %s: new earnings call, refreshing", sym)

    fields = await enrich(sym)
    if not fields:
        # Keep serving the previous copy rather than blanking the row.
        prev = (doc or {}).get("fields") or {}
        if prev:
            _ENRICH_MEM[sym] = prev
        return prev

    try:
        await _aio.to_thread(_kv.set, _ENRICH_NS, sym, {
            "fields": fields,
            "earnings_ms": await last_earnings_ms(sym),
            "at": int(_t.time()),
        })
    except Exception as exc:
        logger.warning("eulerpool enrich %s: cache write failed: %s", sym, exc)
    _ENRICH_MEM[sym] = fields
    return fields


def apply_cached(rows: list) -> int:
    """Overlay whatever enrichment is already in memory. NO I/O — safe to call
    from the scoring loop on every cycle. Returns how many blanks were filled."""
    n = 0
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        got = _ENRICH_MEM.get(str(r.get("ticker") or "").upper())
        if not got:
            continue
        for k in FILLS:
            if r.get(k) is None and got.get(k) is not None:
                r[k] = got[k]
                n += 1
    return n


async def share_growth(ticker: str) -> float | None:
    """Year-over-year change in diluted share count, as a FRACTION, or None.

    The dilution scan's own enrichment goes through FMP's quarterly income
    statement, which is both plan-gated per symbol and the first thing to hit
    the daily quota — a scan that logged "80 with share counts" earlier in the
    day came back with ZERO once the quota was spent, and every row then reads
    "Unchecked". Eulerpool carries `shares` on each income row, is not
    symbol-gated, and answers when FMP will not.

    Fraction, not percent: safeguards.reading() multiplies by 100 itself.
    """
    sym = (ticker or "").upper().strip()
    if not sym or not _KEY:
        return None
    rows = await _get(f"incomestatement/{sym}")
    filed = []
    for r in rows:
        if not isinstance(r, dict) or _is_estimate(r.get("period")):
            continue
        s = r.get("shares")
        try:
            s = float(s)
        except (TypeError, ValueError):
            continue
        if s > 0:
            filed.append((str(r.get("period")), s))
    if len(filed) < 2:
        return None
    filed.sort(key=lambda x: x[0])          # oldest first
    prior, now = filed[-2][1], filed[-1][1]
    if not prior:
        return None
    return (now - prior) / prior


def missing_fields(row: dict) -> bool:
    """True when this row has a gap Eulerpool could fill.

    `last_earnings_date` is included deliberately: it is null on most of the
    universe, and a row can have a perfectly good market cap while still
    dating its last results to the quarter end.
    """
    if not isinstance(row, dict):
        return False
    return (any(row.get(k) is None for k in FILLS[:3])
            or row.get("last_earnings_date") is None)


async def get_statements(ticker: str) -> dict:
    """Annual income / balance / cash-flow / estimates in FMP's field names."""
    sym = (ticker or "").upper().strip()
    if not sym or not _KEY:
        return {}

    # Concurrently — three independent endpoints. Serially these added ~3x to a
    # cold financials call that was already slow enough for the pane to give up.
    import asyncio as _aio
    inc_raw, bal_raw, cf_raw = await _aio.gather(
        _get(f"incomestatement/{sym}"),
        _get(f"balancesheet/{sym}"),
        _get(f"cashflowstatement/{sym}"),
    )

    income, inc_est = _translate(inc_raw, _INCOME_MAP)
    balance, _ = _translate(bal_raw, _BALANCE_MAP)
    cashflow, _ = _translate(cf_raw, _CASHFLOW_MAP)

    return {
        "income":    income[:_PERIODS],
        "balance":   balance[:_PERIODS],
        "cashflow":  cashflow[:_PERIODS],
        "estimates": _as_estimates(inc_est)[:_PERIODS],
        "source":    "eulerpool",
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
