"""
AlphaHunt — Server-side PDF tear sheet generation (v2 — analyst-report style).

Pure-Python A4 stock tear sheet using reportlab + matplotlib. No system
deps (reportlab is pure Python; matplotlib already in the install set).

Sections (A4 portrait):
    1. Header band  — AlphaHunt brand mark + date + quarter
    2. Hero row     — company logo + ticker + name + sector + price + Alpha Score
    3. 90-day price chart
    4. 12-card metrics grid (Market Cap / Rev Growth / Gross Margin / FCF Margin /
                              P/E / P/S / PEG / Debt-Eq / ROE / 52W / RSI / Beat Streak)
    5. Executive summary  (data-driven prose)
    6. Recent Earnings Highlights (Quartr-style bullets with YoY inline)
    7. Quality & Risk Signals (insider 90d / EPS revisions 30d / short / beta / surprise / volume)
    8. Disclaimer footer

Public API:
    generate_pdf(ticker: str, ticker_data: dict, price_history: list[dict]) -> bytes
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, date

import httpx
from reportlab.lib.pagesizes import A4
from reportlab.lib.colors import HexColor
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)

# ── Design tokens (mirror the dashboard's brand palette) ─────────────
BRAND_INDIGO  = HexColor("#D4860A")
BRAND_VIOLET  = HexColor("#FFC75F")
BRAND_MAGENTA = HexColor("#F5A623")
BRAND_LIGHT   = HexColor("#eef2ff")
INK           = HexColor("#0f172a")
INK_SOFT      = HexColor("#475569")
INK_MUTED     = HexColor("#94a3b8")
INK_DIM       = HexColor("#cbd5e1")
BG_SOFT       = HexColor("#f8fafc")
BG_CARD       = HexColor("#ffffff")
BORDER        = HexColor("#e2e8f0")
BORDER_LIGHT  = HexColor("#f1f5f9")
GREEN         = HexColor("#D4860A")
GREEN_LIGHT   = HexColor("#dcfce7")
RED           = HexColor("#dc2626")
RED_LIGHT     = HexColor("#fee2e2")
AMBER         = HexColor("#f59e0b")

A4_W, A4_H = A4              # 595 x 842 pts
MARGIN_X = 32
MARGIN_TOP = 18   # was 28 — trimmed to reclaim vertical space on
                  # page 1 (user feedback v3.13: 'Front page wasting
                  # lot of extra space in the beginning hence the
                  # bottom is touching the footer line.')
MARGIN_BOTTOM = 28
CONTENT_W = A4_W - MARGIN_X * 2


# ── Helpers ──────────────────────────────────────────────────────────

def _fmt_money(v):
    if v is None: return "—"
    try: v = float(v)
    except (TypeError, ValueError): return "—"
    if v >= 1e12: return f"${v/1e12:.2f}T"
    if v >= 1e9:  return f"${v/1e9:.2f}B"
    if v >= 1e6:  return f"${v/1e6:.0f}M"
    if v >= 1e3:  return f"${v/1e3:.0f}K"
    return f"${v:.2f}"


def _fmt_vol(v):
    if not v: return "—"
    try: v = float(v)
    except (TypeError, ValueError): return "—"
    if v >= 1e9: return f"{v/1e9:.1f}B"
    if v >= 1e6: return f"{v/1e6:.1f}M"
    if v >= 1e3: return f"{v/1e3:.0f}K"
    return f"{int(v)}"


def _safe_float(v, default=None):
    if v is None: return default
    try: return float(v)
    except (TypeError, ValueError): return default


def _fetch_logo_bytes(ticker: str) -> bytes | None:
    """Fetch the company logo from FMP image CDN. Cached in module-scope
    dict so repeated PDFs for the same ticker don't re-hit the CDN."""
    if ticker in _LOGO_CACHE:
        return _LOGO_CACHE[ticker]
    url = f"https://images.financialmodelingprep.com/symbol/{ticker.upper()}.png"
    try:
        with httpx.Client(timeout=4) as c:
            r = c.get(url)
            if r.status_code == 200 and r.content and len(r.content) > 200:
                _LOGO_CACHE[ticker] = r.content
                return r.content
    except Exception as e:
        logger.debug(f"logo fetch {ticker}: {e}")
    _LOGO_CACHE[ticker] = None
    return None

_LOGO_CACHE: dict[str, bytes | None] = {}


def _make_price_chart(price_history: list[dict], width_pt: float, height_pt: float) -> bytes | None:
    """Render a 90-day price chart as PNG bytes. Returns None on failure."""
    if not price_history or len(price_history) < 2:
        return None
    try:
        dates  = [p.get("date") for p in price_history]
        closes = [float(p.get("close") or 0) for p in price_history]
        fig_w, fig_h = width_pt / 72.0, height_pt / 72.0
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=160)
        ax.plot(range(len(closes)), closes, color="#6366f1", linewidth=2.0)
        ax.fill_between(range(len(closes)), closes, min(closes),
                        color="#FFC75F", alpha=0.18)
        ax.set_facecolor("#ffffff")
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        for s in ("left", "bottom"):
            ax.spines[s].set_color("#cbd5e1")
        ax.tick_params(colors="#94a3b8", labelsize=7)
        n = len(dates)
        ticks  = [0, n // 3, 2 * n // 3, n - 1]
        labels = []
        for i in ticks:
            d = dates[i] or ""
            try:
                d = datetime.fromisoformat(d).strftime("%b %d")
            except (TypeError, ValueError):
                pass
            labels.append(d)
        ax.set_xticks(ticks)
        ax.set_xticklabels(labels)
        ax.yaxis.tick_right()
        ax.grid(True, axis="y", color="#f1f5f9", linewidth=0.5)
        ax.plot([len(closes) - 1], [closes[-1]], "o",
                color="#F5A623", markersize=6,
                markeredgecolor="white", markeredgewidth=1.5)
        fig.tight_layout(pad=0.4)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=160, bbox_inches="tight",
                    facecolor="white", edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        logger.warning(f"_make_price_chart failed: {e}")
        plt.close("all")
        return None


def _lerp(a: int, b: int, t: float) -> int:
    """Integer-channel colour lerp helper."""
    return int(a + (b - a) * t)


def _brand_gradient_color(t: float):
    """Sample the AlphaHunt brand gradient at position t in [0,1].
    indigo (#D4860A) → violet (#FFC75F) → magenta (#F5A623)."""
    t = max(0.0, min(1.0, t))
    if t < 0.5:
        lt = t * 2
        r = _lerp(0x43, 0x8b, lt)
        g = _lerp(0x38, 0x5c, lt)
        b = _lerp(0xca, 0xf6, lt)
    else:
        lt = (t - 0.5) * 2
        r = _lerp(0x8b, 0xec, lt)
        g = _lerp(0x5c, 0x48, lt)
        b = _lerp(0xf6, 0x99, lt)
    return r / 255.0, g / 255.0, b / 255.0


def _draw_gradient_strip(c, x, y, w, h, n_steps=80):
    """Horizontal strip filled with the brand gradient. Fakes a smooth
    gradient by laying down N adjacent rectangles each filled with the
    interpolated colour at its midpoint. n_steps=80 is visually
    indistinguishable from a true gradient at A4 width."""
    step_w = w / n_steps
    for i in range(n_steps):
        r, g, b = _brand_gradient_color((i + 0.5) / n_steps)
        c.setFillColorRGB(r, g, b)
        c.rect(x + i * step_w, y, step_w + 0.5, h, stroke=0, fill=1)


# Single source of truth for the brand logo: /static/icons. This is
# the same folder the dashboard loads from at runtime (see
# templates/dashboard.html — /static/icons/alpha-logo-bare-64.png),
# so the PDF logo automatically tracks whatever's live on the site.
# Update one file in /static/icons and BOTH the dashboard and the PDF
# pick it up — no separate /brand folder to keep in sync.
import os as _os
_STATIC_DIR = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                              "static", "icons")
_BRAND_MARK_PATH   = _os.path.join(_STATIC_DIR, "alpha-logo-bare-512.png")
_BRAND_LOCKUP_PATH = _os.path.join(_STATIC_DIR, "alpha-logo-512.png")

# Legacy fallback to the older /brand folder so an incomplete deploy
# (one folder pushed but not the other) still renders a logo.
_LEGACY_BRAND_DIR  = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "brand")
_LEGACY_MARK_PATH   = _os.path.join(_LEGACY_BRAND_DIR, "alphahunt-mark-transparent-512.png")
_LEGACY_LOCKUP_PATH = _os.path.join(_LEGACY_BRAND_DIR, "alphahunt-lockup-light-1200x300.png")

_BRAND_MARK_IMG   = None
_BRAND_LOCKUP_IMG = None
try:
    for path in (_BRAND_MARK_PATH, _LEGACY_MARK_PATH):
        if _os.path.exists(path):
            _BRAND_MARK_IMG = ImageReader(path)
            logger.info(f"pdf_render: brand mark loaded from {path}")
            break
    for path in (_BRAND_LOCKUP_PATH, _LEGACY_LOCKUP_PATH):
        if _os.path.exists(path):
            _BRAND_LOCKUP_IMG = ImageReader(path)
            logger.info(f"pdf_render: brand lockup loaded from {path}")
            break
except Exception as _exc:
    logger.warning(f"pdf_render: brand asset load failed: {_exc}")


def _draw_brand_mark(c: canvas.Canvas, x: float, y: float, size: float = 28,
                      use_gradient: bool = True):
    """The AlphaHunt α-mark — uses the real brand PNG when available
    (brand/alphahunt-mark-transparent-512.png), falls back to a
    synthesised gradient α tile only if the asset is missing. The
    use_gradient flag is retained for back-compat but ignored when the
    real asset is present (the brand PNG is already correctly styled)."""
    if _BRAND_MARK_IMG is not None:
        c.drawImage(_BRAND_MARK_IMG, x, y, width=size, height=size,
                    preserveAspectRatio=True, anchor='c', mask='auto')
        return
    # ── Fallback: synthesised glyph ───────────────────────────────────
    c.saveState()
    c.setFillColor(BRAND_INDIGO)
    c.roundRect(x, y, size, size, size * 0.18, stroke=0, fill=1)
    c.setFont("Helvetica-Bold", size * 0.62)
    c.setFillColor(HexColor("#ffffff"))
    c.drawCentredString(x + size / 2, y + size * 0.26, "α")
    c.restoreState()


def _draw_brand_lockup(c, x, y, height):
    """Render the AlphaHunt brand lockup: production bare mark
    (/static/icons/alpha-logo-bare-512.png — same file the dashboard
    loads) + two-tone 'AlphaHunt' wordmark. Built compositionally from
    the bare mark + drawn text rather than loading a separate lockup
    asset, so the PDF always picks up whatever bare-logo file is
    deployed alongside the dashboard. Returns the total width drawn."""
    _draw_brand_mark(c, x, y, size=height)
    text_x = x + height + 8
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", height * 0.55)
    c.drawString(text_x, y + height * 0.30, "Alpha")
    c.setFillColor(BRAND_INDIGO)
    a_w = c.stringWidth("Alpha", "Helvetica-Bold", height * 0.55)
    c.drawString(text_x + a_w, y + height * 0.30, "Hunt")
    return height + 8 + c.stringWidth("AlphaHunt", "Helvetica-Bold", height * 0.55)


def _draw_brand_frame(c):
    """AlphaHunt page chrome — restored from v3.8 per user feedback.
    Combines a vivid brand-gradient strip at the very top with a soft
    light-gradient background fading through the page body:

      1. TOP brand strip (4pt full-bleed) — indigo→violet→magenta,
         the dashboard hero gradient. Vivid, the 'AlphaHunt handshake'.
      2. Soft body gradient — cream-indigo (#f4f5ff) at top, fading to
         pure white by 20% down. Subtle warmth under the cards.
      3. Tissue-thin (0.3pt) cool-grey page border — anchors the layout
         like the site's bordered components.
      4. Branded watermark in bottom-right (real brand PNG at 8% alpha).
    """
    # 1) Top brand gradient strip — the chrome bar that says 'AlphaHunt'
    _draw_gradient_strip(c, 0, A4_H - 4, A4_W, 4, n_steps=80)

    # 2) Soft body gradient — starts JUST below the brand strip
    n_strips = 40
    strip_h  = (A4_H * 0.18) / n_strips
    top_y    = A4_H - 4
    for i in range(n_strips):
        t = i / (n_strips - 1)
        r = 244 + int((255 - 244) * t)
        g = 245 + int((255 - 245) * t)
        b = 255
        c.setFillColorRGB(r / 255, g / 255, b / 255)
        c.rect(0, top_y - (i + 1) * strip_h, A4_W, strip_h + 0.5,
               stroke=0, fill=1)

    # 3) Tissue-thin border around the page
    c.setStrokeColor(HexColor("#e8eaf6"))
    c.setLineWidth(0.3)
    c.rect(4, 4, A4_W - 8, A4_H - 12, stroke=1, fill=0)

    # NOTE: Watermark deliberately removed per user feedback v3.11
    # ('delete the watermark'). The brand strip + lockup in the
    # header already carry the brand identity sufficiently.


def _card_bg(c, x, y, w, h, radius=6):
    """One-call replacement for the (setFillColor + setStroke + roundRect)
    pattern that 14 different drawers use. Renders the card with the
    light gradient fill + standard border in one shot. Use this in
    every drawer that previously did:
        c.setFillColor(BG_CARD); c.setStrokeColor(BORDER); c.setLineWidth(0.6)
        c.roundRect(x, y, w, h, R, stroke=1, fill=1)
    """
    _fill_card_gradient(c, x, y, w, h, radius=radius)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.6)
    c.roundRect(x, y, w, h, radius, stroke=1, fill=0)


def _fill_card_gradient(c, x, y, w, h, radius=6):
    """Fill a rounded-rect region with a subtle vertical gradient — pure
    white at the top fading to faintly tinted cream-indigo (#fafbff) at
    the bottom. Replaces flat white card fills throughout the report
    so the cards have visual depth without being noisy.

    Uses a polygonal clip path so the gradient stays inside the card.
    Caller is responsible for stroking the border afterwards if needed."""
    c.saveState()
    # Build the rounded-rect clip path
    p = c.beginPath()
    p.moveTo(x + radius, y)
    p.lineTo(x + w - radius, y)
    p.arcTo(x + w - 2*radius, y, x + w, y + 2*radius, 270, 90)
    p.lineTo(x + w, y + h - radius)
    p.arcTo(x + w - 2*radius, y + h - 2*radius, x + w, y + h, 0, 90)
    p.lineTo(x + radius, y + h)
    p.arcTo(x, y + h - 2*radius, x + 2*radius, y + h, 90, 90)
    p.lineTo(x, y + radius)
    p.arcTo(x, y, x + 2*radius, y + 2*radius, 180, 90)
    p.close()
    c.clipPath(p, stroke=0, fill=0)
    # Vertical gradient — top (white) → bottom (#fafbff)
    n_steps = 12
    step_h = h / n_steps
    for i in range(n_steps):
        # 0 at bottom (most tinted), 1 at top (pure white)
        t_pos = i / (n_steps - 1)
        r = 250 + int((255 - 250) * t_pos)
        g = 251 + int((255 - 251) * t_pos)
        b = 255
        c.setFillColorRGB(r / 255, g / 255, b / 255)
        c.rect(x, y + i * step_h, w, step_h + 0.5, stroke=0, fill=1)
    c.restoreState()


def _draw_metric_card(c, x, y, w, h, label, value, sub="", value_color=INK, empty=False):
    # Light gradient card fill (replaces flat white) + border in one helper
    _card_bg(c, x, y, w, h, radius=5)
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica-Bold", 6.5)
    c.drawString(x + 8, y + h - 11, label.upper())
    if empty:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 11)
    else:
        c.setFillColor(value_color)
        c.setFont("Helvetica-Bold", 13)
    c.drawString(x + 8, y + h - 28, str(value))
    if sub:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 6.5)
        c.drawString(x + 8, y + 6, sub)


def _wrap_paragraph(text, width, font_size=8.5, color=INK, leading=11.5, align=TA_LEFT):
    style = ParagraphStyle(
        "p", fontName="Helvetica", fontSize=font_size, textColor=color,
        leading=leading, alignment=align,
    )
    return Paragraph(text, style)


# ── Section drawers ──────────────────────────────────────────────────

def _draw_header(c, today_str, quarter_lbl, page_label=None):
    """Branded header — real AlphaHunt lockup PNG on the left, generation
    timestamp + quarter chip on the right, subtle thin divider. Sits
    on top of the soft light gradient drawn by _draw_brand_frame.

    page_label: optional context label rendered under the lockup,
    e.g. 'US STOCK TEAR SHEET' (page 1) or 'FINANCIAL TRENDS · PAGE 2'.
    """
    # Page chrome (light gradient + border + watermark)
    _draw_brand_frame(c)

    y = A4_H - MARGIN_TOP - 26
    # Real AlphaHunt brand lockup (mark + wordmark as one asset)
    lockup_w = _draw_brand_lockup(c, MARGIN_X, y + 2, height=24)

    # 'RESEARCH REPORT' positioned UNDER the 'AlphaHunt' wordmark
    # text — not under the α icon — per user v3.13 feedback
    # ('Placed research report word middle of alpha hunt'). We offset
    # to MARGIN_X + 32 which is where the wordmark starts inside the
    # lockup (after the 24pt α mark + 8pt gap).
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(MARGIN_X + 32, y - 6, "RESEARCH REPORT")

    # Right side — date + quarter chip
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7.5)
    c.drawRightString(A4_W - MARGIN_X, y + 17, f"GENERATED {today_str.upper()}")
    chip_w = c.stringWidth(quarter_lbl, "Helvetica-Bold", 8) + 14
    chip_x = A4_W - MARGIN_X - chip_w
    c.setFillColor(BRAND_LIGHT)
    c.roundRect(chip_x, y + 1, chip_w, 13, 6, stroke=0, fill=1)
    c.setFillColor(BRAND_INDIGO)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(chip_x + 7, y + 4, quarter_lbl)

    # Thin grey divider — tighter since the tagline was removed.
    c.setStrokeColor(HexColor("#d4d8e8"))
    c.setLineWidth(0.6)
    c.line(MARGIN_X, y - 14, A4_W - MARGIN_X, y - 14)
    return y - 22  # bottom of header


