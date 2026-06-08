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

logger = logging.getLogger(__name__)

_UA = os.environ.get("SEC_EDGAR_UA", "AlphaHunt research alphahunt@example.com")
_HEADERS = {"User-Agent": _UA, "Accept-Encoding": "gzip, deflate"}


async def _cik(ticker: str) -> str | None:
    try:
        import event_intel as ei
        return await ei._edgar_ticker_to_cik(ticker)
    except Exception as exc:
        logger.warning(f"stock_docs: CIK lookup {ticker} failed: {exc}")
        return None


async def list_documents(ticker: str, limit: int = 10) -> dict:
    empty = {"annual": [], "quarterly": [], "events": [], "cik": None}
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
        yr = d[:4] if d else ""
        if f in ("10-K", "20-F", "40-F") and len(annual) < ANN_CAP:
            annual.append({"label": f"FY {yr}" if yr else "Annual report",
                           "date": d, "url": doc_url(i), "type": f})
        elif f in ("10-Q", "6-K") and len(quarterly) < QTR_CAP:
            quarterly.append({"label": f"Quarter ending {d}" if d else "Quarterly",
                              "date": d, "url": doc_url(i), "type": f})
        elif f == "8-K" and len(evt_idx) < EVT_CAP:
            # Item 2.02 = Results of Operations; 7.01 = Reg-FD (investor decks).
            it = (items[i] if i < len(items) else "") or ""
            if "2.02" in it or "7.01" in it:
                evt_idx.append(i)

    events = await _build_events(cik_int, evt_idx, dates, accs, descs, items,
                                 idx_json_url, idx_url, doc_url)
    return {"annual": annual, "quarterly": quarterly, "events": events, "cik": cik_int}


def _classify_exhibit(name: str) -> tuple[str, int, bool] | None:
    """Map an 8-K exhibit filename to (label, sort-priority, is_pdf). Surfaces
    the investor-facing documents — press release, investor deck, CFO commentary
    — linked directly, whether they're HTML or PDF. Returns None for filing
    machinery (XBRL, index pages, stylesheets, images) we don't want to list."""
    n = name.lower()
    # Drop machinery / noise.
    if any(n.endswith(x) for x in (".xsd", ".xml", ".zip", ".json", ".css", ".js",
                                   ".jpg", ".jpeg", ".png", ".gif", ".txt")):
        return None
    if "index" in n or "filingsummary" in n or "metalinks" in n:
        return None
    if n.startswith("r") and n.endswith(".htm") and n[1:-4].isdigit():   # R1.htm, R2.htm …
        return None
    is_pdf = n.endswith(".pdf")
    if not (n.endswith(".htm") or n.endswith(".html") or is_pdf):
        return None
    suf = " (PDF)" if is_pdf else ""
    if any(k in n for k in ("present", "slide", "deck", "investor", "ex992", "ex-992", "99-2", "9902")):
        return ("Investor presentation · slides" + suf, 1, is_pdf)
    if n.endswith("pr.htm") or any(k in n for k in ("press", "release", "earn", "ex991", "ex-991", "99-1", "9901")):
        return ("Press release" + suf, 0, is_pdf)
    if "commentary" in n or "cfo" in n:
        return ("CFO commentary" + suf, 2, is_pdf)
    if "ex99" in n or "ex-99" in n:
        return ("Exhibit 99" + suf, 3, is_pdf)
    if is_pdf:
        return (f"{name} (PDF)", 4, True)
    return None   # skip the bare 8-K cover body & other htm to avoid noise


async def _build_events(cik_int, evt_idx, dates, accs, descs, items,
                        idx_json_url, idx_url, doc_url) -> list[dict]:
    """For each earnings-related 8-K, read its index.json and surface any PDF
    exhibits (press release, investor deck) as direct download links. Falls
    back to the SEC filing index page when no PDF exhibit is present."""
    if not evt_idx:
        return []

    async def one(i):
        d = dates[i] if i < len(dates) else ""
        acc = (accs[i] if i < len(accs) else "").replace("-", "")
        base = idx_url(i).rstrip("/")
        rows = []
        try:
            async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as c:
                r = await c.get(idx_json_url(i))
                r.raise_for_status()
                files = (r.json().get("directory", {}) or {}).get("item", []) or []
            seen = set()
            cand = []
            for fobj in files:
                nm = fobj.get("name", "") or ""
                cl = _classify_exhibit(nm)
                if cl and cl[0] not in seen:
                    seen.add(cl[0])
                    cand.append((cl[1], {"label": cl[0], "date": d,
                                          "url": f"{base}/{nm}", "type": "8-K",
                                          "pdf": cl[2]}))
            cand.sort(key=lambda x: x[0])
            rows = [c for _, c in cand]
        except Exception as exc:
            logger.warning(f"stock_docs: 8-K index {acc} failed: {exc}")
        if not rows:
            # No PDF exhibit — link the filing index so the user still sees it.
            rows = [{"label": "8-K — earnings release / event", "date": d,
                     "url": idx_url(i), "type": "8-K", "pdf": False}]
        return rows

    nested = await asyncio.gather(*[one(i) for i in evt_idx], return_exceptions=True)
    out = []
    for grp in nested:
        if isinstance(grp, list):
            out.extend(grp)
    return out
