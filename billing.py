"""
TickerMover — plan gating helpers  |  tickermover.com

TickerMover is a free, non-commercial research project. There is no paid tier
and no payment integration: Razorpay was removed on 29 Aug 2026 and Stripe on
30 Aug 2026.

The gating vocabulary below is deliberately retained but DORMANT. PRO_GATING
defaults to "0" in config.py, so is_pro() is never consulted to withhold
anything and every feature is available to everyone. It stays because ~18 call
sites in app.py branch on it, and because it is the natural seam if a paid tier
is ever reintroduced — at which point a payment provider would need wiring back
in from scratch.
"""
from __future__ import annotations


def is_pro(plan: str, status: str) -> bool:
    """Return True if the user has an active Pro subscription.

    Always False in practice: nothing sets plan="pro" now that the payment
    path is gone. Kept so the gating call sites still resolve.
    """
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