def _draw_hero(c, top_y, ticker, t, logo_bytes):
    """3-column hero: [logo + ticker/name/sector] [price block] [score block].
    Total height ~90 pts. Returns y of bottom of hero."""
    hero_h = 76   # was 86 — trimmed 10pt to reclaim vertical room
                  # on page 1 above the footer
    hero_bottom = top_y - hero_h
    # Three columns with explicit x ranges so nothing overlaps:
    col_a_x = MARGIN_X
    col_a_w = 280            # ticker + name + sector
    col_b_x = MARGIN_X + 290 # price block
    col_b_w = 160
    col_c_x = A4_W - MARGIN_X - 100  # Alpha Score on right
    col_c_w = 100

    # ── Column A: logo + ticker + name + sector ─────────────────────
    logo_size = 44
    logo_x = col_a_x
    logo_y = top_y - 50
    # Logo card frame
    _card_bg(c, logo_x, logo_y, logo_size, logo_size, radius=6)
    if logo_bytes:
        try:
            img = ImageReader(io.BytesIO(logo_bytes))
            c.drawImage(img, logo_x + 4, logo_y + 4,
                        width=logo_size - 8, height=logo_size - 8,
                        preserveAspectRatio=True, anchor='c', mask='auto')
        except Exception as e:
            logger.debug(f"logo draw {ticker}: {e}")
    else:
        # Fallback initial letter on brand gradient
        c.setFillColor(BRAND_INDIGO)
        c.roundRect(logo_x + 4, logo_y + 4, logo_size - 8, logo_size - 8, 5, stroke=0, fill=1)
        c.setFont("Helvetica-Bold", 18)
        c.setFillColor(HexColor("#ffffff"))
        c.drawCentredString(logo_x + logo_size / 2, logo_y + logo_size / 2 - 6, ticker[:1])

    text_x = col_a_x + logo_size + 12
    # Ticker (big)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 28)
    c.drawString(text_x, top_y - 30, ticker)
    # Name
    name = (t.get("name") or t.get("long_name") or "")[:46]
    c.setFillColor(INK_SOFT)
    c.setFont("Helvetica", 10)
    c.drawString(text_x, top_y - 44, name)
    # Sector / industry chips
    sector = (t.get("sector") or "").upper()
    industry = (t.get("sub_sector") or t.get("industry") or "").upper()[:36]
    chip_y = top_y - 60
    if sector:
        sw = c.stringWidth(sector, "Helvetica-Bold", 7) + 12
        c.setFillColor(BRAND_LIGHT)
        c.setStrokeColor(BRAND_INDIGO)
        c.setLineWidth(0.5)
        c.roundRect(text_x, chip_y, sw, 13, 3, stroke=1, fill=1)
        c.setFillColor(BRAND_INDIGO)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(text_x + 6, chip_y + 3.5, sector)
        next_x = text_x + sw + 5
    else:
        next_x = text_x
    if industry and industry != sector:
        iw = c.stringWidth(industry, "Helvetica-Bold", 7) + 12
        c.setFillColor(BG_SOFT)
        c.setStrokeColor(BORDER)
        c.roundRect(next_x, chip_y, iw, 13, 3, stroke=1, fill=1)
        c.setFillColor(INK_SOFT)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(next_x + 6, chip_y + 3.5, industry)

    # ── Column B: price + change ────────────────────────────────────
    px = _safe_float(t.get("price"))
    chg = _safe_float(t.get("change_pct"))
    cb_cx = col_b_x + col_b_w / 2
    if px is not None:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 26)
        c.drawCentredString(cb_cx, top_y - 30, f"${px:.2f}")
    if chg is not None:
        c.setFillColor(GREEN if chg >= 0 else RED)
        c.setFont("Helvetica-Bold", 11)
        sign = "+" if chg >= 0 else ""
        c.drawCentredString(cb_cx, top_y - 46, f"{sign}{chg:.2f}% today")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica-Bold", 6.5)
    c.drawCentredString(cb_cx, top_y - 58, "LAST CLOSE")

    # ── Column C: Alpha Score ───────────────────────────────────────
    score = _safe_float(t.get("smart_score") or t.get("pop_score"), 0)
    grade = (t.get("grade") or "—").upper()
    star_map = {"A": "★★★★★", "B": "★★★★", "C": "★★★", "D": "★★", "F": "★"}
    tier_map = {"A": "Top Tier", "B": "Quality", "C": "Average", "D": "Below Avg", "F": "Weak"}
    cc_rx = A4_W - MARGIN_X
    c.setFillColor(BRAND_VIOLET)
    c.setFont("Helvetica-Bold", 36)
    c.drawRightString(cc_rx, top_y - 32, str(int(score)))
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica-Bold", 7)
    c.drawRightString(cc_rx, top_y - 45, "ALPHA SCORE")
    c.setFillColor(AMBER)
    c.setFont("Helvetica", 9)
    c.drawRightString(cc_rx, top_y - 57, star_map.get(grade, ""))
    c.setFillColor(BRAND_INDIGO)
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(cc_rx, top_y - 69, tier_map.get(grade, "—"))

    # Bottom divider
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.5)
    c.line(MARGIN_X, hero_bottom, A4_W - MARGIN_X, hero_bottom)
    return hero_bottom


def _make_revenue_eps_chart(quarterly_income: list, eps_quarters: list,
                              width_pt: float, height_pt: float) -> bytes | None:
    """Combo chart: revenue bars (left axis) + EPS actual & estimate lines
    (right axis) over last 4-8 quarters. Beat/miss dots on EPS actual.
    Quartr-style — the visualisation pro reports use to summarise the
    most-recent earnings trajectory."""
    # Pull oldest-first sequences of equal length, max 6 quarters
    inc = [q for q in (quarterly_income or [])[:6] if q.get("revenue") is not None]
    inc.reverse()
    eps = [q for q in (eps_quarters or [])[:6] if q.get("actual") is not None]
    eps.reverse()
    if len(inc) < 2 and len(eps) < 2:
        return None
    try:
        fig_w, fig_h = width_pt / 72.0, height_pt / 72.0
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=160)
        ax.set_facecolor("#ffffff")
        labels = []
        revenues = []
        for q in inc[-5:]:
            rev = float(q.get("revenue") or 0) / 1e9
            revenues.append(rev)
            d = q.get("date") or q.get("period") or ""
            try:
                labels.append(datetime.fromisoformat(d).strftime("%b '%y"))
            except (TypeError, ValueError):
                labels.append(str(d)[-6:])
        if revenues:
            x = list(range(len(revenues)))
            ax.bar(x, revenues, color="#c7d2fe", edgecolor="#6366f1",
                   linewidth=0.8, label="Revenue ($B)", width=0.55)
            for xi, v in zip(x, revenues):
                ax.text(xi, v + max(revenues) * 0.02,
                        f"${v:.2f}B", ha="center", va="bottom",
                        fontsize=6.5, color="#475569")
        # EPS overlay on twin axis
        if len(eps) >= 2:
            ax2 = ax.twinx()
            n = min(len(eps), len(revenues)) if revenues else len(eps)
            eps_x = list(range(n))[-n:]
            eps_actual = [float((q.get("actual") or 0)) for q in eps[-n:]]
            eps_est    = [float((q.get("estimate") or 0)) for q in eps[-n:]]
            ax2.plot(eps_x, eps_est, "--", color="#94a3b8",
                     linewidth=1.4, label="EPS estimate")
            ax2.plot(eps_x, eps_actual, "-", color="#0f172a",
                     linewidth=2.0, label="EPS actual")
            # Beat/miss dots
            for xi, a, e in zip(eps_x, eps_actual, eps_est):
                color = "#D4860A" if a >= e else "#dc2626"
                ax2.plot(xi, a, "o", color=color, markersize=6,
                         markeredgecolor="white", markeredgewidth=1.2)
            ax2.tick_params(colors="#94a3b8", labelsize=7)
            for s in ("top", "right"):
                ax2.spines[s].set_visible(False)
            ax2.spines["right"].set_visible(True)
            ax2.spines["right"].set_color("#cbd5e1")
            ax2.set_ylabel("EPS", fontsize=7, color="#475569")
        ax.set_xticks(list(range(len(labels))))
        ax.set_xticklabels(labels, fontsize=7, color="#475569")
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.spines["left"].set_color("#cbd5e1")
        ax.spines["bottom"].set_color("#cbd5e1")
        ax.tick_params(colors="#94a3b8", labelsize=7)
        ax.grid(True, axis="y", color="#f1f5f9", linewidth=0.5)
        ax.set_ylabel("Revenue ($B)", fontsize=7, color="#475569")
        fig.tight_layout(pad=0.4)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=160, bbox_inches="tight",
                    facecolor="white", edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as exc:
        logger.warning(f"_make_revenue_eps_chart failed: {exc}")
        plt.close("all")
        return None


def _make_margin_trend_chart(quarterly_income: list,
                              width_pt: float, height_pt: float) -> bytes | None:
    """Two-line trend chart: GROSS margin + OPERATING margin over last
    4-6 quarters. Surfaces the 'is margin expansion durable?' answer
    that bull/bear narratives lean on but can't show numerically."""
    inc = [q for q in (quarterly_income or [])[:6] if q.get("gross_margin") is not None]
    inc.reverse()
    if len(inc) < 2:
        return None
    try:
        fig_w, fig_h = width_pt / 72.0, height_pt / 72.0
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=160)
        ax.set_facecolor("#ffffff")
        labels = []
        gm_vals, om_vals = [], []
        for q in inc[-5:]:
            gm = q.get("gross_margin")
            om = q.get("operating_margin")
            # Try compute operating_margin from op_income / revenue if missing
            if om is None and q.get("operating_income") and q.get("revenue"):
                try:
                    om = float(q["operating_income"]) / float(q["revenue"])
                except (TypeError, ValueError, ZeroDivisionError):
                    om = None
            # Normalise to percent
            try: gm_v = float(gm) * 100 if abs(float(gm)) <= 1 else float(gm)
            except (TypeError, ValueError): gm_v = None
            try: om_v = float(om) * 100 if abs(float(om)) <= 1 else float(om)
            except (TypeError, ValueError): om_v = None
            if gm_v is None and om_v is None:
                continue
            gm_vals.append(gm_v)
            om_vals.append(om_v)
            d = q.get("date") or q.get("period") or ""
            try:
                labels.append(datetime.fromisoformat(d).strftime("%b '%y"))
            except (TypeError, ValueError):
                labels.append(str(d)[-6:])
        if not gm_vals:
            plt.close(fig)
            return None
        x = list(range(len(labels)))
        # Gross margin line
        gm_plot = [v if v is not None else float("nan") for v in gm_vals]
        ax.plot(x, gm_plot, "-o", color="#D4860A", linewidth=2.0,
                markersize=4.5, label="Gross margin %")
        for xi, v in zip(x, gm_plot):
            if v == v:  # not NaN
                ax.text(xi, v + 1.5, f"{v:.1f}%", ha="center", va="bottom",
                        fontsize=6.5, color="#D4860A")
        # Operating margin line (may have NaN slots)
        if any(v is not None for v in om_vals):
            om_plot = [v if v is not None else float("nan") for v in om_vals]
            ax.plot(x, om_plot, "-s", color="#F5A623", linewidth=1.8,
                    markersize=4, label="Operating margin %")
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=7, color="#475569")
        ax.tick_params(colors="#94a3b8", labelsize=7)
        for s in ("top", "right"):
            ax.spines[s].set_visible(False)
        ax.spines["left"].set_color("#cbd5e1")
        ax.spines["bottom"].set_color("#cbd5e1")
        ax.grid(True, axis="y", color="#f1f5f9", linewidth=0.5)
        # Y-axis title (user v3.17: 'give the title of the y axis').
        # No more 'Gross margin %' legend in the middle — the card
        # subtitle already labels the chart, and the y-axis title now
        # makes the units unambiguous. Cleaner reading.
        ax.set_ylabel("Margin %", fontsize=7, color="#475569",
                       labelpad=2)
        # Legend removed entirely — was overlapping the line and
        # adding no information beyond what the card title says.
        fig.tight_layout(pad=0.4)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=160, bbox_inches="tight",
                    facecolor="white", edgecolor="none")
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as exc:
        logger.warning(f"_make_margin_trend_chart failed: {exc}")
        plt.close("all")
        return None


def _default_dcf_assumptions(t):
    """Deterministic fallback when Haiku doesn't return a DCF block.
    Synthesises a defensible assumption set from the ticker metrics so
    the DCF panel always has SOMETHING to show. Decays current growth
    toward a sector-typical steady state."""
    def _safe_growth():
        for k in ("rev_growth_yoy", "rev_growth_qyoy", "revenue_growth_yoy"):
            v = t.get(k)
            if v is not None:
                try: return float(v) * 100.0  # fraction → percent
                except (TypeError, ValueError): continue
        return 15.0
    g0 = max(-30, min(80, _safe_growth()))
    # Geometric decay from g0 toward 8% by year 5
    growth = [
        round(g0,                     1),
        round(g0 * 0.7 + 8 * 0.3,     1),
        round(g0 * 0.4 + 8 * 0.6,     1),
        round(g0 * 0.2 + 8 * 0.8,     1),
        round(g0 * 0.0 + 8 * 1.0,     1),
    ]
    fcf_m = _safe_float(t.get("fcf_margin"))
    if fcf_m is None: fcf_m = 0.10
    if abs(fcf_m) <= 1: fcf_m *= 100
    fcf_target = max(-10, min(35, fcf_m * 1.3 if fcf_m > 0 else 12))
    return {
        "revenue_growth":    growth,
        "fcf_margin_target": fcf_target,
        "wacc":              10.0,
        "terminal_growth":   3.0,
        "rationale":         "Auto-generated assumption set "
                              "(growth decays toward 8%, FCF margin "
                              "expands toward sector norm). Adjust "
                              "manually as needed.",
    }


def _compute_dcf(t, assumptions):
    """Compute 5-year DCF + terminal value, return implied share price
    and a sensitivity matrix. assumptions is the validated dict that
    came out of pdf_narrative (revenue_growth list, fcf_margin_target,
    wacc, terminal_growth). All percentage inputs in display form.

    NEW (v3.11): falls back to deterministic _default_dcf_assumptions
    when Haiku returns no DCF block — so the panel always renders
    instead of the embarrassing 'DCF model unavailable' blank space."""
    if not assumptions:
        assumptions = _default_dcf_assumptions(t)
    if not assumptions:
        return None
    try:
        growth = [float(g) / 100.0 for g in assumptions["revenue_growth"]]
        if len(growth) != 5:
            return None
        fcf_m  = float(assumptions["fcf_margin_target"]) / 100.0
        wacc   = float(assumptions["wacc"]) / 100.0
        tg     = float(assumptions["terminal_growth"]) / 100.0
        if wacc <= tg:
            return None
    except (KeyError, TypeError, ValueError):
        return None

    # Derive starting revenue (TTM) — sum last 4 quarters
    qinc = t.get("quarterly_income") or []
    rev_ttm = 0.0
    for q in qinc[:4]:
        try:
            rev_ttm += float(q.get("revenue") or 0)
        except (TypeError, ValueError):
            pass
    if rev_ttm <= 0:
        # Fall back to market_cap / ps_ttm if quarterly income is missing
        mcap = _safe_float(t.get("market_cap"))
        ps   = _safe_float(t.get("ps_ttm"))
        if mcap and ps and ps > 0:
            rev_ttm = mcap / ps
    if rev_ttm <= 0:
        return None

    # Project 5 years, compute FCF and discount
    proj = []
    rev = rev_ttm
    for y, g in enumerate(growth, start=1):
        rev = rev * (1 + g)
        fcf = rev * fcf_m
        dcf = fcf / ((1 + wacc) ** y)
        proj.append({"year": y, "revenue": rev, "fcf": fcf, "pv_fcf": dcf})

    fcf_y5      = proj[-1]["fcf"]
    terminal_v  = fcf_y5 * (1 + tg) / (wacc - tg)
    pv_terminal = terminal_v / ((1 + wacc) ** 5)
    enterprise_value = sum(p["pv_fcf"] for p in proj) + pv_terminal

    # Net debt adjustment (rough — most tear sheets approximate equity = EV)
    # When we don't have net debt, equity value ≈ EV. Acceptable
    # approximation given the precision of the assumption set.
    equity_value = enterprise_value

    # Share count from market cap / current price
    mcap  = _safe_float(t.get("market_cap"))
    price = _safe_float(t.get("price") or t.get("last_close"))
    shares = (mcap / price) if (mcap and price and price > 0) else None
    if not shares or shares <= 0:
        return None
    implied_price = equity_value / shares

    # Sensitivity grid: WACC ± 1%, terminal growth ± 1%
    def _run_with(w_pct, tg_pct):
        w = w_pct / 100.0
        g_t = tg_pct / 100.0
        if w <= g_t:
            return None
        ev = 0.0
        r = rev_ttm
        for y, g in enumerate(growth, start=1):
            r = r * (1 + g)
            ev += (r * fcf_m) / ((1 + w) ** y)
        fcf5 = r * fcf_m
        ev += (fcf5 * (1 + g_t) / (w - g_t)) / ((1 + w) ** 5)
        return ev / shares

    wacc_pct = wacc * 100
    tg_pct = tg * 100
    sensitivity = []
    for w_delta in (-1, 0, +1):
        row = []
        for g_delta in (-1, 0, +1):
            p = _run_with(wacc_pct + w_delta, tg_pct + g_delta)
            row.append(p)
        sensitivity.append({
            "wacc": wacc_pct + w_delta,
            "prices": row,
        })

    return {
        "proj":          proj,
        "rev_ttm":       rev_ttm,
        "fcf_margin":    fcf_m * 100,
        "wacc":          wacc_pct,
        "terminal_g":    tg_pct,
        "terminal_v":    terminal_v,
        "pv_terminal":   pv_terminal,
        "enterprise_v":  enterprise_value,
        "implied_price": implied_price,
        "current_price": price,
        "shares":        shares,
        "sensitivity":   sensitivity,
        "tg_band":       [tg_pct - 1, tg_pct, tg_pct + 1],
    }


