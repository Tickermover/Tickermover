"""
TickerMover — Event intel (earnings call summarization)
=====================================================

Quartr-style structured event summary for a ticker. Pulls the most recent
earnings call transcript from Alpha Vantage, summarizes via Anthropic
Haiku into 4 topic buckets, caches the result in Supabase so subsequent
hits are free + instant.

Output shape (also the SQL row shape, see db/2026-05-23-event-intel.sql):
    {
        "ticker":       str,
        "event_title":  "Q1 2026 earnings call",
        "event_date":   "2026-04-30",
        "source":       "alpha_vantage_transcript" | "sec_8k_fallback",
        "key_updates":   ["...", "..."],         # 3-5 bullet points
        "operations":    ["...", "..."],         # manufacturing / capex
        "outlook":       ["...", "..."],         # guidance + forward-looking
        "risks":         ["...", "..."],         # caveats / headwinds
        "raw_excerpt":   "...",                  # 1-paragraph verbatim quote
        "summarized_at": "2026-05-23T15:42:00Z",
    }

Cost: ~$0.001 per stock-quarter (Haiku is cheap). Cache TTL = 14 days so
new earnings calls trigger a refresh automatically.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx

import config
import anthropic_shim

logger = logging.getLogger(__name__)

_ANTHROPIC_KEY     = os.environ.get("ANTHROPIC_API_KEY", "").strip()
_ANTHROPIC_MODEL   = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
_ANTHROPIC_TIMEOUT = 18.0
_AV_BASE           = "https://www.alphavantage.co/query"

# SEC EDGAR — free, no key, no rate limit beyond their fair-use 10 req/s
# policy. They require a descriptive User-Agent identifying who's hitting
# the service (their robots.txt enforces this).
_EDGAR_UA = os.environ.get(
    "SEC_EDGAR_UA",
    "TickerMover Research alphahunt-bot@example.com",
)
_EDGAR_HEADERS = {"User-Agent": _EDGAR_UA, "Accept-Encoding": "gzip, deflate"}
_EDGAR_TICKER_MAP: dict[str, str] | None = None   # ticker -> 10-digit CIK


# ── 1. Transcript fetch ───────────────────────────────────────────────────

def _recent_quarters(n: int = 4) -> list[str]:
    """Return last N quarter labels (e.g. '2026Q1') newest-first.

    Alpha Vantage's EARNINGS_CALL_TRANSCRIPT endpoint REQUIRES an
    explicit quarter parameter — without it the response is an empty
    array even for major tickers. We try the most-recent-reported
    quarter first (the previous quarter; companies typically report
    1-2 months after quarter-end) and fall back through older ones
    until we find a populated transcript."""
    from datetime import datetime as _dt
    now = _dt.utcnow()
    # Start from the PREVIOUS quarter — most companies have reported
    # by mid-second-month of the new quarter.
    y, m = now.year, now.month
    prev_q = (m - 1) // 3   # 0..3 — Q1 of current year already finished if we subtract 1
    # Build (year, quarter) pairs walking backwards
    out = []
    q = prev_q   # last completed quarter index (0-3)
    yr = y
    if q == 0:
        # Wrap to Q4 of last year
        q = 4
        yr = y - 1
    for _ in range(n):
        out.append(f"{yr}Q{q}")
        q -= 1
        if q == 0:
            q = 4
            yr -= 1
    return out


async def _fetch_av_transcript(ticker: str, quarter: str | None = None) -> dict | None:
    """Hit Alpha Vantage EARNINGS_CALL_TRANSCRIPT. Returns the raw transcript
    payload or None when unavailable. Quarter format: '2026Q1'. When None,
    cycles through the last 4 quarters until a populated transcript lands."""
    if not config.ALPHA_VANTAGE_KEY:
        logger.warning("event_intel: ALPHA_VANTAGE_KEY not configured")
        return None
    quarters_to_try = [quarter] if quarter else _recent_quarters(4)
    last_info = None
    import av_budget
    for q in quarters_to_try:
        # Share the 25/day Alpha Vantage pool with fundamentals + PDF fallback.
        if not av_budget.try_spend(1):
            # Carry forward the reason AV gave us earlier today, so callers can
            # still tell "throttled" from "this company has no transcript"
            # without re-hitting a provider we know is refusing.
            last_info = av_budget.blocked() or last_info
            logger.warning(f"event_intel: AV unavailable — skipping transcript {ticker} {q}")
            break
        params = {
            "function": "EARNINGS_CALL_TRANSCRIPT",
            "symbol":   ticker.upper(),
            "quarter":  q,
            "apikey":   config.ALPHA_VANTAGE_KEY,
        }
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(_AV_BASE, params=params)
                if r.status_code != 200:
                    logger.warning(f"event_intel: AV transcript {ticker} {q} HTTP {r.status_code}")
                    continue
                data = r.json()
        except Exception as exc:
            logger.warning(f"event_intel: AV transcript {ticker} {q} failed: {exc}")
            continue
        # Detect rate-limit message and abort the loop (no point trying more).
        # Trip the shared breaker too: the cap is per-key across every process,
        # and EARNINGS_CALL_TRANSCRIPT is premium-only, so on a free key this
        # notice comes back for every ticker. Without the breaker each concall
        # load burned four more calls out of the 25/day pool that fundamentals
        # and the PDF fallback also draw on.
        if isinstance(data, dict) and data.get("Information"):
            last_info = data["Information"]
            logger.warning(f"event_intel: AV rate-limited on {ticker} {q}: {last_info[:120]}")
            av_budget.mark_blocked(last_info)
            break
        if isinstance(data, dict) and data.get("transcript"):
            data.setdefault("quarter", q)
            return data
    if last_info:
        # Surface the AV rate-limit reason through the caller chain so the
        # frontend can show a real message instead of generic 'no coverage'.
        return {"error": "rate_limited", "info": last_info}
    return None


# ── 1b. SEC EDGAR fetch (free primary source) ─────────────────────────────

async def _edgar_ticker_to_cik(ticker: str) -> str | None:
    """Resolve a ticker to a zero-padded 10-digit CIK using the SEC's
    public ticker map. Cached in-process after first hit."""
    global _EDGAR_TICKER_MAP
    sym = ticker.upper()
    if _EDGAR_TICKER_MAP is None:
        try:
            async with httpx.AsyncClient(timeout=10, headers=_EDGAR_HEADERS) as c:
                r = await c.get("https://www.sec.gov/files/company_tickers.json")
                r.raise_for_status()
                raw = r.json()
            # The JSON is keyed by integer strings: { "0": {cik_str, ticker, title}, ... }
            _EDGAR_TICKER_MAP = {
                str(v.get("ticker", "")).upper(): str(v.get("cik_str", "")).zfill(10)
                for v in raw.values()
            }
            logger.info(f"event_intel: loaded EDGAR ticker map ({len(_EDGAR_TICKER_MAP)} entries)")
        except Exception as exc:
            logger.warning(f"event_intel: EDGAR ticker map fetch failed: {exc}")
            return None
    return _EDGAR_TICKER_MAP.get(sym)


async def _strip_html_async(html: str, limit: int = 60000) -> str:
    """Thread-offloaded _strip_html. The regex pass over a 100k+ char filing is
    CPU-bound and blocks the event loop long enough to time out concurrent
    requests (the 3.4MB /app render started returning 502s while the Fact
    Check pre-warm was chewing through filings)."""
    import asyncio
    return await asyncio.to_thread(_strip_html, html, limit)


def _strip_html(html: str, limit: int = 60000) -> str:
    """Crude HTML-to-text strip. EDGAR filings are dense — we just need
    enough clean prose for Haiku. Drops <script>/<style>, collapses
    whitespace, decodes basic entities."""
    import re
    from html import unescape
    txt = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = unescape(txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:limit]


async def _fetch_edgar_recent(ticker: str) -> dict | None:
    """Pull the most recent earnings-bearing filings from EDGAR.

    Strategy: fetch the last ~40 filings, pick the most recent 8-K with an
    earnings press release (Item 2.02) AND the most recent 10-Q for MD&A.
    Returns dict with {event_date, source_url, source_label, text} or None.
    """
    cik = await _edgar_ticker_to_cik(ticker)
    if not cik:
        return None
    sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        async with httpx.AsyncClient(timeout=12, headers=_EDGAR_HEADERS) as c:
            r = await c.get(sub_url)
            r.raise_for_status()
            sub = r.json()
    except Exception as exc:
        logger.warning(f"event_intel: EDGAR submissions {ticker} failed: {exc}")
        return None
    recent = sub.get("filings", {}).get("recent", {}) or {}
    forms     = recent.get("form", []) or []
    dates     = recent.get("filingDate", []) or []
    accs      = recent.get("accessionNumber", []) or []
    primaries = recent.get("primaryDocument", []) or []
    items     = recent.get("items", []) or []

    # Prefer 8-K with Item 2.02 (Results of Operations) — that's the
    # quarterly earnings release. Falls back to any 8-K, then 10-Q.
    # Scan a WIDE window: the recent-filings list is dominated by Form 3/4/5
    # insider filings, so a 40-row window routinely misses the 10-Q entirely
    # and falls through to whatever 8-K happens to be latest (e.g. a CEO
    # compensation award), which answers none of the analyst questions.
    # Bound the search by DATE, not by position. A positional window is fatal
    # for the heaviest filers: JPM's 300 most recent filings are 280 structured
    # -note prospectuses (424B2) and its 10-Q sits at index 6661, so every big
    # bank silently returned "no source". The recent arrays are already parsed
    # (25k entries for JPM) so a full scan costs nothing, and EDGAR returns
    # them newest-first, which lets us stop at the first filing past the cutoff.
    MAX_AGE_DAYS = 500          # a full annual cycle plus slack, so 10-Ks qualify
    _cutoff = (datetime.now(timezone.utc).date() - timedelta(days=MAX_AGE_DAYS)).isoformat()

    def _first(pred):
        for i, form in enumerate(forms):
            d = dates[i] if i < len(dates) else ""
            if d and d < _cutoff:
                break           # newest-first: everything from here on is older still
            if pred(i, form):
                return i
        return None

    def _is_earnings_8k(i, form):
        return form == "8-K" and i < len(items) and "2.02" in (items[i] or "")

    pick_idx, pick_form = None, None
    idx = _first(_is_earnings_8k)
    if idx is not None:
        pick_idx, pick_form = idx, "8-K (earnings)"
    if pick_idx is None:
        idx = _first(lambda i, f: f == "10-Q")
        if idx is not None:
            pick_idx, pick_form = idx, "10-Q"
    if pick_idx is None:
        idx = _first(lambda i, f: f == "10-K")
        if idx is not None:
            pick_idx, pick_form = idx, "10-K"
    if pick_idx is None:
        idx = _first(lambda i, f: f == "8-K")
        if idx is not None:
            pick_idx, pick_form = idx, "8-K"
    if pick_idx is None:
        return None

    # Secondary document: the latest periodic report. Its MD&A and Risk
    # Factors are where "what is management worried about" and order-book /
    # backlog commentary actually live — an 8-K press release rarely covers
    # them. Skipped when the primary pick IS that report.
    second_idx, second_label = None, None
    if pick_form.startswith("8-K"):
        j = _first(lambda i, f: f == "10-Q")
        if j is None:
            j = _first(lambda i, f: f == "10-K")
        if j is not None:
            second_idx = j
            second_label = forms[j]

    acc       = accs[pick_idx].replace("-", "")
    primary   = primaries[pick_idx]
    filing_dt = dates[pick_idx]
    base_dir  = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc}"
    doc_url   = f"{base_dir}/{primary}"

    # For 8-Ks the primaryDocument is the cover sheet (mostly boilerplate).
    # The actual earnings press release is usually Exhibit 99.1. Pull the
    # filing index and concatenate the cover + any ex-99* exhibit so Haiku
    # has the real content.
    html_blobs: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=20, headers=_EDGAR_HEADERS) as c:
            r = await c.get(doc_url)
            if r.status_code == 200:
                html_blobs.append(r.text)
            # For 8-Ks, the primaryDocument is just the cover sheet. The
            # actual earnings press release / CFO commentary lives in
            # sibling .htm files. Some filers use ex99*.htm (the canonical
            # exhibit naming); others (e.g. NVDA) use custom names like
            # q1fy27pr.htm. Filter by size + extension + exclusions to
            # capture the real content regardless of naming convention.
            if pick_form.startswith("8-K"):
                try:
                    idx_r = await c.get(f"{base_dir}/index.json")
                    if idx_r.status_code == 200:
                        idx_data = idx_r.json() or {}
                        items_list = (idx_data.get("directory") or {}).get("item") or []
                        primary_lower = (primary or "").lower()
                        candidates = []
                        for it in items_list:
                            name = (it.get("name") or "")
                            nlow = name.lower()
                            try:
                                sz = int(it.get("size") or 0)
                            except (TypeError, ValueError):
                                sz = 0
                            if not nlow.endswith((".htm", ".html")):
                                continue
                            if nlow == primary_lower:
                                continue
                            # Skip XBRL viewer reports (R1.htm, R2.htm, …)
                            # and the auto-generated index file
                            if nlow.startswith("r") and nlow[1:nlow.index(".")].isdigit():
                                continue
                            if "index" in nlow:
                                continue
                            # Filings have a lot of tiny boilerplate .htm
                            # files — anything >20KB is meaningful prose.
                            if sz < 20000:
                                continue
                            candidates.append((sz, name))
                        # Largest first — press release usually dwarfs other
                        # exhibits. Pull up to 2 of them.
                        candidates.sort(reverse=True)
                        for _, name in candidates[:2]:
                            ex_r = await c.get(f"{base_dir}/{name}")
                            if ex_r.status_code == 200:
                                html_blobs.append(ex_r.text)
                except Exception as exc:
                    logger.debug(f"event_intel: EDGAR index {ticker} skipped: {exc}")
    except Exception as exc:
        logger.warning(f"event_intel: EDGAR doc {ticker} {doc_url} failed: {exc}")
        return None
    if not html_blobs:
        return None
    import asyncio as _aio
    _parts = await _aio.gather(*[_strip_html_async(b, 40000) for b in html_blobs]) if html_blobs else []
    text = " ".join(_parts)[:90000]

    # Append the periodic report (10-Q/10-K). MD&A and Risk Factors are where
    # backlog/order-book commentary and "what management is worried about"
    # are actually disclosed — an 8-K press release almost never covers them.
    if second_idx is not None:
        try:
            s_acc  = accs[second_idx].replace("-", "")
            s_prim = primaries[second_idx]
            s_url  = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{s_acc}/{s_prim}"
            async with httpx.AsyncClient(timeout=25, headers=_EDGAR_HEADERS) as c:
                rq = await c.get(s_url)
            if rq.status_code == 200:
                extra = await _strip_html_async(rq.text, 120000)
                # Hoist Risk Factors / MD&A to the FRONT of the appended text.
                # Providers with a small context (Groq ≈24k chars) otherwise
                # truncate before reaching them — which is exactly why "what is
                # management worried about" kept coming back unanswered.
                try:
                    import re as _re2
                    picks = []
                    for pat in (r"Item\s*1A[.\s—-]*Risk\s*Factors",
                                r"\bRisk\s*Factors\b",
                                r"Management[’'`s]*\s*Discussion\s*and\s*Analysis"):
                        m = _re2.search(pat, extra, _re2.I)
                        if m:
                            picks.append(extra[m.start():m.start() + 30000])
                        if len(picks) >= 2:
                            break
                    if picks:
                        extra = "\n\n".join(picks) + "\n\n" + extra[:30000]
                except Exception:
                    pass
                if len(extra) > 500:
                    header = (
                        "\n\n=== From the most recent " + str(second_label)
                        + " (filed " + str(dates[second_idx])
                        + ") — MD&A / Risk Factors ===\n"
                    )
                    # Budget the two documents so BOTH survive a small-context
                    # provider (Groq ≈24k chars). Without this the earnings
                    # release fills the window and the Risk Factors — the only
                    # place "what management is worried about" is disclosed —
                    # get truncated away. Gemini (~1M context) ignores this cap.
                    text = text[:9000] + header + extra[:13000] + "\n\n" + extra[13000:]
                    pick_form = f"{pick_form} + {second_label}"
        except Exception as exc:
            logger.warning(f"event_intel: EDGAR secondary doc {ticker} failed: {exc}")

    if len(text) < 500:
        return None
    return {
        "event_date":   filing_dt,
        "source_url":   doc_url,
        "source_label": pick_form,
        "text":         text,
    }


# ── 2. Summarization (Anthropic Haiku) ────────────────────────────────────

_SUMMARY_PROMPT = """You are writing a FAST RESULTS BRIEFING — a quantitative, skimmable
"catch me up in 30 seconds" digest of what this company just REPORTED. The
input is usually an 8-K earnings press release or a 10-Q (the filed numbers).

