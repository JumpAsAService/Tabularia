---
name: Tabularia
description: Self-hosted visual data preparation; a dense, themed workbench where the data is the subject and the interface recedes.
colors:
  # Default theme (dark, no data-theme attribute). Keys are the real custom-property
  # names in frontend/assets/main.css. Light / dracula / monokai values: see Colors.
  accent: "#4f8cff"
  accent-hi: "#7aa8ff"
  grad-accent-end: "#7c6cff"
  on-accent: "#ffffff"
  accent-2: "#6ee7b7"
  on-accent-2: "#0f1117"
  violet: "#a78bfa"
  danger: "#ff6b6b"
  bg: "#0b0e14"
  bg-soft: "#0f131c"
  panel: "#141926"
  panel-2: "#1b2130"
  border: "#262e40"
  border-soft: "#1e2534"
  control-border: "#5c6884"
  edge: "#3d4a66"
  text: "#e8ebf2"
  muted: "#8b93a7"
  # Operation-category encoding (composables/useOpIcons.ts and components/nodes).
  # Literal values, identical in every theme.
  op-columns: "#4f8cff"
  op-rows: "#f59e0b"
  op-nulls: "#a78bfa"
  op-shape: "#6ee7b7"
  op-foreach: "#f472b6"
  op-output: "#fbbf24"
  op-control: "#38bdf8"
  edge-sequence: "#c084fc"
typography:
  headline:
    fontFamily: "'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "19px"
    fontWeight: 700
  title:
    fontFamily: "'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "16px"
    fontWeight: 700
  body:
    fontFamily: "'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "13.5px"
    fontWeight: 400
  body-dense:
    fontFamily: "'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "12px"
    fontWeight: 400
  label:
    fontFamily: "'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "12px"
    fontWeight: 500
  label-caps:
    fontFamily: "'IBM Plex Sans', system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif"
    fontSize: "11.5px"
    fontWeight: 600
    letterSpacing: "0.05em"
  mono:
    fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace"
    fontSize: "12px"
    fontWeight: 400
rounded:
  xs: "4px"
  sm: "6px"
  md: "8px"
  lg: "10px"
  xl: "14px"
  pill: "999px"
spacing:
  control-gap: "6px"
  stack: "8px"
  section: "16px"
  page-y: "22px"
  page-x: "24px"
components:
  button:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "6px 12px"
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "{rounded.md}"
    padding: "6px 12px"
  button-mini:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "3px 8px"
    height: "24px"
  button-danger:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.danger}"
    rounded: "{rounded.md}"
    padding: "6px 12px"
  button-danger-hover:
    backgroundColor: "{colors.danger}"
    textColor: "#ffffff"
  input:
    backgroundColor: "{colors.bg-soft}"
    textColor: "{colors.text}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "6px 10px"
  select-trigger:
    backgroundColor: "{colors.bg-soft}"
    textColor: "{colors.text}"
    rounded: "{rounded.md}"
    padding: "6px 10px"
  select-panel:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    padding: "4px"
  nav-link:
    textColor: "{colors.muted}"
    typography: "{typography.label}"
    padding: "0 13px"
    height: "52px"
  nav-link-active:
    textColor: "{colors.text}"
  tag:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.muted}"
    rounded: "{rounded.md}"
    padding: "1px 7px"
  list-table:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
  list-table-header:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.muted}"
    typography: "{typography.label-caps}"
    padding: "9px 14px"
  data-table-header:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.accent-hi}"
    padding: "5px 9px"
  dialog-card:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.xl}"
    padding: "18px 20px"
  toast:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
    padding: "10px 12px"
  flow-node:
    backgroundColor: "{colors.panel}"
    textColor: "{colors.text}"
    rounded: "{rounded.lg}"
  skeleton:
    backgroundColor: "{colors.panel-2}"
    rounded: "{rounded.xs}"
    height: "12px"
---

# Design System: Tabularia

## Overview

**Creative North Star: "The Quiet Instrument"**

