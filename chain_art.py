"""Thumbnail art for the capex-chain cards.

Eleven cards on /who-benefits (and in the in-app Capex Chains panel) were eleven
identical white rectangles: eyebrow, title, three lines of grey. Nothing told a
reader at a glance that one map was about power and the next about payments, so
the grid read as a list rather than a library.

The art is generated, not drawn: a new map ships every Sunday from
`thesis_map_gen`, so anything hand-made per slug would leave the newest map --
the one actually being promoted -- as the only blank card. Each thumbnail is
inline SVG picked by keyword from the map's own slug/title/standfirst, so a
generated map lands with art on the day it publishes, with no asset pipeline, no
image weight and no cache-busting involved.

One family, two variables: every thumbnail is the same composition (a soft
two-stop wash, a dashed chain rule with three nodes, one line-art glyph), and
only the TONE and the GLYPH change. That is what keeps eleven different subjects
looking like one product. The tones are the site palette's existing accent
family -- the same blue/amber/teal/green/violet the panel ribbons use -- so no
new hue enters the system here.

`thumb_svg()` is the whole public surface; it never raises and always returns a
string, because a missing thumbnail must not be able to take a page down.
"""

from __future__ import annotations

# -- tones ----------------------------------------------------------------
# (wash start, wash end, glyph ink, accent). The wash lands on a white card, so
# both stops stay very light; the ink is dark enough to hold a 2px stroke.
TONES = {
    "blue":   ("#EAF1F8", "#FBFCFE", "#0A2F46", "#14587D"),
    "amber":  ("#FDF1E6", "#FFFCFA", "#6B3410", "#C74E00"),
    "teal":   ("#E8F2F1", "#FBFDFD", "#12433E", "#0F766E"),
    "green":  ("#E9F4EE", "#FBFDFC", "#14432B", "#15803D"),
    "violet": ("#EFEFFA", "#FCFCFE", "#252F63", "#4A5BC4"),
    "navy":   ("#ECEFF2", "#FBFCFD", "#0A2F46", "#33566B"),
}

