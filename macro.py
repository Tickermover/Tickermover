"""macro — US macro context from FRED (St. Louis Fed).

WHY
The app had no macro source at all: no policy rate, no yield curve, no
inflation, no unemployment. Every other data lane (prices, fundamentals, news,
search) had at least two providers while this one had zero.

NO API KEY NEEDED
FRED publishes an API that wants a registered key, but its graph CSV endpoint
(`fredgraph.csv?id=<SERIES>`) serves the same observations with **no key and no
registration**, and honours `cosd` to bound the start date. That keeps this in
line with the other free bottom tiers ([[edgar_facts]], [[news_gdelt]]) — there
is nothing to configure and nothing that can run out of credit.

One quirk drove the fetch shape: `cosd` is honoured for a SINGLE series but
silently IGNORED when several ids are passed at once (`id=DGS10,DGS2` returned
13,100 rows back to 1976 regardless). So series are fetched one at a time with
their own start date rather than batched.

FRAMING — THIS IS CONTEXT, NOT A SIGNAL
Per the product's disclosure-not-prediction stance, nothing here is allowed to
become an entry/exit trigger, and the UI must not colour these values green or
red: an inverted curve or rising unemployment is *context a reader should know*,
not an instruction. `direction` is a factual up/down against the prior reading
and carries no judgement. Each series ships a plain-English `note` saying what
it measures, so a card can explain itself without implying a trade.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import date, datetime, timedelta

import httpx

logger = logging.getLogger(__name__)

CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
UA = "TickerMover/1.0 (+https://tickermover.com)"

KV_NS = "macro_v1"
KV_KEY = "snapshot"
# FRED posts at most once a day per series, so anything under a few hours is
# wasted work. Six hours keeps a same-day release fresh without polling.
MAX_AGE_S = 6 * 3600

# Three years covers a 12-month change plus enough history for a sparkline,
# and keeps even the daily series to roughly 750 rows.
_HISTORY_DAYS = 365 * 3
_SPARK_POINTS = 24

# `yoy` converts an index level into a year-on-year percentage — CPI is
# published as an index (332.813), which is meaningless on a card.
SERIES: list[dict] = [
    {"id": "DFF", "label": "Fed funds rate", "unit": "pct",
     "note": "What banks charge each other overnight — the Fed's policy rate."},
    {"id": "DGS10", "label": "10-year Treasury", "unit": "pct",
     "note": "The yield on 10-year US government debt."},
    {"id": "DGS2", "label": "2-year Treasury", "unit": "pct",
     "note": "The 2-year yield, which tracks rate expectations most closely."},
    {"id": "T10Y2Y", "label": "10y minus 2y", "unit": "pct",
     "note": "The gap between the 10-year and 2-year yields. Below zero is "
             "called an inverted curve."},
    {"id": "CPIAUCSL", "label": "Inflation (CPI)", "unit": "pct", "transform": "yoy",
     "note": "Consumer prices versus a year ago, all items."},
    {"id": "CPILFESL", "label": "Core inflation", "unit": "pct", "transform": "yoy",
     "note": "Consumer prices excluding food and energy, versus a year ago."},
    {"id": "UNRATE", "label": "Unemployment", "unit": "pct",
     "note": "Share of the labour force looking for work."},
    {"id": "MORTGAGE30US", "label": "30-year mortgage", "unit": "pct",
     "note": "Average rate on a 30-year fixed US home loan."},
    {"id": "DTWEXBGS", "label": "US dollar index", "unit": "index",
     "note": "The dollar against a trade-weighted basket of currencies."},
    {"id": "DCOILWTICO", "label": "WTI crude oil", "unit": "usd",
     "note": "Spot price of a barrel of West Texas Intermediate."},
]


def _parse(text: str) -> list[tuple[date, float]]:
    """FRED CSV → [(date, value)], with missing observations dropped.

    FRED writes a missing value as `.` (and, on some series, as empty) rather
    than omitting the row — a holiday in a daily series looks like a reading.
    """
    out: list[tuple[date, float]] = []
    rdr = csv.reader(io.StringIO(text))
    rows = list(rdr)
    for row in rows[1:]:
        if len(row) < 2:
            continue
        raw = (row[-1] or "").strip()
        if not raw or raw == ".":
            continue
        try:
            out.append((date.fromisoformat(row[0].strip()), float(raw)))
        except Exception:
            continue
    out.sort(key=lambda p: p[0])
    return out


def _yoy(points: list[tuple[date, float]]) -> list[tuple[date, float]]:
    """Index level → year-on-year percent, matched on the nearest prior year."""
    out: list[tuple[date, float]] = []
    for i, (d, v) in enumerate(points):
        target = d - timedelta(days=365)
        prior = None
        for pd_, pv in points[:i]:
            if pd_ <= target + timedelta(days=20):
                prior = pv
            else:
                break
        if prior:
            out.append((d, round((v - prior) / abs(prior) * 100, 2)))
    return out


def _value_on_or_before(points: list[tuple[date, float]], target: date) -> float | None:
    prior = None
    for d, v in points:
        if d <= target:
            prior = v
        else:
            break
    return prior


def _spark(points: list[tuple[date, float]], n: int = _SPARK_POINTS) -> list[float]:
    """Evenly-spaced sample of the tail, oldest first, for a sparkline."""
    if len(points) <= n:
        return [round(v, 4) for _, v in points]
    step = len(points) / n
    return [round(points[min(len(points) - 1, int(i * step))][1], 4) for i in range(n)]


async def _fetch_one(client: httpx.AsyncClient, spec: dict) -> dict | None:
    start = (date.today() - timedelta(days=_HISTORY_DAYS)).isoformat()
    try:
        r = await client.get(CSV_URL, params={"id": spec["id"], "cosd": start},
                             headers={"User-Agent": UA})
        if r.status_code != 200:
            logger.info("macro: %s HTTP %s", spec["id"], r.status_code)
            return None
        points = _parse(r.text)
        if spec.get("transform") == "yoy":
            points = _yoy(points)
        if not points:
            return None

        last_d, last_v = points[-1]
        prev = points[-2][1] if len(points) > 1 else None
        m1 = _value_on_or_before(points, last_d - timedelta(days=30))
        y1 = _value_on_or_before(points, last_d - timedelta(days=365))

        return {
            "id":         spec["id"],
            "label":      spec["label"],
            "unit":       spec["unit"],
            "note":       spec["note"],
            "latest":     round(last_v, 4),
            "latest_date": last_d.isoformat(),
            # Absolute moves. Percentage-point moves for a rate, plain units
            # otherwise — a rate that goes 4.2 -> 4.7 has risen 0.5 points, and
            # calling that "+11.9%" would be true but useless on a card.
            "change_1m":  round(last_v - m1, 4) if m1 is not None else None,
            "change_12m": round(last_v - y1, 4) if y1 is not None else None,
            # Factual only — see the module docstring. Not a verdict.
            "direction":  ("flat" if prev is None or last_v == prev
                           else "up" if last_v > prev else "down"),
            "spark":      _spark(points),
        }
    except Exception as exc:
        logger.warning("macro: %s failed: %s", spec["id"], exc)
        return None


async def snapshot(force: bool = False) -> dict:
    """All series with their latest readings. Cached; never raises."""
    if not force:
        try:
            import kv_store
            cached = kv_store.store.get(KV_NS, KV_KEY, max_age_s=MAX_AGE_S)
            if cached and cached.get("series"):
                return cached
        except Exception as exc:
            logger.debug("macro: cache read failed: %s", exc)

    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
            results = await asyncio.gather(
                *(_fetch_one(c, s) for s in SERIES), return_exceptions=True)
    except Exception as exc:
        logger.warning("macro: fetch failed: %s", exc)
        results = []

    series = [r for r in results if isinstance(r, dict)]
    if not series:
        # Serve a stale snapshot rather than nothing — macro moves slowly, so
        # yesterday's curve is far more useful than an empty panel.
        try:
            import kv_store
            stale = kv_store.store.get(KV_NS, KV_KEY)
            if stale and stale.get("series"):
                stale["stale"] = True
                return stale
        except Exception:
            pass
        return {"as_of": None, "series": [], "source": "FRED", "stale": True}

    out = {
        "as_of": max(s["latest_date"] for s in series),
        "fetched_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "series": series,
        "source": "Federal Reserve Bank of St. Louis (FRED)",
        "source_url": "https://fred.stlouisfed.org",
    }
    try:
        import kv_store
        kv_store.store.set(KV_NS, KV_KEY, out)
    except Exception as exc:
        logger.debug("macro: cache write failed: %s", exc)
    return out
