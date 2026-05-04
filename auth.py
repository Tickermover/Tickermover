"""
AlphaHunt — Supabase Auth Layer  |  alphahunt.in
Handles JWT verification, user lookups, and watchlist CRUD via Supabase REST API.

Endpoints consumed:
  POST /auth/v1/token?grant_type=password   — email/password sign-in
  POST /auth/v1/signup                      — new user registration
  POST /auth/v1/token?grant_type=refresh_token — token refresh
  POST /auth/v1/logout                      — sign-out (revoke token)
  GET  /auth/v1/user                        — get user from access token
  GET/POST/DELETE /rest/v1/watchlists       — watchlist CRUD
  GET/POST/PATCH  /rest/v1/subscriptions    — subscription read

JWT verification is done locally using the SUPABASE_JWT_SECRET so we
never make a network round-trip for every protected API call.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# ── lazy import jwt to avoid hard dep when Supabase not configured ─────────────
def _decode_jwt(token: str, secret: str) -> Optional[dict]:
    try:
        import jwt as pyjwt
        return pyjwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"verify_aud": False},
        )
    except Exception as e:
        logger.debug(f"JWT decode failed: {e}")
        return None


# ── SupabaseClient ─────────────────────────────────────────────────────────────

class SupabaseClient:
    """
    Thin async wrapper around Supabase Auth + Postgres REST API.
    One shared instance wired into app.py.
    """

    def __init__(self, url: str, anon_key: str, jwt_secret: str):
        self.url        = url.rstrip("/") if url else ""
        self.anon_key   = anon_key
        self.jwt_secret = jwt_secret
        self.enabled    = bool(url and anon_key)

    # ── helpers ────────────────────────────────────────────────────────────────

    def _auth_headers(self, access_token: Optional[str] = None) -> dict:
        h = {
            "apikey":       self.anon_key,
            "Content-Type": "application/json",
        }
        if access_token:
            h["Authorization"] = f"Bearer {access_token}"
        return h

    async def _post(self, path: str, body: dict, token: str | None = None) -> dict:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{self.url}{path}",
                json=body,
                headers=self._auth_headers(token),
            )
            try:
                return r.json()
            except Exception:
                return {"error": r.text, "status_code": r.status_code}

    async def _get(self, path: str, token: str | None = None, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"{self.url}{path}",
                headers=self._auth_headers(token),
                params=params or {},
            )
            try:
                return r.json()
            except Exception:
                return {"error": r.text}

    async def _delete(self, path: str, token: str, params: dict | None = None) -> bool:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.delete(
                f"{self.url}{path}",
                headers=self._auth_headers(token),
                params=params or {},
            )
            return r.status_code in (200, 204)

    # ── Auth ───────────────────────────────────────────────────────────────────

    async def sign_up(self, email: str, password: str) -> dict:
        """Register a new user. Returns {access_token, refresh_token, user} or {error}."""
        if not self.enabled:
            return {"error": "Supabase not configured"}
        resp = await self._post("/auth/v1/signup", {"email": email, "password": password})

        # Check if the user was actually created (has an id / access_token).
        # Supabase can return an error for "confirmation email sending failed" even when
        # the account IS created — we treat that as success so users aren't blocked.
        user_obj = resp.get("user") or {}
        has_user = bool(user_obj.get("id") or resp.get("access_token"))

        if (resp.get("error") or resp.get("error_code")) and not has_user:
            err_msg = resp.get("error_description") or resp.get("msg") or "Sign-up failed"
            return {"error": err_msg}

        # Success (with or without email confirmation)
        return {
            "access_token":  resp.get("access_token"),
            "refresh_token": resp.get("refresh_token"),
            "user": {
                "id":    user_obj.get("id"),
                "email": user_obj.get("email") or email,
            },
        }

    async def sign_in(self, email: str, password: str) -> dict:
        """Sign in with email + password. Returns {access_token, refresh_token, user} or {error}."""
        if not self.enabled:
            return {"error": "Supabase not configured"}
        resp = await self._post(
            "/auth/v1/token?grant_type=password",
            {"email": email, "password": password},
        )
        if resp.get("error") or resp.get("error_code"):
            return {"error": resp.get("error_description") or resp.get("msg") or "Sign-in failed"}
        return {
            "access_token":  resp.get("access_token"),
            "refresh_token": resp.get("refresh_token"),
            "expires_in":    resp.get("expires_in", 3600),
            "user": {
                "id":    resp.get("user", {}).get("id"),
                "email": resp.get("user", {}).get("email"),
            },
        }

    async def refresh_token(self, refresh_token: str) -> dict:
        """Refresh an access token. Returns new {access_token, refresh_token} or {error}."""
        if not self.enabled:
            return {"error": "Supabase not configured"}
        resp = await self._post(
            "/auth/v1/token?grant_type=refresh_token",
            {"refresh_token": refresh_token},
        )
        if resp.get("error"):
            return {"error": resp.get("error_description") or "Refresh failed"}
        return {
            "access_token":  resp.get("access_token"),
            "refresh_token": resp.get("refresh_token"),
            "expires_in":    resp.get("expires_in", 3600),
        }

    async def sign_out(self, access_token: str) -> bool:
        """Revoke the access token server-side."""
        if not self.enabled:
            return True
        resp = await self._post("/auth/v1/logout", {}, token=access_token)
        return not resp.get("error")

    async def update_password(self, access_token: str, new_password: str) -> dict:
        """
        Update the password for the user identified by access_token.
        Used by the /reset-password flow — Supabase issues a recovery
        access_token in the password-reset email link, which the user
        passes here along with their chosen new password.

        PUT /auth/v1/user with {"password": "..."} updates the current
        user's password. The token must be a valid Supabase access token
        (recovery tokens are accepted just like regular session tokens).
        """
        if not self.enabled:
            return {"error": "Supabase not configured"}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.put(
                f"{self.url}/auth/v1/user",
                json={"password": new_password},
                headers=self._auth_headers(access_token),
            )
            try:
                resp = r.json()
            except Exception:
                resp = {"error": r.text, "status_code": r.status_code}
        if r.status_code >= 400 or resp.get("error") or resp.get("error_code"):
            return {"error": resp.get("error_description") or resp.get("msg") or resp.get("error") or "Password update failed"}
        return {"ok": True, "user": {"id": resp.get("id"), "email": resp.get("email")}}

    async def get_user(self, access_token: str) -> Optional[dict]:
        """Get user profile from a valid access token."""
        if not self.enabled:
            return None
        resp = await self._get("/auth/v1/user", token=access_token)
        if resp.get("error") or not resp.get("id"):
            return None
        return {
            "id":    resp["id"],
            "email": resp.get("email"),
            "role":  resp.get("role", "authenticated"),
        }

    # ── JWT verification (local — no network call) ─────────────────────────────

    def verify_token(self, access_token: str) -> Optional[dict]:
        """
        Verify JWT locally using SUPABASE_JWT_SECRET.
        Returns payload dict {sub: user_id, email, role} or None if invalid/expired.
        """
        if not self.jwt_secret:
            return None
        payload = _decode_jwt(access_token, self.jwt_secret)
        if not payload:
            return None
        return {
            "user_id": payload.get("sub"),
            "email":   payload.get("email"),
            "role":    payload.get("role", "authenticated"),
        }

    # ── Subscription ──────────────────────────────────────────────────────────

    async def get_subscription(self, access_token: str, user_id: str) -> dict:
        """Get subscription record for user. Returns {plan, status, valid_until} or default free."""
        if not self.enabled:
            return {"plan": "free", "status": "active"}
        resp = await self._get(
            "/rest/v1/subscriptions",
            token=access_token,
            params={"user_id": f"eq.{user_id}", "select": "plan,status,valid_until", "limit": "1"},
        )
        if isinstance(resp, list) and resp:
            return resp[0]
        return {"plan": "free", "status": "active"}

    async def upsert_subscription(self, access_token: str, data: dict) -> bool:
        """Create or update a subscription record."""
        if not self.enabled:
            return False
        resp = await self._post("/rest/v1/subscriptions?on_conflict=user_id", data, token=access_token)
        return not (isinstance(resp, dict) and resp.get("error"))

    # ── Watchlist ─────────────────────────────────────────────────────────────

    async def get_watchlist(self, access_token: str, user_id: str) -> list[str]:
        """Return list of ticker strings in the user's watchlist."""
        if not self.enabled:
            return []
        resp = await self._get(
            "/rest/v1/watchlists",
            token=access_token,
            params={"user_id": f"eq.{user_id}", "select": "ticker"},
        )
        if isinstance(resp, list):
            return [r["ticker"] for r in resp if r.get("ticker")]
        return []

    async def add_to_watchlist(self, access_token: str, user_id: str, ticker: str) -> bool:
        """Add a ticker to the user's watchlist (upsert — safe to call if already present)."""
        if not self.enabled:
            return False
        resp = await self._post(
            "/rest/v1/watchlists?on_conflict=user_id,ticker",
            {"user_id": user_id, "ticker": ticker.upper()},
            token=access_token,
        )
        return not (isinstance(resp, dict) and resp.get("error"))

    async def remove_from_watchlist(self, access_token: str, user_id: str, ticker: str) -> bool:
        """Remove a ticker from the user's watchlist."""
        if not self.enabled:
            return False
        return await self._delete(
            "/rest/v1/watchlists",
            token=access_token,
            params={"user_id": f"eq.{user_id}", "ticker": f"eq.{ticker.upper()}"},
        )
