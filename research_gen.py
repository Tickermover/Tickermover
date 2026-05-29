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
_TIMEOUT = float(os.environ.get("RESEARCH_TIMEOUT", "60"))

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
        f"You are a senior equity-research analyst writing a concise Deep-Dive "
        f"brief on {ticker} ({t.get('name','')}).\n\n"
        "GROUND TRUTH — use these exact figures for price/score/fundamentals; "
        "do NOT invent or override them:\n"
        f"{_ground_block(t)}\n\n"
        "TASK: Use web search to find the LATEST (last few weeks) catalysts, "
        "news, earnings, guidance, analyst moves and risks for this company, "
        "then write the brief in GitHub-flavoured Markdown with this structure:\n"
        "1. A one-sentence bold summary of where the company stands right now.\n"
        "2. '## Catalysts' — 3-5 bullets of what's driving / could drive the "
        "stock, each with a specific fact.\n"
        "3. '## Risks' — 2-3 bullets.\n"
        "4. '## Bottom line' — 2-3 sentences.\n"
        "5. '## Sources' — numbered list of the web pages you actually used, "
        "as markdown links.\n\n"
        "RULES: Cite every external/quantitative claim inline with a markdown "
        "link to the source you read. Never fabricate numbers, quotes, or "
        "URLs. Keep it tight and specific — no filler. End with this exact "
        f"line:\n{DISCLAIMER}"
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
        "max_tokens": 2600,
        "tools": [
            {"type": "web_search_20250305", "name": "web_search", "max_uses": 6}
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
            logger.error(f"research_gen {ticker} → {r.status_code}: {r.text[:300]}")
        r.raise_for_status()
        data = r.json()

    # Concatenate the assistant text blocks → markdown. Collect every URL the
    # web_search tool surfaced so we have a structured sources list even if the
    # model's own '## Sources' section is thin.
    markdown_parts: list[str] = []
    sources: list[dict] = []
    seen: set[str] = set()
    for block in data.get("content", []):
        bt = block.get("type")
        if bt == "text":
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
