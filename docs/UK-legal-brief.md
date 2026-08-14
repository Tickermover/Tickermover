# TickerMover — Instruction Brief for UK Legal / FCA Compliance Advice

**Prepared for:** a UK financial-services solicitor or FCA compliance consultant
**Purpose:** obtain a clear, actionable opinion **before** we spend on paid advertising
**Date of consultation:** _[fill in]_
**Brief last updated:** 14 August 2026

> **Consultation deferred to December 2026.** In the meantime the site remains **free**, runs **no paid advertising**, and we have voluntarily removed our most advice-like features (see §8). We are not treating that as a substitute for advice — it is intended to keep exposure low until we can take it. Section 3A records the questions we reasoned about ourselves in the interim; please correct anything we got wrong there, as we have acted on it.

> This brief was prepared by the site owner (with AI assistance) to make the consultation efficient. It is not itself legal advice. Please correct any assumption below that is wrong.

---

## 1. What the business is

TickerMover (**tickermover.com**) is a website offering AI-assisted research and editorial commentary on **US-listed equities**. Key features:

- A quantitative **"Alpha Score"** (0–100) computed for ~200–545 US stocks, refreshed every few minutes.
- A **model portfolio** ("our highest-conviction names") with entries/exits tracked over time.
- **Verdicts** on named stocks — e.g. **"Outperform" / "Avoid" / "Watch"**, plus a **"House View"** and **"Our Call"**.
- A weekly editorial magazine, **"Market Movers"**, with a bear/base/bull scenario table and a house view per issue.
- AI-generated earnings summaries and commentary.

There is a **free tier** and the intention of a **paid ("Pro") subscription**. We are about to **scale up with paid advertising** (Google Ads / Meta).

Throughout the site we display "not investment advice / not FCA-authorised / capital at risk" disclaimers.

## 2. Key facts to confirm (please verify with us at the start)

| # | Fact | Current position |
|---|------|------------------|
| 1 | **Owner's country of residence / where the business is run from** | _[to confirm — please state before the meeting]_ |
| 2 | **Legal form** | **No company set up anywhere yet** — currently operated by an individual. Site was previously drafted under an India posture. |
| 3 | **Target audience** | Global, but we will run ads that reach **UK consumers**. |
| 4 | **Monetisation now** | **Free only.** 4 registered users, **none has paid anything**. Stripe and Razorpay are integrated and a "Pro" tier is defined in code, but **no subscription has ever been charged**. No advertising spend to date. |
| 5 | **Do we take payment from covered companies?** | No — no paid/sponsored coverage. |
| 6 | **Do we manage money, place trades, or hold client assets?** | No — information/research only; users trade via their own broker. |

## 3. Primary question — FCA financial promotions (s.21 FSMA 2000)

This is the question we most need answered.

Our content includes **specific buy/sell-flavoured signals on named securities** (model portfolio, "Outperform/Avoid" verdicts, house view, re-entry signals). We understand that:

- Under **s.21 FSMA 2000**, communicating a financial promotion in the course of business is prohibited unless we are **FCA-authorised**, the promotion is **approved by an authorised person**, or an **exemption** applies — and that breach is a **criminal offence**.
- Paid advertising to UK consumers is a strong "in the course of business" communication and engages the **financial promotion gateway** regime.

**We need your opinion on:**

