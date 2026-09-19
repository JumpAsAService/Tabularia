---
version: 1
slug: "frontend-pages-chat-vue"
primary_target: "frontend/pages/chat.vue"
related_targets: []
---

# Surface brief — Assistant (frontend/pages/chat.vue)

Scope and mode: Operate. A chat over the catalog, open to every signed-in user, inside the established Tabularia world (DESIGN.md unchanged).

Audience and job: analysts who already work in Explore, the editor and the Viewer, asking a question of data they can read and wanting a number they can trust. The assistant only sees datasources the user has VIEW on; field descriptions are its semantic context. Constraints: answers stream; every figure comes from a query whose result table is shown; conversations are stored server-side and private to their author; models are enabled by an administrator; five locales; four themes.

Unresolved: charts from results; flow-building assistance (next feature).

## Direction contract

THESIS: the assistant works inside the user's catalog, and the page says so: the datasources it can read are always on screen beside the conversation. It refuses the category default of an anonymous centered chat floating on an empty page, where nobody can tell what the model can see.

OWN-WORLD: the instrument housing of the rest of the product. Page ground, one panel rail, 1px borders, 13.5px body and 12px data cells, IBM Plex Sans, monospace only for SQL and identifiers, lucide icons. Evidence is drawn with the product's own data grid (accent-hi column headers, zebra rows, tabular numerals). The accent appears on the send action and the focused datasource, nowhere else.

STORY: the user sees what the assistant can reach, picks or names a datasource, asks in plain language, watches the steps it takes (catalog, fields, query), reads a short answer, and checks it against the table underneath. They believe the number because the query and its rows are there.

FIRST VIEWPORT: at 1440 wide, a 280px left rail titled with the count of readable datasources, each row with name, folder, rows and a marker when its fields are described; the conversation column (max 780px) takes the rest, empty state with three example questions as plain buttons; the composer is pinned to the bottom of the column with the send action at its right edge and the model and engine pickers under it at label size. Below 900px the rail collapses into a disclosure above the conversation.

FORM: catalog-anchored conversation (position 5 of 7 on the ordered list: notebook ledger, conversation plus evidence pane, centered chat, ask-bar report, catalog-anchored, step timeline, pinned-table board). Seed key edcb2ec4, dealt 5, 7, 3, lead built; evidence blocks borrowed from position 3. Code-led.

ADDED 2026-09-19 (refinement, world unchanged): conversations persist and reopen, so the rail's counterpart is a history disclosure under the page head, with the list-page search pill over it and a per-conversation cost. Each answer carries the cost of its turn at meta size; the head carries the conversation total. The three-dot placeholder became a status line: one pulsing accent dot plus a sentence naming the phase the assistant is actually in — catalog, fields, query, writing — read from the same stream events the steps are read from, never invented. The pulse is a loading indicator of the skeleton family, not a second authored motion moment; under prefers-reduced-motion the dot is still and the sentence still changes. Conversation titles are written by the model on the first turn, falling back to the truncated question; the naming call happens AFTER the turn closes, so the status line never outlives what it describes, and its cost is added to the turn. Finish review of 2026-09-19 applied: the page now defines the `.small` and `.mini` conventions it borrows from listpage.css instead of inheriting nothing, the open conversation is marked with `--tint-accent` and an inset accent edge (plus `aria-current`), rows no longer lift on hover, the per-step dots are gone so one loading indicator speaks at a time, and the cost is formatted through `Intl.NumberFormat` in the user's locale with an explicit floor instead of exponential notation.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
