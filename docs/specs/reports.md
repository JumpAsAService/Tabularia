# Spec: pixel-perfect reports

Status: implemented, 2026-10-28. Pieces 1 and 2, the engine and the gateway, done (with
the fix that keeps a closed group from bringing back the header before it); 3, the
designer and the viewer, done; 4, the Pushmail cell, the API and `tabularia.report()` in
notebooks, done; 5, the new chart kinds in the Viewer and in the chat, done; 6, the
documentation, done: the user's guide is
[docs/capabilities/reports.md](../capabilities/reports.md), with an example report on the
Sample data. The bug hunt of the same day closed the live check of expressions
(`POST /reports/check`, called by the editor as one types) and the preview's problems tied
to their item and field. Still open:

- the preview's bar takes a parameter whose choices come from a dataset as free text: the
  choices are read from the saved report only;
- a saved view or a SQL script that a report reads can be deleted without a word: the
  report then fails, naming the dataset, and so does the email of a group that sends it;
- an optional parameter that has a default cannot be emptied: an empty field takes the
  default, so «no value» can be chosen only for a parameter without one;
- an interactive hotspot (a column to sort, a group to close) carries the box of its cell
  from the engine (`box`); the viewer still rebuilds a box from the table's layout for the
  pages of a render that has none.

## Objective

A person designs a **paginated, pixel-perfect report** in a modern editor in the browser —
a page of fixed size with bands, tables that group and subtotal over many pages, charts,
gauges and KPIs, texts with expressions — keeps it as an object of a folder, opens it on
screen with its parameters, downloads it as PDF or Excel, and has it **sent by email from
a flow group** right after the data it shows is refreshed. The same report can be
**downloaded through the API with its parameters**, from any script.

The idea is the classic banded report designer, made modern; nothing of another product's
file format, expression syntax or look is copied.

## Decisions (owner, 2026-10-29)

- **Data — datasets** of a report, each one of:
  - SQL on the catalog or on an existing connection (as SQL scripts: the same engine,
    read-only rules and permissions), with the report's parameters inside the query;
  - a datasource as it is, or a saved view (its filters, pivot and computed fields);
  - a saved SQL script (whoever changes the script changes the report).
- **Rights**: a report opened by hand reads with the rights of **whoever opens it**
  (they see only what they may read); a report sent from a flow group reads with the
  rights of **whoever scheduled the group** (as every schedule), checked at every send;
  an API download with the **token's** rights.
- **Formats**: **PDF** (the pixel-perfect one), **Excel** (the report's tables, groups and
  subtotals as rows), **HTML** in the body of an email (charts as images) next to the
  attachment.
- **Sending**: only through **flow groups**: the Pushmail cell can attach a report (format,
  parameter values), so it leaves right after the refresh steps before it. No schedule on
  the report itself.
- **API**: `GET /reports/{id}/render?format=pdf|xlsx|html&<parameter>=<value>…` with an API
  token (read is enough), the rights of the token's owner, audited; in marimo notebooks
  `tabularia.report(…)`. Python/R scripts running in Tabularia's pods cannot reach the
  gateway (network policy): there it needs an opening of its own, as for notebooks.
- **Report items, v1**: text boxes with expressions, images and logos, lines and
  rectangles, page header and footer; the **table/matrix/list** item complete (nested row
  and column groups, group headers and footers, subtotals and totals, page breaks per
  group, headers repeated on every page, keep-together rules, conditional formatting);
  **charts and KPIs** (bar, line, area, pie/donut, combo, waterfall, funnel, treemap,
  heatmap, scatter, gauge, KPI card with change and sparkline, bullet); **interactivity
  in the browser only** (sorting a column, expanding/collapsing groups, a link to another
  report with parameters). Not in v1: subreports, Word output.
- **Expressions**: a language of Tabularia's own, familiar to whoever knows spreadsheet
  formulas but not a copy of any product's: fields as `[Revenue]`, functions in capitals
  (`SUM`, `AVG`, `COUNT`, `IF`, `ROUND`, `FORMAT`, `CONCAT`…), the date placeholders
  Tabularia already has (`{today}`, `{today-7}`…), `PAGE()`, `PAGES()`, `PARAM("region")`;
  aggregates take the scope they sit in (group, dataset) unless told otherwise. Parsed and
  evaluated by the server with a grammar of its own — never `eval`, never code — with
  completion and checking in the editor.
- **Charts for the rest of Tabularia**: the new chart types (gauge, KPI, funnel, treemap,
  heatmap, waterfall, combo…) also reach the Viewer and the chat, through the shared chart
  component.

## Rendering (decided 2026-10-29 from a measured prototype)