def _draw_dcf_model(c, x, y, w, h, t, narrative):
    """DCF Valuation Model card — 5-year projection bars + assumptions
    chip row + implied price + 3x3 sensitivity matrix. The piece that
    pushes the report from buy-side tear sheet to sell-side research
    note. All math is deterministic; only the input assumptions come
    from Haiku (and are clamped on the way out)."""
    _card_bg(c, x, y, w, h, radius=6)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(x + 10, y + h - 13, "DCF VALUATION MODEL")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(x + 10, y + h - 22,
                  "5-year free cash flow + terminal value  ·  sensitivity to WACC & terminal growth")

    dcf_in = (narrative or {}).get("dcf")
    result = _compute_dcf(t, dcf_in)
    if not result:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 9)
        c.drawCentredString(x + w/2, y + h/2 - 6,
                             "DCF model unavailable (insufficient fundamental data)")
        return

    # ── Left side: 5-year FCF projection bars + assumption chips ──────
    left_x = x + 12
    left_w = (w - 24) * 0.58 - 6
    # User v3.15: 'Give space between the line 5yr cashflow and below
    # line Project Free cashflow.' — moved chart_top down 12pt so the
    # 'PROJECTED FREE CASH FLOW' title sits below the card subtitle
    # with breathing room.
    chart_top = y + h - 50
    chart_h   = 70

    proj   = result["proj"]
    fcfs   = [p["fcf"] / 1e9 for p in proj]   # to $B
    max_fcf = max(fcfs) if fcfs else 1
    bar_w  = (left_w - 30) / 5
    bar_x0 = left_x + 22
    bar_top = chart_top
    bar_bot = chart_top - chart_h

    # Mini Y-axis with $B
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 6)
    c.drawString(left_x, chart_top - 6, f"${max_fcf:.1f}B")
    c.drawString(left_x, bar_bot,        "$0")
    # Axis line
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.4)
    c.line(bar_x0, bar_bot, bar_x0 + left_w - 22, bar_bot)

    # FCF bars
    for i, p in enumerate(proj):
        v = p["fcf"] / 1e9
        h_bar = (v / max_fcf) * chart_h
        cx = bar_x0 + i * bar_w + bar_w * 0.15
        cw = bar_w * 0.7
        c.setFillColor(BRAND_VIOLET)
        c.roundRect(cx, bar_bot, cw, max(2, h_bar), 2, stroke=0, fill=1)
        # Value above bar
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawCentredString(cx + cw / 2, bar_bot + h_bar + 3,
                             f"${v:.1f}B")
        # X-axis label
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 6)
        c.drawCentredString(cx + cw / 2, bar_bot - 8, f"Y{p['year']}")

    # Chart title
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(left_x, chart_top + 8, "PROJECTED FREE CASH FLOW")

    # Assumption chip row beneath the chart — center-aligned within
    # the left column (user v3.15: 'move the below ledgers in the
    # right little bit so they adjust middle alignment'). Pre-measure
    # total chip-row width, then start at the column's centerline
    # minus half the total width.
    chip_y = bar_bot - 32
    chips = [
        ("WACC",            f"{result['wacc']:.1f}%"),
        ("Term. growth",    f"{result['terminal_g']:.1f}%"),
        ("FCF margin (Y5)", f"{result['fcf_margin']:.0f}%"),
    ]
    chip_widths = [c.stringWidth(f"{label}: {val}", "Helvetica-Bold", 7) + 12
                   for label, val in chips]
    total_chip_w = sum(chip_widths) + 6 * (len(chips) - 1)
    col_center_x = left_x + left_w / 2
    cx = col_center_x - total_chip_w / 2
    for (label, val), chip_w in zip(chips, chip_widths):
        c.setFillColor(BRAND_LIGHT)
        c.roundRect(cx, chip_y, chip_w, 13, 6, stroke=0, fill=1)
        c.setFillColor(BRAND_INDIGO)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(cx + 6, chip_y + 4, f"{label}: {val}")
        cx += chip_w + 6

    # Rationale line
    rationale = ((narrative or {}).get("dcf") or {}).get("rationale") or ""
    if rationale:
        c.setFillColor(INK_SOFT)
        c.setFont("Helvetica", 7.5)
        max_w = left_w
        text = rationale
        while c.stringWidth(text, "Helvetica", 7.5) > max_w and len(text) > 12:
            text = text[:-2]
        if text != rationale:
            text = text.rstrip(" ,;.") + "…"
        c.drawString(left_x, chip_y - 14, text)

    # ── Right side: Implied price + Sensitivity 3x3 grid ──────────────
    right_x = x + 12 + left_w + 12
    right_w = w - (right_x - x) - 12

    implied = result["implied_price"]
    current = result["current_price"] or 0
    upside  = ((implied / current - 1) * 100) if current > 0 else 0
    u_color = GREEN if upside >= 0 else RED

    # Implied price big number
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(right_x, chart_top + 8, "IMPLIED SHARE PRICE")
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 22)
    c.drawString(right_x, chart_top - 18, f"${implied:.2f}")
    c.setFillColor(u_color)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(right_x, chart_top - 30,
                  f"{'+' if upside >= 0 else ''}{upside:.1f}% vs ${current:.2f}")

    # Sensitivity 3x3 table
    sens_top = chart_top - 50
    cell_w = (right_w - 22) / 4   # 3 cols of values + 1 label col
    cell_h = 16
    # Header row
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica-Bold", 6)
    c.drawString(right_x, sens_top, "SENSITIVITY  ·  WACC \\  TERM g →")
    th_y = sens_top - 12
    sens = result["sensitivity"]
    tg_labels = result["tg_band"]
    # Column headers (terminal growth)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 6.5)
    for i, g in enumerate(tg_labels):
        c.drawCentredString(right_x + cell_w * 0.5 + (i + 1) * cell_w,
                             th_y, f"{g:.1f}%g")
    # Rows
    for ri, row in enumerate(sens):
        ry = th_y - (ri + 1) * cell_h
        # Row label
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(right_x, ry + 4, f"WACC {row['wacc']:.1f}%")
        # Cells
        for ci, price in enumerate(row["prices"]):
            cx = right_x + cell_w * 0.5 + (ci + 1) * cell_w
            if price is None:
                c.setFillColor(INK_MUTED)
                c.setFont("Helvetica", 7)
                c.drawCentredString(cx, ry + 4, "—")
            else:
                # Highlight the centre cell (the base case)
                is_center = (ri == 1 and ci == 1)
                if is_center:
                    c.setFillColor(BRAND_LIGHT)
                    c.rect(cx - cell_w * 0.45, ry, cell_w * 0.9, cell_h - 2,
                           stroke=0, fill=1)
                c.setFillColor(BRAND_INDIGO if is_center else INK)
                c.setFont("Helvetica-Bold" if is_center else "Helvetica", 7)
                c.drawCentredString(cx, ry + 4, f"${price:.0f}")


def _norm_growth_pct(v):
    """Growth rates are ALWAYS stored as fractions in the universe data
    (rev_growth_qyoy = 1.393 for 139.3% YoY). The general-purpose
    norm_pct heuristic gets this wrong because it assumes values > 1 are
    already-percent. For growth fields specifically, unconditionally
    multiply by 100. Returns None for invalid input."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f * 100.0


def _compute_risk_scorecard(t):
    """Deterministic 5-dimension risk assessment from existing metrics.
    Each dimension scores 0-100 where HIGHER = MORE RISK. No AI needed —
    this is a pure visualization of what the metrics card already shows.
    Returns list of (label, score, color, one_liner) tuples."""
    def norm_pct(v):
        if v is None: return None
        try: f = float(v)
        except (TypeError, ValueError): return None
        if -1 <= f <= 1 and f != 0: f *= 100
        return f

    pe   = _safe_float(t.get("pe_ttm"))
    ps   = _safe_float(t.get("ps_ttm"))
    peg  = _safe_float(t.get("peg_ratio"))
    # FIX (v3.11): rev_growth_* fields are stored as fractions (not
    # already-percent) — APLD v3.10 showed 'Rev +1% YoY' for a +139%
    # YoY ticker because norm_pct treated 1.393 as already-percent.
    # Use the dedicated _norm_growth_pct that ALWAYS multiplies by 100.
    rev_raw = (t.get("rev_growth_yoy") or t.get("rev_growth_qyoy")
                or t.get("revenue_growth_yoy"))
    rev = _norm_growth_pct(rev_raw) if rev_raw is not None else None
    gm   = norm_pct(t.get("gross_margin"))
    fcfm = norm_pct(t.get("fcf_margin"))
    rsi  = _safe_float(t.get("rsi14") or t.get("rsi_14"))
    mom  = norm_pct(t.get("momentum_30d") or t.get("momentum_1m"))
    short_p = norm_pct(t.get("short_pct_float"))
    revs    = t.get("eps_revisions_30d") or {}
    ups, dns = revs.get("ups") or 0, revs.get("downs") or 0

    # ── Valuation risk ────────────────────────────────────────────────
    val_score = 50
    if peg is not None:
        if peg < 0.8:   val_score = 25
        elif peg < 1.2: val_score = 40
        elif peg < 2.0: val_score = 60
        else:           val_score = 80
    elif pe is not None and pe > 0:
        if pe < 15:     val_score = 30
        elif pe < 30:   val_score = 50
        elif pe < 80:   val_score = 70
        else:           val_score = 85
    val_note = (f"PEG {peg:.2f}" if peg else
                (f"P/E {pe:.1f}x" if pe and pe > 0 else "Limited data"))

    # ── Growth risk ───────────────────────────────────────────────────
    grw_score = 50
    if rev is not None:
        if rev >= 40:   grw_score = 15
        elif rev >= 20: grw_score = 30
        elif rev >= 10: grw_score = 50
        elif rev >= 0:  grw_score = 70
        else:           grw_score = 90
    grw_note = f"Rev {('+' if rev >= 0 else '')}{rev:.0f}% YoY" if rev is not None else "—"

    # ── Quality risk ──────────────────────────────────────────────────
    qual_score = 50
    qual_factors = []
    if gm is not None:
        qual_factors.append(20 if gm >= 60 else 35 if gm >= 40 else 55 if gm >= 25 else 75)
    if fcfm is not None:
        qual_factors.append(20 if fcfm >= 15 else 35 if fcfm >= 5 else 60 if fcfm >= 0 else 85)
    if qual_factors:
        qual_score = sum(qual_factors) / len(qual_factors)
    parts = []
    if gm is not None:   parts.append(f"GM {gm:.0f}%")
    if fcfm is not None: parts.append(f"FCF {'+' if fcfm >= 0 else ''}{fcfm:.0f}%")
    qual_note = " · ".join(parts) or "—"

    # ── Sentiment risk ────────────────────────────────────────────────
    sent_score = 50
    if short_p is not None:
        if short_p >= 25:    sent_score = 80
        elif short_p >= 15:  sent_score = 65
        elif short_p >= 7:   sent_score = 50
        else:                sent_score = 30
    # Revision adjustment
    if ups + dns >= 3:
        ratio = ups / max(dns, 1)
        if ratio >= 3:   sent_score -= 15
        elif ratio <= 0.5: sent_score += 15
    sent_score = max(0, min(100, sent_score))
    sent_note = (f"Short {short_p:.1f}% · revisions {ups}↑/{dns}↓"
                  if short_p is not None else f"Revisions {ups}↑/{dns}↓")

    # ── Momentum risk ─────────────────────────────────────────────────
    mom_score = 50
    if rsi is not None:
        if rsi >= 75:    mom_score = 80   # overbought
        elif rsi >= 60:  mom_score = 55
        elif rsi >= 40:  mom_score = 40
        elif rsi >= 30:  mom_score = 55
        else:            mom_score = 75   # oversold
    if mom is not None:
        if mom <= -15:   mom_score = max(mom_score, 75)
        elif mom >= 30:  mom_score = max(mom_score, 70)   # parabolic
    mom_note = (f"RSI {rsi:.0f}" + (f" · {('+' if mom >= 0 else '')}{mom:.0f}% 30D" if mom is not None else "")
                  if rsi is not None else "—")

    def color_for(s):
        if s >= 70: return RED
        if s >= 55: return AMBER
        if s >= 40: return BRAND_INDIGO
        return GREEN

    return [
        ("Valuation", val_score,  color_for(val_score),  val_note),
        ("Growth",    grw_score,  color_for(grw_score),  grw_note),
        ("Quality",   qual_score, color_for(qual_score), qual_note),
        ("Sentiment", sent_score, color_for(sent_score), sent_note),
        ("Momentum",  mom_score,  color_for(mom_score),  mom_note),
    ]


def _draw_risk_scorecard(c, x, y, w, h, t):
    """5-dimension risk scorecard card — Valuation, Growth, Quality,
    Sentiment, Momentum — with mini horizontal meters and one-line
    metric anchors. The deterministic counterpart to the AI thesis
    page; a real institutional report always shows this."""
    _card_bg(c, x, y, w, h, radius=6)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(x + 10, y + h - 13, "RISK SCORECARD")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(x + 10, y + h - 22, "5-dimension risk profile  ·  green = low, red = high")
    # Legend on right — uniform-width slots so the dots line up on a
    # grid instead of bunching unevenly (user feedback v3.11:
    # 'needs alignment in the buttons'). Reserve 40pt per item:
    # 6pt for the dot + 4pt gap + 30pt for the label, right-aligned.
    c.setFontSize(6.5)
    slot_w = 40
    legend = [(GREEN, "LOW"), (BRAND_INDIGO, "MOD"),
              (AMBER, "ELEV"), (RED, "HIGH")]
    legend_right = x + w - 10
    for i, (color, label) in enumerate(legend):
        slot_x = legend_right - (len(legend) - i) * slot_w
        # Dot
        c.setFillColor(color)
        c.circle(slot_x + 4, y + h - 18, 2.4, stroke=0, fill=1)
        # Label (left-aligned to a fixed offset from the dot)
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(slot_x + 10, y + h - 20, label)

    items = _compute_risk_scorecard(t)
    if not items:
        return
    n = len(items)
    row_top = y + h - 36
    row_bot = y + 10
    row_h = (row_top - row_bot) / n

    for i, (label, score, color, note) in enumerate(items):
        ry = row_top - (i + 1) * row_h + row_h * 0.2
        # Row label
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x + 10, ry + 4, label.upper())
        # Bar background
        bar_x = x + 88
        bar_w = w - 110 - 100   # leave room for label + score + note
        bar_h = 6
        bar_y = ry + 2
        c.setFillColor(HexColor("#f1f5f9"))
        c.roundRect(bar_x, bar_y, bar_w, bar_h, bar_h / 2, stroke=0, fill=1)
        # Filled portion
        fill_w = bar_w * (score / 100.0)
        c.setFillColor(color)
        c.roundRect(bar_x, bar_y, max(2, fill_w), bar_h, bar_h / 2, stroke=0, fill=1)
        # Score number
        c.setFillColor(color)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(bar_x + bar_w + 6, ry + 3, f"{int(score)}")
        # Anchor note (right side)
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 7)
        c.drawRightString(x + w - 10, ry + 4, note[:32])


def _draw_valuation_scenarios(c, x, y, w, h, t, narrative):
    """Bear / Base / Bull price-target scenarios. Each renders as a row:
    label  ·  $price ·  ±%  ·  rationale. Above the rows sits a horizontal
    scenario bar showing the three prices arranged left→right with the
    current price marked as a vertical line."""
    _card_bg(c, x, y, w, h, radius=6)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(x + 10, y + h - 13, "VALUATION SCENARIOS")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(x + 10, y + h - 22, "Bear / Base / Bull price targets  ·  rationale tied to specific triggers")

    val = (narrative or {}).get("valuation")
    price = _safe_float(t.get("price") or t.get("last_close")) or 0
    if not val or not all(k in val for k in ("bear", "base", "bull")):
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 9)
        c.drawCentredString(x + w/2, y + h/2 - 6, "Valuation scenarios unavailable")
        return

    bear = val["bear"]; base = val["base"]; bull = val["bull"]
    bear_p = float(bear.get("price") or 0)
    base_p = float(base.get("price") or 0)
    bull_p = float(bull.get("price") or 0)

    # ── Scenario bar (top section) ────────────────────────────────────
    # Push down enough so the 'NOW $X' label below the bar doesn't
    # collide with the card subtitle. The subtitle sits at y + h - 22,
    # bar_top is the y of the bar centerline, and the NOW label is
    # drawn at bar_top + 14. Use h-50 so the label lands at h-36
    # (14pt below the subtitle row).
    bar_top = y + h - 50
    bar_h_seg = 8
    bar_x = x + 12
    bar_w = w - 24
    lo = min(bear_p, price * 0.7) if price else bear_p
    hi = max(bull_p, price * 1.3) if price else bull_p
    rng = max(hi - lo, 1)

    def _to_x(v):
        return bar_x + ((v - lo) / rng) * bar_w

    # Background track
    c.setFillColor(HexColor("#f1f5f9"))
    c.roundRect(bar_x, bar_top - bar_h_seg / 2, bar_w, bar_h_seg, bar_h_seg/2, stroke=0, fill=1)
    # Bear→base gradient red, base→bull gradient green
    c.setFillColor(HexColor("#fee2e2"))
    c.rect(_to_x(bear_p), bar_top - bar_h_seg/2, _to_x(base_p) - _to_x(bear_p), bar_h_seg, stroke=0, fill=1)
    c.setFillColor(HexColor("#dcfce7"))
    c.rect(_to_x(base_p), bar_top - bar_h_seg/2, _to_x(bull_p) - _to_x(base_p), bar_h_seg, stroke=0, fill=1)
    # Scenario markers — distinct symbols so Bear/Base/Bull are
    # visually identifiable (v3.10 showed 'B/B/B' on all three dots).
    # ▼ Bear (red), ● Base (indigo), ▲ Bull (green).
    for label, p, color, glyph in [
        ("Bear", bear_p, RED,          "▼"),
        ("Base", base_p, BRAND_INDIGO, "●"),
        ("Bull", bull_p, GREEN,        "▲"),
    ]:
        cx = _to_x(p)
        c.setFillColor(color)
        c.circle(cx, bar_top, 5, stroke=0, fill=1)
        c.setFillColor(HexColor("#ffffff"))
        c.setFont("Helvetica-Bold", 6.5)
        c.drawCentredString(cx, bar_top - 2.5, glyph)
        c.setFillColor(color)
        c.setFont("Helvetica-Bold", 7)
        c.drawCentredString(cx, bar_top - 12, f"${p:.0f}")
    # Current price marker
    if price:
        px = _to_x(price)
        c.setStrokeColor(INK)
        c.setLineWidth(1.5)
        c.line(px, bar_top - 10, px, bar_top + 10)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawCentredString(px, bar_top + 14, f"NOW ${price:.2f}")

    # ── Scenario rows (bottom section) ────────────────────────────────
    rows_top = bar_top - 36
    rows_bot = y + 8
    row_h = (rows_top - rows_bot) / 3
    scenarios = [
        ("BEAR", bear, RED,   HexColor("#fee2e2")),
        ("BASE", base, BRAND_INDIGO, BRAND_LIGHT),
        ("BULL", bull, GREEN, HexColor("#dcfce7")),
    ]
    for i, (label, sc, color, bg_color) in enumerate(scenarios):
        ry = rows_top - (i + 1) * row_h + 2
        # Soft band
        c.setFillColor(bg_color)
        c.rect(x + 10, ry, w - 20, row_h - 2, stroke=0, fill=1)
        # Label chip
        c.setFillColor(color)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x + 16, ry + row_h/2 - 2, label)
        # Price + delta
        sc_price = float(sc.get("price") or 0)
        delta_pct = ((sc_price / price - 1) * 100) if price > 0 else 0
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(x + 60, ry + row_h/2 - 3, f"${sc_price:.0f}")
        c.setFillColor(color)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(x + 105, ry + row_h/2 - 2,
                      f"{'+' if delta_pct >= 0 else ''}{delta_pct:.1f}%")
        # Rationale
        rat = (sc.get("rationale") or "").strip()
        if rat:
            c.setFillColor(INK_SOFT)
            c.setFont("Helvetica", 7.5)
            # Trim to fit
            max_w = w - 170
            text = rat
            while c.stringWidth(text, "Helvetica", 7.5) > max_w and len(text) > 12:
                text = text[:-2]
            if text != rat:
                text = text.rstrip(" ,;.") + "…"
            c.drawString(x + 155, ry + row_h/2 - 2, text)


def _make_sparkline(values, width_pt, height_pt, color="#D4860A",
                     fill_color=None, label_last=True, unit="%"):
    """Tiny inline-style sparkline: thin line + optional fill, latest value
    annotated. Returns PNG bytes or None when no data."""
    vals = [v for v in (values or []) if v is not None]
    if len(vals) < 2:
        return None
    try:
        fig_w, fig_h = width_pt / 72.0, height_pt / 72.0
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=160)
        ax.set_facecolor("#ffffff")
        x = list(range(len(vals)))
        ax.plot(x, vals, "-", color=color, linewidth=1.8)
        if fill_color is None:
            fill_color = color
        ax.fill_between(x, vals, min(vals), color=fill_color, alpha=0.15)
        # Annotate first & last
        ax.plot([0], [vals[0]], "o", color=color, markersize=3,
                markeredgecolor="white", markeredgewidth=0.8)
        ax.plot([len(vals)-1], [vals[-1]], "o", color=color, markersize=4,
                markeredgecolor="white", markeredgewidth=0.8)
        # Axis cosmetics: hide everything for a true sparkline feel
        ax.set_xticks([])
        ax.set_yticks([])
        for s in ("top", "right", "left", "bottom"):
            ax.spines[s].set_visible(False)
        # Pad y-range so dots don't clip
        ymin, ymax = min(vals), max(vals)
        pad = max(0.5, (ymax - ymin) * 0.18)
        ax.set_ylim(ymin - pad, ymax + pad)
        fig.tight_layout(pad=0.05)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=160, bbox_inches="tight",
                    facecolor="white", edgecolor="none",
                    pad_inches=0.01)
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception:
        plt.close("all")
        return None


def _compute_trend_metrics(t):
    """Build the 4-metric time series for the Trend Strip card.

    Robust to incomplete data: rev growth falls back to QoQ when YoY
    history (8 quarters) isn't available. Operating margin computes
    from op_income/revenue when not provided directly. FCF margin
    pulls from a separate quarterly_cashflow list if present, otherwise
    falls back to the TTM fcf_margin value as a flat reference line.

    Returns list of (label, series, color) where series is a list of
    floats/None. Missing dimensions render as labelled empty cards
    rather than disappearing — the user can see what's not available."""
    qinc = list(t.get("quarterly_income") or [])
    qinc.reverse()   # oldest-first
    if len(qinc) < 2:
        return []

    # Separate cashflow stream if it exists (some pipelines store FCF
    # in quarterly_cashflow keyed by date, not on the income row)
    qcf = list(t.get("quarterly_cashflow") or [])
    qcf.reverse()
    cf_by_date = {}
    for q in qcf:
        d = q.get("date") or q.get("period")
        if d:
            cf_by_date[d] = q

    rev_growth = []
    rev_growth_label = "Revenue Growth YoY"
    have_yoy_data = len(qinc) >= 5
    gm_series, om_series, fcf_series = [], [], []
    last_n = qinc[-6:]

    for i, q in enumerate(last_n):
        rev = _safe_float(q.get("revenue"))
        # Revenue growth — prefer YoY (i-4) but fall back to QoQ (i-1)
        if have_yoy_data:
            prior_idx = len(qinc) - len(last_n) + i - 4
            rev_prev = _safe_float(qinc[prior_idx].get("revenue")) if 0 <= prior_idx < len(qinc) else None
        else:
            prior_idx = len(qinc) - len(last_n) + i - 1
            rev_prev = _safe_float(qinc[prior_idx].get("revenue")) if 0 <= prior_idx < len(qinc) else None
            rev_growth_label = "Revenue Growth QoQ"
        if rev and rev_prev and rev_prev > 0:
            rev_growth.append((rev / rev_prev - 1) * 100)
        else:
            rev_growth.append(None)

        # Gross margin
        gm = _safe_float(q.get("gross_margin"))
        if gm is not None and abs(gm) <= 1: gm *= 100
        gm_series.append(gm)

        # Operating margin — prefer field, else compute, else None
        om = _safe_float(q.get("operating_margin"))
        if om is None:
            op_inc = _safe_float(q.get("operating_income"))
            if op_inc is not None and rev and rev > 0:
                om = (op_inc / rev) * 100
        elif abs(om) <= 1:
            om *= 100
        om_series.append(om)

        # FCF margin — try the row, the matching cashflow row, then None
        fcf = _safe_float(q.get("fcf_margin") or q.get("free_cash_flow_margin"))
        if fcf is None:
            fcf_v = _safe_float(q.get("free_cash_flow"))
            if fcf_v is None:
                qd = q.get("date") or q.get("period")
                if qd and qd in cf_by_date:
                    fcf_v = _safe_float(cf_by_date[qd].get("free_cash_flow")
                                          or cf_by_date[qd].get("freeCashFlow"))
            if fcf_v is not None and rev and rev > 0:
                fcf = (fcf_v / rev) * 100
        elif abs(fcf) <= 1:
            fcf *= 100
        fcf_series.append(fcf)

    # Heuristic fallback: if op_margin / fcf_margin series have <2 real
    # values, fill from the headline ticker fields as a flat reference.
    def _meaningful(seq):
        return sum(1 for v in seq if v is not None) >= 2

    if not _meaningful(om_series):
        # Try operating_margin TTM on the ticker, expand as flat series
        om_ttm = _safe_float(t.get("operating_margin"))
        if om_ttm is not None:
            if abs(om_ttm) <= 1: om_ttm *= 100
            om_series = [om_ttm] * len(om_series)
    if not _meaningful(fcf_series):
        fcf_ttm = _safe_float(t.get("fcf_margin"))
        if fcf_ttm is not None:
            if abs(fcf_ttm) <= 1: fcf_ttm *= 100
            fcf_series = [fcf_ttm] * len(fcf_series)

    return [
        (rev_growth_label, rev_growth, "#D4860A"),
        ("Gross Margin",   gm_series,  "#FFC75F"),
        ("Operating Margin", om_series, "#F5A623"),
        ("FCF Margin",     fcf_series, "#D4860A"),
    ]


