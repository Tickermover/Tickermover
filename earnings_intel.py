"""
AlphaHunt — Earnings Intelligence Layer

Two async functions that call Anthropic's Haiku model to convert raw
earnings-event text into structured signals the UI can render:

    extract_guidance_from_press_release(text)
        Reads an earnings press release. Returns:
            {
              "tone":              "raised" | "maintained" | "lowered" | "none",
              "revenue_guidance":  short string ("$1.2B–$1.3B (+15-20% YoY)"),
              "eps_guidance":      short string ("$0.42–$0.48"),
              "key_metrics":       [3 short bullet strings, optional],
              "summary":           one-sentence plain-English takeaway,
            }

    analyze_call_transcript(text)
        Reads an earnings-call transcript. Returns:
            {
              "sentiment_score":     float in [-1.0, +1.0],
              "sentiment_label":     "Very bullish" | "Bullish" | "Neutral" |
                                     "Cautious" | "Bearish",
              "positives":           [up to 3 verbatim-ish quotes],
              "concerns":            [up to 3 verbatim-ish quotes],
              "qa_summary":          one-sentence summary of analyst Q&A tone,
            }

Both fall back to {} when:
    - ANTHROPIC_API_KEY isn't configured (free / local dev)
    - The HTTP call fails or times out
    - The model returns malformed JSON

That way the UI just hides the relevant section instead of crashing.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Re-use the same env vars as intelligence.py so all LLM calls share config.
_ANTHROPIC_KEY     = os.environ.get("ANTHROPIC_API_KEY", "").strip()
_ANTHROPIC_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
_ANTHROPIC_TIMEOUT = 25.0  # transcripts are larger than thesis prompts

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False


# ── Token-budget guards ──────────────────────────────────────────────
# Press releases are usually 2–8 KB. Transcripts are 30–80 KB.
# We trim aggressively before sending to the LLM to keep costs predictable.
_MAX_PRESS_RELEASE_CHARS = 12_000
_MAX_TRANSCRIPT_CHARS    = 30_000


def _truncate(text: str, limit: int) -> str:
    """Truncate text to approximately `limit` characters, breaking on a word."""
    if not text or len(text) <= limit:
        return text or ""
    cut = text[:limit]
    # Break at the last whitespace so we don't bisect a word.
    last_space = cut.rfind(" ")
    if last_space > limit * 0.9:
        cut = cut[:last_space]
    return cut + " […truncated]"


# ── Anthropic call helper (shared between guidance + sentiment) ──────
async def _claude_json(prompt: str, max_tokens: int = 700) -> Optional[dict]:
    """
    Send `prompt` to Claude and parse the response as JSON.
    Returns a dict on success, None on any failure (caller falls back to {}).
    """
    if not _ANTHROPIC_KEY or not _HTTPX_AVAILABLE:
        return None

    try:
        async with httpx.AsyncClient(timeout=_ANTHROPIC_TIMEOUT) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         _ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      _ANTHROPIC_MODEL,
                    "max_tokens": max_tokens,
                    "messages":   [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning(f"Anthropic call failed: {exc}")
        return None

    # Concatenate text blocks
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    text = text.strip()
    if not text:
        return None

    # The model usually returns clean JSON, but sometimes wraps it in
    # markdown code fences or precedes it with prose. Extract the first
    # {...} block defensively.
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        logger.warning(f"No JSON block found in LLM response: {text[:200]}")
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        logger.warning(f"JSON parse failed: {exc} — raw: {text[:200]}")
        return None


# ── Public: forward guidance extraction ──────────────────────────────
async def extract_guidance_from_press_release(text: str) -> dict:
    """
    Extract forward guidance from an earnings press release.
    Returns {} on any failure or when no guidance is present.
    """
    if not text or len(text) < 200:
        return {}

    trimmed = _truncate(text, _MAX_PRESS_RELEASE_CHARS)

    prompt = (
        "You are an equity research analyst reading a public earnings "
        "press release. Extract ONLY the forward-looking guidance (i.e., "
        "what the company says about NEXT quarter or NEXT fiscal year). "
        "Ignore the trailing-quarter results — those are reported elsewhere.\n\n"
        "Return EXACTLY this JSON shape (no prose, no markdown):\n"
        "{\n"
        '  "tone":              "raised" | "maintained" | "lowered" | "none",\n'
        '  "revenue_guidance":  "<short string e.g. $1.2B–$1.3B (+15-20% YoY)>" or "",\n'
        '  "eps_guidance":      "<short string e.g. $0.42–$0.48>" or "",\n'
        '  "key_metrics":       ["<bullet>", "<bullet>"],\n'
        '  "summary":           "<one plain-English sentence about the guidance>"\n'
        "}\n\n"
        'Use "tone": "none" and empty strings/lists if no forward guidance is given.\n'
        '"raised" means guidance is HIGHER than the prior outlook; '
        '"lowered" means LOWER; "maintained" means roughly the same.\n'
        "Be conservative — if you cannot tell, use \"maintained\".\n\n"
        "─── Press release ───\n"
        f"{trimmed}\n"
        "─── End ───"
    )

    result = await _claude_json(prompt, max_tokens=600)
    if not result:
        return {}

    # Schema-validate / coerce
    tone = str(result.get("tone", "none")).lower().strip()
    if tone not in ("raised", "maintained", "lowered", "none"):
        tone = "none"
    return {
        "tone":             tone,
        "revenue_guidance": str(result.get("revenue_guidance", "") or "").strip(),
        "eps_guidance":     str(result.get("eps_guidance", "") or "").strip(),
        "key_metrics":      [str(b).strip() for b in (result.get("key_metrics") or []) if b][:3],
        "summary":          str(result.get("summary", "") or "").strip(),
    }


# ── Public: conference-call sentiment ────────────────────────────────
async def analyze_call_transcript(text: str) -> dict:
    """
    Score and summarize an earnings call transcript.
    Returns {} on any failure.
    """
    if not text or len(text) < 1000:
        return {}

    trimmed = _truncate(text, _MAX_TRANSCRIPT_CHARS)

    prompt = (
        "You are an equity research analyst reading a quarterly earnings "
        "call transcript. Produce a tight, structured summary.\n\n"
        "Return EXACTLY this JSON shape (no prose, no markdown):\n"
        "{\n"
        '  "sentiment_score":  <float between -1.0 and +1.0>,\n'
        '  "sentiment_label":  "Very bullish" | "Bullish" | "Neutral" | "Cautious" | "Bearish",\n'
        '  "positives":        ["<verbatim or near-verbatim quote>", ...],\n'
        '  "concerns":         ["<verbatim or near-verbatim quote>", ...],\n'
        '  "qa_summary":       "<one sentence on overall analyst Q&A tone>"\n'
        "}\n\n"
        "Rules:\n"
        '- "sentiment_score": +1.0 = strongly bullish, 0 = neutral, -1.0 = strongly bearish.\n'
        '- "positives" + "concerns": up to 3 each. Pull short, specific quotes (≤25 words).\n'
        '  Skip generic boilerplate ("we had a strong quarter").\n'
        '- "qa_summary": describe whether analysts pushed back, were satisfied, or asked\n'
        '  pointed questions about specific risks. One sentence.\n'
        "- If transcript is too short or unparseable, return all empty values "
        'and sentiment_label "Neutral".\n\n'
        "─── Transcript ───\n"
        f"{trimmed}\n"
        "─── End ───"
    )

    result = await _claude_json(prompt, max_tokens=900)
    if not result:
        return {}

    # Coerce sentiment_score to float in valid range
    try:
        score = float(result.get("sentiment_score", 0.0))
        score = max(-1.0, min(1.0, score))
    except (TypeError, ValueError):
        score = 0.0

    label = str(result.get("sentiment_label", "Neutral"))
    if label not in ("Very bullish", "Bullish", "Neutral", "Cautious", "Bearish"):
        label = "Neutral"

    return {
        "sentiment_score": round(score, 2),
        "sentiment_label": label,
        "positives":       [str(q).strip() for q in (result.get("positives") or []) if q][:3],
        "concerns":        [str(q).strip() for q in (result.get("concerns") or []) if q][:3],
        "qa_summary":      str(result.get("qa_summary", "") or "").strip(),
    }
