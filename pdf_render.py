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
BRAND_INDIGO  = HexColor("#4338ca")
BRAND_VIOLET  = HexColor("#8b5cf6")
BRAND_MAGENTA = HexColor("#ec4899")
BRAND_LIGHT   = HexColor("#eef2ff")
INK           = HexColor("#0f172a")
INK_SOFT      = HexColor("#475569")
INK_MUTED     = HexColor("#94a3b8")
INK_DIM       = HexColor("#cbd5e1")
BG_SOFT       = HexColor("#f8fafc")
BG_CARD       = HexColor("#ffffff")
BORDER        = HexColor("#e2e8f0")
BORDER_LIGHT  = HexColor("#f1f5f9")
GREEN         = HexColor("#16a34a")
GREEN_LIGHT   = HexColor("#dcfce7")
RED           = HexColor("#dc2626")
RED_LIGHT     = HexColor("#fee2e2")
AMBER         = HexColor("#f59e0b")

A4_W, A4_H = A4              # 595 x 842 pts
MARGIN_X = 32
MARGIN_TOP = 28
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
                        color="#8b5cf6", alpha=0.18)
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
                color="#ec4899", markersize=6,
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


def _draw_brand_mark(c: canvas.Canvas, x: float, y: float, size: float = 28):
    c.saveState()
    c.setFillColor(BRAND_INDIGO)
    c.roundRect(x, y, size, size, 5, stroke=0, fill=1)
    c.setFont("Helvetica-Bold", size * 0.55)
    c.setFillColor(HexColor("#ffffff"))
    c.drawCentredString(x + size / 2, y + size * 0.28, "α")
    c.restoreState()


def _draw_metric_card(c, x, y, w, h, label, value, sub="", value_color=INK, empty=False):
    c.setFillColor(BG_CARD)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.6)
    c.roundRect(x, y, w, h, 5, stroke=1, fill=1)
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

def _draw_header(c, today_str, quarter_lbl):
    y = A4_H - MARGIN_TOP - 28
    _draw_brand_mark(c, MARGIN_X, y + 2, size=28)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(MARGIN_X + 38, y + 16, "AlphaHunt")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(MARGIN_X + 38, y + 5, "US STOCK TEAR SHEET")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7.5)
    c.drawRightString(A4_W - MARGIN_X, y + 16, f"GENERATED {today_str.upper()}")
    c.setFillColor(BRAND_INDIGO)
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(A4_W - MARGIN_X, y + 5, quarter_lbl)
    c.setStrokeColor(INK)
    c.setLineWidth(1.2)
    c.line(MARGIN_X, y - 6, A4_W - MARGIN_X, y - 6)
    return y - 14  # bottom of header


def _draw_hero(c, top_y, ticker, t, logo_bytes):
    """3-column hero: [logo + ticker/name/sector] [price block] [score block].
    Total height ~90 pts. Returns y of bottom of hero."""
    hero_h = 86
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
    c.setFillColor(BG_CARD)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.6)
    c.roundRect(logo_x, logo_y, logo_size, logo_size, 6, stroke=1, fill=1)
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
    c.setFillColor(BG_CARD)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.6)
    c.roundRect(MARGIN_X, chart_y, half_w, chart_h, 6, stroke=1, fill=1)
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
    c.setFillColor(BG_CARD)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.6)
    c.roundRect(sb_x, chart_y, half_w, chart_h, 6, stroke=1, fill=1)
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
    de = _safe_float(t.get("debt_to_equity"))
    roe = _safe_float(t.get("roe") or t.get("return_on_equity"))
    if roe is not None and abs(roe) <= 1: roe *= 100
    fcfm = _safe_float(t.get("fcf_margin"))
    if fcfm is not None and abs(fcfm) <= 1: fcfm *= 100
    revttm = _safe_float(t.get("revenue_ttm"))
    ps = (mcap / revttm) if (mcap and revttm and revttm > 0) else None
    hi52 = _safe_float(t.get("high_52w"))
    lo52 = _safe_float(t.get("low_52w"))
    px = _safe_float(t.get("price"))
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
        ("Debt / Equity", f"{de:.2f}" if (de is not None and de < 10) else f"{de:.0f}" if de is not None else "Not reported",
            "Conservative" if (de and de < 0.5) else "Moderate" if (de and de < 1.5) else
            "High leverage" if de is not None else "",
            INK, de is None),
        ("ROE (TTM)", f"{roe:.1f}%" if roe is not None else "Not reported",
            "Excellent" if (roe and roe >= 20) else "Healthy" if (roe and roe >= 10) else
            "Modest" if (roe and roe >= 0) else "Negative" if roe is not None else "",
            INK, roe is None),
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


