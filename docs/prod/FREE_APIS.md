# Free API accounts — the complete list

TickerMover runs at **£0/month** except the domain. Everything below is a free
tier. Register every one with **support@tickermover.com**.

Paste results into `/etc/tickermover.env` on the Oracle box (mode 0600), then
`sudo systemctl restart tickermover`.

> **Rotate first.** Six keys were hardcoded in `config.py` in this PUBLIC repo
> until 30 Aug 2026 — Alpaca (key *and* broker secret), FMP, Finnhub, sec-api,
> ApeWisdom. They are in git history and cannot be unpublished. Treat them as
> burnt: revoke at the provider and issue new ones.

---

## 1. AI — the important one

Every AI call already routes through the free chain in `llm_free.py`.
`ANTHROPIC_FALLBACK` is **off by default**, so Anthropic is never called and
costs nothing. Do NOT set `ANTHROPIC_API_KEY`.

Get these in order — the first three carry 90% of the load:

| # | Provider | Free budget/day | Sign up | Env var |
|---|---|---|---|---|
| 1 | **Google AI Studio (Gemini)** | **700,000 tok** | aistudio.google.com/apikey | `GEMINI_API_KEY` |
| 2 | **Mistral** | 100,000 tok | console.mistral.ai | `MISTRAL_API_KEY` |
| 3 | **Together AI** | 100,000 tok | api.together.xyz | `TOGETHER_API_KEY` |
| 4 | NVIDIA NIM | 45,000 tok | build.nvidia.com | `NVIDIA_API_KEY` |
| 5 | SambaNova | 30,000 tok | cloud.sambanova.ai | `SAMBANOVA_API_KEY` |
| 6 | OpenRouter | 24,000 tok | openrouter.ai/keys | `OPENROUTER_API_KEY` |
| 7 | Cerebras | 20,000 tok | cloud.cerebras.ai | `CEREBRAS_API_KEY` |
| 8 | Groq | 11,000 tok | console.groq.com | `GROQ_API_KEY` |

**~1,030,000 free tokens/day** once all eight are in. The chain fails over
automatically and cools off a provider that rate-limits.

### Budget discipline matters more than money now

The constraint is no longer your card, it is the daily allowance. At the stock
prewarm defaults (150+120+60+40 = 370 generations/cycle at ~3k tokens each)
one cycle would consume the entire daily budget and every AI surface would go
dark until midnight. These are **required**, not optional:

```
OVERVIEW_PREWARM_N=25
DEPS_PREWARM_N=20
FACTCHECK_PREWARM_N=15
BRIEF_PREWARM_N=10
```

~70 generations/cycle, ~210k tokens. Sustainable.

---

## 2. Market data

| Provider | Free tier | Sign up | Env var |
|---|---|---|---|
| **Alpaca** | Unlimited IEX feed | alpaca.markets → Paper Trading → API Keys | `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY` |
| **Finnhub** | 60 calls/min | finnhub.io/register | `FINNHUB_KEY` |
| **Alpha Vantage** | 25 calls/day | alphavantage.co/support/#api-key | `ALPHA_VANTAGE_KEY` |
| **FMP** | 250 calls/day | site.financialmodelingprep.com | `FMP_API_KEY` |
| sec-api.io | 100 calls | sec-api.io | `SEC_API_KEY` *(optional)* |

**Keyless, nothing to do:** SEC EDGAR (needs only a contact string in
`SEC_EDGAR_UA`), GDELT news, yfinance, FRED.

Leave `POLYGON_API_KEY` **empty** — it falls back to yfinance for free.
Leave `API_NINJAS_KEY` empty — that path is paid-only and short-circuits.

---

## 3. Search — grounding for AI answers

| Provider | Free tier | Sign up | Env var |
|---|---|---|---|
| Serper | 2,500 searches once | serper.dev | `SERPER_API_KEY` |
| Tavily | 1,000/month | tavily.com | `TAVILY_API_KEY` |
| Brave Search | 2,000/month | brave.com/search/api | `BRAVE_API_KEY` |

**DuckDuckGo is the keyless bottom tier** — no quota, always last, so the
search chain can never be fully knocked out by billing.

---

## 4. Infrastructure

| Service | Free tier | Env var |
|---|---|---|
| **Oracle Cloud** | 4 ARM cores, 24GB RAM, forever | — |
| **Supabase** | 500MB DB, 50k MAU | `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_JWT_SECRET`, `SUPABASE_SERVICE_KEY` |
| **Resend** | 3,000 emails/mo, 100/day | `RESEND_API_KEY` |
| **Cloudflare** | DNS, proxy, analytics | — |
| **GitHub** | Public repo, Actions | — |

Cloudflare Web Analytics replaces Plausible (~$9/mo) at no cost.
`PLAUSIBLE_SCRIPT_ID` now defaults to `off`.

---

## 5. Images & embeddings

| Provider | Free tier | Env var |
|---|---|---|
| Unsplash | 50 req/hr demo | `UNSPLASH_ACCESS_KEY` |
| Pexels | 200 req/hr | `PEXELS_API_KEY` |
| Voyage AI | 50M tokens | `VOYAGE_API_KEY` |

All three degrade gracefully if unset — cover images fall back to generated
art, and RAG falls back to keyword matching.

---

## 6. The one thing that is not free

**`tickermover.com` — roughly £10–15/year.** No free substitute keeps the
name. If it must be £0, run on the instance IP or a free subdomain and accept
losing the brand and any accumulated SEO.

---

## Minimum viable set

If you only do eight signups, do these — the site works properly with them:

1. Google AI Studio (Gemini) — carries the AI on its own
2. Supabase — auth + durable state
3. Alpaca — prices
4. Finnhub — news
5. FMP — fundamentals
6. Resend — signup/reset email
7. Serper — AI grounding
8. Cloudflare — DNS + TLS + analytics

Everything else is redundancy, and redundancy is what stops one provider's
rate limit taking a feature offline.
