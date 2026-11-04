# Spec: the gateway in Go

Status: started 2026-11-03 on the `gateway-go` branch; phase 2 begun with `/auth/me`. Nothing here reaches `main`, the
images or the Helm chart until the Go gateway serves every route (see *Done*).

## Objective

Replace the Python gateway (FastAPI, ~60,000 lines, 332 routes) with one written in Go,
for two reasons the maintainer chose:

- **more requests per second**: measured on the production compose, three Python
  replicas serve about 245 requests a second on the folder lists, and the limit is the
  gateway's CPU, not PostgreSQL (at 60 people opening a folder at once, Postgres never had
  more than 10 queries running);
- **types and maintenance**: a compiled, typed code base.

The browser, the scripts, the engine, the Helm chart and the database see no difference:
same addresses, same JSON, same status codes, same cookies, same tables.

## Decisions (the maintainer's, 2026-11-03)

1. **Go**, not Rust: the gateway's work is HTTP, PostgreSQL, Valkey and proxying, where
   Go's standard library (reverse proxy, HTTP/2, WebSockets via a small library) and pgx
   do the job, and it is faster to write.
2. **In front of the Python gateway**: the Go process serves the routes already ported and
   forwards every other request, untouched, to the Python gateway behind it. Routes are
   ported one at a time; each one is verified against the Python one.
3. **On a branch of its own**, tested side by side with the Python gateway. It goes to
   production only when it is complete: no mixed deployment is shipped.
4. **What only Python can do stays in a small Python service** (2026-11-03): the AI
   assistant, built on pydantic-ai (the chat on the data, the flows' agent, the plans, the AI
   comments of reports and dashboards), and the SQL parsing done with sqlglot (the SQL node's
   fence, the reports' parameters, the tables a SQL script names, the dbt export's dialects).
   There is no faithful Go equivalent of either, and a parser written by approximation would
   weaken the SQL fence. The same image as today with a role of its own (`APP__ROLE=ai`):
   the Go gateway forwards the assistant's routes to it and asks it for the SQL parsing over
   an internal API. Rewriting the assistant in Go with the providers' official SDKs stays
   possible later, checked against a fake provider that answers deterministically.

## Architecture

```
browser / scripts ──► gateway-go :8000 ──► ported route: answered in Go
                                      └──► anything else: forwarded to gateway (Python)
                         │
                         ├── PostgreSQL (the same database, the same tables)
                         └── Valkey (the same keys)
```

At the end (decision 4) the Python gateway behind is gone, and a small Python service stays:

```
browser / scripts ──► gateway-go ──► every route in Go
                         ├──► the assistant's routes forwarded to the AI service (Python, APP__ROLE=ai)
                         └──► SQL parsing asked of the AI service over an internal API (sqlglot)
```

- **One table of ported routes** (`internal/routes`): method + path pattern → handler.
  Everything not in it goes to the upstream (`GATEWAY_GO__UPSTREAM`, the Python gateway's
  address). The upstream sees the request as the browser sent it (headers, cookies, body,
  client address in `X-Forwarded-For` as the proxy set it).
- **The database belongs to Alembic.** The Go gateway never creates or migrates a table:
  it reads and writes the schema the Python migrations define, and refuses to start when
  the database is at a revision it does not know (`alembic_version`), as the Python
  gateway does when it is behind.
- **The same environment.** It reads the variables the Python gateway reads (`DB__*`,
  `JWT__*`, `AUTH__*`, `APP__*`, `VALKEY__*`…) with the same defaults, plus its own
  `GATEWAY_GO__*`. Settings an administrator may change from the panel are read from
  `gateway_settings` every 10 seconds and validated with the same ranges
  (`app/core/overrides.py` → `internal/settings`).
- **No new behaviour.** A ported route answers what the Python one answers, bugs
  included; a bug is fixed in Python first (on `main`), then the port follows. The Go
  code cites the Python function it ports.

## The AI service (decision 4)

The Python gateway's image, started with `APP__ROLE=ai`: a process that answers the API like
`api` (no background work) and, only in this role, an **internal API** for the SQL parsing done
with sqlglot. The Go gateway reaches it at `GATEWAY_GO__AI_SERVICE` (required, like
`GATEWAY_GO__UPSTREAM`) with two clients:

