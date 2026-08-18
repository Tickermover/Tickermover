"""web_search.py — free web search for grounding AI answers.

Replaces Anthropic's server-side `web_search` tool (which has no free
equivalent) with the search APIs already configured in this project. Tiers are
tried in order; the first that returns results wins:

  1. SERPER_API_KEY  — serper.dev, Google results, best snippets
  2. BRAVE_API_KEY   — brave.com/search/api, independent index
  3. TAVILY_API_KEY  — tavily.com, ~1k searches/month free, LLM-shaped snippets
  4. DuckDuckGo      — NO KEY, no quota; the floor that always answers

WHY 3 AND 4 EXIST (added 18 Aug 2026)
Brave began returning `HTTP 402 Usage limit exceeded`, leaving Serper as a
single point of failure for every grounded answer on the site — when its free
credits run out too, every AI surface loses its web context at once. Tiers 3
and 4 are purely additive: the existing order is untouched and neither is
consulted while an earlier tier is still answering.

Tier 4 is the important one. It needs no key and has no quota, so the chain can
no longer be exhausted by billing alone — it reads DuckDuckGo's HTML endpoint
using the bs4/lxml already in requirements. Scraping is fragile by nature, but
its failure mode is the empty list callers already handle, which is exactly
today's behaviour when every paid tier is out of credit.

Returns a normalised [{title, url, snippet, source}] list. Never raises —
callers treat an empty list as "no web context" and fall back to the filing.
"""
from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse

import httpx

logger = logging.getLogger(__name__)

LAST_ERROR: dict = {}

# DuckDuckGo's HTML endpoint serves an empty shell to obvious bots.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# DuckDuckGo bot-blocks datacenter IPs with HTTP 202 (observed live on Railway,
# 18 Aug 2026). That state persists, so back off for a while instead of paying
# a round-trip on every search. See _duckduckgo().
_DDG_BLOCK_S = 900
_ddg_blocked_until = 0.0


def _norm(s: str) -> str:
    """Collapse a key name to letters+digits joined by single underscores.

    The old version mapped each non-alphanumeric character to one underscore,
    which meant a name typed with BOTH a separator and a space missed: the
    Tavily key was entered in Railway as `TAVILY_ API_Key`, normalising to
    `TAVILY__API_KEY` (two underscores) and never matching `TAVILY_API_KEY`.
    Collapsing runs makes the loose match live up to its name.
    """
    return "_".join(p for p in re.split(r"[^A-Za-z0-9]+", s.upper()) if p)


def _key(*names: str) -> str:
    for n in names:
        v = (os.environ.get(n, "") or "").strip()
        if v:
            return v
    # tolerate loose naming ("Serper API Key", "TAVILY_ API_Key")
    want = {_norm(n) for n in names}
    for k, v in os.environ.items():
        if _norm(k) in want and (v or "").strip():
            return v.strip()
    return ""


def available() -> list[str]:
    out = []
    if _key("SERPER_API_KEY"):
        out.append("serper")
    if _key("BRAVE_API_KEY"):
        out.append("brave")
    if _key("TAVILY_API_KEY"):
        out.append("tavily")
    # No key to check — DuckDuckGo is always available, which is the whole point
    # of having it. Listed last so /api/event-intel-status reads in tier order.
    out.append("duckduckgo")
    return out


