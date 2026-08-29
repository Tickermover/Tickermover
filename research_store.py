"""
TickerMover — per-stock AI Deep-Dive research store.

Stores the generated research brief for each ticker so we never regenerate on
page-load (slow + costly). Mirrors PortfolioStore: Supabase in prod, local JSON
fallback for dev. Tagged with the same env_id convention (1=prod, 2=dev).

Supabase table (create once)::

    create table stock_research (
      env_id        int          not null,
      ticker        text         not null,
      generated_at  timestamptz  not null default now(),
      model         text,
      markdown      text,
      sources       jsonb        default '[]'::jsonb,
      status        text         default 'ready',
      primary key (env_id, ticker)
    );

If Supabase isn't configured, briefs are written to ``output/research/{T}.json``
so local dev still works (won't survive a Railway deploy, which is fine for dev).
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx

import config

logger = logging.getLogger(__name__)


def _detect_env_id() -> int:
    # TICKERMOVER_ENV is the current name; ALPHAHUNT_ENV is the pre-rebrand
    # fallback, kept only so the retired Railway project still resolves.
    override = (os.environ.get("TICKERMOVER_ENV")
                or os.environ.get("ALPHAHUNT_ENV") or "").lower().strip()
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
_DISK_DIR = _BASE_DIR / "output" / "research"

# How long a brief stays "fresh" before /api/research treats it as stale and
# kicks off a regeneration. Bumped 20h → 30 days (2026-06-13) to fit the
# flat-cost model: first Pro open of a stock generates the web-grounded
# Deep-Dive once, then it's free for everyone for the month. Trade-off: a
# catalyst/news line can lag up to ~30 days (user-accepted). Override via env.
TTL_SECONDS = int(os.environ.get("RESEARCH_TTL_SECONDS", str(30 * 24 * 60 * 60)))  # 30 days


class ResearchStore:
    def __init__(self) -> None:
        self.url = (config.SUPABASE_URL or "").rstrip("/")
        self.service_key = config.SUPABASE_SERVICE_KEY or ""
        self.anon_key = config.SUPABASE_ANON_KEY or ""
        self.env_id = _ENV_ID
        self.enabled = bool(self.url and (self.service_key or self.anon_key))
        if not self.enabled:
            logger.warning(
                "ResearchStore: Supabase not configured — Deep-Dive briefs will "
                "be cached to output/research/*.json (local dev only)."
            )
        try:
            _DISK_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # ── REST helpers (same shape as PortfolioStore) ───────────────────────
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

    # ── Public API ────────────────────────────────────────────────────────
    def get(self, ticker: str) -> dict | None:
        """Return the stored brief dict for a ticker, or None if absent."""
        ticker = ticker.upper()
        if self.enabled:
            try:
                with httpx.Client(timeout=8) as c:
                    r = c.get(
                        f"{self.url}/rest/v1/stock_research",
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
                logger.error(f"ResearchStore.get({ticker}) Supabase error: {e}")
        # disk fallback
        try:
            p = self._disk_path(ticker)
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return None

    def save(self, ticker: str, doc: dict) -> None:
        """Persist a brief. ``doc`` = {markdown, sources, model, status}."""
        ticker = ticker.upper()
        row = {
            "env_id": self.env_id,
            "ticker": ticker,
            "model": doc.get("model", ""),
            "markdown": doc.get("markdown", ""),
            "sources": doc.get("sources", []),
            "status": doc.get("status", "ready"),
            # generated_at handled by DB default; also store epoch on disk
        }
        if self.enabled:
            try:
                with httpx.Client(timeout=8) as c:
                    r = c.post(
                        f"{self.url}/rest/v1/stock_research",
                        headers=self._headers(
                            "return=minimal,resolution=merge-duplicates"
                        ),
                        params={"on_conflict": "env_id,ticker"},
                        json=row,
                    )
                    if r.status_code >= 400:
                        logger.error(
                            f"ResearchStore.save({ticker}) → {r.status_code}: {r.text[:200]}"
                        )
                    r.raise_for_status()
            except Exception as e:
                logger.error(f"ResearchStore.save({ticker}) Supabase error: {e}")
        # always mirror to disk (acts as cache + dev fallback)
        try:
            disk_doc = dict(row)
            disk_doc["generated_epoch"] = time.time()
            self._disk_path(ticker).write_text(
                json.dumps(disk_doc, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    @staticmethod
    def is_stale(doc: dict | None, last_earnings_epoch: float | None = None) -> bool:
        """True if missing, older than the 30-day TTL, OR an earnings report was
        released AFTER the brief was generated. The earnings trigger refreshes the
        catalysts/risks the moment a new quarter lands (whichever comes first —
        the 30-day clock or the next report); the regenerated brief then restarts
        the 30-day clock. `last_earnings_epoch` is the most recent PAST release
        (epoch seconds); None disables the earnings trigger. Uses disk epoch when
        present; otherwise parses the Supabase row's generated_at (ISO)."""
        if not doc:
            return True
        epoch = doc.get("generated_epoch")
        if epoch is None:
            # Supabase row without local epoch — parse generated_at if present.
            ga = doc.get("generated_at")
            if not ga:
                return True
            try:
                from datetime import datetime, timezone

                ts = datetime.fromisoformat(ga.replace("Z", "+00:00"))
                epoch = ts.timestamp()
            except Exception:
                return True
        epoch = float(epoch)
        if last_earnings_epoch is not None and last_earnings_epoch > epoch:
            return True
        return (time.time() - epoch) > TTL_SECONDS


# Module-level singleton (mirrors persistence.store).
store = ResearchStore()
