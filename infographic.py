"""
TickerMover — AI-summarised infographic (one shareable image)
=============================================================

NotebookLM-style: read the content, pull out what matters, lay it out as a
single poster you can read standing still. 1080x1350 portrait, which is the
shape that survives Instagram, LinkedIn and a phone screenshot.

THE DIVISION OF LABOUR MATTERS. Every NUMBER on the poster is computed from the
map's own data (`/api/thesis-shares` maths — revenue x exposure weight, rolled
up by layer). The model is only ever asked for WORDS: a headline, a one-line
read of the split, and three takeaways. A model that invents a statistic about a
real company is worse than no infographic, so it is never given the chance —
if the AI chain is down (it frequently is; the free tier 402s and 429s), the
poster still renders with deterministic copy derived from the same numbers.

Public API:
    summarise(thesis, shares)  -> dict   (cheap, cached by caller)
    render_png(summary)        -> bytes  (Pillow, no network)
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

W, H = 1080, 1350

# Brand palette — navy + warm, matching the site.
INK = (10, 47, 70)
INK_SOFT = (93, 108, 123)
FAINT = (148, 163, 184)
INDIGO = (74, 91, 196)
ORANGE = (199, 78, 0)
PAPER = (255, 242, 236)
CARD = (255, 255, 255)
HAIR = (228, 231, 236)
GREEN = (10, 125, 51)
# Layer ramp, upstream -> downstream, same order the map page uses.
RAMP = [(96, 165, 250), (56, 189, 248), (45, 212, 191), (163, 230, 53),
        (250, 204, 21), (251, 146, 60)]


# ── 1. Summarise ──────────────────────────────────────────────────────────

_PROMPT = """You are writing the copy for a single-image infographic about a
supply-chain "map" published by TickerMover. The map traces one macro driver
down the chain of companies that capture the spending.

THE NUMBERS ARE ALREADY COMPUTED AND ARE NOT YOURS TO CHANGE OR RESTATE. Write
only the words that frame them.

Return ONLY JSON, no prose, no markdown fence:
{{
  "headline": "<=64 chars. The single most interesting thing about this split.
                Concrete. Not a restatement of the title.",
  "read": "<=150 chars. One sentence explaining what the concentration means for
            someone looking at this chain.",
  "takeaways": ["<=90 chars each", "<=90 chars each", "<=90 chars each"]
}}

Rules: no buy/sell/hold, no price targets, no predictions, no invented numbers.
Descriptive only — this map shows where money has ALREADY landed, and says
nothing about future upside. Plain English, no jargon for its own sake.

MAP: {title}
WHAT IT TRACES: {standfirst}

