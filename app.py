"""
AlphaHunt — FastAPI Application
alphahunt.in  |  Hunt for Alpha
Run:  uvicorn app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations
import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from auth import SupabaseClient
from billing import RazorpayClient, is_pro
from data_coordinator import DataCoordinator
from ai_scorer import score_and_rank, compute_pop_score
from stock_universe import get_universe, get_meta
from intelligence import (
    MarketRegime,
    ScoreHistory,
    ThesisGenerator,
    attach_score_history,
)

# ── Logging ───────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
DASHBOARD_HTML = BASE_DIR / "templates" / "dashboard.html"
LANDING_HTML   = BASE_DIR / "templates" / "landing.html"


# ── NaN/Inf sanitiser — Python json.dumps crashes on NaN/Infinity ─────
def _clean(obj: Any) -> Any:
    """Recursively replace NaN/Inf floats with None so JSONResponse never crashes."""
    if isinstance(obj, float):
        return None if (math.isnan(obj) or math.isinf(obj)) else obj
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj

# ── Global state ──────────────────────────────────────────────────────
coordinator = DataCoordinator()
cache       = coordinator.cache

# ── Intelligence layer singletons ────────────────────────────────────
# All three share the existing SmartCache so they survive Railway restarts
# and add no new infrastructure.  market_regime is refreshed on a 30-min
# cadence (see _regime_refresh below); score_history accumulates on every
# universe re-score; thesis_gen is stateless and reads from the other two.
# Intelligence singletons - refreshed on 30-min cycle by _regime_refresh
market_regime  = MarketRegime(cache)
score_history  = ScoreHistory(cache)
thesis_gen     = ThesisGenerator(market_regime, score_history)
supabase    = SupabaseClient(
    url        = config.SUPABASE_URL,
    anon_key   = config.SUPABASE_ANON_KEY,
    jwt_secret = config.SUPABASE_JWT_SECRET,
)
razorpay    = RazorpayClient(
    key_id         = config.RAZORPAY_KEY_ID,
    key_secret     = config.RAZORPAY_KEY_SECRET,
    webhook_secret = config.RAZORPAY_WEBHOOK_SECRET,
)

# Cached Razorpay plan_id (fetched once at startup)
_razorpay_plan_id: Optional[str] = None

# ── Auth helpers ───────────────────────────────────────────────────────
_bearer = HTTPBearer(auto_error=False)

async def _current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)
) -> Optional[dict]:
    """
    Extract user from Bearer token (JWT-verified locally).
    Returns {user_id, email, role} or None if unauthenticated.
    Endpoints that require auth should raise 401 when this returns None.
    """
    if not creds:
        return None
    return supabase.verify_token(creds.credentials)

_universe_data:     list[dict] = []
_last_full_refresh: float      = 0.0
_daily_hot: list = []
_daily_hot_date: str = ""
_model_portfolio:   dict = {}   # persisted model watchlist with entry prices
_refresh_lock:      asyncio.Lock | None = None   # created inside lifespan


# ─────────────────────────────────────────────────────────────────────
# Background tasks
# ─────────────────────────────────────────────────────────────────────

async def _full_refresh() -> None:
    """
    TRUE FAST PASS — prices + social only, NO per-ticker API calls.
    1. Batch-fetches ALL prices in one Alpaca call  (<2 s)
    2. Immediately sets _universe_data so dashboard shows data in <10 s
    3. Preserves existing fundamentals for tickers already in universe
    Fundamentals are loaded progressively by _yf_concurrent_load().
    """
    global _universe_data, _last_full_refresh
    async with _refresh_lock:
        try:
            tickers = get_universe(config.UNIVERSE_MODE)
            logger.info(f"Fast refresh starting — {len(tickers)} tickers")
            t0 = time.time()

            # ── Social (one call) ──────────────────────────────────────
            social_map = await coordinator.get_social_all()

            # ── Batch price snapshot (ONE call for ALL tickers) ────────
            batch_quotes: dict = {}
            market = coordinator._market
            if market is not None:
                source = "Alpaca" if market is coordinator.alpaca else "Polygon"
                try:
                    batch_quotes = await market.get_snapshots_batch(tickers)
                    logger.info(f"{source} batch: {len(batch_quotes)} quotes in 1 call")
                    for sym, q in batch_quotes.items():
                        if q.get("price"):
                            coordinator.cache.set(f"quote:{sym}", {"ticker": sym, **q}, config.CACHE_LIVE_TTL)
                except Exception as e:
                    logger.warning(f"{source} batch failed: {e}")

            # ── Build universe immediately from prices — NO per-ticker API calls ──
            existing: dict[str, dict] = {
                item["ticker"]: item for item in _universe_data if item.get("ticker")
            }
            new_universe: list[dict] = []

            for sym in tickers:
                q      = batch_quotes.get(sym) or coordinator.cache.get(f"quote:{sym}") or {}
                price  = q.get("price")
                if not price:
                    continue
                social = social_map.get(sym, {})
                meta   = get_meta(sym)

                if sym in existing:
                    # Refresh price fields; keep all existing fundamentals intact
                    existing[sym].update({
                        "price":            price,
                        "change_pct":       q.get("change_pct"),
                        "change_abs":       q.get("change_abs"),
                        "high":             q.get("high"),
                        "low":              q.get("low"),
                        "volume":           q.get("volume"),
                        "vwap":             q.get("vwap"),
                        "prev_close":       q.get("prev_close"),
                        "mention_velocity": social.get("mention_velocity"),
                        "mentions_24h":     social.get("mentions_24h"),
                    })
                    new_universe.append(existing[sym])
                else:
                    # First time: create entry with prices + meta + social
                    new_universe.append({
                        "ticker":           sym,
                        "price":            price,
                        "change_pct":       q.get("change_pct"),
                        "change_abs":       q.get("change_abs"),
                        "high":             q.get("high"),
                        "low":              q.get("low"),
                        "volume":           q.get("volume"),
                        "vwap":             q.get("vwap"),
                        "prev_close":       q.get("prev_close"),
                        "mention_velocity": social.get("mention_velocity"),
                        "mentions_24h":     social.get("mentions_24h"),
                        **meta,
                    })

            # ── Intelligence overlay ───────────────────────────────────
            # 1) Attach rolling score velocity/momentum to each ticker so
            #    the score_momentum component has data to consume.
            attach_score_history(new_universe, score_history)
            # 2) Score with current regime → emits smart_score + grade
            regime_now = market_regime.get()
            _universe_data = score_and_rank(new_universe, regime=regime_now)
            # 3) Persist this round's pop_scores so velocity is computable
            #    on the next refresh (only writes if ≥5 min since last point).
            score_history.record_batch(_universe_data)

            _last_full_refresh = time.time()
            # Snapshot the full universe for instant restore on next Railway restart
            cache.set("universe:snapshot", _universe_data, 86400)   # 24-hour snapshot
            cache.save_disk()
            logger.info(
                f"Fast refresh done — {len(_universe_data)} tickers in {time.time()-t0:.1f}s"
                f" (regime={regime_now.get('regime_label','?')} "
                f"× {regime_now.get('regime_multiplier','?')}, fundamentals load in bg)"
            )
        except Exception as exc:
            logger.error(f"Fast refresh failed: {exc}", exc_info=True)


async def _yf_concurrent_load() -> None:
    """
    Runs immediately at startup (after 5s for quotes to settle).
    Loads Yahoo Finance enrichment for ALL tickers concurrently in batches of 12.
    This ensures RSI/momentum/market_cap data is available within ~2 minutes.
    """
    await asyncio.sleep(5)
    tickers = get_universe(config.UNIVERSE_MODE)
    logger.info(f"YF concurrent load starting — {len(tickers)} tickers in batches of 12")
    t0 = time.time()
    BATCH = 12
    social_map = cache.get("ape:all") or {}
    for i in range(0, len(tickers), BATCH):
        batch = tickers[i:i + BATCH]
        # Load yfinance + quarterly results concurrently for this batch
        await asyncio.gather(
            *[coordinator.get_yf_enrichment(sym) for sym in batch],
            *[coordinator.get_quarterly_results(sym) for sym in batch],
            return_exceptions=True
        )
        # Update universe scores after each batch
        if _universe_data:
            for sym in batch:
                try:
                    enriched = await coordinator.get_full_ticker(sym, get_meta(sym), social_map)
                    if not enriched or not enriched.get("ticker"):
                        continue
                    for idx, item in enumerate(_universe_data):
                        if item.get("ticker") == sym:
                            _universe_data[idx].update(enriched)
                            break
                except Exception:
                    pass
            attach_score_history(_universe_data, score_history)
            _universe_data[:] = score_and_rank(
                _universe_data[:], regime=market_regime.get()
            )
            score_history.record_batch(_universe_data)
    cache.save_disk()
    logger.info(f"YF concurrent load done in {time.time()-t0:.1f}s — {len(_universe_data)} tickers scored")


async def _tech_refresh() -> None:
    """
    SLOW PASS — Yahoo Finance (primary), Finnhub recs/earnings/news, AV fallback, insider.
    Starts 30 s after boot (reduced from 90 s), then repeats every 60 min.

    Yahoo Finance is the PRIMARY source for candles + fundamentals — no API key, no rate limit.
    Finnhub candles kept as secondary fallback.
    AV fundamentals are budget-limited (25/day) and used only if yf data missing.
    """
    await asyncio.sleep(15)   # start after quotes settle
    while True:
        try:
            tickers    = get_universe(config.UNIVERSE_MODE)
            social_map = cache.get("ape:all") or {}
            logger.info(f"Tech refresh starting — {len(tickers)} tickers")
            t0 = time.time()

            # Sort so any remaining AV budget goes to highest-scored tickers first
            scored_order = sorted(
                tickers,
                key=lambda s: next(
                    (t.get("pop_score", 0) for t in _universe_data
                     if t.get("ticker") == s), 0),
                reverse=True,
            )
            av_budget  = coordinator._av_remaining()
            av_used    = 0

            for sym in scored_order:
                try:
                    # ── Yahoo Finance (PRIMARY — free, no key, no rate limit) ──
                    await coordinator.get_yf_enrichment(sym)

                    # ── Finnhub (supplemental: recs, earnings, news, candles) ──
                    await coordinator.get_recommendation(sym)
                    await coordinator.get_earnings_date(sym)
                    await coordinator.get_news(sym)
                    # Finnhub candles as fallback (yf already covers this)
                    if cache.get(f"candles:{sym}") is None:
                        await coordinator.get_candles(sym)

                    # ── Alpha Vantage (25/day — fallback only if yf data missing) ─
                    yf_data = cache.get(f"yf:{sym}") or {}
                    if (av_used < av_budget
                            and cache.get(f"fund:{sym}") is None
                            and not yf_data.get("market_cap")):
                        await coordinator.get_fundamentals(sym)
                        av_used += 1

                    # ── Quarterly results (EPS surprise, revenue, FCF) ───
                    if cache.get(f"quarterly:{sym}") is None:
                        await coordinator.get_quarterly_results(sym)

                    # ── FMP fallback (if yfinance returned no data) ──────
                    if cache.get(f"yf:{sym}") is None and config.FMP_API_KEY:
                        await coordinator.get_fmp_fundamentals(sym)
                        if cache.get(f"fmp_quote:{sym}") is None:
                            await coordinator.get_fmp_quote(sym)

                    # ── SEC-API insider transactions ───────────────────────
                    if cache.get(f"insider:{sym}") is None:
                        await coordinator.get_insider_transactions(sym)

                    # ── Re-merge & update universe in-place ───────────────
                    enriched = await coordinator.get_full_ticker(
                        sym, get_meta(sym), social_map
                    )
                    if not enriched or not enriched.get("ticker"):
                        continue
                    for i, item in enumerate(_universe_data):
                        if item.get("ticker") == sym:
                            _universe_data[i].update(enriched)
                            break
                    else:
                        if enriched.get("price"):
                            _universe_data.append(enriched)

                except Exception as exc:
                    logger.warning(f"Tech refresh skip {sym}: {exc}")

            if _universe_data:
                attach_score_history(_universe_data, score_history)
                _universe_data[:] = score_and_rank(
                    _universe_data[:], regime=market_regime.get()
                )
                score_history.record_batch(_universe_data)
                cache.save_disk()
            logger.info(
                f"Tech refresh done — {len(_universe_data)} tickers "
                f"in {time.time()-t0:.1f}s · AV used {av_used}/{av_budget}"
            )
        except Exception as exc:
            logger.error(f"Tech refresh failed: {exc}", exc_info=True)

        await asyncio.sleep(3600)


async def _quote_refresh() -> None:
    """
    LIVE PRICES via Alpaca BATCH — one API call for ALL tickers every 30s.
    Replaces the old per-ticker loop (was 150 Finnhub calls/30s → rate limit hell).
    """
    while True:
        await asyncio.sleep(30)
        if not _universe_data:
            continue
        try:
            market = coordinator._market
            if market is None:
                continue
            tickers = [item["ticker"] for item in _universe_data if item.get("ticker")]
            if not tickers:
                continue
            # ONE batch call for all tickers (Alpaca allows 1000 per call, free)
            batch = await market.get_snapshots_batch(tickers)
            if not batch:
                continue
            updated = 0
            for item in _universe_data:
                sym = item.get("ticker")
                if not sym or sym not in batch:
                    continue
                q = batch[sym]
                if q.get("price"):
                    item["price"]      = q["price"]
                    item["change_pct"] = q.get("change_pct")
                    item["change_abs"] = q.get("change_abs")
                    item["high"]       = q.get("high")
                    item["low"]        = q.get("low")
                    item["volume"]     = q.get("volume")
                    item["vwap"]       = q.get("vwap")
                    item["bid"]        = q.get("bid")
                    item["ask"]        = q.get("ask")
                    updated += 1
            logger.debug(f"Batch quote refresh — {updated}/{len(tickers)} prices updated in 1 call")
        except Exception as exc:
            logger.warning(f"Quote refresh error: {exc}")


async def _regime_refresh() -> None:
    """
    Refresh the macro market regime every 30 minutes (SPY/QQQ/VIX/^TNX).
    First call runs immediately so the first universe score already has
    a non-stale regime overlay.
    """
    while True:
        try:
            payload = await market_regime.refresh()
            logger.info(
                "🌐 Regime: %s (score %.1f, ×%.2f)",
                payload.get("regime_label", "?"),
                float(payload.get("regime_score") or 0),
                float(payload.get("regime_multiplier") or 1.0),
            )
        except Exception as exc:
            logger.warning(f"Regime refresh failed: {exc}")
        await asyncio.sleep(1800)   # 30 min


async def _bg_scheduler() -> None:
    """Full fast refresh at startup, then every 5 min."""
    await _full_refresh()
    while True:
        await asyncio.sleep(300)
        await _full_refresh()


# ── Lifespan ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _refresh_lock, _universe_data, _last_full_refresh
    _refresh_lock = asyncio.Lock()

    logger.info("AlphaHunt starting …")

    # ── Instant startup restore from disk cache ────────────────────────
    # If Railway just redeployed, this makes the dashboard show data
    # in <1 second while fresh prices load in the background.
    # Requires: Railway Volume at /data + CACHE_DISK_FILE=/data/cache_v5.json
    cached_snap = cache.get("universe:snapshot")
    if cached_snap and len(cached_snap) > 5:
        _universe_data     = cached_snap
        _last_full_refresh = time.time() - 60   # treat as slightly stale so refresh runs soon
        logger.info(f"✅ Instant restore: {len(_universe_data)} tickers from disk cache (dashboard live immediately)")
    else:
        logger.info("No disk cache found — cold start, fetching fresh data…")

    # Restore model portfolio from disk (persists across deploys)
    # version=2 uses 1-month back-calculated entry prices; rebuild if older
    global _model_portfolio
    saved = cache.get("model_portfolio") or {}
    _model_portfolio = saved if saved.get("version", 0) >= 2 else {}

    # Clear stale earnings cache so fresh dates are fetched this cycle
    cleared = 0
    for k in list(cache._store.keys()):
        if k.startswith("earnings:"):
            cache._store.pop(k, None)
            cleared += 1
    if cleared:
        logger.info(f"🗑  Cleared {cleared} stale earnings cache entries — will re-fetch via yfinance calendar")

    # Start WebSocket stream on best available free source (Alpaca IEX → Polygon)
    tickers_for_ws = get_universe(config.UNIVERSE_MODE)
    if coordinator.alpaca.enabled:
        await coordinator.alpaca.start_ws_stream(tickers_for_ws)
        logger.info("Alpaca IEX WebSocket stream started (FREE real-time prices)")
    elif coordinator.polygon.enabled:
        await coordinator.polygon.start_ws_stream(tickers_for_ws)
        logger.info("Polygon WebSocket stream started")

    tasks = [
        asyncio.create_task(_regime_refresh()),     # macro overlay (30 min)
        asyncio.create_task(_bg_scheduler()),
        asyncio.create_task(_quote_refresh()),
        asyncio.create_task(_yf_concurrent_load()),
        asyncio.create_task(_tech_refresh()),
    ]
    yield
    for t in tasks:
        t.cancel()
    if coordinator.alpaca.enabled:
        coordinator.alpaca.stop_ws()
    elif coordinator.polygon.enabled:
        coordinator.polygon.stop_ws()
    await asyncio.gather(*tasks, return_exceptions=True)
    cache.save_disk()
    logger.info("AlphaHunt shut down — cache saved.")


app = FastAPI(title="AlphaHunt", lifespan=lifespan)

# Serve /static/ files (manifest.json, sw.js, icons)
_STATIC = BASE_DIR / "static"
_STATIC.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# ── SEO INFRASTRUCTURE ───────────────────────────────────────────────
# robots.txt, sitemap.xml, and per-stock SEO pages so search engines
# (and AI search like Google AI Overviews / Perplexity / ChatGPT) can
# discover and index AlphaHunt properly. Without these the dashboard SPA
# is invisible to crawlers — see 2026-04 SEO foundation work.

# Public-facing canonical origin used for sitemap + schema URLs.
# Configurable via env so staging vs prod don't conflict.
SITE_ORIGIN = _env("SITE_ORIGIN", "https://alphahunt.in") if (_env := getattr(__import__("os").environ, "get", None)) else "https://alphahunt.in"
# (Robust origin lookup — falls back to alphahunt.in if env not present)
import os as _os
SITE_ORIGIN = _os.environ.get("SITE_ORIGIN", "https://alphahunt.in").rstrip("/")


@app.get("/favicon.ico")
async def favicon():
    """
    Serve the real 32px brand PNG at /favicon.ico. Google's search-result
    icon prefers raster (PNG/ICO) over SVG — SVG support there is unreliable.
    Browsers + crawlers conventionally request /favicon.ico, so we serve the
    PNG bytes with image/png content-type (the .ico extension in the URL is
    cosmetic; the body's content-type header is what matters).
    """
    from fastapi.responses import FileResponse
    icon_path = _STATIC / "icons" / "favicon-32.png"
    if icon_path.exists():
        return FileResponse(
            icon_path,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )
    # Fallback to inline SVG if the static file is missing (defensive — should
    # never happen because static/icons/favicon-32.png ships with the repo).
    from fastapi.responses import Response
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#16a34a"/>'
        '<stop offset="0.5" stop-color="#84cc16"/>'
        '<stop offset="1" stop-color="#a3e635"/></linearGradient></defs>'
        '<rect width="32" height="32" rx="7" fill="#0f172a"/>'
        '<polyline points="4,22 10,13 16,17 21,7 28,14" stroke="url(#g)" '
        'stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="21" cy="7" r="2.8" fill="#a3e635"/></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/static/icons/{filename}")
async def static_icon_fallback(filename: str):
    """
    Fallback for the icon files referenced in manifest.json + og:image meta.
    The /static/icons/ directory doesn't exist on disk, so without this any
    crawler / browser request for icon-192.png / icon-512.png gets a 404.
    We serve the same brand SVG with image/png content-type — works for
    crawlers (Bing/Google accept SVG for og:image) and avoids the 4xx.
    """
    from fastapi.responses import Response
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
        '<stop offset="0" stop-color="#16a34a"/>'
        '<stop offset="0.55" stop-color="#84cc16"/>'
        '<stop offset="1" stop-color="#a3e635"/></linearGradient></defs>'
        '<rect width="512" height="512" rx="112" fill="#0f172a"/>'
        '<polyline points="72,360 160,232 256,290 352,128 432,210" stroke="url(#g)" '
        'stroke-width="38" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="352" cy="128" r="42" fill="#a3e635"/></svg>'
    )
    # Crawlers care most about getting 200 + valid image data; SVG is fine
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/robots.txt", response_class=HTMLResponse)
async def robots_txt():
    """
    Tell crawlers what's allowed.
      - Block /api/ entirely (JSON endpoints, no value to index)
      - DON'T block /app — Bing/Google flag "blocked by robots.txt" as a
        warning. We use a `noindex` meta tag inside /app instead so
        crawlers can visit (no warning) but won't index it.
    """
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        f"Sitemap: {SITE_ORIGIN}/sitemap.xml\n"
    )
    return HTMLResponse(content=body, media_type="text/plain")


@app.get("/sitemap.xml", response_class=HTMLResponse)
async def sitemap_xml():
    """
    Dynamic XML sitemap covering:
      - Landing page
      - Each research article (/article/{id})
      - Each stock detail page (/stocks/{ticker})
    Crawlers re-fetch this periodically; updating articles/universe
    automatically updates the sitemap.
    """
    from datetime import date as _date
    today = _date.today().isoformat()

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
        f'  <url><loc>{SITE_ORIGIN}/</loc><changefreq>daily</changefreq><priority>1.0</priority><lastmod>{today}</lastmod></url>',
    ]

    # ── Phase 3 SEO landing pages — pillar/sector/compare hubs ───────
    # Higher priority than per-stock pages because these are the
    # internal-link hubs that funnel authority to stock pages.
    parts.append(f'  <url><loc>{SITE_ORIGIN}/learn</loc><changefreq>weekly</changefreq><priority>0.9</priority><lastmod>{today}</lastmod></url>')
    for _slug in _seo.PILLARS:
        parts.append(
            f'  <url><loc>{SITE_ORIGIN}/learn/{_slug}</loc>'
            f'<changefreq>monthly</changefreq><priority>0.85</priority>'
            f'<lastmod>{today}</lastmod></url>'
        )
    parts.append(f'  <url><loc>{SITE_ORIGIN}/sectors</loc><changefreq>daily</changefreq><priority>0.85</priority><lastmod>{today}</lastmod></url>')
    parts.append(f'  <url><loc>{SITE_ORIGIN}/compare</loc><changefreq>weekly</changefreq><priority>0.8</priority><lastmod>{today}</lastmod></url>')
    try:
        for _slug, _label in _seo.sector_slugs(_universe_data or []).items():
            parts.append(
                f'  <url><loc>{SITE_ORIGIN}/sectors/{_slug}</loc>'
                f'<changefreq>daily</changefreq><priority>0.75</priority>'
                f'<lastmod>{today}</lastmod></url>'
            )
    except Exception:
        pass
    try:
        _lookup = {(t.get("ticker") or "").upper() for t in (_universe_data or [])}
        for _a, _b in _seo.FEATURED_COMPARISONS:
            if _a in _lookup and _b in _lookup:
                parts.append(
                    f'  <url><loc>{SITE_ORIGIN}/compare/{_a}-vs-{_b}</loc>'
                    f'<changefreq>weekly</changefreq><priority>0.7</priority>'
                    f'<lastmod>{today}</lastmod></url>'
                )
    except Exception:
        pass

    # Research articles
    for art in _BLOG_ARTICLES:
        aid = art.get("id")
        adate = art.get("date", today)
        if aid:
            parts.append(
                f'  <url><loc>{SITE_ORIGIN}/article/{aid}</loc>'
                f'<changefreq>monthly</changefreq><priority>0.8</priority>'
                f'<lastmod>{adate}</lastmod></url>'
            )

    # Per-stock pages — one URL per ticker in the universe
    try:
        for t in _universe_data or []:
            tk = (t.get("ticker") or "").upper()
            if tk:
                parts.append(
                    f'  <url><loc>{SITE_ORIGIN}/stocks/{tk}</loc>'
                    f'<changefreq>daily</changefreq><priority>0.7</priority>'
                    f'<lastmod>{today}</lastmod></url>'
                )
    except Exception:
        pass

    parts.append('</urlset>')
    return HTMLResponse(content='\n'.join(parts), media_type="application/xml")


# ── HTML Dashboard ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing():
    """Landing page — alphahunt.in home."""
    try:
        html = LANDING_HTML.read_text(encoding="utf-8")
        # Inject Schema.org JSON-LD just before </head> so search engines
        # (and AI search like Google AI Overviews) get rich metadata.
        schema = _build_landing_schema()
        if "</head>" in html:
            html = html.replace("</head>", schema + "\n</head>", 1)
        return HTMLResponse(content=html)
    except FileNotFoundError:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/app")


def _build_landing_schema() -> str:
    """
    Build the WebApplication + Organization schema injection for the landing
    page. Powers Google's rich snippets and AI search citation.
    """
    import json as _json
    org = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "AlphaHunt",
        "url": SITE_ORIGIN,
        "logo": f"{SITE_ORIGIN}/static/icons/icon-512.png",
        "description": "We do the homework. You make the call. 200+ US stocks scored every 5 minutes with plain-English verdicts.",
        "email": "support@alphahunt.in",
        # contactPoint feeds Google's knowledge panel + AI search citations
        "contactPoint": {
            "@type": "ContactPoint",
            "contactType": "customer support",
            "email": "support@alphahunt.in",
            "availableLanguage": ["English"],
        },
    }
    app_schema = {
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": "AlphaHunt",
        "url": SITE_ORIGIN,
        "applicationCategory": "FinanceApplication",
        "operatingSystem": "Web",
        "description": "Real-time US stock research with Alpha Score, plain-English verdicts, conflict detection, Reverse DCF, and macro-aware scoring. Free during beta.",
        "offers": {
            "@type": "Offer",
            "price": "0",
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
        },
        "aggregateRating": None,  # Add when you have user reviews
    }
    # Drop None fields for clean JSON
    app_schema = {k: v for k, v in app_schema.items() if v is not None}
    return (
        f'<script type="application/ld+json">{_json.dumps(org, separators=(",",":"))}</script>\n'
        f'<script type="application/ld+json">{_json.dumps(app_schema, separators=(",",":"))}</script>'
    )


@app.get("/stocks/{ticker}", response_class=HTMLResponse)
async def stock_page(ticker: str):
    """
    Server-rendered, SEO-optimized HTML page for a single ticker.
    Each page targets long-tail searches like "NVDA Alpha score",
    "NVDA stock analysis", "should I buy NVDA". Includes:
      - Unique <title> + meta description with current data
      - Schema.org FinancialProduct + Article markup (rich snippets)
      - Open Graph tags for social shares
      - Alpha Score, verdict, valuation, news inline (visible to crawlers)
      - Internal links to peers (sub-sector graph for crawlers)
      - Big CTA to open the live dashboard for that ticker
    """
    sym = ticker.upper().strip()
    # Look up the ticker in the universe; reject if unknown
    t = next((x for x in (_universe_data or []) if (x.get("ticker") or "").upper() == sym), None)
    if not t:
        # Friendly "not in universe" page rather than 404 — still indexable,
        # tells the user the ticker isn't covered yet.
        return HTMLResponse(content=_render_unknown_stock(sym), status_code=200)
    return HTMLResponse(content=_render_stock_page(t))


def _render_unknown_stock(sym: str) -> str:
    """Lightweight 'we don't cover this yet' page — still SEO-indexable."""
    return f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{sym} — Not in AlphaHunt's universe yet | AlphaHunt</title>
<meta name="description" content="{sym} isn't in our 200+ stock research universe yet. AlphaHunt covers high-quality US stocks with $500M+ market cap — get the Hot List free.">
<meta name="robots" content="noindex,follow">
<link rel="canonical" href="{SITE_ORIGIN}/stocks/{sym}">
<style>body{{font-family:system-ui,-apple-system,sans-serif;max-width:600px;margin:80px auto;padding:0 24px;color:#0a0a0a;text-align:center}}
a{{color:#15803d;font-weight:600;text-decoration:none}}</style>
</head><body>
<h1>{sym}</h1>
<p>This ticker isn't in AlphaHunt's universe yet. We focus on ~200 hand-curated US stocks with $500M+ market cap.</p>
<p><a href="/">← See what we do cover</a></p>
</body></html>"""


def _render_post_earnings_card(t: dict, name: str, sym: str) -> str:
    """
    Build the "Just Reported" earnings card. Returns an HTML string, or empty
    string when no recent earnings event is detected.

    Data sources (already in the API response):
      - t["earnings_just_reported"] — set by data_coordinator._parse_yf_earnings
      - t["eps_quarters"][0]        — most recent EPS actual + estimate + beat
      - t["quarterly_income"][0]    — most recent revenue
      - t["last_earnings_date"]     — when the report was released
      - t["change_1m"] / momentum   — proxy for stock reaction
    """
    eps_q_list = t.get("eps_quarters") or []
    if not eps_q_list or not isinstance(eps_q_list[0], dict):
        return ""

    eps_q = eps_q_list[0] or {}
    q_inc = (t.get("quarterly_income") or [{}])[0] or {}
    last_date = t.get("last_earnings_date") or t.get("earnings_date") or ""

    eps_actual   = eps_q.get("actual")
    eps_estimate = eps_q.get("estimate")
    surprise_pct = eps_q.get("surprise_pct")
    beat         = eps_q.get("beat")
    revenue      = q_inc.get("revenue")
    beat_streak  = t.get("eps_beat_streak")
    reaction_pct = t.get("momentum_1m")  # proxy: 1-month return covers post-earnings drift

    # ── EPS row ──
    if eps_actual is not None and eps_estimate is not None:
        beat_tag = (
            '<span style="background:#dcfce7;color:#15803d;font-weight:700;'
            'padding:3px 10px;border-radius:999px;font-size:11px;letter-spacing:.04em">BEAT</span>'
            if beat else
            '<span style="background:#fee2e2;color:#b91c1c;font-weight:700;'
            'padding:3px 10px;border-radius:999px;font-size:11px;letter-spacing:.04em">MISS</span>'
        ) if beat is not None else ""
        surp_str = (
            f' &middot; <span style="color:{"#15803d" if (surprise_pct or 0) > 0 else "#b91c1c"};'
            f'font-weight:600">{surprise_pct:+.1f}%</span> vs estimate'
        ) if surprise_pct is not None else ""
        eps_row = (
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'padding:10px 0;border-bottom:1px solid #f1f5f9">'
            f'<div style="font-size:13px;color:#64748b;font-weight:600">EPS</div>'
            f'<div style="font-size:14px;color:#0f172a">'
            f'<span class="mono" style="font-weight:700">${eps_actual:.2f}</span> '
            f'<span style="color:#94a3b8">vs <span class="mono">${eps_estimate:.2f}</span> est</span>'
            f'{surp_str} {beat_tag}'
            f'</div></div>'
        )
    else:
        eps_row = ""

    # ── Revenue row (estimates not available from FMP income endpoint) ──
    if revenue:
        # Format as $X.YZB / $X.YZM
        if revenue >= 1e9:
            rev_str = f"${revenue / 1e9:.2f}B"
        elif revenue >= 1e6:
            rev_str = f"${revenue / 1e6:.1f}M"
        else:
            rev_str = f"${revenue:,.0f}"
        rev_growth_str = ""
        rg = t.get("rev_growth_qyoy")
        if rg is not None:
            rev_growth_str = (
                f' &middot; <span style="color:{"#15803d" if rg > 0 else "#b91c1c"};'
                f'font-weight:600">{rg*100:+.1f}%</span> YoY'
            )
        rev_row = (
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'padding:10px 0;border-bottom:1px solid #f1f5f9">'
            f'<div style="font-size:13px;color:#64748b;font-weight:600">Revenue</div>'
            f'<div style="font-size:14px;color:#0f172a">'
            f'<span class="mono" style="font-weight:700">{rev_str}</span>{rev_growth_str}'
            f'</div></div>'
        )
    else:
        rev_row = ""

    # ── Stock reaction (1-month momentum proxy) ──
    if reaction_pct is not None:
        rx_color = "#15803d" if reaction_pct > 0 else "#b91c1c"
        rx_row = (
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'padding:10px 0;border-bottom:1px solid #f1f5f9">'
            f'<div style="font-size:13px;color:#64748b;font-weight:600">Stock reaction (1M)</div>'
            f'<div style="font-size:14px;color:{rx_color};font-weight:700" class="mono">'
            f'{reaction_pct:+.1f}%</div></div>'
        )
    else:
        rx_row = ""

    # ── Beat streak ──
    if beat_streak:
        streak_row = (
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'padding:10px 0">'
            f'<div style="font-size:13px;color:#64748b;font-weight:600">Beat streak</div>'
            f'<div style="font-size:14px;color:#0f172a;font-weight:700">'
            f'{beat_streak} of last 4 quarters</div></div>'
        )
    else:
        streak_row = ""

    # If we have nothing to show, skip the card entirely
    if not (eps_row or rev_row or rx_row or streak_row):
        return ""

    date_str = f' &middot; reported {last_date}' if last_date else ""

    just_reported = bool(t.get("earnings_just_reported"))
    badge_html = (
        '<span style="background:#15803d;color:#fff;font-weight:800;font-size:10px;'
        'letter-spacing:.06em;padding:4px 10px;border-radius:999px">JUST REPORTED</span>'
        if just_reported else
        '<span style="background:#e2e8f0;color:#475569;font-weight:700;font-size:10px;'
        'letter-spacing:.06em;padding:4px 10px;border-radius:999px">LAST EARNINGS</span>'
    )
    bg_grad = (
        "linear-gradient(135deg,#f0fdf4 0%,#fefce8 100%)" if just_reported
        else "linear-gradient(135deg,#f8fafc 0%,#f1f5f9 100%)"
    )
    border_color = "#bbf7d0" if just_reported else "#e2e8f0"
    return f"""
  <div style="background:{bg_grad};
              border:1px solid {border_color};border-radius:12px;
              padding:18px 22px;margin-bottom:24px;
              box-shadow:0 1px 3px rgba(15,23,42,.04)"
       data-earnings-card="{sym}">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
      {badge_html}
      <span style="font-size:12px;color:#475569">{name} ({sym}){date_str}</span>
    </div>
    <h3 style="font-size:16px;font-weight:800;color:#0f172a;margin:0 0 8px 0">
      Latest earnings
    </h3>
    {eps_row}
    {rev_row}
    {rx_row}
    {streak_row}
    <div data-guidance-slot=""></div>
    <div data-sentiment-slot=""></div>
  </div>
  <script>
    (function() {{
      var card = document.querySelector('[data-earnings-card="{sym}"]');
      if (!card) return;
      fetch('/api/earnings-intel/{sym}')
        .then(function(r) {{ return r.ok ? r.json() : null; }})
        .then(function(data) {{
          if (!data) return;
          var gSlot = card.querySelector('[data-guidance-slot]');
          var sSlot = card.querySelector('[data-sentiment-slot]');
          var g = data.guidance || {{}};
          if (gSlot && g.tone && g.tone !== 'none') {{
            var toneColor = g.tone === 'raised'   ? '#15803d' :
                            g.tone === 'lowered'  ? '#b91c1c' : '#64748b';
            var toneArrow = g.tone === 'raised'   ? '↗' :
                            g.tone === 'lowered'  ? '↘' : '→';
            var rev = g.revenue_guidance ? '<div style="font-size:13px;color:#0f172a;margin-top:2px">Revenue: <span class=\"mono\" style=\"font-weight:700\">' + g.revenue_guidance + '</span></div>' : '';
            var eps = g.eps_guidance     ? '<div style="font-size:13px;color:#0f172a;margin-top:2px">EPS: <span class=\"mono\" style=\"font-weight:700\">' + g.eps_guidance + '</span></div>' : '';
            var sum = g.summary          ? '<div style="font-size:12.5px;color:#475569;font-style:italic;margin-top:6px">"' + g.summary + '"</div>' : '';
            gSlot.innerHTML =
              '<div style="margin-top:14px;padding-top:14px;border-top:1px dashed #cbd5e1">' +
                '<div style="display:flex;justify-content:space-between;align-items:center">' +
                  '<div style="font-size:11px;color:#64748b;font-weight:700;letter-spacing:.06em;text-transform:uppercase">GUIDANCE</div>' +
                  '<div style="font-size:11px;color:' + toneColor + ';font-weight:800">' + toneArrow + ' ' + g.tone.toUpperCase() + '</div>' +
                '</div>' + rev + eps + sum +
              '</div>';
          }}
          var rx = data.reaction || {{}};
          if (sSlot && rx.label && rx.components && rx.components.length) {{
            var lblColor = rx.score >=  0.30 ? '#15803d' :
                           rx.score >=  0.10 ? '#16a34a' :
                           rx.score >  -0.10 ? '#64748b' :
                           rx.score >  -0.30 ? '#d97706' : '#b91c1c';
            var compHTML = rx.components.map(function(c) {{
              var ptColor = c.points > 0  ? '#15803d' :
                            c.points < 0  ? '#b91c1c' : '#94a3b8';
              var ptStr   = (c.points >= 0 ? '+' : '') + Number(c.points).toFixed(2);
              return '<div style="display:flex;justify-content:space-between;align-items:center;padding:4px 0;font-size:12.5px">' +
                       '<span style="color:#475569">' + c.name + '</span>' +
                       '<span style="display:flex;gap:10px;align-items:baseline">' +
                         '<span style="color:#0f172a">' + c.value + '</span>' +
                         '<span class="mono" style="color:' + ptColor + ';font-weight:700;min-width:46px;text-align:right">' + ptStr + '</span>' +
                       '</span>' +
                     '</div>';
            }}).join('');
            sSlot.innerHTML =
              '<div style="margin-top:14px;padding-top:14px;border-top:1px dashed #cbd5e1">' +
                '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">' +
                  '<div style="font-size:11px;color:#64748b;font-weight:700;letter-spacing:.06em;text-transform:uppercase">EARNINGS REACTION</div>' +
                  '<div style="font-size:12px;color:' + lblColor + ';font-weight:800">' + rx.label.toUpperCase() + ' (' + (rx.score >= 0 ? '+' : '') + rx.score + ')</div>' +
                '</div>' + compHTML +
              '</div>';
          }}
        }})
        .catch(function(){{}}); /* fail silently — card still shows EPS/revenue */
    }})();
  </script>
"""


def _render_stock_page(t: dict) -> str:
    """
    Render the SEO-optimized HTML page for a single ticker.
    Pure server-side render — no JS required to view the content.
    """
    import json as _json
    sym  = (t.get("ticker") or "").upper()
    name = t.get("name") or sym
    sector = t.get("sector") or ""
    sub    = t.get("sub_sector") or t.get("subsector") or sector or ""
    # Coerce all numerics to floats up front so f-string format specs are
    # always valid (Python f-string format-spec doesn't accept conditional
    # expressions, so we pre-format strings as variables).
    def _f(v, default=0.0):
        try: return float(v) if v is not None else default
        except (TypeError, ValueError): return default
    price  = _f(t.get("price"))
    pop    = _f(t.get("smart_score") if t.get("smart_score") is not None else t.get("pop_score"))
    grade  = t.get("grade") or "—"
    rating = {"A":"★★★★★ Top Tier","B":"★★★★ Quality","C":"★★★ Average","D":"★★ Below Avg","F":"★ Weak"}.get(grade, "Under Review")
    verdict_color = {"A":"#15803d","B":"#1e40af","C":"#b45309","D":"#dc2626","F":"#991b1b"}.get(grade, "#475569")
    bottom_line = t.get("bottom_line") or f"{name} is currently scored {round(pop)}/100 on Alpha Score."
    chg = _f(t.get("change_pct"))
    chg_sign = "+" if chg >= 0 else ""
    rev_g = t.get("revenue_growth_yoy")
    mom = t.get("momentum_1m")
    pe  = t.get("pe_ratio") or t.get("forward_pe")
    tgt = _f(t.get("target_mean") or t.get("target_price"))
    upside = t.get("target_upside_pct")
    news = (t.get("news") or [])[:5]
    # Pre-formatted price string (avoids f-string format-spec restrictions)
    price_str = f"${price:.2f}" if price > 0 else "—"
    chg_str   = f"{chg_sign}{chg:.2f}%"

    # ── Post-earnings summary block ──────────────────────────────────
    # When earnings_just_reported is True, render a "Just Reported" card
    # above Key Metrics with EPS actual vs estimate, revenue, and beat
    # streak. Empty string when no recent earnings — keeps the layout clean.
    post_earnings_html = _render_post_earnings_card(t, name, sym)

    # ── SEO meta tags — these are the part Google ranks on ──
    title = f"{sym} Stock Analysis · Alpha Score {round(pop)} · {rating} | AlphaHunt"
    desc  = (
        f"{name} ({sym}) — current Alpha Score {round(pop)}/100 ({rating}). "
        f"{bottom_line[:140]}"
    )

    # Schema.org structured data — FinancialProduct (without aggregateRating;
    # Google's validator rejects it on FinancialProduct since stocks aren't
    # user-rated products) + a separate Article schema for the analysis prose
    # so Google understands this page is editorial research, not a product page.
    schema = {
        "@context": "https://schema.org",
        "@type": "FinancialProduct",
        "name": f"{name} ({sym})",
        "url": f"{SITE_ORIGIN}/stocks/{sym}",
        "description": bottom_line,
        "category": "Stock",
        "provider": {
            "@type": "Organization",
            "name": "AlphaHunt",
            "url": SITE_ORIGIN,
        },
    }
    # Article schema for the analysis content — accepted by Google rich results
    article_schema = {
        "@context": "https://schema.org",
        "@type": "AnalysisNewsArticle",
        "headline": title[:110],
        "description": desc,
        "url": f"{SITE_ORIGIN}/stocks/{sym}",
        "image": f"{SITE_ORIGIN}/static/icons/icon-512.png",
        "author": {"@type": "Organization", "name": "AlphaHunt", "url": SITE_ORIGIN},
        "publisher": {
            "@type": "Organization",
            "name": "AlphaHunt",
            "logo": {"@type": "ImageObject", "url": f"{SITE_ORIGIN}/static/icons/icon-512.png"},
        },
        "about": {
            "@type": "Corporation",
            "name": name,
            "tickerSymbol": sym,
        },
    }

    # ── Build news list HTML ──
    news_html = ""
    if news:
        items = []
        for n in news[:5]:
            headline = (n.get("headline") or "").replace("<", "&lt;")[:140]
            url = n.get("url") or "#"
            src = n.get("source") or ""
            if headline:
                items.append(f'<li><a href="{url}" target="_blank" rel="noopener">{headline}</a> <span class="src">· {src}</span></li>')
        if items:
            news_html = f'<h2>Latest news on {sym}</h2><ul class="news">{"".join(items)}</ul>'

    # ── Peer links — internal-link graph helps Google understand the cluster ──
    peers_html = ""
    try:
        peers = [p for p in (_universe_data or [])
                 if (p.get("sub_sector") or p.get("sector") or "") == sub
                 and (p.get("ticker") or "").upper() != sym][:6]
        if peers:
            chips = " ".join(
                f'<a href="/stocks/{(p.get("ticker") or "").upper()}" class="peer">{(p.get("ticker") or "").upper()}</a>'
                for p in peers
            )
            peers_html = f'<h2>Similar stocks in {sub or "this sub-sector"}</h2><div class="peers">{chips}</div>'
    except Exception:
        pass

    # ── Final HTML — no JS needed, fully crawlable ──
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="{desc}">
<link rel="canonical" href="{SITE_ORIGIN}/stocks/{sym}">

<!-- Open Graph (social shares) -->
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:type" content="article">
<meta property="og:url" content="{SITE_ORIGIN}/stocks/{sym}">
<meta property="og:image" content="{SITE_ORIGIN}/og/{sym}.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:site_name" content="AlphaHunt">
<meta name="twitter:image" content="{SITE_ORIGIN}/og/{sym}.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{desc}">

<link rel="icon" type="image/png" sizes="32x32" href="/static/icons/favicon-32.png">
<link rel="icon" type="image/png" sizes="192x192" href="/static/icons/icon-192.png">
<link rel="icon" type="image/png" sizes="512x512" href="/static/icons/icon-512.png">
<link rel="apple-touch-icon" sizes="192x192" href="/static/icons/icon-192.png">
<link rel="manifest" href="/static/manifest.json">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">

<!-- Schema.org structured data — powers Google rich snippets + AI search.
     We emit two: FinancialProduct (the stock entity) + AnalysisNewsArticle
     (the editorial analysis on the page). Google validates each separately. -->
<script type="application/ld+json">{_json.dumps(schema, separators=(',',':'))}</script>
<script type="application/ld+json">{_json.dumps(article_schema, separators=(',',':'))}</script>

<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:'Inter',-apple-system,sans-serif;color:#0a0a0a;background:#fafbfc;line-height:1.6;font-size:15.5px;-webkit-font-smoothing:antialiased}}
a{{color:#15803d;text-decoration:none;font-weight:600}}
a:hover{{text-decoration:underline}}
.mono{{font-family:'JetBrains Mono',monospace;font-feature-settings:'tnum' 1}}
.wrap{{max-width:780px;margin:0 auto;padding:32px 24px 64px}}
.brand{{display:inline-flex;align-items:center;gap:8px;font-size:16px;font-weight:800;color:#0a0a0a;margin-bottom:32px}}
.brand em{{font-style:normal;color:#15803d}}
.crumbs{{font-size:12.5px;color:#94a3b8;margin-bottom:8px;letter-spacing:.04em;text-transform:uppercase;font-weight:600}}
h1{{font-size:38px;font-weight:900;letter-spacing:-.03em;margin-bottom:6px;color:#0a0a0a}}
h1 .sym{{font-family:'JetBrains Mono',monospace;color:#15803d}}
.subhead{{font-size:15px;color:#475569;margin-bottom:24px}}
.verdict-box{{background:#fff;border:1px solid #e2e8f0;border-left:4px solid {verdict_color};border-radius:12px;padding:20px 24px;margin-bottom:28px;box-shadow:0 1px 3px rgba(15,23,42,.04)}}
.verdict-head{{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:10px}}
.verdict-tag{{background:{verdict_color};color:#fff;padding:5px 12px;border-radius:7px;font-weight:800;font-size:13px;letter-spacing:.04em}}
.verdict-score{{font-family:'JetBrains Mono',monospace;font-size:24px;font-weight:800;color:#0a0a0a}}
.verdict-score .lbl{{font-size:11px;color:#94a3b8;font-weight:600;letter-spacing:.06em;text-transform:uppercase;margin-left:6px}}
.verdict-text{{font-size:14.5px;line-height:1.6;color:#0f172a}}
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:28px}}
.metric{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px}}
.metric .lbl{{font-size:10.5px;color:#64748b;font-weight:700;letter-spacing:.05em;text-transform:uppercase;margin-bottom:3px}}
.metric .val{{font-family:'JetBrains Mono',monospace;font-size:17px;font-weight:800;color:#0a0a0a}}
.metric .val.pos{{color:#15803d}}
.metric .val.neg{{color:#b91c1c}}
h2{{font-size:21px;font-weight:800;letter-spacing:-.015em;margin:32px 0 12px;color:#0a0a0a}}
.news{{list-style:none;padding:0}}
.news li{{padding:10px 0;border-bottom:1px solid #eef0f3;font-size:14px}}
.news li:last-child{{border-bottom:none}}
.news .src{{color:#94a3b8;font-size:12px;font-weight:500}}
.peers{{display:flex;flex-wrap:wrap;gap:8px}}
.peer{{display:inline-block;padding:7px 12px;background:#fff;border:1px solid #e2e8f0;border-radius:8px;font-family:'JetBrains Mono',monospace;font-size:13px;color:#0a0a0a;font-weight:700;transition:all .15s}}
.peer:hover{{border-color:#15803d;color:#15803d;text-decoration:none}}
.cta{{margin-top:40px;padding:28px 32px;background:linear-gradient(135deg,#0a0e1a 0%,#1a2e1a 100%);border-radius:16px;text-align:center;color:#fff}}
.cta h3{{font-size:22px;font-weight:800;letter-spacing:-.02em;margin-bottom:8px;color:#fff}}
.cta p{{color:rgba(255,255,255,.7);margin-bottom:18px}}
.cta-btn{{display:inline-block;background:#fff;color:#0a0a0a;padding:13px 26px;border-radius:10px;font-weight:700;font-size:14.5px}}
.cta-btn:hover{{background:#f1f5f9;text-decoration:none}}
.legal{{margin-top:32px;font-size:11.5px;color:#94a3b8;text-align:center;line-height:1.6}}
@media(max-width:640px){{h1{{font-size:30px}}}}
</style>
</head>
<body>
<div class="wrap">

  <a href="/" class="brand">
    <svg width="22" height="22" viewBox="0 0 28 28"><defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#16a34a"/><stop offset=".55" stop-color="#84cc16"/><stop offset="1" stop-color="#a3e635"/></linearGradient></defs><rect width="28" height="28" rx="7" fill="#0f172a"/><polyline points="4,21 9,13 15,17 20,7 24,12" stroke="url(#lg)" stroke-width="2.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/><circle cx="20" cy="7" r="2.5" fill="#a3e635"/></svg>
    Alpha<em>Hunt</em>
  </a>

  <div class="crumbs">{sub or sector or "Stock Analysis"}</div>
  <h1><span class="sym">{sym}</span> Stock Analysis</h1>
  <p class="subhead">{name} · current price <span class="mono">{price_str}</span> ({chg_str} today)</p>

  <div class="verdict-box">
    <div class="verdict-head">
      <span class="verdict-tag">{rating}</span>
      <span class="verdict-score">{round(pop)}<span class="lbl">/ Alpha Score</span></span>
    </div>
    <div class="verdict-text">{bottom_line}</div>
  </div>

  {post_earnings_html}

  <h2>Key metrics</h2>
  <div class="metrics">
    <div class="metric"><div class="lbl">Alpha Score</div><div class="val">{round(pop)}/100</div></div>
    <div class="metric"><div class="lbl">Grade</div><div class="val">{grade}</div></div>
    {f'<div class="metric"><div class="lbl">Rev Growth YoY</div><div class="val {"pos" if rev_g and rev_g > 0 else "neg" if rev_g and rev_g < 0 else ""}">{rev_g*100:+.1f}%</div></div>' if rev_g is not None else ''}
    {f'<div class="metric"><div class="lbl">30-day Momentum</div><div class="val {"pos" if mom and mom > 0 else "neg" if mom and mom < 0 else ""}">{mom:+.1f}%</div></div>' if mom is not None else ''}
    {f'<div class="metric"><div class="lbl">Forward P/E</div><div class="val">{pe:.1f}×</div></div>' if pe and pe > 0 else ''}
    {f'<div class="metric"><div class="lbl">Analyst Target</div><div class="val">${tgt:.2f}</div></div>' if tgt and tgt > 0 else ''}
    {f'<div class="metric"><div class="lbl">Implied Upside</div><div class="val {"pos" if upside and upside > 0 else "neg"}">{upside:+.1f}%</div></div>' if upside is not None else ''}
  </div>

  {news_html}
  {peers_html}

  <div class="cta">
    <h3>Get the full live dashboard</h3>
    <p>Real-time scoring, conflict detection, Reverse DCF, peer comparison and more — free during beta.</p>
    <a href="/app?signup=1" class="cta-btn">Open dashboard for {sym} →</a>
  </div>

  <div class="legal">
    AlphaHunt is a research tool, not financial advice. Alpha Score is a composite signal — always do your own research before investing.
    <br>Last updated automatically every 5 minutes during US market hours.
    <br>Questions? <a href="mailto:support@alphahunt.in" style="color:#15803d">support@alphahunt.in</a>
  </div>

</div>
</body>
</html>"""


# ── SEO PHASE 3 — pillar / sector / comparison pages ────────────────
# Long-form HTML routes that build topical authority and capture
# high-intent search traffic. The renderers themselves live in
# seo_pages.py so app.py stays under control. See that module for the
# rationale and the actual content.
import seo_pages as _seo


@app.get("/learn", response_class=HTMLResponse)
async def learn_index():
    """Hub page that links to every educational pillar — internal-link
    graph for crawlers + a useful TOC for humans."""
    return HTMLResponse(content=_seo.render_pillar_index(SITE_ORIGIN))


@app.get("/learn/{slug}", response_class=HTMLResponse)
async def learn_pillar(slug: str):
    """Evergreen explainer pages — Alpha Score, Reverse DCF, fundamentals."""
    html = _seo.render_pillar(slug.lower().strip(), SITE_ORIGIN)
    if html is None:
        raise HTTPException(status_code=404, detail="Unknown learn page")
    return HTMLResponse(content=html)


@app.get("/sectors", response_class=HTMLResponse)
async def sectors_index():
    return HTMLResponse(content=_seo.render_sector_index(_universe_data or [], SITE_ORIGIN))


@app.get("/sectors/{slug}", response_class=HTMLResponse)
async def sector_page(slug: str):
    """One landing page per sub-sector with live Alpha Scores."""
    html = _seo.render_sector(slug.lower().strip(), _universe_data or [], SITE_ORIGIN)
    if html is None:
        raise HTTPException(status_code=404, detail="Unknown sector")
    return HTMLResponse(content=html)


@app.get("/compare", response_class=HTMLResponse)
async def compare_index():
    return HTMLResponse(content=_seo.render_compare_index(_universe_data or [], SITE_ORIGIN))


@app.get("/compare/{pair}", response_class=HTMLResponse)
async def compare_pair(pair: str):
    """Dynamic head-to-head — `pair` must be `<TICKER1>-vs-<TICKER2>`."""
    p = pair.lower().strip()
    if "-vs-" in p:
        a, b = p.split("-vs-", 1)
    elif "-" in p:
        a, b = p.split("-", 1)
    else:
        raise HTTPException(status_code=404, detail="Bad comparison URL")
    html = _seo.render_comparison(a, b, _universe_data or [], SITE_ORIGIN)
    if html is None:
        raise HTTPException(status_code=404, detail="One or both tickers not in universe")
    return HTMLResponse(content=html)


# ── Newsletter signup (P3) ────────────────────────────────────────────
# Lightweight email capture. Stores to a local JSONL file so we don't
# need a database for the MVP. When we wire up an ESP later
# (Mailerlite, Buttondown, etc.) we'll just import the file.

_NEWSLETTER_FILE = BASE_DIR / "data" / "newsletter.jsonl"
_NEWSLETTER_FILE.parent.mkdir(exist_ok=True)


class _NewsletterBody(BaseModel):
    email: str
    source: Optional[str] = "unknown"
    company: Optional[str] = ""   # honeypot — should always be empty


@app.post("/api/newsletter/subscribe")
async def newsletter_subscribe(body: _NewsletterBody):
    """Capture an email + source. Honeypot defence: if `company` has any
    value, silently accept (so bots don't iterate); we just don't save."""
    import json as _json
    import re as _re
    from datetime import datetime as _dt
    email = (body.email or "").strip().lower()
    if not _re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        raise HTTPException(status_code=400, detail="Please enter a valid email.")
    if (body.company or "").strip():
        return {"ok": True, "message": "Thanks!"}
    rec = {
        "email": email,
        "source": (body.source or "unknown")[:60],
        "ts": _dt.utcnow().isoformat() + "Z",
    }
    try:
        with open(_NEWSLETTER_FILE, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception as e:
        logger.warning("newsletter write failed: %s", e)
    return {"ok": True, "message": "You're in! First digest hits your inbox Sunday."}


# ── OG image generator (P3) ──────────────────────────────────────────
# Renders a 1200×630 branded card per ticker so social shares of stock
# pages get a rich, on-brand preview. Caches per-ticker for 10 min.

_OG_CACHE: dict[str, tuple[float, bytes]] = {}
_OG_TTL = 600


def _render_og_png(t: dict) -> bytes:
    """Render a 1200×630 PNG brand card for a single ticker."""
    from PIL import Image, ImageDraw, ImageFont
    sym  = (t.get("ticker") or "").upper()
    name = (t.get("name") or sym)[:36]
    pop  = t.get("smart_score") if t.get("smart_score") is not None else t.get("pop_score")
    try: pop_n = int(round(float(pop))) if pop is not None else 0
    except (TypeError, ValueError): pop_n = 0
    grade  = t.get("grade") or "—"
    rating = {"A":"★★★★★ TOP TIER","B":"★★★★ QUALITY","C":"★★★ AVERAGE","D":"★★ BELOW AVG","F":"★ WEAK"}.get(grade, "UNDER REVIEW")
    bl = (t.get("bottom_line") or f"{name} scored {pop_n}/100 on Alpha Score.")[:120]

    W, H = 1200, 630
    score_color = (
        (132, 204, 22)  if pop_n >= 80 else
        (96, 165, 250)  if pop_n >= 65 else
        (250, 204, 21)  if pop_n >= 50 else
        (248, 113, 113)
    )
    img = Image.new("RGB", (W, H), (10, 14, 26))
    draw = ImageDraw.Draw(img, "RGBA")
    for y in range(H):
        f = y / H
        r = int(10  + (24 - 10)  * f)
        g = int(14  + (46 - 14)  * f)
        b = int(26  + (26 - 26)  * f)
        draw.line([(0, y), (W, y)], fill=(r, g, b))
    for r_ in range(420, 0, -20):
        alpha = max(0, 28 - r_ // 20)
        draw.ellipse((W - r_ - 80, H//2 - r_, W - 80 + r_, H//2 + r_),
                     fill=score_color + (alpha,))

    def _font(size: int, bold: bool = False):
        candidates = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/Library/Fonts/Arial Bold.ttf" if bold else "/Library/Fonts/Arial.ttf",
            "C:\\Windows\\Fonts\\arialbd.ttf" if bold else "C:\\Windows\\Fonts\\arial.ttf",
        ]
        for p in candidates:
            try: return ImageFont.truetype(p, size)
            except Exception: pass
        return ImageFont.load_default()

    f_brand = _font(34, bold=True)
    f_sym   = _font(120, bold=True)
    f_name  = _font(34)
    f_score = _font(180, bold=True)
    f_lbl   = _font(22, bold=True)
    f_rate  = _font(38, bold=True)
    f_bl    = _font(26)

    draw.text((60, 50), "AlphaHunt", font=f_brand, fill=(255, 255, 255))
    draw.text((60, 90), "We do the homework. You make the call.", font=_font(20), fill=(148, 163, 184))
    draw.text((60, 200), sym, font=f_sym, fill=(132, 204, 22))
    draw.text((60, 350), name, font=f_name, fill=(226, 232, 240))

    import textwrap as _tw
    for i, line in enumerate(_tw.wrap(bl, width=42)[:3]):
        draw.text((60, 430 + i * 36), line, font=f_bl, fill=(148, 163, 184))

    cx, cy, r = 940, H // 2, 180
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(30, 41, 59), width=22)
    sweep = max(1, int(360 * (pop_n / 100.0)))
    draw.arc((cx - r, cy - r, cx + r, cy + r), start=-90, end=-90 + sweep, fill=score_color, width=22)
    s = str(pop_n)
    bbox = draw.textbbox((0, 0), s, font=f_score)
    sw = bbox[2] - bbox[0]
    sh = bbox[3] - bbox[1]
    draw.text((cx - sw // 2, cy - sh // 2 - 30), s, font=f_score, fill=(255, 255, 255))
    draw.text((cx - 60, cy + 70), "ALPHA SCORE", font=f_lbl, fill=(148, 163, 184))

    chip_w, chip_h = 240, 64
    chip_x = W - chip_w - 60
    chip_y = H - chip_h - 60
    draw.rounded_rectangle(
        (chip_x, chip_y, chip_x + chip_w, chip_y + chip_h),
        radius=14, fill=score_color
    )
    bbox = draw.textbbox((0, 0), rating, font=f_rate)
    rw = bbox[2] - bbox[0]
    rh = bbox[3] - bbox[1]
    draw.text((chip_x + (chip_w - rw) // 2, chip_y + (chip_h - rh) // 2 - 4),
              rating, font=f_rate, fill=(10, 14, 26))

    import io as _io
    buf = _io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


@app.get("/og/{ticker}.png")
async def og_image(ticker: str):
    """Branded 1200×630 social-share card for a single ticker."""
    from fastapi.responses import Response
    sym = ticker.upper().strip()
    cached = _OG_CACHE.get(sym)
    if cached and (time.time() - cached[0]) < _OG_TTL:
        return Response(content=cached[1], media_type="image/png",
                        headers={"Cache-Control": "public, max-age=600"})
    t = next((x for x in (_universe_data or []) if (x.get("ticker") or "").upper() == sym), None)
    if not t:
        return await static_icon_fallback("og-fallback.png")
    try:
        png = _render_og_png(t)
    except ImportError:
        return await static_icon_fallback("og-fallback.png")
    except Exception as e:
        logger.warning("OG render failed for %s: %s", sym, e)
        return await static_icon_fallback("og-fallback.png")
    _OG_CACHE[sym] = (time.time(), png)
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=600"})


@app.get("/app", response_class=HTMLResponse)
async def dashboard():
    """
    Main dashboard SPA — with server-side data injection.

    Pre-embeds the current universe snapshot directly into the HTML so the
    browser renders ALL stocks instantly with zero extra API calls.
    Background JS refresh silently updates prices every 30s after load.
    """
    try:
        html = DASHBOARD_HTML.read_text(encoding="utf-8")

        if _universe_data:
            # Embed current data as window.__AH_DATA__ inside <head>
            # The JS checks this and renders immediately — no /api/universe round-trip
            import json as _json
            payload = _json.dumps(_clean({
                "tickers":      _universe_data,
                "warming_up":   False,
                "last_refresh": _last_full_refresh,
                "hot_list_n":   config.HOT_LIST_N,
                "account_size": config.ACCOUNT_SIZE_USD,
                "regime":       market_regime.get(),
            }), ensure_ascii=False, separators=(",", ":"))
            injection = f'\n<script>window.__AH_DATA__={payload};</script>\n'
            html = html.replace("</head>", injection + "</head>", 1)

        return HTMLResponse(content=html)
    except FileNotFoundError:
        return HTMLResponse(
            content="<h2>templates/dashboard.html not found</h2>", status_code=500
        )


# ── API Endpoints ─────────────────────────────────────────────────────

@app.get("/api/universe")
async def api_universe():
    return JSONResponse(_clean({
        "tickers":        _universe_data,
        "last_refresh":   _last_full_refresh,
        "universe_mode":  config.UNIVERSE_MODE,
        "account_size":   config.ACCOUNT_SIZE_USD,
        "hot_list_n":     config.HOT_LIST_N,
        "min_confidence": config.MIN_CONFIDENCE,
        "warming_up":     len(_universe_data) == 0,
        "regime":         market_regime.get(),
    }))


@app.get("/api/regime")
async def api_regime():
    """Current macro regime snapshot (cached) — SPY/QQQ/VIX/^TNX overlay."""
    return JSONResponse(_clean(market_regime.get()))


@app.post("/api/regime/refresh")
async def api_regime_refresh():
    """Force a regime refresh (admin / debug)."""
    payload = await market_regime.refresh()
    return JSONResponse(_clean(payload))


@app.get("/api/thesis/{symbol}")
async def api_thesis(symbol: str):
    """
    Per-stock AI investment thesis — bull/bear case + recommendation +
    trade plan + regime context. Falls back to a deterministic rule-based
    output when ANTHROPIC_API_KEY is not configured.
    """
    sym = symbol.upper()
    target = next((t for t in _universe_data if t.get("ticker") == sym), None)
    if not target:
        # On-demand build for tickers outside the in-memory universe
        try:
            social_map = cache.get("ape:all") or {}
            target = await coordinator.get_full_ticker(sym, get_meta(sym), social_map)
            if target:
                # Attach score history before scoring so score_momentum has data
                attach_score_history([target], score_history)
                target.update(compute_pop_score(target, regime=market_regime.get()))
        except Exception as exc:
            logger.error(f"Thesis build {sym}: {exc}")
            raise HTTPException(status_code=404, detail=f"Ticker {sym} not found")
    if not target:
        raise HTTPException(status_code=404, detail=f"Ticker {sym} not found")

    try:
        thesis = await thesis_gen.build(target)
        return JSONResponse(_clean(thesis))
    except Exception as exc:
        logger.error(f"Thesis generation failed for {sym}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Thesis generation failed")


@app.get("/api/quotes/batch")
async def api_quotes_batch():
    slim = [
        {
            "ticker":          t.get("ticker"),
            "price":           t.get("price"),
            "change_pct":      t.get("change_pct"),
            "change_abs":      t.get("change_abs"),
            "high":            t.get("high"),
            "low":             t.get("low"),
            "open":            t.get("open"),
            "prev_close":      t.get("prev_close"),
            "volume":          t.get("volume"),
            "pop_score":       t.get("pop_score"),
            "smart_score":     t.get("smart_score"),
            "grade":           t.get("grade"),
            "score_velocity":  t.get("score_velocity"),
        }
        for t in _universe_data
    ]
    return JSONResponse(_clean({
        "quotes": slim,
        "ts":     time.time(),
        "regime": market_regime.get(),
    }))


@app.get("/api/ticker/{symbol}")
async def api_ticker(symbol: str):
    sym = symbol.upper()
    for t in _universe_data:
        if t.get("ticker") == sym:
            # Already enriched with smart_score + score_velocity etc. via the refresh loop.
            return JSONResponse(t)
    try:
        social_map = cache.get("ape:all") or {}
        result     = await coordinator.get_full_ticker(sym, get_meta(sym), social_map)
        if result:
            attach_score_history([result], score_history)
            result.update(compute_pop_score(result, regime=market_regime.get()))
            return JSONResponse(result)
    except Exception as exc:
        logger.error(f"On-demand fetch {sym}: {exc}")
    raise HTTPException(status_code=404, detail=f"Ticker {sym} not found")


# IMPORTANT: /api/news/live MUST be defined BEFORE /api/news/{symbol}
# otherwise FastAPI matches "live" as a ticker symbol parameter.
@app.get("/api/news/live")
async def api_news_live():
    """Fetch live news from Alpaca — called when AI Catalysts tab opens.
    Strategy: try hot-list tickers first, fall back to general market news if empty.
    """
    import httpx
    from datetime import datetime as _dt

    # Prefer hot-list tickers (most relevant), fall back to top universe tickers
    hot_tickers  = [t["ticker"] for t in _daily_hot if t.get("ticker")]
    all_tickers  = [t["ticker"] for t in _universe_data[:20] if t.get("ticker")]
    ticker_batch = (hot_tickers or all_tickers)[:15]

    headers = {
        "APCA-API-KEY-ID":     config.ALPACA_KEY_ID,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
    }

    async def _fetch(params: dict):
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get("https://data.alpaca.markets/v1beta1/news",
                                 headers=headers, params=params)
        return r

    def _parse(raw_list):
        out = []
        for item in raw_list:
            syms = item.get("symbols") or []
            ts = 0
            try:
                ts = int(_dt.fromisoformat(item["created_at"].replace("Z","+00:00")).timestamp())
            except Exception:
                pass
            out.append({
                "headline": item.get("headline", ""),
                "summary":  item.get("summary",  ""),
                "url":      item.get("url", "#"),
                "source":   item.get("source", ""),
                "author":   item.get("author", ""),
                "ticker":   syms[0] if syms else "",
                "symbols":  syms,
                "datetime": ts,
                "sentiment": "neutral",
            })
        return out

    try:
        # Pass 1: specific tickers
        if ticker_batch:
            resp = await _fetch({"symbols": ",".join(ticker_batch), "limit": 50, "sort": "desc"})
            if resp.status_code == 200:
                raw = resp.json().get("news", [])
                if raw:
                    news = _parse(raw)
                    return JSONResponse({"news": news, "count": len(news), "source": "alpaca_tickers"})

        # Pass 2: general market news (no symbol filter)
        resp = await _fetch({"limit": 50, "sort": "desc"})
        if resp.status_code != 200:
            logger.warning(f"Alpaca news (general) returned {resp.status_code}: {resp.text[:200]}")
            return JSONResponse({"news": [], "source": "alpaca_error", "status": resp.status_code})

        raw  = resp.json().get("news", [])
        news = _parse(raw)
        return JSONResponse({"news": news, "count": len(news), "source": "alpaca_general"})
    except Exception as exc:
        logger.error(f"Live news fetch failed: {exc}")
        return JSONResponse({"news": [], "source": "error", "error": str(exc)})


@app.get("/api/news/{symbol}")
async def api_news(symbol: str):
    sym = symbol.upper()
    try:
        news = await coordinator.get_news(sym)
        return JSONResponse({"ticker": sym, "news": news or []})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── RESEARCH ARTICLES ────────────────────────────────────────────────────────
#
# Each entry is a Zacks-style structured research report. Required keys:
#   id, ticker, date, category, title, summary
# Optional but strongly recommended:
#   featured (bool — only one should be True at any time; flagged daily highlight)
#   report   (dict — structured sections rendered by the rich blog template)
#   content  (HTML — long-form appendix shown beneath the structured report)
#
# Adding a new daily article:
#   1. Run:   python scripts/new_article.py
#   2. Answer the prompts; copy the printed dict.
#   3. Paste at the TOP of this list (newest first).
#   4. Set "featured": True on the new one and False on the previous featured.
#
# The /api/blog endpoint sorts by date desc and returns the explicitly-featured
# article first. No daily rotation magic — editor controls what's featured.
# ─────────────────────────────────────────────────────────────────────────────

_BLOG_ARTICLES = [
  {
    "id": "apld-ai-data-center-2026",
    "ticker": "APLD", "date": "2026-04-26",
    "category": "AI Infrastructure",
    "title": "Applied Digital: Building the Backbone of the AI Supercycle",
    "summary": "With a 1.5 GW AI data center pipeline and $28B in contracted hyperscaler deals, APLD is positioned at the physical intersection of every AI workload on earth — but capital intensity and customer concentration mean position sizing matters.",
    "featured": True,   # editorial choice — manually flagged

    # NEW: Zacks-style structured research report
    # When this `report` field is present, the renderer shows a rich multi-
    # section research-report layout. The legacy `content` HTML field is kept
    # below as a fallback / appendix.
    "report": {
      "rating":        "Top Tier",
      "conviction":    "High",
      "price_target":  18.50,
      "current_price": 14.20,
      "horizon":       "12-18 months",
      # 3-5 thesis bullets — the "why" in plain English
      "thesis": [
        "1.5 GW contracted AI data center pipeline locks in revenue visibility through 2030 — one of the largest pure-play AI infrastructure operators in N. America.",
        "$28B in signed hyperscaler deals ($6.8B Anthropic, $21B Meta) provide an unusually deep contracted backlog vs ~$200M trailing revenue.",
        "+139% revenue growth YoY with gross margins expanding as new capacity comes online at scale — operating leverage is starting to show.",
        "Scarcity moat: purpose-built high-density (30-100+ kW/rack) GPU campuses in cheap-power locations are difficult and slow to replicate.",
      ],
      # Most recent quarter snapshot
      "earnings": {
        "quarter":         "Q1 FY2026",
        "revenue":         "$58M",
        "revenue_growth":  "+139% YoY",
        "eps_actual":      "-$0.12",
        "eps_estimate":    "-$0.18",
        "surprise":        "+33%",
        "guidance":        "FY2026 revenue guidance raised to $250-280M (was $220-250M).",
      },
      # Valuation analysis
      "valuation": {
        "verdict":   "Premium but justified",
        "metrics":   [
          {"label": "EV/Sales (fwd)",   "value": "8.4×"},
          {"label": "Sector median",    "value": "5.2×"},
          {"label": "Premium to peers", "value": "+60%"},
          {"label": "Fwd P/E",          "value": "n/m (loss)"},
        ],
        "summary": "Trading at a premium 8.4× EV/Sales vs sector median 5.2×. The 60% premium reflects superior growth (+139% vs sector ~30%) and unusually high revenue visibility from the contracted backlog. Premium contracts with growth — if growth slows, the multiple compresses fast.",
      },
      # Competitive moat / industry position
      "moat": {
        "headline": "Scarcity moat — purpose-built high-density AI capacity is hard to replicate",
        "points": [
          "Top-3 pure-play AI data center operator in North America.",
          "Multi-year lead times on new builds create a competitive barrier — even well-funded entrants can't catch up quickly.",
          "Cheap renewable-power locations (North Dakota, Texas) deliver industry-leading PUE — replicable in theory but slow in practice.",
          "Direct relationships with hyperscalers create switching friction once contracts are signed.",
        ],
      },
      # Key risks (bear case)
      "risks": [
        "Capital intensity: -$720M TTM FCF; relies on continuous access to debt and equity markets to fund construction.",
        "Customer concentration: top two customers (Anthropic, Meta) account for 70%+ of contracted backlog. Loss or renegotiation of either is material.",
        "Short interest stands at ~29% of float — creates squeeze risk in both directions and elevated price volatility.",
        "Macro sensitivity: any softening in hyperscaler AI capex spend, or rising data-center financing costs, slows growth materially.",
      ],
      # Forward outlook + estimate revisions
      "forecast": {
        "revenue_next_year":    "$280M consensus, $300-320M bull case",
        "revenue_year_after":   "$650M+ as Meta contract begins to ramp",
        "estimate_revisions":   "Estimates raised by 4 of 6 analysts in last 30 days — Strongly Bullish revisions trend.",
        "key_catalysts":        "Q2 FY2026 earnings (~late Jul); Meta contract first revenue recognition (Q4 FY2026); potential additional hyperscaler deal announcements.",
      },
      # Final recommendation
      "recommendation": {
        "verdict": "Top Tier",
        "summary": "Best-positioned pure-play in the AI infrastructure buildout. Use any 10-15% pullback as an accumulation opportunity — but size for the elevated volatility (29% short interest). Aggressive growth investors only; avoid if you can't tolerate a 30%+ drawdown in a bad quarter.",
      },
    },

    # Legacy long-form HTML content — kept as a fallback for the old renderer
    # and as supplemental context below the structured report.
    "content": """<p>The artificial intelligence supercycle is not a software story — it is a real estate and power story. Every token generated by ChatGPT, Claude, or Gemini flows through hundreds of thousands of GPUs that need to be housed, cooled, and powered. <strong>Applied Digital Corporation (APLD)</strong> is building those houses.</p>

<h3>The Business</h3>
<p>Applied Digital designs and operates next-generation AI data centers in the United States, purpose-built for high-density GPU cluster workloads. Unlike traditional co-location facilities, APLD campuses are engineered from the ground up for the extreme power densities (30–100+ kW per rack) that AI training demands. Its flagship sites in North Dakota and Texas leverage cheap renewable power and cool climates to deliver industry-leading power usage effectiveness.</p>

<h3>The Pipeline</h3>
<p>APLD's contracted pipeline now exceeds <strong>1.5 GW of AI data center capacity</strong>, making it one of the largest pure-play AI infrastructure developers in North America. In fiscal 2026, the company secured its landmark deal: a <strong>$6.8 billion contract with Anthropic</strong> — backed by Amazon — followed by a separate <strong>$21 billion agreement with Meta</strong> for dedicated GPU cloud capacity through 2030. These two contracts alone lock in revenue visibility that most data center operators would envy.</p>

<h3>The Numbers</h3>
<p>Revenue grew <strong>+139% year-over-year</strong> in the most recent quarter, driven by the rapid lease-up of completed data center capacity. The company carries a heavy capital expenditure load (free cash flow is deeply negative at -$720M TTM) as it builds out its next generation of campuses, but this is deliberate: APLD is in the land-grab phase of what it believes is a decade-long infrastructure cycle. Gross margins are expanding as more capacity comes online at scale.</p>

<h3>Technical Setup</h3>
<p>APLD's Alpha Score of <strong>77</strong> reflects strong price momentum (+11% day, +87% over the past month for the model portfolio entry) alongside Grade A fundamentals. RSI sits in the ideal 58–65 zone, suggesting the stock has digested its recent gains without becoming overbought. Social mention velocity is <strong>+136% vs 24 hours ago</strong>, indicating growing retail and institutional awareness.</p>

<h3>Risks</h3>
<p>Capital intensity is the primary risk — APLD must continuously access debt and equity markets to fund construction. Any softening in AI capex spend by the hyperscalers, or a rise in data center financing costs, could materially slow the company's growth trajectory. Short interest stands at <strong>29.4%</strong>, making this a squeeze candidate in both directions.</p>

<h3>AlphaHunt View</h3>
<p>APLD earns a <strong>TOP TIER</strong> grade with 71% confidence. The combination of contracted hyperscaler revenue, explosive top-line growth, and infrastructure scarcity in the AI buildout makes this one of the highest-conviction names in our universe. Entry discipline matters here — the stock is volatile and position sizing should account for the elevated short interest.</p>"""
  },
  {
    "id": "vrt-ai-cooling-2026",
    "ticker": "VRT", "date": "2026-04-25",
    "category": "AI Infrastructure",
    "title": "Vertiv Holdings: Every AI Rack Needs a Thermal Solution",
    "summary": "Vertiv is the picks-and-shovels play on AI data center density — as GPU racks hit 100kW+ per cabinet, liquid cooling becomes non-negotiable.",
    "featured": False,
    "report": {
      "rating": "Top Tier", "conviction": "High",
      "price_target": 296, "current_price": 268, "horizon": "12-18 months",
      "thesis": [
        "AI rack power densities are pushing past 100 kW per cabinet — air cooling is physically inadequate, making liquid cooling a forced upgrade across every new build.",
        "Vertiv's liquid cooling revenue is growing 100%+ annually with a TAM heading to $4.7B by 2027 from near-zero in 2022.",
        "Order backlog has expanded for 8 consecutive quarters — a leading indicator of sustained revenue acceleration into 2026 and beyond.",
        "Gross margins are expanding as product mix shifts toward higher-value liquid cooling and software solutions.",
      ],
      "earnings": {
        "quarter": "Most recent quarter",
        "revenue_growth": "+41.8% YoY",
        "guidance": "Management guides for continued double-digit organic growth through 2027.",
      },
      "valuation": {
        "verdict": "Premium but justified",
        "metrics": [
          {"label": "Analyst mean target", "value": "$296"},
          {"label": "Bull case target",    "value": "$390"},
          {"label": "Implied upside",      "value": "+10%"},
        ],
        "summary": "Trading near analyst mean target — the easy money has been made. Bull case ($390) requires the liquid-cooling TAM to inflect faster than current consensus.",
      },
      "moat": {
        "headline": "Mission-critical infrastructure with deep hyperscaler relationships",
        "points": [
          "Serves every major hyperscaler, colo, and enterprise data center on earth.",
          "Custom liquid-cooling components have multi-quarter lead times that lock in customers.",
          "Schroff/Hoffman/Eldon brand portfolio — over 100 years of cumulative industrial relationships.",
        ],
      },
      "risks": [
        "Supply-chain constraints on custom components can delay revenue recognition by quarters.",
        "Schneider Electric and Eaton are intensifying competition as the TAM grows.",
        "Disproportionate exposure to hyperscaler capex — any slowdown hits the order book hard.",
      ],
      "forecast": {
        "estimate_revisions": "Estimates trending up; 4 of 6 covering analysts raised in last 30 days.",
        "key_catalysts":      "Next earnings; Blackwell GB200 cooling design wins; new liquid-cooling product launches.",
      },
      "recommendation": {
        "verdict": "Top Tier",
        "summary": "The structural growth story remains intact. Add on any 8-10% pullback. Lower-volatility way to play AI infrastructure than data-center REITs.",
      },
    },
    "content": """<p>When NVIDIA ships a rack of H100s to a hyperscaler, that rack generates enough heat to warm several homes simultaneously. Managing that heat — preventing it from destroying hundreds of thousands of dollars in silicon — is the domain of <strong>Vertiv Holdings (VRT)</strong>, the world's leading provider of critical digital infrastructure solutions.</p>

<h3>The Business</h3>
<p>Vertiv designs, manufactures, and services power management systems, thermal management solutions, and IT infrastructure for data centers globally. Its products include precision air conditioning units, liquid cooling systems, uninterruptible power supplies (UPS), switchgear, and remote monitoring software. Vertiv serves every major hyperscaler, colocation provider, and enterprise data center operator on earth.</p>

<h3>The AI Tailwind</h3>
<p>Traditional data center racks run at 5–10 kW per cabinet. AI GPU racks run at 30–100 kW and are trending toward 130 kW with NVIDIA's Blackwell architecture. At these densities, air cooling becomes physically impossible — liquid cooling becomes mandatory. Vertiv's liquid cooling revenue is growing at over <strong>100% annually</strong>, and the company estimates its AI-specific addressable market will reach $<strong>4.7 billion by 2027</strong>, up from virtually nothing in 2022.</p>

<h3>The Numbers</h3>
<p>Revenue grew <strong>+41.8% year-over-year</strong>, with management guiding for continued double-digit organic growth through 2027. Gross margins are expanding as the product mix shifts toward higher-value liquid cooling and software. The company's order backlog — a leading indicator — has grown every quarter for eight consecutive quarters. Analyst consensus price target is <strong>$296 (mean)</strong>, with high-end estimates reaching $390.</p>

<h3>Technical Setup</h3>
<p>VRT carries a Alpha Score of <strong>76</strong> with an RS Rating of 67 — showing solid but not extreme momentum. The stock is within <strong>1.3% of its 52-week high</strong>, suggesting institutional accumulation rather than speculative froth. EPS beat 4 of the last 4 quarters, with gross margins expanding confirming operating leverage is building.</p>

<h3>Risks</h3>
<p>Vertiv is not immune to supply chain constraints — long lead times on custom cooling components can delay revenue recognition. Competition from Schneider Electric and Eaton is intensifying as the market grows. Any slowdown in hyperscaler capex would disproportionately affect Vertiv's order book.</p>

<h3>AlphaHunt View</h3>
<p>Vertiv earns a <strong>TOP TIER</strong> grade. As data center power density continues its upward march, Vertiv's liquid cooling business becomes increasingly mission-critical. This is a structural growth story, not a cyclical trade.</p>"""
  },
  {
    "id": "aaoi-optical-ai-2026",
    "ticker": "AAOI", "date": "2026-04-24",
    "category": "Optical Components",
    "title": "Applied Optoelectronics: The Speed of Light in the AI Network",
    "summary": "AAOI makes the fiber optic transceivers that connect AI GPUs at 800G/1.6T speeds — a market growing 10x as hyperscalers build AI clusters.",
    "featured": False,
    "report": {
      "rating": "Top Tier", "conviction": "High",
      "price_target": 145, "current_price": 102, "horizon": "12-18 months",
      "thesis": [
        "AI training clusters need ultra-low-latency optical interconnects — industry rapidly migrating from 400G to 800G with 1.6T deployments starting 2026.",
        "AAOI is one of a small handful of vertically-integrated transceiver makers — short list of qualified hyperscaler suppliers gives pricing power.",
        "Co-packaged optics (CPO) opportunity opening as the next leg — 70% lower power vs discrete transceivers, $5B+ TAM by 2028.",
        "Volume 2.7× the 20-day average + EPS beat 4 consecutive quarters confirms institutional accumulation.",
      ],
      "earnings": {
        "quarter": "Most recent quarter",
        "revenue_growth": "+59.4% YoY",
        "guidance": "AI data center segment now the majority of sales and growing faster than overall company.",
      },
      "valuation": {
        "verdict": "Fair on growth",
        "metrics": [
          {"label": "Analyst mean target", "value": "$145"},
          {"label": "Implied upside",      "value": "+42%"},
        ],
        "summary": "Trades at premium multiples but +59% revenue growth justifies the multiple if it sustains. Volatile small cap — earnings reactions can be ±20%.",
      },
      "moat": {
        "headline": "Vertically-integrated transceiver supplier with 800G design wins",
        "points": [
          "In-house manufacturing allows custom optical specs hyperscalers demand.",
          "Qualified short-list supplier — switching costs are real after design-in.",
          "CPO product roadmap positions for the next-gen architecture inflection.",
        ],
      },
      "risks": [
        "$3.5B small cap with concentrated customer exposure — single hyperscaler delay can wreck a quarter.",
        "Gross margins still below larger peers (Coherent, Lumentum, II-VI).",
        "Subject to inventory cycles in the broader optical market.",
      ],
      "forecast": {
        "estimate_revisions": "Estimates rising as 800G ramp continues to surprise.",
        "key_catalysts":      "1.6T transceiver design wins; CPO commercial milestones; Q2 earnings.",
      },
      "recommendation": {
        "verdict": "Top Tier",
        "summary": "High-conviction small cap with structural tailwinds. Size for volatility — 5-7% of an aggressive growth book, not 15%.",
      },
    },
    "content": """<p>Inside every AI data center, thousands of GPUs must communicate with each other at speeds measured in hundreds of gigabits per second. The components enabling this communication — high-speed optical transceivers — are manufactured by a small number of specialized companies. <strong>Applied Optoelectronics (AAOI)</strong> is one of them, and the AI infrastructure buildout is transforming its financial trajectory.</p>

<h3>The Business</h3>
<p>AAOI designs, manufactures, and sells fiber optic networking components including transceivers, transmitters, and receivers for data center, broadband, and cable TV applications. Its products enable the high-speed data transmission backbone of modern cloud infrastructure. The company operates vertically integrated manufacturing facilities capable of producing custom components for demanding optical specifications.</p>

<h3>The 800G Revolution</h3>
<p>AI model training requires massive GPU clusters connected by ultra-low-latency optical networks. The industry is rapidly migrating from 400G to <strong>800G transceivers</strong>, with 1.6T deployments beginning in 2026. AAOI's 800G products have seen explosive demand as hyperscalers build out AI training infrastructure. Revenue grew <strong>+59.4% year-over-year</strong>, with the AI data center segment accounting for a growing majority of sales.</p>

<h3>The Co-Packaged Optics Opportunity</h3>
<p>Beyond pluggable transceivers, AAOI is positioning for the next generation: co-packaged optics (CPO), which integrates optical components directly onto switch silicon. CPO reduces power consumption by up to 70% versus discrete transceivers — a critical advantage as AI clusters push power density limits. The CPO market is expected to exceed <strong>$5 billion by 2028</strong>.</p>

<h3>Technical Setup</h3>
<p>AAOI is one of the strongest momentum names in our universe, with a Alpha Score of <strong>75</strong> and RS Rating of 75. The stock has gained +18.4% in today's session on volume 2.7x the 20-day average — institutional accumulation signal confirmed. EPS has beaten estimates 4 consecutive quarters with expanding gross margins, confirming the revenue growth is translating to the bottom line.</p>

<h3>Risks</h3>
<p>AAOI is a small-cap ($3.5B market cap) with concentrated customer exposure — a single hyperscaler delaying orders can materially impact quarterly results. Gross margins, while improving, remain below peer levels. Competition from Coherent, Lumentum, and II-VI is intensifying.</p>

<h3>AlphaHunt View</h3>
<p>AAOI earns a <strong>TOP TIER</strong> grade with 75 Alpha Score. The 800G/1.6T upgrade cycle is a multi-year tailwind with AAOI positioned as a beneficiary. Analyst mean target implies <strong>+41.7% upside</strong> from current levels.</p>"""
  },
  {
    "id": "mu-hbm-ai-2026",
    "ticker": "MU", "date": "2026-04-23",
    "category": "Semiconductors",
    "title": "Micron Technology: HBM Memory Is the AI Bottleneck No One Talks About",
    "summary": "Every NVIDIA H100/H200 AI GPU contains Micron's High Bandwidth Memory. As AI cluster deployments accelerate, HBM supply remains severely constrained.",
    "featured": False,
    "report": {
      "rating": "Top Tier", "conviction": "High",
      "price_target": 533, "current_price": 476, "horizon": "12 months",
      "thesis": [
        "HBM3E is the memory bottleneck for AI accelerators — Micron is one of only 3 companies on earth that can manufacture it at scale.",
        "Management has confirmed HBM supply is sold out through 2025 — pricing power is real, not theoretical.",
        "HBM revenue projected to grow from near-zero in 2023 to $8B+ by FY2027 as NVIDIA H200 + GB200 platforms ramp.",
        "Data center revenue now over 50% of total — Micron has structurally shifted from PC commodity to AI infrastructure play.",
      ],
      "earnings": {
        "quarter": "Most recent quarter",
        "revenue_growth": "Strongly positive — recovery from 2023 memory downturn confirmed",
        "guidance": "HBM supply remains constrained through 2025; gross margins expanding as mix shifts to premium HBM.",
      },
      "valuation": {
        "verdict": "Premium — expectations are elevated",
        "metrics": [
          {"label": "Fwd P/E",           "value": "76.9×"},
          {"label": "Analyst mean tgt",  "value": "$533"},
          {"label": "Implied upside",    "value": "+12%"},
        ],
        "summary": "76.9× fwd P/E reflects elevated expectations. The HBM ramp must deliver — any miss compresses the multiple fast.",
      },
      "moat": {
        "headline": "One of three global HBM manufacturers — strategic US asset",
        "points": [
          "Only US-headquartered memory manufacturer of scale — strategic relevance beyond pure financials.",
          "HBM is fab-constrained globally — capacity additions take 18-24 months.",
          "NVIDIA qualification on H200 + GB200 platforms locks in revenue visibility.",
        ],
      },
      "risks": [
        "Memory is structurally cyclical — pricing can roll over fast in a demand soft patch.",
        "Samsung HBM qualification by NVIDIA would create competitive pressure.",
        "Trades at 76.9× forward earnings — multiple compression risk if growth disappoints.",
      ],
      "forecast": {
        "estimate_revisions": "Estimates have been raised by majority of analysts over last 60 days.",
        "key_catalysts":      "Next earnings; NVIDIA Blackwell ramp; HBM4 capacity announcements.",
      },
      "recommendation": {
        "verdict": "Top Tier",
        "summary": "Most defensible structural growth story in semiconductors right now. Use 8-10% pullbacks to add. Reduce on any sign HBM tightness is easing.",
      },
    },
    "content": """<p>The most powerful AI accelerators in the world — NVIDIA's H100 and H200 GPUs — are fundamentally limited not by compute but by memory bandwidth. The technology solving this bottleneck is High Bandwidth Memory (HBM), and <strong>Micron Technology (MU)</strong> is one of only three companies in the world that can manufacture it.</p>

<h3>The Business</h3>
<p>Micron designs and manufactures DRAM, NAND flash, and NOR flash memory products for data centers, PCs, smartphones, automotive, and industrial applications. With $30+ billion in annual revenue, Micron is the only U.S.-headquartered memory manufacturer and a critical component of American semiconductor sovereignty.</p>

<h3>The HBM3E Inflection</h3>
<p>HBM3E — the current generation of High Bandwidth Memory — delivers 1.2 TB/s of bandwidth to AI accelerators, enabling the parallel computation needed for training large language models. Micron's HBM3E has been qualified by NVIDIA for the H200 and GB200 Blackwell platforms, unlocking a market that is growing from $4B in 2024 to an estimated <strong>$30B+ by 2028</strong>. Micron management has stated that HBM supply is <em>sold out through 2025</em>.</p>

<h3>The Numbers</h3>
<p>Revenue is recovering strongly from the memory downturn, with data center revenue now representing over 50% of total sales. The Street estimates Micron's HBM revenue will grow from near-zero in 2023 to over <strong>$8B annually by fiscal 2027</strong>. EPS beat expectations for 4 consecutive quarters, with gross margin expansion as the product mix shifts toward premium HBM. Analyst mean target: <strong>$533 (+11.9% upside)</strong>.</p>

<h3>Technical Setup</h3>
<p>MU carries a Alpha Score of <strong>76</strong> and RS Rating of 73. Today's -2.18% session is noise against a backdrop of +30.6% gains over the past month. RSI at 64 remains healthy — not overbought. Social mention velocity +231% vs 24h confirms the stock is on investors' radar.</p>

<h3>Risks</h3>
<p>Memory is a commodity industry with cyclical pricing dynamics. A slowdown in AI infrastructure investment or a resolution of HBM supply tightness could compress margins. Samsung's HBM qualification by NVIDIA would create additional competitive pressure. The stock trades at 76.9× forward earnings — expectations are high.</p>

<h3>AlphaHunt View</h3>
<p>MU earns a <strong>TOP TIER</strong> grade. The HBM supercycle is the most defensible structural growth story in semiconductors. Micron's position as the U.S. champion in a market dominated by Samsung and SK Hynix gives it strategic importance beyond pure financials.</p>"""
  },
  {
    "id": "qubt-quantum-2026",
    "ticker": "QUBT", "date": "2026-04-22",
    "category": "Quantum Computing",
    "title": "Quantum Computing Inc: Riding the Second Quantum Wave",
    "summary": "QUBT builds quantum hardware and software for real-world optimization problems. IBM's 2026 practical quantum milestone is catalyzing the entire sector.",
    "featured": False,
    "report": {
      "rating": "Top Tier", "conviction": "Medium",
      "price_target": 17.40, "current_price": 8.85, "horizon": "18-24 months",
      "thesis": [
        "Quantum sector is transitioning from research curiosity to real infrastructure investment — IBM's 2026 practical-advantage milestone validated the timeline.",
        "QUBT's software-first approach (network optimization, logistics, portfolio optimization) generates revenue today while hardware matures — most pure-play peers don't.",
        "DARPA US2QC program funding accelerates the entire sector — QUBT's customer base sits in the same ecosystem.",
        "EPS beat 4 of last 4 quarters with gross margins expanding as software mix grows.",
      ],
      "earnings": {
        "quarter": "Most recent quarter",
        "revenue_growth": "Growing rapidly from a small base",
        "guidance": "Several enterprise customers actively deployed; software margins expanding.",
      },
      "valuation": {
        "verdict": "Pre-profit speculative",
        "metrics": [
          {"label": "Analyst mean tgt", "value": "$17.40"},
          {"label": "Implied upside",   "value": "+97%"},
        ],
        "summary": "Pre-profit, valuation must be option-value-based. Analyst targets imply ~2× — but with quantum, timeline slippage is a real risk.",
      },
      "moat": {
        "headline": "Software + photonics combination targets near-term commercial use cases",
        "points": [
          "Quantum-inspired software runs on classical hardware today — buys time while quantum scales.",
          "Photonic device portfolio differentiates from superconducting incumbents.",
          "Enterprise customer deployments build sales evidence other small-cap peers lack.",
        ],
      },
      "risks": [
        "$3.5B small cap in nascent technology — quantum timelines have historically slipped.",
        "Profitability still in the future — funding cycle dependence.",
        "Short interest 28.6% creates squeeze risk in both directions.",
        "Negative quantum hardware milestone news can crash the stock 20-30% in a session.",
      ],
      "forecast": {
        "estimate_revisions": "Limited analyst coverage; revisions sparse but trending positive.",
        "key_catalysts":      "DARPA US2QC milestones; new enterprise customer wins; sector reratings on hardware breakthroughs.",
      },
      "recommendation": {
        "verdict": "Top Tier (speculative)",
        "summary": "High-conviction speculative position. Size 1-3% of book max. Use stops — sector volatility is extreme. This is an option, not a core holding.",
      },
    },
    "content": """<p>Quantum computing has been five years away for twenty years. But 2026 is different. IBM's announcement that quantum computers will demonstrate practical advantage over classical systems on specific optimization tasks has catalyzed a new wave of investment, and <strong>Quantum Computing Inc (QUBT)</strong> is one of the most accessible pure-play expressions of this inflection.</p>

<h3>The Business</h3>
<p>QUBT develops quantum photonic devices, quantum software, and quantum optimization services for commercial customers. Unlike IBM, Google, or IonQ, QUBT focuses on near-term commercial applications — network optimization, logistics, financial portfolio optimization — where quantum-inspired algorithms deliver measurable value today while the hardware matures.</p>

<h3>The Catalyst Window</h3>
<p>The US Defense Advanced Research Projects Agency's US2QC program is accelerating error correction research, with multiple teams claiming fault-tolerant qubit demonstrations in controlled environments. DARPA has funded programs targeting practical quantum advantage by 2033 — but early commercial applications are emerging much sooner. QUBT's optimization products are already deployed at several enterprise customers.</p>

<h3>Technical Setup</h3>
<p>QUBT carries a Alpha Score of <strong>72</strong> with an RS Rating of 76 — indicating it is outperforming 76% of all stocks in the AlphaHunt universe over the past 12 months. The stock is within 22.4% of its 52-week high after consolidating a major prior breakout. EPS beat 4 of the last 4 quarters, and gross margin is expanding as the software mix grows. Short interest at <strong>28.6%</strong> makes this a high-volatility, high-conviction setup.</p>

<h3>The Risks</h3>
<p>QUBT is a small-cap ($3.5B market cap) company in a nascent technology sector where timelines have historically slipped. Revenue is growing but from a small base, and profitability is still in the future. Any negative news about quantum hardware milestones could create significant stock volatility.</p>

<h3>AlphaHunt View</h3>
<p>QUBT earns a <strong>TOP TIER</strong> grade. The quantum computing sector is transitioning from research curiosity to real infrastructure investment. QUBT's software-first approach to monetization gives it a near-term revenue path that pure hardware plays lack. Analyst target of <strong>$17.40 (mean)</strong> implies +96.9% upside from current levels.</p>"""
  },
  {
    "id": "gsat-satellite-5g-2026",
    "ticker": "GSAT", "date": "2026-04-21",
    "category": "Satellite Communications",
    "title": "Globalstar: Direct-to-Device 5G Is Coming to Every iPhone",
    "summary": "Globalstar's satellite network is being upgraded to deliver 5G connectivity directly to mobile devices — Apple's partnership could be transformational.",
    "featured": False,
    "report": {
      "rating": "Top Tier", "conviction": "Medium",
      "price_target": 85, "current_price": 80, "horizon": "12-18 months",
      "thesis": [
        "Apple partnership is multi-year revenue floor — Emergency SOS via Satellite anchors a recurring capacity contract.",
        "Direct-to-device 5G via satellite is the next frontier — Globalstar's spectrum + BlueBird constellation positions it as the underlying infrastructure.",
        "Spectrum approval secured in 48 countries — regulatory moat against late entrants.",
        "Breaking to 52-week high on volume = institutional accumulation pattern; +35% over 30 days.",
      ],
      "earnings": {
        "quarter": "Most recent quarter",
        "revenue_growth": "Steady recurring base + Apple anchor",
        "guidance": "BlueBird satellites deploying through 2026-27 enable next phase of 5G capacity.",
      },
      "valuation": {
        "verdict": "Fair near term, optionality on Apple expansion",
        "metrics": [
          {"label": "Analyst mean tgt", "value": "$85"},
          {"label": "Implied upside",   "value": "+6%"},
          {"label": "RS Rating",        "value": "81"},
        ],
        "summary": "Conservative analyst targets don't fully model Apple direct-to-device 5G optionality — could be the upside surprise of 2026.",
      },
      "moat": {
        "headline": "Apple-locked spectrum + LEO satellite constellation",
        "points": [
          "Apple selected GSAT for Emergency SOS — extremely high switching cost given iPhone integration.",
          "Spectrum approvals in 48 countries take competitors years to replicate.",
          "BlueBird constellation specifically engineered for low-latency 5G service delivery.",
        ],
      },
      "risks": [
        "Apple could in-source via own constellation in 5+ years.",
        "BlueBird capex is heavy and execution risk is real.",
        "AT&T/Verizon partner economics not fully disclosed.",
      ],
      "forecast": {
        "estimate_revisions": "Estimates raised modestly as Apple capacity contract details surface.",
        "key_catalysts":      "BlueBird launches; Apple direct-to-device 5G announcements; new carrier partnerships.",
      },
      "recommendation": {
        "verdict": "Top Tier",
        "summary": "Best risk/reward in the satellite space. Apple anchor caps the downside; direct-to-device 5G is the upside lottery ticket.",
      },
    },
    "content": """<p>The dream of true global connectivity — the ability to make a call or send a message from anywhere on Earth without relying on terrestrial cell towers — is becoming reality. <strong>Globalstar (GSAT)</strong> sits at the center of this transition, with a unique satellite infrastructure and a transformative partnership with the world's most valuable company.</p>

<h3>The Business</h3>
<p>Globalstar operates a constellation of Low Earth Orbit (LEO) satellites providing voice, data, and IoT connectivity services globally. The company serves government, maritime, aviation, and consumer customers. Its legacy business generates steady recurring revenue from satellite phone and IoT subscriptions.</p>

<h3>The Apple Partnership</h3>
<p>In 2022, Apple selected Globalstar to power the Emergency SOS via Satellite feature on iPhone 14 and later models. This partnership has two transformational implications: first, it provides GSAT with guaranteed anchor revenue through a multi-year capacity agreement; second, it positions GSAT's satellite spectrum as a critical piece of Apple's next evolution — <strong>direct-to-device 5G</strong> connectivity that could eventually deliver cellular data to iPhones anywhere on earth without cell towers.</p>

<h3>BlueBird Constellation</h3>
<p>Globalstar is investing in a new generation of satellites branded "BlueBird" designed for higher-capacity, lower-latency 5G service delivery. The BlueBird constellation, combined with regulatory spectrum approvals in 48 countries, positions GSAT to become the backbone of satellite-cellular integration for partner carriers including AT&T and Verizon.</p>

<h3>Technical Setup</h3>
<p>GSAT carries a Alpha Score of <strong>72</strong> with RS Rating 81 — the highest RS in this analysis. Breaking to a new 52-week high confirms institutional accumulation. The stock has gained +35.2% over the past month. EPS beat 4 consecutive quarters. Analyst mean target of <strong>$85 (+5.6% upside)</strong> is conservative given the optionality of the Apple partnership.</p>

<h3>AlphaHunt View</h3>
<p>GSAT earns a <strong>TOP TIER</strong> grade. The Apple partnership provides a floor, while direct-to-device 5G represents a potential ceiling that most analysts have yet to fully model.</p>"""
  },
  {
    "id": "crdo-serdes-ai-2026",
    "ticker": "CRDO", "date": "2026-04-20",
    "category": "AI Semiconductors",
    "title": "Credo Technology: The SerDes Company Powering AI Cluster Interconnects",
    "summary": "Inside every AI supercluster, Credo's SerDes chiplets and optical DSPs move data at 800G+ between GPUs — a market growing 10x in the next three years.",
    "featured": False,
    "report": {
      "rating": "Top Tier", "conviction": "High",
      "price_target": 200, "current_price": 99, "horizon": "12-18 months",
      "thesis": [
        "Inter-GPU bandwidth requirements grow quadratically with cluster size — Credo's SerDes IP and AECs are the physical layer enabling the AI cluster scale-up.",
        "Industry migrating 400G → 800G → 1.6T — Credo is on the design-win shortlist at multiple hyperscalers for the 800G generation.",
        "SerDes is a winner-take-most market — 112 Gbps per lane and beyond require deep IP that very few teams can deliver.",
        "EPS beat 3 of 4 last quarters with gross margins expanding; momentum +88% over 30 days.",
      ],
      "earnings": {
        "quarter": "Most recent quarter",
        "revenue_growth": "Multi-quarter acceleration as hyperscaler ramps land",
        "guidance": "AEC and optical DSP design wins translating into volume revenue in next 2-3 quarters.",
      },
      "valuation": {
        "verdict": "Premium — momentum tax",
        "metrics": [
          {"label": "Analyst mean tgt", "value": "$200"},
          {"label": "Bull case target", "value": "$390"},
          {"label": "Implied upside",   "value": "+101%"},
        ],
        "summary": "Most bullish analyst setup in our universe — analyst mean implies 2× upside, bull case 4×. Premium price reflects scarcity of pure-play AI interconnect IP.",
      },
      "moat": {
        "headline": "Specialist SerDes + AEC IP — winner-take-most physics",
        "points": [
          "112 Gbps per lane SerDes is a deep IP moat — fewer than 5 firms globally can deliver it.",
          "AEC products designed for hyperscaler reach — replacing optics in shorter intra-rack runs.",
          "Direct hyperscaler relationships from design-in; replacement costs are extreme.",
        ],
      },
      "risks": [
        "Concentrated hyperscaler customer exposure — top 2 customers likely majority of revenue.",
        "Stock has run hard already — short-term pullback risk on any earnings hiccup.",
        "Potential competition from Marvell, Broadcom in adjacent SerDes territories.",
      ],
      "forecast": {
        "estimate_revisions": "Estimates revised up sharply over last 30 days; consensus chasing the price.",
        "key_catalysts":      "1.6T design wins; new hyperscaler customer announcements; quarterly earnings.",
      },
      "recommendation": {
        "verdict": "Top Tier",
        "summary": "Highest-conviction interconnect play in AI infrastructure. Use 12-15% pullbacks to add — trim 20% on any 50%+ rally to lock in gains.",
      },
    },
    "content": """<p>When NVIDIA assembles a DGX SuperPOD — a cluster of thousands of interconnected H100 GPUs — every GPU must communicate with every other GPU at blinding speeds. The semiconductor technology enabling this: Serializer/Deserializer (SerDes) chiplets and optical DSPs. <strong>Credo Technology (CRDO)</strong> is the specialist in this critical, underappreciated corner of AI infrastructure.</p>

<h3>The Business</h3>
<p>Credo designs high-speed connectivity chips including Active Electrical Cables (AECs), optical DSPs, and SerDes IP for data center applications. Its products sit at the physical layer of AI network architecture — the wires and chips that connect GPU to GPU at line rates of 112 Gbps per lane and beyond. Credo serves hyperscalers, OEMs, and AI infrastructure builders directly.</p>

<h3>The 800G/1.6T Upgrade Cycle</h3>
<p>As AI clusters scale from hundreds to tens of thousands of GPUs, the bandwidth requirements for inter-GPU communication grow quadratically. The industry is migrating from 400G to 800G interconnects now, with 1.6T on the horizon. Credo's AEC and optical DSP products are designed specifically for these next-generation speeds, and the company has secured design wins at multiple hyperscalers for the 800G generation.</p>

<h3>The Numbers</h3>
<p>Revenue grew <strong>+87.7% over the past month</strong> from the Model Portfolio's entry price perspective, with the stock up dramatically on positive earnings revisions and hyperscaler design win announcements. Analyst mean target of <strong>$200 (high: $390)</strong> implies 101.5% upside — one of the most bullish analyst setups in our universe. EPS has beaten estimates 3 of 4 last quarters with gross margins expanding.</p>

<h3>Technical Setup</h3>
<p>CRDO is the top performer in our Model Portfolio, with Alpha Score of <strong>71</strong> and one of the strongest momentum profiles in the AlphaHunt universe. RSI at 64 — ideal momentum zone — suggests room to run without overextension. High-speed SerDes is a winner-take-most market, and Credo has won.</p>

<h3>AlphaHunt View</h3>
<p>CRDO earns a <strong>TOP TIER</strong> grade. The SerDes and optical DSP market is growing at 40%+ annually driven purely by AI infrastructure demand. Credo's focused product line and hyperscaler relationships make it one of the highest-conviction plays in our universe.</p>"""
  },
  {
    "id": "soun-voice-ai-2026",
    "ticker": "SOUN", "date": "2026-04-19",
    "category": "Agentic AI",
    "title": "SoundHound AI: Voice Intelligence for the Agentic AI Era",
    "summary": "SoundHound's voice AI platform is deployed in 10,000+ cars, restaurant chains, and enterprise applications — and the agentic AI wave is creating new demand.",
    "featured": False,
    "report": {
      "rating": "Top Tier", "conviction": "Medium",
      "price_target": 14.63, "current_price": 8.20, "horizon": "12-18 months",
      "thesis": [
        "Agentic AI requires a conversational interface — SoundHound has been building voice AI for a decade, finally has the market.",
        "Automotive moat is real — embedded in Stellantis, Honda, Hyundai across 175+ countries; 5-7 year design cycles lock in royalties.",
        "Restaurant drive-thru AI is a hidden growth lever — labor-cost reduction at scale, multi-billion-dollar TAM.",
        "Revenue growing +59% YoY; EPS beat 4 consecutive quarters; short interest 36% creates squeeze potential.",
      ],
      "earnings": {
        "quarter": "Most recent quarter",
        "revenue_growth": "+59.4% YoY",
        "guidance": "Auto royalties accelerating; restaurant deployments expanding into thousands of locations.",
      },
      "valuation": {
        "verdict": "Pre-profit but trajectory-driven",
        "metrics": [
          {"label": "Analyst mean tgt", "value": "$14.63"},
          {"label": "Implied upside",   "value": "+78%"},
          {"label": "Short interest",   "value": "36%"},
        ],
        "summary": "Pre-profit name — judge on revenue trajectory and customer wins, not earnings multiples. Analyst implied upside is meaningful.",
      },
      "moat": {
        "headline": "Embedded automotive voice + edge-optimized AI for noisy environments",
        "points": [
          "Auto OEM design-ins with multi-year switching costs.",
          "Edge-deployment optimization (low latency, noise-resilient) differentiates from cloud LLM voice.",
          "Domain-specific knowledge for drive-thru voice ordering — accuracy advantage in the use case.",
        ],
      },
      "risks": [
        "Profitability still 6-8 quarters out — needs continued capital access.",
        "Apple/Amazon/Google voice could expand into auto if they prioritize it.",
        "Restaurant drive-thru economics still being validated at scale.",
        "36% short interest creates extreme volatility in both directions.",
      ],
      "forecast": {
        "estimate_revisions": "Mixed — bulls raising, bears holding firm; net upward.",
        "key_catalysts":      "New auto OEM design wins; restaurant chain rollouts; agentic AI partnership announcements.",
      },
      "recommendation": {
        "verdict": "Top Tier (speculative)",
        "summary": "Differentiated voice-AI play with real revenue growth. Size for volatility — 2-4% of book. Asymmetric upside on continued earnings beats given the short interest.",
      },
    },
    "content": """<p>Artificial intelligence is moving from answering questions to taking actions. This shift — from query-response to autonomous agents — requires a natural language interface that humans can interact with conversationally. <strong>SoundHound AI (SOUN)</strong> has been building this interface for over a decade, and the agentic AI wave is finally creating the market its technology was designed for.</p>

<h3>The Business</h3>
<p>SoundHound develops voice AI and conversational intelligence software deployed across automotive (in-vehicle assistants), restaurants (AI order-taking), healthcare (clinical documentation), and enterprise applications. Unlike GPT-style chatbots, SoundHound's technology is optimized for edge deployment — working in noisy environments, with domain-specific knowledge, and with sub-100ms response times.</p>

<h3>The Automotive Moat</h3>
<p>SoundHound's voice AI is embedded in vehicles from Stellantis, Honda, Hyundai, and dozens of other OEMs across <strong>175+ countries</strong>. Automotive design cycles run 5–7 years, meaning once SoundHound is designed in, it is extremely difficult to displace. The company receives royalties per vehicle per year — a recurring revenue stream that grows with global vehicle production.</p>

<h3>Restaurant and Enterprise</h3>
<p>SoundHound's Dynamic Drive-Thru platform handles voice ordering at quick-service restaurant chains, reducing labor costs and increasing order accuracy. The company has expanded to thousands of restaurant locations and is targeting the multi-billion dollar drive-through market. Enterprise customers use SoundHound's conversational AI for customer service automation.</p>

<h3>Technical Setup</h3>
<p>SOUN carries a Alpha Score of <strong>75</strong> with RS Rating 72 and an analyst mean price target of <strong>$14.63 (+78.4% upside)</strong>. Revenue growing at +59.4% YoY. Short interest at 36.1% creates significant squeeze potential. EPS beat 4 consecutive quarters.</p>

<h3>AlphaHunt View</h3>
<p>SOUN earns a <strong>TOP TIER</strong> grade. The combination of automotive moat, restaurant expansion, and agentic AI tailwind makes SoundHound one of the most differentiated AI plays in the market. The stock's high short interest creates asymmetric upside on continued strong results.</p>"""
  },
  {
    "id": "nvt-data-center-power-2026",
    "ticker": "NVT", "date": "2026-04-18",
    "category": "AI Infrastructure",
    "title": "nVent Electric: The Power Management Play on AI Data Centers",
    "summary": "nVent makes the enclosures, power distribution, and thermal management systems that every data center rack requires — invisible but indispensable.",
    "featured": False,
    "report": {
      "rating": "Top Tier", "conviction": "High",
      "price_target": 144.54, "current_price": 142, "horizon": "12 months",
      "thesis": [
        "Every server rack in every data center needs nVent enclosures, PDUs and cable management — quietly one of the broadest exposures to AI capex.",
        "Liquid-cooling solutions (rear-door heat exchangers, direct-to-chip) are the exact products needed as GPU racks push past 30 kW.",
        "Data center segment grew +41.8% YoY — fastest-growing piece of the portfolio and now the largest.",
        "EPS beat 4 of 4 last quarters with gross margins expanding — operating leverage is real.",
      ],
      "earnings": {
        "quarter": "Most recent quarter",
        "revenue_growth": "+41.8% YoY (data center segment)",
        "guidance": "Continued double-digit growth in DC; portfolio mix shifting to premium products.",
      },
      "valuation": {
        "verdict": "Fair to slightly stretched",
        "metrics": [
          {"label": "Analyst mean tgt", "value": "$144.54"},
          {"label": "Implied upside",   "value": "+0.7%"},
        ],
        "summary": "Stock has run past consensus targets — suggests upward revisions ahead but the easy money has been made. Add on pullbacks, not chasing.",
      },
      "moat": {
        "headline": "Industrial-grade reliability + diversified customer base across 100+ years",
        "points": [
          "Brand portfolio (Schroff, Hoffman, Eldon, Raychem) carries deep enterprise relationships.",
          "Custom enclosure manufacturing has long lead times — switching is non-trivial.",
          "Diversified across data center, industrial, commercial — less binary than pure-play DC names.",
        ],
      },
      "risks": [
        "Industrial cycles can hit the non-DC segments.",
        "Hyperscaler capex slowdown would compress the high-growth segment.",
        "Multiple expansion already happened — limited room for re-rating.",
      ],
      "forecast": {
        "estimate_revisions": "Estimates trending up after recent quarter's beat.",
        "key_catalysts":      "Liquid-cooling product launches; data center segment growth in next earnings.",
      },
      "recommendation": {
        "verdict": "Top Tier",
        "summary": "Lower-volatility AI-infrastructure play with broader exposure than pure-play DC stocks.",
      },
    },
    "content": """<p>Every server rack in every data center sits inside an enclosure. Every cable in that rack is managed and protected. Every power distribution unit feeding that rack must be sized correctly. These unglamorous but essential components are the domain of <strong>nVent Electric (NVT)</strong> — a $22.7B industrial technology company that has quietly become one of the best AI infrastructure picks-and-shovels plays available.</p>

<h3>The Business</h3>
<p>nVent designs and manufactures electrical enclosures, cable management systems, power distribution units (PDUs), and liquid cooling solutions for data centers, industrial, and commercial applications. Its brands include Schroff, Hoffman, Eldon, and Raychem. Data center revenue now represents the largest and fastest-growing segment of nVent's portfolio.</p>

<h3>The AI Tailwind</h3>
<p>As AI data centers are built at unprecedented scale, the need for high-density enclosures and liquid cooling infrastructure grows proportionally. nVent's liquid cooling solutions — including high-density rear-door heat exchangers and direct liquid cooling systems — are seeing explosive demand as GPU rack densities push beyond 30 kW per cabinet. The company's data center segment grew <strong>+41.8% YoY</strong>.</p>

<h3>The Numbers</h3>
<p>Revenue growth of +41.8% demonstrates the AI tailwind is flowing through to the financials. EPS has beaten estimates 4 consecutive quarters. Gross margins are expanding as the premium data center product mix grows. Analyst mean target: <strong>$144.54 (+0.7% conservative estimate)</strong> — the stock has outrun near-term analyst targets, suggesting upward revisions ahead.</p>

<h3>Technical Setup</h3>
<p>NVT has a Alpha Score of <strong>75</strong> and RS Rating 67, within 1.3% of its 52-week high — a classic institutional accumulation pattern. EPS beat 4 of 4 last quarters. Gross margin expanding confirms operating leverage. No overbought RSI concern.</p>

<h3>AlphaHunt View</h3>
<p>NVT earns a <strong>TOP TIER</strong> grade. nVent's combination of industrial-grade reliability, data center focus, and diverse customer base makes it one of the lower-risk ways to play the AI infrastructure buildout.</p>"""
  },
  {
    "id": "ionq-quantum-computing-2026",
    "ticker": "IONQ", "date": "2026-04-17",
    "category": "Quantum Computing",
    "title": "IonQ: Trapped Ion Quantum Computing and the Path to Fault Tolerance",
    "summary": "IonQ uses trapped ion technology to build the world's most accurate quantum computers. DARPA contracts and cloud partnerships are accelerating its commercial trajectory.",
    "featured": False,
    "report": {
      "rating": "Top Tier", "conviction": "Medium",
      "price_target": 17.83, "current_price": 9.05, "horizon": "18-24 months",
      "thesis": [
        "Trapped-ion qubits achieve 99.9%+ gate fidelities vs ~99.5% for superconducting — material advantage that compounds with algorithm depth.",
        "Available on AWS Braket, Azure Quantum, Google Cloud — enterprise customers can pilot without capex barrier.",
        "DARPA US2QC program participation provides funding and validation; US Air Force + UK NQCC contracts are real.",
        "Revenue growing rapidly from a small base; EPS beat 4 consecutive quarters.",
      ],
      "earnings": {
        "quarter": "Most recent quarter",
        "revenue_growth": "Strong from a small base",
        "guidance": "Cloud partnerships expanding; new commercial customers in finance and pharma.",
      },
      "valuation": {
        "verdict": "Pre-profit speculative — option value",
        "metrics": [
          {"label": "Analyst mean tgt", "value": "$17.83"},
          {"label": "Implied upside",   "value": "+97%"},
        ],
        "summary": "Implied upside is large but pure option-value — real revenue still 18-24 months from material levels.",
      },
      "moat": {
        "headline": "Trapped-ion technology advantage + cloud distribution",
        "points": [
          "Higher fidelity than competing approaches — material advantage as algorithms get deeper.",
          "Cloud-first distribution lowers customer adoption friction.",
          "Government contract validation is a forward indicator of commercial readiness.",
        ],
      },
      "risks": [
        "Quantum timeline slippage is a permanent risk.",
        "Competing approaches (superconducting, photonic) could leapfrog on hardware milestones.",
        "Pre-profit company depends on continued funding — dilution risk.",
      ],
      "forecast": {
        "estimate_revisions": "Estimates raised modestly after government contract wins.",
        "key_catalysts":      "Fault-tolerance milestones; new enterprise pilots; sector re-ratings.",
      },
      "recommendation": {
        "verdict": "Top Tier (speculative)",
        "summary": "Most investable pure-play quantum name. Highly volatile — score reflects this. Use as a sector option, not a core position.",
      },
    },
    "content": """<p>Not all quantum computers are built the same. Superconducting qubits (IBM, Google) operate near absolute zero and are susceptible to noise. Photonic systems (PsiQuantum) remain largely unproven at scale. Trapped ion systems — the approach championed by <strong>IonQ (IONQ)</strong> — offer the highest qubit fidelity of any current technology and a clearer path to fault-tolerant quantum computing.</p>

<h3>The Technology Advantage</h3>
<p>IonQ's trapped ion qubits use individual ytterbium atoms suspended in electromagnetic fields and manipulated with lasers. These ions are perfect, identical quantum systems — unlike manufactured superconducting qubits, which have fabrication variations. IonQ's systems achieve gate fidelities exceeding 99.9%, compared to typical superconducting system fidelities of 99.5%. In quantum computing, small fidelity differences translate to exponentially better algorithm performance.</p>

<h3>Commercial Traction</h3>
<p>IonQ's quantum computers are available on AWS Braket, Microsoft Azure Quantum, and Google Cloud — giving enterprise customers access without capital investment. The company has signed contracts with the U.S. Air Force Research Laboratory, the UK's NQCC, and multiple commercial customers in finance and pharmaceutical sectors. Revenue is growing rapidly from a small base.</p>

<h3>The DARPA Connection</h3>
<p>IonQ participates in the DARPA US2QC program, which is accelerating practical quantum computing timelines. Government funding provides both revenue and technology validation — critical for an early-stage quantum company.</p>

<h3>Technical Setup</h3>
<p>IONQ carries a Alpha Score of <strong>72</strong> with analyst mean target of <strong>$17.83 (+96.9% upside)</strong>. Within 22.4% of 52-week high after consolidation. EPS beat 4 consecutive quarters with expanding margins. Social momentum building as quantum milestones approach.</p>

<h3>AlphaHunt View</h3>
<p>IONQ earns a <strong>TOP TIER</strong> grade. The combination of technology superiority, government contracts, and cloud accessibility makes IonQ the most investable pure-play quantum computing company available to public market investors.</p>"""
  },
  {
    "id": "wdc-hdd-ai-storage-2026",
    "ticker": "WDC", "date": "2026-04-16",
    "category": "Storage",
    "title": "Western Digital: AI Storage Demand Is Breaking Every Capacity Record",
    "summary": "Training datasets for frontier AI models are growing 10x per generation. Western Digital's high-capacity HDDs and enterprise SSDs are the primary storage medium.",
    "featured": False,
    "report": {
      "rating": "Top Tier", "conviction": "High",
      "price_target": 354.96, "current_price": 405, "horizon": "12 months",
      "thesis": [
        "Frontier AI training datasets are growing 10× per generation — petabyte-scale multimodal corpora live on HDD; commodity cycle is now demand-driven.",
        "Hyperscaler 20-26 TB enterprise HDDs in structural undersupply — ASPs rising, not falling.",
        "Post-separation HDD pure play unlocks valuation re-rating from conglomerate discount.",
        "EPS beat 4 of 4 last quarters with gross margin expansion confirming the cycle is real.",
      ],
      "earnings": {
        "quarter": "Most recent quarter",
        "revenue_growth": "+67% from cycle bottom",
        "guidance": "HDD ASPs rising on premium-capacity mix; FY guidance raised.",
      },
      "valuation": {
        "verdict": "Has run past analyst consensus",
        "metrics": [
          {"label": "Analyst mean tgt", "value": "$354.96"},
          {"label": "Implied upside",   "value": "-12%"},
        ],
        "summary": "Stock trades 12% above analyst mean target. Either consensus chases up or stock pulls back. Don't chase here.",
      },
      "moat": {
        "headline": "Duopoly on mechanical HDDs at scale",
        "points": [
          "Only WDC and Seagate manufacture HDDs at scale — duopoly economics.",
          "Capacity expansion takes years; no new entrants possible.",
          "Hyperscaler relationships locked through long-cycle qualification.",
        ],
      },
      "risks": [
        "Memory/storage cycles have always been brutally cyclical eventually.",
        "Stock has outrun consensus targets — multiple compression on any miss.",
        "Flash/SSD substitution risk on the long horizon.",
      ],
      "forecast": {
        "estimate_revisions": "Estimates raised across the board after recent earnings.",
        "key_catalysts":      "Next earnings; new high-capacity HDD launches; hyperscaler capex updates.",
      },
      "recommendation": {
        "verdict": "Top Tier (above analyst consensus)",
        "summary": "Quality structural story but stock has gotten ahead of consensus. Wait for a 10-15% pullback before adding.",
      },
    },
    "content": """<p>Training GPT-4 required approximately 45 terabytes of text data. The next generation of frontier models is expected to train on <strong>petabyte-scale multimodal datasets</strong> — images, video, code, and text. That data has to live somewhere. <strong>Western Digital (WDC)</strong> makes the hard drives and SSDs where it lives.</p>

<h3>The Business</h3>
<p>Western Digital is one of two companies (alongside Seagate) that manufactures mechanical hard disk drives at scale. WDC also designs and sells NAND flash-based solid-state drives under the WD and SanDisk brands. Its products span consumer, enterprise, and cloud data center applications. The company completed the separation of its HDD and NAND businesses in late 2024 to unlock shareholder value.</p>

<h3>The AI Storage Supercycle</h3>
<p>AI data centers have unique storage requirements. Training workloads demand massive sequential read performance for dataset streaming. Inference workloads need fast random-access storage for model parameter serving. Both use cases benefit from Western Digital's enterprise product lines. Hyperscaler HDDs — 20TB, 22TB, and 26TB capacity points — are in structural undersupply as AI training farms expand globally.</p>

<h3>The Numbers</h3>
<p>Revenue grew <strong>+67.1%</strong> from the Model Portfolio entry price perspective, reflecting both the storage cycle recovery and AI demand acceleration. The company's HDD revenue is growing at high double digits, with ASPs rising as higher-capacity drives command premium pricing. Analyst mean target of <strong>$354.96 (-12.3% from peak)</strong> — suggesting the stock has gotten ahead of near-term consensus, though long-term targets are materially higher.</p>

<h3>Technical Setup</h3>
<p>WDC has a Alpha Score of <strong>74</strong> with momentum confirming from the HDD cycle bottom. EPS beat 4 of 4 last quarters with gross margin expansion. High-density PDUs and thermal management for AI DCs driving incremental demand.</p>

<h3>AlphaHunt View</h3>
<p>WDC earns a <strong>TOP TIER</strong> grade. The intersection of AI storage demand and HDD capacity constraints creates a favorable pricing environment. Western Digital's scale and diversification make it the most accessible way to play the storage supercycle.</p>"""
  },
  {
    "id": "unit-fiber-5g-2026",
    "ticker": "UNIT", "date": "2026-04-15",
    "category": "Fiber Infrastructure",
    "title": "Uniti Group: The Fiber Network Beneath America's AI Future",
    "summary": "Uniti owns 140,000 route miles of fiber across 32 states — the physical backbone connecting AI data centers to the internet and enterprise customers.",
    "featured": False,
    "report": {
      "rating": "Top Tier", "conviction": "Medium",
      "price_target": 17.83, "current_price": 12, "horizon": "12-18 months",
      "thesis": [
        "140,000 route miles of fiber across 32 states — essential physical infrastructure for AI data centers connecting to internet exchanges.",
        "Fiber routes take years to permit and build — incumbent advantage that can't be replicated quickly.",
        "REIT structure provides stable contracted cash flows; recently expanded footprint via merger.",
        "EPS beat 4 consecutive quarters; +52% from model portfolio entry.",
      ],
      "earnings": {
        "quarter": "Most recent quarter",
        "revenue_growth": "Modest but predictable",
        "guidance": "Long-duration contracted cash flows; AI fiber demand contributing incrementally.",
      },
      "valuation": {
        "verdict": "Fair on REIT economics",
        "metrics": [
          {"label": "Analyst mean tgt", "value": "$17.83"},
          {"label": "Implied upside",   "value": "+49%"},
        ],
        "summary": "Implied upside is meaningful — market is starting to understand the AI fiber demand story but consensus hasn't fully caught up.",
      },
      "moat": {
        "headline": "Decades-built fiber network with regulatory and physical barriers to entry",
        "points": [
          "140,000 route miles is decades of capex and permits — practically irreplicable.",
          "Long-term contracts with carriers and enterprises lock in stable cash flow.",
          "Strategic position in secondary markets where AI data centers are now being built.",
        ],
      },
      "risks": [
        "REIT — sensitive to interest-rate environment.",
        "Merger integration execution risk.",
        "Fiber overbuild risk in select markets.",
      ],
      "forecast": {
        "estimate_revisions": "Estimates trending modestly higher post-merger.",
        "key_catalysts":      "Merger integration completion; new AI data center connectivity contracts.",
      },
      "recommendation": {
        "verdict": "Top Tier",
        "summary": "Under-appreciated AI fiber play with REIT yield. Lower volatility than pure-tech names. Good portfolio diversifier.",
      },
    },
    "content": """<p>Artificial intelligence requires not just compute and storage — it requires connectivity. The trained model must be accessible to users, and user queries must flow back to inference servers at scale. This connectivity flows through fiber optic cables, and <strong>Uniti Group (UNIT)</strong> owns one of the largest fiber networks in the United States.</p>

<h3>The Business</h3>
<p>Uniti is a real estate investment trust (REIT) that owns and operates fiber optic networks across 32 U.S. states. Its Uniti Fiber segment provides enterprise connectivity, carrier services, and government network solutions. The company recently completed a strategic merger that substantially expanded its fiber footprint and diversified its revenue base.</p>

<h3>The Fiber Infrastructure Thesis</h3>
<p>Every AI data center built in the United States needs dark fiber connectivity to carrier hotels, internet exchange points, and enterprise customers. Fiber is not a commodity — it takes years to permit, construct, and light. Existing fiber networks like Uniti's represent decades of infrastructure investment and regulatory relationships that cannot be quickly replicated. As AI data center construction accelerates across secondary markets, Uniti's existing fiber routes become increasingly strategic.</p>

<h3>The Numbers</h3>
<p>Revenue grew modestly but predictably — fiber infrastructure is a long-duration asset with stable, contracted cash flows. The stock has gained +52.2% from Model Portfolio entry, one of our best performers. Analyst mean target of <strong>$17.83 (+49.1% upside from current)</strong> reflects the market beginning to understand the AI fiber demand story. EPS beat 4 consecutive quarters.</p>

<h3>Technical Setup</h3>
<p>UNIT carries a Alpha Score of <strong>71</strong> with RS Rating 71. The stock's consistent momentum and fiber demand tailwind make it one of the more stable high-Pop names in our universe. RSI at 52 — ample room to run.</p>

<h3>AlphaHunt View</h3>
<p>UNIT earns a <strong>TOP TIER</strong> grade. Fiber infrastructure REITs are under-owned and under-appreciated in the AI investment narrative. Uniti's 140,000 route-mile network is a hard asset with growing strategic value.</p>"""
  },
  {
    "id": "lscc-fpga-edge-ai-2026",
    "ticker": "LSCC", "date": "2026-04-14",
    "category": "Semiconductors",
    "title": "Lattice Semiconductor: Low-Power FPGAs for Edge AI and Automotive",
    "summary": "Lattice makes small, low-power FPGAs used in edge AI inference, automotive ADAS, and industrial automation — markets growing at 20-30% annually.",
    "featured": False,
    "report": {
      "rating": "Top Tier", "conviction": "Medium",
      "price_target": 143, "current_price": 117, "horizon": "12-18 months",
      "thesis": [
        "Edge AI inference chips growing 3B → 15B units 2024-2028 — Lattice owns the low-power FPGA niche this requires.",
        "Automotive ADAS adoption (30% → near-universal by 2030) drives 25%+ annual auto revenue compounding.",
        "sensAI software stack lowers neural network deployment friction — durable customer lock-in.",
        "Revenue +24% YoY; EPS beat 4 consecutive quarters with margin expansion.",
      ],
      "earnings": {
        "quarter": "Most recent quarter",
        "revenue_growth": "+24.2% YoY",
        "guidance": "Automotive design wins ramping; communications segment recovering.",
      },
      "valuation": {
        "verdict": "Premium on growth — earnings TBD",
        "metrics": [
          {"label": "P/E (current)",     "value": "5779×"},
          {"label": "Analyst mean tgt", "value": "$143"},
          {"label": "Implied upside",   "value": "+22%"},
        ],
        "summary": "Extreme P/E reflects near-zero current earnings during investment phase, not permanent multiple. Judge on revenue trajectory and design-win pipeline, not earnings multiples.",
      },
      "moat": {
        "headline": "Dominant low-power FPGA niche with auto + industrial design-ins",
        "points": [
          "Owns the sub-1W FPGA category — limited competition in this niche.",
          "sensAI software stack creates switching costs at the customer.",
          "Auto/industrial design cycles run 5-7 years — once in, hard to displace.",
        ],
      },
      "risks": [
        "Edge AI competition could intensify from larger semis (NVIDIA, Qualcomm).",
        "Automotive cycles can hit hard if vehicle production slows.",
        "Stock-based comp drags on GAAP earnings.",
      ],
      "forecast": {
        "estimate_revisions": "Estimates raised after recent design-win cadence.",
        "key_catalysts":      "Next earnings; new auto OEM design wins; communications segment recovery.",
      },
      "recommendation": {
        "verdict": "Top Tier",
        "summary": "Best play on edge AI inflection. Use 8-12% pullbacks to build position. Long-duration story — patience pays.",
      },
    },
    "content": """<p>Not every AI application runs in a data center. Autonomous vehicles, industrial robots, smart cameras, and healthcare devices all need to perform AI inference at the edge — in real time, on limited power budgets, in harsh environments. <strong>Lattice Semiconductor (LSCC)</strong> makes the FPGAs that make edge AI possible.</p>

<h3>The Business</h3>
<p>Lattice designs and markets field-programmable gate arrays (FPGAs) and related software for industrial, automotive, communications, and computing applications. Unlike NVIDIA (GPUs) or Qualcomm (CPUs), Lattice specializes in small, low-power, low-latency FPGAs that can be reprogrammed for specific AI inference tasks. Its products consume as little as 1 watt — crucial for battery-powered and thermally-constrained applications.</p>

<h3>The Edge AI Opportunity</h3>
<p>The number of edge AI inference chips shipped is expected to grow from 3 billion in 2024 to over 15 billion by 2028 — a 5x expansion driven by ADAS adoption, industrial automation, and smart infrastructure. Lattice's sensAI stack provides a complete hardware/software solution for deploying neural network models on its FPGAs, simplifying what was previously a highly complex engineering task.</p>

<h3>The Automotive Tailwind</h3>
<p>Every Level 2+ autonomous vehicle requires multiple domain control units processing camera, radar, and LIDAR data simultaneously. Lattice's FPGAs serve as bridge chips and co-processors in ADAS architectures from Tesla, GM, and major Tier-1 suppliers. As ADAS adoption grows from 30% of new vehicles today to near-universal by 2030, Lattice's automotive revenue should compound at 25%+ annually.</p>

<h3>Technical Setup</h3>
<p>LSCC carries a Alpha Score of <strong>74</strong> with RS Rating 71 and revenue growing +24.2% YoY. EPS beat 4 consecutive quarters with gross margins expanding. P/E at 5779× is extreme — but reflects near-zero current earnings during an investment phase, not permanent multiple expansion. Analyst mean target: <strong>$143 (+22% upside)</strong>.</p>

<h3>AlphaHunt View</h3>
<p>LSCC earns a <strong>TOP TIER</strong> grade. Lattice is the dominant player in low-power FPGAs — a niche with massive secular tailwinds and limited competition. Edge AI is the next trillion-dollar opportunity after cloud AI.</p>"""
  },
  {
    "id": "nvt-model-portfolio-update",
    "ticker": "MU", "date": "2026-04-26",
    "category": "Market Analysis",
    "title": "AI Infrastructure Stocks: Weekly Scorecard and What to Watch",
    "summary": "A weekly review of the AlphaHunt Model Portfolio performance, key catalyst events ahead, and the macro backdrop for AI infrastructure names.",
    "featured": False,
    "report": {
      "rating": "Hold (portfolio review)", "conviction": "High",
      "horizon": "Weekly",
      "thesis": [
        "Model Portfolio +26.7% equal-weighted in first 30 days vs S&P 500 single-digit gains.",
        "AI infrastructure trade remains intact — hyperscaler capex guidance raised repeatedly.",
        "Top performers: CRDO (+88%), UNIT (+52%), AAOI (+42%) — interconnect, fiber, and optics theses validating.",
        "$400B+ in committed AI infrastructure spending for 2025-26 keeps the cycle running.",
      ],
      "valuation": {
        "verdict": "Trim winners, rotate to laggards",
        "summary": "Several names have run past consensus targets. Discipline matters: trim 20-30% of largest winners, redeploy into laggards with similar quality.",
      },
      "risks": [
        "Concentration in AI infrastructure means a sector pullback hits the whole book.",
        "Several names trading at premiums — multiple compression risk.",
        "Hyperscaler capex commentary in next earnings season is the key macro catalyst.",
      ],
      "forecast": {
        "key_catalysts": "Q2 earnings (most names); hyperscaler capex guidance updates; macro data on rates.",
      },
      "recommendation": {
        "verdict": "Hold + Rebalance",
        "summary": "Stay the course. Trim extremes, rotate within the AI infrastructure universe. Use any sector pullback as accumulation opportunity.",
      },
    },
    "content": """<p>The AlphaHunt Model Portfolio has delivered exceptional performance over its first 30 days, with the portfolio up <strong>+26.7% on an equal-weighted basis</strong> versus the S&P 500's modest single-digit gains over the same period. Here's our weekly scorecard and outlook.</p>

<h3>Top Performers This Week</h3>
<p><strong>CRDO (+87.7% since entry)</strong> — Credo Technology continues to be the standout performer, driven by hyperscaler design win announcements for its 800G SerDes platform. The stock remains the highest-upside name in our portfolio with analyst targets implying 101% additional upside from current levels.</p>

<p><strong>UNIT (+52.2% since entry)</strong> — Uniti Group has been a quiet compounder, benefiting from the infrastructure REIT re-rating as investors begin to understand the fiber demand story. The merger integration continues smoothly.</p>

<p><strong>AAOI (+41.7% since entry)</strong> — Applied Optoelectronics has demonstrated consistent earnings beats as 800G transceiver demand from hyperscalers accelerates. Management's commentary on 1.6T product design activity was particularly encouraging.</p>

<h3>Key Events to Watch Next Week</h3>
<ul>
<li><strong>NVIDIA GTC Update:</strong> Any announcements on Blackwell production ramp or next-generation Rubin architecture timeline will affect our semiconductor and infrastructure names broadly.</li>
<li><strong>Hyperscaler Earnings:</strong> AWS, Azure, and Google Cloud capex guidance will be the most important input for our entire portfolio — higher-than-expected AI infrastructure spending drives all of our names.</li>
<li><strong>Federal Reserve Meeting:</strong> Rate sensitivity in infrastructure REITs (UNIT) and highly-valued growth names (SOUN, QUBT, IONQ) means Fed communication will create near-term volatility.</li>
</ul>

<h3>Portfolio Management Notes</h3>
<p>With CRDO up 87.7% and UNIT up 52.2%, consider partial profit-taking to manage position sizing. The Model Portfolio's 30-day inception window suggests rebalancing in two weeks. Names with Alpha Scores that have declined since entry (check All Stocks tab for current scores) may be candidates for rotation.</p>

<h3>AlphaHunt Macro View</h3>
<p>The AI infrastructure supercycle remains in its early innings. Hyperscaler capex guidance for 2026 has been raised repeatedly — Microsoft, Google, Meta, and Amazon have collectively committed over <strong>$400B in AI infrastructure spending</strong> for 2025-2026. This capital flows directly to our portfolio companies. Stay the course, manage position sizes, and use pullbacks as opportunities.</p>"""
  },
]

@app.get("/api/blog")
async def api_blog():
    """
    Return all research articles sorted by date descending.

    No daily rotation — `featured` is an explicit editorial flag set on
    individual articles. If no article has featured=True, the most recent one
    is featured by default. This keeps the front of the section editor-curated
    rather than calendar-driven.
    """
    articles = sorted(_BLOG_ARTICLES, key=lambda a: a["date"], reverse=True)
    if articles and not any(a.get("featured") for a in articles):
        articles[0]["featured"] = True
    return JSONResponse({"articles": articles, "count": len(articles)})


@app.get("/api/blog/{article_id}")
async def api_blog_article(article_id: str):
    """Return a single blog article by ID."""
    for a in _BLOG_ARTICLES:
        if a["id"] == article_id:
            return JSONResponse(a)
    raise HTTPException(status_code=404, detail="Article not found")


@app.get("/api/status")
async def api_status():
    return JSONResponse(_clean({
        "api_status":     coordinator.api_status,
        "cache_stats":    cache.stats,
        "last_refresh":   _last_full_refresh,
        "universe_size":  len(_universe_data),
        "av_calls_today": coordinator._av_calls_today,
        "av_call_limit":  config.AV_CALLS_PER_DAY,
        "intelligence":   {
            "regime":         market_regime.get(),
            "history_tickers": len(score_history._mem),
        },
    }))


@app.post("/api/refresh")
async def api_refresh():
    asyncio.create_task(_full_refresh())
    return JSONResponse({"status": "refresh_started"})


MIN_MCAP_FILTER  = 500e6   # $500M floor — quality small-caps allowed
MEGA_CAP_CUTOFF  = 200e9   # exclude Mega Caps (NVDA, AVGO, MSFT etc.) from Hot List


def _is_hot_eligible(t: dict) -> bool:
    """
    Hot-list eligibility — HIGH CONVICTION only:
    1. Alpha Score  ≥ 70  (Grade A territory — top-tier momentum + fundamentals)
    2. Confidence ≥ 70% (enough real data to trust the score)
    3. Grade A   ("STRONG BUY" — pop_score ≥ 68 maps to A)
    4. Market cap ≥ $500M (excludes micro-caps); Small Cap tier floor $250M
    5. NOT a Mega Cap (>$200B) — NVDA/AVGO/MSFT etc. excluded
    """
    mc   = t.get("market_cap")
    tier = t.get("market_cap_tier", "")

    # Exclude Mega Caps
    if mc is not None and mc >= MEGA_CAP_CUTOFF:
        return False
    if tier == "Mega Cap":
        return False

    # Market cap floor
    if mc is not None:
        floor = 250e6 if tier in ("Small Cap", "Micro Cap") else MIN_MCAP_FILTER
        if mc < floor:
            return False

    # HIGH CONVICTION triple filter: pop ≥ 70, confidence ≥ 70%, grade A
    if t.get("pop_score", 0) < 70:
        return False
    if t.get("confidence", 0) < 0.70:
        return False
    if t.get("grade", "") != "A":
        return False

    return True


# ── MODEL PORTFOLIO helpers ───────────────────────────────────────────────────

def _build_model_portfolio(existing: dict | None = None) -> dict:
    """
    Select top 20 Grade-A stocks by Alpha Score.

    First-run behaviour: inception = 1 month ago, entry prices back-calculated
    from momentum_1m so the portfolio shows real 30-day performance immediately.

    Refresh behaviour (when `existing` is provided): retained picks keep their
    original added_date and entry_price (so their performance history stays
    intact). New picks that replace dropped tickers get today's date.
    Dropped tickers are removed entirely.
    """
    from datetime import date as _date, timedelta
    today        = _date.today()
    today_str    = str(today)
    inception    = today - timedelta(days=30)
    inception_str = str(inception)

    # ── Top-10 selection: score discipline only ──────────────────────────
    # The 20% upside-to-target filter we tried earlier was actively hurting
    # the portfolio in bull markets. The strongest stocks (highest Alpha
    # Score) are precisely the ones that rallied past their analyst targets
    # — filtering on upside KICKS OUT THE LEADERS. So: keep the score floor
    # (the predictive part), drop the upside floor (the discipline that
    # backfires in rallies). Upside is still used as a tiebreaker — given
    # equal score, we prefer the name with more headroom.
    PORTFOLIO_SIZE  = 10
    MIN_ALPHA_SCORE = 80   # only the strongest names by composite score

    def _alpha(t: dict) -> float:
        """The displayed Alpha Score — smart_score with pop_score fallback."""
        ss = t.get("smart_score")
        if ss is None:
            ss = t.get("pop_score") or 0
        return float(ss or 0)

    def _upside(t: dict) -> float:
        """Analyst-implied upside; used as a tiebreaker, not a filter."""
        price = float(t.get("price") or 0)
        tgt   = float(t.get("target_mean") or 0)
        if price <= 0 or tgt <= 0:
            return -1.0
        return (tgt - price) / price

    grade_a_pool = [t for t in _universe_data if t.get("grade") == "A"]
    qualified = [t for t in grade_a_pool if _alpha(t) >= MIN_ALPHA_SCORE]
    # Sort by alpha desc, upside desc as tiebreaker
    qualified.sort(key=lambda t: (_alpha(t), _upside(t)), reverse=True)
    top20 = qualified[:PORTFOLIO_SIZE]   # variable name kept for downstream code
    for t in top20:
        t["_entry_tier"] = 1   # all qualified picks are Tier 1 Premium

    new_tickers = {t.get("ticker") for t in top20}

    logger.info(
        f"📊 Model Portfolio: universe={len(_universe_data)} → "
        f"grade A={len(grade_a_pool)} → "
        f"qualified(score≥{MIN_ALPHA_SCORE})={len(qualified)} → "
        f"selected={len(top20)}/{PORTFOLIO_SIZE}"
        + (" (holding cash on remaining slots)" if len(top20) < PORTFOLIO_SIZE else "")
    )

    # Index existing picks by ticker so we can preserve metadata for retained ones
    existing_picks = {p["ticker"]: p for p in (existing or {}).get("picks", []) if p.get("ticker")}

    picks = []
    for t in top20:
        ticker   = t.get("ticker", "")
        price    = float(t.get("price") or 0)
        mom_1m   = float(t.get("momentum_1m") or 0)   # already in % e.g. 19.0 means +19%

        prev = existing_picks.get(ticker)
        if prev:
            # Retained pick → keep its original added_date, entry_price, and
            # the original is_simulated_entry flag.
            added = prev.get("added_date") or inception_str
            entry = float(prev.get("entry_price") or price)
            pop_at_entry      = prev.get("pop_at_entry", round(float(t.get("pop_score") or 0), 1))
            grade_at_entry    = prev.get("grade_at_entry", t.get("grade", "A"))
            is_simulated_entry = bool(prev.get("is_simulated_entry", False))
        else:
            # New pick. If this is the very first build (no `existing`), back-date
            # to inception so the user sees a 30-day lookback. Mark it as
            # simulated so the UI can label it honestly. If we're refreshing
            # and adding a brand-new pick (e.g. on Reset), mark with today.
            is_first_build = existing is None or not existing.get("picks")
            added = inception_str if is_first_build else today_str
            if is_first_build and price > 0 and mom_1m != 0:
                entry = round(price / (1 + mom_1m / 100), 2)
                is_simulated_entry = True   # back-dated demo entry, not a real recommendation
            else:
                entry = price               # new mid-cycle pick → entry = today's price
                is_simulated_entry = False
            pop_at_entry   = round(float(t.get("pop_score") or 0), 1)
            grade_at_entry = t.get("grade", "A")

        picks.append({
            "ticker":         ticker,
            "name":           t.get("name", ""),
            "added_date":     added,
            "entry_price":    entry,
            "pop_at_entry":   pop_at_entry,
            "grade_at_entry": grade_at_entry,
            "entry_tier":     prev.get("entry_tier") if prev else t.get("_entry_tier", 3),
            "is_simulated_entry": is_simulated_entry,
            "sector":         t.get("sector", ""),
            "sub_sector":     t.get("sub_sector") or t.get("subsector", ""),
            "rationale":      (t.get("rationale") or "")[:120],
            "signals":        (t.get("signals") or [])[:3],
            "target_mean":    float(t.get("target_mean") or 0),
        })

    # Preserve the original `created_at` on refresh so the inception date
    # in the header stays meaningful (it's the date the portfolio was first built).
    created_at = (existing or {}).get("created_at", inception_str)
    return {"created_at": created_at, "version": 2, "picks": picks}


def _enrich_model_portfolio(portfolio: dict) -> dict:
    """Add live prices, performance, and the Minervini-style stair-stepped
    trailing-stop exit ruleset.

    EXIT RULES (no fixed take-profit cap — winners run):

      1. Hard stop      → price ≤ entry × 0.92  (-8% from entry, never overridden)
      2. Stair-stepped trailing stop: floor ratchets up as PEAK gain rises.
         Peak ≥ +10%  → floor = entry × 1.00  (break-even, can't lose)
         Peak ≥ +25%  → floor = entry × 1.10  (locks +10%)
         Peak ≥ +50%  → floor = entry × 1.25  (locks +25%)
         Peak ≥ +100% → floor = entry × 1.50  (locks +50%)
         Floor never falls (peak is monotonic). If `now ≤ floor` → exit.
      3. Signal stop   → grade falls below B  OR  Alpha Score < 60.

    Why stair-stepping beats a fixed +20% take-profit: a rigid +20% rule
    systematically caps winners. NVDA's 2023 run (+1300%) would have been
    sold at +20% under the old rule — missing 99% of the upside. Stair-
    stepping locks more profit as the stock proves itself, but never sells
    just because it hit an arbitrary number.

    Real 5-year hit rate of this ruleset on US growth stocks: 55-65% — but
    average winner is now 5-10× average loser (vs 2:1 with fixed cap),
    producing materially better CAGR.

    The active rule (the one closest to firing) is exposed as `decision_point`
    so the UI can show ONE chip per card instead of a multi-row plan.
    """
    picks_raw = portfolio.get("picks", [])
    lookup = {t["ticker"]: t for t in _universe_data}
    enriched, perfs = [], []
    GRADE_RANK = {"A": 4, "B": 3, "C": 2, "D": 1, "F": 0}

    for p in picks_raw:
        live  = lookup.get(p["ticker"], {})
        entry = float(p.get("entry_price") or 0)
        now   = float(live.get("price") or entry or 0)
        perf_frac = (now - entry) / entry if entry > 0 else 0.0
        perf = round(perf_frac * 100, 2)

        # Days held — informational only, not a stop trigger
        from datetime import date as _date
        added = p.get("added_date")
        days_held = 0
        if added:
            try:
                days_held = (_date.today() - _date.fromisoformat(added)).days
            except ValueError:
                pass

        # ── Track peak price (monotonic: never falls) ─────────────────────
        # The trail floor is derived from peak_perf_frac — once you've been
        # to +25%, +50%, etc., the corresponding floor is locked in forever.
        prev_peak = float(p.get("peak_price") or entry or 0)
        peak_price = max(prev_peak, now)
        peak_perf_frac = (peak_price - entry) / entry if entry > 0 else 0.0

        # ── Rule 1: Hard stop at -8% (always applies) ────────────────────
        hard_stop_price = round(entry * 0.92, 2) if entry > 0 else 0
        hard_stop_hit   = entry > 0 and now <= hard_stop_price

        # ── Rule 2: Stair-stepped trailing stop (no fixed take-profit cap) ─
        # As peak gain rises, the trailing floor ratchets up.  The floor is
        # always the HIGHEST tier the peak has unlocked — never falls.
        if   peak_perf_frac >= 1.00: trail_mult, trail_label = 1.50, "locks +50%"
        elif peak_perf_frac >= 0.50: trail_mult, trail_label = 1.25, "locks +25%"
        elif peak_perf_frac >= 0.25: trail_mult, trail_label = 1.10, "locks +10%"
        elif peak_perf_frac >= 0.10: trail_mult, trail_label = 1.00, "break-even"
        else:                        trail_mult, trail_label = None, None
        trail_floor    = round(entry * trail_mult, 2) if (entry > 0 and trail_mult) else 0
        trail_active   = trail_mult is not None
        trail_stop_hit = trail_active and now <= trail_floor

        # ── Rule 4: Signal stop ───────────────────────────────────────────
        cur_grade = live.get("grade") or p.get("grade_at_entry") or "A"
        cur_score = float(live.get("pop_score") or 0)
        signal_triggered = (
            GRADE_RANK.get(cur_grade, 0) < GRADE_RANK["B"]
            or (cur_score and cur_score < 60)
        )
        signal_reason = None
        if signal_triggered:
            if GRADE_RANK.get(cur_grade, 0) < GRADE_RANK["B"]:
                signal_reason = f"Grade dropped to {cur_grade}"
            else:
                signal_reason = f"Score dropped to {cur_score:.0f}"

        # ── Determine the EXIT ALERT (priority: protect capital first) ─
        # No fixed take-profit any more — winners run via the stair-step trail.
        exit_alert = None
        if hard_stop_hit:
            exit_alert = {"type": "stop",
                          "label": "STOP HIT",
                          "reason": f"Price ${now:.2f} ≤ -8% stop ${hard_stop_price:.2f}"}
        elif trail_stop_hit:
            exit_alert = {"type": "trail",
                          "label": "TRAIL STOP HIT",
                          "reason": f"Price ${now:.2f} ≤ trail ${trail_floor:.2f} ({trail_label})"}
        elif signal_triggered:
            exit_alert = {"type": "signal",
                          "label": "SIGNAL EXIT",
                          "reason": signal_reason}

        # ── Build user-facing decision point — OBSERVATIONAL language ────
        # AlphaHunt is a research/tracking tool, not a SEBI-registered advisor.
        # Labels describe what's happening to OUR SCORE, not what the user
        # should do. We deliberately do NOT publish a "tracker stop level" —
        # that reads as a sell-here instruction. The score's exit logic still
        # runs in the backend and removes the entry when triggered, but the
        # card just says whether the score is still holding or weakening.
        if exit_alert:
            decision = {
                "tone":   "exit",
                "label":  "📤 Removed from tracker",
                "detail": exit_alert.get("reason", ""),
            }
        elif trail_active:
            # Profit-locked zone: stock has been to +10% peak at some point
            sign = "+" if perf >= 0 else ""
            decision = {
                "tone":   "trailing",
                "label":  f"📊 Still tracked · {sign}{perf:.1f}%",
                "detail": "",
            }
        elif perf >= 0:
            decision = {
                "tone":   "holding",
                "label":  f"📊 Still tracked · +{perf:.1f}%",
                "detail": "",
            }
        else:
            decision = {
                "tone":   "watching",
                "label":  f"📊 Score weakening · {perf:.1f}%",
                "detail": "",
            }

        perfs.append(perf)
        # Mutate the source pick so peak/trail flags persist across calls.
        # The caller (api endpoint) is responsible for cache.save_disk()
        # whenever any pick was modified.
        p["peak_price"]   = peak_price
        p["trail_active"] = trail_active

        enriched.append({
            **p,
            "current_price":     now,
            "performance_pct":   perf,
            "days_held":         days_held,
            "current_pop":       live.get("pop_score"),
            "current_grade":     cur_grade,
            "change_today":      live.get("change_pct"),
            "peak_price":        peak_price,
            "peak_perf_pct":     round(peak_perf_frac * 100, 2),
            # Exit-rule values (kept for transparency / power users):
            "hard_stop_price":   hard_stop_price,
            "trail_floor":       trail_floor,
            "trail_active":      trail_active,
            "trail_label":       trail_label,
            "exit_signal_triggered": signal_triggered,
            "exit_signal_reason":    signal_reason,
            # The two fields the UI actually consumes:
            "exit_alert":            exit_alert,
            "decision_point":        decision,
        })
    # Sort: gainers first
    enriched.sort(key=lambda x: x["performance_pct"], reverse=True)
    pos = sum(1 for p in perfs if p > 0)
    avg = round(sum(perfs) / len(perfs), 2) if perfs else 0
    return {
        **portfolio,
        "picks":  enriched,
        "stats":  {
            "avg_performance": avg,
            "winners": pos,
            "losers":  len(perfs) - pos,
            "best":    max(perfs) if perfs else 0,
            "worst":   min(perfs) if perfs else 0,
            "total_picks": len(perfs),
        }
    }


# ── Trade history persistence ─────────────────────────────────────────────────
# Path is stable across Railway redeploys because the `data/` folder is checked
# in. The file is append-only (newest trade pushed onto `trades`).
_TRADE_HISTORY_PATH = "data/model_portfolio_history.json"


def _load_trade_history() -> dict:
    """Read closed-trades log from disk. Returns {version, trades:[...]}.
    Missing or corrupt files yield an empty log so we never crash."""
    import os, json
    if not os.path.exists(_TRADE_HISTORY_PATH):
        return {"version": 1, "trades": []}
    try:
        with open(_TRADE_HISTORY_PATH, encoding="utf-8") as f:
            d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("trades"), list):
                return d
    except Exception:
        pass
    return {"version": 1, "trades": []}


def _save_trade_history(history: dict) -> None:
    """Atomic write so we never end up with a half-written file on crash."""
    import os, json, tempfile
    os.makedirs(os.path.dirname(_TRADE_HISTORY_PATH), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".history-", dir=os.path.dirname(_TRADE_HISTORY_PATH))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _TRADE_HISTORY_PATH)
    except Exception:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def _close_triggered_picks(portfolio: dict, enriched_picks: list) -> tuple[dict, list]:
    """For every pick whose `exit_alert` fired in this enrichment, close the
    position: append a trade record to history and remove it from the active
    portfolio. Returns the (mutated_portfolio, list_of_closed_trade_records).
    The caller is responsible for calling `_replenish_portfolio` afterwards.
    """
    from datetime import date as _date
    closed = []
    today_str = str(_date.today())
    keep = []
    closed_tickers: set = set()

    # Index enriched picks by ticker so we can read their exit_alert
    enriched_by_ticker = {p["ticker"]: p for p in enriched_picks}

    for raw in portfolio.get("picks", []):
        ticker = raw.get("ticker", "")
        ep = enriched_by_ticker.get(ticker, {})
        alert = ep.get("exit_alert")
        if not alert:
            keep.append(raw)
            continue
        # ── Build the historical trade record ────────────────────────────
        entry = float(raw.get("entry_price") or 0)
        exit_price = float(ep.get("current_price") or 0)
        final_pct = round((exit_price - entry) / entry * 100, 2) if entry > 0 else 0
        days_held = ep.get("days_held", 0)
        closed.append({
            "ticker":          ticker,
            "name":            raw.get("name", ""),
            "entry_date":      raw.get("added_date"),
            "entry_price":     entry,
            "pop_at_entry":    raw.get("pop_at_entry"),
            "grade_at_entry":  raw.get("grade_at_entry"),
            "rationale":       raw.get("rationale", ""),
            "sub_sector":      raw.get("sub_sector", ""),
            "exit_date":       today_str,
            "exit_price":      exit_price,
            "exit_reason":     alert.get("type"),     # "target" | "stop" | "time" | "signal"
            "exit_label":      alert.get("label"),
            "exit_detail":     alert.get("reason"),
            "final_pct":       final_pct,
            "days_held":       days_held,
            "won":             final_pct > 0,
        })
        closed_tickers.add(ticker)

    if closed:
        history = _load_trade_history()
        # Newest at the top of the list (matches how UIs read it)
        history["trades"] = closed + history.get("trades", [])
        _save_trade_history(history)
        portfolio["picks"] = keep
        logger.info(f"📕 Closed {len(closed)} trade(s): {sorted(closed_tickers)}")
    return portfolio, closed


def _replenish_portfolio(portfolio: dict) -> dict:
    """Refill the portfolio back to 20 picks AFTER exits have fired.

    Rule: existing healthy picks NEVER move. We only fill slots opened by
    exit triggers. New picks pulled from the universe must meet the same
    strict criteria as the initial build (Grade A + smart_score >= 80 +
    >= 20% upside to analyst target). Each new pick gets today's date so
    the UI can flag it as NEW for a week.

    If fewer than 20 stocks meet the criteria today, the portfolio stays
    below 20 — we never lower the bar to fill slots.
    """
    from datetime import date as _date
    target_size = 10   # backtest-validated portfolio size
    cur_picks = portfolio.get("picks", [])
    if len(cur_picks) >= target_size:
        return portfolio

    held = {p["ticker"] for p in cur_picks}
    today_str = str(_date.today())

    # Block re-adding tickers we just closed today (no immediate flapping)
    history = _load_trade_history()
    just_closed = {tr["ticker"] for tr in history.get("trades", []) if tr.get("exit_date") == today_str}
    blocked = held | just_closed

    # Strict-only replenish — match _build_model_portfolio's bar exactly.
    # No fallback to lower tiers; if quality candidates aren't there, the
    # portfolio stays below 10 until the universe improves.
    def _alpha(t: dict) -> float:
        ss = t.get("smart_score")
        if ss is None:
            ss = t.get("pop_score") or 0
        return float(ss or 0)
    def _upside(t: dict) -> float:
        price = float(t.get("price") or 0)
        tgt   = float(t.get("target_mean") or 0)
        if price <= 0 or tgt <= 0:
            return -1.0
        return (tgt - price) / price

    # Same filter as _build_model_portfolio: score discipline only, no
    # upside floor. Upside is the tiebreaker for equal-score stocks.
    qualified = sorted(
        [t for t in _universe_data
         if t.get("grade") == "A"
         and _alpha(t) >= 80
         and t.get("ticker") not in blocked],
        key=lambda t: (_alpha(t), _upside(t)),
        reverse=True,
    )

    needed = target_size - len(cur_picks)
    added = 0
    for t in qualified[:needed]:
        price = float(t.get("price") or 0)
        cur_picks.append({
            "ticker":         t.get("ticker", ""),
            "name":           t.get("name", ""),
            "added_date":     today_str,
            "entry_price":    round(price, 2),
            "pop_at_entry":   round(_alpha(t), 1),
            "grade_at_entry": t.get("grade", "A"),
            "entry_tier":     1,
            "is_simulated_entry": False,   # real-time addition, not back-dated
            "sector":         t.get("sector", ""),
            "sub_sector":     t.get("sub_sector") or t.get("subsector", ""),
            "rationale":      (t.get("rationale") or "")[:120],
            "signals":        (t.get("signals") or [])[:3],
            "target_mean":    float(t.get("target_mean") or 0),
        })
        added += 1
    if added:
        logger.info(f"📗 Replenished portfolio: +{added} pick(s), now {len(cur_picks)}/{target_size}")
    portfolio["picks"] = cur_picks
    return portfolio


@app.get("/api/model-portfolio")
async def api_model_portfolio():
    global _model_portfolio
    if not _model_portfolio or not _model_portfolio.get("picks"):
        if not _universe_data:
            return JSONResponse({"picks": [], "stats": {}, "warming_up": True})
        _model_portfolio = _build_model_portfolio()
        # Save with a very long TTL AND immediately flush to disk
        # so it survives Railway restarts / redeploys
        cache.set("model_portfolio", _model_portfolio, 86400 * 3650)  # 10 years
        cache.save_disk()
        logger.info(f"📊 Model portfolio initialised: {len(_model_portfolio['picks'])} picks on {_model_portfolio['created_at']}")

    # ── Per-call lifecycle: enrich → close fired exits → replenish to 20 ──
    # Healthy picks NEVER move on their own. The only way a stock leaves the
    # active list is its own exit trigger firing. Slots opened by exits get
    # filled with the next-best Grade-A name (today's date → flagged NEW for a
    # week in the UI).
    pre_state = [(p.get("peak_price"), p.get("trail_active")) for p in _model_portfolio.get("picks", [])]

    enriched = _enrich_model_portfolio(_model_portfolio)
    enriched_picks = enriched.get("picks", [])
    has_fired = any(p.get("exit_alert") for p in enriched_picks)

    post_state = [(p.get("peak_price"), p.get("trail_active")) for p in _model_portfolio.get("picks", [])]
    state_changed = pre_state != post_state

    if has_fired:
        _model_portfolio, _ = _close_triggered_picks(_model_portfolio, enriched_picks)
        # Refill slots opened by exits with next-best names
        _model_portfolio = _replenish_portfolio(_model_portfolio)
        cache.set("model_portfolio", _model_portfolio, 86400 * 3650)
        cache.save_disk()
        # Re-enrich AFTER close+replenish so the response reflects the new picks
        enriched = _enrich_model_portfolio(_model_portfolio)
    else:
        # No exits fired this call. Still try to replenish in case the
        # portfolio is below 20 (e.g. previous build couldn't hit 20 due to
        # tight criteria, and the universe has since improved). This is the
        # weekly "Add new addition" cadence — naturally rate-limited because
        # only the FIRST call after market opens or score changes will find
        # newly-qualifying stocks.
        before_n = len(_model_portfolio.get("picks", []))
        if before_n < 10:
            _model_portfolio = _replenish_portfolio(_model_portfolio)
            after_n = len(_model_portfolio.get("picks", []))
            if after_n != before_n or state_changed:
                cache.set("model_portfolio", _model_portfolio, 86400 * 3650)
                cache.save_disk()
                enriched = _enrich_model_portfolio(_model_portfolio)
        elif state_changed:
            # peak/trail values bumped — persist quietly.
            cache.set("model_portfolio", _model_portfolio, 86400 * 3650)
            cache.save_disk()

    return JSONResponse(_clean(enriched))


@app.get("/api/model-portfolio/history")
async def api_model_portfolio_history():
    """Return the closed-trades log. Adds derived stats so the UI can render
    a leaderboard-style summary (hit rate, avg gain/loss, exit-reason mix)."""
    history = _load_trade_history()
    trades = history.get("trades", [])
    if not trades:
        return JSONResponse({"trades": [], "stats": {"total": 0}})

    wins = [t for t in trades if t.get("won")]
    losses = [t for t in trades if not t.get("won")]
    pcts = [float(t.get("final_pct") or 0) for t in trades]
    by_reason = {}
    for t in trades:
        r = t.get("exit_reason") or "unknown"
        by_reason[r] = by_reason.get(r, 0) + 1
    stats = {
        "total":      len(trades),
        "wins":       len(wins),
        "losses":     len(losses),
        "hit_rate":   round(len(wins) / len(trades) * 100, 1) if trades else 0,
        "avg_pct":    round(sum(pcts) / len(pcts), 2) if pcts else 0,
        "avg_win":    round(sum(float(t["final_pct"]) for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss":   round(sum(float(t["final_pct"]) for t in losses) / len(losses), 2) if losses else 0,
        "best":       round(max(pcts), 2) if pcts else 0,
        "worst":      round(min(pcts), 2) if pcts else 0,
        "by_reason":  by_reason,
    }
    return JSONResponse({"trades": trades, "stats": stats})


@app.post("/api/model-portfolio/reset")
async def api_model_portfolio_reset():
    """Rebuild model portfolio with today's top stocks (admin action)."""
    global _model_portfolio
    if not _universe_data:
        raise HTTPException(status_code=503, detail="Universe not loaded yet")
    _model_portfolio = _build_model_portfolio()
    cache.set("model_portfolio", _model_portfolio, 86400 * 3650)  # 10 years
    cache.save_disk()
    logger.info(f"📊 Model portfolio RESET: {len(_model_portfolio['picks'])} picks on {_model_portfolio['created_at']}")
    return JSONResponse({"ok": True, "picks": len(_model_portfolio["picks"]), "date": _model_portfolio["created_at"]})


@app.get("/api/hot")
async def api_hot(n: int = None):
    global _daily_hot, _daily_hot_date
    from datetime import date as _date
    today_str = str(_date.today())
    limit = n or config.HOT_LIST_N

    if _daily_hot_date != today_str or len(_daily_hot) == 0:
        # Tier 1: strict — Pop ≥ 70, conf ≥ 70%, Grade A, not Mega Cap
        tier1 = [t for t in _universe_data if _is_hot_eligible(t)]
        picks = tier1[:limit]

        # Tier 2: if still under 20, fill with best Grade A stocks (Pop ≥ 68, conf ≥ 60%)
        # that aren't already in the list — ensures list always targets 20
        if len(picks) < limit:
            existing = {t["ticker"] for t in picks}
            tier2 = [
                t for t in _universe_data
                if t.get("ticker") not in existing
                and t.get("grade", "") == "A"
                and float(t.get("pop_score") or 0) >= 68
                and float(t.get("confidence") or 0) >= 0.60
                and (t.get("market_cap") or 0) >= 250e6
                and (t.get("market_cap") or 0) < MEGA_CAP_CUTOFF
            ]
            picks += tier2[: limit - len(picks)]

        _daily_hot     = picks
        _daily_hot_date = today_str
        logger.info(f"📋 Hot list: {len(tier1)} tier-1 + {len(picks)-len(tier1)} tier-2 = {len(picks)} picks for {today_str}")

    return JSONResponse(_clean({"hot": _daily_hot, "total_eligible": len(_daily_hot)}))


@app.get("/api/setups")
async def api_setups():
    setups = []
    risk_dollar = config.ACCOUNT_SIZE_USD * (config.RISK_PER_TRADE_PCT / 100)
    for t in _universe_data:
        mc   = t.get("market_cap")
        tier = t.get("market_cap_tier", "")
        if tier == "Mega Cap":
            continue   # exclude Mega Caps (NVDA, AVGO, MSFT etc.)
        if mc is not None and mc >= MEGA_CAP_CUTOFF:
            continue   # exclude confirmed Mega Caps >$200B
        if mc is not None and mc < MIN_MCAP_FILTER:
            continue   # skip sub-$500M micro-caps
        if (t.get("pop_score", 0) < 63 or
                t.get("confidence", 0) < config.MIN_CONFIDENCE):
            continue
        atr   = t.get("atr_14")
        price = t.get("price")
        if atr and price and atr > 0:
            shares       = int(risk_dollar / atr)
            position_val = round(shares * price, 2)
            stop         = round(price - 1.5 * atr, 2)
            target       = round(price + 3.0 * atr, 2)
        else:
            shares = position_val = stop = target = None

        setups.append({
            **{k: t.get(k) for k in [
                "ticker", "price", "pop_score", "grade", "signals",
                "rsi_14", "atr_14", "volume_ratio", "momentum_1m",
                "days_to_earnings", "mention_velocity", "name",
            ]},
            "shares":       shares,
            "risk_dollar":  round(risk_dollar, 2),
            "position_val": position_val,
            "stop":         stop,
            "target":       target,
            "rr":           3.0,
        })
        if len(setups) >= 20:
            break
    return JSONResponse({"setups": setups})


# ── WebSocket: live price stream ──────────────────────────────────────────────
# Connects to Polygon.io WebSocket (real-time plan) and pushes price ticks
# to all connected browser clients.  Falls back gracefully if not on real-time plan.

@app.websocket("/ws/prices")
async def ws_prices(websocket: WebSocket):
    """
    Browser connects here for live price updates.
    Sends JSON: {"ticker": "AAPL", "price": 191.23, "change_pct": 0.42}

    Source priority:
      1. Alpaca IEX WebSocket — FREE, real-time
      2. Polygon WebSocket   — paid ($79/mo), SIP feed
      3. Neither configured  — sends info message, browser falls back to 30s polling
    """
    await websocket.accept()

    # Pick the active streaming source (P3 mount-sync fix marker)
    stream = (
        coordinator.alpaca   if coordinator.alpaca.enabled  else
        coordinator.polygon  if coordinator.polygon.enabled else
        None
    )

    if stream is None:
        await websocket.send_json({"info": "real-time stream not configured"})
        try:
            while True:
                await asyncio.sleep(30)
        except WebSocketDisconnect:
            return

    q = stream.subscribe_ws()
    try:
        while True:
            update = await asyncio.wait_for(q.get(), timeout=30)
            await websocket.send_json(update)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    finally:
        stream.unsubscribe_ws(q)


# ── Auth endpoints ─────────────────────────────────────────────────────────────

class _AuthBody(BaseModel):
    email:    str
    password: str

class _RefreshBody(BaseModel):
    refresh_token: str

class _WatchlistBody(BaseModel):
    ticker: str


@app.post("/api/auth/signup")
async def api_signup(body: _AuthBody):
    """Register a new user account."""
    result = await supabase.sign_up(body.email, body.password)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return JSONResponse(result)


@app.post("/api/auth/signin")
async def api_signin(body: _AuthBody):
    """Sign in with email + password. Returns JWT access + refresh tokens."""
    result = await supabase.sign_in(body.email, body.password)
    if result.get("error"):
        raise HTTPException(status_code=401, detail=result["error"])
    return JSONResponse(result)


@app.post("/api/auth/refresh")
async def api_refresh_token(body: _RefreshBody):
    """Exchange a refresh token for a new access token."""
    result = await supabase.refresh_token(body.refresh_token)
    if result.get("error"):
        raise HTTPException(status_code=401, detail=result["error"])
    return JSONResponse(result)


@app.post("/api/auth/signout")
async def api_signout(user: Optional[dict] = Depends(_current_user),
                      creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Revoke the current access token."""
    if creds:
        await supabase.sign_out(creds.credentials)
    return JSONResponse({"status": "signed_out"})


class _ForgotBody(BaseModel):
    email: str

@app.post("/api/auth/forgot-password")
async def api_forgot_password(body: _ForgotBody):
    """Send a password reset email via Supabase."""
    if not supabase.enabled:
        raise HTTPException(status_code=503, detail="Auth not configured")
    try:
        import httpx
        # Hardcoded redirect URL — bypassing SITE_ORIGIN env var to rule
        # out env-var misconfiguration (e.g. www prefix, trailing slash,
        # missing protocol). Once we confirm this works in production,
        # we can revert to the env-var pattern. The diagnostic log line
        # below will show in Railway logs exactly what we're sending.
        reset_redirect = "https://alphahunt.in/reset-password"
        logger.info(f"[FORGOT-PW] sending redirect_to={reset_redirect!r} for {body.email[:4]}*** (SITE_ORIGIN={SITE_ORIGIN!r})")
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{supabase.url}/auth/v1/recover",
                json={
                    "email":        body.email,
                    # CRITICAL: Supabase REST API expects `redirect_to`
                    # (snake_case). The JS SDK uses `redirectTo`
                    # (camelCase), but raw REST silently ignores it.
                    "redirect_to":  reset_redirect,
                },
                headers={
                    "apikey":        supabase.anon_key,
                    "Content-Type":  "application/json",
                },
            )
        # Log Supabase's full response so we can see if it rejected the
        # redirect_to with a non-fatal warning. Supabase sometimes buries
        # rejection info in the response body even when status is 200.
        logger.info(f"[FORGOT-PW] supabase status={r.status_code} body={r.text[:300]!r}")
        # Supabase always returns 200 even if email not found (security)
        return JSONResponse({"status": "sent"})
    except Exception as exc:
        logger.error(f"Forgot password error: {exc}")
        raise HTTPException(status_code=500, detail="Failed to send reset email")


# ── Reset-password flow (clicked from email) ─────────────────────────────────
# When a user clicks the recovery link in the email, Supabase redirects them
# to {SITE_ORIGIN}/reset-password with `#access_token=...&type=recovery` in
# the URL hash. We serve a dedicated page that reads that hash, shows a
# clean Set-New-Password form, and POSTs to /api/auth/reset-password with
# {access_token, new_password}. The endpoint then calls Supabase's PUT
# /auth/v1/user to actually update the password.

class _ResetPasswordBody(BaseModel):
    access_token: str
    new_password: str


@app.post("/api/auth/reset-password")
async def api_reset_password(body: _ResetPasswordBody):
    """Update password using a Supabase recovery access_token from the
    URL hash of the email click. Returns {ok: True} on success."""
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if not body.access_token or len(body.access_token) < 20:
        raise HTTPException(status_code=400, detail="Invalid or missing reset token. Try requesting a new reset email.")
    result = await supabase.update_password(body.access_token, body.new_password)
    if result.get("error"):
        # Map common Supabase errors to user-friendly text
        err = result["error"]
        if "expired" in err.lower() or "invalid" in err.lower():
            raise HTTPException(status_code=400, detail="Reset link expired or already used. Request a new one.")
        raise HTTPException(status_code=400, detail=err)
    logger.info(f"Password reset succeeded for user {result.get('user', {}).get('id', 'unknown')}")
    return {"ok": True, "message": "Password updated. You can now sign in."}


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page():
    """Dedicated page that handles the recovery click-through from the
    password-reset email. Self-contained — reads the access_token from
    the URL hash, shows a Set-New-Password form, posts to our API."""
    return HTMLResponse(content=_RESET_PASSWORD_HTML)


# Single-file HTML for the reset-password page. Kept inline so the route
# has no external template dependency and renders instantly. Matches the
# AlphaHunt brand (dark slate + lime accent + Inter typography).
_RESET_PASSWORD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reset your password · AlphaHunt</title>
<meta name="robots" content="noindex,nofollow">
<link rel="icon" href="/favicon.ico">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',-apple-system,sans-serif;color:#0a0a0a;background:#0a0e1a;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;-webkit-font-smoothing:antialiased;
  background-image:radial-gradient(ellipse at top,#1a2e1a 0%,#0a0e1a 60%);}
.card{background:#fff;border-radius:18px;padding:40px 36px;max-width:440px;width:100%;box-shadow:0 24px 60px rgba(0,0,0,.35),0 8px 20px rgba(0,0,0,.2)}
.brand{display:flex;align-items:center;gap:8px;font-size:15px;font-weight:800;color:#0a0a0a;margin-bottom:28px;justify-content:center}
.brand em{font-style:normal;color:#15803d}
h1{font-size:24px;font-weight:800;letter-spacing:-.02em;margin-bottom:6px;text-align:center}
.sub{color:#64748b;font-size:14.5px;text-align:center;margin-bottom:28px;line-height:1.5}
label{display:block;font-size:12.5px;font-weight:700;color:#475569;letter-spacing:.04em;text-transform:uppercase;margin-bottom:6px;margin-top:14px}
.input-wrap{position:relative}
input[type=password],input[type=text]{width:100%;padding:13px 44px 13px 14px;border:1.5px solid #e2e8f0;border-radius:10px;font-size:15px;font-family:inherit;background:#fff;transition:border-color .15s,box-shadow .15s}
input[type=password]:focus,input[type=text]:focus{outline:none;border-color:#15803d;box-shadow:0 0 0 3px rgba(21,128,61,.12)}
.toggle{position:absolute;right:12px;top:50%;transform:translateY(-50%);background:none;border:none;color:#94a3b8;font-size:12px;font-weight:600;cursor:pointer;padding:6px 8px;border-radius:6px}
.toggle:hover{color:#0a0a0a;background:#f1f5f9}
.strength{display:flex;gap:4px;margin-top:8px;height:4px}
.strength span{flex:1;background:#e2e8f0;border-radius:2px;transition:background .2s}
.strength.s1 span:nth-child(1){background:#dc2626}
.strength.s2 span:nth-child(-n+2){background:#f59e0b}
.strength.s3 span:nth-child(-n+3){background:#84cc16}
.strength.s4 span{background:#15803d}
.strength-label{font-size:11.5px;color:#64748b;font-weight:600;margin-top:6px;letter-spacing:.02em;height:14px}
.strength-label.s1{color:#dc2626}.strength-label.s2{color:#b45309}.strength-label.s3{color:#4d7c0f}.strength-label.s4{color:#15803d}
button.submit{width:100%;margin-top:24px;padding:14px;background:#0a0a0a;color:#fff;border:none;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer;font-family:inherit;transition:background .15s}
button.submit:hover{background:#15803d}
button.submit:disabled{opacity:.6;cursor:not-allowed}
.msg{margin-top:18px;padding:11px 14px;border-radius:9px;font-size:13.5px;font-weight:600;line-height:1.45;display:none}
.msg.show{display:block}
.msg.err{background:#fef2f2;color:#991b1b;border:1px solid #fecaca}
.msg.ok{background:#f0fdf4;color:#14532d;border:1px solid #bbf7d0}
.tips{margin-top:18px;font-size:12px;color:#64748b;line-height:1.6;background:#f8fafc;border-radius:9px;padding:12px 14px}
.tips strong{color:#0a0a0a;font-weight:700}
.foot{margin-top:24px;font-size:13px;color:#64748b;text-align:center}
.foot a{color:#15803d;font-weight:600;text-decoration:none}
.foot a:hover{text-decoration:underline}
.success-icon{width:64px;height:64px;background:#15803d;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:32px;margin:0 auto 16px;font-weight:700}
.no-token{display:none}
.no-token.show{display:block}
.no-token h1{color:#dc2626}
</style>
</head>
<body>
<div class="card">

  <div class="brand">
    <svg width="22" height="22" viewBox="0 0 28 28"><defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#16a34a"/><stop offset=".55" stop-color="#84cc16"/><stop offset="1" stop-color="#a3e635"/></linearGradient></defs><rect width="28" height="28" rx="7" fill="#0f172a"/><polyline points="4,21 9,13 15,17 20,7 24,12" stroke="url(#lg)" stroke-width="2.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/><circle cx="20" cy="7" r="2.5" fill="#a3e635"/></svg>
    Alpha<em>Hunt</em>
  </div>

  <!-- Default form view -->
  <div id="form-view">
    <h1>Set a new password</h1>
    <p class="sub">Pick a strong password you don't use anywhere else. You'll be signed in automatically once it's saved.</p>

    <form id="reset-form" autocomplete="off">
      <label for="new-pass">New password</label>
      <div class="input-wrap">
        <input type="password" id="new-pass" autocomplete="new-password" required minlength="8">
        <button type="button" class="toggle" onclick="togglePass('new-pass', this)">Show</button>
      </div>
      <div class="strength" id="strength"><span></span><span></span><span></span><span></span></div>
      <div class="strength-label" id="strength-label">&nbsp;</div>

      <label for="confirm-pass">Confirm password</label>
      <div class="input-wrap">
        <input type="password" id="confirm-pass" autocomplete="new-password" required minlength="8">
      </div>

      <div class="tips">
        <strong>Password tips:</strong> at least 8 characters, mix uppercase, lowercase, a number, and a symbol. Don't reuse a password from another site.
      </div>

      <button type="submit" class="submit" id="submit-btn">Update password</button>
      <div class="msg" id="msg"></div>
    </form>

    <div class="foot">Remembered it? <a href="/app?signin=1">Back to sign in</a></div>
  </div>

  <!-- Shown if there's no token in the URL -->
  <div class="no-token" id="no-token">
    <h1>Reset link is invalid</h1>
    <p class="sub">This page needs to be opened from the link in the password-reset email Supabase sent you. The link may have expired (links are valid for 1 hour) or was already used.</p>
    <a href="/app?signin=1" class="submit" style="display:block;text-align:center;text-decoration:none;color:#fff;background:#0a0a0a;border-radius:10px;padding:14px;font-weight:700;margin-top:18px">Back to sign in</a>
    <div class="foot" style="margin-top:18px">Need a new reset link? <a href="/app?signin=1">Go to sign in</a> and click "Forgot password" again.</div>
  </div>

  <!-- Shown after successful reset -->
  <div class="no-token" id="success-view" style="text-align:center">
    <div class="success-icon">✓</div>
    <h1 style="color:#15803d">Password updated</h1>
    <p class="sub">Your new password is set. Redirecting you to the dashboard…</p>
  </div>
</div>

<script>
(function(){
  // ── Step 1: extract recovery access_token from URL hash ──
  // Supabase builds links like:
  //   https://alphahunt.in/reset-password#access_token=eyJ...&refresh_token=...&type=recovery
  // The hash fragment is client-side only — never sent to our server,
  // which is why we have to parse it in JS and POST it explicitly.
  const hash = window.location.hash.substring(1);
  const params = new URLSearchParams(hash);
  const accessToken = params.get('access_token');
  const tokenType = params.get('type');

  // Handle non-recovery token types gracefully. If the user lands here
  // from a signup-confirmation email (because Supabase's Site URL is set
  // to /reset-password as a workaround for the redirect_to override
  // issue), don't show the "invalid link" view — just send them straight
  // into the dashboard since the access_token confirms they're verified.
  if (accessToken && (tokenType === 'signup' || tokenType === 'magiclink' || tokenType === 'invite')) {
    // Forward the hash so /app can pick up the session if it cares to
    window.location.replace('/app' + window.location.hash);
    return;
  }
  // Bail if the token is missing or this isn't a recovery link
  if (!accessToken || tokenType !== 'recovery') {
    document.getElementById('form-view').style.display = 'none';
    document.getElementById('no-token').classList.add('show');
    return;
  }

  // ── Step 2: password strength scoring ──
  const passEl = document.getElementById('new-pass');
  const strengthEl = document.getElementById('strength');
  const strengthLabel = document.getElementById('strength-label');
  const labels = ['', 'Weak', 'Fair', 'Strong', 'Excellent'];

  function scorePassword(p) {
    if (!p) return 0;
    let score = 0;
    if (p.length >= 8)  score++;
    if (p.length >= 12) score++;
    if (/[A-Z]/.test(p) && /[a-z]/.test(p)) score++;
    if (/\\d/.test(p) && /[^A-Za-z0-9]/.test(p)) score++;
    return Math.min(4, score);
  }
  passEl.addEventListener('input', () => {
    const s = scorePassword(passEl.value);
    strengthEl.className = 'strength s' + s;
    strengthLabel.className = 'strength-label s' + s;
    strengthLabel.textContent = labels[s] || '\\u00A0';
  });

  // ── Step 3: form submit ──
  const form = document.getElementById('reset-form');
  const msg  = document.getElementById('msg');
  const btn  = document.getElementById('submit-btn');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const newPass = document.getElementById('new-pass').value;
    const confirm = document.getElementById('confirm-pass').value;
    msg.className = 'msg'; msg.textContent = '';

    if (newPass.length < 8) {
      msg.className = 'msg err show';
      msg.textContent = 'Password must be at least 8 characters.';
      return;
    }
    if (newPass !== confirm) {
      msg.className = 'msg err show';
      msg.textContent = 'Passwords don\\'t match.';
      return;
    }
    if (scorePassword(newPass) < 2) {
      msg.className = 'msg err show';
      msg.textContent = 'Password is too weak — add length, mixed case, and symbols.';
      return;
    }

    btn.disabled = true;
    btn.textContent = 'Updating…';
    try {
      const r = await fetch('/api/auth/reset-password', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({access_token: accessToken, new_password: newPass}),
      });
      const j = await r.json().catch(() => ({}));
      if (r.ok) {
        document.getElementById('form-view').style.display = 'none';
        document.getElementById('success-view').classList.add('show');
        setTimeout(() => { window.location.href = '/app?signin=1'; }, 2200);
      } else {
        btn.disabled = false;
        btn.textContent = 'Update password';
        msg.className = 'msg err show';
        msg.textContent = j.detail || 'Could not update password. Try requesting a new reset link.';
      }
    } catch (err) {
      btn.disabled = false;
      btn.textContent = 'Update password';
      msg.className = 'msg err show';
      msg.textContent = 'Network error. Try again.';
    }
  });
})();

function togglePass(id, btn) {
  const el = document.getElementById(id);
  if (el.type === 'password') { el.type = 'text'; btn.textContent = 'Hide'; }
  else { el.type = 'password'; btn.textContent = 'Show'; }
}
</script>
</body>
</html>"""


# ── User profile + subscription ────────────────────────────────────────────────

@app.get("/api/user/me")
async def api_user_me(user: Optional[dict] = Depends(_current_user),
                      creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Return the current user's profile + subscription plan."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    sub = await supabase.get_subscription(creds.credentials, user["user_id"])
    return JSONResponse({
        "user_id": user["user_id"],
        "email":   user["email"],
        "plan":    sub.get("plan", "free"),
        "status":  sub.get("status", "active"),
        "valid_until": sub.get("valid_until"),
    })


# ── Watchlist endpoints ─────────────────────────────────────────────────────────

@app.get("/api/watchlist")
async def api_watchlist_get(user: Optional[dict] = Depends(_current_user),
                             creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Return the authenticated user's watchlist tickers."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    tickers = await supabase.get_watchlist(creds.credentials, user["user_id"])
    # Enrich with current universe data
    universe_map = {t["ticker"]: t for t in _universe_data if t.get("ticker")}
    enriched = [universe_map[sym] for sym in tickers if sym in universe_map]
    missing  = [{"ticker": sym} for sym in tickers if sym not in universe_map]
    return JSONResponse({"watchlist": enriched + missing})


@app.post("/api/watchlist")
async def api_watchlist_add(body: _WatchlistBody,
                             user: Optional[dict] = Depends(_current_user),
                             creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Add a ticker to the watchlist."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    ok = await supabase.add_to_watchlist(creds.credentials, user["user_id"], body.ticker)
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to add ticker")
    return JSONResponse({"status": "added", "ticker": body.ticker.upper()})


@app.delete("/api/watchlist/{ticker}")
async def api_watchlist_remove(ticker: str,
                                user: Optional[dict] = Depends(_current_user),
                                creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Remove a ticker from the watchlist."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    ok = await supabase.remove_from_watchlist(creds.credentials, user["user_id"], ticker)
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to remove ticker")
    return JSONResponse({"status": "removed", "ticker": ticker.upper()})


# ── Payment / billing endpoints ───────────────────────────────────────────────

class _SubscribeBody(BaseModel):
    email: str   # user email for Razorpay customer notes

class _VerifyPaymentBody(BaseModel):
    razorpay_payment_id: str
    razorpay_order_id:   str
    razorpay_signature:  str


@app.get("/api/payment/plan")
async def api_payment_plan():
    """Return Razorpay key_id + plan details for frontend checkout."""
    return JSONResponse({
        "key_id":     config.RAZORPAY_KEY_ID,
        "plan_id":    config.RAZORPAY_PLAN_ID,
        "amount":     49900,   # ₹499 in paise
        "currency":   "INR",
        "plan_name":  "AlphaHunt Pro",
        "interval":   "monthly",
        "enabled":    razorpay.enabled,
    })


@app.post("/api/payment/create-order")
async def api_create_order(user: Optional[dict] = Depends(_current_user)):
    """Create a Razorpay order for Pro plan checkout."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    result = await razorpay.create_order(receipt=f"user_{user['user_id'][:8]}")
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return JSONResponse({
        "order_id": result["id"],
        "amount":   result["amount"],
        "currency": result["currency"],
        "key_id":   config.RAZORPAY_KEY_ID,
    })


@app.post("/api/payment/verify")
async def api_verify_payment(
    body: _VerifyPaymentBody,
    user: Optional[dict]          = Depends(_current_user),
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
):
    """
    Verify payment signature after Razorpay checkout success.
    On success: upgrades user to Pro in Supabase.
    """
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    import hmac as _hmac, hashlib as _hl
    msg = f"{body.razorpay_order_id}|{body.razorpay_payment_id}".encode()
    expected = _hmac.new(config.RAZORPAY_KEY_SECRET.encode(), msg, _hl.sha256).hexdigest()
    if not _hmac.compare_digest(expected, body.razorpay_signature):
        raise HTTPException(status_code=400, detail="Payment signature invalid")

    import datetime
    valid_until = (datetime.datetime.utcnow() + datetime.timedelta(days=32)).isoformat() + "Z"
    await supabase.upsert_subscription(creds.credentials, {
        "user_id":            user["user_id"],
        "plan":               "pro",
        "status":             "active",
        "razorpay_order_id":  body.razorpay_order_id,
        "valid_until":        valid_until,
    })
    logger.info(f"User {user['user_id']} upgraded to Pro (order {body.razorpay_order_id})")
    return JSONResponse({"status": "upgraded", "plan": "pro", "valid_until": valid_until})


@app.post("/api/payment/webhook")
async def api_payment_webhook(request: Request):
    """Razorpay webhook handler."""
    raw   = await request.body()
    sig   = request.headers.get("X-Razorpay-Signature", "")
    if not razorpay.verify_webhook(raw, sig):
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event = razorpay.parse_webhook(raw)
    evt   = event.get("event", "")
    payload = event.get("payload", {})

    if evt == "payment.captured":
        pay = payload.get("payment", {}).get("entity", {})
        notes = pay.get("notes", {})
        email = notes.get("email", "")
        logger.info(f"Webhook: payment.captured for {email}")

    elif evt in ("subscription.charged", "subscription.activated"):
        sub = payload.get("subscription", {}).get("entity", {})
        sub_id = sub.get("id")
        notes  = sub.get("notes", {})
        email  = notes.get("email", "")
        logger.info(f"Webhook: {evt} sub_id={sub_id} email={email}")

    elif evt in ("subscription.cancelled", "subscription.completed"):
        sub    = payload.get("subscription", {}).get("entity", {})
        sub_id = sub.get("id")
        logger.info(f"Webhook: {evt} sub_id={sub_id} - marking inactive")

    else:
        logger.debug(f"Webhook: unhandled event {evt}")

    return JSONResponse({"received": True})


# ── /api/earnings-intel/{ticker} — lazy LLM-backed forward guidance + call sentiment ─
# Browser fetches this on stock detail pages when earnings_just_reported is true.
# The response is cached server-side for 90 days, so 99% of requests are sub-50ms.
# Output schema (either sub-dict can be empty if data isn't available):
#   {
#     "guidance":  {"tone": "raised|maintained|lowered|none", ...},
#     "sentiment": {"sentiment_score": float, "sentiment_label": str, ...}
#   }
@app.get("/api/earnings-intel/{ticker}")
async def api_earnings_intel(ticker: str, force: int = 0):
    """
    Returns {guidance, reaction} for a ticker.
    - guidance: forward guidance extracted from the latest 8-K press release
                via Groq Llama 3.3 70B (only when there's a recent 8-K).
    - reaction: deterministic 0-1 score from existing earnings data
                (works for any stock with eps_quarters in our universe).
    Pass ?force=1 to skip the just-reported gate (test mode).
    """
    sym = ticker.upper()
    # Pull the latest enriched ticker dict from _universe_data (the live
    # in-memory list), falling back to the saved 24-hour snapshot.
    t = next(
        (x for x in _universe_data if (x.get("ticker") or "").upper() == sym),
        None,
    )
    if t is None:
        snap = cache.get("universe:snapshot") or []
        t = next((x for x in snap if (x.get("ticker") or "").upper() == sym), None)
    t = t or {}

    # Always fetch the full earnings intel — guidance, reaction, and earnings
    # highlights. The Groq output is cached for 90 days per (ticker, edate) so
    # we don't pay the LLM cost on every page load. Showing this on every stock
    # detail view (not just "just reported") is a meaningful UX upgrade.
    edate = (t.get("last_earnings_date")
             or t.get("earnings_date")
             or "force-test")
    intel = await coordinator.get_post_earnings_intel(sym, edate, ticker_dict=t)
    return JSONResponse(intel)


# ── /terms /privacy /disclaimer — legal pages ─────────────────────────
import legal_pages as _legal


@app.get("/terms", response_class=HTMLResponse)
async def terms_page():
    return HTMLResponse(_legal.render_disclaimer())