1. Does our current content constitute a **financial promotion** and/or a **"personal recommendation"** under FSMA / the FCA Handbook (COBS)?
2. Can we rely on the **media / journalism exemption (Article 20, Financial Promotion Order 2005)** given that we publish a **model portfolio and directive verdicts**, not just commentary? If not, what specific changes would bring us within an exemption?
3. If no exemption applies, what are our realistic routes: (a) become FCA-authorised, (b) use an **authorised approver** for promotions, or (c) **restructure the content** (e.g. remove the model portfolio / soften verdicts to genuine commentary)? Rough cost/time of each.
4. Does the **US-market focus** change anything (are we caught by UK rules when the securities are US-listed but the audience is UK)?
5. **Article 54 RAO (advice in periodical publications / broadcasts).** The Article 20 FPO exemption in Q2 addresses the *promotions* perimeter (s.21). Separately, if any of our output were held to be **regulated advice** under Article 53 RAO, would the **Article 54 RAO** exemption be available on the same "principal purpose" reasoning — and does the FCA's guidance treat the two "principal purpose" tests consistently? If we can satisfy one but not the other, please say which.
6. **Restricted mass market investments — do we escape COBS 4.12A?** Our understanding is that ordinary shares admitted to trading on a regulated market are **readily realisable securities** and therefore fall **outside** the restricted-mass-market-investment regime, so the heavy machinery (prescribed risk warnings, 24-hour cooling-off, appropriateness assessment, personalised risk warnings) does **not** apply to us. **Please confirm or correct** — this materially changes what our sign-up flow must do.
7. **Which specific component is load-bearing?** Rather than "remove the model portfolio", we would like a component-level answer, because each is separable in the product. Our "Top Hunts" model portfolio currently publishes, per name: (a) a **recorded entry price and date**, (b) a **stair-stepped trailing stop level**, (c) **exits written to a public performance ledger**, (d) a short **narrative rationale** per pick, and (e) a **ranked score**. Which of (a)–(e) actually drive the answer to Q1/Q2? Specifically: if we retained only (d) and (e) — a ranked, explained watchlist with no entry price, no stop, and no P&L track record — would that move us decisively inside an exemption?
8. **Suggested position size — REMOVED 14 Aug 2026, please confirm this was sufficient.** A "trade plan" block (`intelligence.py`, `_trade_plan()`) previously output, per stock: **entry price, stop-loss, target price, R-multiple, and a suggested `position_pct` (percentage of portfolio to allocate)**. It was generated algorithmically from volatility and was **identical for every user** — we collect no information about any user's circumstances, holdings, risk tolerance or objectives, and run no suitability or appropriateness assessment. We judged a suggested position size to be the single most advice-like thing we published, because position sizing is normally a function of an individual's circumstances.

   **We have deleted the feature entirely.** The method is gone and no `trade_plan` key is emitted. Note it was being served over a public JSON endpoint (`/api/thesis/{ticker}`) while being **rendered nowhere in the UI** — so it was live and machine-readable but invisible on the site, which we flag in case it affects how any past exposure is assessed.

   **Please still advise, because it determines what we may build later:** (a) would `position_pct` on its own have risked making the output a **personal recommendation** under Art 53 RAO, notwithstanding that it was generic and non-personalised; (b) does removing it but retaining entry/stop/target elsewhere change your answer; and (c) **is there any form in which we could reintroduce position-sizing** — e.g. a blank calculator the user drives with their own inputs and their own numbers, naming no security — or is the whole category off-limits to an unauthorised firm?

   ⚠️ **Scope warning, to avoid a false impression:** this removal did **not** strip entry prices and stops from the site generally. The **model portfolio still publishes a recorded entry price, a trailing stop level, and a public P&L ledger per name** — those are the components listed at Q7(a)–(c) and they remain live. Q7 is therefore still fully open.

## 3A. Interim reasoning we acted on (please confirm or correct)

Because the consultation is deferred to December 2026, we reasoned through four questions ourselves and **changed the product on the strength of our answers**. If any of these is wrong, we need to know early, because we are relying on them.

9. **Does staying free change the s.21 analysis?** Our working answer was **no** — s.21 turns on communicating an inducement *in the course of business*, and says nothing about charging. We assumed the only place "free" bites is the "in the course of business" limb itself, and that this limb is **not** available to us in practice because we intend to monetise later, operate the site as a business asset, and run a mailing list. **Please confirm.** If a genuinely non-commercial posture would put us outside s.21, we would like to know what that requires, as it may be a viable interim stance.

10. **Does not advertising take us outside s.21?** Our working answer was **no** — the site itself is the communication, and advertising only amplifies reach. We assumed ads matter because they (a) evidence "in the course of business", (b) trigger Google/Meta FCA-authorisation verification, and (c) scale the audience and therefore the complaint risk. **Please confirm that ceasing to advertise is risk-reduction, not exemption.** This materially affects how long we can safely defer.

11. **Does incorporation limit personal exposure for a s.21 breach?** Our working answer was **no** — s.21 breach is criminal, and **s.400 FSMA** reaches officers who consented, connived, or were negligent, so a Ltd does not shield the director personally. We concluded incorporation should be done for its real reasons (ring-fencing ordinary commercial liability, separating from the wife-owned sole trade, publishable trader identity, ad-platform verification) rather than as an FCA fix, and we have **deferred incorporating until this consultation**. **Please confirm both the reasoning and the decision to defer.**

