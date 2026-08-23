"""
thesis_map_gen.py — build a NEW chain map every week.

The weekly used to be a 2,200-word article. A chain map says the same thing
better: pick one macro driver, trace where the money actually lands layer by
layer, name the companies in each layer, and let the reader argue with the
judgements. It is also alive in a way prose is not — /api/thesis-shares
recomputes each company's share of theme revenue from the live universe every
time someone opens it, and /api/thesis-perf marks the winners.

What this module does:

  1. Picks the week's driver from a curated pool, skipping anything already
     mapped. Curated rather than model-chosen because a bad driver produces a
     bad map, and there is no cheap way to tell afterwards.
  2. Researches it with the free web-search chain (serper → brave).
  3. Asks a free model for the map, CONSTRAINED to tickers that exist in our
     universe — a map naming companies we do not price is a dead map: no
     shares, no performance, no detail panel.
  4. Validates hard. Everything the model returns is untrusted: layers are
     capped and ordered, exposures coerced to hi/mid/lo, unknown tickers
     dropped, duplicates removed, text length-limited.

Output matches data/theses.json exactly, so every existing endpoint and the
whole thesis.html renderer work on a generated map with no changes.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

MODEL_PREFER = "gemini"          # biggest free context — the universe list is long
MAX_LAYERS   = 6
MIN_LAYERS   = 4
MAX_PER_LAYER = 5
MIN_PER_LAYER = 2

# Curated driver pool. Each entry is one macro force with a real supply chain
# beneath it — the test for inclusion is "can you name 4+ distinct layers of
# listed companies that get paid from this". Ordered roughly by how much of the
# market currently cares.
DRIVER_POOL: list[dict] = [
    {"key": "electric-grid-upgrade", "driver": "the US electric grid rebuild",
     "angle": "Transformers, switchgear, transmission lines and the utilities paying for them."},
    {"key": "space-economy", "driver": "the commercial space build-out",
     "angle": "Launch, satellites, ground stations and the defence primes behind them."},
    {"key": "water-infrastructure", "driver": "water infrastructure spending",
     "angle": "Treatment, pipes, pumps, testing and the utilities that own the assets."},
    {"key": "nuclear-restart", "driver": "the nuclear power restart",
     "angle": "Uranium, fuel services, SMR developers, EPC contractors, the utilities."},
    {"key": "ai-inference-shift", "driver": "the shift from AI training to inference",
     "angle": "Who gets paid when the workload moves from building models to running them."},
    {"key": "obesity-drug-supply", "driver": "GLP-1 manufacturing capacity",
     "angle": "API makers, fill-finish, devices, distribution — the picks and shovels."},
    {"key": "grid-storage", "driver": "grid-scale battery storage",
     "angle": "Cells, inverters, integrators, developers and the offtakers."},
    {"key": "cyber-insurance", "driver": "cyber risk transfer",
     "angle": "From breach to claim: security vendors, insurers, reinsurers, brokers."},
    {"key": "housing-shortage", "driver": "the US housing shortage",
     "angle": "Builders, materials, fixtures, mortgage finance, title."},
    {"key": "aging-demographics", "driver": "the aging population",
     "angle": "Devices, care delivery, pharma, insurers and senior housing."},
    {"key": "supply-chain-software", "driver": "supply-chain digitisation",
     "angle": "Planning software, sensors, logistics networks, freight."},
    {"key": "auto-software", "driver": "the software-defined vehicle",
     "angle": "Silicon, tier-1 integrators, OS vendors, OEMs, aftermarket data."},
    {"key": "biologics-capacity", "driver": "biologics manufacturing capacity",
     "angle": "Bioreactors, consumables, CDMOs, cold chain."},
    {"key": "rare-earth-supply", "driver": "rare-earth and critical-minerals supply",
     "angle": "Mining, separation, magnets, and the defence and EV buyers."},
    {"key": "payments-rails", "driver": "the rebuild of payment rails",
     "angle": "Networks, processors, issuers, fraud, and the merchants."},
    {"key": "industrial-automation", "driver": "factory automation retrofits",
     "angle": "Robots, vision, motion control, integrators, the plants buying it."},
    {"key": "datacenter-cooling", "driver": "datacenter thermal management",
     "angle": "Liquid cooling, chillers, power distribution, the REITs."},
    {"key": "agriculture-tech", "driver": "precision agriculture",
     "angle": "Equipment, seed and chemistry, sensing, the processors downstream."},
    {"key": "aviation-aftermarket", "driver": "the aviation aftermarket",
     "angle": "Engines, parts, MRO, lessors and the airlines paying for it."},
    {"key": "fiber-buildout", "driver": "the fibre and connectivity build-out",
     "angle": "Optics, cable, construction, towers and the carriers."},

    # ── Growth and emerging themes, added 23 Aug 2026 ──────────────────────
    # The pool is the publication schedule: one entry is one week, and at 20
    # entries the maps would have run dry inside five months. These are chosen
    # on the same test as the rest -- can you name four distinct layers of
    # LISTED companies that get paid from this -- which is what rules out the
    # themes that read exciting and have no chain beneath them (fusion, most
    # of longevity, vertical farming).
    {"key": "humanoid-robotics", "driver": "the humanoid robot build-out",
     "angle": "Actuators, harmonic drives, vision, edge silicon and the first buyers."},
    {"key": "advanced-packaging", "driver": "advanced chip packaging",
     "angle": "CoWoS and HBM stacking: OSATs, bonders, substrates, test."},
    {"key": "behind-the-meter-power", "driver": "on-site power for data centres",
     "angle": "Gas turbines, fuel cells, gensets, switchgear and the operators buying them."},
    {"key": "datacenter-construction", "driver": "the physical data-centre build",
     "angle": "Land and shell, concrete and steel, electrical contracting, commissioning."},
    {"key": "power-semis", "driver": "silicon carbide and GaN power semiconductors",
     "angle": "Substrates, devices, modules and the EV, grid and datacentre buyers."},
    {"key": "hydrogen-economy", "driver": "the hydrogen build-out",
     "angle": "Electrolysers, industrial gases, storage, pipelines and the offtakers."},
    {"key": "carbon-capture", "driver": "carbon capture and storage",
     "angle": "Capture kit, EPC, compression, pipelines and the emitters paying."},
    {"key": "lng-export", "driver": "the LNG export build-out",
     "angle": "Liquefaction trains, EPC, shipping, terminals and the producers."},
    {"key": "copper-supply", "driver": "the copper supply crunch",
     "angle": "Miners, smelters, wire and cable, and the grid and EV buyers."},
    {"key": "shipbuilding-maritime", "driver": "naval shipbuilding and maritime security",
     "angle": "Yards, propulsion, sensors, munitions and the sustainment tail."},
    {"key": "defense-drones", "driver": "drones and counter-drone",
     "angle": "Airframes, autonomy software, sensors, jammers and the primes integrating them."},
    {"key": "subsea-cables", "driver": "the subsea cable build-out",
     "angle": "Cable manufacture, laying vessels, landing stations, repeaters and the hyperscalers funding it."},
    {"key": "quantum-computing", "driver": "quantum computing",
     "angle": "Qubit hardware, cryogenics, control electronics, cloud access and the early buyers."},
    {"key": "robotaxi-autonomy", "driver": "autonomy going commercial",
     "angle": "Compute, lidar and radar, mapping, fleet operations and the OEMs."},
    {"key": "surgical-robotics", "driver": "robotic surgery",
     "angle": "Systems, instruments and consumables, imaging, and the hospitals buying."},
    {"key": "early-cancer-detection", "driver": "early cancer detection",
     "angle": "Sequencing, assays, labs, reimbursement and the pharma buyers of the data."},
    {"key": "ai-drug-discovery", "driver": "AI in drug discovery",
     "angle": "Compute, software, CROs and the pharma programmes actually using it."},
    {"key": "heat-electrification", "driver": "the electrification of heat",
     "angle": "Heat pumps, controls, distribution, installers and the utilities incentivising it."},
    {"key": "digital-identity", "driver": "digital identity and fraud prevention",
     "angle": "Verification, device signals, orchestration and the banks and marketplaces buying."},
    {"key": "rail-freight", "driver": "freight rail and intermodal renewal",
     "angle": "Locomotives, wagons, components, terminals and the railroads."},
    {"key": "animal-health", "driver": "animal health and pet care",
     "angle": "Pharma, diagnostics, clinics, food and the retail channel."},
    {"key": "space-defense", "driver": "military space",
     "angle": "Small satellites, launch, ground segment, comms and the primes."},
]


def driver_by_key(key: str) -> dict | None:
    """The driver a published map was built from. Needed to RE-cut an existing
    map: pick_driver deliberately skips anything already used, so a refresh
    cannot go through it without being handed a different subject."""
    k = (key or "").strip()
    if not k:
        return None
    for d in DRIVER_POOL:
        if d.get("key") == k:
            return d
    return None


def pick_driver(used_keys: set[str], seed: int = 0) -> dict | None:
    """Next unused driver. Deterministic per seed so a retry in the same week
    produces the same map rather than a different one."""
    fresh = [d for d in DRIVER_POOL if d["key"] not in (used_keys or set())]
    if not fresh:
        return None
    return fresh[seed % len(fresh)]


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:48] or "thesis-map"


def _s(v, limit: int, default: str = "") -> str:
    """Untrusted model text → a bounded single-line string."""
    out = re.sub(r"\s+", " ", str(v or "")).strip()
    return (out[:limit] or default)


def _universe_brief(rows: list[dict], cap: int = 600) -> str:
    """TICKER|Name|Sector lines. The model may ONLY choose from these, which is
    what keeps a generated map wired to live prices, shares and performance."""
    seen, out = set(), []
    for r in rows or []:
        t = (r.get("ticker") or "").upper()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(f"{t}|{_s(r.get('name'), 40)}|{_s(r.get('sub_sector') or r.get('sector'), 34)}")
        if len(out) >= cap:
            break
    return "\n".join(out)


_SCHEMA_HINT = """{
  "title": "<headline, max 62 chars, concrete. e.g. 'Where the grid rebuild money lands'>",
  "accent": "<2-4 word fragment OF THE TITLE to colour-highlight, must appear verbatim in title>",
  "standfirst": "<2-3 sentences, max 340 chars: what this chain is and what the map shows. End with 'Descriptive, not a forecast.'>",
  "market": {
    "note": "<1 sentence on where the size estimates come from, max 200 chars>",
    "stats": [{"k": "<label, max 30 chars>", "v": "<value, max 18 chars, e.g. ~$90B/yr>", "bar": 62}]
  },
  "layers": [
    {
      "name": "<layer name, max 54 chars — a STAGE of the chain, not a sector>",
      "lag": 0,
      "lagText": "<max 90 chars: when this layer sees the money, and why it matters>",
      "names": [
        {"t": "<TICKER from the list>", "n": "<company name>",
         "exposure": "hi|mid|lo",
         "stage": "<max 22 chars, e.g. 'Mature ramp' / 'Early'>",
         "concen": "<max 34 chars, customer concentration read>",
         "note": "<max 110 chars: why this company sits in THIS layer>",
         "watch": "<max 110 chars: the specific number to watch next print>"}
      ]
    }
  ],
  "how": "<max 420 chars: how to read the map and what the weights mean>"
}"""


def _prompt(driver: dict, universe: str, web_ctx: str) -> str:
    return f"""You are building a CHAIN MAP for a stock research app: one macro driver traced through the listed companies that get paid from it, layer by layer.