# -- glyphs ---------------------------------------------------------------
# Each drawn stroke-only inside a 48x48 box, so one transform places them all.
# %(acc)s marks the one element per glyph that carries the accent colour.
GLYPHS = {
    # a die with its pins, plus the links fanning out of it
    "chip": """<rect x="13" y="13" width="22" height="22" rx="3"/>
<rect x="20" y="20" width="8" height="8" rx="1" stroke="%(acc)s"/>
<path d="M19 13V7M24 13V7M29 13V7M19 35v6M24 35v6M29 35v6M13 19H7M13 24H7M13 29H7M35 19h6M35 24h6M35 29h6"/>""",
    # transmission pylon carrying two lines off the edges
    "pylon": """<path d="M24 6 13 42M24 6l11 36M17 19h14M15 28h18M13 37h22"/>
<path d="M2 15l10-4 12 4 12-4 10 4" stroke="%(acc)s"/>""",
    # generating plant: two cooling towers and the output bolt. Separate from
    # the pylon on purpose -- the grid map and the power-build map are two
    # different maps and were drawing the identical thumbnail.
    "plant": """<path d="M6 42V28c0-5 2.5-7 2.5-12h9C17.5 21 20 23 20 28v14z"/>
<path d="M26 42V32c0-4 2-5.5 2-9.5h7c0 4 2 5.5 2 9.5v10z"/>
<path d="M14 12v-4M31 18v-4" stroke="%(acc)s"/>
<path d="M30 8l-4 6h4l-2 4 5-6h-4z" stroke="%(acc)s"/>""",
    # shield with a chevron
    "shield": """<path d="M24 6l15 6v11c0 10-6.5 16.5-15 19-8.5-2.5-15-9-15-19V12z"/>
<path d="M17 24l5 5 9-10" stroke="%(acc)s"/>""",
    # sawtooth factory roof and a stack
    "factory": """<path d="M5 42V25l10 6v-6l10 6v-6l10 6v16z"/><path d="M35 31V13h7v29"/>
<path d="M12 42v-6h6v6M27 42v-6h6v6" stroke="%(acc)s"/>""",
    # cell with a terminal and a bolt
    "battery": """<rect x="6" y="16" width="32" height="17" rx="4"/><path d="M38 22v5h4v-5z"/>
<path d="M23 19l-6 8h6l-2 6 7-9h-6z" stroke="%(acc)s"/>""",
    # jointed arm over a base
    "robot": """<path d="M9 42h16M17 42V31"/><circle cx="17" cy="30" r="3"/>
<path d="M19 28l11-9"/><circle cx="31" cy="18" r="3"/>
<path d="M34 17l6 3M40 16v9" stroke="%(acc)s"/>""",
    # wafer with a die grid and the exposure beam
    "wafer": """<circle cx="24" cy="27" r="15"/><path d="M14 14l4 4"/>
<path d="M12 21h24M12 33h24M18 13v29M30 13v29"/>
<path d="M24 4v6M18 6l2 4M30 6l-2 4" stroke="%(acc)s"/>""",
    # padlock over its layers
    "lock": """<rect x="11" y="22" width="26" height="18" rx="4"/>
<path d="M17 22v-5a7 7 0 0114 0v5"/><path d="M24 29v5" stroke="%(acc)s"/>""",
    # card and the flow through it
    "card": """<rect x="5" y="14" width="38" height="22" rx="4"/><path d="M5 22h38"/>
<path d="M12 30h7"/><path d="M28 30h8m-3-3l3 3-3 3" stroke="%(acc)s"/>""",
    # capsule and a molecule
    "capsule": """<path d="M12 30l12-12a7 7 0 0110 10L22 40a7 7 0 01-10-10z"/><path d="M17 25l10 10"/>
<circle cx="37" cy="12" r="3" stroke="%(acc)s"/><circle cx="29" cy="8" r="2.5" stroke="%(acc)s"/>
<path d="M31 9l4 2" stroke="%(acc)s"/>""",
    # nucleus and its orbits
    "atom": """<circle cx="24" cy="24" r="4" stroke="%(acc)s"/><ellipse cx="24" cy="24" rx="18" ry="7"/>
<ellipse cx="24" cy="24" rx="18" ry="7" transform="rotate(60 24 24)"/>
<ellipse cx="24" cy="24" rx="18" ry="7" transform="rotate(-60 24 24)"/>""",
    # dish on a mast
    "tower": """<path d="M24 42V20M16 42h16"/><path d="M14 20L24 8l10 12"/>
<path d="M10 16a19 19 0 0128 0" stroke="%(acc)s"/>""",
    # a body in orbit
    "satellite": """<rect x="19" y="19" width="10" height="10" rx="2"/>
<path d="M19 22H8v4h11M29 22h11v4H29"/><path d="M13 38a20 20 0 0022-28" stroke="%(acc)s"/>""",
    # a drop over a level line
    "drop": """<path d="M24 7c7 9 11 14 11 20a11 11 0 01-22 0c0-6 4-11 11-20z"/>
<path d="M15 29c4 3 6 3 9 0s5-3 9 0" stroke="%(acc)s"/>""",
    # stacked cloud
    "cloud": """<path d="M15 34a9 9 0 010-18 12 12 0 0123 3 8 8 0 01-2 15z"/>
<path d="M18 40h12" stroke="%(acc)s"/>""",
    # the fallback: the chain itself, narrowing as it goes
    "layers": """<path d="M8 14h32M13 24h22M18 34h12"/>
<path d="M24 38v4m-3-4l3 4 3-4" stroke="%(acc)s"/>""",
}

