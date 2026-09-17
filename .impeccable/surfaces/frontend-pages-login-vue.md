---
version: 1
slug: "frontend-pages-login-vue"
primary_target: "frontend/pages/login.vue"
related_targets: []
---

# Surface brief — sign-in (`frontend/pages/login.vue`)

Scope: the sign-in screen only. Visitor mode: Operate (the task is to get in), with one authored moment because outsiders from client companies see it first. Inherits the app's established world (tokens, themes, IBM Plex Sans); no new identity.

Audience and job: analysts who sign in daily, and client users meeting the product for the first time. Task: email + password, or SSO when the gateway exposes it. Constraints: all four themes, five locales, reduced motion, small screens, SSO error returned via `?sso_error=`.

Chosen direction (owner's answers, 2026-09-17): a field of numbers that gets put in order; the card centred on the field.

## Direction contract

THESIS: The screen shows what the product does instead of saying it — raw values become a table — and refuses the category default of a card on a decorative gradient, as well as the literal Matrix rain.

OWN-WORLD: The app's own tokens, nothing added. A full-viewport canvas of numeric values in the monospace stack, drawn in `--text` at low alpha with rare `--accent` / `--accent-2` values. Left: dirty values (mixed separators, stray spaces, n/a), scattered, tilted, uneven in size. Right: the same values cleaned, right-aligned in fixed columns on hairline row rules.

STORY: The visitor sees disorder resolve into a table across the width of the screen, understands "this tool cleans data", and signs in.

FIRST VIEWPORT: Canvas edge to edge. The sign-in card sits at the centre, exactly on the boundary where values snap to the grid, over a soft veil of `--bg` so nothing moves behind the inputs. Logo, product name, one line of positioning, the form, SSO below it. Bottom corners carry two small labels naming the two sides.

FORM: Single centred card over a full-bleed field; chosen directly by the owner, no concept-seed (precisely specified request).

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
