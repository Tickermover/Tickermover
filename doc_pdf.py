"""
doc_pdf.py — turn a SEC document URL into a clean, in-app PDF.

Pure-Python (reportlab + BeautifulSoup), no system deps — works on Railway
nixpacks like the rest of the PDF stack.

  • If the URL is already a PDF (e.g. an investor-deck EX-99.2) → stream it
    through unchanged.
  • If it's HTML (press release, 10-Q/10-K primary doc) → extract the readable
    blocks (headings, paragraphs, list items, tables) and lay them out as a
    tidy A4 PDF.

SSRF guard: only sec.gov hosts are fetched.
"""
from __future__ import annotations

import io
import asyncio
import logging
from urllib.parse import urlparse, urljoin
from xml.sax.saxutils import escape as _xesc

import httpx
from bs4 import BeautifulSoup

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.lib.utils import ImageReader
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, Image as RLImage)

logger = logging.getLogger(__name__)

_UA = "AlphaHunt research alphahunt@example.com"
_HEADERS = {"User-Agent": _UA, "Accept-Encoding": "gzip, deflate"}

_MAX_BLOCKS = 1400          # hard cap so a 300-page 10-K can't blow up memory
_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "p", "li", "tr")


class DocError(Exception):
    pass


# SEC filings + reputable IR content CDNs that host investor decks/releases.
_HOST_ALLOW = ("sec.gov", "q4cdn.com", "gcs-web.com", "irpass.com",
               "investorroom.com", "media-server.com")


def _allowed(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host == d or host.endswith("." + d) for d in _HOST_ALLOW)


async def fetch_doc_pdf(url: str, title: str = "") -> tuple[bytes, str]:
    """Return (pdf_or_passthrough_bytes, media_type). Raises DocError on bad
    input. Only sec.gov URLs are fetched."""
    if not url or not url.lower().startswith("http"):
        raise DocError("Missing or invalid document URL.")
    if not _allowed(url):
        raise DocError("Only SEC (sec.gov) documents can be rendered here.")
    try:
        async with httpx.AsyncClient(timeout=25, headers=_HEADERS,
                                     follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
    except Exception as exc:
        logger.warning(f"doc_pdf: fetch {url} failed: {exc}")
        raise DocError("Could not fetch the document from SEC.")

    ctype = (r.headers.get("content-type") or "").lower()
    if "application/pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        return r.content, "application/pdf"

    try:
        # Conversion (incl. synchronous image fetches for slide decks) runs in
        # a worker thread so it never blocks the event loop.
        pdf = await asyncio.to_thread(_html_to_pdf, r.text,
                                      title or _guess_title(r.text), url)
    except Exception as exc:
        logger.warning(f"doc_pdf: render {url} failed: {exc}")
        raise DocError("Could not render this document as a PDF.")
    return pdf, "application/pdf"


def _guess_title(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "lxml")
        if soup.title and soup.title.string:
            return soup.title.string.strip()[:140]
    except Exception:
        pass
    return "SEC document"


def _styles():
    ss = getSampleStyleSheet()
    body = ParagraphStyle("doc-body", parent=ss["BodyText"], fontName="Helvetica",
                          fontSize=9.5, leading=14, spaceAfter=6)
    h = ParagraphStyle("doc-h", parent=ss["Heading2"], fontName="Helvetica-Bold",
                       fontSize=12.5, leading=16, spaceBefore=10, spaceAfter=4,
                       textColor=colors.HexColor("#0f172a"))
    title = ParagraphStyle("doc-title", parent=ss["Title"], fontName="Helvetica-Bold",
                           fontSize=16, leading=20, textColor=colors.HexColor("#0a0a0a"))
    small = ParagraphStyle("doc-small", parent=ss["BodyText"], fontName="Helvetica",
                           fontSize=8, leading=11, textColor=colors.HexColor("#64748b"),
                           spaceAfter=10)
    cell = ParagraphStyle("doc-cell", parent=body, fontSize=8, leading=10, spaceAfter=0)
    return body, h, title, small, cell


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _html_to_pdf(html: str, title: str, source_url: str) -> bytes:
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style", "head", "noscript"]):
        t.decompose()

    # Slide decks (e.g. an EX-99.2 investor presentation) are usually a thin
    # HTML wrapper around a sequence of slide images. When the page is
    # image-heavy, render those images one per page so the actual slides show.
    imgs = []
    for im in soup.find_all("img"):
        src = (im.get("src") or "").strip()
        if not src:
            continue
        absu = urljoin(source_url, src)
        if _allowed(absu) and absu not in imgs:
            imgs.append(absu)
    if len(imgs) >= 5:
        return _images_to_pdf(imgs, title, source_url)

    for im in soup.find_all("img"):       # text doc — drop inline images
        im.decompose()

    body, h_st, title_st, small_st, cell_st = _styles()
    story = [Paragraph(_xesc(_clean(title) or "SEC document"), title_st),
             Paragraph("Source: " + _xesc(source_url), small_st)]

    root = soup.body or soup
    count = 0
    seen_tables = set()
    for el in root.find_all(_BLOCK_TAGS):
        if count >= _MAX_BLOCKS:
            story.append(Paragraph("… document truncated — open the original for the full text.", small_st))
            break
        name = el.name
        if name == "tr":
            tbl = el.find_parent("table")
            if tbl is None or id(tbl) in seen_tables:
                continue
            seen_tables.add(id(tbl))
            flow = _render_table(tbl, cell_st)
            if flow is not None:
                story.append(flow)
                story.append(Spacer(1, 6))
                count += 1
            continue
        txt = _clean(el.get_text(" ", strip=True))
        if not txt:
            continue
        style = h_st if name in ("h1", "h2", "h3", "h4") else body
        story.append(Paragraph(_xesc(txt), style))
        count += 1

    if count == 0:
        # Fallback: dump the page text so we never produce an empty PDF.
        txt = _clean(root.get_text(" ", strip=True))[:20000]
        story.append(Paragraph(_xesc(txt) or "No readable content found.", body))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm, title=title)
    doc.build(story)
    return buf.getvalue()