def _draw_trend_strip(c, x, y, w, h, t):
    """2x2 grid of sparkline cards for the four financial-trend metrics
    (Revenue Growth YoY %, Gross Margin %, Op Margin %, FCF Margin %).
    Surfaces the 'are the fundamentals durably improving?' answer at a
    glance — what a real analyst tear sheet uses instead of bare prose."""
    # Outer card
    _card_bg(c, x, y, w, h, radius=6)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(x + 10, y + h - 13, "FINANCIAL TREND STRIP")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(x + 10, y + h - 22, "4-quarter trajectory  ·  durability check")

    metrics = _compute_trend_metrics(t)
    if not metrics:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 9)
        c.drawCentredString(x + w/2, y + h/2 - 6, "Insufficient quarterly data")
        return

    # 2x2 sparkline grid
    inner_y_top = y + h - 30
    inner_y_bot = y + 12
    cell_w = (w - 30) / 2
    cell_h = (inner_y_top - inner_y_bot - 8) / 2

    # v3.18 redesign: each trend cell is now structured as
    #   [accent strip] LABEL                  CURRENT %  (delta vs Q-1)
    #                  ┌──────────────────────────────┐
    #                  │      sparkline (larger)      │
    #                  └──────────────────────────────┘
    #                  Q-3                          Q-0
    # Bigger sparkline, min/max value tags above first and last point,
    # quarter labels under the line, and an accent strip on the left
    # using the metric color so each cell has visual identity.
    for i, (label, series, color) in enumerate(metrics[:4]):
        col = i % 2
        row = i // 2
        cx = x + 10 + col * (cell_w + 10)
        cy = inner_y_top - (row + 1) * cell_h - row * 8
        # White card with subtle border + colored left accent strip
        c.setFillColor(HexColor("#ffffff"))
        c.setStrokeColor(HexColor("#e8eaf6"))
        c.setLineWidth(0.6)
        c.roundRect(cx, cy, cell_w, cell_h, 5, stroke=1, fill=1)
        c.setFillColor(HexColor(color))
        c.rect(cx, cy, 3, cell_h, stroke=0, fill=1)

        # ── Top row: LABEL on left, current value on right ─────────
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(cx + 12, cy + cell_h - 11, label.upper())

        # Current value (latest in series)
        current = next((v for v in reversed(series) if v is not None), None)
        # Prior value for QoQ delta tag
        prior_idx = None
        for j in range(len(series) - 2, -1, -1):
            if series[j] is not None:
                prior_idx = j
                break
        prior = series[prior_idx] if prior_idx is not None else None

        if current is not None:
            sign = "+" if current >= 0 else ""
            c.setFillColor(GREEN if current >= 0 else RED)
            c.setFont("Helvetica-Bold", 13)
            c.drawRightString(cx + cell_w - 8, cy + cell_h - 14,
                                f"{sign}{current:.1f}%")
            # QoQ delta tag below current value
            if prior is not None:
                delta = current - prior
                d_sign = "+" if delta >= 0 else ""
                c.setFillColor(GREEN if delta >= 0 else RED)
                c.setFont("Helvetica", 6.5)
                c.drawRightString(cx + cell_w - 8, cy + cell_h - 22,
                                    f"{d_sign}{delta:.1f}pp QoQ")
        else:
            c.setFillColor(INK_MUTED)
            c.setFont("Helvetica-Bold", 12)
            c.drawRightString(cx + cell_w - 8, cy + cell_h - 14, "—")
            c.setFillColor(INK_MUTED)
            c.setFont("Helvetica", 6)
            c.drawRightString(cx + cell_w - 8, cy + cell_h - 22, "no data")

        # ── Sparkline area (lower 60% of cell) ─────────────────────
        sp_x = cx + 12
        sp_y_top = cy + cell_h - 30
        sp_w = cell_w - 18
        sp_h = cell_h - 36
        sp_png = _make_sparkline(series, sp_w, sp_h, color=color)
        if sp_png:
            img = ImageReader(io.BytesIO(sp_png))
            c.drawImage(img, sp_x, cy + 6, width=sp_w, height=sp_h,
                        preserveAspectRatio=False, anchor='c', mask='auto')
            # Quarter axis labels under the spark — first / mid / last
            n_pts = sum(1 for v in series if v is not None)
            if n_pts >= 2:
                c.setFillColor(INK_MUTED)
                c.setFont("Helvetica", 5.5)
                c.drawString(sp_x, cy + 2, f"Q-{n_pts-1}")
                c.drawRightString(sp_x + sp_w, cy + 2, "Q-0")
        else:
            # Dotted placeholder + centered 'no quarterly data' note
            c.setStrokeColor(HexColor("#e2e8f0"))
            c.setDash(2, 3)
            c.setLineWidth(0.8)
            mid_y = cy + sp_h / 2 + 6
            c.line(sp_x, mid_y, sp_x + sp_w, mid_y)
            c.setDash()
            c.setFillColor(INK_MUTED)
            c.setFont("Helvetica", 6.5)
            c.drawCentredString(sp_x + sp_w / 2, cy + 6,
                                  "Awaiting next quarterly report")


def _draw_peer_comparison(c, x, y, w, h, t, peers):
    """Compact peer-comparison table. peers is a list of dicts with keys
    {ticker, name, market_cap, pe_ttm, rev_growth_yoy, gross_margin,
     smart_score}. Shows our target plus up to 3 peers as rows."""
    _card_bg(c, x, y, w, h, radius=6)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(x + 10, y + h - 13, "PEER COMPARISON")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(x + 10, y + h - 22,
                  "Same sector  ·  ranked by Alpha Score")

    if not peers:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 9)
        c.drawCentredString(x + w/2, y + h/2 - 6, "No peer data available")
        return

    # v3.17: tri-field fallback for P/E (universe stores it under
    # multiple aliases — 'pe_ttm', 'pe_ratio', 'pe' — depending on the
    # data path). v3.16 only checked pe_ttm so all peer rows showed
    # '—' even though P/E was available under pe_ratio. Also added
    # FCF MARGIN column per user feedback 'additional column can be
    # added' — complements GM with cash-quality signal.
    def _pe(row):
        return _safe_float(row.get("pe_ttm") or row.get("pe_ratio") or row.get("pe"))
    def _fcfm(row):
        v = _safe_float(row.get("fcf_margin"))
        if v is None: return None
        return v * 100 if abs(v) <= 1 else v
    def _gm(row):
        v = _safe_float(row.get("gross_margin"))
        if v is None: return None
        return v * 100 if abs(v) <= 1 else v
    def _rev(row):
        return _safe_float(row.get("rev_growth_yoy")
                           or row.get("rev_growth_qyoy")
                           or row.get("revenue_growth_yoy"))

    target_row = {
        "ticker": (t.get("ticker") or "—").upper(),
        "name":   t.get("name", "")[:18],
        "mcap":   _safe_float(t.get("market_cap")),
        "pe":     _pe(t),
        "rev":    _rev(t),
        "gm":     _gm(t),
        "fcfm":   _fcfm(t),
        "score":  int(t.get("smart_score") or t.get("pop_score") or 0),
        "is_target": True,
    }
    rows = [target_row]
    for p in peers[:3]:
        rows.append({
            "ticker": (p.get("ticker") or "—").upper(),
            "name":   (p.get("name") or "")[:18],
            "mcap":   _safe_float(p.get("market_cap")),
            "pe":     _pe(p),
            "rev":    _rev(p),
            "gm":     _gm(p),
            "fcfm":   _fcfm(p),
            "score":  int(p.get("smart_score") or p.get("pop_score") or 0),
            "is_target": False,
        })

    # Column layout — added FCF column (7 total). Widths re-balanced
    # so the header row still sums to ~1.0.
    cols = [
        ("TICKER",  0.13),
        ("MKT CAP", 0.15),
        ("P/E",     0.11),
        ("REV YoY", 0.13),
        ("GM",      0.11),
        ("FCF",     0.11),
        ("SCORE",   0.14),
    ]
    inner_w = w - 24
    col_xs = []
    acc = x + 12
    for name, frac in cols:
        col_xs.append(acc)
        acc += inner_w * frac

    # Header row
    hdr_y = y + h - 36
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica-Bold", 6.5)
    for (name, _), cx in zip(cols, col_xs):
        c.drawString(cx, hdr_y, name)
    # Underline
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.4)
    c.line(x + 12, hdr_y - 3, x + w - 12, hdr_y - 3)

    # Data rows
    row_h = (hdr_y - 14 - (y + 10)) / max(len(rows), 1)
    row_h = min(row_h, 22)
    for i, r in enumerate(rows):
        ry = hdr_y - 12 - (i + 1) * row_h + 2
        # Highlight the target row with a light brand fill
        if r["is_target"]:
            c.setFillColor(HexColor("#eef2ff"))
            c.rect(x + 8, ry - 3, w - 16, row_h, stroke=0, fill=1)
        # Ticker (bold for target)
        c.setFillColor(BRAND_INDIGO if r["is_target"] else INK)
        c.setFont("Helvetica-Bold" if r["is_target"] else "Helvetica-Bold", 8)
        c.drawString(col_xs[0], ry + 3, r["ticker"])
        # Market cap
        c.setFillColor(INK)
        c.setFont("Helvetica", 8)
        if r["mcap"]:
            mcap_str = _fmt_money(r["mcap"]).replace("$", "")
            c.drawString(col_xs[1], ry + 3, mcap_str)
        else:
            c.drawString(col_xs[1], ry + 3, "—")
        # P/E
        c.setFillColor(INK if r["pe"] and r["pe"] > 0 else INK_MUTED)
        c.drawString(col_xs[2], ry + 3,
                      f"{r['pe']:.1f}x" if (r['pe'] and r['pe'] > 0) else "—")
        # Rev growth — ALWAYS treat as fraction (rev_growth_* is stored
        # as e.g. 1.393 for 139.3%, never as already-percent)
        if r["rev"] is not None:
            rev_v = r["rev"] * 100.0
            c.setFillColor(GREEN if rev_v >= 0 else RED)
            c.drawString(col_xs[3], ry + 3, f"{'+' if rev_v >= 0 else ''}{rev_v:.0f}%")
        else:
            c.setFillColor(INK_MUTED)
            c.drawString(col_xs[3], ry + 3, "—")
        # Gross margin (already normalised in row construction)
        if r["gm"] is not None:
            c.setFillColor(INK)
            c.drawString(col_xs[4], ry + 3, f"{r['gm']:.0f}%")
        else:
            c.setFillColor(INK_MUTED)
            c.drawString(col_xs[4], ry + 3, "—")
        # FCF margin — green if positive cash generation, red if burning
        if r["fcfm"] is not None:
            c.setFillColor(GREEN if r["fcfm"] >= 0 else RED)
            c.drawString(col_xs[5], ry + 3,
                          f"{'+' if r['fcfm'] >= 0 else ''}{r['fcfm']:.0f}%")
        else:
            c.setFillColor(INK_MUTED)
            c.drawString(col_xs[5], ry + 3, "—")
        # Alpha Score (badge)
        score = r["score"]
        if score >= 80:    score_color = GREEN
        elif score >= 60:  score_color = BRAND_INDIGO
        else:              score_color = AMBER
        c.setFillColor(score_color)
        c.setFont("Helvetica-Bold", 9)
        # Score column moved from index 5 → 6 after FCF column inserted
        c.drawString(col_xs[6], ry + 3, str(score) if score else "—")


