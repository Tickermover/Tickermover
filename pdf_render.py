"""
AlphaHunt — Server-side PDF tear sheet generation.

Replaces the brittle client-side html2canvas + jsPDF + hidden-iframe path
with a deterministic pure-Python generator. One call →  PDF bytes →
streamed to the browser as `application/pdf`. No iframes, no waiting,
no separate URL ever rendered.

Why reportlab + matplotlib (not weasyprint):
- reportlab is pure Python — no system deps (cairo, pango, fontconfig)
  that would break Railway's nixpacks build.
- matplotlib also already in the project; produces clean SVG/PNG charts
  for embedding.
- weasyprint would render the existing HTML nicely but adds 5+ system
  libs at deploy time and choked at Chart.js anyway.

Public API:
    generate_pdf(ticker: str, ticker_data: dict, price_history: list[dict]) -> bytes

Caller passes pre-fetched ticker data + price history. This module is
pure compute — no I/O. Lets callers cache aggressively.
"""
from __future__ import annotations

import io
import logging
from datetime import datetime, date

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, Color
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_RIGHT

import matplotlib
matplotlib.use("Agg")  # headless backend, no GUI
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

logger = logging.getLogger(__name__)

# ── Design tokens (mirror the dashboard's brand palette) ─────────────
BRAND_INDIGO  = HexColor("#4338ca")
BRAND_VIOLET  = HexColor("#8b5cf6")
BRAND_MAGENTA = HexColor("#ec4899")
INK           = HexColor("#0f172a")
INK_SOFT      = HexColor("#475569")
INK_MUTED     = HexColor("#94a3b8")
BG_SOFT       = HexColor("#f8fafc")
BORDER        = HexColor("#e2e8f0")
GREEN         = HexColor("#16a34a")
RED           = HexColor("#dc2626")
AMBER         = HexColor("#f59e0b")

A4_W, A4_H = A4  # 595 x 842 pts
MARGIN_X = 36
MARGIN_Y = 36


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


def _fmt_pct(v, digits=1, signed=True):
    if v is None: return "—"
    try: v = float(v)
    except (TypeError, ValueError): return "—"
    s = ("+" if v >= 0 else "") if signed else ""
    return f"{s}{v:.{digits}f}%"


def _safe_float(v, default=None):
    if v is None: return default
    try: return float(v)
    except (TypeError, ValueError): return default


def _make_price_chart(price_history: list[dict], width_pt: float, height_pt: float) -> bytes | None:
    """Render a 90-day price chart as PNG bytes. Returns None on failure."""
    if not price_history or len(price_history) < 2:
        return None
    try:
        dates  = [p.get("date") for p in price_history]
        closes = [float(p.get("close") or 0) for p in price_history]
        # Convert width/height from points (72 dpi) to inches for matplotlib
        fig_w, fig_h = width_pt / 72.0, height_pt / 72.0
        fig, ax = plt.subplots(figsize=(fig_w, fig_h), dpi=160)
        # Brand gradient line — matplotlib can't do per-segment gradient
        # easily, so use a solid brand color and fill underneath.
        ax.plot(range(len(closes)), closes, color="#6366f1", linewidth=2.0)
        ax.fill_between(range(len(closes)), closes, min(closes),
                        color="#8b5cf6", alpha=0.18)
        # Style
        ax.set_facecolor("#ffffff")
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color("#cbd5e1")
        ax.spines["bottom"].set_color("#cbd5e1")
        ax.tick_params(colors="#94a3b8", labelsize=7)
        # X labels: first / middle / last date
        n = len(dates)
        ticks  = [0, n // 2, n - 1]
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
        # Mark the latest point
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


def _draw_logo(c: canvas.Canvas, x: float, y: float, size: float = 36):
    """Small AlphaHunt brand mark (rounded square with gradient fill stub)."""
    c.saveState()
    c.setFillColor(BRAND_INDIGO)
    c.roundRect(x, y, size, size, 6, stroke=0, fill=1)
    # Greek alpha
    c.setFont("Helvetica-Bold", size * 0.55)
    c.setFillColor(HexColor("#ffffff"))
    c.drawCentredString(x + size / 2, y + size * 0.28, "α")
    c.restoreState()


def _draw_chip(c, x, y, w, h, label, fill=BG_SOFT, stroke=BORDER, label_color=INK_SOFT, font_size=8):
    c.setFillColor(fill)
    c.setStrokeColor(stroke)
    c.roundRect(x, y, w, h, 3, stroke=1, fill=1)
    c.setFillColor(label_color)
    c.setFont("Helvetica-Bold", font_size)
    c.drawString(x + 6, y + h / 2 - font_size / 2.6, label)


def _draw_metric_card(c, x, y, w, h, label, value, sub="", value_color=INK):
    """A single metric card — label up top, big value middle, optional sub at bottom."""
    c.setFillColor(HexColor("#ffffff"))
    c.setStrokeColor(BORDER)
    c.roundRect(x, y, w, h, 6, stroke=1, fill=1)
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(x + 9, y + h - 13, label.upper())
    c.setFillColor(value_color)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(x + 9, y + h - 32, str(value))
    if sub:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 7)
        c.drawString(x + 9, y + 8, sub)