OBJECTIVE: lead with the hard reported results and what changed this period —
revenue, EPS, margins, growth rates, beats/misses vs expectations, and the
headline guidance figures. This is the NUMBERS view; a separate "Concall
Summary" feature covers the management narrative, so stay results-first and
tight here rather than retelling the story.

Be specific with numbers, quote exact figures and percentages from the source.
Keep every bullet dense and decision-relevant — no filler. Do NOT make up data.
If the source doesn't cover something, omit that section.

Generate 3-5 SECTIONS with DYNAMIC headings that reflect the actual content
of this specific event. Typical headings might be:
  - "Industry trends and demand drivers"
  - "Technology innovation and roadmap"
  - "Financial performance and outlook"
  - "Capacity expansion and manufacturing"
  - "Product roadmap"
  - "Capital allocation"
  - "Risks and headwinds"
  - "Customer and segment dynamics"
But choose what fits THIS event. A semiconductor company's tech-conference
talk will have different headings than a bank's earnings call.

Return ONLY a JSON object (no prose, no markdown fences):

{{
  "event_title":   "Short descriptive title (e.g. 'J.P. Morgan 54th Annual Global Technology Conference summary' or 'Q1 2026 earnings call')",
  "event_date":    "YYYY-MM-DD",
  "sections": [
    {{
      "heading": "Section heading (4-7 words, sentence case)",
      "bullets": ["3-5 bullets per section, each 15-35 words"]
    }},
    ...
  ],
  "raw_excerpt": "One verbatim paragraph (50-80 words) from the transcript that best captures the executive's key message"
}}

