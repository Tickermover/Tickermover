"""
TickerMover - Earnings Intelligence Layer

Two functions that turn raw earnings data into UI-ready signals:

    extract_guidance_from_press_release(text)  [async, LLM]
        Reads a press release via Groq Llama 3.3 70B. Returns:
            {tone, revenue_guidance, eps_guidance, key_metrics, summary}

    compute_earnings_reaction(ticker_dict)  [sync, deterministic]
        Computes a 0-to-1 reaction score from the data already in our
        ticker dict — no API call needed, no LLM, no rate limits, works
        for every stock in the universe. Returns:
            {score, label, components: [{name, value, points, weight}]}

Why no transcript-based sentiment? Transcripts aren't available free at
scale (200+ tickers). Reaction Score gives a deterministic, explainable,
backtestable alternative using inputs we already have.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────────
# Groq config (forward-guidance LLM call)
# ────────────────────────────────────────────────────────────────────
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
    """Send a chat completion to Groq with response_format=json_object."""
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
        return None
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None


# ────────────────────────────────────────────────────────────────────
# Public: Forward Guidance (Groq + SEC EDGAR press release)
# ────────────────────────────────────────────────────────────────────
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


# ────────────────────────────────────────────────────────────────────
# Public: Combined Press-Release Analysis (guidance + earnings highlights)
# ────────────────────────────────────────────────────────────────────
async def analyze_press_release_full(text: str) -> dict:
    """
    Extract BOTH forward guidance AND earnings-highlights sentiment from a
    single press release in one Groq call. Free (no paid transcript needed).

    Returns:
        {
          "guidance":  {tone, revenue_guidance, eps_guidance, key_metrics, summary},
          "sentiment": {sentiment_score, sentiment_label, positives, concerns, qa_summary},
        }

    Either sub-dict is empty when the press release doesn't contain that info.
    """
    if not text or len(text) < 200:
        return {"guidance": {}, "sentiment": {}}

    trimmed = _truncate(text, _MAX_PRESS_RELEASE_CHARS)

    system = (
        "You are an equity research analyst. You read public earnings press "
        "releases and extract both forward guidance and qualitative highlights "
        "as structured JSON. You return ONLY valid JSON - no prose, no markdown."
    )
    user = (
        "Analyse this earnings press release. Extract TWO things:\n"
        "1. Forward-looking guidance (next quarter / next year numbers).\n"
        "2. Earnings highlights — positive themes, concerns, and the analyst-Q&A-equivalent summary.\n\n"
        "Return JSON in EXACTLY this shape:\n"
        "{\n"
        '  "guidance": {\n'
        '    "tone":              "raised" | "maintained" | "lowered" | "none",\n'
        '    "revenue_guidance":  "<short string e.g. $1.2B-$1.3B (+15-20% YoY)>" or "",\n'
        '    "eps_guidance":      "<short string e.g. $0.42-$0.48>" or "",\n'
        '    "key_metrics":       ["<bullet>", "<bullet>"],\n'
        '    "summary":           "<one sentence>"\n'
        '  },\n'
        '  "sentiment": {\n'
        '    "sentiment_score":  <float -1.0..+1.0>,\n'
        '    "sentiment_label":  "Very bullish" | "Bullish" | "Neutral" | "Cautious" | "Bearish",\n'
        '    "positives":  ["<verbatim or near-verbatim quote, <=25 words>", ...],\n'
        '    "concerns":   ["<verbatim or near-verbatim quote, <=25 words>", ...],\n'
        '    "qa_summary": "<one-sentence read-through, e.g. \\"Management framing is forward-looking and constructive on AI capex\\"">"\n'
        '  }\n'
        "}\n\n"
        "Rules:\n"
        '- Up to 3 positives + 3 concerns. Pull SHORT, SPECIFIC quotes from CEO/CFO commentary.\n'
        '  Skip generic boilerplate ("we had a strong quarter") and forward-looking-statement boilerplate.\n'
        '- For "concerns", look at risk factors, headwind language, FX/macro mentions, segment weakness.\n'
        '- "qa_summary": describe management\'s overall framing and the kind of questions analysts would likely focus on.\n'
        '- "tone" rules: "raised"=guidance higher than prior; "lowered"=lower; "maintained"=roughly same; "none"=no guidance given.\n'
        '- If a section has no extractable info, return its values empty (sentiment_label "Neutral", lists [], strings "").\n\n'
        "--- Press release ---\n"
        f"{trimmed}\n"
        "--- End ---"
    )

    result = await _groq_json(system, user, max_tokens=1100)
    if not result:
        return {"guidance": {}, "sentiment": {}}

    g_raw = result.get("guidance") or {}
    s_raw = result.get("sentiment") or {}

    g_tone = str(g_raw.get("tone", "none")).lower().strip()
    if g_tone not in ("raised", "maintained", "lowered", "none"):
        g_tone = "none"
    guidance = {
        "tone":             g_tone,
        "revenue_guidance": str(g_raw.get("revenue_guidance", "") or "").strip(),
        "eps_guidance":     str(g_raw.get("eps_guidance", "") or "").strip(),
        "key_metrics":      [str(b).strip() for b in (g_raw.get("key_metrics") or []) if b][:3],
        "summary":          str(g_raw.get("summary", "") or "").strip(),
    }

    try:
        score = float(s_raw.get("sentiment_score", 0.0))
        score = max(-1.0, min(1.0, score))
    except (TypeError, ValueError):
        score = 0.0
    label = str(s_raw.get("sentiment_label", "Neutral"))
    if label not in ("Very bullish", "Bullish", "Neutral", "Cautious", "Bearish"):
        label = "Neutral"
    sentiment = {
        "sentiment_score": round(score, 2),
        "sentiment_label": label,
        "positives":       [str(q).strip() for q in (s_raw.get("positives") or []) if q][:3],
        "concerns":        [str(q).strip() for q in (s_raw.get("concerns") or []) if q][:3],
        "qa_summary":      str(s_raw.get("qa_summary", "") or "").strip(),
    }

    return {"guidance": guidance, "sentiment": sentiment}


# ────────────────────────────────────────────────────────────────────
# Public: Call Sentiment (Groq + transcript text from API Ninjas Premium)
# ────────────────────────────────────────────────────────────────────
_MAX_TRANSCRIPT_CHARS = 30_000


async def analyze_call_transcript(text: str) -> dict:
    """Score and summarize an earnings call transcript via Groq."""
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
        '  "qa_summary":       "<one sentence summarizing analyst Q&A tone>"\n'
        "}\n\n"
        "Rules:\n"
        '- "sentiment_score": +1.0 = strongly bullish, 0 = neutral, -1.0 = strongly bearish.\n'
        '- "positives" + "concerns": up to 3 each, short specific quotes (<=25 words each).\n'
        "  Skip generic boilerplate.\n"
        '- "qa_summary": describe Q&A tone in one sentence (e.g. "8 analyst questions, mostly forward-looking on AI capex").\n'
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


# ────────────────────────────────────────────────────────────────────
# Public: Earnings Reaction Score (deterministic, no API)
# ────────────────────────────────────────────────────────────────────
def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def compute_earnings_reaction(t: dict, guidance_tone: str = "") -> dict:
    """
    Compute a deterministic 0-to-1 earnings reaction score from the data
    already in our ticker dict. No API call, no LLM. Works for any stock
    that has eps_quarters in its payload.

    Returns:
        {
          "score":  float in [-1.0, +1.0],
          "label":  "Very Strong" | "Strong" | "Positive" | "Mixed" | "Weak" | "Disappointing",
          "components": [
            {"name": "EPS Surprise",      "value": "+24.0%",     "points": 0.40, "weight": "high"},
            {"name": "Revenue Growth",    "value": "+18.4% YoY", "points": 0.18, "weight": "med"},
            {"name": "Forward Guidance",  "value": "Raised",     "points": 0.30, "weight": "high"},
            {"name": "Stock Reaction",    "value": "+8.2%",      "points": 0.10, "weight": "low"},
            {"name": "Beat Streak",       "value": "4/4",        "points": 0.05, "weight": "bonus"},
          ],
        }

    Returns {} if there's not enough data (no eps_quarters at all).
    """
    eps_q = t.get("eps_quarters") or []
    if not eps_q or not isinstance(eps_q[0], dict):
        return {}

    components = []
    score = 0.0

    # ── 1. EPS Surprise (weight: high, max ±0.40) ──
    surprise_pct = eps_q[0].get("surprise_pct")
    if surprise_pct is not None:
        # Saturate at ±20% surprise → ±0.40 points
        pts = _clamp(surprise_pct * 0.02, -0.40, 0.40)
        score += pts
        components.append({
            "name":   "EPS Surprise",
            "value":  f"{surprise_pct:+.1f}%",
            "points": round(pts, 2),
            "weight": "high",
        })

    # ── 2. Revenue Growth YoY (weight: medium, max ±0.20) ──
    rev_g = t.get("rev_growth_qyoy")
    if rev_g is not None:
        # Saturate at ±40% YoY growth → ±0.20 points
        pts = _clamp(rev_g * 0.5, -0.20, 0.20)
        score += pts
        components.append({
            "name":   "Revenue Growth",
            "value":  f"{rev_g*100:+.1f}% YoY",
            "points": round(pts, 2),
            "weight": "med",
        })

    # ── 3. Forward Guidance (weight: high, ±0.30) ──
    if guidance_tone in ("raised", "maintained", "lowered"):
        pts = {"raised": 0.30, "maintained": 0.0, "lowered": -0.30}[guidance_tone]
        score += pts
        label = {"raised": "Raised", "maintained": "Maintained", "lowered": "Lowered"}[guidance_tone]
        components.append({
            "name":   "Forward Guidance",
            "value":  label,
            "points": pts,
            "weight": "high",
        })

    # ── 4. Stock Reaction (1M momentum proxy, weight: low, max ±0.10) ──
    momentum = t.get("momentum_1m")
    if momentum is not None:
        # Saturate at ±20% one-month move → ±0.10 points
        pts = _clamp(momentum * 0.005, -0.10, 0.10)
        score += pts
        components.append({
            "name":   "Stock Reaction (1M)",
            "value":  f"{momentum:+.1f}%",
            "points": round(pts, 2),
            "weight": "low",
        })

    # ── 5. Beat Streak (bonus, max ±0.05) ──
    streak = t.get("eps_beat_streak")
    if streak is not None:
        if streak >= 4:
            pts, val = 0.05, "4/4 last quarters"
        elif streak == 3:
            pts, val = 0.02, "3/4 last quarters"
        elif streak == 2:
            pts, val = 0.0, "2/4 last quarters"
        else:
            pts, val = -0.02, f"{streak}/4 last quarters"
        score += pts
        components.append({
            "name":   "Beat Streak",
            "value":  val,
            "points": pts,
            "weight": "bonus",
        })

    score = round(_clamp(score, -1.0, 1.0), 2)

    if   score >=  0.60: label = "Very Strong"
    elif score >=  0.30: label = "Strong"
    elif score >=  0.10: label = "Positive"
    elif score > -0.10:  label = "Mixed"
    elif score > -0.30:  label = "Weak"
    else:                label = "Disappointing"

    return {
        "score":      score,
        "label":      label,
        "components": components,
    }
