"""
TickerMover — AI analyst-judge for Top Hunts selection.

Role: an ADVISORY re-ranker. The quant gate (Grade A + Quant Score floor +
4-of-6 pillars + theme cap in app.py) decides which names are *eligible*. This
module never invents a pick and never changes eligibility — it scores conviction
ONLY among names that already passed the quant bar, so its judgment is used as a
tiebreaker that orders comparable-score candidates within the shortlist.

Model: Claude Opus 4.8 (`claude-opus-4-8`). Stock selection is a weigh-conflicting-
evidence reasoning task where the capability tier earns its keep, and it runs over
only ~20-25 qualified candidates once per rebuild, so the per-token price is
immaterial. Adaptive thinking at effort=high is the right setting for the task.

Output is forced to a JSON schema (structured outputs) so the result drops
straight into the selection sort with no parsing fragility.

Compliance (FCA / UK research tool): the model rates conviction on our own
Outperform/Avoid scale — it must NOT emit "Buy"/"Sell" instructions. Provided
data is ground truth; the model may not invent figures.

Returns: {ticker: {conviction:int, thesis:str, red_flags:[str], lean:str}}
"""
from __future__ import annotations

import json
import logging
import os

import httpx

import usage_log
import anthropic_shim

logger = logging.getLogger(__name__)

_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Opus 4.8 for the judge — overridable, but default to the strongest tier since
# this is the highest-stakes, lowest-volume AI call in the product.
_MODEL = (
    os.environ.get("ANTHROPIC_SELECTOR_MODEL")
    or "claude-opus-4-8"
).strip()
# Generous: runs as a fire-and-forget nightly job, not request-bound. Adaptive
# thinking over ~25 candidates at effort=high can take a while.
_TIMEOUT = float(os.environ.get("SELECTOR_TIMEOUT", "180"))

# Cap how many candidates we send. The qualified pool is ~20-25; 40 is plenty of
# headroom and bounds the output size / cost.
_MAX_CANDIDATES = int(os.environ.get("SELECTOR_MAX_CANDIDATES", "40"))


def available() -> bool:
    return bool(_KEY)


_SYSTEM = """\
You are the lead analyst for TickerMover's "Top Hunts" — a curated shortlist of US \
growth equities. Every candidate you are shown has ALREADY passed a quantitative \
gate (Grade A, a high composite Quant Score, and at least four of six investment \
pillars). Your job is NOT to re-check eligibility — it is to adjudicate AMONG these \
pre-qualified names so the strongest convictions rise to the top.

For each candidate, weigh the provided data holistically — momentum and trend, \
growth durability, quality/margins/balance sheet, valuation vs. analyst headroom, \
sentiment, and the realism of the implied path to target. Reward names where the \
pillars reinforce each other; penalise one-dimensional setups (e.g. pure momentum \
on stretched valuation, or cheap-but-decelerating).

Rules:
- The data block is GROUND TRUTH. Never invent or assume figures not given.
- This is a research/ranking tool, not investment advice. Rate conviction on an \
Outperform/Avoid basis. Do NOT use the words "buy" or "sell" as instructions.
- `conviction` is 0-100: 80-100 = highest-conviction Outperform, 60-79 = solid, \
40-59 = neutral/watch, below 40 = would Avoid despite passing the screen.
- `thesis` is ONE sentence (<= 30 words), specific to this name's data — no boilerplate.
- `red_flags` lists concrete concerns from the data (empty list if none material).
- `lean` names the single pillar the conviction leans on most (one of: momentum, \
growth, quality, valuation, sentiment, growth_potential).
- Return a judgment for EVERY ticker provided, keyed by its exact ticker symbol.\
"""

