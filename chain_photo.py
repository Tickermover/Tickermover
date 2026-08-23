"""Real photography for the capex-chain cards.

The generated SVG glyphs in `chain_art` gave the grid a family look, but the
user's call was for real images: a photograph of a switchyard says "this map is
about the grid" faster than any icon can.

The provider plumbing is `weekly_cover_image`'s -- same free commercial-use
sources (Unsplash, then Pexels), same key, same rule that we never put a
photograph of a person next to a scored company, because a stock portrait beside
"NVDA" reads as an endorsement. What this module does NOT reuse is that module's
subject map and its pick: those are tuned for a magazine cover, where any
plausible industrial scene works.

WHY THE PICK IS DIFFERENT HERE -- and this is the whole point of the file. The
first version took the provider's results for a sector word and chose one by
seed. That put a photo of **disposable cigarette lighters** on the EV & battery
chain and a **toothbrush** on the obesity-drug chain: both were high-ranked
results for a loose query, and nothing checked that the picture was of the thing
the map is about. So:

  1. queries are per chain and concrete ("lithium battery pack", not "battery"),
  2. a candidate is only accepted if its own alt text names something the query
     asked for -- a picture nobody described as a battery is not a battery,
  3. candidates are scored, and the seed only breaks ties among the best.

A miss is better than a wrong picture: when nothing matches, the card keeps the
generated glyph, which is why that art stays.

TWO RULES THIS FILE ALSO ENFORCES:
  * **The render path never makes a network call.** `cached()` is a pure KV
    read; a hub that fetched eleven photos inline would hang on a slow provider,
    and it is server-rendered for crawlers. `_chain_photo_prewarm` fills it.
  * **No key, no change.** Without UNSPLASH_ACCESS_KEY / PEXELS_API_KEY,
    `available()` is False, the prewarm idles, every card keeps its glyph.

Photos are hotlinked from the provider CDN, which is what the Unsplash API terms
require (self-hosting their files is not permitted), so this adds no repo weight
and no image serving of our own. The photographer is credited on the card.
"""

from __future__ import annotations

import logging
import zlib

logger = logging.getLogger(__name__)

# v2: v1 shipped the lighters and the toothbrush. Bumping the namespace re-picks
# every photo under the matching rules below rather than leaving them cached.
NS = "chain_photo_v2"
MAX_AGE_S = 60 * 86400          # a chain's subject does not change; re-pick rarely
_PER_PAGE = 25

