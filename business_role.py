"""
TickerMover — "Where it earns": one plain-English read of what a company
actually sells and who pays it, for EVERY scored stock.

WHY IT EXISTS
The overview's third measured card used to be "Chain position", filled only
from the hand-mapped AI-capex theses. Two problems: the label meant nothing to
a reader who had never opened a thesis map, and ~9 out of 10 stocks are in no
mapped chain at all, so the card simply vanished for them. This module supplies
the same idea for the whole universe, in words a first-time reader gets:

    role   — the niche it earns from, e.g. "Optical interconnect"
    buyers — who actually pays for it, e.g. "AI data-centre builders"

Two fields, deliberately. The card is a one-glance orientation, not a business
summary; anything longer belongs in the business overview below it.

COST MODEL — why this uses the FREE search path
What a company sells is a STRUCTURAL fact, so this is generated once per ticker
and cached 180 days. The whole universe still has to be covered though, and
that is what rules out Anthropic's server-side `web_search` tool here: at $0.01
a search plus Sonnet input, one pass over ~545 names is about $22 — seven times
the $3 daily cap, and 44% of the monthly one, for a card that is two words long.
So this grounds on `web_search.py` (Serper/Brave, already configured, free) and
spends only Haiku tokens on the extraction: ~$0.002 a ticker, ~$1.30 for the
universe. Cheap enough that the background prewarm can simply run.

The read path serves cache only, so this can never become a per-visitor cost.

GROUNDING
One free search per ticker, snippets passed in as context. The model is told to
use ONLY that context plus the company name, so an ungrounded guess is a visible
prompt violation rather than the default behaviour. If search is unconfigured or
returns nothing, generation is skipped entirely and the caller falls back to the
sub-sector — a wrong niche is worse than a vague one.

COMPLIANCE
Descriptive only: what the business IS, never what it will do or be worth. The
prompt forbids advice phrasing and the output is swept for it before caching,
because a prompt rule is a request and a filter is a guarantee.
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx

import anthropic_shim
import usage_log

logger = logging.getLogger(__name__)

_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Haiku on purpose: the job is a two-field extraction from supplied snippets,
# which is exactly what the cheap tier is good at, and the universe-wide budget
# above is what actually decides this.
_MODEL = (os.environ.get("ANTHROPIC_ROLE_MODEL") or "claude-haiku-4-5-20251001").strip()
_TIMEOUT = float(os.environ.get("ROLE_TIMEOUT", "60"))

# Namespace + max age shared with app.py, so the reader and the writer cannot
# drift apart on where this lives or how long it lives for.
# v2: v1 capped role at 28 chars and buyers at 34, which clipped honest answers
# ("Semiconductor inspection too", "Enterprise servers and stora"). Bumping the
# namespace discards those rather than serving them for 180 days; regenerating
# the few hundred already written costs well under a dollar.
KV_NS = "business_role_v2"
MAX_AGE_S = 180 * 86400
ROLE_MAX, BUYERS_MAX = 34, 38

_SYSTEM = (
    "You describe, in a few plain words, WHAT a listed company sells and WHO "
    "pays it — the kind of orientation a first-time reader needs before looking "
    "at anything else on the page.\n"
    "You are given web search results about the company. Base your answer on "
    "them and on what the company is plainly known for. Never invent a business "
    "line you cannot see in the context.\n"
    "This is generic research for a UK audience. It is NOT financial advice and "
    "NOT a personal recommendation: describe the business as it IS, never what "
    "it will do, never whether it is a good holding, no hype, no returns, no "
    "outlook, no 'you should'.\n"
    "Output ONLY one strict JSON object inside a single ```json fence."
)


def queries(ticker: str, t: dict) -> list[str]:
    """ONE search per ticker. Serper/Brave credits — not model tokens — are the
    scarce resource once this runs over the whole universe (~545 searches for a
    full pass), and a single "what it sells / who buys it" query returns
    snippets that answer both fields."""
    name = (t or {}).get("name") or ticker
    return [f"{name} ({ticker}) what the company does main products and who its customers are"]


def _user(ticker: str, t: dict, web_ctx: str) -> str:
    name = (t or {}).get("name") or ticker
    sector = (t or {}).get("sub_sector") or (t or {}).get("sector") or ""
    return (
        f"Company: {name} ({ticker}){(' — ' + sector) if sector else ''}.\n\n"
        f"Web context:\n{web_ctx}\n\n"
        "Return exactly this JSON:\n"
        "{\n"
        '  "role":   "<the specific niche it earns from>",\n'
        '  "buyers": "<who actually pays for it>"\n'
        "}\n\n"
        "Rules:\n"
        "- role: 1-4 words, maximum 30 characters. As SPECIFIC as the truth allows — "
        "\"Optical interconnect\", \"Discount groceries\", \"Contract drug manufacturing\". "
        "Never a sector name like \"Technology\" or \"Healthcare\", and never the company's own "
        "marketing slogan. A complete short phrase — never one that trails off.\n"
        "- buyers: 2-5 words, maximum 34 characters, naming the customer that provides most of "
        "the revenue — \"AI data-centre builders\", \"US households\", \"Hospitals and insurers\", "
        "\"Oil majors\". Not \"customers\" or \"businesses\".\n"
        "- If the company earns from several things, describe the LARGEST by revenue.\n"
        "- Plain English. No jargon a non-specialist would have to look up. Sentence case, "
        "no trailing full stop.\n"
        "Respond with ONLY the JSON."
    )

# Deterministic backstop against advice/hype creeping into a two-word field.
_BANNED = re.compile(
    r"\b(buy|sell|hold|undervalued|overvalued|bargain|opportunity|poised|"
    r"set to|growth story|winner|best-in-class|must-own|target)\b", re.I)

# Sector words the card must never end up showing: they say nothing the sector
# chip above the card has not already said, which is what the "role" field is
# specifically there to improve on.
_TOO_GENERIC = {
    "technology", "healthcare", "financials", "energy", "industrials",
    "utilities", "materials", "consumer", "real estate", "communication services",
    "software", "services", "products", "manufacturing", "retail", "banking",
}


def available() -> bool:
    return bool(_KEY)


def _extract_json(text: str) -> object:
    text = (text or "").strip()
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


def _clean_field(v: object, limit: int) -> str:
    """Normalise and cap a field, cutting on a WORD boundary.

    A hard slice produced "Optical Components / Photoni" live — a truncated
    word reads as a bug, and on a two-word card there is nowhere for it to
    hide. Cutting back to the last space (and dropping a dangling separator)
    costs a word and looks deliberate.
    """
    s = re.sub(r"\s+", " ", str(v or "")).strip().strip('"').rstrip(".")
    if len(s) <= limit:
        return s
    cut = s[:limit]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    cut = cut.rstrip(" ,;/&-")
    # "Refined fuels and" is a sentence that stopped, not a label. Drop a
    # dangling conjunction so the cut reads as a name instead of a truncation.
    return re.sub(r"\s+(and|or|&|with|for|plus)$", "", cut, flags=re.I).rstrip(" ,;/&-")


def fallback(t: dict | None) -> dict | None:
    """Deterministic floor, used when AI is unavailable, over budget, or has
    not run for this ticker yet.

    Worth having rather than hiding the card: the sub-sector is a weaker answer
    than "Optical interconnect", but it is a TRUE one and it is present for
    every name in the universe — which is the whole point of this card.
    """
    sub = _clean_field((t or {}).get("sub_sector") or (t or {}).get("sector"), ROLE_MAX)
    if not sub:
        return None
    return {"role": sub, "buyers": "", "source": "sector"}


async def generate(ticker: str, ticker_data: dict | None) -> dict:
    """Generate one role card. Raises on failure — the caller decides whether a
    failure is worth surfacing (it usually is not; the fallback covers it)."""
    sym = (ticker or "").upper().strip()
    if not available():
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    import web_search
    if not web_search.available():
        # Grounding is not optional for this card. Ungrounded, the model will
        # cheerfully name a plausible niche for a ticker it has confused with
        # another, and a confidently wrong two-word label is worse than the
        # sub-sector the caller already has.
        raise RuntimeError("web search not configured")
    hits: list = []
    for q in queries(sym, ticker_data or {}):
        hits += await web_search.search(q, count=8)
    web_ctx = web_search.as_context(hits, limit=8)
    if not web_ctx:
        raise RuntimeError("no web context")
    sources, seen = [], set()
    for h in hits[:8]:
        u = h.get("url")
        if u and u not in seen:
            seen.add(u)
            sources.append({"n": len(sources) + 1,
                            "title": (h.get("title") or u)[:90], "url": u})

    body = {
        "model": _MODEL,
        "max_tokens": 400,
        "system": [{"type": "text", "text": _SYSTEM,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user",
                      "content": _user(sym, ticker_data or {}, web_ctx)}],
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT):
        r = await anthropic_shim.post(
            headers={"x-api-key": _KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Anthropic {r.status_code}: {r.text[:300]}")
    data = r.json()
    usage_log.record("business_role", _MODEL, data.get("usage") or {}, ticker=sym)

    parts = [b.get("text", "") for b in (data.get("content") or [])
             if b.get("type") == "text"]
    parsed = _extract_json("".join(parts))
    if not isinstance(parsed, dict):
        raise RuntimeError("Unparseable role response")
    role = _clean_field(parsed.get("role"), ROLE_MAX)
    buyers = _clean_field(parsed.get("buyers"), BUYERS_MAX)
    if not role:
        raise RuntimeError("Empty role")
    if _BANNED.search(role) or _BANNED.search(buyers):
        logger.warning("business_role %s rejected by compliance filter: %r / %r",
                       sym, role, buyers)
        raise RuntimeError("Role rejected by compliance filter")
    if role.lower() in _TOO_GENERIC:
        # A sector word here is no better than the fallback and costs a call to
        # display, so let the fallback own that case instead of caching a dud.
        raise RuntimeError(f"Role too generic: {role}")

    return {"ticker": sym, "role": role, "buyers": buyers,
            "sources": sources[:4], "source": "ai", "model": _MODEL,
            "status": "ready"}
