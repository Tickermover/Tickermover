"""
TickerMover — Polygon.io Data Client
Replaces yfinance + Finnhub for price/candle/ticker data.

REST endpoints used:
  /v2/aggs/ticker/{sym}/range/1/day  — OHLCV history (RSI, ATR, momentum)
  /v2/last/trade/{sym}               — latest trade price
  /v3/reference/tickers/{sym}        — name, sector, market cap
  /v1/indicators/rsi/{sym}           — server-side RSI (optional)

WebSocket (real-time plan only):
  wss://socket.polygon.io/stocks      — live trade/quote ticks

Free tier:   5 req/min  — good for dev / testing
Starter:    $29/mo, unlimited calls, 15-min delayed data
Realtime:   $79/mo, unlimited calls, true real-time + WebSocket

Set POLYGON_API_KEY in config.py or leave "" to skip (falls back to yfinance).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.polygon.io"
_WS   = "wss://socket.polygon.io/stocks"


# ── helpers ──────────────────────────────────────────────────────────────────

def _sf(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    except (TypeError, ValueError):
        return default


def _compute_rsi(closes: list, period: int = 14) -> Optional[float]:
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


# ── PolygonClient ─────────────────────────────────────────────────────────────

class PolygonClient:
    """
    Async Polygon.io REST + WebSocket client.
    One shared instance; wire into DataCoordinator.
    """

    def __init__(self, api_key: str):
        self.api_key   = api_key
        self.enabled   = bool(api_key)
        self._limiter  = _RateLimiter(calls_per_minute=300)   # Starter = unlimited; cap at 300 to be safe
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_prices: Dict[str, dict] = {}   # ticker → {price, change_pct, volume, ts}
        self._ws_subscribers: List[asyncio.Queue] = []

    # ── REST core ─────────────────────────────────────────────────────────────

    async def _get(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        if not self.enabled:
            return None
        await self._limiter.wait()
        p = {"apiKey": self.api_key, **(params or {})}
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(f"{_BASE}{path}", params=p)
                if r.status_code == 403:
                    logger.warning("Polygon: 403 Forbidden — check API key or plan tier")
                    return None
                if r.status_code == 429:
                    logger.warning("Polygon: 429 rate-limited — backing off 2 s")
                    await asyncio.sleep(2)
                    return None
                r.raise_for_status()
                return r.json()
        except Exception as e:
            logger.warning(f"Polygon {path}: {e}")
            return None

    # ── Quote (last trade price) ──────────────────────────────────────────────

    async def get_quote(self, ticker: str) -> dict:
        """
        Returns {price, change_pct, change_abs, high, low, open, prev_close, volume}.
        Uses /v2/snapshot/locale/us/markets/stocks/tickers/{sym} — one call, all fields.
        """
        # Check live WebSocket cache first (real-time plan)
        if ticker in self._ws_prices:
            return self._ws_prices[ticker]

        data = await self._get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}")
        if not data or data.get("status") == "ERROR":
            return {}

        snap = data.get("ticker", {})
        day  = snap.get("day", {})
        prev = snap.get("prevDay", {})
        lp   = snap.get("lastTrade", {}) or snap.get("lastQuote", {})

        price     = _sf(lp.get("p")) or _sf(day.get("c"))
        prev_c    = _sf(prev.get("c"))
        chg_abs   = round(price - prev_c, 4) if price and prev_c else None
        chg_pct   = round((chg_abs / prev_c) * 100, 2) if chg_abs and prev_c else None

        return {
            "price":      price,
            "change_abs": chg_abs,
            "change_pct": chg_pct,
            "high":       _sf(day.get("h")),
            "low":        _sf(day.get("l")),
            "open":       _sf(day.get("o")),
            "prev_close": prev_c,
            "volume":     _sf(day.get("v")),
            "vwap":       _sf(day.get("vw")),
        }

    # ── OHLCV aggregates → technicals ─────────────────────────────────────────

    async def get_candles(self, ticker: str, days: int = 130) -> dict:
        """
        Returns RSI-14, ATR-14, momentum 1m/3m, dist_52w_high, volume_ratio.
        Uses /v2/aggs/ticker/{sym}/range/1/day/{from}/{to}
        """
        to_dt   = date.today()
        from_dt = to_dt - timedelta(days=days + 30)   # buffer for weekends/holidays
        path    = f"/v2/aggs/ticker/{ticker}/range/1/day/{from_dt}/{to_dt}"
        data    = await self._get(path, {"adjusted": "true", "sort": "asc", "limit": 200})

        if not data or data.get("resultsCount", 0) == 0:
            return {}

        bars    = data.get("results", [])
        closes  = [b["c"] for b in bars]
        highs   = [b["h"] for b in bars]
        lows    = [b["l"] for b in bars]
        vols    = [b["v"] for b in bars]
        n       = len(closes)
        if n < 5:
            return {}

        hi52    = max(highs[-252:]) if n >= 252 else max(highs)
        lo52    = min(lows[-252:])  if n >= 252 else min(lows)
        price   = closes[-1]

        avg_vol      = (sum(vols[-21:-1]) / 20) if n >= 21 else None
        volume_ratio = round(vols[-1] / avg_vol, 2) if (avg_vol and avg_vol > 0) else None

        return {
            "rsi_14":       _compute_rsi(closes),
            "atr_14":       _compute_atr(highs, lows, closes),
            "momentum_1m":  round((closes[-1] / closes[-22] - 1), 4) if n >= 22 else None,
            "momentum_3m":  round((closes[-1] / closes[-66] - 1), 4) if n >= 66 else None,
            "momentum_6m":  round((closes[-1] / closes[-130] - 1), 4) if n >= 130 else None,
            "dist_52w_high": round((hi52 - price) / hi52, 4) if hi52 else None,
            "dist_52w_low":  round((price - lo52) / lo52, 4) if lo52 else None,
            "week_52_high":  hi52,
            "week_52_low":   lo52,
            "volume_ratio":  volume_ratio,
            "avg_volume_20": round(avg_vol) if avg_vol else None,
            "closes":        closes[-66:],   # last 66 for mini-chart
        }

    # ── Ticker details (name, sector, market cap) ─────────────────────────────

    async def get_ticker_details(self, ticker: str) -> dict:
        """
        Returns name, sector, industry, market_cap, shares_outstanding.
        Uses /v3/reference/tickers/{sym}
        """
        data = await self._get(f"/v3/reference/tickers/{ticker}")
        if not data or "results" not in data:
            return {}

        r   = data["results"]
        mc  = _sf(r.get("market_cap"))
        sic = r.get("sic_description") or ""

        return {
            "name":               r.get("name"),
            "sector":             r.get("sector") or sic,
            "industry":           r.get("sic_description"),
            "market_cap":         mc,
            "shares_outstanding": _sf(r.get("share_class_shares_outstanding")),
            "country":            r.get("locale", "").upper(),
            "currency":           r.get("currency_name", "usd").upper(),
            "description":        r.get("description", "")[:300],
            "homepage":           r.get("homepage_url"),
            "employees":          r.get("total_employees"),
        }

    # ── Batch snapshots — all tickers in one call ─────────────────────────────

    async def get_snapshots_batch(self, tickers: List[str]) -> Dict[str, dict]:
        """
        Fetch quotes for up to 250 tickers in a single API call.
        Returns {ticker: quote_dict, ...}
        Uses /v2/snapshot/locale/us/markets/stocks/tickers?tickers=A,B,C
        """
        if not tickers:
            return {}

        # Polygon accepts up to 250 tickers per call
        chunks  = [tickers[i:i+250] for i in range(0, len(tickers), 250)]
        result  = {}

        for chunk in chunks:
            data = await self._get(
                "/v2/snapshot/locale/us/markets/stocks/tickers",
                {"tickers": ",".join(chunk)}
            )
            if not data or "tickers" not in data:
                continue
            for snap in data["tickers"]:
                sym  = snap.get("ticker")
                day  = snap.get("day", {})
                prev = snap.get("prevDay", {})
                lp   = snap.get("lastTrade", {}) or snap.get("lastQuote", {})
                price    = _sf(lp.get("p")) or _sf(day.get("c"))
                prev_c   = _sf(prev.get("c"))
                chg_abs  = round(price - prev_c, 4) if (price and prev_c) else None
                chg_pct  = round((chg_abs / prev_c) * 100, 2) if (chg_abs and prev_c) else None
                result[sym] = {
                    "price":      price,
                    "change_abs": chg_abs,
                    "change_pct": chg_pct,
                    "high":       _sf(day.get("h")),
                    "low":        _sf(day.get("l")),
                    "open":       _sf(day.get("o")),
                    "prev_close": prev_c,
                    "volume":     _sf(day.get("v")),
                    "vwap":       _sf(day.get("vw")),
                }

        return result

    # ── WebSocket real-time stream ─────────────────────────────────────────────

    async def start_ws_stream(self, tickers: List[str]) -> None:
        """
        Connect to Polygon WebSocket and stream real-time trade ticks.
        Requires real-time plan ($79/mo). Silently skips on free/starter plan.
        Call this once from FastAPI startup; it runs forever in the background.
        """
        if not self.enabled:
            return
        if self._ws_task and not self._ws_task.done():
            return
        self._ws_task = asyncio.create_task(self._ws_loop(tickers))
        logger.info(f"Polygon WS stream started for {len(tickers)} tickers")

    async def _ws_loop(self, tickers: List[str]) -> None:
        sub_msg = json.dumps({"action": "subscribe", "params": ",".join(f"T.{t}" for t in tickers)})
        auth_msg = json.dumps({"action": "auth", "params": self.api_key})

        while True:
            try:
                import websockets
                async with websockets.connect(_WS, ping_interval=30) as ws:
                    # Auth
                    await ws.send(auth_msg)
                    resp = json.loads(await ws.recv())
                    if any(m.get("status") not in ("auth_success", "connected") for m in resp):
                        logger.warning(f"Polygon WS auth failed: {resp} — real-time plan required")
                        return   # don't retry — not on right plan

                    # Subscribe
                    await ws.send(sub_msg)
                    logger.info("Polygon WS subscribed")

                    async for raw in ws:
                        msgs = json.loads(raw)
                        for m in msgs:
                            if m.get("ev") == "T":   # trade tick
                                ticker = m.get("sym")
                                price  = _sf(m.get("p"))
                                if ticker and price:
                                    prev = self._ws_prices.get(ticker, {}).get("prev_close")
                                    chg  = round((price - prev) / prev * 100, 2) if prev else None
                                    self._ws_prices[ticker] = {
                                        "price":      price,
                                        "change_pct": chg,
                                        "volume":     _sf(m.get("s")),
                                        "ts":         m.get("t"),
                                    }
                                    # Push to all subscribed FastAPI WS clients
                                    update = {"ticker": ticker, "price": price, "change_pct": chg}
                                    for q in self._ws_subscribers:
                                        await q.put(update)

            except Exception as e:
                logger.warning(f"Polygon WS disconnected: {e} — reconnecting in 5 s")
                await asyncio.sleep(5)

    def subscribe_ws(self) -> asyncio.Queue:
        """Return a queue that receives live price updates."""
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._ws_subscribers.append(q)
        return q

    def unsubscribe_ws(self, q: asyncio.Queue) -> None:
        try:
            self._ws_subscribers.remove(q)
        except ValueError:
            pass

    def stop_ws(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()


# ── Simple rate limiter ───────────────────────────────────────────────────────

class _RateLimiter:
    def __init__(self, calls_per_minute: int):
        self._interval = 60.0 / max(calls_per_minute, 1)
        self._last     = 0.0
        self._lock: Optional[asyncio.Lock] = None

    async def wait(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            elapsed = time.monotonic() - self._last
            if elapsed < self._interval:
                await asyncio.sleep(self._interval - elapsed)
            self._last = time.monotonic()
