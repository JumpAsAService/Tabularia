# Spec: XMLA endpoint

Status: decided with the owner on 2026-11-25: the client (spreadsheet pivot tables), the model
(generated), what is exposed (published datasources only), who tests with the real client (the
owner, on Windows), and where the aggregation runs (owner: «let us use DuckDB on the pod»).
Nothing built yet. Two defaults taken here are the owner's to change at the first trial: see
«Open questions».

## Objective

People who work in a spreadsheet want a **pivot table on a Tabularia datasource, live**: drag a
field, get the totals, without exporting a file that is stale the day after and too big to
open. Pivot tables speak XMLA (XML for Analysis: SOAP over HTTP, `Discover` and `Execute`) and
ask their questions in MDX.

An **XMLA endpoint** is an interface a data API can switch on: its resources become cubes, and
a pivot table connects to it with a key.

Who uses it: whoever publishes the data API (MANAGE) switches it on and hands out a key; an
analyst pastes the address and the key into the spreadsheet's «From Analysis Services»
connection; an administrator enables the feature for the installation and sets its caps.

Success: Excel on Windows connects with the address and a key, lists the cubes, and a pivot
with two fields on rows, one on columns, a filter and three measures answers in about a second
on a datasource of 20 million rows; a refresh of the datasource reaches the pivot at its next
refresh; a revoked key stops working within a minute.

## Decisions

| Topic | Decision |
|---|---|
| Client | **Spreadsheet pivot tables** (owner, 2026-11-25): MDX, in the subset a pivot table generates. No DAX, no calculated members written by hand, no named sets |
| Model | **Generated** (owner): one cube per resource, measures and dimensions worked out from the columns. Nobody writes a model |
| What is exposed | **Published datasources only** (owner): a datasource is a cube when it is a resource of a data API that has XMLA on |
| Testing | The owner tries everything with the real client on Windows; the automatic suite is made **as complete as it can be** (owner) so that little is left to find there |
| Shape | **A second interface of the data APIs**, not an object of its own. Same pods, same keys, same administration; an installation switch enables it and each data API turns it on for itself. No database migration |
| Where the aggregation runs | **In the pod, on DuckDB** (owner, 2026-11-25), on the local copy the pod already keeps of the datasource: the same copy and the same engine as the REST interface, asked to group and sum instead of returning rows. Computed at every request; nothing is precomputed or kept |
| Later | The installation's engine as a second backend, for a datasource a pod is too small for. Not in the first version: the MDX is translated to SQL through a dialect, so that it can be added without a second interpreter |
| Where | **Kubernetes only**, as the data APIs |
| Aggregations | The data APIs' spec keeps aggregations out of the REST interface («Not now»). XMLA is aggregation by nature: this spec is the owner's decision to have it, **in the XMLA interface only**. The REST interface does not change |
| Who calls | Systems and people **outside the cluster**, through the ingress, with a key of the data API |

## Where the aggregation runs

The owner's doubt (2026-11-25): «DuckDB may be too light for XMLA; ClickHouse and BigQuery made
sense, serving an aggregation made on the parquet».

What a pivot table needs is many small aggregations, one after another, each answered fast: a
drag of a field is several `Discover` requests and one or two `Execute`. That is a different
load from one large batch aggregation.

- **DuckDB in the pod.** It is a columnar engine built for exactly this kind of query, and the
  pod already holds the datasource as a local DuckDB file, sorted and with zone maps. The
  measured reference is the data APIs' own: 20 million rows in a container of 1 GB and 2 CPUs.
  Nothing travels over the network per query, and the people using the flows never feel the
  pivots. Its ceiling is the pod: one machine's CPUs and disk, and every pivot user of that
  data API shares them. An administrator raises the ceiling with the runtime (CPU, memory,
  replicas), as for the REST interface.
- **The installation's engine (an external ClickHouse).** It scales past one pod and is what
  most installations already run. Against it: every `Execute` reads the parquet from object
  storage instead of a local file, so a small aggregation is slower than in the pod; the pivots
  share the engine with the flows' runs; and the pods would need a way to the engine, which
  today they do not have (network policy, credentials, the «ai» style of dedicated user).
