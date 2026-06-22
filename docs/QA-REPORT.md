# 🧪 TickerMover — Full QA Test Report

**Build:** dev @ `abdb207` · **Date:** 2026-06-22
**Scope:** 118 routes, ~40 Python modules, 13 templates
**Method:** Static code audit (the app can't fully execute locally — auth gate + 0 tickers without API keys). Items requiring a live browser/server are tagged 🔬NEEDS-EXEC and are **not** yet confirmed.

## Verdict
Functionally solid, but **NOT launch-ready for paid/scale**. Good engineering in places (error degradation, caching architecture, scoring math, reduced-motion, empty states), but **7 BLOCKER-class issues** in billing, authorization, and compliance must be fixed before taking real money or opening signups. There are **zero automated tests** in the repo.

## Severity rollup

| Severity | Count | Theme |
|---|---|---|
| 🔴 BLOCKER | 7 | Payment bypass, unauth destructive endpoints, compliance, auth brute-force |
| 🟠 HIGH | ~14 | XSS (reflected + stored), event-loop blocking, cache re-bill, SEO canonical |
| 🟡 MEDIUM | ~25 | Cost-breaker gaps, palette split, webhook idempotency, tap targets |
| 🔵 LOW / INFO | ~30 | Aesthetic drift, dead code, minor data integrity |
| Tests in repo | 0 | No `test_*.py` anywhere |

---

## 🔴 BLOCKERS — fix before paid launch / opening signups

| # | Issue | Location | Impact |
|---|---|---|---|
| B1 | Stripe webhook fails OPEN when secret unset — grants Pro from `metadata.user_id` via service key | billing.py:248, app.py:10107 | Forged event → free Pro for any account |
| B2 | Razorpay webhook `return True` when secret unset | billing.py:166 | Same fail-open; live once Pro-writes wired in |
| B3 | `/api/payment/verify` never checks amount/currency/order ownership | app.py:9971 | Pro for a tampered order; cross-order replay |
| B4 | `/api/model-portfolio/reset` has zero auth | app.py:8573 | Anonymous POST wipes the public track record |
| B5 | `/api/admin/reprice-closed-trades` unauthenticated destructive | app.py:8406 | Anyone overwrites the audit log via service key |
| B6 | `/api/thesis` emits literal Buy/Sell + entry/stop/target — violates FCA rule; un-gated | intelligence.py:1023 | Regulatory exposure on the UK pivot |
| B7 | No rate-limiting on ANY auth endpoint | app.py:938 | Unlimited brute-force + magic-link/forgot-password mail-bomb |

The billing trio (B1–B3) is masked now only because `BETA_PRO_UNTIL` makes everyone Pro free until 2026-08-01 — meaning these paths are untested in production and all go live simultaneously at launch.

## 🟠 HIGH

- Reflected XSS on `/stocks/{ticker}` — raw `sym` into `<title>`/meta/OG (app.py:2178)
- Stored/reflected XSS in dashboard — API `name`/`ticker` into `.innerHTML` unescaped (dashboard.html:12694, 14186, 14659; escaper `v2EscHTML` exists but unused here)
- Prime approve/reject are unauth GET links in email — link-prefetch auto-approves picks (app.py:8069)
- Sync blocking HTTP inside async handlers — every durable store uses `httpx.Client` (8s) → stalls event loop (overview_store.py:97 + 5 others)
- Cache stampede — no single-flight; cold start fires duplicate paid fetches (cache.py:32)
- Durable caches silently re-bill if a Supabase table is missing (no startup probe)
- 3 AI paths bypass the cost breaker — desk, thesis, `/api/concall` (latter also un-gated/anon)
- Daily $12 breaker resets on every redeploy (in-process only)
- Duplicate `/api/watchlist` routes split storage across two backends (app.py:9626 vs 9865)
- Signin email enumeration + OAuth open-redirect surface
- AV daily-budget guard reads a different counter than it spends → overspend under concurrency
- Canonical apex-vs-www mismatch — diluted indexing, broken OG host
- Legal `/terms` still has India governing law + INR cap under "UK/FCA" framing (legal_pages.py:357)

## 🟡 MEDIUM (selected)

- Client `force=true` forces paid regen; `/api/why` is anon (app.py:4910)
- Greedy-brace JSON extraction can mis-parse model output (compare_gen.py:110 + others)
- Webhook idempotency/replay protection absent; no refund handling; out-of-order events resurrect Pro
- NaN pillar inputs route to wrong scoring buckets (no isnan screen) (ai_scorer.py)
- Newsletter unsubscribe URL not URL-encoded → `+`-addressed unsub fails (app.py:3234)
- `%s` strftime non-portable + dead cutoff var (data_coordinator.py:614)
- `/api/refresh` anon-triggerable (DoS)
- Off-brand gold favicon/OG fallback shadows real blue PNGs (app.py:1037)
- Dashboard: two competing palettes (amber vs blue) + 2,328 hardcoded hex
- Sub-44px tap targets on primary nav; modals lack focus trap/restore; no focus-visible on CTAs
- `.callout-green` undefined CSS class on legal pages (legal_pages.py:237)

## 🔵 LOW / INFO

- Stale comments ("ALPHAHUNT / AROOTH theme / Poppins") in landing.html
- Three different blues for one logo across pages
- `/learn` & `/terms` use a different design system than `/`
- Grade colors disagree between app and `/sectors`
- Radius scale sprawls to ~11 values
- Sitemap omits legal/login pages
- Disk-fallback caches not env-namespaced
- `_dead_endpoints` never reset for process lifetime (data_coordinator.py)

---

## Per-cluster coverage

| Cluster | Cases | Pass | Fail | Risk | Worst |
|---|---|---|---|---|---|
| Auth & Account | 72 | 28 | 20 | 24 | BLOCKER (B7) |
| Billing & Payments | 35 | 6 | 16 | 13 | BLOCKER (B1–B3) |
| AI Generation & RAG | 73 | 44 | 4 | 20 | BLOCKER (B6) |
| Data Layer & Caching | — | — | — | — | HIGH (event-loop blocking) |
| Scoring & Core Ops | 62 | 46 | 4 | 10 | BLOCKER (B4, B5) |
| Public Pages & SEO | — | — | — | — | HIGH (XSS, canonical) |
| Dashboard SPA | — | — | — | — | HIGH (innerHTML XSS) |

Confirmed correct from prior work: bottom-line AI disabled by default ✅, conviction TTL 29d ✅, monthly $50 cap durable+enforced ✅.

---

## Recommended remediation order
1. **Blockers (this week):** webhook fail-closed (B1/B2), payment amount/order binding (B3), auth on reset+reprice (B4/B5), rate-limiter (B7).
2. **Before signups:** thesis compliance (B6), XSS escaping pass, prime approve/reject → POST.
3. **Before scale:** wrap store HTTP in `to_thread`, single-flight, table-existence probes, breaker on the 3 ungated AI paths.
4. **Polish:** unify palette to 2 token accents, fix canonical host, swap gold favicon fallback, a11y (tap targets, focus traps).
5. **Foundational:** stand up `tests/` — start with pure functions (scoring, cost estimate, cache signatures, JWT verify, webhook signature).

---

## Remediation status (2026-06-22)

### ✅ Fixed & shipped (dev + prod)
- **B1/B2** — webhooks fail CLOSED (unsigned rejected); Stripe 5-min replay window. `ALLOW_UNSIGNED_WEBHOOKS=1` for dev only.
- **B3** — `/api/payment/verify` fetches the order from Razorpay and asserts amount/currency/status/receipt-binding.
- **B4/B5** — `/api/model-portfolio/reset` and `/api/admin/reprice-closed-trades` now admin-gated.
- **B6** — thesis uses the house research scale (Strong Outperform/Outperform/Neutral/Lagging/Avoid); LLM prompt reframed; no buy/sell/price-target language.
- **B7** — in-process per-IP rate limiter on all auth endpoints (signin/signup/forgot/magic-link/resend/reset).
- **HIGH** — open-redirect guard (`_safe_redirect`) on OAuth/magic-link/resend.
- **HIGH** — reflected XSS on `/stocks/{ticker}` (strict ticker format → 404).
- **HIGH** — stored XSS in dashboard (company-name + error-message innerHTML sinks escaped).
- **HIGH** — cost breaker now covers `/api/thesis` + `/api/concall` (2 of 3 bypass paths).
- **MEDIUM** — `/api/refresh` admin-only; `/api/why` force admin-only; unsubscribe URL-encoded; gold favicon fallback → real PNGs; `.callout-green` CSS.

### ⏳ Deferred — needs a decision, live verification, or a dedicated refactor
- **Watchlist dual-storage** (HIGH) — needs deciding which backend is canonical before removing the dead route; touches frontend contract.
- **Event-loop blocking / single-flight / cache-table probe** (HIGH) — data-layer refactor across 6 stores; do as one focused PR with load testing.
- **Webhook idempotency + Stripe out-of-order guard** (MEDIUM) — needs a processed-events table.
- **Legal `/terms` India→UK** (HIGH) — requires the UK solicitor rewrite (already tracked).
- **Canonical apex-vs-www** (HIGH) — product decision (apex needs to be live first); don't flip `SITE_ORIGIN` blindly.
- **Dashboard palette unification + a11y** (MEDIUM) — 2,328 inline hex, tap targets, focus traps; dedicated UX pass + live browser verification.
- **NaN scoring screen, `%s` strftime dead code, `_dead_endpoints` TTL** (MEDIUM/LOW).
- **Automated test suite** (foundational) — start with pure functions.

_Generated from a 7-agent parallel audit, 2026-06-22. Remediation waves 1–4 shipped same day._
