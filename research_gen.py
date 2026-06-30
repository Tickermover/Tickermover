"""
TickerMover — AI Deep-Dive research generator.

Produces a web-grounded research brief (markdown + sources) for a single
ticker, using the Anthropic Messages API with the server-side `web_search`
tool so every external claim can be cited with a real URL.

The ticker's OWN live data (price, Alpha Score, grade, key fundamentals) is
passed in as ground truth so the model never invents those numbers; the web
search is for recent catalysts / news / context only.

Returns:  {markdown, sources:[{n,title,url}], model, status}
"""
from __future__ import annotations

import logging
import os

import httpx

import usage_log

logger = logging.getLogger(__name__)

_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Research wants a stronger model than the haiku used for thesis polish.
# Override with ANTHROPIC_RESEARCH_MODEL; falls back to ANTHROPIC_MODEL.
_MODEL = (
    os.environ.get("ANTHROPIC_RESEARCH_MODEL")
    or os.environ.get("ANTHROPIC_MODEL")
    or "claude-haiku-4-5-20251001"
).strip()
# Model for the OVERVIEW snapshot that fills the stock page's default boxes:
# the heading verdict, "what you'd actually be buying" (business), catalysts and
# risks. Sonnet (not the much pricier Opus, and a step up from Haiku) gives the
# marquee, user-facing boxes sharper prose. No web search, small output → ~$0.02
# (₹1.9)/stock, vs ~$0.30 for an Opus web-grounded note. Override via env.
_OVERVIEW_MODEL = (
    os.environ.get("ANTHROPIC_OVERVIEW_MODEL")
    or "claude-sonnet-4-6"
).strip()
# Premium model used for the curated/featured set only — those ~35 names are
# cached and concentrate user attention, so upgrading just them is a bounded cost
# (~+$0.02/gen) that doesn't scale with users. Override via env.
_OVERVIEW_PREMIUM_MODEL = (
    os.environ.get("ANTHROPIC_OVERVIEW_PREMIUM_MODEL")
    or "claude-opus-4-8"
).strip()
# Generous default: generation runs as a fire-and-forget background job (not
# request-bound), and a web-grounded Opus note with up to 8 searches + a 6000-
# token budget routinely needs well over 60s. Too low → the httpx call times out,
# the job fails, and the cached note never refreshes.
_TIMEOUT = float(os.environ.get("RESEARCH_TIMEOUT", "180"))

DISCLAIMER = "_Research tool, not financial advice. Figures are point-in-time and may be stale._"

# ── Shared anti-drift rule ─────────────────────────────────────────────────
# Every AI brief (overview snapshot + web-grounded deep-dive) is CACHED for up
# to 30 days and rendered right beside a LIVE data strip. Any specific price /
# target / multiple / date baked into the prose drifts within days and then
# visibly contradicts the strip ("Price ($350)… target ($245)" sitting next to
# a live $489 / $272). One identical, hard rule for both prompts keeps every
# drifting number OUT of the cached text and pushes the model to judge those
# things qualitatively instead. Durable fundamentals (revenue, growth, margins)
# are explicitly still allowed — those don't drift between cache refreshes.
_FRESHNESS_RULE = (
    "FRESHNESS RULE — CRITICAL, APPLIES TO EVERY WORD YOU WRITE. This text is "
    "CACHED for up to 30 days and shown right next to a LIVE data strip (current "
    "price, analyst mean target, upside %, forward P/E, market cap, 52-week range, "
    "rating, beta). Any specific number you bake in WILL drift within days and then "
    "CONTRADICT that strip — making the whole note look stale and wrong. So ANYWHERE "
    "in your output (verdict, key points, edge, business, catalysts, risks — every "
    "sentence and bullet), you must NEVER state as a specific figure: the current "
    "share price (e.g. 'Price ($350)', 'trading at $489'); an analyst price target in "
    "dollars (e.g. 'mean target $245'); any %/dollar premium or discount to target "
    "(e.g. '21% above target', '$100 below target'); a precise current valuation "
    "multiple (e.g. 'Forward P/E ~235', '12x sales'); current market cap as a precise "
    "number; or any near-term calendar date or day-countdown (e.g. 'Next 13 days', "
    "'reports July 25'). INSTEAD judge them QUALITATIVELY / directionally: 'trades well "
    "above analyst consensus', 'priced for near-flawless execution', 'nosebleed / "
    "triple-digit multiple', 'richly valued vs its own history and peers', 'crowded "
    "long', 'cheap vs peers', 'near its next earnings'. ALWAYS FINE (durable — these "
    "do NOT drift between refreshes, use them freely and specifically): revenue, "
    "revenue growth %, gross/operating margins, beat streak, segment mix, customer "
    "concentration, unit/volume figures, and multi-year historical trends."
)


