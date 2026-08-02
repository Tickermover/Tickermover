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

import zlib

import httpx

logger = logging.getLogger(__name__)

_UNSPLASH_KEY = (os.environ.get("UNSPLASH_ACCESS_KEY") or "").strip()
_PEXELS_KEY = (os.environ.get("PEXELS_API_KEY") or "").strip()
_TIMEOUT = float(os.environ.get("COVER_IMAGE_TIMEOUT", "12"))

# Sector / theme → ORDERED fallback queries (specific first, broad last). Each is
# a hard, photographable BUSINESS/INDUSTRIAL noun. Generic finance words
# ("communication services") return handshakes, and vague/short terms drift to
# landscapes — so we anchor on concrete objects and keep a business-only fallback.
_SUBJECT_SCENES = {
    "semiconductor": ["semiconductor microchip", "computer chip circuit board"],
    "semis":         ["semiconductor microchip", "computer chip circuit board"],
    "technology":    ["data center server room", "computer server technology"],
    "information technology": ["data center server room", "computer server technology"],
    "communication services": ["fiber optic network cables", "television broadcast studio"],
    "energy":        ["oil refinery plant", "oil and gas industry"],
    "oil":           ["oil refinery plant", "oil and gas industry"],
    "financials":    ["financial district skyscrapers", "stock exchange trading"],
    "financial":     ["financial district skyscrapers", "stock exchange trading"],
    "bank":          ["financial district skyscrapers", "stock exchange trading"],
    "health":        ["pharmaceutical laboratory", "medical research lab"],
    "healthcare":    ["pharmaceutical laboratory", "medical research lab"],
    "pharma":        ["pharmaceutical laboratory", "medical research lab"],
    "biotech":       ["biotech laboratory science", "medical research lab"],
    "industrials":   ["factory assembly line", "industrial manufacturing robots"],
    "materials":     ["steel factory industry", "industrial metal production"],
    "utilities":     ["electricity transmission towers", "power plant industry"],
    "real estate":   ["city skyscrapers construction", "urban office skyline"],
    "consumer discretionary": ["modern retail store interior", "shopping mall retail"],
    "consumer staples": ["supermarket grocery aisle", "grocery store shelves"],
    "retail":        ["modern retail store interior", "shopping mall retail"],
    "automotive":    ["car factory assembly line", "automobile manufacturing"],
    "artificial intelligence": ["data center server room", "circuit board technology"],
    "ai":            ["data center server room", "circuit board technology"],
    # AI / tech sub-themes (match the chain-map + weekly subjects specifically)
    "cloud":         ["data center server room", "cloud computing servers"],
    "software":      ["data center server room", "software code screen"],
    "saas":          ["data center server room", "cloud computing servers"],
    "cybersecurity": ["cybersecurity data protection", "network security server room"],
    "security":      ["cybersecurity data protection", "network security server room"],
    "networking":    ["fiber optic network cables", "network server cables"],
    "network":       ["fiber optic network cables", "network server cables"],
    "optical":       ["fiber optic cables light", "optical fiber network"],
    "photonics":     ["fiber optic cables light", "optical fiber network"],
    "memory":        ["computer memory chip module", "semiconductor memory"],
    "hbm":           ["computer memory chip module", "semiconductor memory"],
    "data center":   ["data center server room", "server racks technology"],
    "payment":       ["credit card payment terminal", "digital payment technology"],
    "fintech":       ["financial technology payment", "digital payment technology"],
    "robot":         ["industrial robot arm factory", "robotics automation"],
    "automation":    ["industrial robot arm factory", "factory automation"],
    "defense":       ["military aircraft aerospace", "defense technology"],
    "aerospace":     ["military aircraft aerospace", "jet aircraft"],
    "electric vehicle": ["electric vehicle charging station", "electric car"],
    "battery":       ["lithium battery cells", "battery manufacturing"],
    "lithium":       ["lithium mine", "battery manufacturing"],
    "media":         ["television broadcast studio", "streaming media screens"],
    "streaming":     ["streaming media screens", "television broadcast studio"],
    "telecom":       ["cellular tower telecom", "5g network tower"],
    "obesity":       ["medical injection pen", "pharmaceutical laboratory"],
    "glp":           ["medical injection pen", "pharmaceutical laboratory"],
    "power":         ["power plant electricity", "electricity transmission towers"],
    "grid":          ["electricity transmission towers", "power grid"],
}
# Marquee tickers -> concrete scene, used when the SUBJECT is a single company
# (the subject string "NVIDIA Corporation (NVDA)" matches no sector keyword, so
# without this it drifts to generic charts). Grouped by what the business does.
def _grp(tks, scenes):
    return {t: scenes for t in tks}