- `internal/aisvc`: the internal API below, one call per use of sqlglot the gateway makes (and of
  Python's `re.compile`), and the AI comments of a Pushmail cell's documents;
- a forwarding proxy for the assistant's routes, sent as they came (headers, cookies, body,
  the answer streamed back), where the Python code authenticates them as it always has.

While the Python gateway behind exists, the AI service may be another process of the same
image on the same database (the comparison runs it so); at the switch only it stays.

### Where the gateway reads SQL with sqlglot (the whole list)

| Python | What it asks of sqlglot | Internal call |
|---|---|---|
| `services/sql_scripts.py` `resolve` | the tables a SQL on the catalog names, where each is written, its CTEs, whether it is one query | `POST /internal/sql/tables` |
| `services/sql_scripts.py` `_named_tables` (`named_datasources`, the lineage) | the same, without refusing | `POST /internal/sql/tables` |
| `services/report_params.py` `without_params` | a dataset's SQL with the conditions on some parameters taken for true (a list of choices) | `POST /internal/sql/without-params` |
| `services/openlineage.py` `_qualified_tables` | the qualified names of the tables a query on a connection reads | `POST /internal/sql/qualified-tables` |
| `services/report_params.py` `_text` | `exp.Literal.string(v).sql(dialect)`: a string literal | none: a rule of a few lines, ported (`reportparams.Text`) and checked character by character against sqlglot |

The SQL node's fence (`engine/sql_guard.py`) and the dbt export's translation run in the
engine, not in the gateway: nothing of them goes through this API.

### The internal API

Mounted only when `APP__ROLE=ai`, under `/internal/sql`, outside the OpenAPI schema. Every
request carries `X-Tabularia-Internal` with the token the gateway already sends the engine
(`core/crypto.py` `internal_token()`: HMAC-SHA256 of a fixed label with `SECURITY__FERNET_KEY`),
compared in constant time; without it the answer is a 404 as for a route that does not exist.
The Go gateway never forwards `/internal/…` from a browser (its forwards are named routes), and
a process in another role does not have the routes at all. Bodies and answers are JSON; text
offsets are in characters (code points), as Python indexes a string.

- `POST /internal/sql/tables` — `{"sql", "dialect"}` (a sqlglot dialect: `postgres`, `duckdb`,
  `clickhouse`, `bigquery`, `mysql`, `tsql`, `oracle`, `trino`) → `{"error", "statements",
  "query", "ctes", "tables"}`. `error`: sqlglot's message when it cannot read the SQL (then
  the rest is empty); `statements`: how many statements it holds; `query`: whether the only one
  is a query (`exp.Query`); `ctes`: the CTEs' names, lower case; `tables`: when there is one
  statement, every table it names in sqlglot's order (`find_all(exp.Table)`), table functions
  left out: `{"name", "folder", "catalog", "written", "aliased", "start", "end"}` — the name, its
  folder (`table.db`), whether a catalog qualifies it, the table as the dialect writes it,
  whether it has an alias, and where its folder (or its name) starts and its name ends in the
  text (null when the parser did not say).
- `POST /internal/sql/without-params` — `{"sql", "dialect", "names"}` (`dialect`: a key of
  `report_params.DIALECTS`, an engine or a connection type) → `{"sql"}`, `without_params` as
  it is; its refusals are the 422 it raises.
- `POST /internal/sql/qualified-tables` — `{"sql", "db_type"}` → `{"tables"}`, the sorted
  qualified names `_qualified_tables` finds (none for a SQL it cannot read).
- `POST /internal/script/lint` — `{"path", "content", "language_version", "check_imports",
  "installed", "local", "environment"}` → `{"diagnostics"}`: the editor's check of a Script
  node's Python file (`routes/internal_script.py`), Ruff (a binary the Python package ships)
  and the imports read with Python's `ast`, as `routes/scripting.py editor_check` merges them.
  Who may check, the size, the rate per person and the installed modules are the Go gateway's
  (`POST /scripting/lint`).

