"""
Tests for billing.py — plan gating.

The webhook signature tests that used to live here went with the payment
integrations (Razorpay 29 Aug 2026, Stripe 30 Aug 2026). TickerMover takes no
payments, so there is no webhook to forge and no Pro to grant.

Run: python tests/run_all.py   (no pytest needed)  — or  pytest tests/
"""
from billing import is_pro


def test_is_pro_true_only_for_active_pro():
    assert is_pro("pro", "active") is True


def test_is_pro_false_for_inactive_or_free():
    assert is_pro("pro", "canceled") is False
    assert is_pro("free", "active") is False
    assert is_pro("", "") is False
