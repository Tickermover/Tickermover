"""
AlphaHunt — generic durable key/value store.

A small Supabase-backed KV for caches that must survive a Railway redeploy but
don't warrant their own table — e.g. the PDF-narrative blocks and concall
summaries, which were module-level dicts (wiped on every deploy, re-paying Haiku).
Mirrors the other stores: Supabase in prod, local JSON fallback for dev.
env_id 1=prod, 2=dev.

Supabase table (create once)::

    create table app_kv (
      env_id      int          not null,
      ns          text         not null,        -- namespace, e.g. 'pdf_narrative'
      k           text         not null,        -- entry key, e.g. the ticker
      v           jsonb        default '{}'::jsonb,
      updated_at  timestamptz  not null default now(),
      primary key (env_id, ns, k)
    );
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
_DISK_DIR = _BASE_DIR / "output" / "kv"


class KVStore:
    def __init__(self) -> None:
        self.url = (config.SUPABASE_URL or "").rstrip("/")
        self.service_key = config.SUPABASE_SERVICE_KEY or ""
        self.anon_key = config.SUPABASE_ANON_KEY or ""
        self.env_id = _ENV_ID
        self.enabled = bool(self.url and (self.service_key or self.anon_key))
        if not self.enabled:
            logger.warning(
                "KVStore: Supabase not configured — durable KV falls back to "
                "output/kv/*.json (local dev only)."
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

    def _disk_path(self, ns: str, k: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in f"{ns}__{k}")
        return _DISK_DIR / f"{safe}.json"

    def get(self, ns: str, k: str, max_age_s: float | None = None) -> dict | None:
        """Return the stored value for (ns, k), or None. If max_age_s is given,
        entries older than that (by the stored `_kv_epoch`) are treated as miss."""
        k = str(k)
        val = None
        if self.enabled:
            try:
                with httpx.Client(timeout=8) as c:
                    r = c.get(
                        f"{self.url}/rest/v1/app_kv",
                        headers=self._headers(),
                        params={
                            "env_id": f"eq.{self.env_id}",
                            "ns": f"eq.{ns}",
                            "k": f"eq.{k}",
                            "select": "v,updated_at",
                            "limit": "1",
                        },
                    )
                    r.raise_for_status()
                    rows = r.json()
                    if rows and isinstance(rows[0].get("v"), dict):
                        val = rows[0]["v"]
            except Exception as e:
                logger.error(f"KVStore.get({ns},{k}) Supabase error: {e}")
        if val is None:
            try:
                p = self._disk_path(ns, k)
                if p.exists():
                    val = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        if val is None:
            return None
        if max_age_s is not None:
            epoch = val.get("_kv_epoch")
            if epoch is None or (time.time() - float(epoch)) > max_age_s:
                return None
        return val.get("_kv_data", val)

    def set(self, ns: str, k: str, value: dict) -> None:
        k = str(k)
        wrapped = {"_kv_epoch": time.time(), "_kv_data": value}
        if self.enabled:
            try:
                with httpx.Client(timeout=8) as c:
                    r = c.post(
                        f"{self.url}/rest/v1/app_kv",
                        headers=self._headers("return=minimal,resolution=merge-duplicates"),
                        params={"on_conflict": "env_id,ns,k"},
                        json={"env_id": self.env_id, "ns": ns, "k": k, "v": wrapped},
                    )
                    if r.status_code >= 400:
                        logger.error(f"KVStore.set({ns},{k}) → {r.status_code}: {r.text[:200]}")
                    r.raise_for_status()
            except Exception as e:
                logger.error(f"KVStore.set({ns},{k}) Supabase error: {e}")
        try:
            self._disk_path(ns, k).write_text(
                json.dumps(wrapped, ensure_ascii=False, default=str), encoding="utf-8"
            )
        except Exception:
            pass


# Module-level singleton.
store = KVStore()
