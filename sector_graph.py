"""
AlphaHunt — sector relationship graph generator.

Produces the *topology* for the Universe "Sector connections" graph: a directed
edge list among the live sub-sectors, where each edge `source → target` means
"source supplies / enables / feeds into target" (value or material flows from
source to target). The focused-node UI derives upstream (incoming) vs downstream
(outgoing) from edge direction, so the edge `type` only classifies the KIND of
link for colour:

    raw_material  — base materials / commodities feeding a sector   (amber)
    supply        — components, equipment, chips, parts             (blue)
    infrastructure— compute / power / network / platform depended on (green)
    adjacent      — complementary or substitute peers               (slate)

The node *values* (α-Score, stock count, colour, size) are NOT produced here —
they stay live, overlaid client-side from the universe data, exactly like the
old heat map. This module only authors the (rarely-changing) wiring, so the
result is generated ONCE with the cheapest model and cached durably.

Returns:  {nodes:[{id}], edges:[{source,target,type,note}], model, status}
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx

logger = logging.getLogger(__name__)

_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
# Cheapest model — the wiring is simple and generated once, so Haiku is plenty.
# Override with ANTHROPIC_SECTOR_GRAPH_MODEL; falls back to ANTHROPIC_MODEL.
_MODEL = (
    os.environ.get("ANTHROPIC_SECTOR_GRAPH_MODEL")
    or os.environ.get("ANTHROPIC_MODEL")
    or "claude-haiku-4-5-20251001"
).strip()
_TIMEOUT = float(os.environ.get("SECTOR_GRAPH_TIMEOUT", "120"))

_TYPES = {"raw_material", "supply", "infrastructure", "adjacent"}
_MAX_EDGES = 140


def available() -> bool:
    return bool(_KEY)


# ── Hand-curated fallback wiring ──────────────────────────────────────────
# Used when the AI is unavailable (no key / API down / Supabase down) and to
# backfill when the model returns too few edges, so the graph is NEVER empty
# and costs $0 in that path. Endpoints are matched case-insensitively against
# the live sub-sector list and substring-matched, so generic GICS names
# ("Basic Materials", "Utilities") still attach even when the exact curated
# label isn't a live group. Direction = source feeds/enables target.
SEED_EDGES: list[dict] = [
    # Raw materials / power feeding the silicon + datacenter stack
    {"source": "Basic Materials", "target": "AI Semiconductors", "type": "raw_material", "note": "wafers, rare earths"},
    {"source": "Energy", "target": "AI Power / Grid Infrastructure", "type": "raw_material", "note": "fuel & generation"},
    {"source": "Utilities", "target": "AI Power / Grid Infrastructure", "type": "raw_material", "note": "grid capacity"},
    {"source": "AI Power / Grid Infrastructure", "target": "Data Center REITs", "type": "infrastructure", "note": "powers racks"},
    {"source": "AI Power / Grid Infrastructure", "target": "AI Data Center / GPU Cloud", "type": "infrastructure", "note": "1GW per cluster"},
    # Components / equipment → chips
    {"source": "Semiconductor Test Equipment", "target": "AI Semiconductors", "type": "supply", "note": "ATE / probe cards"},
    {"source": "SiC / Wide Bandgap Power Semis", "target": "AI Power / Grid Infrastructure", "type": "supply", "note": "power modules"},
    {"source": "Optical Components / Photonics", "target": "Networking & Switch Infrastructure", "type": "supply", "note": "800G/1.6T optics"},
    {"source": "Thermal / Cooling Management", "target": "Data Center REITs", "type": "supply", "note": "liquid cooling"},
    {"source": "Networking & Switch Infrastructure", "target": "AI Data Center / GPU Cloud", "type": "supply", "note": "cluster fabric"},
    # Chips → compute infrastructure
    {"source": "AI Semiconductors", "target": "AI Data Center / GPU Cloud", "type": "supply", "note": "GPUs / HBM"},
    {"source": "AI Semiconductors", "target": "Edge AI / Edge Computing", "type": "supply", "note": "low-power inference"},
    {"source": "AI Data Center / GPU Cloud", "target": "Cloud / Hyperscalers", "type": "infrastructure", "note": "GPU-as-a-service"},
    # Infrastructure → platforms → applications
    {"source": "Cloud / Hyperscalers", "target": "Enterprise SaaS / Cloud", "type": "infrastructure", "note": "hosts SaaS"},
    {"source": "Cloud / Hyperscalers", "target": "Agentic AI / AI Software", "type": "infrastructure", "note": "model serving"},
    {"source": "Enterprise SaaS / Cloud", "target": "Agentic AI / AI Software", "type": "infrastructure", "note": "system of record"},
    {"source": "Cloud / Hyperscalers", "target": "Cybersecurity", "type": "adjacent", "note": "expands attack surface"},
    {"source": "Data Center REITs", "target": "AI Data Center / GPU Cloud", "type": "infrastructure", "note": "colocation"},
    {"source": "5G & Fiber Infrastructure", "target": "Edge AI / Edge Computing", "type": "infrastructure", "note": "last-mile latency"},
    {"source": "Satellite / Space Communications", "target": "5G & Fiber Infrastructure", "type": "adjacent", "note": "direct-to-device"},
    {"source": "Networking & Switch Infrastructure", "target": "Cloud / Hyperscalers", "type": "supply", "note": "backend fabric"},
    {"source": "Robotics / Physical AI", "target": "Industrial Automation / Smart Grid", "type": "infrastructure", "note": "factory cobots"},
    {"source": "AI Semiconductors", "target": "Robotics / Physical AI", "type": "supply", "note": "edge SoCs"},
    {"source": "Quantum Computing", "target": "Cybersecurity", "type": "adjacent", "note": "post-quantum crypto"},
    {"source": "Digital Payments / Fintech", "target": "Financial Services", "type": "infrastructure", "note": "payment rails"},
    {"source": "AI Data Center / GPU Cloud", "target": "AI Defense & Intelligence", "type": "infrastructure", "note": "ISR compute"},
    {"source": "Agentic AI / AI Software", "target": "Digital Media / Streaming / AdTech", "type": "infrastructure", "note": "ad optimisation"},
    {"source": "Cloud / Hyperscalers", "target": "Digital Media / Streaming / AdTech", "type": "infrastructure", "note": "CDN / streaming"},
]


def _system() -> str:
    return (
        "You are a sell-side equity strategist who maps supply chains and value "
        "chains between stock-market sub-sectors. You output ONLY strict JSON — no "
        "prose, no markdown, no code fences."
    )


def _user(sectors: list[str]) -> str:
    listing = "\n".join(f"- {s}" for s in sectors)
    return (
        "Below is the EXACT list of sub-sectors currently in our universe. Build a "
        "directed relationship graph that connects them into a coherent value chain "
        "(raw materials → components → infrastructure → platforms → applications), "
        "plus lateral/complementary links.\n\n"
        "Rules:\n"
        "1. Every edge is DIRECTED: \"source → target\" means source SUPPLIES, ENABLES, "
        "or FEEDS INTO target (value/material flows source→target).\n"
        "2. Use ONLY names from the list verbatim for both `source` and `target`. "
        "Never invent a sub-sector.\n"
        "3. Classify each edge `type` as exactly one of: "
        "\"raw_material\" (base materials/commodities/energy feeding a sector), "
        "\"supply\" (components, equipment, chips, parts), "
        "\"infrastructure\" (compute/power/network/platform the target runs on), "
        "\"adjacent\" (complementary or substitute peers).\n"
        "4. `note` = a ≤4-word plain-English label for the link (e.g. \"HBM test demand\").\n"
        "5. Aim for 2-5 edges per sub-sector where real relationships exist; skip a "
        "sector rather than invent a weak link. Prefer economically meaningful links.\n\n"
        f"Sub-sectors:\n{listing}\n\n"
        "Respond with ONLY this JSON shape:\n"
        '{"edges":[{"source":"<name>","target":"<name>","type":"<type>","note":"<≤4 words>"}]}'
    )


def _coerce_edges(raw: object, sectors: list[str]) -> list[dict]:
    """Keep only well-formed edges whose endpoints are both in `sectors`,
    dedupe by (source,target,type), drop self-loops, cap the total."""
    by_lower = {s.lower(): s for s in sectors}
    out: list[dict] = []
    seen: set = set()
    items = raw if isinstance(raw, list) else []
    for e in items:
        if not isinstance(e, dict):
            continue
        src = by_lower.get(str(e.get("source", "")).strip().lower())
        tgt = by_lower.get(str(e.get("target", "")).strip().lower())
        typ = str(e.get("type", "")).strip().lower()
        if not src or not tgt or src == tgt:
            continue
        if typ not in _TYPES:
            typ = "supply"
        key = (src, tgt, typ)
        if key in seen:
            continue
        seen.add(key)
        note = str(e.get("note", "")).strip()[:40]
        out.append({"source": src, "target": tgt, "type": typ, "note": note})
        if len(out) >= _MAX_EDGES:
            break
    return out


def seed_graph(sectors: list[str]) -> dict:
    """Curated fallback wiring filtered to the live sub-sector list. Matches
    endpoints case-insensitively with a substring fallback so generic GICS
    names attach. Always returns a usable (possibly small) graph."""
    lower = {s.lower(): s for s in sectors}

    def _match(name: str) -> str | None:
        n = name.lower()
        if n in lower:
            return lower[n]
        for low, orig in lower.items():
            if n in low or low in n:
                return orig
        return None

    edges: list[dict] = []
    seen: set = set()
    for e in SEED_EDGES:
        src = _match(e["source"])
        tgt = _match(e["target"])
        if not src or not tgt or src == tgt:
            continue
        key = (src, tgt, e["type"])
        if key in seen:
            continue
        seen.add(key)
        edges.append({"source": src, "target": tgt, "type": e["type"], "note": e["note"]})
    return {
        "nodes": [{"id": s} for s in sectors],
        "edges": edges,
        "model": "seed",
        "status": "ready",
    }


async def generate_sector_graph(sectors: list[str]) -> dict:
    """Generate the relationship topology for `sectors` via one cheap Haiku call.
    Falls back to (and backfills from) the curated seed so the result is never
    empty. Raises only if the AI is unavailable AND the seed is empty."""
    sectors = [s for s in dict.fromkeys(s for s in sectors if s and str(s).strip())]
    if not sectors:
        return {"nodes": [], "edges": [], "model": "seed", "status": "ready"}

    if not available():
        logger.info("sector_graph: ANTHROPIC_API_KEY unset — serving seed wiring")
        return seed_graph(sectors)

    body = {
        "model": _MODEL,
        "max_tokens": 4000,
        "system": [
            {"type": "text", "text": _system(), "cache_control": {"type": "ephemeral"}}
        ],
        "messages": [{"role": "user", "content": _user(sectors)}],
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": _KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
            )
            if r.status_code >= 400:
                logger.error(f"sector_graph → {r.status_code} (model={_MODEL}): {r.text[:400]}")
                return seed_graph(sectors)
            data = r.json()
    except Exception as exc:
        logger.error(f"sector_graph request failed: {exc}")
        return seed_graph(sectors)

    _u = data.get("usage") or {}
    logger.info(
        "sector_graph (%s): nodes=%s in=%s cache_write=%s cache_read=%s out=%s",
        _MODEL, len(sectors), _u.get("input_tokens"),
        _u.get("cache_creation_input_tokens"), _u.get("cache_read_input_tokens"),
        _u.get("output_tokens"),
    )

    text = "".join(
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE).strip()
    parsed: object = None
    try:
        parsed = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group(0))
            except Exception:
                parsed = None

    raw_edges = parsed.get("edges") if isinstance(parsed, dict) else None
    edges = _coerce_edges(raw_edges, sectors)

    # Backfill from the seed if the model under-delivered (e.g. truncation), so
    # the web is never sparse. Seed edges that duplicate AI edges are dropped.
    if len(edges) < max(6, len(sectors) // 3):
        seen = {(e["source"], e["target"], e["type"]) for e in edges}
        for e in seed_graph(sectors)["edges"]:
            key = (e["source"], e["target"], e["type"])
            if key not in seen:
                edges.append(e)
                seen.add(key)

    if not edges:
        return seed_graph(sectors)

    return {
        "nodes": [{"id": s} for s in sectors],
        "edges": edges[:_MAX_EDGES],
        "model": _MODEL,
        "status": "ready",
    }