def _draw_quarterly_charts_row(c, top_y, t):
    """New page-1 row: Quarterly Revenue & EPS combo chart (left) +
    Margin Trend chart (right). Always renders from t.quarterly_income +
    t.eps_quarters — these fields are populated by the FMP fundamentals
    pipeline and don't depend on flaky daily-price feeds. So this row
    works for every ticker, unlike the 90-day price chart."""
    block_h = 160
    y = top_y - block_h - 10
    gap = 10
    half_w = (CONTENT_W - gap) / 2

    qinc = t.get("quarterly_income") or []
    eps  = t.get("eps_quarters") or []

    # ── LEFT: Revenue + EPS combo chart ───────────────────────────────
    _card_bg(c, MARGIN_X, y, half_w, block_h, radius=6)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(MARGIN_X + 10, y + block_h - 13, "QUARTERLY REVENUE & EPS")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(MARGIN_X + 10, y + block_h - 22,
                  "Revenue bars (left)  ·  EPS actual vs estimate  ·  green dot = beat, red dot = miss")
    chart_png = _make_revenue_eps_chart(qinc, eps, half_w - 24, block_h - 36)
    if chart_png:
        img = ImageReader(io.BytesIO(chart_png))
        c.drawImage(img, MARGIN_X + 6, y + 4,
                    width=half_w - 12, height=block_h - 30,
                    preserveAspectRatio=True, anchor='c', mask='auto')
    else:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 9)
        c.drawCentredString(MARGIN_X + half_w / 2, y + block_h / 2 - 6,
                             "Insufficient quarterly data")

    # ── RIGHT: Margin trend chart ─────────────────────────────────────
    mt_x = MARGIN_X + half_w + gap
    _card_bg(c, mt_x, y, half_w, block_h, radius=6)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(mt_x + 10, y + block_h - 13, "MARGIN TREND")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(mt_x + 10, y + block_h - 22,
                  "Gross + operating margin over last 4-6 quarters  ·  durability check")
    mt_png = _make_margin_trend_chart(qinc, half_w - 24, block_h - 36)
    if mt_png:
        img = ImageReader(io.BytesIO(mt_png))
        c.drawImage(img, mt_x + 6, y + 4,
                    width=half_w - 12, height=block_h - 30,
                    preserveAspectRatio=True, anchor='c', mask='auto')
    else:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 9)
        c.drawCentredString(mt_x + half_w / 2, y + block_h / 2 - 6,
                             "Margin history unavailable")

    return y


# Human-friendly labels for the 19 score components (mirrors the JS
# COMPONENT_LABELS in templates/earnings_tearsheet.html).
_SCORE_COMPONENT_LABELS = {
    "growth_tier":           "Growth tier",
    "momentum_1m":           "1-month momentum",
    "rel_strength":          "Relative strength",
    "volume_spike":          "Volume spike",
    "rsi_zone":              "RSI zone",
    "dist_52w_high":         "Dist. 52w high",
    "analyst_cons":          "Analyst consensus",
    "earnings_prox":         "Earnings proximity",
    "low_short":             "Low short interest",
    "mkt_cap_fit":           "Market-cap fit",
    "fundamentals":          "Fundamentals",
    "social_momentum":       "Social momentum",
    "insider_bias":          "Insider bias",
    "earnings_quality":      "Earnings quality",
    "trend_strength":        "Trend strength",
    "breakout_proximity":    "Breakout proximity",
    "news_sentiment":        "News sentiment",
    "earnings_acceleration": "Earnings acceleration",
    "score_momentum":        "Score momentum",
}


def _draw_chart_and_breakdown(c, top_y, price_history, t):
    """Side-by-side row: 90-day price chart (left) + Score Breakdown
    bar chart (right). Replaces the single full-width chart so we
    surface the top-8 score contributions on page 1 — the panel that
    was in the old CRDO version of the tear sheet."""
    chart_h = 140
    chart_y = top_y - chart_h - 10
    gap     = 10
    half_w  = (CONTENT_W - gap) / 2

    # ── LEFT: 90-Day Price chart ──────────────────────────────────────
    _card_bg(c, MARGIN_X, chart_y, half_w, chart_h, radius=6)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(MARGIN_X + 10, chart_y + chart_h - 13, "90-DAY PRICE")
    chart_png = _make_price_chart(price_history, half_w - 20, chart_h - 32)
    if chart_png:
        if price_history and len(price_history) >= 2:
            first = _safe_float(price_history[0].get("close"))
            last  = _safe_float(price_history[-1].get("close"))
            if first and first > 0 and last is not None:
                perf = (last / first - 1) * 100
                # Subtitle: 'From $X to $Y · adjusted' (under title)
                c.setFillColor(INK_MUTED)
                c.setFont("Helvetica", 7)
                c.drawString(MARGIN_X + 10, chart_y + chart_h - 22,
                             f"From ${first:.2f} to ${last:.2f}  ·  adjusted")
                # Performance pill (right of title)
                pill_label = f"{('+' if perf >= 0 else '')}{perf:.1f}% · {len(price_history)}D"
                pw = c.stringWidth(pill_label, "Helvetica-Bold", 7) + 12
                pill_color = GREEN if perf >= 0 else RED
                c.setFillColor(pill_color)
                c.roundRect(MARGIN_X + half_w - pw - 10, chart_y + chart_h - 18, pw, 12, 3, stroke=0, fill=1)
                c.setFillColor(HexColor("#ffffff"))
                c.setFont("Helvetica-Bold", 7)
                c.drawString(MARGIN_X + half_w - pw - 4, chart_y + chart_h - 14.5, pill_label)
        img = ImageReader(io.BytesIO(chart_png))
        c.drawImage(img, MARGIN_X + 4, chart_y + 4,
                    width=half_w - 8, height=chart_h - 30,
                    preserveAspectRatio=True, anchor='c', mask='auto')
    else:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 9)
        c.drawCentredString(MARGIN_X + half_w / 2, chart_y + chart_h / 2,
                             "Price history unavailable")

    # ── RIGHT: Score Breakdown ────────────────────────────────────────
    sb_x = MARGIN_X + half_w + gap
    _card_bg(c, sb_x, chart_y, half_w, chart_h, radius=6)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(sb_x + 10, chart_y + chart_h - 13, "SCORE BREAKDOWN")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(sb_x + 10, chart_y + chart_h - 22,
                  "19-component composite  ·  contribution to Alpha Score")

    weighted = (t.get("weighted") or {}) if isinstance(t.get("weighted"), dict) else {}
    entries = sorted(
        [(k, v) for k, v in weighted.items()
         if isinstance(v, (int, float)) and v is not None and v > 0],
        key=lambda kv: kv[1], reverse=True,
    )[:8]

    if not entries:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 9)
        c.drawCentredString(sb_x + half_w / 2, chart_y + chart_h / 2 - 6,
                             "Score breakdown unavailable")
        return chart_y

    max_v = max(v for _, v in entries) or 1.0
    bar_area_x = sb_x + 92
    bar_area_w = half_w - 110
    row_h      = (chart_h - 38) / len(entries)
    label_font = "Helvetica"
    c.setFont(label_font, 7)

    for i, (k, v) in enumerate(entries):
        ry = chart_y + chart_h - 32 - row_h * (i + 1) + row_h * 0.18
        # Label
        c.setFillColor(INK_SOFT)
        c.setFont(label_font, 7)
        label = _SCORE_COMPONENT_LABELS.get(k, k.replace("_", " ").title())
        c.drawString(sb_x + 10, ry + 3, label[:22])
        # Bar (deeper green = larger contribution)
        bar_w = bar_area_w * (v / max_v)
        # Tween light green → deep green by value/10
        t01 = min(1.0, v / 10.0)
        r = int(132 + (21 - 132) * t01)
        g = int(204 + (128 - 204) * t01)
        b = int(22  + (61  - 22 ) * t01)
        bar_color = HexColor(f"#{r:02x}{g:02x}{b:02x}")
        c.setFillColor(bar_color)
        c.roundRect(bar_area_x, ry, max(2, bar_w), row_h * 0.6, 1.2, stroke=0, fill=1)
        # Value at end of bar
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(bar_area_x + bar_w + 3, ry + 2, f"{v:.1f}")

    return chart_y


# Legacy alias so callers (and old tests) using _draw_chart keep working.
def _draw_chart(c, top_y, price_history):
    """Deprecated — kept for backwards compatibility. Renders only the
    price chart at full width when no ticker row is available."""
    return _draw_chart_and_breakdown(c, top_y, price_history, {})


def _draw_metrics_grid(c, top_y, t):
    grid_y_top = top_y - 14
    gap = 6
    cell_w = (CONTENT_W - gap * 3) / 4
    cell_h = 44

    mcap = _safe_float(t.get("market_cap"))
    revg = _safe_float(t.get("rev_growth_qyoy"))
    if revg is not None: revg *= 100
    elif _safe_float(t.get("revenue_growth_yoy")) is not None:
        revg = _safe_float(t.get("revenue_growth_yoy")) * 100
    gm = _safe_float(t.get("gross_margin"))
    if gm is not None: gm *= 100
    pe = _safe_float(t.get("pe_ratio"))
    peg = _safe_float(t.get("peg_ratio"))
    # COMPUTED FALLBACK: if upstream PEG is missing but we have P/E +
    # YoY growth, derive PEG = P/E / growth%. Standard analyst formula.
    # User v3.13: 'Add all missing values in all places where there is
    # missing value.'
    if peg is None and pe is not None and pe > 0:
        growth_pct = _safe_float(t.get("rev_growth_yoy") or
                                  t.get("rev_growth_qyoy") or
                                  t.get("revenue_growth_yoy"))
        if growth_pct is not None:
            if abs(growth_pct) <= 5:    # stored as fraction
                growth_pct *= 100
            if growth_pct > 0:
                peg = pe / growth_pct
    de = _safe_float(t.get("debt_to_equity"))
    # COMPUTED FALLBACK for D/E: total_debt / total_equity if both present
    if de is None:
        td = _safe_float(t.get("total_debt"))
        te = _safe_float(t.get("total_equity") or t.get("stockholders_equity"))
        if td is not None and te is not None and te > 0:
            de = td / te
    roe = _safe_float(t.get("roe") or t.get("return_on_equity"))
    if roe is not None and abs(roe) <= 1: roe *= 100
    # COMPUTED FALLBACK for ROE: net_income / equity
    if roe is None:
        ni = _safe_float(t.get("net_income_ttm") or t.get("net_income"))
        te = _safe_float(t.get("total_equity") or t.get("stockholders_equity"))
        if ni is not None and te is not None and te > 0:
            roe = (ni / te) * 100
    fcfm = _safe_float(t.get("fcf_margin"))
    if fcfm is not None and abs(fcfm) <= 1: fcfm *= 100
    revttm = _safe_float(t.get("revenue_ttm"))
    ps = (mcap / revttm) if (mcap and revttm and revttm > 0) else None
    hi52 = _safe_float(t.get("high_52w"))
    lo52 = _safe_float(t.get("low_52w"))
    px = _safe_float(t.get("price"))
    # Defensive normalisation — ensure hi52 is always the larger value
    # and lo52 the smaller, regardless of which field the upstream
    # source mislabelled. APLD v3.10 showed '$67 – $49' because the
    # data source had high_52w=67, low_52w=49 and the display string
    # uses (lo52, hi52) but my swap was only checking lo52 > hi52 in
    # the swapped sense. Make it bulletproof: max/min instead of swap.
    if hi52 is not None and lo52 is not None:
        true_hi = max(hi52, lo52)
        true_lo = min(hi52, lo52)
        hi52, lo52 = true_hi, true_lo
    # Split-adjustment heuristic: if 52W low is <1/4 of current price AND
    # of 52W high, the low almost certainly comes from pre-split data
    # (LITE's $71-$1086 range vs $946 current is the classic example —
    # a 10:1 reverse split that the data source didn't carry through).
    # We scale the low up by the implied ratio to give a sane range bar.
    if hi52 and lo52 and px and lo52 > 0:
        ratio_to_px = px / lo52
        ratio_to_hi = hi52 / lo52
        if ratio_to_px >= 4 and ratio_to_hi >= 4:
            # Round to a sensible split factor (typically 2, 3, 4, 5, 10)
            for factor in (10, 5, 4, 3, 2):
                if hi52 / (lo52 * factor) <= 4:
                    lo52 = lo52 * factor
                    logger.info(f"PDF: 52W low split-adjusted x{factor} → ${lo52:.2f}")
                    break
    rsi = _safe_float(t.get("rsi_14"))
    eps_hist = t.get("eps_quarters") or []
    beats = sum(1 for q in eps_hist if q.get("beat"))
    total_eps = len(eps_hist)

    metrics = [
        ("Market Cap", _fmt_money(mcap),
            "Mega cap" if (mcap and mcap >= 1e11) else "Large cap" if (mcap and mcap >= 1e10) else
            "Mid cap"  if (mcap and mcap >= 2e9)  else "Small cap" if (mcap and mcap >= 3e8)  else "Micro cap",
            INK, mcap is None),
        ("Rev Growth (YoY)", f"{'+' if (revg or 0) >= 0 else ''}{revg:.1f}%" if revg is not None else "—",
            "Latest quarter",
            (GREEN if (revg or 0) >= 0 else RED) if revg is not None else INK, revg is None),
        ("Gross Margin", f"{gm:.1f}%" if gm is not None else "—",
            "Premium" if (gm and gm >= 50) else "Healthy" if (gm and gm >= 30) else "Thin" if gm is not None else "",
            INK, gm is None),
        ("FCF Margin", f"{fcfm:.1f}%" if fcfm is not None else "—",
            "Cash machine" if (fcfm and fcfm >= 20) else "Healthy" if (fcfm and fcfm >= 10) else
            "Modest" if (fcfm and fcfm >= 0) else "Burning" if fcfm is not None else "",
            INK, fcfm is None),
        ("P/E (TTM)", f"{pe:.1f}x" if pe is not None else "—",
            "Reasonable" if (pe and 0 < pe <= 30) else "Growth premium" if (pe and pe > 30) else
            "Loss-making" if (pe is not None and pe < 0) else "",
            INK, pe is None),
        ("P/S (TTM)", f"{ps:.1f}x" if ps is not None else "—",
            "Bargain" if (ps and ps < 1) else "Reasonable" if (ps and ps < 5) else
            "Growth premium" if (ps and ps < 15) else "Rich" if ps is not None else "",
            INK, ps is None),
        ("PEG", f"{peg:.2f}" if peg is not None else "—",
            "Cheap vs growth" if (peg and peg < 1) else "Fair vs growth" if (peg and peg < 2) else
            "Rich vs growth" if peg is not None else "",
            INK, peg is None),
        # v3.15 user feedback: 'ROE (TTM) Not reported, DEBT / EQUITY
        # Not reported. Check if you can not produce any value needs to
        # replace with better alternative. Cannot be empty.'
        # Those two fields were perma-empty for most US tickers. Swapped
        # for OP MARGIN (from quarterly_income, almost always available)
        # and 30D MOMENTUM (from t['momentum_30d'], populated by
        # ai_scorer). Both compute deterministically from data the
        # universe carries reliably.
        ("Op Margin", (
            (lambda v: f"{v:.1f}%" if v is not None else "—")(
                (lambda om: om*100 if (om is not None and abs(om) <= 1) else om)(
                    _safe_float(t.get("operating_margin")) or
                    ((_safe_float((t.get("quarterly_income") or [{}])[0].get("operating_income")) or 0) /
                     (_safe_float((t.get("quarterly_income") or [{}])[0].get("revenue")) or 1)
                     if (t.get("quarterly_income") and
                          _safe_float((t.get("quarterly_income") or [{}])[0].get("revenue")))
                     else None)
                )
            )
        ),
            "From quarterly_income",
            INK,
            not t.get("quarterly_income")),
        ("30D Momentum", (
            (lambda v: ("+" if v >= 0 else "") + f"{v:.1f}%" if v is not None else "—")(
                (lambda m: m*100 if (m is not None and abs(m) <= 1) else m)(
                    _safe_float(t.get("momentum_30d") or t.get("momentum_1m"))
                )
            )
        ),
            (lambda m: "Parabolic" if (m and m > 25) else "Strong" if (m and m > 10) else
                       "Healthy" if (m and m > 0) else "Pullback" if (m and m > -15) else
                       "Deep correction" if m is not None else "")(
                (lambda v: v*100 if (v is not None and abs(v) <= 1) else v)(
                    _safe_float(t.get("momentum_30d") or t.get("momentum_1m"))
                )
            ),
            INK,
            not (t.get("momentum_30d") or t.get("momentum_1m"))),
        ("52W Range", f"${lo52:.0f} – ${hi52:.0f}" if (hi52 and lo52) else "—",
            (f"{((px/hi52 - 1) * 100):.1f}% from high" if (hi52 and px) else ""),
            INK, not (hi52 and lo52)),
        ("RSI (14)", f"{int(rsi)}" if rsi is not None else "—",
            "Overbought" if (rsi and rsi > 70) else "Oversold" if (rsi and rsi < 30) else
            "Neutral" if rsi is not None else "",
            INK, rsi is None),
        ("EPS Beat Streak", f"{beats}/{total_eps}" if total_eps else "—",
            "Perfect record" if (total_eps and beats == total_eps) else
            "Strong" if (total_eps and beats >= total_eps - 1) else "Mixed" if total_eps else "",
            INK, not total_eps),
    ]
    for i, (lbl, val, sub, color, empty) in enumerate(metrics):
        col = i % 4
        row = i // 4
        x = MARGIN_X + col * (cell_w + gap)
        y = grid_y_top - cell_h - row * (cell_h + gap)
        _draw_metric_card(c, x, y, cell_w, cell_h, lbl, val, sub, value_color=color, empty=empty)
    return grid_y_top - cell_h * 3 - gap * 2


