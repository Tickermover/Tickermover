"""
AlphaHunt — Dependencies & ripple-risk generator.

For a single stock, produces a STRUCTURED, web-grounded map of what the company
depends on, so the user can see how a shock to a supplier / customer / end-market
could ripple into the stock they're looking at:

    {
      "exposure":     [{"name","pct"}],                  # end-market / segment weights (donut)
      "dependencies": [{"name","ticker","type","what","ripple"}],  # depended companies
      "summary":      "...",
      "sources":      [{"n","title","url"}],
      "model","status"
    }

Uses the Anthropic Messages API with the server-side `web_search` tool so the
relationships are grounded in real sources (cited). Kept on Sonnet/Haiku (NOT
Opus) so a first generation is a few cents, then cached 30 days — negligible.
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx

import usage_log

logger = logging.getLogger(__name__)

_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Sonnet by default (clean structured synthesis from search results); override to
# Haiku via ANTHROPIC_DEPS_MODEL for an even cheaper run.
_MODEL = (
    os.environ.get("ANTHROPIC_DEPS_MODEL")
    or "claude-sonnet-4-6"
).strip()
_TIMEOUT = float(os.environ.get("DEPS_TIMEOUT", "150"))
_TYPES = {"supplier", "customer", "input", "partner"}

DISCLAIMER = (
    "AI-estimated from public sources — directional, not a verified financial "
    "disclosure. Verify against filings before relying on it."
)


def available() -> bool:
    return bool(_KEY)


def _system() -> str:
    return (
        "You are a sell-side analyst mapping a company's DEPENDENCIES and ripple "
        "risk: who supplies it, who its biggest customers are, the raw inputs and "
        "partners it relies on, and which end-market verticals drive its revenue. "
        "Use web search to ground every relationship in real, recent sources. "
        "Output ONLY one strict JSON object — no prose outside it, no markdown "
        "except the single ```json fence."
    )


def _user(ticker: str, t: dict) -> str:
    name = t.get("name") or ticker
    sector = t.get("sub_sector") or t.get("sector") or ""
    return (
        f"Company: {name} ({ticker}){(' — ' + sector) if sector else ''}.\n\n"
        "Research and return its dependency / ripple-risk map as JSON with this exact shape:\n"
        "{\n"
        '  "exposure": [{"name": "<end-market or segment>", "pct": <number>}],\n'
        '  "dependencies": [{"name": "<company>", "ticker": "<US ticker or empty>", '
        '"type": "supplier|customer|input|partner", "what": "<=6 words", "ripple": "<=14 words"}],\n'
        '  "summary": "<=55 words on the biggest ripple risks"\n'
        "}\n\n"
        "Rules:\n"
        "- exposure: 4-8 segments that approximately sum to 100 (revenue by end-market / business segment). Numbers only.\n"
        "- dependencies: 5-10 of the MOST material suppliers, customers, key inputs or partners. "
        "Put a US-listed `ticker` ONLY when you're confident; otherwise leave it empty.\n"
        "- `what` = what they provide/represent; `ripple` = how a shock there hits this company.\n"
        "- Prefer concrete, named relationships over generic ones. Skip anything you can't ground.\n"
        "Respond with ONLY the JSON (inside one ```json fence)."
    )


def _coerce(parsed: object) -> dict:
    out = {"exposure": [], "dependencies": [], "summary": ""}
    if not isinstance(parsed, dict):
        return out
    exp = parsed.get("exposure")
    if isinstance(exp, list):
        for e in exp:
            if not isinstance(e, dict):
                continue
            nm = str(e.get("name", "")).strip()[:40]
            try:
                pct = round(float(e.get("pct")), 1)
            except (TypeError, ValueError):
                continue
            if nm and pct > 0:
                out["exposure"].append({"name": nm, "pct": pct})
        out["exposure"] = out["exposure"][:8]
    deps = parsed.get("dependencies")
    if isinstance(deps, list):
        for d in deps:
            if not isinstance(d, dict):
                continue
            nm = str(d.get("name", "")).strip()[:48]
            if not nm:
                continue
            typ = str(d.get("type", "")).strip().lower()
            if typ not in _TYPES:
                typ = "partner"
            tk = re.sub(r"[^A-Z.\-]", "", str(d.get("ticker", "")).upper())[:8]
            out["dependencies"].append({
                "name": nm,
                "ticker": tk,
                "type": typ,
                "what": str(d.get("what", "")).strip()[:60],
                "ripple": str(d.get("ripple", "")).strip()[:110],
            })
        out["dependencies"] = out["dependencies"][:12]
    out["summary"] = str(parsed.get("summary", "")).strip()[:500]
    return out


def _extract_json(text: str) -> object:
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    blob = m.group(1) if m else None
    if blob is None:
        m2 = re.search(r"\{.*\}", text, re.DOTALL)
        blob = m2.group(0) if m2 else None
    if blob is None:
        return None
    try:
        return json.loads(blob)
    except Exception:
        return None


async def generate_dependencies(ticker: str, ticker_data: dict | None) -> dict:
    ticker = ticker.upper()
    t = ticker_data or {"ticker": ticker}
    if not available():
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    body = {
        "model": _MODEL,
        "max_tokens": 2600,
        "system": [
            {"type": "text", "text": _system(), "cache_control": {"type": "ephemeral", "ttl": "1h"}}
        ],
        "tools": [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
        ],
        "messages": [{"role": "user", "content": _user(ticker, t)}],
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": _KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
        )
        if r.status_code >= 400:
            detail = r.text[:500]
            logger.error(f"dependencies_gen {ticker} → {r.status_code} ({_MODEL}): {detail}")
            raise RuntimeError(f"Anthropic {r.status_code}: {detail}")
        data = r.json()

    _u = data.get("usage") or {}
    usage_log.record("dependencies", _MODEL, _u, ticker=ticker)
    logger.info(
        "dependencies_gen %s (%s): in=%s out=%s searches~%s",
        ticker, _MODEL, _u.get("input_tokens"), _u.get("output_tokens"),
        (_u.get("server_tool_use") or {}).get("web_search_requests"),
    )

    content = data.get("content", [])
    last_search = -1
    for i, b in enumerate(content):
        if b.get("type") == "web_search_tool_result":
            last_search = i
    text_parts, sources, seen = [], [], set()
    for i, b in enumerate(content):
        bt = b.get("type")
        if bt == "text" and i > last_search:
            text_parts.append(b.get("text", ""))
        elif bt == "web_search_tool_result":
            for res in b.get("content", []) or []:
                url = res.get("url")
                if url and url not in seen:
                    seen.add(url)
                    sources.append({"n": len(sources) + 1, "title": res.get("title") or url, "url": url})

    parsed = _extract_json("".join(text_parts))
    out = _coerce(parsed)
    if not out["exposure"] and not out["dependencies"]:
        raise RuntimeError("Empty dependency response")
    out.update({"ticker": ticker, "sources": sources, "model": _MODEL, "status": "ready"})
    return out