Tabularia is a working tool that people keep open all day: a flow canvas, preview grids, run histories, catalogs. The interface is built like an instrument housing around the data. Surfaces are a stack of close, cool neutrals; chrome is small, sentence-case and low-contrast against itself; the only saturated things on a screen are the primary action, the operation-category colour of a node, and the state of a run. The product principle "the data is the subject; the interface recedes" is visible in the build as density (13.5px body, 12px data cells, 52px top bar, 6px control padding) rather than as emptiness.

The whole palette lives in CSS custom properties on `:root` and is swapped by a single `data-theme` attribute on `<html>`: dark (default, midnight blue), light (cool greys, not pure white pages), dracula and monokai (the editor palettes this audience already lives in). Components never know which theme is active; they name roles. Charts and the sign-in canvas, which cannot read CSS variables, resolve the same tokens at runtime and re-resolve when the theme changes.

Motion is functional and brief (0.1 to 0.22s state transitions, a skeleton shimmer, a dashed flow on data edges). There is exactly one authored motion moment in the product: the ordering field behind the sign-in card, where scattered, dirty numeric values settle into a right-aligned table. It is the product's purpose drawn in its own material (monospace numbers, table rules, theme tokens) and it is not a pattern to repeat elsewhere.

**Key Characteristics:**
- Four themes from one role vocabulary; dark is the default, selection persisted per browser.
- Dense by intent: 13.5px base, 12px data, controls at 6px vertical padding; density relaxes only under `pointer: coarse`.
- Tonal layering first (bg, bg-soft, panel, panel-2), 1px borders second, two shadow steps third.
- One gradient, reserved for the primary action.
- Colour as encoding: operation categories, run outcomes, engines. Never as decoration.
- IBM Plex Sans for the interface, the system monospace for anything that is data, code or an identifier.
- lucide icons at 11 to 18px, always beside a label or carrying an `aria-label`.
- Every visible string goes through i18n (five locales, English base).

## Colors

A cool, low-chroma neutral stack carrying one blue accent, one green for positive outcomes, one violet for a third category, and one red for danger; each theme re-values the same roles.

### Primary
- **Signal Blue** (`accent`): hover and focus borders, the active nav underline, selected rows and edges, links on hover, handles on the canvas, the default node accent.
- **Signal Blue, raised** (`accent-hi`): text that must read as accent on a panel: data-grid column headers, the active option in a Select.
- **Accent Gradient** (`--grad-accent`, 135deg from `accent` to `grad-accent-end`): the fill of `button.primary` and `.btn-link` only.
- **On Accent** (`on-accent`): text on the gradient. White on dark and light; dark on dracula and monokai, where the gradient is light.
- **Accent Tint** (`--tint-accent`, accent at 10 to 16% alpha): the fill of active tabs, active theme chips, role badges.

### Secondary
- **Outcome Green** (`accent-2`): success messages, OK status lines, the source-node accent, success toasts. `on-accent-2` is the text colour when green is used as a fill (light theme flips it to white).

### Tertiary
- **Violet** (`violet`): a third category where blue and green are taken (executions and refreshes in charts, null-handling operations).
- **Danger Red** (`danger`): error text, destructive buttons (outlined at rest, filled on hover), error toasts.

### Neutral
- **Page** (`bg`): the app background and the canvas.
- **Well** (`bg-soft`): the inside of inputs and Select triggers.
- **Panel** (`panel`): every raised surface: sidebars, tables, dialogs, menus, toasts, nodes.
- **Panel, second step** (`panel-2`): default buttons, table headers, row hover in list tables, tags, skeleton blocks.
- **Border** (`border`) and **Soft Border** (`border-soft`): panel outlines and header rules; cell rules and the 1px gaps of the editor grid.
- **Control Border** (`control-border`): the outline of anything you type or pick in. Separate from `border` on purpose: a field's fill differs from its panel by about 1.06:1, so the outline is what says "write here" and it holds 3:1 (WCAG 1.4.11) in every theme.
- **Text** (`text`) and **Muted** (`muted`): primary copy; labels, hints, table headers, inactive nav, placeholder.
- **Edge** (`edge`): data edges on the canvas at rest.
- Derived, theme-dependent alphas: `--scrim` (behind modals), `--topbar-bg` (88 to 92% panel, under an 8px blur), `--row-even` and `--row-hover` (data-grid zebra and hover), `--sheen` (node title highlight), `--shimmer` (skeleton sweep).