_TICKER_SCENES: dict[str, list[str]] = {}
_TICKER_SCENES.update(_grp(
    ["NVDA","AMD","AVGO","MRVL","TSM","ARM","QCOM","INTC","SMCI","ASML","AMAT","LRCX","KLAC","TER","ON","TXN","ADI","ALAB","CRDO","LSCC","ENTG","MKSI","ACLS","ONTO"],
    ["semiconductor microchip macro", "computer chip circuit board"]))
_TICKER_SCENES.update(_grp(["MU","SNDK"], ["computer memory chip module", "semiconductor memory"]))
_TICKER_SCENES.update(_grp(
    ["SNOW","MSFT","GOOGL","GOOG","AMZN","ORCL","CRM","DDOG","MDB","NOW","PLTR"],
    ["data center server room", "cloud computing servers"]))
_TICKER_SCENES.update(_grp(["ANET","CIEN","COHR","FN","AAOI","LITE","NET"], ["fiber optic network cables", "network server cables"]))
_TICKER_SCENES.update(_grp(["CRWD","PANW","FTNT","ZS","S","OKTA","CYBR","QLYS","TENB","RBRK","GEN"], ["cybersecurity data protection", "network security server room"]))
_TICKER_SCENES.update(_grp(["V","MA","PYPL","FI","FIS","AXP","XYZ","AFRM","GPN","TOST","COF"], ["credit card payment terminal", "digital payment technology"]))
_TICKER_SCENES.update(_grp(["TSLA","RIVN","LCID","GM","F","APTV","MGA"], ["electric vehicle charging station", "car factory assembly line"]))
_TICKER_SCENES.update(_grp(["LLY","NVO","AMGN","VKTX","PFE","MRK","REGN","HIMS"], ["medical injection pen", "pharmaceutical laboratory"]))
_TICKER_SCENES.update(_grp(["CEG","VST","GEV","NEE","TLN","NRG","VRT","ETN","HUBB"], ["power plant electricity", "electricity transmission towers"]))
_TICKER_SCENES.update(_grp(["LMT","RTX","NOC","GD","LHX","BA"], ["military aircraft aerospace", "defense technology"]))
_TICKER_SCENES.update(_grp(["ISRG","ROK","EMR","PH","ABB","TER"], ["industrial robot arm factory", "robotics automation"]))
# Business-only fallback for an unknown subject (usually a single company): we do
# NOT search the company name — that returns logos and executive headshots — and
# we deliberately avoid any term that lets the result drift to nature/landscape.
_UNIVERSAL = ["stock market financial charts", "financial district skyscrapers", "modern corporate office"]

# Anything person-centric is pushed out of the result set.
_NEGATIVE = ("portrait", "businessman", "businesswoman", "woman", "man ", "model", "selfie", "face")


def available() -> bool:
    return bool(_UNSPLASH_KEY or _PEXELS_KEY)


def _queries_for(subject: str, tickers: list[str] | None = None) -> list[str]:
    """Ordered candidate queries for the week's subject — specific scene(s) first,
    then a business-only fallback so we never end up on generic/landscape filler.
    For a SECTOR/theme subject the sector keyword leads; for a single COMPANY the
    marquee-ticker scene leads (the company name matches no keyword, so without this
    it drifts to generic charts)."""
    s = (subject or "").strip().lower()
    subj_scenes: list[str] = []
    for key, scenes in _SUBJECT_SCENES.items():
        if key in s:
            subj_scenes = list(scenes)
            break
    tick_scenes: list[str] = []
    for t in (tickers or [])[:4]:
        sc = _TICKER_SCENES.get(str(t).strip().upper())
        if sc:
            tick_scenes = list(sc)
            break
    is_sector = any(w in s for w in ("sector", "index", " etf"))
    ordered = (subj_scenes + tick_scenes) if is_sector else (tick_scenes + subj_scenes)
    out, seen = [], set()
    for q in (ordered or []) + _UNIVERSAL:
        if q not in seen:
            seen.add(q)
            out.append(q)
    return out


