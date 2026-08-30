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


async def _get(path: str) -> list:
    import httpx
    if not _KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(f"{_BASE}/{path}", params={"token": _KEY})
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


async def get_statements(ticker: str) -> dict:
    """Annual income / balance / cash-flow / estimates in FMP's field names."""
    sym = (ticker or "").upper().strip()
    if not sym or not _KEY:
        return {}

    inc_raw = await _get(f"incomestatement/{sym}")
    bal_raw = await _get(f"balancesheet/{sym}")
    cf_raw = await _get(f"cashflowstatement/{sym}")

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
