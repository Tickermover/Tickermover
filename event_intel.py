"""
AlphaHunt — Event intel (earnings call summarization)
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
from datetime import datetime, timezone

import httpx

import config

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
    "AlphaHunt Research alphahunt-bot@example.com",
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
            logger.warning(f"event_intel: AV daily budget exhausted — skipping transcript {ticker} {q}")
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
        # Detect rate-limit message and abort the loop (no point trying more)
        if isinstance(data, dict) and data.get("Information"):
            last_info = data["Information"]
            logger.warning(f"event_intel: AV rate-limited on {ticker} {q}: {last_info[:120]}")
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
    pick_idx = None
    pick_form = None
    for i, form in enumerate(forms[:40]):
        if form == "8-K" and i < len(items) and "2.02" in (items[i] or ""):
            pick_idx, pick_form = i, "8-K (earnings)"
            break
    if pick_idx is None:
        for i, form in enumerate(forms[:40]):
            if form == "10-Q":
                pick_idx, pick_form = i, "10-Q"
                break
    if pick_idx is None:
        for i, form in enumerate(forms[:40]):
            if form == "8-K":
                pick_idx, pick_form = i, "8-K"
                break
    if pick_idx is None:
        return None

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
    text = " ".join(_strip_html(b, limit=40000) for b in html_blobs)[:90000]
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


async def _summarize_with_haiku(ticker: str, quarter: str, transcript_text: str,
                                source_label: str = "earnings call transcript") -> dict | None:
    """Call Anthropic Haiku to produce the structured summary. Returns dict
    on success, None on failure (e.g. API key missing, rate-limited)."""
    if not _ANTHROPIC_KEY:
        logger.warning("event_intel: ANTHROPIC_API_KEY not set, skipping summarization")
        return None
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
            r = await c.post("https://api.anthropic.com/v1/messages",
                             json=payload, headers=headers)
            if r.status_code != 200:
                logger.warning(f"event_intel: Anthropic {ticker} HTTP {r.status_code}: {r.text[:200]}")
                return None
            resp = r.json()
    except Exception as exc:
        logger.warning(f"event_intel: Anthropic {ticker} failed: {exc}")
        return None
    try:
        import usage_log
        usage_log.record("concall", _ANTHROPIC_MODEL, resp.get("usage"), ticker=ticker)
    except Exception:
        pass
    text = (resp.get("content") or [{}])[0].get("text", "").strip()
    # Strip code fences if Haiku added any
    if text.startswith("```"):
        text = text.strip("`")
        # Drop leading 'json' tag if present
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(f"event_intel: JSON decode failed for {ticker}: {exc}; text head: {text[:200]}")
        return None
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


async def get_event_summary(ticker: str, force_refresh: bool = False) -> dict | None:
    """Main entry. Returns the latest event summary for the ticker, falling
    back to cache when fresh. Returns None when nothing is available
    (e.g., Alpha Vantage doesn't cover the ticker AND no prior cached row).
    """
    sym = ticker.upper()
    if not force_refresh:
        cached = await _load_cached(sym)
        if cached and not _is_stale(cached):
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
        # Nothing worked — return stale cache if we have one
        return await _load_cached(sym)

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
