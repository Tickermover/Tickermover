"""
AlphaHunt — Portfolio + closed-trades persistence layer.

Why this exists
---------------
The Top Hunts portfolio and the closed-trades ledger were previously stored as
JSON files under ``data/``. Railway's filesystem is ephemeral: every deploy
restored ``data/model_portfolio.json`` to its committed state and wiped
``data/model_portfolio_history.json`` entirely. Picks vanished without trade
records, breaking the "every pick goes through closed trades" audit promise.

This module moves both files to Supabase using the SUPABASE_SERVICE_KEY (which
bypasses RLS). If Supabase is unreachable or unconfigured, we transparently
fall back to the original JSON files so local development keeps working.

Env-id convention
-----------------
A single Supabase project may back both prod and dev deploys. We tag every
write with an ``env_id``::

    1 = prod   (alphahunt.in)
    2 = dev    (web-production-17a78.up.railway.app + local)

The env is auto-detected from the ``RAILWAY_ENVIRONMENT`` env var if present,
falling back to the explicit ``ALPHAHUNT_ENV`` override (``prod`` / ``dev``).
"""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)

# ── Env detection ──────────────────────────────────────────────────────────
def _detect_env_id() -> int:
    """Return 1 for prod, 2 for dev. Both share one Supabase project."""
    override = (os.environ.get("ALPHAHUNT_ENV") or "").lower().strip()
    if override in ("prod", "production"):
        return 1
    if override in ("dev", "development", "staging"):
        return 2
    rwy = (os.environ.get("RAILWAY_ENVIRONMENT") or "").lower().strip()
    if rwy == "production":
        return 1
    if rwy:
        return 2
    # No Railway env vars → almost certainly local development.
    return 2


_ENV_ID = _detect_env_id()

# Disk fallback paths (same locations the old code used)
_BASE_DIR = Path(__file__).resolve().parent
_PORTFOLIO_PATH = _BASE_DIR / "data" / "model_portfolio.json"
_HISTORY_PATH   = _BASE_DIR / "data" / "model_portfolio_history.json"


