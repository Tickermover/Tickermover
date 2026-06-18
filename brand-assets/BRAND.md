# TickerMover — Brand assets

Master logo files for TickerMover. The website/app pull their own copies from
`static/` (see "Where it's wired" below) — these are the canonical sources to
hand to designers, upload to Stripe/social, or print.

## The mark
A royal-blue rounded tile with a white upward ticker line ending in an
up-right arrow — the market *moving up*. Reads as "Ticker" (the subject) +
"Mover" (the action; the part that's coloured).

## Files
| File | What | Use |
|---|---|---|
| `tickermover-mark.svg` | Full-colour gradient mark | App icon, avatar, favicon, anywhere square |
| `tickermover-lockup.svg` | Mark + wordmark + tagline | Headers, email signatures, decks, print |
| `tickermover-mark-mask.svg` | Silhouette (tile − arrow) | CSS `mask-image` spots that fill with a gradient |
| `tickermover-mark-1024.png` | High-res raster mark | Stripe, social avatars, app stores, anything needing PNG |
| `tickermover-mark-512.png` | Raster mark | General raster use |

## Colours
- **Gradient** (135°): `#5DB3F1` → `#2970FF` → `#0040C1`
  `linear-gradient(135deg, #5DB3F1 0%, #2970FF 50%, #0040c1 100%)`
- **Primary blue:** `#0040C1`
- **Accent / on-dark blue:** `#2970FF`
- **Ink (wordmark "Ticker"):** `#0F172A`
- **Muted text:** `#64748B`

## Type
Wordmark: a bold geometric sans — **Inter** / **Instrument Sans** at weight
800. "Ticker" in ink `#0F172A`, "Mover" in the brand gradient.

## Usage
- Keep clear space around the mark equal to ~25% of its width.
- Minimum mark size: 16px (favicon) — the arrow stays legible.
- On dark backgrounds, the gradient tile works as-is; use `#2970FF` for the
  "Mover" wordmark so it pops.
- Don't recolour the tile, stretch the mark, or add effects.

## Where it's wired in the app
- Web (SVG): `static/brand/tickermover-mark.svg`, `tickermover-mark-mask.svg`
- Favicons / email / PDF (PNG): `static/icons/` (`favicon-32.png`,
  `icon-192/256/512.png`, `alpha-logo-bare-*.png`, `alpha-logo-*.png` —
  legacy filenames kept so the swap was drop-in).
- PDF reports load `static/icons/alpha-logo-bare-512.png` (`pdf_render.py`).
