"""
TickerMover — Razorpay Billing  |  tickermover.com
Handles order creation, subscription gating, and webhook verification.

Plans:
  free — 180 stocks, Hot List, Fundamentals (no login required)
  pro  — ₹499/mo | $7/mo: Watchlist, Email Alerts, Real-time data

Razorpay docs:
  https://razorpay.com/docs/payments/payment-gateway/web-integration/
  https://razorpay.com/docs/payments/subscriptions/

Webhook endpoint: POST /api/payment/webhook
Set this URL in Razorpay Dashboard → Webhooks → URL.
Secret: set RAZORPAY_WEBHOOK_SECRET in env or config.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.razorpay.com/v1"

# SECURITY: webhook signature verification fails CLOSED by default. An unsigned
# webhook (no secret configured) is REJECTED — a forged event must never be able
# to grant Pro. For local dev where you replay events without a secret, set
# ALLOW_UNSIGNED_WEBHOOKS=1 explicitly. Never set it in production.
_ALLOW_UNSIGNED_WEBHOOKS = os.environ.get("ALLOW_UNSIGNED_WEBHOOKS", "").strip().lower() in ("1", "true", "yes", "on")

# Stripe Tax is OFF by default. It is a paid add-on (~0.5% per transaction) that
# needs an active Tax setting and an origin address, and asking for automatic tax
# while it is inactive makes EVERY checkout session creation fail — a hard launch
# blocker for a seller who isn't VAT-registered and doesn't need it. Set
# STRIPE_AUTOMATIC_TAX=1 once you are registered and have enabled Stripe Tax;
# until then the listed price is what the customer pays.
_AUTOMATIC_TAX = os.environ.get("STRIPE_AUTOMATIC_TAX", "").strip().lower() in ("1", "true", "yes", "on")

# Monthly plan amounts (paise = INR × 100)
PRO_AMOUNT_INR   = 499_00   # ₹499 in paise
PRO_AMOUNT_USD   = 7_00     # $7 in cents (for Stripe fallback, not used here)
PRO_PLAN_PERIOD  = "monthly"
PRO_INTERVAL     = 1


class RazorpayClient:
    """
    Async Razorpay client — order creation, subscription, webhook verification.
    One shared instance wired into app.py.
    """

    def __init__(self, key_id: str, key_secret: str, webhook_secret: str = ""):
        self.key_id        = key_id
        self.key_secret    = key_secret
        self.webhook_secret = webhook_secret
        self.enabled       = bool(key_id and key_secret)
        self._auth         = (key_id, key_secret) if self.enabled else None

    # ── REST helper ──────────────────────────────────────────────────────────────

    async def _post(self, path: str, body: dict) -> dict:
        if not self.enabled:
            return {"error": "Razorpay not configured"}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    f"{_BASE}{path}",
                    json=body,
                    auth=self._auth,
                )
                data = r.json()
                if r.status_code >= 400:
                    err = data.get("error", {})
                    return {"error": err.get("description") or err.get("code") or "Razorpay error"}
                return data
        except Exception as e:
            logger.error(f"Razorpay {path}: {e}")
            return {"error": str(e)}

    async def _get(self, path: str) -> dict:
        if not self.enabled:
            return {"error": "Razorpay not configured"}
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(f"{_BASE}{path}", auth=self._auth)
                return r.json()
        except Exception as e:
            logger.error(f"Razorpay GET {path}: {e}")
            return {"error": str(e)}

    # ── Plans ────────────────────────────────────────────────────────────────────

    async def get_or_create_pro_plan(self) -> Optional[str]:
        """
        Return the Razorpay plan_id for TickerMover Pro.
        Creates it if it doesn't exist.
        Cache the result in config or env: RAZORPAY_PLAN_ID
        """
        # Check env first
        import config as cfg
        plan_id = getattr(cfg, "RAZORPAY_PLAN_ID", "") or ""
        if plan_id:
            return plan_id

        # Create plan via API
        result = await self._post("/plans", {
            "period":   PRO_PLAN_PERIOD,
            "interval": PRO_INTERVAL,
            "item": {
                "name":     "TickerMover Pro",
                "amount":   PRO_AMOUNT_INR,
                "currency": "INR",
                "description": "Full access: Watchlist, Alerts, Real-time data",
            },
        })
        if result.get("error"):
            logger.warning(f"Razorpay plan create failed: {result['error']}")
            return None
        pid = result.get("id")
        logger.info(f"Razorpay plan created: {pid}")
        return pid

    # ── Subscriptions ────────────────────────────────────────────────────────────

    async def create_subscription(self, plan_id: str, customer_email: str) -> dict:
        """
        Create a Razorpay subscription for a user.
        Returns {id: sub_id, short_url: checkout_link} or {error: ...}
        """
        return await self._post("/subscriptions", {
            "plan_id":          plan_id,
            "total_count":      120,   # 10 years max — effectively lifetime
            "quantity":         1,
            "customer_notify":  1,
            "notes": {
                "email": customer_email,
                "product": "TickerMover Pro",
            },
        })

    async def cancel_subscription(self, subscription_id: str, cancel_at_cycle_end: bool = True) -> dict:
        """Cancel a subscription (at end of billing cycle by default)."""
        return await self._post(
            f"/subscriptions/{subscription_id}/cancel",
            {"cancel_at_cycle_end": 1 if cancel_at_cycle_end else 0},
        )

    async def get_subscription(self, subscription_id: str) -> dict:
        """Fetch subscription details."""
        return await self._get(f"/subscriptions/{subscription_id}")

    async def fetch_order(self, order_id: str) -> dict:
        """Fetch a server-created order so /verify can confirm the REAL paid
        amount/currency/status server-side (never trust the client). Returns the
        order entity {amount, amount_paid, currency, status, receipt, ...} or
        {error}."""
        return await self._get(f"/orders/{order_id}")

    # ── One-time orders (alternative to subscriptions) ────────────────────────────

    async def create_order(self, amount_paise: int = PRO_AMOUNT_INR, receipt: str = "") -> dict:
        """
        Create a one-time Razorpay order (for first payment or manual upgrade).
        Returns order object with id, amount, currency.
        """
        return await self._post("/orders", {
            "amount":   amount_paise,
            "currency": "INR",
            "receipt":  receipt or "alphahunt_pro",
            "notes":    {"product": "TickerMover Pro"},
        })

    # ── Webhook verification ──────────────────────────────────────────────────────

    def verify_webhook(self, raw_body: bytes, signature: str) -> bool:
        """
        Verify Razorpay webhook signature.
        signature = X-Razorpay-Signature header value.
        Returns True if valid.
        """
        if not self.webhook_secret:
            if _ALLOW_UNSIGNED_WEBHOOKS:
                logger.warning("RAZORPAY_WEBHOOK_SECRET not set — accepting UNSIGNED webhook (dev opt-in)")
                return True
            logger.error("RAZORPAY_WEBHOOK_SECRET not set — REJECTING webhook (fail-closed)")
            return False   # fail closed: never trust an unsigned webhook in prod
        expected = hmac.new(
            self.webhook_secret.encode(),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def parse_webhook(self, raw_body: bytes) -> dict:
        """Parse webhook JSON body."""
        try:
            return json.loads(raw_body)
        except Exception:
            return {}


# ── Stripe (primary gateway for the UK/global launch) ─────────────────────────
# Hosted Stripe Checkout (subscription mode) + webhooks. With Stripe's Managed
# Payments / merchant-of-record enabled on the account, tax is handled by Stripe
# (automatic_tax). The app never touches card data — Checkout is hosted, the
# client just redirects to the returned URL.

_STRIPE_BASE = "https://api.stripe.com/v1"


class StripeClient:
    """Async Stripe client — hosted Checkout, Billing Portal, webhook verify."""

    def __init__(self, secret_key: str, webhook_secret: str = "", price_id: str = ""):
        self.secret_key     = secret_key
        self.webhook_secret = webhook_secret
        self.price_id       = price_id
        self.enabled        = bool(secret_key and price_id)

    async def _post(self, path: str, data: dict) -> dict:
        """Stripe's API is form-encoded; auth is HTTP-Basic with the secret key."""
        if not self.secret_key:
            return {"error": "Stripe not configured"}
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.post(f"{_STRIPE_BASE}{path}", data=data, auth=(self.secret_key, ""))
                j = r.json()
                if r.status_code >= 400:
                    return {"error": (j.get("error") or {}).get("message") or f"HTTP {r.status_code}"}
                return j
        except Exception as e:
            logger.error(f"Stripe {path}: {e}")
            return {"error": str(e)}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.secret_key:
            return {"error": "Stripe not configured"}
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(f"{_STRIPE_BASE}{path}", params=params or {},
                                auth=(self.secret_key, ""))
                j = r.json()
                if r.status_code >= 400:
                    return {"error": (j.get("error") or {}).get("message") or f"HTTP {r.status_code}"}
                return j
        except Exception as e:
            logger.error(f"Stripe GET {path}: {e}")
            return {"error": str(e)}

    # Events the webhook handler acts on. A subscription that is created or
    # cancelled outside Checkout (dunning, portal cancel, admin refund) only
    # reaches us through these, so a missing one silently leaves users on the
    # wrong plan.
    REQUIRED_EVENTS = (
        "checkout.session.completed",
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    )

    async def preflight(self, *, webhook_url: str = "", promo_code: str = "") -> dict:
        """Answer 'is Stripe actually ready to take money?' from Stripe's own
        API, one check per onboarding step, so the remaining work is visible
        without clicking through the dashboard.

        Returns booleans and short labels ONLY — never key material, account
        ids, customer data or business identifiers."""
        sk = self.secret_key or ""
        out: dict = {
            "mode": "live" if sk.startswith("sk_live") else ("test" if sk.startswith("sk_test") else
                    ("restricted" if sk.startswith("rk_") else "unset" if not sk else "unknown")),
            "env": {
                "STRIPE_SECRET_KEY":     bool(sk),
                "STRIPE_PRICE_ID":       bool(self.price_id),
                "STRIPE_WEBHOOK_SECRET": bool(self.webhook_secret),
            },
            "enabled": self.enabled,
        }
        blockers: list[str] = []
        warnings: list[str] = []
        if not sk:
            out["blockers"] = ["Set STRIPE_SECRET_KEY (exact name) in Railway — nothing can be checked without it."]
            out["warnings"] = []
            out["ready"] = False
            return out

        # 1. Key works + is the account actually activated for charges?
        acct = await self._get("/account")
        key_ok = not acct.get("error")
        if acct.get("error"):
            out["account"] = {"ok": False, "error": str(acct["error"])[:160]}
            blockers.append("The secret key was rejected by Stripe — check it was copied whole and matches live/test mode.")
        else:
            caps = acct.get("capabilities") or {}
            out["account"] = {
                "ok":                True,
                "country":           acct.get("country"),
                "default_currency":  (acct.get("default_currency") or "").upper() or None,
                "details_submitted": bool(acct.get("details_submitted")),
                "charges_enabled":   bool(acct.get("charges_enabled")),
                "payouts_enabled":   bool(acct.get("payouts_enabled")),
                "card_payments":     caps.get("card_payments"),
            }
            if not acct.get("details_submitted"):
                blockers.append("Stripe account activation is unfinished — complete the business/identity form in the dashboard.")
            if not acct.get("charges_enabled"):
                blockers.append("Charges are not enabled on the account yet, so Checkout cannot take a payment.")
            if not acct.get("payouts_enabled"):
                warnings.append("Payouts are not enabled — you can charge, but Stripe will hold the money until a bank account is verified.")

        # 2. The price the app checks out with must exist, be active, and recur
        #    (Checkout is created in subscription mode).
        if self.price_id:
            pr = await self._get(f"/prices/{self.price_id}", {"expand[]": "product"})
            if pr.get("error"):
                out["price"] = {"ok": False, "error": str(pr["error"])[:160]}
                if key_ok:
                    blockers.append("STRIPE_PRICE_ID does not resolve — it must be a price_… id from the SAME mode as the secret key.")
            else:
                prod = pr.get("product") if isinstance(pr.get("product"), dict) else {}
                rec = pr.get("recurring") or {}
                amt = pr.get("unit_amount")
                out["price"] = {
                    "ok":        True,
                    "active":    bool(pr.get("active")),
                    "recurring": bool(rec),
                    "interval":  rec.get("interval"),
                    "currency":  (pr.get("currency") or "").upper() or None,
                    "amount":    (amt / 100) if isinstance(amt, int) else None,
                    "product":   prod.get("name"),
                }
                if not rec:
                    blockers.append("The configured price is one-off, not recurring — Checkout runs in subscription mode and will fail.")
                if not pr.get("active"):
                    blockers.append("The configured price is archived in Stripe — activate it or point STRIPE_PRICE_ID at the live one.")
        else:
            blockers.append("Set STRIPE_PRICE_ID to the recurring Pro price (price_…).")

        # 3. Stripe Tax only has to be active when the app actually asks for
        #    automatic tax. With STRIPE_AUTOMATIC_TAX unset the listed price is
        #    what the customer pays, so Tax being off is not a blocker — it is
        #    only reported so the state is visible when you do register.
        tax = await self._get("/tax/settings")
        if tax.get("error"):
            out["tax"] = {"requested_by_app": _AUTOMATIC_TAX, "ok": not _AUTOMATIC_TAX,
                          "error": str(tax["error"])[:160]}
            if _AUTOMATIC_TAX:
                warnings.append("Could not read Stripe Tax settings — the app requests automatic tax, so checkout will fail if it is not active.")
        else:
            head = (tax.get("head_office") or {}).get("address") or {}
            active = tax.get("status") == "active"
            out["tax"] = {"requested_by_app": _AUTOMATIC_TAX, "ok": active or not _AUTOMATIC_TAX,
                          "status": tax.get("status"), "origin_country": head.get("country")}
            if _AUTOMATIC_TAX and not active:
                blockers.append("STRIPE_AUTOMATIC_TAX is on but Stripe Tax is not active — Checkout will 502 until you enable it and set the origin address (or unset the variable).")

        # 4. The Billing Portal needs a saved configuration or /portal 502s the
        #    moment a subscriber tries to cancel.
        portal = await self._get("/billing_portal/configurations", {"limit": "5", "is_default": "true"})
        if portal.get("error"):
            out["portal"] = {"ok": False, "error": str(portal["error"])[:160]}
            warnings.append("Could not read the Billing Portal configuration.")
        else:
            cfgs = [c for c in (portal.get("data") or []) if c.get("active")]
            out["portal"] = {"ok": bool(cfgs), "configurations": len(cfgs)}
            if not cfgs:
                warnings.append("No active Billing Portal configuration — subscribers will not be able to manage or cancel Pro until you save one.")

        # 5. Webhook endpoint — our URL, enabled, carrying the four events the
        #    handler acts on.
        hooks = await self._get("/webhook_endpoints", {"limit": "50"})
        if hooks.get("error"):
            out["webhook"] = {"ok": False, "error": str(hooks["error"])[:160]}
            warnings.append("Could not list webhook endpoints.")
        else:
            want = (webhook_url or "").rstrip("/")
            mine = [h for h in (hooks.get("data") or [])
                    if want and (h.get("url") or "").rstrip("/") == want]
            if not mine:
                out["webhook"] = {"ok": False, "registered": False, "expected_url": want,
                                  "endpoints_on_account": len(hooks.get("data") or [])}
                if key_ok:
                    blockers.append(f"No webhook endpoint points at {want} — Pro would never switch on after payment.")
            else:
                h = mine[0]
                evs = set(h.get("enabled_events") or [])
                missing = [] if "*" in evs else [e for e in self.REQUIRED_EVENTS if e not in evs]
                out["webhook"] = {"ok": (h.get("status") == "enabled" and not missing),
                                  "registered": True, "status": h.get("status"),
                                  "missing_events": missing}
                if h.get("status") != "enabled":
                    blockers.append("The webhook endpoint exists but is disabled in Stripe.")
                if missing:
                    blockers.append("Webhook endpoint is missing events: " + ", ".join(missing))
                if not self.webhook_secret:
                    blockers.append("STRIPE_WEBHOOK_SECRET is not set — the app rejects every webhook (fail-closed), so no one would ever get Pro.")

        # 6. Checkout is created with allow_promotion_codes=true. If the
        #    marketing copy names a code, it has to exist and be active.
        if promo_code:
            pc = await self._get("/promotion_codes", {"code": promo_code, "limit": "1"})
            if pc.get("error"):
                out["promo"] = {"code": promo_code, "ok": False, "error": str(pc["error"])[:160]}
            else:
                rows = pc.get("data") or []
                live = [r for r in rows if r.get("active")]
                out["promo"] = {"code": promo_code, "ok": bool(live), "found": len(rows)}
                if not live:
                    warnings.append(f"Promotion code {promo_code} is not active in Stripe — anyone typing it at checkout gets an error.")

        out["blockers"] = blockers
        out["warnings"] = warnings
        out["ready"] = not blockers
        return out

    async def create_checkout_session(self, *, user_id: str, email: str,
                                      success_url: str, cancel_url: str) -> dict:
        """Hosted subscription Checkout. user_id is threaded through so the
        webhook can map the payment back to the account. Returns {id, url} or
        {error}."""
        data = {
            "mode":                              "subscription",
            "line_items[0][price]":              self.price_id,
            "line_items[0][quantity]":           "1",
            "success_url":                       success_url,
            "cancel_url":                        cancel_url,
            "client_reference_id":               user_id,
            "metadata[user_id]":                 user_id,
            "subscription_data[metadata][user_id]": user_id,
            "allow_promotion_codes":             "true",
            "automatic_tax[enabled]":            "true" if _AUTOMATIC_TAX else "false",
            "billing_address_collection":        "auto",
        }
        if email:
            data["customer_email"] = email
        return await self._post("/checkout/sessions", data)

    async def create_portal_session(self, customer_id: str, return_url: str) -> dict:
        """Stripe Billing Portal so a user can manage / cancel their plan."""
        return await self._post("/billing_portal/sessions",
                                {"customer": customer_id, "return_url": return_url})

    def verify_webhook(self, payload: bytes, sig_header: str) -> Optional[dict]:
        """Verify the Stripe-Signature header (t=…,v1=…) and return the parsed
        event, or None if invalid. Falls back to unverified parse only when no
        webhook secret is configured (dev)."""
        if not self.webhook_secret:
            if _ALLOW_UNSIGNED_WEBHOOKS:
                logger.warning("STRIPE_WEBHOOK_SECRET not set — accepting UNSIGNED webhook (dev opt-in)")
                try:
                    return json.loads(payload)
                except Exception:
                    return None
            logger.error("STRIPE_WEBHOOK_SECRET not set — REJECTING webhook (fail-closed)")
            return None   # fail closed: never trust an unsigned webhook in prod
        try:
            parts = dict(p.split("=", 1) for p in sig_header.split(",") if "=" in p)
            t, v1 = parts.get("t"), parts.get("v1")
            if not t or not v1:
                return None
            # Replay guard: reject events whose timestamp is outside a 5-minute
            # tolerance (matches Stripe's own libraries), bounding signature replay.
            try:
                if abs(int(__import__("time").time()) - int(t)) > 300:
                    logger.error("Stripe webhook timestamp outside tolerance — rejecting")
                    return None
            except (TypeError, ValueError):
                return None
            signed = t.encode() + b"." + payload
            expected = hmac.new(self.webhook_secret.encode(), signed, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, v1):
                logger.error("Stripe webhook signature mismatch")
                return None
            return json.loads(payload)
        except Exception as e:
            logger.error(f"Stripe webhook verify error: {e}")
            return None


# ── Plan gating helpers ───────────────────────────────────────────────────────

def is_pro(plan: str, status: str) -> bool:
    """Return True if the user has an active Pro subscription."""
    return plan == "pro" and status == "active"


PRO_FEATURES = {
    "watchlist",
    "email_alerts",
    "realtime_data",
    "portfolio_tracker",
}

FREE_FEATURES = {
    "hot_list",
    "fundamentals",
    "risk_radar",
    "news",
    "sector_map",
    "data_sources",
    "guide",
}
