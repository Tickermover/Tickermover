"""
stock_docs.py — real, dated document lists for US tickers from SEC EDGAR.

EDGAR is free and needs no API key (fair-use ~10 req/s). We read the
submissions feed (data.sec.gov/submissions/CIK##########.json) and split the
recent filings into:

  annual     — 10-K (annual report)        → "FY 2025" etc.
  quarterly  — 10-Q (quarterly report)
  events     — 8-K (material events / earnings releases / investor decks)

Each row carries the filing date and a direct link to the primary document
(plus the filing-index URL for 8-Ks, where EX-99 investor-presentation and
press-release exhibits — often real PDFs — can be downloaded).

US credit-rating PDFs (Moody's/S&P/Fitch) are paywalled and intentionally
NOT faked here.
"""
from __future__ import annotations

import os
import asyncio
import logging
import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_UA = os.environ.get("SEC_EDGAR_UA", "TickerMover research alphahunt@example.com")
_HEADERS = {"User-Agent": _UA, "Accept-Encoding": "gzip, deflate"}


async def _cik(ticker: str) -> str | None:
    try:
        import event_intel as ei
        return await ei._edgar_ticker_to_cik(ticker)
    except Exception as exc:
        logger.warning(f"stock_docs: CIK lookup {ticker} failed: {exc}")
        return None


async def list_documents(ticker: str, limit: int = 10) -> dict:
    empty = {"annual": [], "quarterly": [], "events": [], "cik": None, "fiscal_year_end": None}
    cik = await _cik(ticker)
    if not cik:
        return empty
    cik_int = str(int(cik))
    try:
        async with httpx.AsyncClient(timeout=12, headers=_HEADERS) as c:
            r = await c.get(f"https://data.sec.gov/submissions/CIK{cik}.json")
            r.raise_for_status()
            sub = r.json()
    except Exception as exc:
        logger.warning(f"stock_docs: submissions {ticker} failed: {exc}")
        return empty

    rec   = sub.get("filings", {}).get("recent", {}) or {}
    forms = rec.get("form", []) or []
    dates = rec.get("filingDate", []) or []
    accs  = rec.get("accessionNumber", []) or []
    prim  = rec.get("primaryDocument", []) or []
    items = rec.get("items", []) or []
    descs = rec.get("primaryDocDescription", []) or []
    reps  = rec.get("reportDate", []) or []          # fiscal period-of-report
    fye   = sub.get("fiscalYearEnd")                 # 'MMDD', for fiscal-Q labels

    def doc_url(i):
        acc = (accs[i] if i < len(accs) else "").replace("-", "")
        p = prim[i] if i < len(prim) else ""
        if not acc or not p:
            return f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
        return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{p}"

    def idx_url(i):
        acc = (accs[i] if i < len(accs) else "").replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/"

    def idx_json_url(i):
        acc = (accs[i] if i < len(accs) else "").replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/index.json"

    ANN_CAP, QTR_CAP, EVT_CAP = 4, 4, 6   # last N annual / quarterly / earnings 8-Ks
    annual, quarterly = [], []
    evt_idx = []                          # indices of earnings-related 8-Ks
    for i, f in enumerate(forms):
        d = dates[i] if i < len(dates) else ""
        period = reps[i] if i < len(reps) else ""
        yr = d[:4] if d else ""
        if f in ("10-K", "20-F", "40-F") and len(annual) < ANN_CAP:
            annual.append({"label": f"FY {yr}" if yr else "Annual report",
                           "date": d, "period": period, "url": doc_url(i), "type": f})
        elif f in ("10-Q", "6-K") and len(quarterly) < QTR_CAP:
            quarterly.append({"label": f"Quarter ending {period or d}",
                              "date": d, "period": period, "url": doc_url(i), "type": f})
        elif f == "8-K" and len(evt_idx) < EVT_CAP:
            # Require item 2.02 (Results of Operations) so we only list real
            # quarterly EARNINGS releases — not 7.01-only conference decks or
            # other 8-K events that were polluting the list with wrong quarters.
            it = (items[i] if i < len(items) else "") or ""
            if "2.02" in it:
                evt_idx.append(i)

    events = await _build_events(cik_int, evt_idx, dates, accs, descs, items,
                                 idx_json_url, idx_url, doc_url)
    return {"annual": annual, "quarterly": quarterly, "events": events,
            "cik": cik_int, "fiscal_year_end": fye}


