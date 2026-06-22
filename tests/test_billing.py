"""
Tests for billing.py — Pro gating + webhook signature verification.

These guard the QA blockers B1/B2 (webhooks must FAIL CLOSED on a missing secret)
and the signature math behind B3. If any of these regress, a forged webhook could
grant Pro for free — so treat a failure here as release-blocking.

Run: python tests/run_all.py   (no pytest needed)  — or  pytest tests/
"""
import hashlib
import hmac
import time

import billing
from billing import RazorpayClient, StripeClient, is_pro


# ── Pro gating ────────────────────────────────────────────────────────────────
def test_is_pro_true_only_for_active_pro():
    assert is_pro("pro", "active") is True
    assert is_pro("pro", "canceled") is False
    assert is_pro("free", "active") is False
    assert is_pro("", "") is False
    assert is_pro(None, None) is False  # must not raise


# ── Stripe webhook verification ───────────────────────────────────────────────
def _stripe_sig(secret: str, payload: bytes, t: int | None = None) -> str:
    t = int(time.time()) if t is None else t
    signed = str(t).encode() + b"." + payload
    v1 = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={t},v1={v1}"


def test_stripe_valid_signature_returns_event():
    secret, payload = "whsec_test", b'{"type":"checkout.session.completed"}'
    c = StripeClient("sk_test", secret, "price_x")
    out = c.verify_webhook(payload, _stripe_sig(secret, payload))
    assert out == {"type": "checkout.session.completed"}


def test_stripe_bad_signature_rejected():
    secret, payload = "whsec_test", b'{"type":"x"}'
    c = StripeClient("sk_test", secret, "price_x")
    assert c.verify_webhook(payload, "t=%d,v1=deadbeef" % int(time.time())) is None


def test_stripe_no_secret_fails_closed():
    # B1: with no webhook secret AND no dev opt-in, an unsigned webhook must be
    # REJECTED (None), never parsed-and-trusted.
    assert billing._ALLOW_UNSIGNED_WEBHOOKS is False, "test env must not opt into unsigned"
    c = StripeClient("sk_test", "", "price_x")
    assert c.verify_webhook(b'{"type":"x"}', "") is None


def test_stripe_stale_timestamp_rejected():
    secret, payload = "whsec_test", b'{"type":"x"}'
    c = StripeClient("sk_test", secret, "price_x")
    stale = _stripe_sig(secret, payload, t=int(time.time()) - 600)  # 10 min old
    assert c.verify_webhook(payload, stale) is None


# ── Razorpay webhook verification ─────────────────────────────────────────────
def test_razorpay_valid_signature_true():
    secret, body = "rzp_secret", b'{"event":"payment.captured"}'
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    c = RazorpayClient("key", "ks", secret)
    assert c.verify_webhook(body, sig) is True


def test_razorpay_bad_signature_false():
    c = RazorpayClient("key", "ks", "rzp_secret")
    assert c.verify_webhook(b'{"event":"x"}', "bad") is False


def test_razorpay_no_secret_fails_closed():
    # B2: missing secret + no dev opt-in -> reject (False), not allow.
    assert billing._ALLOW_UNSIGNED_WEBHOOKS is False
    c = RazorpayClient("key", "ks", "")
    assert c.verify_webhook(b'{"event":"x"}', "anything") is False


def test_pro_amount_is_positive():
    assert billing.PRO_AMOUNT_INR > 0
