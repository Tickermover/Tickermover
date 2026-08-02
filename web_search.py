"""web_search.py — free web search for grounding AI answers.

Replaces Anthropic's server-side `web_search` tool (which has no free
equivalent) with the search APIs already configured in this project:

  1. SERPER_API_KEY  — serper.dev, Google results, best snippets
  2. BRAVE_API_KEY   — brave.com/search/api, independent index

Returns a normalised [{title, url, snippet, source}] list. Never raises —
callers treat an empty list as "no web context" and fall back to the filing.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

LAST_ERROR: dict = {}


def _key(*names: str) -> str:
    for n in names:
        v = (os.environ.get(n, "") or "").strip()
        if v:
            return v
    # tolerate loose naming ("Serper API Key")
    want = {"".join(c if c.isalnum() else "_" for c in n.upper()) for n in names}
    for k, v in os.environ.items():
        if "".join(c if c.isalnum() else "_" for c in k.upper()) in want and (v or "").strip():
            return v.strip()
    return ""


def available() -> list[str]:
    out = []
    if _key("SERPER_API_KEY"):
        out.append("serper")
    if _key("BRAVE_API_KEY"):
        out.append("brave")
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
                return [{"title": i.get("title", ""), "url": i.get("url", ""),
                         "snippet": i.get("description", ""), "source": "brave"}
                        for i in results[:count]]
            LAST_ERROR.update({"provider": "brave",
                               "detail": f"HTTP {r.status_code}: {r.text[:140]}"})
        except Exception as exc:
            LAST_ERROR.update({"provider": "brave", "detail": f"exception: {exc}"})
    return []


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
