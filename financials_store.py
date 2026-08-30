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
# Periods per statement. MUST NOT EXCEED 5 — the FMP plan rejects anything
# higher with HTTP 402 "The values for 'limit' must be between 0 and 5 based on
# your current subscription". That 402 hits income, balance, cashflow, ratios
# AND key-metrics, so an over-large value here does not degrade the Financials
# tab, it empties it for every ticker in the universe. It also emptied the
# dilution scan's share-count column ("Unchecked" on all 80 enriched names) via
# the same endpoint, and retry traffic from the 402s burned the daily quota
# into 429s. Omitting the parameter does not help — the plan caps the response
# at 5 rows either way, so 5 is the ceiling, not a preference.
_LIMIT = 5          # periods per statement (5 quarters, or 5 years for annual)

# endpoint -> key in the payload
_ENDPOINTS = {
    "income":   "income-statement",
    "balance":  "balance-sheet-statement",
    "cashflow": "cash-flow-statement",
    "ratios":   "ratios",
    "metrics":  "key-metrics",
    "estimates": "analyst-estimates",
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
    if endpoint == "analyst-estimates":
        # This endpoint REQUIRES an explicit period (400 "Invalid or missing
        # query parameter - period" without one) and only annual is on Starter.
        params["period"] = "annual"
    elif period == "quarter":
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
    # CONCURRENTLY. These six endpoints are independent, but they ran in a
    # sequential `for` loop, so their latencies ADDED: a cold /api/financials
    # took 61s on LITE and the pane sat on "Loading statements..." forever
    # because the browser gave up first. The endpoints share a rate limiter, so
    # this does not spend FMP quota any faster — it just stops waiting serially.
    import asyncio as _aio
    keys = list(_ENDPOINTS)
    results = await _aio.gather(
        *(_fetch(coordinator, _ENDPOINTS[k], sym, per) for k in keys),
        return_exceptions=True,
    )
    for k, r in zip(keys, results):
        out[k] = r if isinstance(r, list) else []

    # ── Fallback tier: Eulerpool ────────────────────────────────────────────
    # ADD, never replace: FMP stays the primary and this only fills arrays it
    # left empty. The case it exists for is the per-symbol 402 — "this value
    # set for 'symbol' is not available under your current subscription" —
    # which no parameter change can get past and which silently emptied the
    # Financials tab for roughly 45% of the universe (DXCM, SNOW, CRWD, NET,
    # DDOG, ANET, SMCI, VRT, CEG in a 20-name sample).
    #
    # Annual only, deliberately: Eulerpool has no quarterly balance/cash-flow
    # to match its quarterly income, and a toggle showing one populated
    # statement beside two blank ones reads worse than the honest empty state.
    if per == "annual" and not all(out.get(k) for k in ("income", "balance", "cashflow")):
        try:
            import eulerpool_store as _ep
            if _ep.available():
                alt = await _ep.get_statements(sym)
                filled = [k for k in ("income", "balance", "cashflow", "estimates")
                          if not out.get(k) and alt.get(k)]
                for k in filled:
                    out[k] = alt[k]
                if filled:
                    out["fallback_source"] = "eulerpool"
                    out["fallback_filled"] = filled
                    logger.info("financials %s: eulerpool filled %s", sym, filled)
        except Exception as exc:
            logger.warning("financials %s: eulerpool fallback failed: %s", sym, exc)

    # `available` deliberately keys off the STATEMENTS, not any array. It used
    # to be `any(out[k] for k in _ENDPOINTS)`, so a ticker with nothing but
    # analyst estimates reported available:true and rendered an empty shell
    # instead of saying the statements were missing — which is how AAPL showed
    # available:true with income:[] while the FMP limit bug was live.
    out["available"] = any(out.get(k) for k in ("income", "balance", "cashflow"))
    out["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if cache and out["available"]:
        try:
            cache.set("financials", ck, out)
        except Exception:
            pass
    return out
