"""
operating_kpis.py — Screener.in-style "Insights": company-specific OPERATING
KPIs extracted from annual filings and laid out as a per-fiscal-year table.

These are the *physical business drivers* (volumes, capacity, units sold,
subscribers, deliveries, stores, load factor, ARPU …) — NOT the standard
financial-statement lines (revenue / EPS / margins) that already have their
own panels. They live scattered in 10-K MD&A prose and exhibits, so we let
Claude extract them into structured JSON, then merge across years.

Pipeline (lazy, on-demand per ticker):
    ticker → CIK → last N annual 10-K filings (EDGAR submissions)
           → fetch each filing's text → Haiku extracts {metric, unit, value}
           → merge into a metric × fiscal-year grid, each cell carrying the
             source filing URL so the value is auditable.

Everything degrades gracefully: no ANTHROPIC_API_KEY → "not enabled"; a
ticker with no 10-Ks (foreign/ETF) → empty result with a clear note.

Cost: ~5 Haiku calls (one per annual filing) on first open, then served from
the on-disk cache for 30 days. Re-runs are free.

Env:
  ANTHROPIC_API_KEY   — extraction (shared with the rest of the app)
  ANTHROPIC_MODEL     — default "claude-haiku-4-5-20251001"
  SEC_EDGAR_UA        — descriptive UA for SEC fair-use (shared)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re

import httpx

import event_intel as ei  # reuse CIK resolver, EDGAR headers, HTML stripper
from cache import SmartCache

logger = logging.getLogger(__name__)

_ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_API_KEY", "").strip()
_ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

_MAX_FILINGS  = 5          # how many annual reports back to extract
_DOC_CHARS    = 110000     # text per filing handed to the model
_CACHE_TTL    = 30 * 24 * 3600   # 30 days — refresh roughly per new 10-K
_HTTP_TIMEOUT = 35.0

# Persisted across restarts so we don't pay to re-extract.
_cache = SmartCache(disk_path="output/operating_kpis_cache.json")


def is_enabled() -> bool:
    return bool(_ANTHROPIC_KEY)


# ── 1. Find the last N annual filings ──────────────────────────────────────

async def _annual_filings(ticker: str) -> list[dict]:
    """Return up to _MAX_FILINGS annual reports (10-K / 20-F / 40-F), newest
    first, each as {form, filing_date, report_date, year, url}."""
    cik = await ei._edgar_ticker_to_cik(ticker)
    if not cik:
        return []
    sub_url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        async with httpx.AsyncClient(timeout=12, headers=ei._EDGAR_HEADERS) as c:
            r = await c.get(sub_url)
            r.raise_for_status()
            sub = r.json()
    except Exception as exc:
        logger.warning(f"operating_kpis: submissions {ticker} failed: {exc}")
        return []

    recent      = sub.get("filings", {}).get("recent", {}) or {}
    forms       = recent.get("form", []) or []
    fdates      = recent.get("filingDate", []) or []
    rdates      = recent.get("reportDate", []) or []
    accs        = recent.get("accessionNumber", []) or []
    primaries   = recent.get("primaryDocument", []) or []

    ANNUAL = {"10-K", "20-F", "40-F"}
    out: list[dict] = []
    seen_years: set[str] = set()
    for i, form in enumerate(forms):
        if form not in ANNUAL:          # exact match → skips "10-K/A" amendments
            continue
        report_dt = rdates[i] if i < len(rdates) else ""
        year = (report_dt or fdates[i])[:4]
        if year in seen_years:          # one filing per fiscal year
            continue
        acc     = accs[i].replace("-", "")
        primary = primaries[i]
        url = (f"https://www.sec.gov/Archives/edgar/data/"
               f"{int(cik)}/{acc}/{primary}")
        out.append({
            "form":        form,
            "filing_date": fdates[i],
            "report_date": report_dt,
            "year":        year,
            "label":       _fy_label(report_dt or fdates[i]),
            "url":         url,
        })
        seen_years.add(year)
        if len(out) >= _MAX_FILINGS:
            break
    return out


def _fy_label(date_str: str) -> str:
    """'2025-03-31' → 'FY 2025'. Falls back to the raw year."""
    y = (date_str or "")[:4]
    return f"FY {y}" if y else (date_str or "—")


# ── 2. Fetch one filing's text ─────────────────────────────────────────────

# 10-Ks open with a wall of inline-XBRL hidden facts (P1Y, FASB taxonomy
# URLs, context ids …) before any readable prose. Skip to the first real
# section anchor so the model's budget is spent on business content, not noise.
_BIZ_ANCHOR = re.compile(
    r"(PART\s+I\b|Item\s*1\.?\s+Business|Item\s*1A\.?\s+Risk|"
    r"Management.s\s+Discussion\s+and\s+Analysis)",
    re.IGNORECASE,
)


def _focus(text: str) -> str:
    """Drop the leading XBRL/boilerplate header by starting at the first
    business-section anchor. Falls back to the raw text if none is found."""
    if not text:
        return text
    m = _BIZ_ANCHOR.search(text, 200)   # skip the very top where stray hits live
    return text[m.start():] if m else text


async def _filing_text(url: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT,
                                     headers=ei._EDGAR_HEADERS) as c:
            r = await c.get(url)
            if r.status_code != 200:
                return ""
            # Strip with extra headroom, then focus on the business prose and
            # cap to the model budget.
            raw = ei._strip_html(r.text, limit=_DOC_CHARS + 60000)
            return _focus(raw)[:_DOC_CHARS]
    except Exception as exc:
        logger.warning(f"operating_kpis: filing fetch {url} failed: {exc}")
        return ""


# ── 3. Extract operating KPIs from one filing via Haiku ────────────────────

_EXTRACT_PROMPT = """You are extracting OPERATING KPIs for the US-listed company {ticker}
from its annual report (fiscal year {year}). The text is below.

