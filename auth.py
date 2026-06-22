"""
TickerMover — Supabase Auth Layer  |  tickermover.com
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


# ── JWKS verification — handles new asymmetric Supabase signing keys ───────────
# Supabase rolled out a new asymmetric JWT signing system (ECC P-256 / RSA)
# in 2024-25. Newly created projects sign tokens with the asymmetric private
# key; verification needs the public key from the project's JWKS endpoint.
# Older projects still use HS256 with a shared secret. We support both —
# try JWKS first, fall back to the shared secret.
#
# Cached at module level so repeated verifications don't re-fetch the JWKS.
_jwks_cache: dict = {}
_jwks_cache_ts: float = 0.0
_JWKS_TTL_SECONDS = 3600  # refresh once per hour


def _fetch_jwks(supabase_url: str) -> dict:
    """Fetch and cache the JWKS document from Supabase. Returns {} on failure."""
    global _jwks_cache, _jwks_cache_ts
    import time
    now = time.time()
    if _jwks_cache and (now - _jwks_cache_ts) < _JWKS_TTL_SECONDS:
        return _jwks_cache
    try:
        r = httpx.get(f"{supabase_url}/auth/v1/.well-known/jwks.json", timeout=5.0)
        r.raise_for_status()
        _jwks_cache = r.json()
        _jwks_cache_ts = now
        logger.debug(f"JWKS refreshed — {len(_jwks_cache.get('keys', []))} key(s)")
        return _jwks_cache
    except Exception as e:
        logger.warning(f"JWKS fetch failed (will fall back to HS256): {e}")
        # Return stale cache if available, else empty
        return _jwks_cache or {}


def _decode_jwt_via_jwks(token: str, supabase_url: str) -> Optional[dict]:
    """
    Verify a JWT using the public key from Supabase's JWKS endpoint.
    Handles ECC (P-256), RSA, and asymmetric HS256 keys uniformly.
    Returns the decoded payload, or None if verification fails.
    """
    if not supabase_url:
        return None
    try:
        import jwt as pyjwt
        # Find the right signing key by matching the JWT's `kid` header
        unverified_header = pyjwt.get_unverified_header(token)
        kid = unverified_header.get("kid")
        if not kid:
            # Tokens without a kid header are old HS256 tokens — let HS256 path handle them
            return None
        jwks = _fetch_jwks(supabase_url)
        signing_key = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                # PyJWT's PyJWK helper turns a JWK dict into a usable key object
                signing_key = pyjwt.PyJWK(key).key
                break
        if signing_key is None:
            logger.debug(f"No JWKS key matches kid={kid!r} — try refreshing")
            return None
        # Asymmetric keys ONLY here — never list HS256 alongside an asymmetric
        # signing key (classic alg-confusion: a token forged with HS256 using the
        # public key as the secret would otherwise verify). HS256 has its own path.
        return pyjwt.decode(
            token,
            signing_key,
            algorithms=["ES256", "RS256"],
            options={"verify_aud": False},
        )
    except Exception as e:
        logger.debug(f"JWKS verify failed: {e}")
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

    async def sign_up(self, email: str, password: str, redirect_to: str | None = None) -> dict:
        """Register a new user. Returns {access_token, refresh_token, user} or {error}.
        `redirect_to` sets where the confirmation-email link lands — pass our
        /auth/callback so the session is captured and onboarding fires."""
        if not self.enabled:
            return {"error": "Supabase not configured"}
        path = "/auth/v1/signup"
        if redirect_to:
            from urllib.parse import quote
            path += f"?redirect_to={quote(redirect_to, safe='')}"
        resp = await self._post(path, {"email": email, "password": password})

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

    def oauth_authorize_url(self, provider: str, redirect_to: str) -> str:
        """Build the Supabase OAuth authorize URL for an external provider
        (google, apple, github, etc.). The frontend should `window.location =`
        this URL. Supabase handles the provider handshake and returns the
        user to `redirect_to` with access_token + refresh_token in the URL
        hash fragment."""
        if not self.enabled:
            return ""
        from urllib.parse import urlencode
        qs = urlencode({"provider": provider, "redirect_to": redirect_to})
        return f"{self.url}/auth/v1/authorize?{qs}"

    async def send_magic_link(self, email: str, redirect_to: str) -> dict:
        """Email the user a one-tap sign-in link via Supabase OTP. Costs $0
        on Supabase's free tier (rate-limited) and works with any SMTP
        configured in the Supabase dashboard (Resend free tier ~3k/mo is
        the recommended pairing for low-volume production)."""
        if not self.enabled:
            return {"error": "Supabase not configured"}
        resp = await self._post(
            "/auth/v1/otp",
            {"email": email, "create_user": True,
             "options": {"email_redirect_to": redirect_to}},
        )
        if resp.get("error") or resp.get("error_code"):
            return {"error": resp.get("error_description") or resp.get("msg") or "Magic link send failed"}
        return {"ok": True}

    async def resend_confirmation(self, email: str, redirect_to: str | None = None) -> dict:
        """Resend the sign-up confirmation email (Supabase POST /auth/v1/resend,
        type=signup). Uses the dashboard-configured SMTP (Resend)."""
        if not self.enabled:
            return {"error": "Supabase not configured"}
        body: dict = {"type": "signup", "email": email}
        if redirect_to:
            body["options"] = {"email_redirect_to": redirect_to}
        resp = await self._post("/auth/v1/resend", body)
        if resp.get("error") or resp.get("error_code"):
            return {"error": resp.get("error_description") or resp.get("msg") or "Could not resend confirmation"}
        return {"ok": True}

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

    async def get_user_full(self, access_token: str) -> Optional[dict]:
        """One call to /auth/v1/user returning the fields the account panel
        needs: email, email-verification status, and display name."""
        if not self.enabled:
            return None
        resp = await self._get("/auth/v1/user", token=access_token)
        if not isinstance(resp, dict) or resp.get("error") or not resp.get("id"):
            return None
        md = resp.get("user_metadata") if isinstance(resp.get("user_metadata"), dict) else {}
        return {
            "id":             resp["id"],
            "email":          resp.get("email"),
            "email_verified": bool(resp.get("email_confirmed_at") or resp.get("confirmed_at")),
            "name":           md.get("name") or md.get("full_name") or md.get("display_name") or "",
        }

    async def get_user_metadata(self, access_token: str) -> dict:
        """Return the user's Supabase user_metadata dict ({} on failure).
        Used for cross-device prefs (display name, risk profile, onboarded)."""
        if not self.enabled:
            return {}
        resp = await self._get("/auth/v1/user", token=access_token)
        if not isinstance(resp, dict) or resp.get("error"):
            return {}
        md = resp.get("user_metadata")
        return md if isinstance(md, dict) else {}

    async def update_user_metadata(self, access_token: str, data: dict) -> dict:
        """Shallow-merge `data` into the user's Supabase user_metadata.
        Returns the updated metadata dict, or {'error': ...}."""
        if not self.enabled:
            return {"error": "Supabase not configured"}
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.put(
                f"{self.url}/auth/v1/user",
                json={"data": data},
                headers=self._auth_headers(access_token),
            )
            try:
                resp = r.json()
            except Exception:
                return {"error": r.text or "metadata update failed"}
        if r.status_code >= 400 or resp.get("error") or resp.get("error_code"):
            return {"error": resp.get("error_description") or resp.get("msg")
                    or resp.get("error") or "metadata update failed"}
        md = resp.get("user_metadata")
        return md if isinstance(md, dict) else {}

    # ── JWT verification (local — no network call after JWKS cached) ──────────

    def verify_token(self, access_token: str) -> Optional[dict]:
        """
        Verify a Supabase access token. Tries TWO mechanisms in order:

          1. **Asymmetric JWKS** (preferred): the new Supabase signing system
             uses ECC P-256 / RSA keys. We fetch the public key from the
             project's JWKS endpoint and verify the signature locally.
             Cached for 1 hour, so this is effectively zero-network after
             the first verify.

          2. **Legacy HS256** (fallback): older Supabase projects sign with
             a shared secret. If we have one configured, we try it after
             JWKS fails. This keeps prod working if it hasn't been migrated
             yet AND keeps old tokens valid during a rotation window.

        Returns {user_id, email, role} on success, None on invalid/expired.
        """
        if not access_token:
            return None

        # 1. Asymmetric verification via JWKS
        payload = _decode_jwt_via_jwks(access_token, self.url) if self.url else None

        # 2. Fall back to HS256 with shared secret (legacy)
        if payload is None and self.jwt_secret:
            payload = _decode_jwt(access_token, self.jwt_secret)

        if not payload:
            return None

        # Reject anything that isn't a real end-user token: a verified signature
        # alone isn't enough — the anon and service_role keys are also signed by
        # the project. Require a subject and refuse privileged/non-user roles.
        sub = payload.get("sub")
        role = payload.get("role")
        if not sub or role in ("anon", "service_role"):
            logger.debug(f"Rejecting non-user token (sub={bool(sub)}, role={role!r})")
            return None

        return {
            "user_id": sub,
            "email":   payload.get("email"),
            "role":    role or "authenticated",
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
