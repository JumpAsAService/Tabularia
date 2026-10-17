# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Analysts and data teams who prepare data for reports and scheduled pipelines, working in the tool daily. The sign-in screen is also seen by people from client companies who are given access, so it is the first thing an outsider sees of the product. (Confirmed by the owner, 2026-09-17.)

## Product Purpose

Tabularia is a self-hosted data-preparation tool: connect a source, build a flow of transformation nodes on a canvas, preview every step, publish the result as a datasource or write it to a database, S3 or email, and schedule it. Success is a flow that turns messy input into a table someone can trust, without writing a pipeline by hand.

## Positioning

The same flow runs on four interchangeable engines (Polars, DuckDB, chDB, an external ClickHouse) with one verified SQL semantics across them, and can be exported as a dbt project — the visual flow is not a dead end.

## Operating Context

Runs behind a gateway (JWT auth, RBAC on users, groups and projects, optional OIDC single sign-on). Deployed with Docker Compose in development and Helm on Kubernetes in production. Users move between Explore (catalog), the flow editor, the read-only Viewer, Flows (runs, schedules) and an admin panel.

## Capabilities and Constraints

- Frontend: Nuxt 3, hand-written CSS with design tokens in `frontend/assets/main.css`; five themes (dark default, light, dracula, monokai, imperium) switched through `data-theme`; IBM Plex Sans self-hosted; lucide icons.
- Five locales (en base, it, fr, de, es); every visible string goes through i18n.
- No image-generation or asset pipeline: visuals are drawn in code.

## Brand Commitments

Name "Tabularia" and its mark, "order from noise": loose fragments (noise) before a blue flow, a clean 3×3 table grid (order) only after it, the flow cutting its own channel through the grid. Tagline "Messy in. Trusted out." The mark is vector (`frontend/components/ui/BrandMark.vue` in the app, `frontend/public/logo.svg` outside it, the standalone files in `brand/`): grid in the text colour, fragments in the muted colour, flow in the accent, a reduced variant (two fragments, a 2×2 grid) below 24px. Redrawn on 2026-10-18 from the approved brand board (`brand/board.html`, the visual reference; rules in `brand/BRAND.md`), replacing the 2026-09-17 grid-and-flow mark. Palette Flow `#4F8CFF`, Order `#E8EBF2`, Noise `#8B93A7`, Ink `#0B0E14`, Trusted `#6EE7B7`; IBM Plex Sans and Plex Mono. Sober, technical voice, never naming or comparing with competitors; Italian-first team, English as the base locale.

## Evidence on Hand

No testimonials, customer names, benchmarks for marketing use, or pricing exist. Future work must not invent them.

## Product Principles

1. The data is the subject; the interface recedes.
2. Every step is inspectable: nothing happens to the data that the user cannot preview.
3. Same meaning on every engine.
4. Fast enough to explore by clicking, not by waiting.

## Accessibility & Inclusion

Keyboard operability, visible focus and `prefers-reduced-motion` are honoured across the app (hardening pass of 2026-09-13).