### Theme values

| role | dark (default) | light | dracula | monokai |
|---|---|---|---|---|
| bg | #0b0e14 | #f4f6fb | #1e1f29 | #1e1f1c |
| bg-soft | #0f131c | #ffffff | #21222c | #242520 |
| panel | #141926 | #ffffff | #282a36 | #272822 |
| panel-2 | #1b2130 | #eef1f7 | #343746 | #33342b |
| border | #262e40 | #d7dde9 | #44475a | #49483e |
| border-soft | #1e2534 | #e6eaf2 | #343746 | #33342b |
| control-border | #5c6884 | #7c8699 | #737898 | #7b7867 |
| text | #e8ebf2 | #1b2233 | #f8f8f2 | #f8f8f2 |
| muted | #8b93a7 | #5f6b81 | #9ca5d0 | #a6a28c |
| accent | #4f8cff | #3b6fe0 | #bd93f9 | #66d9ef |
| accent-hi | #7aa8ff | #2b57c4 | #d3b6fb | #8ee6f5 |
| grad-accent end | #7c6cff | #6a5cff | #ff79c6 | #a6e22e |
| on-accent | #fff | #fff | #21222c | #1e1f1c |
| accent-2 | #6ee7b7 | #0a7d56 | #50fa7b | #a6e22e |
| on-accent-2 | #0f1117 | #ffffff | #0f1117 | #0f1117 |
| violet | #a78bfa | #7c3aed | #ff79c6 | #ae81ff |
| danger | #ff6b6b | #e02424 | #ff5555 | #fb4a8b |
| edge | #3d4a66 | #b3bdd0 | #6272a4 | #75715e |

Several theme values were moved off the "official" palette to reach AA (light `accent-2`, dracula `muted`, monokai `danger`); the reasons are in comments beside each value in `main.css`. Keep that habit: a theme colour that fails contrast is changed, with the measurement written next to it.

### Operation-category encoding
Nodes, sidebar entries and lineage use a fixed category colour as a 3px left edge plus icon tint: column operations `op-columns`, row operations `op-rows`, null handling `op-nulls`, reshape and combine (and sources) `op-shape`, foreach `op-foreach`, outputs and sinks `op-output`, control nodes and S3 `op-control`. Sequence (orchestration) edges are `edge-sequence`, dashed and static, to separate them from animated data edges. The map lives in one place, `composables/useOpIcons.ts`.

### Named Rules
**The Role, Not Hex Rule.** Component CSS names a role (`var(--panel-2)`), never a value. A colour that must sit on the accent uses `--on-accent`, never `#fff`: on two of four themes the accent is light.

**The Control Border Rule.** Anything the user types or picks in is outlined with `--control-border`; separators, tables and cards use `--border`. The two are never interchanged.

**The One Gradient Rule.** `--grad-accent` fills the primary action of a view and nothing else. No gradient text, no gradient panels.

**The Colour Is Encoding Rule.** Saturated colour outside the accent means something: an operation category, a run outcome, an engine. If it encodes nothing, it is a neutral.

## Typography

**Display / Body Font:** IBM Plex Sans, self-hosted variable woff2 (`frontend/assets/fonts/ibm-plex-sans-latin-var.woff2`, weights 100 to 700, latin subset, `font-display: swap`, no CDN), falling back to `system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif`.
**Label/Mono Font:** the system monospace stack (`ui-monospace, SFMono-Regular, Menlo, monospace`). No mono webfont is shipped.

**Character:** One technical sans at small sizes, chosen because it is narrower than the geometric face it replaced and keeps tables compact. Hierarchy comes from weight and from `muted` versus `text`, not from size jumps; the whole working ramp sits between 10 and 19px.