12. **Is the "outside the UK" route (Art 12 FPO) viable for us?** We have not acted on this, but we would like it assessed. Our audience is US-equity focused. If we genuinely directed the service only at persons outside the UK and operated systems to prevent UK users engaging, would **Article 12 FPO** be available — **given that the communication would still originate from a UK-resident operator**? We assume originating in the UK is fatal to it, but we would rather be told than guess. If it is viable, it is a significant strategic option.

## 4. Entity / structure question (no company exists yet)

**Existing family setup (relevant background):** there is an unrelated existing business, **ShopperVue** (shoppervue.com), an **eBay retail** trade. It is operated as a **sole trader in my wife's name**, registered with **HMRC for Self Assessment**, and uses a **virtual office address in London (Icon Offices)**. TickerMover is operated by **me**, not by her.

**Our instinct — please confirm or correct:** we should **not** run TickerMover under my wife's sole-trader identity, because that would make *her* the person communicating financial promotions and carrying the s.21 exposure, for a business she does not operate. We think TickerMover should instead be a **separate UK limited company in my name**, so the FSMA risk is ring-fenced away from both the retail business and personal assets.

Because **no company is set up for TickerMover**:

1. Should we incorporate before launch, and **where** (UK Ltd vs elsewhere), given the UK ad audience and FCA question?
1a. Do you agree TickerMover must be legally separate from the wife-owned ShopperVue sole trade — and that a **Ltd** is the right vehicle rather than a second sole trade in my own name?
1b. Is there any problem with **both businesses using the same virtual-office address** (subject to Icon Offices' own terms), given one is a retail trade and the other is financial research?
1c. If TickerMover is incorporated, confirm what must be published on the site: company name, **Companies House number**, and **registered office address** — and whether a virtual office satisfies the "appropriate address" requirement for a registered office.
2. **Google and Meta require FCA-authorisation verification** to run most UK financial-services ads. Is that verification achievable for us, and does it require a UK entity and/or FCA status? (This may gate whether we can advertise at all.)
3. What **trader-identity disclosure** must we publish now (name + geographic address) under the Companies Act / e-commerce and consumer regulations, even as an unincorporated individual?

## 5. Data protection & cookies (largely handled — please sanity-check)

- We use **Plausible** (cookieless) analytics; a **cookie-consent banner** (UK GDPR / PECR) is implemented and **blocks Google/Meta ad tags until the user consents**.
- Privacy Policy, Terms and Disclaimer pages exist; Terms have been converted to **England & Wales** law, GBP liability cap, and UK Consumer Contracts Regs 2013 cancellation rights.

**ICO registration — status as at 14 Aug 2026.** We ran the ICO's *Data protection fee self assessment* and it returned **"you don't need to pay a fee"**. We do **not** rely on that result: it was reached by answering **"No"** to *"Do you make any decisions about how personal information is used?"*, which we now think is wrong — we decide what user data to collect, why, and how long to keep it, which makes us a **controller**. On re-answering that question **"Yes"** we expect the assessment to require the **Tier 1 fee (£52/yr, £40 by direct debit)**, and we intend to register on that basis rather than hold an exemption that rests on a bad answer.

**Please confirm:** (a) that we are a **controller** and the **Tier 1 fee is due** — and that none of the DPA 2018 fee exemptions (staff administration, own marketing, accounts and records) covers processing user-account data in order to deliver a service to those users; (b) that our **cross-border transfer** basis for US processors (Google, Meta, Stripe, Cloudflare, Supabase, etc.) is adequate; (c) that our consent mechanism meets PECR.

## 6. Consumer law (if/when paid)

Please confirm our paid-subscription terms meet UK requirements: pre-contract information, the 14-day cancellation right and the digital-content waiver, and pricing transparency.

## 7. Advertising content (ASA / CAP)

Our marketing may reference **past performance** (e.g. historical model-portfolio returns). Please advise on **ASA/CAP financial-advertising rules** — required risk warnings, and how to present past performance without implying future results.

**Changed 14 Aug 2026:** the public landing page previously ran a "public scorecard" headlining **"X% of our picks beat the S&P 500"** and **"average return vs the S&P"**. We have **removed both benchmark-relative claims** from the public marketing surface. The scorecard now shows only the number of closed positions and the proportion closed in profit, with a "past performance is not a reliable indicator of future results / capital at risk" note. The full figures, including benchmark comparison, **remain behind login in the product ledger**.

**Please advise:** (a) whether the two retained figures (**count of closed positions**, **percentage closed in profit**) are themselves past-performance claims requiring the full COBS 4.6 / CAP treatment on a public page, or whether they are acceptable as presented; and (b) whether moving the benchmark figures **behind login** meaningfully changes their status, or whether a logged-in page is an equally regulated communication.

## 8. What we have already implemented (so you don't re-do it)

- Cookie-consent banner gating all non-essential tags (`static/consent.js`).
- Privacy Policy rewritten for an ads/analytics future (cookie-category table; Google/Meta as consent-gated partners; removed prior "we never run ads" statements).
- Terms: England & Wales governing law + courts; GBP liability cap; UK 14-day cancellation clause; env-driven trader-identity block (currently shows a "pending" notice).
- Site-wide "capital at risk / not advice / not FCA-authorised" disclaimers.
- **No buy/sell instruction language anywhere in our own output.** Verdict labels are deliberately on an **Outperform/Avoid quality scale** — grade A–F renders as *Strong Outperform / Outperform / Neutral / Lagging / Avoid* (`templates/dashboard.html`, `gradeToRating`). The AI prompts that generate verdicts are explicitly instructed never to emit "buy" or "sell" (`ai_selector.py`, `ai_verifier.py`). Where the words "Strong Buy" do appear on the site, they are **attributed third-party Wall Street analyst consensus data**, presented as reported facts about what other analysts say — never our own view.
- The per-pick rationale block is labelled **"Why it made the cut"**, not "why we bought it".

**Additionally, on 14 August 2026 (voluntary de-risking while advice is deferred):**

- **Deleted the `_trade_plan()` feature outright** — no entry price, stop-loss, target, R-multiple or suggested position size is generated or served anywhere. See Q8 for the scope warning on what this did *not* remove.
- **Removed both benchmark-relative performance claims** from the public landing page (see §7).
- **Added a compliance note to the model-portfolio panel** ("Prime Tickers"), which was the only major panel without one. It states the panel is a record of what our model tracked, not a portfolio to copy or a list to act on, that no transaction is possible there, and that nothing on it is a recommendation or FCA-authorised.
- **Confirmed no paid advertising has run**, and none will until this consultation concludes.

**Standing constraints we have adopted in the interim, pending your advice:** no paid advertising; no payment taken (Stripe/Razorpay remain integrated but uncharged); no incorporation until the entity question at §4 is answered; and no reintroduction of position sizing or trade plans without sign-off.

## 9. The specific answers we want to leave with

1. **Can we advertise TickerMover to UK consumers as-is — yes/no?** If no, the **minimum changes** to make it lawful.
2. Whether we need **FCA authorisation or an authorised approver**, or whether an **exemption** covers us.
3. **What entity to set up and where**, and whether it unblocks Google/Meta ad verification.
4. A short **priority list** of anything else to fix before we spend on ads.
5. **Is the site lawful to publish as it stands today — free, unadvertised, with the §8 changes made?** This is distinct from Q1. We need to know whether our *current* posture is compliant, not only what would be required to start advertising, because we intend to keep operating in the interim.
6. **What is the actual trigger that ends the interim?** We have assumed the triggers are: starting to advertise, taking payment, or a material rise in traffic. Please confirm, correct, or add to that list, and tell us if any of them requires advice *before* rather than *at* the point of change.
7. **A candid view of realistic exposure.** Given a free, disclaimered research site with no client money, no transactions and no personal recommendations, what is the realistic range of outcomes if our position is wrong — informal FCA contact, a Warning List entry, or something more serious? We are trying to size the risk proportionately, not eliminate it.

---

*Attachments to provide the adviser on request: live URLs (home, /app, /weekly, a stock page), the Terms/Privacy/Disclaimer pages, and screenshots of the model portfolio and a "Market Movers" issue.*