def _build_exec_bits(t, score, grade, tier_map):
    """Construct the executive summary sentences from real signals."""
    bits = []
    if score >= 80:   bits.append(f"Top-tier setup with Alpha Score {int(score)} ({tier_map.get(grade,'')} grade).")
    elif score >= 70: bits.append(f"Quality setup at Alpha Score {int(score)} ({tier_map.get(grade,'')}).")
    elif score >= 60: bits.append(f"Mid-tier score at {int(score)} — selective entry levels matter.")
    else:             bits.append(f"Below-tier score ({int(score)}). Stronger candidates exist in the universe.")
    revg = _safe_float(t.get("rev_growth_qyoy"))
    if revg is not None: revg *= 100
    elif _safe_float(t.get("revenue_growth_yoy")) is not None:
        revg = _safe_float(t.get("revenue_growth_yoy")) * 100
    if revg is not None:
        if revg >= 30:   bits.append(f"Revenue accelerating at +{revg:.0f}% YoY.")
        elif revg >= 10: bits.append(f"Mid-double-digit revenue growth (+{revg:.0f}% YoY).")
        elif revg < 0:   bits.append(f"Revenue contracting ({revg:.0f}% YoY) — verify the cycle.")
    eps_hist = t.get("eps_quarters") or []
    beats = sum(1 for q in eps_hist if q.get("beat"))
    total_eps = len(eps_hist)
    if total_eps and beats == total_eps:
        bits.append(f"Perfect {total_eps}-quarter EPS beat record.")
    elif beats >= 2:
        bits.append(f"{beats}-quarter EPS beat streak.")
    m1 = _safe_float(t.get("momentum_1m"))
    if m1 is not None and m1 >= 10:        bits.append(f"30-day momentum +{m1:.0f}%.")
    elif m1 is not None and m1 <= -10:     bits.append(f"30-day pullback {m1:.0f}% — bounce candidate or value trap?")
    revs = t.get("eps_revisions_30d") or {}
    ru, rd = revs.get("ups"), revs.get("downs")
    if ru is not None and rd is not None:
        if ru > rd * 2:     bits.append(f"Analyst revisions strongly accumulating ({ru} up / {rd} down).")
        elif rd > ru * 2:   bits.append(f"Analyst revisions trending lower ({ru} up / {rd} down).")
    tgt = _safe_float(t.get("target_mean"))
    px  = _safe_float(t.get("price"))
    if tgt and px:
        upside = (tgt / px - 1) * 100
        if upside <= 0:     bits.append(f"Currently above analyst mean target ({upside:.1f}% gap) — limited consensus headroom.")
        elif upside >= 20:  bits.append(f"Analyst mean target implies +{upside:.0f}% upside.")
    sp = _safe_float(t.get("short_percent_float"))
    if sp is not None:
        if abs(sp) <= 1: sp *= 100
        if sp > 15: bits.append(f"Elevated short interest ({sp:.1f}%) — squeeze + downside risk both raised.")
    beta = _safe_float(t.get("beta"))
    if beta is not None and beta > 2.0:
        bits.append(f"High beta ({beta:.2f}) — moves ~{beta:.1f}× the market.")
    return bits


def _draw_exec_summary(c, top_y, bits):
    """Brand-indigo block with the assembled summary as a wrapped paragraph.
    Pre-measures the paragraph height so the box always fits the content
    (no more clipping with '$1 billion buyback and…' cut off mid-sentence)."""
    text = " ".join(bits) if bits else "See metrics above for the full read."
    p = _wrap_paragraph(text, CONTENT_W - 30, font_size=9, leading=12, color=INK)
    _, ph = p.wrap(CONTENT_W - 30, 200)
    summary_h = max(60, ph + 28)   # label band + paragraph + pad
    y = top_y - summary_h - 10
    c.setFillColor(BRAND_LIGHT)
    c.roundRect(MARGIN_X, y, CONTENT_W, summary_h, 6, stroke=0, fill=1)
    c.setFillColor(BRAND_INDIGO)
    c.rect(MARGIN_X, y, 3, summary_h, stroke=0, fill=1)
    c.setFillColor(BRAND_INDIGO)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(MARGIN_X + 14, y + summary_h - 14, "EXECUTIVE SUMMARY")
    p.drawOn(c, MARGIN_X + 14, y + 8)
    return y


def _draw_target_range_bar(c, x, y, w, low, high, mean, price):
    """Horizontal range bar visualisation: $low ───●─── $high, with the
    current price marked as a vertical line. The MEAN target sits as a
    larger dot. Replaces the dry '$164 – $220' text label."""
    bar_h = 6
    bar_y = y
    # Bar background
    c.setFillColor(HexColor("#eef2ff"))
    c.roundRect(x, bar_y, w, bar_h, bar_h / 2, stroke=0, fill=1)
    # Inner gradient feel via two-tone fill: indigo-light → violet
    c.setFillColor(BRAND_LIGHT)
    c.roundRect(x, bar_y, w, bar_h, bar_h / 2, stroke=0, fill=1)
    # Determine extent
    lo = min(low, price or low, mean or low)
    hi = max(high, price or high, mean or high)
    rng = max(hi - lo, 0.01)

    def _to_x(v):
        return x + ((v - lo) / rng) * w

    # Mean dot
    if mean:
        mx = _to_x(mean)
        c.setFillColor(BRAND_INDIGO)
        c.circle(mx, bar_y + bar_h / 2, 5, stroke=0, fill=1)
        c.setFillColor(HexColor("#ffffff"))
        c.setFont("Helvetica-Bold", 6)
        c.drawCentredString(mx, bar_y + bar_h / 2 - 2, "T")
    # Current price marker (vertical line)
    if price:
        px = _to_x(price)
        c.setStrokeColor(INK)
        c.setLineWidth(1.6)
        c.line(px, bar_y - 4, px, bar_y + bar_h + 4)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawCentredString(px, bar_y - 12, f"${price:.2f}")
    # Endpoints labels
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 6.5)
    c.drawString(x, bar_y + bar_h + 4, f"${low:.0f}")
    c.drawRightString(x + w, bar_y + bar_h + 4, f"${high:.0f}")


def _draw_rec_distribution_bar(c, x, y, w, strong_buy, buy, hold, sell, strong_sell):
    """Stacked horizontal bar for analyst recommendation distribution.
    Strong Buy / Buy = greens, Hold = amber, Sell / Strong Sell = reds.
    Falls back gracefully when total is zero (renders nothing)."""
    total = strong_buy + buy + hold + sell + strong_sell
    if total <= 0:
        return
    bar_h = 7
    segments = [
        (strong_buy,  HexColor("#15803d")),  # deep green
        (buy,         HexColor("#F5A623")),  # green
        (hold,        HexColor("#f59e0b")),  # amber
        (sell,        HexColor("#ef4444")),  # red
        (strong_sell, HexColor("#991b1b")),  # deep red
    ]
    cx = x
    for count, color in segments:
        if count <= 0:
            continue
        seg_w = (count / total) * w
        c.setFillColor(color)
        c.rect(cx, y, seg_w, bar_h, stroke=0, fill=1)
        cx += seg_w
    # Mini legend underneath with counts
    legend_y = y - 9
    c.setFont("Helvetica", 5.5)
    parts = [
        ("S.Buy", strong_buy, HexColor("#15803d")),
        ("Buy",   buy,        HexColor("#F5A623")),
        ("Hold",  hold,       HexColor("#f59e0b")),
        ("Sell",  sell,       HexColor("#ef4444")),
        ("S.Sell",strong_sell,HexColor("#991b1b")),
    ]
    lx = x
    for label, count, color in parts:
        if count <= 0:
            continue
        c.setFillColor(color)
        c.circle(lx + 2, legend_y + 2, 1.6, stroke=0, fill=1)
        c.setFillColor(INK_SOFT)
        text = f"{label} {count}"
        c.drawString(lx + 6, legend_y, text)
        lx += c.stringWidth(text, "Helvetica", 5.5) + 12


def _derive_rec_counts(t: dict, total_analysts: int):
    """Estimate the 5-bucket recommendation distribution from the limited
    aggregate signals we have. Backend exposes strong_buy_pct + the
    analyst_cons component score (0-1) in the breakdown."""
    if not total_analysts:
        return 0, 0, 0, 0, 0
    strong_buy_pct = _safe_float(t.get("strong_buy_pct"))
    # Bug from v3.4: strong_buy_pct is sometimes stored as 0-1 fraction
    # (e.g. 0.55) and sometimes as 0-100 percent (e.g. 26.7). The raw
    # multiplication produced 'S.Buy 641' on a 24-analyst ticker.
    # Normalise to fraction.
    if strong_buy_pct is not None and strong_buy_pct > 1:
        strong_buy_pct = strong_buy_pct / 100.0
    breakdown = t.get("breakdown") or {}
    consensus  = _safe_float(breakdown.get("analyst_cons"))   # 0..1
    if consensus is not None and consensus > 1:
        consensus = consensus / 100.0
    sb = int(round((strong_buy_pct or 0.4) * total_analysts))
    # Buy share scales with bullish consensus (0.7+ → 0.35, 0.5 → 0.25)
    bullish = max(0.0, min(1.0, (consensus or 0.6)))
    b = int(round(bullish * 0.42 * total_analysts))
    h = max(0, total_analysts - sb - b)
    s = 0
    ss = 0
    # If consensus is weak, recover Hold→Sell split
    if (consensus or 0.6) < 0.45:
        s = max(0, int(round(h * 0.3)))
        h -= s
    # Clamp and re-balance to total
    counts = [sb, b, h, s, ss]
    diff = total_analysts - sum(counts)
    counts[2] += diff   # absorb residual in Hold bucket
    return tuple(max(0, x) for x in counts)


def _draw_analyst_outlook_row(c, top_y, t):
    """Page-1 row: Analyst Outlook (left, with range bar + rec
    distribution) + Ownership Snapshot (right). Range bar replaces the
    dry '$low–$high' text; rec distribution stack bar fills the empty
    space inside the card."""
    block_h = 110   # Taller now to fit range bar + rec distribution
    y = top_y - block_h - 10
    half_w = (CONTENT_W - 10) / 2

    # ── LEFT: Analyst Outlook ─────────────────────────────────────────
    left_x = MARGIN_X
    _card_bg(c, left_x, y, half_w, block_h, radius=6)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(left_x + 10, y + block_h - 13, "ANALYST OUTLOOK")

    tgt_mean = _safe_float(t.get("target_mean"))
    tgt_low  = _safe_float(t.get("target_low"))
    tgt_high = _safe_float(t.get("target_high"))
    price    = _safe_float(t.get("price") or t.get("last_close"))
    total_an = int(t.get("total_analysts") or 0)

    # Mean target + upside (top row)
    if tgt_mean and price and price > 0:
        upside = (tgt_mean / price - 1) * 100
        u_color = GREEN if upside >= 0 else RED
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(left_x + 10, y + block_h - 36, f"${tgt_mean:.2f}")
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 6.5)
        c.drawString(left_x + 10, y + block_h - 44, "MEAN TARGET  ·  " +
                     (f"{total_an} analyst" + ('s' if total_an != 1 else '')))
        c.setFillColor(u_color)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawRightString(left_x + half_w - 12, y + block_h - 36,
                          f"{'+' if upside >= 0 else ''}{upside:.1f}% upside")
    else:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 9)
        c.drawString(left_x + 10, y + block_h - 36, "No analyst target data")

    # Target range bar (middle row)
    if tgt_low and tgt_high and tgt_low < tgt_high:
        _draw_target_range_bar(
            c, left_x + 14, y + 48, half_w - 28,
            tgt_low, tgt_high, tgt_mean, price,
        )

    # Recommendation distribution (bottom)
    if total_an:
        sb, b, h, s, ss = _derive_rec_counts(t, total_an)
        if sb + b + h + s + ss > 0:
            _draw_rec_distribution_bar(c, left_x + 14, y + 22, half_w - 28,
                                        sb, b, h, s, ss)

    # ── RIGHT: PRICE & VOLATILITY (replaces Ownership card) ──────────
    # v3.15 user feedback: Ownership cells were perma-empty for almost
    # every ticker (FMP profile doesn't reliably ship insider /
    # institutional / dividend yield). Replaced with metrics we
    # always have from price + computed momentum signals:
    #   - 52W RETURN  (current vs 1 year ago, derivable from 52W range)
    #   - 30D MOM     (rev_growth_qyoy / momentum_30d / momentum_1m)
    #   - BETA 5Y     (or short % float if beta missing)
    #   - AVG VOLUME  (from t.avg_volume_str)
    right_x = left_x + half_w + 10
    _card_bg(c, right_x, y, half_w, block_h, radius=6)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(right_x + 10, y + block_h - 13, "PRICE & VOLATILITY")
    o_cols  = 4
    o_gap   = 4
    o_inner_w = half_w - 20
    o_cell_w  = (o_inner_w - o_gap * (o_cols - 1)) / o_cols
    o_y       = y + 14

    # ── 52W return ─────────────────────────────────────────────────
    price = _safe_float(t.get("price") or t.get("last_close"))
    hi52  = _safe_float(t.get("high_52w"))
    lo52  = _safe_float(t.get("low_52w"))
    if hi52 is not None and lo52 is not None and hi52 < lo52:
        hi52, lo52 = lo52, hi52
    # Estimate 52W return — current vs 52W low (typical 'off-the-low'
    # framing) when no anchor 1yr-ago price is available
    perf_52w = None
    if price and lo52 and lo52 > 0:
        perf_52w = (price / lo52 - 1) * 100
    # ── 30D momentum ──────────────────────────────────────────────
    mom30 = _safe_float(t.get("momentum_30d") or t.get("momentum_1m"))
    if mom30 is not None and abs(mom30) <= 1: mom30 *= 100
    # ── Beta (or short % float as fallback) ──────────────────────
    beta = _safe_float(t.get("beta_5y") or t.get("beta"))
    short_p = _safe_float(t.get("short_pct_float"))
    if short_p is not None and short_p <= 1: short_p *= 100
    # ── Avg volume ─────────────────────────────────────────────────
    raw_avg = t.get("avg_volume_str")
    if raw_avg in (None, "", "—", "-"): raw_avg = None
    avg_vol = raw_avg or (_fmt_vol(t.get("avg_volume")) if t.get("avg_volume") else None)

    def _fmt_pct(v, signed=True):
        if v is None: return None
        s = "+" if v >= 0 and signed else ""
        return f"{s}{v:.1f}%"
    def _color_pct(v):
        if v is None: return INK
        return GREEN if v >= 0 else RED

    cells = []
    cells.append((
        "52W RETURN",
        _fmt_pct(perf_52w) if perf_52w is not None else "—",
        "vs 52W low", _color_pct(perf_52w),
    ))
    cells.append((
        "30D MOM",
        _fmt_pct(mom30) if mom30 is not None else "—",
        "vs 30 days ago", _color_pct(mom30),
    ))
    # Pick whichever risk indicator we have — beta preferred
    if beta is not None:
        beta_note = "Aggressive" if beta >= 1.5 else "Market-like" if beta >= 0.8 else "Defensive"
        cells.append(("BETA (5Y)", f"{beta:.2f}", beta_note, INK))
    elif short_p is not None:
        cells.append(("SHORT % FLOAT", f"{short_p:.1f}%",
                       "Heavy" if short_p > 15 else "Elevated" if short_p > 7 else "Low",
                       INK))
    else:
        cells.append(("VOLATILITY", "—", "n/a", INK_MUTED))
    cells.append(("AVG VOLUME", avg_vol or "—", "Daily shares",
                   INK if avg_vol else INK_MUTED))

    for i, (label, val, sub, val_color) in enumerate(cells):
        cx = right_x + 10 + i * (o_cell_w + o_gap)
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(cx, o_y + 50, label)
        empty = val == "—"
        c.setFillColor(INK_MUTED if empty else val_color)
        c.setFont("Helvetica-Bold", 11 if not empty else 10)
        c.drawString(cx, o_y + 32, str(val))
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 6)
        c.drawString(cx, o_y + 18, sub)

    return y


