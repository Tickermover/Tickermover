"""
bottom_line_ai.py — Haiku-polished, quant-grounded bottom_line prose.

The deterministic `_build_bottom_line()` in ai_scorer.py turns each stock's
quant drivers (grade, score, revenue growth, momentum, EPS beats, caveats)
into one factual sentence — accurate, but formulaic and repetitive across the
540+ name universe. This module rewrites that sentence into natural, varied
prose with cheap Haiku, grounded STRICTLY in the deterministic text: the model
may only rephrase what it's given — it cannot add a number, a fact, or a
buy/sell recommendation.

Cost control (mirrors the other durable AI caches):
  • Cache-first in the durable app_kv store (kv_store), keyed by ticker.
  • The entry stores a hash of the *template* sentence. Haiku only re-runs when
    that sentence changes (drivers moved) OR the entry is older than 15 days.
  • A process-local mirror (_MEM) lets apply_cached() overlay the live snapshot
    after every 5-minute re-score with ZERO network I/O.
  • Any failure (no API key, API error, missing Supabase table) → the template
    stays. The AI layer is a pure enhancement; the deterministic text is always
    present, so a page never renders blank.

The output lands in a separate field, `bottom_line_ai`; renderers prefer it and
fall back to `bottom_line`. We never overwrite `bottom_line` itself, so the
template hash stays stable and re-scoring can't trigger needless regeneration.

Generation NEVER runs on the synchronous scoring loop — it happens only in the
_bottom_line_prewarm background task in app.py.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Optional

import httpx

import kv_store
import usage_log

logger = logging.getLogger(__name__)

_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# DROPPED 2026-06-22: the AI rewrite was the single biggest Haiku line (whole
# universe ×545). The deterministic `bottom_line` template is always present and
# reads fine on its own, so the polish layer is off by default. Flip
# BOTTOM_LINE_AI_ENABLED=1 to bring it back (no other change needed).
_ENABLED = os.environ.get("BOTTOM_LINE_AI_ENABLED", "0").strip().lower() in ("1", "true", "yes", "on")
# Cheapest current tier — this is a pure rephrasing task, no reasoning needed.
_MODEL = (
    os.environ.get("ANTHROPIC_BOTTOM_LINE_MODEL")
    or "claude-haiku-4-5-20251001"
).strip()
_TIMEOUT = float(os.environ.get("BOTTOM_LINE_TIMEOUT", "30"))

_NS = "bottom_line_ai"
_TTL_S = float(os.environ.get("BOTTOM_LINE_TTL_S", str(15 * 86400)))  # 15 days

# ticker -> (template_hash, prose). In-memory mirror of the durable cache so the
# snapshot overlay after each re-score costs nothing.
_MEM: dict[str, tuple[str, str]] = {}


def available() -> bool:
    return bool(_KEY) and _ENABLED


def _hash(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def _sig(t: dict) -> str:
    """STABLE cache signature for a stock's bottom-line prose.

    COST FIX (2026-06-20): the cache used to key on _hash(template_text), but the
    template embeds live numbers (momentum %, score) that drift every day — so the
    key changed daily for most of the universe and Haiku re-billed the whole book
    every day (the '15-day TTL' never got to hit). Key instead on a coarse,
    *material* signature: grade, caution set, a 5-point score bucket, and growth
    tier. A 14%→15% momentum wiggle no longer invalidates; a grade change or a real
    score move does. Cuts steady-state regeneration ~10x back to baseline."""
    g = (t.get("grade") or "")
    cr = t.get("caution_reasons") or []
    crk = "|".join(sorted(str(c[0] if isinstance(c, (list, tuple)) else c) for c in cr))
    try:
        score = float(t.get("smart_score") if t.get("smart_score") is not None
                      else (t.get("pop_score") or 0))
    except (TypeError, ValueError):
        score = 0.0
    sbucket = int(score // 5)
    gt = str(t.get("growth_tier") or "")
    return _hash(f"{g}|{crk}|{sbucket}|{gt}")


# ── Static instruction block (prompt-cached across the batch) ──────────
def _system() -> str:
    return (
        "You rewrite a single-sentence stock research note into natural, "
        "varied prose. Strict rules:\n"
        "- Use ONLY the facts in the note. Never add a number, ratio, date, "
        "price, or claim that isn't already there.\n"
        "- Keep every figure and every caveat exactly as given.\n"
        "- This is generic research/commentary for a UK audience, NOT financial "
        "advice and NOT a personal recommendation. Never instruct the reader to "
        "buy, sell, hold, accumulate, or trade, never use those words as "
        "directives, never say 'you should', and never give a price target. Stay "
        "descriptive and observational.\n"
        "- 1–2 sentences, at most ~40 words. Plain English, no hype (no 'will "
        "soar', 'guaranteed'), no promised returns, no emoji.\n"
        "- Vary sentence structure so 500 of these don't read identically.\n"
        "Return ONLY the rewritten note — no preamble, no quotes."
    )


def _user(ticker: str, name: str, sector: str, template: str) -> str:
    ctx = f"Ticker: {ticker}"
    if name and name != ticker:
        ctx += f" ({name})"
    if sector:
        ctx += f"  ·  Sector: {sector}"
    return f"{ctx}\n\nNote to rewrite:\n{template}"


async def _polish(ticker: str, template: str, t: dict) -> Optional[str]:
    """One Haiku call. Returns rewritten prose, or None on any failure."""
    if not _KEY:
        return None
    name = (t.get("name") or ticker)
    sector = (t.get("sub_sector") or t.get("subsector") or t.get("sector") or "")
    body = {
        "model": _MODEL,
        "max_tokens": 160,
        "system": [
            {"type": "text", "text": _system(),
             "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": _user(ticker, name, sector, template)}],
    }
    try:
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
                logger.error(f"bottom_line_ai {ticker} → {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning(f"bottom_line_ai {ticker} call failed: {exc}")
        return None

    try:
        usage_log.record("bottom_line", _MODEL, data.get("usage") or {}, ticker=ticker)
    except Exception:
        pass

    parts = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    prose = "".join(parts).strip().strip('"').strip()
    # Guard: if the model returns something empty or absurdly long, drop it.
    if not prose or len(prose) > 320:
        return None
    return prose


def apply_cached(universe: list[dict]) -> int:
    """Pure in-memory overlay — no network. Sets `bottom_line_ai` on each stock
    whose cached prose still matches its current template sentence. Safe to call
    after every re-score; stale entries are cleared so the template shows."""
    if not _ENABLED:
        return 0
    n = 0
    for t in universe or []:
        sym = (t.get("ticker") or "").upper()
        tpl = t.get("bottom_line") or ""
        if not sym or not tpl:
            continue
        hit = _MEM.get(sym)
        if hit and hit[0] == _sig(t):
            t["bottom_line_ai"] = hit[1]
            n += 1
        else:
            t.pop("bottom_line_ai", None)
    return n


async def prewarm(universe: list[dict], throttle_s: float = 2.0,
                  max_gen: Optional[int] = None) -> dict:
    """Cache-first batch. Fills _MEM + the durable KV cache and overlays the
    snapshot. Only stocks whose template changed or whose cache is >15 days old
    actually call Haiku; everything else is a free cache hit. `max_gen` caps the
    number of live generations per cycle so cost/latency never spikes."""
    if not _ENABLED:
        return {"generated": 0, "cache_hits": 0, "model": _MODEL, "disabled": True}
    gen = hits = 0
    import asyncio
    for t in list(universe or []):
        sym = (t.get("ticker") or "").upper()
        tpl = t.get("bottom_line") or ""
        if not sym or not tpl:
            continue
        h = _sig(t)
        mem = _MEM.get(sym)
        if mem and mem[0] == h:
            t["bottom_line_ai"] = mem[1]
            hits += 1
            continue
        cached = kv_store.store.get(_NS, sym, max_age_s=_TTL_S)
        if isinstance(cached, dict) and cached.get("h") == h and cached.get("prose"):
            _MEM[sym] = (h, cached["prose"])
            t["bottom_line_ai"] = cached["prose"]
            hits += 1
            continue
        if not _KEY:
            continue
        if max_gen is not None and gen >= max_gen:
            continue
        prose = await _polish(sym, tpl, t)
        if prose:
            kv_store.store.set(_NS, sym, {"h": h, "prose": prose, "model": _MODEL})
            _MEM[sym] = (h, prose)
            t["bottom_line_ai"] = prose
            gen += 1
            await asyncio.sleep(throttle_s)   # gentle pacing — yield to live requests
    return {"generated": gen, "cache_hits": hits, "model": _MODEL}
