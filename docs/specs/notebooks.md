# Spec: marimo notebooks

Status: designed 2026-10-27 with the owner; pieces 1–3 written and tried on minikube; piece 4 written (kind and the browsers still to try); the origin of their own written 2026-10-28 (kind and the browsers still to try).

## Objective

A person writes a [marimo](https://marimo.io) notebook in marimo's own reactive editor,
keeps it as **an object of a folder** (saved with its versions, found in Explore and in
the search, shared through the folder's permissions) and publishes it as **a web app**
that only the people with access to it in Tabularia can open.

The app's pod starts **when someone opens it** and stops after a while with nobody on it:
no resources spent on apps nobody looks at.

Kubernetes only, as saved Python/R scripts and Container nodes: on Docker Compose the
object shows and says that it needs Kubernetes.

## Decisions (owner, 2026-10-27)

- **The app reads with the rights of its author** — precisely, of **whoever saved the
  version the app runs** (`created_by` of the current version). An editor who changes the
  code and saves it becomes that author: nobody ever runs code with someone else's
  rights. Tabularia's RBAC is checked again at every start of the app's pod.
- **Editing happens in marimo's own editor**, served by a pod in edit mode behind the
  gateway, for whoever has EDIT; that pod reads with **the editor's own rights**.
- **Kubernetes only.**
- marimo is Apache-2.0, compatible with Tabularia's AGPL-3.0; its name is used only to
  describe the feature.
- **An origin of their own** (2026-10-28, after the bug hunt's Critical): apps and
  editors are served from `NOTEBOOKS__ORIGIN`, never from the interface's origin; see
  «An origin of their own» below.

## Model

- `Notebook`: name, description, folder, scripting environment (a Python one: its image
  must carry `marimo`), `current_version` (the one the app runs), a working draft
  (`draft`, `draft_by`, `draft_at`: what the edit pod last saved, kept so that nothing is
  lost if the pod stops). No «always on»: an app's pod runs only while somebody uses it
  (decided 2026-10-27).
- `NotebookVersion`: `content` JSON — `source` (the marimo `.py` file), `note`,
  `created_by`, `created_at`. Saving from the
  editor and restoring an older version make a new version, as for saved scripts. While
  an edit session is live (starting or ready), the source changes only in marimo's
  editor: `PATCH /notebooks/{id}` with a different source and a restore answer 409, saying
  who has it open; **Save version** (`POST /notebooks/{id}/edit/save`) goes on.
  The page sends a new source with the version it was written on (`base_version`): when
  another version became current since, the change is refused (409) instead of undoing
  it (bug hunt 2026-10-28); a save that does not change the source does not send it.
- `NotebookSession` (piece 2): one row per live pod — notebook, mode (`app`|`edit`),
  user whose rights it carries, version, Job name, pod IP, status, `started_at`,
  `last_seen_at`. One app session and at most one edit session per notebook.
- `Notebook.max_visitors` and `NotebookVisitor` (2026-10-28, migration 0028): see
  «Visitors» below.

## Permissions

| To | You need |
|---|---|
| see it in Explore, open the app | VIEW on its folder, and the use of its environment |
| open the editor, save a version, restore | EDIT on its folder |
| create, rename, move, duplicate, delete | EDIT on the folder(s) |
| what the app reads | whatever the author of the current version may read (VIEW), checked at every read |
| what the editor reads | whatever the editor may read, checked at every read |

Administrators see every notebook. Observers open apps (VIEW), never the editor.

## Data (decided 2026-10-27: read on demand, no copies)

The notebook reads datasources **when its code asks**: `tabularia.read("Folder/Name")`
(or by id) returns a polars DataFrame; `tabularia.datasources()` lists what may be read.
No inputs are declared and nothing is copied.

- The pod gets a **notebook token** (`nbk_…`, random): it lives only in the pod's Secret,
  the session row keeps its SHA-256; it is valid only on `/notebook-data/*`, only while
  the session lives (at most `NOTEBOOKS__MAX_LIFETIME_HOURS`), and only from the pod's own
  IP, so a token the author's code printed in the app is useless anywhere else. A call
  with a forwarding header (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`) is refused: the
  gateway's uvicorn trusts them from the private ranges, and on kind any pod could claim
  the notebook's address with one (found 2026-10-27). `NOTEBOOKS__POD_IP_CHECK=false` only
  behind a service mesh.
  Viewers never hold it: in the app only the author's own code runs.
- `GET /notebook-data/datasources` and `POST /notebook-data/read` check VIEW for that user
  **at every call** (a right taken away stops the next read) and answer with a short-lived
  presigned URL to the datasource's own parquet; the pod downloads it from the object
  storage. Every read is audited (`notebook.read`), as the assistant's queries are.
- The `tabularia` module is mounted into the pod (no image rebuild).
- The environment's variables (Administration → Scripting) reach the pod as they reach a
  script: the plain ones in its environment, the secret ones and those handed over from
  Tabularia in its Secret. They never replace the pod's own `HOME`, `PYTHONPATH`,
  `TABULARIA_URL`, `TABULARIA_NOTEBOOK_TOKEN`.
- Network: a notebook pod may reach the gateway's data endpoint and the object storage,
  nothing else of Tabularia.

## Runtime (piece 2)

- A **Job** in the scripts namespace (the RBAC the chart already grants), no restart,
  `activeDeadlineSeconds` = a maximum lifetime (default 12 h), the scripts' security
  context (non-root 10001, read-only root, no capabilities), the environment's resources.
- Containers: `marimo run|edit /work/notebook.py --host 0.0.0.0 --port 2718 --headless
  --base-url /api/notebooks/<id>/<mode>` with `HOME=/tmp`; the file copied from a
  ConfigMap into an emptyDir (`/work`) so edit mode can save; a second tiny container
  serves `/work` read-only on 2719 so the gateway can read the saved file.
- **Network**: a NetworkPolicy lets only the gateway's pods reach notebook pods, on 2718
  and 2719; egress as every user-code pod, plus the gateway's notebook data endpoint.
- **Start on demand**: the first request finds no live session, creates the Job and
  answers with a «starting» page that polls; when the pod answers, requests are proxied.
- **Stop when idle**: every proxied request, and every open websocket once a minute,
  updates `last_seen_at` (throttled); a loop in the orchestrator role stops sessions idle
  for `NOTEBOOKS__IDLE_MINUTES` (default 15), syncing an edit pod's file into the draft
  first. A global cap of live pods; past it, a clear message.

## Visitors (decided 2026-10-28)

marimo's run mode gives **every browser that opens the app a kernel of its own** (its own
session, with its own copy of the data), all inside the app's one pod. Ten people on a
heavy app are ten copies: the pod reaches its memory limit and is killed, for everyone.
Decision of the owner: a **cap of visitors at once per app**.

- `NOTEBOOKS__MAX_VISITORS_PER_APP` (default 10) is every app's cap;
  `NOTEBOOKS__MAX_VISITORS_CEILING` (default 50) is the most a notebook may set for
  itself. Both in Administration → Settings (group «Notebooks»), where the default may
  not exceed the ceiling, and in the chart (`scripts.notebooks.maxVisitorsPerApp`,
  `maxVisitorsCeiling`).
- `Notebook.max_visitors`: nullable (null = the default), set with EDIT through `PATCH
  /notebooks/{id}` (`null` goes back to the default, left out = unchanged) or at
  creation, validated 1..ceiling (422 past it), copied by a duplicate, audited. The cap
  in force is `min(max_visitors or default, ceiling)`: lowering the ceiling caps the
  notebooks that chose more.
- A **visitor** is marimo's session WebSocket (`…/app/ws`) open through the proxy: one
  row of `notebook_visitors` (notebook, user, `opened_at`, `last_seen_at`), in Postgres
  because the gateway has several replicas and no shared memory. The row is taken when
  the WebSocket opens — counted and inserted under `pg_advisory_xact_lock(7402142,
  notebook_id)`, so two replicas cannot both take the last place —, refreshed with the
  minute's check the proxy already does on every open WebSocket, deleted when it closes
  (also when the handler is cancelled). A row not refreshed for 3 minutes (a replica
  that died) no longer counts, and the reaper deletes it. Rows go with their notebook
  and their account (cascade, and explicit deletes).
- **Full**: the GET of the app's root answers 503 (`Retry-After: 10`) with a small page
  — «This app already has N people connected, the most it allows. It will open by itself
  as soon as someone leaves.» — that reloads itself every 10 s, in the frame of
  `/notebook-app`. Whoever already holds a place gets through (a reload, a second tab).
  A WebSocket that loses the race for the last place waits up to 2 s (a reload closes
  the old one a moment after the new page opens its own), then is accepted and closed
  with **1013** (Try Again Later) and the same sentence as reason. marimo's page then
  shows itself disconnected and tries again.
- The **editor** has no cap (one person at a time edits).
- `GET /notebooks/{id}/sessions`: the app's session carries `visitors` (the people
  connected now) and `max_visitors` (the cap in force); the notebook's page shows them
  next to «App running…». `GET /notebooks/{id}` carries `max_visitors`,
  `visitors_default` and `visitors_ceiling` for the field «Visitors at once».
- Good practice, in the docs: read only the columns the app uses
  (`tabularia.read(…, columns=[…])`) and keep aggregates, not rows.

## Proxy (piece 3)

- `/api/notebooks/{id}/app/…` and `/api/notebooks/{id}/edit/…`, on the notebooks' own
  origin (below): HTTP through httpx, WebSocket through a websocket client, both to the
  pod IP. Authentication by the notebook's cookie there (by Tabularia's session cookie
  only with `NOTEBOOKS__ALLOW_SAME_ORIGIN`); the WebSocket handshake and every non-GET
  request must come from a page of the origin they are sent to (marimo's requests carry
  no CSRF header). The ingress gets a long read timeout on these paths.
- Refused (2026-10-28): an app's `…/sse` (marimo's server-sent events open a kernel per
  stream, a visitor outside the cap; its page uses the WebSocket) and any path with dot
  segments (httpx removes them: `x/../sse` reached `sse`). The pod is reached directly,
  never through the gateway's environment proxy, with no certificates loaded per
  request; a message of the pod larger than 64 MiB closes its WebSocket (1009).
- The editor shows inside a Tabularia page (an iframe of the notebooks' origin) with a
  toolbar: **Save version**, versions, **Open app**. The app opens on its own page, in
  the same kind of iframe.

## An origin of their own (decided 2026-10-28)

The bug hunt of 2026-10-28 (docs/audit/CACCIA-BUG-2026-10-28-NOTEBOOKS.md, #1, Critical):
marimo does not sanitise the HTML of an app's outputs (`mo.Html('<iframe srcdoc=…>')`;
`mo.iframe` serves the author's HTML as `./@file/….html` on the same origin), so a
notebook's author ran JavaScript on Tabularia's origin, with the session of whoever
opened the app or the editor: someone with EDIT became administrator as soon as an
administrator opened their app. A CSP or a `sandbox` breaks marimo (scripts,
`localStorage`, the WebSocket's `Origin`) or is bypassed by the author's `@file/*.js`.
Decision: the notebooks get an origin of their own.

- **Setting** `NOTEBOOKS__ORIGIN` (`scripts.notebooks.host`, with `origin` when the
  browser writes a port, `tlsSecretName`): scheme and host, `https` or `http` only for
  `localhost`/`*.localhost` (the cookie is `Secure`), never in `APP__CORS_ORIGINS` (the
  gateway refuses to start). The frontend gets it as `NUXT_PUBLIC_NOTEBOOKS_ORIGIN`, for
  its CSP's `frame-src`. Installation-only (the Ingress and the frontend need it too):
  not in Administration → Settings.
- **Separation by host**: a request whose `Host` is the notebooks' goes to an
  application holding only the proxy's routes (`notebook_proxy.install`, an ASGI
  middleware in front of the gateway): the rest of the API, `POST
  /notebooks/{id}/edit/save` included, does not exist there (404). On the interface's
  host the proxy's routes answer 404.
- **Ticket**: `POST /notebooks/{id}/ticket {"mode": "app"|"edit"}`, with the rights that
  start the session (VIEW, EDIT), only from a browser session (its `sid`) and a page of
  the interface (`Origin` same-origin or in `APP__CORS_ORIGINS`). A random ticket in
  `notebook_tickets` (migration 0031: SHA-256, notebook, mode, user, sid, the asking
  page's origin, expiry), used once (the delete decides between replicas) within 60 s.
  Answer: `{url: "<origin><root>/notebooks/{id}/{mode}/?ticket=…", same_origin: false,
  expires_in: 60}`; without an origin, 409 «Notebooks need an address of their own
  (NOTEBOOKS__ORIGIN): an administrator must set it», or, with
  `NOTEBOOKS__ALLOW_SAME_ORIGIN`, `{url: null, same_origin: true}` and the old paths.
- **Exchange**: the root with `?ticket=` uses it up and answers 303 to `./` (other query
  parameters kept, `Referrer-Policy: no-referrer`) with the notebook's cookie: a JWT
  signed with a key derived from `JWT__SECRET` (a session token is never one, nor the
  other way round) — user, sid, notebook, mode, the asking page's origin —, `HttpOnly;
  Secure; SameSite=None; Partitioned`, `Path=<root>/notebooks/{id}/{mode}/`, an hour,
  written again on the root and on non-GET requests once ten minutes old. A used ticket
  with a good cookie (the frame reloaded) goes on; otherwise a page says to open the
  notebook from Tabularia.
- **On the notebooks' host** the proxy reads only that cookie, checks that its session
  is still open (sign-out, revocation, idle timeout close the frame) and the person's
  rights at every request, as before; an open WebSocket's minute check ignores the
  cookie's age, not its session. `Origin` must be the notebooks' origin itself.
- **Framing**: the answers carry `Content-Security-Policy: frame-ancestors <the asking
  page's origin>` (marimo's own CSP rewritten or completed), no `X-Frame-Options`; the
  pages without a cookie (words only) carry no restriction.
- **Editors start only from Tabularia's page** (`POST /notebooks/{id}/sessions`): on the
  notebooks' host the root of an editor without a session answers «The editor is not
  running» instead of starting one, so a notebook's code cannot open another notebook's
  editor with the rights of whoever looks. The app still starts from its frame (it reads
  with its author's rights).
- **What remains**: the notebooks share one origin; a notebook's code can reach another
  notebook the same person opened in the same browser within the cookie's hour (its app;
  its editor while running). Documented; one host per notebook (a wildcard) would close
  it.
- **Pages**: `/notebook-app` and `/notebook-edit` ask a fresh ticket every time the frame
  is shown (composables/useNotebookFrame.ts), and show the 409's words; with
  `same_origin` they show a warning strip.

## Pieces

1. The object: model and migration, routes (create, read, update, versions, restore,
   rename, move, duplicate, delete), Explore and search, the page with the source, audit.
   marimo in the Python environments' base packages.
2. The runtime on Kubernetes: Jobs, sessions, the notebook token and the data endpoints,
   the `tabularia` module, the idle reaper, the Helm chart (NetworkPolicies, settings).
3. The proxy (HTTP + WebSocket), the editor and the app in the interface, saving and the
   draft.
4. The environment's variables in the pod, the source locked on the server while the
   editor is open, docs (docs/capabilities/notebooks.md), kind with 3 nodes, Chrome and
   Firefox. «Always on» and opening hours dropped (owner, 2026-10-27): apps start only on
   demand.
