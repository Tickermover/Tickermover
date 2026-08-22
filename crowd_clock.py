"""crowd_clock.py — the Crowd Clock card for /report and /stocks pages.

ONE reading, 0-100, describing where a share sits between "nobody is watching"
and "everybody is watching", with the historical base rates for that band.

WHAT IT IS NOT
    Not a buy/sell signal, not a price forecast, not a personal recommendation.
    The study behind it found forward RETURNS are close to unpredictable from
    price data while forward RISK is not, so the bands are risk-and-position
    descriptors. Specifically NOT supported by the evidence, so do not reword:
      · "overbought = sell"  — Crowded-band shares had slightly BETTER median
        forward returns than the rest across the full cross-section.
      · an exit trigger for a share already held — within one share's own
        history the most fragile quintile had higher median returns and a
        lower chance of being underwater a year later.

EVIDENCE
    1,514 US listed names (S&P 500 + 400 + 600 + the growth universe),
    233,855 stock-month observations, Feb 2009 - Jan 2026. Bands fixed on
    2009-2017; the rates below are held-out 2018-2026.
    See CROWD_CLOCK_RESEARCH.md for method, controls and limitations.

RENDERING
    Same shape as fact_check.py: render_card() returns a self-contained string
    (scoped CSS + HTML + a small script). The numbers load client-side from
    /api/crowd-clock/{ticker} so the SEO page render stays network-free.
"""
from __future__ import annotations

import html as _html
import logging

logger = logging.getLogger(__name__)

# ── the score ────────────────────────────────────────────────────────────────
# Published linear ramps, fixed weights. Deliberately transparent rather than
# fitted: a reader can recompute the number by hand and there is nothing to
# overfit.
RAMPS = {"run": (0.00, 1.50), "crowd": (0.70, 1.60), "stretch": (0.00, 0.40)}
WEIGHTS = {"run": 0.40, "crowd": 0.35, "stretch": 0.25}
BAND_EDGES = [(20, "Ignored"), (40, "Quiet"), (60, "Noticed"), (80, "Busy"),
              (101, "Crowded")]
ORDER = ["Damaged", "Ignored", "Quiet", "Noticed", "Busy", "Crowded"]

#: Share of observations in each band that, over the SIX MONTHS after the
#: reading, fell 30%+ / rose 30%+ from it, plus the median return and the share
#: still below the reading price a year later.
#: "all" = full sample. "cycle" = names that had a 40%+ drawdown in the prior
#: two years, held-out 2018-2026 only — the population the signal is built for.
BASE_RATES = {
    "all": {
        "Damaged": dict(n=43239, fall=.183, rise=.402, med6m=.103, uw12=.316),
        "Ignored": dict(n=56535, fall=.079, rise=.142, med6m=.059, uw12=.319),
        "Quiet":   dict(n=76211, fall=.082, rise=.154, med6m=.058, uw12=.314),
        "Noticed": dict(n=35336, fall=.106, rise=.241, med6m=.064, uw12=.321),
        "Busy":    dict(n=14312, fall=.126, rise=.337, med6m=.073, uw12=.329),
        "Crowded": dict(n=8222,  fall=.204, rise=.435, med6m=.080, uw12=.341),
    },
    "cycle": {
        "Damaged": dict(n=22294, fall=.222, rise=.438, med6m=.091, uw12=.337),
        "Ignored": dict(n=7560,  fall=.158, rise=.208, med6m=.014, uw12=.450),
        "Quiet":   dict(n=13739, fall=.139, rise=.243, med6m=.037, uw12=.414),
        "Noticed": dict(n=10343, fall=.152, rise=.347, med6m=.076, uw12=.371),
        "Busy":    dict(n=5759,  fall=.143, rise=.407, med6m=.088, uw12=.355),
        "Crowded": dict(n=4190,  fall=.213, rise=.465, med6m=.085, uw12=.339),
    },
}

