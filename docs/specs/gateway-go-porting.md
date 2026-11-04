# Porting a module of the gateway to Go

How a module of the Python gateway (`gateway/app/routes/<module>.py` and the services it
calls) becomes Go, for whoever ports one. The why and the phases are in
[gateway-go.md](gateway-go.md).

## The rule

The Go route answers **exactly** what the Python route answers, for every request: status,
JSON, headers, cookies, the rows it writes, what it sends to the engine, the audit events.
Bugs included: a Python bug is fixed in Python first (on `main`), then ported. When in
doubt, the Python code decides, not this document.

## Where things go

| What | Where |
|---|---|
| a module's routes | `gateway-go/internal/api/<module>.go` (+ more files `<module>_*.go` if long) |
| a service shared by several modules | `gateway-go/internal/<service>/` |
| the module's comparison | `gateway-go/cmd/diffcheck/suite_<module>.go` |
| the module's unit tests | `<file>_test.go` next to the code |

Each module registers its routes in an `init()`, never in a shared list:

```go
func init() {
	register("flows", func(d *Deps) []server.Route {
		return []server.Route{
			{Method: http.MethodGet, Pattern: "/flows/{flow_id}", Handler: server.Authed(d.Auth, "/flows/{flow_id}", getFlow(d))},
		}
	})
}
```

`Pattern` is the path exactly as FastAPI declares it (`/flows/{flow_id}`); the gateway
answers only the exact method, everything else still goes to Python.

## The shared packages

- `auth` — the person of the request (`server.Authed` gives the `*auth.Principal`).
- `perm` — permissions: `perm.For(db, &p.User)` per request; `Has`, `Ensure` (the 403 of
  `ensure_can`), `Readable`, `Visible`, `Projects(ctx, perm.Edit)`, `IsAdmin`, `Roles`.
- `audit` — `audit.Record(ctx, db, log, audit.Event{…})`; the detail is a `pyjson.Object`
  with the keys **in the order the Python code builds the dict**.
- `pyjson` — JSON as Python's `json.dumps` writes it: for every text column the Python code
  fills with `json.dumps` (check its arguments: `ensure_ascii`, `separators`, `default`).
  `pyjson.Isoformat` is `_utc_iso` (`UtcDateTime` fields of the schemas).
- `pyd` — request validation as pydantic and FastAPI do it, with the same 422s (body,
  query and path parameters).
- `server` — `server.JSON` (Starlette's JSONResponse), `server.Refuse` (an HTTPException),
  `server.Authed`.
- `reqinfo` — the request's address, user agent, token, agent.
- `roles`, `settings` (the panel's values), `language`, `clientip`, `config`.
- `aisvc` (`Deps.SQL`) — what the Python code reads with sqlglot, asked of the AI service
  ([gateway-go.md](gateway-go.md), «The AI service»); `sqlscripts.Resolve` is
  `sql_scripts.resolve` on top of it. Never parse SQL in Go. A route of the assistant (built
  on pydantic-ai) is not ported: it goes to the AI service whole (`ai_service.go`,
  `toAIService`).

- `connpayload` — `engine_connection_payload` (the engine's `connection` of every kind, the
  secret still encrypted) and the tolerant readings of a connection's options (SMTP domains,
  SFTP method, API options); `dbtls` — `services/db_tls.py`.
- `runlaunch` — the launching half of `routes/runs.py` and `services/sql_script_runs.py`:
  `SnapshotKey`, `ContractPayload` (`contracts.payload`), `Ingest` (`launch_ingest_run`) with
  `InLine`/`LiveRefresh` (the refresh lock), `LaunchFlowRun` (`_launch_flow_run`, on a validated
  `RunCreate`), `LaunchSQLScript` (`sql_script_runs.launch`); `EngineDown` is httpx's
  TransportError, main.py's 503. The routes and the background work share them.
- `flowresolver` — `services/flow_resolver.py`; `contracts` — `services/contracts.py`
  (validation, save, ODCS).

A service that several modules need and that does not exist yet: write it in its own
package, small, with the Python function it ports cited in its comment.

## Writing the Go

- **English** for code and comments, as the rest of the repository: comments say why, cite
  the Python function ported (`// routes/flows.py get_flow`), short.
- The database belongs to the Python migrations (Alembic): never create or alter a table.
  Read the model in `gateway/app/models/` for the columns; timestamps are naive UTC.
- Transactions: where the Python route commits several writes together, do them in one
  transaction; where it commits in steps, do the same steps (the audit row is written after
  the route's own commit, as `record_audit` commits).
- Order matters wherever it is visible: the JSON keys (Go structs keep field order), lists
  (the Python query's `ORDER BY`, or its absence: then the database's order, usually by id),
  the audit rows, the rows' ids.
- FastAPI checks things in an order: the dependencies (authentication, then the
  route's guards) before the body's validation, a 401 before a 422. Reproduce it, and let the
  comparison prove it.
- Errors: `{"detail": "…"}` with the same text, the same status. An unexpected error (an
  exception no handler catches) is Starlette's 500, `Internal Server Error` as **plain
  text** (`server.InternalError`), not JSON.

## Proving it: the comparison

`gateway-go/scripts/diffcheck.sh` starts twin stacks (each its own PostgreSQL and fake
engine; Python in A, Go in B with Python behind) and sends each step of each suite to both.
After every step it compares the answer, the **whole content of both databases** and the
requests sent to the engine. A suite:

```go
func init() {
	register(Suite{
		Module: "flows",
		Seed: func(w *World) { /* w.Insert / w.Exec on both databases */ },
		Engine: []EngineRule{{Method: "GET", Path: "^/engines$", Body: []any{…}}}, // the fake engine's answers
		Steps: func(w *World) []Step {
			return []Step{
				{Name: "admin reads a flow", Method: "GET", Path: "/flows/1", Auth: w.As("admin")},
				{Name: "a viewer cannot edit", Method: "PATCH", Path: "/flows/1", Auth: w.As("editor"), JSON: map[string]any{"name": "x"}},
			}
		},
	})
}
```

The people of `cmd/diffcheck/people.go` (admin, editor, observers, inactive, service,
sso, writer) are seeded in both databases with their sessions; `w.As(person)` signs a
request in as a browser does (cookie + CSRF header). A random value the two sides cannot
share (a token, a generated id) is masked: `Mask` on the step for the answer,
`maskColumns(table, columns…)` for a table.

**What a suite must cover, for every route:** each kind of person (administrator, someone
with the needed grant on the folder, someone without it, an observer, no session); good
input and each kind of bad input (missing field, wrong type, out of range, unknown id, a
name taken); the order of the checks (401 before 404 before 422…); every branch the Python
code has (read it). A route is ported only when its suite passes with **no difference**:

```bash
DIFF_ONLY=flows gateway-go/scripts/diffcheck.sh
gateway-go/scripts/test.sh          # the unit tests, on a disposable PostgreSQL
gateway-go/scripts/inventory.sh     # docs/gateway-go-routes.md
```

Go is not installed on the machine: the scripts run it in `golang:1.27.3-bookworm`.

## What a port may not do

- change a behaviour, even to fix it (fix the Python first, on `main`);
- touch the Python gateway on this branch (it is the reference the comparison runs);
- add a dependency to `go.mod` without a reason written in the commit;
- skip a step of the suite, or mask a value that is not random.