def _images_to_pdf(imgs: list[str], title: str, source_url: str) -> bytes:
    """Render a sequence of slide images (a deck) as a one-image-per-row PDF."""
    body, h_st, title_st, small_st, cell_st = _styles()
    story = [Paragraph(_xesc(_clean(title) or "Investor presentation"), title_st),
             Paragraph("Source: " + _xesc(source_url), small_st)]
    avail_w = A4[0] - 36 * mm
    avail_h = A4[1] - 46 * mm
    with httpx.Client(timeout=20, headers=_HEADERS, follow_redirects=True) as c:
        for u in imgs[:80]:
            try:
                raw = c.get(u).content
                iw, ih = ImageReader(io.BytesIO(raw)).getSize()
                if not iw or not ih:
                    continue
                scale = min(avail_w / iw, avail_h / ih)
                story.append(RLImage(io.BytesIO(raw), width=iw * scale, height=ih * scale))
                story.append(Spacer(1, 10))
            except Exception:
                continue
    if len(story) <= 2:
        story.append(Paragraph("Could not load the presentation images — open the original filing.", body))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=16 * mm, bottomMargin=16 * mm, title=title)
    doc.build(story)
    return buf.getvalue()


def _render_table(tbl, cell_st) -> Table | None:
    """Render a small HTML table as a reportlab Table; bail (None) on anything
    too large or malformed so layout never crashes."""
    rows = []
    for tr in tbl.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        row = [Paragraph(_xesc(_clean(c.get_text(" ", strip=True)))[:300] or "·", cell_st)
               for c in cells[:8]]            # cap columns
        rows.append(row)
        if len(rows) >= 40:                    # cap rows
            break
    if not rows or len(rows) < 2:
        return None
    width = max(len(r) for r in rows)
    if width < 2:
        return None
    rows = [r + [Paragraph("", cell_st)] * (width - len(r)) for r in rows]
    try:
        t = Table(rows, hAlign="LEFT", repeatRows=1)
        t.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#e4e7eb")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f6f9ff")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        return t
    except Exception:
        return None
