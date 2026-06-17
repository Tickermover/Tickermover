"""
TickerMover — DataCoordinator
Orchestrates data sources with smart caching and rate limiting.
Primary: Yahoo Finance (free, no key) for candles + fundamentals.
Supplemental: Finnhub (news, recs, quotes), ApeWisdom (social), SEC-API (insider).
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import date, datetime, timedelta
from typing import Any, Optional

import httpx

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False
    logging.getLogger(__name__).warning(
        "yfinance not installed — run: pip install yfinance pandas --break-system-packages"
    )

import config
from cache import SmartCache
from alpaca_client import AlpacaClient
from polygon_client import PolygonClient

logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────

class RateLimiter:
    """Async token-bucket rate limiter — safe to create before event loop."""

    def __init__(self, calls_per_minute: int):
        self._interval = 60.0 / max(calls_per_minute, 1)
        self._last     = 0.0
        self._lock: Optional[asyncio.Lock] = None   # lazy — created on first use

    async def wait(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            now     = time.monotonic()
            elapsed = now - self._last
            if elapsed < self._interval:
                await asyncio.sleep(self._interval - elapsed)
            self._last = time.monotonic()


def _safe_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def _sanitize_change_pct(price: Any, prev_close: Any, change_pct: Any) -> Optional[float]:
    """Cross-check a quote's day-change % against price & prev_close.

    Provider day-change fields (Finnhub `dp`, FMP `changePercentage`) sometimes
    go stale or reflect an unhandled split, producing impossible values like
    KLAC −89.5%. price and prev_close are two independent fields, so the true
    change is (price/prev_close − 1)×100. We use that to validate:

      • price & prev_close present and the reported pct diverges from the
        computed one by > 3 points → reported field is untrustworthy, use the
        computed value.
      • even the computed move is wild (> 75%) → price OR prev_close itself is
        corrupt → drop the day-change (None) rather than publish a wrong number
        everywhere (movers, tape, stock card, scoring).
      • can't cross-check (no usable prev_close) → drop a lone reported move
        that is implausibly large (> 60%); otherwise pass it through.

    Returns the trustworthy change_pct, or None when it can't be trusted.
    """
    p  = _safe_float(price)
    pc = _safe_float(prev_close)
    cp = _safe_float(change_pct)

    if p is not None and pc is not None and pc != 0:
        computed = (p - pc) / pc * 100.0
        if abs(computed) > 75:          # price or prev_close is corrupt
            return None
        if cp is None or abs(cp - computed) > 3:
            cp = computed
        return round(cp, 2)

    if cp is not None and abs(cp) > 60:  # uncorroborated + implausible
        return None
    return cp


def _yf_ts(v: Any) -> Optional[str]:
    """yfinance unix-seconds timestamp → 'YYYY-MM-DD' (or None)."""
    try:
        return datetime.utcfromtimestamp(int(v)).date().isoformat() if v else None
    except (TypeError, ValueError, OSError, OverflowError):
        return None


# Map common upstream sector labels to canonical GICS-aligned names that
# the frontend baselines + backend SECTOR_WEIGHT_DELTAS keys use. Without
# this normalization, legacy curated rows ('Telecom') and weird FMP values
# ('Financial' instead of 'Financial Services') skip sector-aware scoring.
_SECTOR_ALIASES = {
    # Communication Services
    "telecom":                  "Communication Services",
    "telecommunications":       "Communication Services",
    "communication":            "Communication Services",
    "communications":           "Communication Services",
    "media":                    "Communication Services",
    # Financial Services
    "financial":                "Financial Services",
    "financials":               "Financial Services",
    "banks":                    "Financial Services",
    "banking":                  "Financial Services",
    "insurance":                "Financial Services",
    # Healthcare
    "health care":              "Healthcare",
    "health":                   "Healthcare",
    "biotechnology":            "Healthcare",
    # Consumer
    "consumer staples":         "Consumer Defensive",
    "consumer cyclical":        "Consumer Cyclical",
    "consumer discretionary":   "Consumer Cyclical",
    # Materials
    "materials":                "Basic Materials",
    # Real Estate
    "real estate investment trusts (reits)": "Real Estate",
    # Tech aliases
    "information technology":   "Technology",
    "tech":                     "Technology",
    # Energy / Utilities / Industrials usually arrive already canonical
}
_CANONICAL_SECTORS = {
    "Technology", "Communication Services", "Healthcare", "Financial Services",
    "Consumer Defensive", "Consumer Cyclical", "Industrials", "Energy",
    "Utilities", "Real Estate", "Basic Materials",
}


def _normalize_sector(s: Any) -> str:
    """Map any upstream sector label to canonical GICS name. Returns empty
    string for missing / placeholder values."""
    if not s or not isinstance(s, str):
        return ""
    raw = s.strip()
    if not raw or raw in ("—", "-", "?", "n/a", "N/A"):
        return ""
    if raw in _CANONICAL_SECTORS:
        return raw
    aliased = _SECTOR_ALIASES.get(raw.lower())
    return aliased if aliased else raw  # unknown labels pass through as-is


def _compute_rsi(closes: list[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 2:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    avg_g  = sum(gains[:period]) / period
    avg_l  = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i])  / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_g / avg_l), 1)


def _compute_atr(highs: list, lows: list, closes: list, period: int = 14) -> Optional[float]:
    if len(highs) < period + 1:
        return None
    trs = []
    for i in range(1, len(highs)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        )
        trs.append(tr)
    return round(sum(trs[-period:]) / period, 4)


# ── DataCoordinator ────────────────────────────────────────────────────

# Ticker aliases: maps symbols that yfinance can't resolve to the correct one.
# Add entries here when a ticker needs a yfinance-specific suffix.
_YF_ALIAS: dict[str, str] = {
    # OTC/Pink-sheet ADRs — yfinance usually handles these directly but
    # the Yahoo Finance internal symbol may differ
    "ABBNY": "ABBNY",   # ABB Ltd ADR — OTC; works as-is
    "FANUY": "FANUY",   # Fanuc Corp ADR — OTC; works as-is
    # If a ticker keeps failing, add: "BADTK": "GOODTK"
}


def _parse_yf_earnings(info: dict) -> dict:
    """
    Extract earnings date + days_to_earnings from yfinance info dict.

    Strategy: collect EVERY candidate date from yfinance, then prefer the
    SOONEST FUTURE date. Only fall back to a recent past date if no future
    one is found. This fixes the long-standing bug where the function would
    keep showing "earnings in 0 days" for days/weeks after a release because
    `earningsDate` was sticky and shadowed `nextEarningsDate`.

    Also returns:
      - earnings_just_reported: True if the most recent past earnings is
        within the last 2 days (lets the UI show a post-earnings summary
        card and stop saying "in 0 days" indefinitely).
    """
    from datetime import datetime as _dt, date as _date
    today = _date.today()

    raw_values = []
    for key in ("earningsDate", "earningsTimestamp", "earningsTimestampStart",
                "earningsTimestampEnd", "nextEarningsDate", "mostRecentQuarter"):
        v = info.get(key)
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            raw_values.extend(v)
        else:
            raw_values.append(v)

    parsed = []
    for raw in raw_values:
        if raw is None:
            continue
        try:
            if isinstance(raw, (int, float)):
                edate = _dt.fromtimestamp(raw).date()
            elif isinstance(raw, _date):
                edate = raw
            else:
                edate = _date.fromisoformat(str(raw)[:10])
            parsed.append(edate)
        except Exception:
            continue

    if not parsed:
        return {}

    parsed = list(dict.fromkeys(parsed))

    future_dates = sorted(d for d in parsed if (d - today).days >= 0)
    past_dates   = sorted(
        (d for d in parsed if 0 > (d - today).days >= -90),
        reverse=True,
    )

    if future_dates:
        edate = future_dates[0]
        days  = (edate - today).days
        # Show the post-earnings card for ~2 weeks after a reported event,
        # so users who don't check daily still see the EPS / revenue summary.
        just_reported = bool(
            past_dates and (today - past_dates[0]).days <= 14
        )
        out = {
            "earnings_date":     edate.isoformat(),
            "days_to_earnings":  days,
        }
        if just_reported:
            out["earnings_just_reported"] = True
            out["last_earnings_date"]     = past_dates[0].isoformat()
        return out

    if past_dates:
        last = past_dates[0]
        days_since = (today - last).days
        return {
            "earnings_date":           last.isoformat(),
            "days_to_earnings":        None,
            "earnings_just_reported":  days_since <= 14,
            "last_earnings_date":      last.isoformat(),
            "days_since_earnings":     days_since,
        }

    return {}


class DataCoordinator:
    """Single shared instance owning the cache and all HTTP clients."""

    def __init__(self) -> None:
        self.cache           = SmartCache(disk_path=config.CACHE_DISK_FILE)
        self._fh_limiter     = RateLimiter(config.FINNHUB_CALLS_PER_MIN)
        self._fmp_limiter    = RateLimiter(config.FMP_CALLS_PER_MIN)
        self._av_calls_today = 0
        self._av_reset_day   = date.today()
        self._fmp_calls_today= 0
        self._fmp_reset_day  = date.today()

        # ── Alpaca Markets (FREE) — primary price + candle source ─────────────
        self.alpaca = AlpacaClient(
            key_id     = getattr(config, "ALPACA_KEY_ID",     ""),
            secret_key = getattr(config, "ALPACA_SECRET_KEY", ""),
        )
        if self.alpaca.enabled:
            logger.info("Alpaca Markets enabled (FREE IEX feed) — primary price/candle source")

        # ── Polygon.io — optional paid upgrade ────────────────────────────────
        self.polygon = PolygonClient(api_key=getattr(config, "POLYGON_API_KEY", ""))
        _plan = getattr(config, "POLYGON_PLAN", "free")
        if self.polygon.enabled:
            logger.info(f"Polygon.io enabled (plan: {_plan}) — upgraded price source")

        # Log active data source
        if not self.alpaca.enabled and not self.polygon.enabled:
            logger.info("No paid data source configured — using yfinance + Finnhub fallback")

        # Convenience property: best available market data client
        # (Polygon takes priority if both are configured — SIP > IEX)
        self._market: AlpacaClient | PolygonClient | None = (
            self.polygon if self.polygon.enabled else
            self.alpaca  if self.alpaca.enabled  else
            None
        )

        self.api_status: dict[str, dict] = {
            "alpaca":        {"ok": True,  "calls": 0, "errors": 0, "last_ok": None,
                              "enabled": self.alpaca.enabled, "plan": "free-iex"},
            "polygon":       {"ok": True,  "calls": 0, "errors": 0, "last_ok": None,
                              "enabled": self.polygon.enabled, "plan": _plan},
            "finnhub":       {"ok": True,  "calls": 0, "errors": 0, "last_ok": None},
            "alpha_vantage": {"ok": True,  "calls": 0, "errors": 0,
                              "remaining_today": config.AV_CALLS_PER_DAY},
            "apewisdom":     {"ok": True,  "calls": 0, "errors": 0, "last_ok": None},
            "sec_api":       {"ok": True,  "calls": 0, "errors": 0, "last_ok": None},
            "fmp":           {"ok": True,  "calls": 0, "errors": 0,
                              "remaining_today": config.FMP_CALLS_PER_DAY,
                              "enabled": bool(config.FMP_API_KEY)},
        }

    def _fmp_remaining(self) -> int:
        today = date.today()
        if today != self._fmp_reset_day:
            self._fmp_calls_today = 0
            self._fmp_reset_day   = today
        return config.FMP_CALLS_PER_DAY - self._fmp_calls_today

    # ── AV daily-limit guard ──────────────────────────────────────────

    def _av_remaining(self) -> int:
        # Shared across ALL Alpha Vantage consumers (fundamentals + transcript +
        # PDF fallback) so the 25/day pool can't be silently overspent.
        import av_budget
        return av_budget.remaining()

    # ── FINNHUB ───────────────────────────────────────────────────────

    async def _fh_get(self, path: str, params: dict) -> Optional[dict]:
        await self._fh_limiter.wait()
        params = {**params, "token": config.FINNHUB_KEY}
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=False) as c:
                r = await c.get(f"https://finnhub.io/api/v1{path}", params=params)
                # 302 → endpoint not available on this tier; return silently
                if r.is_redirect or r.status_code in (301, 302, 303, 307, 308):
                    return None
                r.raise_for_status()
            data = r.json()
            self.api_status["finnhub"]["calls"]  += 1
            self.api_status["finnhub"]["ok"]      = True
            self.api_status["finnhub"]["last_ok"] = datetime.now().isoformat()
            return data
        except Exception as e:
            self.api_status["finnhub"]["errors"] += 1
            self.api_status["finnhub"]["ok"]      = False
            logger.warning(f"Finnhub {path} error: {e}")
            return None

    async def get_quote(self, ticker: str) -> dict:
        key    = f"quote:{ticker}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        result: dict = {}

        # ── Alpaca (free IEX — primary) ───────────────────────────────
        if self.alpaca.enabled:
            alp = await self.alpaca.get_quote(ticker)
            if alp and alp.get("price"):
                result = {"ticker": ticker, **alp}
                self.api_status["alpaca"]["calls"] += 1
                self.cache.set(key, result, config.CACHE_LIVE_TTL)
                return result

        # ── Polygon.io (paid upgrade — secondary) ─────────────────────
        if self.polygon.enabled:
            poly = await self.polygon.get_quote(ticker)
            if poly and poly.get("price"):
                result = {"ticker": ticker, **poly}
                self.api_status["polygon"]["calls"] += 1
                self.api_status["polygon"]["ok"]     = True
                self.cache.set(key, result, config.CACHE_LIVE_TTL)
                return result

        # ── Finnhub fallback ─────────────────────────────────────────
        data = await self._fh_get("/quote", {"symbol": ticker})
        if data and data.get("c") and data.get("c") != 0:
            result = {
                "ticker":     ticker,
                "price":      _safe_float(data.get("c")),
                "change_abs": _safe_float(data.get("d")),
                "change_pct": _safe_float(data.get("dp")),
                "high":       _safe_float(data.get("h")),
                "low":        _safe_float(data.get("l")),
                "open":       _safe_float(data.get("o")),
                "prev_close": _safe_float(data.get("pc")),
                "volume":     _safe_float(data.get("v")),
                "ts":         data.get("t"),
            }
            self.cache.set(key, result, config.CACHE_LIVE_TTL)
            return result

        return {"ticker": ticker, "price": None}

    async def get_candles(self, ticker: str, days: int = 130) -> dict:
        key    = f"candles:{ticker}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        # ── Alpaca (free IEX — primary) ───────────────────────────────
        if self.alpaca.enabled:
            alp = await self.alpaca.get_candles(ticker, days)
            if alp and alp.get("rsi_14") is not None:
                self.cache.set(key, alp, config.CACHE_TECH_TTL)
                return alp

        # ── Polygon.io (paid upgrade — secondary) ─────────────────────
        if self.polygon.enabled:
            poly = await self.polygon.get_candles(ticker, days)
            if poly and poly.get("rsi_14") is not None:
                self.cache.set(key, poly, config.CACHE_TECH_TTL)
                return poly

        # ── Finnhub fallback ─────────────────────────────────────────
        to_ts   = int(time.time())
        from_ts = to_ts - days * 86_400
        data    = await self._fh_get("/stock/candle", {
            "symbol": ticker, "resolution": "D",
            "from": from_ts, "to": to_ts,
        })
        if not data or data.get("s") != "ok":
            return {}

        closes = data.get("c", [])
        highs  = data.get("h", [])
        lows   = data.get("l", [])
        vols   = data.get("v", [])
        n      = len(closes)

        momentum_1m  = round((closes[-1] / closes[-22] - 1) * 100, 2) if n >= 22 else None
        momentum_3m  = round((closes[-1] / closes[-66] - 1) * 100, 2) if n >= 66 else None
        avg_vol      = (sum(vols[-21:-1]) / 20) if n >= 21 else None
        volume_ratio = round(vols[-1] / avg_vol, 2) if (avg_vol and avg_vol > 0 and vols) else None

        result = {
            "ticker":       ticker,
            "rsi_14":       _compute_rsi(closes),
            "atr_14":       _compute_atr(highs, lows, closes),
            "momentum_1m":  momentum_1m,
            "momentum_3m":  momentum_3m,
            "volume_ratio": volume_ratio,
            "high_52w":     max(highs[-252:]) if n >= 60 else None,
            "low_52w":      min(lows[-252:])  if n >= 60 else None,
            "price_latest": closes[-1]        if closes else None,
        }
        self.cache.set(key, result, config.CACHE_TECH_TTL)
        return result

    async def get_candles_raw(self, ticker: str, days: int = 130) -> dict:
        """Full daily OHLCV array for client charting + price-action analysis.
        Cached 24h under candles_raw:{ticker}. Alpaca primary, Finnhub fallback."""
        key    = f"candles_raw:{ticker}:{days}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        if self.alpaca.enabled:
            alp = await self.alpaca.get_candles_raw(ticker, days)
            if alp and alp.get("candles"):
                self.cache.set(key, alp, config.CACHE_TECH_TTL)
                return alp

        # ── Finnhub fallback ─────────────────────────────────────────
        to_ts   = int(time.time())
        from_ts = to_ts - (days + 60) * 86_400
        data    = await self._fh_get("/stock/candle", {
            "symbol": ticker, "resolution": "D",
            "from": from_ts, "to": to_ts,
        })
        if not data or data.get("s") != "ok":
            return {}
        ts  = data.get("t", []); op = data.get("o", []); hi = data.get("h", [])
        lo  = data.get("l", []); cl = data.get("c", []); vo = data.get("v", [])
        n   = len(cl)
        if n < 5:
            return {}
        candles = [{
            "t": datetime.utcfromtimestamp(ts[i]).strftime("%Y-%m-%d") if i < len(ts) else "",
            "o": round(op[i], 4), "h": round(hi[i], 4), "l": round(lo[i], 4),
            "c": round(cl[i], 4), "v": int(vo[i] if i < len(vo) else 0),
        } for i in range(n)][-days:]
        result = {
            "ticker":       ticker,
            "candles":      candles,
            "week_52_high": max(hi[-252:]) if n >= 60 else max(hi),
            "week_52_low":  min(lo[-252:]) if n >= 60 else min(lo),
            "atr_14":       _compute_atr(hi, lo, cl),
        }
        self.cache.set(key, result, config.CACHE_TECH_TTL)
        return result

    async def get_recommendation(self, ticker: str) -> dict:
        key    = f"rec:{ticker}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        data = await self._fh_get("/stock/recommendation", {"symbol": ticker})
        if not data or not isinstance(data, list) or not data:
            return {}

        latest     = data[0]
        strong_buy = latest.get("strongBuy", 0)
        buy        = latest.get("buy", 0)
        hold       = latest.get("hold", 0)
        sell       = latest.get("sell", 0)
        strong_sell= latest.get("strongSell", 0)
        total      = strong_buy + buy + hold + sell + strong_sell

        result = {
            "strong_buy_pct":  round(strong_buy / total * 100, 1) if total else None,
            "total_analysts":  total,
            "strong_buy":      strong_buy,
        }
        self.cache.set(key, result, config.CACHE_TECH_TTL)
        return result

    async def get_news(self, ticker: str, days: int = 7) -> list:
        """
        Fetch recent company news. Tries Finnhub first (richer payload), then
        falls back to Yahoo Finance via yfinance (free, no rate limit).

        On the Finnhub free tier `/company-news` returns 302 — so the fallback
        path is the one that actually delivers headlines for most users.
        """
        key    = f"news:{ticker}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        # ── Primary: Finnhub (paid tiers only — usually 302s on free) ─────
        result: list = []
        to_d   = date.today()
        from_d = to_d - timedelta(days=days)
        data   = await self._fh_get("/company-news", {
            "symbol": ticker, "from": str(from_d), "to": str(to_d),
        })
        if isinstance(data, list) and data:
            result = [
                {
                    "headline":        n.get("headline", ""),
                    "source":          n.get("source", ""),
                    "url":             n.get("url", ""),
                    "datetime":        n.get("datetime", 0),
                    "sentiment_score": None,
                }
                for n in data[:15]
                if n.get("headline")
            ]

        # ── Fallback: Yahoo Finance via yfinance (always available, free) ──
        if not result:
            try:
                import yfinance as _yf
                # Run blocking call in a thread so we don't stall the event loop
                yf_news = await asyncio.to_thread(
                    lambda: _yf.Ticker(ticker).news or []
                )
                cutoff_ts = int((to_d - timedelta(days=days)).strftime("%s")) if hasattr(to_d, 'strftime') else 0
                for n in yf_news[:15]:
                    # yfinance v0.2+ wraps each item under "content"
                    c = n.get("content") if isinstance(n.get("content"), dict) else n
                    title = c.get("title") or n.get("title") or ""
                    if not title:
                        continue
                    pub_dt = c.get("pubDate") or n.get("providerPublishTime") or 0
                    # pubDate is ISO string in v0.2+, providerPublishTime is unix int
                    if isinstance(pub_dt, str):
                        try:
                            pub_dt = int(datetime.fromisoformat(pub_dt.replace("Z", "+00:00")).timestamp())
                        except Exception:
                            pub_dt = 0
                    elif not isinstance(pub_dt, (int, float)):
                        pub_dt = 0
                    url = (c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict) else None
                    url = url or c.get("link") or n.get("link", "")
                    src = (c.get("provider") or {}).get("displayName") if isinstance(c.get("provider"), dict) else None
                    src = src or n.get("publisher", "")
                    result.append({
                        "headline":        title,
                        "source":          src,
                        "url":             url,
                        "datetime":        pub_dt,
                        "sentiment_score": None,
                    })
            except Exception as e:
                logger.warning(f"yfinance news fallback failed for {ticker}: {e}")

        # Cache: full TTL on hits, shorter TTL on empty so we recover fast
        ttl = config.CACHE_NEWS_TTL if result else 300
        self.cache.set(key, result, ttl)
        return result

    async def get_earnings_date(self, ticker: str) -> Optional[int]:
        """
        Get days to next earnings. Priority:
        1. Already extracted from yfinance info (in candles cache)
        2. Direct yfinance calendar fetch
        3. Finnhub earnings calendar (fallback)
        """
        key    = f"earnings:{ticker}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        today = date.today()

        # ── Try yfinance calendar directly ───────────────────────────────
        try:
            import yfinance as _yf
            tk = _yf.Ticker(ticker)
            cal = tk.calendar  # DataFrame or dict with 'Earnings Date' key
            edate = None
            if cal is not None:
                if hasattr(cal, 'columns'):
                    # DataFrame: 'Earnings Date' column
                    col = next((c for c in cal.columns if 'earnings' in str(c).lower()), None)
                    if col is not None and len(cal[col]) > 0:
                        edate = cal[col].iloc[0]
                elif isinstance(cal, dict):
                    raw = cal.get('Earnings Date') or cal.get('earningsDate')
                    if isinstance(raw, (list, tuple)) and raw:
                        edate = raw[0]
                    elif raw:
                        edate = raw
            if edate is not None:
                if hasattr(edate, 'date'):
                    edate = edate.date()
                elif not isinstance(edate, date):
                    from datetime import datetime as _dt
                    try:
                        edate = _dt.fromisoformat(str(edate)[:10]).date()
                    except Exception:
                        edate = None
            if edate and isinstance(edate, date):
                days = (edate - today).days
                if days >= -7:  # accept very recent past too
                    self.cache.set(key, days, config.CACHE_TECH_TTL)
                    return days
        except Exception as exc:
            logger.debug(f"yfinance calendar failed for {ticker}: {exc}")

        # ── Fallback: Finnhub earnings calendar ──────────────────────────
        to_d = today + timedelta(days=90)
        data = await self._fh_get("/calendar/earnings", {
            "symbol": ticker, "from": str(today), "to": str(to_d),
        })
        days_until = None
        if data and isinstance(data.get("earningsCalendar"), list):
            for entry in data["earningsCalendar"]:
                try:
                    edate     = date.fromisoformat(entry["date"])
                    days_until = (edate - today).days
                    break
                except Exception:
                    continue

        self.cache.set(key, days_until, config.CACHE_TECH_TTL)
        return days_until

    # ── EARNINGS INTELLIGENCE (FMP press release + transcript + LLM) ──
    async def get_post_earnings_intel(self, ticker: str, earnings_date: str,
                                       ticker_dict: dict = None) -> dict:
        """
        Lazy-loaded. When called for a ticker that just reported earnings,
        fetches the FMP press release + earnings-call transcript and runs
        them through Anthropic Haiku to extract:
            - forward guidance (raised / maintained / lowered + numbers)
            - call sentiment (bullish/cautious + key quotes)
        Cached by (ticker, earnings_date) for 90 days, so we only pay the
        LLM cost once per quarter per stock.

        Returns: {"guidance": {...}, "sentiment": {...}}
        Either sub-dict can be empty if data is missing or LLM is offline.
        """
        ticker = ticker.upper()
        cache_key = f"earnings_intel:{ticker}:{earnings_date or 'unknown'}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        # Lazy-import so the rest of the app loads cleanly even if a
        # supplementary module is missing in some build.
        try:
            import earnings_intel  as ei
            import earnings_sources as es
        except ImportError as exc:
            logger.warning(f"earnings modules not available: {exc}")
            return {"guidance": {}, "reaction": {}}

        result = {"guidance": {}, "reaction": {}, "sentiment": {}}

        # ── 1. Press release → guidance + earnings highlights (one Groq call) ──
        # SEC EDGAR 8-K Item 2.02 + Exhibit 99.1 gives us the press release
        # for free. We extract BOTH forward guidance AND sentiment-style
        # highlights (positives/concerns/Q&A summary) in a single LLM call.
        # The SEC filing_date is the AUTHORITATIVE publication date — always
        # fresh, unlike FMP's earnings-surprises endpoint which lags by months.
        try:
            press = await es.fetch_earnings_press_release(ticker) or {}
            press_text  = press.get("text", "")
            filing_date = press.get("filing_date", "")
            if filing_date:
                # Surface the SEC filing date so the UI can show "Reported [date]"
                # without depending on stale FMP eps_quarters dates.
                result["filing_date"] = filing_date
            if press_text:
                full = await ei.analyze_press_release_full(press_text)
                result["guidance"]  = full.get("guidance", {}) or {}
                result["sentiment"] = full.get("sentiment", {}) or {}
        except Exception as exc:
            logger.warning(f"Press-release analysis failed for {ticker}: {exc}")

        # ── 2. Earnings Reaction Score (deterministic, no API) ────────
        # Quick sanity score from existing ticker data. Cheap to keep, useful
        # as a complement; UI may or may not surface it.
        try:
            t_data = ticker_dict if ticker_dict else (self.cache.get(f"ticker:{ticker}") or {})
            tone   = (result.get("guidance") or {}).get("tone", "") or ""
            result["reaction"] = ei.compute_earnings_reaction(t_data, guidance_tone=tone)
        except Exception as exc:
            logger.warning(f"Reaction score failed for {ticker}: {exc}")

        # ── 3. (optional) Live transcript sentiment ──
        # Only runs if API_NINJAS_KEY is set (paid plan). When set, this
        # OVERRIDES the press-release-derived sentiment with richer call data.
        try:
            transcript_text = await es.fetch_call_transcript(ticker)
            if transcript_text:
                live = await ei.analyze_call_transcript(transcript_text)
                if live:
                    result["sentiment"] = live
        except Exception as exc:
            logger.warning(f"Transcript fetch failed for {ticker}: {exc}")

        # Cache for 90 days (next earnings is ~90 days out anyway).
        # We cache even partial / empty results so we don't retry the LLM
        # on every refresh for a stock whose data is genuinely unavailable.
        self.cache.set(cache_key, result, 90 * 86400)
        return result

    # ── ALPHA VANTAGE ─────────────────────────────────────────────────

    async def get_fundamentals(self, ticker: str) -> dict:
        key    = f"fund:{ticker}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        if self._av_remaining() <= 0:
            logger.warning(f"AV daily limit reached — skipping {ticker}")
            self.api_status["alpha_vantage"]["ok"] = False
            return {}

        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    "https://www.alphavantage.co/query",
                    params={"function": "OVERVIEW", "symbol": ticker,
                            "apikey": config.ALPHA_VANTAGE_KEY},
                )
                r.raise_for_status()
            data = r.json()

            if "Note" in data or "Information" in data:
                msg = data.get("Note") or data.get("Information", "")
                logger.warning(f"AV rate limit hit: {msg[:80]}")
                self.api_status["alpha_vantage"]["ok"] = False
                return {}

            if not data.get("Symbol"):
                return {}

            import av_budget
            av_budget.spend(1)
            self._av_calls_today += 1
            self.api_status["alpha_vantage"]["calls"]          += 1
            self.api_status["alpha_vantage"]["remaining_today"]  = self._av_remaining()
            self.api_status["alpha_vantage"]["ok"]               = True

            def f(k): return _safe_float(data.get(k))

            # Compute gross_margin from AV fields
            gross_profit = f("GrossProfitTTM") or f("GrossProfit")
            revenue_ttm  = f("RevenueTTM")
            gross_margin = None
            if gross_profit and revenue_ttm and revenue_ttm > 0:
                gross_margin = round(gross_profit / revenue_ttm, 4)

            # Revenue growth: AV gives QuarterlyRevenueGrowthYOY as decimal (e.g. 0.35)
            rev_g = _safe_float(data.get("QuarterlyRevenueGrowthYOY"))
            eps_g = _safe_float(data.get("QuarterlyEarningsGrowthYOY"))

            result = {
                "ticker":              ticker,
                "name":                data.get("Name", ""),
                "sector":              data.get("Sector", ""),
                "industry":            data.get("Industry", ""),
                "exchange":            data.get("Exchange", ""),
                "market_cap":          f("MarketCapitalization"),
                "pe_ratio":            f("PERatio") or f("ForwardPE"),
                "peg_ratio":           f("PEGRatio"),
                "eps":                 f("EPS"),
                "revenue_ttm":         revenue_ttm,
                "gross_margin":        gross_margin,
                "profit_margin":       _safe_float(data.get("ProfitMargin")),
                "revenue_growth_yoy":  rev_g,
                "eps_growth_yoy":      eps_g,
                "free_cashflow":       None,       # AV OVERVIEW doesn't include FCF
                "operating_cashflow":  None,
                "debt_to_equity":      f("DebtToEquityRatio"),
                "price_to_sales":      f("PriceToSalesRatioTTM"),
                "beta":                f("Beta"),
                "week_52_high":        f("52WeekHigh"),
                "week_52_low":         f("52WeekLow"),
                "sma_50":              f("50DayMovingAverage"),
                "sma_200":             f("200DayMovingAverage"),
                "target_price":        f("AnalystTargetPrice"),
                "short_percent_float": f("ShortPercentOutstanding"),
                "description":         (data.get("Description") or "")[:500],
            }
            self.cache.set(key, result, config.CACHE_FUND_TTL)
            return result

        except Exception as e:
            self.api_status["alpha_vantage"]["errors"] += 1
            logger.warning(f"AV overview error for {ticker}: {e}")
            return {}

    # ── FINANCIAL MODELING PREP (FMP) — fallback data source ─────

    async def _fmp_get(self, path: str, params: dict) -> Optional[Any]:
        """Shared FMP HTTP helper. Returns parsed JSON or None on failure."""
        if not config.FMP_API_KEY or self._fmp_remaining() <= 0:
            return None
        await self._fmp_limiter.wait()   # Starter = 300/min; throttle per-minute, not per-day
        try:
            params = {**params, "apikey": config.FMP_API_KEY}
            async with httpx.AsyncClient(timeout=12) as c:
                r = await c.get(f"https://financialmodelingprep.com/api{path}", params=params)
                r.raise_for_status()
            data = r.json()
            self._fmp_calls_today += 1
            self.api_status["fmp"]["calls"]          += 1
            self.api_status["fmp"]["ok"]              = True
            self.api_status["fmp"]["remaining_today"] = self._fmp_remaining()
            return data
        except Exception as e:
            self.api_status["fmp"]["errors"] += 1
            self.api_status["fmp"]["ok"]      = False
            logger.warning(f"FMP {path} error: {e}")
            return None

    async def _fmp_stable(self, endpoint: str, params: dict) -> Optional[Any]:
        """FMP **stable**-API helper. New FMP accounts (incl. our paid Starter
        key) MUST use https://financialmodelingprep.com/stable/{endpoint}?symbol=…
        — the old /v3 + /v4 paths now return "Legacy Endpoint" for non-legacy
        keys. Returns parsed JSON or None on failure / error payloads."""
        if not config.FMP_API_KEY or self._fmp_remaining() <= 0:
            return None
        await self._fmp_limiter.wait()   # Starter = 300/min
        try:
            q = {**params, "apikey": config.FMP_API_KEY}
            async with httpx.AsyncClient(timeout=12) as c:
                r = await c.get(
                    f"https://financialmodelingprep.com/stable/{endpoint}", params=q
                )
                r.raise_for_status()
            data = r.json()
            # The stable API returns an error OBJECT (dict) instead of a list when
            # an endpoint is restricted / params are wrong / the key is legacy.
            if isinstance(data, dict) and any(
                k in data for k in ("Error Message", "error", "message")
            ):
                logger.warning(f"FMP stable {endpoint}: {str(data)[:140]}")
                self.api_status["fmp"]["ok"] = False
                return None
            self._fmp_calls_today += 1
            self.api_status["fmp"]["calls"]          += 1
            self.api_status["fmp"]["ok"]              = True
            self.api_status["fmp"]["remaining_today"] = self._fmp_remaining()
            return data
        except Exception as e:
            self.api_status["fmp"]["errors"] += 1
            self.api_status["fmp"]["ok"]      = False
            logger.warning(f"FMP stable {endpoint} error: {e}")
            return None

    async def get_fmp_quote(self, ticker: str) -> dict:
        """
        FMP real-time quote — used as fallback when Finnhub/yfinance returns
        no price for a ticker (e.g. OTC ADRs, recently IPO'd stocks).
        """
        key    = f"fmp_quote:{ticker}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        data = await self._fmp_stable("quote", {"symbol": ticker})
        if not data or not isinstance(data, list) or not data:
            return {}

        q = data[0]
        result = {
            "ticker":     ticker,
            "price":      _safe_float(q.get("price")),
            "change_abs": _safe_float(q.get("change")),
            "change_pct": _safe_float(q.get("changePercentage")),
            "high":       _safe_float(q.get("dayHigh")),
            "low":        _safe_float(q.get("dayLow")),
            "open":       _safe_float(q.get("open")),
            "prev_close": _safe_float(q.get("previousClose")),
            "volume":     _safe_float(q.get("volume")),
            "market_cap": _safe_float(q.get("marketCap")),
            "pe_ratio":   _safe_float(q.get("pe")),
            "high_52w":   _safe_float(q.get("yearHigh")),
            "low_52w":    _safe_float(q.get("yearLow")),
        }
        self.cache.set(key, result, config.CACHE_LIVE_TTL)
        return result

    async def get_fmp_fundamentals(self, ticker: str) -> dict:
        """
        FMP company profile + quarterly earnings surprises.
        Called for tickers where yfinance returns empty enrichment.
        Returns: market_cap, beta, revenue_growth_yoy, eps_quarters, avg_eps_surprise_pct, etc.
        """
        key    = f"fmp_fund:{ticker}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        result: dict = {}

        # ── Company profile ────────────────────────────────────────
        profile_data = await self._fmp_stable("profile", {"symbol": ticker})
        if profile_data and isinstance(profile_data, list) and profile_data:
            p = profile_data[0]
            result.update({
                "name":         p.get("companyName", ""),
                "sector":       p.get("sector", ""),
                "industry":     p.get("industry", ""),
                "exchange":     p.get("exchange", ""),
                "market_cap":   _safe_float(p.get("marketCap")),
                "beta":         _safe_float(p.get("beta")),
                "description":  (p.get("description") or "")[:500],
                "price":        _safe_float(p.get("price")),
                "high_52w":     _safe_float(p.get("range", "").split("-")[-1]) if "-" in str(p.get("range","")) else None,
                "low_52w":      _safe_float(p.get("range", "").split("-")[0])  if "-" in str(p.get("range","")) else None,
            })

        # ── Quarterly income statement (revenue + gross margin) ────
        inc_data = await self._fmp_stable(
            "income-statement",
            {"symbol": ticker, "period": "quarter", "limit": 4}
        )
        if inc_data and isinstance(inc_data, list):
            q_income = []
            for row in inc_data[:4]:
                rev = _safe_float(row.get("revenue"))
                gp  = _safe_float(row.get("grossProfit"))
                ni  = _safe_float(row.get("netIncome"))
                gm  = round(gp/rev, 4) if rev and gp and rev > 0 else None
                q_income.append({
                    "date":         (row.get("date") or "")[:7],
                    "revenue":      rev,
                    "gross_profit": gp,
                    "gross_margin": gm,
                    "net_income":   ni,
                })
            if q_income:
                result["quarterly_income"] = q_income
                # Revenue growth: compare latest vs 4 quarters ago
                r0 = q_income[0].get("revenue")
                r3 = q_income[-1].get("revenue") if len(q_income) >= 4 else None
                if r0 and r3 and r3 > 0:
                    result["rev_growth_qyoy"] = round((r0 - r3) / r3, 4)
                margins = [q["gross_margin"] for q in q_income if q["gross_margin"] is not None]
                if len(margins) >= 2:
                    trend = margins[0] - margins[-1]
                    result["gross_margin_trend"] = "expanding" if trend > 0.02 else "contracting" if trend < -0.02 else "stable"

        # ── EPS surprise history ───────────────────────────────────
        surp_data = await self._fmp_stable("earnings", {"symbol": ticker, "limit": 12})
        if surp_data and isinstance(surp_data, list):
            # stable /earnings includes upcoming quarters (epsActual=null) —
            # keep only the reported ones, newest first.
            reported = [r for r in surp_data if r.get("epsActual") is not None][:4]
            eps_quarters = []
            for row in reported:
                actual   = _safe_float(row.get("epsActual"))
                estimate = _safe_float(row.get("epsEstimated"))
                surp_pct = None
                if actual is not None and estimate is not None and estimate != 0:
                    surp_pct = round((actual - estimate) / abs(estimate) * 100, 2)
                beat = (surp_pct > 0) if surp_pct is not None else None
                eps_quarters.append({
                    "date":         (row.get("date") or "")[:7],
                    "actual":       actual,
                    "estimate":     estimate,
                    "surprise_pct": surp_pct,
                    "beat":         beat,
                })
            if eps_quarters:
                result["eps_quarters"] = eps_quarters
                surprises = [q["surprise_pct"] for q in eps_quarters if q["surprise_pct"] is not None]
                beats     = [q["beat"]         for q in eps_quarters if q["beat"] is not None]
                if surprises:
                    result["avg_eps_surprise_pct"] = round(sum(surprises)/len(surprises), 2)
                if beats:
                    result["eps_beat_streak"] = sum(1 for b in beats if b)
                    result["eps_last_beat"]   = beats[0]

        # ── Key ratios ─────────────────────────────────────────────
        ratio_data = await self._fmp_stable("ratios-ttm", {"symbol": ticker})
        if ratio_data and isinstance(ratio_data, list) and ratio_data:
            r = ratio_data[0]
            result.update({
                # stable renamed the TTM ratio fields (peRatioTTM →
                # priceToEarningsRatioTTM, pegRatioTTM → priceToEarningsGrowthRatioTTM).
                "pe_ratio":      _safe_float(r.get("priceToEarningsRatioTTM")),
                "peg_ratio":     _safe_float(r.get("priceToEarningsGrowthRatioTTM")),
                "gross_margin":  _safe_float(r.get("grossProfitMarginTTM")),
                "profit_margin": _safe_float(r.get("netProfitMarginTTM")),
            })

        if result:
            self.cache.set(key, result, config.CACHE_TECH_TTL)
        return result

    async def get_fmp_enrichment(self, ticker: str) -> dict:
        """On-demand FMP **stable** enrichment for the stock-detail drawer tabs.
        Fills valuation/financial fields yfinance often misses AND adds analyst
        estimates, rating-grade changes, peers and recent SEC filings. ~8 FMP
        calls, only paid when a user actually opens a stock; cached 24h.
        Returns a flat dict of pane-ready fields + structured blocks."""
        key    = f"fmp_extra:{ticker}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        out: dict = {}
        _today = date.today()

        # ── Company profile — the Overview-strip basics yfinance often
        #    misses on thin/new names (market cap, beta, sector). ───────
        pf = await self._fmp_stable("profile", {"symbol": ticker})
        if isinstance(pf, list) and pf:
            p = pf[0]
            out.update({
                "name":       p.get("companyName") or None,
                "sector":     p.get("sector") or None,
                "industry":   p.get("industry") or None,
                "market_cap": _safe_float(p.get("marketCap")),
                "beta":       _safe_float(p.get("beta")),
            })
            _rng = str(p.get("range") or "")
            if "-" in _rng:
                lo, _, hi = _rng.partition("-")
                out["low_52w"]  = _safe_float(lo)
                out["high_52w"] = _safe_float(hi)

        # ── Valuation / profitability ratios (TTM) ──────────────────
        rt = await self._fmp_stable("ratios-ttm", {"symbol": ticker})
        if isinstance(rt, list) and rt:
            r = rt[0]
            out.update({
                "pe_ratio":         _safe_float(r.get("priceToEarningsRatioTTM")),
                "pb_ratio":         _safe_float(r.get("priceToBookRatioTTM")),
                "ps_ratio":         _safe_float(r.get("priceToSalesRatioTTM")),
                "peg_ratio":        _safe_float(r.get("priceToEarningsGrowthRatioTTM")),
                "operating_margin": _safe_float(r.get("operatingProfitMarginTTM")),
                "ebitda_margin":    _safe_float(r.get("ebitdaMarginTTM")),
                "gross_margin":     _safe_float(r.get("grossProfitMarginTTM")),
                "profit_margin":    _safe_float(r.get("netProfitMarginTTM")),
                "debt_to_equity":   _safe_float(r.get("debtToEquityRatioTTM")),
                "current_ratio":    _safe_float(r.get("currentRatioTTM")),
                "dividend_yield":   _safe_float(r.get("dividendYieldTTM")),
            })

        # ── Enterprise-value multiples + returns (TTM) ──────────────
        km = await self._fmp_stable("key-metrics-ttm", {"symbol": ticker})
        if isinstance(km, list) and km:
            m = km[0]
            out.update({
                "ev_ebitda":        _safe_float(m.get("evToEBITDATTM")),
                "ev_sales":         _safe_float(m.get("evToSalesTTM")),
                "roe":              _safe_float(m.get("returnOnEquityTTM")),
                "roa":              _safe_float(m.get("returnOnAssetsTTM")),
                "roic":             _safe_float(m.get("returnOnInvestedCapitalTTM")),
                "fcf_yield":        _safe_float(m.get("freeCashFlowYieldTTM")),
                "enterprise_value": _safe_float(m.get("enterpriseValueTTM")),
            })

        # ── Free cash flow (latest annual) ──────────────────────────
        cf = await self._fmp_stable("cash-flow-statement", {"symbol": ticker, "limit": 1})
        if isinstance(cf, list) and cf:
            c = cf[0]
            out["free_cashflow"]      = _safe_float(c.get("freeCashFlow"))
            out["operating_cashflow"] = _safe_float(c.get("operatingCashFlow"))

        # ── Analyst price-target summary ────────────────────────────
        pt = await self._fmp_stable("price-target-summary", {"symbol": ticker})
        if isinstance(pt, list) and pt:
            p = pt[0]
            tgt = _safe_float(p.get("lastQuarterAvgPriceTarget")) or \
                  _safe_float(p.get("allTimeAvgPriceTarget"))
            if tgt:
                out["target_mean"]    = tgt
                out["total_analysts"] = p.get("lastQuarterCount") or p.get("allTimeCount")

        # ── Forward analyst estimates (next FY) ─────────────────────
        ae = await self._fmp_stable("analyst-estimates",
                                    {"symbol": ticker, "period": "annual", "limit": 8})
        if isinstance(ae, list) and ae:
            # estimates aren't nearest-first — pick the nearest upcoming fiscal
            # year (smallest date whose year >= this year), not the furthest.
            future = sorted(
                [e for e in ae if (e.get("date") or "")[:4].isdigit()
                 and int((e["date"])[:4]) >= _today.year],
                key=lambda e: e.get("date", ""),
            )
            fwd = future[0] if future else ae[0]
            out["est_eps_next_fy"]  = _safe_float(fwd.get("epsAvg"))
            out["est_rev_next_fy"]  = _safe_float(fwd.get("revenueAvg"))
            out["est_num_analysts"] = fwd.get("numAnalystsEps")
            out["est_year"]         = (fwd.get("date") or "")[:4]

        # ── Recent rating changes (upgrades / downgrades) ───────────
        gr = await self._fmp_stable("grades", {"symbol": ticker, "limit": 6})
        if isinstance(gr, list) and gr:
            out["grades"] = [{
                "firm":   g.get("gradingCompany"),
                "from":   g.get("previousGrade"),
                "to":     g.get("newGrade"),
                "action": g.get("action"),
                "date":   g.get("date"),
            } for g in gr[:6] if g.get("gradingCompany")]

        # ── Peers (Benchmark tab) ───────────────────────────────────
        pr = await self._fmp_stable("stock-peers", {"symbol": ticker})
        if isinstance(pr, list) and pr:
            out["peers"] = [{
                "ticker":     x.get("symbol"),
                "name":       x.get("companyName"),
                "price":      _safe_float(x.get("price")),
                "market_cap": _safe_float(x.get("mktCap")),
            } for x in pr[:8] if x.get("symbol")]

        # ── Recent SEC filings (Documents tab) ──────────────────────
        sf = await self._fmp_stable("sec-filings-search/symbol", {
            "symbol": ticker,
            "from":   (_today - timedelta(days=365)).isoformat(),
            "to":     _today.isoformat(),
            "limit":  10,
        })
        if isinstance(sf, list) and sf:
            out["filings"] = [{
                "form": f.get("formType"),
                "date": (f.get("filingDate") or "")[:10],
                "link": f.get("link"),
            } for f in sf[:10] if f.get("formType")]

        if out:
            self.cache.set(key, out, config.CACHE_FUND_TTL)
        return out

    # ── APEWISDOM ─────────────────────────────────────────────────────

    async def get_social_all(self) -> dict[str, dict]:
        key    = "ape:all"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        try:
            headers = {}
            if config.APEWISDOM_KEY:
                headers["Authorization"] = f"Bearer {config.APEWISDOM_KEY}"

            async with httpx.AsyncClient(timeout=12) as c:
                r = await c.get(
                    "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1",
                    headers=headers,
                )
                r.raise_for_status()
            data = r.json()

            result: dict[str, dict] = {}
            for item in data.get("results", []):
                tk   = (item.get("ticker") or "").upper()
                if not tk:
                    continue
                cur  = item.get("mentions", 0) or 0
                prev = item.get("mentions_24h_ago", 0) or 0
                vel  = round((cur - prev) / prev * 100, 1) if prev else 0.0
                result[tk] = {
                    "rank":             item.get("rank"),
                    "mentions_24h":     cur,
                    "mention_velocity": vel,
                }

            self.cache.set(key, result, config.CACHE_SOCIAL_TTL)
            self.api_status["apewisdom"]["calls"]  += 1
            self.api_status["apewisdom"]["ok"]      = True
            self.api_status["apewisdom"]["last_ok"] = datetime.now().isoformat()
            return result

        except Exception as e:
            self.api_status["apewisdom"]["errors"] += 1
            self.api_status["apewisdom"]["ok"]      = False
            logger.warning(f"ApeWisdom error: {e}")
            return {}

    # ── SEC-API ───────────────────────────────────────────────────────

    async def get_insider_transactions(self, ticker: str) -> dict:
        """
        Returns dict with insider_buys_90d / insider_sells_90d counts plus
        a `insider_detail` list of recent transactions.

        Primary source: SEC-API.io (Form 4 filings).
        Fallback: yfinance Ticker.insider_transactions (covers some tickers
        that SEC-API misses — happens when the SEC-API search index is
        slow to ingest a particular issuer's filings).
        """
        key    = f"insider:{ticker}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        # Durable L2 (Supabase) — SEC-API.io is PAID, and insider Form-4 data
        # changes slowly, so a 30-day durable cache means a cold start / redeploy
        # reuses it instead of re-paying for the whole universe. Decoupled from
        # the disk volume so it works on every environment.
        try:
            from kv_store import store as _kv
            durable = await asyncio.to_thread(_kv.get, "insider", ticker, 30 * 24 * 3600)
            if isinstance(durable, dict):
                self.cache.set(key, durable, config.CACHE_INSIDER_TTL)
                return durable
        except Exception:
            pass

        try:
            query = {
                "query": {"query_string": {
                    "query": f'issuerTradingSymbol:"{ticker}" AND formType:"4"'
                }},
                "from": "0", "size": "20",
                "sort": [{"filedAt": {"order": "desc"}}],
            }
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    f"https://api.sec-api.io?token={config.SEC_API_KEY}",
                    json=query,
                )
                r.raise_for_status()
            data = r.json()

            cutoff  = datetime.now().timestamp() - 90 * 86400
            buys = sells = 0
            details = []
            for filing in data.get("filings", []):
                filed_str = (filing.get("filedAt") or "")[:10]
                try:
                    filed_ts = datetime.strptime(filed_str, "%Y-%m-%d").timestamp()
                except Exception:
                    continue
                if filed_ts < cutoff:
                    continue

                owner = filing.get("reportingOwner", {})
                rel   = owner.get("reportingOwnerRelationship", {})
                nd    = filing.get("nonDerivativeTable", {})
                txns  = nd.get("transactions", [{}])
                first = txns[0] if txns else {}
                code  = first.get("transactionCoding", {}).get("transactionCode", "")
                shares= _safe_float(first.get("transactionAmounts", {}).get("shares"))
                price = _safe_float(first.get("transactionAmounts", {}).get("pricePerShare"))

                if code == "P":    buys  += 1
                elif code == "S":  sells += 1

                details.append({
                    "filed_at":    filed_str,
                    "insider":     owner.get("reportingOwnerName", ""),
                    "title":       rel.get("officerTitle", "Director" if rel.get("isDirector") else ""),
                    "tx_code":     code,
                    "shares":      shares,
                    "price":       price,
                    "value":       round(shares * price, 0) if shares and price else None,
                })

            result = {
                "insider_buys_90d":  buys,
                "insider_sells_90d": sells,
                "insider_detail":    details[:8],
            }
            # If SEC-API returned nothing, try yfinance as a fallback (covers
            # tickers like CIEN where the SEC-API search index lags behind)
            if buys == 0 and sells == 0:
                yf_fb = await self._yf_insider_fallback(ticker)
                if yf_fb["insider_buys_90d"] > 0 or yf_fb["insider_sells_90d"] > 0:
                    result = yf_fb
            self.cache.set(key, result, config.CACHE_INSIDER_TTL)
            try:
                from kv_store import store as _kv
                await asyncio.to_thread(_kv.set, "insider", ticker, result)   # durable — survives redeploys
            except Exception:
                pass
            self.api_status["sec_api"]["calls"]  += 1
            self.api_status["sec_api"]["ok"]      = True
            self.api_status["sec_api"]["last_ok"] = datetime.now().isoformat()
            return result

        except Exception as e:
            self.api_status["sec_api"]["errors"] += 1
            self.api_status["sec_api"]["ok"]      = False
            logger.warning(f"SEC-API error for {ticker}: {e}")
            # Try yfinance fallback even when SEC-API fails entirely
            try:
                yf_fb = await self._yf_insider_fallback(ticker)
                self.cache.set(key, yf_fb, config.CACHE_INSIDER_TTL)
                return yf_fb
            except Exception:
                pass
            return {"insider_buys_90d": 0, "insider_sells_90d": 0, "insider_detail": []}

    async def _yf_insider_fallback(self, ticker: str) -> dict:
        """
        Pull insider buy/sell counts from yfinance.Ticker.insider_transactions.
        Used when SEC-API returns nothing for a ticker. Free, no rate limit.
        Returns the same shape as the SEC-API path so callers don't care which
        source provided the data.
        """
        if not _YF_AVAILABLE:
            return {"insider_buys_90d": 0, "insider_sells_90d": 0, "insider_detail": []}

        def _fetch():
            try:
                tk = yf.Ticker(ticker)
                df = getattr(tk, "insider_transactions", None)
                if df is None or df.empty:
                    return {"insider_buys_90d": 0, "insider_sells_90d": 0, "insider_detail": []}
                cutoff = datetime.now() - timedelta(days=90)
                buys = sells = 0
                details = []
                for _, row in df.head(40).iterrows():
                    # Date column varies by yfinance version
                    raw_date = row.get("Start Date") or row.get("Date") or row.get("date")
                    if raw_date is None:
                        continue
                    try:
                        d = raw_date if hasattr(raw_date, 'to_pydatetime') else datetime.fromisoformat(str(raw_date)[:10])
                        d = d.to_pydatetime() if hasattr(d, 'to_pydatetime') else d
                    except Exception:
                        continue
                    if d < cutoff:
                        continue
                    txt = str(row.get("Text") or row.get("Transaction") or "").lower()
                    shares = _safe_float(row.get("Shares"))
                    val    = _safe_float(row.get("Value"))
                    # yfinance text: "Purchase" / "Sale" / "Sale (Multiple)" / "Award"
                    if "purchase" in txt or "buy" in txt:
                        buys += 1
                        code = "P"
                    elif "sale" in txt or "sell" in txt:
                        sells += 1
                        code = "S"
                    else:
                        # Skip awards/grants — those aren't open-market signals
                        continue
                    details.append({
                        "filed_at": d.strftime("%Y-%m-%d"),
                        "insider":  str(row.get("Insider") or row.get("Name") or ""),
                        "title":    str(row.get("Position") or ""),
                        "tx_code":  code,
                        "shares":   shares,
                        "price":    (val / shares) if (shares and val) else None,
                        "value":    val,
                    })
                return {
                    "insider_buys_90d":  buys,
                    "insider_sells_90d": sells,
                    "insider_detail":    details[:8],
                }
            except Exception as e:
                logger.debug(f"yf insider fallback failed for {ticker}: {e}")
                return {"insider_buys_90d": 0, "insider_sells_90d": 0, "insider_detail": []}

        return await asyncio.to_thread(_fetch)

    # ── YAHOO FINANCE (free, no key) ─────────────────────────────────

    async def get_bid_ask(self, ticker: str) -> dict:
        """
        Fetch live bid/ask from Finnhub /stock/bid-ask.
        Returns bid, ask, spread (abs + %), used as a liquidity proxy.
        Short TTL — same as live quotes.
        """
        key    = f"bidask:{ticker}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        data = await self._fh_get("/stock/bid-ask", {"symbol": ticker})
        if not data:
            return {}

        bid = _safe_float(data.get("b"))
        ask = _safe_float(data.get("a"))
        if bid is None or ask is None:
            return {}

        spread     = round(ask - bid, 4)
        spread_pct = round(spread / bid * 100, 4) if bid > 0 else None

        result = {
            "bid":              bid,
            "ask":              ask,
            "bid_ask_spread":   spread,
            "bid_ask_spread_pct": spread_pct,
        }
        self.cache.set(key, result, config.CACHE_LIVE_TTL)
        return result

    async def get_quarterly_results(self, ticker: str) -> dict:
        """
        Fetch last 4 quarters of earnings quality data via Yahoo Finance:
          - EPS actual vs estimate → surprise % and beat/miss flag
          - Revenue, gross margin, net income per quarter
          - Free cash flow per quarter
          - Summary: avg_eps_surprise_pct, eps_beat_streak, rev_growth_qyoy
        Cached for CACHE_TECH_TTL (1 hour).
        """
        key    = f"quarterly:{ticker}"
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        if not _YF_AVAILABLE:
            return {}

        def _fetch():
            try:
                tk = yf.Ticker(ticker)
                result: dict = {}

                # ── EPS surprise history ──────────────────────────────
                eps_quarters: list[dict] = []
                for attr in ("earnings_history", "quarterly_earnings"):
                    try:
                        df = getattr(tk, attr, None)
                        if df is None or df.empty:
                            continue
                        df = df.head(4)
                        for idx, row in df.iterrows():
                            # Column names vary by yfinance version
                            actual   = _safe_float(row.get("epsActual")   or row.get("Reported EPS"))
                            estimate = _safe_float(row.get("epsEstimate") or row.get("EPS Estimate"))
                            surp_pct = _safe_float(row.get("surprisePercent") or row.get("Surprise(%)"))
                            if surp_pct is None and actual is not None and estimate is not None and estimate != 0:
                                surp_pct = round((actual - estimate) / abs(estimate) * 100, 2)
                            beat = (surp_pct > 0) if surp_pct is not None else None
                            eps_quarters.append({
                                "date":         str(idx)[:10],
                                "actual":       actual,
                                "estimate":     estimate,
                                "surprise_pct": surp_pct,
                                "beat":         beat,
                            })
                        if eps_quarters:
                            break
                    except Exception:
                        continue

                if eps_quarters:
                    result["eps_quarters"] = eps_quarters
                    surprises = [q["surprise_pct"] for q in eps_quarters if q["surprise_pct"] is not None]
                    beats     = [q["beat"] for q in eps_quarters if q["beat"] is not None]
                    if surprises:
                        result["avg_eps_surprise_pct"] = round(sum(surprises) / len(surprises), 2)
                    if beats:
                        result["eps_beat_streak"] = sum(1 for b in beats if b)
                        result["eps_last_beat"]   = beats[0]   # most recent Q

                # ── Quarterly income statement ─────────────────────────
                try:
                    qi = tk.quarterly_income_stmt
                    if qi is not None and not qi.empty:
                        q_income: list[dict] = []
                        for col in list(qi.columns)[:4]:
                            rev = gp = ni = None
                            for row_label in qi.index:
                                rl = str(row_label).lower()
                                if rl in ("total revenue", "revenue"):
                                    rev = _safe_float(qi.loc[row_label, col])
                                elif "gross profit" in rl:
                                    gp  = _safe_float(qi.loc[row_label, col])
                                elif rl == "net income":
                                    ni  = _safe_float(qi.loc[row_label, col])
                            gm = round(gp / rev, 4) if (rev and gp and rev > 0) else None
                            q_income.append({
                                "date":         str(col)[:10],
                                "revenue":      rev,
                                "gross_profit": gp,
                                "gross_margin": gm,
                                "net_income":   ni,
                            })
                        result["quarterly_income"] = q_income

                        # Revenue YoY growth (latest Q vs same Q last year = index 4)
                        # We only have 4 quarters so compare Q1 vs Q4 as best proxy
                        if len(q_income) >= 2:
                            r0 = q_income[0].get("revenue")
                            r3 = q_income[-1].get("revenue")
                            if r0 and r3 and r3 > 0:
                                result["rev_growth_qyoy"] = round((r0 - r3) / r3, 4)

                        # Gross margin trend (expanding/stable/contracting)
                        margins = [q["gross_margin"] for q in q_income if q["gross_margin"] is not None]
                        if len(margins) >= 2:
                            trend = margins[0] - margins[-1]   # latest vs oldest in window
                            if   trend >  0.02:  result["gross_margin_trend"] = "expanding"
                            elif trend < -0.02:  result["gross_margin_trend"] = "contracting"
                            else:                result["gross_margin_trend"] = "stable"
                except Exception as e:
                    logger.debug(f"Quarterly income error {ticker}: {e}")

                # ── Quarterly cash flow ────────────────────────────────
                try:
                    qcf = tk.quarterly_cashflow
                    if qcf is not None and not qcf.empty:
                        q_cf: list[dict] = []
                        for col in list(qcf.columns)[:4]:
                            fcf = ocf = None
                            for row_label in qcf.index:
                                rl = str(row_label).lower()
                                if "free cash flow" in rl:
                                    fcf = _safe_float(qcf.loc[row_label, col])
                                elif "operating" in rl and "cash" in rl:
                                    ocf = _safe_float(qcf.loc[row_label, col])
                            q_cf.append({
                                "date":              str(col)[:10],
                                "fcf":               fcf,
                                "operating_cashflow": ocf,
                            })
                        result["quarterly_cashflow"] = q_cf
                        # Surface latest FCF/OCF so scorer can use it
                        if q_cf:
                            result["free_cashflow"]      = q_cf[0].get("fcf")
                            result["operating_cashflow"] = q_cf[0].get("operating_cashflow")
                except Exception as e:
                    logger.debug(f"Quarterly CF error {ticker}: {e}")

                # ── EPS estimate revisions (Zacks-style signal) ────────
                # yfinance exposes a `eps_revisions` DataFrame with rows per
                # period (0q, +1q, 0y, +1y) and columns counting analysts who
                # raised / lowered estimates over different windows.
                # We focus on the next quarter (+1q), 30-day window — the
                # most actionable + frequently-updated signal.
                try:
                    rev_df = getattr(tk, "eps_revisions", None)
                    est_df = getattr(tk, "earnings_estimate", None)
                    ups = downs = analysts = None
                    if rev_df is not None and not rev_df.empty and "+1q" in rev_df.index:
                        row = rev_df.loc["+1q"]
                        # Column names: upLast30days / downLast30days (newer yf)
                        # OR: upLast7days / upLast30days / downLast30days etc
                        ups   = _safe_float(row.get("upLast30days"))   or _safe_float(row.get("upLast30Days"))
                        downs = _safe_float(row.get("downLast30days")) or _safe_float(row.get("downLast30Days"))
                    if est_df is not None and not est_df.empty and "+1q" in est_df.index:
                        analysts = _safe_float(est_df.loc["+1q"].get("numberOfAnalysts"))
                    # Only persist if at least one number is real
                    if any(v is not None for v in (ups, downs, analysts)):
                        result["eps_revisions_30d"] = {
                            "ups":            int(ups) if ups is not None else 0,
                            "downs":          int(downs) if downs is not None else 0,
                            "total_analysts": int(analysts) if analysts else None,
                            "period":         "+1q",
                        }
                except Exception as e:
                    logger.debug(f"EPS revisions error {ticker}: {e}")

                return result

            except Exception as exc:
                logger.warning(f"get_quarterly_results error for {ticker}: {exc}")
                return {}

        try:
            result = await asyncio.to_thread(_fetch)
            if result:
                self.cache.set(key, result, config.CACHE_TECH_TTL)
            return result
        except Exception as exc:
            logger.warning(f"quarterly thread error for {ticker}: {exc}")
            return {}

    async def get_yf_enrichment(self, ticker: str) -> dict:
        """
        Fetch candle data + fundamentals from Yahoo Finance.
        Covers: RSI-14, ATR-14, momentum, 52w high/low, volume ratio,
                market cap, PE, PEG, revenue growth, profit margin,
                analyst targets (low/mean/high), beta, short %, description.
        Cached for CACHE_TECH_TTL (1 hour).
        """
        key    = f"yf:{ticker}:v5"   # v5: adds corporate actions (ex-dividend / splits)
        cached = self.cache.get(key)
        if cached is not None:
            return cached

        if not _YF_AVAILABLE:
            return {}

        yf_symbol = _YF_ALIAS.get(ticker, ticker)

        def _fetch():
            try:
                tk   = yf.Ticker(yf_symbol)
                # Try 6mo first; fall back to 3mo then 1mo for thinly-traded/new tickers
                hist = None
                for period in ("6mo", "3mo", "1mo"):
                    try:
                        h = tk.history(period=period, interval="1d", auto_adjust=True)
                        if not h.empty and len(h) >= 5:
                            hist = h
                            break
                    except Exception:
                        continue
                if hist is None or hist.empty:
                    return {}
                info = tk.info or {}

                if len(hist) < 5:
                    return {}

                closes = hist["Close"].tolist()
                highs  = hist["High"].tolist()
                lows   = hist["Low"].tolist()
                vols   = hist["Volume"].tolist()
                n      = len(closes)

                # ── Technical ──────────────────────────────────────────
                rsi     = _compute_rsi(closes)
                atr     = _compute_atr(highs, lows, closes)
                mom_1m  = round((closes[-1] / closes[-22] - 1) * 100, 2) if n >= 22 else None
                mom_3m  = round((closes[-1] / closes[-66] - 1) * 100, 2) if n >= 66 else None
                avg_vol = (sum(vols[-21:-1]) / 20) if n >= 21 else None
                vol_r   = round(vols[-1] / avg_vol, 2) if (avg_vol and avg_vol > 0 and vols) else None
                hi52    = max(highs[-252:]) if n >= 60 else max(highs)
                lo52    = min(lows[-252:])  if n >= 60 else min(lows)

                # ── Fundamentals from info ─────────────────────────────
                mc          = _safe_float(info.get("marketCap"))
                pe          = _safe_float(info.get("trailingPE") or info.get("forwardPE"))
                peg         = _safe_float(info.get("pegRatio"))
                beta        = _safe_float(info.get("beta"))
                rev_g       = _safe_float(info.get("revenueGrowth"))          # decimal e.g. 0.35
                eps_g       = _safe_float(info.get("earningsGrowth"))
                profit_m    = _safe_float(info.get("profitMargins"))          # decimal
                gross_m     = _safe_float(info.get("grossMargins"))
                short_pct   = _safe_float(info.get("shortPercentOfFloat"))    # decimal
                tgt_low     = _safe_float(info.get("targetLowPrice"))
                tgt_mean    = _safe_float(info.get("targetMeanPrice"))
                tgt_high    = _safe_float(info.get("targetHighPrice"))
                tgt_med     = _safe_float(info.get("targetMedianPrice"))
                analysts    = info.get("numberOfAnalystOpinions")
                name        = info.get("longName") or info.get("shortName") or ""
                sector      = info.get("sector") or ""
                industry    = info.get("industry") or ""
                exchange    = info.get("exchange") or ""
                desc        = (info.get("longBusinessSummary") or "")[:500]
                week52hi    = _safe_float(info.get("fiftyTwoWeekHigh")) or hi52
                week52lo    = _safe_float(info.get("fiftyTwoWeekLow"))  or lo52
                sma50       = _safe_float(info.get("fiftyDayAverage"))
                sma200      = _safe_float(info.get("twoHundredDayAverage"))

                # yfinance also exposes TTM cash flow and revenue
                yf_fcf     = _safe_float(info.get("freeCashflow"))
                yf_ocf     = _safe_float(info.get("operatingCashflow"))
                yf_rev_ttm = _safe_float(info.get("totalRevenue"))

                # ── Extended fundamentals (long-term lens) ─────────────
                fwd_pe      = _safe_float(info.get("forwardPE"))
                pb          = _safe_float(info.get("priceToBook"))
                ps          = _safe_float(info.get("priceToSalesTrailing12Months"))
                ev_ebitda   = _safe_float(info.get("enterpriseToEbitda"))
                op_m        = _safe_float(info.get("operatingMargins"))
                ebitda_m    = _safe_float(info.get("ebitdaMargins"))
                roe         = _safe_float(info.get("returnOnEquity"))
                roa         = _safe_float(info.get("returnOnAssets"))
                de_raw      = _safe_float(info.get("debtToEquity"))
                de          = (de_raw / 100.0) if (de_raw is not None and de_raw > 5) else de_raw
                curr_ratio  = _safe_float(info.get("currentRatio"))
                quick_ratio = _safe_float(info.get("quickRatio"))
                div_yield   = _safe_float(info.get("dividendYield"))
                payout      = _safe_float(info.get("payoutRatio"))
                inst_pct    = _safe_float(info.get("heldPercentInstitutions"))
                insider_pct = _safe_float(info.get("heldPercentInsiders"))

                return {
                    # Technical
                    "rsi_14":        rsi,
                    "atr_14":        atr,
                    "momentum_1m":   mom_1m,
                    "momentum_3m":   mom_3m,
                    "volume_ratio":  vol_r,
                    "high_52w":      week52hi,
                    "low_52w":       week52lo,
                    # Fundamentals
                    "name":          name,
                    "sector":        sector,
                    "industry":      industry,
                    "exchange":      exchange,
                    "market_cap":    mc,
                    "pe_ratio":      pe,
                    "peg_ratio":     peg,
                    "beta":          beta,
                    "revenue_growth_yoy": rev_g,
                    "revenue_ttm":   yf_rev_ttm,   # TTM revenue from yfinance
                    "eps_growth_yoy":     eps_g,
                    "profit_margin": profit_m,
                    "gross_margin":  gross_m,
                    "short_percent_float": short_pct,
                    "sma_50":        sma50,
                    "sma_200":       sma200,
                    # Cash flow (TTM from yfinance — reliable and free)
                    "free_cashflow":      yf_fcf,
                    "operating_cashflow": yf_ocf,
                    "fcf_margin": (
                        round(yf_fcf / yf_rev_ttm, 4)
                        if yf_fcf is not None and yf_rev_ttm and yf_rev_ttm > 0
                        else None
                    ),
                    # Analyst targets
                    "target_low":    tgt_low,
                    "target_mean":   tgt_mean or tgt_med,
                    "target_median": tgt_med,
                    "target_high":   tgt_high,
                    "total_analysts": analysts,
                    # Real analyst consensus (1=Strong Buy … 5=Strong Sell)
                    "analyst_recommendation": info.get("recommendationKey"),
                    "recommendation_mean":    _safe_float(info.get("recommendationMean")),
                    # Pre-market / after-hours snapshot (from the yfinance quote)
                    "market_state":           info.get("marketState"),
                    "pre_market_price":       _safe_float(info.get("preMarketPrice")),
                    "pre_market_change_pct":  _safe_float(info.get("preMarketChangePercent")),
                    "post_market_price":      _safe_float(info.get("postMarketPrice")),
                    "post_market_change_pct": _safe_float(info.get("postMarketChangePercent")),
                    "regular_close":          _safe_float(info.get("regularMarketPrice")),
                    # Extended fundamentals (long-term lens)
                    "forward_pe":            fwd_pe,
                    "pb_ratio":              pb,
                    "ps_ratio":              ps,
                    "ev_ebitda":             ev_ebitda,
                    "operating_margin":      op_m,
                    "ebitda_margin":         ebitda_m,
                    "roe":                   roe,
                    "roa":                   roa,
                    "debt_to_equity":        de,
                    "current_ratio":         curr_ratio,
                    "quick_ratio":           quick_ratio,
                    "dividend_yield":        div_yield,
                    "payout_ratio":          payout,
                    "held_pct_institutions": inst_pct,
                    "held_pct_insiders":     insider_pct,
                    # Corporate actions (dividends / splits) from the yfinance quote
                    "ex_dividend_date":      _yf_ts(info.get("exDividendDate")),
                    "dividend_rate":         _safe_float(info.get("dividendRate")),
                    "last_dividend_value":   _safe_float(info.get("lastDividendValue")),
                    "last_dividend_date":    _yf_ts(info.get("lastDividendDate")),
                    "last_split_factor":     info.get("lastSplitFactor"),
                    "last_split_date":       _yf_ts(info.get("lastSplitDate")),
                    # Description
                    "description":   desc,
                    # Earnings date from yfinance info
                    **_parse_yf_earnings(info),
                }
            except Exception as exc:
                logger.warning(f"yfinance enrichment error for {ticker}: {exc}")
                return {}

        # Yahoo throttles intermittently (empty result on an otherwise-valid
        # ticker). Retry a couple of times with a short backoff before giving
        # up to FMP — most "empty" cards are transient, not genuinely uncovered.
        for attempt in range(3):
            try:
                result = await asyncio.to_thread(_fetch)
                if result:
                    self.cache.set(key, result, config.CACHE_TECH_TTL)
                    return result
            except Exception as exc:
                logger.warning(f"yfinance thread error for {ticker} (attempt {attempt+1}/3): {exc}")
            if attempt < 2:
                await asyncio.sleep(0.6 * (attempt + 1))   # 0.6s, then 1.2s

        # ── FMP fallback: if yfinance totally failed and FMP is enabled ──
        if config.FMP_API_KEY:
            logger.info(f"yfinance empty for {ticker} — trying FMP fallback")
            fmp = await self.get_fmp_fundamentals(ticker)
            if fmp:
                self.cache.set(key, fmp, config.CACHE_TECH_TTL)
                return fmp
        return {}

    # ── Full merged ticker data ────────────────────────────────────────

    async def get_full_ticker(
        self,
        ticker: str,
        meta: dict = None,
        social_map: dict = None,
    ) -> dict:
        """
        Fetch + merge all data for one ticker.
        Yahoo Finance (yf:) is PRIMARY for candles + fundamentals.
        Finnhub candles/AV fundamentals serve as fallbacks if yf data absent.
        social_map is passed in from the batch call to avoid per-ticker API hits.
        """
        meta = meta or {}

        # ── Fetch / read cache ────────────────────────────────────────
        quote   = await self.get_quote(ticker)

        # Primary: Yahoo Finance enrichment (candles + fundamentals in one call)
        yf      = self.cache.get(f"yf:{ticker}:v5") or {}

        # Fallbacks: Finnhub candles, Alpha Vantage fundamentals
        candles = self.cache.get(f"candles:{ticker}") or {}
        fund    = self.cache.get(f"fund:{ticker}")     or {}

        rec      = self.cache.get(f"rec:{ticker}")      or {}
        insider  = self.cache.get(f"insider:{ticker}") or {}
        quarterly= self.cache.get(f"quarterly:{ticker}") or {}
        bidask   = self.cache.get(f"bidask:{ticker}")   or {}
        fmp_fund = self.cache.get(f"fmp_fund:{ticker}") or {}   # FMP fundamentals fallback

        # Social: use passed-in map first, fall back to cache
        if social_map is not None:
            social = social_map.get(ticker, {})
        else:
            social = (self.cache.get("ape:all") or {}).get(ticker, {})

        # FMP quote — checked only when needed (zero extra API calls if Finnhub works)
        fmp_q = self.cache.get(f"fmp_quote:{ticker}") or {}
        price = quote.get("price") or yf.get("price_latest") or candles.get("price_latest") or fmp_q.get("price")

        # ── Helpers: yf-first with fallbacks ──────────────────────────
        def _yf(field, *fallbacks):
            """Return first non-None value: yf → fallback dicts."""
            v = yf.get(field)
            if v is not None:
                return v
            for fb in fallbacks:
                if isinstance(fb, dict):
                    v = fb.get(field)
                    if v is not None:
                        return v
            return None

        # Analyst targets — yf has low/mean/high; AV only has single target_price
        target_mean = yf.get("target_mean") or fund.get("target_price")
        target_low  = yf.get("target_low")
        target_high = yf.get("target_high")

        # total_analysts: yf first, then rec (Finnhub recommendation count)
        total_analysts = yf.get("total_analysts") or rec.get("total_analysts")

        # ── Pre-compute FCF variables (need them in the dict below) ───
        _fcf = (quarterly.get("free_cashflow")
                or yf.get("free_cashflow")
                or fund.get("free_cashflow"))
        _ocf = (quarterly.get("operating_cashflow")
                or yf.get("operating_cashflow")
                or fund.get("operating_cashflow"))
        _rev_ttm = (yf.get("revenue_ttm")        # yfinance totalRevenue — most reliable
                    or fund.get("revenue_ttm"))   # AV RevenueTTM fallback

        # ── Merge ──────────────────────────────────────────────────────
        # Validate the day-change against price/prev_close so a stale or
        # split-broken provider quote (e.g. KLAC −89.5%) can't poison the
        # universe (movers, tape, stock card, scoring). When the % is dropped,
        # drop the matching absolute change too so the two never disagree.
        _clean_pct = _sanitize_change_pct(price, quote.get("prev_close"), quote.get("change_pct"))
        if _clean_pct is None:
            _clean_abs = None
        else:
            _pc = _safe_float(quote.get("prev_close"))
            _px = _safe_float(price)
            _clean_abs = round(_px - _pc, 2) if (_px is not None and _pc is not None) else quote.get("change_abs")

        merged = {
            **meta,
            "ticker":     ticker,

            # Live price fields (always from quote)
            "price":      price,
            "change_abs": _clean_abs,
            "change_pct": _clean_pct,
            "high":       quote.get("high"),
            "low":        quote.get("low"),
            "open":       quote.get("open"),
            "prev_close": quote.get("prev_close"),
            "volume":     quote.get("volume"),

            # Technical — yf primary, Finnhub candles fallback
            "rsi_14":       _yf("rsi_14",       candles),
            "atr_14":       _yf("atr_14",        candles),
            "momentum_1m":  _yf("momentum_1m",   candles),
            "momentum_3m":  _yf("momentum_3m",   candles),
            "volume_ratio": _yf("volume_ratio",  candles),
            "high_52w":     _yf("high_52w",      candles) or fund.get("week_52_high"),
            "low_52w":      _yf("low_52w",       candles) or fund.get("week_52_low"),
            "sma_50":       _yf("sma_50",        fund),
            "sma_200":      _yf("sma_200",       fund),

            # Analyst consensus
            "strong_buy_pct": rec.get("strong_buy_pct"),
            "total_analysts": total_analysts,

            # Fundamentals — yf primary, AV fund secondary, FMP fallback
            "name":               _yf("name")    or fund.get("name")     or fmp_fund.get("name")    or meta.get("name", ""),
            "sector":             _normalize_sector(_yf("sector")  or fund.get("sector")   or fmp_fund.get("sector")  or meta.get("sector", "")),
            "industry":           _yf("industry") or fund.get("industry") or fmp_fund.get("industry", ""),
            "exchange":           _yf("exchange") or fund.get("exchange") or fmp_fund.get("exchange") or meta.get("exchange", ""),
            "market_cap":         _yf("market_cap",         fund) or fmp_fund.get("market_cap"),
            "pe_ratio":           _yf("pe_ratio",           fund) or fmp_fund.get("pe_ratio"),
            "peg_ratio":          _yf("peg_ratio",          fund) or fmp_fund.get("peg_ratio"),
            "revenue_ttm":        _rev_ttm,
            "gross_margin":       _yf("gross_margin",        fund) or fmp_fund.get("gross_margin"),
            "profit_margin":      _yf("profit_margin",       fund) or fmp_fund.get("profit_margin"),
            "revenue_growth_yoy": _yf("revenue_growth_yoy",  fund) or fmp_fund.get("revenue_growth_yoy"),
            "eps_growth_yoy":     _yf("eps_growth_yoy",      fund),
            "beta":               _yf("beta",                fund) or fmp_fund.get("beta"),
            "short_percent_float":_yf("short_percent_float",  fund),
            "debt_to_equity":     _yf("debt_to_equity",       fund) or fund.get("debt_to_equity") or fmp_fund.get("debt_to_equity"),
            # Extended fundamentals (long-term lens)
            "forward_pe":            _yf("forward_pe"),
            "pb_ratio":              _yf("pb_ratio",  fund) or fmp_fund.get("pb_ratio"),
            "ps_ratio":              _yf("ps_ratio",  fund) or fmp_fund.get("ps_ratio"),
            "ev_ebitda":             _yf("ev_ebitda", fund) or fmp_fund.get("ev_ebitda"),
            "operating_margin":      _yf("operating_margin", fund),
            "ebitda_margin":         _yf("ebitda_margin", fund),
            "roe":                   _yf("roe",  fund) or fmp_fund.get("roe"),
            "roa":                   _yf("roa",  fund),
            "current_ratio":         _yf("current_ratio", fund) or fmp_fund.get("current_ratio"),
            "quick_ratio":           _yf("quick_ratio", fund),
            "dividend_yield":        _yf("dividend_yield", fund),
            "payout_ratio":          _yf("payout_ratio", fund),
            "held_pct_institutions": _yf("held_pct_institutions"),
            "held_pct_insiders":     _yf("held_pct_insiders"),
            "description":        _yf("description") or fund.get("description", "") or fmp_fund.get("description", ""),

            # Analyst price targets (yf has all three; AV only has mean)
            "target_price": target_mean,     # legacy field — same as mean
            "target_mean":  target_mean,
            "target_low":   target_low,
            "target_median": yf.get("target_median"),
            "target_high":  target_high,
            "analyst_recommendation": yf.get("analyst_recommendation"),
            "recommendation_mean":    yf.get("recommendation_mean"),
            "ex_dividend_date":       yf.get("ex_dividend_date"),
            "dividend_rate":          yf.get("dividend_rate"),
            "last_dividend_value":    yf.get("last_dividend_value"),
            "last_dividend_date":     yf.get("last_dividend_date"),
            "last_split_factor":      yf.get("last_split_factor"),
            "last_split_date":        yf.get("last_split_date"),

            # Pre-market / after-hours snapshot (yfinance quote, refreshed with
            # the fundamentals cycle — informational, not tick-by-tick).
            "market_state":           yf.get("market_state"),
            "pre_market_price":       yf.get("pre_market_price"),
            "pre_market_change_pct":  yf.get("pre_market_change_pct"),
            "post_market_price":      yf.get("post_market_price"),
            "post_market_change_pct": yf.get("post_market_change_pct"),
            "regular_close":          yf.get("regular_close"),

            # FCF / OCF — quarterly actuals → yfinance TTM → AV fallback
            "free_cashflow":      _fcf,
            "operating_cashflow": _ocf,
            # FCF margin = FCF ÷ Revenue TTM
            "fcf_margin": (
                round(_fcf / _rev_ttm, 4)
                if _fcf is not None and _rev_ttm and _rev_ttm > 0
                else yf.get("fcf_margin")   # pre-computed in yf enrichment
            ),


            # Order Book (Finnhub bid-ask)
            "bid":                bidask.get("bid"),
            "ask":                bidask.get("ask"),
            "bid_ask_spread":     bidask.get("bid_ask_spread"),
            "bid_ask_spread_pct": bidask.get("bid_ask_spread_pct"),

            # Quarterly earnings quality (yf primary, FMP fallback)
            "eps_quarters":        quarterly.get("eps_quarters")        or fmp_fund.get("eps_quarters",        []),
            "quarterly_income":    quarterly.get("quarterly_income")    or fmp_fund.get("quarterly_income",    []),
            "quarterly_cashflow":  quarterly.get("quarterly_cashflow",  []),
            "avg_eps_surprise_pct":quarterly.get("avg_eps_surprise_pct") or fmp_fund.get("avg_eps_surprise_pct"),
            "eps_beat_streak":     quarterly.get("eps_beat_streak")     or fmp_fund.get("eps_beat_streak"),
            "eps_last_beat":       quarterly.get("eps_last_beat")       or fmp_fund.get("eps_last_beat"),
            "rev_growth_qyoy":     quarterly.get("rev_growth_qyoy")    or fmp_fund.get("rev_growth_qyoy"),
            "gross_margin_trend":  quarterly.get("gross_margin_trend")  or fmp_fund.get("gross_margin_trend"),
            "eps_last_actual":     (quarterly.get("eps_quarters") or fmp_fund.get("eps_quarters") or [{}])[0].get("actual"),
            "eps_revisions_30d":   quarterly.get("eps_revisions_30d"),

            # Social
            "mention_velocity":   social.get("mention_velocity"),
            "mentions_24h":       social.get("mentions_24h"),

            # Earnings
            "days_to_earnings":   candles.get("days_to_earnings") or self.cache.get(f"earnings:{ticker}"),
            "earnings_date":      candles.get("earnings_date"),
            "earnings_just_reported": candles.get("earnings_just_reported", False),
            "last_earnings_date":     candles.get("last_earnings_date"),
            "days_since_earnings":    candles.get("days_since_earnings"),

            # Insider (SEC + yfinance fallback)
            "insider_buys_90d":   insider.get("insider_buys_90d", 0),
            "insider_sells_90d":  insider.get("insider_sells_90d", 0),
            "insider_detail":     insider.get("insider_detail", []),

            # News
            "news":               self.cache.get(f"news:{ticker}") or [],
        }

        # Derived fields
        high_52w = merged.get("high_52w")
        if price and high_52w and high_52w > 0:
            merged["dist_52w_high"] = round((price - high_52w) / high_52w * 100, 2)
        else:
            merged["dist_52w_high"] = None

        # ── Fallback: detect "just reported" from FMP eps_quarters[0] date ──
        # yfinance often returns ONLY the next earnings date, not the most recent.
        # When that happens, our _parse_yf_earnings can't flag earnings_just_reported.
        # FMP's earnings-surprises endpoint returns the latest-quarter date though
        # (in YYYY-MM format), so we use that as a fallback signal.
        if not merged.get("earnings_just_reported"):
            eps_q = merged.get("eps_quarters") or []
            if eps_q and isinstance(eps_q[0], dict) and eps_q[0].get("date"):
                try:
                    from datetime import date as _date2
                    edate_str = str(eps_q[0]["date"]).strip()
                    # FMP returns either YYYY-MM (e.g. "2026-04") or YYYY-MM-DD.
                    # Treat YYYY-MM as mid-month for the purpose of "days since".
                    if len(edate_str) == 7:
                        edate_str = edate_str + "-15"
                    edate = _date2.fromisoformat(edate_str[:10])
                    days_since = (_date2.today() - edate).days
                    # Window: 0 to 21 days post-earnings shows the card.
                    # 21 (not 14) accounts for FMP's mid-month approximation.
                    if 0 <= days_since <= 21:
                        merged["earnings_just_reported"] = True
                        merged["last_earnings_date"]     = edate.isoformat()
                        merged["days_since_earnings"]    = days_since
                except Exception:
                    pass

        t_mean = merged.get("target_mean") or merged.get("target_price")
        if price and t_mean and price > 0:
            merged["target_upside_pct"] = round((t_mean - price) / price * 100, 1)
        else:
            merged["target_upside_pct"] = None

        return merged

    # Batch helpers

    async def refresh_quotes_batch(self, tickers: list[str]) -> dict[str, dict]:
        results = {}
        for tk in tickers:
            q = await self.get_quote(tk)
            if q.get("price"):
                results[tk] = q
        return results

    async def refresh_candles_batch(self, tickers: list[str]) -> None:
        for tk in tickers:
            await self.get_candles(tk)

    async def refresh_recommendations_batch(self, tickers: list[str]) -> None:
        for tk in tickers:
            await self.get_recommendation(tk)

    async def refresh_earnings_batch(self, tickers: list[str]) -> None:
        for tk in tickers:
            await self.get_earnings_date(tk)

    async def refresh_yf_batch(self, tickers: list[str]) -> None:
        """Pre-fetch Yahoo Finance enrichment for all tickers."""
        for tk in tickers:
            try:
                await self.get_yf_enrichment(tk)
            except Exception as exc:
                logger.warning(f"yf batch skip {tk}: {exc}")
