"""
TickerMover — Configuration  |  tickermover.com
Keys are read from environment variables first, falling back to hardcoded defaults.
On Railway: set each key in the Variables tab — never commit real keys to git.
Locally: edit the fallback strings below, or create a .env file.
"""
import os

# Tracks which keys fell back to a committed default (env var not set) so the app
# can warn at startup that those secrets are unrotated / live-in-repo.
KEYS_ON_FALLBACK: list[str] = []

# ── Brand identity (canonical source) ─────────────────────────────────────────
# Single place to change the product name / domain. New code should reference
# these instead of hardcoding the string, so the next rename is a one-line edit.
APP_NAME      = os.environ.get("APP_NAME", "TickerMover")
APP_DOMAIN    = os.environ.get("APP_DOMAIN", "tickermover.com")
APP_ORIGIN    = os.environ.get("APP_ORIGIN", "https://tickermover.com")  # canonical = apex
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL", "support@tickermover.com")

def _env(key: str, default: str = "") -> str:
    val = os.environ.get(key)
    if val is None and default:
        KEYS_ON_FALLBACK.append(key)
        return default.strip()
    return (val if val is not None else default).strip()

# ── Alpaca Markets — FREE primary price + candle source ──────────────────────
# Free real-time IEX data + unlimited REST calls — no credit card needed.
# Setup (2 min): https://alpaca.markets → Sign Up → Paper Trading → API Keys
# ─────────────────────────────────────────────────────────────────────────────
# NO CREDENTIAL MAY HAVE A HARDCODED DEFAULT.
# Until 30 Aug 2026 six live keys sat here as fallbacks — Alpaca (including the
# broker secret), FMP, Finnhub, sec-api and ApeWisdom. This repository is
# PUBLIC, so they were readable by anyone and are now burnt; they have been
# blanked and must be rotated at the provider.
# A missing key must degrade the feature, never fall back to a shared secret.
# ─────────────────────────────────────────────────────────────────────────────

ALPACA_KEY_ID     = _env("ALPACA_KEY_ID",     "")
ALPACA_SECRET_KEY = _env("ALPACA_SECRET_KEY", "")

# ── Polygon.io — optional paid upgrade ($29/mo Starter) ───────────────────────
# Leave blank to use Alpaca (free) instead. Only upgrade when you have revenue.
POLYGON_API_KEY = _env("POLYGON_API_KEY", "")
POLYGON_PLAN    = _env("POLYGON_PLAN",    "free")  # "free" | "starter" | "realtime"

# ── Financial Modeling Prep — fundamentals + earnings calendar ────────────────
# Starter plan ($19/mo annual): 300 calls/MINUTE, no daily cap, US coverage.
# Free tier was 250/day — too small for a 540-name universe (couldn't cover it
# once/day). Starter is rate-limited per-minute instead, so we throttle on
# FMP_CALLS_PER_MIN and keep FMP_CALLS_PER_DAY only as a high runaway backstop.
FMP_API_KEY       = _env("FMP_API_KEY",       "")
FMP_CALLS_PER_MIN = int(_env("FMP_CALLS_PER_MIN", "280"))      # Starter = 300/min; 280 leaves headroom
FMP_CALLS_PER_DAY = int(_env("FMP_CALLS_PER_DAY", "200000"))   # backstop only — Starter has no daily cap

# ── Finnhub — news + recommendations (supplemental) ──────────────────────────
FINNHUB_KEY = _env("FINNHUB_KEY", "")

# ── Alpha Vantage — fundamentals fallback ────────────────────────────────────
ALPHA_VANTAGE_KEY = _env("ALPHA_VANTAGE_KEY", "")

# ── SEC-API — insider transactions ───────────────────────────────────────────
SEC_API_KEY = _env("SEC_API_KEY", "")

# ── ApeWisdom — social sentiment ─────────────────────────────────────────────
APEWISDOM_KEY = _env("APEWISDOM_KEY", "")

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


# ── AI spend circuit breaker ──────────────────────────────────────────────────
# Hard daily (UTC) ceiling on AI cost. Once today's recorded spend crosses this,
# the background prewarms + the expensive web-search generators STOP generating
# (serve cached/template) so a runaway loop can never bleed past it. Normal days
# now run well under $1; default lowered 12 -> $5 (2026-06-23) as a tight daily
# guard under the $50/month cap. Override with the AI_DAILY_USD_CAP env var.
AI_DAILY_USD_CAP = float(_env("AI_DAILY_USD_CAP", "3") or "3")

