"""
TickerMover — Intelligence Layer (v1)
====================================

A self-contained "second brain" for the Alpha Score engine. Adds four classes of
intelligence on top of the existing scoring pipeline:

    1. MarketRegime          — top-down macro overlay (SPY, QQQ, VIX, ^TNX)
                               produces a 0–100 regime_score, regime_label,
                               and a 0.85–1.15 multiplier applied to Alpha Score.

    2. ScoreHistory          — rolling per-ticker Alpha Score history persisted
                               via the existing SmartCache. Surfaces a
                               *score_velocity* (delta over last N hours) and
                               *score_acceleration* signal — early movers light
                               up before the underlying components flip.

    3. New component scorers — trend_strength, breakout_proximity,
                               news_sentiment, earnings_acceleration.
                               Importable from ai_scorer.py.

    4. ThesisGenerator       — turns a fully-enriched ticker dict + the
                               component breakdown into a structured
                               investment thesis: bull case, bear case,
                               plain-English explanation, trade plan, and
                               recommendation. Pure-Python / deterministic so
                               it works with zero extra API keys; falls back to
                               an LLM when ANTHROPIC_API_KEY is configured.

The module is designed to be additive — nothing here mutates existing
state unless an explicit `apply_*` helper is called. Importing this file alone
has no side effects.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Optional LLM dependency — never required ──────────────────────────────
try:
    import httpx                 # already a project dep via data_coordinator
    _HTTPX_AVAILABLE = True
except ImportError:              # pragma: no cover
    _HTTPX_AVAILABLE = False

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:              # pragma: no cover
    _YF_AVAILABLE = False


# ╔════════════════════════════════════════════════════════════════════════╗
# ║  1. New Component Scorers                                              ║
# ╚════════════════════════════════════════════════════════════════════════╝

def _safe(d: dict, key: str, default=None):
    v = d.get(key)
    return default if v is None else v


def _clamp(val: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, val))


def score_trend_strength(t: dict) -> float:
    """
    Multi-timeframe trend alignment.
        price > sma50 > sma200   ⇒ strong uptrend (1.00)
        price > sma50 < sma200   ⇒ recovery       (0.65)
        price < sma50 > sma200   ⇒ pullback       (0.45)
        price < sma50 < sma200   ⇒ downtrend      (0.15)
    Falls back to neutral when SMAs are missing.
    """
    p   = _safe(t, "price")
    s50 = _safe(t, "sma_50")
    s200= _safe(t, "sma_200")
    if not p or not s50 or not s200:
        return 0.60
    above_50  = p   > s50
    above_200 = p   > s200
    fifty_gt_200 = s50 > s200

    if above_50 and above_200 and fifty_gt_200:    return 1.00   # textbook uptrend
    if above_50 and above_200:                      return 0.85
    if above_50 and not above_200:                  return 0.65   # base building
    if not above_50 and above_200 and fifty_gt_200: return 0.45   # in pullback
    if not above_50 and above_200:                  return 0.35
    if not above_50 and not above_200:              return 0.15   # bear trend
    return 0.50


def score_breakout_proximity(t: dict) -> float:
    """
    Composite of distance-to-52w-high + recent volume confirmation.
    A stock 0–3% from new highs on >1.5× volume = breakout candidate.
    """
    dist = _safe(t, "dist_52w_high")     # negative pct, e.g. -2.0 = 2% below
    vr   = _safe(t, "volume_ratio")
    if dist is None and vr is None:
        return 0.55

    near_high = 0.55
    if dist is not None:
        if   dist >= -1:   near_high = 1.00
        elif dist >= -3:   near_high = 0.92
        elif dist >= -7:   near_high = 0.78
        elif dist >= -15:  near_high = 0.55
        elif dist >= -30:  near_high = 0.35
        else:              near_high = 0.18

    vol_boost = 0.55
    if vr is not None:
        if   vr >= 3.0:  vol_boost = 1.00
        elif vr >= 2.0:  vol_boost = 0.85
        elif vr >= 1.5:  vol_boost = 0.72
        elif vr >= 1.2:  vol_boost = 0.58
        else:            vol_boost = 0.40

    # Breakout = need both: near-high AND volume. Weight 60/40 toward proximity.
    return _clamp(0.6 * near_high + 0.4 * vol_boost)


# Lightweight news-sentiment lexicon. Tuned for financial news headlines
# rather than general-purpose sentiment — picks up the verbs that move stocks.
_BULL_WORDS = {
    "beats", "beat", "surges", "soars", "rallies", "jumps", "gains",
    "upgrade", "upgraded", "raises", "raised", "strong", "record",
    "tops", "outperform", "buy", "bullish", "expands", "wins",
    "approval", "approved", "launches", "breakthrough", "milestone",
    "guidance raised", "raises guidance", "exceeds", "boost", "boosts",
    "deal", "partnership", "contract", "acquires", "ai", "growth",
    "all-time high", "52-week high",
}
_BEAR_WORDS = {
    "miss", "misses", "plunges", "tumbles", "falls", "drops", "slides",
    "downgrade", "downgraded", "cuts", "weak", "warns", "warning",
    "concern", "concerns", "lawsuit", "investigation", "probe",
    "delay", "delays", "halts", "recall", "loss", "losses",
    "underperform", "sell", "bearish", "fraud", "guidance cut",
    "cuts guidance", "missing", "shortfall",
}


def score_news_sentiment(t: dict) -> float:
    """
    Tally bullish vs bearish keywords across recent news headlines.
    Score 0–1 with 0.55 = neutral. Returns neutral when no news cached.
    """
    news = _safe(t, "news") or []
    if not news:
        return 0.55

    bull = bear = 0
    for n in news[:15]:
        h = (n.get("headline") or "").lower()
        if not h:
            continue
        for w in _BULL_WORDS:
            if w in h:
                bull += 1
                break  # one headline = one vote
        for w in _BEAR_WORDS:
            if w in h:
                bear += 1
                break

    total = bull + bear
    if total == 0:
        return 0.55
    ratio = bull / total
    # Map ratio → score with mild compression so a few headlines don't dominate
    if   ratio >= 0.85: return 0.95
    elif ratio >= 0.65: return 0.82
    elif ratio >= 0.50: return 0.65
    elif ratio >= 0.30: return 0.45
    elif ratio >= 0.15: return 0.30
    else:               return 0.18


def score_earnings_acceleration(t: dict) -> float:
    """
    Revenue *acceleration* across the last 4 quarters. A company growing 20%
    YoY is good. A company whose growth rate is *itself* accelerating is
    much better — that is what hyper-growth winners look like before they
    get re-rated by the market.
    """
    qi = _safe(t, "quarterly_income") or []
    if len(qi) < 4:
        return 0.55   # not enough data — neutral

    # quarterly_income is ordered most-recent-first
    revs = [q.get("revenue") for q in qi[:4] if q.get("revenue")]
    if len(revs) < 4:
        return 0.55

    # QoQ growth rates: most recent quarter vs the one 3 quarters back
    # Use sequential pairs to detect acceleration vs. deceleration
    growth_rates: list[float] = []
    for i in range(len(revs) - 1):
        cur, prev = revs[i], revs[i + 1]
        if prev and prev > 0:
            growth_rates.append((cur - prev) / prev)

    if len(growth_rates) < 2:
        return 0.55

    recent_avg = sum(growth_rates[:1]) / 1                  # most recent QoQ
    prior_avg  = sum(growth_rates[1:]) / max(len(growth_rates) - 1, 1)
    delta = recent_avg - prior_avg

    if   delta >=  0.10: return 1.00     # explosive acceleration
    elif delta >=  0.05: return 0.88
    elif delta >=  0.02: return 0.75
    elif delta >= -0.02: return 0.55     # stable
    elif delta >= -0.05: return 0.40
    elif delta >= -0.10: return 0.25
    else:                return 0.10     # decelerating fast


# ╔════════════════════════════════════════════════════════════════════════╗
# ║  2. ScoreHistory — rolling per-ticker Alpha Score memory                 ║
# ╚════════════════════════════════════════════════════════════════════════╝

class ScoreHistory:
    """
    Persists a rolling window of Alpha Scores per ticker via the existing
    SmartCache (so it survives Railway restarts). Exposes:

        record(ticker, pop_score)
        velocity(ticker)        — pop-score change per 24h (float)
        acceleration(ticker)    — change-of-change (float)
        momentum_score(ticker)  — 0..1 normalised "score is rising fast"
    """

    CACHE_KEY  = "intel:score_history"
    MAX_POINTS = 48           # ~2 days at 30-min cadence
    TTL        = 86400 * 14   # 14 days

    def __init__(self, cache):
        self.cache = cache
        self._mem: dict[str, list[tuple[float, float]]] = (
            cache.get(self.CACHE_KEY) or {}
        )

    # ── Mutation ─────────────────────────────────────────────────────
    def record_batch(self, items: list[dict]) -> None:
        now = time.time()
        changed = False
        for t in items:
            tk    = t.get("ticker")
            score = t.get("pop_score")
            if not tk or score is None:
                continue
            series = self._mem.setdefault(tk, [])
            # Only record if at least 5 minutes since last point — avoid bloat
            if not series or (now - series[-1][0]) >= 300:
                series.append((now, float(score)))
                if len(series) > self.MAX_POINTS:
                    del series[0:len(series) - self.MAX_POINTS]
                changed = True
        if changed:
            self.cache.set(self.CACHE_KEY, self._mem, self.TTL)

    # ── Query ────────────────────────────────────────────────────────
    def velocity(self, ticker: str, hours: float = 24.0) -> Optional[float]:
        """Return pop_score change over the last `hours` hours, or None."""
        series = self._mem.get(ticker, [])
        if len(series) < 2:
            return None
        now    = time.time()
        cutoff = now - hours * 3600
        # find the oldest point still inside the window
        old = next((pt for pt in series if pt[0] >= cutoff), series[0])
        return round(series[-1][1] - old[1], 2)

    def acceleration(self, ticker: str) -> Optional[float]:
        """Velocity-of-velocity: compares last-24h delta to prior-24h delta."""
        v_now  = self.velocity(ticker, hours=24)
        v_prev = self._velocity_in_window(ticker, hours_from=48, hours_to=24)
        if v_now is None or v_prev is None:
            return None
        return round(v_now - v_prev, 2)

    def _velocity_in_window(
        self, ticker: str, hours_from: float, hours_to: float
    ) -> Optional[float]:
        series = self._mem.get(ticker, [])
        if len(series) < 2:
            return None
        now  = time.time()
        a    = now - hours_from * 3600
        b    = now - hours_to   * 3600
        in_win = [pt for pt in series if a <= pt[0] <= b]
        if len(in_win) < 2:
            return None
        return round(in_win[-1][1] - in_win[0][1], 2)

    def momentum_score(self, ticker: str) -> float:
        """
        Map velocity → 0..1 score for use in the weighted scorer.
        +5 pts/24h ≈ 0.85, –5 pts/24h ≈ 0.20, no data ≈ 0.55.
        """
        v = self.velocity(ticker, hours=24)
        if v is None:
            return 0.55
        if   v >=  6:  return 1.00
        elif v >=  3:  return 0.85
        elif v >=  1:  return 0.70
        elif v >= -1:  return 0.55
        elif v >= -3:  return 0.38
        elif v >= -6:  return 0.22
        else:          return 0.10

    def to_payload(self, ticker: str) -> dict:
        """Snapshot suitable for embedding in /api/ticker payloads."""
        return {
            "score_velocity":     self.velocity(ticker),
            "score_acceleration": self.acceleration(ticker),
            "score_momentum":     round(self.momentum_score(ticker), 3),
            "history_points":     len(self._mem.get(ticker, [])),
        }


# ╔════════════════════════════════════════════════════════════════════════╗
# ║  3. MarketRegime — macro overlay                                       ║
# ╚════════════════════════════════════════════════════════════════════════╝

class MarketRegime:
    """
    Detects the prevailing market regime from a small basket of macro tickers.

        SPY  — broad market
        QQQ  — growth/tech proxy (high-beta confirmation)
        ^VIX — volatility
        ^TNX — 10-year yield (rising rates pressure long-duration assets)

    Produces:
        regime_score      0..100  (higher = friendlier to long high-beta names)
        regime_label      "Bullish" | "Mixed" | "Bearish"
        regime_multiplier 0.85..1.15 — applied to per-ticker Alpha Score
        components        dict of input readings for transparency

    Cache TTL is 30 minutes — regime doesn't whip around minute-to-minute.
    """

    CACHE_KEY = "intel:market_regime"
    TTL       = 1800   # 30 minutes
    # SPY = S&P 500 · QQQ = NASDAQ-100 · DIA = Dow Jones Industrial Average ·
    # ^VIX = volatility · ^TNX = 10-year Treasury yield (kept for regime
    # scoring but no longer surfaced in the topbar Market Pulse).
    INDICES   = ("SPY", "QQQ", "DIA", "^VIX", "^TNX")

    def __init__(self, cache):
        self.cache = cache

    def get(self) -> dict:
        cached = self.cache.get(self.CACHE_KEY)
        if cached is not None:
            return cached
        # Cold start — return a sensible neutral until first refresh
        return {
            "regime_score":      55.0,
            "regime_label":      "Mixed",
            "regime_multiplier": 1.00,
            "components":        {},
            "stale":             True,
            "ts":                None,
        }

    async def refresh(self) -> dict:
        """Fetch macro indices and recompute regime. Safe to call frequently."""
        if not _YF_AVAILABLE:
            logger.warning("yfinance not available — regime stays neutral")
            return self.get()

        try:
            comps = await asyncio.to_thread(self._fetch_components)
            if not comps:
                return self.get()

            score = self._score_components(comps)
            label = (
                "Bullish" if score >= 65 else
                "Bearish" if score <= 40 else
                "Mixed"
            )
            # Multiplier: linear map 0..100 → 0.85..1.15, clipped
            mult = round(_clamp(0.85 + (score / 100) * 0.30, 0.85, 1.15), 3)

            # Overlay LIVE session quotes onto the DISPLAYED indices so the
            # header pills agree with the Pre-Market Report cards (which use
            # fast_info). The regime SCORE above intentionally stays on the
            # stable daily-history view — only the surfaced last/pct_1d change.
            try:
                disp = [s for s in ("SPY", "QQQ", "DIA", "^VIX") if s in comps]
                live = await asyncio.to_thread(self._live_quotes, disp)
                for sym, qd in live.items():
                    if qd.get("last") is not None:
                        comps[sym]["last"] = qd["last"]
                        if qd.get("pct_1d") is not None:
                            comps[sym]["pct_1d"] = qd["pct_1d"]
            except Exception as exc:
                logger.debug(f"regime live-quote overlay failed: {exc}")

            payload = {
                "regime_score":      round(score, 1),
                "regime_label":      label,
                "regime_multiplier": mult,
                "components":        comps,
                "stale":             False,
                "ts":                datetime.now(tz=timezone.utc).isoformat(),
            }
            self.cache.set(self.CACHE_KEY, payload, self.TTL)
            return payload
        except Exception as exc:
            logger.warning(f"MarketRegime.refresh failed: {exc}")
            return self.get()

    # ── internals ────────────────────────────────────────────────────
    def _fetch_components(self) -> dict:
        """Synchronous yfinance calls — runs in a thread via asyncio.to_thread."""
        out: dict = {}
        for sym in self.INDICES:
            try:
                tk = yf.Ticker(sym)
                hist = tk.history(period="3mo", interval="1d", auto_adjust=True)
                if hist is None or hist.empty:
                    continue
                # Drop NaN closes BEFORE computing — yfinance often returns a
                # trailing NaN bar for ETFs (e.g. today's forming/pre-market bar
                # for SPY/QQQ/DIA), which otherwise made every %-change NaN→null
                # and hid the index pills while ^VIX/^TNX (index symbols) showed.
                closes = [c for c in hist["Close"].tolist() if c is not None and c == c]
                if len(closes) < 22:
                    continue
                last   = closes[-1]
                ref_1  = closes[-2]   if len(closes) >= 2  else last
                ref_5  = closes[-5]   if len(closes) >= 5  else last
                ref_22 = closes[-22]  if len(closes) >= 22 else last
                hi_3m  = max(closes)
                lo_3m  = min(closes)
                out[sym] = {
                    "last":         round(last,   2),
                    "pct_1d":       round((last/ref_1  - 1) * 100, 2),
                    "pct_5d":       round((last/ref_5  - 1) * 100, 2),
                    "pct_1m":       round((last/ref_22 - 1) * 100, 2),
                    "dist_3m_high": round((last/hi_3m  - 1) * 100, 2),
                    "dist_3m_low":  round((last/lo_3m  - 1) * 100, 2),
                }
            except Exception as exc:
                logger.debug(f"regime fetch {sym} failed: {exc}")
        return out

    @staticmethod
    def _fi_val(fi, *keys):
        for k in keys:
            try:
                v = fi[k]
            except Exception:
                v = getattr(fi, k, None)
            if v is not None and isinstance(v, (int, float)) and not math.isnan(v):
                return float(v)
        return None

    def _live_quotes(self, syms: list) -> dict:
        """Live session quote (last + 1d% vs previous close) per symbol via
        fast_info — the SAME source the Pre-Market Report uses. This picks up
        pre-/post-market prices, which daily history bars do not. Used to
        overlay the *displayed* pill values so they match the report.
        Blocking — call via asyncio.to_thread."""
        out: dict = {}
        from concurrent.futures import ThreadPoolExecutor
        def q(sym):
            try:
                fi = yf.Ticker(sym).fast_info
                last = self._fi_val(fi, "last_price", "lastPrice")
                prev = self._fi_val(fi, "previous_close", "previousClose")
                pct  = round((last / prev - 1) * 100, 2) if (last and prev) else None
                return sym, (round(last, 2) if last is not None else None), pct
            except Exception as exc:
                logger.debug(f"regime live quote {sym} failed: {exc}")
                return sym, None, None
        with ThreadPoolExecutor(max_workers=6) as ex:
            for sym, last, pct in ex.map(q, syms):
                out[sym] = {"last": last, "pct_1d": pct}
        return out

    def _score_components(self, c: dict) -> float:
        """Combine SPY/QQQ trend + VIX level + TNX direction → 0..100."""
        score = 50.0   # start neutral

        spy = c.get("SPY") or {}
        qqq = c.get("QQQ") or {}
        vix = c.get("^VIX") or {}
        tnx = c.get("^TNX") or {}

        # SPY trend (broad market): each +1% over 1m = +1.5 pts (cap ±15)
        if spy.get("pct_1m") is not None:
            score += _clamp_signed(spy["pct_1m"] * 1.5, 15)
        # QQQ trend (growth confirmation): each +1% over 1m = +1.0 pts (cap ±12)
        if qqq.get("pct_1m") is not None:
            score += _clamp_signed(qqq["pct_1m"] * 1.0, 12)
        # 5d momentum confirmation
        if spy.get("pct_5d") is not None:
            score += _clamp_signed(spy["pct_5d"] * 1.5, 8)

        # VIX: level matters — <14 calm, 14-20 normal, 20-30 nervous, >30 fear
        v = vix.get("last")
        if v is not None:
            if   v < 13:   score += 8
            elif v < 17:   score += 4
            elif v < 22:   score += 0
            elif v < 28:   score -= 6
            elif v < 35:   score -= 12
            else:          score -= 18

        # 10Y yield direction — rising yields hurt growth multiples
        ty = tnx.get("pct_1m")
        if ty is not None:
            if   ty >  10: score -= 6     # yields up >10% in a month is hot
            elif ty >   3: score -= 3
            elif ty < -10: score += 5
            elif ty <  -3: score += 2

        # Distance from 3m high — broad market near highs = risk-on
        if spy.get("dist_3m_high") is not None:
            d = spy["dist_3m_high"]   # negative pct
            if   d >= -1:  score += 5
            elif d >= -3:  score += 2
            elif d <= -8:  score -= 4

        return _clamp(score, 0, 100)


def _clamp_signed(val: float, cap: float) -> float:
    return max(-cap, min(cap, val))


# ╔════════════════════════════════════════════════════════════════════════╗
# ║  3b. MarketAnalysis — US pre/post-market snapshot for the dashboard     ║
# ╚════════════════════════════════════════════════════════════════════════╝

def _ema(values: list[float], period: int) -> Optional[float]:
    """Exponential moving average of the last value in `values`."""
    if not values or len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def _rsi(closes: list[float], period: int = 14) -> Optional[float]:
    """Wilder's RSI on a list of closing prices."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def _macd(closes: list[float]) -> dict:
    """MACD(12,26,9). Returns macd, signal, hist (or None values)."""
    if len(closes) < 35:
        return {"macd": None, "signal": None, "hist": None}
    # Build a short MACD-line series so the 9-EMA signal has something to chew on.
    macd_series = []
    for i in range(26, len(closes) + 1):
        window = closes[:i]
        e12, e26 = _ema(window, 12), _ema(window, 26)
        if e12 is None or e26 is None:
            continue
        macd_series.append(e12 - e26)
    if not macd_series:
        return {"macd": None, "signal": None, "hist": None}
    macd_line = macd_series[-1]
    signal = _ema(macd_series, 9) if len(macd_series) >= 9 else macd_series[-1]
    return {
        "macd":   round(macd_line, 2),
        "signal": round(signal, 2) if signal is not None else None,
        "hist":   round(macd_line - signal, 2) if signal is not None else None,
    }


