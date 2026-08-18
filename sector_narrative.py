"""
sector_narrative — one short, plain-English read of a sub-sector.

COST MODEL (the important part)
The stats underneath this change every five minutes. Keying the cache on them
directly would bust it on essentially every refresh and turn a one-off cost
into a permanent one — on a free, anonymously-reachable page, which is the
worst possible place to leak spend. So the key is a COARSE BUCKET of the
figures: medians rounded to 5, breadth to 10, momentum to 10. Ordinary drift
maps to the same bucket and hits cache forever; only a genuine change in the
sector's character crosses a boundary and pays for one regeneration.

GROUNDING
The model is given the computed numbers and told to explain them. It is never
asked to supply a figure, so it cannot invent one. No web search: the stats are
the story here, and search would multiply the cost for nothing.

COMPLIANCE
The prompt forbids buy/sell/hold directives, "you should", price targets,
predictions, and hype. It is asked to describe what the group looks like today
and what the spread implies about it — a characteristic, not a course of
action. Output is additionally swept for banned phrasing before it is cached,
because a prompt rule is a request and a filter is a guarantee.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

import httpx

import anthropic_shim
import usage_log
from kv_store import store as _kv

logger = logging.getLogger(__name__)

_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# A two-sentence descriptive paragraph is not a high-stakes generation, and it
# is cached per bucket rather than per view, so the cheap tier is right here.
_MODEL = (os.environ.get("ANTHROPIC_SECTOR_MODEL") or "claude-haiku-4-5-20251001").strip()
_NS = "sector_note_v1"

_SYSTEM = (
    "You write one short, factual paragraph describing a stock sub-sector for "
    "TickerMover, a UK research site.\n"
    "You are given computed statistics for the group and the same statistics "
    "for the whole scored universe. Explain what the group looks like RIGHT NOW "
    "and what the numbers imply about its character.\n"
    "RULES:\n"
    "- Use ONLY the figures provided. Never invent a number, company, event or date.\n"
    "- 2 to 3 sentences, maximum 60 words. Plain English. No markdown, no emoji, no lists.\n"
    "- Describe the GROUP, not individual companies, and never single one out as preferable.\n"
    "- The score spread is the most interesting fact: a narrow spread means the names "
    "move as a block, a wide one means the individual name matters more than the theme. "
    "Say which it is.\n"
    "- Generic research for a UK audience. NOT advice and NOT a personal recommendation: "
    "never tell the reader to buy, sell, hold, avoid, rotate into or out of anything; never "
    "use 'you should'; never give a price target; never predict a direction or name a "
    "timeframe; never promise or imply returns; no hype ('poised to', 'set to soar').\n"
    "- Describe what IS, never what WILL BE.\n"
    "- Output ONLY the paragraph text."
)

# Cheap, deterministic backstop. A prompt rule is a request; this is the check.
_BANNED = re.compile(
    r"\b(you should|should buy|should sell|we recommend|recommend(?:ing)? (?:buy|sell)|"
    r"buy now|sell now|time to buy|worth buying|avoid this|rotate into|rotate out|"
    r"price target|will (?:rise|fall|soar|surge|outperform)|poised to|set to soar|"
    r"guaranteed)\b",
    re.I,
)


def available() -> bool:
    return bool(_KEY)


def _bucket(s: dict) -> str:
    """Coarse signature of a sector's character. See the cost note at the top."""
    def r(v, step):
        if v is None:
            return "na"
        return str(int(round(float(v) / step) * step))
    return "-".join([
        r(s.get("count"), 5),
        r(s.get("alpha_median"), 5),
        r(s.get("alpha_spread"), 5),
        r(s.get("breadth_strong_pct"), 10),
        r(s.get("momentum_3m_median"), 10),
    ])


def _facts(s: dict, base: dict) -> str:
    def line(label, v, bv, suffix=""):
        if v is None:
            return None
        b = "" if bv is None else f" (whole universe: {bv}{suffix})"
        return f"- {label}: {v}{suffix}{b}"
    rows = [
        f"Sub-sector: {s.get('label')}",
        line("Companies scored", s.get("count"), base.get("count")),
        line("Median Quant Score (0-100)", s.get("alpha_median"), base.get("alpha_median")),
        line("Score spread, 75th minus 25th percentile", s.get("alpha_spread"), base.get("alpha_spread")),
        line("Share scoring 65 or above", s.get("breadth_strong_pct"), base.get("breadth_strong_pct"), "%"),
        line("Median 3-month price move", s.get("momentum_3m_median"), base.get("momentum_3m_median"), "%"),
        line("Median P/E", s.get("pe_median"), base.get("pe_median"), "x"),
        line("Median revenue growth", s.get("growth_median"), base.get("growth_median"), "%"),
        line("Median net margin", s.get("margin_median"), base.get("margin_median"), "%"),
    ]
    return "\n".join(x for x in rows if x)


async def _call(s: dict, base: dict) -> Optional[str]:
    body = {
        "model": _MODEL,
        "max_tokens": 300,
        "system": _SYSTEM,
        "messages": [{
            "role": "user",
            "content": ("Statistics:\n" + _facts(s, base)
                        + "\n\nWrite the paragraph."),
        }],
    }
    async with httpx.AsyncClient(timeout=45):
        r = await anthropic_shim.post(
            headers={"x-api-key": _KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Anthropic {r.status_code}: {r.text[:200]}")
    data = r.json()
    usage_log.record("sector_note", _MODEL, data.get("usage") or {})
    txt = "".join(b.get("text", "") for b in data.get("content", [])
                  if b.get("type") == "text").strip()
    if not txt:
        return None
    txt = re.sub(r"\s+", " ", txt).strip().strip('"')
    if _BANNED.search(txt):
        # Do not attempt a rewrite: a second paid call to fix a compliance miss
        # is the wrong trade. Drop it and show the deterministic stats alone,
        # which are the substance anyway.
        logger.warning("sector_note %s rejected by compliance filter: %s",
                       s.get("slug"), txt[:120])
        return None
    return txt


async def generate(s: dict, base: dict, force: bool = False) -> dict:
    """Return {slug, note, status}. Durably cached per (slug, stats-bucket)."""
    slug = s.get("slug") or ""
    if not slug:
        return {"slug": slug, "note": None, "status": "unavailable"}
    key = f"{slug}:{_bucket(s)}"

    if not force:
        cached = _kv.get(_NS, key)
        if cached and cached.get("note"):
            return {"slug": slug, "note": cached["note"], "status": "ready"}
    if not available():
        return {"slug": slug, "note": None, "status": "unavailable"}

    try:
        note = await _call(s, base)
    except Exception as exc:
        logger.error("sector_note %s failed: %s", slug, exc)
        return {"slug": slug, "note": None, "status": "error"}
    if not note:
        return {"slug": slug, "note": None, "status": "unavailable"}
    _kv.set(_NS, key, {"note": note, "model": _MODEL})
    return {"slug": slug, "note": note, "status": "ready"}


def cached_note(s: dict) -> Optional[str]:
    """Synchronous cache peek — for server-rendered pages, which must never
    block on a model call or pay for one during a page render."""
    slug = s.get("slug") or ""
    if not slug:
        return None
    try:
        hit = _kv.get(_NS, f"{slug}:{_bucket(s)}")
    except Exception:
        return None
    return (hit or {}).get("note")