THE DRIVER: {driver['driver']}
THE ANGLE: {driver['angle']}

RECENT WEB RESEARCH (may be empty; prefer it over memory for figures and dates):
{web_ctx or '(none available)'}

COMPANIES YOU MAY USE — ticker|name|sector. You may ONLY name tickers from this
list. A company not on it does not exist for this map:
{universe}

RULES
- {MIN_LAYERS} to {MAX_LAYERS} layers, ordered UPSTREAM → DOWNSTREAM (who gets paid first → last).
  A layer is a STAGE in the money's journey ("Transformers & switchgear"), never a
  sector label ("Industrials").
- {MIN_PER_LAYER} to {MAX_PER_LAYER} companies per layer. Every ticker must come from the list above and
  appear in ONE layer only.
- "lag" is whole QUARTERS between the driver moving and that layer seeing revenue:
  0 for the layer that moves first, rising downstream. Max 4.
- "exposure" is how much of that company's TOTAL revenue depends on this driver:
  hi = most of the business, mid = a meaningful segment, lo = a small but real slice.
  Be honest — most large caps are "lo" or "mid" on any single theme. A map where
  everything is "hi" is a useless map.
- 3 or 4 "stats", each a round, defensible size estimate for the driver, with "bar"
  0-100 only where a share/percentage is genuinely meant.