def _draw_analyst_outlook_row(c, top_y, t):
    """New page-1 row: Analyst Outlook (left) + Ownership Snapshot (right).
    Restores the data-rich sections from the old CRDO tear sheet that the
    AI-only v3 version dropped. Pure data — no AI commentary, so the page
    reads as a pro analyst report, not an LLM essay."""
    block_h = 86
    y = top_y - block_h - 10
    half_w = (CONTENT_W - 10) / 2

    # ── LEFT: Analyst Outlook ─────────────────────────────────────────
    left_x = MARGIN_X
    c.setFillColor(BG_CARD)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.6)
    c.roundRect(left_x, y, half_w, block_h, 6, stroke=1, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(left_x + 10, y + block_h - 13, "ANALYST OUTLOOK")
    # Mean target with upside
    tgt_mean = _safe_float(t.get("target_mean"))
    tgt_low  = _safe_float(t.get("target_low"))
    tgt_high = _safe_float(t.get("target_high"))
    price    = _safe_float(t.get("price") or t.get("last_close"))
    total_an = int(t.get("total_analysts") or 0)
    if tgt_mean and price and price > 0:
        upside = (tgt_mean / price - 1) * 100
        u_color = GREEN if upside >= 0 else RED
        u_label = f"{'+' if upside >= 0 else ''}{upside:.1f}% upside"
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(left_x + 10, y + block_h - 36, f"${tgt_mean:.2f}")
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 7)
        c.drawString(left_x + 10, y + block_h - 47, "MEAN TARGET")
        c.setFillColor(u_color)
        c.setFont("Helvetica-Bold", 8)
        c.drawString(left_x + 10, y + 10, u_label)
    else:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 9)
        c.drawString(left_x + 10, y + block_h / 2 - 4, "No analyst target data")
    # Target band on the right of the card
    if tgt_low and tgt_high:
        band_x = left_x + half_w / 2 + 4
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 7)
        c.drawString(band_x, y + block_h - 28, "TARGET BAND")
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(band_x, y + block_h - 44, f"${tgt_low:.0f} – ${tgt_high:.0f}")
        # Coverage strip
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 7)
        cov_text = f"{total_an} analyst{'s' if total_an != 1 else ''}" if total_an else "Limited coverage"
        c.drawString(band_x, y + block_h - 56, cov_text)

    # ── RIGHT: Ownership Snapshot ─────────────────────────────────────
    right_x = left_x + half_w + 10
    c.setFillColor(BG_CARD)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.6)
    c.roundRect(right_x, y, half_w, block_h, 6, stroke=1, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(right_x + 10, y + block_h - 13, "OWNERSHIP & FLOAT")
    # 4-cell mini-grid: Insider Own / Institutional / Dividend Yield / Avg Volume
    o_cols, o_rows = 4, 1
    o_gap = 4
    o_inner_w = half_w - 20
    o_cell_w = (o_inner_w - o_gap * (o_cols - 1)) / o_cols
    o_y = y + 14

    ins_own = _safe_float(t.get("insider_ownership"))
    inst    = _safe_float(t.get("institutional_ownership"))
    if ins_own is not None and ins_own <= 1: ins_own = ins_own * 100
    if inst    is not None and inst    <= 1: inst    = inst * 100
    div_y   = _safe_float(t.get("dividend_yield"))
    if div_y is not None and div_y <= 1: div_y = div_y * 100
    avg_vol = t.get("avg_volume_str") or (_fmt_vol(t.get("avg_volume")) if t.get("avg_volume") else None)

    cells = [
        ("INSIDER OWN",  f"{ins_own:.1f}%" if ins_own else "—",          "Self-held"),
        ("INSTITUTIONAL", f"{inst:.0f}%" if inst else "—",                "Held by funds"),
        ("DIVIDEND YIELD", f"{div_y:.2f}%" if (div_y and div_y > 0) else "None", "Trailing"),
        ("AVG VOLUME",   avg_vol or "—",                                  "Daily shares"),
    ]
    for i, (label, val, sub) in enumerate(cells):
        cx = right_x + 10 + i * (o_cell_w + o_gap)
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica-Bold", 6.5)
        c.drawString(cx, o_y + 50, label)
        empty = val == "—" or val == "None"
        c.setFillColor(INK_MUTED if empty else INK)
        c.setFont("Helvetica-Bold", 11 if not empty else 10)
        c.drawString(cx, o_y + 32, str(val))
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 6)
        c.drawString(cx, o_y + 18, sub)

    return y