class PortfolioStore:
    """Sync data-access wrapper. Sync intentionally — the call-sites in
    app.py are inside async handlers but the disk I/O they replaced was
    blocking, and writes here are infrequent (only on exit / replenish).
    Avoids forcing every caller to await."""

    def __init__(self) -> None:
        self.url = (config.SUPABASE_URL or "").rstrip("/")
        # Prefer service key for writes; fall back to anon for read-only
        # if service key isn't configured. Writes will fail without the
        # service key unless the table has open RLS policies.
        self.service_key = config.SUPABASE_SERVICE_KEY or ""
        self.anon_key    = config.SUPABASE_ANON_KEY or ""
        self.env_id      = _ENV_ID

        self.enabled = bool(self.url and (self.service_key or self.anon_key))
        if not self.enabled:
            logger.warning(
                "PortfolioStore: Supabase not configured (need SUPABASE_URL + "
                "SUPABASE_SERVICE_KEY). Falling back to local JSON files — "
                "portfolio state will NOT survive a Railway deploy."
            )
        elif not self.service_key:
            logger.warning(
                "PortfolioStore: SUPABASE_SERVICE_KEY is unset. Writes will "
                "fail unless RLS is open on the tables (which is unsafe). "
                "Set the service-role key to enable persistence."
            )
        # Cache the last successful Supabase response so reads stay fast
        # within a single process (the data only changes on our writes).
        self._portfolio_cache: dict[str, Any] | None = None
        self._lock = threading.Lock()

    # ── REST helpers ──────────────────────────────────────────────────────

    def _headers(self) -> dict:
        key = self.service_key or self.anon_key
        return {
            "apikey":        key,
            "Authorization": f"Bearer {key}",
            "Content-Type":  "application/json",
            # Tell PostgREST to return the row representation on writes so we
            # can log the affected rows for debug.
            "Prefer":        "return=representation",
        }

    def _get(self, path: str, params: dict | None = None) -> Any:
        with httpx.Client(timeout=8) as c:
            r = c.get(f"{self.url}{path}", headers=self._headers(), params=params or {})
            r.raise_for_status()
            return r.json()

    def _post(self, path: str, body: Any, params: dict | None = None) -> Any:
        with httpx.Client(timeout=8) as c:
            r = c.post(f"{self.url}{path}", headers=self._headers(),
                       json=body, params=params or {})
            r.raise_for_status()
            try:
                return r.json()
            except Exception:
                return {}

    # ── Portfolio (single row JSONB) ──────────────────────────────────────

    def load_portfolio(self) -> dict:
        """Return the active Top Hunts portfolio. Falls back to disk if
        Supabase is unreachable or returns an empty payload."""
        if self.enabled:
            try:
                rows = self._get(
                    "/rest/v1/model_portfolio_state",
                    params={
                        "id":     f"eq.{self.env_id}",
                        "select": "payload",
                        "limit":  "1",
                    },
                )
                if rows:
                    payload = rows[0].get("payload") or {}
                    # Treat seed-empty rows ({picks:[]}) as "not initialized"
                    # so the boot path runs _build_model_portfolio() once.
                    if payload.get("picks"):
                        self._portfolio_cache = payload
                        return dict(payload)
            except Exception as e:
                logger.warning(f"PortfolioStore.load_portfolio Supabase read failed: {e}")
        # Disk fallback (mostly for local dev or first-boot bootstrap)
        if _PORTFOLIO_PATH.exists():
            try:
                with open(_PORTFOLIO_PATH, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"PortfolioStore.load_portfolio disk read failed: {e}")
        return {}

    def save_portfolio(self, portfolio: dict) -> bool:
        """Upsert the portfolio JSON blob. Also mirrors to disk so the
        legacy code path stays consistent during the migration window."""
        ok = False
        if self.enabled and self.service_key:
            try:
                with self._lock:
                    self._post(
                        "/rest/v1/model_portfolio_state",
                        body={
                            "id":         self.env_id,
                            "payload":    portfolio,
                            "updated_at": "now()",
                        },
                        params={"on_conflict": "id"},
                    )
                self._portfolio_cache = portfolio
                ok = True
            except Exception as e:
                logger.error(f"PortfolioStore.save_portfolio Supabase upsert FAILED (env_id={self.env_id}): {e}")
        else:
            logger.debug("PortfolioStore.save_portfolio: Supabase write disabled (no service key), disk only")
        # Disk mirror — keeps the legacy JSON files in sync so reads from
        # any code path that hasn't been migrated yet still see fresh data.
        try:
            _PORTFOLIO_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = _PORTFOLIO_PATH.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(portfolio, f, indent=2)
            os.replace(tmp, _PORTFOLIO_PATH)
        except Exception as e:
            logger.warning(f"PortfolioStore.save_portfolio disk mirror failed: {e}")
        return ok

    # ── Closed trades (append-only) ───────────────────────────────────────

    def load_trades(self, limit: int = 500) -> list[dict]:
        """Return closed trades, newest first. Limit caps the response so
        the closed-trades tab and landing-page track record don't drag
        once we have years of history."""
        rows: list[dict] = []
        if self.enabled:
            try:
                rows = self._get(
                    "/rest/v1/closed_trades",
                    params={
                        "env_id": f"eq.{self.env_id}",
                        "select": "*",
                        "order":  "exit_date.desc,id.desc",
                        "limit":  str(limit),
                    },
                )
            except Exception as e:
                logger.warning(f"PortfolioStore.load_trades Supabase read failed: {e}")
                rows = []
        if not rows and _HISTORY_PATH.exists():
            try:
                with open(_HISTORY_PATH, encoding="utf-8") as f:
                    d = json.load(f)
                    rows = d.get("trades", []) if isinstance(d, dict) else []
            except Exception:
                rows = []
        return rows

    def append_trades(self, new_trades: list[dict]) -> int:
        """Insert one row per closed trade. Returns the count actually
        inserted. Also appends to the disk history file as a belt-and-
        braces fallback so a Supabase outage doesn't silently lose data."""
        if not new_trades:
            return 0
        inserted = 0
        if self.enabled and self.service_key:
            rows = [
                {
                    "env_id":          self.env_id,
                    "ticker":          t.get("ticker"),
                    "name":            t.get("name"),
                    "entry_date":      t.get("entry_date"),
                    "entry_price":     t.get("entry_price"),
                    "pop_at_entry":    t.get("pop_at_entry"),
                    "grade_at_entry":  t.get("grade_at_entry"),
                    "rationale":       (t.get("rationale") or "")[:1000],
                    "sub_sector":      t.get("sub_sector"),
                    "exit_date":       t.get("exit_date"),
                    "exit_price":      t.get("exit_price"),
                    "exit_reason":     t.get("exit_reason"),
                    "exit_label":      t.get("exit_label"),
                    "exit_detail":     (t.get("exit_detail") or "")[:1000],
                    "final_pct":       t.get("final_pct"),
                    "days_held":       t.get("days_held"),
                    "won":             bool(t.get("won")),
                }
                for t in new_trades
            ]
            try:
                resp = self._post("/rest/v1/closed_trades", body=rows)
                inserted = len(resp) if isinstance(resp, list) else len(new_trades)
                logger.info(f"📕 PortfolioStore.append_trades: inserted {inserted} trade(s) into Supabase")
            except Exception as e:
                logger.error(f"PortfolioStore.append_trades Supabase insert FAILED: {e}")
                inserted = 0
        # Disk mirror — append to existing JSON file so disk + DB stay aligned.
        try:
            _HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            history: dict = {}
            if _HISTORY_PATH.exists():
                try:
                    with open(_HISTORY_PATH, encoding="utf-8") as f:
                        history = json.load(f)
                except Exception:
                    history = {}
            if not isinstance(history, dict):
                history = {}
            existing = history.get("trades", []) if isinstance(history.get("trades"), list) else []
            history["trades"] = new_trades + existing
            tmp = _HISTORY_PATH.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(history, f, indent=2)
            os.replace(tmp, _HISTORY_PATH)
        except Exception as e:
            logger.warning(f"PortfolioStore.append_trades disk mirror failed: {e}")
        return inserted


# Singleton — modules import this directly.
store = PortfolioStore()