The prototype (2,000 orders, regions and customers, credit notes, hostile names; kept in
`~/tabularia-carico/reports-proto/`, outside the repository) settled the engines:

- **PDF: Typst** (`typst` on PyPI, Apache-2.0, pinned): a fixed, versioned template; the
  data reaches it ONLY as JSON (`sys.inputs`), so names are text, never markup. 2,000
  rows → 40 pages in ~0.6 s; header repeated on every page, the group's subtitle repeated
  on continuation pages, page break per group, subtotals and totals, positions exact to a
  tenth of a millimetre, «Page X of Y», byte-identical output for the same data (golden
  tests possible). Memory is the risk: the compiler's cache grows ~130 MB per render in a
  live process, so **every render runs in a child process** of an engine worker, with a
  memory limit and a timeout (cancelling = killing it), and limits of rows (1,000,000) and
  pages (200) per render, which the default memory holds (some 4.5 MiB a page, measured).
  Keep-together of a group's last row with its subtotal is a template rule (Typst alone
  leaves widows). Fonts shipped complete (IBM Plex Sans TTF), with a check that reports
  substitutions. Numbers and dates formatted by the server.
- **Charts: ECharts in SVG, server side**, in a small Node service built from the
  frontend image (Node 22 and echarts 6.1.0 already there): a sidecar of the worker pod
  on 127.0.0.1 on Kubernetes (no Service, no NetworkPolicy change), an internal service on
  Compose. Four charts in 33–49 ms, ~85 MiB. Fixed layouts in the presets (ECharts 6's
  defaults cut a treemap), named formatters from an allowlist (no functions, no eval in
  options), a table of the PDF font's widths for text measurement (otherwise accented
  labels truncate wrongly), no external image URLs. If it is down, a render fails clearly.
- **Excel: xlsxwriter** (already a dependency): outline levels per group, SUBTOTAL formulas
  with cached values, red negatives, frozen panes, print titles, page breaks per group,
  strings never turned into formulas.
- **Email HTML**: tables with inline styles, charts as PNG @2x attached by `cid` (Typst
  renders them), only the summary in the body (Gmail clips past ~102 KB), the detail as
  the attached PDF; the attachments within the 15 MB of an email (base64 adds a third).
- **On screen**: the PDF's pages as SVG are exact but not interactive; the interactive
  view (sorting, expanding groups) is built from the same model and is close to the PDF,
  not identical, unless heights are explicit in mm — the editor works in mm.
- Rejected: ReportLab (178 s for 50,000 rows, no repeated group subtitles, charts through
  svglib lose masks and weights), Typst's experimental HTML export, a V8 inside Python.

## Implementation (settled 2026-10-28)

### The object

`reports` + `report_versions` in the gateway (migration 0032), as SQL scripts: a name
unique in its folder, a description, an owner, `current_version`; every save is a version
whose `content` is the report's definition (JSON, below), with a note. Permissions as every
object of a folder: VIEW opens it and renders it, EDIT changes, moves, renames, duplicates,
deletes, restores a version; what a render reads is checked dataset by dataset with the
reader's rights (a dataset the reader may not read stops the render with its name, as a
dataset that does not exist). Audit: `report.create|update|delete|move|rename|duplicate|
restore` and `report.render` (format, parameters, outcome, from the interface, the API or
a flow group). The Explore lists it with its own icon; search, move, duplicate, rename,
delete work as for the other objects. No lineage facet in v1 (a report writes no dataset).

### The definition (`v: 1`)

All lengths are millimetres (numbers, a tenth at most), colours `#rrggbb`.

```json
{
  "v": 1,
  "page": {"size": "A4", "orientation": "portrait", "margins": [15, 12, 15, 12]},
  "locale": "en",
  "timezone": "Europe/Rome",
  "theme": {"font": "IBM Plex Sans", "size": 9, "color": "#1f2933", "accent": "#3b5bdb"},
  "parameters": [
    {"name": "region", "label": "Region", "type": "text", "multi": false, "required": true,
     "default": "North", "choices": {"dataset": "regions", "value": "[code]", "label": "[name]"}},
    {"name": "since", "label": "From", "type": "date", "default": "{today-30}"}
  ],
  "datasets": [
    {"name": "orders",
     "source": {"kind": "sql", "connection_id": null, "engine": null,
                "sql": "SELECT region, customer, amount FROM sales WHERE region = {region} AND day >= {since}"},
     "fields": [{"name": "region", "type": "string"}, {"name": "amount", "type": "float"}],
     "calculated": [{"name": "Margin", "expr": "[amount] - [cost]"}]},
    {"name": "regions", "source": {"kind": "datasource", "datasource_id": 4, "filters": []}},
    {"name": "kpis", "source": {"kind": "view", "view_id": 7, "filters": [{"column": "region", "op": "=", "value": "{region}"}]}},
    {"name": "top", "source": {"kind": "sql_script", "script_id": 3}}
  ],
  "sections": {
    "page_header": {"height": 18, "on_first_page": true, "items": []},
    "body": {"items": []},
    "page_footer": {"height": 10, "on_last_page": true, "items": []}
  }
}
```

