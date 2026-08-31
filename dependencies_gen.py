"""
TickerMover — Dependencies & ripple-risk generator.

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
import anthropic_shim

logger = logging.getLogger(__name__)


def _ai_error(r) -> str:
    """Reader-facing text for a failed AI call.

    Nothing Anthropic is called here — every request goes through
    anthropic_shim to the free-provider chain — so the old
    `f"Anthropic {status}: {body}"` was wrong on both counts: it named a
    vendor we do not call, and because this string is rendered straight into
    the pane it published provider names, HTTP codes and billing wording to
    the reader. The shim's own 503 body is already written for a human; use
    it, and fall back to one plain sentence.
    """
    try:
        msg = ((r.json() or {}).get("error") or {}).get("message")
    except Exception:
        msg = None
    return msg or ("AI generation is temporarily unavailable. It retries automatically.")

_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Sonnet by default (clean structured synthesis from search results); override to
# Haiku via ANTHROPIC_DEPS_MODEL for an even cheaper run.
_MODEL = (
    os.environ.get("ANTHROPIC_DEPS_MODEL")
    or "claude-sonnet-5"
).strip()
_TIMEOUT = float(os.environ.get("DEPS_TIMEOUT", "150"))
_TYPES = {"supplier", "customer", "input", "partner"}

DISCLAIMER = (
    "AI-estimated from public sources — directional, not a verified financial "
    "disclosure. Verify against filings before relying on it."
)


def available() -> bool:
    """Whether a generation request can actually be answered.

    Was `bool(_KEY)` — an ANTHROPIC_API_KEY read at import time. Nothing here
    calls Anthropic: the request goes through anthropic_shim.post(), which
    ignores the headers it is passed and routes to the free provider chain. So
    that key was never what answered, and its absence never meant nothing could.
    On this deployment it was present-but-empty, which switched this feature off
    while five healthy free providers sat idle behind it.
    """
    return anthropic_shim.generation_available()


def _system() -> str:
    return (
        "You are a sell-side analyst mapping a company's DEPENDENCIES and ripple "
        "risk: who supplies it, who its biggest customers are, the raw inputs and "
        "partners it relies on, and which end-market verticals drive its revenue. "
        "Use web search to ground every relationship in real, recent sources. "
        "This is generic research for a UK audience, NOT financial advice and NOT "
        "a personal recommendation: keep every field descriptive, never instruct "
        "the reader to buy, sell, hold or trade, never say 'you should', and use "
        "no hype or promised returns. "
        "Output ONLY one strict JSON object — no prose outside it, no markdown "
        "except the single ```json fence."
    )


def _queries(ticker: str, t: dict) -> list[str]:
    """Searches that ground a dependency map. TWO, not one: who a company buys
    FROM and who it sells TO are different questions that rarely share a page."""
    name = t.get("name") or ticker
    return [
        f"{name} ({ticker}) key suppliers and supply chain partners",
        f"{name} ({ticker}) largest customers and end markets revenue breakdown",
    ]


def _user(ticker: str, t: dict, web_ctx: str = "") -> str:
    name = t.get("name") or ticker
    sector = t.get("sub_sector") or t.get("sector") or ""
    return (
        f"Company: {name} ({ticker}){(' — ' + sector) if sector else ''}.\n\n"
        + (f"Web context — use ONLY this as evidence:\n{web_ctx}\n\n" if web_ctx else "")
        + "Research and return its dependency / ripple-risk map as JSON with this exact shape:\n"
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
        raise RuntimeError("AI generation is not configured")

    # PRE-SEARCH, because the server-side tool below never runs. The request
    # declares Anthropic's `web_search_20250305` tool, but anthropic_shim routes
    # to the free chain, which has no server-side tools — it logs "answering
    # without them" and drops it. The consequence was not just weaker grounding:
    # the source extractor reads `web_search_tool_result` blocks, which then
    # never exist, so every dependency map shipped with `sources: []` — an
    # AI-written supply chain presented with no evidence behind it.
    # Grounding it here with web_search.py fixes both halves at once.
    web_ctx, pre_sources = "", []
    try:
        import web_search
        if web_search.available():
            hits: list = []
            for q in _queries(ticker, t):
                hits += await web_search.search(q, count=6)
            web_ctx = web_search.as_context(hits, limit=10)
            seen_u = set()
            for h in hits[:10]:
                u = h.get("url")
                if u and u not in seen_u:
                    seen_u.add(u)
                    pre_sources.append({"n": len(pre_sources) + 1,
                                        "title": (h.get("title") or u)[:90], "url": u})
    except Exception as exc:
        logger.warning("dependencies_gen %s: pre-search failed: %s", ticker, exc)

    body = {
        "model": _MODEL,
        "max_tokens": 2600,
        "system": [
            {"type": "text", "text": _system(), "cache_control": {"type": "ephemeral"}}
        ],
        "tools": [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}
        ],
        "messages": [{"role": "user", "content": _user(ticker, t, web_ctx)}],
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await anthropic_shim.post(
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
            raise RuntimeError(_ai_error(r))
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
    # `sources` comes from web_search_tool_result blocks, which only exist on a
    # real Anthropic call. On the free chain it is always empty, so fall back to
    # the pre-search hits rather than publishing an unsourced map.
    out.update({"ticker": ticker, "sources": sources or pre_sources,
                "model": _MODEL, "status": "ready"})
    return out