#: THE TURN — fell 40%+ in the last two years, back above a rising 50-day, the
#: crowd not yet arrived (volume < 1.10x its own norm), still 20%+ below the
#: 12-month high with under 60% of the move banked.
#:
#: 15,086 firings, 1,229 names, 2009-2026. Read `all` before believing anything
#: else here: over six months it returned a median +8.6% raw but -0.3% AGAINST
#: THE S&P, underperforming the index exactly half the time. On its own IT IS
#: NOT AN EDGE and must never be shown as an entry.
#:
#: What did separate good turns from bad ones is how much of the market was
#: already wrecked when it fired. `beat` = share that beat the index over 6m.
#: CAVEAT to keep next to the washout numbers wherever they appear: the effect
#: is carried by 2020-2026. In 2009-2014 the same buckets were negative. That
#: era had two violent V-shaped recoveries; this may not survive a slow bear.
TURN_RATES = {
    "all":     dict(n=15086, x6m=-.003, med6m=.086, rise=.36, fall=.18, beat=.50, dip=-.134),
    "calm":    dict(n=3228,  x6m=-.027, med6m=.039, rise=.32, fall=.22, beat=.46, lo=0.0, hi=.10),
    "some":    dict(n=6227,  x6m=-.042, med6m=.033, rise=.28, fall=.23, beat=.43, lo=.10, hi=.20),
    "broad":   dict(n=4544,  x6m=.049,  med6m=.158, rise=.46, fall=.11, beat=.58, lo=.20, hi=.35),
    "washout": dict(n=1087,  x6m=.063,  med6m=.221, rise=.52, fall=.10, beat=.61, lo=.35, hi=1.01),
}
TURN_BUCKETS = ["calm", "some", "broad", "washout"]
TURN_LABEL = {"calm": "a calm market", "some": "a mildly damaged market",
              "broad": "a broadly damaged market", "washout": "a market-wide washout"}


def turn_bucket(wrecked_share) -> str:
    """Which breadth bucket a turn fired in — the only thing in the study that
    separated the good turns from the bad ones."""
    if wrecked_share is None:
        return "all"
    for k in TURN_BUCKETS:
        r = TURN_RATES[k]
        if r["lo"] <= wrecked_share < r["hi"]:
            return k
    return "washout"
BASELINE = dict(fall=.111, rise=.231, med6m=.066, uw12=.318)

#: Descriptive only. No instruction, no directive verb — see the FCA note in
#: CROWD_CLOCK_RESEARCH.md §7 before editing any of this copy.
BAND_COPY = {
    "Damaged": "Down hard and still inside the fall. News flow is heavy here.",
    "Ignored": "Little of the move is behind it and trading volume is below its "
               "own normal — historically the least-watched point of the cycle.",
    "Quiet":   "Off the low, with volume still near its own normal. The crowd "
               "has not arrived yet.",
    "Noticed": "The move is established and volume is picking up.",
    "Busy":    "A large part of the move is behind it and volume is well above "
               "its own average.",
    "Crowded": "Most of the move off the low is already behind it and volume is "
               "far above its own average — the busiest point of the cycle, and "
               "historically the point of highest six-month drawdown risk.",
}
BAND_TONE = {"Damaged": "hot", "Ignored": "cool", "Quiet": "cool",
             "Noticed": "warm", "Busy": "warm", "Crowded": "hot"}


def _ramp(x, lo, hi):
    return min(max((x - lo) / (hi - lo), 0.0), 1.0) * 100.0


def score(run: float, crowd: float, stretch: float) -> float:
    return (WEIGHTS["run"] * _ramp(run, *RAMPS["run"])
            + WEIGHTS["crowd"] * _ramp(crowd, *RAMPS["crowd"])
            + WEIGHTS["stretch"] * _ramp(stretch, *RAMPS["stretch"]))


def band(s: float, damage: float) -> str:
    """A wreck still falling is a different situation from a quiet base, so
    deep-drawdown names scoring low are labelled Damaged, not Ignored."""
    if damage <= -0.25 and s < 40:
        return "Damaged"
    for edge, name in BAND_EDGES:
        if s < edge:
            return name
    return "Crowded"


