"""
AlphaHunt — Configuration  |  alphahunt.in
Keys are read from environment variables first, falling back to hardcoded defaults.
On Railway: set each key in the Variables tab — never commit real keys to git.
Locally: edit the fallback strings below, or create a .env file.
"""
import os

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

# ── Alpaca Markets — FREE primary price + candle source ──────────────────────
# Free real-time IEX data + unlimited REST calls — no credit card needed.
# Setup (2 min): https://alpaca.markets → Sign Up → Paper Trading → API Keys
ALPACA_KEY_ID     = _env("ALPACA_KEY_ID",     "PKYUH2YPV5RF5XBMLZZQL3FWMK")
ALPACA_SECRET_KEY = _env("ALPACA_SECRET_KEY", "8xJW7SsL9QibHkvPY34RVpi2n5zkPz2K4mPxMtXsdifk")

# ── Polygon.io — optional paid upgrade ($29/mo Starter) ───────────────────────
# Leave blank to use Alpaca (free) instead. Only upgrade when you have revenue.
POLYGON_API_KEY = _env("POLYGON_API_KEY", "")
POLYGON_PLAN    = _env("POLYGON_PLAN",    "free")  # "free" | "starter" | "realtime"

# ── Financial Modeling Prep — fundamentals + earnings calendar ────────────────
# Starter plan ($19/mo annual): 300 calls/MINUTE, no daily cap, US coverage.
# Free tier was 250/day — too small for a 540-name universe (couldn't cover it
# once/day). Starter is rate-limited per-minute instead, so we throttle on
# FMP_CALLS_PER_MIN and keep FMP_CALLS_PER_DAY only as a high runaway backstop.
FMP_API_KEY       = _env("FMP_API_KEY",       "DMv41skS17GzmwsBb0GZRaG1jgk6MXLY")
FMP_CALLS_PER_MIN = int(_env("FMP_CALLS_PER_MIN", "280"))      # Starter = 300/min; 280 leaves headroom
FMP_CALLS_PER_DAY = int(_env("FMP_CALLS_PER_DAY", "200000"))   # backstop only — Starter has no daily cap

# ── Finnhub — news + recommendations (supplemental) ──────────────────────────
FINNHUB_KEY = _env("FINNHUB_KEY", "d7hrsapr01qu8vfmdcugd7hrsapr01qu8vfmdcv0")

# ── Alpha Vantage — fundamentals fallback ────────────────────────────────────
ALPHA_VANTAGE_KEY = _env("ALPHA_VANTAGE_KEY", "JCP11I7DK8F60G3O")

# ── SEC-API — insider transactions ───────────────────────────────────────────
SEC_API_KEY = _env("SEC_API_KEY", "f1b1df92b9c91864d033b82bd933851dc718912bb58d14298938749fcfb1ac20")

# ── ApeWisdom — social sentiment ─────────────────────────────────────────────
APEWISDOM_KEY = _env("APEWISDOM_KEY", "KNHvbGE_mK4k79IWd6yTvba3fjYz1Gmv")

# ── Supabase — auth + database (needed for SaaS launch) ──────────────────────
SUPABASE_URL      = _env("SUPABASE_URL",      "")
SUPABASE_ANON_KEY = _env("SUPABASE_ANON_KEY", "")
SUPABASE_JWT_SECRET = _env("SUPABASE_JWT_SECRET", "")
# Service role key — required for server-side writes that bypass RLS.
# Used by persistence.py to read/write the Top Hunts portfolio and the
# closed-trades ledger so they survive Railway deploys (the ephemeral
# filesystem wipes them otherwise). Falls back to anon key with a
# warning if not set, but then writes will fail any non-open RLS table.
SUPABASE_SERVICE_KEY = _env("SUPABASE_SERVICE_KEY", "")

# ── Razorpay — payments ───────────────────────────────────────────────────────
RAZORPAY_KEY_ID       = _env("RAZORPAY_KEY_ID",       "")
RAZORPAY_KEY_SECRET   = _env("RAZORPAY_KEY_SECRET",   "")
RAZORPAY_WEBHOOK_SECRET = _env("RAZORPAY_WEBHOOK_SECRET", "")
RAZORPAY_PLAN_ID      = _env("RAZORPAY_PLAN_ID",      "")   # set after first create_plan call

# ── Trading / Account ─────────────────────────────────────────────────────────
ACCOUNT_SIZE_USD         = float(_env("ACCOUNT_SIZE_USD",         "50000"))
RISK_PER_TRADE_PCT       = float(_env("RISK_PER_TRADE_PCT",       "1.0"))
TOTAL_PORTFOLIO_RISK_PCT = float(_env("TOTAL_PORTFOLIO_RISK_PCT", "6.0"))
MAX_OPEN_POSITIONS       = int(_env("MAX_OPEN_POSITIONS",         "8"))
HOT_LIST_N               = int(_env("HOT_LIST_N",                 "20"))
MIN_CONFIDENCE           = float(_env("MIN_CONFIDENCE",           "0.70"))

# ── Cache TTLs (seconds) ──────────────────────────────────────────────────────
CACHE_LIVE_TTL    = 30       # live price quotes     — 30 s
CACHE_NEWS_TTL    = 300      # news/sentiment        — 5 min
CACHE_SOCIAL_TTL  = 300      # ApeWisdom social      — 5 min
CACHE_FUND_TTL    = 86400    # fundamentals          — 24 h  ← was 1h, now 24h so restarts stay warm
CACHE_TECH_TTL    = 86400    # candles/RSI/ATR       — 24 h
CACHE_INSIDER_TTL = 86400    # SEC insider           — 24 h
# Railway: add a Volume mounted at /data → set CACHE_DISK_FILE=/data/cache_v5.json
# This makes the cache survive across Railway deploys (otherwise disk is wiped each time)
_raw_cache_path = _env("CACHE_DISK_FILE", "")
CACHE_DISK_FILE  = _raw_cache_path if _raw_cache_path else "output/cache_v5.json"

# ── Rate Limits ───────────────────────────────────────────────────────────────
FINNHUB_CALLS_PER_MIN = 55
AV_CALLS_PER_DAY      = 23

# ── Universe ──────────────────────────────────────────────────────────────────
# fast = ~30 tickers test set
# hg = ~187 curated tech/telecom universe (legacy default)
# indices = only S&P 500 + Nasdaq-100 + Dow 30 (~540 tickers, no curated tech extras)
# expanded = HG + index extras (~540 unique tickers — Phase 2 default)
UNIVERSE_MODE = _env("UNIVERSE_MODE", "expanded")

# ── Server ────────────────────────────────────────────────────────────────────
# On Railway, PORT is injected automatically — don't hardcode it
HOST = _env("HOST", "0.0.0.0")   # 0.0.0.0 for cloud, 127.0.0.1 for local
PORT = int(_env("PORT", "8000"))