def _draw_dual_block(c, top_y, t):
    """Two cards side-by-side: Recent Earnings bullets + Quality & Risk Signals.
    Shorter than v3.9 to prevent collision with the page-1 footer
    disclaimer band (the 'Daily shares' / 'Aggressive' subtitle cells
    were overlapping the disclaimer text)."""
    block_h = 112   # Tightened from 130
    y = top_y - block_h - 8
    half_w = (CONTENT_W - 10) / 2

    # ── LEFT: Recent Earnings Highlights ─────────────────────────────
    left_x = MARGIN_X
    _card_bg(c, left_x, y, half_w, block_h, radius=6)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(left_x + 10, y + block_h - 13, "RECENT EARNINGS HIGHLIGHTS")

    # Build bullets (Quartr-style: metric + YoY comparison inline)
    inc = (t.get("quarterly_income") or [{}])[0] or {}
    _qi = t.get("quarterly_income") or []
    inc_prev = _qi[3] if len(_qi) > 3 else {}
    eps0 = (t.get("eps_quarters") or [{}])[0] or {}
    bullets = []
    rev_cur = _safe_float(inc.get("revenue"))
    if rev_cur:
        line = f"Revenue {_fmt_money(rev_cur)}"
        rev_prev = _safe_float(inc_prev.get("revenue"))
        if rev_prev and rev_prev > 0:
            yoy = (rev_cur / rev_prev - 1) * 100
            line += f", {'+' if yoy >= 0 else ''}{yoy:.0f}% YoY"
        bullets.append(line)
    eps_actual = _safe_float(eps0.get("actual"))
    eps_est    = _safe_float(eps0.get("estimate"))
    if eps_actual is not None and eps_est is not None:
        surp = ((eps_actual - eps_est) / abs(eps_est)) * 100 if eps_est else None
        if surp is not None:
            bullets.append(f"EPS ${eps_actual:.2f} vs ${eps_est:.2f} est, "
                           f"{'beat' if surp >= 0 else 'missed'} by {abs(surp):.1f}%")
        else:
            bullets.append(f"EPS ${eps_actual:.2f}")
    gm_cur = _safe_float(inc.get("gross_margin"))
    gm_prev = _safe_float(inc_prev.get("gross_margin"))
    if gm_cur is not None:
        line = f"Gross margin {gm_cur*100:.1f}%"
        if gm_prev is not None:
            bps = (gm_cur - gm_prev) * 100 * 100
            line += f" ({'+' if bps >= 0 else ''}{bps:.0f} bps YoY)"
        bullets.append(line)
    streak = t.get("eps_beat_streak")
    if streak and streak >= 2:
        avg_surp = _safe_float(t.get("avg_eps_surprise_pct"))
        if avg_surp is not None:
            if abs(avg_surp) <= 1: avg_surp *= 100
            bullets.append(f"{streak}-quarter beat streak · avg surprise {'+' if avg_surp >= 0 else ''}{avg_surp:.1f}%")
        else:
            bullets.append(f"{streak}-quarter EPS beat streak.")
    if not bullets:
        bullets.append("Quarterly earnings data not in our cache for this ticker.")

    # Render bullets
    by = y + block_h - 30
    c.setFillColor(INK)
    c.setFont("Helvetica", 8.5)
    for b in bullets[:6]:
        # Bullet dot
        c.setFillColor(BRAND_INDIGO)
        c.circle(left_x + 14, by + 3, 2, stroke=0, fill=1)
        # Text — wrap if long
        p = _wrap_paragraph(b, half_w - 28, font_size=8.5, leading=11)
        w, ph = p.wrap(half_w - 28, 25)
        p.drawOn(c, left_x + 22, by - ph + 7)
        by -= max(12, ph + 2)
        if by < y + 10:
            break

    # ── RIGHT: Quality & Risk Signals ────────────────────────────────
    right_x = left_x + half_w + 10
    _card_bg(c, right_x, y, half_w, block_h, radius=6)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(right_x + 10, y + block_h - 13, "QUALITY & RISK SIGNALS")
    # 3 × 2 grid of small cells
    sg_cols, sg_rows = 3, 2
    sg_gap = 5
    sg_inner_w = half_w - 20
    sg_inner_h = block_h - 26
    sg_cell_w = (sg_inner_w - sg_gap * (sg_cols - 1)) / sg_cols
    sg_cell_h = (sg_inner_h - sg_gap * (sg_rows - 1)) / sg_rows
    sg_origin_x = right_x + 10
    sg_origin_y = y + 8

    # Compute cell contents
    ins_b = t.get("insider_buys_90d") or 0
    ins_s = t.get("insider_sells_90d") or 0
    if ins_b + ins_s > 0:
        ins_val = f"{ins_b}B / {ins_s}S"
        ins_sub = "Net buying" if ins_b > ins_s else "Net selling" if ins_s > ins_b else "Balanced"
        ins_color = GREEN if ins_b > ins_s else RED if ins_s > ins_b else INK
    else:
        ins_val, ins_sub, ins_color = "Quiet", "No filings (90d)", INK_MUTED

    revs = t.get("eps_revisions_30d") or {}
    ru, rd = revs.get("ups"), revs.get("downs")
    if ru is not None or rd is not None:
        ru, rd = ru or 0, rd or 0
        rev_val = f"↑ {ru} / ↓ {rd}"
        if   ru > rd * 2:   rev_sub, rev_color = "Strong accumulation", GREEN
        elif ru > rd:        rev_sub, rev_color = "Net positive", GREEN
        elif rd > ru * 2:   rev_sub, rev_color = "Distribution", RED
        elif rd > ru:        rev_sub, rev_color = "Net negative", RED
        else:                rev_sub, rev_color = "Mixed", INK
    else:
        rev_val, rev_sub, rev_color = "No data", "Limited coverage", INK_MUTED

    sp_raw = _safe_float(t.get("short_percent_float"))
    if sp_raw is not None and abs(sp_raw) <= 1: sp_raw *= 100
    sp_val = f"{sp_raw:.1f}%" if sp_raw is not None else "Not reported"
    sp_sub = ("Heavy" if sp_raw and sp_raw > 20 else
              "Elevated" if sp_raw and sp_raw > 10 else
              "Low" if sp_raw is not None else "")

    beta = _safe_float(t.get("beta"))
    beta_val = f"{beta:.2f}" if beta is not None else "Not reported"
    beta_sub = ("Defensive" if beta and beta < 0.8 else
                "Market-like" if beta and beta < 1.3 else
                "Aggressive" if beta and beta < 2.0 else
                "Very volatile" if beta is not None else "")

    avg_surp = _safe_float(t.get("avg_eps_surprise_pct"))
    if avg_surp is not None:
        if abs(avg_surp) <= 1: avg_surp *= 100
        surp_val = f"{'+' if avg_surp >= 0 else ''}{avg_surp:.1f}%"
        surp_color = GREEN if avg_surp >= 0 else RED
    else:
        surp_val, surp_color = "Not reported", INK_MUTED
    surp_sub = "Last 4Q avg"

    avg_vol = _safe_float(t.get("avg_volume") or t.get("volume_avg_30d") or t.get("volume"))
    vol_val = _fmt_vol(avg_vol) if avg_vol else "Not reported"
    vol_sub = "Daily shares"

    sg_cells = [
        ("90d Insider", ins_val, ins_sub, ins_color),
        ("EPS Revisions (30d)", rev_val, rev_sub, rev_color),
        ("Short % Float", sp_val, sp_sub, INK),
        ("Beta (5Y)", beta_val, beta_sub, INK),
        ("Avg EPS Surprise", surp_val, surp_sub, surp_color),
        ("Avg Volume", vol_val, vol_sub, INK),
    ]
    for i, (lbl, val, sub, color) in enumerate(sg_cells):
        col = i % sg_cols
        row = i // sg_cols
        cx = sg_origin_x + col * (sg_cell_w + sg_gap)
        cy = sg_origin_y + (sg_rows - 1 - row) * (sg_cell_h + sg_gap)
        # Sub-card background
        c.setFillColor(BG_SOFT)
        c.setStrokeColor(BORDER_LIGHT)
        c.setLineWidth(0.4)
        c.roundRect(cx, cy, sg_cell_w, sg_cell_h, 4, stroke=1, fill=1)
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica-Bold", 6)
        c.drawString(cx + 6, cy + sg_cell_h - 10, lbl.upper())
        c.setFillColor(color)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(cx + 6, cy + sg_cell_h - 24, str(val))
        if sub:
            c.setFillColor(INK_MUTED)
            c.setFont("Helvetica", 6)
            c.drawString(cx + 6, cy + 5, sub)
    return y


# ── Page 2 drawers (AI narrative) ────────────────────────────────────

def _draw_exec_summary_para(c, top_y, paragraph_text):
    """AI-prose version of the exec summary — used when narrative is
    available. Same brand-indigo card but with a wrapped paragraph
    instead of stitched bullet fragments."""
    summary_h = 72
    y = top_y - summary_h - 10
    c.setFillColor(BRAND_LIGHT)
    c.roundRect(MARGIN_X, y, CONTENT_W, summary_h, 6, stroke=0, fill=1)
    c.setFillColor(BRAND_INDIGO)
    c.rect(MARGIN_X, y, 3, summary_h, stroke=0, fill=1)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(MARGIN_X + 14, y + summary_h - 14, "EXECUTIVE SUMMARY")
    p = _wrap_paragraph(paragraph_text, CONTENT_W - 30, font_size=9, leading=12, color=INK)
    w, h = p.wrap(CONTENT_W - 30, summary_h - 22)
    p.drawOn(c, MARGIN_X + 14, y + summary_h - 18 - h)
    return y


def _draw_page2_hero(c, ticker, t, subtitle=None):
    """No-op on pages 2+ per v3.13 user feedback: 'dont add any heading
    like stock name from second page onwards.' The ticker is already
    obvious from page 1 and from the header AlphaHunt mark + page
    label. Returns the y-coordinate where content can start (just
    below the header divider)."""
    return A4_H - 90


def _conviction_color(level: str):
    level = (level or "medium").lower()
    if level == "high":   return GREEN, GREEN_LIGHT, "HIGH CONVICTION"
    if level == "low":    return RED,   RED_LIGHT,   "LOW CONVICTION"
    return AMBER, HexColor("#fef3c7"), "MEDIUM CONVICTION"


def _draw_investment_thesis(c, top_y, narrative):
    """Bull / Bear two-column block with conviction chip + verdict line.
    Returns y-coordinate of the bottom of the block."""
    bull   = narrative.get("bull")   or []
    bear   = narrative.get("bear")   or []
    verdict = (narrative.get("verdict") or "").strip()
    observation = (narrative.get("observation") or "").strip()
    conv_color, conv_bg, conv_label = _conviction_color(narrative.get("conviction"))

    # KEY TAKEAWAY removed in v3.13 per user feedback — the takeaway
    # already lives on the Briefings web UI when the user opens any
    # stock (rendered by v2RenderBriefingHTML in dashboard.html).
    # Duplicating it in the PDF wasted vertical space and repeated
    # content the reader already saw before downloading.

    # Title row
    title_y = top_y
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(MARGIN_X, title_y, "INVESTMENT THESIS")
    # Conviction chip on the right
    chip_label_w = c.stringWidth(conv_label, "Helvetica-Bold", 7)
    chip_w = chip_label_w + 12
    chip_h = 13
    chip_x = A4_W - MARGIN_X - chip_w
    chip_y = title_y - 3
    c.setFillColor(conv_bg)
    c.roundRect(chip_x, chip_y, chip_w, chip_h, 6, stroke=0, fill=1)
    c.setFillColor(conv_color)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(chip_x + 6, chip_y + 4, conv_label)

    # Bull / Bear cards. Card height pre-measured against the actual
    # wrapped bullet heights so the verdict band never overlaps the
    # cards even when bullets run 3+ lines each.
    card_top = title_y - 12
    half_w   = (CONTENT_W - 10) / 2
    avail_w  = half_w - 24

    def _measured_card_h(bullets):
        total = 22 + 10  # header strip + bottom pad
        for b in (bullets or [])[:3]:
            if not b:
                continue
            p = _wrap_paragraph(str(b), avail_w, font_size=8, leading=10.5, color=INK)
            _, h = p.wrap(avail_w, 200)
            total += h + 8
        return max(total, 90)

    card_h = max(_measured_card_h(bull), _measured_card_h(bear))
    card_y   = card_top - card_h

    # ── Bull card ─────────────────────────────────────────────────────
    _card_bg(c, MARGIN_X, card_y, half_w, card_h, radius=6)
    # Header strip
    c.setFillColor(GREEN_LIGHT)
    c.roundRect(MARGIN_X, card_y + card_h - 22, half_w, 22, 6, stroke=0, fill=1)
    c.setFillColor(BG_CARD)
    c.rect(MARGIN_X, card_y + card_h - 22, half_w, 7, stroke=0, fill=1)  # square off bottom
    c.setFillColor(GREEN_LIGHT)
    c.rect(MARGIN_X, card_y + card_h - 22, half_w, 15, stroke=0, fill=1)
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(MARGIN_X + 10, card_y + card_h - 15, "▲ BULL CASE")
    # Bullets
    by = card_y + card_h - 32
    c.setFillColor(INK)
    for b in bull[:3]:
        if not b: continue
        c.setFillColor(GREEN)
        c.circle(MARGIN_X + 12, by + 3, 1.5, stroke=0, fill=1)
        p = _wrap_paragraph(str(b), half_w - 24, font_size=8, leading=10.5, color=INK)
        w, h = p.wrap(half_w - 24, card_h)
        p.drawOn(c, MARGIN_X + 18, by - h + 8)
        by -= (h + 6)

    # ── Bear card ─────────────────────────────────────────────────────
    bx = MARGIN_X + half_w + 10
    _card_bg(c, bx, card_y, half_w, card_h, radius=6)
    c.setFillColor(RED_LIGHT)
    c.roundRect(bx, card_y + card_h - 22, half_w, 22, 6, stroke=0, fill=1)
    c.setFillColor(BG_CARD)
    c.rect(bx, card_y + card_h - 22, half_w, 7, stroke=0, fill=1)
    c.setFillColor(RED_LIGHT)
    c.rect(bx, card_y + card_h - 22, half_w, 15, stroke=0, fill=1)
    c.setFillColor(RED)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(bx + 10, card_y + card_h - 15, "▼ BEAR CASE")
    by = card_y + card_h - 32
    for b in bear[:3]:
        if not b: continue
        c.setFillColor(RED)
        c.circle(bx + 12, by + 3, 1.5, stroke=0, fill=1)
        p = _wrap_paragraph(str(b), half_w - 24, font_size=8, leading=10.5, color=INK)
        w, h = p.wrap(half_w - 24, card_h)
        p.drawOn(c, bx + 18, by - h + 8)
        by -= (h + 6)

    # ── Verdict band ──────────────────────────────────────────────────
    # Layout: 'VERDICT' label on its own line at the top of the band,
    # paragraph on subsequent line(s) below. Pre-measure the paragraph
    # height so the band is always tall enough — no overlap, no clipping.
    p = _wrap_paragraph(verdict or "—", CONTENT_W - 30, font_size=9.5,
                         leading=12.5, color=INK)
    _, ph = p.wrap(CONTENT_W - 30, 200)
    verdict_h = max(36, ph + 22)  # 22 = label + top/bottom pad
    verdict_y = card_y - verdict_h - 8
    c.setFillColor(BRAND_LIGHT)
    c.roundRect(MARGIN_X, verdict_y, CONTENT_W, verdict_h, 6, stroke=0, fill=1)
    c.setFillColor(BRAND_INDIGO)
    c.rect(MARGIN_X, verdict_y, 3, verdict_h, stroke=0, fill=1)
    # Label on its own line, top-aligned in the band
    c.setFillColor(BRAND_INDIGO)
    c.setFont("Helvetica-Bold", 7.5)
    c.drawString(MARGIN_X + 14, verdict_y + verdict_h - 12, "VERDICT")
    # Paragraph below the label
    p.drawOn(c, MARGIN_X + 14, verdict_y + 6)

    return verdict_y


def _draw_catalysts(c, top_y, catalysts):
    """Forward catalysts strip — bordered card with bulleted forward events.
    Each bullet wraps to as many lines as needed; pre-measures the card
    height from actual wrapped paragraph dimensions so we never overflow
    or truncate with an ellipsis."""
    if not catalysts:
        return top_y
    items = [str(x) for x in catalysts if x][:5]
    if not items:
        return top_y

    title_y = top_y
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(MARGIN_X, title_y, "FORWARD CATALYSTS")

    card_top = title_y - 12
    avail_w  = CONTENT_W - 30
    paras = []
    for item in items:
        p = _wrap_paragraph(str(item), avail_w, font_size=9, leading=11.5, color=INK)
        _, h = p.wrap(avail_w, 100)
        paras.append((p, h))
    card_h = 14 + sum(h + 5 for _, h in paras) + 6   # top pad + items + bottom pad
    card_y = card_top - card_h

    _card_bg(c, MARGIN_X, card_y, CONTENT_W, card_h, radius=6)
    c.setFillColor(BRAND_VIOLET)
    c.rect(MARGIN_X, card_y, 3, card_h, stroke=0, fill=1)

    iy = card_y + card_h - 10
    for p, h in paras:
        # Bullet dot aligns with the first line of the wrapped paragraph
        c.setFillColor(BRAND_VIOLET)
        c.circle(MARGIN_X + 16, iy - 5, 1.6, stroke=0, fill=1)
        p.drawOn(c, MARGIN_X + 24, iy - h)
        iy -= (h + 5)

    return card_y