- No investment advice, no price targets, no "buy". Describe where money lands.
- Every string within its stated length. No markdown, no commentary.

Return ONLY this JSON object, nothing else:
{_SCHEMA_HINT}"""


def _validate(raw: dict, driver: dict, known: dict[str, str]) -> dict | None:
    """Coerce the model's JSON into the theses.json schema. Anything that cannot
    be made safe is dropped rather than published."""
    if not isinstance(raw, dict):
        return None
    title = _s(raw.get("title"), 62)
    if not title:
        return None
    accent = _s(raw.get("accent"), 34)
    if accent and accent.lower() not in title.lower():
        accent = ""                      # highlight must be a fragment OF the title

    used: set[str] = set()
    layers: list[dict] = []
    for i, L in enumerate((raw.get("layers") or [])[:MAX_LAYERS]):
        if not isinstance(L, dict):
            continue
        names = []
        for c in (L.get("names") or [])[:MAX_PER_LAYER]:
            if not isinstance(c, dict):
                continue
            t = _s(c.get("t"), 8).upper()
            if t not in known or t in used:
                continue                 # unknown or already placed in another layer
            used.add(t)
            exp = _s(c.get("exposure"), 4).lower()
            names.append({
                "t": t,
                "n": _s(c.get("n"), 40) or known[t],
                "exposure": exp if exp in ("hi", "mid", "lo") else "lo",
                "stage":  _s(c.get("stage"), 22) or "—",
                "concen": _s(c.get("concen"), 34) or "—",
                "note":   _s(c.get("note"), 110),
                "watch":  _s(c.get("watch"), 110),
            })
        if len(names) < MIN_PER_LAYER:
            continue
        try:
            lag = max(0, min(4, int(L.get("lag") or 0)))
        except Exception:
            lag = i
        layers.append({
            "id": f"L{len(layers) + 1}",
            "name": _s(L.get("name"), 54) or f"Layer {len(layers) + 1}",
            "lag": lag,
            "color": f"var(--l{len(layers) + 1})",
            "lagText": _s(L.get("lagText"), 90),
            "names": names,
        })
    if len(layers) < MIN_LAYERS:
        logger.warning("thesis_map_gen: only %d valid layers for %s — rejecting",
                       len(layers), driver["key"])
        return None

    stats = []
    for s in ((raw.get("market") or {}).get("stats") or [])[:4]:
        if not isinstance(s, dict):
            continue
        row = {"k": _s(s.get("k"), 30), "v": _s(s.get("v"), 18)}
        try:
            bar = int(s.get("bar"))
            if 0 < bar <= 100:
                row["bar"] = bar
        except Exception:
            pass
        if row["k"] and row["v"]:
            stats.append(row)

    return {
        "slug": _slug(driver["key"]),
        "status": "live",
        "kind": "chain",
        "eyebrow": "Capex Chain",
        "title": title,
        "accent": accent,
        "standfirst": _s(raw.get("standfirst"), 340),
        # Kept for schema parity with the curated maps — thesis.html hides the
        # slider on realized-share maps, but the field is still read.
        "control": {"label": "", "min": -30, "max": 70, "value": 25, "step": 5},
        "share_weights": {"hi": 0.7, "mid": 0.35, "lo": 0.12,
                          "_note": "Theme-attributable fraction of trailing revenue by exposure band — "
                                   "a desk estimate, meant to be argued with."},
        "market": {"note": _s((raw.get("market") or {}).get("note"), 200), "stats": stats},
        "layers": layers,
        "how": _s(raw.get("how"), 420),
        "disclaimer": "Research and opinion for information only — not advice, not a personal "
                      "recommendation, not FCA-authorised. Capital at risk.",
    }


async def generate(driver: dict, universe_rows: list[dict]) -> dict | None:
    """Research + generate + validate one map. Returns None on any failure —
    the caller keeps last week's map rather than publishing something broken."""
    import llm_free

    known = {}
    for r in universe_rows or []:
        t = (r.get("ticker") or "").upper()
        if t:
            known.setdefault(t, _s(r.get("name"), 40) or t)
    if len(known) < 50:
        logger.warning("thesis_map_gen: universe too small (%d) — skipping", len(known))
        return None

    # ── Web research ─────────────────────────────────────────────────────────
    web_ctx, sources = "", []
    try:
        import web_search
        if web_search.available():
            hits = []
            for q in (f"{driver['driver']} market size 2026",
                      f"{driver['driver']} supply chain suppliers",
                      f"{driver['driver']} spending forecast"):
                hits.extend(await web_search.search(q, count=5) or [])
            seen_u = set()
            uniq = []
            for h in hits:
                u = (h.get("url") or "").split("#")[0]
                if u and u not in seen_u:
                    seen_u.add(u)
                    uniq.append(h)
            web_ctx = web_search.as_context(uniq, limit=10)
            sources = [{"title": _s(h.get("title"), 120), "url": h.get("url")}
                       for h in uniq[:8] if h.get("url")]
    except Exception as exc:
        logger.warning(f"thesis_map_gen: web research failed: {exc}")

    prompt = _prompt(driver, _universe_brief(universe_rows), web_ctx)
    raw = await llm_free.chat_json(prompt, max_tokens=4000, timeout=150.0, prefer=MODEL_PREFER)
    if not raw:
        logger.warning("thesis_map_gen: model returned nothing for %s", driver["key"])
        return None

    out = _validate(raw, driver, known)
    if not out:
        return None
    out["driver_key"]   = driver["key"]
    out["generated_at"] = datetime.now(timezone.utc).isoformat()
    out["sources"]      = sources
    out["web_grounded"] = bool(sources)
    n_co = sum(len(L["names"]) for L in out["layers"])
    logger.info("🧭 Thesis map generated: %s — %d layers, %d companies, web=%s",
                out["slug"], len(out["layers"]), n_co, out["web_grounded"])
    return out