- `page.size`: A4, A3, A5, Letter, Legal, or `{"w": mm, "h": mm}`; `orientation`
  portrait | landscape; `margins` top, right, bottom, left. `locale` (en, it, de, es, fr)
  sets the separators of numbers, the names of months and days, the texts the renderer
  writes itself («Page 1 of 3», «Total»).
- **Parameters**: `type` text | number | date | boolean; `multi` = a list of values;
  `default` a value, a placeholder of Tabularia's language (`{today-30}`,
  `{start_of_month}`) or null; `required`; `choices`: fixed `{"values": [...]}`, or taken
  from a dataset (`dataset`, `value`, `label` expressions) — a choices dataset cannot use
  the parameter it fills. Names: letters, digits, `_`, not one of the placeholder
  builtins (`today`, `now`…). Values arrive as text (query string, form, flow group) and
  are converted and checked against the type and the choices before anything runs.
- **Datasets**: `source.kind`
  - `sql`: on the catalog (`connection_id` null; `engine` as SQL scripts', null = the
    default) or on a connection (CONNECT on its folder). Parameters are written `{name}`
    in the SQL — the same braces as Tabularia's placeholders, which work there too — and
    are replaced by the server, outside strings and comments, with a literal of the
    dialect (a string quoted and escaped, a number checked as a number, a date as
    `DATE 'yyyy-mm-dd'`, a list as `(a, b, c)` for `IN {regions}`; an empty list stops
    the render with a clear message). Never text pasted into SQL.
    Where: in the **gateway**, before it reads the SQL to find the datasources (its
    `sql_scripts.resolve`), so the engine receives final SQL. Tabularia's own placeholders
    (`{today-30}`, `{start_of_month}`…) are resolved through the engine's
    `/placeholders/preview` in the report's `timezone` (definition, default UTC): inside a
    string they become their text, as in a datasource's SQL (`'{today-30}'`), outside a
    typed literal (`DATE '…'`, `TIMESTAMP '…'`). A report parameter inside a string
    (`'{region}'`) is refused with a clear message («write {region} without quotes:
    Tabularia quotes it»): text pasted inside quotes is how injections start (a backslash
    escapes in MySQL). A doubled opening brace is a literal brace.
  - `datasource` / `view`: the datasource as it is, or a saved view (its filters, computed
    fields, pivot), plus `filters` (`column`, `op` among = <> < <= > >= in contains, a
    value or `{parameter}`) applied before.
  - `sql_script`: a saved SQL script's current version, on its own catalog or connection;
    its parameters are the report's (`{name}`).
  - `fields`: the columns the editor last saw (refreshed by «Refresh fields»), for
    completion and checking; the render uses what the data has. `calculated`: fields of
    the report's own, expressions per row.
  - At most `REPORTS__MAX_ROWS` rows per dataset (default 1,000,000; measured, a million rows take some 1.1 GiB and 27 s in the worker): above, the render
    stops and says which dataset and how many rows ("aggregate in SQL, or filter").
- **Sections**: page header and footer (fixed heights, on every page; `on_first_page`,
  `on_last_page` false hides them there) and the body. Body items are laid out in
  **rows**: items whose vertical ranges overlap form a row, placed at their x, y within
  it; a table, a matrix or a list grows with its data and takes its row alone (the editor
  prevents another item beside it), and what is below moves down with it. Headers and
  footers hold texts, images, lines, rectangles, and expressions on the page
  (`PAGE()`, `PAGES()`, `PARAM()`, `NOW()`, `REPORT()`), not dataset fields.

**Items** (`id`, `type`, `x`, `y`, `w`, `h`, `style`, optional `visible`: an expression):

- `text`: `value` is text, or an expression when it starts with `=`
  (`="Sales in " & PARAM("region")`); `style`: font size, weight, italic, colour,
  background, align, vertical align, padding, border, `format` (below), `can_grow`.
- `image`: `asset` = an image uploaded into the report (PNG, JPEG or SVG, 512 KiB at
  most, stored with the definition as base64); `fit` contain | cover | stretch. No URLs.
