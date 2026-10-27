# Spec: marimo notebooks

Status: designed 2026-10-27 with the owner; pieces 1–3 written and tried on minikube; piece 4 written (kind and the browsers still to try).

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
- `NotebookSession` (piece 2): one row per live pod — notebook, mode (`app`|`edit`),
  user whose rights it carries, version, Job name, pod IP, status, `started_at`,
  `last_seen_at`. One app session and at most one edit session per notebook.

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

## Proxy (piece 3)

- `/api/notebooks/{id}/app/…` and `/api/notebooks/{id}/edit/…`: HTTP through httpx,
  WebSocket through a websocket client, both to the pod IP. Authentication by the
  session cookie; the WebSocket handshake and every non-GET request must come from the
  app's own origin (marimo's requests carry no CSRF header). The ingress gets a long
  read timeout on these paths.
- The editor shows inside a Tabularia page (an iframe on the same origin) with a toolbar:
  **Save version**, versions, **Open app**. The app opens on its own page.

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
