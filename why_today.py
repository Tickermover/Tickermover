"""
AlphaHunt — "Why we bought it today" narrative for Top Hunts tracker picks.

A single punchy, plain-English sentence explaining what the quantitative scan
flagged when a stock was added to the tracker — grounded ENTIRELY in our own
factor profile (Alpha Score, grade, the six pillars, analyst upside) so the
model never invents live figures. Uses the cheap Haiku tier and is cached
durably (app_kv) keyed by ticker, so each pick is generated once and reused
forever (until the pick is replaced). ~one Haiku call per pick, ever.

Compliance: the text is OBSERVATIONAL (what the scan saw), not advice. No
literal buy/sell, no invented price targets.

Returns: {ticker, why, model, status}
"""
from __future__ import annotations

import logging
import os

import httpx

from kv_store import store as _kv

logger = logging.getLogger(__name__)

_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Top Hunts is the best-of-the-best shortlist, so the selection rationale gets
# the premium Opus tier (not Haiku). Bounded + cached: one Opus call per pick,
# ever (durable app_kv), so cost stays ~one-time per name.
_MODEL = (
    os.environ.get("ANTHROPIC_WHY_MODEL")
    or "claude-opus-4-8"
).strip()
# v2 namespace: abandons the older Haiku-generated cache so every pick
# regenerates once on the upgraded Opus prompt.
_NS = "why_today_v2"

_PILLAR_LABELS = {
    "momentum": "Momentum",
    "growth": "Growth",
    "quality": "Quality",
    "valuation": "Valuation",
    "sentiment": "Sentiment",
    "growth_potential": "Outlook",
}

_SYSTEM = (
    "You are the lead analyst voice of AlphaHunt's 'Top Hunts' — our shortlist of "
    "the strongest stock setups our quantitative scan surfaces right now (the "
    "best-of-the-best, screened for the highest-conviction near-term opportunities). "
    "For ONE stock, write 1–2 sentences (max ~44 words) explaining WHY it earned a "
    "place on this list today, for a retail investor skimming a card.\n"
    "RULES:\n"
    "- Ground every claim in the FACTOR PROFILE provided. Do NOT invent prices, "
    "targets, dates, or figures not given.\n"
    "- Lead with the single strongest edge — what sets this name apart from the "
    "rest of the universe (the standout pillar(s), the catalyst, the quality).\n"
    "- Confident, premium, plain English. No hype words ('skyrocket', 'explode'); "
    "no emoji; no markdown.\n"
    "- This is a research signal, NOT advice: describe what the scan sees. Never "
    "promise or imply returns, never name a timeframe for gains, and never write "
    "'buy', 'sell', 'must own', or a price target.\n"
    "- No preamble, no quotes. Output only the sentence(s)."
)


def available() -> bool:
    return bool(_KEY)


def _profile_block(t: dict) -> str:
    name = t.get("name") or t.get("ticker")
    sub = t.get("sub_sector") or t.get("sector") or ""
    score = t.get("smart_score")
    if score is None:
        score = t.get("pop_score")
    if score is None:
        score = t.get("current_pop") or t.get("pop_at_entry")
    grade = t.get("grade") or t.get("current_grade") or t.get("grade_at_entry") or ""
    tier = {1: "Premium (top tier)", 2: "Strong", 3: "Eligible"}.get(t.get("entry_tier"), "")
    pillars = t.get("pillars") or {}
    lines = [
        f"Company: {name}" + (f" — {sub}" if sub else ""),
        f"Alpha Score: {round(float(score)) if score is not None else 'n/a'} / 100"
        + (f" (Grade {grade})" if grade else ""),
    ]
    if tier:
        lines.append(f"Entry tier: {tier}")
    if pillars:
        pr = ", ".join(
            f"{_PILLAR_LABELS.get(k, k)} {round(float(v))}"
            for k, v in pillars.items() if v is not None
        )
        lines.append(f"Six pillars (0–100): {pr}")
    up = t.get("_target_upside")
    if up is not None:
        lines.append(f"Analyst consensus upside: {up}")
    rat = (t.get("rationale") or "").strip()
    if rat:
        lines.append(f"Curated thesis tag: {rat}")
    sigs = t.get("signals") or []
    if sigs:
        lines.append("Scan signals: " + "; ".join(str(s) for s in sigs[:3]))
    return "\n".join(lines)


async def _call(ticker: str, t: dict) -> str:
    body = {
        "model": _MODEL,
        "max_tokens": 220,
        "system": [
            {"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{
            "role": "user",
            "content": (
                f"FACTOR PROFILE for {ticker}:\n{_profile_block(t)}\n\n"
                "Write the one-sentence reason now."
            ),
        }],
    }
    async with httpx.AsyncClient(timeout=40) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": _KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Anthropic {r.status_code}: {r.text[:200]}")
        data = r.json()
    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    # Strip any stray wrapping quotes the model may add.
    text = text.strip().strip('"').strip("'").strip()
    if not text:
        raise RuntimeError("Empty why-today response")
    return text


async def generate(ticker: str, t: dict | None, force: bool = False) -> dict:
    """Return {ticker, why, model, status}. Cached durably by ticker; only
    (re)generated when missing or `force`. Degrades to status='unavailable'
    when no API key, so callers can fall back to the static rationale."""
    sym = (ticker or "").upper()
    t = t or {"ticker": sym}
    if not force:
        cached = _kv.get(_NS, sym)
        if cached and cached.get("why"):
            return {"ticker": sym, "why": cached["why"],
                    "model": cached.get("model", ""), "status": "ready"}
    if not available():
        return {"ticker": sym, "why": "", "status": "unavailable"}
    try:
        why = await _call(sym, t)
    except Exception as exc:
        logger.error(f"why_today {sym} failed: {exc}")
        prev = _kv.get(_NS, sym)
        if prev and prev.get("why"):
            return {"ticker": sym, "why": prev["why"], "status": "ready"}
        return {"ticker": sym, "why": "", "status": "error", "detail": str(exc)[:200]}
    out = {"why": why, "model": _MODEL}
    _kv.set(_NS, sym, out)
    logger.info("why_today %s (%s): %d chars", sym, _MODEL, len(why))
    return {"ticker": sym, "why": why, "model": _MODEL, "status": "ready"}
