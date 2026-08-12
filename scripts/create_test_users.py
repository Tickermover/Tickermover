#!/usr/bin/env python3
"""
Batch version of create_test_user.py — create N pre-confirmed Pro test accounts.

For each email it:
  1. Creates a pre-confirmed auth user (email_confirm=true) so the confirm-email
     step is skipped and the account can sign in immediately.
  2. Upserts an active 'pro' subscription row, so billing.is_pro() → True and
     every Pro gate (AI Overview, Deep Dive, Supply Chain, watchlist, …) unlocks.

Existing accounts are looked up by email and re-granted — re-running is safe.
Unlike db/2026-08-12-grant-pro-by-email.sql (which only grants to emails that
already signed up), this CREATES the accounts, so use it for throwaway testers
whose passwords you control.

Usage (PowerShell):
    $env:SUPABASE_URL="https://xxxx.supabase.co"
    $env:SUPABASE_SERVICE_KEY="<service-role key>"
    python scripts/create_test_users.py --count 10
    python scripts/create_test_users.py --emails a@x.com,b@y.com
    python scripts/create_test_users.py --file testers.txt   # one email per line

NOTE: needs the SERVICE-ROLE key (admin), not the anon key. Credentials are only
printed to your terminal and optionally written to --out (gitignore that file).
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

PRO_UNTIL = "2099-12-31T00:00:00Z"


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


def _find_user_id(url: str, key: str, email: str) -> str | None:
    """Page through the admin user list looking for this email."""
    page = 1
    while page <= 20:                       # 20 x 200 = 4000 users, plenty
        st, res = _req("GET", f"{url}/auth/v1/admin/users?page={page}&per_page=200", key)
        users = res.get("users") if isinstance(res, dict) else (res if isinstance(res, list) else [])
        if not users:
            return None
        for u in users:
            if (u.get("email") or "").lower() == email.lower():
                return u.get("id")
        page += 1
    return None


def provision(url: str, key: str, email: str, password: str) -> tuple[bool, str]:
    """Create-or-find the account and grant Pro. Returns (ok, note)."""
    st, res = _req("POST", f"{url}/auth/v1/admin/users", key, {
        "email": email,
        "password": password,
        "email_confirm": True,
        "user_metadata": {"display_name": email.split("@")[0], "onboarded": True},
    })
    uid = res.get("id") if isinstance(res, dict) else None
    note = "created"
    if not (st in (200, 201) and uid):
        uid = _find_user_id(url, key, email)
        if not uid:
            return False, f"could not create or find ({st}: {res})"
        # Reset password + confirmation so the printed credentials actually work.
        _req("PUT", f"{url}/auth/v1/admin/users/{uid}", key,
             {"password": password, "email_confirm": True})
        note = "existing (password reset)"

    st3, res3 = _req(
        "POST", f"{url}/rest/v1/subscriptions?on_conflict=user_id", key,
        {"user_id": uid, "plan": "pro", "status": "active", "valid_until": PRO_UNTIL},
        prefer="resolution=merge-duplicates,return=minimal",
    )
    if st3 not in (200, 201, 204):
        return False, f"{note}, but Pro grant failed ({st3}: {res3})"
    return True, note


def main() -> None:
    ap = argparse.ArgumentParser(description="Create N pre-confirmed Pro test users in Supabase.")
    ap.add_argument("--count", type=int, default=0,
                    help="Generate this many accounts as <prefix>N@<domain>.")
    ap.add_argument("--prefix", default="tester", help="Local-part prefix for --count. Default: tester")
    ap.add_argument("--domain", default="tickermover.test",
                    help="Domain for --count. Default: tickermover.test (never receives mail).")
    ap.add_argument("--emails", default="", help="Comma-separated explicit emails.")
    ap.add_argument("--file", default="", help="File with one email per line.")
    ap.add_argument("--password", default="", help="Shared password. Default: a random one per account.")
    ap.add_argument("--out", default="", help="Write the credential table to this file.")
    ap.add_argument("--url", default=os.environ.get("SUPABASE_URL", ""))
    ap.add_argument("--key", default=os.environ.get("SUPABASE_SERVICE_KEY", ""))
    a = ap.parse_args()

    url = (a.url or "").rstrip("/")
    key = a.key or ""
    if not url or not key:
        sys.exit("Set SUPABASE_URL and SUPABASE_SERVICE_KEY (service-role key) in env or pass --url/--key.")

    emails: list[str] = []
    if a.emails:
        emails += [e.strip() for e in a.emails.split(",") if e.strip()]
    if a.file:
        with open(a.file, encoding="utf-8") as fh:
            emails += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    if a.count:
        emails += [f"{a.prefix}{i}@{a.domain}" for i in range(1, a.count + 1)]
    # De-dupe, preserving order.
    emails = list(dict.fromkeys(e.lower() for e in emails))
    if not emails:
        sys.exit("Nothing to do — pass --count, --emails or --file.")

    rows, failures = [], 0
    for em in emails:
        pw = a.password or ("Pro!" + secrets.token_urlsafe(12))
        ok, note = provision(url, key, em, pw)
        print(f"{'OK ' if ok else 'FAIL'}  {em:<38} {note}")
        if ok:
            rows.append((em, pw))
        else:
            failures += 1

    print("\n── Test Pro accounts ─────────────────────────────────────")
    for em, pw in rows:
        print(f"  {em:<38} {pw}")
    print(f"\n  {len(rows)} granted, {failures} failed. Sign in at /app — Pro features unlock.")
    print("  Pro status is cached 300s per user (app.py:_PRO_CACHE_TTL); sign out/in to clear.")

    if a.out and rows:
        with open(a.out, "w", encoding="utf-8") as fh:
            fh.write("email,password\n")
            for em, pw in rows:
                fh.write(f"{em},{pw}\n")
        print(f"  Credentials written to {a.out} — do NOT commit this file.")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
