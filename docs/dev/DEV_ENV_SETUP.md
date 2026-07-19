# AlphaHunt — Dev/Prod Environment Setup

Full-isolation setup. Dev environment is a complete mirror of production with its own database, auth, payment system, and URL. Nothing you do in Dev can ever affect a real user.

**End state:**

| Layer | Production | Development |
|---|---|---|
| Branch | `main` | `dev` |
| Domain | `alphahunt.in` | `dev.alphahunt.in` |
| Railway project | "AlphaHunt" (existing) | "AlphaHunt Dev" (new) |
| Supabase project | existing prod project | new dev project |
| Auto-deploys from | `main` branch | `dev` branch |
| Razorpay mode | Live keys | Test keys |
| Resend domain | `alphahunt.in` (production sender) | Same domain, separate API key |

**Workflow once setup:**
1. Make changes locally on Windows
2. Commit + push to `dev` branch → Railway auto-deploys to `dev.alphahunt.in`
3. Test on `dev.alphahunt.in` — break things, fix things, validate
4. When stable, merge `dev` → `main` → Railway auto-deploys to `alphahunt.in`
5. Production users never see broken code

---

## Cost summary

| Service | Cost |
|---|---|
| Railway dev project | ~$5/mo (covered by Railway's free $5 credit until you scale) |
| Supabase dev project | $0 (free tier: 500MB DB, 50,000 monthly active users — plenty for testing) |
| Cloudflare subdomain | $0 |
| Razorpay test mode | $0 |
| Resend additional API key | $0 (still under 3,000/mo free tier) |
| **Total extra** | **~$5/mo** |

---

## Part 1 — Git: create dev branch (5 min)

On your Windows machine:

```bash
cd "C:\Users\SOURA\Documents\Claude\Projects\USA Stock Market\TickerMover"

# Make sure main is up to date
git checkout main
git pull origin main

# Create dev branch FROM main (so dev starts identical to prod)
git checkout -b dev
git push -u origin dev
```

Now you have two branches on GitHub:
- `main` — production, protected (don't push directly)
- `dev` — your daily working branch

**Optional but recommended — protect main:**
- GitHub repo → Settings → Branches → **Add branch protection rule** for `main`
- Tick "Require a pull request before merging"
- Now you can't accidentally `git push origin main`. You must merge from dev via PR.

---

## Part 2 — Supabase: create dev project (15 min)

### 2a. Create new project

1. Go to [supabase.com/dashboard](https://supabase.com/dashboard)
2. Click **New Project**
3. Name: `alphahunt-dev` (or `AlphaHunt Dev`)
4. Database password: generate a strong one, save it in your password manager
5. Region: pick same as prod (probably Asia/Mumbai or Singapore — match your users)
6. Plan: **Free** tier is fine for dev
7. Wait ~2 minutes for project to provision

### 2b. Replicate the schema from prod

1. In **prod** Supabase project → SQL Editor → run:
   ```sql
   -- Get the schema as SQL
   SELECT pg_dump(...);
   ```
   Actually easier method: **Database → Backups → Download** the latest snapshot, OR
   
   **Even easier**: Database → Migrations → if you've been using Supabase migrations, just re-apply them in dev. If not, manually recreate tables in dev SQL Editor.

2. The tables you definitely need in dev (from looking at your auth.py and app.py):
   - `subscriptions` — for Razorpay subscription tracking
   - `watchlists` — for user watchlists
   - Whatever else your app reads/writes

3. **Important: copy Row Level Security (RLS) policies.** Easy to forget — without RLS in dev, anyone can read everything. Replicate exactly.

### 2c. Configure dev project URL settings

In **dev** Supabase → Authentication → URL Configuration:

- **Site URL**: `https://dev.alphahunt.in/reset-password` (same workaround pattern)
- **Redirect URLs** allow-list: 
  - `https://dev.alphahunt.in/**`
  - `https://dev.alphahunt.in/reset-password`
  - `http://localhost:8000/**` (for running locally without deploying)
  - `http://localhost:8000/reset-password`

### 2d. Configure dev SMTP (Resend)

1. In Resend dashboard → API Keys → Create new key named `supabase-dev`
2. Copy the key
3. In dev Supabase → Project Settings → Auth → SMTP Settings → Enable Custom SMTP:
   - Host: `smtp.resend.com`
   - Port: `465`
   - Username: `resend`
   - Password: your `supabase-dev` API key
   - Sender email: `noreply@alphahunt.in` (same domain works for both)
   - Sender name: `AlphaHunt (Dev)`
   
   The "(Dev)" in sender name helps you spot dev emails in your inbox.

### 2e. Save the dev credentials

You'll need these for Railway env vars in Part 4. Find them in dev Supabase → Project Settings → API:
- `SUPABASE_URL` (looks like `https://xxxxx.supabase.co`)
- `SUPABASE_ANON_KEY` (long JWT)
- `SUPABASE_JWT_SECRET` (under "JWT Settings")
- `SUPABASE_SERVICE_KEY` (if you use it for admin operations)

---

## Part 3 — Cloudflare: add dev subdomain (5 min, then 10 min DNS wait)

You'll point `dev.alphahunt.in` at the new Railway dev project (which you'll create in Part 4 — but DNS can be set up first since propagation takes time).

In Cloudflare → DNS → Records → **Add record**:

| Type | Name | Content | Proxy | TTL |
|---|---|---|---|---|
| CNAME | `dev` | (Railway dev URL — get this in Part 4 step 4d) | Proxied | Auto |

For now, leave the Content blank or use a placeholder. You'll fill it in once Railway gives you the URL.

---

## Part 4 — Railway: create dev project (15 min)

### 4a. Create new project

1. [railway.app](https://railway.app) → **New Project**
2. Pick **Deploy from GitHub repo**
3. Select your AlphaHunt repo (same one as prod)
4. Project name: `alphahunt-dev`

### 4b. Configure to deploy from `dev` branch

This is the critical step that makes Dev separate from Prod:

1. In the new Railway project → click your service
2. **Settings** tab → **Source** section
3. Branch: change from `main` to `dev`
4. Save

Now this Railway project will only auto-deploy when you push to the `dev` branch.

### 4c. Copy environment variables from prod

The fastest way:

1. Open your **prod** Railway project in another tab → Variables → click **Raw Editor** → copy the entire JSON
2. In **dev** Railway project → Variables → Raw Editor → paste
3. Now SWAP the following variables to use dev versions:

| Variable | Change to dev value |
|---|---|
| `SUPABASE_URL` | dev Supabase URL (from Part 2e) |
| `SUPABASE_ANON_KEY` | dev anon key |
| `SUPABASE_JWT_SECRET` | dev JWT secret |
| `SITE_ORIGIN` | `https://dev.alphahunt.in` |
| `RAZORPAY_KEY_ID` | Razorpay TEST mode key (see Part 5) |
| `RAZORPAY_KEY_SECRET` | Razorpay TEST mode secret |
| `RAZORPAY_WEBHOOK_SECRET` | Razorpay TEST webhook secret |
| `RESEND_API_KEY` (if you have one) | the `supabase-dev` key from Part 2d |

Variables to KEEP SAME (data providers — read-only, cheap, no point splitting):
- `FINNHUB_API_KEY`
- `FMP_API_KEY` 
- `ALPACA_API_KEY` + `ALPACA_API_SECRET`
- `SEC_API_KEY`
- `ALPHAVANTAGE_API_KEY`

### 4d. Generate Railway URL + connect Cloudflare

1. Dev Railway project → Settings → **Networking** → **Generate Domain**
2. Railway gives you something like `alphahunt-dev-production.up.railway.app`
3. Then click **+ Custom Domain** → enter `dev.alphahunt.in`
4. Railway shows you a CNAME target — copy that
5. Go back to **Cloudflare DNS** → edit the `dev` CNAME from Part 3
6. Set Content = the Railway CNAME target
7. Wait 5-10 min for DNS propagation
8. Visit `https://dev.alphahunt.in` — should show your dashboard

### 4e. Trigger first deploy

The very first deploy might not auto-trigger:

1. Dev Railway → Deployments tab → **Deploy** button (top right)
2. Or push a tiny change to dev branch:
   ```bash
   git checkout dev
   echo "# dev environment" >> README.md
   git commit -am "trigger first dev deploy"
   git push origin dev
   ```
3. Watch the deploy logs in Railway — should be green within 60-90 sec

---

## Part 5 — Razorpay: enable test mode (5 min)

Razorpay has a built-in test mode with separate keys — no payments are real, you can simulate the whole flow.

1. Login to [dashboard.razorpay.com](https://dashboard.razorpay.com)
2. Top-right toggle: switch from **Live Mode** to **Test Mode**
3. Settings → API Keys → **Generate Test Key**
4. Copy `Key ID` and `Key Secret`
5. Settings → Webhooks → **Add Webhook** for test mode:
   - URL: `https://dev.alphahunt.in/api/payment/webhook`
   - Active events: `payment.captured`, `subscription.charged`, `subscription.activated`, `subscription.cancelled`, `subscription.completed`
   - Generate secret → copy it
6. Add all three to dev Railway env vars (Part 4c)

Razorpay test mode provides test card numbers for payment simulation:
- Success: `4111 1111 1111 1111`
- Failure: `5104 0600 0000 0008`
- (Full list at [razorpay.com/docs/payments/payments/test-card-details](https://razorpay.com/docs/payments/payments/test-card-details))

---

## Part 6 — Resend: same domain, separate API key (already done in Part 2d)

Both prod and dev send from `noreply@alphahunt.in` — same domain, same DKIM, same DMARC. No need to verify a separate domain.

The split is at the API-key level: prod uses one Resend API key, dev uses another. Resend tracks them separately so you can see dev send volume vs prod send volume in the Resend dashboard. Saves the 3,000/mo free tier from being burnt up by your testing.

---

## Part 7 — Daily workflow (this is the payoff)

Once everything is set up, here's how every code change goes from idea to production:

### Step 1 — Make the change locally

```bash
cd "C:\Users\SOURA\Documents\Claude\Projects\USA Stock Market\TickerMover"
git checkout dev          # always start on dev
git pull origin dev       # get any teammates' changes (just you for now, but good habit)

# Make your edits in the editor

# Optional: run locally to smoke test
uvicorn app:app --reload --host 127.0.0.1 --port 8000
# Visit http://localhost:8000 to test
```

### Step 2 — Push to dev

```bash
git add .
git commit -m "feat: clear description of what changed"
git push origin dev
```

Within 60-90 sec, Railway dev auto-deploys to `dev.alphahunt.in`. Watch deploy logs in Railway dashboard.

### Step 3 — Test on dev.alphahunt.in

- Open `https://dev.alphahunt.in` in a browser
- Sign up a test user (uses dev Supabase, not prod)
- Click through your changed flow
- Try edge cases — broken inputs, missing data, slow network
- Check Railway logs for errors

If something is broken: keep iterating on dev branch. Each push triggers a fresh deploy. Production stays untouched.

### Step 4 — Promote to production

When you're confident the change works on dev:

```bash
git checkout main
git pull origin main
git merge dev
git push origin main
```

Within 60-90 sec, Railway prod auto-deploys to `alphahunt.in`. Real users see your change.

### Step 5 — Verify in production

- Open `https://alphahunt.in` (or hard-refresh with Ctrl+Shift+R to bypass cache)
- Confirm the change is live and works
- If something's broken in prod: immediately roll back via Railway dashboard → Deployments → previous deploy → **Redeploy**

---

## Common scenarios

### "I want to test a database schema change"

1. Apply the change in **dev Supabase** SQL Editor first
2. Update your app code on `dev` branch to use the new schema
3. Test on `dev.alphahunt.in` thoroughly
4. When ready to promote: apply the same SQL change in **prod Supabase**, then merge `dev` → `main`
5. Order matters — always migrate the database BEFORE deploying the code that depends on it

### "I want to test a payment flow without real money"

Already covered: dev environment uses Razorpay test mode. Use `4111 1111 1111 1111` as test card. No real charge, full success flow.

### "I broke prod and need to roll back NOW"

1. Railway prod project → Deployments tab
2. Find the last green deploy (before the bad one)
3. Click ⋮ → **Redeploy**
4. Production is back within 60 sec
5. Then debug calmly on dev

### "I want to share dev with someone for review"

Just send them `https://dev.alphahunt.in`. They sign up with their email (creates user in dev Supabase only, not prod). They can use the full dashboard to review your changes.

### "How do I keep dev DB in sync with prod for realistic testing?"

Once a week (manually):
1. Prod Supabase → Database → Backups → Download latest
2. Dev Supabase → Database → Restore → upload the prod backup
3. Now dev has a recent snapshot of prod data — but with separate auth users so testing won't email real customers

---

## Verification checklist (run through after setup)

- [ ] `git branch -a` shows `dev` and `main` branches
- [ ] Pushing to `dev` triggers Railway dev project deploy (NOT prod)
- [ ] Pushing to `main` triggers Railway prod project deploy (NOT dev)
- [ ] `dev.alphahunt.in` loads the dashboard
- [ ] `alphahunt.in` still loads the dashboard (production unaffected)
- [ ] Sign up with a test email on `dev.alphahunt.in` — confirm user appears in dev Supabase, NOT prod Supabase
- [ ] Trigger forgot-password on dev — email arrives, link goes to `dev.alphahunt.in/reset-password`
- [ ] Try a checkout on dev — uses Razorpay test mode, no real charge
- [ ] Sign up on prod (your own email) — confirm user appears in prod Supabase, NOT dev
- [ ] Roll back test: deploy a tiny change to prod, then redeploy the previous version — confirm rollback works in <60 sec

If all 10 are green, you have a real Dev/Prod separation.

---

## Time estimate

| Step | Time |
|---|---|
| Git branch setup | 5 min |
| Supabase dev project + schema replication | 30-45 min (depending on schema complexity) |
| Cloudflare DNS subdomain | 5 min active + 10 min wait |
| Railway dev project + env vars | 30 min |
| Razorpay test mode + webhook | 10 min |
| Resend dev API key | 5 min |
| First deploy + verification | 15 min |
| **Total** | **~2 hours active work, half a day wall-clock** |

Best done in one focused session. Once it's set up you never have to think about it again — just `git checkout dev` and work normally.
