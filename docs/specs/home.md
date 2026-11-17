# Spec: the home page

Status: built (pieces 1 to 4 and the later widgets that read existing pages), 2026-11-17/18. Decisions taken by the owner on 2026-11-17.

## Objective

Tabularia opens on a **home page that each person arranges for themselves**: a grid of
widgets, moved and resized as the tiles of a dashboard, that answers «what needs me today»
without a click. A person who builds flows sees what failed, what runs next and what they
touched last; a manager sees the reports they read and a number or two; an administrator
sees the state of the installation.

It is a page of the person, not an object of a folder: it is not shared, has no versions
and no permissions of its own. A widget never shows more than its reader may already see
elsewhere in Tabularia.

## Decisions (owner, 2026-11-17)

| Question | Decision |
|---|---|
| Start page | The home page. Explore stays one click away, in the menu |
| Layout | Per person, kept on the server, on the dashboards' grid |
| First widgets | Clock, Recent, Favourites, Next runs, Failed runs, Flow durations, Shortcuts, Note |
| Later widgets | Datasource freshness, Resources, Data APIs' state, Reads of reports and dashboards, AI chat, and the proposals of «The catalogue» |
| Administration widgets | In the catalogue of administrators only |
| Accessibility | WCAG 2.1 AA, as the site: everything from the keyboard, including moving and resizing |
| Release | A new minor version on `main`. The stable line stays 1.4.x, long-term support, which takes fixes only and never this |

Proposed here, to confirm with the spec:

| Question | Proposal |
|---|---|
| Addresses | Home at `/`, Explore moves to `/explore`; old links to `/?project=…` are redirected |
| A first visit | A default layout; «Reset to default» brings it back. **The same on Docker Compose and on Kubernetes** (owner, 2026-11-17): the gateway holds it in one place, and it is made only of widgets both have. An administrator's differs by the shortcuts to the administration alone |
| An administrator's default for everyone | Not in the first version (see Open points) |
| On a phone | One column, in reading order, no editing |

Out of the first version: sharing a home page, several home pages per person, widgets with
custom code, a home page per group, widgets fed by external services.

## As built (2026-11-18)

Where the page differs from the plan below:

- **No «Home» item in the bar.** At 1440 px an administrator's bar fits to the pixel; the
  brand at the top left leads home, and `g` `h`.
- **Fifteen widgets**: clock, shortcuts, favourites, recent, flow_runs, runs_by_day,
  running_now, failed_runs, next_runs, datasources, notices, note, and for administrators
  queue, sessions, backups. «Flow durations» became **Flow runs**: bars, as tall as the run
  lasted and coloured by how it ended (owner, 2026-11-17: «like a scheduler's run bars»).
- **A line of four numbers under the greeting** (runs today, failed, running, due), the
  same for every layout.
- **Endpoints**: `/home/favourites` (GET, and PUT/DELETE `/{kind}/{id}`), `/home/next-runs`,
  `/home/flow-runs`; the other widgets read `/explore/recent`, `/runs`, `/runs/activity`,
  `/datasources`, `/banners`, `/queue`, `/audit/sessions`, `/admin/backups`.
- **Not built**: a number from a datasource, a dashboard's tile, shared with me, data
  contracts, reads of reports and dashboards, the AI chat, resources, data APIs' state, AI
  spend, cluster. Each needs an endpoint or a decision of its own.
- **On a phone** a widget is as tall as what it holds.
- WCAG checks inside the application are not a gate (owner, 2026-11-17); the keyboard and
  the announcements stay.

## How it reuses what exists

| Piece | Reused from | New |
|---|---|---|
| Grid, drag, resize, keyboard, breakpoints | `DashboardGrid.vue` and the dashboard designer's pointer code | A thin wrapper: widgets instead of tiles |
| Charts, KPI cards, themes | `ChartPanel`, `KpiCard`, `chartKinds.ts` | Nothing |
| Recent objects | `GET /explore/recent` | Nothing |
| Runs, failed runs, activity | `GET /runs`, `GET /runs/activity` | A filter «mine / all I can see» where missing |
| Schedules | `GET /schedule/load` and the objects' `next_run_at` | `GET /home/next-runs` |
| Memory badge, queue, workers, cluster, backups, sessions | `/system/memory`, `/queue`, `/admin/workers`, `/admin/cluster`, `/admin/backups`, `/audit/sessions` | Nothing: each widget calls the page's own endpoint, with the page's own rights |
| Search, object links, folder paths | `/search`, `useFolderPaths` | Nothing |

Dashboards must not change behaviour: the grid is used, not modified; where the wrapper
needs something the grid does not offer, the change to the grid is a commit of its own with
the dashboard tests green.

## Implementation

### The layout

One row per person: table `user_home` (`user_id` primary key, `definition` JSON,
`updated_at`). Migration `0043_user_home.py` (Alembic, in the Python gateway as every
migration); everything else in the Go gateway.

```json
{ "v": 1,
  "widgets": [
    { "id": "w1", "type": "recent",  "x": 0, "y": 0, "w": 6, "h": 6, "options": { "limit": 8 } },
    { "id": "w2", "type": "note",    "x": 6, "y": 0, "w": 6, "h": 4, "options": { "text": "…" } }
  ] }
```

- At most 30 widgets; `options` validated per type on the server, unknown types and keys
  refused; 64 KB for the whole definition.
- `GET /home` returns the person's layout, or the default when they have none (nothing is
  written until they change something). `PUT /home` replaces it. `POST /home/reset` deletes
  the row. All three for the signed-in person only: no id in the address.
