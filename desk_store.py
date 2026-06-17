"""
TickerMover — durable Desk-Report store.

The pre/post-market desk editions carry an AI-written narrative (paid). They were
cached only in the in-memory SmartCache, so every Railway redeploy wiped them and
the startup `_desk_publisher` re-generated BOTH editions from scratch — paying for
AI on every build even when no user opened anything. This store persists the
frozen editions in Supabase (survives deploys), local JSON fallback for dev.
env_id 1=prod, 2=dev.

Supabase table (create once)::

    create table desk_report (
      env_id        int          not null,
      kind          text         not null,        -- 'pre' | 'post'
      edition_date  text,
      report        jsonb        default '{}'::jsonb,
      updated_at    timestamptz  not null default now(),
      primary key (env_id, kind)
    );
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

import config

logger = logging.getLogger(__name__)


def _detect_env_id() -> int:
    override = (os.environ.get("ALPHAHUNT_ENV") or "").lower().strip()
    if override in ("prod", "production"):
        return 1
    if override in ("dev", "development", "staging"):
        return 2
    rwy = (os.environ.get("RAILWAY_ENVIRONMENT") or "").lower().strip()
    if rwy == "production":
        return 1
    return 2 if rwy else 2


_ENV_ID = _detect_env_id()
_BASE_DIR = Path(__file__).resolve().parent
_DISK_DIR = _BASE_DIR / "output" / "desk"


class DeskStore:
    def __init__(self) -> None:
        self.url = (config.SUPABASE_URL or "").rstrip("/")
        self.service_key = config.SUPABASE_SERVICE_KEY or ""
        self.anon_key = config.SUPABASE_ANON_KEY or ""
        self.env_id = _ENV_ID
        self.enabled = bool(self.url and (self.service_key or self.anon_key))
        if not self.enabled:
            logger.warning(
                "DeskStore: Supabase not configured — desk editions cached to "
                "output/desk/*.json (local dev only)."
            )
        try:
            _DISK_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _headers(self, prefer: str = "return=representation") -> dict:
        key = self.service_key or self.anon_key
        return {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": prefer,
        }

    def _disk_path(self, kind: str) -> Path:
        return _DISK_DIR / f"{kind}.json"

    def get(self, kind: str) -> dict | None:
        """Return the stored report dict for a kind ('pre'|'post'), or None."""
        kind = (kind or "").lower()
        if self.enabled:
            try:
                with httpx.Client(timeout=8) as c:
                    r = c.get(
                        f"{self.url}/rest/v1/desk_report",
                        headers=self._headers(),
                        params={
                            "env_id": f"eq.{self.env_id}",
                            "kind": f"eq.{kind}",
                            "select": "report",
                            "limit": "1",
                        },
                    )
                    r.raise_for_status()
                    rows = r.json()
                    if rows and isinstance(rows[0].get("report"), dict):
                        return rows[0]["report"]
            except Exception as e:
                logger.error(f"DeskStore.get({kind}) Supabase error: {e}")
        try:
            p = self._disk_path(kind)
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return None

    def save(self, kind: str, report: dict) -> None:
        kind = (kind or "").lower()
        edition_date = ((report or {}).get("edition") or {}).get("date")
        row = {
            "env_id": self.env_id,
            "kind": kind,
            "edition_date": edition_date,
            "report": report or {},
        }
        if self.enabled:
            try:
                with httpx.Client(timeout=8) as c:
                    r = c.post(
                        f"{self.url}/rest/v1/desk_report",
                        headers=self._headers("return=minimal,resolution=merge-duplicates"),
                        params={"on_conflict": "env_id,kind"},
                        json=row,
                    )
                    if r.status_code >= 400:
                        logger.error(f"DeskStore.save({kind}) → {r.status_code}: {r.text[:200]}")
                    r.raise_for_status()
            except Exception as e:
                logger.error(f"DeskStore.save({kind}) Supabase error: {e}")
        try:
            self._disk_path(kind).write_text(
                json.dumps(report or {}, ensure_ascii=False, default=str), encoding="utf-8"
            )
        except Exception:
            pass


# Module-level singleton (mirrors research_store.store).
store = DeskStore()