def _looks_like_person(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in _NEGATIVE)


def _pick(candidates: list, seed: str):
    """Choose one candidate, varying by seed but STABLE for a given seed.

    Taking candidates[0] meant an identical query always produced an identical
    photo — and since the scene map sends every technology subject to the same
    query, four of the five published issues carried the same data-centre shot
    and the shelf read as one article repeated. Seeding on the issue's week
    spreads them out while keeping a re-fetch of the same issue idempotent."""
    if not candidates:
        return None
    if not seed:
        return candidates[0]
    return candidates[zlib.crc32(str(seed).encode("utf-8")) % len(candidates)]


async def _from_unsplash(query: str, seed: str = "") -> dict | None:
    url = "https://api.unsplash.com/search/photos"
    params = {
        "query": query,
        "per_page": 15,
        "content_filter": "high",
        # No orientation filter on purpose: forcing portrait starves niche
        # industrial queries and pushes results toward generic filler. The cover
        # uses object-fit:cover, so any orientation crops cleanly to 3:4.
    }
    headers = {"Authorization": f"Client-ID {_UNSPLASH_KEY}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(url, params=params, headers=headers)
        r.raise_for_status()
        results = (r.json() or {}).get("results") or []
    ok = [p for p in results
          if not _looks_like_person(p.get("alt_description") or p.get("description") or "")
          and (p.get("urls") or {}).get("regular")]
    p = _pick(ok, seed)
    if not p:
        return None
    alt = p.get("alt_description") or p.get("description") or ""
    urls, user = p.get("urls") or {}, p.get("user") or {}
    return {
        "url": urls.get("regular"),
        "thumb": urls.get("small") or urls.get("regular"),
        "alt": alt or query,
        "credit_name": user.get("name") or "Unsplash",
        "credit_url": (user.get("links") or {}).get("html") or "https://unsplash.com",
        "source": "Unsplash",
    }


async def _from_pexels(query: str, seed: str = "") -> dict | None:
    url = "https://api.pexels.com/v1/search"
    params = {"query": query, "per_page": 15}
    headers = {"Authorization": _PEXELS_KEY}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(url, params=params, headers=headers)
        r.raise_for_status()
        photos = (r.json() or {}).get("photos") or []
    ok = [p for p in photos
          if not _looks_like_person(p.get("alt") or "") and (p.get("src") or {}).get("large")]
    p = _pick(ok, seed)
    if not p:
        return None
    src = p.get("src") or {}
    return {
        "url": src.get("large"),
        "thumb": src.get("medium") or src.get("large"),
        "alt": p.get("alt") or query,
        "credit_name": p.get("photographer") or "Pexels",
        "credit_url": p.get("photographer_url") or "https://pexels.com",
        "source": "Pexels",
    }


async def fetch(subject: str, tickers: list[str] | None = None,
                seed: str = "") -> dict | None:
    """Return one cover photo for this week's subject, or None to fall back.

    Never raises — a cover image is a nice-to-have, and the weekly build must
    not fail because a photo provider is down or rate-limited.
    """
    if not available():
        return None
    for query in _queries_for(subject, tickers):
        for name, fn, key in (("Unsplash", _from_unsplash, _UNSPLASH_KEY),
                              ("Pexels", _from_pexels, _PEXELS_KEY)):
            if not key:
                continue
            try:
                hit = await fn(query, seed)
                if hit:
                    hit["query"] = query          # kept for relevance debugging
                    logger.info(f"weekly cover image from {name}: {query!r}")
                    return hit
            except Exception as e:
                logger.warning(f"weekly cover image {name} failed ({query!r}): {e}")
    return None