# Hard MONTHLY (UTC) ceiling on total AI cost — the real "≤ $X/month no matter
# how many users" guarantee. The daily cap stops a runaway loop; this stops the
# slow user-linear creep (Ask AI is uncached and scales with signups). Once the
# calendar month's recorded spend crosses this, ALL paid AI degrades gracefully
# (Ask returns a capacity notice, prewarms/web-search generators serve cached) —
# unlike the daily cap, this counter is seeded from the usage table so it survives
# redeploys. Resets on the 1st (UTC).
AI_MONTHLY_USD_CAP = float(_env("AI_MONTHLY_USD_CAP", "50") or "50")

# ── Advice-shaped feature switches ────────────────────────────────────────────
# OFF by default since 20 Aug 2026. These are the features that read least like
# research and most like a recommendation on a named security, and they are the
# ones a financial-promotions review would land on first:
#
#   TRADE_PLAN_ENABLED   the Trigger / Target lines drawn on the price chart and
#                        the ATR position sizer. A quality score describes a
#                        company; an entry price, a target and a position size
#                        tell someone what to do with their own money.
#   TRACK_RECORD_PUBLIC  the model-portfolio performance strip under the picks
#                        (hit rate, beat-the-S&P, avg winner/loser). The ledger
#                        itself is untouched and still readable in the Model
#                        Portfolio panel — this only stops it being used as a
#                        marketing proof point on the main surface.
#
# These are SWITCHES, not deletions. Nothing is removed, so if a solicitor
# confirms the position they come back by setting the env var to 1.
TRADE_PLAN_ENABLED  = (_env("TRADE_PLAN_ENABLED",  "0") or "0").strip().lower() in ("1", "true", "yes", "on")
TRACK_RECORD_PUBLIC = (_env("TRACK_RECORD_PUBLIC", "0") or "0").strip().lower() in ("1", "true", "yes", "on")

# MODEL_PORTFOLIO_ENABLED — the Prime Tickers panel and its API. OFF since
# 20 Aug 2026, at the owner's instruction.
#
# This is the biggest single descope on the site. Prime Tickers publishes a
# tracked list of top-10 picks with open and closed positions and a running
# performance record — which is the hardest feature to describe as anything
# other than a recommendation on named securities, and the hardest to reconcile
# with the "generic commentary" characterisation the disclaimer relies on.
#
# Switched off, not deleted: the panel, its nav entry, the command-palette
# entry and the /api/model-portfolio* endpoints all disappear, while the trade
# ledger and every line of code stay in the repo. Setting this to 1 restores
# the feature exactly as it was.
MODEL_PORTFOLIO_ENABLED = (_env("MODEL_PORTFOLIO_ENABLED", "0") or "0").strip().lower() in ("1", "true", "yes", "on")


# ── Pro gating master switch ──────────────────────────────────────────────────
# OFF since 18 Aug 2026: pricing is not decided, so nothing is paywalled and no
# "PRO" badge is shown. This is a SWITCH, not a removal — every gate, badge and
# upgrade path is still in place and comes back by setting PRO_GATING=1, which
# is why the code was not ripped out. Turn it on the day a price exists.
PRO_GATING_ENABLED = (_env("PRO_GATING", "0") or "0").strip().lower() in ("1", "true", "yes", "on")


# ── AI exit-verification: catastrophic capital floor ──────────────────────────
# The Opus exit brain may override a mechanical exit (incl. the -8% stop) and HOLD
# a position. This floor is the one thing it can NEVER override: once a position is
# at or below this loss, the exit is forced regardless of the AI's verdict, so a
# single bad call or hallucination can't let a loss run to ruin. Expressed as a
# negative percent. Set to a very low number (e.g. -100) to effectively disable.
AI_OVERRIDE_FLOOR_PCT = float(_env("AI_OVERRIDE_FLOOR_PCT", "-15") or "-15")

# ── Analytics (cookieless) ────────────────────────────────────────────────────
# Plausible analytics on all PUBLIC pages (landing, login, weekly, desk,
# /stocks/* SEO pages). Cookieless → no consent banner required under UK
# GDPR/PECR. Uses Plausible's new per-site script: the ID below is the
# tickermover.com site created by the owner 2026-07-05. Override with the
# PLAUSIBLE_SCRIPT_ID env var; set it to "off" to disable analytics entirely.
PLAUSIBLE_SCRIPT_ID = _env("PLAUSIBLE_SCRIPT_ID", "off")

# ── Tracker entry: volatility risk gate ───────────────────────────────────────
# Backtest evidence (2026-07): the catastrophic loss tail (QMCO -58%, QUBT -37%,
# SOUN -29%) came exclusively from hyper-volatile names whose daily ATR was so
# large the -8% protective stop was fiction — one average day gaps straight
# through it. Names whose ATR(14) exceeds this fraction of price are excluded
# from PRIME TRACKER ENTRY only (Featured/Best Ideas lists are unaffected —
# they're research surfaces, not tracked positions). A normal growth name runs
# 2-4%/day; 7%+ is lottery-ticket territory where no stop can do its job.
# Missing ATR data does NOT block entry (a data gap must not starve the tracker).
TRACKER_MAX_ATR_PCT = float(_env("TRACKER_MAX_ATR_PCT", "0.07") or "0.07")