def available() -> bool:
    return bool(_KEY)


def _ground_block(t: dict) -> str:
    """Compact, model-readable snapshot of our own data = ground truth."""
    g = lambda k: t.get(k)
    fields = [
        ("Company", g("name")),
        ("Sector", g("sector") or (t.get("meta") or {}).get("sector")),
        ("Price", g("price")),
        ("Day change %", g("change_pct")),
        ("Alpha Score", g("smart_score") if g("smart_score") is not None else g("pop_score")),
        ("Grade", g("grade")),
        ("Market cap", g("market_cap")),
        ("Revenue growth YoY %", g("revenue_growth_yoy")),
        ("Profit margin %", g("profit_margin")),
        ("Forward P/E", g("pe_ratio") or g("forward_pe")),
        ("RSI(14)", g("rsi_14")),
        ("Analyst mean target", g("target_mean") or g("street_target")),
    ]
    lines = [f"- {k}: {v}" for k, v in fields if v not in (None, "", [])]
    return "\n".join(lines)


# ── Prompt split for caching ──────────────────────────────────────────────
# The large instruction + JSON-schema block is IDENTICAL for every ticker, so
# it lives in a cached `system` prefix (marked cache_control: ephemeral in the
# request). Only the per-ticker ground-truth goes in the user message. This cuts
# repeated input cost — especially across the web_search tool loop, where the
# server re-processes this block on every internal turn within one request.
def _research_system() -> str:
    return (
        "You are a senior equity-research analyst writing a sharp, decision-useful "
        "research note on the US-listed company given in the next message, for a "
        "sophisticated retail investor. Match the depth and specificity of a top "
        "sell-side note.\n\n"
        "TASK: Use web search aggressively to find the LATEST (last few weeks/quarters) "
        "earnings prints, guidance, segment detail, customer/revenue concentration, "
        "analyst price targets (name the firms), valuation context (P/E, P/S, DCF / "
        "fair-value if cited), insider activity, dilution, and risks. Then write the "
        "note in GitHub-flavoured Markdown with EXACTLY this structure, in this order:\n"
        "1. A one-sentence **bold verdict** of where the company stands right now — "
        "the single most important tension.\n"
        "2. THE STRUCTURED BLOCK — output it HERE, immediately after the verdict (do "
        "NOT save it for the end of the note), as ONE fenced ```json code block so the "
        "app can render its visual sections even when the note runs long. Use only "
        "web-verified figures; omit any field you cannot verify. Exact shape:\n"
        "```json\n"
        "{\n"
        '  "business": {\n'
        '    "intro": "1-2 sentence plain-English summary of what the company does / how it makes money",\n'
        '    "engines": [\n'
        '      {"name":"<engine 1 name>","tag":"<short eyebrow, e.g. The AI story · ramping fast>","body":"2-3 sentences with specific figures","tone":"primary"},\n'
        '      {"name":"<engine 2 name>","tag":"<short eyebrow>","body":"2-3 sentences","tone":"secondary"}\n'
        "    ],\n"
        '    "callout": {"title":"<short title, e.g. The expansion bet>","body":"the key strategic bet / pivot in 1-2 sentences with a figure"}\n'
        "  },\n"
        '  "revenue": [\n'
        '    {"label":"FY24","sub":"baseline","display":"$249M","value":249},\n'
        '    {"label":"FY25","sub":"+83%","display":"$456M","value":456},\n'
        '    {"label":"FY26E","sub":"guide ~+140%","display":">$1.1B","value":1100}\n'
        "  ]\n"
        "}\n"
        "```\n"
        "For `revenue`, give 2-4 fiscal-year points (oldest→newest, last point may be a "
        "guide/estimate); `value` is the revenue as a plain number in the SAME unit "
        "across all points (millions preferred) so bars scale correctly; `display` is "
        "the human label. Omit the whole `business` or `revenue` key if you can't verify it.\n"
        "3. '## What you're buying' — 2-4 sentences: what the company actually does / "
        "how it makes money / its core engine(s) and any second growth engine.\n"
        "4. '## The numbers' — the recent revenue & margin trajectory with SPECIFIC "
        "figures (most recent quarter + full-year + guide), growth rates, and "
        "beat/miss vs consensus.\n"
        "5. '## Bull case' — 4-6 tight bullets, each anchored to a specific fact.\n"
        "6. '## Bear case' — 4-6 tight bullets (valuation, concentration, dilution, "
        "competition, profitability), each specific.\n"
        "7. '## Where the price sits' — valuation read: the multiple, whether the "
        "stock is rich or cheap vs its own history and peers, and where analyst "
        "sentiment sits RELATIVE to the price (targets clustering above/below it, plus "
        "any DCF/fair-value view) — described directionally, WITHOUT anchoring to a "
        "specific current price or analyst-target dollar that will drift.\n"
        "8. '## What moves it next' — 3-5 catalysts, each prefixed with a rough "
        "timeframe in **bold**. Use ONLY qualitative buckets — e.g. **Next quarter**, "
        "**Next earnings**, **H2 2026**, **Ongoing**. NEVER use a literal day/week "
        "countdown (e.g. not **Next 13 days**, not **In 3 weeks**): this note is cached "
        "for weeks, so any countdown would be wrong by the time it is read.\n"
        "9. '## Bottom line' — 2-3 sentences ending in the one honest question an "
        "investor should answer before buying.\n"
        "10. '## Sources' — numbered list of the web pages you actually used, as "
        "markdown links.\n\n"
        "RULES: Cite every external/quantitative claim inline with a markdown link to "
        "the source you read. Never fabricate numbers, quotes, or URLs — if you can't "
        "verify something, omit it. Be specific and concrete; no filler or hedging "
        "boilerplate. Do NOT narrate your search process.\n\n"
        f"{_FRESHNESS_RULE}\n\n"
        "End the note with this exact line:\n"
        f"{DISCLAIMER}"
    )