def compute(closes: list[float], volumes: list[float]) -> dict | None:
    """Reading from daily adjusted closes + volumes (oldest first).
    Needs 200 sessions minimum; 504 for the in-cycle gate."""
    n = len(closes)
    if n < 200 or len(volumes) != n:
        return None
    try:
        px = float(closes[-1])
        w252 = closes[-252:] if n >= 252 else closes
        lo252, hi252 = min(w252), max(w252)
        ma200 = sum(closes[-200:]) / 200.0
        dollar = [c * v for c, v in zip(closes, volumes)]
        dv20 = sum(dollar[-20:]) / 20.0
        dv200 = sum(dollar[-200:]) / 200.0
        if lo252 <= 0 or ma200 <= 0 or dv200 <= 0:
            return None

        run = px / lo252 - 1.0
        crowd = dv20 / dv200
        stretch = px / ma200 - 1.0
        damage = px / hi252 - 1.0

        # in_cycle: a 40%+ drawdown from a rolling 252d high in the last 2 years
        in_cycle = False
        if n >= 504:
            for i in range(max(252, n - 504), n):
                w = closes[max(0, i - 252):i + 1]
                if w and closes[i] / max(w) - 1.0 <= -0.40:
                    in_cycle = True
                    break

        s = score(run, crowd, stretch)
        b = band(s, damage)
        return {
            "score": round(s, 1), "band": b, "in_cycle": in_cycle,
            "run": round(run, 4), "crowd": round(crowd, 3),
            "stretch": round(stretch, 4), "damage": round(damage, 4),
            "copy": BAND_COPY[b], "tone": BAND_TONE[b],
            "rates": BASE_RATES["cycle" if in_cycle else "all"][b],
            "baseline": BASELINE,
            "scope": ("US listed shares that had already fallen 40%+ in the "
                      "prior two years" if in_cycle else "all shares studied"),
        }
    except Exception as exc:                       # never break a page
        logger.warning(f"crowd_clock.compute failed: {exc}")
        return None


# ── the card ─────────────────────────────────────────────────────────────────
_CSS = """
<style>
.cclk{border:1px solid rgba(10,10,10,.1);border-radius:16px;padding:24px 26px;margin:36px 0;background:#fff;box-shadow:0 10px 30px -24px rgba(10,10,10,.25)}
.cclk-head{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:6px}
.cclk-h{font-family:'Public Sans',system-ui,sans-serif;font-size:20px;font-weight:500;color:#0A2F46;margin:0}
.cclk-pill{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:12.5px;font-weight:700;letter-spacing:.02em;padding:6px 12px;border-radius:999px;white-space:nowrap}
.cclk-cool .cclk-pill{color:#0E7C66;background:rgba(14,124,102,.1);border:1px solid rgba(14,124,102,.28)}
.cclk-warm .cclk-pill{color:#A16207;background:rgba(161,98,7,.1);border:1px solid rgba(161,98,7,.28)}
.cclk-hot .cclk-pill{color:#C74E00;background:rgba(199,78,0,.1);border:1px solid rgba(199,78,0,.28)}
.cclk-sub{font-size:12.5px;color:#5d6c7b;margin:0 0 18px}
.cclk-track{display:grid;grid-template-columns:repeat(6,1fr);gap:3px;margin:0 0 8px}
.cclk-seg{height:9px;border-radius:4px;background:#EDEBE7}
.cclk-seg.on{background:#758696}
/* Attention RAMP: one hue, light to dark, left to right. More attention is
   darker - nothing more. Deliberately NOT a green-to-red traffic light:
   "Crowded" is not a sell signal (crowded-band shares had slightly BETTER
   median forward returns in the study), and colouring it red would state a
   view the research explicitly refuses to make. Green and red on this site
   mean up and down, and neither is what a band is. */
.cclk-seg:nth-child(1){background:#DDE5EA}
.cclk-seg:nth-child(2){background:#C4D2DB}
.cclk-seg:nth-child(3){background:#A6BAC7}
.cclk-seg:nth-child(4){background:#7F9AAC}
.cclk-seg:nth-child(5){background:#54798F}
.cclk-seg:nth-child(6){background:#2B5A73}
/* the share's own band: the accent, plus a ring so position is not colour
   alone for a reader who cannot separate these steps */
.cclk-seg.here{background:#C74E00!important;box-shadow:0 0 0 2px #fff,0 0 0 4px #C74E00}
.cclk-scale span.here{color:#C74E00;font-weight:600}
.cclk-cool .cclk-seg.here{background:#0E7C66!important;box-shadow:0 0 0 2px #fff,0 0 0 4px #0E7C66}
.cclk-warm .cclk-seg.here{background:#A16207!important;box-shadow:0 0 0 2px #fff,0 0 0 4px #A16207}
.cclk-hot .cclk-seg.here{background:#C74E00!important;box-shadow:0 0 0 2px #fff,0 0 0 4px #C74E00}
.cclk-scale{display:grid;grid-template-columns:repeat(6,1fr);gap:3px;margin:0 0 18px;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:10px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#758696}
.cclk-scale span{text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cclk-scale span.here{color:#0A2F46}
.cclk-copy{font-size:14.5px;line-height:1.6;color:#5d6c7b;margin:0 0 18px}
.cclk-inputs{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin:0 0 20px;padding:14px 0;border-top:1px solid rgba(10,10,10,.06);border-bottom:1px solid rgba(10,10,10,.06)}
.cclk-in b{display:block;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:19px;font-weight:800;color:#0A2F46;font-variant-numeric:tabular-nums}
.cclk-in span{display:block;font-size:11.5px;color:#5d6c7b;margin-top:3px;line-height:1.35}
.cclk-lab{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:#758696;margin:0 0 10px}
.cclk-rates{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.cclk-rate{border:1px solid rgba(10,10,10,.08);border-radius:12px;padding:14px 16px}
.cclk-rate b{display:block;font-family:'JetBrains Mono',ui-monospace,monospace;font-size:26px;font-weight:800;line-height:1;font-variant-numeric:tabular-nums}
.cclk-fall b{color:#C74E00}
.cclk-rise b{color:#0E7C66}
.cclk-rate span{display:block;font-size:12.5px;color:#5d6c7b;margin-top:7px;line-height:1.45}
.cclk-rate em{display:block;font-style:normal;font-size:11.5px;color:#758696;margin-top:6px}
.cclk-note{font-size:12px;line-height:1.55;color:#758696;margin:16px 0 0}
.cclk-off{font-size:13.5px;color:#758696;margin:0}
@media (max-width:520px){.cclk-rates{grid-template-columns:1fr}}
</style>
"""