COMPUTED SPLIT (do not alter these figures):
{facts}
"""


def _fmt_b(v):
    """$ in the units a reader actually holds in their head."""
    try:
        v = float(v or 0)
    except (TypeError, ValueError):
        return "$0"
    if v >= 1e12:
        return "$%.2fT" % (v / 1e12)
    if v >= 1e9:
        return "$%.0fB" % (v / 1e9)
    if v >= 1e6:
        return "$%.0fM" % (v / 1e6)
    return "$%.0f" % v


def _pct(x):
    try:
        return "%.0f%%" % (float(x) * 100)
    except (TypeError, ValueError):
        return "—"


def _facts(thesis: dict, shares: dict) -> dict:
    """Everything the poster states as a number, derived here and only here."""
    layers = shares.get("layers") or []
    comps = shares.get("companies") or []
    total = shares.get("total_ai_rev") or 0
    named = {l.get("id"): l for l in (thesis.get("layers") or [])}
    rows = []
    for l in layers:
        meta = named.get(l.get("id")) or {}
        rows.append({"name": meta.get("name") or l.get("id") or "",
                     "share": float(l.get("share") or 0)})
    rows.sort(key=lambda r: -r["share"])
    top_co = comps[:4]
    top3 = sum(float(c.get("share") or 0) for c in comps[:3])
    return {
        "total": total,
        "layers": rows,
        "top_layer": rows[0] if rows else None,
        "companies": [{"t": c.get("t"), "share": float(c.get("share") or 0)}
                      for c in top_co],
        "top3": top3,
        "n_companies": len(comps),
        "n_layers": len(rows),
        "empty_layers": [r["name"] for r in rows if r["share"] <= 0.0005],
    }


def _facts_text(f: dict) -> str:
    out = ["Total captured revenue across the chain: %s" % _fmt_b(f["total"])]
    if f["top_layer"]:
        out.append("Largest layer: %s at %s of the landed value"
                   % (f["top_layer"]["name"], _pct(f["top_layer"]["share"])))
    out.append("Top 3 companies together: %s" % _pct(f["top3"]))
    for c in f["companies"]:
        out.append("  %s %s" % (c["t"], _pct(c["share"])))
    if f["empty_layers"]:
        out.append("Layers capturing nothing yet: " + ", ".join(f["empty_layers"]))
    return "\n".join(out)


def _fallback_copy(thesis: dict, f: dict) -> dict:
    """Deterministic copy from the same numbers. Used when the AI chain is
    unavailable — which, on the free tier, is often."""
    tl = f["top_layer"]
    head = "Concentrated at the top of the chain"
    if tl and tl["share"] >= 0.5:
        head = "%s takes %s of it" % (tl["name"], _pct(tl["share"]))
    read = ("The value is concentrated: the top three names hold %s of everything "
            "the chain captures today." % _pct(f["top3"]))
    outs = []
    if tl:
        outs.append("%s captures %s of the landed value."
                    % (tl["name"], _pct(tl["share"])))
    outs.append("Top 3 companies hold %s between them." % _pct(f["top3"]))
    if f["empty_layers"]:
        outs.append("Nothing has landed yet at: %s." % f["empty_layers"][0])
    else:
        outs.append("Measured across %d companies in %d layers."
                    % (f["n_companies"], f["n_layers"]))
    return {"headline": head[:64], "read": read[:150],
            "takeaways": [o[:90] for o in outs[:3]], "ai": False}


async def summarise(thesis: dict, shares: dict) -> dict:
    """Poster content: computed facts + AI-written framing (or a deterministic
    fallback). Never lets the model touch a figure."""
    f = _facts(thesis, shares)
    out = {"title": thesis.get("title") or "Chain map",
           "slug": thesis.get("slug") or "",
           "eyebrow": (thesis.get("eyebrow") or "Chain map"),
           "facts": f,
           "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    copy = None
    try:
        import llm_free
        if llm_free.available():
            prompt = _PROMPT.format(
                title=out["title"],
                standfirst=(thesis.get("standfirst") or "")[:400],
                facts=_facts_text(f))
            parsed = await llm_free.chat_json(prompt, max_tokens=600, timeout=40.0,
                                              prefer="gemini")
            if isinstance(parsed, dict) and parsed.get("headline"):
                tk = [str(x)[:90] for x in (parsed.get("takeaways") or [])][:3]
                copy = {"headline": str(parsed["headline"])[:64],
                        "read": str(parsed.get("read") or "")[:150],
                        "takeaways": tk or _fallback_copy(thesis, f)["takeaways"],
                        "ai": True}
    except Exception as exc:
        logger.warning("infographic: AI copy failed for %s: %s", out["slug"], exc)
    out.update(copy or _fallback_copy(thesis, f))
    return out


# ── 2. Render ─────────────────────────────────────────────────────────────

def _font(sz, bold=False):
    """A font that is actually the size we asked for.

    The server has NO system TrueType fonts, so every truetype() lookup failed
    and we fell through to ImageFont.load_default() — a BITMAP font that ignores
    the size argument completely. Every string on the poster rendered at ~10px:
    the 92px headline figure came out 19px wide and the image looked empty.

    Order: real font files where they exist, then Pillow's own scalable default
    (>=10.1 accepts a size and bundles Aileron), then the bitmap as a last
    resort so this can never raise."""
    from PIL import ImageFont
    names = (["DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"]
             if bold else
             ["DejaVuSans.ttf", "LiberationSans-Regular.ttf", "arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"])
    for p in names:
        try:
            return ImageFont.truetype(p, sz)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=sz)      # Pillow >= 10.1, scalable
    except TypeError:
        return ImageFont.load_default()


def _safe(t):
    """Map characters Pillow's fallback font has no glyph for.

    When no system TrueType exists the poster is drawn with Pillow's bundled
    Aileron, which lacks the em-dash, curly quotes and the ellipsis — each of
    those renders as a hollow box. The AI copy and our own strings both use
    them freely, so normalise at the draw boundary rather than policing every
    caller."""
    return (str(t or "")
            .replace("—", "-").replace("–", "-")
            .replace("’", "'").replace("‘", "'")
            .replace("“", '"').replace("”", '"')
            .replace("…", "...").replace(" ", " "))


def _wrap(d, text, font, width):
    words, line, lines = _safe(text).split(), "", []
    for w in words:
        probe = (line + " " + w).strip()
        if d.textlength(probe, font=font) > width and line:
            lines.append(line)
            line = w
        else:
            line = probe
    if line:
        lines.append(line)
    return lines


def _round_rect(d, box, r, fill, outline=None):
    d.rounded_rectangle(box, radius=r, fill=fill, outline=outline, width=1)


def render_png(s: dict) -> bytes:
    """The poster. Pillow only — no browser, no network, no fonts fetched."""
    from PIL import Image, ImageDraw

    f = s["facts"]
    img = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(img)
    M = 64                      # margin
    y = 0

    # accent rule
    d.rectangle([0, 0, W, 8], fill=INDIGO)
    y = 58

    # eyebrow
    d.text((M, y), _safe("TICKERMOVER  ·  " + str(s["eyebrow"])).upper(),
           font=_font(20, True), fill=INDIGO)
    y += 42

    # headline (the AI's line, not the map title — that is the point of it)
    ft = _font(58, True)
    for ln in _wrap(d, s["headline"], ft, W - 2 * M)[:3]:
        d.text((M, y), ln, font=ft, fill=INK)
        y += 66
    y += 6

    # the map this is about
    d.text((M, y), _safe(s["title"])[:70], font=_font(24), fill=INK_SOFT)
    y += 48

    # ── the one number ────────────────────────────────────────────────────
    tl = f["top_layer"]
    CARD_H = 214                      # the 96px figure needs its own band; at
                                      # 176 it collided with the caption below
    _round_rect(d, [M, y, W - M, y + CARD_H], 22, CARD, HAIR)
    d.text((M + 30, y + 24), "LARGEST SHARE OF LANDED VALUE",
           font=_font(18, True), fill=FAINT)
    if tl:
        fbig = _font(92, True)
        d.text((M + 28, y + 56), _pct(tl["share"]), font=fbig, fill=GREEN)
        num_w = d.textlength(_pct(tl["share"]), font=fbig)
        # the layer name sits beside the figure, vertically centred on it
        nm = _wrap(d, _safe(tl["name"]), _font(24), W - 2 * M - int(num_w) - 90)[:2]
        ny = y + 78 + (14 if len(nm) == 1 else 0)
        for ln in nm:
            d.text((M + 54 + num_w, ny), ln, font=_font(24), fill=INK)
            ny += 32
    d.text((M + 30, y + 166), "of %s captured across the chain" % _fmt_b(f["total"]),
           font=_font(21), fill=INK_SOFT)
    y += CARD_H + 24

    # ── layer bars ────────────────────────────────────────────────────────
    rows = f["layers"][:6]
    ROW_H = 52                        # was 44 — label + bar had no breathing room
    _round_rect(d, [M, y, W - M, y + 62 + ROW_H * len(rows)], 22, CARD, HAIR)
    d.text((M + 30, y + 24), "WHERE IT LANDS, LAYER BY LAYER",
           font=_font(18, True), fill=FAINT)
    by = y + 62
    bx0, bx1 = M + 30, W - M - 30
    top = max([l["share"] for l in rows] or [1]) or 1
    f_lbl, f_pct = _font(20), _font(20, True)
    for i, l in enumerate(rows):
        col = RAMP[i % len(RAMP)]
        pct_txt = _pct(l["share"])
        pct_w = d.textlength(pct_txt, font=f_pct)
        # truncate on WIDTH with an ellipsis; a hard [:34] cut mid-word
        label, avail = l["name"], (bx1 - bx0) - pct_w - 24
        if d.textlength(label, font=f_lbl) > avail:
            while label and d.textlength(label + "…", font=f_lbl) > avail:
                label = label[:-1]
            label = label.rstrip(" ,(") + "..."
        d.text((bx0, by), _safe(label), font=f_lbl, fill=INK)
        d.text((bx1 - pct_w, by), pct_txt, font=f_pct, fill=INK)
        ty = by + 30
        d.rounded_rectangle([bx0, ty, bx1, ty + 11], radius=6, fill=(239, 237, 234))
        wpx = int((bx1 - bx0) * (l["share"] / top)) if top else 0
        if wpx > 8:
            d.rounded_rectangle([bx0, ty, bx0 + wpx, ty + 11], radius=6, fill=col)
        by += ROW_H
    y = by + 24

    # ── what it means ─────────────────────────────────────────────────────
    reads = _wrap(d, s["read"], _font(23), W - 2 * M - 60)[:3]
    box_h = 40 + 32 * len(reads) + 22 + 38 * len(s["takeaways"])
    _round_rect(d, [M, y, W - M, y + box_h], 22, CARD, HAIR)
    ry = y + 26
    for ln in reads:
        d.text((M + 30, ry), ln, font=_font(23), fill=INK)
        ry += 32
    ry += 10
    for t in s["takeaways"][:3]:
        d.ellipse([M + 32, ry + 10, M + 42, ry + 20], fill=ORANGE)
        for j, ln in enumerate(_wrap(d, t, _font(21), W - 2 * M - 90)[:2]):
            d.text((M + 58, ry + (j * 26)), ln, font=_font(21), fill=INK_SOFT)
        ry += 38
    y += box_h + 24

    # ── footer ────────────────────────────────────────────────────────────
    d.line([M, H - 128, W - M, H - 128], fill=HAIR, width=1)
    d.text((M, H - 108), "tickermover.com/who-benefits/%s" % s["slug"],
           font=_font(21, True), fill=INDIGO)
    stamp = (s.get("generated_at") or "")[:10]
    d.text((M, H - 74),
           "Descriptive model of where spending has already landed - "
           "not a forecast, not advice.", font=_font(17), fill=FAINT)
    d.text((M, H - 50), "Figures computed from reported revenue · %s" % stamp,
           font=_font(17), fill=FAINT)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