# ── AI-verified selection gate ────────────────────────────────────────────────
# A candidate that already cleared the quant bar (Grade A + Alpha floor + pillars)
# is ALSO checked against the Opus conviction it was scored with: if Opus rated it
# below this floor (an explicit "Avoid" despite passing the screen), it is vetoed
# before it ever reaches the owner approval email. None/unscored names pass through
# (the human approval gate still applies). 0-100 scale; set 0 to disable the veto.
AI_SELECT_MIN_CONVICTION = int(_env("AI_SELECT_MIN_CONVICTION", "45") or "45")

# ── Newsletter cadence ────────────────────────────────────────────────────────
# The weekday daily-brief email. PAUSED 2026-06-24 in favour of the weekly
# editorial (the homepage capture now promotes the weekly). The task no-ops while
# this is false; flip DAILY_BRIEF_ENABLED=true to bring the daily brief back.
DAILY_BRIEF_ENABLED = (_env("DAILY_BRIEF_ENABLED", "false") or "false").lower() in ("1", "true", "yes")

# ── Beta: Pro free for everyone until launch ──────────────────────────────────
# During the public beta every signed-in user gets Pro features for free. This
# flips OFF automatically once the date passes (UTC), so paid Pro takes over at
# launch with no code change. Set BETA_PRO_UNTIL="" to disable immediately.
BETA_PRO_UNTIL = _env("BETA_PRO_UNTIL", "2026-08-01")   # ISO date (YYYY-MM-DD), UTC

# ── Trading / Account ─────────────────────────────────────────────────────────
ACCOUNT_SIZE_USD         = float(_env("ACCOUNT_SIZE_USD",         "50000"))
RISK_PER_TRADE_PCT       = float(_env("RISK_PER_TRADE_PCT",       "1.0"))
TOTAL_PORTFOLIO_RISK_PCT = float(_env("TOTAL_PORTFOLIO_RISK_PCT", "6.0"))
MAX_OPEN_POSITIONS       = int(_env("MAX_OPEN_POSITIONS",         "8"))
HOT_LIST_N               = int(_env("HOT_LIST_N",                 "20"))
# Curated "featured" set — the ~35 names we surface as the default browse
# universe so user attention (and the paid AI generation it triggers) concentrates
# on a small, mostly-cached set instead of scattering across all 547 tickers.
# Broader bar than the strict Hot List; full universe still reachable via search.
FEATURED_N               = int(_env("FEATURED_N",                 "35"))
# How many of the curated names (prime-first, then top score) get the premium
# (Opus) Overview. The elite top slice is what users scrutinise most; the rest of
# the featured set + long tail stay on Sonnet. 0 = nobody premium, >=FEATURED_N = all.
OVERVIEW_PREMIUM_N       = int(_env("OVERVIEW_PREMIUM_N",          "0"))
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
# Free tier nominally allows 60/min, but /company-news + /calendar/earnings get
# throttled (429) well below that when fired across the whole universe. 30/min
# keeps us under the real ceiling; news/earnings are slow-moving so the slower
# cadence is invisible to users. Override via env if the plan changes.
FINNHUB_CALLS_PER_MIN = int(_env("FINNHUB_CALLS_PER_MIN", "30"))
AV_CALLS_PER_DAY      = 23

# ── Universe ──────────────────────────────────────────────────────────────────
# fast = ~30 tickers test set
# hg = ~187 curated tech/telecom universe (legacy default)
# indices = only S&P 500 + Nasdaq-100 + Dow 30 (~540 tickers, no curated tech extras)
# expanded = HG + index extras (~540 unique tickers — Phase 2 default)
# "midlarge" (23 Aug 2026): curated + S&P 500 / MidCap 400 / Nasdaq-100 / Dow,
# about 900 names against the 547 "expanded" scored. Index membership is the
# quality bar — the committees test float, liquidity and positive earnings
# before admitting a name. "full" adds SmallCap 600 for ~1,500; hold that
# until the list payload is split, because it is ~1.4KB per scored name.
UNIVERSE_MODE = _env("UNIVERSE_MODE", "midlarge")

# ── Server ────────────────────────────────────────────────────────────────────
# On Railway, PORT is injected automatically — don't hardcode it
HOST = _env("HOST", "0.0.0.0")   # 0.0.0.0 for cloud, 127.0.0.1 for local
PORT = int(_env("PORT", "8000"))
