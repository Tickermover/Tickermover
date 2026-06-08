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
import logging
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

TRUSTED_HOSTS = ("q4cdn.com",)
_DECK_HINTS = ("presentation", "doc_earnings", "slides", "deck", "investor", "earnings-call")
_CACHE: dict = {}        # (ticker, quarter) -> url|"" (process-life cache)


def _trusted_pdf(url: str) -> bool:
    try:
        clean = url.lower().split("?")[0]
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return clean.endswith(".pdf") and any(host == d or host.endswith("." + d) for d in TRUSTED_HOSTS)


async def _brave(q: str, key: str) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.get("https://api.search.brave.com/res/v1/web/search",
                            params={"q": q, "count": 20},
                            headers={"X-Subscription-Token": key, "Accept": "application/json"})
            r.raise_for_status()
            data = r.json()
        return [it.get("url") for it in (data.get("web", {}) or {}).get("results", []) if it.get("url")]
    except Exception as exc:
        logger.warning(f"slides_finder: brave search failed: {exc}")
        return []


async def _serper(q: str, key: str) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            r = await c.post("https://google.serper.dev/search",
                             json={"q": q, "num": 20},
                             headers={"X-API-KEY": key, "Content-Type": "application/json"})
            r.raise_for_status()
            data = r.json()
        return [it.get("link") for it in data.get("organic", []) if it.get("link")]
    except Exception as exc:
        logger.warning(f"slides_finder: serper search failed: {exc}")
        return []


async def find_deck(company: str, ticker: str, quarter_label: str) -> str | None:
    """Return a q4cdn earnings-presentation PDF URL for this ticker+quarter, or
    None when no provider is configured / nothing suitable is found."""
    ck = ((ticker or "").upper(), quarter_label or "")
    if ck in _CACHE:
        return _CACHE[ck] or None

    serper = os.environ.get("SERPER_API_KEY")
    brave = os.environ.get("BRAVE_API_KEY") or os.environ.get("SEARCH_API_KEY")
    if not (serper or brave):
        return None

    name = (company or ticker or "").strip()
    query = f'{name} {quarter_label} earnings presentation filetype:pdf site:q4cdn.com'
    results = await (_serper(query, serper) if serper else _brave(query, brave))

    trusted = [u for u in results if _trusted_pdf(u)]
    # Prefer real decks (presentation/doc_earnings paths) over news-release PDFs.
    deck = next((u for u in trusted if any(h in u.lower() for h in _DECK_HINTS)), None)
    _CACHE[ck] = deck or ""
    return deck