class MarketAnalysis:
    """
    Builds a US pre/post-market 'Market Analysis' snapshot from a basket of
    macro tickers via yfinance. Powers the dashboard's Market Analysis panel:
    index ETFs (S&P/Nasdaq/Dow/Russell), equity futures (pre-market gap),
    rates/VIX/dollar, commodities, sector-ETF rotation, and SPY technicals.

    Cached ~5 min — pre/post-market readings drift but don't whip second to
    second, and yfinance is rate-limited.
    """

    CACHE_KEY = "intel:market_analysis"
    # 30 min. Was 5 min, which — combined with the 10-min desk publisher loop —
    # regenerated the Haiku AI narrative every cycle all day (a major cost bleed,
    # ~Jun 18 onward). Pre/post-market macro readings drift slowly; the AI prose
    # does NOT need to rewrite every 5 minutes. Live stock movers are layered on
    # separately by the 30-second quote loop, so freshness is unaffected.
    TTL       = 1800   # 30 minutes

    INDEX_ETFS = {
        "SPY": "S&P 500",
        "QQQ": "Nasdaq 100",
        "DIA": "Dow Jones",
        "IWM": "Russell 2000",
    }
    FUTURES = {
        "ES=F": "S&P 500 Futures",
        "NQ=F": "Nasdaq 100 Futures",
        "YM=F": "Dow Futures",
    }
    # symbol -> (label, unit, decimals)
    RATES_FX = {
        "^VIX":     ("Volatility (VIX)", "",   2),
        "^TNX":     ("US 10Y Yield",     "%",  2),
        "DX-Y.NYB": ("Dollar Index",     "",   2),
    }
    COMMODITIES = {
        "CL=F": ("WTI Crude",   "$/bbl", 2),
        "BZ=F": ("Brent Crude", "$/bbl", 2),
        "GC=F": ("Gold",        "$/oz",  1),
        "SI=F": ("Silver",      "$/oz",  2),
    }
    SECTORS = {
        "XLK":  "Technology",
        "XLF":  "Financials",
        "XLE":  "Energy",
        "XLV":  "Health Care",
        "XLY":  "Consumer Disc.",
        "XLP":  "Consumer Staples",
        "XLI":  "Industrials",
        "XLB":  "Materials",
        "XLRE": "Real Estate",
        "XLU":  "Utilities",
        "XLC":  "Communication",
    }

    def __init__(self, cache):
        self.cache = cache

    def get(self) -> Optional[dict]:
        return self.cache.get(self.CACHE_KEY)

    async def refresh(self, allow_ai: bool = True) -> dict:
        """Fetch the macro basket and rebuild the snapshot. Cached on success.
        allow_ai=False (caller passes the AI cost breaker state) skips the paid
        Haiku narrative — the data is still refreshed and cached, the frontend
        renders its deterministic fallback prose."""
        if not _YF_AVAILABLE:
            return {"available": False, "reason": "Market data feed unavailable.",
                    "ts": datetime.now(tz=timezone.utc).isoformat()}
        try:
            payload = await asyncio.to_thread(self._build)
            if payload and payload.get("available"):
                # AI narrative — Claude writes the report from the live numbers.
                # Best-effort: on no-key/timeout/parse-failure (or over budget) we
                # cache the data alone and the frontend renders its fallback prose.
                payload["ai"] = await self._ai_narrative(payload) if allow_ai else None
                self.cache.set(self.CACHE_KEY, payload, self.TTL)
                return payload
        except Exception as exc:
            logger.warning(f"MarketAnalysis.refresh failed: {exc}")
        return self.get() or {"available": False,
                              "reason": "Could not fetch market data right now.",
                              "ts": datetime.now(tz=timezone.utc).isoformat()}

    # ── session clock (US/Eastern) ───────────────────────────────────────
    def _session(self) -> dict:
        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            now = datetime.now(timezone.utc)
        wd = now.weekday()            # 0=Mon .. 6=Sun
        mins = now.hour * 60 + now.minute
        if wd >= 5:
            phase, label = "closed", "Weekend — markets closed"
        elif mins < 4 * 60:
            phase, label = "closed", "Overnight — markets closed"
        elif mins < 9 * 60 + 30:
            phase, label = "pre",  "Pre-market"
        elif mins < 16 * 60:
            phase, label = "live", "Regular session — live"
        elif mins < 20 * 60:
            phase, label = "post", "After-hours"
        else:
            phase, label = "closed", "Overnight — markets closed"
        return {"phase": phase, "label": label,
                "et_time": now.strftime("%H:%M ET · %d %b %Y")}

    # ── quote helpers ────────────────────────────────────────────────────
    @staticmethod
    def _fi_val(fi, *keys):
        for k in keys:
            try:
                v = fi[k]
            except Exception:
                v = getattr(fi, k, None)
            if v is not None and isinstance(v, (int, float)) and not math.isnan(v):
                return float(v)
        return None

    def _quote(self, sym: str) -> dict:
        """last / previous_close / % change for one symbol via fast_info."""
        try:
            fi = yf.Ticker(sym).fast_info
            last = self._fi_val(fi, "last_price", "lastPrice")
            prev = self._fi_val(fi, "previous_close", "previousClose")
            if last is None or prev is None or prev == 0:
                return {"last": last, "prev": prev, "chg_abs": None, "chg_pct": None}
            return {
                "last":    round(last, 2),
                "prev":    round(prev, 2),
                "chg_abs": round(last - prev, 2),
                "chg_pct": round((last / prev - 1) * 100, 2),
            }
        except Exception as exc:
            logger.debug(f"market-analysis quote {sym} failed: {exc}")
            return {"last": None, "prev": None, "chg_abs": None, "chg_pct": None}

    def _quotes(self, syms: list[str]) -> dict:
        from concurrent.futures import ThreadPoolExecutor
        out: dict = {}
        with ThreadPoolExecutor(max_workers=10) as ex:
            for sym, res in zip(syms, ex.map(self._quote, syms)):
                out[sym] = res
        return out

    def _spy_technicals(self) -> dict:
        try:
            hist = yf.Ticker("SPY").history(period="1y", interval="1d", auto_adjust=True)
            if hist is None or hist.empty:
                return {}
            closes = [float(c) for c in hist["Close"].tolist() if c == c]
            if len(closes) < 60:
                return {}
            price = round(closes[-1], 2)
            def sma(n):
                return round(sum(closes[-n:]) / n, 2) if len(closes) >= n else None
            # 52-week range positioning (drawdown from the yearly high).
            hi_52w = round(max(closes), 2)
            lo_52w = round(min(closes), 2)
            dist_high = round((price / hi_52w - 1) * 100, 2) if hi_52w else None
            # Intermediate momentum — ~21 trading days back.
            ret_1m = round((price / closes[-22] - 1) * 100, 2) if len(closes) >= 22 else None
            # Annualised realised volatility from the last 20 daily returns.
            vol_20d = None
            if len(closes) >= 21:
                rets = [closes[i] / closes[i - 1] - 1 for i in range(-20, 0)]
                mean = sum(rets) / len(rets)
                var = sum((r - mean) ** 2 for r in rets) / len(rets)
                vol_20d = round((var ** 0.5) * (252 ** 0.5) * 100, 1)
            return {
                "price":  price,
                "sma20":  sma(20),
                "sma50":  sma(50),
                "sma200": sma(200),
                "rsi14":  _rsi(closes, 14),
                **_macd(closes),
                "hi_52w": hi_52w,
                "lo_52w": lo_52w,
                "dist_52w_high": dist_high,
                "ret_1m": ret_1m,
                "vol_20d": vol_20d,
            }
        except Exception as exc:
            logger.debug(f"market-analysis SPY technicals failed: {exc}")
            return {}

    def _sectors(self) -> list:
        """1d + 5d % change for the 11 SPDR sector ETFs.

        The 1-day move comes from fast_info (the same reliable path the index
        cards use) so every sector shows; the 5-day move is a best-effort
        batch history read layered on top.
        """
        quotes = self._quotes(list(self.SECTORS))   # reliable 1d via fast_info
        five: dict = {}
        try:
            df = yf.download(list(self.SECTORS), period="1mo", interval="1d",
                             auto_adjust=True, group_by="ticker",
                             threads=True, progress=False)
            for sym in self.SECTORS:
                try:
                    closes = [float(c) for c in df[sym]["Close"].tolist() if c == c]
                    if len(closes) >= 6:
                        five[sym] = round((closes[-1] / closes[-6] - 1) * 100, 2)
                except Exception:
                    continue
        except Exception as exc:
            logger.debug(f"market-analysis sector 5d download failed: {exc}")

        out = []
        for sym, name in self.SECTORS.items():
            q = quotes.get(sym, {})
            if q.get("chg_pct") is None:
                continue
            out.append({"symbol": sym, "name": name,
                        "chg_1d": q["chg_pct"], "chg_5d": five.get(sym)})
        out.sort(key=lambda s: s["chg_1d"], reverse=True)
        return out

    def _build(self) -> dict:
        session = self._session()
        indices = self._quotes(list(self.INDEX_ETFS))
        futures = self._quotes(list(self.FUTURES))
        macro   = self._quotes(list(self.RATES_FX) + list(self.COMMODITIES))
        tech    = self._spy_technicals()
        sectors = self._sectors()

        def pack(d: dict, meta: dict):
            rows = []
            for sym, m in meta.items():
                q = d.get(sym, {})
                if isinstance(m, tuple):
                    label, unit, dec = m
                else:
                    label, unit, dec = m, "", 2
                rows.append({"symbol": sym, "label": label, "unit": unit,
                             "decimals": dec, **q})
            return rows

        # Pre-market gap read off the S&P futures (or SPY pre-market quote).
        gap_pct = None
        es = futures.get("ES=F", {})
        if es.get("chg_pct") is not None:
            gap_pct = es["chg_pct"]
        elif indices.get("SPY", {}).get("chg_pct") is not None:
            gap_pct = indices["SPY"]["chg_pct"]

        return {
            "available":   True,
            "session":     session,
            "gap_pct":     gap_pct,
            "indices":     pack(indices, self.INDEX_ETFS),
            "futures":     pack(futures, self.FUTURES),
            "rates_fx":    pack(macro, self.RATES_FX),
            "commodities": pack(macro, self.COMMODITIES),
            "sectors":     sectors,
            "technicals":  tech,
            "source":      "Yahoo Finance (finance.yahoo.com)",
            "ts":          datetime.now(tz=timezone.utc).isoformat(),
        }

    # ── AI narrative — Claude writes the report from the live numbers ─────
    async def ai_narrative(self, data: dict, kind: Optional[str] = None,
                           model: Optional[str] = None) -> Optional[dict]:
        """Public entry: generate a (optionally pre/post-framed) AI narrative.
        `model` overrides the default (the daily editorial passes the sharper
        editorial model; the intraday refresh uses the cheap default)."""
        return await self._ai_narrative(data, kind, model)

    async def _ai_narrative(self, data: dict, kind: Optional[str] = None,
                            model: Optional[str] = None) -> Optional[dict]:
        """Ask Claude to turn the fetched market data into a plain-English
        briefing. The numbers are passed as ground truth — the model only
        writes prose, never invents figures. `kind` ('pre'/'post') frames the
        report. Returns a structured dict or None (no key / disabled / timeout /
        parse error → frontend uses its own deterministic fallback copy)."""
        if not _ANTHROPIC_KEY or not _HTTPX_AVAILABLE:
            return None
        use_model = model or _ANTHROPIC_MODEL
        try:
            prompt = self._build_ai_prompt(data, kind)
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key":         _ANTHROPIC_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type":      "application/json",
                    },
                    json={
                        "model":      use_model,
                        "max_tokens": 1500,   # once/day now — room for the editorial bullets
                        "messages":   [{"role": "user", "content": prompt}],
                    },
                )
                r.raise_for_status()
                payload = r.json()
            try:
                import usage_log
                usage_log.record("desk", use_model, payload.get("usage"))
            except Exception:
                pass
            text = "".join(
                b.get("text", "") for b in payload.get("content", [])
                if b.get("type") == "text"
            ).strip()
            if not text:
                return None
            # Tolerate ```json fences / leading prose — grab the JSON object.
            i, j = text.find("{"), text.rfind("}")
            if i < 0 or j <= i:
                return None
            import json as _json
            obj = _json.loads(text[i:j + 1])
            obj["generated"] = True
            obj["model"] = use_model
            return obj
        except Exception as exc:
            logger.warning(f"market-analysis AI narrative failed: {exc}")
            return None

    def _build_ai_prompt(self, d: dict, kind: Optional[str] = None) -> str:
        ses = d.get("session", {})

        def line(rows):
            out = []
            for r in rows or []:
                if r.get("last") is None:
                    continue
                chg = r.get("chg_pct")
                out.append(f"{r['label']} ({r['symbol']}): {r['last']}"
                           + (f", {chg:+.2f}%" if chg is not None else ""))
            return "; ".join(out) or "n/a"

        def stk(rows):
            out = []
            for s in (rows or [])[:6]:
                chg = s.get("change_pct")
                out.append(f"{s.get('ticker')} {chg:+.1f}%" if chg is not None else f"{s.get('ticker')}")
            return ", ".join(out) or "n/a"

        t = d.get("technicals", {}) or {}
        tech_line = "n/a"
        if t.get("price") is not None:
            tech_line = (f"SPY {t.get('price')}, 20DMA {t.get('sma20')}, "
                         f"50DMA {t.get('sma50')}, 200DMA {t.get('sma200')}, "
                         f"RSI14 {t.get('rsi14')}, MACD hist {t.get('hist')}")
        secs = d.get("sectors", []) or []
        sec_line = "; ".join(
            f"{s['name']} {s['chg_1d']:+.2f}%" for s in secs[:11]
        ) or "n/a"
        st = d.get("stocks", {}) or {}
        evs = d.get("events", []) or []
        ev_line = "; ".join(
            f"{e.get('event')} ({e.get('country')}, {e.get('when')}, {e.get('impact')})"
            for e in evs[:8]
        ) or "n/a"

        # ── House lens — our proprietary engine (this is what makes it editorial) ──
        reg = d.get("regime") or {}
        rc = reg.get("components") or {}
        def _tf(sym, nm):
            c = rc.get(sym) or {}
            if not c:
                return ""
            return (f"{nm} 1d {c.get('pct_1d')}% / 5d {c.get('pct_5d')}% / "
                    f"1m {c.get('pct_1m')}% (dist 3m-high {c.get('dist_3m_high')}%)")
        regime_line = (f"label '{reg.get('regime_label')}', score "
                       f"{reg.get('regime_score')}/100 (50=neutral, higher=risk-on)"
                       if reg else "n/a")
        regime_tf = " | ".join(x for x in (_tf('SPY', 'S&P'), _tf('QQQ', 'Nasdaq'),
                                           _tf('^VIX', 'VIX'), _tf('^TNX', '10-yr')) if x) or "n/a"
        top_rated_line = stk((st.get('top_score') or [])[:6])
        house = d.get("house") or {}
        tr = house.get("track") or {}
        track_line = (f"{tr.get('n')} closed model trades — hit rate {tr.get('hit_rate')}%, "
                      f"avg move {tr.get('avg')}%, avg winner {tr.get('avg_win')}%"
                      if tr.get('n') else "track record still building")
        book_open = (house.get("book") or {}).get("open")

        if kind == "pre":
            frame = ("This is a PRE-MARKET report, written before the 9:30 ET open. "
                     "Frame everything around the expected open and the day ahead: "
                     "the futures gap, overnight moves, and stocks with earnings due today.")
            verdict_head = ('one sentence on the expected open (gap up/down/flat) '
                            'and the overall bias (bullish/bearish/neutral) for the day.')
        elif kind == "post":
            frame = ("This is a POST-MARKET report, written after the 4:00 ET close. "
                     "Frame everything around how the day closed and the setup for "
                     "tomorrow: today's biggest movers, who just reported earnings, "
                     "and where the tape finished.")
            verdict_head = ('one sentence on how the day closed and the overall bias '
                            '(bullish/bearish/neutral) heading into tomorrow.')
        else:
            frame = ("This is a US market briefing for the current session.")
            verdict_head = ('one sentence on the expected open and overall bias '
                            '(bullish/bearish/neutral).')

        return (
            "You are a US equity market analyst writing a short, plain-English "
            "briefing for retail investors. Use ONLY the data below as ground "
            "truth — never invent or alter any number. Keep every sentence short "
            "and simple. No jargon without a plain gloss. IMPORTANT: this is "
            "generic market context for a UK audience, NOT financial advice and "
            "NOT a personal recommendation. Do NOT give buy/sell/hold advice, "
            "never use 'buy'/'sell' as directives, never say 'you should', do NOT "
            "give price targets, and use no hype ('will soar', 'guaranteed') or "
            "promised returns — this is market context only.\n"
            "EDITORIAL RULE (what makes this a 10/10 analyst editorial, not a wrap): "
            "do not just state WHAT moved — explain WHY it moved and WHAT IT IMPLIES "
            "next. Every section should carry an interpretive read, not a number "
            "readout. Connect the dots: tie the index move to the sector rotation, "
            "the VIX to positioning, the regime score to the stance. A reader should "
            "come away with a point of view they could agree or disagree with.\n"
            "ACCURACY RULE (critical): every claim must match the data EXACTLY. "
            "Before writing any magnitude (e.g. 'down more than 1%') or grouping "
            "several names together, verify each named item INDIVIDUALLY meets it "
            "against the figures below — do not lump a -0.6% name into a '>1% move'. "
            "Prefer stating the exact percentage for each name over rounded "
            "thresholds. If unsure, describe direction only, never a wrong size.\n\n"
            f"{frame}\n\n"
            f"Session: {ses.get('label')} ({ses.get('et_time')})\n"
            f"S&P futures gap: {d.get('gap_pct')}%\n"
            f"Index ETFs: {line(d.get('indices'))}\n"
            f"Futures: {line(d.get('futures'))}\n"
            f"Rates/VIX/Dollar: {line(d.get('rates_fx'))}\n"
            f"Commodities: {line(d.get('commodities'))}\n"
            f"Technicals: {tech_line}\n"
            f"Sector moves today: {sec_line}\n"
            f"Top gainers: {stk(st.get('gainers'))}\n"
            f"Top losers: {stk(st.get('losers'))}\n"
            f"Earnings on deck: {stk(st.get('earnings'))}\n"
            f"Just reported: {stk(st.get('just_reported'))}\n"
            f"Upcoming macro events: {ev_line}\n"
            "--- OUR PROPRIETARY ENGINE (write the editorial THROUGH this lens) ---\n"
            f"OUR REGIME MODEL ('Big Picture' market gauge): {regime_line}.\n"
            f"  Multi-timeframe trend: {regime_tf}.\n"
            f"OUR HIGHEST ALPHA-SCORE NAMES today: {top_rated_line}\n"
            f"OUR MODEL BOOK: {book_open if book_open is not None else 'n/a'} open positions; "
            f"track record — {track_line}.\n\n"
            "You are the TickerMover markets editor writing the desk's signed daily "
            "editorial — to the standard a Zacks / IBD / Morningstar analyst would "
            "publish. Voice: sharp, plain-English, data-driven, with a clear point "
            "of view. THE EDGE: anchor the read in OUR regime gauge (state it as a "
            "Big-Picture market-direction call, like IBD's market pulse) and OUR "
            "Alpha-Score lens (what today did to our top-rated names; what it means "
            "for our book, citing the track record). That house view is what makes "
            "this OURS, not a generic wrap. Every claim ties to a number above; "
            "educational context only, never a buy/sell call, never invent a figure. "
            "Return ONLY a JSON object (no markdown, no commentary) with exactly "
            "these keys:\n"
            "{\n"
            '  "regime_read": "2-3 sentences: our Big-Picture market-direction call '
            'straight from the regime gauge — name the label and score, cite the '
            'multi-timeframe trend, and state the stance (risk-on / cautious / '
            'defensive) with the reason. This is our IBD-style market pulse.",\n'
            '  "house_view": "2-3 sentences through our Alpha-Score lens: how today '
            'treated our highest-rated names, and the read for our model book — '
            'reference the track record. Observational, never a trade call.",\n'
            '  "headline": "One punchy editorial headline (<= 12 words) that states '
            'a THESIS — the meaning of the day, not the tape. It must make an '
            'interpretive claim a reader could agree or disagree with. Do NOT lead '
            'with an index name + a percentage (e.g. NOT \'Nasdaq Slumps 3.06% as '
            'VIX Spikes\'); instead name the FORCE and its implication (e.g. '
            '\'Earnings Cracks Surface Just as the Fed Window Narrows\'). No number-'
            'stuffing; at most one figure, only if it IS the story.",\n'
            '  "dek": "One sentence standfirst that sharpens the thesis and hints at '
            'the stakes into the next session — not a restatement of the headline.",\n'
            '  "executive_summary": ["4 to 5 crisp, self-contained bullets — the '
            'key takeaways a busy reader needs. Each bullet leads with the hard '
            'number (e.g. \'Nasdaq -3.1%: growth led the selloff\'). No filler."],\n'
            '  "brief": "3-4 short sentences on the session, the futures gap, the '
            'broad-market move, the VIX mood, and one notable stock move.",\n'
            '  "technicals_note": "1-2 short sentences on where SPY sits vs its '
            'moving averages and what RSI/MACD momentum says.",\n'
            '  "sectors_note": "1 short sentence on which sectors lead/lag and '
            'what that rotation suggests.",\n'
            '  "movers_note": "1 short sentence on the standout stock movers.",\n'
            '  "watching": "1 short sentence on the key macro event(s) the market '
            'is waiting on for direction, by name and day. If none, say markets '
            'have a light data calendar.",\n'
            '  "tomorrow": "2-3 sentences on the set-up into the next session: what '
            'futures imply, the key level/catalyst, and what would change the read.",\n'
            '  "verdict": {\n'
            f'    "headline": "{verdict_head}",\n'
            '    "what_it_means": "2-3 short lines that explain the CAUSE behind the '
            'tone (why the tape did what it did) and the second-order read for the '
            'next session — not a restatement of the move. No trade calls.",\n'
            '    "what_changes_view": "1 short, falsifiable line naming the specific '
            'trigger that would flip this read — a level, a catalyst, or a data '
            'print (e.g. \'A close back above the 20-day line, or a soft PCE print, '
            'would reset the tone to constructive\').",\n'
            '    "confidence": "High | Medium | Low"\n'
            "  }\n"
            "}"
        )


