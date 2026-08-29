# 🎯 TickerMover — US Stock Intelligence Dashboard

> **Hunt for Alpha in the US Stock Market**  
> AI-powered Pop Score · IBD-style RS Rating · Blue Dot signals · Real-time Hot List

[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-green)](https://fastapi.tiangolo.com)
[![License](https://img.shields.io/badge/License-Private-red)](LICENSE)

---

## What is TickerMover?

TickerMover is a real-time US stock intelligence platform that aggregates 14 data signals every 5 minutes and compresses them into a single actionable **Pop Score** (0–100). Stocks scoring 68+ (Grade A) surface in the Hot List as strong buy candidates.

### Key Features

| Feature | Free | Pro |
|---------|------|-----|
| 🔥 Hot List (top 20 Grade-A picks) | ✓ | ✓ |
| 📊 Fundamentals Deep-Dive | ✓ | ✓ |
| 🚨 Risk Radar | ✓ | ✓ |
| 📰 AI Catalysts & News | ✓ | ✓ |
| 🗺 Sector Map | ✓ | ✓ |
| 📐 ATR Trade Sizer | ✓ | ✓ |
| ⭐ Personal Watchlist | — | ✓ |
| 📧 Daily Email Alerts | — | ✓ |
| ⚡ Real-time Price Stream | — | ✓ |

---

## Quick Start (Local)

```bash
# 1. Clone
git clone https://github.com/Tickermover/Tickermover.git
cd Tickermover

# 2. Copy env template
cp .env.example .env
# Edit .env with your API keys

# 3. Install dependencies
pip install -r requirements.txt

# 4. Run
uvicorn app:app --host 127.0.0.1 --port 8000

# Or on Windows: double-click TickerMover.pyw
```

Open **http://localhost:8000** for the landing page, or **http://localhost:8000/app** for the dashboard.

---

## Windows One-Click Launcher

Double-click **`TickerMover.pyw`** — it installs dependencies automatically and opens the dashboard in your browser.  
Requires Python 3.10+ ([python.org](https://python.org) — tick "Add to PATH").

---

## API Keys Needed

| Service | Purpose | Free Tier |
|---------|---------|-----------|
| [Polygon.io](https://polygon.io) | Price data + candles | 5 req/min (free) |
| [FMP](https://financialmodelingprep.com) | Fundamentals + earnings | 250 req/day |
| [Finnhub](https://finnhub.io) | News + recommendations | 60 req/min |
| [Supabase](https://supabase.com) | Auth + database | Free tier |
| [Stripe](https://stripe.com) | Payments (UK/global) | Pay per transaction |

Copy `.env.example` to `.env` and fill in your keys.

---

## Architecture

```
tickermover.com
├── templates/landing.html    ← Marketing landing page (/)
├── templates/dashboard.html  ← Main SPA (/app)
├── app.py                    ← FastAPI routes + background refresh
├── auth.py                   ← Supabase JWT auth + watchlist
├── billing.py                ← Stripe Checkout + webhooks
├── polygon_client.py         ← Real-time price data
├── data_coordinator.py       ← Multi-source data aggregation
├── ai_scorer.py              ← 14-component Pop Score engine
├── stock_universe.py         ← 180-stock curated universe
├── config.py                 ← Env-safe configuration
└── static/                   ← PWA manifest + service worker
```

### Pop Score Components (14 signals)

| Component | Weight |
|-----------|--------|
| Price Momentum (1m, 3m, 6m) | 40% |
| Social Velocity (ApeWisdom) | 20% |
| Technical (RSI, ATR, Volume surge) | 20% |
| Fundamentals (EPS growth, FCF, P/E) | 10% |
| Market Cap Fit | 10% |

---

## Deployment (Railway)

```bash
# Set these in Railway Variables tab:
POLYGON_API_KEY=...
FMP_API_KEY=...
SUPABASE_URL=...
SUPABASE_ANON_KEY=...
SUPABASE_JWT_SECRET=...
STRIPE_SECRET_KEY=...
STRIPE_PRICE_ID=...
STRIPE_WEBHOOK_SECRET=...
```

Railway auto-detects `railway.toml` and deploys on push to `main`.

---

## Database Setup (Supabase)

Run `supabase_schema.sql` in your Supabase project SQL Editor once.  
This creates:
- `subscriptions` table (plan, status, valid_until)
- `watchlists` table (user_id, ticker)
- Row Level Security policies
- Auto-create free subscription trigger on signup

---

## License

Private — all rights reserved. Contact support@tickermover.com for licensing.

---

*Not financial advice. TickerMover is a data intelligence tool — always do your own research.*

<!-- deploy survival test 2026-05-22 -->

<!-- persistence survival test #2 -->