- A widget of a type the person may not use (an administrator who is one no longer) is
  dropped from the answer, not an error.
- The answer also lists the types the person may add (`available`), so the catalogue is the
  server's, and says whether the layout is the default one (`is_default`).
- Widgets that exist on one kind of installation only (the cluster's, the data APIs') may be
  added by hand where they exist, and are never in the default layout.

### The page

- Reading mode by default; **Customise** turns on editing: drag, resize, a **+ Widget**
  catalogue, each widget's options and **Remove**, **Reset to default**, **Done**. Saved at
  each change, with undo and redo as in the dashboard designer.
- From the keyboard: a widget takes the focus; in editing, the arrows move it, Shift+arrows
  resize it, Delete removes it, Enter opens its options; every change is announced to
  screen readers. The same rule as the dashboard designer.
- Each widget is a named region with a heading, loads on its own, and fails on its own: one
  widget's error never blanks the page. Skeleton while loading; an empty state that says
  what would fill it.
- Refresh: each widget type has its own interval (below), paused while the tab is hidden.
- Five languages, five themes, reduced motion, as the rest of the interface.

### The catalogue

First version:

| Widget | Shows | Options | Refresh |
|---|---|---|---|
| **Clock** | Time and date, in the browser's zone or a chosen one; the schedules' zone beside it | Zone, 12/24 h, seconds | In the browser |
| **Recent** | The objects the person opened or changed last, any kind, with folder and time | How many (5–20), kinds | 60 s |
| **Favourites** | Objects the person pinned | Order by hand | On change |
| **Next runs** | The schedules due in the next 24 h among the objects the person can see | Mine / all, hours ahead | 60 s |
| **Failed runs** | The last failed runs, with the error's first line and «Open», «Run again» where allowed | Mine / all, days back | 60 s |
| **Flow durations** | For one to five flows, the duration of their last runs as a chart, with the median; with no flow chosen, the flows the person ran last | Flows, how many runs | 5 min |
| **Shortcuts** | Buttons to pages and tabs (Flows, Viewer, Reports…); for administrators also the sections of Administration. Kept as paths of the installation (`/flows`, `/admin?tab=users`), never an address of elsewhere; a path under an administration page is refused to, and hidden from, whoever is not an administrator | Which, in what order | Never |
| **Note** | The person's own text, with links | Text (Markdown subset, no HTML) | Never |

Later, each a piece of its own:

| Widget | Shows | For |
|---|---|---|
| Datasource freshness | One or more datasources: last refresh, rows, late or not against their schedule | everyone |
| Running now | Flows and scripts running, with their progress | everyone |
| Data contracts | Contracts broken or at risk | everyone |
| Reads of reports and dashboards | How many times, by whom allowed to know, over a period | owners of the objects, administrators |
| A number | One value from a datasource, as a dashboard's KPI tile | everyone |
| A dashboard's tile | An existing tile, shown on the home page with the dashboard's own rights and cache | everyone |
| Shared with me | Objects granted to the person lately | everyone |
| Notices | The administrator's banners | everyone |
| AI chat | A question box that opens the Assistant with the answer | where an AI model is configured |
| Resources | Memory and CPU of the engine and workers | administrators |
| Data APIs' state | Each data API: up or not, requests, errors | administrators |
| Queue and workers | Jobs waiting, workers alive | administrators |
| Sessions | Who is signed in, last sign-ins | administrators |
| AI spend | Today's cost against the daily ceiling | administrators |
| Backups | The last backup's outcome, the next one | administrators |
| Cluster | Pods, restarts, warning events | administrators, on Kubernetes |