Style each bullet like the Quartr / Bloomberg pattern: combine the metric WITH
its driver / comparison inline:
  "Revenue and margin predictability have improved due to build-to-order and
   strong demand, with gross margins approaching 50%."
NOT split sentences. Each bullet self-contained.

Source ({source_label}, ticker: {ticker}, date: {quarter}):

{transcript_text}
"""


_GROQ_KEY      = os.environ.get("GROQ_API_KEY", "").strip()
_GROQ_MODEL    = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

# Breadcrumb of the last summarization failure, surfaced by /api/event-intel-status.
# Without this, a failing provider is invisible: the row just caches empty and
# the UI shows "not available" with no way to tell which provider broke or why.
LAST_ERROR: dict = {}


def _note_error(provider: str, detail: str, ticker: str = "") -> None:
    LAST_ERROR.clear()
    LAST_ERROR.update({"provider": provider, "detail": str(detail)[:200],
                       "ticker": ticker,
                       "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    return None


# ── Management Q&A (Groq-powered, free tier) ─────────────────────────────
# The five questions the numbers can't answer. Answered STRICTLY from the
# filing/transcript text — the model is told to say so when the source is
# silent rather than reason from general knowledge, because an invented
# answer about a real company is worse than no answer.
_QA_QUESTIONS = [
    "Is demand actually improving?",
    "Are margins sustainable or temporary?",
    "Is the order book healthy?",
    "What is management worried about that isn't visible in the P&L?",
    "Are they confident, or just optimistic?",
]

# The hard questions a professional actually has to answer before trusting a
# stock. Deliberately the ones a metric alone cannot settle — each demands the
# model reason across the filing, the numbers and recent coverage.
_DD_QUESTIONS = [
    "What has to be true for this investment to work from here?",
    "Is growth accelerating or decelerating — and is it volume, price, or acquisition?",
    "Is reported profit turning into real cash, or is it accounting?",
    "Where does the competitive advantage come from, and is it widening or eroding?",
    "How concentrated is the business — customers, products, or a single end-market?",
    "Can it fund its own growth, or does it need more debt or equity?",
    "What does today's valuation already assume?",
    "What is the strongest bear case, and what would confirm it first?",
]

_DD_PROMPT = """You are a senior equity analyst writing the internal note a
portfolio manager reads before committing capital to {ticker}.

