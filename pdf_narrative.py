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


_PROMPT = """You are an equity analyst writing the narrative section of an analyst
research report. Style: terse, specific, metric-anchored. NO essay prose,
NO hype, NO hedging filler. Write like Bernstein or Morgan Stanley, not
like a chatbot. Each bullet stands alone as a hard fact.

CRITICAL DATA RULES:
1. The KEY METRICS block shows values ALREADY in display form. If a margin
   reads '83.2%', that IS the margin — do NOT restate it as '0.8%'. If
   revenue growth reads '+139%', that IS the YoY growth. Quote numbers
   exactly as shown; never apply your own scaling.

2. The KEY TAKEAWAY (observation) must reference ONLY numbers from the
   KEY METRICS block — these are the values shown on page 1 of the report.
   The LATEST EVENT block may contain numbers for a different period
   (e.g. a single quarter's revenue growth vs. the TTM growth on the
   metrics card). Citing those creates contradictions on the same page.
   For the observation: use rev_growth_yoy, gross_margin, beat streak,
   momentum_30d from KEY METRICS — not figures from the event block.

3. For BULL / BEAR bullets, you MAY cite event-block numbers, but lead
   each bullet with a metric from the KEY METRICS block when possible so
   the reader can cross-reference page 1.

OUTPUT — return ONLY a JSON object (no markdown fences, no prose around it):

{{
  "observation": "ONE compact sentence (15-25 words) summarising the setup. Combine 2-3 strongest signals + 1 caveat. Example: 'Top-tier setup — +139% rev growth, 4/4 beats, +21% 30D momentum; tempered by RSI 60 and -2.3% FCF margin.' No hedges, no filler words like 'overall' or 'in conclusion'.",
  "bull": [
    "3 bullets, each 15-25 words. State a specific fact + its implication. Cite numbers exactly as shown in the metrics block. Example: 'Revenue accelerated to +139% YoY on \\$126.6M, with HPC Hosting contributing \\$71M as Polaris Forge 1 reached full 100MW operational status.'",
    "...",
    "..."
  ],
  "bear": [
    "3 bullets, each 15-25 words. Specific risk + supporting metric. NEVER invent margins or growth rates — only use values present in the metrics or event blocks.",
    "...",
    "..."
  ],
  "verdict": "ONE sentence, ≤ 30 words, conviction-led. State an action lens (build, hold, trim, wait for pullback) tied to a specific trigger. No generic phrases like 'monitor closely' or 'maintain conviction'.",
  "conviction": "high|medium|low",
  "catalysts": [
    "4-5 items, each 10-20 words. Forward-looking, dated where possible. Examples: 'Q1 FY27 earnings expected late Aug 2026', 'Avant E1 customer ramp through Q3', 'Industry conference participation in October'. Pull from the event block where it provides clues."
  ]
}}

TICKER: {ticker}
SECTOR: {sector}
ALPHA SCORE: {score}/100 ({grade})

KEY METRICS (values are already in display form — do not rescale):
{metrics_block}

LATEST EVENT (from SEC filing):
{event_block}
"""