# Per-chain scenes, specific first: (chain keywords, ordered queries, accept
# vocabulary). The vocabulary is what a candidate's own alt text is checked
# against. Query words alone are too strict -- provider descriptions are terse
# ("silhouette of utility pole" never says "electricity") -- and no check at all
# is what put lighters on the battery chain.
_SCENES = (
    (("semi-equipment", "chip factor", "wafer", "lithograph", "foundry",
      "semiconductor equipment"),
     ["semiconductor wafer fab cleanroom", "silicon wafer semiconductor",
      "microchip circuit board macro"],
     "wafer semiconductor chip microchip circuit silicon cleanroom fab processor "
     "electronics electronic transistor motherboard"),
    (("ai-capex", "hyperscaler", "datacenter", "data center", "ai capex"),
     ["data center server room", "server racks data centre", "network server room"],
     "data center centre server servers rack racks network cables computer "
     "supercomputer datacenter"),
    (("grid", "transmission", "substation", "electrif"),
     ["electricity transmission tower power line", "electrical substation power grid",
      "high voltage power lines"],
     "power electric electricity transmission pylon pole tower towers line lines "
     "cable wire wires substation voltage utility grid insulator"),
    (("power", "turbine", "generation", "megawatt", "gigawatt"),
     ["power plant electricity generation", "gas turbine power station",
      "power station cooling towers"],
     "power plant turbine station energy electricity cooling chimney generator "
     "steam smokestack refinery industrial"),
    (("nuclear", "uranium", "reactor"),
     ["nuclear power plant reactor", "nuclear cooling tower"],
     "nuclear reactor cooling tower plant power steam"),
    (("defense", "defence", "military", "missile", "munition"),
     ["military fighter jet aircraft", "military helicopter aircraft carrier",
      "defense radar military equipment"],
     "military jet fighter aircraft plane helicopter warship navy army tank "
     "missile radar carrier airforce defence defense"),
    (("cyber", "security budget", "zero trust"),
     ["network security server room", "cyber security data protection",
      "network cables server rack"],
     "security server network code laptop screen data cables computer lock "
     "encryption monitor keyboard"),
    (("reshor", "factory build", "onshor", "manufactur", "industrial build"),
     ["factory assembly line manufacturing", "industrial factory production line",
      "steel factory industrial plant"],
     "factory manufacturing assembly production industrial plant machine machinery "
     "workshop warehouse welding steel crane"),
    (("battery", "ev-", "electric vehicle", "lithium", "cathode"),
     ["lithium battery pack electric vehicle", "electric car charging station",
      "battery cells manufacturing"],
     "battery batteries lithium cell cells charging charger electric vehicle car "
     "charge tesla plug"),
    (("robot", "automation", "cobot"),
     ["industrial robot arm factory", "robotic arm assembly line automation"],
     "robot robotic robotics arm automation machine assembly manufacturing factory"),
    (("payment", "fintech", "checkout", "merchant"),
     ["credit card payment terminal", "contactless card payment machine",
      "point of sale payment terminal"],
     "card payment terminal checkout contactless credit debit register till "
     "pos machine reader"),
    (("glp", "obesity", "drug", "pharma", "biotech", "therap"),
     ["pharmaceutical production line medicine", "injection pen insulin medical",
      "pharmaceutical laboratory vials"],
     "pharmaceutical medicine medical pills pill drug drugs injection syringe vial "
     "vials laboratory lab capsule capsules tablet tablets pharmacy insulin"),
    (("telecom", "5g", "broadband", "fiber", "fibre"),
     ["fiber optic cables network", "cellular tower telecom antenna"],
     "fiber fibre optic cable cables antenna tower telecom network mast"),
    (("cloud", "software", "saas"),
     ["data center server room", "cloud computing servers"],
     "server servers data center centre cloud computer network rack"),
    (("space", "satellite", "orbit"),
     ["satellite in orbit space", "rocket launch spacecraft"],
     "satellite space rocket orbit launch spacecraft earth"),
    (("water", "desalinat", "irrigation"),
     ["water treatment plant industrial", "water pipeline infrastructure"],
     "water pipe pipes pipeline treatment plant reservoir dam tank"),
)
_FALLBACK = ["industrial factory production line", "factory assembly line manufacturing"]
_FALLBACK_VOCAB = ("factory industrial manufacturing production machine plant "
                   "assembly warehouse")

# Words that carry no subject meaning, so a candidate matching only these has
# not been shown to be a picture of the right thing.
_STOP = {"a", "an", "and", "of", "the", "in", "on", "at", "with", "macro", "close",
         "up", "industrial", "modern"}


def available() -> bool:
    """True when a photo provider is configured. Cheap and import-safe."""
    try:
        import weekly_cover_image as _wci
        return _wci.available()
    except Exception:
        return False


def scenes_for(slug: str = "", title: str = "", standfirst: str = "") -> tuple:
    """(ordered queries, accept vocabulary) for one map. Slug and title decide
    it; the standfirst is a tie-breaker only, because it is prose and will
    happily mention "power" in a map about payments."""
    head = (" %s %s " % (slug or "", title or "")).lower().replace("-", " ")
    tail = " %s " % (standfirst or "").lower()
    for text in (head, tail):
        for keys, scenes, vocab in _SCENES:
            if any(k.replace("-", " ") in text for k in keys):
                return list(scenes) + _FALLBACK, vocab + " " + _FALLBACK_VOCAB
    return list(_FALLBACK), _FALLBACK_VOCAB


def _score(alt: str, query: str, vocab: str) -> int:
    """How many subject words the photo's OWN description names -- from the
    query and from the chain's vocabulary. Zero means we have no evidence the
    picture is of the subject, and zero is rejected: a miss shows the generated
    glyph, which is better than a confident picture of the wrong thing."""
    a = (alt or "").lower()
    if not a:
        return 0
    words = {w for w in (query + " " + vocab).lower().split()
             if w not in _STOP and len(w) > 2}
    hits = 0
    for w in words:
        stem = w[:-1] if w.endswith("s") and len(w) > 4 else w
        if stem in a:
            hits += 1
    return hits


def _best(cands: list, query: str, vocab: str, seed: str):
    """Highest-scoring candidate; the seed only breaks ties, so two chains that
    share a query do not share a photograph."""
    scored = [(c, _score(c.get("alt") or "", query, vocab)) for c in cands]
    scored = [(c, n) for c, n in scored if n > 0]
    if not scored:
        return None
    top = max(n for _, n in scored)
    best = [c for c, n in scored if n == top]
    if not seed or len(best) == 1:
        return best[0]
    return best[zlib.crc32(str(seed).encode("utf-8")) % len(best)]