- **BigQuery.** Each query costs money by the bytes it scans and takes a second or more before
  the first row: a pivot that sends five queries per gesture is slow and has a bill. Not
  planned.

Decided: **DuckDB in the pod**. The engine stays a possible second backend for later, chosen by
an administrator per data API when a datasource is too large for a pod; the translator writes
SQL through a dialect interface from the first piece, so adding it is a dialect and a way to
run, not a second interpreter.

## The object

Nothing new in Explore. A data API's definition gains:

```json
{ "xmla": { "enabled": true } }
```

and each resource may carry a cube caption (its name by default). The definition is the JSON
the versions already hold: no migration. A data API with XMLA on answers both interfaces; the
REST one is unchanged.

Publishing a datasource to XMLA = adding it as a resource of a data API that has XMLA on. The
resource's chosen columns are the cube's columns; its filters are not parameters of the cube
(a pivot filters by itself).

## Access

- **The key is the password.** The client sends HTTP Basic; the user name is ignored (the
  key's name is suggested), the password is a data API key (`tdk_…`). `Authorization: Bearer`
  and `X-API-Key` keep working. A request with no key gets `401` with
  `WWW-Authenticate: Basic realm="<data API name>"`, which is what makes the client ask.
- A key limited to some resources sees those cubes only. CIDR limits, expiry, revocation and
  the per-minute rate apply as they do today. The default rate is checked against what one
  pivot gesture sends (a burst of `Discover`), and raised if it is too tight.
- HTTPS only: Basic over plain HTTP is refused on the public host.
- The authority is the creator's, checked again at every manifest, as for the REST interface.

## The interface

`POST /data/{slug}/xmla`, SOAP 1.1, `text/xml`.

**Discover**, the rowsets a pivot table asks for: `DISCOVER_PROPERTIES`,
`DISCOVER_DATASOURCES`, `DISCOVER_SCHEMA_ROWSETS`, `DISCOVER_ENUMERATORS`, `DISCOVER_KEYWORDS`,
`DISCOVER_LITERALS`, `DBSCHEMA_CATALOGS`, `MDSCHEMA_CUBES`, `MDSCHEMA_MEASUREGROUPS`,
`MDSCHEMA_MEASURES`, `MDSCHEMA_DIMENSIONS`, `MDSCHEMA_HIERARCHIES`, `MDSCHEMA_LEVELS`,
`MDSCHEMA_MEMBERS`, `MDSCHEMA_PROPERTIES`, `MDSCHEMA_SETS`, `MDSCHEMA_KPIS`,
`MDSCHEMA_MEASUREGROUP_DIMENSIONS`, `MDSCHEMA_FUNCTIONS`. Each with its restrictions; the ones
with nothing to say (sets, KPIs) answer an empty rowset of the right shape. A rowset not in the
list answers a SOAP fault that names it, and is logged by name.

**Execute**, an MDX `SELECT` answered as a multidimensional dataset (`Format=Multidimensional`,
`AxisFormat=TupleFormat`) with the cell and member properties the client asks for (`VALUE`,
`FORMATTED_VALUE`, `FORMAT_STRING`; `MEMBER_UNIQUE_NAME`, `MEMBER_CAPTION`, `LEVEL_NUMBER`,
`PARENT_UNIQUE_NAME`, `CHILDREN_CARDINALITY`). Also the session statements a client sends
around its queries (`BeginSession`, `EndSession`, an empty `Execute`).

One catalog per data API (its name), one cube per resource.

## The model

Worked out from the resource's columns and types, the same way every time:

| Column | Becomes |
|---|---|
| Any | A row count measure of the cube, `Rows` |
| Decimal, float | Measures: `Sum` (the default), `Average`, `Min`, `Max` |
| Integer | The same measures, **and** a dimension (an integer is as often a code or a year as an amount) |
| Text, boolean | A dimension with one level |
| Date, timestamp | A dimension with a hierarchy Year → Quarter → Month → Day, and the plain value as a second hierarchy |

Every hierarchy has an `All` member. A null is a member of its own, captioned as the app shows
a null. The column's description, where the datasource has one, is the caption's description.
Measure and dimension names are the column names: a pivot built today must open tomorrow, so
they change only when the column does.

## The MDX that is understood

The subset a pivot table writes, and nothing claimed beyond it:

- `SELECT … ON COLUMNS, … ON ROWS [, … ON PAGES] FROM [cube] WHERE (…)`, `NON EMPTY`,
  `DIMENSION PROPERTIES`, `CELL PROPERTIES`;
- a subselect in `FROM` (how a pivot filter arrives);
- sets and tuples: `{…}`, `(…)`, `CrossJoin` and `*`, `Hierarchize`, `DrilldownLevel`,
  `DrilldownMember`, `AddCalculatedMembers` (answers its argument), `Except`, `Distinct`,
  `Head`, `TopCount` and `BottomCount`, `Filter` with a comparison on a measure, `Order`;
- members: `.Members`, `.AllMembers`, `.Children`, `.[All]`, `.DefaultMember`, a member by its
  unique name, `[Measures].[…]`.

A statement outside the subset answers a SOAP fault that says which construct was not
understood. **With a switch an administrator turns on for one data API**, the MDX of a refused
statement is kept for a day for whoever manages it to read and send: that is how the owner's
trials on Windows become test cases. Off by default, because an MDX statement carries member
names, which are values of the data.

Parser written by hand (recursive descent): the subset is small, and its errors must name the
construct.

## Execution

1. The MDX is parsed and resolved against the cube into a plan: the hierarchies on each axis
   with their levels and member filters, the measures, the slicer.
2. The plan becomes SQL through a **dialect** (DuckDB first): one aggregation with grouping
   sets for the totals of every expanded level, so a pivot's subtotals are one pass.
3. The result is written into a temporary table of the pod's DuckDB, on disk, then read back in
   axis order and streamed as XML. **Never held whole in memory**: the axes and the cells are
   two ordered reads of that table.

Limits, all the administrator's caps: cells per answer, tuples per axis, members per `Discover`
answer (a level with millions of members is paged, not sent), seconds per statement (the pod's
query timeout), statements at once (the pod's existing turn-taking). Past a cap the answer is a
SOAP fault that says which cap and its value.

## Administration and enabling

- Installation: `dataApi.xmla.enabled: false` in the Helm values → `DATA_API__XMLA_ENABLED`,
  read by the Go gateway; it needs the data APIs on. Off, the switch is not offered and a data
  API saved with XMLA on is refused (409), as the data APIs' own switch does.
- Caps in the data APIs' administration: cells, tuples, members, and whether refused statements
  may be kept.
- `GET /data-apis/limits` says whether XMLA is offered, so the page shows the switch or not.

## The page

On a data API's page: a switch «Answer pivot tables (XMLA)» in the definition, the cube caption
beside each resource, and in the Documentation tab the steps to connect a spreadsheet (the
address, the catalog, the key as the password), with the cubes' measures and dimensions listed
as the client will show them. Five languages.

## Where the code goes

- `gateway-go/dataapi/internal/xmla/`: SOAP envelope, `Discover` rowsets, the model from a
  resource's columns, the MDX parser and plan, the dialects, the dataset writer. Pure Go.
- `gateway-go/dataapi/internal/pod/`: the route, Basic in `KeyOf`, the temporary table, the caps.
- `gateway-go/internal/dataapis/` and `internal/dataapiwire/`: the `xmla` field of the
  definition and of the manifest, the caps, the check at save.
- `frontend/pages/data-api.vue`, `frontend/components/dataapi/`: the switch and the docs.
- Docs: `docs/data-apis.md`, `docs/configuration.md`, the Helm README.

## Pieces

1. **Wire and switch**: the definition's `xmla` field, the installation switch, the route that
   answers `DISCOVER_PROPERTIES` and a fault for everything else, Basic authentication.
2. **Discover**: the model from the columns, every rowset of the list, members paged.
3. **MDX**: parser, plan, the errors that name a construct.
4. **Execute on DuckDB**: the DuckDB dialect, grouping sets, the temporary table, the dataset
   writer, the caps.
5. **The page and the docs**: switch, captions, the connection steps, the five languages.

Later, and only if a datasource proves too large for a pod: the engine as a second backend (its
dialect, the pods' way to the engine, the administrator's choice per data API).

## As built

**Piece 1** (wire and switch), 2026-11-25:

- The definition's `xmla` and the resources' `cube` live in the JSON of the versions. The current
  definition is read from rows that have no column for them, so it takes those two from the
  current version's content: no migration, and a version saved before them reads as off.
- With XMLA on, two cubes of the same name (case apart) are refused at save. `xmla` is a reserved
  resource name, as `health` and `openapi`. A copy of a data API is made with XMLA off.
- `DATA_API__XMLA_ENABLED`, `DATA_API__XMLA_MAX_CELLS` (1,000,000), `_MAX_TUPLES` (10,000),
  `_MAX_MEMBERS` (5,000), `_KEEP_REFUSED` (false); Helm `dataApi.xmla.*`. Keeping refused
  statements is a switch of the installation, not of one data API as first written above.
- The pod answers `404 xmla_off` only **after** the key is checked, so a stranger cannot tell
  whether a data API has the interface on. Other methods get `405` with `Allow: POST`.
- Basic is accepted on every route, and refused (`403 https_required`) when the request did not
  come over HTTPS as a trusted proxy reports it. A call in plain HTTP inside the cluster uses
  `Authorization: Bearer` or `X-API-Key`.
- `DISCOVER_PROPERTIES` is answered; every other request gets a fault that names what is not
  served yet. A request body is 1 MB at most.

**Piece 2** (Discover), 2026-11-25. The model is `gateway-go/internal/xmlamodel` (pure, shared by the
pod and, later, the gateway's documentation); the rowsets are `gateway-go/dataapi/internal/xmla`
(`discover.go`, `structure.go`, `members.go`); the members are read by `pod/xmla_data.go`.

- **Unique names**, every name between brackets with `]` doubled: measure `[Measures].[amount]` (the
  sum, named as the column; `[Measures].[amount (Average)]`, `(Min)`, `(Max)`; `[Measures].[Rows]`),
  all four of a column in the display folder `amount`; dimension `[country]`; hierarchy
  `[country].[country]`; level `[country].[country].[country]` and `[country].[country].[(All)]` for
  level 0; All member `[country].[country].[All]`; null member `[country].[country].[(null)]`; a member
  `[country].[country].[country].&[IT]`. A date has the hierarchy `[day].[Year-Quarter-Month-Day]`
  (levels Year, Quarter, Month, Day; keys `2026`, `2026-Q1`, `2026-01`, `2026-01-15`) and the plain
  value as `[day].[day]` (key `2026-01-15`; a timestamp `2026-01-15 08:30:00`, the fraction only when
  there is one, a zone read in UTC). The null of a date is the member `…[(null)]` of the first level.
  The measures are the members of `[Measures]`, level `[Measures].[MeasuresLevel]`.
- **Keys**: the value as it is, except that the empty text is `\` (a bracket cannot be empty in MDX) and
  a text starting with `\` gets another one in front. An integer key is canonical (`+042` reads as
  `42`); a boolean is `true` or `false`. Names match without regard to case, keys do not.
- **Collisions** (names are case-insensitive): in column order the first gets the plain name, the
  next `name (2)`. The sums' names (the columns') are taken before the Average, Min and Max ones,
  and `Rows`, `Measures`, `All`, `(All)` and `(null)` are reserved: a column called so is renamed,
  never the other way.
- **Types** (`KindOfDuckDB`): all integers (`HUGEINT` too) are a dimension and the four measures;
  `DECIMAL`, `FLOAT`, `DOUBLE` the measures only; `VARCHAR`, `JSON`, enums, `BOOLEAN`, `UUID` and
  `TIME` a one-level dimension (the last three by their text); `DATE`, `TIMESTAMP` (any unit) and
  `TIMESTAMP WITH TIME ZONE` (in UTC) the calendar. Lists, structs, maps, blobs and intervals are left
  out of the cube (nothing sensible to group on or sum): the model lists them in `Cube.Skipped`.
- A sum of a decimal is a decimal, of an integer an integer, an average a float; the format strings
  (`#,0`, `#,0.00`, the decimal's scale) are the model's, for `DEFAULT_FORMAT_STRING` and Execute's
  `FORMAT_STRING`.
- **Rowsets served**: every one of the list in «The interface», plus `DISCOVER_PROPERTIES` of piece 1.
  Empty, with the columns of the standard: `MDSCHEMA_SETS`, `MDSCHEMA_KPIS` and
  `MDSCHEMA_PROPERTIES` (no member properties beyond the intrinsic ones). `MDSCHEMA_FUNCTIONS` lists
  the functions the parser reads. `HIERARCHY_CARDINALITY`, `LEVEL_CARDINALITY` and
  `DIMENSION_CARDINALITY` of a data dimension are left out (a client may not rely on it): counting
  them is a scan of the table for each level at every Discover.
- **Members** are read from the pod's table, in order (a null last), each with the count of its
  children (`CHILDREN_CARDINALITY`), through the same turns and timeout as the REST interface. A
  `MEMBER_UNIQUE_NAME` with `TREE_OP` (children 1, siblings 2, parent 4, self 8, descendants 16,
  ancestors 32, as a sum; self when absent) gives the relations, each member once, a member the data
  does not have giving nothing. Past `MaxMembers` the answer is a fault that names the cap and its
  value, before any byte of the answer is sent: the answer is held (at most the cap's rows) until it
  is whole, so a timeout is a clean fault too.
- A key limited to some resources sees only those cubes, in every rowset. A resource whose table is
  still loading makes the rowsets that list cubes answer a fault («still loading»), not a short list;
  a resource with no data or whose publisher lost the right to read it is left out.
- An unknown rowset is a fault that names it (cut at 64 characters) and a log line with its name only.
- The conversation of connecting and listing the fields is `pod/testdata/xmla/connect.txt`: written from
  the documentation, **not recorded from a client**; its format (request, answer expected) is at its
  head, ready for the owner's recordings.

**Piece 3** (the MDX parser), 2026-11-25: `gateway-go/dataapi/internal/xmla/mdx`. A statement is
1 MiB at most, 64 levels deep, 128 axes. An empty argument is allowed between two others, never
first or last. `Filter` compares a measure with a number, joined by AND and OR. `a * b * c` and
`CrossJoin(a, b, c)` are one form in the tree. Its long examples are written from memory of what
a pivot sends: the first trials with the real client replace them.

## Testing strategy

- **Recorded conversations.** The requests a pivot table sends (connecting, listing, dragging a
  field, filtering, expanding, refreshing) kept as fixtures, each with the answer expected. The
  first set is written from the protocol's documentation; every trial of the owner on Windows
  adds the real ones, through the kept refused statements.
- **Parser**: a table of statements that must parse into a given plan, and of statements that
  must be refused naming the construct.
- **Numbers**: every `Execute` fixture is checked against the same aggregation written by hand
  in SQL on the same small table (small tables only, as the project's rule): totals, subtotals
  of every level, non-empty, nulls, a filter on each axis and in the slicer.
- **Rowsets**: each `Discover` answer validated for its columns and types.
- **End to end**: the standalone script of the data APIs, with a client that plays a recorded
  conversation against a real pod.
- **Caps and faults**: each cap reached, an unknown rowset, a refused statement, a wrong key, a
  key without the cube.
- What cannot be automated: the real client. The owner's checklist on Windows goes in
  `docs/data-apis.md` with the piece that ships it.

## Boundaries

- **Always**: the key checked in the pod; the creator's authority at every manifest; no value
  of the data in a log unless the administrator's switch is on; every cap a fault that names it.
- **Ask first**: any MDX beyond the subset; a model that somebody edits; DAX; the engine backend; the licence terms of the protocol's specification, which are quoted and not
  interpreted.
- **Never**: a query from the pods to the source databases; a result held whole in memory;
  aggregations in the REST interface; an answer that guesses at a statement it did not
  understand.

## Open questions

1. Integers are dimensions as well as measures by default, as in «The model». The other way
   (dimensions only when the person who publishes says so) needs a setting per column: the
   owner's call after the first trial.
2. The default caps (cells per answer, tuples per axis), to be set from the first trials.
