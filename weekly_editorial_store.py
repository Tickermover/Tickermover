"""
TickerMover — durable store for the signed WEEKLY EDITORIAL (Opus 4.8).

Mirrors selection_store / overview_store: Supabase-backed so the weekly edition
survives Railway redeploys, with a local-JSON fallback for dev. env_id 1=prod,
2=dev.

One frozen edition per ISO week, keyed by the Monday's ISO date (week_start).
The article is generated once on Sunday evening ET and then served frozen for the
whole week (8-day TTL covers the gap before the next Sunday rebuild).

Supabase table (create once — see db/2026-06-24-weekly-editorial.sql)::

    create table weekly_editorial (
      env_id        int          not null,
      week_start    text         not null,
      generated_at  timestamptz  not null default now(),
      model         text,
      article       jsonb        not null,
      status        text         default 'ready',
      primary key (env_id, week_start)
    );

Without Supabase, editions are written to ``output/weekly/{WEEK}.json`` so local
dev still works.
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
    return 2


_ENV_ID = _detect_env_id()
_BASE_DIR = Path(__file__).resolve().parent
_DISK_DIR = _BASE_DIR / "output" / "weekly"

# 8 days — the edition is rebuilt every Sunday; 8 days bridges to the next rebuild
# so a never-refreshed read is still considered fresh within its own week.
TTL_SECONDS = int(os.environ.get("WEEKLY_TTL_SECONDS", str(8 * 24 * 60 * 60)))


class WeeklyEditorialStore:
    def __init__(self) -> None:
        self.url = (config.SUPABASE_URL or "").rstrip("/")
        self.service_key = config.SUPABASE_SERVICE_KEY or ""
        self.anon_key = config.SUPABASE_ANON_KEY or ""
        self.env_id = _ENV_ID
        self.enabled = bool(self.url and (self.service_key or self.anon_key))
        if not self.enabled:
            logger.warning(
                "WeeklyEditorialStore: Supabase not configured — weekly editions "
                "cached to output/weekly/*.json (local dev only)."
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

    def _disk_path(self, week_start: str) -> Path:
        return _DISK_DIR / f"{week_start}.json"

    def get(self, week_start: str) -> dict | None:
        """Return the stored edition for an ISO-Monday key, or None."""
        if not week_start:
            return None
        if self.enabled:
            try:
                with httpx.Client(timeout=8) as c:
                    r = c.get(
                        f"{self.url}/rest/v1/weekly_editorial",
                        headers=self._headers(),
                        params={
                            "env_id": f"eq.{self.env_id}",
                            "week_start": f"eq.{week_start}",
                            "select": "week_start,generated_at,model,article,status",
                            "limit": "1",
                        },
                    )
                    r.raise_for_status()
                    rows = r.json()
                    if rows:
                        return rows[0]
            except Exception as e:
                logger.error(f"WeeklyEditorialStore.get Supabase error: {e}")
        try:
            p = self._disk_path(week_start)
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return None

    def latest(self) -> dict | None:
        """Most recent edition regardless of week — used to serve a prior edition
        if the current week's hasn't been generated yet."""
        if self.enabled:
            try:
                with httpx.Client(timeout=8) as c:
                    r = c.get(
                        f"{self.url}/rest/v1/weekly_editorial",
                        headers=self._headers(),
                        params={
                            "env_id": f"eq.{self.env_id}",
                            "select": "week_start,generated_at,model,article,status",
                            "order": "week_start.desc",
                            "limit": "1",
                        },
                    )
                    r.raise_for_status()
                    rows = r.json()
                    if rows:
                        return rows[0]
            except Exception as e:
                logger.error(f"WeeklyEditorialStore.latest Supabase error: {e}")
        # Disk fallback: newest file by name (ISO dates sort lexically).
        try:
            files = sorted(_DISK_DIR.glob("*.json"), reverse=True)
            if files:
                return json.loads(files[0].read_text(encoding="utf-8"))
        except Exception:
            pass
        return None

    def list_editions(self, limit: int = 52) -> list[dict]:
        """Lightweight index of all stored editions, newest first — for the
        public archive. Returns week_start + a few display fields per edition."""
        def _row(week_start, generated_at, art):
            art = art or {}
            return {
                "week_start":   week_start,
                "generated_at": generated_at,
                "week_label":   art.get("week_label"),
                "title":        art.get("title"),
                "standfirst":   art.get("standfirst"),
                "subject":      art.get("subject"),
                "tickers":      art.get("tickers") or [],
                "cover_lines":  art.get("cover_lines") or [],
                "cover_splash": art.get("cover_splash") or None,
                "cover_image":  art.get("cover_image") or None,
                "slug":         art.get("slug") or None,
            }
        if self.enabled:
            try:
                with httpx.Client(timeout=8) as c:
                    r = c.get(
                        f"{self.url}/rest/v1/weekly_editorial",
                        headers=self._headers(),
                        params={
                            "env_id": f"eq.{self.env_id}",
                            "select": "week_start,generated_at,article",
                            "order": "week_start.desc",
                            "limit": str(limit),
                        },
                    )
                    r.raise_for_status()
                    return [_row(x.get("week_start"), x.get("generated_at"), x.get("article"))
                            for x in r.json()]
            except Exception as e:
                logger.error(f"WeeklyEditorialStore.list_editions Supabase error: {e}")
        try:
            files = sorted(_DISK_DIR.glob("*.json"), reverse=True)[:limit]
            out = []
            for f in files:
                doc = json.loads(f.read_text(encoding="utf-8"))
                out.append(_row(doc.get("week_start") or f.stem,
                                doc.get("generated_at"), doc.get("article")))
            return out
        except Exception:
            return []

    def save(self, week_start: str, article: dict, model: str = "") -> None:
        """Persist one edition (upsert on env_id+week_start)."""
        if not week_start or not article:
            return
        row = {
            "env_id":     self.env_id,
            "week_start": week_start,
            "model":      model or article.get("model", ""),
            "article":    article,
            "status":     article.get("status", "ready"),
        }
        if self.enabled:
            try:
                with httpx.Client(timeout=12) as c:
                    r = c.post(
                        f"{self.url}/rest/v1/weekly_editorial",
                        headers=self._headers("return=minimal,resolution=merge-duplicates"),
                        params={"on_conflict": "env_id,week_start"},
                        json=[row],
                    )
                    if r.status_code >= 400:
                        logger.error(f"WeeklyEditorialStore.save → {r.status_code}: {r.text[:200]}")
                    r.raise_for_status()
            except Exception as e:
                logger.error(f"WeeklyEditorialStore.save Supabase error: {e}")
        try:
            disk_doc = dict(row)
            disk_doc["generated_epoch"] = time.time()
            self._disk_path(week_start).write_text(
                json.dumps(disk_doc, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def delete(self, week_start: str) -> bool:
        """Remove one edition from BOTH backends (Supabase row + disk fallback).

        Deleting only the Supabase row would let the local JSON resurrect the
        edition on the next get(), because the disk copy is a fallback, not a
        cache. Returns True if either backend reported a removal."""
        if not week_start:
            return False
        gone = False
        if self.enabled:
            try:
                with httpx.Client(timeout=12) as c:
                    r = c.delete(
                        f"{self.url}/rest/v1/weekly_editorial",
                        headers=self._headers("return=minimal"),
                        params={"env_id": f"eq.{self.env_id}",
                                "week_start": f"eq.{week_start}"},
                    )
                    if r.status_code >= 400:
                        logger.error(f"WeeklyEditorialStore.delete → {r.status_code}: {r.text[:200]}")
                    else:
                        gone = True
            except Exception as e:
                logger.error(f"WeeklyEditorialStore.delete Supabase error: {e}")
        try:
            p = self._disk_path(week_start)
            if p.exists():
                p.unlink()
                gone = True
        except Exception as e:
            logger.error(f"WeeklyEditorialStore.delete disk error: {e}")
        return gone

    @staticmethod
    def is_stale(row: dict | None) -> bool:
        if not row:
            return True
        epoch = row.get("generated_epoch")
        if epoch is None:
            ga = row.get("generated_at")
            if not ga:
                return True
            try:
                from datetime import datetime
                ts = datetime.fromisoformat(ga.replace("Z", "+00:00"))
                epoch = ts.timestamp()
            except Exception:
                return True
        return (time.time() - float(epoch)) > TTL_SECONDS


# Module-level singleton (mirrors selection_store.store / overview_store.store).
store = WeeklyEditorialStore()
