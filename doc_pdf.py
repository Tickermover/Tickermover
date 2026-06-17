"""
doc_pdf.py — serve a SEC / IR document for the in-app viewer, faithfully.

The goal is to show the REAL original document, not a re-rendered approximation:

  • PDFs (q4cdn / IR decks, SEC PDF exhibits) stream through unchanged — the
    browser's native PDF viewer renders them perfectly.
  • HTML documents (press releases, 10-Q/10-K primary docs, image-based decks)
    are served as-is with a <base> tag injected so their images, styles and
    slide graphics load straight from the source. The viewer then shows the
    actual filing/release exactly as published.

SSRF guard: only SEC + a short allow-list of reputable IR CDNs are fetched.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_UA = "TickerMover research alphahunt@example.com"
_HEADERS = {"User-Agent": _UA, "Accept-Encoding": "gzip, deflate"}

# SEC filings + reputable IR content CDNs that host investor decks/releases.
_HOST_ALLOW = ("sec.gov", "q4cdn.com", "gcs-web.com", "irpass.com",
               "investorroom.com", "media-server.com")


class DocError(Exception):
    pass


def _allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in _HOST_ALLOW)


async def fetch_doc(url: str) -> tuple[bytes, str]:
    """Return (bytes, media_type). PDFs pass through; HTML is served faithfully
    with a <base> so relative assets resolve. Raises DocError on bad input."""
    if not url or not url.lower().startswith("http"):
        raise DocError("Missing or invalid document URL.")
    if not _allowed(url):
        raise DocError("Only SEC and approved IR documents can be shown here.")
    try:
        async with httpx.AsyncClient(timeout=25, headers=_HEADERS,
                                     follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
    except Exception as exc:
        logger.warning(f"doc_pdf: fetch {url} failed: {exc}")
        raise DocError("Could not fetch the document.")

    ctype = (r.headers.get("content-type") or "").lower()
    if "application/pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        return r.content, "application/pdf"

    try:
        html = _with_base(r.text, url)
    except Exception as exc:
        logger.warning(f"doc_pdf: rewrite {url} failed: {exc}")
        html = r.text
    return html.encode("utf-8", "ignore"), "text/html; charset=utf-8"


def _with_base(html: str, url: str) -> str:
    """Inject a <base href> pointing at the document's directory so relative
    images/styles load from the source, and strip <script> for safety."""
    base_dir = url.rsplit("/", 1)[0] + "/"
    soup = BeautifulSoup(html, "lxml")
    for s in soup(["script"]):
        s.decompose()
    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        (soup.html or soup).insert(0, head)
    for b in head.find_all("base"):
        b.decompose()
    head.insert(0, soup.new_tag("base", href=base_dir))
    return str(soup)