OPERATING KPIs are the company's PHYSICAL / OPERATIONAL business drivers — the
volumes, units, and capacity behind the financials. Examples by sector:
  - Energy/industrial: production volume, capacity (MW/GW/MMT), utilization
  - Retail: store count, same-store sales %, square footage
  - Airlines/transport: passengers, RPM/ASM, load factor, fleet size
  - Telecom/subscription: subscribers, ARPU, churn %, net adds
  - SaaS: customers, ARR, net revenue retention %, seats
  - Semis/hardware: units shipped, wafers, deliveries, backlog

STRICT RULES:
  - EXCLUDE standard financial-statement lines: revenue, net income, EPS,
    gross/operating margin, cash, debt, dividends. Those have their own report.
  - Include ONLY metrics with a concrete numeric value for THIS fiscal year.
  - Quote the value and unit EXACTLY as stated. Do NOT estimate or invent.
  - Use a clear, stable metric name (e.g. "Solar Module Sales Volume"),
    Title Case, no fiscal-year inside the name.
  - Return at most 12 of the most decision-relevant KPIs.
  - If the text contains no genuine operating KPIs, return an empty list.

Return ONLY a JSON object (no prose, no markdown fences):
{{
  "fiscal_year": "{year}",
  "kpis": [
    {{"metric": "Metric name", "unit": "MW", "value": "4,263"}},
    ...
  ]
}}