def _draw_dual_block(c, top_y, t):
    """Two cards side-by-side: Recent Earnings bullets + Quality & Risk Signals.
    Designed to fill the empty bottom half the user complained about."""
    block_h = 130
    y = top_y - block_h - 10
    half_w = (CONTENT_W - 10) / 2

    # ── LEFT: Recent Earnings Highlights ─────────────────────────────
    left_x = MARGIN_X
    c.setFillColor(BG_CARD)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.6)
    c.roundRect(left_x, y, half_w, block_h, 6, stroke=1, fill=1)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(left_x + 10, y + block_h - 13, "RECENT EARNINGS HIGHLIGHTS")

    # Build bullets (Quartr-style: metric + YoY comparison inline)
    inc = (t.get("quarterly_income") or [{}])[0] or {}
    inc_prev = (t.get("quarterly_income") or [{}, {}, {}, {}])[3] or {}
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
    c.setFillColor(BG_CARD)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.6)
    c.roundRect(right_x, y, half_w, block_h, 6, stroke=1, fill=1)
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


def _draw_page2_hero(c, ticker, t):
    """Compact second-page header: ticker + name + page label.
    Positioned BELOW the header band so it doesn't overlap. Returns
    the y-coordinate where downstream content can start."""
    # Header band's _draw_header() leaves us around A4_H - 75.
    # Push hero to A4_H - 115 so the divider line and the ticker
    # don't collide.
    y = A4_H - 105
    co_name = (t.get("name") or t.get("company_name") or "").strip()
    c.setFillColor(BRAND_INDIGO)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(MARGIN_X, y + 18, ticker)
    c.setFillColor(INK)
    c.setFont("Helvetica", 11)
    c.drawString(MARGIN_X + 65, y + 22, co_name[:60])
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7.5)
    c.drawString(MARGIN_X + 65, y + 9, "ANALYST NARRATIVE  ·  PAGE 2")
    # Thin brand accent bar at bottom of the hero band
    c.setStrokeColor(BRAND_INDIGO)
    c.setLineWidth(1.2)
    c.line(MARGIN_X, y - 2, A4_W - MARGIN_X, y - 2)
    return y - 12


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

    # ── Key Takeaway line (above the thesis title) — the AI's tight
    # one-sentence read that distills the setup. Always single-line.
    if observation:
        c.setFillColor(BRAND_INDIGO)
        c.setFont("Helvetica-Bold", 7.5)
        c.drawString(MARGIN_X, top_y, "KEY TAKEAWAY")
        c.setFillColor(INK)
        c.setFont("Helvetica", 9.5)
        # Trim to single line — fits because prompt enforces 15-25 words
        max_w = CONTENT_W
        text = observation
        while c.stringWidth(text, "Helvetica", 9.5) > max_w and len(text) > 10:
            text = text[:-2]
        if text != observation:
            text = text.rstrip(" ,;.") + "…"
        c.drawString(MARGIN_X, top_y - 14, text)
        top_y -= 28

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
    c.setFillColor(BG_CARD)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.6)
    c.roundRect(MARGIN_X, card_y, half_w, card_h, 6, stroke=1, fill=1)
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
    c.setFillColor(BG_CARD)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.6)
    c.roundRect(bx, card_y, half_w, card_h, 6, stroke=1, fill=1)
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

    c.setFillColor(BG_CARD)
    c.setStrokeColor(BORDER)
    c.setLineWidth(0.6)
    c.roundRect(MARGIN_X, card_y, CONTENT_W, card_h, 6, stroke=1, fill=1)
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


