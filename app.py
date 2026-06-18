"""
TickerMover — FastAPI Application
tickermover.com  |  From market noise to one clear number
Run:  uvicorn app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations
import asyncio
import logging
import math
import os
import time
from datetime import datetime, timezone
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
from email_sender import send_welcome_email, send_password_changed_email
from data_coordinator import DataCoordinator
from ai_scorer import score_and_rank, compute_pop_score
import ai_selector
from selection_store import store as _selstore
from stock_universe import get_universe, get_meta
from intelligence import (
    MarketRegime,
    MarketAnalysis,
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
LOGIN_HTML     = BASE_DIR / "templates" / "login.html"
DESK_HTML      = BASE_DIR / "templates" / "desk.html"


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
market_analysis = MarketAnalysis(cache)
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


# ── AI cost gating ───────────────────────────────────────────────────────────
# The web-grounded AI features (research Deep-Dive, peer-compare, Ask AI) are the
# only ones that cost Anthropic credits, so they are gated to Pro subscribers.
# Pro status is cached briefly in-memory to avoid a Supabase lookup on every poll.
_pro_cache: dict = {}                       # user_id -> (is_pro, expiry_epoch)
_PRO_CACHE_TTL = 300                        # seconds
# Monthly Ask-AI cap per Pro user (Ask AI is per-user / uncached, so it's the one
# AI cost that scales linearly with usage). Override via env.
ASK_MONTHLY_CAP = int(os.environ.get("ASK_MONTHLY_CAP", "100"))
# Daily inner bound on top of the monthly cap: smooths bursts (a power user can't
# blow the whole month — or hammer the API — in one sitting) and keeps a heavy
# user's worst-case day cheap (~12 Haiku questions ≈ $0.15/day). Override via env.
ASK_DAILY_CAP = int(os.environ.get("ASK_DAILY_CAP", "12"))
# Comma-separated emails that get AI access without a paid subscription row —
# for the dev's own account and any comped beta testers. e.g. AI_ALLOW_EMAILS=me@x.com,beta@y.com
_AI_ALLOW = {e.strip().lower() for e in os.environ.get("AI_ALLOW_EMAILS", "").split(",") if e.strip()}


def _beta_pro_active() -> bool:
    """During the public beta (config.BETA_PRO_UNTIL, UTC) every signed-in user
    gets Pro for free. Auto-expires at launch — no code change needed."""
    raw = (getattr(config, "BETA_PRO_UNTIL", "") or "").strip()
    if not raw:
        return False
    try:
        end = datetime.fromisoformat(raw)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < end
    except Exception:
        return False


async def _is_pro_user(user: Optional[dict],
                       creds: Optional[HTTPAuthorizationCredentials]) -> bool:
    """True iff the request carries an authenticated user with an active Pro
    subscription (or an allow-listed email). Free/anonymous → False.
    During the beta window, every authenticated user counts as Pro."""
    if not user or not creds:
        return False
    if _beta_pro_active():
        return True
    if (user.get("email") or "").lower() in _AI_ALLOW:
        return True
    uid = user.get("user_id")
    if not uid:
        return False
    now = time.time()
    cached = _pro_cache.get(uid)
    if cached and cached[1] > now:
        return cached[0]
    try:
        sub = await supabase.get_subscription(creds.credentials, uid)
        pro = is_pro(sub.get("plan", "free"), sub.get("status", "active"))
    except Exception:
        pro = False
    _pro_cache[uid] = (pro, now + _PRO_CACHE_TTL)
    return pro


def _ask_period() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m")


def _ask_period_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


async def _ask_quota(creds: HTTPAuthorizationCredentials) -> dict:
    """Return both the monthly and daily Ask-AI usage/caps for this Pro user.

    The daily counter rolls over via a single key pair (`ai_ask_day_date` /
    `ai_ask_day_n`) that resets when the stored date isn't today — this avoids
    accumulating one metadata key per calendar day."""
    mkey = "ai_ask_" + _ask_period()
    today = _ask_period_day()
    try:
        md = await supabase.get_user_metadata(creds.credentials) or {}
        used_m = int(md.get(mkey, 0) or 0)
        used_d = int(md.get("ai_ask_day_n", 0) or 0) if md.get("ai_ask_day_date") == today else 0
    except Exception:
        used_m = used_d = 0
    return {"mkey": mkey, "used_m": used_m, "cap_m": ASK_MONTHLY_CAP,
            "today": today, "used_d": used_d, "cap_d": ASK_DAILY_CAP}

_universe_data:     list[dict] = []
_last_full_refresh: float      = 0.0
_daily_hot: list = []
_daily_hot_date: str = ""
_LIVE_NEWS_CACHE: dict = {"ts": 0.0, "payload": None}   # short TTL cache for /api/news/live
_quote_pending: dict = {}   # sym -> suspect price awaiting a second-tick confirmation (bad-tick guard)
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
                    yf_data = cache.get(f"yf:{sym}:v5") or {}
                    if (av_used < av_budget
                            and cache.get(f"fund:{sym}") is None
                            and not yf_data.get("market_cap")):
                        await coordinator.get_fundamentals(sym)
                        av_used += 1

                    # ── Quarterly results (EPS surprise, revenue, FCF) ───
                    if cache.get(f"quarterly:{sym}") is None:
                        await coordinator.get_quarterly_results(sym)

                    # ── FMP fallback (if yfinance returned no data) ──────
                    # Audit found ~7% of universe stuck without sector when YF
                    # returned price/etc. but no sector field. Trigger FMP
                    # whenever YF cache is empty OR YF returned no sector —
                    # FMP profile reliably carries sector for SP500/Nasdaq names.
                    _yf_cached = cache.get(f"yf:{sym}:v5") or {}
                    _yf_has_sector = bool((_yf_cached.get("sector") or "").strip())
                    if (not _yf_cached or not _yf_has_sector) and config.FMP_API_KEY:
                        if cache.get(f"fmp_fund:{sym}") is None:
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
                    new_price = q["price"]
                    old_price = item.get("price")
                    new_chg   = q.get("change_pct")
                    try:
                        chg_abs = abs(float(new_chg)) if new_chg is not None else 0.0
                    except (TypeError, ValueError):
                        chg_abs = 0.0
                    # Bad-tick guard. A >40% price jump from the last good tick, or
                    # a |change_pct| over 90%, is almost always a feed glitch / bad
                    # symbol — real moves halt long before that and trickle in small
                    # 30s steps. We DON'T apply such a tick immediately (that's what
                    # poisoned the universe: e.g. KLAC $2410 → $253 / -89%, which the
                    # radar then cached). Instead we hold it as "pending" and only
                    # apply it if the NEXT refresh confirms a similar price — so a
                    # single glitch is dropped while a genuine large move lands a
                    # beat later. Keeps both the radar and Market Analysis clean.
                    suspect = (old_price and old_price > 0
                               and abs(new_price / old_price - 1) > 0.40) or chg_abs > 90
                    if suspect:
                        pend = _quote_pending.get(sym)
                        confirmed = pend and pend > 0 and abs(new_price / pend - 1) <= 0.10
                        if not confirmed:
                            _quote_pending[sym] = new_price
                            logger.warning(
                                f"⚠️  Rejected implausible quote for {sym}: "
                                f"{old_price} → {new_price} ({new_chg}%) — awaiting confirmation"
                            )
                            continue
                    _quote_pending.pop(sym, None)
                    item["price"]      = new_price
                    item["change_pct"] = new_chg
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
        # Keep the Market Analysis snapshot warm on the same cadence so the
        # first panel opener rides the cache instead of paying the yfinance fetch.
        try:
            await market_analysis.refresh()
        except Exception as exc:
            logger.warning(f"Market analysis refresh failed: {exc}")
        await asyncio.sleep(1800)   # 30 min


async def _bg_scheduler() -> None:
    """Full fast refresh at startup, then every 5 min."""
    await _full_refresh()
    while True:
        await asyncio.sleep(300)
        await _full_refresh()


# ── Durable AI-cache health check ─────────────────────────────────────
_CACHE_TABLE_SQL = {
    "stock_overview": (
        "create table stock_overview (env_id int not null, ticker text not null, "
        "generated_at timestamptz not null default now(), model text, markdown text, "
        "sources jsonb default '[]'::jsonb, status text default 'ready', "
        "primary key (env_id, ticker));"
    ),
    "stock_research": (
        "create table stock_research (env_id int not null, ticker text not null, "
        "generated_at timestamptz not null default now(), model text, markdown text, "
        "sources jsonb default '[]'::jsonb, status text default 'ready', "
        "primary key (env_id, ticker));"
    ),
    "stock_compare": (
        "create table stock_compare (env_id int not null, ticker text not null, "
        "generated_at timestamptz not null default now(), model text, card jsonb default '{}'::jsonb, "
        "sources jsonb default '[]'::jsonb, status text default 'ready', "
        "primary key (env_id, ticker));"
    ),
    "desk_report": (
        "create table desk_report (env_id int not null, kind text not null, "
        "edition_date text, report jsonb default '{}'::jsonb, "
        "updated_at timestamptz not null default now(), primary key (env_id, kind));"
    ),
    "app_kv": (
        "create table app_kv (env_id int not null, ns text not null, k text not null, "
        "v jsonb default '{}'::jsonb, updated_at timestamptz not null default now(), "
        "primary key (env_id, ns, k));"
    ),
    "selection_judgments": (
        "create table selection_judgments (env_id int not null, ticker text not null, "
        "generated_at timestamptz not null default now(), model text, conviction int, "
        "thesis text, red_flags jsonb default '[]'::jsonb, lean text, "
        "primary key (env_id, ticker));"
    ),
}


def _check_research_caches() -> None:
    """Probe each durable AI-cache table once at startup. If Supabase isn't
    configured, or a table is missing/unreachable, log a loud warning with the
    fix — otherwise these caches silently degrade to ephemeral disk and the same
    stock is re-billed after every redeploy."""
    import httpx
    from config import SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY
    url = (SUPABASE_URL or "").rstrip("/")
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY or ""
    if not (url and key):
        logger.warning(
            "⚠️  AI caches: Supabase NOT configured — Overview/Deep-Dive/Compare "
            "fall back to ephemeral output/*.json, wiped on every redeploy. The "
            "same stock will re-bill after each deploy until SUPABASE_URL + key "
            "are set and a Volume backs CACHE_DISK_FILE."
        )
        return
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    for table, ddl in _CACHE_TABLE_SQL.items():
        try:
            with httpx.Client(timeout=6) as c:
                # select=* (not a hardcoded column) so the probe works regardless
                # of each table's schema — desk_report/app_kv have no `ticker`.
                r = c.get(f"{url}/rest/v1/{table}",
                          headers={**headers, "Prefer": "count=none"},
                          params={"select": "*", "limit": "1"})
            if r.status_code == 200:
                logger.info(f"✅ AI cache table '{table}' reachable (durable across redeploys).")
            elif r.status_code in (404, 400) or "does not exist" in r.text.lower():
                logger.warning(
                    f"⚠️  AI cache table '{table}' MISSING ({r.status_code}) — this cache "
                    f"is NOT durable and will re-bill after every redeploy. Create it once:\n    {ddl}"
                )
            else:
                logger.warning(f"⚠️  AI cache table '{table}' probe → {r.status_code}: {r.text[:160]}")
        except Exception as e:
            logger.warning(f"⚠️  AI cache table '{table}' probe failed: {e}")


# ── Curated Overview pre-warm ──────────────────────────────────────────
# Keeps the curated set's Overviews fresh in the durable store so heavy users
# never trigger a cold (slow + paid) model generation on first view. Cheap: only
# regenerates names that are aging out (refresh-ahead 1 day before the 30-day TTL).
_OVERVIEW_PREWARM_AGE = 29 * 24 * 3600    # refresh when older than 29 days


def _overview_age_s(doc: dict | None) -> float:
    if not doc:
        return 1e12
    ep = doc.get("generated_epoch")
    if ep is None:
        ga = doc.get("generated_at")
        if not ga:
            return 1e12
        try:
            from datetime import datetime
            ep = datetime.fromisoformat(str(ga).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 1e12
    return time.time() - float(ep)


async def _prewarm_one_overview(sym: str) -> bool:
    """(Re)generate `sym`'s Overview if the durable copy is missing or aging out.
    Returns True if it generated. Premium-tier aware."""
    import research_gen
    from overview_store import store as _ovstore
    doc = _ovstore.get(sym)
    if (doc and doc.get("status") == "ready" and doc.get("markdown")
            and _overview_age_s(doc) < _OVERVIEW_PREWARM_AGE):
        return False                       # still fresh — nothing to do
    target = next((t for t in _universe_data if t.get("ticker") == sym), None)
    try:
        out = await research_gen.generate_overview(sym, target, premium=_is_premium_overview(sym))
        out.setdefault("status", "ready")
        _ovstore.save(sym, out)
        cache.set("overview:" + sym, out, ttl=2592000)   # 30 days
        return True
    except Exception as exc:
        logger.warning(f"overview pre-warm {sym} failed: {exc}")
        return False


async def _overview_prewarm() -> None:
    """Background loop: keep the curated ~35 Overviews warm. Runs ~every 6h; most
    cycles do nothing because the 30-day cache is still fresh."""
    import research_gen
    await asyncio.sleep(150)               # let the universe + featured set load first
    while True:
        try:
            if research_gen.available() and _universe_data:
                warmed = 0
                for t in _ensure_featured():
                    sym = t.get("ticker")
                    if not sym:
                        continue
                    if await _prewarm_one_overview(sym):
                        warmed += 1
                        await asyncio.sleep(3)   # gentle pacing — no burst
                if warmed:
                    logger.info(f"🔥 Overview pre-warm: refreshed {warmed} curated overviews")
        except Exception as exc:
            logger.warning(f"overview pre-warm cycle failed: {exc}")
        await asyncio.sleep(6 * 3600)      # every 6h


async def _data_prewarm() -> None:
    """Pre-warm the FREE on-demand data — the FMP-enriched 'stock-extra'
    (valuation multiples, FCF, estimates, targets, ratings, peers, filings) — for
    the WHOLE universe so any stock's Financials/Valuation/Estimates tabs load
    instantly. $0 in fees (FMP is a flat plan; the limiter self-paces it).

    Self-limiting: only tickers whose cache is cold actually fetch, so steady
    state it's near-free. NOTE: most valuable once the disk volume persists —
    without it the cache is wiped each deploy and this re-warms the universe
    every build (free, but heavy background HTTP). Candles are already kept warm
    by _tech_refresh, so this only fills the stock-extra gap."""
    await asyncio.sleep(300)               # after universe load + tech_refresh's first sweep
    while True:
        try:
            warmed = 0
            for t in list(_universe_data):
                sym = t.get("ticker")
                if not sym or cache.get(f"fmp_extra:{sym}") is not None:
                    continue                # already warm — skip (no fetch)
                try:
                    await coordinator.get_fmp_enrichment(sym)
                    warmed += 1
                except Exception:
                    pass
                await asyncio.sleep(1.5)    # gentle — yield to live requests + FMP budget
            if warmed:
                logger.info(f"🗄️  Data pre-warm: filled stock-extra for {warmed} tickers (free/FMP)")
        except Exception as exc:
            logger.warning(f"data pre-warm cycle failed: {exc}")
        await asyncio.sleep(12 * 3600)     # twice a day


# ── Lifespan ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _refresh_lock, _universe_data, _last_full_refresh
    _refresh_lock = asyncio.Lock()

    logger.info("TickerMover starting …")

    # ── Durable-cache health check ─────────────────────────────────────
    # The AI snapshot/deep-dive/compare caches only survive a Railway redeploy
    # if their Supabase tables exist. When a table is missing the stores silently
    # fall back to ephemeral output/*.json (wiped on every deploy) → the same
    # stock gets re-billed after each push. Probe loudly at startup so that
    # failure mode is never invisible again.
    _check_research_caches()

    # ── Unrotated-secret warning ───────────────────────────────────────
    # Flag any sensitive API key still served from the committed fallback in
    # config.py (env var not set) — those keys are live-in-repo and should be
    # rotated on the provider dashboard + set as Railway env vars.
    _SENSITIVE_KEYS = {"FMP_API_KEY", "ALPACA_KEY_ID", "ALPACA_SECRET_KEY",
                       "FINNHUB_KEY", "ALPHA_VANTAGE_KEY", "SEC_API_KEY", "APEWISDOM_KEY"}
    _unrotated = sorted(_SENSITIVE_KEYS.intersection(getattr(config, "KEYS_ON_FALLBACK", [])))
    if _unrotated:
        logger.warning(
            "🔑 SECURITY: %d API key(s) using the committed fallback (set these as env "
            "vars and rotate them on the provider dashboard — they are exposed in git): %s",
            len(_unrotated), ", ".join(_unrotated),
        )

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

    # Restore Score Tracker from disk (persists across Railway redeploys
    # because data/ is committed to the repo). Falls back to legacy cache
    # if no JSON file yet (one-time migration after the first save).
    global _model_portfolio
    _model_portfolio = _load_portfolio_from_disk()
    if not _model_portfolio:
        legacy = cache.get("model_portfolio") or {}
        if legacy.get("version", 0) >= 2:
            _model_portfolio = legacy
            _save_portfolio_to_disk(_model_portfolio)   # migrate to disk
            logger.info("📦 Migrated portfolio from cache → data/model_portfolio.json")

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
        asyncio.create_task(_desk_publisher()),     # freeze pre/post editions
        asyncio.create_task(_overview_prewarm()),   # keep curated Overviews warm
        asyncio.create_task(_data_prewarm()),        # keep all stocks' free data (stock-extra) warm
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
    logger.info("TickerMover shut down — cache saved.")


app = FastAPI(title="TickerMover", lifespan=lifespan)

# ── GZip compression ────────────────────────────────────────────────
# /api/universe and /app both return ~1.6 MB of JSON/HTML. Gzipping
# shrinks them to ~170 KB and ~300 KB respectively — about 80% smaller.
# Cloudflare-edge gzip is also applied but only for content >= 1KB and
# only when the upstream doesn't compress; doing it here is reliable.
# minimum_size=1000 means tiny responses (e.g. /api/regime at 500b) skip
# compression to avoid wasting CPU on payloads that are already small.
from fastapi.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=1000)

# Serve /static/ files (icons, images, css/js assets)
_STATIC = BASE_DIR / "static"
_STATIC.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# ── SEO INFRASTRUCTURE ───────────────────────────────────────────────
# robots.txt, sitemap.xml, and per-stock SEO pages so search engines
# (and AI search like Google AI Overviews / Perplexity / ChatGPT) can
# discover and index TickerMover properly. Without these the dashboard SPA
# is invisible to crawlers — see 2026-04 SEO foundation work.

# Public-facing canonical origin used for sitemap + schema URLs.
# Configurable via env so staging vs prod don't conflict. Default is the
# www host because the GoDaddy apex (tickermover.com) 301-forwards to www
# (apex can't be a CNAME to Railway), so www is the real canonical host.
import os as _os, re as _re_origin
SITE_ORIGIN = _os.environ.get("SITE_ORIGIN", "https://www.tickermover.com").strip().rstrip("/")
# Self-repair a malformed origin from a bad env var (e.g. "https:tickermover.com"
# missing the // or the www) so sitemap / canonical / og: URLs are never broken.
SITE_ORIGIN = _re_origin.sub(r'^\s*https?:/*', 'https://', SITE_ORIGIN) or "https://www.tickermover.com"
# Canonical host is www (the GoDaddy apex 301-redirects to www).
if SITE_ORIGIN.startswith("https://tickermover.com"):
    SITE_ORIGIN = SITE_ORIGIN.replace("https://tickermover.com", "https://www.tickermover.com", 1)


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
        '<stop offset="0" stop-color="#D4860A"/>'
        '<stop offset="0.5" stop-color="#F5A623"/>'
        '<stop offset="1" stop-color="#FFE9B0"/></linearGradient></defs>'
        '<rect width="32" height="32" rx="7" fill="#0f172a"/>'
        '<polyline points="4,22 10,13 16,17 21,7 28,14" stroke="url(#g)" '
        'stroke-width="2.5" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="21" cy="7" r="2.8" fill="#FFE9B0"/></svg>'
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
        '<stop offset="0" stop-color="#D4860A"/>'
        '<stop offset="0.55" stop-color="#F5A623"/>'
        '<stop offset="1" stop-color="#FFE9B0"/></linearGradient></defs>'
        '<rect width="512" height="512" rx="112" fill="#0f172a"/>'
        '<polyline points="72,360 160,232 256,290 352,128 432,210" stroke="url(#g)" '
        'stroke-width="38" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        '<circle cx="352" cy="128" r="42" fill="#FFE9B0"/></svg>'
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
        f'  <url><loc>{SITE_ORIGIN}/reports</loc><changefreq>daily</changefreq><priority>0.9</priority><lastmod>{today}</lastmod></url>',
        f'  <url><loc>{SITE_ORIGIN}/brief</loc><changefreq>daily</changefreq><priority>0.85</priority><lastmod>{today}</lastmod></url>',
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


@app.get("/api/ticker-strip")
async def api_ticker_strip():
    """Thin payload for the landing-page ticker tape: top 12 daily gainers
    + bottom 8 daily losers, with only the fields the marquee needs. Avoids
    forcing the landing page to download the ~1.6 MB /api/universe payload."""
    uni = _universe_data or []
    if not uni:
        return JSONResponse({"items": []}, headers={"Cache-Control": "public, max-age=30, s-maxage=60"})
    with_chg = [t for t in uni if t.get("price") and t.get("change_pct") is not None]
    by_chg = sorted(with_chg, key=lambda t: float(t.get("change_pct") or 0), reverse=True)
    picked = (by_chg[:12] + list(reversed(by_chg[-8:])))[:22]
    items = [
        {
            "symbol": t.get("ticker", ""),
            "price":  float(t.get("price") or 0),
            "change_pct": float(t.get("change_pct") or 0),
        }
        for t in picked
    ]
    return JSONResponse({"items": items}, headers={"Cache-Control": "public, max-age=30, s-maxage=60"})


# ── HTML Dashboard ────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def landing():
    """Landing page — tickermover.com home.

    The redesigned landing (May 21 2026) uses client-side fetches for the
    ticker tape, live picks preview, and track-record stats — so SSR
    injection is no longer needed. The handler just serves the template
    with Schema.org JSON-LD prepended for SEO.
    """
    try:
        html = LANDING_HTML.read_text(encoding="utf-8")
        schema = _build_landing_schema()
        # Safety-net: if Supabase drops redirect_to and lands a password
        # recovery (or signup/magic) link on the home page, forward it to
        # the right page with the URL hash intact. Runs before render.
        recovery_forward = (
            "<script>(function(){try{var h=location.hash||'';"
            "if(h.indexOf('access_token=')>-1){"
            "if(h.indexOf('type=recovery')>-1){location.replace('/reset-password'+h);return;}"
            "if(/type=(signup|magiclink|invite)/.test(h)){location.replace('/app'+h);return;}"
            "}}catch(e){}})();</script>"
        )
        if "</head>" in html:
            html = html.replace("</head>", recovery_forward + "\n" + schema + "\n</head>", 1)
        return HTMLResponse(
            content=html,
            headers={"Cache-Control": "public, max-age=30, s-maxage=60"},
        )
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
        "name": "TickerMover",
        "url": SITE_ORIGIN,
        "logo": f"{SITE_ORIGIN}/static/icons/icon-512.png",
        "description": "We do the homework. You make the call. 200+ US stocks scored every 5 minutes with plain-English verdicts.",
        "email": "support@tickermover.com",
        # contactPoint feeds Google's knowledge panel + AI search citations
        "contactPoint": {
            "@type": "ContactPoint",
            "contactType": "customer support",
            "email": "support@tickermover.com",
            "availableLanguage": ["English"],
        },
    }
    app_schema = {
        "@context": "https://schema.org",
        "@type": "WebApplication",
        "name": "TickerMover",
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


# ═════════════════════════════════════════════════════════════════════════
#  /brief — Daily Morning Brief (Reading Mode)
# ═════════════════════════════════════════════════════════════════════════
#  The single biggest retention lever: a one-page editorial brief
#  published daily that users open over morning coffee. Same Reading
#  Mode design language as /report/{ticker}, structured as: market
#  overview → today's 3 picks → sector spotlight → what's changed →
#  earnings calendar. Pulls all data from the live universe so it
#  refreshes throughout the trading day without manual editorial work.
# ═════════════════════════════════════════════════════════════════════════
@app.get("/brief", response_class=HTMLResponse)
async def daily_brief():
    """Today's Morning Brief — auto-assembled from live universe data."""
    return HTMLResponse(content=_render_morning_brief())


