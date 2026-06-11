"""
AlphaHunt — AI Deep-Dive research generator.

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
# Generous default: generation runs as a fire-and-forget background job (not
# request-bound), and a web-grounded Opus note with up to 8 searches + a 6000-
# token budget routinely needs well over 60s. Too low → the httpx call times out,
# the job fails, and the cached note never refreshes.
_TIMEOUT = float(os.environ.get("RESEARCH_TIMEOUT", "180"))

DISCLAIMER = "_Research tool, not financial advice. Figures are point-in-time and may be stale._"


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
        ("Days to earnings", g("days_to_earnings")),
    ]
    lines = [f"- {k}: {v}" for k, v in fields if v not in (None, "", [])]
    return "\n".join(lines)


def _prompt(ticker: str, t: dict) -> str:
    return (
        f"You are a senior equity-research analyst writing a sharp, decision-useful "
        f"research note on {ticker} ({t.get('name','')}) for a sophisticated retail "
        f"investor. Match the depth and specificity of a top sell-side note.\n\n"
        "GROUND TRUTH — use these exact figures for price/score/fundamentals; "
        "do NOT invent or override them:\n"
        f"{_ground_block(t)}\n\n"
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
        "7. '## Where the price sits' — valuation read: the multiple, the spread of "
        "analyst targets (low / average / high, with firms if available), and any "
        "intrinsic / DCF fair-value range cited.\n"
        "8. '## What moves it next' — 3-5 catalysts, each prefixed with a rough "
        "timeframe in **bold** (e.g. **Next quarter**, **H2 2026**, **Ongoing**).\n"
        "9. '## Bottom line' — 2-3 sentences ending in the one honest question an "
        "investor should answer before buying.\n"
        "10. '## Sources' — numbered list of the web pages you actually used, as "
        "markdown links.\n\n"
        "RULES: Cite every external/quantitative claim inline with a markdown link to "
        "the source you read. Never fabricate numbers, quotes, or URLs — if you can't "
        "verify something, omit it. Be specific and concrete; no filler or hedging "
        "boilerplate. Do NOT narrate your search process. End the note with this exact "
        "line:\n"
        f"{DISCLAIMER}"
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
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 4}
        ],
        "messages": [{"role": "user", "content": _prompt(ticker, t)}],
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
    logger.info(
        "research_gen %s (%s): in=%s cache_read=%s out=%s",
        ticker, _MODEL, _u.get("input_tokens"),
        _u.get("cache_read_input_tokens"), _u.get("output_tokens"),
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
def _overview_prompt(ticker: str, t: dict) -> str:
    return (
        f"You are an equity-research analyst writing a COMPACT business snapshot for "
        f"{ticker} ({t.get('name','')}) for a retail investor. Work from the ground-truth "
        f"data below PLUS your own training knowledge of the company. This is a quick "
        f"overview — do NOT claim live/breaking figures, do NOT web-search, do NOT add "
        f"source links.\n\n"
        "GROUND TRUTH — use these exact figures; do not invent live data:\n"
        f"{_ground_block(t)}\n\n"
        "Output GitHub-flavoured Markdown in EXACTLY this order:\n"
        "1. A one-sentence **bold verdict** of where the company stands.\n"
        "2. THE STRUCTURED BLOCK — immediately after the verdict, as ONE fenced ```json "
        "code block. Use only figures you're confident about (ground truth or well-known "
        "facts); omit any field you can't support. Exact shape:\n"
        "```json\n"
        "{\n"
        '  "key_points": ["3 punchy, decision-relevant takeaways, max ~13 words each — the things that matter most about this stock right now"],\n'
        '  "edge": {\n'
        '    "the_catch": {"label": "the single biggest risk to the investment in 2-5 words", "detail": "one short sentence (max ~18 words) on why it matters"}\n'
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
        "timeframe (e.g. **Next quarter**, **H2 2026**, **Ongoing**).\n"
        "4. '## Bear case' — 3-5 specific risk bullets (valuation, concentration, "
        "competition, dilution, execution).\n"
        "No '## Sources' or '## Bottom line' section, no inline links. Be specific "
        "and concrete; no filler."
    )


async def generate_overview(ticker: str, ticker_data: dict | None) -> dict:
    """Cheap, fast (~3-8s) overview snapshot — Haiku, no web search. Powers the
    stock page's default Overview boxes. Raises on hard failure."""
    ticker = ticker.upper()
    t = ticker_data or {"ticker": ticker}
    if not available():
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    body = {
        "model": _OVERVIEW_MODEL,
        "max_tokens": 1800,
        "messages": [{"role": "user", "content": _overview_prompt(ticker, t)}],
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
    logger.info("overview_gen %s (%s): in=%s out=%s",
                ticker, _OVERVIEW_MODEL, _u.get("input_tokens"), _u.get("output_tokens"))
    markdown = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    if not markdown:
        raise RuntimeError("Empty overview response")
    return {
        "ticker": ticker,
        "markdown": markdown,
        "sources": [],
        "model": _OVERVIEW_MODEL,
        "kind": "overview",
        "status": "ready",
    }
