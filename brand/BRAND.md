# Tabularia brand

> **Visual reference: [`board.html`](board.html)** (rendered in [`board.png`](board.png)), approved by the owner on 2026-10-18. The marks below are copied from its `#M` and `#m` symbols; if this file and the board ever disagree, the board wins and this file is corrected.

## The idea: order from noise

Tabularia turns messy input into a table someone can trust. The mark draws exactly that, in three parts:

| Part | What it is | What it means |
| --- | --- | --- |
| **Noise** | loose, uneven fragments on the left, before the flow | the input as it arrives: scattered, inconsistent, not yet a table |
| **Flow** | one blue curve rising from bottom left to top right | the transformation, the flow a user builds; it cuts its own channel through the grid |
| **Order** | a clean, rounded 3×3 grid, only on the side after the flow | the result: rows and columns, ready to publish |

Tagline: **Messy in. Trusted out.**

## Marks

### Full mark (24 px and larger)

Drawn on a 48 × 48 grid (`viewBox="0 0 48 48"`):

- **Order**: a 36 × 36 rounded square at (6, 6), corner radius 8, plus two vertical and two horizontal rules at 18 and 30, all stroked at 3 with round joins. The grid is **clipped** to the region after the flow (`M48 0V48H0V41C16 41 22 9.5 48 8Z`) and **masked** by a 9-unit channel along the flow, so the curve never touches it.
- **Noise**: three pill-shaped fragments, 3 units tall, radius 1.5: 7 wide at (9, 9.5), 5 wide at (5.5, 17), 5.5 wide at (11, 24).
- **Flow**: `M5 39C19 39 24 10 43 9`, stroke 4.6, round caps.

### Reduced mark (below 24 px)

At favicon sizes a 3×3 grid and three fragments become a smudge, so a separate drawing takes over on a 24 × 24 grid: two fragments and a 2×2 grid (18 × 18 at (3, 3), radius 4.5, stroke 2.2), flow `M2.5 19.5C9.5 19.5 12 5 21.5 4.5` at 2.8 with a 5.4 channel. It is not the full mark scaled down; never scale the full mark below 24 px or the reduced mark above 32 px.

### Minimum sizes and clear space

| Asset | Minimum |
| --- | --- |
| Full mark | 24 px on screen, 7 mm in print |
| Reduced mark | 12 px; use it from 12 to 23 px |
| Lockup (mark + wordmark) | 32 px tall (the wordmark is then ≈ 14 px) |

Clear space around the mark's 48-unit box: at least **one grid cell** (12 units, a quarter of the mark's size) on every side. The box already carries a small inner margin; the clear space is in addition to it. Nothing (text, edges, other marks) enters that area.

### Lockup

Mark on the left, the wordmark **Tabularia** on the right, in IBM Plex Sans SemiBold (600), tracking −0.02 em. Proportions from the board: with the mark at height *h*, the wordmark is set at 0.426 *h* and the gap between them is 0.204 *h*; the wordmark's line box is centred on the mark, which in Plex Sans centres the capitals on it (within half a unit). The lockup files have the wordmark **outlined** (paths from the self-hosted Plex Sans, `wght` 600), so they do not depend on an installed font.

Inside the app the wordmark beside the mark is live text in the interface's own type (`AppShell.vue`, `Toolbar.vue`, the sign-in card); it follows the app's type scale rather than the lockup.

### App icon

The full mark centred on an Ink tile (56 × 56 units, radius 12.5, mark at 48 units): use it wherever the mark stands alone outside the product (repository, docs, store-like listings, a tile on a dashboard). The apple-touch icon is the same tile square and full-bleed, because iOS rounds it itself. Browser-tab PNG favicons are a small Ink tile with the **reduced** mark, so they read on light and dark tab bars alike.

## Colour

| Token | Hex | Role |
| --- | --- | --- |
| **Flow** | `#4F8CFF` | the flow in the mark; the accent: primary actions, links, focus |
| **Order** | `#E8EBF2` | the grid on dark grounds; primary text on Ink |
| **Noise** | `#8B93A7` | the fragments; secondary text on dark |
| **Ink** | `#0B0E14` | the ground: app background, app-icon tile |
| **Trusted** | `#6EE7B7` | secondary, sparing: a result that passed (contract OK, a successful run) |

