"""
slides_finder.py — locate a company's earnings-presentation deck on its IR CDN
when it isn't filed to SEC.

Q4 Inc (q4cdn.com) hosts the IR materials — earnings releases AND investor
presentation decks — for a large share of US public companies, e.g.
  https://s206.q4cdn.com/<id>/files/doc_earnings/2025/q4/presentation/4Q25-Earnings-Presentation.pdf

The per-company server/id isn't guessable, so we discover the deck URL with a
web-search API (Brave or Serper), restricted to q4cdn PDFs and biased toward
presentation/deck paths. The found PDF is rendered in-app via /api/doc-pdf
(q4cdn is allow-listed there).

No search key configured → returns None and the UI falls back to a manual
"Find slides" web-search link. Set ONE of:
  SERPER_API_KEY   (serper.dev — generous free tier)
  BRAVE_API_KEY    (Brave Search API — free tier ~2k/mo)
"""
from __future__ import annotations

import os
import re
import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Reputable IR content CDNs we trust to fetch/embed deck PDFs from. Keep in
# sync with doc_pdf._HOST_ALLOW (minus sec.gov).
TRUSTED_HOSTS = ("q4cdn.com", "gcs-web.com", "irpass.com",
                 "investorroom.com", "media-server.com")
_DECK_HINTS = ("presentation", "doc_earnings", "slides", "deck", "investor", "earnings-call")
_CACHE: dict = {}        # (ticker, quarter) -> url|"" (process-life cache)


def _trusted_pdf(url: str) -> bool:
    try:
        clean = url.lower().split("?")[0]
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return clean.endswith(".pdf") and any(host == d or host.endswith("." + d) for d in TRUSTED_HOSTS)


async def _brave(q: str, key: str) -> list[tuple[str, str]]:
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get("https://api.search.brave.com/res/v1/web/search",
                            params={"q": q, "count": 20},
                            headers={"X-Subscription-Token": key, "Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
        return [(it.get("url"), it.get("title") or "")
                for it in (data.get("web", {}) or {}).get("results", []) if it.get("url")]
    except Exception as exc:
        logger.warning(f"slides_finder: brave search failed: {exc}")
        return []


async def _serper(q: str, key: str) -> list[tuple[str, str]]:
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post("https://google.serper.dev/search",
                             json={"q": q, "num": 20},
                             headers={"X-API-KEY": key, "Content-Type": "application/json"})
            r.raise_for_status()
            data = r.json()
        return [(it.get("link"), it.get("title") or "")
                for it in data.get("organic", []) if it.get("link")]
    except Exception as exc:
        logger.warning(f"slides_finder: serper search failed: {exc}")
        return []


_STOP = {"inc", "corp", "corporation", "co", "ltd", "limited", "holdings", "holding",
         "group", "technologies", "technology", "plc", "the", "company",
         "international", "industries", "incorporated", "systems"}


def _company_tokens(name: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", (name or "").lower())
            if len(w) >= 4 and w not in _STOP]


def _parse_quarter(label: str) -> tuple[int | None, int | None]:
    m = re.search(r"q\s*([1-4])", (label or "").lower())
    y = re.search(r"(\d{4})", label or "")
    return (int(m.group(1)) if m else None, int(y.group(1)) if y else None)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _quarter_ok(text: str, qn: int | None, year: int | None) -> bool:
    """True if the URL/title clearly references the requested fiscal quarter."""
    t = _norm(text)
    if qn is None:
        return True
    if not (f"q{qn}" in t or f"{qn}q" in t):
        return False
    if year is None:
        return True
    y = str(year); y2 = y[2:]
    return any(tok in t for tok in (y, f"fy{y}", f"fy{y2}",
                                    f"q{qn}{y2}", f"{qn}q{y2}", f"q{qn}{y}", f"{qn}q{y}"))


async def find_deck(company: str, ticker: str, quarter_label: str) -> dict:
    """Return {url, reason, n}. reason ∈ no_key | found | no_results | cache*.
    Tries Serper then Brave, with a site-restricted query first and a broad
    fallback, accepting only PDFs on the trusted IR CDNs."""
    ck = ((ticker or "").upper(), quarter_label or "")
    if ck in _CACHE:
        c = _CACHE[ck]
        return {"url": c or None, "reason": "cache" if c else "cache_empty", "n": 0}

    serper = os.environ.get("SERPER_API_KEY")
    brave = os.environ.get("BRAVE_API_KEY") or os.environ.get("SEARCH_API_KEY")
    if not (serper or brave):
        return {"url": None, "reason": "no_key", "n": 0}

    name = (company or ticker or "").strip()
    ctoks = _company_tokens(name)
    qn, year = _parse_quarter(quarter_label)
    sites = " OR ".join(f"site:{d}" for d in TRUSTED_HOSTS)
    queries = [
        f'{name} {quarter_label} earnings presentation filetype:pdf ({sites})',
        f'{name} {quarter_label} investor presentation slides filetype:pdf',
    ]
    seen: list[tuple[str, str]] = []
    for q in queries:
        res = await _serper(q, serper) if serper else []
        if not res and brave:
            res = await _brave(q, brave)
        seen.extend(res or [])

        # Keep only trusted PDFs whose URL/title clearly match BOTH the company
        # and the requested quarter — so we never host a wrong-ticker or
        # wrong-quarter deck. Prefer real presentation paths.
        def _company_match(url, title):
            blob = (title + " " + url).lower()
            return (not ctoks) or any(w in blob for w in ctoks)

        cand = []
        for url, title in seen:
            if not _trusted_pdf(url):
                continue
            if not _quarter_ok(url + " " + title, qn, year):
                continue
            if not _company_match(url, title):
                continue
            is_deck = any(h in url.lower() for h in _DECK_HINTS)
            cand.append((0 if is_deck else 1, url))
        cand.sort(key=lambda x: x[0])
        if cand:
            deck = cand[0][1]
            _CACHE[ck] = deck
            return {"url": deck, "reason": "found", "n": len(seen)}
    _CACHE[ck] = ""
    return {"url": None, "reason": "no_match", "n": len(seen)}
