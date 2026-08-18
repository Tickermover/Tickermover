"""news_gdelt — free, keyless news fallback under Finnhub and yfinance.

WHY
`DataCoordinator.get_news` tries Finnhub (which 302s on the free tier) then
yfinance. When both come back empty the panel simply has no headlines. GDELT
2.0's Doc API needs no key and has no quota, so it can answer on a day every
metered source is out.

THE PRECISION PROBLEM — AND WHY THE TITLE FILTER IS NOT OPTIONAL
GDELT matches the query anywhere in the article BODY, so a bare company query
is very noisy: `"Coca-Cola"` returned articles about a NASCAR driver, a
basketball game and an ad-agency merger, and a plain `Nvidia` query returned a
Ukrainian video game. Unfiltered, this source would put visible nonsense on a
stock page — worse than showing nothing.

So we pull a WIDE page (75 records) and keep only articles whose TITLE actually
names the company. GDELT has no title-only search operator, hence filtering
client-side. Measured on a live index: NVIDIA 10 relevant hits out of 75, and
Coca-Cola 0 of 75 — the zero is the filter working, not failing. Returning
nothing is the correct answer when the day's index holds no real headline.

RATE LIMITS
GDELT answers HTTP 429 readily (it did on the first call of every test run).
There is no documented quota, just a per-IP gap of roughly five seconds. Calls
are therefore serialised behind a lock with a minimum gap, and a 429 puts the
whole module to sleep for a couple of minutes rather than hammering it — the
same shape as the cooling-off in llm_free.

GDELT returns NO snippet, only a title, so `sentiment_score` stays None and the
headline carries the whole payload. That matches the existing news dict shape.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

API = "https://api.gdeltproject.org/api/v2/doc/doc"
UA = "TickerMover/1.0 (+https://tickermover.com)"

# Pull wide, then filter hard — see the module docstring. Most of a 75-record
# page is body-mention noise; the survivors are the real headlines.
MAX_RECORDS = 75
_MIN_GAP = 5.5          # seconds between calls, per GDELT's per-IP pacing
_COOLDOWN_S = 120       # how long to stand down after a 429

_lock = asyncio.Lock()
_last_call = 0.0
_cooldown_until = 0.0

# Words that carry no identifying power once the legal suffix is stripped, so
# matching on them would re-admit the noise the filter exists to remove.
_STOP = {"inc", "corp", "corporation", "co", "company", "ltd", "limited",
         "plc", "holdings", "group", "the", "and", "class", "common", "stock"}


def _tidy(title: str) -> str:
    """GDELT tokenises titles ("S & P 500", "52 - Week"). Close the gaps."""
    t = re.sub(r"\s+", " ", (title or "").strip())
    t = re.sub(r"\s+([,.;:!?%])", r"\1", t)
    t = re.sub(r"\(\s+", "(", t)
    t = re.sub(r"\s+\)", ")", t)
    return t.strip()


def _to_ts(seendate: str) -> int:
    """GDELT stamps are `20260818T210000Z`."""
    try:
        return int(datetime.strptime(seendate, "%Y%m%dT%H%M%SZ")
                   .replace(tzinfo=timezone.utc).timestamp())
    except Exception:
        return 0


def _terms(ticker: str, name: str) -> list[str]:
    """Lower-cased tokens whose presence in a title proves relevance."""
    out = {(ticker or "").lower().strip()}
    n = re.sub(r"[^\w\s&-]", " ", (name or "")).lower()
    if n:
        out.add(n.strip())
        # "NVIDIA Corp" should also match a headline saying just "Nvidia".
        for w in n.split():
            if len(w) > 3 and w not in _STOP:
                out.add(w)
    return [t for t in out if len(t) >= 3]


def _query(ticker: str, name: str) -> str:
    # A quoted phrase is far tighter than loose words, and the company name
    # beats the ticker — bare tickers collide with ordinary English.
    subject = (name or "").strip() or (ticker or "").strip()
    subject = re.sub(r'["\\]', "", subject)
    return f'"{subject}" sourcelang:english'


async def fetch(ticker: str, name: str = "", days: int = 7,
                limit: int = 15, timeout: float = 20.0) -> list[dict]:
    """Headlines in the shape get_news returns. [] on any failure."""
    global _last_call, _cooldown_until

    subject = (name or ticker or "").strip()
    if not subject:
        return []
    if time.time() < _cooldown_until:
        return []

    params = {
        "query": _query(ticker, name),
        "mode": "ArtList", "format": "json",
        "maxrecords": MAX_RECORDS, "sort": "DateDesc",
        "timespan": f"{max(1, int(days))}d",
    }

    try:
        async with _lock:
            r = None
            # GDELT answers 429 to the FIRST call of a burst and then serves
            # normally a few seconds later, so one retry is worth far more than
            # standing the module down. Only a second 429 is treated as a real
            # rate limit — otherwise a single cold call disables the tier for
            # two minutes, which is what the first version did.
            for attempt in range(2):
                gap = time.time() - _last_call
                if gap < _MIN_GAP:
                    await asyncio.sleep(_MIN_GAP - gap)
                _last_call = time.time()

                async with httpx.AsyncClient(timeout=timeout) as c:
                    r = await c.get(API, headers={"User-Agent": UA}, params=params)

                if r.status_code != 429:
                    break
                if attempt == 0:
                    logger.debug("gdelt: 429 on first call for %s — retrying", ticker)

        if r is None:
            return []
        if r.status_code == 429:
            _cooldown_until = time.time() + _COOLDOWN_S
            logger.info("gdelt: 429 twice — standing down %ss", _COOLDOWN_S)
            return []
        if r.status_code != 200:
            logger.info("gdelt: HTTP %s for %s", r.status_code, ticker)
            return []

        # GDELT answers with an HTML error page rather than a JSON error body
        # when a query upsets it, so check the shape before parsing.
        body = r.text.strip()
        if not body.startswith("{"):
            logger.info("gdelt: non-JSON body for %s (%s)", ticker, body[:60])
            return []

        articles = r.json().get("articles") or []
        terms = _terms(ticker, name)

        out: list[dict] = []
        seen: set[str] = set()
        for a in articles:
            title = _tidy(a.get("title") or "")
            url = a.get("url") or ""
            if not title or not url or url in seen:
                continue
            low = title.lower()
            if not any(t in low for t in terms):
                continue          # body-only mention — the noise this drops
            seen.add(url)
            out.append({
                "headline":        title,
                "source":          a.get("domain") or "",
                "url":             url,
                "datetime":        _to_ts(a.get("seendate") or ""),
                "sentiment_score": None,   # GDELT ArtList carries no snippet
            })
            if len(out) >= limit:
                break
        return out
    except Exception as exc:
        logger.warning("gdelt fetch failed for %s: %s", ticker, exc)
        return []
