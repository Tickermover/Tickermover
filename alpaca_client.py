"""
AlphaHunt — Alpaca Markets Data Client  |  alphahunt.in
FREE replacement for Polygon.io Starter ($29/mo).

REST endpoints used:
  GET /v2/stocks/snapshots           — batch quotes (all tickers in 1 call)
  GET /v2/stocks/{sym}/bars          — OHLCV history → RSI, ATR, momentum
  GET /v2/stocks/{sym}/trades/latest — latest trade price (single ticker)

WebSocket (real-time, also FREE on IEX feed):
  wss://stream.data.alpaca.markets/v2/iex  — live trade ticks

Setup (2 minutes, no credit card):
  1. Go to https://alpaca.markets → Sign Up
  2. Create a Paper Trading account (no real money needed)
  3. Go to Paper Trading → API Keys → Generate
  4. Copy Key ID + Secret Key into config.py or .env

Free tier covers:
  - Unlimited REST calls
  - Real-time IEX feed (WebSocket)
  - 6+ years historical OHLCV data
  - ~180 stocks we track are all on IEX

Interface is identical to PolygonClient so it's a drop-in replacement.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_REST = "https://data.alpaca.markets"
_WS   = "wss://stream.data.alpaca.markets/v2/iex"   # free IEX feed


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


# ── AlpacaClient ─────────────────────────────────────────────────────────────

class AlpacaClient:
    """
    Async Alpaca Markets Data API v2 client.
    Free IEX feed — unlimited REST calls + real-time WebSocket.
    Drop-in replacement for PolygonClient.
    """

    def __init__(self, key_id: str, secret_key: str):
        self.key_id     = key_id
        self.secret_key = secret_key
        self.enabled    = bool(key_id and secret_key)
        self._headers   = {
            "APCA-API-KEY-ID":     key_id,
            "APCA-API-SECRET-KEY": secret_key,
        } if self.enabled else {}

        # WebSocket state
        self._ws_task:        Optional[asyncio.Task] = None
        self._ws_prices:      Dict[str, dict]        = {}
        self._ws_subscribers: List[asyncio.Queue]    = []

    # ── REST core ─────────────────────────────────────────────────────────────

    async def _get(self, path: str, params: Optional[dict] = None) -> Optional[Any]:
        if not self.enabled:
            return None
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
                r = await c.get(
                    f"{_REST}{path}",
                    params=params or {},
                    headers=self._headers,
                )
                if r.status_code == 401:
                    logger.warning("Alpaca: 401 Unauthorized — check API key/secret")
                    return None
                if r.status_code == 429:
                    logger.warning("Alpaca: 429 rate-limited — backing off 2s")
                    await asyncio.sleep(2)
                    return None
                r.raise_for_status()
                return r.json()
        except Exception as e:
            logger.warning(f"Alpaca {path}: {e}")
            return None

    # ── Batch snapshot — all tickers in ONE call ──────────────────────────────

    async def get_snapshots_batch(self, tickers: List[str]) -> Dict[str, dict]:
        """
        Fetch quotes for up to 1000 tickers in a single API call.
        Returns {ticker: quote_dict, ...}
        Equivalent to PolygonClient.get_snapshots_batch()
        """
        if not tickers:
            return {}

        # Alpaca allows all tickers in one call (no 250 limit like Polygon)
        chunks = [tickers[i:i + 1000] for i in range(0, len(tickers), 1000)]
        result: Dict[str, dict] = {}

        for chunk in chunks:
            data = await self._get(
                "/v2/stocks/snapshots",
                {"symbols": ",".join(chunk), "feed": "iex"}
            )
            if not data or not isinstance(data, dict):
                continue

            for sym, snap in data.items():
                daily  = snap.get("dailyBar")     or {}
                prev   = snap.get("prevDailyBar") or {}
                trade  = snap.get("latestTrade")  or {}
                quote  = snap.get("latestQuote")  or {}

                # Price: latest trade → daily close → bid
                price  = _sf(trade.get("p")) or _sf(daily.get("c"))
                prev_c = _sf(prev.get("c"))
                chg_abs = round(price - prev_c, 4) if (price and prev_c) else None
                chg_pct = round((chg_abs / prev_c) * 100, 2) if (chg_abs and prev_c) else None

                result[sym] = {
                    "price":      price,
                    "change_abs": chg_abs,
                    "change_pct": chg_pct,
                    "high":       _sf(daily.get("h")),
                    "low":        _sf(daily.get("l")),
                    "open":       _sf(daily.get("o")),
                    "prev_close": prev_c,
                    "volume":     _sf(daily.get("v")),
                    "vwap":       _sf(daily.get("vw")),
                    "bid":        _sf(quote.get("bp")),
                    "ask":        _sf(quote.get("ap")),
                }

        return result

    # ── Single quote ──────────────────────────────────────────────────────────

    async def get_quote(self, ticker: str) -> dict:
        """
        Returns {price, change_pct, change_abs, high, low, open, prev_close, volume}.
        Uses snapshot endpoint for one ticker.
        """
        # Check live WebSocket cache first
        if ticker in self._ws_prices:
            return self._ws_prices[ticker]

        result = await self.get_snapshots_batch([ticker])
        return result.get(ticker, {})

    # ── OHLCV bars → technicals ───────────────────────────────────────────────

    async def get_candles(self, ticker: str, days: int = 130) -> dict:
        """
        Returns RSI-14, ATR-14, momentum 1m/3m/6m, dist_52w_high, volume_ratio.
        Uses /v2/stocks/{sym}/bars with 1Day timeframe.
        """
        to_dt   = date.today()
        from_dt = to_dt - timedelta(days=days + 60)   # extra buffer for weekends/holidays

        # Alpaca uses RFC3339 dates
        params = {
            "timeframe":  "1Day",
            "start":      from_dt.isoformat(),
            "end":        to_dt.isoformat(),
            "limit":      1000,
            "adjustment": "split",   # split-adjusted closes
            "feed":       "iex",
            "sort":       "asc",
        }
        data = await self._get(f"/v2/stocks/{ticker}/bars", params)
        if not data:
            return {}

        bars = data.get("bars") or []
        if len(bars) < 5:
            return {}

        closes = [b["c"] for b in bars]
        highs  = [b["h"] for b in bars]
        lows   = [b["l"] for b in bars]
        vols   = [b["v"] for b in bars]
        n      = len(closes)

        hi52 = max(highs[-252:]) if n >= 252 else max(highs)
        lo52 = min(lows[-252:])  if n >= 252 else min(lows)
        price = closes[-1]

        avg_vol      = (sum(vols[-21:-1]) / 20) if n >= 21 else None
        volume_ratio = round(vols[-1] / avg_vol, 2) if (avg_vol and avg_vol > 0) else None

        return {
            "rsi_14":        _compute_rsi(closes),
            "atr_14":        _compute_atr(highs, lows, closes),
            "momentum_1m":   round((closes[-1] / closes[-22] - 1), 4) if n >= 22  else None,
            "momentum_3m":   round((closes[-1] / closes[-66] - 1), 4) if n >= 66  else None,
            "momentum_6m":   round((closes[-1] / closes[-130] - 1), 4) if n >= 130 else None,
            "dist_52w_high": round((hi52 - price) / hi52, 4) if hi52 else None,
            "dist_52w_low":  round((price - lo52) / lo52, 4) if lo52 else None,
            "week_52_high":  hi52,
            "week_52_low":   lo52,
            "volume_ratio":  volume_ratio,
            "avg_volume_20": round(avg_vol) if avg_vol else None,
            "closes":        closes[-66:],   # last 66 for mini-chart
        }

    async def get_candles_raw(self, ticker: str, days: int = 130) -> dict:
        """
        Returns the full daily OHLCV array (for client-side charting + price-action
        analysis), trimmed to the most recent `days` trading sessions, plus a few
        key levels. Same upstream call as get_candles() — we just keep the bars.
        """
        to_dt   = date.today()
        from_dt = to_dt - timedelta(days=days + 60)
        params = {
            "timeframe":  "1Day",
            "start":      from_dt.isoformat(),
            "end":        to_dt.isoformat(),
            "limit":      1000,
            "adjustment": "split",
            "feed":       "iex",
            "sort":       "asc",
        }
        data = await self._get(f"/v2/stocks/{ticker}/bars", params)
        if not data:
            return {}
        bars = data.get("bars") or []
        if len(bars) < 5:
            return {}

        highs = [b["h"] for b in bars]
        lows  = [b["l"] for b in bars]
        closes = [b["c"] for b in bars]
        n = len(bars)
        hi52 = max(highs[-252:]) if n >= 252 else max(highs)
        lo52 = min(lows[-252:])  if n >= 252 else min(lows)

        candles = [{
            "t": (b.get("t") or "")[:10],
            "o": round(_sf(b["o"]) or 0, 4),
            "h": round(_sf(b["h"]) or 0, 4),
            "l": round(_sf(b["l"]) or 0, 4),
            "c": round(_sf(b["c"]) or 0, 4),
            "v": int(b.get("v") or 0),
        } for b in bars[-days:]]

        return {
            "ticker":       ticker,
            "candles":      candles,
            "week_52_high": round(hi52, 4) if hi52 else None,
            "week_52_low":  round(lo52, 4) if lo52 else None,
            "atr_14":       _compute_atr(highs, lows, closes),
        }

    # ── WebSocket real-time stream (FREE IEX feed) ────────────────────────────

    async def start_ws_stream(self, tickers: List[str]) -> None:
        """
        Connect to Alpaca IEX WebSocket stream for real-time trade ticks.
        Completely FREE — no plan upgrade needed.
        Call once at startup; runs forever in background.
        """
        if not self.enabled:
            return
        if self._ws_task and not self._ws_task.done():
            return
        self._ws_task = asyncio.create_task(self._ws_loop(tickers))
        logger.info(f"Alpaca WS stream started for {len(tickers)} tickers (IEX feed — free)")

    async def _ws_loop(self, tickers: List[str]) -> None:
        """Reconnecting WebSocket loop — handles auth, subscription, and trade ticks."""
        while True:
            try:
                import websockets
                async with websockets.connect(_WS, ping_interval=20) as ws:
                    # 1 — wait for connected message
                    msg = json.loads(await ws.recv())
                    if not any(m.get("T") == "success" and m.get("msg") == "connected"
                               for m in (msg if isinstance(msg, list) else [msg])):
                        logger.warning(f"Alpaca WS unexpected connect response: {msg}")

                    # 2 — authenticate
                    await ws.send(json.dumps({
                        "action": "auth",
                        "key":    self.key_id,
                        "secret": self.secret_key,
                    }))
                    auth_resp = json.loads(await ws.recv())
                    if not any(m.get("T") == "success" and m.get("msg") == "authenticated"
                               for m in (auth_resp if isinstance(auth_resp, list) else [auth_resp])):
                        logger.warning(f"Alpaca WS auth failed: {auth_resp}")
                        return   # bad key — don't retry

                    # 3 — subscribe to trades
                    await ws.send(json.dumps({
                        "action": "subscribe",
                        "trades": tickers,
                    }))
                    logger.info("Alpaca WS authenticated + subscribed")

                    # 4 — stream messages
                    async for raw in ws:
                        msgs = json.loads(raw)
                        if not isinstance(msgs, list):
                            msgs = [msgs]
                        for m in msgs:
                            if m.get("T") == "t":   # trade tick
                                sym   = m.get("S")
                                price = _sf(m.get("p"))
                                if sym and price:
                                    prev  = self._ws_prices.get(sym, {}).get("prev_close")
                                    chg   = round((price - prev) / prev * 100, 2) if prev else None
                                    self._ws_prices[sym] = {
                                        "price":      price,
                                        "change_pct": chg,
                                        "volume":     _sf(m.get("s")),
                                        "ts":         m.get("t"),
                                    }
                                    update = {"ticker": sym, "price": price, "change_pct": chg}
                                    for q in self._ws_subscribers:
                                        try:
                                            q.put_nowait(update)
                                        except asyncio.QueueFull:
                                            pass

            except Exception as e:
                logger.warning(f"Alpaca WS disconnected: {e} — reconnecting in 5s")
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