### Favourites

Table `user_favourites` (`user_id`, `kind`, `object_id`, `position`, `created_at`), in the
same migration. **Add to home** and **Remove from home** in the actions menu of every object in
Explore and on the object's own page. A favourite the person can no longer see is not
listed; one whose object is deleted is removed.

### New endpoints (Go gateway)

| Endpoint | Answers |
|---|---|
| `GET / PUT /home`, `POST /home/reset` | The layout |
| `GET /home/favourites`, `PUT /home/favourites/{kind}/{id}`, `DELETE …` | The pins |
| `GET /home/next-runs?hours=&scope=` | Schedules due, across flows, groups, scripts, datasources |
| `GET /home/flow-durations?flow_id=…&runs=` | Durations of the last runs of up to five flows |

Every endpoint filters by the reader's rights in the query, as `/runs` and `/explore/recent`
do; none takes a user id. Failed runs and Recent use the existing endpoints.

### Moving Explore

`pages/index.vue` becomes `pages/explore.vue`; a new `pages/index.vue` is the home page.
The menu gains **Home** before Explore (and the logo goes home). Requests to `/` with
Explore's query (`project`, `q`) are redirected to `/explore` with it. Links inside the
interface and in the documentation are updated; the getting-started guide's «The
installation opens on Explore» is rewritten, with its screenshot.

## Commands

As in `docs/specs/dashboards.md`: the engine and gateway suites in the CI images, `vue-tsc`
and `npm test` for the interface, `go test ./internal/...` for the gateway,
`python3 .github/scripts/config_reference.py --check`, the site's `generate` and
`check-links`.

## Testing strategy

- **Gateway (Go)**: the layout's validation (types, options, sizes, limits); rights on each
  new endpoint with two users and an administrator; a widget type lost with a role; the
  migration up and down.
- **Interface**: unit tests of the layout's functions (default, breakpoints, undo); the five
  locales complete.
- **In the browser, on kind and on Compose**, Chrome and Firefox: build a home page from
  empty, move and resize by mouse and by keyboard, reload and find it as left, on a phone's
  width, as a non-administrator (no administration widgets, in the catalogue or by a
  hand-written request); axe with zero WCAG 2.1 AA violations in the five themes.
- **Regression**: the dashboard tests and the dashboard browser checks unchanged; Explore at
  its new address, with the old links redirected.

## Boundaries

- **Always**: rights checked on the server for every widget's data; every text in five
  languages; keyboard and screen reader for every action; a reduced-motion state; commits by
  piece with the suites green; docs in English, no competitor named.
- **Ask first**: a new dependency; any change to the dashboards' grid behaviour; a change to
  what an existing endpoint returns; publishing, tagging, releasing.
- **Never**: a user id, a SQL or an expression taken from the browser; HTML from a Note
  rendered as HTML; an administration widget's data served to a non-administrator; the 1.4
  line touched.

## Success criteria

1. A new installation opens on the home page with the default layout; Explore is one click
   away and every old link to it still works.
2. A person adds, moves, resizes and removes widgets by mouse and by keyboard alone, and
   finds the page as they left it on another browser.
3. The eight first widgets show true data for two different people with different rights,
   each seeing only their own.
4. A non-administrator cannot obtain an administration widget, in the catalogue or by a
   hand-written request.
5. axe reports no WCAG 2.1 AA violation on the page, reading and editing, in the five themes.
6. The dashboards and Explore behave as before.

## Pieces

1. Migration, `GET/PUT /home`, reset, validation, tests.
2. Explore moves to `/explore`; the empty home page with the grid, Customise, the catalogue,
   keyboard, undo; Clock, Note and Shortcuts (no data).
3. Recent, Failed runs, Next runs (`/home/next-runs`).
4. Favourites (table, endpoints, the actions menu) and Flow durations.
5. Default layouts, the phone layout, the five locales, documentation and the guide's
   screenshot, the browser checks; release 1.5.0 when the owner says.
6. The later widgets, in groups: everyone's first, then the administrators'.

## Open points, not blocking

- A default layout set by the administrator for the whole installation: useful to a company,
  and one more table and screen. Proposed for after the first version.
- Reads of reports and dashboards: who may see who read what is a privacy question
  ([governance](../governance/README.md)); the widget waits for that decision.
- The AI chat widget costs money per question on installations with a paid model: it stays
  off the default layout.