def card_body(sym: str, r: dict) -> str:
    """Inner HTML for a reading — shared by the server render and the client JS."""
    pct = lambda v: f"{round(v * 100):.0f}%"
    idx = ORDER.index(r["band"])
    segs = "".join(
        f'<div class="cclk-seg {"here" if i == idx else ("on" if i < idx else "")}"></div>'
        for i in range(6))
    scale = "".join(f'<span class="{"here" if i == idx else ""}">{n}</span>'
                    for i, n in enumerate(ORDER))
    rates, base = r["rates"], r["baseline"]
    return f"""
  <div class="cclk-head">
    <h3 class="cclk-h">Hype Check</h3>
    <span class="cclk-pill">{_html.escape(r["band"])} &middot; {r["score"]:.0f} / 100</span>
  </div>
  <p class="cclk-sub">Where {_html.escape(sym)} sits between “nobody is watching” and
     “everybody is watching”, from price and volume alone.</p>
  <div class="cclk-track">{segs}</div>
  <div class="cclk-scale">{scale}</div>
  <p class="cclk-copy">{_html.escape(r["copy"])}</p>
  <div class="cclk-inputs">
    <div class="cclk-in"><b>{r["run"] * 100:+.0f}%</b><span>from its 12-month low</span></div>
    <div class="cclk-in"><b>{r["crowd"]:.2f}&times;</b><span>volume vs its own 200-day average</span></div>
    <div class="cclk-in"><b>{r["stretch"] * 100:+.0f}%</b><span>vs its 200-day average price</span></div>
    <div class="cclk-in"><b>{r["damage"] * 100:.0f}%</b><span>from its 12-month high</span></div>
  </div>
  <p class="cclk-lab">What followed historically, over six months</p>
  <div class="cclk-rates">
    <div class="cclk-rate cclk-fall"><b>{pct(rates["fall"])}</b>
      <span>of readings in this band saw a fall of 30% or more</span>
      <em>all shares, any band: {pct(base["fall"])}</em></div>
    <div class="cclk-rate cclk-rise"><b>{pct(rates["rise"])}</b>
      <span>of readings in this band saw a rise of 30% or more</span>
      <em>all shares, any band: {pct(base["rise"])}</em></div>
  </div>
  <p class="cclk-note">Based on {rates["n"]:,} readings in this band among {r["scope"]},
     1,514 US listed shares, 2009&ndash;2026. These are historical frequencies for shares in a
     similar state &mdash; not a forecast for {_html.escape(sym)} and not a probability for it.
     Past performance is not a reliable indicator of future results. General information and
     research, not advice and not a personal recommendation. Capital at risk.</p>"""


def _bands_frame(score, damage):
    """Vectorised band(), same rules as band() above."""
    import numpy as np, pandas as pd
    out = pd.DataFrame("Crowded", index=score.index, columns=score.columns, dtype=object)
    prev = -1.0
    for edge, name in BAND_EDGES:
        out = out.mask((score >= prev) & (score < edge), name)
        prev = edge
    return out.mask((damage <= -0.25) & (score < 40), "Damaged").where(score.notna())