def _classify_row(blob: str, name: str) -> tuple[str, int, bool] | None:
    """Classify one filing-index row by its SEC exhibit Type. `blob` = the row's
    text (includes the Type, e.g. 'EX-99.2', and any description); `name` = the
    document filename. Returns (label, sort-priority, is_pdf) or None to skip."""
    b = (blob or "").lower()
    n = (name or "").lower()
    if not n or any(n.endswith(x) for x in (".jpg", ".jpeg", ".png", ".gif", ".xml",
                                            ".xsd", ".zip", ".json", ".css", ".js", ".txt")):
        return None
    if "index" in n or "filingsummary" in n or "metalinks" in n:
        return None
    if n.startswith("r") and n.endswith(".htm") and n[1:-4].isdigit():   # R1.htm …
        return None
    is_pdf = n.endswith(".pdf")
    if not (n.endswith(".htm") or n.endswith(".html") or is_pdf):
        return None
    suf = " (PDF)" if is_pdf else ""
    has = lambda *ks: any(k in b or k in n for k in ks)
    # CFO commentary first — some filers (e.g. NVDA) file it AS EX-99.2, so this
    # must win before the generic EX-99.2 → slides rule below.
    if has("commentary", "cfo"):
        return ("CFO commentary" + suf, 2, is_pdf)
    # Investor deck — EX-99.2, or anything described/named as a presentation.
    if "ex-99.2" in b or "ex99.2" in b or has("present", "slide", "deck", "investor", "supplement"):
        return ("Investor presentation · slides" + suf, 1, is_pdf)
    # Press release — EX-99.1 or earnings-release wording (e.g. '...991er.htm').
    if "ex-99.1" in b or "ex99.1" in b or has("press", "release", "earnings") \
            or n.endswith("pr.htm") or "991" in n:
        return ("Press release" + suf, 0, is_pdf)
    if "ex-99" in b or "ex99" in n:
        return ("Exhibit 99" + suf, 3, is_pdf)
    if is_pdf:
        return (name + " (PDF)", 4, True)
    return None   # skip the bare 8-K cover body and misc html


async def _build_events(cik_int, evt_idx, dates, accs, descs, items,
                        idx_json_url, idx_url, doc_url) -> list[dict]:
    """For each earnings-related 8-K, read the filing-index page and surface its
    investor exhibits — press release, slide deck, CFO commentary — by their
    authoritative SEC exhibit Type, linked directly. Falls back to the filing
    index page when nothing classifies."""
    if not evt_idx:
        return []

    async def one(i):
        d = dates[i] if i < len(dates) else ""
        acc_dash = accs[i] if i < len(accs) else ""
        acc = acc_dash.replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}"
        rows = []
        try:
            async with httpx.AsyncClient(timeout=12, headers=_HEADERS) as c:
                r = await c.get(f"{base}/{acc_dash}-index.html")
                r.raise_for_status()
                soup = BeautifulSoup(r.text, "lxml")
            seen = set()
            cand = []
            for tr in soup.find_all("tr"):
                a = tr.find("a", href=True)
                if not a:
                    continue
                href = a["href"]
                if "/Archives/" not in href:
                    continue
                name = href.rsplit("/", 1)[-1].split("?")[0]
                cl = _classify_row(tr.get_text(" ", strip=True), name)
                if cl and cl[0] not in seen:
                    seen.add(cl[0])
                    url = href if href.startswith("http") else ("https://www.sec.gov" + href)
                    cand.append((cl[1], {"label": cl[0], "date": d, "url": url,
                                          "type": "8-K", "pdf": cl[2]}))
            cand.sort(key=lambda x: x[0])
            rows = [c for _, c in cand]
        except Exception as exc:
            logger.warning(f"stock_docs: 8-K index {acc} failed: {exc}")
        if not rows:
            rows = [{"label": "8-K — earnings release / event", "date": d,
                     "url": idx_url(i), "type": "8-K", "pdf": False}]
        return rows

    nested = await asyncio.gather(*[one(i) for i in evt_idx], return_exceptions=True)
    out = []
    for grp in nested:
        if isinstance(grp, list):
            out.extend(grp)
    return out
