"""
AlphaHunt - Earnings Intelligence Layer (Groq edition)

Two async functions that call Groq's hosted Llama 3.3 70B to convert raw
earnings-event text into structured signals the UI can render:

    extract_guidance_from_press_release(text)
        Returns: {tone, revenue_guidance, eps_guidance, key_metrics, summary}

    analyze_call_transcript(text)
        Returns: {sentiment_score, sentiment_label, positives, concerns, qa_summary}

Provider: Groq (https://console.groq.com)
- OpenAI-compatible chat completions API
- Free tier: ~30 req/min, ~14,400 req/day on llama-3.3-70b-versatile

Both functions fall back to {} when GROQ_API_KEY is missing or the call fails.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

_GROQ_KEY      = os.environ.get("GROQ_API_KEY", "").strip()
_GROQ_MODEL    = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
_GROQ_TIMEOUT  = 25.0

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

_MAX_PRESS_RELEASE_CHARS = 12_000
_MAX_TRANSCRIPT_CHARS    = 30_000


def _truncate(text: str, limit: int) -> str:
    if not text or len(text) <= limit:
        return text or ""
    cut = text[:limit]
    last_space = cut.rfind(" ")
    if last_space > limit * 0.9:
        cut = cut[:last_space]
    return cut + " [...truncated]"


async def _groq_json(system_prompt: str, user_prompt: str,
                     max_tokens: int = 700) -> Optional[dict]:
    """Send a chat completion request to Groq, parse JSON. Returns dict or None."""
    if not _GROQ_KEY or not _HTTPX_AVAILABLE:
        return None

    payload = {
        "model": _GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_prompt},
        ],
        "max_tokens":      max_tokens,
        "temperature":     0.2,
        "response_format": {"type": "json_object"},
    }

    try:
        async with httpx.AsyncClient(timeout=_GROQ_TIMEOUT) as c:
            r = await c.post(
                _GROQ_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {_GROQ_KEY}",
                    "Content-Type":  "application/json",
                },
                json=payload,
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning(f"Groq call failed: {exc}")
        return None

    try:
        text = data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError):
        logger.warning(f"Unexpected Groq response shape: {str(data)[:200]}")
        return None

    if not text:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            logger.warning(f"No JSON in Groq response: {text[:200]}")
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            logger.warning(f"JSON parse failed: {exc} - raw: {text[:200]}")
            return None


async def extract_guidance_from_press_release(text: str) -> dict:
    """Extract forward guidance from an earnings press release."""
    if not text or len(text) < 200:
        return {}

    trimmed = _truncate(text, _MAX_PRESS_RELEASE_CHARS)

    system = (
        "You are an equity research analyst. You read public earnings press "
        "releases and extract forward-looking guidance as structured JSON. "
        "You return ONLY valid JSON - no prose, no markdown."
    )

    user = (
        "Extract ONLY the forward-looking guidance from the press release "
        "below - what the company says about NEXT quarter or NEXT fiscal "
        "year. Ignore the trailing-quarter results.\n\n"
        "Return JSON in EXACTLY this shape:\n"
        "{\n"
        '  "tone":              "raised" | "maintained" | "lowered" | "none",\n'
        '  "revenue_guidance":  "<short string e.g. $1.2B-$1.3B (+15-20% YoY)>" or "",\n'
        '  "eps_guidance":      "<short string e.g. $0.42-$0.48>" or "",\n'
        '  "key_metrics":       ["<bullet>", "<bullet>"],\n'
        '  "summary":           "<one plain-English sentence>"\n'
        "}\n\n"
        'Use "tone": "none" and empty strings/lists if no forward guidance is given.\n'
        '"raised" = guidance HIGHER than the prior outlook;\n'
        '"lowered" = LOWER; "maintained" = roughly the same.\n'
        'If you cannot tell, use "maintained".\n\n'
        "--- Press release ---\n"
        f"{trimmed}\n"
        "--- End ---"
    )

    result = await _groq_json(system, user, max_tokens=600)
    if not result:
        return {}

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


async def analyze_call_transcript(text: str) -> dict:
    """Score and summarize an earnings call transcript."""
    if not text or len(text) < 1000:
        return {}

    trimmed = _truncate(text, _MAX_TRANSCRIPT_CHARS)

    system = (
        "You are an equity research analyst. You read quarterly earnings call "
        "transcripts and produce structured summaries as JSON. You return "
        "ONLY valid JSON - no prose, no markdown."
    )

    user = (
        "Summarize this earnings call transcript as structured JSON.\n\n"
        "Return JSON in EXACTLY this shape:\n"
        "{\n"
        '  "sentiment_score":  <float between -1.0 and +1.0>,\n'
        '  "sentiment_label":  "Very bullish" | "Bullish" | "Neutral" | "Cautious" | "Bearish",\n'
        '  "positives":        ["<verbatim or near-verbatim quote>", ...],\n'
        '  "concerns":         ["<verbatim or near-verbatim quote>", ...],\n'
        '  "qa_summary":       "<one sentence>"\n'
        "}\n\n"
        "Rules:\n"
        '- "sentiment_score": +1.0 = strongly bullish, 0 = neutral, -1.0 = strongly bearish.\n'
        '- "positives" + "concerns": up to 3 each. Pull short, specific quotes (<=25 words each).\n'
        "  Skip generic boilerplate.\n"
        '- "qa_summary": describe the analyst Q&A tone in one sentence.\n'
        "- If transcript is unparseable or too short, return all empty values\n"
        '  and sentiment_label "Neutral".\n\n'
        "--- Transcript ---\n"
        f"{trimmed}\n"
        "--- End ---"
    )

    result = await _groq_json(system, user, max_tokens=900)
    if not result:
        return {}

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
