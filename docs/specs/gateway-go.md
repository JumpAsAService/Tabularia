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

## Architecture

```
browser / scripts ──► gateway-go :8000 ──► ported route: answered in Go
                                      └──► anything else: forwarded to gateway (Python)
                         │
                         ├── PostgreSQL (the same database, the same tables)
                         └── Valkey (the same keys)
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
   (OIDC), the notebooks' WebSocket proxy, exports and their slots, OpenLineage, the SQL
   guard (sqlglot has no Go equivalent: a decision of its own), the AI assistant
   (pydantic-ai: same).
5. **Switch**: the images, the compose files and the Helm chart run the Go gateway;
   the Python one is removed. Only when the inventory has no *Python* line.

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

- The SQL guard (`sqlglot`) and the AI assistant (`pydantic-ai`) have no Go equivalent:
  keep them in a small Python service behind the Go gateway, or rewrite them.
- WebSocket library for the notebooks' proxy (`nhooyr.io/websocket` → `coder/websocket`,
  or `gorilla/websocket`).
