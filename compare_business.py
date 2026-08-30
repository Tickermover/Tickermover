"""
compare_business — the QUALITATIVE half of a head-to-head.

WHY IT EXISTS
`/api/compare/{A}-vs-{B}` answers "where do these two differ, and by how much"
with pure arithmetic: P/E, margins, momentum, analyst upside. What it could not
answer is the question a reader actually starts with — *what are these two
companies, and why are their numbers shaped so differently?* Micron at an 80%
gross margin and Western Digital at 45% is not a quality gap, it is DRAM
pricing versus disciplined HDD supply, and the table alone cannot say so.

So this adds six descriptive rows per pair:

    sells    — what each earns from
    engine   — where the margin actually comes from
    moat     — why a competitor cannot simply copy it
    capital  — how capital-hungry the model is
    risk     — the main structural risk
    rivals   — who each competes against

WHAT IT DELIBERATELY DOES NOT DO — read this before extending it
There is **no winner column, no verdict, no "better"**. That is not an
oversight and not a limitation of the model: `sector_intel.compare` had a
verdict once ("X edges out Y…") and it was removed because a tally over
unweighted, correlated metrics reads exactly like a personal recommendation.
This module carries the same rule into prose, where it is far easier to breach:
"superior", "stronger choice", "best suited for" are all verdicts wearing a
descriptive coat. The prompt forbids them and a regex sweep enforces it, because
a prompt rule is a request and a filter is a guarantee. A note that trips the
filter is DROPPED, never rewritten — a second paid call to launder a compliance
miss is the wrong trade, and the measured rows stand perfectly well alone.

COST
Grounded on the free search path (`web_search.py` → Serper/Brave) with Haiku
doing the extraction, ~$0.003 a pair, cached 90 days on the sorted pair key.
Generation is gated to signed-in users by the caller for the same reason sector
notes are: a public, crawlable page must never be able to trigger paid work.
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
_MODEL = (os.environ.get("ANTHROPIC_COMPARE_BIZ_MODEL")
          or "claude-haiku-4-5-20251001").strip()
_TIMEOUT = float(os.environ.get("COMPARE_BIZ_TIMEOUT", "90"))

KV_NS = "compare_business_v1"
MAX_AGE_S = 90 * 86400

# The six rows, in the order they are shown. Kept here so the prompt, the
# coercion and the renderer cannot drift apart.
ROWS = [
    ("sells",   "What it sells"),
    ("engine",  "Where the margin comes from"),
    ("moat",    "What protects it"),
    ("capital", "How capital-hungry it is"),
    ("risk",    "The main structural risk"),
    ("rivals",  "Who it competes with"),
]

DISCLAIMER = ("Descriptive comparison of two business models, compiled from "
              "public sources. Not advice, not a recommendation, and not a "
              "view on which is the better holding. Capital at risk.")

_SYSTEM = (
    "You describe how two listed companies differ AS BUSINESSES, for a UK "
    "research site. You are given web search results for both.\n"
    "Your job is to explain WHY their financials look different — what each "
    "sells, where its margin comes from, what protects it, how much capital it "
    "needs, what could structurally go wrong, and who it competes with.\n"
    "ABSOLUTE RULES — a breach means the whole answer is discarded:\n"
    "- NEVER name a winner, a preference, or a better/worse. No 'superior', "
    "'stronger', 'best', 'the better choice', 'outperforms', 'best suited for', "
    "'we prefer', no verdict and no summary of which to own.\n"
    "- NEVER tell the reader to buy, sell, hold, avoid or allocate, and never "
    "say what suits which kind of investor.\n"
    "- No price targets, no predictions, no timeframes, no promised returns, "
    "no hype ('poised to', 'set to soar').\n"
    "- Describe what IS. Symmetry matters: say something substantive about "
    "BOTH companies in every row, never praise one and criticise the other.\n"
    "- Use only what the context supports. Never invent a figure, product or "
    "customer.\n"
    "Output ONLY one strict JSON object inside a single ```json fence."
)

# Deterministic backstop. Ordered roughly by how often a model reaches for them.
_BANNED = re.compile(
    r"\b(superior|stronger choice|the better|better choice|best choice|best "
    r"suited|winner|wins|outperforms?|we prefer|preferable|recommend\w*|should "
    r"buy|should sell|should own|worth buying|worth owning|avoid|the safer bet|"
    r"more attractive|less attractive|undervalued|overvalued|top pick|verdict|"
    r"poised to|set to soar|guaranteed|will (?:rise|fall|beat|surge))\b", re.I)


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


def key_for(a: str, b: str) -> str:
    """Sorted pair key, so A-vs-B and B-vs-A share one cache entry."""
    x, y = sorted([(a or "").upper().strip(), (b or "").upper().strip()])
    return f"{x}-VS-{y}"


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


def _clean(v: object, limit: int = 150) -> str:
    s = re.sub(r"\s+", " ", str(v or "")).strip().strip('"')
    if len(s) <= limit:
        return s
    cut = s[:limit]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,;:") + "…"


def _user(a: str, an: str, b: str, bn: str, ctx: str) -> str:
    keys = "\n".join(f'    "{k}": {{"a": "<{an}>", "b": "<{bn}>"}},' for k, _ in ROWS)
    return (
        f"Company A: {an} ({a})\nCompany B: {bn} ({b})\n\n"
        f"Web context:\n{ctx}\n\n"
        "Return exactly this JSON:\n"
        "{\n"
        '  "frame": "<one sentence on the structural difference between the two>",\n'
        '  "rows": {\n' + keys + "\n  }\n}\n\n"
        "Rules:\n"
        "- Every row needs BOTH an `a` and a `b`. One clause each, 8-20 words, "
        "no trailing full stop. Concrete and specific — name the product, the "
        "customer, the technology, the competitor.\n"
        "- `frame`: at most 30 words, naming the axis on which they differ "
        "(e.g. one sells compute bandwidth, the other sells storage capacity). "
        "It must NOT say which position is preferable.\n"
        "- If the context does not support a row for a company, write "
        "\"Not established from public sources\" rather than guessing.\n"
        "Respond with ONLY the JSON."
    )


async def generate(a: str, b: str, a_name: str = "", b_name: str = "") -> dict:
    """Generate one pair's business comparison. Raises on failure."""
    a, b = (a or "").upper().strip(), (b or "").upper().strip()
    an, bn = (a_name or a), (b_name or b)
    if not available():
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    import web_search
    if not web_search.available():
        # Ungrounded, the model will produce confident, plausible prose about a
        # business it has half-remembered. For a card that sits next to audited
        # figures that is worse than showing nothing.
        raise RuntimeError("web search not configured")

    hits: list = []
    for nm, tk in ((an, a), (bn, b)):
        hits += await web_search.search(
            f"{nm} ({tk}) business model products margins competitors moat", count=5)
    ctx = web_search.as_context(hits, limit=10)
    if not ctx:
        raise RuntimeError("no web context")

    body = {
        "model": _MODEL,
        "max_tokens": 1400,
        "system": [{"type": "text", "text": _SYSTEM,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": _user(a, an, b, bn, ctx)}],
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT):
        r = await anthropic_shim.post(
            headers={"x-api-key": _KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body,
        )
    if r.status_code >= 400:
        logger.error("compare_business: HTTP %s %s", r.status_code, r.text[:300])
        raise RuntimeError(_ai_error(r))
    data = r.json()
    usage_log.record("compare_business", _MODEL, data.get("usage") or {})

    parsed = _extract_json("".join(
        blk.get("text", "") for blk in (data.get("content") or [])
        if blk.get("type") == "text"))
    if not isinstance(parsed, dict):
        raise RuntimeError("Unparseable comparison response")

    src = parsed.get("rows") if isinstance(parsed.get("rows"), dict) else parsed
    rows = []
    for k, label in ROWS:
        cell = src.get(k) if isinstance(src, dict) else None
        if not isinstance(cell, dict):
            continue
        av, bv = _clean(cell.get("a")), _clean(cell.get("b"))
        if not av and not bv:
            continue
        rows.append({"key": k, "label": label, "a": av, "b": bv})
    frame = _clean(parsed.get("frame"), 220)
    if len(rows) < 3:
        raise RuntimeError(f"Too few usable rows ({len(rows)})")

    blob = " ".join([frame] + [r["a"] + " " + r["b"] for r in rows])
    hit = _BANNED.search(blob)
    if hit:
        # Dropped, never rewritten — see the module docstring.
        logger.warning("compare_business %s-vs-%s rejected by compliance filter: %r",
                       a, b, hit.group(0))
        raise RuntimeError(f"Rejected by compliance filter: {hit.group(0)}")

    seen, sources = set(), []
    for h in hits[:10]:
        u = h.get("url")
        if u and u not in seen:
            seen.add(u)
            sources.append({"n": len(sources) + 1,
                            "title": (h.get("title") or u)[:90], "url": u})

    return {"a": a, "b": b, "frame": frame, "rows": rows,
            "sources": sources[:6], "model": _MODEL,
            "disclaimer": DISCLAIMER, "status": "ready"}