Answer each question below with genuine analysis — not a restatement of the
metric. A useful answer connects two facts ("margins rose, but only because of
a one-off tariff refund"), names the mechanism, and says what it implies.

Rules:
- Ground every answer in the DATA, the FILING or the WEB RESULTS below. Quote
  the number or phrase you relied on.
- Say "Insufficient evidence" when the sources genuinely do not support an
  answer. Never invent, never fall back on general knowledge of the company.
- 2-3 sentences each. No filler, no restating the question.
- Set "verdict" to one of: strength, concern, mixed, unclear.
- Be neutral: no buy/sell/hold advice, no price predictions, no recommendation.

Return ONLY JSON:
{{
  "checkpoints": [
    {{"q": "<question verbatim>", "verdict": "strength",
      "a": "<2-3 sentences of real analysis with evidence>"}}
  ]
}}

Questions:
{questions}

=== REPORTED METRICS ===
{metrics}

=== COMPANY FILING ({source_label}, {quarter}) ===
{text}

=== RECENT WEB RESULTS ===
{web}
"""


_QA_PROMPT = """You are an equity analyst answering questions about {ticker}.

You have TWO sources:
  (A) THE COMPANY FILING ({source_label}, {quarter}) — primary, most reliable.
  (B) RECENT WEB RESULTS — secondary. Use ONLY for what the filing omits
      (management commentary on the call, analyst reaction, order-book or
      demand colour, concerns raised since the filing).

Rules — follow exactly:
- Prefer the filing. Fall back to the web results only when the filing is silent.
- Quote a number or a short phrase as evidence in every answer.
- Set "source" to "filing" or "web" depending on where the evidence came from.
- Only if NEITHER source addresses a question, set "answered": false and say
  "Neither the filing nor recent coverage addresses this." Never speculate and
  never rely on your own background knowledge of the company.
- Lead with a direct verdict word (Yes / No / Partly / Unclear), then one or two
  sentences of specifics.
- Be neutral and factual. Do NOT tell the reader to buy, sell or hold, do not
  give investment advice, and do not predict prices.

Return ONLY a JSON object:
{{
  "answers": [
    {{"q": "<the question, copied verbatim>", "answered": true,
      "source": "filing", "a": "<2 sentences max, with evidence>"}}
  ]
}}

Questions, in order:
{questions}

=== (A) COMPANY FILING ===
{text}

=== (B) RECENT WEB RESULTS ===
{web}
"""


async def answer_questions(ticker: str, text: str, quarter: str = "",
                           source_label: str = "filing", web: str = "") -> list | None:
    """Answer the fixed analyst question set from source text using the FREE
    provider chain (Groq / Gemini / Cerebras / OpenRouter — see llm_free).
    Gemini is preferred when configured: its ~1M-token context reads a whole
    10-Q/10-K without the truncation that makes questions look unanswerable.
    Returns a list of {q, answered, a} or None when every provider fails."""
    import llm_free
    prompt = _QA_PROMPT.format(
        ticker=ticker, quarter=quarter or "latest", source_label=source_label,
        questions=chr(10).join(f"{i+1}. {q}" for i, q in enumerate(_QA_QUESTIONS)),
        text=(text or ""),
        web=(web or "(no web results available)"),
    )
    parsed = await llm_free.chat_json(prompt, max_tokens=1600, timeout=45.0,
                                      prefer="gemini")
    if not isinstance(parsed, dict):
        errs = getattr(llm_free, "LAST_ERRORS", [])
        _note_error((errs[0]["provider"] if errs else "llm_free"),
                    (errs[0]["detail"] if errs else "no provider returned JSON"), ticker)
        return None
    out = []
    for item in (parsed.get("answers") or []):
        if not isinstance(item, dict):
            continue
        q = str(item.get("q") or "").strip()
        a = str(item.get("a") or "").strip()
        if not q:
            continue
        src = str(item.get("source") or "").strip().lower()
        # Models sometimes return answered=true while the text itself says the
        # sources are silent. Trust the text — otherwise the "N of 5 answered"
        # tally overstates what we actually know.
        al = a.lower()
        disclaimed = ("does not address" in al or "neither the filing" in al
                      or "not addressed" in al or "no information" in al)
        out.append({"q": q[:200], "a": a[:600],
                    "source": src if src in ("filing", "web") else "filing",
                    "answered": bool(item.get("answered")) and bool(a) and not disclaimed})
    return out or None


async def answer_checkpoints(ticker: str, text: str, metrics: str = "",
                             quarter: str = "", source_label: str = "filing",
                             web: str = "") -> list | None:
    """Answer the hard due-diligence questions with real analysis, grounded in
    the filing + reported metrics + recent web coverage."""
    import llm_free
    prompt = _DD_PROMPT.format(
        ticker=ticker, quarter=quarter or "latest", source_label=source_label,
        questions=chr(10).join(f"{i+1}. {q}" for i, q in enumerate(_DD_QUESTIONS)),
        metrics=metrics or "(no metric summary supplied)",
        text=(text or ""), web=(web or "(no web results available)"),
    )
    parsed = await llm_free.chat_json(prompt, max_tokens=2200, timeout=90.0,
                                      prefer="gemini")
    if not isinstance(parsed, dict):
        return None
    out = []
    for item in (parsed.get("checkpoints") or []):
        if not isinstance(item, dict):
            continue
        q = str(item.get("q") or "").strip()
        a = str(item.get("a") or "").strip()
        if not q or not a:
            continue
        v = str(item.get("verdict") or "").strip().lower()
        al = a.lower()
        if "insufficient evidence" in al or "not enough" in al:
            v = "unclear"
        out.append({"q": q[:220], "a": a[:900],
                    "verdict": v if v in ("strength", "concern", "mixed", "unclear") else "mixed"})
    return out or None


async def get_management_qa(ticker: str, force: bool = False,
                            metrics: str = "") -> dict:
    """Cached management Q&A for a ticker. Uses the same EDGAR filing the
    summary is built from, answered by Groq. Cached 14 days in the KV store
    (no DB migration needed). Degrades to {available: false, reason}."""
    sym = ticker.upper()
    try:
        import kv_store
        cache = kv_store.store
    except Exception:
        cache = None
    if cache and not force:
        hit = cache.get("mgmt_qa", sym, max_age_s=_CACHE_TTL_DAYS * 86400)
        if hit and hit.get("answers") and hit.get("checkpoints"):
            return hit
    edgar = await _fetch_edgar_recent(sym)
    if not edgar or not edgar.get("text"):
        return {"available": False, "reason": "no_source",
                "ticker": sym, "answers": []}
    # Web grounding: fills the gaps a filing structurally cannot cover — the
    # earnings-call Q&A, analyst reaction, demand/order-book colour. Free via
    # Serper/Brave. Absent keys or failures just mean filing-only answers.
    web_ctx = ""
    try:
        import web_search
        if web_search.available():
            hits = []
            for q in (f"{sym} stock latest earnings call management commentary demand guidance",
                      f"{sym} stock analyst reaction risks concerns outlook"):
                hits += await web_search.search(q, count=5)
            web_ctx = web_search.as_context(hits, limit=10)
    except Exception as exc:
        logger.warning(f"event_intel: web grounding {sym} failed: {exc}")

    answers = await answer_questions(sym, edgar["text"],
                                     quarter=edgar.get("event_date", ""),
                                     source_label=edgar.get("source_label", "filing"),
                                     web=web_ctx)
    if not answers:
        return {"available": False, "reason": "summarizer_failed",
                "ticker": sym, "answers": [],
                "last_error": LAST_ERROR or None}
    checkpoints = await answer_checkpoints(
        sym, edgar["text"], metrics=metrics,
        quarter=edgar.get("event_date", ""),
        source_label=edgar.get("source_label", "filing"), web=web_ctx)
    out = {"available": True, "ticker": sym, "answers": answers,
           "checkpoints": checkpoints or [],
           "web_grounded": bool(web_ctx),
           "source": f"sec_edgar:{edgar.get('source_label','')}",
           "source_url": edgar.get("source_url"),
           "event_date": edgar.get("event_date"),
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if cache:
        try:
            cache.set("mgmt_qa", sym, out)
        except Exception:
            pass
    return out


# ── Thesis audit ─────────────────────────────────────────────────────────
# A structured pass/fail audit, designed for the business being audited rather
# than run off a fixed template: the model first picks the four areas that
# actually decide this company's thesis (a memory maker gets supply/capacity,
# a bank gets credit quality), then runs two named checks inside each and cites
# the source for every finding.
_AUDIT_PROMPT = """You are a senior equity analyst running a structured
due-diligence AUDIT on {ticker} ({company}), sector: {sector}.

STEP 1 — pick exactly 4 audit areas that genuinely decide whether this
company's thesis holds. Design them for THIS business; do not reuse a generic
template. A memory maker is audited on supply/capacity and the pricing cycle,
a bank on credit quality and reserve adequacy, a biotech on trial readouts and
cash runway, a retailer on like-for-like sales and inventory, a software
company on net retention and remaining performance obligation. The 4th area
MUST cover valuation and market sentiment.

STEP 2 — inside each area run exactly 2 named checks. For each check give:
  - "name": the specific thing tested, 2-4 words ("HBM allocation",
    "Customer concentration", "Reserve coverage").
  - "verdict": "pass", "fail" or "mixed".
  - "finding": 1-2 sentences of real analysis. Quote the number or phrase you
    relied on. No filler, no restating the check name.
  - "cites": the ids of the sources you used, e.g. ["F"] or ["W2","W5"].

STEP 3 — give each area a "verdict" (pass / fail / mixed) and a
"verdict_note" of at most 6 words qualifying it ("high geographic
concentration", "cycle-dependent", "").

Rules — follow exactly:
- Ground EVERY finding in the REPORTED METRICS, the FILING or the WEB RESULTS
  below, and quote that evidence.
- Source ids: "F" is the company filing (also cite "F" for anything taken from
  the reported metrics); "W1".."Wn" are the numbered web results. Cite only ids
  that appear below. Never cite a source you did not use. "cites" must never be
  empty — every check names where its evidence came from.
- The filing is silent on how the market is positioned, so anything about
  sentiment, short interest, the bear case, analyst reaction or a competitor's
  actions MUST come from the web results and cite the "W" id it came from. Use
  the web results for the valuation-and-sentiment area in particular; if they
  genuinely say nothing on a point, mark that check "mixed" and say the recent
  coverage does not settle it.
- If the sources cannot settle a check, use "mixed" and say plainly what is
  missing. NEVER invent a fact, a number, a contract or a counterparty.
- Neutral and factual: no buy / sell / hold, no price targets, no
  recommendation, no prediction of returns.

Return ONLY a JSON object:
{{"audit": [
  {{"area": "<area name>", "verdict": "pass", "verdict_note": "<=6 words",
    "checks": [
      {{"name": "<check name>", "verdict": "pass",
        "finding": "<1-2 sentences with quoted evidence>", "cites": ["F"]}}
    ]}}
]}}

=== REPORTED METRICS ===
{metrics}

=== WEB RESULTS ===
{web}

=== [F] COMPANY FILING ({source_label}, {quarter}) ===
{text}
"""
# NOTE ON BLOCK ORDER: llm_free truncates with prompt[:cap], i.e. from the TAIL.
# The filing is the long block, so it goes LAST — with the web results last
# instead, any provider with a smaller context window silently dropped the whole
# web section, and the model then correctly reported that the web said nothing
# about sentiment. Same reason _fetch_edgar_recent hoists Risk Factors/MD&A to
# the front of the filing text. Keep the filing at the bottom.

_AUDIT_VERDICTS = ("pass", "fail", "mixed")

# Domains that must never end up as a footnote on an audit finding. Search
# results legitimately surface these, but "analyst sentiment is split [1]"
# pointing at a Facebook post or a YouTube video reads as sourcing an
# investment view to social media. The first live MU run cited both.
_AUDIT_BLOCKED_DOMAINS = (
    "youtube.com", "youtu.be", "facebook.com", "instagram.com", "tiktok.com",
    "twitter.com", "x.com", "reddit.com", "pinterest.com", "quora.com",
    "linkedin.com", "stocktwits.com",
)


def _audit_usable_hits(hits: list) -> list:
    """Drop social/video results and de-duplicate by URL. Filtering BEFORE the
    context and the citation map are built keeps the "W<n>" indices the model
    sees aligned with the sources we can actually render."""
    out, seen = [], set()
    for h in (hits or []):
        url = (h.get("url") or "").strip()
        if not url or url in seen:
            continue
        host = url.split("//", 1)[-1].split("/", 1)[0].lower()
        if any(host == d or host.endswith("." + d) for d in _AUDIT_BLOCKED_DOMAINS):
            continue
        seen.add(url)
        out.append(h)
    return out


def _audit_sources(cites, srcmap: dict) -> list:
    """Resolve the model's source ids into renderable links, dropping any id it
    invented. Order is preserved and duplicates collapsed so a check that cites
    the same page twice renders one footnote."""
    out, seen = [], set()
    for cid in (cites or []):
        key = str(cid or "").strip().upper()
        src = srcmap.get(key)
        if not src or key in seen:
            continue
        seen.add(key)
        out.append(src)
    return out[:6]


async def audit_thesis(ticker: str, text: str, metrics: str = "",
                       quarter: str = "", source_label: str = "filing",
                       web: str = "", srcmap: dict | None = None,
                       company: str = "", sector: str = "") -> list | None:
    """Run the structured audit. Returns a list of areas, each with its own
    verdict and 2 cited checks, or None when every provider fails."""
    import llm_free
    prompt = _AUDIT_PROMPT.format(
        ticker=ticker, company=company or ticker, sector=sector or "unknown",
        quarter=quarter or "latest", source_label=source_label,
        metrics=metrics or "(no metric summary supplied)",
        text=(text or ""), web=(web or "(no web results available)"),
    )
    parsed = await llm_free.chat_json(prompt, max_tokens=2600, timeout=90.0,
                                      prefer="gemini")
    if not isinstance(parsed, dict):
        errs = getattr(llm_free, "LAST_ERRORS", [])
        _note_error((errs[0]["provider"] if errs else "llm_free"),
                    (errs[0]["detail"] if errs else "no provider returned JSON"), ticker)
        return None
    srcmap = srcmap or {}
    areas = []
    for item in (parsed.get("audit") or []):
        if not isinstance(item, dict):
            continue
        area = str(item.get("area") or "").strip()
        if not area:
            continue
        checks = []
        for c in (item.get("checks") or []):
            if not isinstance(c, dict):
                continue
            name = str(c.get("name") or "").strip()
            finding = str(c.get("finding") or "").strip()
            if not name or not finding:
                continue
            cv = str(c.get("verdict") or "").strip().lower()
            checks.append({
                "name": name[:70],
                "verdict": cv if cv in _AUDIT_VERDICTS else "mixed",
                "finding": finding[:700],
                "sources": _audit_sources(c.get("cites"), srcmap),
            })
        if not checks:
            continue
        av = str(item.get("verdict") or "").strip().lower()
        if av not in _AUDIT_VERDICTS:
            # Derive the area verdict from its checks rather than defaulting to
            # a flat "mixed" — a model that omits the field still gets an
            # honest roll-up: any fail -> fail, all pass -> pass.
            vs = {c["verdict"] for c in checks}
            av = "fail" if "fail" in vs else ("pass" if vs == {"pass"} else "mixed")
        areas.append({"area": area[:80], "verdict": av,
                      "verdict_note": str(item.get("verdict_note") or "").strip()[:60],
                      "checks": checks[:3]})
    return areas[:5] or None


async def get_thesis_audit(ticker: str, force: bool = False, metrics: str = "",
                           company: str = "", sector: str = "") -> dict:
    """Cached thesis audit for a ticker, from the company's own latest filing
    plus recent web coverage. Kept in its OWN cache row and behind its own
    endpoint rather than folded into get_management_qa: it is a third LLM call,
    and bundling it would add its latency to every Q&A load and let one
    failure take the other down. Degrades to {available: false, reason}."""
    sym = ticker.upper()
    try:
        import kv_store
        cache = kv_store.store
    except Exception:
        cache = None
    if cache and not force:
        hit = cache.get("thesis_audit", sym, max_age_s=_CACHE_TTL_DAYS * 86400)
        if hit and hit.get("audit"):
            return hit
    edgar = await _fetch_edgar_recent(sym)
    if not edgar or not edgar.get("text"):
        return {"available": False, "reason": "no_source", "ticker": sym, "audit": []}

    # Web grounding + the citation map. The map is built from the SAME slice
    # as_context renders, and with the same 1-based index, so "W3" in the
    # model's answer resolves to the page it actually read.
    web_ctx, srcmap = "", {"F": {"label": edgar.get("source_label") or "Company filing",
                                 "url": edgar.get("source_url") or ""}}
    try:
        import web_search
        if web_search.available():
            hits = []
            for q in (f"{sym} stock supply chain capacity customers demand risks",
                      f"{sym} stock valuation short interest bear case analyst view"):
                hits += await web_search.search(q, count=5)
            hits = _audit_usable_hits(hits)
            web_ctx = web_search.as_context(hits, limit=10)
            for i, r in enumerate(hits[:10], 1):
                srcmap[f"W{i}"] = {"label": (r.get("title") or r.get("url") or "")[:90],
                                   "url": r.get("url") or ""}
    except Exception as exc:
        logger.warning(f"event_intel: audit web grounding {sym} failed: {exc}")

    areas = await audit_thesis(sym, edgar["text"], metrics=metrics,
                               quarter=edgar.get("event_date", ""),
                               source_label=edgar.get("source_label", "filing"),
                               web=web_ctx, srcmap=srcmap,
                               company=company, sector=sector)
    if not areas:
        return {"available": False, "reason": "summarizer_failed", "ticker": sym,
                "audit": [], "last_error": LAST_ERROR or None}
    out = {"available": True, "ticker": sym, "audit": areas,
           "web_grounded": bool(web_ctx),
           # What the audit was allowed to read, so "the coverage doesn't say"
           # can be told apart from "we never gave it any coverage".
           "web_sources": [v for k, v in srcmap.items() if k != "F" and v.get("url")][:10],
           "source": f"sec_edgar:{edgar.get('source_label','')}",
           "source_url": edgar.get("source_url"),
           "event_date": edgar.get("event_date"),
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if cache:
        try:
            cache.set("thesis_audit", sym, out)
        except Exception:
            pass
    return out


async def probe_groq() -> dict:
    """Make a tiny real Groq call and report exactly what comes back.
    Used to tell 'key missing' from 'model rejected' from 'call succeeded'
    without reading server logs."""
    if not _GROQ_KEY:
        return {"ok": False, "reason": "GROQ_API_KEY not set"}
    payload = {
        "model":           _GROQ_MODEL,
        "max_tokens":      64,
        "response_format": {"type": "json_object"},
        "messages":        [{"role": "user",
                             "content": 'Reply with this exact JSON: {"ok": true}'}],
    }
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(_GROQ_ENDPOINT, json=payload,
                             headers={"Authorization": f"Bearer {_GROQ_KEY}",
                                      "Content-Type": "application/json"})
        body = r.text[:300]
        return {"ok": r.status_code == 200, "status": r.status_code,
                "model": _GROQ_MODEL, "body": body}
    except Exception as exc:
        return {"ok": False, "model": _GROQ_MODEL, "exception": str(exc)[:200]}


def diagnostics() -> dict:
    """Non-secret health view: which providers are configured and why the last
    summarization failed. Booleans only — never the key values."""
    return {
        "anthropic_key_present": bool(_ANTHROPIC_KEY),
        "anthropic_model": _ANTHROPIC_MODEL,
        "groq_key_present": bool(_GROQ_KEY),
        "groq_model": _GROQ_MODEL,
        "last_error": LAST_ERROR or None,
    }


def _parse_summary_json(text: str, ticker: str) -> dict | None:
    """Shared JSON extraction for either provider (tolerates ``` fences)."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(f"event_intel: JSON decode failed for {ticker}: {exc}; head: {text[:200]}")
        return _note_error("parse", f"JSON decode failed: {text[:120]}", ticker)


async def _summarize_with_groq(ticker: str, quarter: str, transcript_text: str,
                               source_label: str = "earnings call transcript") -> dict | None:
    """Groq (Llama) fallback for the structured call summary.

    Used when Anthropic is unavailable — most commonly an exhausted credit
    balance, which otherwise leaves the whole "what management said" surface
    silently empty. Same prompt, same JSON shape, different provider/key."""
    if not _GROQ_KEY:
        _note_error("groq", "GROQ_API_KEY not set", ticker)
        return None
    prompt = _SUMMARY_PROMPT.format(
        ticker=ticker, quarter=quarter, transcript_text=transcript_text[:24000],
        source_label=source_label,
    )
    payload = {
        "model":           _GROQ_MODEL,
        "max_tokens":      1500,
        "temperature":     0.2,
        "response_format": {"type": "json_object"},
        "messages":        [{"role": "user", "content": prompt}],
    }
    try:
        async with httpx.AsyncClient(timeout=_ANTHROPIC_TIMEOUT) as c:
            r = await c.post(_GROQ_ENDPOINT, json=payload,
                             headers={"Authorization": f"Bearer {_GROQ_KEY}",
                                      "Content-Type": "application/json"})
            if r.status_code != 200:
                logger.warning(f"event_intel: Groq {ticker} HTTP {r.status_code}: {r.text[:200]}")
                return _note_error("groq", f"HTTP {r.status_code}: {r.text[:160]}", ticker)
            resp = r.json()
    except Exception as exc:
        logger.warning(f"event_intel: Groq {ticker} failed: {exc}")
        return _note_error("groq", f"exception: {exc}", ticker)
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    parsed = _parse_summary_json(text, ticker)
    if parsed:
        logger.info(f"event_intel: {ticker} summarized via Groq fallback")
    return parsed


async def _summarize_with_haiku(ticker: str, quarter: str, transcript_text: str,
                                source_label: str = "earnings call transcript") -> dict | None:
    """Structured summary of the call. Tries Anthropic Haiku, then falls back
    to Groq so an exhausted Anthropic balance doesn't blank the feature.
    Returns dict on success, None when every provider fails."""
    # Free providers first: the Anthropic balance is exhausted and every call
    # to it costs a round-trip and returns a 400. Anthropic stays as a last
    # resort so the path still works if the balance is topped up later.
    import llm_free
    if llm_free.available():
        prompt_free = _SUMMARY_PROMPT.format(
            ticker=ticker, quarter=quarter,
            transcript_text=transcript_text, source_label=source_label,
        )
        parsed_free = await llm_free.chat_json(prompt_free, max_tokens=1500,
                                               timeout=45.0, prefer="gemini")
        if isinstance(parsed_free, dict):
            return parsed_free
    if not _ANTHROPIC_KEY:
        return await _summarize_with_groq(ticker, quarter, transcript_text, source_label)
    # Truncate the transcript so we stay under Haiku's context comfortably.
    # Haiku 4.5 supports 200k tokens but cost scales linearly. 30k chars ≈
    # 7-8k tokens — plenty of context, very cheap.
    transcript_trimmed = transcript_text[:30000]
    prompt = _SUMMARY_PROMPT.format(
        ticker=ticker, quarter=quarter, transcript_text=transcript_trimmed,
        source_label=source_label,
    )
    payload = {
        "model":      _ANTHROPIC_MODEL,
        "max_tokens": 1500,
        "messages":   [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key":         _ANTHROPIC_KEY,
        "anthropic-version": "2023-06-01",
        "content-type":      "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=_ANTHROPIC_TIMEOUT) as c:
            r = await anthropic_shim.post(
                             json=payload, headers=headers)
            if r.status_code != 200:
                # Covers the "credit balance too low" 400 as well as 429s —
                # fall through to Groq rather than returning nothing.
                logger.warning(f"event_intel: Anthropic {ticker} HTTP {r.status_code}: {r.text[:200]}")
                _note_error("anthropic", f"HTTP {r.status_code}: {r.text[:160]}", ticker)
                return await _summarize_with_groq(ticker, quarter, transcript_text, source_label)
            resp = r.json()
    except Exception as exc:
        logger.warning(f"event_intel: Anthropic {ticker} failed: {exc}")
        _note_error("anthropic", f"exception: {exc}", ticker)
        return await _summarize_with_groq(ticker, quarter, transcript_text, source_label)
    try:
        import usage_log
        usage_log.record("concall", _ANTHROPIC_MODEL, resp.get("usage"), ticker=ticker)
    except Exception:
        pass
    text = (resp.get("content") or [{}])[0].get("text", "").strip()
    parsed = _parse_summary_json(text, ticker)
    if parsed is None:
        return await _summarize_with_groq(ticker, quarter, transcript_text, source_label)
    return parsed


# ── 3. Supabase persistence ───────────────────────────────────────────────

def _supabase_headers() -> dict | None:
    if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
        return None
    return {
        "apikey":        config.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation,resolution=merge-duplicates",
    }


async def _load_cached(ticker: str) -> dict | None:
    """Latest cached summary for a ticker. Returns None if not found or
    Supabase unconfigured. Caller decides whether the row is fresh enough."""
    hdrs = _supabase_headers()
    if not hdrs:
        return None
    base = config.SUPABASE_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"{base}/rest/v1/event_summaries",
                headers=hdrs,
                params={
                    "ticker": f"eq.{ticker.upper()}",
                    "select": "*",
                    "order":  "summarized_at.desc",
                    "limit":  "1",
                },
            )
            r.raise_for_status()
            rows = r.json()
            return rows[0] if rows else None
    except Exception as exc:
        logger.warning(f"event_intel: cache load {ticker} failed: {exc}")
        return None


async def _save_cache(ticker: str, summary: dict) -> bool:
    hdrs = _supabase_headers()
    if not hdrs:
        return False
    base = config.SUPABASE_URL.rstrip("/")
    row = {
        "ticker":        ticker.upper(),
        "event_title":   summary.get("event_title"),
        "event_date":    summary.get("event_date"),
        "source":        summary.get("source", "alpha_vantage_transcript"),
        # New dynamic-sections shape (May 23 v2). Legacy 4-bucket fields
        # left in for backwards compat with older cached rows during the
        # transition; older clients can still read key_updates etc.
        "sections":      summary.get("sections"),
        "key_updates":   summary.get("key_updates"),
        "operations":    summary.get("operations"),
        "outlook":       summary.get("outlook"),
        "risks":         summary.get("risks"),
        "raw_excerpt":   summary.get("raw_excerpt"),
        "summarized_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{base}/rest/v1/event_summaries",
                headers=hdrs,
                json=row,
                params={"on_conflict": "ticker,event_date"},
            )
            if r.status_code >= 400:
                logger.error(f"event_intel: cache save {ticker} → {r.status_code}: {r.text[:200]}")
                return False
            return True
    except Exception as exc:
        logger.warning(f"event_intel: cache save {ticker} failed: {exc}")
        return False


# ── 4. Public API ────────────────────────────────────────────────────────

# Cache TTL — re-fetch + re-summarize after this many days even if a row
# already exists. Tuned to 14 days so a new quarterly call (every ~90 days)
# always triggers a refresh.
_CACHE_TTL_DAYS = 14


async def load_cached_many(tickers: list[str]) -> dict[str, dict]:
    """Cache-ONLY batch read: the newest cached summary per ticker, in ONE query.

    Two things this deliberately is NOT:
      - not a loop over _load_cached(), which costs one Supabase round-trip
        per ticker;
      - not get_event_summary(), which lazily GENERATES on a miss. The events
        timeline asks about every name that just reported, so that version
        would fan out an Alpha Vantage fetch plus a model call per uncached
        row on a single panel load.
    Names with nothing cached are simply absent from the result.
    """
    syms = [(t or "").upper().strip() for t in (tickers or []) if (t or "").strip()]
    syms = list(dict.fromkeys(syms))[:60]           # de-dupe, keep order, cap
    if not syms:
        return {}
    hdrs = _supabase_headers()
    if not hdrs:
        return {}
    base = config.SUPABASE_URL.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"{base}/rest/v1/event_summaries",
                headers=hdrs,
                params={
                    "ticker": "in.(" + ",".join(syms) + ")",
                    "select": "*",
                    "order":  "summarized_at.desc",
                    "limit":  str(len(syms) * 3),    # room for older dupes
                },
            )
            r.raise_for_status()
            rows = r.json() or []
    except Exception as exc:
        _note_error("supabase", f"batch cache read: {exc}")
        return {}
    out: dict[str, dict] = {}
    for row in rows:                     # newest-first, so the first one wins
        sym = (row.get("ticker") or "").upper()
        if sym and sym not in out:
            out[sym] = row
    return out


def takeaway_of(row: dict | None) -> str:
    """The one line worth showing beside a name in a list. Prefers the v2
    "sections" shape, falls back to the legacy bucket fields. Returns an empty
    string when the row carries nothing usable, so callers can skip it."""
    if not row:
        return ""
    for sec in (row.get("sections") or []):
        bullets = (sec or {}).get("bullets") or []
        if bullets:
            return str(bullets[0]).strip()
    for field in ("key_updates", "outlook", "operations", "risks"):
        vals = row.get(field) or []
        if isinstance(vals, list) and vals:
            return str(vals[0]).strip()
    return ""


def factcheck_warm(ticker: str) -> tuple[bool, bool]:
    """(mgmt_qa_warm, thesis_audit_warm) — a cheap cache peek, no network and no
    model call. The pre-warm loop uses it to skip names already cached, so a
    steady-state pass costs seconds instead of sleeping through the whole list."""
    sym = (ticker or "").upper()
    try:
        import kv_store
        cache = kv_store.store
    except Exception:
        return (False, False)
    max_age = _CACHE_TTL_DAYS * 86400
    try:
        qa = cache.get("mgmt_qa", sym, max_age_s=max_age) or {}
        au = cache.get("thesis_audit", sym, max_age_s=max_age) or {}
    except Exception:
        return (False, False)
    return (bool(qa.get("answers") and qa.get("checkpoints")), bool(au.get("audit")))


def _is_stale(row: dict) -> bool:
    ts = row.get("summarized_at")
    if not ts:
        return True
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    age_days = (datetime.now(timezone.utc) - when).total_seconds() / 86400
    return age_days > _CACHE_TTL_DAYS


# "sections" is the current (May 23 v2) shape; the four bucket fields are the
# legacy shape kept for older cached rows. A row is content-ful if EITHER is
# populated — omitting "sections" here made every modern row look empty, which
# forced a needless re-summarize on every request.
_CONTENT_FIELDS = ("sections", "key_updates", "operations", "outlook", "risks")


def _good_source(row: dict | None) -> bool:
    """False for rows summarised from a filing that cannot answer analyst
    questions — e.g. a plain 8-K that turned out to be a CEO compensation
    award or an annual-meeting notice. Those were cached before the source
    picker was fixed; treating them as a miss lets them be rebuilt from the
    earnings 8-K + 10-Q instead of being served forever."""
    if not row:
        return False
    src = (row.get("source") or "").lower()
    if "earnings" in src or "10-q" in src or "10-k" in src or "transcript" in src:
        return True
    title = (row.get("event_title") or "").lower()
    junk = ("performance award", "compensation", "annual meeting", "stockholders",
            "appointment", "resignation", "director", "bylaw", "equity incentive")
    return not any(j in title for j in junk)


def _has_content(row: dict | None) -> bool:
    """True when a cached row actually carries summary content. Rows saved
    after a failed summarization have every list field null/empty and must
    not be treated as a usable cache hit."""
    if not row:
        return False
    return any(row.get(f) for f in _CONTENT_FIELDS)


async def get_event_summary(ticker: str, force_refresh: bool = False) -> dict | None:
    """Main entry. Returns the latest event summary for the ticker, falling
    back to cache when fresh. Returns None when nothing is available
    (e.g., Alpha Vantage doesn't cover the ticker AND no prior cached row).
    """
    sym = ticker.upper()
    if not force_refresh:
        cached = await _load_cached(sym)
        # A row whose summary fields are all empty is a FAILED summarization
        # (e.g. cached while the Anthropic balance was exhausted), not a
        # usable answer. Treat it as a miss so we retry — otherwise the empty
        # row is served forever and the provider fallback never runs.
        if cached and not _is_stale(cached) and _has_content(cached)                 and _good_source(cached):
            return cached

    # ── Primary source: SEC EDGAR (free, unlimited) ───────────────────
    # 8-K Item 2.02 earnings press releases give us the actual numbers and
    # management commentary that Quartr would surface. 10-Q MD&A covers the
    # narrative when no recent 8-K is available. No API key, no rate limit
    # beyond polite use.
    summary = None
    source_tag = None
    edgar = await _fetch_edgar_recent(sym)
    if edgar:
        summary = await _summarize_with_haiku(
            sym, edgar["event_date"], edgar["text"],
            source_label=edgar["source_label"],
        )
        if summary:
            source_tag = f"sec_edgar:{edgar['source_label']}"
            # Stamp the filing URL into raw_excerpt suffix so the UI can
            # show a 'Source: SEC filing' link if it wants. Append softly.
            summary.setdefault("source_url", edgar["source_url"])
            # Override event_date with the actual filing date if Haiku
            # didn't extract one cleanly.
            summary.setdefault("event_date", edgar["event_date"])

    # ── Fallback: Alpha Vantage transcript ─────────────────────────────
    # Only enter this path when EDGAR found NO filing at all (edgar is
    # falsy). If EDGAR fetched a filing but Haiku summarization failed
    # (transient network blip, missing API key, JSON parse fail), we'd
    # rather return stale cache or None than burn an AV call and surface
    # a misleading "rate limited" message — EDGAR succeeding is terminal.
    if not summary and not edgar:
        raw = await _fetch_av_transcript(sym)
        if isinstance(raw, dict) and raw.get("error") == "rate_limited":
            cached = await _load_cached(sym)
            if cached: return cached
            return {"error": "rate_limited", "info": raw.get("info"), "ticker": sym}
        if raw:
            segments = raw.get("transcript", [])
            if segments:
                transcript_text = "\n".join(
                    f"{s.get('speaker','')} ({s.get('title','')}): {s.get('content','')}"
                    for s in segments if s.get("content")
                )
                quarter = raw.get("quarter", "")
                summary = await _summarize_with_haiku(
                    sym, quarter, transcript_text,
                    source_label="earnings call transcript",
                )
                if summary:
                    source_tag = "alpha_vantage_transcript"

    if not summary:
        # Nothing worked. A stale-but-real row beats an error screen, so serve
        # it first — but only if it actually carries content; a row cached
        # during an earlier failed summarization renders as an empty brief.
        stale = await _load_cached(sym)
        if stale and _has_content(stale):
            return stale
        if edgar:
            # We DID find the filing — the SUMMARISER is what failed. Collapsing
            # this into None made the UI say "no 8-K or 10-Q on SEC EDGAR",
            # which is simply false: ACMR and TTWO both filed an 8-K Item 2.02
            # the day before they showed as uncovered. Name the real failure so
            # the reader isn't sent to check a source that was fine.
            return {"error": "summarizer_failed", "ticker": sym,
                    "source_url":   edgar.get("source_url"),
                    "source_label": edgar.get("source_label"),
                    "event_date":   edgar.get("event_date"),
                    "last_error":   LAST_ERROR or None}
        return None

    summary["source"] = source_tag or "sec_edgar"
    # Persist for next time
    await _save_cache(sym, summary)
    # Re-load so the response shape matches what /api/event-intel returns
    # from cache (with summarized_at populated by the DB default).
    fresh = await _load_cached(sym)
    if fresh:
        # Merge non-persisted fields (source_url isn't in the DB schema)
        # so the UI can render a citation link.
        if summary.get("source_url") and not fresh.get("source_url"):
            fresh["source_url"] = summary["source_url"]
        return fresh
    return summary
