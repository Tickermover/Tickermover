# Top Hunts persistence — activation + verification

**Problem fixed:** the Top Hunts portfolio + closed-trades audit log lived
on Railway's ephemeral filesystem. Every deploy reset the portfolio to the
git-committed state and wiped the closed-trades log. Picks vanished without
trade records, breaking the "every pick goes through closed trades" audit
promise.

**Fix:** both files moved to Supabase tables (`model_portfolio_state` and
`closed_trades`). The code in this branch reads/writes through them and
mirrors to local disk as a fallback. Once the SQL is run and the service
key is set on Railway, state survives every deploy / restart.

## Activation (one-time)

### Step 1 — run the SQL in Supabase

Open the Supabase SQL Editor for your project and paste the contents of
`db/2026-05-22-portfolio-persistence.sql`. Click **Run**. Idempotent: safe
to re-run if anything fails partway.

What you should see after it succeeds:

```
SELECT id, payload->'picks' as picks FROM model_portfolio_state;
-- id | picks
-- 1  | []
-- 2  | []

SELECT COUNT(*) FROM closed_trades;
-- 0
```

### Step 2 — copy your service-role key

In the Supabase dashboard → **Project Settings** → **API**. Find the
`service_role` key (under "Project API keys"). It starts with `eyJ...`.

⚠️ This key bypasses RLS — never paste it into client-side code, public
docs, or commit it. Server-side env only.

### Step 3 — set Railway env vars

In Railway → your project → **Variables** tab, add **one variable on each
environment**:

| Environment | Variable               | Value              |
|---|---|---|
| Production (`tickermover.com`)   | `SUPABASE_SERVICE_KEY` | the `eyJ...` key |
| Production                    | `TICKERMOVER_ENV`        | `prod`           |
| Dev (Railway preview)         | `SUPABASE_SERVICE_KEY` | the same key     |
| Dev                           | `TICKERMOVER_ENV`        | `dev`            |

(They share one Supabase project but use different `env_id` rows so prod
and dev portfolios don't collide.)

### Step 4 — deploy

Push the branch, Railway redeploys, app boots and:

- Calls `_load_portfolio_from_disk()` → routes to Supabase → finds an
  empty seed row → returns `{}` → app calls `_build_model_portfolio()`
  → picks 12 fresh names → writes the first real state to Supabase.
- Every subsequent exit fires `_append_closed_trades(...)` → INSERTs
  rows into `closed_trades`.

## Verification (after activation)

Run these to **prove** state survives deploys:

### A. Confirm tables exist + are reachable

```bash
curl -sS 'https://lmjymdjmzvfwhlcosiue.supabase.co/rest/v1/model_portfolio_state?select=id&id=eq.1' \
  -H "apikey: $SUPABASE_ANON_KEY" \
  -H "Authorization: Bearer $SUPABASE_ANON_KEY"
# Expected: [{"id":1}]
```

### B. Capture state before forcing a restart

```bash
curl -sS https://web-production-17a78.up.railway.app/api/model-portfolio  | jq '.picks[].ticker' > /tmp/before-picks.txt
curl -sS https://web-production-17a78.up.railway.app/api/model-portfolio/history | jq '.trades[] | "\(.ticker) \(.exit_date)"' > /tmp/before-trades.txt
```

### C. Force a fresh deploy (the failure case)

Push a trivial commit (whitespace change is fine). Railway redeploys.

### D. Capture state after the deploy

```bash
curl -sS https://web-production-17a78.up.railway.app/api/model-portfolio  | jq '.picks[].ticker' > /tmp/after-picks.txt
curl -sS https://web-production-17a78.up.railway.app/api/model-portfolio/history | jq '.trades[] | "\(.ticker) \(.exit_date)"' > /tmp/after-trades.txt
```

### E. Compare

```bash
diff /tmp/before-picks.txt /tmp/after-picks.txt && echo "PASS: picks unchanged"
diff /tmp/before-trades.txt /tmp/after-trades.txt && echo "PASS: trades preserved"
```

Both diffs must be empty. That's the proof.

## What WON'T be in the closed-trades log

I did **not** fabricate a backfill. Here's why the historical entries
you might expect are missing:

- **The 7 `bar_failed` exits on May 21** (MU/CRDO/BE/RKLB/MPWR/MRVL/LITE)
  were caused by a rule I shipped and reverted within hours. The picks
  themselves never really left the portfolio — same names are still in
  the active list today. Re-adding fake "exit and re-add" rows would be
  noise, not history.
- **The 4 picks that vanished on May 22** (WDC, RGTI, EVRG, NVDA) had
  no real exit reason — they were lost to the storage bug, not closed
  by any rule. Recording fabricated exits for them would be dishonest.

The audit log starts clean from the moment the SQL is run + env vars set.
From that point on, every pick that leaves the portfolio has a row in
`closed_trades`. That's a fresh start, but it's an honest one.