# -- routing --------------------------------------------------------------
# First match wins, so the specific sits above the general: "who builds the chip
# factories" must read as the wafer-fab map, not get caught by "chip" or "ai".
THEMES = [
    (("semi-equipment", "chip factor", "lithograph", "wafer", "fab equipment",
      "foundry", "semiconductor equipment"), "wafer", "blue"),
    (("ai capex", "ai-capex", "hyperscaler", "datacenter", "data center",
      "data centre", "accelerator", "gpu", "compute chain"), "chip", "blue"),
    (("grid", "transmission", "substation", "utility", "electrif"),
     "pylon", "amber"),
    (("power", "turbine", "generation", "megawatt", "gigawatt"), "plant", "teal"),
    (("nuclear", "uranium", "smr", "reactor"), "atom", "teal"),
    (("defense", "defence", "military", "missile", "munition"), "shield", "navy"),
    (("cyber", "security budget", "zero trust", "endpoint"), "lock", "violet"),
    (("reshor", "factory build", "onshor", "manufactur", "industrial build"),
     "factory", "amber"),
    (("battery", "ev ", "ev-", "electric vehicle", "lithium", "cathode"),
     "battery", "green"),
    (("robot", "automation", "cobot", "warehouse"), "robot", "violet"),
    (("payment", "fintech", "card network", "checkout", "merchant"), "card", "teal"),
    (("glp", "obesity", "drug", "pharma", "biotech", "therap", "vaccine"),
     "capsule", "violet"),
    (("water", "desalinat", "irrigation"), "drop", "blue"),
    (("space", "satellite", "orbit", "launch vehicle"), "satellite", "navy"),
    (("telecom", "5g", "broadband", "fiber", "fibre", "spectrum"), "tower", "violet"),
    (("cloud", "software", "saas", "subscription"), "cloud", "blue"),
]


def theme_for(slug: str = "", title: str = "", standfirst: str = "") -> tuple:
    """(glyph key, tone key) for one map.

    Slug and title carry the subject and are read first; the standfirst is a
    tie-breaker only, because it is prose and will happily mention "power" in a
    map about payments."""
    head = (" %s %s " % (slug or "", title or "")).lower().replace("-", " ")
    tail = " %s " % (standfirst or "").lower()
    for keys, glyph, tone in THEMES:
        if any(k in head for k in keys):
            return glyph, tone
    for keys, glyph, tone in THEMES:
        if any(k in tail for k in keys):
            return glyph, tone
    return "layers", "navy"


def thumb_svg(slug: str = "", title: str = "", standfirst: str = "",
              cls: str = "wb-art") -> str:
    """One card thumbnail as inline SVG. Never raises."""
    try:
        glyph_key, tone_key = theme_for(slug, title, standfirst)
        c1, c2, ink, acc = TONES.get(tone_key, TONES["navy"])
        glyph = GLYPHS.get(glyph_key, GLYPHS["layers"]) % {"acc": acc}
        # gradient ids must not collide when several thumbnails share a page
        uid = "ca-" + "".join(
            ch if ch.isalnum() else "-" for ch in (slug or "map").lower())[:40]
        return (
            '<svg class="%s" viewBox="0 0 320 116" preserveAspectRatio="xMidYMid slice" '
            'role="img" aria-hidden="true" focusable="false">'
            '<defs><linearGradient id="%s" x1="0" y1="0" x2="1" y2="1">'
            '<stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/>'
            "</linearGradient></defs>"
            '<rect width="320" height="116" fill="url(#%s)"/>'
            # the chain rule: the one element every map shares, so the grid
            # reads as a family before the glyph says which map it is
            '<path d="M-4 92H324" fill="none" stroke="%s" stroke-opacity=".2" '
            'stroke-width="1.4" stroke-dasharray="2 6"/>'
            '<g fill="%s" fill-opacity=".16"><circle cx="58" cy="92" r="3.4"/>'
            '<circle cx="160" cy="92" r="3.4"/><circle cx="262" cy="92" r="3.4"/></g>'
            '<circle cx="58" cy="92" r="3.4" fill="%s" fill-opacity=".6"/>'
            '<g transform="translate(132 14) scale(1.12)" fill="none" stroke="%s" '
            'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" '
            'stroke-opacity=".82">%s</g>'
            "</svg>"
        ) % (cls, uid, c1, c2, uid, ink, ink, acc, ink, glyph)
    except Exception:
        return ""
