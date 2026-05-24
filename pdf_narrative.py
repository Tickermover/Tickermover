"""
AlphaHunt — AI narrative builder for the tear-sheet PDF (v3).

Pulls together everything the PDF needs for its analyst-narrative page:
    - 3-bullet bull case + 3-bullet bear case + 1-sentence verdict
    - A short prose exec paragraph (3-4 sentences) that ties the metrics
      to the most recent filing
    - 3-5 forward catalysts (next earnings, product cycles, etc.)

Input sources combined:
    - The live ticker row (metrics, score, sector, beat streak, etc.)
    - The most recent event_summaries row (Quartr-style sections built
      from SEC EDGAR — see event_intel.py)

The output gets rendered as page 2 of the PDF tear sheet. Cached
in-process for 7 days per ticker so repeat downloads stay instant.

Cost: ~$0.003 / call (Haiku 4.5 — 8k input, 700 output).
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_ANTHROPIC_KEY     = os.environ.get("ANTHROPIC_API_KEY", "").strip()
_ANTHROPIC_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
_ANTHROPIC_TIMEOUT = 22.0

# In-process LRU cache. Keyed by ticker. Tier-2 cache is the underlying
# event_summaries Supabase row (re-fetched by the caller), which gates
# what we send to Haiku.
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 7 * 24 * 3600   # 7 days


_PROMPT = """You are an equity analyst producing the narrative page of a one-pager
research report. Be specific, metric-anchored, neutral. Quote actual numbers
from the data provided. Do NOT make up data. Do NOT hedge needlessly.

Return ONLY a JSON object (no prose, no markdown fences):

{{
  "exec_para": "3-4 sentence prose paragraph tying the metrics to the recent event. State the setup factually: e.g. 'Lattice trades at a growth premium (P/E 1023x) on accelerating revenue (+38% YoY) and 3-quarter beat streak; the Q1 print showed gross margin expanding to 68.4% despite mix headwinds.' No hype.",
  "bull": [
    "Each bullet 15-30 words. Cite a specific metric or event detail. Example: 'Revenue accelerating to +38% YoY with gross margin expanding 47 bps to 68.4%, signalling pricing power despite mix headwinds.'",
    "Two more like that"
  ],
  "bear": [
    "Each bullet 15-30 words. Cite a specific risk metric. Example: 'P/E of 1023x and P/S of 34x leave zero margin for execution slip; analyst revisions could turn fast on any guide-down.'",
    "Two more like that"
  ],
  "verdict": "ONE sentence, conviction-led: e.g. 'Quality setup with overbought signals (RSI 71) — wait for pullback to scale in.' Avoid generic phrases.",
  "conviction": "high|medium|low",
  "catalysts": [
    "Each item 8-15 words. Forward-looking events: next earnings expected ~late Aug 2026, product launches, conference participation. Pull from the event summary where possible. Be specific."
  ]
}}

TICKER: {ticker}
SECTOR: {sector}
ALPHA SCORE: {score}/100 ({grade})

KEY METRICS:
{metrics_block}

LATEST EVENT (from SEC filing):
{event_block}
"""


def _format_metrics(t: dict) -> str:
    """Compact 'label: value' block for the prompt — only fields with real
    values, skip nulls so Haiku doesn't reference 'Not reported'."""
    def fmt(label, key, fmt_str="{}"):
        v = t.get(key)
        if v is None or v == "" or v == "—":
            return None
        try:
            return f"{label}: " + fmt_str.format(v)
        except Exception:
            return f"{label}: {v}"

    rows = [
        fmt("Last price",      "last_close",      "${:.2f}"),
        fmt("Day change %",    "change_pct",      "{:+.2f}%"),
        fmt("Market cap",      "market_cap_str"),
        fmt("Revenue growth (YoY)", "rev_growth_yoy", "{:+.1f}%"),
        fmt("Gross margin",    "gross_margin",    "{:.1f}%"),
        fmt("FCF margin",      "fcf_margin",      "{:.1f}%"),
        fmt("P/E (TTM)",       "pe_ttm",          "{:.1f}x"),
        fmt("P/S (TTM)",       "ps_ttm",          "{:.1f}x"),
        fmt("PEG",             "peg_ratio",       "{:.2f}"),
        fmt("Debt/Equity",     "debt_equity",     "{:.2f}"),
        fmt("ROE (TTM)",       "roe_ttm",         "{:.1f}%"),
        fmt("52W range",       "wk52_range_str"),
        fmt("RSI (14)",        "rsi14",           "{:.0f}"),
        fmt("Beat streak",     "beat_streak_str"),
        fmt("90D insider",     "insider_90d_str"),
        fmt("EPS revisions (30D)", "eps_revisions_30d_str"),
        fmt("Short % float",   "short_pct_float", "{:.1f}%"),
        fmt("Beta (5Y)",       "beta_5y",         "{:.2f}"),
        fmt("Avg EPS surprise","avg_eps_surprise","{:+.1f}%"),
        fmt("Avg volume",      "avg_volume_str"),
        fmt("30D momentum %",  "momentum_30d",    "{:+.1f}%"),
    ]
    return "\n".join(r for r in rows if r)


