# Remaining Dev/Prod Setup — Action Checklist

Generated 2026-05-04. Use this alongside `DEV_ENV_SETUP.md` (which has the full reasoning).

## Decision: dev URL

You're using the **Railway-generated URL** for dev, not a custom subdomain:

- **Prod**: https://alphahunt.in (Cloudflare → prod Railway service)
- **Dev**: https://web-production-17a78.up.railway.app (dev Railway service, no Cloudflare)

This skips the Cloudflare CNAME step entirely. Trade-offs:
- No Cloudflare WAF / bot protection in front of dev (fine for personal testing — public dev URLs typically don't need it)
- The Railway URL is stable as long as the dev Railway project exists. If you ever delete-and-recreate the dev service, you'll get a new URL and have to update the Stripe webhook + Supabase Site URL.

## What's already done

- [x] **Part 1** — `dev` git branch exists locally (`6587b8e`, 1 commit ahead of `main`)
- [x] **Part 2** — Dev Supabase project created and schema applied
- [x] **Part 4a-c** — Dev Railway project created and configured to deploy from `dev` branch
- [x] **Part 4d** — Dev Railway URL generated: `web-production-17a78.up.railway.app`
- [x] **Folder reorg** — dev/prod docs separated under `docs/dev` and `docs/prod`
- [x] **Token leak fix** — GitHub PAT removed from `.git/config` and `push_to_github.bat`
- [x] **Workflow scripts** — `scripts/push.bat`, `promote-to-prod.bat`, `rollback-prod.bat`, `repair-git.bat`

## What's left (in execution order)

---

### Step 0 — Local cleanup (one-time, ~5 min)

These have to run on your Windows machine:

1. **Revoke the leaked token.** Open https://github.com/settings/tokens, find the token starting with `ghp_IGSMc4l1...`, and revoke it. It was committed in the old `push_to_github.bat`, so it's already in your repo history and discoverable.

2. **Generate a new GitHub PAT.** Same page → Generate new token (classic) → scope: `repo` → 90-day expiry → copy it.

3. **Save the token to your environment.** Open `cmd` and run:
   ```bat
   setx GH_TOKEN "ghp_yourNewTokenHere"
   ```
   Close that terminal. Open a fresh one — `echo %GH_TOKEN%` should show the token.

4. **Clear the stale git lock.** Open the project folder in cmd and run:
   ```bat
   scripts\repair-git.bat
   ```
   Confirm it shows `dev` as your current branch with the reorg changes pending.

5. **Commit and push the reorg.** Still in cmd:
   ```bat
   scripts\push.bat "chore: reorg docs into dev/prod, replace destructive push script"
   ```
   Verify on GitHub that `dev` branch now has this commit and `main` does NOT (still on `4a1149b`).

---

### Step 1 — Verify dev Railway env vars are wired up — ~5 min

Open https://railway.app → `alphahunt-dev` project → Variables. Confirm these point to dev (not prod):

| Variable | Should be |
|---|---|
| `SUPABASE_URL` | dev Supabase URL (different from prod's) |
| `SUPABASE_ANON_KEY` | dev anon key |
| `SUPABASE_JWT_SECRET` | dev JWT secret |
| `SITE_ORIGIN` | `https://web-production-17a78.up.railway.app` |
| `RESEND_API_KEY` | dev Resend key (if you separated them) |

Variables that should match prod (read-only data feeds, no point splitting):
- `FINNHUB_KEY`, `FMP_API_KEY`, `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, `POLYGON_API_KEY` (if used)

If any of these are missing or wrong, fix them now. Railway redeploys automatically (~60s).

---

### Step 2 — Update Supabase dev project Site URL — ~2 min

Critical — magic-link / password-reset emails won't work otherwise.

1. https://supabase.com → dev project (`alphahunt-dev`) → Authentication → URL Configuration
2. **Site URL**: `https://web-production-17a78.up.railway.app`
3. **Redirect URLs**: add the same URL (and any callback paths your app uses, e.g. `https://web-production-17a78.up.railway.app/**`)
4. Save

---

### Step 4 — Resend dev API key (Part 6) — ~3 min

Likely already partly done if you set up dev SMTP in Part 2d. Verify:

1. https://resend.com → API Keys
2. You should see two keys: prod + a separate one for dev
3. If only one exists, click **Create API Key** → name `alphahunt-dev` → Domain access: `alphahunt.in` → Permission: `Sending access`
4. **In dev Railway → Variables** confirm `RESEND_API_KEY` matches the dev key (NOT prod's)
5. Test: trigger a password reset on https://web-production-17a78.up.railway.app → email should arrive from `noreply@alphahunt.in`. Resend dashboard should show the send under the dev API key.

---

### Step 5 — Final verification (Part 7) — ~10 min

Run the full workflow once end-to-end:

1. **Edit something trivial on dev**
   ```bat
   git checkout dev
   ```
   Change one visible word in `templates/landing.html` (or wherever).

2. **Push to dev**
   ```bat
   scripts\push.bat "test: dev pipeline check"
   ```
   Wait ~90s. Visit https://web-production-17a78.up.railway.app — change should be live. Prod (https://alphahunt.in) should be unchanged.

3. **Verify the network error from your earlier screenshot is gone.** With Supabase Site URL set (Step 2) and dev env vars wired (Step 1), the signup modal's "Network error — try again" should resolve.

4. **Promote to prod**
   ```bat
   scripts\promote-to-prod.bat
   ```
   Type `YES` when prompted. Wait ~90s. Visit https://alphahunt.in — change should now be live.

5. **Smoke test prod**
   - Sign up flow works
   - Existing prod user can still log in
   - Pro upgrade flow works (live Stripe)
   - Password reset email arrives

6. **Roll back drill** (optional, do it once when prod is calm)
   ```bat
   scripts\rollback-prod.bat
   ```
   Type `ROLLBACK`. Verify alphahunt.in goes back. Re-promote with `scripts\promote-to-prod.bat`.

---

### Step 6 — GitHub branch protection (recommended, ~2 min)

Stops accidental direct pushes to `main`:

1. https://github.com/Tickermover/Tickermover → Settings → Branches → **Add branch protection rule**
2. Branch name pattern: `main`
3. Tick **Require a pull request before merging**
4. Save

The only way to update `main` becomes the `promote-to-prod.bat` script.

---

## Reference: which env vars live where

| Variable | Prod Railway | Dev Railway |
|---|---|---|
| `SUPABASE_URL` | prod Supabase URL | dev Supabase URL |
| `SUPABASE_ANON_KEY` | prod anon | dev anon |
| `SUPABASE_JWT_SECRET` | prod jwt | dev jwt |
| `SITE_ORIGIN` | `https://alphahunt.in` | `https://web-production-17a78.up.railway.app` |
| `STRIPE_SECRET_KEY` | live `sk_live_...` | test `sk_test_...` |
| `STRIPE_PRICE_ID` | live price | test price |
| `STRIPE_WEBHOOK_SECRET` | live webhook | test webhook |
| `RESEND_API_KEY` | prod Resend key | dev Resend key |
| `FINNHUB_KEY` | same | same |
| `FMP_API_KEY` | same | same |
| `ALPACA_KEY_ID` | same | same |
| `ALPACA_SECRET_KEY` | same | same |
| `POLYGON_API_KEY` (if used) | same | same |

## Daily workflow (after setup is done)

```bat
:: Start of session
git checkout dev
git pull origin dev

:: ... edit code ...

:: Push to dev — auto-deploys to web-production-17a78.up.railway.app
scripts\push.bat "feat: what changed"

:: Test on https://web-production-17a78.up.railway.app — break things, fix things
:: When happy:
scripts\promote-to-prod.bat
:: Type YES — auto-deploys to alphahunt.in
```

That's it. Prod stays untouched until you explicitly promote.
