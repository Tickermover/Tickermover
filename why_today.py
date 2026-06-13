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

import json
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
# v3 namespace: structured 3-reason web-grounded report (was a single
# sentence) — busts the older cache so every pick regenerates once.
_NS = "why_today_v3"

_PILLAR_LABELS = {
    "momentum": "Momentum",
    "growth": "Growth",
    "quality": "Quality",
    "valuation": "Valuation",
    "sentiment": "Sentiment",
    "growth_potential": "Outlook",
}

_SYSTEM = (
    "You are the lead analyst of AlphaHunt's 'Top Hunts' — our shortlist of the "
    "strongest stock setups our quantitative scan surfaces right now. For ONE "
    "stock, explain WHY it earned a place on this best-of-the-best list today, as "
    "THREE distinct, tagged reasons for a retail investor. Use web search to "
    "ground the MACRO and CATALYST reasons in current, real information.\n"
    "Return ONLY a JSON object (no prose, no code fences) with exactly these "
    "three string keys:\n"
    "{\n"
    '  "macro":    "1 sentence — the sector / industry tailwind this name rides right now (what is happening in its space, demand cycle, spending trend).",\n'
    '  "quant":    "1 sentence — the single strongest signal from OUR FACTOR PROFILE, citing the actual numbers we give (the Alpha Score and the standout pillar(s)).",\n'
    '  "catalyst": "1 sentence — the most compelling company-specific driver you can VERIFY via search: a recent earnings beat, guidance raise, product / customer / order win, partnership, or analyst upgrade. Prefer a specific, recent, real event; if none is verifiable, state the strongest structural demand driver instead."\n'
    "}\n"
    "RULES:\n"
    "- macro + catalyst: ground in web-search results (recent, real). quant: ground STRICTLY in the numbers we provide.\n"
    "- Do NOT invent figures, customers, dates, or events. If you cannot verify a catalyst, do not fabricate one — fall back to the structural driver.\n"
    "- Each value is ONE tight sentence (max ~28 words): confident, plain English, no hype words, no emoji, no markdown.\n"
    "- Research signal, NOT advice: never promise or imply returns, never name a timeframe for gains, never write 'buy', 'sell', 'must own', or a price target.\n"
    "- Output ONLY the JSON object."
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


_KEYS = ("macro", "quant", "catalyst")


def _parse_points(content: list) -> dict:
    """Pull the JSON object out of the model's content blocks. The text AFTER
    the last web-search result is the actual answer (earlier text is the
    model narrating its searches)."""
    last_search = -1
    for i, b in enumerate(content):
        if b.get("type") == "web_search_tool_result":
            last_search = i
    text = "".join(b.get("text", "") for b in content[last_search + 1:]
                   if b.get("type") == "text").strip()
    if not text:
        text = "".join(b.get("text", "") for b in content
                       if b.get("type") == "text").strip()
    # Tolerate ```json fences / stray prose around the object.
    i0, i1 = text.find("{"), text.rfind("}")
    if i0 < 0 or i1 <= i0:
        raise RuntimeError("No JSON object in why response")
    obj = json.loads(text[i0:i1 + 1])
    points = {k: str(obj.get(k, "") or "").strip()[:300] for k in _KEYS}
    if not any(points.values()):
        raise RuntimeError("Empty why points")
    return points


async def _call(ticker: str, t: dict) -> dict:
    body = {
        "model": _MODEL,
        "max_tokens": 900,
        "system": [
            {"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}
        ],
        # Web search grounds the macro + catalyst reasons in real, recent info.
        # Capped to keep per-pick cost bounded (each search ~$0.01); cached
        # durably so this is a one-time cost per name.
        "tools": [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
        ],
        "messages": [{
            "role": "user",
            "content": (
                f"OUR FACTOR PROFILE for {ticker}:\n{_profile_block(t)}\n\n"
                "Search the web for the current sector backdrop and any recent "
                "company catalyst, then return the JSON object with macro, quant "
                "and catalyst reasons."
            ),
        }],
    }
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": _KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"Anthropic {r.status_code}: {r.text[:200]}")
        data = r.json()
    _u = data.get("usage") or {}
    logger.info("why_today %s (%s): in=%s out=%s", ticker, _MODEL,
                _u.get("input_tokens"), _u.get("output_tokens"))
    return _parse_points(data.get("content", []))


async def generate(ticker: str, t: dict | None, force: bool = False) -> dict:
    """Return {ticker, points:{macro,quant,catalyst}, model, status}. Cached
    durably by ticker; only (re)generated when missing or `force`. Degrades to
    status='unavailable' when no API key so callers can hide the block."""
    sym = (ticker or "").upper()
    t = t or {"ticker": sym}
    if not force:
        cached = _kv.get(_NS, sym)
        if cached and cached.get("points"):
            return {"ticker": sym, "points": cached["points"],
                    "model": cached.get("model", ""), "status": "ready"}
    if not available():
        return {"ticker": sym, "points": None, "status": "unavailable"}
    try:
        points = await _call(sym, t)
    except Exception as exc:
        logger.error(f"why_today {sym} failed: {exc}")
        prev = _kv.get(_NS, sym)
        if prev and prev.get("points"):
            return {"ticker": sym, "points": prev["points"], "status": "ready"}
        return {"ticker": sym, "points": None, "status": "error", "detail": str(exc)[:200]}
    out = {"points": points, "model": _MODEL}
    _kv.set(_NS, sym, out)
    return {"ticker": sym, "points": points, "model": _MODEL, "status": "ready"}