async def search(query: str, count: int = 5, timeout: float = 12.0) -> list[dict]:
    """Search the web. Returns [] when nothing is configured or all fail."""
    q = (query or "").strip()
    if not q:
        return []

    serper = _key("SERPER_API_KEY")
    if serper:
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post("https://google.serper.dev/search",
                                 json={"q": q, "num": count},
                                 headers={"X-API-KEY": serper,
                                          "Content-Type": "application/json"})
            if r.status_code == 200:
                data = r.json()
                out = []
                for item in (data.get("organic") or [])[:count]:
                    out.append({"title": item.get("title", ""),
                                "url": item.get("link", ""),
                                "snippet": item.get("snippet", ""),
                                "source": "serper"})
                if data.get("answerBox", {}).get("snippet"):
                    out.insert(0, {"title": "Answer box", "url": "",
                                   "snippet": data["answerBox"]["snippet"],
                                   "source": "serper"})
                if out:
                    return out
            else:
                LAST_ERROR.update({"provider": "serper",
                                   "detail": f"HTTP {r.status_code}: {r.text[:140]}"})
        except Exception as exc:
            LAST_ERROR.update({"provider": "serper", "detail": f"exception: {exc}"})

    brave = _key("BRAVE_API_KEY")
    if brave:
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get("https://api.search.brave.com/res/v1/web/search",
                                params={"q": q, "count": count},
                                headers={"X-Subscription-Token": brave,
                                         "Accept": "application/json"})
            if r.status_code == 200:
                results = (r.json().get("web") or {}).get("results") or []
                out = [{"title": i.get("title", ""), "url": i.get("url", ""),
                        "snippet": i.get("description", ""), "source": "brave"}
                       for i in results[:count]]
                # Guarded with `if out` so a 200-with-no-results falls THROUGH
                # to the tiers below instead of returning []. Same idiom the
                # serper tier above already uses.
                if out:
                    return out
            else:
                LAST_ERROR.update({"provider": "brave",
                                   "detail": f"HTTP {r.status_code}: {r.text[:140]}"})
        except Exception as exc:
            LAST_ERROR.update({"provider": "brave", "detail": f"exception: {exc}"})

    # ── 3. Tavily — free ~1k searches/month, built for LLM grounding ──────
    # Inert until TAVILY_API_KEY is set; costs nothing to leave unconfigured.
    tavily = _key("TAVILY_API_KEY")
    if tavily:
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.post("https://api.tavily.com/search",
                                 json={"query": q, "max_results": count,
                                       "search_depth": "basic"},
                                 headers={"Authorization": f"Bearer {tavily}",
                                          "Content-Type": "application/json"})
            if r.status_code == 200:
                data = r.json()
                out = [{"title": i.get("title", ""), "url": i.get("url", ""),
                        # Tavily calls the snippet "content".
                        "snippet": i.get("content", ""), "source": "tavily"}
                       for i in (data.get("results") or [])[:count]]
                if data.get("answer"):
                    out.insert(0, {"title": "Answer", "url": "",
                                   "snippet": data["answer"], "source": "tavily"})
                if out:
                    return out
            else:
                LAST_ERROR.update({"provider": "tavily",
                                   "detail": f"HTTP {r.status_code}: {r.text[:140]}"})
        except Exception as exc:
            LAST_ERROR.update({"provider": "tavily", "detail": f"exception: {exc}"})

    # ── 4. DuckDuckGo — no key, no quota, last resort ─────────────────────
    try:
        return await _duckduckgo(q, count, timeout)
    except Exception as exc:
        LAST_ERROR.update({"provider": "duckduckgo", "detail": f"exception: {exc}"})
    return []


async def _duckduckgo(q: str, count: int, timeout: float) -> list[dict]:
    """Scrape DuckDuckGo's HTML endpoint. No key, no quota, no SLA.

    KNOWN LIMITATION — READ BEFORE RELYING ON THIS TIER.
    DuckDuckGo answers **HTTP 202** to requests from datacenter IPs; that is its
    anti-bot response, not a transient error. It works from a residential IP
    (i.e. local dev) and is refused from Railway, so in production this tier
    usually returns nothing. It is kept because it costs one request, needs no
    key, and does answer from some hosts — but Tavily, not this, is the real
    fallback once Serper and Brave are out.

    Because a blocked host stays blocked, a 202/403/429 puts the tier to sleep
    rather than spending a round-trip on every future search.

    This is the floor of the chain, so it must never raise and never block for
    long. A layout change upstream degrades it to [] — the same thing every
    caller already sees when the paid tiers are out of credit.
    """
    global _ddg_blocked_until
    if time.time() < _ddg_blocked_until:
        return []

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.post("https://html.duckduckgo.com/html/", data={"q": q},
                         headers={"User-Agent": _UA})
    if r.status_code in (202, 403, 429):
        _ddg_blocked_until = time.time() + _DDG_BLOCK_S
        LAST_ERROR.update({"provider": "duckduckgo",
                           "detail": f"HTTP {r.status_code} (bot-blocked; "
                                     f"skipping {_DDG_BLOCK_S}s)"})
        return []
    if r.status_code != 200:
        LAST_ERROR.update({"provider": "duckduckgo",
                           "detail": f"HTTP {r.status_code}"})
        return []

    from bs4 import BeautifulSoup            # already a hard requirement
    soup = BeautifulSoup(r.text, "lxml")

    out: list[dict] = []
    for res in soup.select("div.result")[: count * 2]:
        a = res.select_one("a.result__a")
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        if not title:
            continue
        sn = res.select_one(".result__snippet")
        out.append({"title": title,
                    "url": _unwrap(a.get("href", "")),
                    "snippet": sn.get_text(" ", strip=True) if sn else "",
                    "source": "duckduckgo"})
        if len(out) >= count:
            break
    if not out:
        LAST_ERROR.update({"provider": "duckduckgo",
                           "detail": "no results parsed (layout may have changed)"})
    return out


def _unwrap(href: str) -> str:
    """DuckDuckGo wraps every hit in //duckduckgo.com/l/?uddg=<encoded>."""
    if not href:
        return ""
    if "uddg=" not in href:
        return href
    try:
        if href.startswith("//"):
            href = "https:" + href
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        return urllib.parse.unquote(qs.get("uddg", [""])[0]) or href
    except Exception:
        return href


def as_context(results: list[dict], limit: int = 8) -> str:
    """Render results as a labelled block for an LLM prompt."""
    if not results:
        return ""
    lines = []
    for i, r in enumerate(results[:limit], 1):
        sn = (r.get("snippet") or "").strip()
        if not sn:
            continue
        lines.append(f"[W{i}] {r.get('title','').strip()} — {sn} (source: {r.get('url','')})")
    return "\n".join(lines)