# ── Main entrypoint ──────────────────────────────────────────────────

def generate_pdf(ticker: str, t: dict, price_history: list[dict] | None = None) -> bytes:
    """Render a one-page A4 tear sheet PDF for `ticker` using the data in
    `t` (a /api/ticker payload) and an optional price_history list of
    {date, close}. Returns PDF bytes. Pure compute — no network I/O."""
    ticker = (ticker or "").upper()
    price_history = price_history or []

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, pageCompression=1)
    c.setTitle(f"AlphaHunt Tear Sheet — {ticker}")
    c.setAuthor("AlphaHunt")
    c.setSubject(f"{ticker} stock tear sheet")

    # ── HEADER BAND ─────────────────────────────────────────────────
    header_h = 40
    y_top = A4_H - MARGIN_Y - header_h
    _draw_logo(c, MARGIN_X, y_top + 4, size=32)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(MARGIN_X + 42, y_top + 22, "AlphaHunt")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(MARGIN_X + 42, y_top + 9, "US STOCK TEAR SHEET")
    # Generated date (right side)
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 8)
    today = date.today().strftime("%b %d, %Y")
    c.drawRightString(A4_W - MARGIN_X, y_top + 22, f"GENERATED {today.upper()}")
    quarter_lbl = f"Q{(date.today().month - 1)//3 + 1}'{str(date.today().year)[-2:]} Reported"
    c.setFillColor(BRAND_INDIGO)
    c.setFont("Helvetica-Bold", 9)
    c.drawRightString(A4_W - MARGIN_X, y_top + 9, quarter_lbl)
    # Separator line
    c.setStrokeColor(INK)
    c.setLineWidth(1.5)
    c.line(MARGIN_X, y_top - 4, A4_W - MARGIN_X, y_top - 4)

    # ── HERO ROW ────────────────────────────────────────────────────
    hero_y = y_top - 80
    # Ticker (big)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 42)
    c.drawString(MARGIN_X, hero_y + 40, ticker)
    # Company name
    name = (t.get("name") or t.get("long_name") or "")[:60]
    c.setFillColor(INK_SOFT)
    c.setFont("Helvetica", 11)
    c.drawString(MARGIN_X, hero_y + 22, name)
    # Sector / industry
    sector = t.get("sector") or ""
    industry = t.get("industry") or ""
    sec_text = sector
    if industry and industry != sector:
        sec_text = f"{sector} · {industry}"
    c.setFillColor(BRAND_INDIGO)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(MARGIN_X, hero_y + 8, sec_text.upper())

    # Price block (centered)
    px = _safe_float(t.get("price"))
    chg = _safe_float(t.get("change_pct"))
    price_x = A4_W / 2
    if px is not None:
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 28)
        c.drawCentredString(price_x, hero_y + 30, f"${px:.2f}")
    if chg is not None:
        c.setFillColor(GREEN if chg >= 0 else RED)
        c.setFont("Helvetica-Bold", 12)
        c.drawCentredString(price_x, hero_y + 14, f"{('+' if chg >= 0 else '')}{chg:.2f}% today")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica-Bold", 7)
    c.drawCentredString(price_x, hero_y + 2, "LAST CLOSE")

    # Alpha Score (right side)
    score = _safe_float(t.get("smart_score") or t.get("pop_score"), 0)
    grade = (t.get("grade") or "—").upper()
    star_map = {"A": "*****", "B": "****", "C": "***", "D": "**", "F": "*"}
    tier_map = {"A": "Top Tier", "B": "Quality", "C": "Average", "D": "Below Avg", "F": "Weak"}
    score_x = A4_W - MARGIN_X
    c.setFillColor(BRAND_VIOLET)
    c.setFont("Helvetica-Bold", 40)
    c.drawRightString(score_x, hero_y + 28, str(int(score)))
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica-Bold", 7)
    c.drawRightString(score_x, hero_y + 16, "ALPHA SCORE")
    c.setFillColor(AMBER)
    c.setFont("Helvetica", 10)
    c.drawRightString(score_x, hero_y + 4, star_map.get(grade, "—"))
    c.setFillColor(BRAND_INDIGO)
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(score_x, hero_y - 7, tier_map.get(grade, "—"))

    # ── PRICE CHART ─────────────────────────────────────────────────
    chart_y = hero_y - 30
    chart_h = 140
    chart_y -= chart_h
    chart_w = A4_W - MARGIN_X * 2
    chart_png = _make_price_chart(price_history, chart_w, chart_h)
    if chart_png:
        from reportlab.lib.utils import ImageReader
        img = ImageReader(io.BytesIO(chart_png))
        # Card frame
        c.setFillColor(HexColor("#ffffff"))
        c.setStrokeColor(BORDER)
        c.roundRect(MARGIN_X, chart_y, chart_w, chart_h, 8, stroke=1, fill=1)
        # Title
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 9)
        c.drawString(MARGIN_X + 10, chart_y + chart_h - 14, "90-DAY PRICE")
        # Performance pill
        if price_history and len(price_history) >= 2:
            first = _safe_float(price_history[0].get("close"))
            last  = _safe_float(price_history[-1].get("close"))
            if first and first > 0 and last is not None:
                perf = (last / first - 1) * 100
                pill_label = f"{('+' if perf >= 0 else '')}{perf:.1f}% · {len(price_history)}D"
                pill_w = len(pill_label) * 4.6 + 16
                _draw_chip(c, MARGIN_X + chart_w - pill_w - 10, chart_y + chart_h - 21,
                           pill_w, 14, pill_label,
                           fill=BRAND_INDIGO, stroke=BRAND_INDIGO,
                           label_color=HexColor("#ffffff"), font_size=7)
        # Embed chart image inside the card
        c.drawImage(img, MARGIN_X + 6, chart_y + 6, width=chart_w - 12, height=chart_h - 26,
                    preserveAspectRatio=True, anchor='c', mask='auto')
    else:
        c.setFillColor(INK_MUTED)
        c.setFont("Helvetica", 9)
        c.drawCentredString(A4_W / 2, chart_y + chart_h / 2, "Price history unavailable")

    # ── METRICS GRID (4 cols × 3 rows = 12 cards) ──────────────────
    grid_y = chart_y - 16
    gap = 8
    cell_w = (A4_W - MARGIN_X * 2 - gap * 3) / 4
    cell_h = 50

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
    ps = None
    if mcap and revttm and revttm > 0:
        ps = mcap / revttm
    hi52 = _safe_float(t.get("high_52w"))
    lo52 = _safe_float(t.get("low_52w"))
    rsi = _safe_float(t.get("rsi_14"))
    eps_hist = t.get("eps_quarters") or []
    beats = sum(1 for q in eps_hist if q.get("beat"))
    total_eps = len(eps_hist)

    metrics = [
        ("Market Cap", _fmt_money(mcap), "Mega cap" if (mcap and mcap >= 1e11) else
                                          "Large cap" if (mcap and mcap >= 1e10) else
                                          "Mid cap" if (mcap and mcap >= 2e9) else
                                          "Small cap" if (mcap and mcap >= 3e8) else "Micro cap"),
        ("Rev Growth (YoY)", _fmt_pct(revg, 1) if revg is not None else "—", "Latest quarter"),
        ("Gross Margin", f"{gm:.1f}%" if gm is not None else "—",
                          "Premium" if (gm and gm >= 50) else "Healthy" if (gm and gm >= 30) else "Thin" if gm is not None else ""),
        ("FCF Margin", f"{fcfm:.1f}%" if fcfm is not None else "—", "Cash from ops"),
        ("P/E (TTM)", f"{pe:.1f}x" if pe is not None else "—",
                       "Reasonable" if (pe and 0 < pe <= 30) else "Growth premium" if (pe and pe > 30) else "Loss-making" if (pe is not None and pe < 0) else ""),
        ("P/S (TTM)", f"{ps:.1f}x" if ps is not None else "—",
                       "Bargain" if (ps and ps < 1) else "Reasonable" if (ps and ps < 5) else "Growth premium" if (ps and ps < 15) else "Rich" if ps is not None else ""),
        ("PEG", f"{peg:.2f}" if peg is not None else "—",
                  "Cheap" if (peg and peg < 1) else "Fair" if (peg and peg < 2) else "Rich" if peg is not None else ""),
        ("Debt / Equity", f"{de:.2f}" if de is not None and de < 10 else f"{de:.0f}" if de is not None else "Not reported",
                            "Conservative" if (de and de < 0.5) else "Moderate" if (de and de < 1.5) else "High leverage" if de is not None else ""),
        ("ROE (TTM)", f"{roe:.1f}%" if roe is not None else "Not reported",
                       "Excellent" if (roe and roe >= 20) else "Healthy" if (roe and roe >= 10) else "Modest" if roe is not None else ""),
        ("52W Range", f"${lo52:.0f} - ${hi52:.0f}" if (hi52 and lo52) else "—",
                       f"{((px/hi52 - 1) * 100):.1f}% from high" if (hi52 and px) else ""),
        ("RSI (14)", f"{int(rsi)}" if rsi is not None else "—",
                      "Overbought" if (rsi and rsi > 70) else "Oversold" if (rsi and rsi < 30) else "Neutral" if rsi is not None else ""),
        ("EPS Beat Streak", f"{beats}/{total_eps}" if total_eps else "—",
                              "Perfect record" if (total_eps and beats == total_eps) else "Strong" if (total_eps and beats >= total_eps - 1) else "Mixed" if total_eps else ""),
    ]
    for i, (lbl, val, sub) in enumerate(metrics):
        col = i % 4
        row = i // 4
        x = MARGIN_X + col * (cell_w + gap)
        y = grid_y - cell_h - row * (cell_h + gap)
        # Color hints for some metrics
        color = INK
        if lbl == "Rev Growth (YoY)" and revg is not None:
            color = GREEN if revg >= 0 else RED
        _draw_metric_card(c, x, y, cell_w, cell_h, lbl, val, sub, value_color=color)

    # ── EXECUTIVE SUMMARY (paragraph) ───────────────────────────────
    summary_y = grid_y - cell_h * 3 - gap * 2 - 16
    summary_h = 86
    summary_y -= summary_h
    c.setFillColor(HexColor("#eef2ff"))
    c.setStrokeColor(BRAND_INDIGO)
    c.setLineWidth(2)
    c.roundRect(MARGIN_X, summary_y, A4_W - MARGIN_X * 2, summary_h, 8, stroke=0, fill=1)
    # Left accent stripe
    c.setFillColor(BRAND_INDIGO)
    c.rect(MARGIN_X, summary_y, 3, summary_h, stroke=0, fill=1)
    c.setFillColor(BRAND_INDIGO)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(MARGIN_X + 14, summary_y + summary_h - 16, "EXECUTIVE SUMMARY")
    # Summary text — build from the strongest signals
    bits = []
    if score >= 80:
        bits.append(f"Top-tier setup with Alpha Score {int(score)} ({tier_map.get(grade,'')}).")
    elif score >= 70:
        bits.append(f"Quality setup at Alpha Score {int(score)} ({tier_map.get(grade,'')}).")
    elif score >= 60:
        bits.append(f"Mid-tier score at {int(score)} - selective entry levels matter.")
    else:
        bits.append(f"Below-tier score ({int(score)}). Stronger candidates exist in the universe.")
    if revg is not None:
        if revg >= 30:   bits.append(f"Revenue accelerating at +{revg:.0f}% YoY.")
        elif revg >= 10: bits.append(f"Mid-double-digit revenue growth (+{revg:.0f}% YoY).")
        elif revg < 0:   bits.append(f"Revenue contracting ({revg:.0f}% YoY).")
    if total_eps and beats == total_eps:
        bits.append(f"Perfect {total_eps}-quarter EPS beat record.")
    elif beats >= 2:
        bits.append(f"{beats}-quarter EPS beat streak.")
    m1 = _safe_float(t.get("momentum_1m"))
    if m1 is not None and m1 >= 10:
        bits.append(f"30-day momentum +{m1:.0f}%.")
    tgt = _safe_float(t.get("target_mean"))
    if tgt and px:
        upside = (tgt / px - 1) * 100
        if upside <= 0:   bits.append(f"Above analyst mean target ({upside:.1f}% gap).")
        elif upside >= 20: bits.append(f"Analyst mean target implies +{upside:.0f}% upside.")

    summary_text = " ".join(bits) if bits else "See metrics above for the full read."
    style = ParagraphStyle("ts-exec", fontName="Helvetica", fontSize=9,
                            textColor=INK, leading=12, alignment=TA_LEFT)
    p = Paragraph(summary_text, style)
    p.wrapOn(c, A4_W - MARGIN_X * 2 - 20, summary_h - 24)
    p.drawOn(c, MARGIN_X + 14, summary_y + 8)

    # ── DISCLAIMER FOOTER ──────────────────────────────────────────
    foot_y = MARGIN_Y
    c.setStrokeColor(INK)
    c.setLineWidth(0.5)
    c.line(MARGIN_X, foot_y + 24, A4_W - MARGIN_X, foot_y + 24)
    c.setFillColor(INK_SOFT)
    c.setFont("Helvetica-Bold", 7)
    c.drawString(MARGIN_X, foot_y + 14, "EDUCATIONAL USE ONLY.")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(MARGIN_X + 92, foot_y + 14,
                 "AlphaHunt is not a SEBI-registered advisor. Alpha Score is a quantitative composite for screening; not investment advice.")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7)
    c.drawString(MARGIN_X, foot_y + 4,
                 "Data: SEC EDGAR, Yahoo Finance, FMP. Past performance does not guarantee future results. Do your own research.")
    c.setFillColor(BRAND_INDIGO)
    c.setFont("Helvetica-Bold", 8)
    c.drawRightString(A4_W - MARGIN_X, foot_y + 14, "alphahunt.in")
    c.setFillColor(INK_MUTED)
    c.setFont("Helvetica", 7)
    c.drawRightString(A4_W - MARGIN_X, foot_y + 4, "Hunt for Alpha")

    c.showPage()
    c.save()
    buf.seek(0)
    return buf.read()