def _pct(v):
    """Normalise a percent value that might be stored as either a fraction
    (0.832) OR a percent (83.2). Returns a percent number for display, or
    None if invalid. Heuristic: any absolute value <= 1 is assumed to be a
    fraction (since real-world margins / growth rates virtually never sit
    in (0, 1) as percents — 0.5% gross margin is nonsensical for an
    operating company). Above 1 → already a percent."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if -1.0 <= f <= 1.0 and f != 0:
        return f * 100.0
    return f


def _format_metrics(t: dict) -> str:
    """Compact 'label: value' block for the prompt — only fields with real
    values, skip nulls so Haiku doesn't reference 'Not reported'.

    All percent-style fields run through _pct() which auto-detects the
    fraction-vs-percent encoding the rest of the codebase is inconsistent
    about (some tickers store 0.832 for 83.2% gross margin, others 83.2).
    Without this, Haiku quotes 'Gross margin of 0.8%' which is nonsensical
    and looks AI-hallucinated to the reader."""
    def fmt(label, key, fmt_str="{}"):
        v = t.get(key)
        if v is None or v == "" or v == "—":
            return None
        try:
            return f"{label}: " + fmt_str.format(v)
        except Exception:
            return f"{label}: {v}"

    def fmt_pct(label, key, sign=False):
        v = _pct(t.get(key))
        if v is None:
            return None
        spec = "{:+.1f}%" if sign else "{:.1f}%"
        return f"{label}: " + spec.format(v)

    rows = [
        fmt("Last price",      "last_close",      "${:.2f}"),
        fmt_pct("Day change",  "change_pct",      sign=True),
        fmt("Market cap",      "market_cap_str"),
        fmt_pct("Revenue growth (YoY)", "rev_growth_yoy", sign=True),
        fmt_pct("Gross margin",         "gross_margin"),
        fmt_pct("FCF margin",           "fcf_margin", sign=True),
        fmt("P/E (TTM)",       "pe_ttm",          "{:.1f}x"),
        fmt("P/S (TTM)",       "ps_ttm",          "{:.1f}x"),
        fmt("PEG",             "peg_ratio",       "{:.2f}"),
        fmt("Debt/Equity",     "debt_equity",     "{:.2f}"),
        fmt_pct("ROE (TTM)",   "roe_ttm"),
        fmt("52W range",       "wk52_range_str"),
        fmt("RSI (14)",        "rsi14",           "{:.0f}"),
        fmt("Beat streak",     "beat_streak_str"),
        fmt("90D insider",     "insider_90d_str"),
        fmt("EPS revisions (30D)", "eps_revisions_30d_str"),
        fmt_pct("Short % float",   "short_pct_float"),
        fmt("Beta (5Y)",       "beta_5y",         "{:.2f}"),
        fmt_pct("Avg EPS surprise","avg_eps_surprise", sign=True),
        fmt("Avg volume",      "avg_volume_str"),
        fmt_pct("30D momentum",    "momentum_30d", sign=True),
        # Analyst data (new — drives the AI's coverage commentary)
        fmt("Analyst mean target", "target_mean", "${:.2f}"),
        fmt("Analyst high target", "target_high", "${:.2f}"),
        fmt("Analyst low target",  "target_low",  "${:.2f}"),
        fmt("Total analysts",      "total_analysts", "{:.0f}"),
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


_OBSERVATION_PROMPT = """You are writing the KEY TAKEAWAY line of an equity analyst report.
This sits next to a 12-metric snapshot card on page 1. The reader cross-
references your sentence against those metrics — so any number you quote
MUST match what's in the metrics block. Do NOT pull figures from any
other source (filings, press releases, news). The metrics card is the
single source of truth for this field.

Return ONLY a JSON object (no markdown fences, no surrounding prose):

{{
  "observation": "ONE sentence, 18-30 words. Lead with the headline read (top-tier / quality / mixed / weak setup), cite 2-3 strongest metrics from the block, end with one caveat. Example: 'Top-tier setup at score 81 — +27% YoY rev, 4/4 beats, +38% momentum, 41.6% gross margin; high 2.01 beta amplifies tape moves.' No hedging, no filler."
}}

TICKER: {ticker}
SECTOR: {sector}
ALPHA SCORE: {score}/100 ({grade})

METRICS (your only allowed data source — values are already in display form):
{metrics_block}
"""


_VALUATION_PROMPT = """You are an equity analyst writing the VALUATION SCENARIOS
section of a research report. Three price-target scenarios — Bear / Base
/ Bull — each with a one-sentence rationale tied to specific multiples,
growth assumptions, or events. Style: terse, conviction-led. No hedging.

Anchor the BASE case to the analyst mean target if available, otherwise
to a defensible multiple expansion / contraction from current.

Bear case: meaningful downside from current price — typically 15-30% off
the current level, anchored to a specific multiple compression or
fundamental disappointment. Cite WHAT goes wrong.

Bull case: meaningful upside — typically 20-50% above current — anchored
to a specific scenario (margin expansion sustaining, multiple re-rate,
TAM growth materialising). Cite WHAT goes right.

Numerical rules: prices ABSOLUTE dollar values. Cite multiples or
percentages exactly as they appear in the metrics block.

Return ONLY a JSON object (no markdown fences, no surrounding prose):

{{
  "bear": {{
    "price":     <number, dollar target>,
    "rationale": "≤ 18 words tying the bear price to a specific trigger"
  }},
  "base": {{
    "price":     <number>,
    "rationale": "≤ 18 words anchored to consensus or central call"
  }},
  "bull": {{
    "price":     <number>,
    "rationale": "≤ 18 words tied to a specific upside scenario"
  }}
}}