Two more calls stand outside `/internal/sql`, on their own prefix and behind the same token: what
only Python's own libraries say. Python's regular expressions, which Go's (RE2: no look-behind, no
back-references, another syntax) cannot stand in for; and httpx's encoding of a host that is not
ASCII, done by the package `idna` (IDNA 2008 with its own Unicode tables: which code points a label
may hold, the joiners' and bidirectional rules), which no Go library reproduces character by
character (`golang.org/x/net/idna` follows UTS 46, other tables, other refusals).

- `POST /internal/python/re-compile` — `{"patterns": [text…]}` → `{"compiles": [bool…]}`, one per
  pattern in their order: whether `re.compile` takes it (`re.error` is a no; any other exception is
  not caught, a 500, as in Python). `services/contracts.py` `_check_rule` asks it of a «pattern»
  rule; the Go gateway sends the patterns of a document in one call, only when it has some
  (`internal/contracts.ValidateDocument`).
- `POST /internal/python/idna-encode` — `{"hosts": [text…]}` → `{"hosts": [text|null…]}`, one per
  host in their order: httpx 0.28's `encode_host` (`idna.encode(host.lower())` for a host that is
  not ASCII), null for its `InvalidURL` («Invalid IDNA hostname»). The Go gateway reads every address
  as httpx does (`internal/pyurl.HTTPXURL`: `webhooks.validate_url` at a webhook's or the SIEM's
  save, `webhooks._resolve` at a delivery or a send) and asks this only for a host that is not
  ASCII (`pyurl.UseIDNA`, set by `api.Routes` and `api.Jobs`); the rest of the address — urlsplit's
  NFKC check, `str.lower` of a hostname, the port, the labels' lengths — is Go's.

And one call on `/internal/ai`, behind the same token: the AI comments the queue cannot write itself
(the assistant's, built on pydantic-ai).

- `POST /internal/ai/comments` — `{"user_id", "definition", "datasets", "params", "given", "where",
  "title", "of_report", "words_of"}` → `{"comments", "reading"}`: `reports.comments_for` for one
  report or dashboard of a flow group's Pushmail cell, as `flow_group_runner._cell_comments` calls it
  (`routes/internal_ai.py`). `definition`, `datasets` and `params` are what the engine renders (a
  dashboard's definition as laid out), `given` the cell's values (`reports.given_params`), `where`
  `["report" | "dashboard", id]`, `words_of` a dashboard's content as the screen has it (null for a
  report); `user_id` is whoever runs the group, on whose bill and with whose rights the comments are
  written. The numbers they read are asked of the engine by the service, as in Python; `reading` is
  the random word that names those rows (null in metadata-only mode). The Go queue
  (`internal/api/jobs_pushmail.go` `cellComments`) asks for the documents of a cell at the same
  time, only for those whose definition holds a comment and not for a report going as Excel without
  a summary in the body; a failure leaves that document without comments and the email goes all the
  same, as in Python. A 422 for a user that does not exist.

Everything around the parse — who may read which datasource, the refusals' words, the aliases
put in the text, the engine's chain — is ported to Go and compared as any route
(`sqlscripts.Resolve`).

### The assistant's routes

Forwarded to the AI service as they came: `POST /ai/chat`, `/ai/flow-assist`,
`/ai/script-assist`; `GET /ai/flow-chats/latest`; `GET /ai/chats`, `GET|DELETE
/ai/chats/{chat_id}`, `GET /ai/chats/{chat_id}/turns/{seq}/steps/{step_id}/export`; `GET
/ai/models`, `PUT /ai/models/{model_id:path}`; `POST /agent/plans/{plan_id}/confirm`; the MCP
server's `tools/call` (the rest of `/mcp` is Go's); the AI comments: `POST
/reports/preview/comment`, `POST /dashboards/preview/comment`, `POST
/dashboards/{dashboard_id}/comment`, and every render of a report or a dashboard whose
definition holds a comment (the comment is written before the render starts). The inventory
says *AI service* for them.

## Compatibility contract (what «the same» means)

For a ported route, for every request in its corpus:

