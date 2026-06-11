"""
The Verdict — AlphaHunt's flagship, web-checked decision read.

The ONE premium box: Opus 4.8 + live web search. It weighs the bull and bear,
surfaces the latest analyst actions (attributed), and takes a compliant stance
(Outperform / Neutral / Avoid — never "Buy/Sell"), ending by handing the
decision back to the user.

Cached per ticker by the caller (app.py) so the expensive call is paid once per
ticker per staleness window and shared across all users.

Env:
  ANTHROPIC_API_KEY      — required
  ANTHROPIC_VERDICT_MODEL — default "claude-opus-4-8"
  VERDICT_TIMEOUT        — seconds, default 180
"""
from __future__ import annotations
import os
import logging
import httpx

logger = logging.getLogger(__name__)

_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Premium model for the single decision box. Opus by default — this is the one
# place we deliberately pay for the best reasoning + live web grounding.
_MODEL = (os.environ.get("ANTHROPIC_VERDICT_MODEL") or "claude-opus-4-8").strip()
_TIMEOUT = float(os.environ.get("VERDICT_TIMEOUT", "180"))

DISCLAIMER = (
    "_Research tool, not financial advice. AlphaHunt is not a SEBI-registered "
    "adviser. Any 'buy/sell/overweight' wording reflects third-party analyst "
    "views (attributed), not an AlphaHunt recommendation._"
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
        ("Analyst mean target", g("target_mean") or g("street_target")),
        ("Implied upside %", g("target_upside_pct")),
        ("Days to earnings", g("days_to_earnings")),
    ]
    lines = [f"- {k}: {v}" for k, v in fields if v not in (None, "", [])]
    return "\n".join(lines)


def _prompt(ticker: str, t: dict) -> str:
    return (
        f"You are AlphaHunt's senior equity analyst writing \"The Verdict\" — a "
        f"decisive, web-checked read on {ticker} ({t.get('name','')}) that helps a "
        f"sophisticated retail investor make their OWN decision. You are a RESEARCH "
        f"tool, NOT a SEBI-registered adviser: weigh the evidence and frame the "
        f"decision; do NOT instruct the user to buy or sell.\n\n"
        "GROUND TRUTH — use these exact figures; do not override them:\n"
        f"{_ground_block(t)}\n\n"
        "TASK: Use web search to find the LATEST (last few weeks/quarters) earnings "
        "and guidance, analyst rating changes and price-target revisions (NAME the "
        "firms and dates), valuation context, and material news. Then write the note "
        "in GitHub-flavoured Markdown with EXACTLY this structure, in this order:\n\n"
        "1. THE STRUCTURED BLOCK — output it FIRST as ONE fenced ```json code block "
        "so the app can render the stance badge even if the note runs long:\n"
        "```json\n"
        "{\n"
        '  "one_liner": "the single tension that decides this stock, one sentence",\n'
        '  "stance": "Outperform",\n'
        '  "conviction": 72,\n'
        '  "decisive_question": "the one question the investor must answer for themselves before deciding"\n'
        "}\n"
        "```\n"
        "`stance` MUST be exactly one of \"Outperform\", \"Neutral\", or \"Avoid\" — this "
        "is AlphaHunt's OWN research rating and must NEVER be \"Buy\" or \"Sell\". "
        "`conviction` is an integer 0-100 reflecting how strongly the evidence agrees.\n\n"
        "2. '## Bull case' — 3-4 bullets, each anchored to a web-verified fact with an "
        "inline markdown source link.\n"
        "3. '## Bear case' — 3-4 specific, sourced bullets (valuation, concentration, "
        "competition, dilution, execution).\n"
        "4. '## What the Street is saying' — the LATEST analyst actions: recent "
        "upgrades/downgrades and price-target revisions WITH firm names and dates, and "
        "the consensus target vs the current price. This is the ONLY section where "
        "'buy/sell/overweight/underweight' may appear, and ONLY as attributed "
        "third-party analyst views (name the firm).\n"
        "5. '## What would change our mind' — 2-3 concrete signposts (a specific number, "
        "an event, or a date) that would flip the stance.\n"
        "6. '## Bottom line' — 2-3 sentences that synthesise the evidence WITHOUT telling "
        "the user what to do, ending by handing the decision back to them.\n\n"
        "RULES: Cite every external/quantitative claim inline with a markdown link to a "
        "source you read. Never fabricate numbers, quotes, or URLs. Never write 'I "
        "recommend', 'you should buy', or 'sell now'. AlphaHunt's stance is "
        "Outperform/Neutral/Avoid only. Do NOT add a '## Sources' section (the app "
        "appends one). End the note with this exact line:\n"
        f"{DISCLAIMER}"
    )


async def generate_verdict(ticker: str, ticker_data: dict | None) -> dict:
    """Web-grounded Opus decision read. Raises on hard failure so the caller can
    persist a 'failed' status."""
    ticker = ticker.upper()
    t = ticker_data or {"ticker": ticker}
    if not available():
        raise RuntimeError("ANTHROPIC_API_KEY not configured")

    body = {
        "model": _MODEL,
        # Opus writes verbose, URL-heavy web citations + tool-use blocks all draw
        # from the output budget, so 2200 truncated the note mid-"Street" section
        # (dropping What-would-change-our-mind / Bottom line / disclaimer). 4500
        # comfortably fits the full 6-section note. Adds ~$0.05/note — fine for an
        # on-demand premium box.
        "max_tokens": 4500,
        # Stable web search (NOT the 20260209 dynamic-filter version, which timed
        # out the long web-grounded note). 3 searches cover earnings + analyst
        # actions + a risk/news sweep.
        "tools": [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 3}
        ],
        "messages": [{"role": "user", "content": _prompt(ticker, t)}],
    }

    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": _KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json=body,
        )
        if r.status_code >= 400:
            detail = r.text[:500]
            logger.error(f"verdict_gen {ticker} → {r.status_code} (model={_MODEL}): {detail}")
            raise RuntimeError(f"Anthropic {r.status_code} (model={_MODEL}): {detail}")
        data = r.json()

    _u = data.get("usage") or {}
    logger.info("verdict_gen %s (%s): in=%s out=%s",
                ticker, _MODEL, _u.get("input_tokens"), _u.get("output_tokens"))

    # Keep only the text AFTER the last web-search result (the model narrates
    # "I'll search…" between searches); collect sources from every result.
    content = data.get("content", [])
    last_search_idx = -1
    for i, block in enumerate(content):
        if block.get("type") == "web_search_tool_result":
            last_search_idx = i

    parts: list[str] = []
    sources: list[dict] = []
    seen: set[str] = set()
    for i, block in enumerate(content):
        bt = block.get("type")
        if bt == "text" and i > last_search_idx:
            parts.append(block.get("text", ""))
        elif bt == "web_search_tool_result":
            for res in block.get("content", []) or []:
                url = res.get("url")
                if url and url not in seen:
                    seen.add(url)
                    sources.append({"n": len(sources) + 1,
                                    "title": res.get("title") or url, "url": url})

    markdown = "".join(parts).strip()
    if not markdown:
        raise RuntimeError("Empty verdict response")
    # Compliance guarantee: the disclaimer must always be present, even if the
    # model omitted it or the note was truncated before the final line.
    if "SEBI-registered" not in markdown:
        markdown = markdown.rstrip() + "\n\n" + DISCLAIMER
    return {
        "ticker": ticker,
        "markdown": markdown,
        "sources": sources,
        "model": _MODEL,
        "status": "ready",
    }
