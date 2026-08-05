"""
stripe_setup.py  ─  Create everything TickerMover Pro needs inside Stripe, in
                    one command, and print the values to paste into Railway.

Usage (PowerShell):
    $env:STRIPE_SECRET_KEY = "sk_test_…"          # your key, your machine
    python scripts/stripe_setup.py

Usage (bash):
    STRIPE_SECRET_KEY=sk_test_… python scripts/stripe_setup.py

The key is read from the environment, never passed as an argument, so it does
not end up in your shell history — and it is never printed back out.

What it creates (matching what the site already advertises):
    • Product  "TickerMover Pro"
    • Price    £19.99 / month, recurring          → STRIPE_PRICE_ID
    • Coupon   50% off for 3 months
    • Promo    WELCOME50 pointing at that coupon
    • Webhook  https://tickermover.com/api/billing/stripe/webhook
               listening for the four events the app acts on
                                                  → STRIPE_WEBHOOK_SECRET

SAFE TO RE-RUN. Every step looks for what it would create first and reuses it,
so running twice does not leave you with two products or two promo codes.

LIVE KEYS ARE REFUSED unless you pass --live, so a stray sk_live_ in the
environment cannot silently create real objects while you are still testing.

Options:
    --live              allow an sk_live_ key (required for the real launch)
    --amount 19.99      monthly price
    --currency gbp      price currency
    --promo WELCOME50   promotion code text ('' to skip the coupon entirely)
    --percent 50        discount size
    --months 3          how many months the discount repeats
    --url <endpoint>    webhook URL (defaults to the production one)
    --dry-run           show what would be created, create nothing
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx

API = "https://api.stripe.com/v1"
PRODUCT_NAME = "TickerMover Pro"
WEBHOOK_URL = "https://tickermover.com/api/billing/stripe/webhook"
EVENTS = [
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
]


class Stripe:
    def __init__(self, key: str, dry_run: bool = False):
        self.auth = (key, "")
        self.dry_run = dry_run

    def get(self, path: str, params: dict | None = None) -> dict:
        r = httpx.get(f"{API}{path}", params=params or {}, auth=self.auth, timeout=30)
        return self._unwrap(r, "GET", path)

    def post(self, path: str, data) -> dict:
        if self.dry_run:
            print(f"    [dry-run] POST {path}")
            return {"id": "dry_run", "secret": "whsec_dry_run"}
        r = httpx.post(f"{API}{path}", data=data, auth=self.auth, timeout=30)
        return self._unwrap(r, "POST", path)

    @staticmethod
    def _unwrap(r, verb: str, path: str) -> dict:
        try:
            j = r.json()
        except Exception:
            die(f"{verb} {path} returned non-JSON (HTTP {r.status_code})")
        if r.status_code >= 400:
            die(f"{verb} {path} failed: {(j.get('error') or {}).get('message') or r.status_code}")
        return j


def die(msg: str) -> None:
    # Plain ASCII on purpose: this is run from a Windows console, where a cp1252
    # terminal turns a fancy glyph into mojibake right where an error must read
    # clearly.
    print(f"\n  ERROR: {msg}\n", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(description="Set up Stripe for TickerMover Pro.")
    p.add_argument("--live", action="store_true", help="allow a live (sk_live_) key")
    p.add_argument("--amount", type=float, default=19.99)
    p.add_argument("--currency", default="gbp")
    p.add_argument("--promo", default="WELCOME50")
    p.add_argument("--percent", type=float, default=50)
    p.add_argument("--months", type=int, default=3)
    p.add_argument("--url", default=WEBHOOK_URL)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    key = (os.environ.get("STRIPE_SECRET_KEY") or "").strip()
    if not key:
        die("STRIPE_SECRET_KEY is not set. Put your Stripe secret key in that "
            "environment variable and re-run (see the usage note at the top).")
    if key.startswith("sk_live") and not a.live:
        die("That is a LIVE key. Re-run with --live if you really mean to create "
            "real billing objects; otherwise switch to your sk_test_ key first.")
    mode = "LIVE" if key.startswith("sk_live") else "test"
    s = Stripe(key, a.dry_run)

    print(f"\n  Stripe setup - {mode} mode{' (dry run)' if a.dry_run else ''}\n")

    # ── Account ──────────────────────────────────────────────────────────────
    acct = s.get("/account")
    print(f"  Account   {acct.get('country') or '?'} | "
          f"charges {'on' if acct.get('charges_enabled') else 'OFF'} | "
          f"payouts {'on' if acct.get('payouts_enabled') else 'OFF'}")
    if mode == "LIVE" and not acct.get("charges_enabled"):
        print("            -> activation is unfinished, so Stripe will not take a real payment yet")

    # ── Product + price ──────────────────────────────────────────────────────
    # Reuse an existing "TickerMover Pro" rather than making a second one.
    prods = s.get("/products", {"active": "true", "limit": "100"}).get("data") or []
    prod = next((x for x in prods if (x.get("name") or "").strip().lower() == PRODUCT_NAME.lower()), None)
    if prod:
        print(f"  Product   reusing {PRODUCT_NAME}")
    else:
        prod = s.post("/products", {"name": PRODUCT_NAME,
                                    "description": "Full access to TickerMover Pro."})
        print(f"  Product   created {PRODUCT_NAME}")

    unit = int(round(a.amount * 100))
    cur = a.currency.lower()
    prices = s.get("/prices", {"product": prod["id"], "active": "true", "limit": "100"}).get("data") or []
    price = next((x for x in prices
                  if x.get("unit_amount") == unit
                  and (x.get("currency") or "").lower() == cur
                  and (x.get("recurring") or {}).get("interval") == "month"), None)
    if price:
        print(f"  Price     reusing {a.amount:.2f} {cur.upper()}/month")
    else:
        price = s.post("/prices", {"product": prod["id"], "unit_amount": str(unit),
                                   "currency": cur, "recurring[interval]": "month"})
        print(f"  Price     created {a.amount:.2f} {cur.upper()}/month")

    # ── Coupon + promotion code ──────────────────────────────────────────────
    promo_note = "skipped"
    if a.promo:
        existing = s.get("/promotion_codes", {"code": a.promo, "limit": "1"}).get("data") or []
        live_codes = [c for c in existing if c.get("active")]
        if live_codes:
            promo_note = f"reusing {a.promo}"
        else:
            coupon = s.post("/coupons", {
                "percent_off": str(a.percent),
                "duration": "repeating",
                "duration_in_months": str(a.months),
                "name": f"{a.percent:.0f}% off for {a.months} months",
            })
            s.post("/promotion_codes", {"coupon": coupon["id"], "code": a.promo})
            promo_note = f"created {a.promo} ({a.percent:.0f}% off, {a.months} months)"
        print(f"  Promo     {promo_note}")

    # ── Webhook ──────────────────────────────────────────────────────────────
    # The signing secret is only returned when the endpoint is CREATED, so an
    # endpoint that already exists cannot hand it back — say so plainly rather
    # than pretending the setup is complete.
    hooks = s.get("/webhook_endpoints", {"limit": "100"}).get("data") or []
    mine = next((h for h in hooks if (h.get("url") or "").rstrip("/") == a.url.rstrip("/")), None)
    secret = None
    if mine:
        missing = [e for e in EVENTS if e not in (mine.get("enabled_events") or [])
                   and "*" not in (mine.get("enabled_events") or [])]
        if missing:
            # Repeated form keys go as a dict with a LIST value: httpx encodes
            # {"k": [a, b]} as k=a&k=b. A list of (key, value) tuples is NOT a
            # form body to httpx — it tries to send it as raw content and dies
            # inside h11 with "expected a bytes-like object, tuple found".
            s.post(f"/webhook_endpoints/{mine['id']}",
                   {"enabled_events[]": list(EVENTS)})
            print(f"  Webhook   endpoint existed - added {len(missing)} missing event(s)")
        else:
            print("  Webhook   endpoint already registered with all four events")
    else:
        created = s.post("/webhook_endpoints",
                         {"url": a.url, "description": "TickerMover Pro",
                          "enabled_events[]": list(EVENTS)})
        secret = created.get("secret")
        print("  Webhook   created")

    # ── What to paste into Railway ───────────────────────────────────────────
    print("\n  -- Set these in Railway (exact names) --\n")
    print(f"  STRIPE_SECRET_KEY      = (the {mode} key you just used)")
    print(f"  STRIPE_PRICE_ID        = {price['id']}")
    if secret:
        print(f"  STRIPE_WEBHOOK_SECRET  = {secret}")
    else:
        print("  STRIPE_WEBHOOK_SECRET  = (open the endpoint in the Stripe dashboard ->")
        print("                            'Signing secret' -> reveal -> whsec_...)")
        print("                           Stripe only returns it when the endpoint is")
        print("                           first created, so it cannot be read back here.")
    print("\n  Then check the whole setup end-to-end:")
    print("    curl -s https://tickermover.com/api/billing/selftest\n")


if __name__ == "__main__":
    main()
