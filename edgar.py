"""edgar.py — read what a company has actually filed with the SEC.

Built for one purpose: an at-the-market ("ATM") equity programme is the
machinery by which a company sells stock straight into a rally that retail
created. It is disclosed, in public, days before the shares hit the tape — and
essentially no private investor ever sees it. Institutions read these the
morning they land.

    · shelf registrations          S-3, S-3ASR
    · takedowns off the shelf      424B5  (each one is a real sale)
    · ATM programmes               "at-the-market" in a 424B5 or its 8-K
    · convertible notes, warrants  read from the filing text, not guessed

SOURCES (all free, no key):
    https://www.sec.gov/files/company_tickers.json        ticker -> CIK
    https://data.sec.gov/submissions/CIK##########.json   every recent filing
    https://www.sec.gov/Archives/edgar/data/...           the document itself

SEC FAIR ACCESS: a descriptive User-Agent with a contact address is mandatory
and the cap is 10 requests/second. `_throttle` enforces a slower rate than
that, and every result is cached hard, because nothing here changes intraday.
"""
from __future__ import annotations

import gzip
import json
import logging
import re
import threading
import time
import urllib.request
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

UA = "TickerMover research (support@tickermover.com)"
_HDRS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
_MIN_GAP = 0.11                     # ~6 req/s, comfortably inside the SEC cap
_last_call = [0.0]
_lock = threading.Lock()

RAISE_FORMS = ("S-3", "S-3ASR", "S-3/A", "424B5", "424B3", "424B4", "S-1", "S-1/A")
DOC_CAP = 800_000                   # prospectuses are big; the front matter is enough


def _throttle():
    with _lock:
        gap = time.time() - _last_call[0]
        if gap < _MIN_GAP:
            time.sleep(_MIN_GAP - gap)
        _last_call[0] = time.time()


def _get(url: str, cap: int | None = None) -> bytes | None:
    _throttle()
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=_HDRS), timeout=30)
        b = r.read(cap) if cap else r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            try:
                b = gzip.decompress(b)
            except Exception:
                pass
        return b
    except Exception as exc:
        logger.warning("edgar GET %s failed: %s", url[:80], exc)
        return None


_TICKER_MAP: dict[str, str] = {}
_MAP_AT = [0.0]


def cik_for(ticker: str) -> str | None:
    """Zero-padded 10-digit CIK. The map is ~800KB, fetched once a day."""
    global _TICKER_MAP
    sym = (ticker or "").upper().strip()
    if not sym:
        return None
    if not _TICKER_MAP or (time.time() - _MAP_AT[0]) > 86400:
        b = _get("https://www.sec.gov/files/company_tickers.json")
        if b:
            try:
                raw = json.loads(b)
                _TICKER_MAP = {v["ticker"].upper(): str(v["cik_str"]).zfill(10)
                               for v in raw.values() if v.get("ticker")}
                _MAP_AT[0] = time.time()
            except Exception as exc:
                logger.warning("edgar ticker map parse failed: %s", exc)
    return _TICKER_MAP.get(sym)


def _recent(cik: str) -> list[dict]:
    b = _get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    if not b:
        return []
    try:
        rec = json.loads(b)["filings"]["recent"]
    except Exception:
        return []
    keys = ("form", "filingDate", "accessionNumber", "primaryDocument", "items")
    n = len(rec.get("form") or [])
    return [{k: (rec.get(k) or [None] * n)[i] for k in keys} for i in range(n)]


def _doc_url(cik: str, accession: str, doc: str) -> str:
    return (f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession.replace('-', '')}/{doc}")


_PATS = {
    "atm": re.compile(r"at[\s‐-―-]the[\s‐-―-]market", re.I),
    "sales_agreement": re.compile(r"sales agreement|equity distribution agreement", re.I),
    "convertible": re.compile(r"convertible (senior |subordinated )?notes", re.I),
    "warrants": re.compile(r"warrants? to purchase", re.I),
}
_PRICE = re.compile(r"(?:sale price|closing (?:sale )?price)[^$]{0,120}\$\s?([\d,]+\.?\d*)", re.I)
_SIZE = re.compile(r"aggregate (?:offering price|sales price|principal amount)[^$]{0,80}"
                   r"\$\s?([\d,]+\.?\d*)\s*(million|billion)?", re.I)


def _scan(text: str) -> dict:
    """Strip tags once, then look for the things that dilute a holder."""
    t = re.sub(r"<[^>]+>", " ", text)
    t = t.replace("&#8220;", '"').replace("&#8221;", '"').replace("&#160;", " ")
    t = re.sub(r"\s+", " ", t)
    out = {k: bool(p.search(t)) for k, p in _PATS.items()}
    m = _PRICE.search(t)
    out["price_at_filing"] = None
    if m:
        try:
            out["price_at_filing"] = float(m.group(1).replace(",", ""))
        except ValueError:
            pass
    s = _SIZE.search(t)
    out["size"] = None
    if s:
        try:
            v = float(s.group(1).replace(",", ""))
            unit = (s.group(2) or "").lower()
            out["size"] = v * (1e6 if unit == "million" else 1e9 if unit == "billion" else 1)
        except ValueError:
            pass
    return out


def programmes(ticker: str, today=None) -> dict | None:
    """What this company has filed that dilutes, and whether an ATM is live.

    Reads at most the two most recent prospectus supplements — the front matter
    carries the programme type, the size and the price on the day — so one
    ticker costs three or four SEC requests, not dozens."""
    today = today or datetime.now(timezone.utc).date()
    cik = cik_for(ticker)
    if not cik:
        return None
    rows = _recent(cik)
    if not rows:
        return None

    raises, counts = [], {"424b5": 0, "shelf": 0}
    for r in rows:
        form = (r.get("form") or "").upper()
        d = r.get("filingDate") or ""
        if not form.startswith(RAISE_FORMS) or not d:
            continue
        try:
            age = (today - datetime.strptime(d, "%Y-%m-%d").date()).days
        except ValueError:
            continue
        if age > 730:
            continue
        if form.startswith("424B5"):
            counts["424b5"] += 1
        if form.startswith("S-3"):
            counts["shelf"] += 1
        raises.append({"form": form, "date": d, "days": age,
                       "accession": r.get("accessionNumber"),
                       "doc": r.get("primaryDocument")})
    raises.sort(key=lambda x: x["date"], reverse=True)
    if not raises:
        return {"cik": cik, "filings": [], "atm": None,
                "offerings_24m": 0, "shelves_24m": 0,
                "convertible": False, "warrants": False}

    atm = None
    conv = warr = False
    for r in [x for x in raises if x["form"].startswith("424B")][:2]:
        if not (r.get("accession") and r.get("doc")):
            continue
        b = _get(_doc_url(cik, r["accession"], r["doc"]), cap=DOC_CAP)
        if not b:
            continue
        sc = _scan(b.decode("utf-8", "ignore"))
        conv = conv or sc["convertible"]
        warr = warr or sc["warrants"]
        if atm is None and sc["atm"] and sc["sales_agreement"]:
            atm = {"date": r["date"], "days": r["days"], "form": r["form"],
                   "price_at_filing": sc["price_at_filing"], "size": sc["size"],
                   "url": _doc_url(cik, r["accession"], r["doc"])}

    return {"cik": cik, "filings": raises[:8], "atm": atm,
            "offerings_24m": counts["424b5"], "shelves_24m": counts["shelf"],
            "convertible": conv, "warrants": warr,
            "last_raise": raises[0] if raises else None}