TICKER: {ticker}
SECTOR: {sector}
CURRENT PRICE: ${price}

KEY METRICS (values are in display form — quote exactly):
{metrics_block}

LATEST EVENT (forward guidance and recent narrative context):
{event_block}
"""


_DCF_PROMPT = """You are an equity analyst building a 5-year DCF model. Generate the
KEY ASSUMPTIONS only — the math is computed downstream. Be defensible:
ground each number in the metrics block, current trajectory, and sector
norms. Do NOT manufacture aggressive numbers to hit a desired price.

Constraints — Haiku output is sanitised downstream, but stay reasonable:
- Year 1-5 revenue growth: each value in [-30, 80] percent. Smooth
  decay from current rate toward steady-state typical for the sector.
- FCF margin target (steady-state by year 5): in [-20, 50] percent.
  Anchor to current FCF margin trajectory + reasonable expansion.
- WACC: in [6, 14] percent. Use sector beta as guide (high-beta tech =
  10-12%, mature low-beta = 7-9%).
- Terminal growth: in [1.5, 4.5] percent. Long-run nominal GDP-ish.

Return ONLY a JSON object (no markdown fences):

{{
  "revenue_growth": [<y1>, <y2>, <y3>, <y4>, <y5>],
  "fcf_margin_target": <number>,
  "wacc": <number>,
  "terminal_growth": <number>,
  "rationale": "≤ 25 words: anchor for the assumption set. e.g. 'Decay from current +68% to 12% steady-state on competitive AI infra; 18% FCF margin by Y5 on scale.'"
}}

TICKER: {ticker}
SECTOR: {sector}
CURRENT PRICE: ${price}

KEY METRICS (anchor your assumptions in this data):
{metrics_block}

LATEST EVENT (recent trajectory, guidance, capacity / pricing dynamics):
{event_block}
"""


_THESIS_PROMPT = """You are writing the Investment Thesis section of an equity
analyst report — Bernstein / Morgan Stanley voice. Terse, metric-anchored,
no essay prose, no hedging filler.

CRITICAL DATA RULE: The KEY METRICS block shows values ALREADY in display
form. If a margin reads '83.2%', that IS the margin. Quote numbers exactly
as shown; never apply your own scaling.

