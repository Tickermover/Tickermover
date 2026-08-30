"""
TickerMover — "has this company reported since we wrote that?"

WHY THIS EXISTS
Every AI surface caches its output for weeks: bottom lines 15 days, dependency
maps 30, concall summaries 30, operating KPIs and PDF narratives their own TTLs.
That is correct for cost, and wrong for accuracy at exactly one moment — the day
results land. A dependency map or a bottom line written before an earnings
release keeps being served for up to a month AFTER the numbers that would change
it are public, and nothing in the page says it is out of date.

A time-based TTL cannot fix that: shorten it and you pay to regenerate content
that has not changed; lengthen it and you serve stale reads through a quarter.
The event, not the clock, is the right trigger.

THE DATE HAS TO BE THE REPORT DATE
`eps_quarters[].date` is the QUARTER END — 2025-06-30 for results announced in
late July or August. Gating on that fires weeks BEFORE the numbers exist, so the
regeneration reads the same old filings and burns the budget for nothing. This
module uses Eulerpool's `earning-calls/list`, whose `datePublished` is when the
call actually happened.

COST
One lookup per ticker per 12 hours, shared by every caller through a
process-level cache and the durable KV. Checking twelve AI surfaces for one
ticker costs one call, not twelve.

USAGE
    import earnings_gate
    doc = kv.get(ns, sym, ttl)
    if doc and await earnings_gate.is_stale(sym, doc):
        doc = None                      # reported since — regenerate
    ...
    await earnings_gate.stamp(doc, sym)  # before writing back
    kv.set(ns, sym, doc)
"""
from __future__ import annotations

import logging
import os
import time

logger = logging.getLogger(__name__)

# Field written into every cached AI document.
STAMP_KEY = "_earnings_ms"

_NS = "earnings_report_ms"
# How long a report date is trusted before re-checking. Earnings dates do not
# move; this is only about noticing a NEW one, and half a day is well inside the
# window between a release and anyone reading a stale summary.
_TTL_S = int(os.environ.get("EARNINGS_GATE_TTL_S", str(12 * 3600)))

_MEM: dict[str, tuple[float, int | None]] = {}   # sym -> (checked_at, report_ms)


def _enabled() -> bool:
    try:
        import eulerpool_store as ep
        return ep.available()
    except Exception:
        return False


async def report_ms(ticker: str) -> int | None:
    """Epoch-millis of the latest earnings CALL for `ticker`, or None.

    None means "cannot tell", and every caller treats that as NOT stale — an
    outage at the data provider must never invalidate the whole AI cache at
    once and trigger a regeneration storm across the universe.
    """
    sym = (ticker or "").upper().strip()
    if not sym or not _enabled():
        return None

    hit = _MEM.get(sym)
    if hit and (time.time() - hit[0]) < _TTL_S:
        return hit[1]

    import asyncio
    try:
        from kv_store import store as _kv
        doc = await asyncio.to_thread(_kv.get, _NS, sym, _TTL_S)
        if isinstance(doc, dict) and "ms" in doc:
            _MEM[sym] = (time.time(), doc.get("ms"))
            return doc.get("ms")
    except Exception:
        pass

    try:
        import eulerpool_store as ep
        ms = await ep.last_earnings_ms(sym)
    except Exception as exc:
        logger.debug("earnings_gate %s: lookup failed: %s", sym, exc)
        return None

    _MEM[sym] = (time.time(), ms)
    try:
        from kv_store import store as _kv
        await asyncio.to_thread(_kv.set, _NS, sym, {"ms": ms})
    except Exception:
        pass
    return ms


async def is_stale(ticker: str, doc) -> bool:
    """True when `doc` was generated BEFORE the company's latest earnings call.

    Deliberately conservative — returns False whenever it cannot prove
    staleness: no stamp (documents written before this existed), no report date,
    or any error. A false negative serves one stale summary until its normal TTL
    expires; a false positive regenerates the entire universe against a
    rate-limited free AI chain.
    """
    if not isinstance(doc, dict):
        return False
    stamped = doc.get(STAMP_KEY)
    if not stamped:
        return False
    latest = await report_ms(ticker)
    if not latest:
        return False
    try:
        return int(latest) > int(stamped)
    except (TypeError, ValueError):
        return False


async def stamp(doc, ticker: str):
    """Record the current report date on a document about to be cached.

    Content generated now reflects everything reported up to now, so it is only
    stale once a LATER call appears. Returns the doc for chaining.
    """
    if isinstance(doc, dict):
        ms = await report_ms(ticker)
        if ms:
            doc[STAMP_KEY] = ms
    return doc
