# Railway — production variables

Paste into **Variables → Raw Editor** on the `web` service. Railway accepts a
whole `KEY=VALUE` block at once.

`<...>` = fill in. Everything else is a deliberate value, not a placeholder.

---

## 1. Environment — SET THIS FIRST

```
TICKERMOVER_ENV=prod
```

Without it the app falls through to **dev** and writes production data into the
dev namespace. Silent: no error, no warning. `RAILWAY_ENVIRONMENT` is only a
fallback and is not reliable on a fresh project.

## 2. Identity

```
SITE_ORIGIN=https://tickermover.com
SUPPORT_EMAIL=support@tickermover.com
PRIME_REVIEW_EMAIL=support@tickermover.com
PRO_ALLOW_EMAILS=support@tickermover.com
SEC_EDGAR_UA=TickerMover research support@tickermover.com
EMAIL_FROM=TickerMover <noreply@tickermover.com>
```

## 3. Legal — required before paid launch

Sole trader: leave the company number empty. The address is a Companies Act
2006 Part 41 disclosure duty, not optional.

```
LEGAL_ENTITY_NAME=Mousumi Kheto trading as TickerMover
LEGAL_COMPANY_NUMBER=
LEGAL_ADDRESS=<her trading address>
LEGAL_CONTACT_EMAIL=support@tickermover.com
LEGAL_JURISDICTION=England and Wales
```

## 4. Cost controls — the biggest lever

Was `50`/`3`. AI spend dominates the bill; these are hard ceilings.

```
AI_MONTHLY_USD_CAP=15
AI_DAILY_USD_CAP=1
```

Prewarm, scaled to a 4-user audience (defaults were 150/120/60/40):

```
OVERVIEW_PREWARM_N=25
DEPS_PREWARM_N=20
FACTCHECK_PREWARM_N=15
BRIEF_PREWARM_N=10
```

Analytics: Plausible is ~$9/mo. Off, pending the free Cloudflare swap.

```
PLAUSIBLE_SCRIPT_ID=off
```

Polygon stays empty — it falls back to yfinance for free. Do not set a key.

```
POLYGON_API_KEY=
POLYGON_PLAN=free
```

## 5. Secrets — new accounts in her name

Create each account under her email + Tide card BEFORE pasting, so no key here
belongs to the old identity.

```
SUPABASE_URL=<...>
SUPABASE_ANON_KEY=<...>
SUPABASE_JWT_SECRET=<...>
SUPABASE_SERVICE_KEY=<...>
ANTHROPIC_API_KEY=<...>
GROQ_API_KEY=<...>
RESEND_API_KEY=<...>
FMP_API_KEY=<...>
FINNHUB_KEY=<...>
ALPACA_KEY_ID=<...>
ALPACA_SECRET_KEY=<...>
ALPHA_VANTAGE_KEY=<...>
SERPER_API_KEY=<...>
BRAVE_API_KEY=<...>
TAVILY_API_KEY=<...>
VOYAGE_API_KEY=<...>
UNSPLASH_ACCESS_KEY=<...>
PEXELS_API_KEY=<...>
```

### One value to CARRY OVER unchanged

```
REFERRAL_SALT=<copy from the old Railway project>
```

It derives existing users' referral codes. A new salt invalidates every code
already shared.

### Leave unset

`ALLOW_UNSIGNED_WEBHOOKS` — dev only. Never in production.

---

## After pasting

1. **Merge `dev` into `main`.** Railway deploys `main`, which is 53 commits
   behind — the Razorpay removal and email cleanup are not live until you do.
2. Redeploy, then check `/api/status` returns 200.
3. Check `/terms` no longer says "business details pending".
4. Confirm the boot log does NOT warn about Supabase falling back to local JSON.
5. Point `tickermover.com` DNS at the new service, last.

Poll until the service is **stable**, not until the first success — the restart
window looks broken before it settles.
