"""
AlphaHunt — per-stock AI Overview-snapshot store.

The Overview snapshot (cheap, no-web business/risk/edge boxes that auto-load on
every stock open) used to live ONLY in the in-memory SmartCache, so it was lost
on every server restart / Railway redeploy — meaning the same stock re-ran a
paid model call after each deploy. This store mirrors ResearchStore: durable in
Supabase (survives deploys), local JSON fallback for dev. env_id 1=prod, 2=dev.

Supabase table (create once)::

    create table stock_overview (
      env_id        int          not null,
      ticker        text         not null,
      generated_at  timestamptz  not null default now(),
      model         text,
      markdown      text,
      sources       jsonb        default '[]'::jsonb,
      status        text         default 'ready',
      primary key (env_id, ticker)
    );

If Supabase isn't configured, snapshots are written to
``output/overview/{T}.json`` so local dev still works (won't survive a Railway
deploy without a Volume, which is fine for dev).
"""
from __future__ import annotations

import json
import logging
import os
import time
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
_DISK_DIR = _BASE_DIR / "output" / "overview"

# The snapshot is built from our own ground-truth data + the model's training
# knowledge (no live web), so it ages slowly — a day is plenty. Override via env.
TTL_SECONDS = int(os.environ.get("OVERVIEW_TTL_SECONDS", str(24 * 60 * 60)))  # ~24h


class OverviewStore:
    def __init__(self) -> None:
        self.url = (config.SUPABASE_URL or "").rstrip("/")
        self.service_key = config.SUPABASE_SERVICE_KEY or ""
        self.anon_key = config.SUPABASE_ANON_KEY or ""
        self.env_id = _ENV_ID
        self.enabled = bool(self.url and (self.service_key or self.anon_key))
        if not self.enabled:
            logger.warning(
                "OverviewStore: Supabase not configured — Overview snapshots "
                "will be cached to output/overview/*.json (local dev only)."
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

    def _disk_path(self, ticker: str) -> Path:
        return _DISK_DIR / f"{ticker.upper()}.json"

    def get(self, ticker: str) -> dict | None:
        ticker = ticker.upper()
        if self.enabled:
            try:
                with httpx.Client(timeout=8) as c:
                    r = c.get(
                        f"{self.url}/rest/v1/stock_overview",
                        headers=self._headers(),
                        params={
                            "env_id": f"eq.{self.env_id}",
                            "ticker": f"eq.{ticker}",
                            "select": "ticker,generated_at,model,markdown,sources,status",
                            "limit": "1",
                        },
                    )
                    r.raise_for_status()
                    rows = r.json()
                    if rows:
                        return rows[0]
            except Exception as e:
                logger.error(f"OverviewStore.get({ticker}) Supabase error: {e}")
        try:
            p = self._disk_path(ticker)
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return None

    def save(self, ticker: str, doc: dict) -> None:
        """Persist a snapshot. ``doc`` = {markdown, sources, model, status}."""
        ticker = ticker.upper()
        row = {
            "env_id": self.env_id,
            "ticker": ticker,
            "model": doc.get("model", ""),
            "markdown": doc.get("markdown", ""),
            "sources": doc.get("sources", []) or [],
            "status": doc.get("status", "ready"),
        }
        if self.enabled:
            try:
                with httpx.Client(timeout=8) as c:
                    r = c.post(
                        f"{self.url}/rest/v1/stock_overview",
                        headers=self._headers(
                            "return=minimal,resolution=merge-duplicates"
                        ),
                        params={"on_conflict": "env_id,ticker"},
                        json=row,
                    )
                    if r.status_code >= 400:
                        logger.error(
                            f"OverviewStore.save({ticker}) → {r.status_code}: {r.text[:200]}"
                        )
                    r.raise_for_status()
            except Exception as e:
                logger.error(f"OverviewStore.save({ticker}) Supabase error: {e}")
        try:
            disk_doc = dict(row)
            disk_doc["generated_epoch"] = time.time()
            self._disk_path(ticker).write_text(
                json.dumps(disk_doc, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    @staticmethod
    def is_stale(doc: dict | None) -> bool:
        if not doc:
            return True
        epoch = doc.get("generated_epoch")
        if epoch is None:
            ga = doc.get("generated_at")
            if not ga:
                return True
            try:
                from datetime import datetime

                ts = datetime.fromisoformat(ga.replace("Z", "+00:00"))
                epoch = ts.timestamp()
            except Exception:
                return True
        return (time.time() - float(epoch)) > TTL_SECONDS


# Module-level singleton (mirrors research_store.store).
store = OverviewStore()
