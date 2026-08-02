"""financials_store.py — full financial statements per ticker.

The dashboard only ever had point-in-time metrics (margins, ratios, one
quarter of revenue). This module fetches the actual statements — income,
balance sheet, cash flow — for several periods, plus per-period ratios and
key metrics, so the Financials / Valuation / Estimates tabs can show what a
site like stockanalysis.com shows.

Source: FMP `/stable/` endpoints via data_coordinator._fmp_stable (paid
Starter key, 300 req/min). Cached 24h in the KV store so a ticker costs a
handful of calls per day, not per page view.

Shape returned by get_financials():
    {
      "ticker": "NVDA", "period": "annual"|"quarter",
      "income":  [ {...}, ... ],   # newest first, ~8 periods
      "balance": [ {...}, ... ],
      "cashflow":[ {...}, ... ],
      "ratios":  [ {...}, ... ],
      "metrics": [ {...}, ... ],
      "fetched_at": iso8601,
      "available": bool,
    }
Every list degrades to [] independently — one endpoint missing from the plan
never blanks the rest.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_TTL_S = 24 * 3600
_LIMIT = 8          # periods per statement (8 quarters ≈ 2y, 8 years for annual)

# endpoint -> key in the payload
_ENDPOINTS = {
    "income":   "income-statement",
    "balance":  "balance-sheet-statement",
    "cashflow": "cash-flow-statement",
    "ratios":   "ratios",
    "metrics":  "key-metrics",
}


def _cache():
    try:
        import kv_store
        return kv_store.store
    except Exception:
        return None


LAST_STATUS: dict = {}


async def _fetch(coordinator, endpoint: str, ticker: str, period: str) -> list:
    """One statement endpoint, fetched directly.

    Deliberately NOT via data_coordinator._fmp_stable: that helper marks an
    endpoint dead process-wide on a single error, so one 402 on a quarterly
    call also killed the annual fetch for every later ticker. We reuse its
    rate limiter but keep our own failure handling, and record the HTTP
    status in LAST_STATUS so a plan-gated endpoint is visible instead of
    silently returning [].
    """
    import httpx
    import config
    if not config.FMP_API_KEY:
        return []
    params = {"symbol": ticker, "limit": _LIMIT, "apikey": config.FMP_API_KEY}
    if period == "quarter":
        params["period"] = "quarter"
    skey = f"{endpoint}:{period}"
    try:
        lim = getattr(coordinator, "_fmp_limiter", None)
        if lim is not None:
            await lim.wait()
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                f"https://financialmodelingprep.com/stable/{endpoint}", params=params
            )
        LAST_STATUS[skey] = r.status_code
        if r.status_code != 200:
            LAST_STATUS[skey + ":body"] = r.text[:140]
            logger.warning("financials: %s %s (%s) HTTP %s: %s",
                           ticker, endpoint, period, r.status_code, r.text[:140])
            return []
        data = r.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and data.get("symbol"):
            return [data]
        if isinstance(data, dict) and data.get("Error Message"):
            LAST_STATUS[skey + ":body"] = str(data)[:140]
        return []
    except Exception as exc:
        LAST_STATUS[skey] = f"exception: {exc}"[:120]
        logger.warning("financials: %s %s (%s) failed: %s", ticker, endpoint, period, exc)
    return []


async def get_financials(coordinator, ticker: str, period: str = "annual",
                         force: bool = False) -> dict:
    """Cached statements for one ticker. `period` is 'annual' or 'quarter'."""
    sym = (ticker or "").upper().strip()
    per = "quarter" if str(period).lower().startswith("q") else "annual"
    if not sym:
        return {"available": False, "ticker": sym, "period": per}

    ck = f"{sym}:{per}"
    cache = _cache()
    if cache and not force:
        hit = cache.get("financials", ck, max_age_s=_TTL_S)
        if hit and any(hit.get(k) for k in _ENDPOINTS):
            return hit

    out: dict = {"ticker": sym, "period": per}
    for key, endpoint in _ENDPOINTS.items():
        out[key] = await _fetch(coordinator, endpoint, sym, per)
    out["available"] = any(out.get(k) for k in _ENDPOINTS)
    out["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if cache and out["available"]:
        try:
            cache.set("financials", ck, out)
        except Exception:
            pass
    return out
