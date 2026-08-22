"""Inline-SVG charts for the /stocks one-pager.

Server-rendered on purpose: a crawler sees them, there is no chart library to
load, and nothing here can fail at runtime the way a client-side fetch can.

FORM FOLLOWS THE DATA'S JOB, which is the only reason these are the shapes they
are:
  * pillar scores and margins are MAGNITUDE across labelled rows -> horizontal
    bars, ONE hue, length carries the value. No categorical palette is involved,
    so no hue is doing identity work and none needs CVD separation.
  * 52-week and analyst-target are POSITION WITHIN A SPAN -> a track with a
    marker, not a bar. The value is a point, not a quantity from zero. This is
    the shape a real analyst report already uses for both.
  * EPS surprise is POLARITY -> the reserved up/down status pair, and the label
    always carries the sign so it is never colour alone.

Every chart is a SINGLE series, so none carries a legend - the caption names it.
Values are direct-labelled; axes and tracks stay recessive. Each mark carries a
<title> so hovering gives the number with no script.
"""

import html as _h

INK = "#14587D"      # single sequential hue for magnitude
TRACK = "#EDEBE7"    # recessive track
UP = "#16a34a"
DOWN = "#ea384c"
MARK = "#C74E00"     # AA-safe orange: the current-price marker


def _pretty(key):
    """ai_scorer component keys -> reader-facing labels."""
    NAMES = {
        "growth_tier": "Growth", "momentum_1m": "1-month momentum",
        "rel_strength": "Relative strength", "volume_spike": "Volume",
        "rsi_zone": "RSI zone", "dist_52w_high": "Near 52w high",
        "analyst_cons": "Analyst consensus", "earnings_prox": "Earnings proximity",
        "low_short": "Low short interest", "mkt_cap_fit": "Size fit",
        "fundamentals": "Fundamentals", "social_momentum": "Social momentum",
        "insider_bias": "Insider bias", "earnings_quality": "Earnings quality",
        "trend_strength": "Trend strength", "breakout_proximity": "Breakout proximity",
        "news_sentiment": "News sentiment", "earnings_acceleration": "Earnings acceleration",
        "score_momentum": "Score momentum",
    }
    return NAMES.get(key, str(key).replace("_", " ").capitalize())


def _num(v):
    try:
        f = float(v)
        return None if f != f else f
    except Exception:
        return None


def hbars(rows, maxv=100.0, height_per=30):
    """rows = [(label, value, display)] -> horizontal bars."""
    rows = [r for r in rows if _num(r[1]) is not None]
    if not rows:
        return ""
    W, LBL, PADR = 470, 118, 56
    plot = W - LBL - PADR
    H = len(rows) * height_per + 8
    out = ['<svg class="ch" viewBox="0 0 %d %d" role="img" '
           'preserveAspectRatio="xMidYMid meet">' % (W, H)]
    for i, (lbl, val, disp) in enumerate(rows):
        v = max(0.0, min(float(val), maxv))
        y = i * height_per + 6
        bw = max(3.0, plot * (v / maxv)) if maxv else 3.0
        out.append('<text x="0" y="%d" class="ch-lbl">%s</text>' % (y + 12, _h.escape(str(lbl))))
        out.append('<rect x="%d" y="%d" width="%d" height="10" rx="5" fill="%s"/>'
                   % (LBL, y + 3, plot, TRACK))
        out.append('<rect x="%d" y="%d" width="%.1f" height="10" rx="5" fill="%s">'
                   '<title>%s: %s</title></rect>'
                   % (LBL, y + 3, bw, INK, _h.escape(str(lbl)), _h.escape(str(disp))))
        out.append('<text x="%d" y="%d" class="ch-val">%s</text>'
                   % (LBL + plot + 8, y + 12, _h.escape(str(disp))))
    out.append('</svg>')
    return "".join(out)


def span(lo, hi, cur, mid=None, label_lo="", label_hi="", cur_label=""):
    """Where one value sits inside a range. A marker, not a bar."""
    lo, hi, cur = _num(lo), _num(hi), _num(cur)
    if lo is None or hi is None or cur is None or hi <= lo:
        return ""
    W, H, PAD = 470, 62, 12
    plot = W - PAD * 2
    fx = lambda v: PAD + plot * (max(lo, min(v, hi)) - lo) / (hi - lo)
    x = fx(cur)
    out = ['<svg class="ch" viewBox="0 0 %d %d" role="img" '
           'preserveAspectRatio="xMidYMid meet">' % (W, H),
           '<rect x="%d" y="20" width="%d" height="8" rx="4" fill="%s"/>' % (PAD, plot, TRACK)]
    m = _num(mid)
    if m is not None and lo < m < hi:
        mx = fx(m)
        out.append('<rect x="%.1f" y="16" width="2" height="16" fill="#B9C2C9"/>' % (mx - 1))
        out.append('<text x="%.1f" y="50" class="ch-tick" text-anchor="middle">mean</text>' % mx)
    out.append('<circle cx="%.1f" cy="24" r="7" fill="%s" stroke="#fff" stroke-width="2">'
               '<title>%s</title></circle>' % (x, MARK, _h.escape(cur_label or str(cur))))
    out.append('<text x="%d" y="12" class="ch-tick">%s</text>' % (PAD, _h.escape(label_lo)))
    out.append('<text x="%d" y="12" class="ch-tick" text-anchor="end">%s</text>'
               % (W - PAD, _h.escape(label_hi)))
    if cur_label:
        anchor = "start" if x < W * 0.5 else "end"
        out.append('<text x="%.1f" y="50" class="ch-cur" text-anchor="%s">%s</text>'
                   % (min(max(x, PAD), W - PAD), anchor, _h.escape(cur_label)))
    out.append('</svg>')
    return "".join(out)