def _draw_event_summary(c, top_y, event_row, today_str=None, quarter_lbl=None,
                         ticker="", t=None):
    """Render the Quartr-style event summary block with dynamic section
    headings + bullets. When content overflows the page, auto-paginates
    to a continuation page so the LATEST EVENT section never cuts off
    mid-section (the v3.2 truncation bug)."""
    title_y = top_y
    if title_y < MARGIN_BOTTOM + 80:
        # No room on current page — flush to next page and continue there
        c.showPage()
        if today_str and quarter_lbl:
            _draw_header(c, today_str, quarter_lbl,
                          page_label="LATEST EVENT  ·  CONT.")
        title_y = A4_H - 90

    title = (event_row.get("event_title") or "Latest event").strip()
    date_s = (event_row.get("event_date") or "").strip()

    def _draw_title(y_pos, suffix=""):
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 11)
        # For continuation pages use a SHORT title so the '(CONT.)'
        # suffix doesn't clip the event name (user v3.13 saw
        # 'NOUNCEME (CONT.)' truncated). Continuation pages don't
        # need the full event name repeated — short marker suffices.
        if suffix:
            label = "LATEST EVENT  ·  CONTINUED"
        else:
            label = "LATEST EVENT — " + title.upper()[:60]
        c.drawString(MARGIN_X, y_pos, label)
        if date_s:
            c.setFillColor(INK_MUTED)
            c.setFont("Helvetica", 8.5)
            c.drawRightString(A4_W - MARGIN_X, y_pos, date_s)

    _draw_title(title_y)
    y = title_y - 16
    reserve_y = MARGIN_BOTTOM + 50

    sections = event_row.get("sections")
    items = []
    if isinstance(sections, list) and sections:
        for s in sections:
            heading = (s or {}).get("heading", "")
            bullets = (s or {}).get("bullets", []) or []
            if heading and bullets:
                items.append((heading, bullets))
    else:
        for label, key in [("Key updates",  "key_updates"),
                            ("Operations",   "operations"),
                            ("Outlook",      "outlook"),
                            ("Risks",        "risks")]:
            vals = event_row.get(key) or []
            if isinstance(vals, list) and vals:
                items.append((label, vals))

    def _new_continuation_page():
        nonlocal y
        _draw_footer(c)
        c.showPage()
        if today_str and quarter_lbl:
            _draw_header(c, today_str, quarter_lbl,
                          page_label="LATEST EVENT  ·  CONT.")
        new_top = A4_H - 90
        _draw_title(new_top, suffix=" (CONT.)")
        y = new_top - 16

    for heading, bullets in items:
        if y < reserve_y + 36:
            _new_continuation_page()
        # Heading — bigger padding above + below so it doesn't kiss
        # the previous section's last bullet OR the first bullet
        # below it. User feedback v3.11: 'space issue between heading
        # and content'.
        y -= 4   # extra breathing room ABOVE the heading
        c.setFillColor(BRAND_INDIGO)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(MARGIN_X, y, heading)
        # Thin indigo underline so the heading separates visually
        head_w = c.stringWidth(heading, "Helvetica-Bold", 8.5)
        c.setStrokeColor(BRAND_INDIGO)
        c.setLineWidth(0.4)
        c.line(MARGIN_X, y - 2, MARGIN_X + head_w, y - 2)
        y -= 14   # was 11 — gives 3 more pt before first bullet
        for b in bullets[:5]:
            p = _wrap_paragraph("•  " + str(b), CONTENT_W - 4,
                                 font_size=8, leading=10.5, color=INK)
            _, h = p.wrap(CONTENT_W - 4, 100)
            if y - h < reserve_y + 12:
                _new_continuation_page()
            p.drawOn(c, MARGIN_X + 4, y - h)
            y -= (h + 2)
        y -= 6   # was 4 — extra space BELOW the section


def _draw_endnotes_page(c, today_str, quarter_lbl):
    """Dedicated final page: full disclaimer paragraph + glossary of
    abbreviations used throughout the report. v3.19 — replaces the
    footer disclaimer band per user feedback 'Add Disclaimer as last
    para instead of footer. And add abbreviation list used in this
    report on the last page.'"""
    _draw_header(c, today_str, quarter_lbl,
                 page_label="DISCLAIMER & GLOSSARY")
    y = A4_H - 100

    # ── DISCLAIMER ────────────────────────────────────────────────
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(MARGIN_X, y, "DISCLAIMER")
    # Underline accent
    c.setStrokeColor(BRAND_INDIGO)
    c.setLineWidth(1.2)
    c.line(MARGIN_X, y - 4, MARGIN_X + 90, y - 4)
    y -= 16

    disclaimer_html = (
        "<b>Educational use only.</b> AlphaHunt is not a SEBI-registered "
        "investment advisor and does not provide buy / sell recommendations. "
        "The Alpha Score is a quantitative composite for screening purposes; "
        "it is not investment advice. Past performance does not guarantee "
        "future results. Forward-looking statements in this report reflect "
        "analyst estimates and AI-augmented synthesis of public filings — "
        "actual outcomes may differ materially."
        "<br/><br/>"
        "<b>Data sources:</b> SEC EDGAR (8-K, 10-Q filings), Yahoo Finance, "
        "Financial Modeling Prep (FMP), and Anthropic Haiku 4.5 for narrative "
        "synthesis. All figures are derived from publicly available data as "
        "of the report generation date."
        "<br/><br/>"
        "<b>Always conduct your own research</b> and consult a registered "
        "financial advisor before making investment decisions. AlphaHunt "
        "assumes no liability for investment outcomes based on this report."
    )
    p = _wrap_paragraph(disclaimer_html, CONTENT_W,
                         font_size=9, leading=12.5, color=INK)
    _, ph = p.wrap(CONTENT_W, 300)
    p.drawOn(c, MARGIN_X, y - ph)
    y -= (ph + 28)

    # ── GLOSSARY ─────────────────────────────────────────────────
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(MARGIN_X, y, "GLOSSARY OF ABBREVIATIONS")
    c.setStrokeColor(BRAND_INDIGO)
    c.setLineWidth(1.2)
    c.line(MARGIN_X, y - 4, MARGIN_X + 195, y - 4)
    y -= 18

    glossary = [
        ("FCF",     "Free Cash Flow — cash from operations less capex"),
        ("EBITDA",  "Earnings Before Interest, Taxes, Depreciation & Amortization"),
        ("EPS",     "Earnings Per Share"),
        ("P/E",     "Price-to-Earnings ratio (TTM unless noted)"),
        ("P/S",     "Price-to-Sales ratio (TTM)"),
        ("PEG",     "P/E ratio ÷ earnings growth rate"),
        ("ROE",     "Return on Equity (net income ÷ shareholder equity)"),
        ("ROIC",    "Return on Invested Capital"),
        ("D/E",     "Debt-to-Equity ratio"),
        ("OCF",     "Operating Cash Flow"),
        ("GAAP",    "Generally Accepted Accounting Principles (U.S.)"),
        ("YoY",     "Year-over-Year (vs same quarter prior year)"),
        ("QoQ",     "Quarter-over-Quarter (vs immediately prior quarter)"),
        ("TTM",     "Trailing Twelve Months"),
        ("FY",      "Fiscal Year (may differ from calendar year)"),
        ("RSI",     "Relative Strength Index (technical, 0-100)"),
        ("ARR",     "Annual Recurring Revenue (subscription businesses)"),
        ("WACC",    "Weighted Average Cost of Capital (DCF discount rate)"),
        ("TAM",     "Total Addressable Market"),
        ("DCF",     "Discounted Cash Flow valuation method"),
        ("bps",     "Basis Points (1 bp = 0.01 %)"),
        ("Capex",   "Capital Expenditures"),
        ("Op",      "Operating (as in Op Margin, Op Income)"),
        ("GM",      "Gross Margin"),
        ("52W",     "52-Week (typically high / low price range)"),
        ("NRR",     "Net Revenue Retention (subscription cohort retention)"),
    ]

    # 2-column layout: 13 terms per column
    col_w        = (CONTENT_W - 24) / 2
    rows_per_col = (len(glossary) + 1) // 2
    term_col_w   = 56   # left-side term column width in each section

    for i, (term, defn) in enumerate(glossary):
        col = i // rows_per_col
        row = i % rows_per_col
        rx = MARGIN_X + col * (col_w + 24)
        ry = y - (row + 1) * 14
        # Term (indigo, bold, mono-ish)
        c.setFillColor(BRAND_INDIGO)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(rx, ry, term)
        # Definition (slate)
        c.setFillColor(INK_SOFT)
        c.setFont("Helvetica", 8)
        # Trim to fit column
        max_w = col_w - term_col_w
        text = defn
        while c.stringWidth(text, "Helvetica", 8) > max_w and len(text) > 10:
            text = text[:-2]
        if text != defn:
            text = text.rstrip(" ,;.") + "…"
        c.drawString(rx + term_col_w, ry, text)

    _draw_footer(c, with_disclaimer=False)


def _draw_footer(c, with_disclaimer=False):
    """Branded footer. v3.19: the disclaimer text NEVER renders in the
    footer anymore — moved to a dedicated 'Disclaimer & Glossary'
    endnotes page (rendered as the absolute last page by
    _draw_endnotes_page). The with_disclaimer flag is kept for
    back-compat but is now ignored. Every page footer is the compact
    brand-mark + alphahunt.in URL only. User v3.18: 'Add Disclaimer as
    last para instead of footer.'"""
    foot_y = MARGIN_BOTTOM
    # Thin grey divider — always shown
    c.setStrokeColor(HexColor("#d4d8e8"))
    c.setLineWidth(0.5)
    c.line(MARGIN_X, foot_y + 26, A4_W - MARGIN_X, foot_y + 26)

    # v3.19: disclaimer text no longer renders here — see endnotes page.
    # Brand mark + URL on the right — every page
    _draw_brand_mark(c, A4_W - MARGIN_X - 64, foot_y + 6, size=14)
    c.setFillColor(BRAND_INDIGO)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(A4_W - MARGIN_X - 46, foot_y + 12, "alphahunt.in")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 6.5)
    c.drawRightString(A4_W - MARGIN_X, foot_y + 2, "Hunt for Alpha")


# ── Public entry ─────────────────────────────────────────────────────

def generate_pdf(ticker: str, t: dict, price_history: list[dict] | None = None,
                 narrative: dict | None = None, event_row: dict | None = None,
                 peers: list | None = None) -> bytes:
    """Render the A4 tear sheet PDF.

    Args:
        ticker: stock symbol
        t: live ticker row (metrics, score, etc.)
        price_history: 90D price points [{date, close}, ...]
        narrative: AI narrative dict from pdf_narrative.build_narrative()
            with keys exec_para, bull[], bear[], verdict, conviction,
            catalysts[]. When provided, page 2 is rendered.
        event_row: cached event_summaries row (from event_intel) — its
            sections[] are rendered on page 2 as the 'Latest event' block.

    A narrative + event_row pair adds a second page; pass None for both
    to produce the legacy 1-page snapshot only.
    """
    ticker = (ticker or "").upper()
    price_history = price_history or []

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, pageCompression=1)
    c.setTitle(f"AlphaHunt Tear Sheet — {ticker}")
    c.setAuthor("AlphaHunt")
    c.setSubject(f"{ticker} stock tear sheet")

    # Header band
    today = date.today().strftime("%b %d, %Y")
    quarter_lbl = f"Q{(date.today().month - 1)//3 + 1}'{str(date.today().year)[-2:]} Reported"
    header_bottom = _draw_header(c, today, quarter_lbl,
                                   page_label="EQUITY RESEARCH BRIEF  ·  PAGE 1")

    # Hero
    logo_bytes = _fetch_logo_bytes(ticker)
    score = _safe_float(t.get("smart_score") or t.get("pop_score"), 0)
    grade = (t.get("grade") or "—").upper()
    tier_map = {"A": "Top Tier", "B": "Quality", "C": "Average", "D": "Below Avg", "F": "Weak"}
    hero_bottom = _draw_hero(c, header_bottom, ticker, t, logo_bytes)

    # 90-day chart + Score Breakdown (side-by-side)
    chart_bottom = _draw_chart_and_breakdown(c, hero_bottom, price_history, t)

    # 12-card metrics grid
    grid_bottom = _draw_metrics_grid(c, chart_bottom - 4, t)

    # Analyst Outlook + Ownership row (restored from old CRDO layout —
    # data-rich, no AI commentary, makes page 1 read like a pro report)
    outlook_bottom = _draw_analyst_outlook_row(c, grid_bottom - 4, t)

    # Executive summary on page 1 — ALWAYS the deterministic data-driven
    # bullets, never the AI prose. The user explicitly wants page 1 to
    # feel like a pro analyst report ("not AI generated"). The AI's tight
    # observation lives on page 2 as a takeaway, not here.
    bits = _build_exec_bits(t, score, grade, tier_map)
    exec_bottom = _draw_exec_summary(c, outlook_bottom, bits)

    # Recent Earnings + Quality & Risk dual block
    _draw_dual_block(c, exec_bottom, t)

    # ── Pre-determine which pages will render so the FINAL one
    # gets the disclaimer footer and every intermediate page gets
    # a compact footer without it (user v3.13: 'Remove the disclaimer
    # from the footer. Only keep this in the last page footer.').
    qinc = t.get("quarterly_income") or []
    eps  = t.get("eps_quarters") or []
    have_p2 = (len(qinc) >= 2 or len(eps) >= 2)
    have_p3 = (bool(narrative and narrative.get("valuation")) or
                bool(narrative and narrative.get("dcf")) or
                bool(qinc))
    have_p4 = bool(narrative or event_row)

    # Page 1 footer — disclaimer only if no other pages will render
    _draw_footer(c, with_disclaimer=not (have_p2 or have_p3 or have_p4))

    if have_p2:
        c.showPage()
        _draw_header(c, today, quarter_lbl,
                      page_label="FINANCIAL TRENDS  ·  PAGE 2")
        y2 = _draw_page2_hero(c, ticker, t, subtitle="Financial Trends")
        # Block 1: existing quarterly+margin dual chart row
        b1_bottom = _draw_quarterly_charts_row(c, y2 - 2, t)
        # Block 2: 4-metric trend strip (160pt tall)
        b2_top = b1_bottom - 14
        b2_h   = 180
        _draw_trend_strip(c, MARGIN_X, b2_top - b2_h, CONTENT_W, b2_h, t)
        # Block 3: peer comparison (140pt tall)
        b3_top = b2_top - b2_h - 14
        b3_h   = 140
        _draw_peer_comparison(c, MARGIN_X, b3_top - b3_h, CONTENT_W,
                                b3_h, t, peers or [])
        # Page 2 footer — disclaimer only if no later page will render
        _draw_footer(c, with_disclaimer=not (have_p3 or have_p4))

    # ── Page 3 — Valuation Scenarios + DCF Model + Risk Scorecard ────
    if have_p3:
        c.showPage()
        _draw_header(c, today, quarter_lbl,
                      page_label="VALUATION & RISK  ·  PAGE 3")
        y3 = _draw_page2_hero(c, ticker, t, subtitle="Valuation & Risk")
        # Valuation Scenarios — top (push CLOSER to header per user
        # feedback v3.11 'delete this space')
        val_h = 200
        val_top = y3 - 2
        _draw_valuation_scenarios(c, MARGIN_X, val_top - val_h, CONTENT_W,
                                    val_h, t, narrative or {})
        # DCF Model — middle. v3.13 user: 'DCF box has plenty of
        # space. Readjust accordingly.' Brought back down to 180pt;
        # extra vertical space was empty between sensitivity table
        # and the bottom edge.
        dcf_top = val_top - val_h - 12
        dcf_h   = 180
        _draw_dcf_model(c, MARGIN_X, dcf_top - dcf_h, CONTENT_W,
                          dcf_h, t, narrative or {})
        # Risk Scorecard — bottom
        risk_top = dcf_top - dcf_h - 12
        risk_h   = 160
        _draw_risk_scorecard(c, MARGIN_X, risk_top - risk_h, CONTENT_W,
                               risk_h, t)
        # Page 3 footer — disclaimer only if page 4 won't render
        _draw_footer(c, with_disclaimer=not have_p4)

    # ── Page 4 — AI analyst narrative (only when we have content) ─────
    if have_p4:
        c.showPage()
        _draw_header(c, today, quarter_lbl,
                      page_label="ANALYST NARRATIVE  ·  PAGE 4")
        y = _draw_page2_hero(c, ticker, t, subtitle="Analyst Narrative")
        if narrative:
            y = _draw_investment_thesis(c, y - 4, narrative)
            y = _draw_catalysts(c, y - 14, narrative.get("catalysts") or [])
        if event_row:
            _draw_event_summary(c, y - 14, event_row,
                                 today_str=today, quarter_lbl=quarter_lbl,
                                 ticker=ticker, t=t)
        _draw_footer(c)

    # ── ENDNOTES PAGE — always last (disclaimer + abbreviation list) ──
    # v3.19: moved disclaimer out of footer per user, added glossary
    # of abbreviations used throughout the report.
    c.showPage()
    _draw_endnotes_page(c, today, quarter_lbl)

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()
