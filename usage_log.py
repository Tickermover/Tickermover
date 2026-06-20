"""
TickerMover — per-call AI usage / cost logger.

Every paid Anthropic call records one row: feature + model + token breakdown +
web searches + an estimated USD cost + (optional) user_id / ticker. This gives
us TRUE per-feature and per-user attribution (the Anthropic billing export only
has model), so the monthly cost report is measured, not estimated.

Storage mirrors the other stores: a Supabase `usage` table in prod, append-only
JSONL on disk for local/dev. Best-effort — never raises into a generation.

Supabase table (create once)::

    create table usage (
      id           bigint generated always as identity primary key,
      env_id       int          not null,
      ts           timestamptz  not null default now(),
      feature      text,
      model        text,
      in_nc        bigint  default 0,
      cache_write  bigint  default 0,
      cache_read   bigint  default 0,
      out_tok      bigint  default 0,
      web          int     default 0,
      est_cost_usd numeric default 0,
      user_id      text,
      ticker       text
    );
    create index on usage (env_id, ts);
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
    return 1 if rwy == "production" else 2


_ENV_ID = _detect_env_id()
_DISK = Path(__file__).resolve().parent / "output" / "usage"

# Per-token USD prices (≤200k context), derived from the published rates and
# cross-checked against the billing export. input / output / cache-write-5m /
# cache-read. Web search billed per request.
_PRICES = {
    "haiku":  (1.0e-6,  5.0e-6,  1.25e-6, 0.10e-6),
    "sonnet": (3.0e-6, 15.0e-6,  3.75e-6, 0.30e-6),
    "opus":   (5.0e-6, 25.0e-6,  6.25e-6, 0.50e-6),
}
_WEB_SEARCH = 0.01


def _tier(model: str) -> str:
    m = (model or "").lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    return "haiku"


def estimate_cost(model: str, in_nc: int, cache_write: int, cache_read: int,
                  out: int, web: int) -> float:
    pin, pout, pcw, pcr = _PRICES[_tier(model)]
    return round(in_nc * pin + out * pout + cache_write * pcw
                 + cache_read * pcr + (web or 0) * _WEB_SEARCH, 6)


class UsageLog:
    def __init__(self) -> None:
        self.url = (config.SUPABASE_URL or "").rstrip("/")
        self.key = config.SUPABASE_SERVICE_KEY or config.SUPABASE_ANON_KEY or ""
        self.enabled = bool(self.url and self.key)
        # In-memory running tally of TODAY's AI spend (UTC) — the input to the
        # daily spend circuit breaker. Resets at midnight UTC and on restart
        # (a restart simply gives the cap a fresh budget; the breaker's real job
        # is to halt a runaway loop within a running process).
        self._day: str | None = None
        self._day_cost: float = 0.0
        try:
            _DISK.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _bump_day(self, cost: float) -> None:
        import datetime as _dt
        d = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        if d != self._day:
            self._day, self._day_cost = d, 0.0
        self._day_cost += float(cost or 0)

    def today_cost_usd(self) -> float:
        import datetime as _dt
        if _dt.datetime.utcnow().strftime("%Y-%m-%d") != self._day:
            return 0.0
        return self._day_cost

    def record(self, feature: str, model: str, usage: dict | None, *,
               user_id: str | None = None, ticker: str | None = None,
               web_searches: int | None = None) -> None:
        """Log one paid call. `usage` is the Anthropic response's `usage` dict."""
        try:
            u = usage or {}
            in_nc = int(u.get("input_tokens", 0) or 0)
            cw = int(u.get("cache_creation_input_tokens", 0) or 0)
            cr = int(u.get("cache_read_input_tokens", 0) or 0)
            out = int(u.get("output_tokens", 0) or 0)
            if web_searches is None:
                web_searches = int(((u.get("server_tool_use") or {})
                                    .get("web_search_requests", 0)) or 0)
            row = {
                "env_id":       _ENV_ID,
                "feature":      (feature or "")[:60],
                "model":        (model or "")[:60],
                "in_nc":        in_nc,
                "cache_write":  cw,
                "cache_read":   cr,
                "out_tok":      out,
                "web":          int(web_searches or 0),
                "est_cost_usd": estimate_cost(model, in_nc, cw, cr, out, web_searches),
                "user_id":      user_id,
                "ticker":       (ticker or None),
            }
        except Exception as e:
            logger.debug(f"usage_log build failed: {e}")
            return

        try:
            self._bump_day(row["est_cost_usd"])
        except Exception:
            pass

        if self.enabled:
            try:
                with httpx.Client(timeout=6) as c:
                    r = c.post(
                        f"{self.url}/rest/v1/usage",
                        headers={"apikey": self.key, "Authorization": f"Bearer {self.key}",
                                 "Content-Type": "application/json", "Prefer": "return=minimal"},
                        json=row,
                    )
                    if r.status_code < 400:
                        return
                    logger.debug(f"usage_log supabase {r.status_code}: {r.text[:160]}")
            except Exception as e:
                logger.debug(f"usage_log supabase error: {e}")
        # Disk fallback (always, if Supabase off or errored).
        try:
            row["ts"] = int(time.time())
            with (_DISK / "usage.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception:
            pass

    def _fetch(self, limit: int = 20000) -> list:
        rows: list = []
        if self.enabled:
            try:
                with httpx.Client(timeout=15) as c:
                    r = c.get(
                        f"{self.url}/rest/v1/usage",
                        headers={"apikey": self.key, "Authorization": f"Bearer {self.key}"},
                        params={"env_id": f"eq.{_ENV_ID}", "select": "*",
                                "order": "ts.desc", "limit": str(limit)},
                    )
                    if r.status_code < 400:
                        rows = r.json()
            except Exception as e:
                logger.debug(f"usage_log fetch error: {e}")
        if not rows:
            p = _DISK / "usage.jsonl"
            if p.exists():
                for line in p.read_text(encoding="utf-8").splitlines():
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
        return rows

    def summary(self, limit: int = 20000) -> dict:
        """Aggregate logged usage by feature / model / user for the cost report."""
        rows = self._fetch(limit)
        bf: dict = {}; bm: dict = {}; bu: dict = {}
        total = 0.0
        for r in rows:
            c = float(r.get("est_cost_usd") or 0)
            total += c
            for d, k in ((bf, r.get("feature") or "?"), (bm, r.get("model") or "?")):
                e = d.setdefault(k, {"cost": 0.0, "calls": 0})
                e["cost"] += c; e["calls"] += 1
            uid = r.get("user_id")
            if uid:
                e = bu.setdefault(uid, {"cost": 0.0, "calls": 0})
                e["cost"] += c; e["calls"] += 1
        rnd = lambda d: {k: {"cost": round(v["cost"], 4), "calls": v["calls"]}
                         for k, v in sorted(d.items(), key=lambda x: -x[1]["cost"])}
        return {"rows": len(rows), "total_cost_usd": round(total, 4),
                "by_feature": rnd(bf), "by_model": rnd(bm), "top_users": rnd(bu)}


store = UsageLog()


def record(feature: str, model: str, usage: dict | None, **kw) -> None:
    """Module-level convenience: usage_log.record(feature, model, usage, ...)."""
    store.record(feature, model, usage, **kw)


def today_cost_usd() -> float:
    """Today's (UTC) running AI spend in USD — input to the daily circuit breaker."""
    return store.today_cost_usd()