def _draw_event_summary(c, top_y, event_row):
    """Render the Quartr-style event summary block with dynamic section
    headings + bullets. Stops drawing once we hit the footer reserve."""
    title_y = top_y
    if title_y < MARGIN_BOTTOM + 80:
        return  # no room
    title = (event_row.get("event_title") or "Latest event").strip()
    date_s = (event_row.get("event_date") or "").strip()

    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(MARGIN_X, title_y, "LATEST EVENT — " + title.upper()[:60])
    if date_s:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 8.5)
        c.drawRightString(A4_W - MARGIN_X, title_y, date_s)

    y = title_y - 16
    reserve_y = MARGIN_BOTTOM + 50   # leave room for footer
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

    for heading, bullets in items:
        if y < reserve_y + 20:
            break
        # Heading
        c.setFillColor(BRAND_INDIGO)
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(MARGIN_X, y, heading)
        y -= 11
        # Bullets
        for b in bullets[:4]:
            if y < reserve_y + 12:
                break
            p = _wrap_paragraph("•  " + str(b), CONTENT_W - 4,
                                 font_size=8, leading=10.5, color=INK)
            w, h = p.wrap(CONTENT_W - 4, 50)
            if y - h < reserve_y + 12:
                break
            p.drawOn(c, MARGIN_X + 4, y - h)
            y -= (h + 2)
        y -= 4


def _draw_footer(c):
    foot_y = MARGIN_BOTTOM
    c.setStrokeColor(INK)
    c.setLineWidth(0.5)
    c.line(MARGIN_X, foot_y + 26, A4_W - MARGIN_X, foot_y + 26)
    c.setFillColor(INK_SOFT)
    c.setFont("Helvetica-Bold", 6.8)
    c.drawString(MARGIN_X, foot_y + 16, "EDUCATIONAL USE ONLY.")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 6.8)
    c.drawString(MARGIN_X + 96, foot_y + 16,
                 "AlphaHunt is not a SEBI-registered advisor. Alpha Score is a quantitative composite — not investment advice.")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 6.8)
    c.drawString(MARGIN_X, foot_y + 6,
                 "Data: SEC EDGAR, Yahoo Finance, FMP. Past performance does not guarantee future results. Do your own research.")
    c.setFillColor(BRAND_INDIGO)
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(A4_W - MARGIN_X, foot_y + 16, "alphahunt.in")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 6.8)
    c.drawRightString(A4_W - MARGIN_X, foot_y + 6, "Hunt for Alpha")


# ── Public entry ─────────────────────────────────────────────────────

def generate_pdf(ticker: str, t: dict, price_history: list[dict] | None = None,
                 narrative: dict | None = None, event_row: dict | None = None) -> bytes:
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
    header_bottom = _draw_header(c, today, quarter_lbl)

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

    # Footer
    _draw_footer(c)

    # ── Page 2 — AI analyst narrative (only when we have content) ─────
    if narrative or event_row:
        c.showPage()
        _draw_header(c, today, quarter_lbl)
        y = _draw_page2_hero(c, ticker, t)
        if narrative:
            y = _draw_investment_thesis(c, y - 4, narrative)
            y = _draw_catalysts(c, y - 14, narrative.get("catalysts") or [])
        if event_row:
            _draw_event_summary(c, y - 14, event_row)
        _draw_footer(c)

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()