def scan_universe(tickers: list[str], sectors: dict | None = None,
                  progress=None) -> dict:
    """The whole universe in one pass: a reading per name, recent band changes,
    the turn list, and where the damage sits by sector.

    Slow (about a minute for ~500 names) — call it from a background task.
    `progress(done, total)` is called after each chunk."""
    import numpy as np, pandas as pd, yfinance as yf
    sectors = sectors or {}
    syms = sorted({(s or "").upper().strip() for s in tickers if s})
    cl_parts, vo_parts, done, CH = [], [], 0, 60
    for i in range(0, len(syms), CH):
        chunk = syms[i:i + CH]
        try:
            raw = yf.download(chunk, period="3y", interval="1d", auto_adjust=True,
                              progress=False, threads=True, group_by="column",
                              actions=False)
            if raw is not None and not raw.empty:
                c, v = raw["Close"], raw["Volume"]
                if isinstance(c, pd.Series):
                    c, v = c.to_frame(chunk[0]), v.to_frame(chunk[0])
                cl_parts.append(c); vo_parts.append(v)
        except Exception as exc:
            logger.warning(f"crowd_clock scan chunk {i} failed: {exc}")
        done += len(chunk)
        if progress:
            try: progress(done, len(syms))
            except Exception: pass
    if not cl_parts:
        return {"rows": [], "sectors": [], "turns": [], "changes": []}

    close = pd.concat(cl_parts, axis=1).sort_index()
    vol = pd.concat(vo_parts, axis=1).sort_index()
    close = close.loc[:, ~close.columns.duplicated()].astype("float64")
    vol = vol.reindex(columns=close.columns).astype("float64")
    close = close.dropna(axis=1, thresh=200)
    vol = vol[close.columns]

    dollar = close * vol
    adv = dollar.rolling(200).mean()
    crowd = dollar.rolling(20).mean() / adv
    ma50, ma200 = close.rolling(50).mean(), close.rolling(200).mean()
    mx, mn = close.rolling(252, min_periods=200).max(), close.rolling(252, min_periods=200).min()
    dmg, run = close / mx - 1.0, close / mn - 1.0
    stretch = close / ma200 - 1.0
    sl50 = ma50 / ma50.shift(21) - 1.0
    worst2y = dmg.rolling(504, min_periods=252).min()

    def _ramp_df(x, lo, hi):
        return ((x - lo) / (hi - lo)).clip(0, 1) * 100.0

    sc = (WEIGHTS["run"] * _ramp_df(run, *RAMPS["run"])
          + WEIGHTS["crowd"] * _ramp_df(crowd, *RAMPS["crowd"])
          + WEIGHTS["stretch"] * _ramp_df(stretch, *RAMPS["stretch"]))
    bands = _bands_frame(sc, dmg)

    # ── the turn: fell hard, stopped falling, crowd not here yet ────────────
    turn = ((worst2y <= -0.40) & (dmg <= -0.20) & (close > ma50) & (sl50 > 0)
            & (crowd < 1.10) & (run < 0.60) & (adv > 3e6) & (close > 5))
    turn = turn.fillna(False).astype(bool)
    fired = turn & ~turn.shift(1, fill_value=False)

    last = close.index[-1]
    # ── sector breadth: share of a sector 30%+ below its own 12-month high ──
    sec_of = {t: (sectors.get(t) or "Other").strip() or "Other" for t in close.columns}
    wreck = (dmg <= -0.30)
    sec_rows = []
    groups = {}
    for t, s in sec_of.items():
        groups.setdefault(s, []).append(t)
    groups["All shares"] = list(close.columns)
    for s, cols in groups.items():
        # a 5-name "sector" produces a headline percentage that means nothing —
        # one stock moves it 20 points
        if len(cols) < 15 and s != "All shares":
            continue
        w = wreck[cols].sum(axis=1) / dmg[cols].notna().sum(axis=1)
        w = w.dropna()
        if len(w) < 250:
            continue
        now = float(w.iloc[-1])
        yr = w.iloc[-252:]
        sec_rows.append({
            "sector": s, "n": len(cols), "now": round(now, 4),
            "median": round(float(w.median()), 4),
            "pctile": round(float((w <= now).mean()), 3),
            "peak1y": round(float(yr.max()), 4),
            "peak1y_at": yr.idxmax().strftime("%Y-%m-%d"),
            "chg21": round(now - float(w.iloc[-22]), 4) if len(w) > 22 else None,
            # buckets are the ones the study validated (see TURN_RATES), not a
            # percentile of this sector's own 3y window — too short to trust
            "bucket": turn_bucket(now),
        })
    sec_rows.sort(key=lambda r: -r["now"])

    rows, changes, turns = [], [], []
    b_last = bands.iloc[-1]
    for t in close.columns:
        b = b_last.get(t)
        if not isinstance(b, str):
            continue
        col = bands[t].dropna()
        # how long has it read this band, and what did it read before?
        held, prev = 0, None
        for k in range(len(col) - 1, -1, -1):
            if col.iloc[k] == b:
                held += 1
            else:
                prev = col.iloc[k]; break
        chg_at = col.index[len(col) - held] if held < len(col) else None
        in_cycle = bool(worst2y[t].iloc[-1] <= -0.40) if not np.isnan(worst2y[t].iloc[-1]) else False
        r = {"ticker": t, "score": round(float(sc[t].iloc[-1]), 1), "band": b,
             "tone": BAND_TONE[b], "in_cycle": in_cycle,
             "sector": sec_of.get(t, "Other"),
             "run": round(float(run[t].iloc[-1]), 4),
             "crowd": round(float(crowd[t].iloc[-1]), 3),
             "stretch": round(float(stretch[t].iloc[-1]), 4),
             "damage": round(float(dmg[t].iloc[-1]), 4),
             "px": round(float(close[t].iloc[-1]), 2),
             "held": int(held), "prev": prev,
             "changed": chg_at.strftime("%Y-%m-%d") if chg_at is not None else None,
             "turn": bool(turn[t].iloc[-1])}
        rates = BASE_RATES["cycle" if in_cycle else "all"][b]
        r["fall"], r["rise"] = rates["fall"], rates["rise"]
        rows.append(r)

        # band changes in the last 30 sessions, upgrades out of the wreck first
        if chg_at is not None and held <= 30 and prev:
            changes.append({**{k: r[k] for k in ("ticker", "band", "sector", "px",
                                                 "run", "crowd", "damage", "score",
                                                 "tone", "in_cycle")},
                            "prev": prev, "changed": r["changed"], "held": int(held),
                            "up": ORDER.index(b) > ORDER.index(prev)})
        # turns fired in the last 30 sessions
        f = fired[t].iloc[-30:]
        if f.any():
            d = f[f].index[-1]
            secnow = next((x["now"] for x in sec_rows if x["sector"] == sec_of.get(t)), None)
            turns.append({**{k: r[k] for k in ("ticker", "band", "sector", "px", "run",
                                               "crowd", "damage", "score", "tone")},
                          "fired": d.strftime("%Y-%m-%d"),
                          "days": int((last - d).days),
                          "since": round(float(close[t].iloc[-1] / close[t][d] - 1), 4),
                          "sector_wrecked": secnow})
    rows.sort(key=lambda x: -x["score"])
    changes.sort(key=lambda x: (x["changed"] or ""), reverse=True)
    turns.sort(key=lambda x: (x["fired"] or ""), reverse=True)
    return {"rows": rows, "sectors": sec_rows, "changes": changes, "turns": turns,
            "asof": last.strftime("%Y-%m-%d")}