def _format_event(event_row: dict | None) -> str:
    """Compact representation of the latest event_summary for the prompt."""
    if not event_row:
        return "(No recent SEC filing summary available. Reason about metrics alone, and put 'Next earnings expected' in catalysts based on quarterly cadence.)"
    parts = []
    title = event_row.get("event_title") or "Latest event"
    date  = event_row.get("event_date") or ""
    parts.append(f"Event: {title} ({date})")
    sections = event_row.get("sections")
    if isinstance(sections, list) and sections:
        for s in sections[:5]:
            heading = (s or {}).get("heading", "")
            bullets = (s or {}).get("bullets", []) or []
            if heading and bullets:
                parts.append(f"\n[{heading}]")
                for b in bullets[:5]:
                    parts.append(f"  - {b}")
    else:
        # Legacy schema fallback
        for label, key in [("Key updates", "key_updates"),
                            ("Operations", "operations"),
                            ("Outlook",    "outlook"),
                            ("Risks",      "risks")]:
            vals = event_row.get(key) or []
            if isinstance(vals, list) and vals:
                parts.append(f"\n[{label}]")
                for v in vals[:5]:
                    parts.append(f"  - {v}")
    excerpt = event_row.get("raw_excerpt")
    if excerpt:
        parts.append(f"\nVerbatim excerpt: {excerpt[:500]}")
    return "\n".join(parts)


async def _haiku_call(prompt: str) -> dict | None:
    if not _ANTHROPIC_KEY:
        logger.info("pdf_narrative: ANTHROPIC_API_KEY not set, skipping")
        return None
    headers = {
        "x-api-key":         _ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    payload = {
        "model":      _ANTHROPIC_MODEL,
        "max_tokens": 1200,
        "messages":   [{"role": "user", "content": prompt}],
    }
    try:
        async with httpx.AsyncClient(timeout=_ANTHROPIC_TIMEOUT) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                             json=payload, headers=headers)
            if r.status_code != 200:
                logger.warning(f"pdf_narrative: Haiku HTTP {r.status_code}: {r.text[:200]}")
                return None
            resp = r.json()
    except Exception as exc:
        logger.warning(f"pdf_narrative: Haiku call failed: {exc}")
        return None
    text = (resp.get("content") or [{}])[0].get("text", "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(f"pdf_narrative: JSON decode failed: {exc}; head: {text[:200]}")
        return None


async def build_narrative(ticker: str, t: dict, event_row: dict | None) -> dict | None:
    """Build the AI narrative dict consumed by pdf_render page 2.

    Returns None when Haiku is unavailable — pdf_render then renders a
    metrics-only page 2 with a small note rather than crashing."""
    sym = (ticker or "").upper()
    now = time.time()
    cached = _CACHE.get(sym)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    prompt = _PROMPT.format(
        ticker=sym,
        sector=t.get("sector") or t.get("sub_sector") or "—",
        score=int(t.get("smart_score") or t.get("pop_score") or 0),
        grade=(t.get("grade") or "—").upper(),
        metrics_block=_format_metrics(t),
        event_block=_format_event(event_row),
    )
    out = await _haiku_call(prompt)
    if not out:
        return None

    # Normalise — guarantee the keys the PDF expects, even if Haiku omits one
    out.setdefault("exec_para", "")
    out.setdefault("bull", [])
    out.setdefault("bear", [])
    out.setdefault("verdict", "")
    out.setdefault("conviction", "medium")
    out.setdefault("catalysts", [])
    _CACHE[sym] = (now, out)
    # LRU-trim
    if len(_CACHE) > 200:
        oldest_key = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest_key, None)
    return out
