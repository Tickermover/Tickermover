# AlphaHunt — Email Setup Guide (Cloudflare + Gmail path)

End-to-end setup for proper business email at `@alphahunt.in`. Total cost: **₹0/month forever.** Everything lives in your existing Gmail — no app switching.

---

## What you'll have at the end

| Address | Used for | Lands where | Sends as |
|---|---|---|---|
| `support@alphahunt.in` | All user-facing contact: support queries, partnership, press, contact links | digitalquery.ai@gmail.com | Gmail (send-as) |
| `founder@alphahunt.in` | Optional, for outbound only: PR, investor intros, signing emails personally | digitalquery.ai@gmail.com | Gmail (send-as) |
| `noreply@alphahunt.in` | App transactional: password reset, signup, newsletter | (sender only) | Resend API |

`digitalquery.ai@gmail.com` stays as your master inbox — you just gain new "from" addresses. Start with just `support@`; add `founder@` later only if you want a personal "from" for outbound.

---

## Why this stack

- **Cloudflare Email Routing**: Free forever, no user/storage limits, instant inbound forwarding. They get the email at `support@alphahunt.in`, you read it in your existing Gmail.
- **Gmail "Send mail as"**: Built into Gmail. Lets you compose new messages and reply *from* `support@alphahunt.in`. The recipient sees your @alphahunt.in address in the From header — they have no idea Gmail's underneath.
- **Resend (free tier)**: Handles two jobs: (1) the SMTP relay that Gmail uses to actually deliver outbound mail as `@alphahunt.in` (so it's properly DKIM-signed and lands in inboxes, not spam), AND (2) your app's transactional emails (password resets, newsletter sends).

The hidden cleverness: Resend gives you SMTP credentials. Gmail's send-as feature wants SMTP credentials. So Resend handles outbound delivery for everything — your manual emails AND your app's automated ones — all DKIM-signed from your domain, all from one DNS setup.

---

## Part 1 — Move DNS to Cloudflare (15 min active, 24 hr wait)

Cloudflare Email Routing requires that Cloudflare manages your DNS. This is also a great move for your site — free SSL, faster DNS, free DDoS protection.

### 1a. Find out who your domain registrar is

You bought `alphahunt.in` somewhere — GoDaddy, BigRock, Namecheap, Hostinger, Google Domains, etc. Log in there. Don't transfer the domain (slow + sometimes paid). You're just changing **nameservers**.

### 1b. Sign up for Cloudflare

1. Go to [cloudflare.com](https://www.cloudflare.com) → Sign up (free)
2. **Add a Site → enter `alphahunt.in`**
3. Pick the **Free** plan
4. Cloudflare will scan your existing DNS records and import them. **Verify the list** — make sure your A record pointing to your Railway/host IP is there. If anything's missing, add it manually before continuing.
5. Cloudflare will give you two nameservers like `lara.ns.cloudflare.com` and `sid.ns.cloudflare.com`. Copy these.

### 1c. Update nameservers at your registrar

In your registrar's dashboard:
- **GoDaddy**: My Products → DNS → **Nameservers → Change → Custom**
- **BigRock**: Manage Orders → click domain → Name Servers → **Change**
- **Namecheap**: Domain List → Manage → Nameservers → **Custom DNS**
- **Hostinger**: Domains → Manage → DNS / Nameservers → **Change Nameservers**

Replace whatever's there with Cloudflare's two nameservers.

### 1d. Wait for propagation

Cloudflare will email you when nameservers are active. Usually 1-4 hours, max 24. While you wait, your existing site stays live — nameserver changes are seamless.

> **Verify with**: `dig +short NS alphahunt.in` — should return Cloudflare's nameservers once propagated. Or use [whatsmydns.net](https://whatsmydns.net).

---

## Part 2 — Set up Cloudflare Email Routing (10 min)

Once DNS is on Cloudflare:

### 2a. Enable Email Routing

1. Cloudflare dashboard → click `alphahunt.in` → left sidebar **Email → Email Routing**
2. Click **Get Started**
3. Cloudflare will offer to add the required MX records and SPF record automatically. **Click Add records** — it'll inject:
   - `MX @ → route1.mx.cloudflare.net` (priority 1)
   - `MX @ → route2.mx.cloudflare.net` (priority 2)
   - `MX @ → route3.mx.cloudflare.net` (priority 3)
   - `TXT @ → v=spf1 include:_spf.mx.cloudflare.net ~all`
4. **Verify your destination address**: enter `digitalquery.ai@gmail.com`. Cloudflare sends a verification email — click the link in it.

### 2b. Add the routing rules

In **Email Routing → Routes**, click **Create address**:

| Custom address | Action | Destination |
|---|---|---|
| `support@alphahunt.in` | Send to email | `digitalquery.ai@gmail.com` |

Then enable the **Catch-all address** at the bottom and set it to also forward to `digitalquery.ai@gmail.com`. The catch-all means anything sent to any other `@alphahunt.in` address (info@, hello@, contact@, etc.) still reaches you — you don't have to create them explicitly.

(If you later decide you want a personal `founder@alphahunt.in` for outbound investor emails, come back here and add that route the same way.)

**Test it**: from any other email, send a message to `support@alphahunt.in`. It should land in your Gmail within 30 seconds with a Cloudflare envelope header.

---

## Part 3 — Set up Resend (10 min)

This gives you SMTP credentials so Gmail can actually send AS `@alphahunt.in` (and so your app can send transactional mail).

### 3a. Sign up

1. [resend.com](https://resend.com) → sign up with `digitalquery.ai@gmail.com`
2. **Domains → Add Domain → `alphahunt.in`**
3. Resend shows you 3 records. **Important**: ignore Resend's MX record — Cloudflare's MX is already there, and you don't want to overwrite it (Cloudflare handles inbound, Resend only handles outbound). Add ONLY:

| Type | Host | Value |
|---|---|---|
| TXT | `send.alphahunt.in` | `v=spf1 include:amazonses.com ~all` |
| TXT | `resend._domainkey.alphahunt.in` | (long DKIM string Resend gives you) |

Add them in Cloudflare DNS, wait ~5 min, click **Verify** in Resend.

> If Resend insists the MX record is required for verification, add the MX it suggests as a SUBDOMAIN MX (e.g. `MX send.alphahunt.in → feedback-smtp.us-east-1.amazonses.com`) — that's fine because it's on the `send.` subdomain, not the root. The root MX stays Cloudflare-only.

### 3b. Combine SPF (one-time DNS edit)

You now have two services that need to be in your SPF record (Cloudflare for inbound, Resend for outbound). You can only have **ONE** SPF TXT record on the root.

Go to Cloudflare DNS, find the `TXT @ v=spf1 include:_spf.mx.cloudflare.net ~all` record, **edit** it to:

```
v=spf1 include:_spf.mx.cloudflare.net include:amazonses.com ~all
```

### 3c. Get Resend SMTP credentials

In Resend dashboard:
- **API Keys → Create → name `gmail-sendas` → Permission: Sending Access** → copy the key (starts with `re_...`). This is your **SMTP password**.
- SMTP server settings (use these in Gmail next): `smtp.resend.com`, port `465`, username `resend`, password = your API key.

---

## Part 4 — Wire Gmail "Send mail as" (10 min)

This is the magic step that makes outbound emails appear FROM `support@alphahunt.in`.

For `support@alphahunt.in` (repeat the same steps later if you add `founder@`):

1. In Gmail (`digitalquery.ai@gmail.com`) → ⚙ Settings → **See all settings → Accounts and Import** tab
2. Find **Send mail as** → click **Add another email address**
3. A popup opens:
   - **Name**: AlphaHunt (or "Sourav · AlphaHunt" — recipient sees this)
   - **Email address**: `support@alphahunt.in`
   - **Treat as alias**: ✅ checked
   - Click **Next Step**
4. Next screen:
   - **SMTP Server**: `smtp.resend.com`
   - **Port**: `465`
   - **Username**: `resend`
   - **Password**: your Resend API key (the `re_...` string)
   - **Secured connection using SSL**: ✅
   - Click **Add Account**
5. Gmail sends a verification code to `support@alphahunt.in`. Since Cloudflare routes it to your inbox, you get it instantly. Paste the code → Verify.

**Optional but recommended**: in the same Settings page, set **"When replying to a message: Reply from the same address the message was sent to"** — that way replies to `support@` automatically come from `support@` without you having to remember to switch.

**Test it**: Compose new mail in Gmail → click the **From** dropdown → pick `support@alphahunt.in` → send to a different email account. Check headers — should show `From: support@alphahunt.in`, SPF and DKIM PASS.

---

## Part 5 — Add DMARC (5 min)

Anti-spoofing record. Without this, anyone can send phishing emails "as" you.

In Cloudflare DNS, add:

| Type | Host | Value |
|---|---|---|
| TXT | `_dmarc` | `v=DMARC1; p=none; rua=mailto:support@alphahunt.in; pct=100; aspf=r; adkim=r` |

Start with `p=none` (monitor mode) for 2-4 weeks — you'll get weekly aggregate reports showing who's sending mail "as you". After confirming only Cloudflare and Resend show up, change to `p=quarantine`, then later `p=reject`.

---

## Part 6 — Wire Resend into your app for transactional email (5 min)

Resend is also doing double-duty as the sender for password resets and your future newsletter.

### Supabase Auth (for password reset, signup confirmation):

1. **Supabase Dashboard → Project Settings → Auth → SMTP Settings → Enable Custom SMTP**
2. Fill in:
   - **Sender email**: `noreply@alphahunt.in`
   - **Sender name**: `AlphaHunt`
   - **Host**: `smtp.resend.com`
   - **Port**: `465`
   - **Username**: `resend`
   - **Password**: your Resend API key (same one Gmail uses, or create a separate `supabase-prod` key — separate keys are cleaner so you can revoke independently)
3. Save → click **Send test email** to your personal Gmail.

### Newsletter (for the weekly digest you'll send via the existing capture endpoint):

Add `RESEND_API_KEY` to Railway env vars. When you're ready to send, two lines of Python:

```python
import resend, os
resend.api_key = os.environ["RESEND_API_KEY"]
resend.Emails.send({
    "from": "AlphaHunt Weekly <noreply@alphahunt.in>",
    "to": [subscriber_email],
    "subject": "Top 3 US stocks this week",
    "html": "<h1>...</h1>",
})
```

---

## Part 7 — Verify everything (5 min)

After all DNS is in place, run:

```
dig +short MX alphahunt.in
# Expect: 1 route1.mx.cloudflare.net, 2 route2.mx.cloudflare.net, 3 route3.mx.cloudflare.net

dig +short TXT alphahunt.in
# Expect: v=spf1 include:_spf.mx.cloudflare.net include:amazonses.com ~all

dig +short TXT _dmarc.alphahunt.in
# Expect: DMARC record

dig +short TXT resend._domainkey.alphahunt.in
# Expect: DKIM key from Resend
```

Then send a test email from `support@alphahunt.in` (via Gmail send-as) to a fresh Gmail address and check the message headers — should show:
- `SPF: PASS`
- `DKIM: PASS` (signed by `resend._domainkey.alphahunt.in`)
- `DMARC: PASS`

Use [mail-tester.com](https://www.mail-tester.com) for a one-shot 10/10 readiness score.

---

## Part 8 — Update your app + listings to use the new addresses

Things to change:
- App footer / contact pages already updated to `support@alphahunt.in` (done)
- Schema.org markup in app.py + landing.html
- Razorpay merchant customer-facing email → `support@alphahunt.in`
- Supabase project email → `support@alphahunt.in`
- Product Hunt / BetaList / AlternativeTo contact → `support@alphahunt.in`

I can do the search-and-replace across the codebase whenever you say the word.

---

## Time estimate

| Step | Time |
|---|---|
| Cloudflare account + nameserver change at registrar | 15 min active (then 1-24 hr propagation) |
| Email Routing setup + 1 route + catch-all | 5 min |
| Resend signup + DNS + SMTP credentials | 10 min |
| Gmail Send-as for `support@` | 10 min |
| DMARC record | 5 min |
| Supabase SMTP swap + test | 10 min |
| **Total active work** | **~55 min** |
| **Wall-clock including DNS propagation** | **~1 day** |

Best done a few days before your Product Hunt launch so DNS is fully baked and you've had time to test that emails are actually arriving.

---

## What this looks like in daily use

You open Gmail like normal → see emails sent to `support@alphahunt.in` alongside personal mail (a Gmail filter + label can tag them automatically) → reply to one → Gmail automatically sends from `support@alphahunt.in` (because of the "reply from same address" setting) → recipient sees a clean `support@alphahunt.in` From header, no `via gmail.com`, no spam folder.

You're never logging into a separate app. You don't pay anyone. You look like a real company.
