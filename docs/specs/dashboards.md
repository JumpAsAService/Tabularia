# Spec: dashboards

Status: approved scope, not started. Decisions taken by the owner on 2026-10-31.

## Objective

A person builds a **dashboard**, a screen of live tiles (KPIs, charts, tables, matrices,
texts) on a grid, keeps it as an object of a folder, and opens it to read the state of
something at a glance: choose its parameters at the top, click a bar to filter the other
tiles, leave it on a wall screen refreshing itself. It can be downloaded as a PDF or an
image and sent by email from a flow group.

A report is for a document with a fixed page; a dashboard is for a screen that adapts and
answers clicks. They share what they can: **datasets and parameters are the same
definitions as a report's**, with the same rights and the same expression language.

Readers are analysts and the people they serve, including non-technical managers. A
manager should get the answer without reading documentation; an analyst who knows the
report designer should already know how to build a dashboard.

## Decisions (owner, 2026-10-31)

| Question | Decision |
|---|---|
| Datasets and parameters | As in reports, sure: same sources, same parameters, same behaviour |
| Layout | An adaptive 12-column grid, not free positions |
| Click on a chart filters the others | In the first version |
| Refresh | By hand, plus an optional interval per dashboard (one minute at least), with a cache shared by everyone |
| Licence | AGPL, as the rest |
| Tiles | KPI, every chart kind, table, matrix, text, image. No custom code |
| Going out | A link holding the filters, a PDF or an image, a scheduled send. **No CSV or Excel of a tile** |
| Where | A **Dashboards** item in the Analysis menu with the tab of the Reports (trending, search); created from Explore → New |
| AI | Not creating them yet, but prepared: reading and opening them from MCP yes |

Out of this version: custom tiles, drill-down to another dashboard, filters across
datasets by a click (a parameter does that), comments, alerts, a dashboard public without
signing in, CSV or Excel of a tile, creating dashboards from the chat or MCP.

## Depends on, in this order

1. **Parameters that filter their own dataset** (in progress): a list read from the dataset
   it filters. Without it, the commonest dashboard does not work.
2. **The new functions of the expression language**: an aggregate on a named group,
   `PREVIOUS`, `LOOKUP`, `SUMIF`, `COUNTIF`. Tiles use the same language.

## How it reuses the reports

| Piece | Reused from | New |
|---|---|---|
| Datasets, parameters, their choices, rights | `services/reports.py`, `report_definition.py`, `report_params.py` | Called with a dashboard's definition |
| Expressions, formats | `backend/app/reports/expressions.py`, `formats.py` | Nothing |
| Computing in columns, both ways | `frames.py`, `sql.py`, `model.py` | Output as data for the browser, per tile |
| Charts on screen | `chartKinds.ts`, `ChartPanel`, `KpiCard`, the app's five themes | A tile wrapper, clicks |
| PDF, image, email | Typst, the chart service, `render.py`, the Pushmail cell | A dashboard converted to a report's layout |
| Designer panels | `RdDatasetDialog`, `RdParameterDialog`, `RdExprInput`, `RdChartProps`, `RdKpiProps`, `RdTableProps` | The grid canvas |
| Object, versions, audit, Explore, search, trending | As reports (0032, 0033) | Its own tables |

Reports must not change behaviour: every report test stays green and the golden PDF stays
byte-identical. Shared code moves to a common place only where a third copy would appear;
a move is a commit of its own, with no change of behaviour.

## Implementation

### The object

`dashboards`, `dashboard_versions`, `dashboard_views`: the next free migration, additions
only, compatible with the previous version's code. Name unique in its folder, description,
owner, `current_version`; every save is a version.

- **VIEW** opens it; **EDIT** changes, moves, renames, duplicates, deletes, restores.
- What a tile reads is checked dataset by dataset with **the reader's** rights. A tile whose
  dataset the reader may not read says so inside that tile; the others work.
- Deleting a view, a SQL script, a datasource or a connection a dashboard reads answers 409
  and names it, as for reports.
- Audit: `dashboard.create|update|delete|move|rename|duplicate|restore`, and
  `dashboard.open` and `dashboard.render` (PDF, image, a send). A click and an automatic
  refresh are not audited, they would drown the log.
