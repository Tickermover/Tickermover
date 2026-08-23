"""Real photography for the capex-chain cards.

The generated SVG glyphs in `chain_art` gave the grid a family look, but the
user's call was for real images: a photograph of a switchyard says "this map is
about the grid" faster than any icon can.

Nothing new is invented here. `weekly_cover_image` already fetches free,
commercial-use editorial photography from Unsplash (then Pexels), already biases
the search AWAY from people -- a stock portrait beside a scored company implies
that person endorses the call -- and already carries the credit fields their
licence requires. This module is the thin chain-map adapter over it: slug to
subject, subject to cached photo.

TWO RULES THIS FILE EXISTS TO ENFORCE:

1. **The render path never makes a network call.** `cached()` is a pure KV read.
   A page that fetched eleven photos inline would hang on a slow provider, and
   /who-benefits is server-rendered for crawlers. The background prewarm fills
   the cache; until it has, the card falls back to the `chain_art` glyph, which
   is why that art stays.
2. **No key, no change.** With neither UNSPLASH_ACCESS_KEY nor PEXELS_API_KEY
   set, `available()` is False, the prewarm idles and every card keeps its
   glyph. The feature degrades to exactly what shipped before it.

Photos are hotlinked from the provider CDN, which is what the Unsplash API
terms require (self-hosting their files is not allowed), so this adds no repo
weight and no image serving of our own.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

NS = "chain_photo_v1"
MAX_AGE_S = 60 * 86400          # a chain's subject does not change; re-pick rarely

# slug/title keyword -> the subject word weekly_cover_image's own scene map
# understands. Single words on purpose: that map matches by substring and takes
# the FIRST hit, so a phrase can pick up an unintended key.
_SUBJECTS = (
    (("semi-equipment", "chip factor", "wafer", "lithograph", "foundry"), "semiconductor"),
    (("ai-capex", "hyperscaler", "datacenter", "data center", "capex"), "data center"),
    (("grid", "transmission", "substation"), "grid"),
    (("power", "turbine", "generation", "nuclear"), "power"),
    (("defense", "defence", "military"), "defense"),
    (("cyber", "security"), "cybersecurity"),
    (("reshor", "factory", "onshor", "manufactur"), "industrials"),
    (("battery", "ev-", "electric vehicle", "lithium"), "battery"),
    (("robot", "automation"), "robot"),
    (("payment", "fintech", "checkout"), "payment"),
    (("glp", "obesity", "drug", "pharma", "biotech"), "obesity"),
    (("telecom", "5g", "broadband", "fiber", "fibre"), "networking"),
    (("cloud", "software", "saas"), "cloud"),
    (("space", "satellite", "aerospace"), "aerospace"),
    (("water",), "utilities"),
)


def available() -> bool:
    """True when a photo provider is configured. Cheap and import-safe."""
    try:
        import weekly_cover_image as _wci
        return _wci.available()
    except Exception:
        return False


def subject_for(slug: str = "", title: str = "", standfirst: str = "") -> str:
    """The scene subject for one map. Slug and title first; the standfirst is a
    tie-breaker only, for the same reason as in `chain_art.theme_for`."""
    head = (" %s %s " % (slug or "", title or "")).lower()
    tail = " %s " % (standfirst or "").lower()
    for keys, subject in _SUBJECTS:
        if any(k in head for k in keys):
            return subject
    for keys, subject in _SUBJECTS:
        if any(k in tail for k in keys):
            return subject
    return "industrials"


def cached(slug: str) -> dict | None:
    """The stored photo for one map, or None. PURE READ -- safe in a renderer."""
    if not slug:
        return None
    try:
        from kv_store import store as _kv
        rec = _kv.get(NS, slug.lower(), max_age_s=MAX_AGE_S)
        if not rec:
            return None
        # a stored miss is remembered too, so a subject with no good photo does
        # not re-query the provider on every prewarm cycle
        return rec if rec.get("url") else None
    except Exception:
        return None


async def refresh(theses: list, force: bool = False) -> int:
    """Fetch and store any missing photo. Returns how many were written.

    Never raises: a missing thumbnail must not be able to break the hub or the
    weekly publish that calls it."""
    if not available():
        return 0
    written = 0
    try:
        from kv_store import store as _kv
        import weekly_cover_image as _wci
    except Exception:
        return 0
    for t in theses or []:
        slug = (t.get("slug") or "").lower()
        if not slug:
            continue
        try:
            if not force and _kv.get(NS, slug, max_age_s=MAX_AGE_S) is not None:
                continue
            subject = subject_for(slug, t.get("title") or "", t.get("standfirst") or "")
            # seed on the slug: two maps sharing a subject (the grid map and the
            # power-build map both land on industrial power scenes) must not end
            # up carrying the same photograph
            hit = await _wci.fetch(subject, seed=slug)
            rec = {"slug": slug, "subject": subject}
            if hit:
                rec.update({k: hit.get(k) for k in
                            ("url", "thumb", "alt", "credit_name", "credit_url", "source")})
            _kv.set(NS, slug, rec)
            written += 1
            logger.info("chain photo %s <- %s (%s)", slug, subject,
                        (hit or {}).get("source") or "no result")
        except Exception as e:
            logger.warning("chain photo failed for %s: %s", slug, e)
    return written