# Structured-output schema. JSON-schema numeric/length constraints aren't
# supported by the API, so conviction is a bare integer (clamped in code) and
# string lengths are guided by the prompt, not enforced here.
_SCHEMA = {
    "type": "object",
    "properties": {
        "judgments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ticker":    {"type": "string"},
                    "conviction": {"type": "integer"},
                    "thesis":    {"type": "string"},
                    "red_flags": {"type": "array", "items": {"type": "string"}},
                    "lean": {
                        "type": "string",
                        "enum": ["momentum", "growth", "quality",
                                 "valuation", "sentiment", "growth_potential"],
                    },
                },
                "required": ["ticker", "conviction", "thesis", "red_flags", "lean"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["judgments"],
    "additionalProperties": False,
}


def _candidate_line(t: dict) -> dict:
    """Compact, model-readable snapshot of ONE candidate = ground truth.
    Only fields the model should reason over; keeps the payload small."""
    g = lambda k: t.get(k)
    score = g("smart_score")
    if score is None:
        score = g("pop_score")
    price = float(g("price") or 0)
    tgt = float(g("target_mean") or 0)
    upside = round((tgt - price) / price * 100, 1) if price > 0 and tgt > 0 else None
    return {
        "ticker":          g("ticker"),
        "name":            g("name"),
        "sector":          g("sector"),
        "theme":           g("sub_sector") or g("subsector") or g("sector"),
        "alpha_score":     round(float(score), 1) if score is not None else None,
        "grade":           g("grade"),
        "price":           price or None,
        "analyst_upside_pct": upside,
        "momentum_1m_pct": g("momentum_1m"),
        "momentum_3m_pct": g("momentum_3m"),
        "rev_growth_yoy_pct": g("revenue_growth_yoy") or g("rev_growth_qyoy"),
        "eps_growth_yoy_pct": g("eps_growth_yoy"),
        "gross_margin":    g("gross_margin"),
        "fcf_margin":      g("fcf_margin"),
        "pe_ratio":        g("pe_ratio"),
        "peg_ratio":       g("peg_ratio"),
        "debt_to_equity":  g("debt_to_equity"),
        "pillars":         g("_pillars"),   # optional: caller may attach
        "signals":         (g("signals") or [])[:4],
    }


async def score_candidates(candidates: list[dict], model: str | None = None) -> dict[str, dict]:
    """Score a list of quant-qualified candidates. Returns {ticker: judgment}.

    Pure read of the model — caller is responsible for caching the result
    (see selection_store). Returns {} on any failure so selection can fall
    back cleanly to the quant-only ordering.

    `model` overrides the default selector model for one call (used by the
    Opus-vs-Sonnet A/B diagnostic); falls back to _MODEL when None.
    """
    if not available() or not candidates:
        return {}

    use_model = (model or _MODEL).strip()
    pool = candidates[:_MAX_CANDIDATES]
    payload = [_candidate_line(t) for t in pool if t.get("ticker")]
    if not payload:
        return {}

    body = {
        "model": use_model,
        "max_tokens": 8000,
        "thinking": {"type": "adaptive"},
        # effort lives inside output_config alongside the format constraint.
        "output_config": {
            "effort": "high",
            "format": {"type": "json_schema", "schema": _SCHEMA},
        },
        # Methodology is stable → cache_control (only engages above the model's
        # min cacheable prefix; harmless otherwise). Cost here is already trivial.
        "system": [
            {"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{
            "role": "user",
            "content": (
                "Score these pre-qualified Top Hunts candidates. Return one "
                "judgment per ticker.\n\nCANDIDATES (JSON):\n"
                + json.dumps(payload, ensure_ascii=False, default=str)
            ),
        }],
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await anthropic_shim.post(
                headers={
                    "x-api-key": _KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
        if r.status_code >= 400:
            logger.error(f"ai_selector → {r.status_code} (model={use_model}): {r.text[:400]}")
            return {}
        data = r.json()
    except Exception as e:
        logger.error(f"ai_selector call failed: {e}")
        return {}

    _u = data.get("usage") or {}
    try:
        usage_log.record("selection", use_model, _u)
    except Exception:
        pass
    logger.info(
        "ai_selector scored %d candidates (%s): in=%s cache_read=%s out=%s",
        len(payload), use_model, _u.get("input_tokens"),
        _u.get("cache_read_input_tokens"), _u.get("output_tokens"),
    )

    # output_config.format guarantees the first text block is valid JSON for
    # the schema. Find it defensively (thinking blocks precede it).
    text = next(
        (b.get("text") for b in data.get("content", [])
         if b.get("type") == "text" and b.get("text")),
        None,
    )
    if not text:
        logger.error("ai_selector: no text block in response")
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"ai_selector: bad JSON ({e}): {text[:200]}")
        return {}

    out: dict[str, dict] = {}
    for j in parsed.get("judgments", []):
        tkr = (j.get("ticker") or "").strip().upper()
        if not tkr:
            continue
        conv = j.get("conviction")
        try:
            conv = max(0, min(100, int(round(float(conv)))))
        except (TypeError, ValueError):
            conv = None
        out[tkr] = {
            "conviction": conv,
            "thesis":     (j.get("thesis") or "").strip()[:240],
            "red_flags":  [str(x).strip()[:160] for x in (j.get("red_flags") or [])][:4],
            "lean":       (j.get("lean") or "").strip(),
            "model":      use_model,
        }
    return out