On light grounds the mark uses deeper values with the same roles: grid `#141926`, fragments `#6B7590`, flow `#3B6FE0` (the light theme's accent). In the app these are not new tokens: in the default dark theme Order, Noise, Flow, Ink and Trusted are exactly `--text`, `--muted`, `--accent`, `--bg` and `--accent-2`, and every theme re-values those roles, so `BrandMark.vue` follows all five themes by naming roles (grid = `--text`, fragments = `--muted`, flow = `--accent`).

### Contrast (WCAG 2.x)

Text uses (AA: 4.5:1 normal, 3:1 large ≥ 24 px or ≥ 18.7 px bold):

| Foreground on background | Ratio | Verdict |
| --- | --- | --- |
| Order `#E8EBF2` on Ink | 16.2:1 | AAA |
| Noise `#8B93A7` on Ink | 6.3:1 | AA (secondary text) |
| Flow `#4F8CFF` on Ink | 6.0:1 | AA (links, accent words) |
| Trusted `#6EE7B7` on Ink | 12.7:1 | AAA |
| Ink on Flow | 6.0:1 | AA |
| Ink on Trusted | 12.7:1 | AAA |
| White on Flow | 3.2:1 | **large text only**: for normal-size labels on a Flow fill use Ink, or white on the deeper `#3B6FE0` (4.6:1) |
| `#141926` on white | 17.6:1 | AAA |
| `#6B7590` on white | 4.6:1 | AA |
| `#3B6FE0` on white | 4.6:1 | AA |
| Noise `#8B93A7` on white | 3.1:1 | fails for text: use `#6B7590` on light grounds |
| Trusted `#6EE7B7` on white | 1.5:1 | never on light grounds; the light theme uses `#0A7D56` for this role |

The mark itself is a graphic (WCAG 1.4.11, 3:1): every part of the dark variant clears 5:1 on Ink, every part of the light variant clears 4.5:1 on white. Check any new ground before placing the mark on it.

## Typography

- **IBM Plex Sans** (OFL): display and text, wordmark at 600. Self-hosted in the app as a variable woff2 (`frontend/assets/fonts/ibm-plex-sans-latin-var.woff2`, weights 100 to 700).
- **IBM Plex Mono** (OFL): code, data, identifiers and labels in brand material. The app does not ship it yet: in the interface the system monospace stack plays that role (see `DESIGN.md`).

## Voice

Sober and technical. Say what the product does, in the user's terms: connect, prepare, preview, publish, schedule. Short declarative lines (*Your data, in order. On your servers.* — *Open source · self-hosted · no lock-in*), no superlatives, no exclamation marks.

- **Never** name or compare with competitors in public texts (README, docs, site, brand material). Integrations are named for what they are (dbt, ClickHouse, OpenLineage); rival products are not named at all.
- Never invent testimonials, customer names, benchmarks or pricing (see `PRODUCT.md`).
- English is the base language; the product is localised in five languages.

## Do

- Use the files below as they are; pick the dark or light variant by the ground, the mono one for single-colour reproduction (engraving, fax-like prints, a one-colour sticker).
- Keep the three colour roles: grid = order, fragments = noise, flow = accent.
- Use the reduced mark below 24 px and the full mark from 24 px up.
- Keep the clear space; put the mark on a flat ground.

## Don't

- Don't redraw, rotate, mirror, stretch or re-space the mark, and don't move the fragments to the right of the flow.
- Don't fill the grid, draw it through the channel, or show it before the flow: order only exists after the transformation.
- Don't recolour the flow with Trusted, a gradient or a glow; don't add shadows, outlines or 3D.
- Don't scale the full mark below 24 px or the reduced mark above 32 px.
- Don't put the dark variant on a light ground or the Ink tile on Ink without a border.
- Don't set the wordmark in another typeface or weight, or retype it in a lockup: use the outlined files.

## Files

| File | Use |
| --- | --- |
| `board.html`, `board.png` | the approved brand board (visual reference) |
| `mark-dark.svg` | full mark for dark grounds |
| `mark-light.svg` | full mark for light grounds |
| `mark-mono.svg` | full mark in one colour (`currentColor`: inherits the text colour when inlined, black as an image) |
| `mark-small-dark.svg`, `mark-small-light.svg` | reduced mark (below 24 px) |
| `app-icon.svg` | Ink tile with the full mark |
| `lockup-dark.svg`, `lockup-light.svg` | mark + outlined wordmark |

In the app: `frontend/components/ui/BrandMark.vue` (theme-aware, picks the reduced drawing below 24 px), `frontend/public/favicon.svg` (reduced mark, follows the browser's colour scheme), `frontend/public/logo.svg` (app icon), and the rasters `favicon-16.png`, `favicon-32.png`, `favicon.ico` (16/32 reduced, 48 full), `favicon-512.png`, `logo.png` (1024) and `apple-touch-icon.png` (180, full-bleed). Every SVG uses ids unique to its file, so several can be inlined in one page.