=== ANNUAL REPORT TEXT ({form}, fiscal year {year}) ===
{text}
"""


async def _extract_one(ticker: str, filing: dict) -> dict | None:
    if not _ANTHROPIC_KEY:
        return None
    text = await _filing_text(filing["url"])
    if len(text) < 800:
        return None
    prompt = _EXTRACT_PROMPT.format(
        ticker=ticker, year=filing["year"], form=filing["form"],
        text=text[:_DOC_CHARS],
    )
    payload = {"model": _ANTHROPIC_MODEL, "max_tokens": 1400,
               "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": _ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.post("https://api.anthropic.com/v1/messages",
                             json=payload, headers=headers)
            if r.status_code != 200:
                logger.warning(f"operating_kpis: Anthropic {ticker} {filing['year']} "
                               f"HTTP {r.status_code}: {r.text[:160]}")
                return None
            out = (r.json().get("content") or [{}])[0].get("text", "").strip()
    except Exception as exc:
        logger.warning(f"operating_kpis: Anthropic {ticker} {filing['year']}: {exc}")
        return None
    if out.startswith("```"):
        out = out.strip("`")
        if out.lower().startswith("json"):
            out = out[4:].strip()
    try:
        parsed = json.loads(out)
    except json.JSONDecodeError:
        logger.warning(f"operating_kpis: bad JSON for {ticker} {filing['year']}")
        return None
    parsed["_filing"] = filing
    return parsed


# ── 4. Merge per-year extractions into a metric × year grid ────────────────

def _merge(extractions: list[dict]) -> dict:
    """Build {years[], metrics[{name, unit, values{year->val}, sources{year->url}}]}.

    Metric names are matched case-insensitively so the same KPI lines up across
    years even if capitalization wobbles."""
    years_order: list[dict] = []     # [{year, label}] newest-first
    canon: dict[str, dict] = {}      # lowercase-name -> metric row

    for ex in extractions:
        filing = ex.get("_filing", {})
        yr = filing.get("year")
        lbl = filing.get("label") or yr
        url = filing.get("url")
        if yr and yr not in {y["year"] for y in years_order}:
            years_order.append({"year": yr, "label": lbl})
        for kpi in (ex.get("kpis") or []):
            name = (kpi.get("metric") or "").strip()
            val  = kpi.get("value")
            if not name or val in (None, "", "N/A"):
                continue
            key = name.lower()
            row = canon.get(key)
            if not row:
                row = {"name": name, "unit": (kpi.get("unit") or "").strip(),
                       "values": {}, "sources": {}}
                canon[key] = row
            row["values"][yr]  = str(val).strip()
            row["sources"][yr] = url
            if not row["unit"] and kpi.get("unit"):
                row["unit"] = str(kpi["unit"]).strip()

    years_order.sort(key=lambda y: y["year"])   # chronological for display
    # Keep metrics with at least one value; order by how many years they cover
    # (denser series first — they tell the better story).
    metrics = sorted(canon.values(),
                     key=lambda m: (-len(m["values"]), m["name"].lower()))
    return {"years": years_order, "metrics": metrics}


# ── 5. Public API ──────────────────────────────────────────────────────────

async def get_operating_kpis(ticker: str, force: bool = False) -> dict:
    """Main entry. Returns:
       {ok, ticker, beta, years:[{year,label}], metrics:[...], note}"""
    sym = (ticker or "").upper()
    if not sym:
        return {"ok": False, "note": "No ticker.", "metrics": [], "years": []}
    if not is_enabled():
        return {"ok": False, "metrics": [], "years": [],
                "note": "Operating-KPI extraction needs ANTHROPIC_API_KEY."}

    ck = f"opkpi:{sym}"
    if not force:
        hit = _cache.get(ck)
        if hit is not None:
            return hit

    filings = await _annual_filings(sym)
    if not filings:
        res = {"ok": False, "ticker": sym, "beta": True, "metrics": [], "years": [],
               "note": "No annual 10-K/20-F filings found on EDGAR for this ticker."}
        _cache.set(ck, res, 6 * 3600)   # short TTL — coverage may appear later
        return res

    # Extract all years concurrently (each is one EDGAR fetch + one Haiku call).
    extractions = await asyncio.gather(*[_extract_one(sym, f) for f in filings])
    extractions = [e for e in extractions if e]
    grid = _merge(extractions)

    res = {
        "ok": bool(grid["metrics"]),
        "ticker": sym,
        "beta": True,
        "years": grid["years"],
        "metrics": grid["metrics"],
        "note": ("AI-extracted from annual SEC filings — verify against the "
                 "linked source. Coverage varies by company."),
    }
    if not grid["metrics"]:
        res["note"] = ("No distinct operating KPIs were disclosed in this "
                       "company's recent annual filings.")
    _cache.set(ck, res, _CACHE_TTL)
    try:
        _cache.save_disk()
    except Exception:
        pass
    return res
