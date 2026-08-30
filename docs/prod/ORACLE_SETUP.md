# Oracle Cloud — step by step

Target: TickerMover running always-on at **£0/month**, on Oracle's Ampere A1
Always Free tier (4 ARM cores, 24 GB RAM, free permanently).

Budget ~90 minutes, most of it waiting.

---

## Part 1 — Account (15 min)

1. Go to **oracle.com/cloud/free** → *Start for free*.
2. Sign up with **support@tickermover.com**.
3. A card is required for identity verification. Always Free resources are
   **not charged**; a ~£1 authorisation may appear and reverse. The account
   stays on "Always Free" unless you explicitly upgrade.
4. **Home Region — pick carefully, it cannot be changed later.**
   `UK South (London)` is the right choice for a UK audience.
   If A1 capacity turns out to be unavailable there (see Part 2), your only
   remedies are to keep retrying or to open a second account in another
   region. Do not upgrade to Pay As You Go hoping it helps — it does not.
5. Verify the email, then sign in to the OCI console.

---

## Part 2 — The instance (20 min, plus retries)

**Compute → Instances → Create instance**

| Field | Value |
|---|---|
| Name | `tickermover` |
| Image | **Canonical Ubuntu 24.04** (make sure it says *aarch64*) |
| Shape | **VM.Standard.A1.Flex** (Ampere, "Always Free eligible") |
| OCPUs | **4** |
| Memory | **24 GB** |
| Boot volume | 50 GB is plenty (200 GB total is free) |
| Public IPv4 | **Assign** |

Save the **private SSH key** when prompted — you cannot download it again.

### If you see "Out of host capacity"

This is the single most common blocker and it is not your mistake — A1 is
genuinely oversubscribed in popular regions. Options, in order:

1. Change the **Availability Domain** (AD-1 / AD-2 / AD-3) and retry.
2. Retry later. Capacity frees up in waves; early morning UTC is often better.
3. Ask for less: 2 OCPUs / 12 GB still runs this app comfortably.
4. Last resort — a different home region, which means a new account.

Do not give up after one attempt. Most people get in within a day or two.

---

## Part 3 — Open the ports (5 min) — BOTH layers

Oracle blocks traffic in **two** places. Missing the second is why "I opened
the ports and it still times out" is so common.

**Layer 1 — the cloud firewall.** Networking → Virtual Cloud Networks → your
VCN → Security Lists → Default → **Add Ingress Rules**:

| Source | Protocol | Dest. port |
|---|---|---|
| `0.0.0.0/0` | TCP | `80` |
| `0.0.0.0/0` | TCP | `443` |

**Layer 2 — the OS firewall.** Oracle's Ubuntu images ship a DROP-all iptables
policy. `bootstrap.sh` opens 80/443 and persists the rule, so you get this for
free — but if you ever rebuild by hand, remember it exists.

---

## Part 4 — Bootstrap (20 min, mostly unattended)

SSH in with the key you saved:

```
ssh -i /path/to/your-key.key ubuntu@<PUBLIC_IP>
```

Then one command:

```
curl -fsSL https://raw.githubusercontent.com/Tickermover/Tickermover/main/deploy/oracle/bootstrap.sh | bash
```

It installs system build deps, creates the `tickermover` service user, clones
`main`, builds the venv, installs requirements, installs Caddy, opens the OS
firewall, and starts the service.

The pip step is the slow part — pandas, matplotlib and lxml on ARM. Wheels
exist for all three, so it should not compile from source; if it starts
building numpy you are on the wrong architecture (check the image is aarch64).

---

## Part 5 — Secrets (10 min)

```
sudo nano /etc/tickermover.env
```

Add the keys from `docs/prod/FREE_APIS.md`. Minimum to be genuinely useful:

```
TICKERMOVER_ENV=prod
SITE_ORIGIN=https://tickermover.com
SUPPORT_EMAIL=support@tickermover.com
SEC_EDGAR_UA=TickerMover research support@tickermover.com

GEMINI_API_KEY=...
SUPABASE_URL=...
SUPABASE_ANON_KEY=...
SUPABASE_JWT_SECRET=...
SUPABASE_SERVICE_KEY=...
ALPACA_KEY_ID=...
ALPACA_SECRET_KEY=...
FINNHUB_KEY=...
FMP_API_KEY=...
RESEND_API_KEY=...
SERPER_API_KEY=...

OVERVIEW_PREWARM_N=25
DEPS_PREWARM_N=20
FACTCHECK_PREWARM_N=15
BRIEF_PREWARM_N=10
PLAUSIBLE_SCRIPT_ID=off
```

`TICKERMOVER_ENV=prod` is not optional. Unset, the app resolves to **dev** and
writes production data into the dev namespace, silently.

Do NOT set `ANTHROPIC_API_KEY` or `ANTHROPIC_FALLBACK` — that is what keeps
the AI free.

```
sudo systemctl restart tickermover
journalctl -u tickermover -f
```

---

## Part 6 — Smoke test BEFORE moving DNS (10 min)

The Caddyfile keeps a plain `:80` block for exactly this, so you can test by IP
while `tickermover.com` still points at Railway.

```
curl -s http://<PUBLIC_IP>/api/status | head -c 400
```

Check, in order:

- HTTP 200
- `universe_size` climbs above 0 within a few minutes (it warms on boot)
- `journalctl` shows no Supabase-fallback warning
- `/terms` renders and says non-commercial, not "business details pending"

If `universe_size` stays 0, the data providers are not authenticating — check
the keys before going further. Everything else is cosmetic by comparison.

---

## Part 7 — Cut over (10 min + propagation)

1. Cloudflare DNS → `tickermover.com` **A** record → the Oracle public IP.
   Also `www`. Set proxy to **DNS only (grey cloud)** initially so Caddy can
   complete the Let's Encrypt HTTP-01 challenge.
2. Wait for propagation, then `https://tickermover.com` should serve with a
   valid certificate — Caddy obtains it automatically, no certbot, no cron.
3. Once TLS works you may switch Cloudflare back to proxied (orange cloud) if
   you want its CDN and analytics.
4. Delete the `:80 { }` block from `/etc/caddy/Caddyfile`, then
   `sudo systemctl reload caddy`.
5. Only now: shut down the Railway project.

---

## Day to day

```
sudo /opt/tickermover/deploy/oracle/deploy.sh   # pull main + restart, safely
journalctl -u tickermover -f                    # logs
systemctl status tickermover                    # health
```

`deploy.sh` polls until the service is **stable**, not until the first success,
and rolls back automatically if it never stabilises.

---

## Honest risks

- **No SLA.** If it goes down, you fix it. For a hobby project that is fine.
- **Capacity.** A1 can be hard to get initially. Once running, you keep it.
- **Idle reclamation.** Oracle reclaims idle Always Free *compute* after long
  inactivity. A site serving traffic with 22 background loops is not idle.
- **One box.** No redundancy. Take a boot volume backup occasionally — the OCI
  console does this in two clicks and it is also free.