def _research_user(ticker: str, t: dict) -> str:
    """Per-ticker, NON-cached portion: the company + our ground-truth figures."""
    return (
        f"Company: {ticker} ({t.get('name','')}).\n\n"
        "GROUND TRUTH — use these exact figures for price/score/fundamentals; "
        "do NOT invent or override them:\n"
        f"{_ground_block(t)}\n\n"
        "Write the research note now, following the required structure exactly."
    )


async def generate_research(ticker: str, ticker_data: dict | None) -> dict:
    """Generate a grounded brief. Raises on hard failure so the caller can
    persist a 'failed' status if desired."""
    ticker = ticker.upper()
    t = ticker_data or {"ticker": ticker}
    if not available():
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    body = {
        "model": _MODEL,
        # Headroom so the full note (8 prose sections + the structured JSON block,
        # which now comes first) completes without truncation. 3800 cut notes off
        # mid-prose, dropping the JSON block → empty Business/revenue sections.
        "max_tokens": 6000,
        # Cache the big static instruction/schema block so the web_search tool
        # loop (which re-processes the prompt on every internal turn) and any
        # back-to-back generations pay ~0.1× on it instead of full input price.
        "system": [
            {"type": "text", "text": _research_system(),
             "cache_control": {"type": "ephemeral"}}
        ],
        "tools": [
            # Each web search costs ~$0.01 regardless of model — cap tighter to
            # keep the per-note cost down (was 8). 4 searches still cover
            # earnings + guidance + targets + a risk/catalyst sweep.
            #
            # NOTE: web_search_20260209 (dynamic filtering) was tried here to cut
            # premium input tokens, but its server-side code-filtering step pushed
            # the web-grounded Opus note past the 180s timeout → notes never
            # completed. Reverted to the stable 20250305 search. Cost is bounded
            # instead via max_uses + per-ticker caching in research_store.
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 2}
        ],
        "messages": [{"role": "user", "content": _research_user(ticker, t)}],
    }

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
            detail = r.text[:600]
            logger.error(f"research_gen {ticker} → {r.status_code} (model={_MODEL}): {detail}")
            # Raise with the API body so the failure reason propagates to the
            # caller (and out through /api/research) instead of a generic 4xx.
            raise RuntimeError(f"Anthropic {r.status_code} (model={_MODEL}): {detail}")
        data = r.json()

    _u = data.get("usage") or {}
    usage_log.record("research", _MODEL, _u, ticker=ticker)
    logger.info(
        "research_gen %s (%s): in=%s cache_write=%s cache_read=%s out=%s",
        ticker, _MODEL, _u.get("input_tokens"),
        _u.get("cache_creation_input_tokens"), _u.get("cache_read_input_tokens"),
        _u.get("output_tokens"),
    )

    # Assemble the note from the assistant text blocks. The model emits
    # conversational narration between web searches ("I'll search for…", "Let me
    # compile the brief…") as its own text blocks; only the text AFTER the last
    # search result is the actual note, so we drop everything up to that point.
    # Sources are still collected from EVERY search result regardless of order.
    content = data.get("content", [])
    last_search_idx = -1
    for i, block in enumerate(content):
        if block.get("type") == "web_search_tool_result":
            last_search_idx = i

    markdown_parts: list[str] = []
    sources: list[dict] = []
    seen: set[str] = set()
    for i, block in enumerate(content):
        bt = block.get("type")
        if bt == "text" and i > last_search_idx:
            markdown_parts.append(block.get("text", ""))
        elif bt == "web_search_tool_result":
            for res in block.get("content", []) or []:
                url = res.get("url")
                if url and url not in seen:
                    seen.add(url)
                    sources.append({
                        "n": len(sources) + 1,
                        "title": res.get("title") or url,
                        "url": url,
                    })

    markdown = "".join(markdown_parts).strip()
    if not markdown:
        raise RuntimeError("Empty research response")

    return {
        "ticker": ticker,
        "markdown": markdown,
        "sources": sources,
        "model": _MODEL,
        "status": "ready",
    }