### Hierarchy
- **Headline** (700, 19px): the single `<h1>` of a page, with an 18px lucide icon before it and an optional muted count after it (13px, 500).
- **Title** (bold, 16px): dialog headings (`<h3>` with a leading icon); the top-bar wordmark sits next to this step (16.5px, 700, 0.3px tracking).
- **Body** (400, 13.5px on `html`): everything not otherwise specified; 13px for nav links, node text, options, toasts.
- **Body dense** (400, 12px to 12.5px): data-grid cells, descriptions under a row title, hints, secondary buttons in toolbars and tabs.
- **Label** (500, 12px, `muted`): field labels above inputs; nav links use the same weight at 13px.
- **Label caps** (600, 11.5px, 0.05em, uppercase, `muted`): column headers of list tables. The same device at 10 to 10.5px (0.03 to 0.1em) marks tags, role badges and option-group headings in the Select.
- **Mono** (400, 11 to 12.5px): SQL and expression textareas (every `<textarea>` is mono by default), cron strings, IDs, paths, queue and run logs, column names in datasource dialogs.
- Emphasis weights in use: 500 (links, primary buttons, labels), 600 (node titles, table headers, badges), 700 (page headline, wordmark). The sign-in wordmark (26px, 600, -0.01em) is the only larger step in the product.

### Named Rules
**The Data Is Mono Rule.** If the user could paste it into a terminal or a query (SQL, expressions, identifiers, cron, logs), it is set in the mono stack. Interface copy never is.

**The Caps Are For Headers Rule.** Uppercase with tracking belongs to table column headers, tags and group headings, at 11.5px or below. Sentences, buttons, nav and page titles are sentence case.

## Layout

Two shells.

**App shell** (`components/AppShell.vue`): a sticky 52px top bar (`--topbar-bg` with `backdrop-filter: blur(8px)`, 1px soft bottom border, z-index 100) holding wordmark, the main nav, global search and a settings gear; below it a content column padded 22px 24px and capped at 1440px, or `.fluid` with no cap for the Viewer and lineage. At 760px and below the nav scrolls horizontally inside the bar (hidden scrollbar) and content padding drops to 16px 14px; there is no hamburger.

**Editor shell** (`.app` in `main.css`): a full-viewport CSS grid, columns `200px minmax(0, 1fr) 320px`, rows toolbar / canvas / data view (34vh), separated by 1px gaps that show `--border-soft`. The right panel is resizable by a 7px handle. `minmax(0, 1fr)` is load-bearing: wide previews must not push the panel off-screen.

**List pages** (`assets/listpage.css`, imported scoped by Flows, Datasources, Connections and siblings): a `.page-head` row (h1 left, search box and actions right, 14px gap, 16px below), then one `table.list`, then a right-aligned Pager 14px under it. Action bars do not wrap: protect the button label, never break the bar onto two lines.

Spacing is a loose 2px-grain rhythm rather than a token scale: 4 to 6px inside controls, 8 to 10px between sibling controls, 14 to 16px between blocks, 22 to 24px at the page edge. Table cells are 10px 14px in list tables and 5px 9px in data grids.

Breakpoints in use: 900px (the admin side nav goes horizontal, global search narrows), 760px (top bar), 720px (sign-in side labels hide; the ordering field narrows its columns). Touch is handled by input method, not width: under `pointer: coarse` the small close and mini buttons grow to 32px minimum; on desktop the density is untouched.

Scrollbars are thin (8px), thumb `--border`, transparent track, everywhere.

## Elevation & Depth

Hybrid, tonal first. Depth is mostly the neutral stack (`bg` under `panel` under `panel-2`) plus 1px borders. Shadows are two steps and mark things that float or respond.

### Shadow Vocabulary
- **`--shadow-1`** (`0 1px 2px rgba(0,0,0,0.4)`; light: `0 1px 2px rgba(20,30,60,0.08)`): buttons on hover, nodes at rest, canvas controls.
- **`--shadow-2`** (`0 4px 16px rgba(0,0,0,0.45)`; light: `0 6px 20px rgba(20,30,60,0.12)`; dracula and monokai 0.5 alpha): everything detached from the page: dialogs, menus, Select panels, toasts, nodes on hover.
- **Focus glow** (`0 0 0 3px` accent at 18% alpha, with the border turning `accent`): focused inputs and open Select triggers.
- **Selected node** (`0 0 0 1px` node accent plus a soft accent glow): the one selected node on the canvas.

