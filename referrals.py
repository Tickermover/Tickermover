"""referrals.py — "Give a month, get a month".

The landing page has advertised this since launch with nothing behind it:
no code per user, no attribution, no credit. This module is that machinery.

Design notes worth keeping:

* CODES ARE DERIVED, NOT ALLOCATED. A code is a truncated HMAC of the user
  id, so it is stable across sessions and needs no generator or uniqueness
  table. It is registered into the kv store on first request purely so the
  reverse lookup (code -> user) exists; nothing is lost if that write fails,
  because the same user always derives the same code.

* CREDIT IS EARNED ON CLEARED PAYMENT, NOT ON SIGNUP. That is the whole
  anti-abuse design: a referral costs marginal server time until the referee
  actually pays, so fake accounts earn nothing. The webhook is the only
  caller that can mark a month earned.

* CREDIT IS APPLIED AS A STRIPE CUSTOMER BALANCE where possible, NOT by
  writing valid_until. Extending valid_until looks right until Stripe's next
  invoice webhook overwrites it with the true period end — the free month
  would silently evaporate. A negative customer balance is what Stripe
  itself applies to the next invoice, so it survives.

Storage is the durable kv store; no schema change.
  ref_code    CODE            -> {user_id}
  ref_by      referee_user_id -> {referrer, credited, at}
  ref_earned  referrer_id     -> {months, from[], applied}
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

logger = logging.getLogger(__name__)

# Codes are public (they live in a share URL), so the salt only needs to stop
# someone deriving OTHER users' codes from a known user id.
_SALT = (os.environ.get("REFERRAL_SALT") or os.environ.get("SUPABASE_SERVICE_KEY") or "tm-ref").encode()

# Unambiguous alphabet — no O/0, I/1, so a code read aloud or retyped survives.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_CODE_LEN = 7

# Cap, matching what the landing page promises ("up to 12 free months a year").
MAX_MONTHS_PER_YEAR = 12
_MONTH_CENTS = 1999          # one month of Pro, in pence — mirrors PRO price


def _kv():
    from kv_store import store
    return store


def code_for(user_id: str) -> str:
    """Stable, public referral code for a user. Derived, so it never needs
    allocating and cannot collide with a previously-issued one for the same id."""
    if not user_id:
        return ""
    dig = hmac.new(_SALT, str(user_id).encode(), hashlib.sha256).digest()
    n = int.from_bytes(dig[:8], "big")
    out = []
    for _ in range(_CODE_LEN):
        out.append(_ALPHABET[n % len(_ALPHABET)])
        n //= len(_ALPHABET)
    return "".join(out)


def register_code(user_id: str) -> str:
    """Derive the code and make it resolvable. Idempotent."""
    code = code_for(user_id)
    if not code:
        return ""
    try:
        kv = _kv()
        if not (kv.get("ref_code", code) or {}).get("user_id"):
            kv.set("ref_code", code, {"user_id": user_id, "at": time.time()})
    except Exception as exc:                    # a failed write is recoverable
        logger.warning("referral: register_code %s: %s", code, exc)
    return code


def resolve(code: str) -> str:
    """code -> user_id, or '' if unknown."""
    code = (code or "").strip().upper()
    if not code:
        return ""
    try:
        return str((_kv().get("ref_code", code) or {}).get("user_id") or "")
    except Exception:
        return ""


def claim(new_user_id: str, code: str) -> dict:
    """Attribute a new account to a referrer. Returns {ok, reason}.

    Refuses self-referral and re-attribution: a user's referrer is written
    once and never changed, so someone cannot re-point an existing account at
    a friend's code after the fact.
    """
    code = (code or "").strip().upper()
    if not new_user_id or not code:
        return {"ok": False, "reason": "missing"}
    referrer = resolve(code)
    if not referrer:
        return {"ok": False, "reason": "unknown_code"}
    if referrer == new_user_id:
        return {"ok": False, "reason": "self"}
    try:
        kv = _kv()
        if (kv.get("ref_by", new_user_id) or {}).get("referrer"):
            return {"ok": False, "reason": "already_claimed"}
        kv.set("ref_by", new_user_id,
               {"referrer": referrer, "credited": False, "at": time.time()})
    except Exception as exc:
        logger.warning("referral: claim for %s: %s", new_user_id, exc)
        return {"ok": False, "reason": "error"}
    return {"ok": True, "referrer": referrer}


def _year_key() -> str:
    return time.strftime("%Y", time.gmtime())


def on_first_payment(user_id: str) -> str:
    """Called from the Stripe webhook when a referee's payment clears.

    Marks the month earned and returns the REFERRER's user id (or '' if this
    user was not referred, was already credited, or the referrer is at the
    annual cap). Idempotent: a redelivered webhook cannot double-credit.
    """
    if not user_id:
        return ""
    try:
        kv = _kv()
        rec = kv.get("ref_by", user_id) or {}
        referrer = rec.get("referrer")
        if not referrer or rec.get("credited"):
            return ""
        yk = _year_key()
        earned = kv.get("ref_earned", referrer) or {}
        if earned.get("year") != yk:                 # rolls over each year
            earned = {"year": yk, "months": 0, "from": []}
        if int(earned.get("months") or 0) >= MAX_MONTHS_PER_YEAR:
            logger.info("referral: %s at annual cap, not crediting", referrer[:8])
            return ""
        earned["months"] = int(earned.get("months") or 0) + 1
        earned.setdefault("from", []).append(user_id)
        kv.set("ref_earned", referrer, earned)
        rec["credited"] = True
        rec["credited_at"] = time.time()
        kv.set("ref_by", user_id, rec)
        return referrer
    except Exception as exc:
        logger.warning("referral: on_first_payment %s: %s", user_id, exc)
        return ""


def stats(user_id: str) -> dict:
    """Everything the referral UI needs for one user."""
    code = register_code(user_id)
    earned = {}
    try:
        earned = _kv().get("ref_earned", user_id) or {}
    except Exception:
        pass
    months = int(earned.get("months") or 0) if earned.get("year") == _year_key() else 0
    return {
        "code":      code,
        "months":    months,
        "referred":  len(earned.get("from") or []) if earned.get("year") == _year_key() else 0,
        "cap":       MAX_MONTHS_PER_YEAR,
        "remaining": max(0, MAX_MONTHS_PER_YEAR - months),
    }


async def apply_credit(stripe_client, referrer_id: str) -> bool:
    """Give the referrer one month, in the way that actually survives.

    A negative customer balance is what Stripe applies to the next invoice, so
    it holds. Writing valid_until instead would be overwritten by the next
    subscription webhook with the real period end — the month would vanish
    with nothing in the logs to say why.

    Returns False when the referrer has no Stripe customer yet (they are on
    free); the month stays banked in ref_earned and can be applied when they
    subscribe.
    """
    if not referrer_id or not stripe_client or not getattr(stripe_client, "enabled", False):
        return False
    try:
        from kv_store import store as kv
        cust = (kv.get("stripe_customer", referrer_id) or {}).get("customer_id")
    except Exception:
        cust = None
    if not cust:
        logger.info("referral: %s has no Stripe customer — month banked", referrer_id[:8])
        return False
    try:
        res = await stripe_client._post(
            f"/customers/{cust}/balance_transactions",
            {"amount": -_MONTH_CENTS, "currency": "gbp",
             "description": "TickerMover referral — one month of Pro"},
        )
        if isinstance(res, dict) and res.get("error"):
            logger.warning("referral: credit failed for %s: %s", referrer_id[:8], res["error"])
            return False
        logger.info("🎁 referral: credited one month to %s", referrer_id[:8])
        return True
    except Exception as exc:
        logger.warning("referral: credit exception for %s: %s", referrer_id[:8], exc)
        return False
