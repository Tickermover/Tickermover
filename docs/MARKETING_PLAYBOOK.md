# TickerMover — Marketing Playbook

_Written 2026-07-05, at go-live. Solo-founder budget: ~$0 + time. Everything here
is executable by one person, and every line of copy respects the compliance rules
at the bottom (UK FCA pivot — research, not advice)._

---

## 1. Positioning — say this everywhere, word for word

**One-liner:**
> TickerMover scores 540+ US stocks into one plain-English number every morning —
> and logs every call, win or lose, to a public ledger.

**The hook that no competitor can copy:** the open ledger. Zacks, TipRanks and
Motley Fool sell picks; none of them show you their closed losers on the
homepage. You do. Every piece of marketing should hammer one word: **receipts**.

Three message variants (rotate them):
- "Most stock sites show you their winners. We publish the whole tape."
- "One number. Six pillars. Every call on the record."
- "We do the homework. You make the call." *(existing slogan — keep)*

**Who it's for (ICP):** self-directed retail investors, 25-45, holding US
large-caps for weeks-to-months (not day traders), who already read FinTwit /
r/stocks and are tired of paywalled hype with no accountability.

---

## 2. The growth engine (how the pieces feed each other)

```
Weekly Editorial (Sunday, free)  ──►  email list  ──►  beta signups
        │                                                   ▲
        ▼                                                   │
  repurpose into 3-5 X posts + 1 Reddit comment ──► profile clicks
        │                                                   ▲
        ▼                                                   │
  618 SEO pages (/stocks/*) catch long-tail search ─────────┘
```

One asset (the Sunday editorial) becomes the whole week's content. Never write
content twice.

---

## 3. Channel plan — in order of expected return

### A. X / FinTwit (daily, 15 min/day) — the main channel
Your data generates posts nobody else can write. Templates (fill from the
dashboard each morning):

1. **Score-mover post (Mon-Fri):**
   > "$TICKER's Quant Score jumped 71 → 84 this week. What moved: estimates
   > raised (+), broke above the 50-day (+), volume 2.1× average (+).
   > Full six-pillar breakdown: tickermover.com/stocks/TICKER"
2. **Earnings-week post (any week):**
   > "7 of our tracked names report this week: $A $B $C… Here's what the score
   > says going in — and we'll post the after, whatever it looks like."
3. **Ledger post (monthly, the trust bomb):**
   > "Every position our tracker closed in June — wins AND losses, with the
   > exit reason for each. No cherry-picking, the ledger is public: [link]"
4. **Exit-discipline post (whenever a stop fires):**
   > "Our tracker just closed $TICKER at -7.8%. Rule that fired: price hit the
   > protective stop. Losses stay small by design. The system, explained: [link]"

Rules: always a specific number, always a link to a /stocks/ page (they carry
analytics now, so you'll see what converts), never a prediction, never "buy".

### B. Reddit (2-3×/week, give-value-first)
Strict no-self-promo subs — the play is **useful comments, link in profile**:
- r/stocks, r/StockMarket daily discussion threads: answer "what do you think of
  $X?" with your six-pillar readout in plain text. No link in the comment. Your
  profile bio carries the link.
- Allowed-promo subs for the launch post: r/SideProject, r/InternetIsBeautiful
  (the ledger angle fits), r/DataIsBeautiful (score-vs-outcome charts).

### C. Product Hunt (one shot — do it in week 3-4, not day 1)
Prep checklist: 5 gallery screenshots (hero, one /stocks page, the ledger tab,
weekly editorial, mobile), a 40-char tagline, first-comment story.
- **Tagline:** "US stock research with a public track record"
- **First comment (paste-ready):**
  > Hi PH — solo founder here. I got tired of stock-pick sites that show only
  > their winners, so I built the opposite: 540+ US large-caps scored 0-100
  > every morning across six pillars (momentum, growth, quality, valuation,
  > sentiment, risk), and a tracker whose every entry AND exit is logged to a
  > public ledger with the reason. It's research, not advice — the whole point
  > is you can audit it. Free during beta. I'd love brutal feedback.
- Launch on a Tuesday-Thursday, be online all day answering comments.

### D. Hacker News "Show HN" (same week as PH)
Title: `Show HN: I score 540 US stocks daily and publish every call to a public ledger`
HN loves: the engineering story (data pipeline, exit-rule backtest, AI-verified
exits with a hard catastrophic floor), the honesty (post your backtest caveats
section verbatim — the "read carefully" caveats ARE the credibility).

### E. SEO (compounding, mostly done — finish these)
- ✅ 618 URLs in sitemap, per-page schema, OG images. Already strong.
- ☐ **Verify the site in Google Search Console + Bing Webmaster Tools and
  submit the sitemap.** 10 minutes, unlocks the ranking data → tells you which
  /stocks pages to double down on. (Only you can do this — needs your Google login.)
- ☐ Weekly editorial should get its own permalink page per edition (archive =
  compounding content), not just the latest.

---

## 4. 30-day calendar (compressed)

| Week | Theme | Actions |
|---|---|---|
| 1 | Plumbing + soft start | Create Plausible account, set `PLAUSIBLE_DOMAIN` on Railway. Search Console + sitemap. X account warm-up: 1 score-mover post/day, follow 50 FinTwit accounts, reply to 5 posts/day with data. |
| 2 | Rhythm | Daily X post from templates. First Reddit value-comments. First "ledger post" on X. Sunday editorial goes out — repurpose into 3 X posts. |
| 3 | Launch week | Product Hunt Tue + Show HN Thu (independent shots). r/SideProject post same week. Reply to everything within the hour. |
| 4 | Double down | Check Plausible: which channel drove signups? Do 2× more of the top channel, drop the bottom one. Post the month-1 ledger recap. |

**KPIs (check in Plausible weekly):** unique visitors → /login?signup clicks →
signups (top of funnel); email subscribers (retention); which /stocks/* pages
get organic entries (SEO steer). Month-1 realistic targets: 2-5k uniques,
100-300 signups, 1 channel clearly winning.

---

## 5. Compliance guardrails (non-negotiable, UK FCA pivot)

Every post, comment, and reply must pass these:
1. **Never** "buy", "sell", "you should", price targets, or "will go up".
   Say: "our score says", "the data shows", "our tracker closed it because…".
2. **Never** promise returns or say "beat the market". Show the ledger and let
   it speak. Always pair performance mentions with "past performance is not a
   reliable indicator of future results."
3. Always identifiable as the founder when posting about your own product
   (Reddit/HN will destroy you for astroturfing — honesty is also the brand).
4. The footer disclaimer ("research and data tool… not authorised or regulated
   by the FCA") stays on every page — already live.

---

## 6. What's already shipped vs. what only you can do

**Shipped in code (this session):**
- Cookieless analytics on every public page — activates the moment you set
  `PLAUSIBLE_DOMAIN=tickermover.com` on Railway (create the site at plausible.io
  first; ~£9/mo, no cookie banner needed).

**Only you can do (accounts/identity — in priority order):**
1. Plausible account + Railway env var → unblinds ALL other marketing.
2. Google Search Console + Bing verification + submit sitemap.
3. X account for TickerMover (or post from your own — personal converts better).
4. Product Hunt + HN accounts (age them 2+ weeks before launching).