Stacking order is fixed: top bar 100, settings menu 200 to 201, dialogs 2000, Select panels 2500 (teleported to `body`, so they sit above a dialog's backdrop), toasts 3000.

Modals sit on `--scrim` with a 2px backdrop blur; the top bar and editor toolbar use an 8px blur over a translucent panel colour. Those are the only two uses of blur.

### Named Rules
**The Flat At Rest Rule.** A surface in the page flow has a border and no shadow. `--shadow-1` answers hover; `--shadow-2` means "this floats above the page".

## Shapes

Softly rounded rectangles, stepped by the size of the thing: 4px for skeleton bars and scrollbar thumbs; 6px for options, tabs and small toolbar links; 8px for every control (buttons, inputs, Select triggers, search box, tags); 10px (`--radius`) for containers (list tables, nodes, menus, Select panels, toasts); 14px for the largest floating cards (dialogs, the sign-in card); 999px only for engine badges and similar pills. Borders are always 1px solid, with one exception: a flow node carries a 3px left edge in its category colour, which is the canvas's native way of saying what an operation is. Canvas handles are 11px accent dots with a 2px `bg` ring, centred on the node border, scaling to 1.35 on hover. The logo always sits on a white rounded tile (27px in the top bar, 20px in the editor toolbar, 40px on sign-in) so it survives every theme.

## Components

### Buttons
Small, quiet, tactile: the default button is a neutral chip, and the press is felt.
- **Shape:** 8px radius, 6px 12px padding, inline-flex with a 6px gap for a leading lucide icon; inherits the body font.
- **Default:** `panel-2` fill, `border` outline, `text` colour.
- **Primary (`.primary`, and `.btn-link` for anchors):** `--grad-accent` fill, transparent border, `--on-accent` text, weight 500. One per view or dialog footer.
- **Danger (`.danger`):** `danger` outline and text at rest; fills `danger` on hover.
- **Mini (`.mini`):** 3px 8px, 24px minimum height (the AA target floor), for row actions and pager arrows.
- **Hover / active / disabled:** border turns `accent` with `--shadow-1` (primary instead brightens 1.12 with an accent glow); `:active` moves 1px down; disabled is 45% opacity, no shadow, no press. Transitions 0.15s (transform 0.1s).
- **Focus:** `:focus-visible` only: `outline: 2px solid var(--accent); outline-offset: 2px`. Outline, not box-shadow, so it follows the theme and does not fight the hover shadow.
- **Busy:** the leading icon is replaced by a spinner (lucide `Loader` with `.spin`, 1s linear) and the label changes to the progressive form; the button is disabled.

### Inputs / Fields
- **Style:** `bg-soft` fill, 1px `control-border`, 8px radius, 6px 10px padding, full width, body font. Applies to text, email, password, number, date, time, datetime-local, `select`, `textarea`.
- **Focus:** border `accent` plus the 3px focus glow; no outline.
- **Textarea:** mono stack, vertical resize only.
- **Labels:** above the field, 12px, `muted` (500 where emphasised); hints below at 12px `muted`; errors in `danger`, with `role="alert"` when they appear after submit.
- **Autofill** is repainted to `bg-soft` and `text` so the browser's light autofill never breaks a dark theme; native pickers follow the theme through `color-scheme`.
- **Search box (list pages):** a `panel` pill with a 14px search icon and a borderless 220px input inside.

### Select (`components/ui/Select.vue`)
The product's own dropdown, used instead of native `<select>` wherever options are dynamic. The trigger is styled as an input (same fill, `control-border`, radius, focus glow when open) with a chevron that flips. The panel is teleported to `body`: `panel` fill, `border`, 10px radius, `--shadow-2`, 280px max height, 4px padding. Options are 13px on 6px-radius rows; the highlighted row is `panel-2`, the selected one is `accent-hi` with a check. A search box appears automatically above 8 options (or when `searchable` is set) and stays fixed while the list scrolls. Groups get a 10px uppercase heading. Contextual widths are set by modifier classes in `main.css`, not inline.

### Tables
- **List table (`table.list`):** `panel` fill inside a 1px `border` with 10px radius and clipped corners; header row `panel-2`, label-caps in `muted`; rows separated by `border-soft`, last row unruled; the whole row tints `panel-2` on hover. The first cell is a `.rowlink` (icon plus 500-weight name, `accent` on hover) with an optional 12px muted description under it; actions sit right-aligned as mini buttons.
- **Data grid (`table.data`, `DataGrid.vue`, Viewer):** 12px, every cell ruled with `border-soft`, 5px 9px padding, no wrapping; sticky header on `panel-2` with `accent-hi` text at 600; zebra `--row-even`, hover `--row-hover`; nulls are italic `muted`.
- **Pager:** right-aligned, 13px, mini chevron buttons, 14px above.

### Tags, badges, status lines
- **Tag:** 10px uppercase, 0.04em, `muted` on `panel-2` with a `border`, 8px radius, 1px 7px.
- **Engine badge:** 11px, 600, pill, tinted with its engine colour at 12% fill and 35% border.
- **Outcome lines (`.okline` / `.koline`):** 12px, icon plus text in `accent-2` or `danger`. Outcome is always icon and words, never colour alone.

### Navigation
Top-bar links are 13px, 500, `muted`, full bar height with a 15px-class lucide icon; hover turns `text`; the current section is `text` with a 2px `accent` underline on the bar's bottom edge (a matching transparent top border keeps the label centred). The settings gear opens a 230px `panel` menu (10px radius, `--shadow-2`) holding the user, role badge, theme chips, preferred engine, language and sign out; an invisible full-screen backdrop closes it. View tabs in the editor (`.viewtabs`) are small buttons with a 2px bottom border that turns `accent` over a `--tint-accent` fill when active.

### Dialogs
All modal dialogs share one skeleton: a fixed `--scrim` backdrop with a 2px blur at z-index 2000, and a `panel` card with 1px `border`, 14px radius, `--shadow-2`, 18px 20px padding, 380 to 520px wide by content, always `min(<width>, calc(100vw - 32px))`, scrolling internally past `100vh - 32px`. Header: an `<h3>` with icon on the left and an `X` mini button on the right; footer: actions right-aligned, 8px apart, primary last. Every dialog is `role="dialog"` and wires `useDialogA11y`: focus lands on the card (so the title is announced, and `[role="dialog"]:focus` draws no ring), Tab is trapped, Esc closes from anywhere, and focus returns to the opener.

### Toasts (`ToastHost.vue`)
Bottom-right stack (18px inset, 380px max, z-index 3000, `aria-live="polite"`): `panel` cards with 10px radius and `--shadow-2`, a 45 to 50% alpha border in the outcome colour, a coloured icon, 13px message, a bare close button. Enter from 8px below, leave 12px to the right, 0.22s ease. Click anywhere dismisses.

### Flow nodes and edges
A node is a `panel` card (min 160px, 10px radius, 1px `border`, 3px category left edge, `--shadow-1`, `--shadow-2` on hover) with a 600-weight title row (icon plus name, `--sheen` highlight, soft bottom rule) and a 13px body. Selection recolours the border to the node's category and adds the selected-node glow. Data edges are 1.6px `edge` strokes that turn `accent` on hover or selection and run a dashed flow when animated; sequence edges are violet, dashed and still. No `overflow: hidden` on nodes: handles must overhang the border.

### Loading: skeletons
Loading is shown with `.sk` bars (`panel-2`, 4px radius, 12px high, a `--shimmer` sweep every 1.1s), usually through `SkeletonRows` (rows of three bars at varied widths, `aria-busy="true"`). A skeleton stays up for at least 300ms (`skeletonPad` in `composables/useSkeleton.ts`), waiting only the difference so slow loads are not slowed further. The sweep stops under `prefers-reduced-motion: reduce`. Spinners are for in-button busy states, not for page loads.

### Charts
ECharts panels take their chrome (axes, labels, borders, tooltip surfaces, success / danger / accent / violet) from `composables/useChartTheme.ts`, which resolves the live tokens and re-resolves on theme change. Data series colours are encoding and stay in the component.

### Icons
`lucide-vue-next` only, stroke icons inheriting `currentColor`. Sizes by context: 18px in page headlines; 15 to 16px in primary buttons, nav and field affordances; 13 to 14px (the default) in buttons, rows, options and toasts; 11 to 12px in dense meta lines and tags. Icon-only buttons carry an `aria-label` and a `title`.

### Signature: the ordering field (`components/ui/OrderingField.vue`)
The one authored motion moment, used behind the sign-in card only. A single decorative `<canvas>` (`aria-hidden`) draws one ideal table drifting right at 13px/s: on the left, values are scattered, tilted, of mixed size and written dirty (`' 1.204,50'`, `n/a`, `3,1E2`); crossing the centre band they settle into right-aligned cells as their clean form, row rules fade in where there is order, and a few settled values light in `accent` or `accent-2`. Set in the mono stack, coloured from `--text`, `--accent`, `--accent-2` read at runtime (it follows all four themes and repaints on `data-theme` change). It renders at 30fps, caps device pixel ratio at 2, stops when the tab is hidden, leaves a measured quiet zone behind the card so nothing moves behind someone typing, and under `prefers-reduced-motion` shows the same picture, still. The card above it enters once (620ms, `cubic-bezier(0.16, 1, 0.3, 1)`, 10px rise) and the field fades in over 1100ms, both only under `prefers-reduced-motion: no-preference`.

## Do's and Don'ts

### Do:
- **Do** name roles: `var(--panel)`, `var(--control-border)`, `var(--on-accent)`. Check any new surface in all four themes; two of them have a light accent.
- **Do** outline every typeable or pickable control with `--control-border` and keep it at 3:1 against its surroundings.
- **Do** keep one `<h1>` per page, 19px / 700, with an 18px lucide icon before it and a muted count after it where the page is a list.
- **Do** use the shared list-page stylesheet (`~/assets/listpage.css`) for any new catalog-style page, with `SkeletonRows` while loading and `skeletonPad` so the skeleton shows for at least 300ms.
- **Do** use `components/ui/Select.vue` for option lists; let it add its own search above 8 options.
- **Do** build modal dialogs on the shared skeleton and `useDialogA11y` (focus on the card, Tab trapped, Esc closes, focus returned).
- **Do** give buttons a `:focus-visible` outline of `2px solid var(--accent)` at 2px offset, and give every icon-only button an `aria-label`.
- **Do** set SQL, expressions, identifiers, cron strings and logs in the mono stack.
- **Do** put every visible string through `$t`. English is the base locale and the fallback for `it`, `fr`, `de`, `es`. Each locale has a hand-written base catalog (`<locale>.ts`: nav, settings, sign-in) and a `<locale>.gen.ts` catalog produced by the i18n sweep, namespaced per component or page. In a new component, import `useI18n` explicitly.
- **Do** gate any new animation behind `prefers-reduced-motion` and leave a meaningful still state, as the skeleton and the ordering field do.
- **Do** resolve tokens at runtime (and re-resolve on `data-theme` change) for anything drawn outside CSS: canvas, ECharts.
- **Do** widen small targets under `@media (pointer: coarse)`, not under a width breakpoint.

### Don't:
- **Don't** write a hex or an `rgba()` of the dark accent in component CSS; it will be wrong in three themes.
- **Don't** put `#fff` on the accent; use `--on-accent`.
- **Don't** use `--grad-accent` for anything but the primary action.
- **Don't** use `--border` on a field or `--control-border` on a card.
- **Don't** add a shadow to a surface that sits in the page flow.
- **Don't** wrap a page's action bar onto a second line to fit a new control; protect the button label instead.
- **Don't** replace the scrolling top-bar nav with a hamburger as an "adaptation"; that is a redesign.
- **Don't** set `overflow: hidden` on a flow node; the handles overhang by design.
- **Don't** signal an outcome with colour alone; pair `accent-2` or `danger` with an icon and words.
- **Don't** load fonts or icons from a CDN at runtime; the font is one self-hosted woff2 and icons are lucide components.
- **Don't** reuse the ordering field, or add a second ambient animation, inside the working app. The product has one authored motion moment and it is before sign-in.
- **Don't** flash a skeleton: never show one for less than 300ms, and never use a full-page spinner where a skeleton fits.
