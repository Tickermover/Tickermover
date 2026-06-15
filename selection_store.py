"""
AlphaHunt — durable store for the AI analyst-judge's Top Hunts conviction scores.

Mirrors overview_store / research_store: Supabase-backed so judgments survive
Railway redeploys, with a local-JSON fallback for dev. env_id 1=prod, 2=dev.

The judge (ai_selector.py) is advisory — its conviction score is a re-rank
tiebreaker within the quant-qualified shortlist. We refresh it ~nightly, so a
~20h TTL means one regeneration per day and intraday refreshes reuse the cache.

Supabase table (create once — see db/2026-06-15-selection-judgments.sql)::

    create table selection_judgments (
      env_id        int          not null,
      ticker        text         not null,
      generated_at  timestamptz  not null default now(),
      model         text,
      conviction    int,
      thesis        text,
      red_flags     jsonb        default '[]'::jsonb,
      lean          text,
      primary key (env_id, ticker)
    );

Without Supabase, judgments are written to ``output/selection/{T}.json`` so
local dev still works.
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
_DISK_DIR = _BASE_DIR / "output" / "selection"

# ~20h: one regen per day; intraday portfolio refreshes reuse the cache.
TTL_SECONDS = int(os.environ.get("SELECTION_TTL_SECONDS", str(20 * 60 * 60)))


class SelectionStore:
    def __init__(self) -> None:
        self.url = (config.SUPABASE_URL or "").rstrip("/")
        self.service_key = config.SUPABASE_SERVICE_KEY or ""
        self.anon_key = config.SUPABASE_ANON_KEY or ""
        self.env_id = _ENV_ID
        self.enabled = bool(self.url and (self.service_key or self.anon_key))
        if not self.enabled:
            logger.warning(
                "SelectionStore: Supabase not configured — AI conviction scores "
                "will be cached to output/selection/*.json (local dev only)."
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

    def get_many(self, tickers: list[str]) -> dict[str, dict]:
        """Return {ticker: judgment_row} for the tickers we have cached."""
        want = [t.upper() for t in tickers if t]
        if not want:
            return {}
        out: dict[str, dict] = {}
        if self.enabled:
            try:
                # PostgREST `in` filter — comma-joined, parenthesised.
                in_list = "(" + ",".join(want) + ")"
                with httpx.Client(timeout=8) as c:
                    r = c.get(
                        f"{self.url}/rest/v1/selection_judgments",
                        headers=self._headers(),
                        params={
                            "env_id": f"eq.{self.env_id}",
                            "ticker": f"in.{in_list}",
                            "select": "ticker,generated_at,model,conviction,thesis,red_flags,lean",
                        },
                    )
                    r.raise_for_status()
                    for row in r.json():
                        tkr = (row.get("ticker") or "").upper()
                        if tkr:
                            out[tkr] = row
            except Exception as e:
                logger.error(f"SelectionStore.get_many Supabase error: {e}")
        # Fill any gaps from disk (and for the local-dev no-Supabase path).
        for tkr in want:
            if tkr in out:
                continue
            try:
                p = self._disk_path(tkr)
                if p.exists():
                    out[tkr] = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
        return out

    def save_many(self, judgments: dict[str, dict]) -> None:
        """Persist {ticker: {conviction, thesis, red_flags, lean, model}}."""
        if not judgments:
            return
        rows = []
        for tkr, j in judgments.items():
            rows.append({
                "env_id":     self.env_id,
                "ticker":     tkr.upper(),
                "model":      j.get("model", ""),
                "conviction": j.get("conviction"),
                "thesis":     j.get("thesis", ""),
                "red_flags":  j.get("red_flags", []) or [],
                "lean":       j.get("lean", ""),
            })
        if self.enabled:
            try:
                with httpx.Client(timeout=10) as c:
                    r = c.post(
                        f"{self.url}/rest/v1/selection_judgments",
                        headers=self._headers(
                            "return=minimal,resolution=merge-duplicates"
                        ),
                        params={"on_conflict": "env_id,ticker"},
                        json=rows,
                    )
                    if r.status_code >= 400:
                        logger.error(
                            f"SelectionStore.save_many → {r.status_code}: {r.text[:200]}"
                        )
                    r.raise_for_status()
            except Exception as e:
                logger.error(f"SelectionStore.save_many Supabase error: {e}")
        for row in rows:
            try:
                disk_doc = dict(row)
                disk_doc["generated_epoch"] = time.time()
                self._disk_path(row["ticker"]).write_text(
                    json.dumps(disk_doc, ensure_ascii=False), encoding="utf-8"
                )
            except Exception:
                pass

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


# Module-level singleton (mirrors overview_store.store / research_store.store).
store = SelectionStore()