- `line` (from x, y to x+w, y+h), `rect` (its `style`: border, radius, background).
- `table`: `dataset`; `columns` [{`header`, `value` (expression), `width`, `align`,
  `format`, `total` (an aggregate expression or null), `style`, `rules`: [{`when`,
  `style`}] (conditional formatting)}]; `groups` [{`by` (expression), `label`
  (expression, default the value), `header`, `footer` (subtotals from the columns'
  `total`), `page_break` none | between | after, `sort` asc | desc, `keep_together`}];
  `detail` (show the rows, or only groups); `sort` [{`by`, `dir`}]; `filter` (expression);
  `repeat_header`; `grand_total`; `zebra`; `matrix` {`columns_by` (expression), `value`
  (aggregate), `total`} turns it into a matrix (one column per value, at most 60; its first
  column gives only a header, a width and a style, no `value`);
  `interactive`: {`sort`: column sorting on screen, `toggle`: groups collapsed/expanded
  on screen}.
- `chart`: `dataset`, `kind` bar | column | line | area | pie | donut | combo | waterfall |
  funnel | treemap | heatmap | scatter | gauge | bullet, `category` (expression),
  `series` [{`value` (aggregate), `name`, `as` bar | line (combo), `axis` left | right}],
  `split_by` (expression: one series per value), `options` {`stacked`, `labels`,
  `legend`, `sort`, `top`, `min`, `max`, `target`, `bands` (gauge, bullet), `format`}.
  `sort` none: the categories (and the series of `split_by`, and a KPI's sparkline) in
  the order they first appear in the rows, i.e. the dataset's `ORDER BY`; asc | desc: by
  the first series' value. `top` N keeps the N largest values of the first series, in the
  order chosen, the rest as «Other».
- `kpi`: `dataset`, `label`, `value` (aggregate), `compare` (aggregate, optional: the
  change in % with its arrow and colour), `format`, `sparkline` {`category`, `value`}.

### Expressions

A closed grammar of Tabularia's own, parsed by a recursive-descent parser and evaluated
by walking the tree — no `eval`, no code. Spreadsheet-like on purpose, a copy of nothing:

```
expr    := or ;  or := and ("OR" and)* ; and := not ("AND" not)* ; not := "NOT" not | cmp
cmp     := concat (("=" | "<>" | "<" | ">" | "<=" | ">=") concat)?
concat  := add ("&" add)* ;  add := mul (("+" | "-") mul)* ;  mul := unary (("*" | "/") unary)*
unary   := "-" unary | primary
primary := number | "text" | TRUE | FALSE | NULL | [field] | {placeholder} | NAME "(" args ")" | "(" expr ")"
```

`[field]` a column (`]]` for a `]` in the name); `{today-7}` a placeholder of Tabularia's
language as a date; functions in capitals, case-insensitive:

- aggregates over the scope they sit in — the group, the table, the dataset of the item
  — or of a dataset named as last argument (`SUM([amount], "orders")`): `SUM AVG MIN MAX
  COUNT COUNTDISTINCT FIRST LAST`; `PERCENT(x)` = x over the same aggregate one level
  up; `RUNNING(SUM([amount]))` running total within the group; `ROWNUMBER()`;
- logic `IF(c, a, b)`, `IFS(c1, a1, c2, a2, …, else)`, `ISNULL(x)`, `COALESCE(a, b…)`;
- maths `ROUND(x, n) ABS FLOOR CEILING POWER`; text `UPPER LOWER LEFT RIGHT MID LEN TRIM
  REPLACE CONTAINS STARTSWITH TEXT(x)`; dates `DATE(y, m, d) YEAR MONTH DAY WEEKDAY
  DATEADD("d"|"w"|"m"|"y", n, date) DATEDIFF(unit, a, b) TODAY() NOW()`;
- `FORMAT(x, "pattern")`; report `PAGE() PAGES() PARAM("name") REPORT()`.

Formats (`format` of a text, column, chart or KPI, and `FORMAT()`): numbers `0`, `0.00`,
`#,##0`, `#,##0.00`, `0%`, `0.0%`, with a currency or text before or after
(`€ #,##0.00`, `#,##0 pcs`), a third section for negatives in brackets
(`#,##0;(#,##0)`); dates `yyyy-MM-dd`, `dd/MM/yyyy`, `d MMM yyyy`, `MMMM yyyy`, `HH:mm`.
Separators and names follow the report's `locale`. Errors (a field that does not exist,
a function with the wrong arguments, a text added to a number) are found when the
definition is saved, with the item and the position, and at render time name the item.
NULL propagates through arithmetic; aggregates skip it; division by zero is NULL.