- Search, move, duplicate, rename and delete work as for the other objects.

### The definition (`v: 1`)

```json
{
  "v": 1,
  "locale": "en",
  "timezone": "Europe/Rome",
  "refresh_seconds": 300,
  "parameters": [
    {"name": "region", "label": "Region", "type": "text", "required": false,
     "choices": {"dataset": "sales", "value": "[area]"}}
  ],
  "datasets": [
    {"name": "sales",
     "source": {"kind": "datasource", "datasource_id": 18,
                "filters": [{"column": "area", "op": "=", "value": "{region}"}]},
     "fields": [{"name": "area", "type": "string"}, {"name": "ricavo", "type": "float"}],
     "calculated": []}
  ],
  "tiles": [
    {"id": "revenue", "type": "kpi", "dataset": "sales", "title": "Revenue",
     "grid": {"x": 0, "y": 0, "w": 3, "h": 3},
     "value": "SUM([ricavo])", "format": "#,##0"},
    {"id": "by_area", "type": "chart", "kind": "bar", "dataset": "sales", "title": "By area",
     "grid": {"x": 3, "y": 0, "w": 9, "h": 6},
     "category": "[area]", "series": [{"value": "SUM([ricavo])"}], "filters_others": true}
  ]
}
```

- `parameters` and `datasets` take exactly a report's schema, checked by the same code.
- A tile takes the properties of the report item of its type, without `x`, `y`, `w`, `h` in
  millimetres; `grid` replaces them. Types: `kpi`, `chart`, `table` (with `matrix`), `text`,
  `image`.
- `refresh_seconds`: `0` is by hand only; otherwise at least 60.
- `page`, optional: the page of its PDF and its image, `{"size", "orientation", "margins"}` as a
  report's. Without it, A4 across with margins of 10 mm. On a screen it says nothing.
- Limits, stated in the error: 40 tiles, 10 datasets, 20 parameters. An image as in reports.
- The definition is JSON checked on the server, as a report's: this is what lets the chat
  and MCP create one later without another format.

### The grid

- 12 columns; row height one unit of 40 px; a tile has a minimum per type.
- **Wide (≥ 1100 px)** as drawn. **Medium (700–1099 px)** on 6 columns, widths halved and
  rounded up, each tile laid in the order drawn and then taking what is free beside and under it,
  so that a row ends in no hole. **Narrow (< 700 px)** one column, in the order top to bottom and
  left to right, a chart at least 6 rows and a table at least 8. The widths are the window's; 10 px
  between tiles (`GRID_GAP_PX`); a tile hidden by its condition takes no place, and a band only
  hidden tiles held closes.
- In the designer: drag to move, handle to resize, snap to the grid, tiles push each other
  rather than overlap, undo and redo as one step, arrows and Delete with the same rule as
  the report designer. Written by hand on the designer's existing pointer code: **no new
  dependency**.
- A full-screen mode for a wall screen: no menu, the title, the time of the last update.

### Reading the data

`POST /dashboards/{id}/data`, on the preview queue:

```json
{"params": {"region": "Nord"},
 "clicks": [{"tile": "by_area", "values": ["Nord-Est"]},
            {"tile": "cross", "on": "columns", "values": [[2023, 7]]}],
 "tiles": ["revenue", "by_area"],
 "page": {"detail": 0},
 "refresh": false}
```

```json
{"tiles": {"revenue": {"value": 123.4, "text": "123", "change": null,
                       "computed_at": "…", "cached": true},
           "other": {"error": "…", "code": "no_access"}},
 "truncated": {"sales": false}}
```

- The answer is **numbers ready to draw**, one tile at a time, computed by the worker with the
  reports' engine. The browser draws them in its theme. An error stays in its tile.
- A dataset is read **once** per request, however many tiles use it.
- `POST /dashboards/preview` takes an unsaved definition: EDIT on the folder, no shared cache: it reads
  its rows every time, measured at 0.5 s for one tile on 1 million rows on the machine of the tests, against
  0.4 s from rows kept. The designer asks after a pause in the editing and only for the tile that changed; a
  tile's place and its title ask nothing. `tab` tells one designer from another of the same person: a newer
  request knocks down an older one of its own tab.
