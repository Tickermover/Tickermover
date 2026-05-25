"""
email_sender.py — Transactional email via Resend API.
=====================================================

Why this module exists
----------------------
Supabase sends the email+password "Confirm signup" email automatically
(via the custom SMTP we have configured = Resend). But Google OAuth
sign-ups SKIP that flow entirely — Supabase trusts Google's email
verification and never sends a confirmation. So a Google user joins
AlphaHunt and never hears from us.

This module fills that gap: when our /api/auth/on-signin endpoint sees
a brand-new user (created less than 2 minutes ago), it calls
`send_welcome_email()` here to ship our branded welcome via Resend's
REST API directly. Same template Supabase uses for password signups —
just delivered through a different code path.

Environment variables
---------------------
  RESEND_API_KEY    Resend secret key (starts with `re_...`). Use a key
                    *separate* from the supabase-prod SMTP password so
                    you can rotate one without breaking the other. The
                    existing "Onboarding" key in the Resend dashboard
                    is a good candidate (no activity yet, sending-only
                    permission).
  EMAIL_FROM        Sender. Default: "AlphaHunt <noreply@alphahunt.in>"
                    (alphahunt.in is already verified in Resend).
  SITE_ORIGIN       Used to build the "Get started" CTA link.

Failure mode
------------
Functions here NEVER raise. If the API key is missing or Resend is
down, we log a warning and return {"ok": False}. Email is best-effort
— never blocks login.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent / "templates"


async def send_welcome_email(to_email: str) -> dict:
    """Send the AlphaHunt welcome email to a freshly signed-up user.

    Returns {"ok": bool, "id": "...", "error": "..."}.
    Never raises.
    """
    html = _render_welcome_html()
    if not html:
        return {"ok": False, "error": "Welcome template missing"}
    return await _send(
        to=to_email,
        subject="Welcome to AlphaHunt 🎯",
        html=html,
    )


# ── Internals ─────────────────────────────────────────────────────────────

def _render_welcome_html() -> Optional[str]:
    """Load the welcome HTML template and substitute the Supabase
    confirmation URL with a direct link to the app dashboard.

    The template was originally written for Supabase (which substitutes
    {{ .ConfirmationURL }} server-side). When *we* send it for Google
    sign-ups, there's nothing to confirm — we just want the user to land
    in the app. So we rewrite that one Go-Templates-style placeholder to
    a plain dashboard URL before sending.
    """
    path = _TEMPLATES_DIR / "email_welcome.html"
    try:
        html = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.error(f"Welcome email template not found at {path}")
        return None

    dashboard_url = os.environ.get("SITE_ORIGIN", "https://alphahunt.in").rstrip("/") + "/app"
    return html.replace("{{ .ConfirmationURL }}", dashboard_url)


async def _send(to: str, subject: str, html: str) -> dict:
    """Low-level Resend API call. Never raises."""
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender  = os.environ.get("EMAIL_FROM", "AlphaHunt <noreply@alphahunt.in>").strip()

    if not api_key:
        logger.warning("RESEND_API_KEY not set — skipping welcome email.")
        return {"ok": False, "error": "RESEND_API_KEY not configured"}

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type":  "application/json",
                },
                json={
                    "from":    sender,
                    "to":      [to],
                    "subject": subject,
                    "html":    html,
                },
            )
        try:
            body = r.json()
        except Exception:
            body = {"raw": r.text[:300]}

        if r.status_code >= 400:
            logger.error(f"[EMAIL] Resend send failed status={r.status_code} body={body!r}")
            return {"ok": False, "error": body.get("message") or f"HTTP {r.status_code}"}

        logger.info(f"[EMAIL] Welcome sent to {to[:4]}*** id={body.get('id', '?')}")
        return {"ok": True, "id": body.get("id")}

    except Exception as exc:
        logger.error(f"[EMAIL] Send exception: {exc}")
        return {"ok": False, "error": str(exc)}
