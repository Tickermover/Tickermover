"""
TickerMover — AI head-to-head comparison-card generator.

Produces a compact, structured set of qualitative comparison metrics for a
single ticker (operational stage, latest revenue, contract backlog, core
execution risk, path to profitability), web-grounded via the Anthropic
Messages API `web_search` tool. Two tickers' cards are rendered side-by-side
in the stock sheet's Peers tab as a head-to-head.

The ticker's own live data is passed in as ground truth so the model never
contradicts our price / market-cap / growth figures.

Returns:  {ticker, card:{...}, sources:[{n,title,url}], model, status}
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

_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Reuse the research model knob — same class of grounded task.
_MODEL = (
    os.environ.get("ANTHROPIC_RESEARCH_MODEL")
    or os.environ.get("ANTHROPIC_MODEL")
    or "claude-haiku-4-5-20251001"
).strip()
_TIMEOUT = float(os.environ.get("RESEARCH_TIMEOUT", "60"))

# (json_key, display_label) — display labels are also used by the frontend, but
# kept here as the single source of truth for which fields a card carries.
FIELDS = [
    ("operational_stage", "Operational stage"),
    ("revenue_latest", "Latest revenue"),
    ("contract_backlog", "Total contract backlog"),
    ("execution_risk", "Core execution risk"),
    ("path_to_profitability", "Path to profitability"),
]


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


def _ground_block(t: dict) -> str:
    g = lambda k: t.get(k)
    fields = [
        ("Company", g("name")),
        ("Sector", g("sector") or (t.get("meta") or {}).get("sector")),
        ("Price", g("price")),
        ("Market cap", g("market_cap")),
        ("Revenue growth YoY %", g("revenue_growth_yoy")),
        ("Profit margin %", g("profit_margin")),
    ]
    return "\n".join(f"- {k}: {v}" for k, v in fields if v not in (None, "", []))


# Static (cacheable) instruction block — identical for every ticker.
def _compare_system() -> str:
    return (
        "You are an equity-research analyst building a head-to-head comparison "
        "card for the company given in the next message.\n\n"
        "TASK: Use web search to find the most recent facts, then fill these "
        "five fields. Each value is a SHORT phrase (max ~12 words), specific "
        "and comparison-ready:\n"
        "- operational_stage: where the company sits in its lifecycle (e.g. "
        "'Highly established, scaled operator' or 'Pre-commercialization build "
        "phase').\n"
        "- revenue_latest: most recent quarterly revenue with YoY growth if "
        "known (e.g. '$200.3M (+63.5% YoY)').\n"
        "- contract_backlog: total contract / order backlog with a figure if "
        "known (e.g. '$2.2B').\n"
        "- execution_risk: the single biggest execution risk right now (e.g. "
        "'Scaling the upcoming Neutron rocket').\n"
        "- path_to_profitability: when / how it reaches sustained net profit "
        "(e.g. 'Anticipated net profits by 2027').\n\n"
        "Respond with ONLY a single minified JSON object with exactly these "
        "keys: operational_stage, revenue_latest, contract_backlog, "
        "execution_risk, path_to_profitability. No markdown, no prose, no code "
        "fences. Use null for any field you genuinely cannot determine."
    )


def _compare_user(ticker: str, t: dict) -> str:
    """Per-ticker, NON-cached portion."""
    return (
        f"Company: {ticker} ({t.get('name','')}).\n\n"
        "GROUND TRUTH — our own data; do not contradict these figures:\n"
        f"{_ground_block(t)}\n\n"
        "Build the comparison card now."
    )


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of the model's final text, tolerating code
    fences or stray prose around it."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


async def generate_compare_card(ticker: str, ticker_data: dict | None) -> dict:
    """Generate a structured comparison card. Raises on hard failure so the
    caller can drop the in-flight flag without persisting a bad card."""
    ticker = ticker.upper()
    t = ticker_data or {"ticker": ticker}
    if not available():
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    body = {
        "model": _MODEL,
        "max_tokens": 1200,
        # Cache the static instruction block across the web_search tool loop and
        # back-to-back card generations.
        "system": [
            {"type": "text", "text": _compare_system(),
             "cache_control": {"type": "ephemeral"}}
        ],
        "tools": [
            # Reverted from web_search_20260209 (dynamic filtering): its code-
            # filtering step risked the same timeout that broke research_gen.
            # Stable 20250305; cost bounded via max_uses + per-ticker caching.
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}
        ],
        "messages": [{"role": "user", "content": _compare_user(ticker, t)}],
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
            logger.error(f"compare_gen {ticker} → {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
        data = r.json()

    _u = data.get("usage") or {}
    usage_log.record("compare", _MODEL, _u, ticker=ticker)
    logger.info("compare_gen %s (%s): in=%s cache_write=%s cache_read=%s out=%s",
                ticker, _MODEL, _u.get("input_tokens"),
                _u.get("cache_creation_input_tokens"), _u.get("cache_read_input_tokens"),
                _u.get("output_tokens"))

    text_parts: list[str] = []
    sources: list[dict] = []
    seen: set[str] = set()
    for block in data.get("content", []):
        bt = block.get("type")
        if bt == "text":
            text_parts.append(block.get("text", ""))
        elif bt == "web_search_tool_result":
            for res in block.get("content", []) or []:
                url = res.get("url")
                if url and url not in seen:
                    seen.add(url)
                    sources.append({
                        "n": len(sources) + 1,
                        "title": res.get("title") or url,
                        "url": url,
                    })

    raw = "".join(text_parts).strip()
    parsed = _extract_json(raw)

    card: dict[str, str | None] = {}
    for key, _label in FIELDS:
        v = parsed.get(key)
        if v in (None, "", "null", "N/A", "n/a", "unknown", "Unknown"):
            card[key] = None
        else:
            card[key] = str(v).strip()

    if not any(card.values()):
        raise RuntimeError("Empty comparison card")

    return {
        "ticker": ticker,
        "card": card,
        "sources": sources,
        "model": _MODEL,
        "status": "ready",
    }