def render_card(t: dict) -> str:
    """Card shell. Numbers arrive client-side from /api/crowd-clock/{ticker} so
    the page render stays network-free."""
    sym = _html.escape((t.get("ticker") or "").upper())
    if not sym:
        return ""
    return _CSS + f"""
<div class="cclk cclk-cool" id="cclk-card" data-sym="{sym}">
  <div class="cclk-head"><h3 class="cclk-h">Hype Check</h3></div>
  <p class="cclk-off" id="cclk-status">Reading {sym}&rsquo;s price and volume history&hellip;</p>
</div>
<script>
(function(){{
  var el = document.getElementById('cclk-card');
  if (!el) return;
  var sym = el.getAttribute('data-sym');
  fetch('/api/crowd-clock/' + encodeURIComponent(sym))
    .then(function(r){{ return r.ok ? r.json() : null; }})
    .then(function(d){{
      if (!d || !d.html) {{
        document.getElementById('cclk-status').textContent =
          'Not enough price history to read the clock for ' + sym + '.';
        return;
      }}
      el.className = 'cclk cclk-' + (d.tone || 'cool');
      el.innerHTML = d.html;
    }})
    .catch(function(){{
      document.getElementById('cclk-status').textContent =
        'The clock is unavailable right now.';
    }});
}})();
</script>"""
