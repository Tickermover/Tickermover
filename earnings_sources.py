"""
TickerMover - Free Earnings Data Sources

One async fetcher that pull earnings-related text from a genuinely-free
public source, so we can feed that text into Groq for analysis without
needing FMP's Premium plan.

    fetch_earnings_press_release(ticker) -> str
        Pulls the most recent earnings press release from SEC EDGAR.
        Source: 8-K filings (Form 8-K, Item 2.02 = "Results of Operations
        and Financial Condition"), specifically Exhibit 99.1 which by SEC
        rule contains the actual press release text.
        Free, no API key, no rate limit beyond SEC fair-use (10 req/sec).


Both functions return "" on any failure so the caller can short-circuit
cleanly without exception handling.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    _HTTPX_AVAILABLE = False

# SEC requires a User-Agent that identifies the requester (a real email).
# Use a project-specific UA so they can contact us if we misbehave.
_SEC_UA = os.environ.get(
    "SEC_USER_AGENT",
    "TickerMover research-tool support@tickermover.com"
)
_SEC_TIMEOUT = 12.0
_SEC_BASE    = "https://www.sec.gov"
_SEC_DATA    = "https://data.sec.gov"

# API Ninjas Premium endpoint for earnings call transcripts.
# Free tier does NOT include this endpoint — Premium plan ($9.99/mo) does.
# Module short-circuits to "" when API_NINJAS_KEY is empty (no calls made).
_NINJAS_KEY      = os.environ.get("API_NINJAS_KEY", "").strip()
_NINJAS_ENDPOINT = "https://api.api-ninjas.com/v1/earningstranscript"
_NINJAS_TIMEOUT  = 15.0

# In-process cache for ticker->CIK lookups (loaded once per app start).
# SEC ships the full mapping at https://www.sec.gov/files/company_tickers.json
_TICKER_TO_CIK: dict[str, str] = {}
_TICKER_MAP_LOADED = False


# ── SEC EDGAR helpers ────────────────────────────────────────────────
async def _ensure_ticker_map_loaded() -> None:
    """Lazy-load the SEC ticker->CIK mapping on first use."""
    global _TICKER_TO_CIK, _TICKER_MAP_LOADED
    if _TICKER_MAP_LOADED or not _HTTPX_AVAILABLE:
        return
    try:
        async with httpx.AsyncClient(timeout=_SEC_TIMEOUT) as c:
            r = await c.get(
                f"{_SEC_BASE}/files/company_tickers.json",
                headers={"User-Agent": _SEC_UA},
            )
            r.raise_for_status()
            data = r.json()
        # Format: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        for row in data.values():
            tk  = str(row.get("ticker", "")).upper().strip()
            cik = str(row.get("cik_str", "")).strip()
            if tk and cik:
                _TICKER_TO_CIK[tk] = cik.zfill(10)  # 10-digit padded
        _TICKER_MAP_LOADED = True
        logger.info(f"SEC ticker map loaded: {len(_TICKER_TO_CIK)} entries")
    except Exception as exc:
        logger.warning(f"Failed to load SEC ticker map: {exc}")


async def _ticker_to_cik(ticker: str) -> Optional[str]:
    """Return zero-padded 10-digit CIK for a ticker, or None."""
    await _ensure_ticker_map_loaded()
    return _TICKER_TO_CIK.get(ticker.upper())


async def _fetch_recent_8k_filing_index(ticker: str) -> Optional[dict]:
    """
    Find the most recent 8-K filing for `ticker` that contains an Item 2.02
    (Earnings) trigger. Returns a dict like:
        {"accession": "0001628280-26-...", "primary_doc": "ny20012345x1_8k.htm",
         "filing_date": "2026-04-30"}
    or None if nothing found.
    """
    if not _HTTPX_AVAILABLE:
        return None
    cik = await _ticker_to_cik(ticker)
    if not cik:
        return None

    try:
        async with httpx.AsyncClient(timeout=_SEC_TIMEOUT) as c:
            r = await c.get(
                f"{_SEC_DATA}/submissions/CIK{cik}.json",
                headers={"User-Agent": _SEC_UA},
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.warning(f"SEC submissions fetch failed for {ticker}: {exc}")
        return None

    recent = data.get("filings", {}).get("recent", {})
    forms       = recent.get("form", [])
    accessions  = recent.get("accessionNumber", [])
    docs        = recent.get("primaryDocument", [])
    items       = recent.get("items", [])
    fil_dates   = recent.get("filingDate", [])

    # Walk in reverse-chronological order (SEC returns newest first).
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        # Item code "2.02" = Results of Operations and Financial Condition.
        # `items` is a string like "2.02,9.01" or sometimes empty.
        item_str = items[i] if i < len(items) else ""
        if "2.02" not in item_str:
            continue
        return {
            "accession":   accessions[i].replace("-", ""),
            "primary_doc": docs[i],
            "filing_date": fil_dates[i],
            "cik":         cik,
        }
    return None


async def _fetch_8k_exhibit_text(filing: dict) -> str:
    """
    Fetch the press-release content from an 8-K filing. SEC packages press
    releases as Exhibit 99.1 (an HTML/PDF file in the filing's directory).
    We grab that exhibit's text rather than the 8-K cover document, because
    the cover only contains a one-line reference to the exhibit.
    """
    if not _HTTPX_AVAILABLE or not filing:
        return ""
    cik       = filing["cik"]
    accession = filing["accession"]
    primary   = filing["primary_doc"]

    # Filing directory listing
    base = f"{_SEC_BASE}/Archives/edgar/data/{int(cik)}/{accession}"
    try:
        async with httpx.AsyncClient(timeout=_SEC_TIMEOUT) as c:
            r = await c.get(
                f"{base}/",
                headers={"User-Agent": _SEC_UA},
            )
            r.raise_for_status()
            index_html = r.text
    except Exception as exc:
        logger.warning(f"SEC filing index fetch failed: {exc}")
        return ""

    # Find an Exhibit 99.1 file in the directory listing.
    # Filenames follow patterns like ex991.htm, ex-99_1.htm, exhibit991.htm, etc.
    candidates = re.findall(
        r'href="([^"]*ex(?:hibit)?[-_]?99[-_]?1[^"]*\.(?:htm|html|txt))"',
        index_html,
        flags=re.IGNORECASE,
    )
    def _resolve(href: str) -> str:
        """Resolve a relative or absolute href against the SEC base URL."""
        if href.startswith("http://") or href.startswith("https://"):
            return href
        if href.startswith("/"):
            return f"{_SEC_BASE}{href}"
        return f"{base}/{href}"

    if candidates:
        target_url = _resolve(candidates[0])
    else:
        # Fall back to the 8-K primary document itself.
        target_url = _resolve(primary)

    try:
        async with httpx.AsyncClient(timeout=_SEC_TIMEOUT) as c:
            r = await c.get(
                target_url,
                headers={"User-Agent": _SEC_UA},
            )
            r.raise_for_status()
            html = r.text
    except Exception as exc:
        logger.warning(f"SEC exhibit fetch failed: {exc}")
        return ""

    return _strip_html(html)


def _strip_html(html: str) -> str:
    """
    Extract plain text from SEC HTML. Defensive — SEC filings can contain
    tables, inline styles, embedded XBRL, etc. We strip tags, decode
    entities, collapse whitespace.
    """
    if not html:
        return ""
    # Drop XBRL/script/style blocks
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<style[\s\S]*?</style>",  " ", html, flags=re.IGNORECASE)
    html = re.sub(r"<ix:[^>]*>",                " ", html, flags=re.IGNORECASE)
    html = re.sub(r"</ix:[^>]*>",               " ", html, flags=re.IGNORECASE)
    # Strip tags
    text = re.sub(r"<[^>]+>", " ", html)
    # Common HTML entities
    text = (text.replace("&nbsp;", " ")
                .replace("&amp;",  "&")
                .replace("&lt;",   "<")
                .replace("&gt;",   ">")
                .replace("&quot;", '"')
                .replace("&#39;",  "'")
                .replace("&#160;", " "))
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ── Public ───────────────────────────────────────────────────────────
async def fetch_earnings_press_release(ticker: str) -> dict:
    """
    Fetch the most recent earnings press release for a ticker, returning
    BOTH the text and the SEC 8-K filing date (the actual publication date).

    Returns:
        {"text": "<press release body>", "filing_date": "YYYY-MM-DD"}
    or {"text": "", "filing_date": ""} on any failure.

    Using the filing date here lets the UI show users a date that's always
    fresh (vs FMP's earnings-surprises endpoint which can lag by months).
    """
    if not _HTTPX_AVAILABLE:
        return {"text": "", "filing_date": ""}
    filing = await _fetch_recent_8k_filing_index(ticker)
    if not filing:
        return {"text": "", "filing_date": ""}
    text = await _fetch_8k_exhibit_text(filing)
    return {
        "text":         text,
        "filing_date":  filing.get("filing_date", ""),
    }


async def fetch_call_transcript(ticker: str) -> str:
    """
    Fetch the most recent earnings-call transcript text from API Ninjas.
    Returns "" if API_NINJAS_KEY isn't set or the call fails.

    Tries the last 4 quarters in reverse-chronological order to find one
    with a transcript. Logs HTTP errors so the cause is visible in dashboard
    logs (401 unauthorized, 403 premium-required, 429 rate-limited, etc.).
    """
    if not _HTTPX_AVAILABLE:
        return ""
    if not _NINJAS_KEY:
        logger.info("[ninjas] API_NINJAS_KEY not set — sentiment disabled")
        return ""

    today = date.today()
    cur_q = (today.month - 1) // 3 + 1
    candidates = []
    y, q = today.year, cur_q
    for _ in range(4):
        candidates.append((y, q))
        q -= 1
        if q == 0:
            q = 4
            y -= 1

    last_error = None
    for y, q in candidates:
        try:
            async with httpx.AsyncClient(timeout=_NINJAS_TIMEOUT) as c:
                r = await c.get(
                    _NINJAS_ENDPOINT,
                    params={"ticker": ticker.upper(), "year": y, "quarter": q},
                    headers={"X-Api-Key": _NINJAS_KEY},
                )
                if r.status_code == 401:
                    logger.warning("[ninjas] 401 unauthorized — check API_NINJAS_KEY value")
                    return ""
                if r.status_code == 403:
                    logger.warning("[ninjas] 403 forbidden — endpoint requires Premium plan")
                    return ""
                if r.status_code == 429:
                    logger.warning("[ninjas] 429 rate-limited")
                    return ""
                r.raise_for_status()
                data = r.json()
        except Exception as exc:
            last_error = exc
            continue

        if isinstance(data, list) and data:
            data = data[0]
        if isinstance(data, dict):
            transcript = str(data.get("transcript") or "").strip()
            if transcript:
                logger.info(f"[ninjas] {ticker} Q{q} {y} transcript fetched ({len(transcript)} chars)")
                return transcript

    if last_error:
        logger.warning(f"[ninjas] {ticker}: all quarters failed: {last_error}")
    return ""