# ╔════════════════════════════════════════════════════════════════════════╗
# ║  4. ThesisGenerator — structured per-stock investment thesis           ║
# ╚════════════════════════════════════════════════════════════════════════╝

# Optional LLM. If ANTHROPIC_API_KEY is set, the rule-based output is sent to
# Claude with a tight prompt to upgrade the prose. Otherwise we ship the
# rule-based output directly — both are structurally identical.
_ANTHROPIC_KEY     = os.environ.get("ANTHROPIC_API_KEY", "").strip()
_ANTHROPIC_MODEL   = os.environ.get(
    "ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"
)
# The signed daily editorial is generated ONCE per trading day, so it can afford a
# sharper model than the intraday Haiku refresh. Haiku writes clean but flat; the
# editorial needs connective, opinionated prose. One call/day keeps the cost trivial.
_ANTHROPIC_EDITORIAL_MODEL = os.environ.get(
    "ANTHROPIC_EDITORIAL_MODEL", "claude-sonnet-5"
)
_ANTHROPIC_TIMEOUT = 12.0


class ThesisGenerator:
    """
    Build a structured investment thesis from a fully-enriched ticker dict.

    Output schema:
        {
            "ticker":            str,
            "recommendation":    "Strong Outperform" | "Outperform" | "Neutral" | "Lagging" | "Avoid",
            "conviction":        "High" | "Medium" | "Low",
            "headline":          short one-liner,
            "explanation":       2-3 sentences in plain English,
            "bull_case":         [3 bullet strings],
            "bear_case":         [3 bullet strings],
            "trade_plan":        {entry, stop, target, r_multiple, position_pct},
            "regime_context":    one sentence about how regime affects this name,
            "source":            "rule-based" | "llm"
        }
    """

    def __init__(self, regime: MarketRegime, history: ScoreHistory):
        self.regime  = regime
        self.history = history

    # ── Public entry point ───────────────────────────────────────────
    async def build(self, ticker_data: dict, allow_llm: bool = True) -> dict:
        # allow_llm=False (e.g. the AI cost breaker is tripped) serves the
        # deterministic rule-based thesis with no paid model call.
        rule_based = self._build_rule_based(ticker_data)
        if not allow_llm or not _ANTHROPIC_KEY or not _HTTPX_AVAILABLE:
            return rule_based
        try:
            upgraded = await self._llm_upgrade(rule_based, ticker_data)
            return upgraded or rule_based
        except Exception as exc:
            logger.warning(f"LLM thesis upgrade failed for "
                           f"{ticker_data.get('ticker','?')}: {exc}")
            return rule_based

    # ── Rule-based engine ────────────────────────────────────────────
    def _build_rule_based(self, t: dict) -> dict:
        tk     = t.get("ticker", "?")
        name   = t.get("name") or tk
        sector = t.get("sector") or t.get("sub_sector") or "this sector"
        pop    = float(t.get("pop_score") or 0)
        conf   = float(t.get("confidence") or 0)
        grade  = t.get("grade", "C")

        regime = self.regime.get()
        velocity = self.history.velocity(tk)

        bull = self._bull_points(t, regime)
        bear = self._bear_points(t, regime)

        # Recommendation: blend grade, regime, velocity
        rec = self._recommendation(grade, pop, regime, velocity)
        conv = (
            "High"   if conf >= 0.80 and pop >= 70 else
            "Medium" if conf >= 0.60 else
            "Low"
        )

        headline = self._headline(t, rec, regime)
        explain  = self._explanation(t, rec, regime, velocity, conv)
        plan     = self._trade_plan(t)
        regime_ctx = self._regime_context(t, regime)
        why = self._why_bullets(t, rec, regime, velocity, bull, bear)

        return {
            "ticker":         tk,
            "name":           name,
            "sector":         sector,
            "recommendation": rec,
            "conviction":     conv,
            "headline":       headline,
            "explanation":    explain,
            "why_bullets":    why,             # NEW: 3 plain-English "why this stock?" bullets
            "bull_case":      bull,            # legacy detail (kept for power users)
            "bear_case":      bear,            # legacy detail (kept for power users)
            "trade_plan":     plan,            # legacy detail (kept for power users)
            "regime_context": regime_ctx,
            "source":         "rule-based",
            "generated_at":   datetime.now(tz=timezone.utc).isoformat(),
        }

    # ── helpers: simple 3-bullet "Why this stock?" answer ────────────
    def _why_bullets(
        self,
        t: dict,
        rec: str,
        regime: dict,
        vel: Optional[float],
        bull: list[str],
        bear: list[str],
    ) -> list[str]:
        """
        Pick the 3 most useful plain-English bullets that answer
        "why is this a Buy/Hold/Avoid right now?". Blends:
          - the strongest bull or bear point (depending on the recommendation)
          - the regime context in one sentence
          - score velocity if it's saying something interesting
        Always returns at most 3 lines.
        """
        out: list[str] = []
        rec_pos = rec in ("Strong Outperform", "Outperform")
        rec_neg = rec in ("Lagging", "Avoid")

        # 1. Top pick from bull or bear, depending on recommendation direction
        if rec_pos and bull:
            out.append(bull[0])
        elif rec_neg and bear:
            out.append(bear[0])
        elif bull:
            out.append(bull[0])
        elif bear:
            out.append(bear[0])

        # 2. One-line market-context sentence
        rl = regime.get("regime_label", "Mixed")
        if rl == "Bullish":
            out.append("Broader market is Bullish — favourable backdrop for new entries.")
        elif rl == "Bearish":
            out.append("Broader market is Bearish — expect bigger swings on entries.")
        else:
            out.append("Broader market is Mixed — no major macro tailwind or headwind.")

        # 3. Velocity / second-best bull or bear, whichever is more useful
        if vel is not None and abs(vel) >= 2:
            arrow = "climbing" if vel > 0 else "falling"
            out.append(f"Alpha Score is {arrow} ({vel:+.1f} pts in the last 24h).")
        elif rec_pos and len(bull) > 1:
            out.append(bull[1])
        elif rec_neg and len(bear) > 1:
            out.append(bear[1])
        elif bear:
            out.append(f"Risk to watch: {bear[0]}")

        return _dedupe(out)[:3]

    # ── helpers: thesis inputs ───────────────────────────────────────
    def _bull_points(self, t: dict, regime: dict) -> list[str]:
        out: list[str] = []
        rev_g = _safe(t, "revenue_growth_yoy")
        if rev_g is not None and rev_g >= 0.20:
            out.append(f"Revenue is growing {rev_g*100:.0f}% YoY — top-quintile pace for the universe.")
        streak = _safe(t, "eps_beat_streak")
        if streak is not None and streak >= 3:
            out.append(f"EPS has beaten estimates {streak}/4 last quarters — management is consistently under-promising.")
        mom = _safe(t, "momentum_1m")
        if mom is not None and mom >= 10:
            out.append(f"+{mom:.1f}% in the last month — institutional demand is showing up in the tape.")
        sb = _safe(t, "strong_buy_pct")
        if sb is not None and sb >= 50:
            out.append(f"{sb:.0f}% of analysts rate it Strong Buy — Wall Street already on board.")
        upside = _safe(t, "target_upside_pct")
        if upside is not None and upside >= 20:
            out.append(f"Mean analyst target implies +{upside:.0f}% upside.")
        ins_b = _safe(t, "insider_buys_90d", 0)
        ins_s = _safe(t, "insider_sells_90d", 0)
        if ins_b > ins_s and ins_b >= 2:
            out.append(f"Insiders bought {ins_b}× and sold {ins_s}× over 90 days — skin in the game.")
        gmt = _safe(t, "gross_margin_trend", "")
        if gmt == "expanding":
            out.append("Gross margin is *expanding* — operating leverage building under the surface.")
        rsi = _safe(t, "rsi_14")
        if rsi is not None and 50 <= rsi <= 65:
            out.append(f"RSI {rsi:.0f} sits in the textbook momentum zone — not yet overheated.")

        if not out:
            out.append("Alpha Score components above neutral baseline — quantitative model leans constructive.")
        # Always cap at 4 points and de-dup
        return _dedupe(out)[:4]

    def _bear_points(self, t: dict, regime: dict) -> list[str]:
        out: list[str] = []
        rsi = _safe(t, "rsi_14")
        if rsi is not None and rsi > 75:
            out.append(f"RSI {rsi:.0f} is overbought — entries here often see a 5–10% reset.")
        short = _safe(t, "short_percent_float")
        if short is not None and short > 0.15:
            out.append(f"Short interest {short*100:.1f}% of float — high conviction short bets exist.")
        pe = _safe(t, "pe_ratio")
        if pe is not None and pe > 50:
            out.append(f"Trades at {pe:.0f}× earnings — multiple compression is a real risk if growth slips.")
        days = _safe(t, "days_to_earnings")
        if days is not None and 0 <= days <= 7:
            out.append(f"Reports earnings in {days}d — binary risk window.")
        margin = _safe(t, "profit_margin")
        if margin is not None and margin < 0:
            out.append(f"Net margin {margin*100:.1f}% — still unprofitable, sensitive to capital cycles.")
        if regime.get("regime_label") == "Bearish":
            out.append("Macro is Bearish — high-beta names face headwinds even with strong fundamentals.")
        elif regime.get("regime_label") == "Mixed":
            out.append("Macro is Mixed — broader market unlikely to provide a tailwind.")
        gmt = _safe(t, "gross_margin_trend", "")
        if gmt == "contracting":
            out.append("Gross margin is *contracting* — pricing power may be eroding.")
        last_beat = _safe(t, "eps_last_beat")
        if last_beat is False:
            out.append("Most recent quarter missed EPS — track record interrupted.")

        if not out:
            out.append("No major red flags detected — primary risk is broader market-driven.")
        return _dedupe(out)[:4]

    def _recommendation(self, grade: str, pop: float, regime: dict, vel: Optional[float]) -> str:
        # COMPLIANCE (B6): these are TickerMover's OWN quality ratings, so they use
        # the house research scale (Strong Outperform / Outperform / Neutral /
        # Lagging / Avoid) — NEVER buy/sell/hold language. Buy/Sell wording is
        # reserved for attributed third-party analyst ratings only. Matches the
        # grade legend shown in the UI. Not an FCA-authorised personal recommendation.
        rl = regime.get("regime_label", "Mixed")
        # Base tier from grade
        base = {
            "A": "Strong Outperform",
            "B": "Outperform",
            "C": "Neutral",
            "D": "Lagging",
            "F": "Avoid",
        }.get(grade, "Neutral")

        # Regime + velocity nudges
        if rl == "Bearish" and base == "Strong Outperform":
            return "Outperform"        # temper one notch in tough macro
        if rl == "Bullish" and base == "Outperform" and (vel or 0) >= 2:
            return "Strong Outperform" # firmer if score is also climbing
        if rl == "Bearish" and (vel or 0) <= -3 and base in ("Outperform", "Neutral"):
            return "Lagging"
        return base

    def _headline(self, t: dict, rec: str, regime: dict) -> str:
        tk   = t.get("ticker", "?")
        name = t.get("name") or tk
        rg   = regime.get("regime_label", "Mixed")
        # Pick the dominant edge to feature in the headline
        edges = []
        if (t.get("momentum_1m") or 0) >= 15:
            edges.append("strong price momentum")
        if (t.get("eps_beat_streak") or 0) >= 3:
            edges.append("consistent earnings beats")
        if (t.get("revenue_growth_yoy") or 0) >= 0.25:
            edges.append("hyper-growth top line")
        if (t.get("strong_buy_pct") or 0) >= 60:
            edges.append("analyst conviction")
        if (t.get("mention_velocity") or 0) >= 50:
            edges.append("rising social attention")
        edge_phrase = ", ".join(edges[:2]) if edges else "balanced score across multiple factors"
        return f"{rec} — {name} ({tk}) on {edge_phrase}. Macro: {rg}."

    def _explanation(self, t: dict, rec: str, regime: dict,
                     vel: Optional[float], conv: str) -> str:
        tk   = t.get("ticker", "?")
        pop  = float(t.get("pop_score") or 0)
        grade= t.get("grade", "C")
        rg   = regime.get("regime_label", "Mixed")
        rmult= regime.get("regime_multiplier", 1.0)
        smart = round(pop * rmult, 1)

        sentence_a = (
            f"{tk} earns a Alpha Score of {smart:.0f} (Grade {grade}, {conv} conviction) "
            f"in the current {rg} market."
        )
        if vel is not None:
            arrow = "rising" if vel > 0.5 else "falling" if vel < -0.5 else "steady"
            sentence_b = (
                f"The score is {arrow} ({vel:+.1f} pts in the last 24 h), which "
                f"{'reinforces' if (vel > 0 and rec in ('Strong Outperform','Outperform')) or (vel < 0 and rec in ('Lagging','Avoid')) else 'modestly conflicts with'} "
                f"the {rec} rating."
            )
        else:
            sentence_b = "Score history is still being collected — momentum-of-score will be tracked from the next refresh."

        sentence_c = self._closer(t, rec)
        return " ".join([sentence_a, sentence_b, sentence_c]).strip()

    def _closer(self, t: dict, rec: str) -> str:
        if rec in ("Strong Outperform", "Outperform"):
            return ("Any position should respect an ATR-based risk level — the model "
                    "highlights the setup; risk control is the reader's own decision.")
        if rec == "Neutral":
            return "The profile is balanced — no clear quality edge either way at the moment."
        return ("The score profile flags more risk than reward here until the "
                "fundamentals or momentum improve.")

    def _trade_plan(self, t: dict) -> dict:
        price = float(t.get("price") or 0)
        atr   = float(t.get("atr_14") or 0)
        if price <= 0 or atr <= 0:
            return {
                "entry":         None,
                "stop":          None,
                "target":        None,
                "r_multiple":    None,
                "position_pct":  None,
                "notes":         "Price or ATR unavailable — chart-based stop required."
            }
        stop   = round(price - 1.5 * atr, 2)
        target = round(price + 3.0 * atr, 2)
        # 1% of account at risk per trade → position size as a % of price
        risk_per_share = max(price - stop, 0.01)
        position_pct   = round(min((1.0 / (risk_per_share / price)) , 25.0), 1)
        return {
            "entry":         round(price, 2),
            "stop":          stop,
            "target":        target,
            "r_multiple":    2.0,    # 1.5 ATR risk → 3.0 ATR reward
            "position_pct":  position_pct,
            "notes":         "Stop = 1.5×ATR below entry. Target = 3.0×ATR above entry. Position sized for 1% account risk.",
        }

    def _regime_context(self, t: dict, regime: dict) -> str:
        rl  = regime.get("regime_label", "Mixed")
        comps = regime.get("components", {})
        vix = ((comps.get("^VIX") or {}).get("last"))
        spy_m = ((comps.get("SPY") or {}).get("pct_1m"))

        if rl == "Bullish":
            return (
                f"Market is Bullish (VIX ~{vix or '?'}, SPY {spy_m:+.1f}% over 1 month). "
                f"High-beta growth names are getting the benefit of the doubt."
            ) if vix and spy_m is not None else (
                "Market is Bullish — broad tailwind for growth names."
            )
        if rl == "Bearish":
            return (
                f"Market is Bearish (VIX ~{vix or '?'}, SPY {spy_m:+.1f}% over 1 month). "
                f"High-beta names face headwinds — expect more chop on entries."
            ) if vix and spy_m is not None else (
                "Market is Bearish — expect more chop on entries."
            )
        return "Market is Mixed — no broad tailwind or headwind to lean on."

    # ── Optional LLM refinement ──────────────────────────────────────
    async def _llm_upgrade(self, rule_based: dict, t: dict) -> Optional[dict]:
        """
        If ANTHROPIC_API_KEY is set, ask Claude to write a DEEP, macro-aware
        investment case: a 3-sentence explanation plus the case-for (tailwinds)
        and case-against (headwinds) as specific, grounded bullet points. The
        structured figures are the ground truth — the model must not invent
        numbers. Falls back silently to the rule-based output on any error.
        """
        if not _ANTHROPIC_KEY or not _HTTPX_AVAILABLE:
            return None

        prompt = self._build_llm_prompt(rule_based, t)
        async with httpx.AsyncClient(timeout=22.0) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":          _ANTHROPIC_KEY,
                    "anthropic-version":  "2023-06-01",
                    "content-type":       "application/json",
                },
                json={
                    "model":      _ANTHROPIC_MODEL,
                    "max_tokens": 1500,
                    "messages":   [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            data = r.json()
        try:
            import usage_log
            usage_log.record("thesis", _ANTHROPIC_MODEL, data.get("usage"))
        except Exception:
            pass
        text = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")
        text = text.strip()
        if not text:
            return None

        upgraded = dict(rule_based)
        parsed = _parse_json_obj(text)
        if parsed:
            expl = parsed.get("explanation")
            if isinstance(expl, str) and expl.strip():
                upgraded["explanation"] = expl.strip()
            bull = _clean_bullets(parsed.get("bull_case"))
            bear = _clean_bullets(parsed.get("bear_case"))
            if bull:
                upgraded["bull_case"] = bull
            if bear:
                upgraded["bear_case"] = bear
        else:
            # Couldn't parse JSON — treat the whole reply as the explanation
            # (preserves the legacy prose-only behaviour).
            upgraded["explanation"] = text
        upgraded["source"] = "llm"
        return upgraded

    def _build_llm_prompt(self, rule_based: dict, t: dict) -> str:
        regime = self.regime.get()
        comps  = regime.get("components", {}) or {}
        vix    = ((comps.get("^VIX") or {}).get("last"))
        spy_m  = ((comps.get("SPY") or {}).get("pct_1m"))

        def fmt(key, pattern, scale=1.0, suffix=""):
            v = _safe(t, key)
            if v is None:
                return None
            try:
                return pattern.format(float(v) * scale) + suffix
            except Exception:
                return None

        facts: list[str] = []
        facts.append(f"Name / sector: {rule_based.get('name') or t.get('ticker')} · {rule_based.get('sector', '?')}")
        facts.append(f"Alpha Score: {round(float(t.get('pop_score') or 0))}/100 (grade {t.get('grade', '?')})")
        macro = f"Market regime: {regime.get('regime_label', 'Mixed')}"
        if vix is not None:
            macro += f", VIX ~{vix}"
        if spy_m is not None:
            macro += f", S&P 500 {spy_m:+.1f}% over 1 month"
        facts.append(macro)
        for label, key, pattern, scale, suffix in [
            ("Revenue growth YoY",       "revenue_growth_yoy", "{:.0f}",  100, "%"),
            ("Net margin",               "profit_margin",      "{:.1f}",  100, "%"),
            ("P/E",                       "pe_ratio",           "{:.0f}",  1,   "x"),
            ("PEG",                       "peg_ratio",          "{:.2f}",  1,   ""),
            ("Beta",                      "beta",               "{:.2f}",  1,   ""),
            ("1-month price momentum",   "momentum_1m",        "{:+.1f}", 1,   "%"),
            ("Mean analyst upside",      "target_upside_pct",  "{:+.0f}", 1,   "%"),
            ("Strong-buy analysts",      "strong_buy_pct",     "{:.0f}",  1,   "%"),
            ("EPS beat streak (of 4)",   "eps_beat_streak",    "{:.0f}",  1,   ""),
            ("Days to next earnings",    "days_to_earnings",   "{:.0f}",  1,   ""),
            ("RSI (14)",                  "rsi_14",             "{:.0f}",  1,   ""),
            ("Short interest",           "short_percent_float","{:.1f}",  100, "% of float"),
        ]:
            s = fmt(key, pattern, scale, suffix)
            if s is not None:
                facts.append(f"{label}: {s}")
        gmt = _safe(t, "gross_margin_trend", "")
        if gmt:
            facts.append(f"Gross-margin trend: {gmt}")
        facts_block = "\n".join("- " + x for x in facts if x)

        return (
            "You are a senior equity research analyst writing an objective research "
            "note on THIS stock. This is generic research for a UK audience, NOT "
            "financial advice and NOT a personal recommendation. Assess how the "
            "investment case stands on the evidence — do NOT tell the reader to buy, "
            "sell, hold or trade, never use those words as directives, and never say "
            "'you should'. Frame OUR stance as opinion (our view is bullish / "
            "cautious). Do NOT guarantee returns or use hype ('will soar', "
            "'guaranteed'); a third-party analyst rating may be cited only when "
            "clearly attributed.\n\n"
            "Ground truth — use ONLY these figures; do NOT invent any numbers:\n"
            f"{facts_block}\n"
            f"- House quality rating: {rule_based['recommendation']} "
            f"({rule_based['conviction']} conviction)\n"
            f"- Bull seeds: {' | '.join(rule_based['bull_case'])}\n"
            f"- Bear seeds: {' | '.join(rule_based['bear_case'])}\n\n"
            "Produce:\n"
            "1) explanation — 3 tight sentences: the thesis, the evidence balance, and "
            "what would change it. Observational, not directive.\n"
            "2) bull_case — 4 to 6 bullets on the tailwinds / what is working. "
            "Each bullet ties a fact to an investment implication; AT LEAST ONE bullet must "
            "address the macro / economic backdrop (rates, growth cycle, market regime, or "
            "sector cycle).\n"
            "3) bear_case — 4 to 6 bullets on the headwinds / what could go wrong; "
            "AT LEAST ONE bullet must address a macro / economic risk.\n\n"
            "Rules: one sentence per bullet, plain English, no markdown or bullet characters, "
            "no fabricated numbers, max ~24 words each. Describe the setup; never instruct "
            "the reader to act, and never write buy/sell/hold or a price target.\n\n"
            'Return ONLY a JSON object of this exact shape:\n'
            '{"explanation": "...", "bull_case": ["...", "..."], "bear_case": ["...", "..."]}'
        )


# ╔════════════════════════════════════════════════════════════════════════╗
# ║  Utilities                                                             ║
# ╚════════════════════════════════════════════════════════════════════════╝

def _dedupe(seq: list[str]) -> list[str]:
    seen, out = set(), []
    for s in seq:
        k = s.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(s)
    return out


def _parse_json_obj(text: str) -> Optional[dict]:
    """Best-effort extraction of a JSON object from an LLM reply (tolerates
    ```json fences and surrounding prose). Returns None if nothing parses."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s[:4].lower() == "json":
            s = s[4:]
    i, j = s.find("{"), s.rfind("}")
    candidate = s[i:j + 1] if (i != -1 and j != -1 and j > i) else s
    try:
        obj = json.loads(candidate)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _clean_bullets(v: Any, cap: int = 6, maxlen: int = 220) -> list[str]:
    """Normalise an LLM bullet array: strip leading markers, cap count/length,
    drop non-strings and duplicates."""
    if not isinstance(v, list):
        return []
    out: list[str] = []
    for x in v:
        if not isinstance(x, str):
            continue
        s = x.strip().lstrip("•-–*").strip()
        if s:
            out.append(s[:maxlen])
    return _dedupe(out)[:cap]


def apply_regime_to_universe(universe: list[dict], regime: dict) -> None:
    """
    Mutates each ticker dict in place: adds smart_score (regime-adjusted Alpha Score)
    plus the regime fields so the dashboard can display them inline.
    """
    mult  = float(regime.get("regime_multiplier") or 1.0)
    label = regime.get("regime_label", "Mixed")
    rscore= regime.get("regime_score")
    for t in universe:
        pop = float(t.get("pop_score") or 0)
        t["smart_score"]       = round(pop * mult, 1)
        t["regime_multiplier"] = mult
        t["regime_label"]      = label
        if rscore is not None:
            t["regime_score"]  = rscore


def attach_score_history(universe: list[dict], history: ScoreHistory) -> None:
    """Adds score_velocity / score_acceleration / score_momentum to each ticker."""
    for t in universe:
        tk = t.get("ticker")
        if not tk:
            continue
        t.update(history.to_payload(tk))


__all__ = [
    "MarketRegime",
    "ScoreHistory",
    "ThesisGenerator",
    "score_trend_strength",
    "score_breakout_proximity",
    "score_news_sentiment",
    "score_earnings_acceleration",
    "apply_regime_to_universe",
    "attach_score_history",
]
