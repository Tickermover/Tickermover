"""
chain_roster — which capex theme does each scored stock actually touch.

THE PROBLEM IT SOLVES
`data/theses.json` carries ten hand-curated chain maps, but only ~105 of the
545 scored names appear in one. Open any other stock and the Capex exposure
card says "No major capex theme" — which is true of a grocer and plainly wrong
of, say, a switchgear maker that simply never got hand-placed. The maps looked
like ten small islands rather than a map of the market.

WHY THIS IS A SEPARATE FIELD, NOT MORE NAMES IN `layers[].names`
The curated layer rosters drive the share-of-landed-value maths, and that model
is deliberately small and verified: every name added dilutes every existing
share, and the exposure-band weighting is known to overstate diversified names
(the reason `theme_rev` overrides exist). Pouring several hundred
machine-classified tickers into it would quietly corrupt the one number the
chain maps are trusted for.

So this produces a second, clearly-named field — `roster` — that answers a
DIFFERENT question:

    layers[].names  →  "whose revenue do we COUNT in this chain's split"   (curated)
    roster          →  "which stocks TOUCH this chain, and how strongly"   (classified)

Both live in theses.json so there is still one source of truth per thesis, and
the share endpoint continues to read only the curated field.

HOW IT CLASSIFIES
It does not search. `business_role` already holds a web-grounded line for every
scored name — what it sells and who pays it — and that, plus the sub-sector, is
a better classification input than a fresh search would be, and free. Haiku
sorts them in batches against the FIXED list of live theses, and "none" is an
expected, common answer: most of the universe is banks, insurers, retailers and
REITs that sit in no capex chain, and saying so is the honest result.
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
_MODEL = (os.environ.get("ANTHROPIC_ROSTER_MODEL") or "claude-haiku-4-5-20251001").strip()
_TIMEOUT = float(os.environ.get("ROSTER_TIMEOUT", "120"))

KV_NS = "chain_roster_v1"
MAX_AGE_S = 60 * 86400
BATCH = 25

EXPOSURE = {"hi", "mid", "lo"}

_SYSTEM = (
    "You place US-listed companies into capital-spending value chains for a UK "
    "research site.\n"
    "You are given a fixed list of chains and a batch of companies, each with "
    "what it sells and who pays it. For EACH company choose the ONE chain whose "
    "spending most directly reaches it, or \"none\".\n"
    "\"none\" is the expected answer for most companies — banks, insurers, "
    "retailers, restaurants, property, media, travel, staples and healthcare "
    "providers do not sit in a capital-spending chain. Do not stretch to place "
    "them; a wrong placement is far worse than none.\n"
    "Exposure band:\n"
    "  hi  — the chain's spending is the company's main source of revenue\n"
    "  mid — a material segment, but the company earns most of its money elsewhere\n"
    "  lo  — a real but minor exposure\n"
    "Output ONLY one strict JSON object inside a single ```json fence."
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


def _chain_block(theses: list[dict]) -> str:
    out = []
    for t in theses:
        layers = ", ".join((l.get("name") or "") for l in (t.get("layers") or []))
        out.append(f"- {t['slug']}: {t.get('title','')} | layers: {layers}")
    return "\n".join(out)


def _extract(text: str):
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.I)
    blob = m.group(1) if m else None
    if blob is None:
        m2 = re.search(r"\{.*\}", text, re.DOTALL)
        blob = m2.group(0) if m2 else None
    try:
        return json.loads(blob) if blob else None
    except Exception:
        return None


async def classify(batch: list[dict], theses: list[dict]) -> dict:
    """Classify one batch. Returns {TICKER: {slug, exposure, why}} — only for
    companies actually placed; "none" answers are simply absent."""
    if not available():
        raise RuntimeError("ANTHROPIC_API_KEY not configured")
    slugs = {t["slug"] for t in theses}

    lines = []
    for c in batch:
        bits = [c.get("sub_sector") or c.get("sector") or "", c.get("role") or "",
                ("buyers: " + c["buyers"]) if c.get("buyers") else ""]
        lines.append(f"{c['ticker']} — {c.get('name','')} — "
                     + " — ".join(x for x in bits if x))

    user = (
        "CHAINS:\n" + _chain_block(theses) + "\n\n"
        "COMPANIES:\n" + "\n".join(lines) + "\n\n"
        'Return {"placements": {"<TICKER>": {"chain": "<slug or none>", '
        '"exposure": "hi|mid|lo", "why": "<<=12 words, why this chain\'s spend reaches it>"}}}\n'
        "One entry per company above. Use the exact slugs given. Use \"none\" freely."
    )

    body = {
        "model": _MODEL, "max_tokens": 3000,
        "system": [{"type": "text", "text": _SYSTEM,
                    "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": user}],
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT):
        r = await anthropic_shim.post(
            headers={"x-api-key": _KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body)
    if r.status_code >= 400:
        logger.error("chain_roster: HTTP %s %s", r.status_code, r.text[:200])
        raise RuntimeError(_ai_error(r))
    data = r.json()
    usage_log.record("chain_roster", _MODEL, data.get("usage") or {})

    parsed = _extract("".join(b.get("text", "") for b in (data.get("content") or [])
                              if b.get("type") == "text"))
    placements = (parsed or {}).get("placements") or {}
    valid = {t["ticker"] for t in batch}
    out: dict = {}
    for tk, p in placements.items():
        tk = str(tk).upper().strip()
        if tk not in valid or not isinstance(p, dict):
            continue
        slug = str(p.get("chain") or "").strip().lower()
        if slug in ("none", "", "null") or slug not in slugs:
            continue                       # unplaced is a legitimate result
        exp = str(p.get("exposure") or "").strip().lower()
        if exp not in EXPOSURE:
            exp = "lo"
        why = re.sub(r"\s+", " ", str(p.get("why") or "")).strip()[:90]
        out[tk] = {"chain": slug, "exposure": exp, "why": why}
    return out