### Rendering (engine)

`POST /reports/render` (internal, gateway → engine): `{definition, datasets: {name:
resolved}, params, format: pdf | xlsx | html | svg, filename, owner, locale, state}` →
`{task_id}`; a Celery task on the export queue writes the result to `exports/` in the
store, owned by `owner`, followed and downloaded as the Viewer's exports
(`/tasks/exports/{id}`). `resolved` is what the gateway built after checking rights: a
catalog chain (as SQL scripts and views), or `{connection, sql}`. Steps: read each
dataset (row cap + 1, to say when it is over), calculated fields, evaluate the layout
into a **render model** (pages are Typst's job; the model has the rows, groups, totals
and formatted texts), charts to SVG through the chart service, then:

- pdf: Typst on a fixed template (`backend/app/reports/template.typ`), the model passed
  as JSON input, fonts shipped (IBM Plex Sans), compiled in a CHILD process with a
  memory limit (`REPORTS__RENDER_MEMORY_MB`, default 1024) and a timeout
  (`REPORTS__RENDER_SECONDS`, default 120), at most `REPORTS__MAX_PAGES` (default 200);
- svg: the same compile, one SVG per page (the on-screen view), with its hotspots: where
  a click sorts a column or opens a group, each with the `box` {x, y, w, h} of its cell in
  mm from the page's top left (a header's box as tall as its row);
- xlsx: xlsxwriter, one sheet per table/matrix (outline levels, SUBTOTAL formulas with
  cached values, formats, frozen header, print titles, page breaks per group), plus a
  sheet with the parameters; strings never become formulas;
- html (email body): the body's texts, KPIs and charts (PNG @2x rendered by Typst,
  attached by `cid`), every table and matrix as the totals of its outermost groups and
  its grand total (at most five columns of values; a table with nothing to total is left
  out, with the row of texts above it that introduces it), under Gmail's ~102 KB: fewer
  groups per table, then fewer tables, and what is cut is said; the same as plain text
  in the MHTML, for the email's text part.

`state` carries the on-screen interactivity (a column sort, groups toggled): the same
renderer, so the screen is the PDF. The chart service: `frontend` image, `node
/app/chart-renderer/render.mjs --serve 8090`; Compose service `chart-renderer`
(internal), Helm a sidecar of the worker pods on 127.0.0.1 (`REPORTS__CHART_URL`).

### Gateway API

- `GET|POST /projects/{id}/reports`, `GET|PATCH|DELETE /reports/{id}`, `/duplicate`,
  `/versions`, `/versions/{v}`, `/versions/{v}/restore` — as SQL scripts.
- `POST /reports/{id}/render` `{format, params, state}` → `{task_id, filename}` (the
  interface: follow and download as an export), and `POST /reports/preview` with an
  unsaved definition (the editor's preview: EDIT on the folder).
- `GET /reports/{id}/render?format=pdf|xlsx|html&<param>=<value>…` — the API: waits for
  the file (up to `REPORTS__API_WAIT_SECONDS`, default 120) and returns it with its
  `Content-Disposition`; a token with read scope is enough; repeated `?region=a&region=b`
  for a multi parameter. 400 with the parameter's name when a value does not fit.
- `GET /reports/{id}/parameters` — the parameters with their choices resolved (the form).
- `POST /reports/{id}/fields` `{dataset}` → the dataset's columns (editor).

### Pieces

1. **Engine**: expression language (parser, checker, evaluator, formats in 5 locales),
   dataset reading with the cap, render model (groups, totals, matrix, conditional
   formats, scopes), Typst template and child-process compile (pdf, svg), xlsx, html;
   the chart service (presets for every kind, font widths) with Compose and Helm. Tests:
   the language, golden PDFs (byte-identical), limits, hostile names.
2. **Gateway**: model and migration, CRUD and versions, permissions, Explore, audit,
   dataset resolution with the reader's rights and parameter substitution, render
   endpoints (interface, preview, API with tokens), parameters endpoint.
3. **Editor and viewer** (frontend): the report page — page in mm with rulers, bands,
   toolbox, drag and drop, snap, properties panel, datasets and parameters panels,
   expression editor with completion and checking, preview; the viewer — parameters
   bar, pages, interactivity, downloads.
4. **Sending and scripts**: the Pushmail cell of flow groups attaches a report (format,
   parameter values; the scheduler's rights); `tabularia.report()` in notebooks; the
   API documented with a Python example.
5. **Charts for the rest**: the new kinds in the Viewer's and the chat's chart.
6. **Docs, site, release**: a page in Capabilities with images and an example report on
   the Sample data, configuration reference, Helm values, the regression.