def _render_morning_brief() -> str:
    """Assemble + render the Daily Morning Brief as a long-form Reading
    Mode page. Pulls live data from the in-memory universe.
    """
    import html as _html
    from datetime import datetime, timezone, timedelta

    # ── Date / greeting ──────────────────────────────────────────────
    # Use US/Eastern for the masthead since the brief is a US-market product.
    et = timezone(timedelta(hours=-5))  # EST; close enough for the masthead
    now = datetime.now(et)
    date_full  = now.strftime("%A, %B %-d") if hasattr(now, "strftime") else str(now)
    try:
        date_full = now.strftime("%A, %B %d").replace(" 0", " ")
    except Exception:
        date_full = now.strftime("%A, %B %d")
    weekday = now.strftime("%A")

    # ── Universe slice ───────────────────────────────────────────────
    universe = [x for x in (_universe_data or []) if x.get("ticker")]

    # Top picks — drawn from the curated featured pool and ROTATED on a time
    # bucket so fresh featured names surface through the day (but always from the
    # small, mostly-cached pool, never random). Falls back to plain top-by-score
    # if the featured pool is too small.
    def _score(t): return float(t.get("smart_score") or t.get("pop_score") or 0)
    def _chg(t):
        v = t.get("change_pct")
        return float(v) if isinstance(v, (int, float)) else 0.0
    ranked = sorted(universe, key=_score, reverse=True)
    _pool = [t for t in ranked if _is_featured_eligible(t)]
    if len(_pool) < 3:
        _pool = ranked
    if _pool:
        _bucket = int(time.time() // 900)          # rotate every 15 minutes
        _off = _bucket % len(_pool)
        top3 = [_pool[(_off + i) % len(_pool)] for i in range(min(3, len(_pool)))]
    else:
        top3 = ranked[:3]

    # Notable movers (top 3 up, top 3 down by change_pct)
    movers_up   = sorted([t for t in universe if _chg(t) >  0.5], key=_chg, reverse=True)[:3]
    movers_down = sorted([t for t in universe if _chg(t) < -0.5], key=_chg)[:3]

    # Sector roll-up — best avg score sector
    sector_buckets = {}
    for t in universe:
        s = (t.get("sector") or "").strip()
        if not s: continue
        sector_buckets.setdefault(s, []).append(_score(t))
    sector_avgs = [
        (s, sum(scores)/len(scores), len(scores))
        for s, scores in sector_buckets.items() if len(scores) >= 3
    ]
    sector_avgs.sort(key=lambda x: x[1], reverse=True)
    hot_sector = sector_avgs[0] if sector_avgs else None
    cold_sector = sector_avgs[-1] if len(sector_avgs) > 1 else None

    # Aggregate stats
    avg_score = (sum(_score(t) for t in universe) / len(universe)) if universe else 0
    a_grade   = sum(1 for t in universe if (t.get("grade") or "").upper() == "A")
    total_n   = len(universe)

    # Earnings reporting in next 24h (best effort — uses next_earnings_date if available)
    earnings_today = []
    for t in universe:
        ne = t.get("next_earnings_date") or t.get("earnings_date")
        if not ne: continue
        try:
            ned = datetime.fromisoformat(str(ne)[:10] + "T00:00:00").date()
            if 0 <= (ned - now.date()).days <= 1:
                earnings_today.append(t)
        except Exception:
            continue
    earnings_today = earnings_today[:5]

    # ── Pick card helper ─────────────────────────────────────────────
    def _pick_card(t):
        sym = (t.get("ticker") or "").upper()
        nm  = _html.escape((t.get("name") or sym)[:40])
        sc  = int(round(_score(t)))
        chg = _chg(t)
        chg_str = f"{'+' if chg>=0 else ''}{chg:.2f}%"
        chg_color = "#15803d" if chg >= 0 else "#b91c1c"
        sect = _html.escape((t.get("sector") or "—")[:24])
        logo_url = f"https://assets.parqet.com/logos/symbol/{sym}"
        return (
            f'<a class="b-pick" href="/report/{sym}">'
              f'<div class="b-pick-logo"><img src="{logo_url}" alt="{sym}" '
                f'onerror="this.outerHTML=\'<span>{sym[:2]}</span>\'"></div>'
              f'<div class="b-pick-body">'
                f'<div class="b-pick-row"><span class="b-pick-sym">{sym}</span>'
                  f'<span class="b-pick-score">{sc}</span></div>'
                f'<div class="b-pick-name">{nm}</div>'
                f'<div class="b-pick-meta"><span>{sect}</span>'
                  f'<span style="color:{chg_color};font-weight:700">{chg_str} today</span></div>'
              f'</div>'
              f'<span class="b-pick-arrow">→</span>'
            f'</a>'
        )

    top3_html = "".join(_pick_card(t) for t in top3) if top3 \
                else '<div class="empty">Universe still loading…</div>'

    def _mini_row(t, with_chg=True):
        sym = (t.get("ticker") or "").upper()
        nm  = _html.escape((t.get("name") or sym)[:28])
        chg = _chg(t)
        chg_str = f"{'+' if chg>=0 else ''}{chg:.2f}%"
        chg_color = "#15803d" if chg >= 0 else "#b91c1c"
        return (
            f'<a class="b-mini" href="/report/{sym}">'
              f'<span class="b-mini-sym">{sym}</span>'
              f'<span class="b-mini-name">{nm}</span>'
              + (f'<span class="b-mini-chg" style="color:{chg_color}">{chg_str}</span>' if with_chg else '')
            + '</a>'
        )

    movers_up_html   = "".join(_mini_row(t) for t in movers_up)   or '<div class="empty">No notable risers yet.</div>'
    movers_down_html = "".join(_mini_row(t) for t in movers_down) or '<div class="empty">No notable decliners — market is calm.</div>'

    earnings_html = ""
    if earnings_today:
        earnings_html = '<div class="b-mini-list">' + "".join(_mini_row(t, with_chg=False) for t in earnings_today) + '</div>'
    else:
        earnings_html = '<div class="empty">No earnings reports in our universe today.</div>'

    # ── Sector spotlight ─────────────────────────────────────────────
    if hot_sector:
        hs_name, hs_avg, hs_n = hot_sector
        hot_sect_html = (
            f'<div class="b-sector"><div class="b-sector-tag">🔥 Heating up</div>'
            f'<div class="b-sector-name">{_html.escape(hs_name)}</div>'
            f'<div class="b-sector-meta">Avg α-Score <strong>{hs_avg:.0f}</strong> across {hs_n} names</div></div>'
        )
    else:
        hot_sect_html = ""
    if cold_sector and cold_sector != hot_sector:
        cs_name, cs_avg, cs_n = cold_sector
        cold_sect_html = (
            f'<div class="b-sector cold"><div class="b-sector-tag">📉 Cooling off</div>'
            f'<div class="b-sector-name">{_html.escape(cs_name)}</div>'
            f'<div class="b-sector-meta">Avg α-Score <strong>{cs_avg:.0f}</strong> across {cs_n} names</div></div>'
        )
    else:
        cold_sect_html = ""

    # ── Lede paragraph (templated; AI-narrated in Phase 2) ──────────
    lede = (
        f"Good morning. Across the {total_n} US stocks TickerMover covers, the average "
        f"α-Score sits at <strong>{avg_score:.0f}</strong> today — with "
        f"<strong>{a_grade}</strong> names holding A-grade status. "
    )
    if hot_sector:
        lede += f"<strong>{_html.escape(hot_sector[0])}</strong> is the strongest pocket of the universe, averaging {hot_sector[1]:.0f}. "
    if top3:
        lede += f"Three names earned the top of today's list — {', '.join((t.get('ticker') or '') for t in top3)}."

    # ── Page render ──────────────────────────────────────────────────
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Morning Brief · {date_full} | TickerMover</title>
<meta name="description" content="TickerMover's daily morning brief for {date_full}: top picks, sector spotlight, notable movers, and what's reporting today. 4-minute read.">
<meta name="theme-color" content="#fafbf7">
<link rel="canonical" href="{SITE_ORIGIN}/brief">
<link rel="icon" type="image/png" sizes="32x32" href="/static/icons/favicon-32.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Manrope:wght@400;500;600;700;800;900&family=Fraunces:opsz,wght@9..144,500;9..144,700;9..144,900&family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">

<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  html{{scroll-behavior:smooth}}
  body{{background:#fafbf7;color:#1A1A1A;font-family:'Source Serif 4',Georgia,serif;line-height:1.6;-webkit-font-smoothing:antialiased}}
  a{{color:inherit;text-decoration:none}}

  .top{{background:#fff;border-bottom:1px solid rgba(10,10,10,.08);position:sticky;top:0;z-index:50}}
  .top-inner{{display:flex;align-items:center;justify-content:space-between;max-width:1100px;margin:0 auto;padding:14px 28px}}
  .brand{{display:flex;align-items:center;gap:10px;font-family:'Manrope','Inter',sans-serif;font-weight:900;font-size:17px;color:#0A0A0A}}
  .brand-mark{{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#fff,#f1f5f9);display:flex;align-items:center;justify-content:center;overflow:hidden;box-shadow:0 0 0 1px rgba(245,166,35,.4)}}
  .brand-mark img{{width:80%;height:80%;object-fit:contain;filter:drop-shadow(0 0 5px rgba(245,166,35,.45))}}
  .brand .h{{background:linear-gradient(135deg,#FFE9B0 0%,#FFC75F 50%,#F5A623 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent}}
  .top-nav{{display:flex;gap:24px;font-family:'Manrope','Inter',sans-serif;font-size:13.5px;font-weight:600;color:#475569}}
  .top-nav a.active{{color:#15803d}}
  .top-cta{{padding:8px 18px;border-radius:999px;background:#0A0A0A;color:#fff;font-family:'Manrope','Inter',sans-serif;font-size:13px;font-weight:700}}
  @media (max-width:760px){{.top-nav{{display:none}}}}

  /* ── Article shell ── */
  .brief{{max-width:760px;margin:0 auto;padding:56px 28px 80px}}
  .masthead{{display:flex;align-items:center;gap:10px;font-family:'Manrope','Inter',sans-serif;font-size:11px;font-weight:700;letter-spacing:.18em;color:#15803d;text-transform:uppercase;margin-bottom:14px}}
  .masthead::before{{content:"";width:24px;height:2px;background:linear-gradient(90deg,#F5A623,#FFC75F);border-radius:2px}}
  h1.brief-title{{font-family:'Fraunces',serif;font-weight:500;font-size:clamp(36px,5vw,58px);line-height:1.05;letter-spacing:-.025em;color:#0A0A0A;margin-bottom:14px}}
  h1.brief-title em{{font-style:italic;color:#15803d}}
  .brief-date{{font-family:'Manrope','Inter',sans-serif;font-size:13.5px;color:#64748b;margin-bottom:8px;font-weight:500}}
  .brief-meta{{font-family:'Manrope','Inter',sans-serif;font-size:12.5px;color:#94a3b8;display:flex;gap:14px;flex-wrap:wrap;margin-bottom:36px}}
  .brief-meta span{{display:inline-flex;align-items:center;gap:6px}}

  .lede{{font-family:'Source Serif 4',serif;font-size:22px;line-height:1.55;color:#0A0A0A;font-weight:500;margin-bottom:48px}}
  .lede strong{{color:#15803d;font-weight:700}}

  h2{{font-family:'Fraunces',serif;font-weight:500;font-size:30px;letter-spacing:-.02em;color:#0A0A0A;margin:48px 0 18px;line-height:1.15}}
  h2 em{{font-style:italic;color:#15803d}}

  /* ── Top 3 pick cards ── */
  .b-picks{{display:flex;flex-direction:column;gap:10px;margin-bottom:24px}}
  .b-pick{{
    display:grid;grid-template-columns:auto 1fr auto;gap:16px;align-items:center;
    padding:18px 22px;border-radius:14px;background:#fff;border:1px solid rgba(10,10,10,.08);
    transition:transform .15s,box-shadow .15s,border-color .15s;
    box-shadow:0 1px 2px rgba(10,10,10,.04);
  }}
  .b-pick:hover{{transform:translateX(4px);border-color:rgba(245,166,35,.30);box-shadow:0 12px 28px -16px rgba(245,166,35,.25)}}
  .b-pick-logo{{width:48px;height:48px;border-radius:11px;background:#fff;box-shadow:0 0 0 1px rgba(10,10,10,.08);display:flex;align-items:center;justify-content:center;overflow:hidden}}
  .b-pick-logo img{{width:100%;height:100%;object-fit:contain;padding:5px}}
  .b-pick-logo span{{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:14px;color:#0A0A0A}}
  .b-pick-body{{min-width:0;font-family:'Manrope','Inter',sans-serif}}
  .b-pick-row{{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:4px}}
  .b-pick-sym{{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:16px;color:#0A0A0A;letter-spacing:-.01em}}
  .b-pick-score{{font-family:'Fraunces',serif;font-weight:500;font-size:28px;color:#15803d;line-height:1;letter-spacing:-.02em}}
  .b-pick-name{{font-size:13.5px;color:#475569;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .b-pick-meta{{font-family:'Manrope','Inter',sans-serif;font-size:12px;color:#94a3b8;margin-top:6px;display:flex;gap:12px;font-feature-settings:'tnum' 1}}
  .b-pick-arrow{{font-family:'Manrope','Inter',sans-serif;font-size:22px;color:#94a3b8;transition:transform .15s,color .15s}}
  .b-pick:hover .b-pick-arrow{{color:#15803d;transform:translateX(4px)}}

  /* ── Two-column row (movers / sectors) ── */
  .b-row{{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-bottom:24px}}
  @media (max-width:640px){{.b-row{{grid-template-columns:1fr}}}}
  .b-col{{background:#fff;border:1px solid rgba(10,10,10,.08);border-radius:14px;padding:20px;box-shadow:0 1px 2px rgba(10,10,10,.04)}}
  .b-col h3{{font-family:'Fraunces',serif;font-weight:500;font-size:18px;color:#0A0A0A;letter-spacing:-.01em;margin-bottom:14px;display:flex;align-items:center;gap:8px}}

  /* Mini ticker rows */
  .b-mini-list{{display:flex;flex-direction:column;gap:2px}}
  .b-mini{{display:grid;grid-template-columns:60px 1fr auto;gap:10px;align-items:baseline;padding:8px 10px;border-radius:8px;font-family:'Manrope','Inter',sans-serif;transition:background .12s;color:inherit}}
  .b-mini:hover{{background:#FAFBFC}}
  .b-mini-sym{{font-family:'JetBrains Mono',monospace;font-weight:800;font-size:13px;color:#0A0A0A}}
  .b-mini-name{{font-size:12.5px;color:#475569;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .b-mini-chg{{font-family:'JetBrains Mono',monospace;font-weight:700;font-size:12.5px;text-align:right;font-feature-settings:'tnum' 1}}

  /* Sector spotlight */
  /* Heating Up sector card — saffron warmth matches the fire metaphor
     (was green-on-green wash). Cold/pressure card stays semantic red. */
  .b-sector{{padding:18px 20px;border-radius:14px;background:linear-gradient(135deg,#FFF8E5 0%,#ffffff 70%);border:1px solid rgba(245,166,35,.25);font-family:'Manrope','Inter',sans-serif}}
  .b-sector.cold{{background:linear-gradient(135deg,#FEE2E2 0%,#ffffff 70%);border-color:rgba(220,38,38,.20)}}
  .b-sector-tag{{font-family:'JetBrains Mono',monospace;font-size:11px;letter-spacing:.10em;text-transform:uppercase;font-weight:700;color:#F5A623;margin-bottom:8px}}
  .b-sector.cold .b-sector-tag{{color:#b91c1c}}
  .b-sector-name{{font-family:'Fraunces',serif;font-weight:500;font-size:22px;color:#0A0A0A;letter-spacing:-.015em;margin-bottom:6px}}
  .b-sector-meta{{font-size:13px;color:#475569}}
  .b-sector-meta strong{{color:#0A0A0A;font-weight:700;font-family:'JetBrains Mono',monospace;font-feature-settings:'tnum' 1}}

  .empty{{padding:20px 4px;color:#94a3b8;font-size:13px;font-family:'Manrope','Inter',sans-serif;font-style:italic;text-align:center}}

  /* ── Mode swap CTA ── */
  .swap{{background:#0A0A0A;color:#fff;border-radius:16px;padding:24px 28px;margin:48px 0 24px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}}
  .swap .lbl{{font-family:'Manrope','Inter',sans-serif;font-size:14px;line-height:1.5}}
  .swap .lbl strong{{color:#FFE9B0;display:block;font-family:'Fraunces',serif;font-weight:500;font-size:18px;margin-bottom:4px;letter-spacing:-.01em;font-style:italic}}
  .swap .btn{{padding:11px 22px;border-radius:999px;background:linear-gradient(135deg,#FFE9B0,#FFC75F,#F5A623);color:#0A0A0A;font-family:'Manrope','Inter',sans-serif;font-weight:800;font-size:13.5px;text-decoration:none;box-shadow:0 8px 22px -8px rgba(245,166,35,.5)}}

  footer{{padding:60px 28px 40px;color:#94a3b8;font-family:'Manrope','Inter',sans-serif;font-size:12.5px;text-align:center;border-top:1px solid rgba(10,10,10,.08);margin-top:60px}}
  footer a{{color:#15803d;font-weight:700}}
</style>
</head>
<body>

<div class="top">
  <div class="top-inner">
    <a class="brand" href="/">
      <span class="brand-mark"><img src="/static/icons/alpha-logo-bare-64.png" alt=""></span>
      Alpha<span class="h">Hunt</span>
    </a>
    <div class="top-nav">
      <a href="/brief" class="active">Daily Brief</a>
      <a href="/reports">Reports</a>
      <a href="/#how">How it works</a>
      <a href="/app">Dashboard</a>
    </div>
    <a class="top-cta" href="/app">Open dashboard →</a>
  </div>
</div>

<article class="brief">
  <div class="masthead">Morning Brief</div>
  <h1 class="brief-title">{weekday}'s <em>edition</em>.</h1>
  <div class="brief-date">{date_full}</div>
  <div class="brief-meta">
    <span>📅 Auto-refreshes during US market hours</span>
    <span>⏱ 4 min read</span>
    <span>📊 {total_n} US stocks scanned</span>
  </div>

  <p class="lede">{lede}</p>

  <h2>Today's <em>three to watch</em>.</h2>
  <div class="b-picks">{top3_html}</div>

  <div class="b-row">
    {hot_sect_html}
    {cold_sect_html}
  </div>

  <h2>Notable <em>movers</em>.</h2>
  <div class="b-row">
    <div class="b-col">
      <h3>📈 Up today</h3>
      <div class="b-mini-list">{movers_up_html}</div>
    </div>
    <div class="b-col">
      <h3>📉 Down today</h3>
      <div class="b-mini-list">{movers_down_html}</div>
    </div>
  </div>

  <h2>Reporting <em>today</em>.</h2>
  <div class="b-col">
    <h3>🗓 Earnings calendar</h3>
    {earnings_html}
  </div>

  <div class="swap">
    <div class="lbl">
      <strong>Want the live data view?</strong>
      Open the dashboard for real-time scores, sector heat map, and the full watchlist.
    </div>
    <a class="btn" href="/app">Open Dashboard →</a>
  </div>

</article>

<footer>
  Daily Brief is auto-assembled from live universe data. Not investment advice. <a href="/reports">Browse all reports →</a>
</footer>

</body></html>"""


# ═════════════════════════════════════════════════════════════════════════
#  /reports — Reading Mode index (all covered tickers, ranked)
# ═════════════════════════════════════════════════════════════════════════
@app.get("/reports", response_class=HTMLResponse)
async def reports_index():
    """Light, editorial index of every covered ticker — sorted by Alpha
    Score, each row deep-links to its full /report/{TICKER} page.

    This is the destination behind the landing nav's "Reports" link.
    Built so a reader can browse the universe in Reading Mode without
    having to type a ticker into the URL.
    """
    import html as _html
    rows_data = sorted(
        (x for x in (_universe_data or []) if x.get("ticker")),
        key=lambda x: (x.get("smart_score") or x.get("pop_score") or 0),
        reverse=True,
    )
    items_html = []
    for t in rows_data[:300]:  # cap to keep first paint fast
        sym  = (t.get("ticker") or "").upper()
        name = _html.escape(t.get("name") or sym)
        sect = _html.escape(t.get("sector") or "—")
        try:
            sc = int(round(float(t.get("smart_score") or t.get("pop_score") or 0)))
        except Exception:
            sc = 0
        chg = t.get("change_pct")
        chg_str = ""
        chg_cls = ""
        if isinstance(chg, (int, float)):
            chg_str = f"{'+' if chg>=0 else ''}{chg:.2f}%"
            chg_cls = "up" if chg >= 0 else "dn"
        # Color the score by band
        band = "hi" if sc >= 80 else "mid" if sc >= 60 else "lo"
        items_html.append(
            f'<a class="row" href="/report/{sym}">'
            f'<span class="sym">{sym}</span>'
            f'<span class="nm">{name}</span>'
            f'<span class="sect">{sect}</span>'
            f'<span class="chg {chg_cls}">{chg_str}</span>'
            f'<span class="sc {band}">{sc}</span>'
            f'</a>'
        )
    rows_html = "\n".join(items_html) or '<div class="empty">Universe not loaded yet — refresh in a moment.</div>'
    total = len(rows_data)
    return HTMLResponse(content=f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Reports — {total} US stocks scored & explained | TickerMover</title>
<meta name="description" content="Every US stock in the TickerMover universe, scored across six investment pillars and explained in a long-form research report. Sorted by today's Alpha Score.">
<meta name="theme-color" content="#fafbf7">
<link rel="canonical" href="{SITE_ORIGIN}/reports">
<link rel="icon" type="image/png" sizes="32x32" href="/static/icons/favicon-32.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Manrope:wght@400;500;600;700;800;900&family=Fraunces:opsz,wght@9..144,500;9..144,700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#fafbf7;color:#1A1A1A;font-family:'Manrope','Inter',system-ui,sans-serif;font-weight:500;-webkit-font-smoothing:antialiased}}
  a{{text-decoration:none;color:inherit}}
  .top{{background:#fff;border-bottom:1px solid rgba(10,10,10,.08);position:sticky;top:0;z-index:50}}
  .top-inner{{display:flex;align-items:center;justify-content:space-between;max-width:1200px;margin:0 auto;padding:14px 28px}}
  .brand{{display:flex;align-items:center;gap:10px;font-weight:900;font-size:17px;letter-spacing:-.02em;color:#0A0A0A}}
  .brand-mark{{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#fff,#f1f5f9);display:flex;align-items:center;justify-content:center;overflow:hidden;box-shadow:0 0 0 1px rgba(245,166,35,.4),0 6px 16px -6px rgba(245,166,35,.3)}}
  .brand-mark img{{width:80%;height:80%;object-fit:contain;filter:drop-shadow(0 0 5px rgba(245,166,35,.45))}}
  .brand .h{{background:linear-gradient(135deg,#FFE9B0 0%,#FFC75F 50%,#F5A623 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent;filter:drop-shadow(0 0 6px rgba(245,166,35,.25))}}
  .top-nav{{display:flex;gap:24px;font-size:13.5px;font-weight:600;color:#475569}}
  .top-nav a.active{{color:#15803d}}
  .top-cta{{padding:8px 18px;border-radius:999px;background:#0A0A0A;color:#fff;font-size:13px;font-weight:700}}
  @media (max-width:760px){{.top-nav{{display:none}}}}

  .head{{max-width:1100px;margin:0 auto;padding:56px 28px 32px}}
  .eyebrow{{display:inline-block;font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:#15803d;font-weight:700;padding:5px 10px;border-radius:999px;background:rgba(245,166,35,.10);border:1px solid rgba(245,166,35,.30);margin-bottom:14px}}
  h1{{font-family:'Fraunces',serif;font-weight:500;font-size:clamp(36px,5vw,56px);line-height:1.05;letter-spacing:-.025em;color:#0A0A0A;margin-bottom:14px}}
  h1 em{{font-style:italic;color:#15803d}}
  .sub{{font-size:16px;color:#475569;line-height:1.6;max-width:600px}}

  .list-wrap{{max-width:1100px;margin:0 auto;padding:0 28px 60px}}
  .list-head{{display:grid;grid-template-columns:80px 1fr 180px 90px 70px;gap:14px;padding:12px 22px;background:#fff;border:1px solid rgba(10,10,10,.08);border-radius:14px 14px 0 0;border-bottom:none;font-family:'JetBrains Mono',monospace;font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:#94a3b8;font-weight:700}}
  .list{{background:#fff;border:1px solid rgba(10,10,10,.08);border-radius:0 0 14px 14px;overflow:hidden;box-shadow:0 1px 2px rgba(10,10,10,.04),0 12px 30px -22px rgba(10,10,10,.15)}}
  .row{{display:grid;grid-template-columns:80px 1fr 180px 90px 70px;gap:14px;padding:16px 22px;border-bottom:1px solid rgba(10,10,10,.06);align-items:center;font-size:14px;transition:background .12s,transform .15s;color:inherit;text-decoration:none}}
  .row:last-child{{border-bottom:none}}
  .row:hover{{background:#FAFBFC;transform:translateX(4px)}}
  .row .sym{{font-family:'JetBrains Mono',monospace;font-weight:800;color:#0A0A0A;letter-spacing:-.01em}}
  .row .nm{{font-family:'Fraunces',serif;font-weight:500;font-size:16px;color:#0A0A0A;letter-spacing:-.01em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .row .sect{{color:#64748b;font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
  .row .chg{{font-family:'JetBrains Mono',monospace;font-weight:700;text-align:right;color:#64748b}}
  .row .chg.up{{color:#15803d}} .row .chg.dn{{color:#b91c1c}}
  .row .sc{{font-family:'Fraunces',serif;font-weight:500;font-size:24px;text-align:right;letter-spacing:-.02em}}
  .row .sc.hi{{color:#15803d}} .row .sc.mid{{color:#D4860A}} .row .sc.lo{{color:#64748b}}
  .empty{{padding:36px;text-align:center;color:#94a3b8}}

  @media (max-width:760px){{
    .list-head{{grid-template-columns:70px 1fr 70px;gap:10px;font-size:9.5px}}
    .list-head .sect-h,.list-head .chg-h{{display:none}}
    .row{{grid-template-columns:70px 1fr 70px;gap:10px;padding:14px 16px;font-size:13px}}
    .row .sect,.row .chg{{display:none}}
    .row .sc{{font-size:20px}}
  }}
</style>
</head>
<body>

<div class="top">
  <div class="top-inner">
    <a class="brand" href="/">
      <span class="brand-mark"><img src="/static/icons/alpha-logo-bare-64.png" alt=""></span>
      Alpha<span class="h">Hunt</span>
    </a>
    <div class="top-nav">
      <a href="/reports" class="active">Reports</a>
      <a href="/#how">How it works</a>
      <a href="/#pillars">Methodology</a>
      <a href="/app">Dashboard</a>
    </div>
    <a class="top-cta" href="/app">Open dashboard →</a>
  </div>
</div>

<div class="head">
  <div class="eyebrow">Research library</div>
  <h1>{total} stocks. <em>Every</em> one explained.</h1>
  <p class="sub">Every name we cover gets a full research report with Alpha Score, six-pillar breakdown, verdict, and the reasoning behind it. Sorted by today's score.</p>
</div>

<div class="list-wrap">
  <div class="list-head"><span>Ticker</span><span>Name</span><span class="sect-h">Sector</span><span class="chg-h" style="text-align:right">Δ Day</span><span style="text-align:right">α-Score</span></div>
  <div class="list">{rows_html}</div>
</div>

</body></html>""")


# ═════════════════════════════════════════════════════════════════════════
#  /report/{ticker} — "Reading Mode" research report (Phase 1)
# ═════════════════════════════════════════════════════════════════════════
#  Long-form, editorial single-ticker page styled like a Bloomberg or
#  Stratechery research report. Light theme, Fraunces serif headings,
#  Source Serif body. Sits alongside the existing SEO-focused /stocks/...
#  page; over time we'll redirect /stocks/* → /report/* once the new
#  surface is feature-complete (AI narration, related-reads, etc).
#
#  Implements the strategy decided in the hybrid theme demo: Reading
#  Mode for deep research consumption (high time-on-site, premium feel),
#  Data Mode for the dashboard (glance & scan).
# ═════════════════════════════════════════════════════════════════════════
@app.get("/report/{ticker}", response_class=HTMLResponse)
async def report_page(ticker: str):
    """Premium long-form research report for a single ticker."""
    sym = ticker.upper().strip()
    t = next((x for x in (_universe_data or []) if (x.get("ticker") or "").upper() == sym), None)
    if not t:
        return HTMLResponse(content=_render_unknown_stock(sym), status_code=200)
    return HTMLResponse(content=_render_report_page(t))


def _render_report_page(t: dict) -> str:
    """
    Render the Reading Mode HTML for one ticker.

    Reads what's available from the universe row `t` with safe fallbacks
    so any missing field degrades gracefully instead of breaking the page.
    """
    import html as _html

    sym       = (t.get("ticker") or "").upper()
    name      = t.get("name") or sym
    sector    = t.get("sector") or "—"
    industry  = t.get("industry") or t.get("sub_industry") or ""
    price     = t.get("price")
    chg_pct   = t.get("change_pct")
    mkt_cap   = t.get("market_cap")
    score     = t.get("smart_score") or t.get("pop_score") or 0
    try:
        score = int(round(float(score)))
    except Exception:
        score = 0

    # Six-pillar breakdown — pull whatever the scorer left on the row.
    # Falls back to a sensible scoring sketch (centered on the headline
    # score) when individual pillars aren't materialized yet.
    def _pillar(*keys, default):
        for k in keys:
            v = t.get(k)
            if v is not None:
                try:
                    return max(0, min(100, int(round(float(v)))))
                except Exception:
                    continue
        return default
    p_mom  = _pillar("momentum_score",  "score_momentum",  default=score)
    p_grw  = _pillar("growth_score",    "score_growth",    default=max(0, score - 4))
    p_qty  = _pillar("quality_score",   "score_quality",   default=max(0, score - 2))
    p_val  = _pillar("valuation_score", "score_valuation", default=max(0, score - 12))
    p_sen  = _pillar("sentiment_score", "score_sentiment", default=score)
    p_pot  = _pillar("potential_score", "score_potential", "growth_potential_score", default=max(0, score - 6))

    # Verdict pill — derived from score band
    if score >= 80:
        verdict, vd_tone = "Strong Buy", "#15803d"
    elif score >= 70:
        verdict, vd_tone = "Buy", "#D4860A"
    elif score >= 60:
        verdict, vd_tone = "Accumulate", "#D4860A"
    elif score >= 45:
        verdict, vd_tone = "Hold", "#475569"
    else:
        verdict, vd_tone = "Avoid", "#b91c1c"

    # Price + change formatted
    px_str  = f"${price:.2f}" if isinstance(price, (int, float)) and price else "—"
    chg_str = ""
    chg_color = "#64748b"
    if isinstance(chg_pct, (int, float)):
        sign = "+" if chg_pct >= 0 else ""
        chg_color = "#15803d" if chg_pct >= 0 else "#b91c1c"
        chg_str = f"{sign}{chg_pct:.2f}% today"
    mc_str = ""
    if isinstance(mkt_cap, (int, float)) and mkt_cap > 0:
        if mkt_cap >= 1_000_000_000_000:
            mc_str = f" · Mkt cap ${mkt_cap/1e12:.1f}T"
        elif mkt_cap >= 1_000_000_000:
            mc_str = f" · Mkt cap ${mkt_cap/1e9:.1f}B"
        elif mkt_cap >= 1_000_000:
            mc_str = f" · Mkt cap ${mkt_cap/1e6:.0f}M"

    # Logo URL (Parqet free symbol CDN, gracefully degrades to monogram)
    logo_url = f"https://assets.parqet.com/logos/symbol/{sym}"
    logo_mono = sym[:2]

    # Headline — composed from the score band
    if score >= 80:
        headline = f"{name.split(',')[0]} — the <em>setup</em> is back."
    elif score >= 70:
        headline = f"{name.split(',')[0]} — quietly worth a <em>look</em>."
    elif score >= 60:
        headline = f"{name.split(',')[0]} — a <em>partial</em> thesis."
    elif score >= 45:
        headline = f"{name.split(',')[0]} — there's <em>nothing</em> wrong, just nothing right."
    else:
        headline = f"{name.split(',')[0]} — the model says <em>wait</em>."

    # Story sections (templated for now; AI-narrated in Phase 2)
    lede = (
        f"TickerMover's engine scores {sym} at <strong>{score}</strong> today across six "
        f"investment pillars. {('That puts it in the top quintile of our universe.' if score >= 80 else 'That places it in the upper-middle of our coverage.' if score >= 60 else 'That keeps it on the watchlist but not in Top Hunts.') }"
    )
    sector_line = (
        f"As a {industry or sector} name, {sym} is compared against {sector.lower() if sector!='—' else 'sector'} peers on every pillar — "
        "so the score reflects relative quality, not absolute size."
    )

    # ── Render the page ──────────────────────────────────────────────
    safe_name = _html.escape(name)
    safe_sector = _html.escape(sector)
    safe_industry = _html.escape(industry or sector)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>{sym} — {safe_name} · Alpha Score {score} | TickerMover</title>
<meta name="description" content="{safe_name} ({sym}) Alpha Score {score} · {verdict}. Six-pillar research report covering momentum, growth, quality, valuation, sentiment and growth potential. Updated today.">
<meta name="theme-color" content="#fafbf7">
<link rel="canonical" href="{SITE_ORIGIN}/report/{sym}">
<link rel="icon" type="image/png" sizes="32x32" href="/static/icons/favicon-32.png">

<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=Manrope:wght@400;500;600;700;800;900&family=Fraunces:opsz,wght@9..144,400;9..144,500;9..144,700;9..144,900&family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">

<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  html{{scroll-behavior:smooth}}
  body{{background:#fafbf7;color:#1A1A1A;font-family:'Source Serif 4',Georgia,serif;-webkit-font-smoothing:antialiased;line-height:1.6}}
  /* OpenType polish — tabular numbers + Inter alt glyphs on every
     numeric / mono / UI surface. Prose stays proportional. */
  .verdict-score .num,.verdict-meta,.verdict-sym,.verdict-tag,
  .pbr-score,.pbr-label,
  .read-meta,.byline,.breadcrumb,
  .top-nav,.top-cta,.related-meta,.related-tag{{
    font-feature-settings:'tnum' 1, 'cv02' 1, 'cv11' 1;
    font-variant-numeric:tabular-nums;
  }}
  a{{color:#15803d;text-decoration:underline;text-decoration-color:rgba(245,166,35,.4);text-underline-offset:3px}}
  img{{display:block;max-width:100%}}

  /* ─── Top bar ─── */
  .top{{background:#fff;border-bottom:1px solid rgba(10,10,10,.08);position:sticky;top:0;z-index:50}}
  .top-inner{{display:flex;align-items:center;justify-content:space-between;max-width:1200px;margin:0 auto;padding:14px 28px}}
  .brand{{display:flex;align-items:center;gap:10px;font-family:'Manrope','Inter',sans-serif;font-weight:900;font-size:17px;letter-spacing:-.02em;color:#0A0A0A;text-decoration:none}}
  .brand-mark{{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#fff,#f1f5f9);display:flex;align-items:center;justify-content:center;overflow:hidden;box-shadow:0 0 0 1px rgba(245,166,35,.4),0 6px 16px -6px rgba(245,166,35,.3)}}
  .brand-mark img{{width:80%;height:80%;object-fit:contain;filter:drop-shadow(0 0 5px rgba(245,166,35,.45))}}
  .brand .h{{background:linear-gradient(135deg,#FFE9B0 0%,#FFC75F 50%,#F5A623 100%);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:transparent;filter:drop-shadow(0 0 6px rgba(245,166,35,.25))}}
  .top-nav{{display:flex;gap:24px;font-family:'Manrope','Inter',sans-serif;font-size:13.5px;font-weight:600;color:#475569}}
  .top-nav a{{text-decoration:none;color:inherit;transition:color .15s}}
  .top-nav a:hover{{color:#0A0A0A}}
  .top-nav a.active{{color:#15803d}}
  .top-cta{{padding:8px 18px;border-radius:999px;background:#0A0A0A;color:#fff;font-family:'Manrope','Inter',sans-serif;font-size:13px;font-weight:700;text-decoration:none}}
  @media (max-width:760px){{.top-nav{{display:none}}}}

  /* ─── Article ─── */
  .article{{max-width:760px;margin:48px auto 0;padding:0 28px}}
  .breadcrumb{{font-family:'Manrope','Inter',sans-serif;font-size:12px;letter-spacing:.08em;color:#64748b;margin-bottom:24px;text-transform:uppercase;font-weight:600}}
  .breadcrumb a{{color:#15803d;text-decoration:none}}
  h1.article-title{{font-family:'Fraunces',serif;font-weight:500;font-size:clamp(34px,5vw,58px);line-height:1.05;letter-spacing:-.025em;color:#0A0A0A;margin-bottom:22px}}
  h1.article-title em{{font-style:italic;color:#15803d}}
  .byline{{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:36px;font-family:'Manrope','Inter',sans-serif;font-size:13.5px;color:#64748b}}
  .byline .avatar{{width:42px;height:42px;border-radius:50%;background:linear-gradient(135deg,#F5A623,#FFC75F);display:flex;align-items:center;justify-content:center;font-weight:800;font-size:13px;color:#0A0A0A}}
  .byline strong{{color:#0A0A0A;font-weight:700;display:block}}
  .read-meta{{display:flex;gap:16px;font-size:12.5px;color:#94a3b8;margin-left:auto;flex-wrap:wrap}}

  /* ─── Verdict card ─── */
  .verdict{{background:linear-gradient(180deg,#ffffff,#FAFBFC);border:1px solid rgba(10,10,10,.08);border-radius:18px;padding:28px 30px;margin-bottom:48px;box-shadow:0 1px 0 #fff inset,0 18px 40px -28px rgba(10,10,10,.18);display:grid;grid-template-columns:auto 1fr auto;gap:24px;align-items:center}}
  .verdict-logo{{width:64px;height:64px;border-radius:14px;background:#fff;color:#0A0A0A;display:flex;align-items:center;justify-content:center;font-family:'JetBrains Mono',monospace;font-weight:800;font-size:18px;letter-spacing:.02em;flex-shrink:0;box-shadow:0 0 0 1px rgba(10,10,10,.08);overflow:hidden}}
  .verdict-logo img{{width:100%;height:100%;object-fit:contain;padding:6px}}
  .verdict-info{{min-width:0}}
  .verdict-sym{{font-family:'Manrope','Inter',sans-serif;font-size:13px;font-weight:700;letter-spacing:.04em;color:#64748b;text-transform:uppercase;margin-bottom:6px}}
  .verdict-title{{font-family:'Fraunces',serif;font-weight:500;font-size:24px;letter-spacing:-.015em;color:#0A0A0A;margin-bottom:6px;line-height:1.15}}
  .verdict-meta{{font-family:'Manrope','Inter',sans-serif;font-size:12.5px;color:#64748b;margin-bottom:8px}}
  .verdict-tag{{display:inline-block;padding:5px 11px;border-radius:999px;background:rgba(245,166,35,.12);color:{vd_tone};font-family:'Manrope','Inter',sans-serif;font-size:11.5px;font-weight:700;letter-spacing:.04em;border:1px solid rgba(245,166,35,.3)}}
  .verdict-score{{text-align:right;flex-shrink:0}}
  .verdict-score .num{{font-family:'Fraunces',serif;font-weight:500;font-size:64px;letter-spacing:-.04em;line-height:1;color:#15803d}}
  .verdict-score .lbl{{font-family:'Manrope','Inter',sans-serif;font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:#94a3b8;font-weight:700;margin-top:4px}}
  @media (max-width:560px){{.verdict{{grid-template-columns:1fr;text-align:center}}.verdict-score{{text-align:center}}}}

  /* ─── Body type ─── */
  .body{{font-family:'Source Serif 4',Georgia,serif;font-size:19px;line-height:1.7;color:#1A1A1A}}
  .body p{{margin-bottom:24px}}
  .body .lede{{font-size:22px;line-height:1.55;color:#0A0A0A;font-weight:500;margin-bottom:36px}}
  .body h2{{font-family:'Fraunces',serif;font-weight:500;font-size:30px;letter-spacing:-.02em;color:#0A0A0A;margin:48px 0 18px;line-height:1.15}}
  .body h2 em{{font-style:italic;color:#15803d}}
  .body strong{{color:#0A0A0A;font-weight:600}}
  .body em{{font-style:italic}}
  .pullquote{{border-left:4px solid #F5A623;padding:8px 24px;margin:36px 0;font-family:'Fraunces',serif;font-weight:400;font-size:22px;line-height:1.45;color:#0A0A0A;font-style:italic}}

  /* ─── Pillar breakdown ─── */
  .pillar-card{{background:#fff;border:1px solid rgba(10,10,10,.08);border-radius:16px;padding:24px;margin:32px 0;box-shadow:0 1px 0 #fff inset,0 12px 30px -22px rgba(10,10,10,.15)}}
  .pillar-card-head{{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:18px}}
  .pillar-card h3{{font-family:'Fraunces',serif;font-weight:500;font-size:18px;color:#0A0A0A}}
  .pillar-card-head span{{font-family:'JetBrains Mono',monospace;font-size:11px;color:#94a3b8;letter-spacing:.10em;text-transform:uppercase}}
  .pillar-bars{{display:flex;flex-direction:column;gap:11px;font-family:'Manrope','Inter',sans-serif}}
  .pillar-bar-row{{display:grid;grid-template-columns:110px 1fr 36px;gap:12px;align-items:center;font-size:13px}}
  .pbr-label{{color:#64748b;font-weight:600}}
  .pbr-track{{height:8px;border-radius:99px;background:#f1f5f9;overflow:hidden;position:relative}}
  .pbr-fill{{position:absolute;inset:0;border-radius:inherit;transform-origin:left;background:var(--pc,#F5A623)}}
  .pbr-score{{font-family:'JetBrains Mono',monospace;font-weight:800;text-align:right;color:#0A0A0A}}

  /* ─── Mode swap card ─── */
  .mode-swap{{background:#0A0A0A;color:#fff;border-radius:16px;padding:24px 28px;margin:48px 0 24px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}}
  .mode-swap .label{{font-family:'Manrope','Inter',sans-serif;font-size:14px;line-height:1.5}}
  .mode-swap .label strong{{color:#FFE9B0;display:block;font-family:'Fraunces',serif;font-weight:500;font-size:18px;margin-bottom:4px;letter-spacing:-.01em}}
  .mode-swap .btn{{padding:11px 22px;border-radius:999px;background:linear-gradient(135deg,#FFE9B0,#FFC75F,#F5A623);color:#0A0A0A;font-family:'Manrope','Inter',sans-serif;font-weight:800;font-size:13.5px;text-decoration:none;box-shadow:0 8px 22px -8px rgba(245,166,35,.5)}}

  /* ─── Related ─── */
  .related{{margin-top:48px;padding-top:32px;border-top:1px solid rgba(10,10,10,.08)}}
  .related h4{{font-family:'Manrope','Inter',sans-serif;font-size:11.5px;letter-spacing:.18em;text-transform:uppercase;color:#94a3b8;font-weight:700;margin-bottom:18px}}
  .related-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}}
  @media (max-width:760px){{.related-grid{{grid-template-columns:1fr}}}}
  .related-item{{padding:18px;border-radius:14px;background:#fff;border:1px solid rgba(10,10,10,.08);transition:transform .15s,box-shadow .15s;text-decoration:none;color:inherit;display:block}}
  .related-item:hover{{transform:translateY(-3px);box-shadow:0 16px 32px -20px rgba(10,10,10,.18)}}
  .related-tag{{font-family:'Manrope','Inter',sans-serif;font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;color:#15803d;font-weight:700;margin-bottom:8px}}
  .related-title{{font-family:'Fraunces',serif;font-weight:500;font-size:17px;line-height:1.25;color:#0A0A0A;letter-spacing:-.01em}}
  .related-meta{{font-family:'Manrope','Inter',sans-serif;font-size:12px;color:#94a3b8;margin-top:10px}}

  /* ─── Footer ─── */
  footer{{padding:60px 28px 40px;color:#94a3b8;font-family:'Manrope','Inter',sans-serif;font-size:12.5px;text-align:center;border-top:1px solid rgba(10,10,10,.08);margin-top:80px}}
</style>
</head>
<body>

<!-- Top bar -->
<div class="top">
  <div class="top-inner">
    <a class="brand" href="/">
      <span class="brand-mark"><img src="/static/icons/alpha-logo-bare-64.png" alt=""></span>
      Alpha<span class="h">Hunt</span>
    </a>
    <div class="top-nav">
      <a href="/">Briefings</a>
      <a href="#" class="active">Reports</a>
      <a href="/#pillars">Methodology</a>
      <a href="/app">Dashboard</a>
    </div>
    <a class="top-cta" href="/app">Open dashboard →</a>
  </div>
</div>

<article class="article">
  <div class="breadcrumb"><a href="/">Reports</a> · <a href="/">{safe_sector}</a> · {sym}</div>

  <h1 class="article-title">{headline}</h1>

  <div class="byline">
    <div class="avatar">α</div>
    <div>
      <strong>TickerMover Research</strong>
      <span>AI-generated · Reviewed by editorial</span>
    </div>
    <div class="read-meta">
      <span>📅 Updated today</span>
      <span>⏱ 5 min read</span>
      <span>🔄 Live data</span>
    </div>
  </div>

  <!-- Verdict card -->
  <div class="verdict">
    <div class="verdict-logo">
      <img src="{logo_url}" alt="{sym} logo" onerror="this.outerHTML='<span style=&quot;font-family:JetBrains Mono,monospace;font-weight:800;font-size:18px;color:#0A0A0A&quot;>{logo_mono}</span>'">
    </div>
    <div class="verdict-info">
      <div class="verdict-sym">{sym} · {safe_industry}</div>
      <div class="verdict-title">{safe_name}</div>
      <div class="verdict-meta">{px_str} · <span style="color:{chg_color};font-weight:700">{chg_str}</span>{mc_str}</div>
      <span class="verdict-tag">{verdict}</span>
    </div>
    <div class="verdict-score">
      <div class="num">{score}</div>
      <div class="lbl">α-Score</div>
    </div>
  </div>

  <!-- Body -->
  <div class="body">
    <p class="lede">{lede}</p>

    <p>{sector_line}</p>

    <h2>The Alpha Score, <em>broken down</em>.</h2>

    <p>The headline {score} is composed from six pillars — each scored 0–100 on quality-adjusted percentile basis against the universe. Here's what they look like for {sym} right now:</p>

    <div class="pillar-card">
      <div class="pillar-card-head">
        <h3>Six-pillar breakdown</h3>
        <span>{sym} · Today</span>
      </div>
      <div class="pillar-bars">
        <div class="pillar-bar-row"><span class="pbr-label">Momentum</span><span class="pbr-track"><span class="pbr-fill" style="--pc:#F5A623;transform:scaleX({p_mom/100:.2f})"></span></span><span class="pbr-score">{p_mom}</span></div>
        <div class="pillar-bar-row"><span class="pbr-label">Growth</span><span class="pbr-track"><span class="pbr-fill" style="--pc:#F5A623;transform:scaleX({p_grw/100:.2f})"></span></span><span class="pbr-score">{p_grw}</span></div>
        <div class="pillar-bar-row"><span class="pbr-label">Quality</span><span class="pbr-track"><span class="pbr-fill" style="--pc:#FFC75F;transform:scaleX({p_qty/100:.2f})"></span></span><span class="pbr-score">{p_qty}</span></div>
        <div class="pillar-bar-row"><span class="pbr-label">Valuation</span><span class="pbr-track"><span class="pbr-fill" style="--pc:#FFC75F;transform:scaleX({p_val/100:.2f})"></span></span><span class="pbr-score">{p_val}</span></div>
        <div class="pillar-bar-row"><span class="pbr-label">Sentiment</span><span class="pbr-track"><span class="pbr-fill" style="--pc:#F5A623;transform:scaleX({p_sen/100:.2f})"></span></span><span class="pbr-score">{p_sen}</span></div>
        <div class="pillar-bar-row"><span class="pbr-label">Potential</span><span class="pbr-track"><span class="pbr-fill" style="--pc:#F5A623;transform:scaleX({p_pot/100:.2f})"></span></span><span class="pbr-score">{p_pot}</span></div>
      </div>
    </div>

    <p>The strongest pillar is the one the model leans on hardest when deciding whether {sym} enters Top Hunts. The weakest is the one we'll watch for warning signs as the thesis plays out.</p>

    <div class="pullquote">"A high Alpha Score isn't a recommendation. It's a starting point — the rest of the work is reading <em>why</em> the score is what it is."</div>

    <h2>What we're <em>watching</em>.</h2>

    <p>Every pick that enters our Top Hunts list comes with an entry and a stair-stepped trailing stop that locks in profit as the move proves itself while letting winners run — every exit recorded to a public ledger the moment it fires. For {sym}, the next two earnings cycles + any major analyst-revision day will move the score most. We'll surface meaningful changes in the daily morning brief.</p>

    <p>For real-time score updates and the full data view, <a href="/app">open the dashboard</a>.</p>

    <!-- Mode swap callout -->
    <div class="mode-swap">
      <div class="label">
        <strong>Want the live data view?</strong>
        Flip to Data Mode for the dashboard — same scores, glanceable.
      </div>
      <a class="btn" href="/app">Open Data Mode →</a>
    </div>

    <!-- Related reads -->
    <div class="related">
      <h4>Continue reading</h4>
      <div class="related-grid">
        <a class="related-item" href="/">
          <div class="related-tag">Sector view</div>
          <div class="related-title">{safe_sector}: the names our model still likes right now.</div>
          <div class="related-meta">5 min read · Updated daily</div>
        </a>
        <a class="related-item" href="/#pillars">
          <div class="related-tag">Methodology</div>
          <div class="related-title">How we score the Momentum pillar — and why the 50-day SMA matters.</div>
          <div class="related-meta">6 min read</div>
        </a>
        <a class="related-item" href="/app">
          <div class="related-tag">Today's picks</div>
          <div class="related-title">Today's Hot List — every name scoring 75+ across four strong pillars.</div>
          <div class="related-meta">Live · refreshes every 5 min</div>
        </a>
      </div>
    </div>
  </div>
</article>

<footer>
  TickerMover is a research and tracking tool. Nothing on this page is investment advice.
  Past performance does not predict future results. Verify all data independently.
</footer>

</body>
</html>"""


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
<title>{sym} — Not in TickerMover's universe yet | TickerMover</title>
<meta name="description" content="{sym} isn't in our 200+ stock research universe yet. TickerMover covers high-quality US stocks with $500M+ market cap — get the Hot List free.">
<meta name="robots" content="noindex,follow">
<link rel="canonical" href="{SITE_ORIGIN}/stocks/{sym}">
<style>body{{font-family:system-ui,-apple-system,sans-serif;max-width:600px;margin:80px auto;padding:0 24px;color:#0a0a0a;text-align:center}}
a{{color:#15803d;font-weight:600;text-decoration:none}}</style>
</head><body>
<h1>{sym}</h1>
<p>This ticker isn't in TickerMover's universe yet. We focus on ~200 hand-curated US stocks with $500M+ market cap.</p>
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
            '<span style="background:#FFF3D9;color:#15803d;font-weight:700;'
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
        "linear-gradient(135deg,#FFF8E5 0%,#FFF8E5 100%)" if just_reported
        else "linear-gradient(135deg,#f8fafc 0%,#f1f5f9 100%)"
    )
    border_color = "#FFE9B0" if just_reported else "#e2e8f0"
    return f"""
  <div style="background:{bg_grad};
              border:1px solid {border_color};border-radius:12px;
              padding:18px 22px;margin-bottom:24px;
              box-shadow:0 1px 3px rgba(10,10,10,.04)"
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
                           rx.score >=  0.10 ? '#D4860A' :
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
    verdict_color = {"A":"#15803d","B":"#D4860A","C":"#D4860A","D":"#dc2626","F":"#991b1b"}.get(grade, "#475569")
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
    import datetime as _dt
    _today = _dt.date.today().isoformat()
    title = f"{sym} Stock Analysis · Alpha Score {round(pop)} · {rating} | TickerMover"
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
            "name": "TickerMover",
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
        "datePublished": _today,
        "dateModified": _today,
        "image": f"{SITE_ORIGIN}/static/icons/icon-512.png",
        "author": {"@type": "Organization", "name": "TickerMover", "url": SITE_ORIGIN},
        "publisher": {
            "@type": "Organization",
            "name": "TickerMover",
            "logo": {"@type": "ImageObject", "url": f"{SITE_ORIGIN}/static/icons/icon-512.png"},
        },
        "about": {
            "@type": "Corporation",
            "name": name,
            "tickerSymbol": sym,
        },
    }

    # ── FAQ — visible Q&A (targets "is X a buy" / "X alpha score" long-tail) + FAQPage schema ──
    _faqs = [
        (f"What is {sym}'s Alpha Score?",
         f"{name} ({sym}) currently has a TickerMover Alpha Score of {round(pop)}/100, rated {rating}. The Alpha Score is a quantitative composite across six research pillars — momentum, quality, growth, valuation, sentiment and risk. It is research information, not investment advice."),
        (f"Is {name} ({sym}) a buy?",
         f"TickerMover does not give buy or sell recommendations. {sym} scores {round(pop)}/100 ({rating}) on our quantitative research model — a screening signal, not a personal recommendation. Do your own research, and consider an FCA-authorised adviser before investing."),
        (f"How often is {sym}'s score updated?",
         f"{sym}'s Alpha Score and underlying data refresh through US market hours (about every five minutes), using public market and fundamental data."),
    ]
    faq_schema = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": _q,
             "acceptedAnswer": {"@type": "Answer", "text": _a}}
            for _q, _a in _faqs
        ],
    }
    faq_html = "<h2>" + sym + " — frequently asked</h2>" + "".join(
        f'<div class="faq-q"><h3>{_q}</h3><p>{_a}</p></div>' for _q, _a in _faqs
    )

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
            _ssl = _seo.slugify(sub) if sub else ""
            _sector_link = f'<p style="margin-top:10px"><a href="/sectors/{_ssl}">View all {sub} stocks &rarr;</a></p>' if _ssl else ""
            peers_html = f'<h2>Similar stocks in {sub or "this sub-sector"}</h2><div class="peers">{chips}</div>{_sector_link}'
    except Exception:
        pass
    # Always append the FAQ block (renders even when there are no peers)
    peers_html = (peers_html or "") + faq_html

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
<meta property="og:site_name" content="TickerMover">
<meta name="twitter:image" content="{SITE_ORIGIN}/og/{sym}.png">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title}">
<meta name="twitter:description" content="{desc}">

<link rel="icon" type="image/png" sizes="32x32" href="/static/icons/favicon-32.png">
<link rel="icon" type="image/png" sizes="192x192" href="/static/icons/icon-192.png">
<link rel="icon" type="image/png" sizes="512x512" href="/static/icons/icon-512.png">
<link rel="apple-touch-icon" sizes="192x192" href="/static/icons/icon-192.png">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">

<!-- Schema.org structured data — powers Google rich snippets + AI search.
     We emit two: FinancialProduct (the stock entity) + AnalysisNewsArticle
     (the editorial analysis on the page). Google validates each separately. -->
<script type="application/ld+json">{_json.dumps(schema, separators=(',',':'))}</script>
<script type="application/ld+json">{_json.dumps(article_schema, separators=(',',':'))}</script>
<script type="application/ld+json">{_json.dumps(faq_schema, separators=(',',':'))}</script>

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
.verdict-box{{background:#fff;border:1px solid #e2e8f0;border-left:4px solid {verdict_color};border-radius:12px;padding:20px 24px;margin-bottom:28px;box-shadow:0 1px 3px rgba(10,10,10,.04)}}
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
.faq-q{{margin:14px 0}}
.faq-q h3{{font-size:16px;font-weight:700;margin-bottom:4px;color:#0a0a0a}}
.faq-q p{{font-size:14px;color:#334155;line-height:1.6}}
.cta{{margin-top:40px;padding:28px 32px;background:linear-gradient(135deg,#0A0A0A 0%,#1a2e1a 100%);border-radius:16px;text-align:center;color:#fff}}
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
    <svg width="22" height="22" viewBox="0 0 28 28"><defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#D4860A"/><stop offset=".55" stop-color="#F5A623"/><stop offset="1" stop-color="#FFE9B0"/></linearGradient></defs><rect width="28" height="28" rx="7" fill="#0f172a"/><polyline points="4,21 9,13 15,17 20,7 24,12" stroke="url(#lg)" stroke-width="2.3" fill="none" stroke-linecap="round" stroke-linejoin="round"/><circle cx="20" cy="7" r="2.5" fill="#FFE9B0"/></svg>
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
    TickerMover is a research tool, not financial advice. Alpha Score is a composite signal — always do your own research before investing.
    <br>Last updated automatically every 5 minutes during US market hours.
    <br>Questions? <a href="mailto:support@tickermover.com" style="color:#15803d">support@tickermover.com</a>
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

    draw.text((60, 50), "TickerMover", font=f_brand, fill=(255, 255, 255))
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


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Full-page sign-in. Auth is client-side (JWT in localStorage); if a
    session already exists the page redirects itself to /app. The scoring
    wall is hydrated live from /api/hot."""
    try:
        return HTMLResponse(content=LOGIN_HTML.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse(content="<h2>templates/login.html not found</h2>", status_code=500)


def _render_desk(initial_kind: str) -> HTMLResponse:
    """Serve the public desk page, seeding the initial report (pre/post/auto)."""
    try:
        html = DESK_HTML.read_text(encoding="utf-8")
    except FileNotFoundError:
        return HTMLResponse(content="<h2>templates/desk.html not found</h2>", status_code=500)
    html = html.replace("__DESK_INITIAL__", initial_kind, 1)
    return HTMLResponse(content=html)


@app.get("/desk", response_class=HTMLResponse)
async def desk_report():
    """Public pre/post-market report hub. Defaults to whichever edition is most
    relevant right now (post-market after ~16:15 ET, otherwise pre-market)."""
    now = _et_now()
    initial = "post" if (now.weekday() < 5 and (now.hour, now.minute) >= (16, 15)) else "pre"
    return _render_desk(initial)


@app.get("/desk/pre", response_class=HTMLResponse)
async def desk_report_pre():
    return _render_desk("pre")


@app.get("/desk/post", response_class=HTMLResponse)
async def desk_report_post():
    return _render_desk("post")


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
            # The JS checks this and renders immediately — no /api/universe round-trip.
            # Apply the same slim-down rules as /api/universe so the inlined
            # payload doesn't push the SSR HTML over 3 MB at 545 tickers.
            import json as _json
            try:
                from index_constituents import indices_for as _idx_for
                from profile_rules import assign_profile as _assign_profile
            except Exception:
                _idx_for = lambda _s: []
                _assign_profile = lambda _t: ["aggressive"]
            _SKIP = (
                # keep `breakdown` — the Today's-Signals pillars are derived from it
                "news", "insider_detail", "description", "weighted",
                "quarterly_income", "quarterly_cashflow", "operating_cashflow",
            )
            _slim = []
            for t in _universe_data:
                row = {k: v for k, v in t.items() if k not in _SKIP}
                epsq = row.get("eps_quarters")
                if isinstance(epsq, list) and len(epsq) > 4:
                    row["eps_quarters"] = epsq[-4:]
                row["indices"]  = _idx_for(row.get("ticker", ""))
                row["profiles"] = _assign_profile(row)
                _slim.append(row)
            payload = _json.dumps(_clean({
                "tickers":      _slim,
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


@app.get("/v2-preview", response_class=HTMLResponse)
async def dashboard_v2_preview():
    """Static design mock for the proposed v2 redesign. Dummy data only,
    no API calls, no auth. Reachable so the user can click through and
    decide whether to commission the real port."""
    path = BASE_DIR / "templates" / "dashboard_v2_mock.html"
    try:
        return HTMLResponse(content=path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse(content="<h2>v2 mock not found</h2>", status_code=404)


@app.get("/signals-preview", response_class=HTMLResponse)
async def signals_preview():
    """Standalone light redesign of the 'Today's Signals' page (no auth).

    Built for review before porting the look into the real /app dashboard.
    Serves templates/signals_preview.html with the SAME SSR data injection
    as /app, EXCEPT it keeps the per-factor `breakdown`/`weighted` objects
    (those are normally stripped) so the page can render REAL six-pillar
    bars (M·G·Q·V·S·R). If the local universe is cold, the template falls
    back to /api/universe and then to /static/_universe_fixture.json."""
    path = BASE_DIR / "templates" / "signals_preview.html"
    try:
        html = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return HTMLResponse(content="<h2>signals_preview.html not found</h2>", status_code=404)

    if _universe_data:
        import json as _json
        try:
            from index_constituents import indices_for as _idx_for
            from profile_rules import assign_profile as _assign_profile
        except Exception:
            _idx_for = lambda _s: []
            _assign_profile = lambda _t: ["aggressive"]
        # NOTE: unlike /app and /api/universe, we deliberately KEEP `breakdown`
        # and `weighted` here — the light page reads `breakdown` to build the
        # real six-pillar bars. We still drop the genuinely heavy fields the
        # page never touches so the SSR HTML stays a sane size.
        _SKIP = (
            "news", "insider_detail", "description",
            "quarterly_income", "quarterly_cashflow", "operating_cashflow",
        )
        _slim = []
        for t in _universe_data:
            row = {k: v for k, v in t.items() if k not in _SKIP}
            epsq = row.get("eps_quarters")
            if isinstance(epsq, list) and len(epsq) > 4:
                row["eps_quarters"] = epsq[-4:]
            row["indices"] = _idx_for(row.get("ticker", ""))
            row["profiles"] = _assign_profile(row)
            _slim.append(row)
        payload = _json.dumps(_clean({
            "tickers":      _slim,
            "warming_up":   False,
            "last_refresh": _last_full_refresh,
            "hot_list_n":   config.HOT_LIST_N,
            "account_size": config.ACCOUNT_SIZE_USD,
            "regime":       market_regime.get(),
        }), ensure_ascii=False, separators=(",", ":"))
        injection = f'\n<script>window.__AH_DATA__={payload};</script>\n'
        html = html.replace("</head>", injection + "</head>", 1)

    return HTMLResponse(content=html)


# ── API Endpoints ─────────────────────────────────────────────────────

@app.get("/api/universe")
async def api_universe():
    # ── Payload slim-down ────────────────────────────────────────────
    # Audit found 5 heavy fields (~6.6 KB/ticker × 187 tickers = 1.2 MB)
    # that the dashboard never reads. Strip them so the wire payload is
    # ~0.4 MB raw (~75 KB gzipped) instead of ~1.6 MB raw (~170 KB
    # gzipped). Modal still gets full detail from /api/ticker/{symbol}.
    # Phase 2 follow-up: at 545 tickers the wire payload ballooned to
    # ~1.8 MB raw, making /app + /api/universe sluggish on dev. quarterly_income
    # and quarterly_cashflow are full statement objects only the per-stock
    # modal needs (and the modal re-fetches via /api/ticker/{symbol} anyway).
    # eps_quarters is universe-relevant (Recent Earnings widget) but we only
    # need the last 4 quarters there — trim history to keep payload sane.
    _SKIP = (
        # keep `breakdown` — Today's-Signals pillars are derived from it
        "news", "insider_detail", "description", "weighted",
        "quarterly_income", "quarterly_cashflow",     # full statements -> /api/ticker only
        "operating_cashflow",                          # series — universe uses only latest
    )
    # Phase 1: enrich each ticker with index membership + profile eligibility
    # so the frontend can filter Conservative/Balanced/Aggressive deterministically.
    try:
        from index_constituents import indices_for as _idx_for
        from profile_rules import assign_profile as _assign_profile
    except Exception:
        _idx_for = lambda _s: []
        _assign_profile = lambda _t: ["aggressive"]
    slim = []
    for t in _universe_data:
        row = {k: v for k, v in t.items() if k not in _SKIP}
        # Trim eps_quarters to the last 4 — Recent Earnings widget + modal
        # status pills only ever consume the most recent few.
        epsq = row.get("eps_quarters")
        if isinstance(epsq, list) and len(epsq) > 4:
            # eps_quarters is oldest-first on the backend; keep the last 4.
            row["eps_quarters"] = epsq[-4:]
        row["indices"]  = _idx_for(row.get("ticker", ""))
        row["profiles"] = _assign_profile(row)
        slim.append(row)
    # Attach the Opus analyst-judge's one-line thesis (cached, ~nightly) to the
    # names that have one — the dashboard surfaces it as a real, specific "why
    # it's here" in place of the templated reason. Only the qualified pool is
    # judged, so most rows fall back cleanly to the rule-based signal.
    try:
        _cmap = _conviction_map([r.get("ticker", "") for r in slim])
        if _cmap:
            for r in slim:
                _aj = _cmap.get((r.get("ticker") or "").upper())
                if _aj and (_aj.get("thesis") or "").strip():
                    row_thesis = _aj["thesis"].strip()
                    r["ai_thesis"] = row_thesis
                    if _aj.get("lean"):
                        r["ai_lean"] = _aj["lean"]
    except Exception as _e:
        logger.debug(f"ai_thesis attach skipped: {_e}")
    # ── CDN caching ──────────────────────────────────────────────────
    # Universe refreshes every 5 min server-side, so a 30-second public
    # cache at the Cloudflare edge dramatically reduces origin load
    # without serving stale data. `s-maxage` is CDN-only, `max-age` is
    # browser-only, kept small so each tab refresh still gets fresh data.
    # Coverage summary — how many of each index we currently track,
    # plus how many tickers qualify for each profile in this snapshot.
    try:
        from index_constituents import coverage_summary as _coverage
    except Exception:
        _coverage = lambda _l: {}
    _tracked_syms = [r.get("ticker", "") for r in slim]
    _profile_counts = {"aggressive": 0, "balanced": 0, "conservative": 0}
    for r in slim:
        for p in r.get("profiles", []):
            if p in _profile_counts:
                _profile_counts[p] += 1
    return JSONResponse(
        _clean({
            "tickers":        slim,
            "last_refresh":   _last_full_refresh,
            "universe_mode":  config.UNIVERSE_MODE,
            "account_size":   config.ACCOUNT_SIZE_USD,
            "hot_list_n":     config.HOT_LIST_N,
            "min_confidence": config.MIN_CONFIDENCE,
            "warming_up":     len(_universe_data) == 0,
            "regime":         market_regime.get(),
            "index_coverage": _coverage(_tracked_syms),
            "profile_counts": _profile_counts,
        }),
        headers={
            # Cache extended (was max-age=15, s-maxage=30) — at 545 tickers
            # the slow tech_refresh loop takes ~5 min to iterate everyone,
            # so the previous 30s CDN cache let users see partial-update
            # state every 30s for 5 minutes ('score taking time to refresh,
            # 5 min to stabilise' user feedback). Now: CDN serves the same
            # snapshot for 2 min before re-checking origin, browser cache
            # 60s. Scores stabilize visually instead of churning.
            "Cache-Control": "public, max-age=60, s-maxage=120",
            "Vary":          "Accept-Encoding",
        },
    )


@app.get("/api/regime")
async def api_regime():
    """Current macro regime snapshot (cached) — SPY/QQQ/VIX/^TNX overlay."""
    return JSONResponse(
        _clean(market_regime.get()),
        # Regime is refreshed every 15 min server-side. Cache aggressively
        # at CDN edge so tabs that all check regime on load are basically free.
        headers={"Cache-Control": "public, max-age=60, s-maxage=120"},
    )


@app.post("/api/regime/refresh")
async def api_regime_refresh():
    """Force a regime refresh (admin / debug)."""
    payload = await market_regime.refresh()
    return JSONResponse(_clean(payload))


def _market_movers() -> dict:
    """Stock-level detail for the Market Analysis report, computed live from the
    in-memory universe (always current — not tied to the 5-min macro cache)."""
    uni = _universe_data or []

    def fnum(t, k):
        try:
            v = float(t.get(k))
            return v if v == v else None   # drop NaN
        except (TypeError, ValueError):
            return None

    def from_high(t):
        """Clean % distance from the 52-week high (0 = at high, negative = below)
        computed straight from price/high_52w so it never depends on a possibly
        stale precomputed field."""
        p, h = fnum(t, "price"), fnum(t, "high_52w")
        return round((p / h - 1) * 100, 2) if (p and h and h > 0) else None

    def slim(t):
        return {
            "ticker":     t.get("ticker"),
            "name":       (t.get("name") or "")[:34],
            "price":      fnum(t, "price"),
            "change_pct": fnum(t, "change_pct"),
            "grade":      t.get("grade"),
            "score":      fnum(t, "smart_score") or fnum(t, "pop_score"),
            "sector":     t.get("sub_sector") or t.get("subsector") or t.get("sector"),
            "dte":        fnum(t, "days_to_earnings"),
            "vol_ratio":  fnum(t, "volume_ratio"),
            "from_high":  from_high(t),
            "upside":     fnum(t, "target_upside_pct"),
        }

    have_chg = [t for t in uni if fnum(t, "price") and fnum(t, "change_pct") is not None]
    gainers  = sorted(have_chg, key=lambda t: fnum(t, "change_pct"), reverse=True)[:8]
    losers   = sorted(have_chg, key=lambda t: fnum(t, "change_pct"))[:8]

    scored    = [t for t in uni if fnum(t, "smart_score") is not None]
    top_score = sorted(scored, key=lambda t: fnum(t, "smart_score") or 0, reverse=True)[:8]

    earnings = sorted(
        [t for t in uni if fnum(t, "days_to_earnings") is not None
         and 0 <= fnum(t, "days_to_earnings") <= 7],
        key=lambda t: fnum(t, "days_to_earnings"),
    )[:10]
    just_reported = [t for t in uni if t.get("earnings_just_reported")][:10]

    unusual = sorted(
        [t for t in uni if fnum(t, "volume_ratio") is not None and fnum(t, "volume_ratio") >= 1.5],
        key=lambda t: fnum(t, "volume_ratio"), reverse=True,
    )[:8]
    # Within 3% below the high, up to 2% above (genuine fresh highs) — the upper
    # cap drops names whose stored 52-week high is stale after a big gap.
    near_high = sorted(
        [t for t in uni if from_high(t) is not None and -3.0 <= from_high(t) <= 2.0],
        key=from_high, reverse=True,
    )[:8]

    return {
        "count":          len(uni),
        "gainers":        [slim(t) for t in gainers],
        "losers":         [slim(t) for t in losers],
        "top_score":      [slim(t) for t in top_score],
        "earnings":       [slim(t) for t in earnings],
        "just_reported":  [slim(t) for t in just_reported],
        "unusual_volume": [slim(t) for t in unusual],
        "near_high":      [slim(t) for t in near_high],
    }


@app.get("/api/market-analysis")
async def api_market_analysis():
    """US pre/post-market report feed for the Market Analysis panel + /desk page.

    Macro half (indices, equity futures, rates/VIX/dollar, commodities, sector
    rotation, SPY technicals, AI narrative) comes from a 5-min cache. The
    stock-level 'stocks' half is layered on live from the in-memory universe so
    movers stay current with the 30-second quote loop.
    """
    data = market_analysis.get()
    if not data or not data.get("available"):
        data = await market_analysis.refresh()
    if isinstance(data, dict) and data.get("available"):
        data = dict(data)                  # shallow copy — never mutate the cache
        data["stocks"] = _market_movers()
        data["events"] = await _key_events()
    return JSONResponse(
        _clean(data),
        headers={"Cache-Control": "public, max-age=60, s-maxage=90"},
    )


# ══════════════════════════════════════════════════════════════════════════
#  Desk reports — two dated editions (pre-market & post-market) that publish
#  on a schedule and otherwise serve the previous (yesterday's) edition.
# ══════════════════════════════════════════════════════════════════════════
# ET publish times: the pre-market edition freezes at 04:00 ET (pre-market
# open) so it's live all morning; the post-market edition freezes at 16:15 ET
# (just after the 4:00 close).
_DESK_PUBLISH = {"pre": (4, 0), "post": (16, 15)}
_DESK_TTL     = 60 * 60 * 30        # 30h — a daily edition outlives the gap to the next

# Major economies whose data the US market actually reacts to.
# Nasdaq country label -> (display label, flag, scope)
_EVENT_COUNTRIES = {
    "United States":  ("United States",  "🇺🇸", "usa"),
    "Euro Zone":      ("Euro Area",      "🇪🇺", "global"),
    "European Union": ("Euro Area",      "🇪🇺", "global"),
    "China":          ("China",          "🇨🇳", "global"),
    "Japan":          ("Japan",          "🇯🇵", "global"),
    "United Kingdom": ("United Kingdom", "🇬🇧", "global"),
    "Germany":        ("Germany",        "🇩🇪", "global"),
}
# Event-name keywords that make a release genuinely market-moving.
_EVENT_HIGH_KW = (
    "nonfarm payroll", "cpi", "core pce", "pce price", "ppi", "gdp",
    "interest rate decision", "fed funds", "fomc statement", "rate decision",
    "retail sales", "unemployment rate", "ism ",
)
_EVENT_MED_KW = (
    "pmi", "jobless claims", "consumer confidence", "consumer sentiment",
    "durable goods", "trade balance", "factory orders", "housing starts",
    "building permits", "michigan", "jolts", "adp employment change",
)
# Central-bank-speaker appearances — relevant but secondary to data releases.
_EVENT_SPEAK_KW = ("speaks", "speech", "testimony", "press conf")
# Sub-component / weekly noise we never want crowding the headline releases.
_EVENT_SKIP_KW = (
    "private nonfarm", "government payrolls", "manufacturing payrolls",
    "weekly", "n.s.a", "adjusted", "revised",
)


def _et_now():
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        return _dt.now(ZoneInfo("America/New_York"))
    except Exception:
        from datetime import datetime as _dt, timezone as _tz
        return _dt.now(_tz.utc)


def _prev_weekday(d):
    from datetime import timedelta
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _clock(h, m):
    """12-hour ET clock label, e.g. (9,0) -> '9:00 AM ET'."""
    ap = "AM" if h < 12 else "PM"
    return f"{(h % 12) or 12}:{m:02d} {ap} ET"


def _next_publish_date(kind: str, now=None):
    """Trading date of the NEXT edition of `kind` after the one showing now."""
    from datetime import timedelta
    now = now or _et_now()
    ed, today = _edition_date_for(kind, now), now.date()
    d = today + timedelta(days=1) if ed == today else today
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _event_when_et(d, gmt: str):
    """Nasdaq's calendar lists the date + an Eastern-time clock (despite the
    'gmt' field name — e.g. 08:30 is the 8:30 AM ET jobs-report slot). Return
    (label, epoch, all_day)."""
    from datetime import datetime
    g = (gmt or "").strip()
    try:
        hh, mm = g.split(":")
        hh, mm = int(hh), int(mm)
        ts = datetime(d.year, d.month, d.day, hh, mm).timestamp()
        ap = "AM" if hh < 12 else "PM"
        return (f"{d.strftime('%a %d %b')} · {(hh % 12) or 12}:{mm:02d} {ap} ET", ts, False)
    except Exception:
        ts = datetime(d.year, d.month, d.day).timestamp()
        return (d.strftime("%a %d %b"), ts, True)


def _clean_econ_val(v):
    v = (v or "").replace("&nbsp;", "").strip()
    return v or None


async def _key_events() -> list:
    """Upcoming high/medium-impact macro events (Fed, CPI, jobs, ECB, …) for the
    next week — 'what the market is waiting for'. Sourced from Nasdaq's free
    economic calendar, filtered to the economies the US tape reacts to and to
    genuinely market-moving releases. Cached ~45 min."""
    cached = cache.get("desk:events:v4")
    if cached is not None:
        return cached
    from datetime import timedelta
    today = _et_now().date()
    days = [today + timedelta(days=i) for i in range(0, 7)]
    hdrs = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124 Safari/537.36"),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }

    async def _fetch_day(client, d):
        try:
            r = await client.get(
                f"https://api.nasdaq.com/api/calendar/economicevents?date={d.isoformat()}",
                headers=hdrs)
            if r.status_code != 200:
                return d, []
            return d, ((r.json().get("data") or {}).get("rows") or [])
        except Exception:
            return d, []

    try:
        import httpx
        async with httpx.AsyncClient(timeout=12) as client:
            results = await asyncio.gather(*[_fetch_day(client, d) for d in days])
    except Exception as exc:
        logger.warning(f"key-events fetch failed: {exc}")
        cache.set("desk:events:v4", [], 600)
        return []

    now_ts, seen = _et_now().timestamp(), {}
    for d, rows in results:
        for x in rows or []:
            meta = _EVENT_COUNTRIES.get((x.get("country") or "").strip())
            if not meta:
                continue
            name = (x.get("eventName") or "").strip()
            low = name.lower()
            if any(k in low for k in _EVENT_SKIP_KW):
                continue
            base = next((k for k in _EVENT_SPEAK_KW if k in low), None)
            impact = "Medium" if base else None
            if not base:
                base = next((k for k in _EVENT_HIGH_KW if k in low), None)
                if base:
                    impact = "High"
            if not base:
                base = next((k for k in _EVENT_MED_KW if k in low), None)
                if base:
                    impact = "Medium"
            if not base:
                continue
            if meta[2] == "global" and impact != "High":
                continue                                   # foreign: high impact only
            when_label, when_ts, all_day = _event_when_et(d, x.get("gmt"))
            if when_ts and when_ts < now_ts - 3600:        # drop already-passed
                continue
            # Collapse sub-components (GDP / GDP Annualized / GDP Price Index …)
            # to the single headline release per country + topic + day.
            day_key = (meta[0], base, d.isoformat())
            cand = {
                "event":    name, "country": meta[0], "flag": meta[1], "scope": meta[2],
                "impact":   impact, "when": when_label, "when_ts": when_ts, "all_day": all_day,
                "estimate": _clean_econ_val(x.get("consensus")),
                "previous": _clean_econ_val(x.get("previous")),
            }
            prev = seen.get(day_key)
            if prev is None or len(name) < len(prev["event"]):
                seen[day_key] = cand            # keep the shortest = headline name

    out = list(seen.values())
    out.sort(key=lambda e: (e["when_ts"] or 0))
    high = [e for e in out if e["impact"] == "High"][:8]
    med  = [e for e in out if e["impact"] == "Medium"]
    picked = (high + med)[:12]
    picked.sort(key=lambda e: (e["when_ts"] or 0))
    cache.set("desk:events:v4", picked, 2700)                 # 45 min
    return picked


def _edition_date_for(kind: str, now=None):
    """The trading date the *current* edition of `kind` represents. Today once
    we're past today's publish time on a weekday; otherwise the prior weekday."""
    now = now or _et_now()
    h, m = _DESK_PUBLISH.get(kind, (9, 0))
    today = now.date()
    if today.weekday() < 5 and (now.hour, now.minute) >= (h, m):
        return today                       # today's edition is out
    if today.weekday() >= 5:               # weekend → roll back to Friday
        d = today
        while d.weekday() >= 5:
            from datetime import timedelta
            d -= timedelta(days=1)
        return d
    return _prev_weekday(today)            # weekday but before publish → yesterday


def _desk_key(kind: str) -> str:
    return f"desk:report:v3:{kind}"


async def _build_desk_report(kind: str, edition_date) -> dict:
    """Assemble a full report payload (macro + stocks + kind-framed AI) and
    stamp it with the edition date it represents."""
    macro = market_analysis.get()
    if not macro or not macro.get("available"):
        macro = await market_analysis.refresh()
    data = dict(macro) if isinstance(macro, dict) else {"available": False}
    data["kind"]   = kind
    data["stocks"] = _market_movers()
    data["events"] = await _key_events()
    try:
        data["ai"] = await market_analysis.ai_narrative(data, kind)
    except Exception as exc:
        logger.warning(f"desk {kind} AI narrative failed: {exc}")
        data["ai"] = None
    now = _et_now()
    h, m = _DESK_PUBLISH.get(kind, (9, 0))
    nxt = _next_publish_date(kind, now)
    next_when = "today" if nxt == now.date() else nxt.strftime("%a %d %b")
    data["edition"] = {
        "kind":         kind,
        "date":         edition_date.isoformat(),
        "date_label":   edition_date.strftime("%a %d %b %Y"),
        "published_at": _clock(h, m),                 # scheduled drop time, not build time
        "is_today":     edition_date == now.date(),
        "next_label":   f"{next_when} {_clock(h, m)}",
        "title":        "Pre-Market Report" if kind == "pre" else "Post-Market Report",
    }
    return data


def _desk_is_current(doc, ed) -> bool:
    return bool(doc and isinstance(doc, dict)
                and doc.get("edition", {}).get("date") == ed.isoformat())


async def _publish_desk(kind: str, force: bool = False) -> dict:
    """Return the current edition for `kind`, freezing a fresh snapshot when the
    stored edition is stale (i.e., a new edition has just come due).

    Durability: editions are persisted in Supabase (desk_store) so a Railway
    redeploy reuses the current edition instead of paying to regenerate the AI
    narrative on every build. L1 = in-memory cache, L2 = desk_store."""
    from desk_store import store as _dstore
    ed  = _edition_date_for(kind)
    key = _desk_key(kind)
    cur = cache.get(key)
    if not force and _desk_is_current(cur, ed):
        return cur
    # L1 missed (e.g. just redeployed) — try the durable store before paying for AI.
    if not force:
        durable = _dstore.get(kind)
        if _desk_is_current(durable, ed):
            cache.set(key, durable, _DESK_TTL)    # re-warm L1
            return durable
    report = await _build_desk_report(kind, ed)
    cache.set(key, report, _DESK_TTL)
    try:
        _dstore.save(kind, report)                # persist so the next deploy reuses it
    except Exception as exc:
        logger.warning(f"desk {kind} durable save failed: {exc}")
    logger.info("🗞️  Published %s desk edition for %s", kind, ed.isoformat())
    return report


async def _desk_publisher() -> None:
    """Background heartbeat — re-publishes each edition as soon as its publish
    time rolls past, so snapshots freeze near 09:00 / 16:15 ET rather than at a
    random first-visit time. Idempotent: skips when the edition is already current."""
    while True:
        for kind in ("pre", "post"):
            try:
                await _publish_desk(kind)
            except Exception as exc:
                logger.warning(f"desk publisher ({kind}) failed: {exc}")
        await asyncio.sleep(600)            # 10 min


@app.get("/api/desk-report")
async def api_desk_report(type: str = "pre"):
    """Current pre- or post-market edition. Serves the frozen snapshot for the
    edition that's due now; before today's publish time that's yesterday's."""
    kind = "post" if str(type).startswith("post") else "pre"
    report = await _publish_desk(kind)
    return JSONResponse(
        _clean(report),
        headers={"Cache-Control": "public, max-age=60, s-maxage=90"},
    )


@app.get("/api/pdf/{symbol}")
async def api_pdf(symbol: str, debug: int = 0):
    """Server-rendered tear sheet PDF — one HTTP call returns the bytes.

    Replaces the brittle client-side html2canvas + iframe approach.
    Pulls live data from in-memory universe, fetches cached price-history,
    runs pdf_render.generate_pdf() → returns application/pdf.

    Pass ?debug=1 to get a JSON dump instead of the PDF — shows what
    each layer of the chart fallback chain returned and what ticker
    fields are populated. Use this when the PDF chart says 'Price
    history unavailable' or the Ownership cells are empty.

    No client-side waiting, no separate URL ever surfaces."""
    from fastapi.responses import Response
    import pdf_render as _pdf
    sym = (symbol or "").upper().strip()
    if not sym or len(sym) > 8:
        raise HTTPException(status_code=400, detail="Bad ticker")
    # Find ticker in the loaded universe; fall back to on-demand build
    target = next((t for t in _universe_data if t.get("ticker") == sym), None)
    if not target:
        try:
            social_map = cache.get("ape:all") or {}
            target = await coordinator.get_full_ticker(sym, get_meta(sym), social_map)
            if target:
                attach_score_history([target], score_history)
                target.update(compute_pop_score(target, regime=market_regime.get()))
        except Exception as exc:
            logger.error(f"pdf build {sym}: on-demand fetch failed: {exc}")
    if not target:
        raise HTTPException(status_code=404, detail=f"Ticker {sym} not found in universe")

    # ── Diagnostic short-circuit ───────────────────────────────────────
    # When debug=1, capture what each layer of the chart fallback chain
    # returns and dump as JSON. Same code path as the real run but with
    # tracing — lets us see live which tier is failing for a given
    # ticker without grepping server logs.
    diag = {"ticker": sym, "layers": []} if debug else None

    # Pull cached price history (6h TTL, populated by tracker-chart). On a
    # miss, fetch directly so the PDF never renders without a chart.
    pts = []
    ck = f"price-history:{sym}:3mo"
    c = cache.get(ck)
    if c and c.get("points"):
        pts = c["points"]
        if diag is not None:
            diag["layers"].append({"layer": "cache", "points": len(pts), "status": "hit"})
    if not pts:
        # 3-layer fallback to dodge yfinance per-ticker rate limits
        def _yf_fetch_sync():
            out = []
            try:
                tk = yf.Ticker(sym)
                h = tk.history(period="3mo", interval="1d", auto_adjust=True)
                if h is not None and not h.empty:
                    for idx, row in h.iterrows():
                        d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
                        close = float(row["Close"]) if row.get("Close") is not None else None
                        if close is not None and not (math.isnan(close) or math.isinf(close)):
                            out.append({"date": d, "close": round(close, 2)})
            except Exception as exc:
                logger.warning(f"pdf build {sym}: Ticker.history failed: {exc}")
            if not out:
                try:
                    df = yf.download(sym, period="3mo", interval="1d",
                                     auto_adjust=True, progress=False, threads=False)
                    if df is not None and not df.empty:
                        closes = df["Close"] if "Close" in df.columns else df.get("Adj Close")
                        if closes is not None:
                            for idx, v in closes.items():
                                try:
                                    d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
                                    vf = float(v)
                                    if not (math.isnan(vf) or math.isinf(vf)):
                                        out.append({"date": d, "close": round(vf, 2)})
                                except (TypeError, ValueError):
                                    continue
                except Exception as exc:
                    logger.warning(f"pdf build {sym}: yf.download failed: {exc}")
            return out
        pts = await asyncio.to_thread(_yf_fetch_sync)
        logger.info(f"pdf build {sym}: yfinance returned {len(pts)} points")
        if diag is not None:
            diag["layers"].append({"layer": "yfinance", "points": len(pts)})

        # FMP fallback when yfinance is blocked/rate-limited. The old
        # /api/v3/historical-price-full path is now legacy-only (returns
        # "Legacy Endpoint" for new keys), so we go straight to the stable
        # endpoint.
        if not pts and config.FMP_API_KEY:
            try:
                try: await coordinator._fmp_limiter.wait()   # respect the 280/min FMP budget
                except Exception: pass
                async with httpx.AsyncClient(timeout=10) as _c:
                    stable_url = (
                        "https://financialmodelingprep.com/stable/historical-price-eod/light"
                        f"?symbol={sym}&apikey={config.FMP_API_KEY}"
                    )
                    r = await _c.get(stable_url)
                    if r.status_code == 200:
                        data = r.json() or []
                        if isinstance(data, list):
                            # newest first → ascending; take last 90
                            data_sorted = sorted(data, key=lambda h: h.get("date", ""))
                            pts = [
                                {"date": h.get("date"), "close": round(float(h.get("price") or h.get("close")), 2)}
                                for h in data_sorted
                                if (h.get("price") is not None or h.get("close") is not None) and h.get("date")
                            ][-90:]
                            logger.info(f"pdf build {sym}: FMP-stable returned {len(pts)} points")
                    else:
                        logger.warning(f"pdf build {sym}: FMP-stable HTTP {r.status_code}: {r.text[:200]}")
            except Exception as exc:
                logger.warning(f"pdf build {sym}: FMP-stable failed: {exc}")

        # 5b-layer fallback: Yahoo Query v8 direct (bypasses yfinance pkg).
        # If yfinance is blocked from Render but direct HTTP works (it
        # sometimes does — yf adds extra headers Yahoo dislikes), this
        # picks up the slack. Truly free, no key.
        if not pts:
            try:
                async with httpx.AsyncClient(timeout=10) as _c:
                    yq_url = (
                        f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
                        "?range=3mo&interval=1d"
                    )
                    r = await _c.get(yq_url, headers={
                        "User-Agent": "Mozilla/5.0 (compatible; TickerMover/1.0)",
                    })
                    if r.status_code == 200:
                        data = r.json() or {}
                        result = (data.get("chart") or {}).get("result") or []
                        if result:
                            ts_ms   = result[0].get("timestamp") or []
                            indic   = result[0].get("indicators") or {}
                            closes  = ((indic.get("adjclose") or [{}])[0].get("adjclose")
                                        or (indic.get("quote") or [{}])[0].get("close")
                                        or [])
                            from datetime import datetime as _dt
                            for ts, cl in zip(ts_ms, closes):
                                if ts is None or cl is None: continue
                                try:
                                    d = _dt.utcfromtimestamp(ts).strftime("%Y-%m-%d")
                                    pts.append({"date": d, "close": round(float(cl), 2)})
                                except (TypeError, ValueError):
                                    continue
                            logger.info(f"pdf build {sym}: Yahoo-v8 returned {len(pts)} points")
                    else:
                        logger.warning(f"pdf build {sym}: Yahoo-v8 HTTP {r.status_code}")
            except Exception as exc:
                logger.warning(f"pdf build {sym}: Yahoo-v8 failed: {exc}")

        # 6th-layer fallback: Alpha Vantage TIME_SERIES_DAILY using the key
        # we already have for Briefings. Free tier 25 calls/day total — so
        # only triggers on the rare ticker where every other source failed.
        # Shares the 25/day pool with fundamentals + transcripts via av_budget.
        import av_budget as _avb
        if not pts and config.ALPHA_VANTAGE_KEY and _avb.try_spend(1):
            try:
                async with httpx.AsyncClient(timeout=15) as _c:
                    av_url = (
                        "https://www.alphavantage.co/query?function=TIME_SERIES_DAILY"
                        f"&symbol={sym}&outputsize=compact&apikey={config.ALPHA_VANTAGE_KEY}"
                    )
                    r = await _c.get(av_url)
                    if r.status_code == 200:
                        data = r.json() or {}
                        ts = data.get("Time Series (Daily)") or {}
                        if ts:
                            items = sorted(ts.items())   # ascending by date
                            pts = [
                                {"date": d, "close": round(float(v.get("4. close") or 0), 2)}
                                for d, v in items if v.get("4. close")
                            ][-90:]
                            logger.info(f"pdf build {sym}: AV fallback returned {len(pts)} points")
                        elif data.get("Information"):
                            logger.warning(f"pdf build {sym}: AV rate-limited: {data['Information'][:120]}")
            except Exception as exc:
                logger.warning(f"pdf build {sym}: AV fallback failed: {exc}")

        if pts:
            cache.set(ck, {"ticker": sym, "period": "3mo", "points": pts}, 60 * 60 * 6)

    # ── Lazy ownership fetch (insider/institutional %, avg volume) ────
    # Uses FMP /profile (proven to work from this IP — already used by
    # the rest of the app) rather than yfinance.info (yf is blocked from
    # Render and was leaving these cells empty in v3.3). Cached 24h.
    def _fmt_avg_volume(v):
        try: v = float(v)
        except (TypeError, ValueError): return None
        if v >= 1e9: return f"{v/1e9:.2f}B"
        if v >= 1e6: return f"{v/1e6:.2f}M"
        if v >= 1e3: return f"{v/1e3:.0f}K"
        return f"{v:.0f}"

    own_ck = f"ownership-info:{sym}"
    own_cached = cache.get(own_ck)
    if not own_cached and config.FMP_API_KEY:
        try:
            try: await coordinator._fmp_limiter.wait()   # respect the 280/min FMP budget
            except Exception: pass
            async with httpx.AsyncClient(timeout=10) as _c:
                prof_url = f"https://financialmodelingprep.com/stable/profile?symbol={sym}&apikey={config.FMP_API_KEY}"
                r = await _c.get(prof_url)
                if r.status_code == 200:
                    arr = r.json() or []
                    prof = arr[0] if isinstance(arr, list) and arr else {}
                    # stable profile renamed volAvg→averageVolume, lastDiv→lastDividend.
                    # Ownership %s aren't in profile (stay None — same as before).
                    own_cached = {
                        "insider_pct":        prof.get("insiderOwnership"),
                        "institutional_pct":  prof.get("institutionalOwnership"),
                        "avg_volume_str":     _fmt_avg_volume(prof.get("averageVolume") or prof.get("volAvg")),
                        "div_yield":          prof.get("lastDividend") or prof.get("lastDiv"),
                    }
                    cache.set(own_ck, own_cached, 24 * 60 * 60)
                    logger.info(f"pdf build {sym}: FMP profile populated ownership fields")
                else:
                    logger.warning(f"pdf build {sym}: FMP profile HTTP {r.status_code}")
        except Exception as exc:
            logger.warning(f"pdf build {sym}: FMP profile failed: {exc}")
    if own_cached:
        target.setdefault("insider_ownership",      own_cached.get("insider_pct"))
        target.setdefault("institutional_ownership", own_cached.get("institutional_pct"))
        target.setdefault("avg_volume_str",          own_cached.get("avg_volume_str"))
        target.setdefault("dividend_yield",          own_cached.get("div_yield"))

    # ── AI narrative + event summary for page 2 ──────────────────────
    # Run in parallel. Both have their own short-circuit caches (event
    # summary in Supabase, narrative in-process), so warm tickers add
    # almost nothing to the latency. Cold tickers add ~3-6s for the
    # Haiku synth on top of whatever event_intel needs (EDGAR is fast).
    import event_intel as _ei_mod
    import pdf_narrative as _nar_mod

    async def _safe_event():
        try:
            return await _ei_mod.get_event_summary(sym)
        except Exception as exc:
            logger.warning(f"pdf build {sym}: event_summary failed: {exc}")
            return None

    event_row = await _safe_event()
    # Don't treat rate-limited stub as a real event row
    if isinstance(event_row, dict) and event_row.get("error") == "rate_limited":
        event_row = None

    try:
        narrative = await _nar_mod.build_narrative(sym, target, event_row)
    except Exception as exc:
        logger.warning(f"pdf build {sym}: narrative failed: {exc}")
        narrative = None

    # Diagnostic dump — return JSON instead of PDF when debug=1
    if diag is not None:
        # Layer marker for completion + ticker field audit
        diag["final_points"] = len(pts)
        diag["target_fields"] = {
            k: target.get(k) for k in (
                "ticker", "name", "price", "last_close", "market_cap",
                "rev_growth_yoy", "rev_growth_qyoy", "gross_margin",
                "fcf_margin", "pe_ttm", "ps_ttm", "peg_ratio",
                "high_52w", "low_52w",
                "target_mean", "target_low", "target_high", "total_analysts",
                "insider_ownership", "institutional_ownership",
                "avg_volume_str", "dividend_yield",
                "strong_buy_pct",
            )
        }
        diag["quarterly_income_count"] = len(target.get("quarterly_income") or [])
        diag["eps_quarters_count"]      = len(target.get("eps_quarters") or [])
        diag["weighted_keys"]            = list((target.get("weighted") or {}).keys())
        return JSONResponse(diag)

    # Build peer set — same-sector tickers (sub_sector preferred), excluding
    # ourselves, sorted by smart_score desc. Top 3. Pulled straight from the
    # in-memory universe so it's free and instant.
    peers = []
    try:
        target_sub  = (target.get("sub_sector") or "").strip().lower()
        target_sect = (target.get("sector") or "").strip().lower()
        cand = []
        for other in _universe_data:
            if other.get("ticker") == sym:
                continue
            o_sub  = (other.get("sub_sector") or "").strip().lower()
            o_sect = (other.get("sector") or "").strip().lower()
            if target_sub and o_sub == target_sub:
                cand.append((2, other))   # exact sub-sector match — strongest peer
            elif target_sect and o_sect == target_sect:
                cand.append((1, other))   # same broader sector — okay peer
        cand.sort(key=lambda r: (r[0], r[1].get("smart_score") or r[1].get("pop_score") or 0),
                  reverse=True)
        peers = [r[1] for r in cand[:3]]
    except Exception as exc:
        logger.warning(f"pdf build {sym}: peer build failed: {exc}")

    # Render PDF (pure compute, no I/O — runs in a thread to keep event loop free)
    try:
        pdf_bytes = await asyncio.to_thread(
            _pdf.generate_pdf, sym, target, pts, narrative, event_row, peers,
        )
    except Exception as exc:
        logger.error(f"pdf build {sym}: render failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"PDF render failed: {exc}")

    from datetime import date as _date
    filename = f"alphahunt-tearsheet-{sym}-{_date.today().isoformat()}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control":       "private, max-age=300",
        },
    )


@app.get("/api/event-intel/{symbol}")
async def api_event_intel(symbol: str, refresh: int = 0):
    """Quartr-style structured summary of the latest earnings call.

    Returns: {ticker, event_title, event_date, source, key_updates[],
    operations[], outlook[], risks[], raw_excerpt, summarized_at}.

    Lazy-fetches from Alpha Vantage + summarizes via Anthropic Haiku when
    cache is missing or stale (>14d). Pass ?refresh=1 to force a re-fetch
    even when cache is fresh."""
    import event_intel as _ei_mod
    sym = symbol.upper().strip()
    if not sym or len(sym) > 8:
        raise HTTPException(status_code=400, detail="Bad ticker")
    try:
        summary = await _ei_mod.get_event_summary(sym, force_refresh=bool(refresh))
    except Exception as exc:
        logger.error(f"event-intel {sym}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))
    if not summary:
        return JSONResponse({"ticker": sym, "available": False})
    # Rate-limit signal passes through with a structured reason
    if isinstance(summary, dict) and summary.get("error") == "rate_limited":
        return JSONResponse({"ticker": sym, "available": False,
                             "reason": "rate_limited",
                             "info": summary.get("info")})
    summary["available"] = True
    return JSONResponse(_clean(summary))


@app.post("/api/ask/{ticker}")
async def api_ask(ticker: str, request: Request,
                  user: Optional[dict] = Depends(_current_user),
                  creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Per-stock AI assistant — RAG over SEC filings + transcript, grounded
    with our own metrics. Pro-gated (paid AI) + metered to a monthly cap, since
    Ask AI is per-user and uncached (the one AI cost that scales with usage)."""
    import stock_rag
    tk = (ticker or "").upper()
    # Pro gate: free/anonymous users can't use the paid assistant.
    if not await _is_pro_user(user, creds):
        return JSONResponse(
            {"answer": None, "status": "locked",
             "detail": "Ask AI is a Pro feature. Upgrade to unlock."},
            headers={"Cache-Control": "no-store"})
    # Fair-use caps: a daily inner bound (burst protection) plus the monthly cap.
    q = await _ask_quota(creds)
    if q["used_d"] >= q["cap_d"]:
        return JSONResponse(
            {"answer": None, "status": "limit", "scope": "day",
             "used": q["used_d"], "cap": q["cap_d"],
             "used_month": q["used_m"], "cap_month": q["cap_m"],
             "detail": f"You've reached today's Ask AI limit ({q['cap_d']}). It resets tomorrow."},
            headers={"Cache-Control": "no-store"})
    if q["used_m"] >= q["cap_m"]:
        return JSONResponse(
            {"answer": None, "status": "limit", "scope": "month",
             "used": q["used_m"], "cap": q["cap_m"],
             "detail": f"You've reached this month's Ask AI limit ({q['cap_m']}). It resets next month."},
            headers={"Cache-Control": "no-store"})
    try:
        body = await request.json()
    except Exception:
        body = {}
    question = (body.get("question") or "").strip()[:600]
    # Compact metrics block from the loaded universe (cheap, always available)
    t = next((x for x in (_universe_data or []) if (x.get("ticker", "").upper() == tk)), None)
    lines = []
    if t:
        sc = t.get("smart_score") if t.get("smart_score") is not None else t.get("pop_score")
        lines.append(f"Name: {t.get('name')} | Sector: {t.get('sector')} / {t.get('sub_sector')}")
        lines.append(f"Alpha score: {sc} | Grade: {t.get('grade')} | RS context via app")
        lines.append(f"Price: {t.get('price')} | Target upside %: {t.get('target_upside_pct')}")
        lines.append(f"Rev growth YoY: {t.get('revenue_growth_yoy')} | EPS growth YoY: {t.get('eps_growth_yoy')}")
        lines.append(f"P/E: {t.get('pe_ratio') or t.get('forward_pe')} | PEG: {t.get('peg_ratio')} | Profit margin: {t.get('profit_margin')}")
    res = await stock_rag.ask(tk, question, "\n".join(lines),
                              user_id=(user or {}).get("user_id"))
    # Count this question against BOTH caps only when it actually produced an
    # answer (don't charge the user for errors / empty responses).
    if isinstance(res, dict) and res.get("answer"):
        try:
            await supabase.update_user_metadata(creds.credentials, {
                q["mkey"]:          q["used_m"] + 1,
                "ai_ask_day_date":  q["today"],
                "ai_ask_day_n":     q["used_d"] + 1,
            })
        except Exception:
            pass
    if isinstance(res, dict):
        res.setdefault("used", q["used_m"] + 1)
        res.setdefault("cap", q["cap_m"])
        res.setdefault("used_today", q["used_d"] + 1)
        res.setdefault("cap_today", q["cap_d"])
    return JSONResponse(res, headers={"Cache-Control": "no-store"})


@app.get("/api/ask-status")
async def api_ask_status():
    import stock_rag
    return JSONResponse(stock_rag.status())


@app.get("/api/documents/{ticker}")
async def api_documents(ticker: str):
    """Real dated filing lists from SEC EDGAR (free, no key): 10-K, 10-Q, 8-K."""
    import stock_docs
    data = await stock_docs.list_documents((ticker or "").upper())
    return JSONResponse(_clean(data), headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/doc-pdf")
async def api_doc_pdf(u: str, dl: int = 0):
    """Serve a SEC / IR document for the in-app viewer: PDFs stream through
    unchanged; HTML is served faithfully (with a <base> so assets load) so the
    viewer shows the real original. `dl=1` forces a download. `u` is the source
    URL — SSRF-guarded to SEC + approved IR CDNs inside doc_pdf."""
    import doc_pdf
    from starlette.responses import Response
    try:
        content, mt = await doc_pdf.fetch_doc(u)
    except doc_pdf.DocError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    fn = "document.pdf" if "pdf" in mt else "document.html"
    disp = "attachment" if dl else "inline"
    return Response(content=content, media_type=mt,
                    headers={"Content-Disposition": f'{disp}; filename="{fn}"',
                             "Cache-Control": "public, max-age=86400",
                             "X-Content-Type-Options": "nosniff"})


@app.get("/api/corporate-actions")
async def api_corporate_actions():
    """Universe-scoped corporate-actions feed (ex-dividends + stock splits),
    aggregated from per-ticker yfinance data already held in memory. Returns a
    dated feed: {date, symbol, name, type, action, upcoming}."""
    from datetime import date as _date, timedelta as _td
    today = _date.today()
    today_s = today.isoformat()
    div_lo = (today - _td(days=120)).isoformat()
    div_hi = (today + _td(days=90)).isoformat()
    split_lo = (today - _td(days=540)).isoformat()
    events = []
    for t in (_universe_data or []):
        sym = t.get("ticker")
        name = t.get("name") or sym
        exd = t.get("ex_dividend_date")
        if exd and div_lo <= exd <= div_hi:
            dv = t.get("last_dividend_value")
            amt = f"${dv:.2f} " if isinstance(dv, (int, float)) and dv else ""
            events.append({"date": exd, "symbol": sym, "name": name, "type": "Dividend",
                           "action": f"{amt}ex-dividend", "upcoming": exd >= today_s})
        spf = (t.get("last_split_factor") or "").strip()
        spd = t.get("last_split_date")
        if spf and spd and spd >= split_lo and spf not in ("1:1", "1:0", "0:0"):
            events.append({"date": spd, "symbol": sym, "name": name, "type": "Split",
                           "action": f"{spf} stock split", "upcoming": spd >= today_s})
    events.sort(key=lambda e: e["date"], reverse=True)
    return JSONResponse({"actions": events[:250], "as_of": today_s},
                        headers={"Cache-Control": "public, max-age=1800"})


@app.get("/api/slides/{ticker}")
async def api_slides(ticker: str, q: str = ""):
    """Find an earnings-presentation deck on the company's IR CDN (q4cdn) for a
    given quarter, when SEC has no deck. Returns {available, url}. Needs a search
    key (SERPER_API_KEY / BRAVE_API_KEY); without one, available is False and the
    UI falls back to a manual web-search link."""
    import slides_finder
    tk = (ticker or "").upper()
    quarter = (q or "").strip()
    name = tk
    row = next((x for x in (_universe_data or []) if x.get("ticker") == tk), None)
    if row and row.get("name"):
        name = row["name"]
    else:
        try:
            from stock_universe import get_meta
            m = get_meta(tk)
            if m and m.get("name"):
                name = m["name"]
        except Exception:
            pass
    res = await slides_finder.find_deck(name, tk, quarter)
    return JSONResponse({"available": bool(res.get("url")), "url": res.get("url") or "",
                         "reason": res.get("reason")},
                        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/transcripts/{ticker}")
async def api_transcripts(ticker: str):
    """Dated earnings-call transcripts list (last 8 quarters). Each row links to
    the in-app reader page, which fetches the transcript on demand from Alpha
    Vantage. We list quarters rather than probe each (AV is rate-limited); the
    reader reports gracefully when a given quarter has no transcript."""
    import event_intel as ei
    tk = (ticker or "").upper()

    def _label(q: str) -> str:          # '2026Q1' -> 'Q1 2026'
        return f"{q[4:]} {q[:4]}" if len(q) >= 6 else q

    rows = [{"quarter": q, "label": _label(q), "url": f"/transcript/{tk}?q={q}"}
            for q in ei._recent_quarters(8)]
    return JSONResponse({"ticker": tk, "transcripts": rows},
                        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/transcript/{ticker}", response_class=HTMLResponse)
async def transcript_page(ticker: str, q: str = ""):
    """Reader page for a single earnings-call transcript (opens in a new tab
    from the Documents list). Source: Alpha Vantage EARNINGS_CALL_TRANSCRIPT."""
    import event_intel as ei
    from html import escape as _esc
    tk = (ticker or "").upper()
    quarter = (q or "").upper().strip() or None
    qlabel = (f"{quarter[4:]} {quarter[:4]}" if quarter and len(quarter) >= 6 else (quarter or ""))

    try:
        data = await ei._fetch_av_transcript(tk, quarter)
    except Exception as exc:
        logger.warning(f"transcript_page {tk} {quarter}: {exc}")
        data = None

    if isinstance(data, dict) and data.get("error") == "rate_limited":
        body = ('<div class="note">The transcript feed is rate-limited right now. '
                'Please try again later.</div>')
    elif isinstance(data, dict) and data.get("transcript"):
        turns = []
        for seg in data.get("transcript", []):
            if not isinstance(seg, dict):
                continue
            who = _esc((seg.get("speaker") or "").strip())
            title = _esc((seg.get("title") or "").strip())
            txt = _esc((seg.get("content") or "").strip())
            if not txt:
                continue
            head = who + (f' <span class="ttl">· {title}</span>' if title else "")
            turns.append(f'<div class="turn"><div class="sp">{head}</div><p>{txt}</p></div>')
        body = "".join(turns) or '<div class="note">This transcript appears to be empty.</div>'
    else:
        body = ('<div class="note">No transcript is available for '
                f'<b>{_esc(tk)}</b>{(" · " + _esc(qlabel)) if qlabel else ""} yet. '
                'Transcripts post within a few days of the earnings call.</div>')

    page = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(tk)} earnings call transcript{(' — ' + _esc(qlabel)) if qlabel else ''}</title>
<style>
:root{{color-scheme:light}}
*{{box-sizing:border-box}}
body{{margin:0;background:#f5f6f8;color:#0f172a;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,Arial,sans-serif;line-height:1.6}}
.wrap{{max-width:820px;margin:0 auto;padding:28px 20px 80px}}
.eyebrow{{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#0040c1}}
h1{{font-size:24px;margin:4px 0 2px}}
.sub{{color:#64748b;font-size:13px;margin:0 0 22px}}
.turn{{background:#fff;border:1px solid #e4e7eb;border-radius:12px;padding:14px 16px;margin:0 0 12px}}
.sp{{font-weight:800;font-size:13.5px;color:#0040c1;margin-bottom:5px}}
.sp .ttl{{font-weight:600;color:#64748b}}
.turn p{{margin:0;color:#1f2937;font-size:14px}}
.note{{background:#fff;border:1px dashed #cbd5e1;border-radius:12px;padding:18px;color:#475569;font-size:14px}}
.foot{{margin-top:24px;font-size:12px;color:#94a3b8}}
</style></head><body><div class="wrap">
<div class="eyebrow">Earnings call transcript</div>
<h1>{_esc(tk)}{(' · ' + _esc(qlabel)) if qlabel else ''}</h1>
<p class="sub">Source: Alpha Vantage · informational only, verify against the company's official filing.</p>
{body}
<div class="foot">TickerMover · transcript reader</div>
</div></body></html>"""
    return HTMLResponse(content=page, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/operating-kpis/{ticker}")
async def api_operating_kpis(ticker: str,
                             user: Optional[dict] = Depends(_current_user),
                             creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Retired from the UI (the Insights tab was removed). Kept as a Pro-gated
    endpoint so nothing can trigger the (paid) AI extraction for free users."""
    if not await _is_pro_user(user, creds):
        return JSONResponse({"ticker": (ticker or "").upper(), "status": "locked",
                             "detail": "Operating KPIs are a Pro feature."},
                            headers={"Cache-Control": "no-store"})
    import operating_kpis
    data = await operating_kpis.get_operating_kpis((ticker or "").upper())
    return JSONResponse(_clean(data), headers={"Cache-Control": "no-store"})


def _ticker_metrics_block(tk: str) -> str:
    t = next((x for x in (_universe_data or []) if (x.get("ticker", "").upper() == tk)), None)
    if not t:
        return ""
    sc = t.get("smart_score") if t.get("smart_score") is not None else t.get("pop_score")
    return (f"Name: {t.get('name')} | Sector: {t.get('sector')} / {t.get('sub_sector')}\n"
            f"Alpha score: {sc} | Grade: {t.get('grade')}\n"
            f"Price: {t.get('price')} | Target upside %: {t.get('target_upside_pct')}\n"
            f"Rev growth YoY: {t.get('revenue_growth_yoy')} | EPS growth YoY: {t.get('eps_growth_yoy')}")


@app.get("/api/concall/{ticker}")
async def api_concall(ticker: str, q: str = ""):
    """Detailed earnings-call (concall) summary. `q` (e.g. '2026Q1') targets a
    specific call for the per-card Documents view; omitted = latest."""
    import stock_rag
    tk = (ticker or "").upper()
    quarter = (q or "").upper().strip() or None
    res = await stock_rag.concall_summary(tk, _ticker_metrics_block(tk), quarter=quarter)
    return JSONResponse(_clean(res), headers={"Cache-Control": "public, max-age=3600"})


@app.get("/api/stock-extra/{ticker}")
async def api_stock_extra(ticker: str):
    """On-demand FMP-enriched data for the stock drawer's Financials / Valuation
    / Estimates / Benchmark / Documents tabs: full valuation multiples, returns,
    free cash flow, analyst estimates + price targets, recent rating changes,
    peers and SEC filings. Cached server-side (24h). Returns {} fields on any
    failure so the drawer degrades gracefully."""
    sym = ticker.upper()
    try:
        data = await coordinator.get_fmp_enrichment(sym)
        return JSONResponse({"ticker": sym, **(data or {})})
    except Exception as exc:
        logger.warning(f"stock-extra {sym}: {exc}")
        return JSONResponse({"ticker": sym})


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

    # Cache the (LLM-backed) thesis so opening a drawer repeatedly doesn't
    # re-hit the model. Keyed by ticker; short TTL keeps it fresh intraday.
    cache_key = f"thesis:{sym}"
    cached = cache.get(cache_key)
    if cached is not None:
        return JSONResponse(cached)

    try:
        thesis = _clean(await thesis_gen.build(target))
        cache.set(cache_key, thesis, ttl=1800)
        return JSONResponse(thesis)
    except Exception as exc:
        logger.error(f"Thesis generation failed for {sym}: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Thesis generation failed")


# ── AI Deep-Dive research (cached; web-grounded via Anthropic) ─────────────
_research_generating: set = set()
# Last generation error per ticker, surfaced via /api/research so a silent
# Anthropic-API failure is diagnosable without server logs.
_research_errors: dict = {}


async def _run_research_job(sym: str, target: dict | None):
    import research_gen
    from research_store import store as _rstore
    try:
        out = await research_gen.generate_research(sym, target)
        _rstore.save(sym, out)
        _research_errors.pop(sym, None)
    except Exception as exc:
        _research_errors[sym] = str(exc)[:600]
        logger.error(f"Deep-Dive research generation failed for {sym}: {exc}")
    finally:
        _research_generating.discard(sym)


@app.get("/api/research/{ticker}")
async def api_research(ticker: str, force: bool = False,
                      user: Optional[dict] = Depends(_current_user),
                      creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Per-stock AI Deep-Dive brief. Served from cache and regenerated in the
    background when missing/stale (so page-load is never blocked). The brief is
    web-grounded with citations; the ticker's own live data is the ground truth.
    Pro-gated: only Pro subscribers can view/trigger the (paid) generation."""
    import asyncio
    import research_gen
    from research_store import store as _rstore

    sym = ticker.upper()
    # Gate the paid AI feature: free/anonymous users never trigger generation.
    if not await _is_pro_user(user, creds):
        return JSONResponse({"ticker": sym, "status": "locked",
                             "detail": "The AI Deep-Dive is a Pro feature. Upgrade to unlock."})
    doc = _rstore.get(sym)

    def _payload(d: dict, **extra) -> dict:
        out = {
            "ticker":       sym,
            "status":       "ready",
            "markdown":     d.get("markdown", ""),
            "sources":      d.get("sources", []) or [],
            "model":        d.get("model", ""),
            "generated_at": d.get("generated_at") or d.get("generated_epoch"),
        }
        out.update(extra)
        return out

    fresh = bool(doc) and doc.get("status") == "ready" and not _rstore.is_stale(doc) and not force
    if fresh:
        return JSONResponse(_payload(doc))

    if not research_gen.available():
        if doc:
            return JSONResponse(_payload(doc, stale=True))
        return JSONResponse({"ticker": sym, "status": "unavailable",
                             "detail": "AI Deep-Dive is not enabled (set ANTHROPIC_API_KEY)."})

    # Kick off a single background generation per ticker.
    if sym not in _research_generating:
        _research_generating.add(sym)
        target = next((t for t in _universe_data if t.get("ticker") == sym), None)
        try:
            asyncio.create_task(_run_research_job(sym, target))
        except RuntimeError:
            _research_generating.discard(sym)

    # Serve a stale brief immediately while the fresh one regenerates. Surface
    # the last generation error (if any) so a silently-failing Anthropic call is
    # visible in the API response.
    last_err = _research_errors.get(sym)
    if doc:
        return JSONResponse(_payload(doc, regenerating=True, last_error=last_err))
    return JSONResponse({"ticker": sym, "status": "generating", "last_error": last_err})


# ── AI overview snapshot (no web) — powers the default Overview business/risk/
#    edge boxes. The full web-grounded note lives on /api/research and is
#    generated lazily only when the user opens the Deep-Dive tab. ─────────────
#
# Two-tier cache so the same stock is NOT re-billed on every open or redeploy:
#   L1 = in-memory SmartCache (instant, but wiped on restart/Railway redeploy)
#   L2 = overview_store → Supabase (durable, survives deploys; disk fallback)
# Plus an in-flight dedupe so N simultaneous opens of one ticker share a single
# paid generation instead of firing N model calls.
_overview_inflight: dict = {}   # sym -> asyncio.Task[dict]


@app.get("/api/overview/{ticker}")
async def api_overview(ticker: str, force: bool = False,
                       user: Optional[dict] = Depends(_current_user),
                       creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Business/risk/edge snapshot for the stock page's default Overview boxes.
    Pro-gated. Served from a durable per-ticker cache (Supabase, survives
    redeploys) and only regenerated when missing/stale — so repeatedly opening
    the same stock costs nothing after the first view."""
    import asyncio
    import research_gen
    from overview_store import store as _ovstore
    sym = ticker.upper()
    if not await _is_pro_user(user, creds):
        return JSONResponse({"ticker": sym, "status": "locked",
                             "detail": "The AI overview is a Pro feature. Upgrade to unlock."})
    if not research_gen.available():
        return JSONResponse({"ticker": sym, "status": "unavailable",
                             "detail": "AI overview is not enabled (set ANTHROPIC_API_KEY)."})

    ck = "overview:" + sym

    def _from_doc(d: dict) -> dict:
        return {
            "ticker":   sym,
            "status":   d.get("status", "ready"),
            "markdown": d.get("markdown", ""),
            "sources":  d.get("sources", []) or [],
            "model":    d.get("model", ""),
            "kind":     "overview",
        }

    # `force` is server-controlled only (no client passes it); honouring it still
    # respects the dedupe below so a manual refresh can't fan out into N calls.
    if not force:
        # L1 — in-memory (fast path within this process)
        cached = cache.get(ck)
        if cached is not None:
            return JSONResponse(cached)
        # L2 — durable store (survives redeploys); re-warm L1 on hit
        doc = _ovstore.get(sym)
        if (doc and doc.get("status") == "ready" and doc.get("markdown")
                and not _ovstore.is_stale(doc)):
            out = _from_doc(doc)
            cache.set(ck, out, ttl=2592000)   # L1 mirrors the 30-day durable TTL
            return JSONResponse(out)

    # Need to (re)generate — dedupe concurrent opens of the same ticker so only
    # ONE paid call runs; every other waiter awaits the same task.
    task = _overview_inflight.get(sym)
    if task is None or task.done():
        async def _gen() -> dict:
            target = next((t for t in _universe_data if t.get("ticker") == sym), None)
            # Elite top slice of the curated set (prime + top scorers) gets the
            # premium Opus tier — the names users scrutinise most. Rest of the 35
            # and the long tail stay on Sonnet. Bounded, cached, tunable.
            out = await research_gen.generate_overview(sym, target, premium=_is_premium_overview(sym))
            out.setdefault("status", "ready")
            _ovstore.save(sym, out)          # durable (Supabase + disk)
            cache.set(ck, out, ttl=2592000)   # warm L1 (30 days, mirrors durable TTL)
            return out
        task = asyncio.ensure_future(_gen())
        _overview_inflight[sym] = task

    try:
        out = await task
        return JSONResponse(out)
    except Exception as exc:
        logger.error(f"Overview generation failed for {sym}: {exc}")
        # Serve a stale snapshot rather than nothing if we have one.
        doc = _ovstore.get(sym)
        if doc and doc.get("markdown"):
            return JSONResponse(_from_doc(doc))
        return JSONResponse({"ticker": sym, "status": "error", "detail": str(exc)[:200]})
    finally:
        if _overview_inflight.get(sym) is task:
            _overview_inflight.pop(sym, None)


# ── "Why we bought it today" — one AI sentence per Top Hunts pick ──────────
# Grounded entirely in our own factor profile (Alpha Score, grade, 6 pillars,
# analyst upside) so nothing is invented. Cheap Haiku tier, cached durably in
# app_kv keyed by ticker → one paid call per pick, ever. Lazy-loaded per card
# so it never blocks the main /api/model-portfolio payload.
_why_inflight: dict = {}   # sym -> asyncio.Task[dict]


@app.get("/api/model-portfolio/why/{ticker}")
async def api_why_today(ticker: str, force: bool = False):
    """Return {ticker, why, status}. Observational (what the scan flagged),
    not advice. Served from a durable per-ticker cache; only (re)generated when
    missing, so it costs nothing after the first view."""
    import asyncio
    import why_today as _why
    sym = ticker.upper()

    # Fast path: durable cache hit (no key needed to serve an existing note).
    if not force:
        cached = _why._kv.get(_why._NS, sym)
        if cached and cached.get("points"):
            return JSONResponse({"ticker": sym, "points": cached["points"], "status": "ready"})

    if not _why.available():
        return JSONResponse({"ticker": sym, "points": None, "status": "unavailable"})

    # Build grounding from the live universe row (+ derived pillars / upside).
    live = next((t for t in _universe_data if t.get("ticker") == sym), None)
    ground = {"ticker": sym}
    if live:
        try:
            price = float(live.get("price") or 0)
            tgt = float(live.get("target_mean") or live.get("street_target") or 0)
        except (TypeError, ValueError):
            price = tgt = 0
        ground.update({
            "name":        live.get("name"),
            "sub_sector":  live.get("sub_sector") or live.get("sector"),
            "smart_score": live.get("smart_score"),
            "pop_score":   live.get("pop_score"),
            "grade":       live.get("grade"),
            "pillars":     _compute_pillars(live),
            "rationale":   live.get("rationale"),
            "signals":     live.get("signals"),
        })
        if price > 0 and tgt > 0:
            up = (tgt - price) / price * 100
            ground["_target_upside"] = f"{'+' if up >= 0 else ''}{round(up, 1)}%"

    # Dedupe concurrent first-views of the same ticker → one paid call only.
    task = _why_inflight.get(sym)
    if task is None or task.done():
        task = asyncio.ensure_future(_why.generate(sym, ground, force=force))
        _why_inflight[sym] = task
    try:
        out = await task
        return JSONResponse({"ticker": sym, "points": out.get("points"),
                             "status": out.get("status", "ready")})
    except Exception as exc:
        logger.error(f"why_today endpoint {sym} failed: {exc}")
        return JSONResponse({"ticker": sym, "points": None, "status": "error"})
    finally:
        if _why_inflight.get(sym) is task:
            _why_inflight.pop(sym, None)


# ── Sector relationship graph (topology only; generated once, cached) ──────
# The Universe "Sector connections" web. Node values (α-Score, size) stay live
# on the client; only the wiring is AI-generated, so this is a one-time Haiku
# cost cached durably in app_kv. Keyed by a hash of the sub-sector list, so a
# taxonomy change auto-regenerates while a stable universe is a permanent hit.
_sector_graph_inflight: dict = {}   # cache-key -> asyncio.Task[dict]


@app.post("/api/sector-graph")
async def api_sector_graph(request: Request, force: bool = False):
    """Return {nodes, edges, model, status} wiring the live sub-sectors into a
    value chain. Body: {"sectors": [...]} — the client's current sub-sector
    names, so edge endpoints line up exactly with the table's data-subsector.
    Never errors to the UI: falls back to the curated seed wiring."""
    import asyncio
    import hashlib
    import sector_graph
    from kv_store import store as _kv

    try:
        body = await request.json()
    except Exception:
        body = {}
    sectors = body.get("sectors") if isinstance(body, dict) else None
    if not isinstance(sectors, list):
        sectors = []
    # Normalise: strip, drop blanks, dedupe (preserve order), cap.
    seen: set = set()
    clean: list = []
    for s in sectors:
        s = str(s or "").strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            clean.append(s)
        if len(clean) >= 80:
            break
    if not clean:
        return JSONResponse({"nodes": [], "edges": [], "model": "seed", "status": "ready"})

    key = hashlib.sha1("\n".join(sorted(s.lower() for s in clean)).encode("utf-8")).hexdigest()[:16]
    ck = "sectorgraph:" + key

    if not force:
        # L1 — in-memory
        cached = cache.get(ck)
        if cached is not None:
            return JSONResponse(cached)
        # L2 — durable KV (survives redeploys); re-warm L1 on hit
        doc = _kv.get("sector_graph", key, max_age_s=45 * 86400)
        if isinstance(doc, dict) and doc.get("edges") is not None:
            cache.set(ck, doc, ttl=2592000)   # 30 days
            return JSONResponse(doc)

    # Generate — dedupe so concurrent first-loads share ONE paid call.
    task = _sector_graph_inflight.get(key)
    if task is None or task.done():
        async def _gen() -> dict:
            out = await sector_graph.generate_sector_graph(clean)
            out.setdefault("status", "ready")
            # Only the AI result is worth persisting; the pure-seed fallback is
            # cheap to recompute and shouldn't poison the durable cache.
            if out.get("model") not in (None, "", "seed"):
                _kv.set("sector_graph", key, out)
            cache.set(ck, out, ttl=2592000)   # 30 days
            return out
        task = asyncio.ensure_future(_gen())
        _sector_graph_inflight[key] = task

    try:
        out = await task
        return JSONResponse(out)
    except Exception as exc:
        logger.error(f"Sector graph generation failed: {exc}")
        return JSONResponse(sector_graph.seed_graph(clean))
    finally:
        if _sector_graph_inflight.get(key) is task:
            _sector_graph_inflight.pop(key, None)


# ── Dependencies & ripple-risk (web-grounded; cached 30 days) ──────────────
_deps_inflight: dict = {}   # sym -> asyncio.Task[dict]


@app.get("/api/dependencies/{ticker}")
async def api_dependencies(ticker: str, force: bool = False,
                           user: Optional[dict] = Depends(_current_user),
                           creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Structured supply-chain / dependency map for the stock detail view:
    exposure donut + the companies it depends on (with ripple-risk). Pro-gated,
    web-grounded, generated once and cached 30 days in app_kv."""
    import asyncio
    import dependencies_gen
    from kv_store import store as _kv
    sym = ticker.upper()
    if not await _is_pro_user(user, creds):
        return JSONResponse({"ticker": sym, "status": "locked",
                             "detail": "Dependency mapping is a Pro feature. Upgrade to unlock."})
    if not dependencies_gen.available():
        return JSONResponse({"ticker": sym, "status": "unavailable",
                             "detail": "Not enabled (set ANTHROPIC_API_KEY)."})

    ck = "deps:" + sym
    if not force:
        cached = cache.get(ck)
        if cached is not None:
            return JSONResponse(cached)
        doc = _kv.get("dependencies", sym, max_age_s=30 * 86400)
        if isinstance(doc, dict) and (doc.get("dependencies") or doc.get("exposure")):
            cache.set(ck, doc, ttl=30 * 86400)
            return JSONResponse(doc)

    task = _deps_inflight.get(sym)
    if task is None or task.done():
        async def _gen() -> dict:
            target = next((t for t in _universe_data if t.get("ticker") == sym), None)
            out = await dependencies_gen.generate_dependencies(sym, target)
            out.setdefault("status", "ready")
            _kv.set("dependencies", sym, out)
            cache.set(ck, out, ttl=30 * 86400)
            return out
        task = asyncio.ensure_future(_gen())
        _deps_inflight[sym] = task

    try:
        out = await task
        return JSONResponse(out)
    except Exception as exc:
        logger.error(f"Dependencies generation failed for {sym}: {exc}")
        return JSONResponse({"ticker": sym, "status": "error", "detail": str(exc)[:200]})
    finally:
        if _deps_inflight.get(sym) is task:
            _deps_inflight.pop(sym, None)


# ── AI head-to-head comparison cards (cached; web-grounded) ────────────────
_compare_generating: set = set()


async def _run_compare_job(sym: str, target: dict | None):
    import compare_gen
    from compare_store import store as _cstore
    try:
        out = await compare_gen.generate_compare_card(sym, target)
        _cstore.save(sym, out)
    except Exception as exc:
        logger.error(f"Comparison card generation failed for {sym}: {exc}")
    finally:
        _compare_generating.discard(sym)


@app.get("/api/compare-card/{ticker}")
async def api_compare_card(ticker: str, force: bool = False,
                           user: Optional[dict] = Depends(_current_user),
                           creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Per-stock structured comparison card (operational stage, revenue,
    backlog, execution risk, profitability path). Served from cache and
    regenerated in the background when missing/stale, so the Peers tab never
    blocks. The ticker's own live data is the ground truth. Pro-gated (paid AI)."""
    import asyncio
    import compare_gen
    from compare_store import store as _cstore

    sym = ticker.upper()
    if not await _is_pro_user(user, creds):
        return JSONResponse({"ticker": sym, "status": "locked",
                             "detail": "AI peer-compare is a Pro feature. Upgrade to unlock."})
    doc = _cstore.get(sym)

    def _payload(d: dict, **extra) -> dict:
        out = {
            "ticker":       sym,
            "status":       "ready",
            "card":         d.get("card", {}) or {},
            "sources":      d.get("sources", []) or [],
            "model":        d.get("model", ""),
            "generated_at": d.get("generated_at") or d.get("generated_epoch"),
        }
        out.update(extra)
        return out

    fresh = bool(doc) and doc.get("status") == "ready" and not _cstore.is_stale(doc) and not force
    if fresh:
        return JSONResponse(_payload(doc))

    if not compare_gen.available():
        if doc:
            return JSONResponse(_payload(doc, stale=True))
        return JSONResponse({"ticker": sym, "status": "unavailable",
                             "detail": "AI comparison is not enabled (set ANTHROPIC_API_KEY)."})

    if sym not in _compare_generating:
        _compare_generating.add(sym)
        target = next((t for t in _universe_data if t.get("ticker") == sym), None)
        try:
            asyncio.create_task(_run_compare_job(sym, target))
        except RuntimeError:
            _compare_generating.discard(sym)

    if doc:
        return JSONResponse(_payload(doc, regenerating=True))
    return JSONResponse({"ticker": sym, "status": "generating"})


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


@app.get("/api/candles/{symbol}")
async def api_candles(symbol: str, days: int = 130):
    """Daily OHLCV candles for client-side charting + price-action analysis.
    Returns candles plus key levels and (when known) analyst-target overlays."""
    sym  = symbol.upper()
    days = max(40, min(int(days or 130), 400))
    try:
        data = await coordinator.get_candles_raw(sym, days)
    except Exception as exc:
        logger.error(f"Candles fetch {sym}: {exc}")
        data = {}
    if not data or not data.get("candles"):
        raise HTTPException(status_code=404, detail=f"No candle data for {sym}")

    # Overlay levels from the live universe row when available.
    meta = {}
    for t in _universe_data:
        if t.get("ticker") == sym:
            for k in ("price", "target_mean", "target_low", "target_high",
                      "sma_50", "sma_200", "smart_score", "grade"):
                if t.get(k) is not None:
                    meta[k] = t[k]
            break
    data["meta"] = meta
    return JSONResponse(
        content=data,
        headers={"Cache-Control": "public, max-age=300, s-maxage=900"},
    )


# IMPORTANT: /api/news/live MUST be defined BEFORE /api/news/{symbol}
# otherwise FastAPI matches "live" as a ticker symbol parameter.
@app.get("/api/news/live")
async def api_news_live():
    """Fetch live news for the Hot-News panel.

    Merges two providers so the feed spans more than one wire service:
      • Alpaca  — Benzinga-sourced breaking headlines.
      • FMP     — aggregated publishers incl. Seeking Alpha, Zacks, Motley Fool.
    Results are de-duplicated by URL/headline, sorted newest-first, and cached
    in-process for a few minutes so panel re-renders don't burn provider quota.
    """
    import time as _time
    import httpx
    from datetime import datetime as _dt, timezone as _tz

    # ── short TTL cache (panel re-renders on every dashboard render) ──
    global _LIVE_NEWS_CACHE
    now = _time.time()
    cached = _LIVE_NEWS_CACHE.get("payload")
    if cached and (now - _LIVE_NEWS_CACHE.get("ts", 0)) < 180:
        return JSONResponse(cached)

    # Prefer hot-list tickers (most relevant), fall back to top universe tickers
    hot_tickers  = [t["ticker"] for t in _daily_hot if t.get("ticker")]
    all_tickers  = [t["ticker"] for t in _universe_data[:20] if t.get("ticker")]
    ticker_batch = (hot_tickers or all_tickers)[:15]

    headers = {
        "APCA-API-KEY-ID":     config.ALPACA_KEY_ID,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
    }

    # ── Alpaca (Benzinga) ───────────────────────────────────────────
    async def _fetch_alpaca(params: dict):
        async with httpx.AsyncClient(timeout=15) as client:
            return await client.get("https://data.alpaca.markets/v1beta1/news",
                                    headers=headers, params=params)

    def _parse_alpaca(raw_list):
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
                "source":   item.get("source", "") or "Benzinga",
                "author":   item.get("author", ""),
                "ticker":   syms[0] if syms else "",
                "symbols":  syms,
                "datetime": ts,
                "sentiment": "neutral",
            })
        return out

    async def _gather_alpaca():
        try:
            if ticker_batch:
                resp = await _fetch_alpaca({"symbols": ",".join(ticker_batch), "limit": 50, "sort": "desc"})
                if resp.status_code == 200 and resp.json().get("news"):
                    return _parse_alpaca(resp.json()["news"])
            resp = await _fetch_alpaca({"limit": 50, "sort": "desc"})
            if resp.status_code == 200:
                return _parse_alpaca(resp.json().get("news", []))
            logger.warning(f"Alpaca news returned {resp.status_code}: {resp.text[:160]}")
        except Exception as exc:
            logger.warning(f"Alpaca news fetch failed: {exc}")
        return []

    # ── FMP (Seeking Alpha, Zacks, Motley Fool, …) ──────────────────
    async def _gather_fmp():
        if not (config.FMP_API_KEY and ticker_batch):
            return []
        try:
            params = {"symbols": ",".join(ticker_batch), "limit": 50, "apikey": config.FMP_API_KEY}
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get("https://financialmodelingprep.com/stable/news/stock", params=params)
            if r.status_code != 200:
                logger.warning(f"FMP news returned {r.status_code}: {r.text[:160]}")
                return []
            out = []
            for item in (r.json() or []):
                sym = (item.get("symbol") or "").upper()
                ts = 0
                pub = item.get("publishedDate") or ""
                try:
                    # FMP timestamps are naive ("YYYY-MM-DD HH:MM:SS"); treat as UTC
                    # so a slight offset can only age an item, never push it future.
                    ts = int(_dt.fromisoformat(pub).replace(tzinfo=_tz.utc).timestamp())
                except Exception:
                    pass
                out.append({
                    "headline": item.get("title", ""),
                    "summary":  (item.get("text") or "")[:280],
                    "url":      item.get("url", "#"),
                    "source":   item.get("publisher", "") or item.get("site", ""),
                    "author":   "",
                    "ticker":   sym,
                    "symbols":  [sym] if sym else [],
                    "datetime": ts,
                    "sentiment": "neutral",
                })
            return out
        except Exception as exc:
            logger.warning(f"FMP news fetch failed: {exc}")
            return []

    try:
        alpaca_news, fmp_news = await asyncio.gather(_gather_alpaca(), _gather_fmp())

        # Merge + de-dupe (same Benzinga story can arrive via both providers).
        merged, seen = [], set()
        for n in (alpaca_news + fmp_news):
            url = (n.get("url") or "").strip().lower()
            key = url if (url and url != "#") else (n.get("headline") or "").strip().lower()[:80]
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append(n)
        merged.sort(key=lambda n: n.get("datetime", 0), reverse=True)

        payload = {"news": merged, "count": len(merged), "source": "alpaca+fmp"}
        _LIVE_NEWS_CACHE = {"ts": now, "payload": payload}
        return JSONResponse(payload)
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

<h3>TickerMover View</h3>
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

<h3>TickerMover View</h3>
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

<h3>TickerMover View</h3>
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

<h3>TickerMover View</h3>
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
<p>QUBT carries a Alpha Score of <strong>72</strong> with an RS Rating of 76 — indicating it is outperforming 76% of all stocks in the TickerMover universe over the past 12 months. The stock is within 22.4% of its 52-week high after consolidating a major prior breakout. EPS beat 4 of the last 4 quarters, and gross margin is expanding as the software mix grows. Short interest at <strong>28.6%</strong> makes this a high-volatility, high-conviction setup.</p>

<h3>The Risks</h3>
<p>QUBT is a small-cap ($3.5B market cap) company in a nascent technology sector where timelines have historically slipped. Revenue is growing but from a small base, and profitability is still in the future. Any negative news about quantum hardware milestones could create significant stock volatility.</p>

<h3>TickerMover View</h3>
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

<h3>TickerMover View</h3>
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
<p>CRDO is the top performer in our Model Portfolio, with Alpha Score of <strong>71</strong> and one of the strongest momentum profiles in the TickerMover universe. RSI at 64 — ideal momentum zone — suggests room to run without overextension. High-speed SerDes is a winner-take-most market, and Credo has won.</p>

<h3>TickerMover View</h3>
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

<h3>TickerMover View</h3>
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

<h3>TickerMover View</h3>
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

<h3>TickerMover View</h3>
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

<h3>TickerMover View</h3>
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

<h3>TickerMover View</h3>
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

<h3>TickerMover View</h3>
<p>LSCC earns a <strong>TOP TIER</strong> grade. Lattice is the dominant player in low-power FPGAs — a niche with massive secular tailwinds and limited competition. Edge AI is the next trillion-dollar opportunity after cloud AI.</p>"""
  },
  {
    "id": "nvt-model-portfolio-update",
    "ticker": "MU", "date": "2026-04-26",
    "category": "Market Analysis",
    "title": "AI Infrastructure Stocks: Weekly Scorecard and What to Watch",
    "summary": "A weekly review of the TickerMover Model Portfolio performance, key catalyst events ahead, and the macro backdrop for AI infrastructure names.",
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
    "content": """<p>The TickerMover Model Portfolio has delivered exceptional performance over its first 30 days, with the portfolio up <strong>+26.7% on an equal-weighted basis</strong> versus the S&P 500's modest single-digit gains over the same period. Here's our weekly scorecard and outlook.</p>

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

<h3>TickerMover Macro View</h3>
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


# ── 6-PILLAR FACTOR BREAKDOWN ─────────────────────────────────────────────────
# The Alpha Score (smart_score) is a multi-factor composite, but the user
# can't see WHICH factors are driving it. _compute_pillars returns a 0-100
# score for each of six investment dimensions so the UI can show:
#
#     Momentum     ████████░░  82
#     Growth       ███████░░░  73
#     Quality      ██████░░░░  61
#     Valuation    ███░░░░░░░  28  ← stretched
#     Sentiment    ██████░░░░  64
#     Growth pot.  ████░░░░░░  41
#
# The selection filter also uses the pillars: a stock must score ≥ 50 on
# at least 4 of the 6 pillars to enter the tracker — a "soft veto" so a
# pure-momentum name with terrible fundamentals can't sneak in.

def _compute_pillars(t: dict) -> dict:
    """Return per-pillar 0-100 scores for a ticker.

    Pillars: momentum, growth, quality, valuation, sentiment, growth_potential.
    """
    def clip(x, lo=0, hi=100):
        return max(lo, min(hi, x))

    price = float(t.get("price") or 0)
    target = float(t.get("target_mean") or 0)

    # ── 1. Momentum — recent price action ───────────────────────────────
    mom1 = float(t.get("momentum_1m") or 0)   # %
    mom3 = float(t.get("momentum_3m") or 0)   # %
    sma50 = float(t.get("sma_50") or 0)
    above_sma = ((price - sma50) / sma50 * 100) if sma50 > 0 else 0
    # 0%/m → 50, +20%/m → 80, +30%/m → 95
    momentum = clip(50 + mom1 * 1.5 + mom3 * 0.3 + (5 if above_sma > 0 else 0))

    # ── 2. Growth — revenue + EPS growth ────────────────────────────────
    rev_g = float(t.get("revenue_growth_yoy") or t.get("rev_growth_qyoy") or 0)
    eps_g = float(t.get("eps_growth_yoy") or 0)
    # 0% → 25, 20% rev → 75, 40%+ → 100
    growth = clip(25 + rev_g * 2.5 + eps_g * 0.5)

    # ── 3. Quality — margins, FCF, balance sheet ───────────────────────
    gross_m = float(t.get("gross_margin") or 0)
    fcf_m   = float(t.get("fcf_margin") or 0)
    debt_eq = float(t.get("debt_to_equity") or 0)
    # Handle both decimal (0.5) and percentage (50) representations
    gm_pct = gross_m * 100 if 0 < gross_m < 2 else gross_m
    fcf_pct = fcf_m * 100 if -2 < fcf_m < 2 else fcf_m
    quality_score = 30
    if gm_pct > 0:
        quality_score += clip(gm_pct, 0, 60) * 0.8
    if fcf_pct > 0:
        quality_score += min(15, fcf_pct * 0.5)
    elif fcf_pct < -10:
        quality_score -= 10
    if 0 < debt_eq < 1:
        quality_score += 10
    elif debt_eq > 3:
        quality_score -= 15
    quality = clip(quality_score)

    # ── 4. Valuation — PEG + price vs. analyst target ──────────────────
    peg = float(t.get("peg_ratio") or 0)
    pe  = float(t.get("pe_ratio") or 0)
    upside = float(t.get("target_upside_pct") or 0)
    val_score = 50
    if 0 < peg < 1:    val_score += 25
    elif peg < 2:      val_score += 10
    elif peg > 4:      val_score -= 30
    elif peg > 3:      val_score -= 15
    if pe > 80:        val_score -= 15
    if upside > 20:    val_score += 15
    elif upside > 10:  val_score += 5
    elif upside < -25: val_score -= 35  # price 25%+ above target → stretched
    elif upside < -10: val_score -= 20
    elif upside < 0:   val_score -= 10
    valuation = clip(val_score)

    # ── 5. Sentiment — analysts + insiders + social ────────────────────
    strong_buy = float(t.get("strong_buy_pct") or 0)
    sb_pct = strong_buy * 100 if 0 < strong_buy < 1.5 else strong_buy
    ins_buys = int(t.get("insider_buys_90d") or 0)
    ins_sells = int(t.get("insider_sells_90d") or 0)
    mv = float(t.get("mention_velocity") or 0)
    sent_score = 30
    if sb_pct > 0:
        sent_score += min(40, sb_pct * 0.5)
    net_ins = ins_buys - ins_sells
    if net_ins > 0:    sent_score += min(15, net_ins * 5)
    elif net_ins < -5: sent_score -= 10
    if mv > 0.5:       sent_score += 10
    elif mv > 0:       sent_score += 5
    sentiment = clip(sent_score)

    # ── 6. Growth potential — analyst headroom + reverse DCF ───────────
    gp_score = 40
    if upside > 30:    gp_score += 35
    elif upside > 15:  gp_score += 20
    elif upside > 5:   gp_score += 10
    elif upside < -10: gp_score -= 25
    elif upside < 0:   gp_score -= 10
    # Reverse DCF: implied CAGR to reach analyst target in 3 years
    if price > 0 and target > 0:
        implied_cagr = ((target / price) ** (1/3) - 1) * 100
        if 5 <= implied_cagr <= 35:
            gp_score += 15      # realistic
        elif implied_cagr > 50:
            gp_score -= 10      # ambitious — too much priced in
        elif implied_cagr < -15:
            gp_score -= 25      # already past target
    growth_potential = clip(gp_score)

    return {
        "momentum":         round(momentum),
        "growth":           round(growth),
        "quality":          round(quality),
        "valuation":        round(valuation),
        "sentiment":        round(sentiment),
        "growth_potential": round(growth_potential),
    }


def _pillar_pass_count(t: dict, threshold: int = 50) -> int:
    """How many of the 6 pillars score >= threshold. Used by the entry
    soft-veto: a stock must hit at least 4 of 6 to enter the tracker."""
    p = _compute_pillars(t)
    return sum(1 for v in p.values() if v >= threshold)


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


def _is_featured_eligible(t: dict) -> bool:
    """Broader quality bar for the CURATED FEATURED set (the default browse
    universe). Same cap guardrails as the Hot List, but Grade A or B and a
    softer score/confidence floor — so it reliably fills ~35 names worth
    surfacing. Strict Hot-List names are a high-conviction subset of this.

    This is deliberately wider than `_is_hot_eligible`: the Hot List is a
    'prime opportunities' signal; the featured set is 'the names we put in front
    of users so attention (and paid AI generation) stays on a small, cached
    pool' instead of scattering across all 547 tickers."""
    mc   = t.get("market_cap")
    tier = t.get("market_cap_tier", "")

    if mc is not None and mc >= MEGA_CAP_CUTOFF:
        return False
    if tier == "Mega Cap":
        return False
    if mc is not None:
        floor = 250e6 if tier in ("Small Cap", "Micro Cap") else MIN_MCAP_FILTER
        if mc < floor:
            return False

    if t.get("pop_score", 0) < 60:
        return False
    if t.get("confidence", 0) < 0.55:
        return False
    if (t.get("grade", "") or "").upper() not in ("A", "B"):
        return False

    return True


# ── MODEL PORTFOLIO helpers ───────────────────────────────────────────────────

# ── Top Hunts construction constants ──────────────────────────────────────
# Hoisted to module level so the initial build (_build_model_portfolio) and
# the daily refill (_replenish_portfolio) share ONE definition of the entry
# bar, size, and diversification cap. They used to each hard-code these
# numbers, which is exactly how two selection paths silently drift apart.
PORTFOLIO_SIZE     = 12   # target number of active Top Hunts picks
MIN_ALPHA_SCORE    = 75   # base entry bar (Bullish / Mixed regimes)
MIN_ALPHA_BEARISH  = 80   # book-level defense: raise the bar in a risk-off tape
MAX_PER_THEME      = 3    # max names from any one THEME (sub-sector) across the book


def _entry_min_score(regime: dict | None = None) -> int:
    """Regime-aware entry floor.

    A world-class long book does not admit names on the same bar in every
    tape — it demands more proof when the macro backdrop is hostile. In a
    Bearish regime we raise the Alpha Score floor to MIN_ALPHA_BEARISH; in
    Bullish / Mixed regimes we use the base MIN_ALPHA_SCORE. This is the
    book-level analogue of the per-ticker regime multiplier already baked
    into smart_score — defense applied at the gate, not just the score.
    """
    try:
        label = (regime or market_regime.get() or {}).get("regime_label", "Mixed")
    except Exception:
        label = "Mixed"
    return MIN_ALPHA_BEARISH if label == "Bearish" else MIN_ALPHA_SCORE


def _theme_of(item: dict) -> str:
    """Concentration key for diversification — the THEME (sub-sector), not the
    coarse sector. This matters: in our universe the `sector` field is only a
    handful of mega-buckets (Technology covers ~140 names), so capping on it
    would be both far too blunt (only 3 of 140 tech names!) AND useless against
    the real risk — 8 'AI Semiconductors' names are all 'Technology'. The
    `sub_sector` taxonomy (AI Semiconductors, Cybersecurity, Data Center REITs,
    …) is the level at which names actually move together, so it's the level we
    diversify across. Falls back to sector, then 'Unknown' (each unknown counts
    on its own — we'd rather under-fill than silently stack unclassified names).
    """
    return (
        (item.get("sub_sector") or item.get("subsector") or item.get("sector") or "")
        .strip()
        or "Unknown"
    )


def _select_with_theme_cap(
    ranked: list[dict],
    size: int,
    max_per_theme: int = MAX_PER_THEME,
    seed_counts: dict[str, int] | None = None,
) -> list[dict]:
    """Greedily take the highest-ranked names while enforcing a per-THEME cap
    — the single biggest gap between a score-ranked list and a real portfolio.
    Without it, 12 momentum names in a semis-led tape become 8 AI-semiconductor
    names, and one theme drawdown trips every stop at once.

    `ranked` MUST already be sorted best-first. `seed_counts` pre-loads theme
    exposure from names already held, so the replenish path enforces the cap
    across the WHOLE book, not just the slice added this cycle.

    Returns up to `size` picks. If theme diversity runs out at the bar it
    returns fewer — we never breach the cap to fill a slot, mirroring the
    'never lower the score bar' discipline elsewhere in this module.
    """
    counts: dict[str, int] = dict(seed_counts or {})
    chosen: list[dict] = []
    skipped_for_cap: list[str] = []
    for t in ranked:
        if len(chosen) >= size:
            break
        theme = _theme_of(t)
        if counts.get(theme, 0) >= max_per_theme:
            skipped_for_cap.append(t.get("ticker", "?"))
            continue
        chosen.append(t)
        counts[theme] = counts.get(theme, 0) + 1
    if skipped_for_cap:
        logger.info(
            f"🛡️  Theme cap (max {max_per_theme}/theme) passed over "
            f"{len(skipped_for_cap)} higher-scored name(s): "
            f"{skipped_for_cap[:8]}"
        )
    return chosen


# ── AI analyst-judge (advisory re-rank) ───────────────────────────────────
# An Opus 4.8 analyst scores conviction for the quant-qualified candidates;
# selection uses it as a tiebreaker WITHIN the shortlist — never to change
# eligibility. Scores are read from the durable cache (selection_store) and
# refreshed ~nightly by _refresh_selection_judgments() (fire-and-forget). When
# the cache is cold (AI off / first run) every conviction is absent and
# selection falls back to the pure quant ordering, unchanged.

_selection_refreshing = False   # single-flight guard for the nightly refresh
# In-process memo so the hot /api/model-portfolio path doesn't hit Supabase on
# every poll — conviction changes only ~nightly, so a short TTL is plenty.
_conv_memo: dict = {"key": None, "at": 0.0, "val": {}}
_CONV_MEMO_TTL = 300   # seconds


def _conviction_map(tickers: list[str]) -> dict[str, dict]:
    """Cached AI judgments for these tickers, keyed by UPPER ticker. Memoised
    for _CONV_MEMO_TTL per ticker-set. Any store error returns {} so selection
    degrades cleanly to quant-only ordering."""
    key = tuple(sorted({(t or "").upper() for t in tickers if t}))
    if not key:
        return {}
    now = time.time()
    if _conv_memo["key"] == key and (now - _conv_memo["at"]) < _CONV_MEMO_TTL:
        return _conv_memo["val"]
    try:
        val = _selstore.get_many(list(key))
    except Exception as e:
        logger.debug(f"_conviction_map error (ignored): {e}")
        val = {}
    _conv_memo.update(key=key, at=now, val=val)
    return val


def _conv_score(ticker: str | None, cmap: dict) -> int:
    """AI conviction (0-100) for a ticker, or -1 when not yet scored. -1 sorts a
    not-yet-scored name to the bottom of its Alpha-Score band — scored names
    lead within the band, but eligibility is never affected."""
    j = cmap.get((ticker or "").upper())
    c = j.get("conviction") if j else None
    return int(c) if isinstance(c, (int, float)) else -1


def _qualified_pool() -> list[dict]:
    """The current quant-eligible candidate set (Grade A + regime-aware Alpha
    Score floor + 4-of-6 pillars), sorted best-first. Shared with the AI refresh
    so the judge scores exactly the names that can actually be selected."""
    def _alpha(t: dict) -> float:
        ss = t.get("smart_score")
        if ss is None:
            ss = t.get("pop_score") or 0
        return float(ss or 0)
    min_score = _entry_min_score()
    pool = [
        t for t in _universe_data
        if t.get("grade") == "A"
        and _alpha(t) >= min_score
        and _pillar_pass_count(t) >= 4
    ]
    pool.sort(key=_alpha, reverse=True)
    return pool


async def _refresh_selection_judgments(force: bool = False) -> None:
    """Fire-and-forget: re-score the qualified pool with the Opus 4.8 judge and
    persist to the cache. Triggered lazily from the portfolio endpoint when the
    cache is stale (≈ daily). Single-flight; never raises into callers."""
    global _selection_refreshing
    if _selection_refreshing:
        return
    if not ai_selector.available() or not _universe_data:
        return
    pool = _qualified_pool()
    if not pool:
        return
    tickers = [t.get("ticker") for t in pool if t.get("ticker")]
    if not force:
        cached = _conviction_map(tickers)
        missing = any((tk or "").upper() not in cached for tk in tickers)
        stale = any(_selstore.is_stale(cached.get((tk or "").upper())) for tk in tickers)
        if not missing and not stale:
            return
    _selection_refreshing = True
    try:
        logger.info(f"🧠 AI selection-judge: scoring {len(pool)} qualified candidates…")
        judgments = await ai_selector.score_candidates(pool)
        if judgments:
            _selstore.save_many(judgments)
            _conv_memo["key"] = None   # invalidate memo so fresh scores show now
            logger.info(f"🧠 AI selection-judge: cached {len(judgments)} conviction scores")
    except Exception as e:
        logger.error(f"_refresh_selection_judgments failed: {e}")
    finally:
        _selection_refreshing = False


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
    # Sized for natural churn: pool at threshold 75 yields ~20-25 qualifying
    # names at any moment, so 12 slots give room for the strongest picks AND
    # enough rotation that fresh names appear as the leaderboard evolves.
    # At threshold 80 only 4 names qualified (May 2026), starving the tracker.
    # PORTFOLIO_SIZE / MIN_ALPHA_SCORE now live as module constants so the
    # replenish path shares them. The active bar is regime-aware: it rises to
    # MIN_ALPHA_BEARISH in a risk-off tape (book-level defense).
    min_score = _entry_min_score()

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
    score_qualified = [t for t in grade_a_pool if _alpha(t) >= min_score]
    # ── Soft 6-pillar veto ────────────────────────────────────────────
    # A stock must hit >= 50 on AT LEAST 4 of the 6 pillars (momentum,
    # growth, quality, valuation, sentiment, growth_potential). This
    # prevents a pure-momentum name with terrible fundamentals or a
    # stretched valuation from being selected just because its composite
    # is high.
    # MANDATORY pillar veto — no fallback. If today yields fewer than
    # PORTFOLIO_SIZE names that pass, the tracker stays smaller. An
    # empty slot is a signal that the market isn't offering enough
    # high-conviction setups today, not an excuse to lower the bar.
    qualified = [t for t in score_qualified if _pillar_pass_count(t) >= 4]
    pool = qualified
    # AI advisory re-rank: within an Alpha-Score band (rounded to the nearest
    # point) order by the analyst-judge's conviction; exact alpha + analyst
    # upside break any remaining ties. When the AI cache is cold every conviction
    # is -1, collapsing this back to the pure quant ordering (alpha, upside).
    cmap = _conviction_map([t.get("ticker") for t in pool])
    pool.sort(
        key=lambda t: (round(_alpha(t)), _conv_score(t.get("ticker"), cmap),
                       _alpha(t), _upside(t)),
        reverse=True,
    )
    # Diversify: take the strongest names subject to MAX_PER_THEME. A pure
    # score-rank slice can return 8 names from one hot theme (e.g. AI
    # Semiconductors); the cap keeps the book from becoming a single-theme bet.
    top20 = _select_with_theme_cap(pool, PORTFOLIO_SIZE)   # name kept for downstream code
    for t in top20:
        t["_entry_tier"] = 1   # all qualified picks are Tier 1 Premium

    new_tickers = {t.get("ticker") for t in top20}

    logger.info(
        f"📊 Model Portfolio: universe={len(_universe_data)} → "
        f"grade A={len(grade_a_pool)} → "
        f"qualified(score≥{min_score})={len(qualified)} → "
        f"theme-capped selected={len(top20)}/{PORTFOLIO_SIZE}"
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
            pop_at_entry      = prev.get("pop_at_entry", round(_alpha(t), 1))
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
            # CRITICAL: use _alpha() (smart_score with pop_score fallback) — same
            # field the selection bar uses. Previously stored pop_score directly
            # which made cards display scores 5-7 points BELOW the actual
            # admission threshold, looking like 65-70 picks were admitted at a
            # 75 bar. They weren't; the display field was just wrong.
            pop_at_entry   = round(_alpha(t), 1)
            grade_at_entry = t.get("grade", "A")

        # Freeze the AI judge's conviction at entry: retained picks keep their
        # original value; new picks capture today's. Lets the closed-trades
        # view later measure conviction-vs-outcome. None when AI hasn't scored.
        if prev and prev.get("conviction_at_entry") is not None:
            conviction_at_entry = prev.get("conviction_at_entry")
        else:
            conviction_at_entry = (cmap.get(ticker.upper()) or {}).get("conviction")

        picks.append({
            "ticker":         ticker,
            "name":           t.get("name", ""),
            "added_date":     added,
            "entry_price":    entry,
            "pop_at_entry":   pop_at_entry,
            "grade_at_entry": grade_at_entry,
            "conviction_at_entry": conviction_at_entry,
            "entry_tier":     prev.get("entry_tier") if prev else t.get("_entry_tier", 3),
            "is_simulated_entry": is_simulated_entry,
            "sector":         t.get("sector", ""),
            "sub_sector":     t.get("sub_sector") or t.get("subsector", ""),
            "rationale":      (t.get("rationale") or "")[:120],
            "signals":        (t.get("signals") or [])[:3],
            "target_mean":    float(t.get("target_mean") or 0),
        })

    # ── Audit trail: every dropped pick MUST be recorded to closed trades ──
    # The user's trust in our selection rests on the closed-trades tab being
    # the complete history of every name we ever picked. So if a rebuild
    # drops a ticker (admin Reset, or a future code path that passes an
    # existing portfolio), record those drops as closed trades here. The
    # normal daily exit path already goes through _close_triggered_picks
    # — this is the safety net for rebuild paths only.
    if existing and existing.get("picks"):
        lookup = {t["ticker"]: t for t in _universe_data}
        existing_tickers = {p["ticker"] for p in existing["picks"] if p.get("ticker")}
        dropped = existing_tickers - new_tickers
        if dropped:
            closed_records = []
            for prev in existing["picks"]:
                tkr = prev.get("ticker")
                if tkr not in dropped:
                    continue
                live = lookup.get(tkr, {})
                entry = float(prev.get("entry_price") or 0)
                exit_price = float(live.get("price") or entry)
                final_pct = round((exit_price - entry) / entry * 100, 2) if entry > 0 else 0
                added_date = prev.get("added_date")
                days_held = 0
                if added_date:
                    try:
                        days_held = (today - _date.fromisoformat(added_date)).days
                    except ValueError:
                        pass
                closed_records.append({
                    "ticker":          tkr,
                    "name":            prev.get("name", ""),
                    "entry_date":      added_date,
                    "entry_price":     entry,
                    "pop_at_entry":    prev.get("pop_at_entry"),
                    "grade_at_entry": prev.get("grade_at_entry"),
                    "conviction_at_entry": prev.get("conviction_at_entry"),
                    "rationale":       prev.get("rationale", ""),
                    "sub_sector":      prev.get("sub_sector", ""),
                    "exit_date":       today_str,
                    "exit_price":      exit_price,
                    "exit_reason":     "rebuild",
                    "exit_label":      "REBUILD",
                    "exit_detail":     "Removed during portfolio rebuild — no longer in top-12 by selection bar",
                    "final_pct":       final_pct,
                    "days_held":       days_held,
                    "won":             final_pct > 0,
                })
            _append_closed_trades(closed_records)
            logger.info(f"📕 Rebuild closed {len(closed_records)} trade(s): {sorted(dropped)}")

    # Preserve the original `created_at` on refresh so the inception date
    # in the header stays meaningful (it's the date the portfolio was first built).
    created_at = (existing or {}).get("created_at", inception_str)
    return {"created_at": created_at, "version": 2, "picks": picks}


def _enrich_model_portfolio(portfolio: dict) -> dict:
    """Add live prices, performance, and the Minervini-style stair-stepped
    trailing-stop exit ruleset.

    EXIT RULES (no fixed take-profit cap — winners run; profit side is the
    trail alone):

      1. Hard stop      → price ≤ entry × 0.92  (-8% from entry, never overridden)
      2. Stair-stepped trailing stop: floor ratchets up as PEAK gain rises.
         Peak ≥ +10%   → floor = entry × 1.00  (break-even, can't lose)
         Peak ≥ +25%   → floor = entry × 1.10  (locks +10%)
         Peak ≥ +50%   → floor = entry × 1.25  (locks +25%)
         Peak ≥ +100%  → floor = entry × 1.50  (locks +50%)
         Peak ≥ +200%  → floor = entry × 2.00  (locks +100%)
         Peak ≥ +300%  → floor = entry × 2.50  (locks +150%)
         Peak ≥ +500%  → floor = entry × 3.50  (locks +250%)
         Floor never falls (peak is monotonic). If `now ≤ floor` → exit.
      3. Stretched valuation → price > 25% above analyst target AND
         (PEG > 4 OR P/E > 80) — no analyst headroom left.
      4. Signal stop    → grade falls below B  OR  Alpha Score < 60.

    Why no take-profit cap: a fixed ceiling (the old +100% rule, and the +20%
    rule before it) systematically sells the fat-tail winners that make
    trend-following profitable. NVDA's 2023 run (+1300%) booked at +100%
    forfeits 92% of the move. Stair-stepping locks progressively more profit
    as the stock proves itself but never sells on an arbitrary round number;
    the -8% hard stop keeps the downside small and fixed. That asymmetry
    (small capped loss, uncapped-then-ratcheted gain) is the entire edge.

    NOTE ON EXPECTED STATS: a 55-65% hit rate with winners 5-10× losers is the
    DESIGN TARGET for this exit shape on US growth names, not a measured result
    for THIS universe. Validate with backtest.py (IC + live-sim) before
    treating those numbers as fact — the fundamental half of the score cannot
    be reconstructed point-in-time, so the backtest is a lower bound.

    The active rule (the one closest to firing) is exposed as `decision_point`
    so the UI can show ONE chip per card instead of a multi-row plan.
    """
    picks_raw = portfolio.get("picks", [])
    lookup = {t["ticker"]: t for t in _universe_data}
    enriched, perfs = [], []
    GRADE_RANK = {"A": 4, "B": 3, "C": 2, "D": 1, "F": 0}

    # AI analyst-judge conviction + thesis for the held picks (cached, read-only
    # here; refreshed nightly elsewhere). Surfaced on each card so the user sees
    # WHY the AI rates a name, not just the quant score.
    _cmap = _conviction_map([p.get("ticker") for p in picks_raw])

    # Tickers that won't ever appear in _universe_data — surface a clear
    # "no live data" state rather than fabricating +0.0% / Score 0 cards.
    # (Happens when a seed pick is a Mega Cap, since the scanner excludes
    # market_cap >= $200B from the tracked universe.)
    out_of_universe = {p["ticker"] for p in picks_raw if p.get("ticker") not in lookup}
    if out_of_universe:
        logger.warning(f"⚠️  Picks not in universe (no live data): {sorted(out_of_universe)}")

    for p in picks_raw:
        live  = lookup.get(p["ticker"], {})
        entry = float(p.get("entry_price") or 0)
        in_universe = bool(live)
        # Has the universe actually populated live data for this ticker on
        # THIS refresh cycle? If not, we must SKIP exit-rule evaluation —
        # falling back `now = entry` makes `now <= trail_floor` evaluate as
        # `entry <= entry = True` whenever the break-even floor (peak >=
        # +10%) is active, which fired phantom trail-stops on every
        # partial-data refresh. That was the source of the '2 picks churn
        # every day' bug.
        live_price = live.get("price")
        has_live_price = live_price is not None and float(live_price) > 0
        now   = float(live_price) if has_live_price else float(entry or 0)
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
        # Only evaluable when we have a real live price — otherwise the
        # fallback to `entry` would NEVER fire the hard stop, but ALSO
        # we shouldn't fire any rule on partial data. Same guard applies
        # to trail / signal / valuation below.
        hard_stop_price = round(entry * 0.92, 2) if entry > 0 else 0
        hard_stop_hit   = has_live_price and entry > 0 and now <= hard_stop_price

        # ── Rule 2: Stair-stepped trailing stop (no fixed take-profit cap) ─
        # As peak gain rises, the trailing floor ratchets up.  The floor is
        # always the HIGHEST tier the peak has unlocked — never falls.
        # Extended rungs at +200/+300/+500 to better discipline extreme
        # winners (a +400% gain on $50 entry locks $200, not just $75).
        if   peak_perf_frac >= 5.00: trail_mult, trail_label = 3.50, "locks +250%"
        elif peak_perf_frac >= 3.00: trail_mult, trail_label = 2.50, "locks +150%"
        elif peak_perf_frac >= 2.00: trail_mult, trail_label = 2.00, "locks +100%"
        elif peak_perf_frac >= 1.00: trail_mult, trail_label = 1.50, "locks +50%"
        elif peak_perf_frac >= 0.50: trail_mult, trail_label = 1.25, "locks +25%"
        elif peak_perf_frac >= 0.25: trail_mult, trail_label = 1.10, "locks +10%"
        elif peak_perf_frac >= 0.10: trail_mult, trail_label = 1.00, "break-even"
        else:                        trail_mult, trail_label = None, None
        trail_floor    = round(entry * trail_mult, 2) if (entry > 0 and trail_mult) else 0
        trail_active   = trail_mult is not None
        # Trail stop only fires on a REAL live price — without this guard,
        # missing-data refreshes set now=entry which trips the break-even
        # floor (entry × 1.0) and incorrectly closes healthy picks.
        trail_stop_hit = has_live_price and trail_active and now <= trail_floor

        # ── Rule 4: Signal stop ───────────────────────────────────────────
        # Use the LIVE grade only if it's actually present. Falling back to
        # grade_at_entry on missing data prevents a phantom signal-exit on
        # tickers whose enrichment hasn't completed yet.
        live_grade = (live.get("grade") or "").strip()
        cur_grade = live_grade if live_grade else (p.get("grade_at_entry") or "A")
        # Use smart_score (the selection score) with pop_score fallback — must
        # match what _alpha() returns or the signal exit + UI score will lie.
        cur_score = float(live.get("smart_score") or live.get("pop_score") or 0)
        # Only fire signal-stop when we have a confirmed live grade or score
        # — otherwise it'd churn picks mid-refresh.
        signal_triggered = bool(live_grade) and (
            GRADE_RANK.get(cur_grade, 0) < GRADE_RANK["B"]
            or (cur_score and cur_score < 60)
        )
        signal_reason = None
        if signal_triggered:
            if GRADE_RANK.get(cur_grade, 0) < GRADE_RANK["B"]:
                signal_reason = f"Grade dropped to {cur_grade}"
            else:
                signal_reason = f"Score dropped to {cur_score:.0f}"

        # ── Rule 5: Stretched-valuation exit (NEW) ────────────────────
        # If the stock has rallied 25%+ past consensus analyst target AND
        # the valuation is priced for perfection (PEG > 4 OR P/E > 80),
        # fire an exit. Disciplined investors take profit when there's
        # literally no analyst left who thinks the stock has headroom.
        target = float(live.get("target_mean") or 0)
        peg    = float(live.get("peg_ratio") or 0)
        pe     = float(live.get("pe_ratio") or 0)
        valuation_stretched = False
        valuation_reason = None
        if entry > 0 and target > 0 and now > target * 1.25:
            if peg > 4 or pe > 80:
                valuation_stretched = True
                valuation_reason = (
                    f"Price ${now:.2f} is {((now/target)-1)*100:.0f}% above "
                    f"analyst target ${target:.2f}"
                    + (f" · PEG {peg:.1f}" if peg > 4 else f" · P/E {pe:.0f}")
                )

        # ── NO FIXED TAKE-PROFIT CAP (reversed Jun 15) ───────────────────
        # The old +100% ceiling booked every double and rotated capital. It
        # was removed because it directly contradicts the stair-stepped trail
        # below — the whole point of which is to let fat-tail winners run while
        # ratcheting a floor underneath them. A hard 2× cap sells exactly the
        # NVDA-type compounders that make trend-following profitable (a +1300%
        # run booked at +100% forfeits 92% of the move). Profit-side exits are
        # now governed solely by the stair-step trail, which locks +50% / +100%
        # / +150% / +250% as the peak proves itself but never sells on an
        # arbitrary round number.
        #
        # NOTE: The "bar_failed" re-vet rule was tried on May 21 and removed
        # the same day. It used pop_score against a 75 threshold, but the
        # SELECTION path uses smart_score — different fields, so picks that
        # cleanly passed entry got evicted on noise. The result was an
        # 11-stock churn storm in a single refresh, which destroyed user
        # confidence in the tracker. Once a pick is in, it stays until one of
        # the four exit rules fires: hard stop, trail stop, valuation
        # stretched, or signal exit (grade<B or score<60). Those are loose
        # enough to let healthy picks run and tight enough to catch genuinely
        # broken ones.
        bar_failed = False
        bar_reason = None

        # ── Determine the EXIT ALERT (priority: protect capital first,
        #    then trail / valuation / signal — winners run via the trail) ──
        exit_alert = None
        if hard_stop_hit:
            exit_alert = {"type": "stop",
                          "label": "STOP HIT",
                          "reason": f"Price ${now:.2f} ≤ -8% stop ${hard_stop_price:.2f}"}
        elif trail_stop_hit:
            exit_alert = {"type": "trail",
                          "label": "TRAIL STOP HIT",
                          "reason": f"Price ${now:.2f} ≤ trail ${trail_floor:.2f} ({trail_label})"}
        elif valuation_stretched:
            exit_alert = {"type": "stretched",
                          "label": "VALUATION STRETCHED",
                          "reason": valuation_reason}
        elif signal_triggered:
            exit_alert = {"type": "signal",
                          "label": "SIGNAL EXIT",
                          "reason": signal_reason}

        # ── Build user-facing decision point — OBSERVATIONAL language ────
        # TickerMover is a research/tracking tool, not an FCA-authorised adviser.
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
        elif days_held < 2:
            # Picks added today or yesterday haven't had a chance to perform.
            # Showing "Score weakening · -0.2%" because of intraday noise is
            # misleading. Use a neutral "just added" label until day 2.
            decision = {
                "tone":   "new",
                "label":  "🆕 Just added · monitoring",
                "detail": "Added recently — performance kicks in after day 2",
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

        # Override decision if the pick is missing from the universe entirely
        # — we can't trust the +0.0%, since `now` was forced to `entry`.
        if not in_universe:
            decision = {
                "tone":   "no-data",
                "label":  "⚠️ No live data",
                "detail": "Not in tracked universe (mega-cap exclusion)",
            }

        perfs.append(perf if in_universe else 0)
        # Mutate the source pick so peak/trail flags persist across calls.
        # The caller (api endpoint) is responsible for cache.save_disk()
        # whenever any pick was modified.
        p["peak_price"]   = peak_price
        p["trail_active"] = trail_active

        # 6-pillar breakdown (uses LIVE data from the universe so the
        # bars reflect the current state of the stock, not entry state).
        pillars = _compute_pillars(live) if in_universe else None

        enriched.append({
            **p,
            "in_universe":       in_universe,
            "pillars":           pillars,
            "current_price":     now,
            "performance_pct":   perf if in_universe else None,
            "days_held":         days_held,
            "current_pop":       live.get("smart_score") or live.get("pop_score"),
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
            # AI analyst-judge (advisory): conviction 0-100, one-line thesis,
            # red flags, and the pillar it leans on. None when not yet scored.
            "ai_conviction":  (_aj := _cmap.get((p.get("ticker") or "").upper()) or {}).get("conviction"),
            "ai_thesis":      _aj.get("thesis"),
            "ai_red_flags":   _aj.get("red_flags") or [],
            "ai_lean":        _aj.get("lean"),
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
# Persistence — moved off ephemeral disk into Supabase (May 22 2026).
#
# Railway's filesystem is wiped on every deploy, so the previous JSON-on-disk
# scheme silently rolled the portfolio back to the committed git state and
# erased the closed-trades log entirely. Both now live in Supabase (see
# db/2026-05-22-portfolio-persistence.sql) so state survives deploys,
# restarts, and container reschedules. `persistence.store` transparently
# falls back to disk if Supabase is unreachable so local dev still works.
from persistence import store as _store


def _load_portfolio_from_disk() -> dict:
    """Read the active portfolio. Name kept for call-site compatibility —
    actually reads from Supabase first, then disk fallback."""
    return _store.load_portfolio()


def _save_portfolio_to_disk(portfolio: dict) -> None:
    """Persist the active portfolio. Writes to Supabase + mirrors to disk."""
    _store.save_portfolio(portfolio)


def _load_trade_history() -> dict:
    """Read closed-trades log. Returns {version, trades:[...]} for backward
    compatibility with the existing call-sites that iterate ``history['trades']``."""
    trades = _store.load_trades()
    return {"version": 2, "trades": trades}


def _append_closed_trades(new_trades: list) -> int:
    """Append closed trades to the audit log. Returns count inserted.
    Use this from exit-handling paths instead of save_trade_history —
    the table is append-only, so we never rewrite the whole list."""
    if not new_trades:
        return 0
    return _store.append_trades(new_trades)


def _save_trade_history(history: dict) -> None:
    """Deprecated. Kept to avoid breaking imports — does nothing because
    closed_trades is append-only via _append_closed_trades(). If you see
    this called from new code, refactor that call-site to use
    _append_closed_trades(list_of_new_trades) directly."""
    logger.debug("_save_trade_history called (deprecated; closed_trades is append-only via _append_closed_trades)")


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
            "conviction_at_entry": raw.get("conviction_at_entry"),
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
        _append_closed_trades(closed)
        portfolio["picks"] = keep
        logger.info(f"📕 Closed {len(closed)} trade(s): {sorted(closed_tickers)}")
    return portfolio, closed


def _replenish_portfolio(portfolio: dict) -> dict:
    """Refill the portfolio back to target size AFTER exits have fired.

    Rule: existing healthy picks NEVER move. We only fill slots opened by
    exit triggers. New picks pulled from the universe must meet the same
    bar as the initial build (Grade A + regime-aware Alpha Score floor +
    4-of-6 pillars) AND respect the per-theme cap across the WHOLE book.
    Each new pick gets today's date so the UI can flag it as NEW for a week.

    If fewer than PORTFOLIO_SIZE stocks meet the criteria today, the portfolio
    stays below target — we never lower the bar (or breach the theme cap) to
    fill slots.
    """
    from datetime import date as _date
    target_size = PORTFOLIO_SIZE   # single source of truth (module constant)
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
    # upside floor. Upside is the tiebreaker for equal-score stocks. The bar
    # is regime-aware (raised in a risk-off tape) — identical to the build.
    min_score = _entry_min_score()
    score_qualified = [
        t for t in _universe_data
        if t.get("grade") == "A"
        and _alpha(t) >= min_score
        and t.get("ticker") not in blocked
    ]
    # MANDATORY pillar veto — must hit >= 4 of 6 pillars. No fallback;
    # if no eligible candidates exist, the portfolio stays below target
    # rather than admit weaker names.
    vetoed = [t for t in score_qualified if _pillar_pass_count(t) >= 4]
    # Same AI advisory re-rank as the initial build: conviction breaks ties
    # within an Alpha-Score band; falls back to (alpha, upside) when the cache
    # is cold.
    cmap = _conviction_map([t.get("ticker") for t in vetoed])
    qualified = sorted(
        vetoed,
        key=lambda t: (round(_alpha(t)), _conv_score(t.get("ticker"), cmap),
                       _alpha(t), _upside(t)),
        reverse=True,
    )

    # ── Daily add cap (May 21 stability fix) ─────────────────────────────
    # Cap how many NEW picks can enter on any single day. Without this, a
    # mass-eviction event (e.g. multiple exits firing at once) refills all
    # the freed slots in a single refresh, which looks like algorithmic
    # instability and breaks user confidence in the tracker. The cap means
    # rotation is visibly gradual.
    DAILY_ADD_CAP = 2
    added_today_n = sum(1 for p in cur_picks if p.get("added_date") == today_str)
    remaining_today = max(0, DAILY_ADD_CAP - added_today_n)
    if remaining_today == 0:
        logger.info(f"📊 Replenish skipped: {added_today_n}/{DAILY_ADD_CAP} daily add cap reached")
        return portfolio

    needed = min(target_size - len(cur_picks), remaining_today)
    # Enforce the per-theme cap ACROSS THE WHOLE BOOK: seed the counter with
    # themes already held so a refill can't push any theme past MAX_PER_THEME.
    # A refill that would breach it is skipped, not downgraded.
    seed_counts: dict[str, int] = {}
    for p in cur_picks:
        theme = _theme_of(p)
        seed_counts[theme] = seed_counts.get(theme, 0) + 1
    to_add = _select_with_theme_cap(qualified, needed, seed_counts=seed_counts)

    added = 0
    for t in to_add:
        price = float(t.get("price") or 0)
        cur_picks.append({
            "ticker":         t.get("ticker", ""),
            "name":           t.get("name", ""),
            "added_date":     today_str,
            "entry_price":    round(price, 2),
            "pop_at_entry":   round(_alpha(t), 1),
            "grade_at_entry": t.get("grade", "A"),
            "conviction_at_entry": (cmap.get((t.get("ticker") or "").upper()) or {}).get("conviction"),
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
        _save_portfolio_to_disk(_model_portfolio)
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
        _save_portfolio_to_disk(_model_portfolio)
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
        if before_n < PORTFOLIO_SIZE:
            _model_portfolio = _replenish_portfolio(_model_portfolio)
            after_n = len(_model_portfolio.get("picks", []))
            if after_n != before_n or state_changed:
                _save_portfolio_to_disk(_model_portfolio)
                cache.save_disk()
                enriched = _enrich_model_portfolio(_model_portfolio)
        elif state_changed:
            # peak/trail values bumped — persist quietly.
            _save_portfolio_to_disk(_model_portfolio)
            cache.save_disk()

    # Keep AI conviction scores fresh (≈ daily). Fire-and-forget + single-flight
    # + staleness-gated, so calling it on every request is cheap and only does
    # real work when the cache has expired.
    if ai_selector.available():
        asyncio.create_task(_refresh_selection_judgments())

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

    # ── AI conviction vs. realized outcome ────────────────────────────────
    # The legitimate, forward-looking validation of the AI re-rank: bucket
    # closed trades by the conviction the judge assigned AT ENTRY, then show
    # hit-rate + avg return per bucket. Over time this answers "did high-
    # conviction picks actually outperform?" — something a historical backtest
    # of the judge cannot (it can't be reconstructed point-in-time).
    def _bucket(c):
        if c is None:
            return None
        try:
            c = float(c)
        except (TypeError, ValueError):
            return None
        if c >= 80:  return "high (80-100)"
        if c >= 60:  return "solid (60-79)"
        return "neutral (<60)"

    conv_buckets: dict[str, dict] = {}
    for t in trades:
        b = _bucket(t.get("conviction_at_entry"))
        if b is None:
            continue
        d = conv_buckets.setdefault(b, {"n": 0, "wins": 0, "sum_pct": 0.0})
        d["n"] += 1
        d["wins"] += 1 if t.get("won") else 0
        d["sum_pct"] += float(t.get("final_pct") or 0)
    by_conviction = {
        b: {
            "trades":   d["n"],
            "hit_rate": round(d["wins"] / d["n"] * 100, 1) if d["n"] else 0,
            "avg_pct":  round(d["sum_pct"] / d["n"], 2) if d["n"] else 0,
        }
        for b, d in conv_buckets.items()
    }
    scored_n = sum(d["n"] for d in conv_buckets.values())

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
        # AI-conviction analytic (empty until conviction-tagged picks close)
        "by_conviction":      by_conviction,
        "conviction_scored":  scored_n,
    }
    return JSONResponse({"trades": trades, "stats": stats})


@app.post("/api/admin/reprice-closed-trades")
async def api_admin_reprice_closed_trades(env_id: int = None):
    """One-shot maintenance: re-price every closed_trade row using the actual
    yfinance close on its entry_date and exit_date. Fixes the rows that were
    backfilled with synthetic entry prices (back-calculated from momentum_1m)
    which produced wildly inflated returns like AAOI +320% and LSCC +121%.

    Idempotent — running twice is a no-op once prices are already real.
    Returns a summary of which rows changed and by how much.

    Auth: open admin endpoint. Lives behind the same trust model as
    /api/model-portfolio/reset (single-user product, no real admin auth).
    """
    import httpx
    if not supabase.enabled or not config.SUPABASE_SERVICE_KEY:
        raise HTTPException(status_code=503, detail="Supabase service key not configured")

    base = config.SUPABASE_URL.rstrip("/")
    hdrs = {
        "apikey":        config.SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
        "Content-Type":  "application/json",
        "Prefer":        "return=representation",
    }

    # Read all closed_trades (optionally scoped to env_id)
    sel_params = {"select": "id,env_id,ticker,entry_date,exit_date,entry_price,exit_price,final_pct"}
    if env_id is not None:
        sel_params["env_id"] = f"eq.{env_id}"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{base}/rest/v1/closed_trades", headers=hdrs, params=sel_params)
        r.raise_for_status()
        rows = r.json()

    # For each unique ticker, fetch yfinance history once. We need ~3mo of
    # daily closes to cover both entry_date and exit_date in most cases.
    tickers = sorted({(row.get("ticker") or "").upper() for row in rows if row.get("ticker")})

    def _fetch_history_for_ticker(sym: str) -> dict:
        """{date_str -> close} for the last 3 months.

        Tries 3 sources in order: (1) the cached price-history blob that
        /api/tracker-chart populates with a 6h TTL, (2) yfinance via the
        Ticker.history API, (3) yfinance via download() as a fallback for
        symbols that 401/429 on the per-ticker API. Returns {} only when
        all three fail."""
        # 1. Reuse the price-history cache populated by /api/tracker-chart
        ck = f"price-history:{sym}:3mo"
        c = cache.get(ck)
        pts = (c or {}).get("points") or []
        if pts:
            return {p["date"]: p["close"] for p in pts}
        # 2. yfinance Ticker.history
        out: dict[str, float] = {}
        try:
            tk = yf.Ticker(sym)
            h = tk.history(period="3mo", interval="1d", auto_adjust=True)
            if h is not None and not h.empty:
                for idx, hrow in h.iterrows():
                    d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
                    close = float(hrow["Close"]) if hrow.get("Close") is not None else None
                    if close is not None and not (math.isnan(close) or math.isinf(close)):
                        out[d] = round(close, 2)
        except Exception as exc:
            logger.warning(f"reprice: Ticker.history({sym}) failed: {exc}")
        # 3. yfinance download() fallback — different code path, often works
        # when Ticker.history hits a 401/429 from Yahoo's per-ticker endpoint.
        if not out:
            try:
                df = yf.download(sym, period="3mo", interval="1d",
                                 auto_adjust=True, progress=False, threads=False)
                if df is not None and not df.empty:
                    # When download() is called with a single ticker the
                    # frame is flat; when multiple it's a multi-index. Handle
                    # both shapes defensively.
                    closes = df["Close"] if "Close" in df.columns else df.get("Adj Close")
                    if closes is not None:
                        for idx, val in closes.items():
                            try:
                                d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
                                v = float(val)
                                if not (math.isnan(v) or math.isinf(v)):
                                    out[d] = round(v, 2)
                            except (TypeError, ValueError):
                                continue
            except Exception as exc:
                logger.warning(f"reprice: yf.download({sym}) failed: {exc}")
        # Cache the result so subsequent re-runs don't re-fetch
        if out:
            pts_to_cache = [{"date": d, "close": v} for d, v in sorted(out.items())]
            cache.set(ck, {"ticker": sym, "period": "3mo", "points": pts_to_cache}, 60 * 60 * 6)
        return out

    histories = await asyncio.to_thread(
        lambda: {s: _fetch_history_for_ticker(s) for s in tickers}
    )

    def _close_on_or_after(hist: dict, target: str) -> float | None:
        if not target or not hist:
            return None
        if target in hist:
            return hist[target]
        # weekend/holiday → use next available trading day
        later = sorted(k for k in hist.keys() if k >= target)
        if later:
            return hist[later[0]]
        # No future data — fall back to most recent prior trading day
        prior = sorted(k for k in hist.keys() if k <= target)
        if prior:
            return hist[prior[-1]]
        return None

    changed = []
    skipped = []
    async with httpx.AsyncClient(timeout=15) as c:
        for row in rows:
            sym = (row.get("ticker") or "").upper()
            h   = histories.get(sym, {})
            new_entry = _close_on_or_after(h, row.get("entry_date"))
            new_exit  = _close_on_or_after(h, row.get("exit_date"))
            if new_entry is None or new_exit is None:
                skipped.append({"id": row["id"], "ticker": sym, "reason": "no_history"})
                continue
            new_pct = round((new_exit / new_entry - 1) * 100, 2) if new_entry > 0 else 0
            old_pct = row.get("final_pct")
            # Skip if already matching (idempotent)
            try:
                if (abs(float(row.get("entry_price") or 0) - new_entry) < 0.005 and
                    abs(float(row.get("exit_price")  or 0) - new_exit)  < 0.005):
                    skipped.append({"id": row["id"], "ticker": sym, "reason": "already_real"})
                    continue
            except (TypeError, ValueError):
                pass
            patch = {
                "entry_price": new_entry,
                "exit_price":  new_exit,
                "final_pct":   new_pct,
                "won":         new_pct > 0,
            }
            pr = await c.patch(
                f"{base}/rest/v1/closed_trades",
                headers=hdrs,
                params={"id": f"eq.{row['id']}"},
                json=patch,
            )
            if pr.status_code >= 400:
                skipped.append({"id": row["id"], "ticker": sym, "reason": f"patch_failed_{pr.status_code}"})
                continue
            changed.append({
                "id": row["id"], "ticker": sym,
                "entry_was": row.get("entry_price"), "entry_now": new_entry,
                "exit_was":  row.get("exit_price"),  "exit_now":  new_exit,
                "pct_was":   old_pct,                "pct_now":   new_pct,
            })

    # Bust the tracker-chart cache so the chart picks up the corrected rows
    cache.delete("tracker-chart:v6") if hasattr(cache, "delete") else None
    return JSONResponse({
        "ok": True,
        "total_rows": len(rows),
        "changed":    len(changed),
        "skipped":    len(skipped),
        "changes":    changed,
        "skips":      skipped,
    })


@app.post("/api/model-portfolio/reset")
async def api_model_portfolio_reset():
    """Rebuild model portfolio with today's top stocks (admin action)."""
    global _model_portfolio
    if not _universe_data:
        raise HTTPException(status_code=503, detail="Universe not loaded yet")
    # Pass the current portfolio as `existing` so any picks dropped during
    # rebuild are recorded to closed trades. The user's track record depends
    # on the closed-trades tab being a complete audit log — every pick we
    # ever surfaced must leave through that tab, not vanish silently.
    _model_portfolio = _build_model_portfolio(existing=_model_portfolio)
    _save_portfolio_to_disk(_model_portfolio)
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
        # Strict tier ONLY — Pop ≥ 70, conf ≥ 70%, Grade A, not Mega Cap.
        # No tier-2 backfill: if today yields 6 high-conviction names, the
        # list shows 6. An empty slot is honest signal that the market
        # isn't offering more setups at our bar.
        tier1 = [t for t in _universe_data if _is_hot_eligible(t)]
        picks = tier1[:limit]

        _daily_hot     = picks
        _daily_hot_date = today_str
        logger.info(f"📋 Hot list: {len(picks)} strict picks for {today_str} (no tier-2 backfill)")

    return JSONResponse(_clean({"hot": _daily_hot, "total_eligible": len(_daily_hot)}))


# ── Curated FEATURED set — the default browse universe ────────────────────────
# Surfaces the top ~FEATURED_N quality names (Alpha-Score-ranked) so user
# attention concentrates on a small, mostly-cached pool. Full 547-ticker universe
# stays reachable via search. Strict Hot-List names lead, then the rest of the
# featured-eligible pool by score. Rebuilt daily (cheap, in-memory) like the hot list.
_daily_featured: list = []
_daily_featured_date: str = ""
_curated_syms_cache: set = set()


def _run_room_rank(t: dict) -> float:
    """Order picks by 'room to run from HERE', not just raw strength: start from
    the Alpha Score, reward upside-to-target + a near-term earnings catalyst, and
    demote names already trading above target or that have gone parabolic (they've
    mostly had their move). Mirrors _runRoomRank in dashboard.html so the client
    headline and the server-curated list agree."""
    s = float(t.get("smart_score") or t.get("pop_score") or 0)

    up = t.get("target_upside_pct")
    if up is None:
        try:
            tm, p = float(t.get("target_mean") or 0), float(t.get("price") or 0)
            up = (tm - p) / p * 100 if tm > 0 and p > 0 else None
        except (TypeError, ValueError):
            up = None
    try:
        if up is not None:
            up = float(up)
            if   up >= 20:  s += 10
            elif up >= 10:  s += 6
            elif up >= 3:   s += 2
            elif up >= -2:  s += 0
            elif up >= -10: s -= 8
            else:           s -= 16   # trading above target → demote
    except (TypeError, ValueError):
        pass

    try:
        m3 = t.get("momentum_3m")
        if m3 is not None:
            m3 = float(m3)
            if   m3 >= 120: s -= 14
            elif m3 >= 70:  s -= 8
            elif m3 >= 40:  s -= 3    # already ran
    except (TypeError, ValueError):
        pass

    try:
        dte = t.get("days_to_earnings")
        if dte is not None and 0 <= int(dte) <= 21:
            s += 3                    # near-term catalyst = a spark
    except (TypeError, ValueError):
        pass

    return s


def _ensure_featured() -> list:
    """Build/refresh the curated featured list (daily, in-memory) and return it.
    Shared by /api/featured and the per-stock model decision so 'curated' means
    the same set everywhere."""
    global _daily_featured, _daily_featured_date, _curated_syms_cache
    from datetime import date as _date
    today_str = str(_date.today())
    limit = config.FEATURED_N
    if _daily_featured_date == today_str and _daily_featured:
        return _daily_featured

    def _score(t):
        return _run_room_rank(t)   # rank for room-to-run, matching the client headline

    def _cap_ok(t):
        mc = t.get("market_cap"); tier = t.get("market_cap_tier", "")
        if mc is not None and mc >= MEGA_CAP_CUTOFF:
            return False
        if tier == "Mega Cap":
            return False
        if mc is not None:
            floor = 250e6 if tier in ("Small Cap", "Micro Cap") else MIN_MCAP_FILTER
            if mc < floor:
                return False
        return True

    ranked   = sorted([t for t in _universe_data if t.get("ticker")], key=_score, reverse=True)
    eligible = [t for t in ranked if _is_featured_eligible(t)]
    hot_syms = {t.get("ticker") for t in eligible if _is_hot_eligible(t)}
    leaders  = [t for t in eligible if t.get("ticker") in hot_syms]
    rest     = [t for t in eligible if t.get("ticker") not in hot_syms]
    pool     = leaders + rest
    if len(pool) < limit:
        have = {t.get("ticker") for t in pool}
        pool += [t for t in ranked if t.get("ticker") not in have and _cap_ok(t)]
    _daily_featured      = pool[:limit]
    _daily_featured_date = today_str
    _curated_syms_cache  = {t.get("ticker") for t in _daily_featured}
    logger.info(f"⭐ Featured set: {len(_daily_featured)} curated names for {today_str} "
                f"({len(hot_syms)} strict hot + {len(eligible)} eligible, backfilled to {limit})")
    return _daily_featured


def _is_curated(sym: str) -> bool:
    """True if the ticker is in today's curated featured set (~35)."""
    try:
        _ensure_featured()
        return (sym or "").upper() in _curated_syms_cache
    except Exception:
        return False


def _is_premium_overview(sym: str) -> bool:
    """True if the ticker is in the elite top slice of the curated set (prime
    names first, then top score) that gets the premium Opus Overview. Stable ~N
    even on days with few strict-prime names. Tune via OVERVIEW_PREMIUM_N."""
    try:
        n = int(getattr(config, "OVERVIEW_PREMIUM_N", 12))
        if n <= 0:
            return False
        feat = _ensure_featured()   # already ordered prime-first, then by score
        top = {t.get("ticker") for t in feat[:n]}
        return (sym or "").upper() in top
    except Exception:
        return False


@app.get("/api/featured")
async def api_featured(n: int = None):
    _ensure_featured()
    # "Prime Opportunities" — the strict high-conviction subset of the pool
    # (same bar as the Hot List). Surfaced with a badge among the featured names.
    prime_syms = [t.get("ticker") for t in _daily_featured if _is_hot_eligible(t)]
    return JSONResponse(_clean({
        "featured": _daily_featured,
        "prime":    prime_syms,
        "count":    len(_daily_featured),
        "prime_count": len(prime_syms),
        "universe_total": len(_universe_data),
    }))


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
async def api_signup(body: _AuthBody, request: Request):
    """Register a new user account.

    Supabase sends the confirmation email via custom SMTP (Resend), so the
    branded email_welcome.html template configured in the Supabase
    dashboard becomes both the "confirm your email" and "welcome to
    TickerMover" message in one. Rate limit is Resend's (3k/month free),
    not Supabase's built-in 3/hour cap.
    """
    # Standard password policy for new accounts: 8+ chars, letters AND numbers.
    pw = body.password or ""
    if len(pw) < 8 or not (any(c.isalpha() for c in pw) and any(c.isdigit() for c in pw)):
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters and include both letters and numbers.",
        )
    # Land the confirmation link on /auth/callback so the session is captured
    # and first-time onboarding (welcome + risk profile) fires.
    redirect_to = f"{request.url.scheme}://{request.url.netloc}/auth/callback"
    result = await supabase.sign_up(body.email, body.password, redirect_to)
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


class _OAuthBody(BaseModel):
    redirect_to: Optional[str] = None

@app.post("/api/auth/oauth/{provider}")
async def api_oauth_start(provider: str, request: Request, body: _OAuthBody | None = None):
    """Start an OAuth flow with an external provider (currently: google).
    Returns {authorize_url} which the frontend should redirect the browser
    to. Supabase handles the provider handshake and returns to our
    /auth/callback page with tokens in the URL hash."""
    if provider not in ("google", "apple", "github", "azure"):
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")
    if not supabase.enabled:
        raise HTTPException(status_code=503, detail="Auth not configured")
    redirect_to = (body.redirect_to if body else None) or f"{request.url.scheme}://{request.url.netloc}/auth/callback"
    url = supabase.oauth_authorize_url(provider, redirect_to)
    if not url:
        raise HTTPException(status_code=503, detail="OAuth URL build failed")
    return JSONResponse({"authorize_url": url})


class _OnSigninBody(BaseModel):
    access_token: str

@app.post("/api/auth/on-signin")
async def api_on_signin(body: _OnSigninBody):
    """Called by the OAuth/magic-link callback page after the browser has
    persisted its tokens. Detects first-time sign-ins (mostly Google OAuth,
    since those skip Supabase's normal Confirm-signup email) and fires our
    branded welcome email via Resend.

    Why this endpoint exists
    ------------------------
    Email+password signups hit /api/auth/signup → Supabase sends the
    Confirm-signup email through custom SMTP (Resend). Google OAuth users
    bypass that path entirely — Supabase trusts Google's email verification
    and never sends a confirmation. Without this endpoint, Google users
    would join TickerMover in silence and never see our welcome.

    "First-time" heuristic
    ----------------------
    We fetch the user from Supabase and compare `created_at` vs
    `last_sign_in_at`. If they're within ~60s of each other, this sign-in
    IS the account-creation event — treat it as a new signup and send the
    welcome. On subsequent sign-ins those timestamps diverge and we skip.

    Failure mode: silent. The frontend doesn't need to know whether we
    sent an email — login proceeds either way.
    """
    if not supabase.enabled:
        return JSONResponse({"ok": False, "reason": "auth-disabled"})

    try:
        import httpx
        from datetime import datetime, timezone

        # Fetch raw user object (not via supabase.get_user, which strips
        # the timestamp fields we need for the first-time check).
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(
                f"{supabase.url}/auth/v1/user",
                headers={
                    "apikey":        supabase.anon_key,
                    "Authorization": f"Bearer {body.access_token}",
                },
            )
            user = r.json() if r.status_code < 400 else {}

        email = user.get("email")
        if not email:
            return JSONResponse({"ok": False, "reason": "no-email"})

        def _parse_ts(s: Optional[str]) -> Optional[datetime]:
            if not s:
                return None
            try:
                # Supabase returns ISO-8601 with trailing Z or +00:00
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except Exception:
                return None

        created    = _parse_ts(user.get("created_at"))
        last_signin = _parse_ts(user.get("last_sign_in_at"))

        # First-time: account was just created (treat <2min gap as "now"
        # to absorb any clock skew between Supabase and our server).
        is_first_time = False
        if created and last_signin:
            is_first_time = abs((last_signin - created).total_seconds()) < 120
        elif created:
            # No last_sign_in_at yet → definitely first sign-in.
            is_first_time = (datetime.now(timezone.utc) - created).total_seconds() < 120

        if not is_first_time:
            return JSONResponse({"ok": True, "welcomed": False, "reason": "returning-user"})

        # Fire welcome (don't block on it).
        asyncio.create_task(send_welcome_email(email))
        logger.info(f"[ON-SIGNIN] First-time user, welcome queued: {email[:4]}***")
        return JSONResponse({"ok": True, "welcomed": True})

    except Exception as exc:
        logger.warning(f"[ON-SIGNIN] non-fatal: {exc}")
        return JSONResponse({"ok": False, "reason": str(exc)})


class _MagicLinkBody(BaseModel):
    email: str
    redirect_to: Optional[str] = None

@app.post("/api/auth/magic-link")
async def api_magic_link(body: _MagicLinkBody, request: Request):
    """Email the user a passwordless one-tap sign-in link. Free via Supabase
    OTP + your configured SMTP (Resend free tier recommended)."""
    if not supabase.enabled:
        raise HTTPException(status_code=503, detail="Auth not configured")
    redirect_to = body.redirect_to or f"{request.url.scheme}://{request.url.netloc}/auth/callback"
    result = await supabase.send_magic_link(body.email, redirect_to)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return JSONResponse(result)


@app.post("/api/auth/resend-confirmation")
async def api_resend_confirmation(body: _MagicLinkBody, request: Request):
    """Resend the sign-up confirmation email to an unconfirmed account."""
    if not supabase.enabled:
        raise HTTPException(status_code=503, detail="Auth not configured")
    redirect_to = body.redirect_to or f"{request.url.scheme}://{request.url.netloc}/auth/callback"
    result = await supabase.resend_confirmation(body.email, redirect_to)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return JSONResponse(result)


@app.get("/auth/callback", response_class=HTMLResponse)
async def auth_callback():
    """OAuth + magic-link return target. Supabase puts the access_token
    and refresh_token in the URL hash (#access_token=...). We can't read
    a hash server-side, so this page is a thin JS shim that parses the
    fragment, stores the session in localStorage in the same shape the
    rest of the app expects, then redirects to /app."""
    return HTMLResponse("""<!doctype html>
<html><head><meta charset="utf-8"><title>Signing you in…</title>
<style>
  body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,sans-serif;
       background:radial-gradient(120% 120% at 50% -10%,rgba(41,112,255,.22),transparent 55%),#0a0e22;color:#cbd5e1;
       display:flex;align-items:center;justify-content:center;height:100vh;flex-direction:column;gap:24px}
  .ring{width:42px;height:42px;border-radius:50%;
        border:3px solid rgba(255,255,255,.14);border-top-color:#2970ff;
        animation:spin 1s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .msg{font-size:14px;color:#94a3b8;letter-spacing:.02em}
</style>
</head><body>
<div class="ring"></div>
<div class="msg">Signing you in…</div>
<script>
(function(){
  // Supabase returns tokens in the URL hash fragment for OAuth + magic link.
  // Parse and persist in the same localStorage keys the app uses.
  const h = window.location.hash.substring(1);
  const p = new URLSearchParams(h);
  const access  = p.get('access_token');
  const refresh = p.get('refresh_token');
  if (access) {
    try {
      localStorage.setItem('ah_token', access);
      if (refresh) localStorage.setItem('ah_refresh', refresh);
    } catch(e) {}
    // Ask the backend whether this is a first-time sign-in (Google OAuth skips
    // Supabase's Confirm-signup email, so this is also where the branded
    // welcome email fires). If it's a new account, flag it so /app runs the
    // welcome + risk-profile onboarding — same as the email-signup path.
    // Capped so a slow/failed call never blocks the redirect for long.
    (function(){
      var done = false;
      function go(){ if (done) return; done = true; window.location.replace('/app'); }
      try {
        fetch('/api/auth/on-signin', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({access_token: access}),
        }).then(function(r){ return r.json(); }).then(function(d){
          if (d && d.welcomed) { try { localStorage.setItem('ah_just_registered','1'); } catch(e){} }
          go();
        }).catch(go);
      } catch(e) { go(); }
      setTimeout(go, 3500);   // hard fallback so we never hang on the spinner
    })();
    return;
  } else {
    // No token — surface the error briefly then bounce home.
    const err = p.get('error_description') || p.get('error') || 'Sign-in failed';
    document.querySelector('.msg').textContent = err;
    setTimeout(() => window.location.replace('/'), 2400);
  }
})();
</script>
</body></html>""")


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
        # Use SITE_ORIGIN (already normalized to the www host that actually
        # serves the app — the apex tickermover.com 404s on app routes).
        # Must also be in Supabase's Redirect URLs allow-list, or Supabase
        # ignores redirect_to and falls back to the Site URL.
        reset_redirect = f"{SITE_ORIGIN}/reset-password"
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
    _email = (result.get("user") or {}).get("email")
    if _email:
        asyncio.create_task(send_password_changed_email(_email))
    return {"ok": True, "message": "Password updated. You can now sign in."}


@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page():
    """Dedicated page that handles the recovery click-through from the
    password-reset email. Self-contained — reads the access_token from
    the URL hash, shows a Set-New-Password form, posts to our API."""
    return HTMLResponse(content=_RESET_PASSWORD_HTML)


# Single-file HTML for the reset-password page. Kept inline so the route
# has no external template dependency and renders instantly. Matches the
# TickerMover brand (white card + blue gradient accent + Inter typography).
_RESET_PASSWORD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reset your password · TickerMover</title>
<meta name="robots" content="noindex,nofollow">
<link rel="icon" href="/favicon.ico">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Inter',-apple-system,sans-serif;color:#0a0a0a;background:#0A0A0A;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;-webkit-font-smoothing:antialiased;
  background-image:radial-gradient(ellipse at top,#0a1a33 0%,#0A0A0A 60%);}
.card{background:#fff;border-radius:18px;padding:0 0 40px;max-width:440px;width:100%;box-shadow:0 24px 60px rgba(0,0,0,.35),0 8px 20px rgba(0,0,0,.2);overflow:hidden}
.accent{height:5px;background:linear-gradient(90deg,#2970FF 0%,#5DB3F1 50%,#0040c1 100%)}
.cardbody{padding:36px 36px 0}
.brand{display:flex;align-items:center;gap:8px;font-size:15px;font-weight:800;color:#0a0a0a;margin-bottom:28px;justify-content:center}
.brand em{font-style:normal;background:linear-gradient(135deg,#2970FF,#0040c1);-webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent;color:#0040c1}
h1{font-size:24px;font-weight:800;letter-spacing:-.02em;margin-bottom:6px;text-align:center}
.sub{color:#64748b;font-size:14.5px;text-align:center;margin-bottom:28px;line-height:1.5}
label{display:block;font-size:12.5px;font-weight:700;color:#475569;letter-spacing:.04em;text-transform:uppercase;margin-bottom:6px;margin-top:14px}
.input-wrap{position:relative}
input[type=password],input[type=text]{width:100%;padding:13px 44px 13px 14px;border:1.5px solid #e2e8f0;border-radius:10px;font-size:15px;font-family:inherit;background:#fff;transition:border-color .15s,box-shadow .15s}
input[type=password]:focus,input[type=text]:focus{outline:none;border-color:#2970FF;box-shadow:0 0 0 3px rgba(41,112,255,.14)}
.toggle{position:absolute;right:12px;top:50%;transform:translateY(-50%);background:none;border:none;color:#94a3b8;font-size:12px;font-weight:600;cursor:pointer;padding:6px 8px;border-radius:6px}
.toggle:hover{color:#0a0a0a;background:#f1f5f9}
.strength{display:flex;gap:4px;margin-top:8px;height:4px}
.strength span{flex:1;background:#e2e8f0;border-radius:2px;transition:background .2s}
.strength.s1 span:nth-child(1){background:#dc2626}
.strength.s2 span:nth-child(-n+2){background:#F5A623}
.strength.s3 span:nth-child(-n+3){background:#F5A623}
.strength.s4 span{background:#15803d}
.strength-label{font-size:11.5px;color:#64748b;font-weight:600;margin-top:6px;letter-spacing:.02em;height:14px}
.strength-label.s1{color:#dc2626}.strength-label.s2{color:#D4860A}.strength-label.s3{color:#9E6308}.strength-label.s4{color:#15803d}
button.submit{width:100%;margin-top:24px;padding:14px;background:linear-gradient(135deg,#2970FF 0%,#0040c1 100%);color:#fff;border:none;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer;font-family:inherit;transition:filter .15s,box-shadow .15s;box-shadow:0 10px 24px rgba(41,112,255,.35)}
button.submit:hover{filter:brightness(1.06)}
button.submit:disabled{opacity:.6;cursor:not-allowed}
.msg{margin-top:18px;padding:11px 14px;border-radius:9px;font-size:13.5px;font-weight:600;line-height:1.45;display:none}
.msg.show{display:block}
.msg.err{background:#fef2f2;color:#991b1b;border:1px solid #fecaca}
.msg.ok{background:#FFF8E5;color:#14532d;border:1px solid #FFE9B0}
.tips{margin-top:18px;font-size:12px;color:#64748b;line-height:1.6;background:#f8fafc;border-radius:9px;padding:12px 14px}
.tips strong{color:#0a0a0a;font-weight:700}
.foot{margin-top:24px;font-size:13px;color:#64748b;text-align:center}
.foot a{color:#2970FF;font-weight:600;text-decoration:none}
.foot a:hover{text-decoration:underline}
.success-icon{width:64px;height:64px;background:#15803d;border-radius:50%;display:flex;align-items:center;justify-content:center;color:#fff;font-size:32px;margin:0 auto 16px;font-weight:700}
.no-token{display:none}
.no-token.show{display:block}
.no-token h1{color:#dc2626}
</style>
</head>
<body>
<div class="card">
  <div class="accent"></div>
  <div class="cardbody">

  <div class="brand">
    <img src="https://www.tickermover.com/static/icons/alpha-logo-bare-64.png" alt="" width="24" height="24" style="display:block;border-radius:6px">
    Ticker<em>Mover</em>
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
    <a href="/app?signin=1" class="submit" style="display:block;text-align:center;text-decoration:none;color:#fff;background:linear-gradient(135deg,#2970FF 0%,#0040c1 100%);border-radius:10px;padding:14px;font-weight:700;margin-top:18px">Back to sign in</a>
    <div class="foot" style="margin-top:18px">Need a new reset link? <a href="/app?signin=1">Go to sign in</a> and click "Forgot password" again.</div>
  </div>

  <!-- Shown after successful reset -->
  <div class="no-token" id="success-view" style="text-align:center">
    <div class="success-icon">✓</div>
    <h1 style="color:#15803d">Password updated</h1>
    <p class="sub">Your new password is set. Redirecting you to the dashboard…</p>
  </div>
  </div>
</div>

<script>
(function(){
  // ── Step 1: extract recovery access_token from URL hash ──
  // Supabase builds links like:
  //   https://tickermover.com/reset-password#access_token=eyJ...&refresh_token=...&type=recovery
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
    beta = _beta_pro_active()
    return JSONResponse({
        "user_id": user["user_id"],
        "email":   user["email"],
        "plan":    "pro" if beta else sub.get("plan", "free"),
        "status":  "active" if beta else sub.get("status", "active"),
        "valid_until": config.BETA_PRO_UNTIL if beta else sub.get("valid_until"),
        "beta_pro": beta,
    })


# ── User preferences (display name + risk profile + onboarding) ──────────────
# Stored in Supabase user_metadata so they follow the user across devices.

class _UserPrefsBody(BaseModel):
    name:         Optional[str]  = None
    risk_profile: Optional[str]  = None
    onboarded:    Optional[bool] = None


@app.get("/api/user/prefs")
async def api_user_prefs_get(user: Optional[dict] = Depends(_current_user),
                             creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Return the current user's saved name / risk profile / onboarding state."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    md = await supabase.get_user_metadata(creds.credentials)
    # Fall back to the OAuth display name (Google populates full_name/name) so
    # Google sign-ups see their name pre-filled in the welcome step.
    name = md.get("name") or md.get("full_name") or md.get("display_name") or ""
    # First name only — the welcome step asks "what should we call you?".
    first = name.split(" ")[0] if isinstance(name, str) else ""
    return JSONResponse({
        "name":         first or (name if isinstance(name, str) else ""),
        "risk_profile": md.get("risk_profile") or "",
        "onboarded":    bool(md.get("onboarded")),
    })


@app.put("/api/user/prefs")
async def api_user_prefs_put(body: _UserPrefsBody,
                             user: Optional[dict] = Depends(_current_user),
                             creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Persist name / risk profile / onboarding state to the user's metadata."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    data: dict = {}
    if body.name is not None:
        data["name"] = body.name.strip()[:60]
    if body.risk_profile is not None and body.risk_profile in ("conservative", "balanced", "aggressive"):
        data["risk_profile"] = body.risk_profile
    if body.onboarded is not None:
        data["onboarded"] = bool(body.onboarded)
    if not data:
        return JSONResponse({"ok": True, "updated": {}})
    md = await supabase.update_user_metadata(creds.credentials, data)
    if isinstance(md, dict) and md.get("error"):
        raise HTTPException(status_code=400, detail=md["error"])
    return JSONResponse({"ok": True, "updated": data})


# ── Account-synced watchlist ────────────────────────────────────────────────
# Stored in the user's Supabase user_metadata (shallow-merged, so it sits
# alongside name/risk_profile without disturbing them). This makes the watchlist
# follow the LOGIN across devices/browsers, instead of the old localStorage-only
# behaviour where a phone started empty. The client keeps a localStorage copy
# too (offline + instant), and merges with the server on load.
class _WatchlistBody(BaseModel):
    entries: list = []


def _valid_ticker(t: str) -> bool:
    t = (t or "").upper().strip()
    return (1 <= len(t) <= 8 and t[0].isalpha()
            and all(c.isalnum() or c in ".-" for c in t))


def _sanitize_watch_entries(raw: list) -> list:
    """Normalise to [{t, at, p}], valid tickers only, deduped, capped at 200."""
    out, seen = [], set()
    for e in (raw or []):
        if isinstance(e, str):
            t, at, p = e, None, None
        elif isinstance(e, dict):
            t = e.get("t") or e.get("ticker") or ""
            at = e.get("at") if isinstance(e.get("at"), (int, float)) else None
            p = e.get("p") if isinstance(e.get("p"), (int, float)) else None
        else:
            continue
        t = str(t).upper().strip()
        if not _valid_ticker(t) or t in seen:
            continue
        seen.add(t)
        out.append({"t": t, "at": at, "p": p})
        if len(out) >= 200:
            break
    return out


@app.get("/api/watchlist")
async def api_watchlist_get(user: Optional[dict] = Depends(_current_user),
                            creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Return the signed-in user's account-synced watchlist as {entries:[{t,at,p}]}.
    Empty list when not authenticated (client falls back to its local copy)."""
    if not user:
        return JSONResponse({"entries": []})
    md = await supabase.get_user_metadata(creds.credentials) or {}
    entries = md.get("watchlist")
    return JSONResponse({"entries": entries if isinstance(entries, list) else []})


@app.put("/api/watchlist")
async def api_watchlist_put(body: _WatchlistBody,
                            user: Optional[dict] = Depends(_current_user),
                            creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Replace the signed-in user's watchlist (sanitized). 401 if not authed."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    clean = _sanitize_watch_entries(body.entries)
    md = await supabase.update_user_metadata(creds.credentials, {"watchlist": clean})
    if isinstance(md, dict) and md.get("error"):
        raise HTTPException(status_code=400, detail=md["error"])
    return JSONResponse({"ok": True, "count": len(clean)})


# ── Account panel: aggregate profile + change password ──────────────────────
@app.get("/api/user/account")
async def api_user_account(user: Optional[dict] = Depends(_current_user),
                           creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Everything the account/profile panel needs in one call: email +
    verification status, display name, and plan/subscription."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    full = await supabase.get_user_full(creds.credentials) or {}
    sub = await supabase.get_subscription(creds.credentials, user["user_id"])
    beta = _beta_pro_active()
    return JSONResponse({
        "email":          full.get("email") or user.get("email"),
        "email_verified": bool(full.get("email_verified")),
        "name":           full.get("name", ""),
        "plan":           "pro" if beta else sub.get("plan", "free"),
        "status":         "active" if beta else sub.get("status", "active"),
        "valid_until":    config.BETA_PRO_UNTIL if beta else sub.get("valid_until"),
        "beta_pro":       beta,
    })


class _ChangePwBody(BaseModel):
    new_password: str


@app.post("/api/user/change-password")
async def api_user_change_password(body: _ChangePwBody,
                                   user: Optional[dict] = Depends(_current_user),
                                   creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Set a new password for the signed-in user (session-authenticated —
    no email round-trip). Supabase PUT /auth/v1/user with the live token."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    pw = body.new_password or ""
    if len(pw) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")
    res = await supabase.update_password(creds.credentials, pw)
    if not isinstance(res, dict) or res.get("error"):
        raise HTTPException(status_code=400, detail=(res or {}).get("error", "Could not update password."))
    _email = (res.get("user") or {}).get("email") or user.get("email")
    if _email:
        asyncio.create_task(send_password_changed_email(_email))
    return JSONResponse({"ok": True})


# ── Admin: measured AI usage / cost attribution ─────────────────────────────
@app.get("/api/admin/usage")
async def api_admin_usage(limit: int = 20000,
                          user: Optional[dict] = Depends(_current_user),
                          creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Aggregated per-call AI cost — by feature, by model, by user. Gated to
    allow-listed (AI_ALLOW_EMAILS) accounts. Powers the monthly cost report."""
    if not user or (user.get("email") or "").lower() not in _AI_ALLOW:
        raise HTTPException(status_code=403, detail="Admin only.")
    import usage_log
    return JSONResponse(usage_log.store.summary(limit=limit))


@app.get("/api/admin/cache-health")
async def api_cache_health(user: Optional[dict] = Depends(_current_user),
                           creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Live 'is the flat-cost guarantee intact?' check. Probes every durable
    cache table for reachability + how many tickers are cached, and reports
    whether the data-layer Volume is persistent. Allow-list gated."""
    if not user or (user.get("email") or "").lower() not in _AI_ALLOW:
        raise HTTPException(status_code=403, detail="Admin only.")
    import httpx
    from config import SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY, CACHE_DISK_FILE
    url = (SUPABASE_URL or "").rstrip("/")
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY or ""
    supa = bool(url and key)
    hdr = {"apikey": key, "Authorization": f"Bearer {key}"}

    def _probe(table: str, extra: dict | None = None) -> dict:
        out: dict = {"reachable": False, "count": None}
        if not supa:
            return out
        params = {"select": "*", "limit": "1"}
        if extra:
            params.update(extra)
        try:
            with httpx.Client(timeout=6) as c:
                r = c.get(f"{url}/rest/v1/{table}",
                          headers={**hdr, "Prefer": "count=exact"}, params=params)
            if r.status_code in (200, 206):
                out["reachable"] = True
                cr = r.headers.get("content-range", "")
                tail = cr.split("/")[-1] if "/" in cr else ""
                out["count"] = int(tail) if tail.isdigit() else None
            else:
                out["status"] = r.status_code
        except Exception as e:
            out["error"] = str(e)[:120]
        return out

    tables = {t: _probe(t) for t in
              ("stock_overview", "stock_research", "stock_compare", "desk_report", "app_kv", "usage")}
    ns_counts = {ns: _probe("app_kv", {"ns": f"eq.{ns}"}).get("count")
                 for ns in ("why_today_v4", "sector_graph", "dependencies", "feedback", "insider", "pdf_narrative")}

    disk = CACHE_DISK_FILE or ""
    volume_persistent = bool(disk) and not disk.startswith("output")
    overview_ok = tables["stock_overview"]["reachable"]
    intact = bool(supa and overview_ok and volume_persistent)

    return JSONResponse({
        "flat_cost_guarantee_intact": intact,
        "supabase_configured": supa,
        "overview_cached_tickers": tables["stock_overview"]["count"],
        "tables": tables,
        "app_kv_namespaces": ns_counts,
        "data_cache_disk_file": disk,
        "data_volume_persistent": volume_persistent,
        "notes": [
            "intact = Supabase configured AND stock_overview reachable AND data volume persistent.",
            "A table with reachable=false re-bills its cache (the same stock) after every redeploy — create it (see _CACHE_TABLE_SQL / store docstrings).",
            "data_volume_persistent=false → the FREE FMP/SEC/candles layer re-fetches the universe on each deploy (no AI cost, but heavy HTTP). Mount a Railway Volume at /data and set CACHE_DISK_FILE=/data/cache_v5.json.",
            "overview_cached_tickers is your fixed-cost ledger: distinct stocks already paid-for this 30-day window (served free to everyone).",
        ],
    })


@app.get("/api/admin/overview-list")
async def api_overview_list(user: Optional[dict] = Depends(_current_user),
                            creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """The actual list of stocks whose AI Overview is currently cached (the
    'fixed-cost ledger'), newest first, with when it was generated and which
    model paid for it. Allow-list gated."""
    if not user or (user.get("email") or "").lower() not in _AI_ALLOW:
        raise HTTPException(status_code=403, detail="Admin only.")
    import httpx
    from overview_store import store as _ov
    from config import SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY
    url = (SUPABASE_URL or "").rstrip("/")
    key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY or ""
    if not (url and key):
        return JSONResponse({"count": 0, "tickers": [], "detail": "Supabase not configured"})
    rows = []
    try:
        with httpx.Client(timeout=8) as c:
            r = c.get(f"{url}/rest/v1/stock_overview",
                      headers={"apikey": key, "Authorization": f"Bearer {key}"},
                      params={"env_id": f"eq.{_ov.env_id}",
                              "select": "ticker,generated_at,model,status",
                              "order": "generated_at.desc", "limit": "1000"})
            if r.status_code == 200:
                rows = r.json()
    except Exception as e:
        return JSONResponse({"count": 0, "tickers": [], "error": str(e)[:120]})
    tickers = [x.get("ticker") for x in rows if x.get("ticker")]
    premium = sum(1 for x in rows if "opus" in (x.get("model") or "").lower())
    return JSONResponse({
        "count": len(tickers),
        "premium_opus": premium,
        "standard_sonnet": len(tickers) - premium,
        "tickers": tickers,
        "rows": rows,
    })


# ── In-app feedback (idle prompt) ───────────────────────────────────────────
class _FeedbackBody(BaseModel):
    rating:  Optional[int] = None        # 1–5 emoji rating
    topic:   Optional[str] = None        # chip: Picks / Speed / Pricing / …
    message: Optional[str] = None
    page:    Optional[str] = None        # document.title at submit time
    path:    Optional[str] = None        # location.pathname + hash


@app.post("/api/feedback")
async def api_feedback(body: _FeedbackBody, request: Request,
                       user: Optional[dict] = Depends(_current_user)):
    """Record a piece of in-app feedback. Anonymous-friendly. Stored append-only
    in the durable KV (app_kv, ns='feedback') under a unique per-submission key,
    so it survives redeploys without needing a dedicated table."""
    import time as _t
    from kv_store import store as _kv
    msg = (body.message or "").strip()
    rating = body.rating if isinstance(body.rating, int) and 1 <= body.rating <= 5 else None
    if not msg and rating is None:
        raise HTTPException(status_code=400, detail="Feedback is empty.")
    uid = (user or {}).get("user_id")
    row = {
        "rating":  rating,
        "topic":   (body.topic or "")[:60],
        "message": msg[:2000],
        "page":    (body.page or "")[:120],
        "path":    (body.path or "")[:200],
        "user_id": uid,
        "email":   (user or {}).get("email"),
        "ua":      (request.headers.get("user-agent") or "")[:200],
        "ts":      int(_t.time()),
    }
    try:
        _kv.set("feedback", f"{uid or 'anon'}:{int(_t.time() * 1000)}", row)
    except Exception as e:
        logger.error(f"feedback save failed: {e}")
    return JSONResponse({"ok": True})


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
        "plan_name":  "TickerMover Pro",
        "interval":   "monthly",
        "enabled":    razorpay.enabled,
    })


@app.post("/api/payment/create-order")
async def api_create_order(request: Request, user: Optional[dict] = Depends(_current_user)):
    """Create a Razorpay order for Pro checkout. Paid plans are billed in INR and
    offered to users in India only; global users stay on the free plan."""
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    # Best-effort geo gate that FAILS OPEN — only blocks when a country is clearly
    # detected AND is not India, so an Indian user is never wrongly blocked when
    # geo is unknown. The reliable backstop is disabling International Payments in
    # the Razorpay dashboard (limits payment to Indian instruments regardless).
    geo = (request.headers.get("CF-IPCountry")
           or request.headers.get("X-Vercel-IP-Country")
           or request.headers.get("X-AppEngine-Country") or "").upper()
    if geo and geo not in ("IN", "XX", "T1"):   # XX / T1 = CF unknown / Tor → allow
        raise HTTPException(status_code=403, detail=(
            "Paid plans are currently available to users in India only. "
            "You can keep using TickerMover on the free plan."))
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
    return HTMLResponse(_legal.render_terms())


@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    return HTMLResponse(_legal.render_privacy())


@app.get("/disclaimer", response_class=HTMLResponse)
async def disclaimer_page():
    return HTMLResponse(_legal.render_disclaimer())


@app.get("/infographics", response_class=HTMLResponse)
async def infographics_page():
    """Daily Top 5 infographic page — pulls from /api/hot, renders 1200x675
    Twitter-card-sized PNG via html2canvas. For sharing on Twitter/Reddit."""
    from pathlib import Path
    return HTMLResponse((Path(__file__).parent / "templates" / "infographics.html").read_text(encoding="utf-8"))


@app.get("/infographics/earnings", response_class=HTMLResponse)
@app.get("/infographics/earnings/{ticker}", response_class=HTMLResponse)
async def earnings_infographic_page(ticker: str = "LITE"):
    """Per-stock A4 portrait tear sheet (1240x1754 px) — header with logo,
    price, Alpha Score, star rating; mini 90-day price chart; Alpha Score
    component breakdown; quarterly Revenue/EPS bars; key metrics grid;
    sector/peer mini-table; full disclaimer footer. Downloadable as PNG
    (html2canvas) or PDF (browser print). Uses /api/universe + /api/earnings-intel
    + /api/price-history. Replaces the older 1200x900 infographic."""
    from pathlib import Path
    return HTMLResponse(
        (Path(__file__).parent / "templates" / "earnings_tearsheet.html").read_text(encoding="utf-8")
    )


# Alias route — also expose as /tearsheet/{ticker} for cleaner share URLs.
@app.get("/tearsheet", response_class=HTMLResponse)
@app.get("/tearsheet/{ticker}", response_class=HTMLResponse)
async def tearsheet_page(ticker: str = "LITE"):
    """Same A4 tear sheet served at the friendlier /tearsheet/{TICKER} URL."""
    from pathlib import Path
    return HTMLResponse(
        (Path(__file__).parent / "templates" / "earnings_tearsheet.html").read_text(encoding="utf-8")
    )


# ── /api/price-history/{ticker} — 90-day daily closes for tear sheet chart ─
@app.get("/api/price-history/{ticker}")
async def api_price_history(ticker: str, period: str = "3mo"):
    """Lightweight close-only price history for the tear sheet chart.
    Returns: {ticker, period, points: [{date, close}, ...]}.
    Cached 6h per (ticker, period). Falls back gracefully if yfinance is
    unavailable (returns empty `points` array)."""
    sym = ticker.upper().strip()
    if not sym or len(sym) > 8:
        raise HTTPException(status_code=400, detail="Bad ticker")
    if period not in ("1mo", "3mo", "6mo", "1y"):
        period = "3mo"

    cache_key = f"price-history:{sym}:{period}"
    cached = cache.get(cache_key)
    if cached is not None:
        return JSONResponse(cached)

    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return JSONResponse({"ticker": sym, "period": period, "points": []})

    def _fetch():
        try:
            tk = yf.Ticker(sym)
            h = tk.history(period=period, interval="1d", auto_adjust=True)
            if h is None or h.empty:
                return []
            out = []
            for idx, row in h.iterrows():
                d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
                close = float(row["Close"]) if row.get("Close") is not None else None
                if close is not None and not (math.isnan(close) or math.isinf(close)):
                    out.append({"date": d, "close": round(close, 2)})
            return out
        except Exception as exc:
            logger.warning(f"price-history fetch {sym} failed: {exc}")
            return []

    pts = await asyncio.to_thread(_fetch)
    payload = {"ticker": sym, "period": period, "points": pts}
    cache.set(cache_key, payload, 60 * 60 * 6)  # 6h
    return JSONResponse(payload)


# ── /api/tracker-chart — Top Hunts portfolio vs SPY vs QQQ (90 days) ──
@app.get("/api/tracker-chart")
async def api_tracker_chart():
    """Daily cumulative-return time series for the Top Hunts portfolio
    compared to SPY (S&P 500) and QQQ (Nasdaq 100). 90-day window.

    Portfolio NAV approximation: equal-weighted, each pick contributes
    its (close_t / entry_close - 1) from entry_date onward. Dates before
    a pick was added simply omit that pick from the average for that day.

    Cached 15 min — the underlying yfinance fetches are also cached for
    6 h each via /api/price-history.
    """
    cache_key = "tracker-chart:v7"  # bump May 23 PM #5 — closed_trades re-priced to real yfinance closes
    cached = cache.get(cache_key)
    if cached is not None:
        return JSONResponse(cached)

    try:
        import yfinance as yf  # type: ignore
    except Exception:
        return JSONResponse({"dates": [], "spy": [], "qqq": [], "tracker": []})

    # Load current model portfolio picks (active)
    active_picks = []
    try:
        active_picks = list((_model_portfolio or {}).get("picks") or [])
    except Exception:
        active_picks = []

    # ALSO load closed trades — historical chart must include positions that
    # have since exited, otherwise the line shows survivorship bias (e.g. a
    # pick stopped out at -8% would disappear from the past 30 days entirely,
    # making the chart look better than the strategy actually performed).
    closed_trades = []
    try:
        closed_trades = _store.load_trades(limit=500) or []
    except Exception:
        closed_trades = []

    # Normalize closed-trades into the same shape pick-loop expects.
    # Skip exit_reason='rebuild' on May 22 only — those were admin closures
    # of synthetic backdated entries, not real positions.
    closed_picks = []
    for t in closed_trades:
        if t.get("exit_reason") == "rebuild" and str(t.get("exit_date")) == "2026-05-22":
            continue
        closed_picks.append({
            "ticker":       t.get("ticker"),
            "entry_date":   str(t.get("entry_date")) if t.get("entry_date") else None,
            "added_date":   str(t.get("entry_date")) if t.get("entry_date") else None,
            "entry_price":  t.get("entry_price"),
            # exit_date / exit_price are the closed-trade-only fields the
            # downstream loop reads to cap the contribution series.
            "exit_date":    str(t.get("exit_date")) if t.get("exit_date") else None,
            "exit_price":   t.get("exit_price"),
        })

    picks = active_picks + closed_picks

    # Tickers to fetch: SPY, QQQ, plus every pick's symbol (active + closed)
    syms = ["SPY", "QQQ"] + [
        (p.get("ticker") or "").upper()
        for p in picks
        if (p.get("ticker") or "").strip()
    ]
    syms = list(dict.fromkeys(syms))  # de-dupe, preserve order

    def _fetch_history(sym: str) -> dict[str, float]:
        """Returns {date_str -> close} for the last 90 days; reuses the
        per-ticker price-history cache so repeat hits are free."""
        ck = f"price-history:{sym}:3mo"
        c = cache.get(ck)
        pts = (c or {}).get("points") or []
        if not pts:
            try:
                tk = yf.Ticker(sym)
                h = tk.history(period="3mo", interval="1d", auto_adjust=True)
                if h is None or h.empty:
                    return {}
                pts = []
                for idx, row in h.iterrows():
                    d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)
                    close = float(row["Close"]) if row.get("Close") is not None else None
                    if close is not None and not (math.isnan(close) or math.isinf(close)):
                        pts.append({"date": d, "close": round(close, 2)})
                cache.set(ck, {"ticker": sym, "period": "3mo", "points": pts}, 60 * 60 * 6)
            except Exception as exc:
                logger.warning(f"tracker-chart history {sym} failed: {exc}")
                return {}
        return {p["date"]: p["close"] for p in pts}

    histories = await asyncio.to_thread(lambda: {s: _fetch_history(s) for s in syms})
    spy_hist = histories.get("SPY", {})
    qqq_hist = histories.get("QQQ", {})
    if not spy_hist or not qqq_hist:
        return JSONResponse({"dates": [], "spy": [], "qqq": [], "tracker": []})

    # Date axis = union of SPY and QQQ dates, sorted
    dates = sorted(set(spy_hist.keys()) | set(qqq_hist.keys()))
    if not dates:
        return JSONResponse({"dates": [], "spy": [], "qqq": [], "tracker": []})

    # Clip the date axis to start at the EARLIEST entry_date across picks
    # so the chart doesn't show a long flat-zero head before any pick was
    # added (which made the line look like it had a fake vertical jump on
    # the day picks first appeared).
    entry_dates = [
        (p.get("entry_date") or p.get("added_date") or "")
        for p in picks
    ]
    entry_dates = [d for d in entry_dates if d]
    if entry_dates:
        earliest = min(entry_dates)
        dates = [d for d in dates if d >= earliest]
        if not dates:
            return JSONResponse({"dates": [], "spy": [], "qqq": [], "tracker": []})

    def pct_series(hist: dict[str, float]) -> list[float]:
        vals = [hist.get(d) for d in dates]
        # Forward-fill missing days from the previous available close
        last = None
        filled = []
        for v in vals:
            if v is not None:
                last = v
            filled.append(last)
        # Drop leading None / find first real close as base
        base = next((v for v in filled if v is not None), None)
        if not base:
            return [0.0] * len(dates)
        return [round(((v if v is not None else base) / base - 1) * 100, 2) for v in filled]

    spy_series = pct_series(spy_hist)
    qqq_series = pct_series(qqq_hist)

    # Tracker: equal-weighted, each pick contributes from its entry_date.
    # KEY INVARIANT (May 23 2026): the chart's pick baseline is the actual
    # yfinance close on the pick's entry_date, NOT the recorded entry_price.
    # Why: many active picks have simulated back-calculated entry prices
    # (is_simulated_entry=true), which would make Day 1 = (real_close /
    # synthetic_entry − 1) ≈ +43% — a fake jump that destroys chart trust.
    # Using the real entry-date close anchors Day 1 = 0% by definition and
    # makes every later day a true price-action gain or loss.
    def _real_entry_close(sym: str, entry_date: str, hist: dict) -> float | None:
        """yfinance close on entry_date, with forward-fill for weekends/
        holidays. Falls back to the first available day after entry_date
        when the entry day itself isn't a trading day."""
        if not entry_date or not hist:
            return None
        if entry_date in hist:
            return hist[entry_date]
        # Forward-fill: next available trading day after entry_date
        later = [k for k in hist.keys() if k >= entry_date]
        if later:
            return hist[min(later)]
        return None

    # Cache resolved entry closes per pick so we don't re-scan history dicts
    # on every chart day.
    pick_baselines: dict[int, float | None] = {}
    for i, p in enumerate(picks):
        sym = (p.get("ticker") or "").upper()
        ed  = p.get("entry_date") or p.get("added_date")
        pick_baselines[i] = _real_entry_close(sym, ed, histories.get(sym, {}))

    # Same treatment for exit_close on closed picks — use yfinance close on
    # exit_date so the realised return matches market reality, not whatever
    # admin value lives in the DB row.
    pick_exit_values: dict[int, float | None] = {}
    for i, p in enumerate(picks):
        sym = (p.get("ticker") or "").upper()
        xd  = p.get("exit_date")
        if xd:
            pick_exit_values[i] = _real_entry_close(sym, xd, histories.get(sym, {}))
        else:
            pick_exit_values[i] = None

    tracker_series = []
    for d in dates:
        contributions = []
        for i, p in enumerate(picks):
            sym = (p.get("ticker") or "").upper()
            h   = histories.get(sym, {})
            entry_date  = p.get("entry_date") or p.get("added_date")
            entry_close = pick_baselines.get(i)
            exit_date   = p.get("exit_date")
            exit_close  = pick_exit_values.get(i)
            if not entry_date or not entry_close:
                continue
            if d < entry_date:
                continue
            # Closed pick on/after exit_date: realised return locks in,
            # using real market exit close (not the admin-recorded value).
            if exit_date and exit_close and d >= exit_date:
                try:
                    pct = (float(exit_close) / float(entry_close) - 1) * 100
                    contributions.append(pct)
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
                continue
            # Still active on this date — compare close-on-d to close-on-entry.
            if not h:
                continue
            close_d = h.get(d)
            if close_d is None:
                prev_dates = [k for k in h.keys() if k <= d]
                if not prev_dates:
                    continue
                close_d = h[max(prev_dates)]
            try:
                pct = (close_d / float(entry_close) - 1) * 100
                contributions.append(pct)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        if contributions:
            tracker_series.append(round(sum(contributions) / len(contributions), 2))
        else:
            # Before any pick was added — anchor at 0
            tracker_series.append(0.0)

    payload = {
        "dates":   dates,
        "spy":     spy_series,
        "qqq":     qqq_series,
        "tracker": tracker_series,
        "picks_count": len(picks),
    }
    cache.set(cache_key, payload, 60 * 15)  # 15 min
    return JSONResponse(payload)