async def _candidates(query: str) -> list:
    """Landscape, person-free candidates for one query, from whichever provider
    is configured. Normalised to one shape so the scorer sees one thing."""
    import httpx
    import weekly_cover_image as _wci
    out = []
    if _wci._UNSPLASH_KEY:
        try:
            async with httpx.AsyncClient(timeout=_wci._TIMEOUT) as c:
                r = await c.get("https://api.unsplash.com/search/photos",
                                params={"query": query, "per_page": _PER_PAGE,
                                        "content_filter": "high",
                                        # the card is a 2.7:1 band; a portrait
                                        # crops to a sliver of its own subject
                                        "orientation": "landscape"},
                                headers={"Authorization": "Client-ID %s" % _wci._UNSPLASH_KEY})
                r.raise_for_status()
                for p in (r.json() or {}).get("results") or []:
                    alt = p.get("alt_description") or p.get("description") or ""
                    urls, user = p.get("urls") or {}, p.get("user") or {}
                    if _wci._looks_like_person(alt) or not urls.get("regular"):
                        continue
                    out.append({"url": urls.get("regular"),
                                "thumb": urls.get("small") or urls.get("regular"),
                                "alt": alt, "credit_name": user.get("name") or "Unsplash",
                                "credit_url": (user.get("links") or {}).get("html")
                                              or "https://unsplash.com",
                                "source": "Unsplash"})
        except Exception as e:
            logger.warning("chain photo unsplash %r: %s", query, e)
    if not out and _wci._PEXELS_KEY:
        try:
            async with httpx.AsyncClient(timeout=_wci._TIMEOUT) as c:
                r = await c.get("https://api.pexels.com/v1/search",
                                params={"query": query, "per_page": _PER_PAGE,
                                        "orientation": "landscape"},
                                headers={"Authorization": _wci._PEXELS_KEY})
                r.raise_for_status()
                for p in (r.json() or {}).get("photos") or []:
                    alt = p.get("alt") or ""
                    src = p.get("src") or {}
                    if _wci._looks_like_person(alt) or not src.get("large"):
                        continue
                    out.append({"url": src.get("large"),
                                "thumb": src.get("medium") or src.get("large"),
                                "alt": alt, "credit_name": p.get("photographer") or "Pexels",
                                "credit_url": p.get("photographer_url") or "https://pexels.com",
                                "source": "Pexels"})
        except Exception as e:
            logger.warning("chain photo pexels %r: %s", query, e)
    return out


async def pick(slug: str, title: str = "", standfirst: str = "") -> dict | None:
    """One photograph for one map, or None when nothing matched well enough."""
    queries, vocab = scenes_for(slug, title, standfirst)
    for query in queries:
        cands = await _candidates(query)
        hit = _best(cands, query, vocab, slug)
        if hit:
            hit = dict(hit, query=query)
            logger.info("chain photo %s <- %r (%s: %s)", slug, query,
                        hit.get("source"), (hit.get("alt") or "")[:60])
            return hit
        logger.info("chain photo %s: no match for %r (%d candidates)",
                    slug, query, len(cands))
    return None


def cached(slug: str) -> dict | None:
    """The stored photo for one map, or None. PURE READ -- safe in a renderer."""
    if not slug:
        return None
    try:
        from kv_store import store as _kv
        rec = _kv.get(NS, slug.lower(), max_age_s=MAX_AGE_S)
        if not rec:
            return None
        # a miss is stored too, so a subject with no good photo does not
        # re-query the provider on every prewarm cycle
        return rec if rec.get("url") else None
    except Exception:
        return None


async def refresh(theses: list, force: bool = False) -> int:
    """Fetch and store any missing photo. Returns how many were written.

    Never raises: a thumbnail must not be able to break the hub or the weekly
    publish."""
    if not available():
        return 0
    try:
        from kv_store import store as _kv
    except Exception:
        return 0
    written = 0
    for t in theses or []:
        slug = (t.get("slug") or "").lower()
        if not slug:
            continue
        try:
            if not force and _kv.get(NS, slug, max_age_s=MAX_AGE_S) is not None:
                continue
            hit = await pick(slug, t.get("title") or "", t.get("standfirst") or "")
            rec = {"slug": slug}
            if hit:
                rec.update({k: hit.get(k) for k in
                            ("url", "thumb", "alt", "credit_name", "credit_url",
                             "source", "query")})
            _kv.set(NS, slug, rec)
            written += 1
        except Exception as e:
            logger.warning("chain photo failed for %s: %s", slug, e)
    return written
