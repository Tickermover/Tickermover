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

    ANN_CAP, QTR_CAP = 4, 4          # last 4 annual reports and last 4 quarters
    annual, quarterly = [], []
    for i, f in enumerate(forms):
        if len(annual) >= ANN_CAP and len(quarterly) >= QTR_CAP:
            break
        d = dates[i] if i < len(dates) else ""
        yr = d[:4] if d else ""
        if f in ("10-K", "20-F", "40-F") and len(annual) < ANN_CAP:
            annual.append({"label": f"FY {yr}" if yr else "Annual report",
                           "date": d, "url": doc_url(i), "type": f})
        elif f in ("10-Q", "6-K") and len(quarterly) < QTR_CAP:
            quarterly.append({"label": f"Quarter ending {d}" if d else "Quarterly",
                              "date": d, "url": doc_url(i), "type": f})
    return {"annual": annual, "quarterly": quarterly, "events": [], "cik": cik_int}