def surprise(quarters):
    """EPS vs estimate by quarter. Beat/miss is a STATE, so it wears the
    reserved up/down pair and the label always carries the sign."""
    pts = []
    for q in (quarters or [])[:4]:
        if not isinstance(q, dict):
            continue
        sp = _num(q.get("surprise_pct"))
        if sp is None:
            continue
        # surprise_pct arrives as a fraction from some sources and a percent
        # from others; see the earnings-dates note. Treat <=1.5 as a fraction.
        if abs(sp) <= 1.5:
            sp *= 100.0
        pts.append(((q.get("date") or "")[:7], sp))
    if not pts:
        return ""
    pts.reverse()
    W, H, PAD = 470, 120, 10
    colw = (W - PAD * 2) / len(pts)
    top, bot = 18, 88
    peak = max(abs(v) for _, v in pts) or 1.0
    zero = bot if min(v for _, v in pts) >= 0 else (top + bot) / 2.0
    out = ['<svg class="ch" viewBox="0 0 %d %d" role="img" '
           'preserveAspectRatio="xMidYMid meet">' % (W, H),
           '<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" stroke="#DDD8D2" stroke-width="1"/>'
           % (PAD, zero, W - PAD, zero)]
    for i, (lab, v) in enumerate(pts):
        cx = PAD + colw * i + colw / 2.0
        bw = min(46.0, colw * 0.52)
        h = max(3.0, (abs(v) / peak) * (zero - top))
        y = zero - h if v >= 0 else zero
        out.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="4" fill="%s">'
                   '<title>%s: %+.1f%% vs estimate</title></rect>'
                   % (cx - bw / 2.0, y, bw, h, UP if v >= 0 else DOWN, _h.escape(lab), v))
        out.append('<text x="%.1f" y="%.1f" class="ch-val" text-anchor="middle">%+.0f%%</text>'
                   % (cx, (y - 5) if v >= 0 else (y + h + 13), v))
        out.append('<text x="%.1f" y="%d" class="ch-tick" text-anchor="middle">%s</text>'
                   % (cx, H - 5, _h.escape(lab)))
    out.append('</svg>')
    return "".join(out)


def build(t, price):
    """Return the inner HTML for the charts panel, or "" when nothing plots."""
    blocks = []

    # What actually drove the score. ai_scorer emits `weighted` = each
    # component's contribution in points; `breakdown` = its raw 0-1 reading.
    # The *_score fields app.py refers to elsewhere are not produced by
    # anything and have never existed, which is why this chart was blank.
    weighted = t.get("weighted") if isinstance(t.get("weighted"), dict) else {}
    breakdown = t.get("breakdown") if isinstance(t.get("breakdown"), dict) else {}
    if weighted:
        top = sorted(((k, _num(v) or 0.0) for k, v in weighted.items()),
                     key=lambda kv: kv[1], reverse=True)[:7]
        rows = []
        for key, contrib in top:
            raw = _num(breakdown.get(key))
            if raw is None:
                continue
            rows.append((_pretty(key), raw * 100.0, "%.0f" % (raw * 100.0)))
        if rows:
            blocks.append(("What drove the score",
                           "The seven components contributing most, each read out of 100.",
                           hbars(rows, 100.0)))

    pr = _num(price)
    # Canonical names on the universe row are high_52w / low_52w; the
    # week_52_* pair exists in data_coordinator but never reaches here.
    lo = _num(t.get("low_52w") or t.get("week_52_low") or t.get("fifty_two_week_low"))
    hi = _num(t.get("high_52w") or t.get("week_52_high") or t.get("fifty_two_week_high"))
    if lo and hi and pr:
        blocks.append(("52-week range", "Where the price sits across the last year.",
                       span(lo, hi, pr, None, "$%,.2f" % lo if False else "${:,.2f}".format(lo),
                            "${:,.2f}".format(hi), "${:,.2f} now".format(pr))))

    tl, th, tm = _num(t.get("target_low")), _num(t.get("target_high")), _num(t.get("target_mean"))
    if tl and th and pr:
        blocks.append(("Analyst target range", "The spread of published targets, against today's price.",
                       span(tl, th, pr, tm, "${:,.2f}".format(tl), "${:,.2f}".format(th),
                            "${:,.2f} now".format(pr))))

    mrows = []
    for lbl, key in (("Gross", "gross_margin"), ("Operating", "operating_margin"),
                     ("Net", "profit_margin"), ("Free cash flow", "fcf_margin")):
        v = _num(t.get(key))
        if v is None:
            continue
        if abs(v) <= 1.5:
            v *= 100.0
        mrows.append((lbl, max(0.0, v), "%.1f%%" % v))
    if mrows:
        blocks.append(("Margin profile", "What survives each step down the income statement.",
                       hbars(mrows, max(100.0, max(r[1] for r in mrows)))))

    sv = surprise(t.get("eps_quarters"))
    if sv:
        blocks.append(("EPS surprise", "Reported EPS against estimate, last four quarters.", sv))

    blocks = [b for b in blocks if b[2]]
    if not blocks:
        return ""
    return '<div class="ch-grid">' + "".join(
        '<figure class="ch-fig"><figcaption><b>%s</b><span>%s</span></figcaption>%s</figure>'
        % (_h.escape(ttl), _h.escape(sub), svg) for ttl, sub, svg in blocks) + '</div>'