Bull/bear bullets MAY cite event-block numbers (e.g. segment revenue, a
specific quarter's growth), but lead each bullet with a metric reference
where possible so readers can cross-check page 1.

Return ONLY a JSON object (no markdown fences, no surrounding prose):

{{
  "bull": [
    "3 bullets, 15-25 words each. State a specific fact + its implication.",
    "...",
    "..."
  ],
  "bear": [
    "3 bullets, 15-25 words each. Specific risk + supporting metric.",
    "...",
    "..."
  ],
  "verdict": "ONE sentence, ≤ 30 words. Action lens (build / hold / trim / wait) tied to a trigger. No phrases like 'monitor closely' or 'maintain conviction'.",
  "conviction": "high|medium|low",
  "catalysts": [
    "4-5 items, 10-20 words each. Forward events, dated where possible."
  ]
}}

TICKER: {ticker}
SECTOR: {sector}
ALPHA SCORE: {score}/100 ({grade})

KEY METRICS (values are in display form — do not rescale):
{metrics_block}

LATEST EVENT (from SEC filing — may reference different periods than metrics):
{event_block}
"""


async def build_narrative(ticker: str, t: dict, event_row: dict | None) -> dict | None:
    """Build the AI narrative dict consumed by pdf_render page 2.

    Uses a TWO-CALL pattern so the KEY TAKEAWAY (observation) is generated
    with metrics-only input, completely insulated from event-block numbers
    that may reference a different period. Without this isolation Haiku
    would routinely cite event-block figures in the takeaway that
    contradicted the metrics card on page 1 (e.g. takeaway says '+44%
    revenue' while card shows '+27%'). The two calls run in parallel so
    cold-ticker latency is unchanged. Cost: ~2x ($0.002 → $0.004), still
    negligible at ~$0.20/mo for the full universe."""
    import asyncio as _asyncio
    sym = (ticker or "").upper()
    now = time.time()
    cached = _CACHE.get(sym)
    if cached and now - cached[0] < _CACHE_TTL:
        return cached[1]
    # Durable L2 (survives redeploys) — avoids re-paying 4 Haiku calls/ticker on
    # every build. Re-warm the in-process cache on a hit.
    try:
        from kv_store import store as _kv
        durable = _kv.get("pdf_narrative", sym, max_age_s=_CACHE_TTL)
        if durable:
            _CACHE[sym] = (now, durable)
            return durable
    except Exception:
        pass

    metrics_block = _format_metrics(t)
    event_block   = _format_event(event_row)
    common = dict(
        ticker=sym,
        sector=t.get("sector") or t.get("sub_sector") or "—",
        score=int(t.get("smart_score") or t.get("pop_score") or 0),
        grade=(t.get("grade") or "—").upper(),
        metrics_block=metrics_block,
    )
    price = t.get("last_close") or t.get("price") or 0
    obs_prompt    = _OBSERVATION_PROMPT.format(**common)
    thesis_prompt = _THESIS_PROMPT.format(**common, event_block=event_block)
    val_prompt    = _VALUATION_PROMPT.format(**common, event_block=event_block,
                                              price=f"{price:.2f}" if price else "—")
    dcf_prompt    = _DCF_PROMPT.format(**common, event_block=event_block,
                                        price=f"{price:.2f}" if price else "—")

    obs_task    = _haiku_call(obs_prompt)
    thesis_task = _haiku_call(thesis_prompt)
    val_task    = _haiku_call(val_prompt)
    dcf_task    = _haiku_call(dcf_prompt)
    obs_out, thesis_out, val_out, dcf_out = await _asyncio.gather(
        obs_task, thesis_task, val_task, dcf_task, return_exceptions=True,
    )

    out = {}
    if isinstance(thesis_out, dict):
        out = thesis_out
    if isinstance(obs_out, dict) and obs_out.get("observation"):
        out["observation"] = obs_out["observation"]
    if isinstance(val_out, dict):
        # Validate the valuation shape so a malformed Haiku response
        # doesn't break rendering — we only require the three keys, the
        # PDF renderer handles missing rationale gracefully.
        valuation = {}
        for k in ("bear", "base", "bull"):
            v = val_out.get(k)
            if isinstance(v, dict) and isinstance(v.get("price"), (int, float)):
                valuation[k] = {
                    "price":     float(v["price"]),
                    "rationale": str(v.get("rationale") or "")[:200],
                }
        if len(valuation) == 3:
            out["valuation"] = valuation
    if isinstance(dcf_out, dict):
        # Sanitise DCF assumptions — clamp to defensible ranges so a
        # hallucinated 200% growth assumption can't produce a $50k
        # implied price. The PDF renderer also validates downstream.
        try:
            rev_growth = [float(x) for x in (dcf_out.get("revenue_growth") or [])][:5]
            if len(rev_growth) == 5:
                rev_growth = [max(-30.0, min(80.0, v)) for v in rev_growth]
                fcf_m = max(-20.0, min(50.0, float(dcf_out.get("fcf_margin_target", 10))))
                wacc  = max(6.0,   min(14.0, float(dcf_out.get("wacc", 10))))
                tg    = max(1.5,   min(4.5,  float(dcf_out.get("terminal_growth", 3))))
                if wacc <= tg:   # math safety — terminal must converge
                    wacc = tg + 2
                out["dcf"] = {
                    "revenue_growth":    rev_growth,
                    "fcf_margin_target": fcf_m,
                    "wacc":              wacc,
                    "terminal_growth":   tg,
                    "rationale":         str(dcf_out.get("rationale") or "")[:200],
                }
        except (TypeError, ValueError):
            pass
    if not out:
        return None

    # Normalise — guarantee the keys the PDF expects, even if Haiku omits one
    out.setdefault("observation", out.get("exec_para", ""))
    out.setdefault("bull", [])
    out.setdefault("bear", [])
    out.setdefault("verdict", "")
    out.setdefault("conviction", "medium")
    out.setdefault("catalysts", [])
    out.setdefault("valuation", None)
    out.setdefault("dcf", None)
    _CACHE[sym] = (now, out)
    if len(_CACHE) > 200:
        oldest_key = min(_CACHE, key=lambda k: _CACHE[k][0])
        _CACHE.pop(oldest_key, None)
    try:
        from kv_store import store as _kv
        _kv.set("pdf_narrative", sym, out)   # persist so the next deploy reuses it
    except Exception:
        pass
    return out
