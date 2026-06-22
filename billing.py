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
            "automatic_tax[enabled]":            "true",
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
