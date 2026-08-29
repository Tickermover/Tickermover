#!/usr/bin/env python3
"""
Create a comped TEST PRO user in Supabase and grant Pro instantly.

It does two things with the Supabase SERVICE-ROLE key:
  1. Creates a *pre-confirmed* auth user (email_confirm=true) so the
     confirm-email requirement is bypassed — the dummy account can sign in
     immediately with the password you choose.
  2. Upserts an active 'pro' subscription row for that user, so
     billing.is_pro(plan='pro', status='active') → True everywhere the app
     gates Pro features (AI Overview, Deep Dive, Supply Chain, …).

Re-running is safe: if the user already exists it's looked up by email and the
Pro subscription is re-applied.

Usage (PowerShell):
    $env:SUPABASE_URL="https://xxxx.supabase.co"
    $env:SUPABASE_SERVICE_KEY="<service-role key>"
    python scripts/create_test_user.py --email tester@example.com --password "ChangeMe!234"

NOTE: needs the SERVICE-ROLE key (admin), not the anon key. Nothing is committed
about the account — credentials are only printed to your terminal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _req(method: str, url: str, key: str, body=None, prefer: str | None = None):
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            txt = r.read().decode()
            return r.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as e:
        txt = e.read().decode()
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, txt


def main() -> None:
    ap = argparse.ArgumentParser(description="Create a pre-confirmed Pro test user in Supabase.")
    ap.add_argument("--email", default=os.environ.get("TEST_USER_EMAIL", "tester@example.com"))
    ap.add_argument("--password", default=os.environ.get("TEST_USER_PASSWORD", "AlphaHuntPro!234"))
    ap.add_argument("--url", default=os.environ.get("SUPABASE_URL", ""))
    ap.add_argument("--key", default=os.environ.get("SUPABASE_SERVICE_KEY", ""))
    a = ap.parse_args()

    url = (a.url or "").rstrip("/")
    key = a.key or ""
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY (service-role key) in env or pass --url/--key.")

    # 1) Create the pre-confirmed auth user.
    st, res = _req("POST", f"{url}/auth/v1/admin/users", key, {
        "email": a.email,
        "password": a.password,
        "email_confirm": True,
        "user_metadata": {"display_name": "Test Pro", "onboarded": True, "risk_profile": "aggressive"},
    })
    uid = res.get("id") if isinstance(res, dict) else None
    if st in (200, 201) and uid:
        print(f"✓ Created user {a.email} (id={uid})")
    else:
        print(f"… admin create returned {st}: {res}\n  Looking up existing user by email…")
        st2, res2 = _req("GET", f"{url}/auth/v1/admin/users?per_page=500", key)
        users = res2.get("users") if isinstance(res2, dict) else (res2 if isinstance(res2, list) else [])
        for u in (users or []):
            if (u.get("email") or "").lower() == a.email.lower():
                uid = u.get("id")
                break
        if uid:
            print(f"✓ Found existing user {a.email} (id={uid})")
            # Make sure the password + confirmation are set for the existing account.
            _req("PUT", f"{url}/auth/v1/admin/users/{uid}", key,
                 {"password": a.password, "email_confirm": True})
    if not uid:
        sys.exit("Could not create or find the user — check the email and that the key is the service-role key.")

    # 2) Grant Pro via an active subscription row (upsert on user_id).
    st3, res3 = _req(
        "POST", f"{url}/rest/v1/subscriptions?on_conflict=user_id", key,
        {"user_id": uid, "plan": "pro", "status": "active", "valid_until": "2099-12-31T00:00:00Z"},
        prefer="resolution=merge-duplicates,return=minimal",
    )
    if st3 in (200, 201, 204):
        print("✓ Granted Pro (plan=pro, status=active)")
    else:
        print(f"⚠ subscription upsert → {st3}: {res3}")
        print("  Fallback: add this email to the AI_ALLOW_EMAILS env var and redeploy "
              "(it comps Pro without a subscription row).")

    print("\n── Test Pro account ──────────────")
    print(f"  email:    {a.email}")
    print(f"  password: {a.password}")
    print("  Sign in at /app — Pro features (Supply Chain, AI Deep Dive, Overview, watchlist) unlock.")


if __name__ == "__main__":
    main()
