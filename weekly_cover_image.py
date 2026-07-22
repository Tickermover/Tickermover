"""
TickerMover — cover photography for the WEEKLY MAGAZINE.

Picks one editorial-quality photo per issue, keyed to that week's subject
(a sector or a marquee company), so the newsstand cover reads like a real
magazine instead of generated vector art.

Providers, tried in order (first one with a key configured wins):
  1. Unsplash  — UNSPLASH_ACCESS_KEY   (best editorial/business imagery)
  2. Pexels    — PEXELS_API_KEY

Both are free for commercial use. If NO key is set, this returns None and the
template falls back to the existing generated vector cover — the magazine keeps
working, it just isn't photographic.

EDITORIAL RULE: we deliberately bias the search AWAY from portraits of people.
A stock photo of a person beside "GOOGL — Outperform" implies that individual
endorses our call. Subject imagery (fabs, refineries, data centres, trading
floors) carries the same richness with none of that problem.

Attribution: Unsplash's API terms require crediting the photographer with a
link back. The returned dict carries `credit_name` / `credit_url`, and the
cover renders them.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_UNSPLASH_KEY = (os.environ.get("UNSPLASH_ACCESS_KEY") or "").strip()
_PEXELS_KEY = (os.environ.get("PEXELS_API_KEY") or "").strip()
_TIMEOUT = float(os.environ.get("COVER_IMAGE_TIMEOUT", "12"))

# Sector / theme → a concrete, photographable scene. Generic finance words
# ("communication services") return stock-photo handshakes; concrete nouns
# ("data centre servers") return real editorial imagery.
_SUBJECT_SCENES = {
    "semiconductor": "semiconductor wafer fab cleanroom",
    "semis": "semiconductor wafer fab cleanroom",
    "technology": "data centre server racks",
    "information technology": "data centre server racks",
    "communication services": "broadcast studio fibre network",
    "energy": "oil refinery industrial pipeline",
    "financials": "city financial district skyline",
    "health": "modern laboratory research",
    "healthcare": "modern laboratory research",
    "industrials": "factory automation robotics",
    "materials": "steel mill industrial production",
    "utilities": "power transmission grid pylons",
    "real estate": "city skyline construction cranes",
    "consumer discretionary": "retail storefront shopping district",
    "consumer staples": "supermarket shelves logistics",
    "artificial intelligence": "data centre gpu server racks",
    "ai": "data centre gpu server racks",
}

# Anything person-centric is pushed out of the result set.
_NEGATIVE = ("portrait", "businessman", "businesswoman", "model", "selfie")


def available() -> bool:
    return bool(_UNSPLASH_KEY or _PEXELS_KEY)


def _query_for(subject: str, tickers: list[str] | None = None) -> str:
    """Map the week's subject onto a concrete, photographable scene."""
    s = (subject or "").strip().lower()
    for key, scene in _SUBJECT_SCENES.items():
        if key in s:
            return scene
    # Unknown subject (often a single company): fall back to a neutral,
    # non-person business scene rather than searching the company name, which
    # tends to return logos and executive headshots.
    return (subject or "stock market trading floor").strip() or "stock market trading floor"


def _looks_like_person(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in _NEGATIVE)


async def _from_unsplash(query: str) -> dict | None:
    url = "https://api.unsplash.com/search/photos"
    params = {
        "query": query,
        "per_page": 12,
        "orientation": "portrait",  # magazine covers are 3:4
        "content_filter": "high",
    }
    headers = {"Authorization": f"Client-ID {_UNSPLASH_KEY}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(url, params=params, headers=headers)
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
    for p in results:
        alt = p.get("alt_description") or p.get("description") or ""
        if _looks_like_person(alt):
            continue
        urls, user = p.get("urls") or {}, p.get("user") or {}
        if not urls.get("regular"):
            continue
        return {
            "url": urls.get("regular"),
            "thumb": urls.get("small") or urls.get("regular"),
            "alt": alt or query,
            "credit_name": user.get("name") or "Unsplash",
            "credit_url": (user.get("links") or {}).get("html") or "https://unsplash.com",
            "source": "Unsplash",
        }
    return None


async def _from_pexels(query: str) -> dict | None:
    url = "https://api.pexels.com/v1/search"
    params = {"query": query, "per_page": 12, "orientation": "portrait"}
    headers = {"Authorization": _PEXELS_KEY}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(url, params=params, headers=headers)
        r.raise_for_status()
        photos = (r.json() or {}).get("photos") or []
    for p in photos:
        alt = p.get("alt") or ""
        if _looks_like_person(alt):
            continue
        src = p.get("src") or {}
        if not src.get("large"):
            continue
        return {
            "url": src.get("large"),
            "thumb": src.get("medium") or src.get("large"),
            "alt": alt or query,
            "credit_name": p.get("photographer") or "Pexels",
            "credit_url": p.get("photographer_url") or "https://pexels.com",
            "source": "Pexels",
        }
    return None


async def fetch(subject: str, tickers: list[str] | None = None) -> dict | None:
    """Return one cover photo for this week's subject, or None to fall back.

    Never raises — a cover image is a nice-to-have, and the weekly build must
    not fail because a photo provider is down or rate-limited.
    """
    if not available():
        return None
    query = _query_for(subject, tickers)
    for name, fn, key in (("Unsplash", _from_unsplash, _UNSPLASH_KEY),
                          ("Pexels", _from_pexels, _PEXELS_KEY)):
        if not key:
            continue
        try:
            hit = await fn(query)
            if hit:
                logger.info(f"weekly cover image from {name}: {query!r}")
                return hit
        except Exception as e:
            logger.warning(f"weekly cover image {name} failed ({query!r}): {e}")
    return None