# ── Cheap OVERVIEW snapshot (Haiku, no web search) ────────────────────────
# Fills the stock page's default Overview boxes (#ovBiz business engines,
# #ovRevBars revenue, ovNoteCat/Risk/Bottom note boxes) without paying for the
# premium web-grounded note. Output format is byte-compatible with the full
# note's parser: a leading bold verdict, one ```json block (business+revenue),
# then `## What moves it next`, `## Bear case`, `## Bottom line` sections.
def _overview_system() -> str:
    """Static (cacheable) instruction + schema block — identical for every
    ticker, so it's sent as a cached system prefix."""
    return (
        "You are an equity-research analyst writing a COMPACT business snapshot for "
        "the company given in the next message, for a retail investor. Work from the "
        "ground-truth data provided PLUS your own training knowledge of the company. "
        "This is a quick overview — do NOT claim live/breaking figures, do NOT "
        "web-search, do NOT add source links.\n\n"
        f"{_FRESHNESS_RULE}\n\n"
        "Output GitHub-flavoured Markdown in EXACTLY this order:\n"
        "1. A one-sentence **bold verdict** of where the company stands.\n"
        "2. THE STRUCTURED BLOCK — immediately after the verdict, as ONE fenced ```json "
        "code block. Use only figures you're confident about (ground truth or well-known "
        "facts); omit any field you can't support. Exact shape:\n"
        "```json\n"
        "{\n"
        '  "key_points": ["3 punchy, decision-relevant takeaways, max ~13 words each — the things that matter most about this stock right now"],\n'
        '  "edge": {\n'
        '    "setup_read": {"label": "characterize the setup in 2-3 words MAX, ~20 chars (e.g. Asymmetric bet / Priced for perfection / Quiet compounder)", "detail": "one short sentence (max ~16 words) on why"},\n'
        '    "thesis_confidence": {"label": "exactly one of: Strong | Moderate | Speculative", "detail": "one short sentence on why the bull case is/ isn\'t solid"},\n'
        '    "the_catch": {"label": "the single biggest risk in 2-4 words", "detail": "one short sentence (max ~18 words) on why it matters"}\n'
        '  },\n'
        '  "business": {\n'
        '    "intro": "1-2 sentence plain-English summary of what the company does / how it makes money",\n'
        '    "engines": [\n'
        '      {"name":"<engine 1 name>","tag":"<short eyebrow>","body":"2-3 sentences with figures","tone":"primary"},\n'
        '      {"name":"<engine 2 name>","tag":"<short eyebrow>","body":"2-3 sentences","tone":"secondary"}\n'
        "    ],\n"
        '    "callout": {"title":"<short title>","body":"the key strategic bet in 1-2 sentences"}\n'
        "  },\n"
        '  "revenue": [\n'
        '    {"label":"FY24","sub":"baseline","display":"$249M","value":249},\n'
        '    {"label":"FY25","sub":"+83%","display":"$456M","value":456},\n'
        '    {"label":"FY26E","sub":"guide ~+140%","display":">$1.1B","value":1100}\n'
        "  ]\n"
        "}\n"
        "```\n"
        "For `revenue`: 2-4 fiscal-year points oldest→newest (last may be an estimate); "
        "`value` is a plain number in the SAME unit across points (millions preferred); "
        "`display` is the human label. Omit the whole `business` or `revenue` key if you "
        "can't support it.\n"
        "3. '## What moves it next' — 3-4 catalysts, each prefixed with a **bold** rough "
        "timeframe. Use ONLY qualitative buckets — e.g. **Next quarter**, **Next earnings**, "
        "**H2 2026**, **Ongoing**. NEVER use a literal day/week countdown (e.g. not "
        "**Next 13 days**): this snapshot is cached for weeks, so a countdown would be "
        "stale by the time it is read.\n"
        "4. '## Bear case' — 3-5 specific risk bullets (valuation, concentration, "
        "competition, dilution, execution).\n"
        "No '## Sources' or '## Bottom line' section, no inline links. Be specific "
        "and concrete; no filler."
    )


