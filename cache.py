"""
TickerMover — Smart Cache
In-memory TTL cache with optional JSON disk persistence so data
survives server restarts (critical for the 25-call/day AV limit).
"""
from __future__ import annotations
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


class SmartCache:
    """
    Thread-safe (GIL-protected) in-memory cache with TTL and disk backup.

    Entry layout in self._store:
        key → {"data": <any JSON-serialisable>, "expires_at": <unix float>}
    """

    def __init__(self, disk_path: Optional[str] = None):
        self._store: dict[str, dict] = {}
        self._disk_path = disk_path
        if disk_path:
            self._load_disk()

    # ── Public API ────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry and time.time() < entry["expires_at"]:
            return entry["data"]
        return None

    def set(self, key: str, data: Any, ttl: int) -> None:
        self._store[key] = {"data": data, "expires_at": time.time() + ttl}

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def keys_matching(self, prefix: str) -> list[str]:
        return [k for k in self._store if k.startswith(prefix)]

    def ttl_remaining(self, key: str) -> float:
        """Seconds until expiry; -1 if missing/expired."""
        entry = self._store.get(key)
        if not entry:
            return -1
        remaining = entry["expires_at"] - time.time()
        return max(remaining, -1)

    # ── Disk persistence ─────────────────────────────────────────────

    def save_disk(self) -> None:
        if not self._disk_path:
            return
        try:
            p = Path(self._disk_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            # Only persist long-lived entries (fund / tech / insider)
            to_save = {
                k: v for k, v in self._store.items()
                if v.get("expires_at", 0) > now + 60   # at least 60s left
            }
            p.write_text(json.dumps(to_save, default=str), encoding="utf-8")
            try:
                _sz = p.stat().st_size
            except Exception:
                _sz = -1
            logger.info(f"💾 Cache saved {len(to_save)} entries ({_sz} bytes) → {p}")
        except Exception as e:
            logger.warning(f"Cache disk-save failed ({self._disk_path}): {e}")

    def _load_disk(self) -> None:
        try:
            p = Path(self._disk_path)
            if not p.exists():
                # Explicit so a deploy log shows WHERE we looked — distinguishes
                # "volume not mounted / wrong path" from "file exists but stale".
                logger.warning(f"📂 Cache disk file NOT FOUND at {p} (cold start; "
                               f"will be written on first save — if it never appears, "
                               f"the volume isn't mounted/writable at that path)")
                return
            try:
                _sz = p.stat().st_size
            except Exception:
                _sz = -1
            raw = json.loads(p.read_text(encoding="utf-8"))
            now = time.time()
            loaded = 0
            for k, v in raw.items():
                if isinstance(v, dict) and v.get("expires_at", 0) > now:
                    self._store[k] = v
                    loaded += 1
            logger.info(f"📂 Cache loaded {loaded}/{len(raw)} valid entries "
                        f"({_sz} bytes) from {p}")
        except Exception as e:
            logger.warning(f"Cache disk-load failed ({self._disk_path}): {e}")

    # ── Stats ────────────────────────────────────────────────────────

    @property
    def stats(self) -> dict:
        now = time.time()
        total = len(self._store)
        valid = sum(1 for v in self._store.values() if v["expires_at"] > now)
        return {
            "total":   total,
            "valid":   valid,
            "expired": total - valid,
        }