- same status code;
- same JSON body, compared as JSON (key order and whitespace aside; floats as numbers);
- same `Set-Cookie` (name, flags, path, max-age) and the same headers the frontend or a
  proxy read (`WWW-Authenticate`, `Content-Type`, `Content-Disposition`, `Location`,
  `Cache-Control`, the CSRF and security headers);
- same rows written (audit, sessions' last activity, the route's own writes), compared by
  reading the tables after each request;
- the same refusals: 401 / 403 / 404 / 409 / 422 with the same `detail`. A 422 from
  validation keeps FastAPI's shape (`{"detail": [{"loc", "msg", "type"}]}`) where the
  frontend reads it.

## Porting a route: definition of done

1. The handler in Go, citing the Python route and services it ports.
2. Unit tests for its logic (permissions, validation).
3. **A differential corpus**: requests run against the Python gateway and the Go gateway
   on the same database, as an administrator, an editor, a viewer, an observer, a service
   account, a person without access, without a session, with an expired one, with an API
   token read-only and read-write; with good and bad input. `cmd/diffcheck` runs it and
   fails on the first difference.
4. Its line in the route inventory (`docs/gateway-go-routes.md`) moves to *Go*.

## Phases

1. **Skeleton** (this commit): configuration, the forwarding proxy, `/health`, the
   authentication of a request (session cookie or header, API tokens, the CSRF header,
   the cookie deleted on a refused session, last activity written once a minute), the
   container build and the tests. All routes still answered by Python.
2. **The hot path**: `/auth/me`, then the folder tree and the folder lists Explore opens
   (`/projects`, `/projects/{id}/…`), the catalogs (`/datasources`, `/flows`,
   `/reports`, `/dashboards`), with the permission model (`services/permissions.py`:
   folders, inheritance, groups, personal folders). The differential harness grows with
   them. A load test compares the two gateways.
3. **The rest of the CRUD**: users, groups, connections (Fernet), settings, audit, API
   tokens, webhooks, banners, the engine proxy routes.
4. **The hard parts**: the orchestrator role (schedules, queue locks, retention), SSO
   (OIDC), the notebooks' WebSocket proxy, exports and their slots, OpenLineage; the AI
   service (decision 4): the assistant's routes and an internal API for the SQL parsing.
   The background work (`APP__ROLE`, `internal/jobs`, compared «a un giro»: see
   [gateway-go-porting.md](gateway-go-porting.md)) is in Go: schedules, the flows' queue,
   retention, housekeeping, backups, webhooks, engine settings, SIEM and the four Kubernetes
   loops, on the same launching, resolving and Kubernetes code as the routes. Still with a
   Python orchestrator: OpenLineage's run events and the notebooks' reaper while OpenLineage
   is on. A flow group's Pushmail cell whose reports or dashboards have AI comments is carried
   by the Go queue, the comments written by the AI service (`POST /internal/ai/comments`).
5. **Switch**: the images, the compose files and the Helm chart run the Go gateway and
   the AI service; the Python gateway behind is removed. Only when the inventory has no
   *Python* line (*AI service* lines stay).

## Commands

All in a container (no Go on the maintainer's machine):

```bash
# tests
docker run --rm -v "$PWD/gateway-go":/src -w /src golang:1.27.3-bookworm go test ./...
# the image
docker build -t tabularia-gateway-go:dev gateway-go
# the two gateways side by side on a disposable stack, the same requests to both
gateway-go/scripts/diffcheck.sh
# the route inventory (docs/gateway-go-routes.md), after porting a route
gateway-go/scripts/inventory.sh
```

## Boundaries

- Always: cite the Python code ported; a differential corpus for every route; the
  secret scan before every commit.
- Ask first: a new dependency; any change of behaviour, even a fix; anything that
  touches `main`.
- Never: migrate or alter the schema from Go; ship a mixed deployment; merge into `main`
  before the switch.

## Open questions

- ~~WebSocket library for the notebooks' proxy~~: none. `net/http/httputil.ReverseProxy`
  carries the upgrade; the gateway's own close frames (an app full: 1013; the minute's check
  failed: 1008) are written between the pod's frames (`internal/api/notebook_proxy.go`).