def _overview_user(ticker: str, t: dict) -> str:
    """Per-ticker, NON-cached portion: the company + our ground-truth figures."""
    return (
        f"Company: {ticker} ({t.get('name','')}).\n\n"
        "GROUND TRUTH — use these exact figures; do not invent live data:\n"
        f"{_ground_block(t)}\n\n"
        "Write the snapshot now, following the required order exactly."
    )


async def generate_overview(ticker: str, ticker_data: dict | None,
                            premium: bool = False) -> dict:
    """Overview snapshot (no web search) — powers the stock page's default
    Overview boxes. `premium=True` uses the Opus tier (for the curated/featured
    set); everything else uses the standard Sonnet tier. Raises on hard failure."""
    ticker = ticker.upper()
    t = ticker_data or {"ticker": ticker}
    if not available():
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    model = _OVERVIEW_PREMIUM_MODEL if premium else _OVERVIEW_MODEL
    body = {
        "model": model,
        "max_tokens": 1800,
        # Cache the static instruction/schema block; back-to-back snapshots
        # (e.g. a universe pre-warm) then pay ~0.1× on it.
        "system": [
            {"type": "text", "text": _overview_system(),
             "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": _overview_user(ticker, t)}],
    }
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": _KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body,
        )
        if r.status_code >= 400:
            detail = r.text[:400]
            logger.error(f"overview_gen {ticker} → {r.status_code}: {detail}")
            raise RuntimeError(f"Anthropic {r.status_code}: {detail}")
        data = r.json()

    _u = data.get("usage") or {}
    usage_log.record("overview", model, _u, ticker=ticker)
    logger.info("overview_gen %s (%s%s): in=%s cache_write=%s cache_read=%s out=%s",
                ticker, model, " ★premium" if premium else "", _u.get("input_tokens"),
                _u.get("cache_creation_input_tokens"), _u.get("cache_read_input_tokens"),
                _u.get("output_tokens"))
    markdown = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    if not markdown:
        raise RuntimeError("Empty overview response")
    return {
        "ticker": ticker,
        "markdown": markdown,
        "sources": [],
        "model": model,
        "kind": "overview",
        "status": "ready",
    }