- A table returns one page at a time (50 rows); a sort or a closed group goes in the state, as
  in the viewer of reports; a chart keeps the limits of the report's.
- The row cap is `DASHBOARDS__MAX_ROWS`, by default `REPORTS__MAX_ROWS`. Past it, the tile says
  the data was cut and how to aggregate. **A total is never computed on cut data in silence.**

### The answer, tile by tile

`POST /dashboards/{id}/data` and `/dashboards/preview` answer `{"tiles": {id: …}, "truncated":
{dataset: bool}}`. `truncated` names only the datasets read: of one the reader may not read,
nothing, not even that it is large. A tile is either an error or its numbers.

**An error** is exactly `{"error": "a sentence", "code": …}`, with no number and no column of a
dataset refused or cut. The browser translates by `code`:

| `code` | Means |
|---|---|
| `no_access` | It reads a dataset, directly or through an expression or a calculated field, that the reader may not read |
| `truncated` | It reads a dataset past `DASHBOARDS__MAX_ROWS`: the sentence says how to aggregate. No number on part of the rows, not even the rows of a plain table |
| `dataset` | Its dataset cannot be resolved for another reason (no data yet, a parameter its SQL needs) |
| `error` | Anything else in this tile: a field that is gone, too many categories, a read that failed |

**Numbers** always have `type` (the tile's), `title`, `computed_at` (ISO, UTC), `cached` and,
when its `visible` says no, only `"hidden": true`. Texts are already written, in the dashboard's
locale; `null` is no value. Then, by type:

```json
{"type": "kpi", "label": "Revenue", "value": 123.4, "text": "123",
 "change": {"ratio": 0.25, "text": "+25.0%", "dir": "up"},
 "spark": {"cats": ["1", "2"], "values": [10.0, null]}}
```

`change` and `spark` are `null` when the KPI has none; with no value, `value` is `null` and `text`
a dash.

```json
{"type": "chart", "kind": "bar", "format": "#,##0", "empty": false,
 "options": {"stacked": false, "labels": false, "legend": null, "min": null, "max": null},
 "data": {}}
```

`empty`: nothing to draw. `data` holds what the kind's builder in
`frontend/composables/chartKinds.ts` takes, under its names; every kind with an axis has `cats` and
`series`, a series being `{"name", "values", "texts", "as", "axis", "format"}`, and `keys` and
`split_keys`, what a click sends back: the raw values of the categories (none for «Other», which
cannot be clicked, so `keys` can be one shorter than `cats`) and of the series of a `split_by`. A key
is JSON: a number, a text, `true`, `null` for an empty group, a date in ISO, and a text for an int
past 2^53, which a browser would change. A gauge has neither.

| `kind` | `data` |
|---|---|
| `column`, `bar`, `line`, `area` | `cats`, `series` |
| `combo` | `cats`, `series`, and for `comboOption`: `bars`, `line`, `barName`, `lineName` |
| `pie`, `donut` | `cats`, `series`, `name`, `slices` (`{"name", "value", "text", "at"}`, those above zero, `at` its place in `cats`), `left_out` |
| `treemap`, `funnel` | `cats`, `series`, `values`, `texts`, `name`; with `split_by`, `series` holds the parts |
| `waterfall` | those of a funnel, and `total`, `total_text`, `total_label` |
| `heatmap` | `cats`, `series`, `xCats`, `yCats`, `cells` (`[x, y, value]`), `texts` (the cells', in order), `name` |
| `bullet` | `cats`, `series`, `values`, `texts`, `targets`, `name`, `targetName` (empty: the interface's word), `max`, `bands` |
| `gauge` | `value`, `text`, `name`, `min`, `max`, `target`, `bands` |
| `scatter` | `series`: `{"name", "points": [[x, y, "text"]]}`, `split_keys` |

```json
{"type": "table", "matrix": false, "empty": false, "levels": 1,
 "columns": [{"header": "Order", "align": "left", "width": null, "style": {}}],
 "head": [[{"t": "Order", "span": 1, "rspan": 1, "align": "left"}]],
 "rows": [
   {"kind": "g", "level": 0, "label": "North", "key": ["North"], "closed": false, "path": ["North"]},
   {"kind": "d", "cells": [{"text": "1.00", "value": 1.0, "style": {"color": "#c92a2a"}}], "zebra": true},
   {"kind": "f", "level": 0, "label": "Total North", "span": 1, "cells": []},
   {"kind": "t", "level": 0, "label": "Total", "span": 1, "cells": []}],
 "page": 0, "pages": 3, "page_rows": 50, "total_rows": 121,
 "sorted": {"column": 1, "dir": "desc"},
 "hot": {"sort": true, "toggle": true, "still": [2]}}
```

A matrix is the same with `"matrix": true`: `head` holds its levels of columns, each cell its
number. `rows` are the kinds of a report's model: `g` a group's header, `d` a detail row, `f` a
group's footer, `t` the grand total; a footer's or total's `label` takes its first `span` columns,
its `cells` are all the columns. `page` is the one given, the last if past it; a group header
counts as a row of the page. `key` is what `state` names a group by (`null` when the table does
not offer closing them), `path` what a click names, at any level; a matrix's innermost group, a `d`
row, and a footer whose group has no header have a `path` too, and each header of a matrix's columns
its `key`, by its levels. `sorted` is what it sorts by; `hot`: what a click can do, as in the viewer of
reports, `still` the columns that offer no sort. `style` is a report's, as `model._style` writes it
(`color`, `fill`, `weight`, `italic`, `size`, `align`…).

The state goes in the request as in the viewer of reports:
`{"state": {"items": {"t": {"sort": {"column": 1, "dir": "desc"}, "collapsed": [["North"]],
"expanded": [], "collapse_all": false}}}}`, and `{"page": {"t": 2}}` the page, from 0.

```json
{"type": "text", "text": "Rows: 120"}
{"type": "image", "src": "data:image/png;base64,…", "fit": "contain"}
```

A tile a click was made on adds what is chosen and still there, as its keys are written, by side:
`"chosen": {"rows": ["Nord-Est"], "columns": [[2023, 7]]}`.

Limits, each said in the tile as an `error`: a chart keeps a report's (2,000 categories, 24
series), a scatter draws at most 10,000 points, and a page more than 100,000 rows down cannot be
reached by a column with `RUNNING` or `PREVIOUS`, which reads every row above it in memory.

The parameters' form: `GET /dashboards/{id}/parameters` (VIEW) and
`POST /dashboards/{id}/parameters` for a definition being edited (EDIT), as a report's.
`POST /dashboards/{id}/fields`: a dataset's columns and rows counted, for a definition being edited (EDIT).
`GET /dashboards/folders`: the folders where this person can make a dashboard, those with EDIT.

### The cache

The promise is: **a hundred people on one dashboard run one query**. On ClickHouse and BigQuery
each query costs money, so this is a requirement, not an optimization.

- Two levels: the dataset's rows (a file on the bucket, under the managed `cache/` prefix, written
  and read by the preview workers, never reached by a browser) and the tile's numbers (Valkey). A
  click recomputes the numbers from the rows and does not go back to the source. A parameter that
  leads to the same resolved source does the same. It lives in the engine (`backend/app/reports/
  tile_cache.py`), since Valkey is not the gateway's: the gateway sends only what `_plan` found the
  reader may read.
- Key: the dataset's resolved source after the parameters, for this reader (a catalog name is the
  parquet it resolved to), its data version where known (a datasource's snapshot, which is the
  parquet's key), the engine; for a tile also the tile's definition, the parameters, its page and
  state, the clicks, the locale, the timezone, the title `REPORT()` reads.
- **Rights come first and are never cached.** The check runs at every request with the reader's
  rights, and only then the key is used. Two people share a result only because the resolved
  source is identical.
- Lifetime `DASHBOARDS__CACHE_SECONDS`, by default 300, and at least the dashboard's interval. A
  number ends with the rows it comes from. `computed_at` is when the rows were read from their
  source; `cached` says this answer did not read it.
- Concurrent requests for one key wait for one computation (a lock in Valkey, renewed while its
  holder lives, ending in 30 s when it dies), with a timeout after which the waiter gets a clear
  error in the tile and not a second query. A failure is kept for 10 s for those waiting. Nothing
  cancels a shared computation: a saved dashboard's request has no slot.
- **Refresh** by hand ignores the cache, at most once per dashboard every 10 seconds, shared by
  everyone: past it, the answer is what there is, without an error. It reads again only what the
  reader may read.
- A parameter's list from a dataset (the check of a value, the form) is kept the same way. One the
  reader may not read checks nothing and stops nothing.
- Too large is not kept, and counted (`dataprep_dashboard_cache`): a tile past
  `DASHBOARDS__CACHE_TILE_BYTES`, anything past `DASHBOARDS__CACHE_VALKEY_BYTES` of Valkey's memory,
  rows past `DASHBOARDS__CACHE_BUCKET_BYTES`, the room of all of them on the bucket.
- Without Valkey or the bucket a dashboard works, without the cache. When the broker is that Valkey
  no worker can be reached: the numbers are worked out in the engine's API, one request at a time.
- The automatic refresh waits the interval and up to a tenth more, and pauses when the tab is
  hidden; on an error it keeps the last numbers with the time they are from, and says so. Kept
  numbers can outlive the interval (5 minutes against one), so a tick first takes what is shared,
  and only when that brings nothing newer than the screen has does it ask for the source: the first
  screen to find out reads, under the limit of 10 seconds, and the others find it shared at their
  tick. About one read per interval, however many people watch.

### A click filters the others

- A click on a bar, slice, point, heatmap cell, a row of a group or a matrix header filters the
  **other** tiles on the **same dataset**. Ctrl or Cmd adds a value. The tile clicked is not
  filtered: it keeps its bars and highlights the chosen ones.
- A strip under the parameters says what is active, `By area: Nord-Est ×`, with **Clear all**.
- `filters_others` on a tile turns it off. A KPI, a text and an image do not filter.
- **The browser sends a tile's id and values, never an expression.** The server takes the
  expression from the saved definition. A value is not written into anything: the rows are split by
  that expression as the tile itself splits them, and a value is only compared with the keys, on
  both ways of computing, after the dataset's rows and its calculated fields. Unknown tile, too many
  values (more than 100) or a tile that cannot filter: 422.
- **What a value is.** The raw one the tile gave (`keys`, `path`, `key`), never the text on the screen:
  a month 202307 written as a date, an amount with its thousands, `null` for an empty group, which can
  be clicked. A text from an address finds the number, the date or the boolean it writes. Several
  levels are a list: a group's path, a matrix's header, a heatmap's cell or a part of a split bar
  (`["Nord", 2023]`). A plain click on a value chooses it alone, and clears it if it was the only one;
  Ctrl or Cmd adds it or takes it away. In one tile values add up; between tiles, and between a matrix's
  rows and columns, each narrows the others.
- **What it reads.** A click filters what a tile reads as its own rows: its numbers, its condition, a
  KPI, a text. An aggregate that names the dataset (`SUM([x], "sales")`) still reads all of it, so that
  a share of the total stays one; so does a calculated field, worked out before the click.
- **What cannot be chosen.** «Other», a waterfall's total, a gauge, a detail row.
- A value is judged on the dataset's rows, not on what another click left: a value no longer there is
  dropped, and with none left the click counts for nothing. So a parameter re-reads the data and keeps the
  clicks whose values still exist, and `chosen` says what stayed; the browser follows it, in the address too.
- **A click on a tile the reader may not read counts for nothing**, whatever its values: its expression
  may read what they may not, and the answer would say which values exist there. It reaches no key of
  the cache and no worker: the answer is the one with no click, and the browser drops it. So for a tile
  whose dataset cannot be resolved (`dataset`).
- A filter that cannot be worked out (the tile clicked lost a field, or a dataset it needs cannot be read)
  is an `error` in the tiles it touches: never the numbers of all the rows under a strip that says otherwise.
- All of it is in the address: `?region=Nord&click.by_area=Nord-Est`, repeated for several values,
  `click.cross.columns` for a matrix's columns, `{none}` for an empty group, `\` before a text that would
  read as something else, and JSON for several levels. Back undoes a click. A link with the click of a
  tile that is gone, or now has `filters_others` off, opens without it; the API still answers 422.
- The numbers a click does not change stay kept: a tile on another dataset is not even asked, and the
  tile clicked is asked only what is chosen, keeping its bars while it waits.
- On a table that can filter, a group's name filters and the arrow beside it closes the group; on one
  that cannot, the name still closes it.
- From the keyboard: a group's name, a matrix's header, the strip's × and **Clear all** are buttons, Ctrl
  with Enter adds. A chart's canvas cannot be reached: a list of its categories shows when it takes the
  focus, and choosing there adds a value. A part of a split, a heatmap's cell and a scatter's point
  cannot be chosen from the keyboard, only their category where they have one.

### On the screen

- The tab **Analysis → Dashboards**: as the Reports tab, with search, most read first, a page
  at a time, and the same counting of people in 30 days.
- The viewer: the parameters' bar, as in reports, the strip of clicks, the grid; on each tile a
  skeleton on its first load, its own error with a retry; the time of the update, Refresh,
  Full screen, Download. It also opens through a link with its filters, after signing in.
- The designer: the three parts of the report designer (elements and data on the left, the
  grid, properties on the right), with a live view, since the data is on the canvas. It opens from Explore
  and from the tab (a new one, where the person has EDIT) and from the viewer's Edit; with VIEW only it
  leads to the viewer. Below 900 px it says it needs a larger screen.
  - Its panels are the report designer's, on its store: a dashboard goes in written as a report's, as the
    gateway writes it to check it, and comes out as it was.
  - Its tiles are the viewer's, on the same rows and gaps. The columns share the room between the panels. A
    click selects a tile and filters nothing; a hidden tile stays on the grid and says so.
  - A new tile opens with data on it, from the fields of its dataset. One half made says what it needs and is
    not asked of the server; one with a mistake says it inside the tile and at its field, and the others go on.
    A problem of the dashboard's own (a dataset, a parameter) is said above the grid and stops the asking.
- Five themes, five languages, keyboard, reduced motion, 1280 px and a phone. Motion only for
  state; the charts have no entrance animation at each refresh.

### Going out

- **PDF and image**: the server converts the definition to a report's layout
  (`backend/app/reports/dashboard_layout.py`) and uses the reports' template, with the parameters and the
  clicks of the moment. It is a faithful document of the numbers, not a screenshot: the look is the
  report's, its colours are not the theme on the screen.
  - The numbers are the screen's because nothing is worked out twice: the same reading (`tiles.Reading`),
    the same rows of a tile with its clicks (`tiles._Tiles`), on both ways of computing, and from the rows
    the screen reads (the cache), so a download asks no source that the screen did not.
  - The 12 columns fill the width of the page; a row is as high, for its width, as on a window of 1280 px.
    Tiles whose rows overlap stand together, in a row a page break never cuts; a row taller than a page is
    cut between tiles, and a tile taller than a page is made to fit it. A hidden tile takes no place.
  - A table takes its row alone and the whole width, and goes on over the pages, with its title in the head
    that comes back on each; what stood beside it moves over the hole. Its columns take the width by what
    they hold, and those that do not fit go on in another table with the first column again: a number is
    never written over the next one. It says the sort and the closed groups of the screen by what it shows,
    without the arrows.
  - On top, what it is showing: the name, the parameters that have a value, the clicks, when the rows are
    from. At the foot, the page's number.
  - A tile that cannot say its numbers says why in its place, in the document's language; of a dataset the
    reader may not read, that it cannot, as the viewer does: its title, and no name of a dataset, no column,
    no number. A click on such a tile counts for nothing, here too.
  - The image is the same document on one long page, PNG at 192 ppi. It is one page in memory: past
    `REPORTS__RENDER_MEMORY_MB` (at the default, a table of 1,000 rows fits and one of 2,000 does not) it fails saying so, and the PDF
    holds more. The limits are those of a report's render: `REPORTS__MAX_PAGES`, `REPORTS__RENDER_SECONDS`.
- **Link**: the address, for whoever has the rights.
- **Scheduled send**: the Pushmail cell of flow groups attaches a dashboard (PDF or PNG, up to 5,
  parameter values), with the same rules as a report: what makes one valid is `services/pushmail.py`,
  which `schedule_authority` asks before a scheduled group starts, with the author's rights, at every run.
  A change by someone else that reaches further (a dataset that reads something else, a SQL script or a
  saved view it reads, a new default of a parameter the cell gives no value) switches the schedule off;
  one a group sends cannot be deleted. Unlike the viewer's, nobody looks at it before it leaves: a dataset
  the author may not read, or a tile that cannot say its numbers, stops the cell with its name, and nothing
  is sent. It reads its source, with no cache: what the steps before it refreshed is what it says.
- `POST /dashboards/{id}/render` starts one for the viewer (followed and taken as a report's, through
  `/tasks/exports`, with the parameters, the clicks and the view of the screen).
  `GET /dashboards/{id}/render?format=pdf|png&<param>=<value>&click.<tile>=<value>` is the same with a token,
  as for reports: it waits for the file, and the clicks are written as in the viewer's address.
- Audit: `dashboard.render`, with its format, where it came from (`interface`, `api`, `flow_group`), its
  parameters and its clicks.

### AI and MCP

- Read tools: `search` finds dashboards; a new `get_dashboard` describes one (`reads_data=False`,
  declared as the registry demands), with its link. They show the structure, never rows.
- No write tool in this version. `POST /dashboards/check` validates a definition as for reports,
  so that adding them later is small.
- The metadata-only mode stays true: no tool here returns values of the data.

### Sample and documentation

- `infrastructure/sampledb/seed_tabularia.py` adds **Sales overview** to `Sample`: KPIs, a chart
  that filters the others, a table, a region parameter, so that the three commands end with a
  dashboard to click.
- `docs/capabilities/dashboards.md` with an example on the Sample data, images and a GIF of
  a click; Capabilities index, `docs/concepts.md`, `docs/governance/permissions.md`,
  `docs/api.md`, `docs/pushmail.md`, the MCP pages, the site's navigation, `CHANGELOG.md`.
- Settings in `docs/configuration.md` (generated), the panel where they are live, Helm values.

## Commands

```bash
# suites, in the CI images
.github/scripts/ci_tests.sh engine tabularia-backend:ci
.github/scripts/ci_tests.sh gateway tabularia-gateway:ci
# frontend
cd frontend && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
cd frontend && npm i --no-save @intlify/message-compiler@9.14.5 && npm test
# settings reference, site
python3 .github/scripts/config_reference.py --check
cd website && npm run generate && npm run check-links
# licences of an image
python3 .github/scripts/image_licence_check.py check --kind python tabularia-gateway:latest
# the stack for browser tests (:8088)
cd infrastructure/production && docker compose --env-file .env.prodtest build <services> \
  && docker compose --env-file .env.prodtest up -d --no-deps <services>
```

## Project structure

```text
gateway/app/models/dashboard.py              the tables
gateway/app/db/migrations/versions/          the migration
gateway/app/routes/dashboards.py             the API
gateway/app/services/dashboards.py           definition, rights, plan of a request
backend/app/reports/tiles.py                 numbers per tile, from the reports' model
backend/app/reports/tile_cache.py            the cache: rows on the bucket, numbers in Valkey, the lock
backend/app/reports/dashboard_layout.py      a dashboard as a report's layout, for PDF and PNG
frontend/pages/dashboards.vue                the tab
frontend/pages/dashboard.vue                 the viewer
frontend/pages/dashboard-design.vue          the designer
frontend/components/dashboard/               grid, tiles, strip of clicks
frontend/composables/useDashboards.ts        the API and the page's state
frontend/composables/dashboardClicks.ts      what a click chose, in the address and on a chart
gateway/tests/test_dashboards_*.py           gateway tests
backend/tests/test_dashboard_*.py            engine tests
frontend/tests/dashboards.test.mjs           grid and state, pure functions
docs/capabilities/dashboards.md              the user's guide
```

## Code style

As the code around it. Short comments that say why, not what; names in English that read as
sentences in tests; pure functions for the grid so they can be tested without a browser.

```python
def test_a_click_sends_a_tile_and_values_never_an_expression(session, client):
    """The expression comes from the saved definition: a viewer cannot make the engine run theirs."""
```

## Testing strategy

Every behaviour with its test, red first. Levels:

| Level | What |
|---|---|
| Engine | Numbers per tile on both ways of computing (`reports_both_ways`), against a calculation by hand; click filters with NULL, Unicode and many values; cut data never totalled in silence; a dashboard's PDF |
| Gateway | Rights tile by tile; **cache isolation**: A fills it, B without rights gets no data and no hint; 50 concurrent requests make one query; the 10-second limit on refresh; hostile clicks and parameters; schedules with the author's rights; the migration on real data |
| Frontend | Grid at each width, collisions, undo, the state in the address, typecheck, translations |
| Browser, Chrome and Firefox | A dashboard built by hand from an empty page with real mouse and keyboard; click, Ctrl-click, clear; parameter, link, refresh, full screen; five themes, four languages, 1280 px and a phone; **look at the screenshots** |
| Whole | From a clean clone with the three commands, Sample's dashboard works; Kubernetes on kind with 3 nodes; PDF and email in Mailpit |
| Load | 1 million rows measured for the targets below; on a real ClickHouse, the number of queries counted |

## Boundaries

- **Always**: rights checked with the reader at every request, before the cache; everything the
  interface says in five languages; a reduced-motion state; the licence check on images; no
  secret readable in any picture; commits by piece, with suites green on the whole; the docs in
  English and no competitor named, here or in the product.
- **Ask first**: a new dependency; changing how an existing report computes or looks; a price or
  a limit that blocks people on existing installations; publishing anything.
- **Never**: an expression, a SQL or a table name from the viewer's browser; a cache key that
  stands in for a rights check; a total on cut data in silence; pushing, tagging or releasing;
  touching the owner's reports and `Sample`'s existing objects other than adding the new sample.

## Success criteria

1. From an empty page, in a browser with the mouse, a person builds a dashboard with a parameter
   from its own dataset, a KPI, a chart and a table, without reading documentation or writing a
   filter or SQL.
2. A click on a bar changes the other tiles on the same dataset, not the tile clicked. Clear
   brings them back. The link reproduces the same state for someone else with the rights.
3. Someone without the rights on one dataset sees that tile refuse and the others work, with no
   number or column name leaking, cache full or empty.
4. **Numbers**: on Sample and on generated data every tile matches a calculation by hand and the
   same dataset in a report. Both ways of computing agree.
5. **Cost**: 100 people on one dashboard for the cache's lifetime make one query per dataset, on
   a real ClickHouse, counted.
6. **Speed**, on the machine of the tests with 1 million rows in the dataset: first open at most
   5 s with an empty cache, 1 s with a full one; a click at most 2 s; the browser stays responsive
   with 40 tiles. These targets are to be confirmed by the first measurement: if one proves wrong,
   say so with the numbers, do not lower it in silence.
7. Refresh by interval runs only with the tab visible and survives a failure, showing the last
   numbers and their time.
8. PDF and PNG show the same numbers as the screen with the same parameters and clicks. A flow
   group sends the PDF by email at a scheduled time, checked in Mailpit.
9. The three commands from a clean clone end with Sample's dashboard working. Kind works too.
10. All suites green, reports unchanged (golden PDF byte-identical), licence check at 0 refused.

## Pieces

Each ends with its tests, and is a commit; no piece is left half done.

1. **Object**: tables, migration, CRUD, versions, rights, Explore, search, audit.
2. **Numbers**: `tiles.py`, `/data`, `/preview`, `/check`, one reading per dataset, the cap.
3. **Cache**: both levels, lock, refresh limit, isolation tests, the count of queries.
4. **Viewer**: the tab, the grid at three widths, tiles in five themes, parameters, refresh, full
   screen.
5. **Clicks**: engine and API, the strip, the address.
6. **Designer**: canvas, panels reused, live view, undo.
7. **Going out**: `dashboard_layout.py`, PDF, PNG, the API, the Pushmail cell.
8. **MCP, sample, docs**: read tools, Sales overview, the guide, images, configuration, Helm.
9. **Verification**: load, clean clone, kind, the whole regression; a report of what is measured.

## Open points, not blocking

- A filter by click across datasets, with an explicit mapping between fields: after this version
  if people ask for it.
- CSV or Excel of a tile: excluded by choice; easy later.
- Row-level security does not exist in Tabularia, here as in reports: rights are on the dataset.
- Reducing what the tools send to the model (see the cost of a turn on Opus): separate work.
