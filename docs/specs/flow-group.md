# Spec: Flow Group

Status: draft, 2026-10-13. Decisions marked **(owner)** were taken by the project
owner; the rest are assumptions to correct.

## Objective

Today a chain of work across flows is built inside a flow, with `refresh` and
`runflow` nodes on the canvas, and everything in it runs one after another. There
is no way to say "refresh these three datasources at the same time, then run these
two flows at the same time, then this one", and no single place that shows such a
chain and schedules it.

A **Flow Group** is that place: a grid where each **row is a step** and the cells of
a row are **flows to run or database datasources to refresh, in parallel**. Rows run
in order. The group is scheduled with a cron like a flow, or run by hand, and its run
shows the state of every cell.

Users: whoever builds data pipelines across several flows (builds and schedules
groups), whoever operates the installation (reads their runs).

Success looks like: a group of three steps with two, three and one cells runs the
cells of each step at the same time (as far as the workers allow), starts a step
only when the previous one has finished well, and stops at the first step with a
failure; its run says which cell failed and why.

## Decisions

- **(owner)** The editor is a table: rows are steps, columns are parallel slots,
  with "add row" and "add column". On the right, every flow and datasource the user
  can see, with a search box; items are dragged into the cells.
- **(owner)** Parallel means launched together; how many really run at once is up to
  the workers and the queue already there. No extra cap.
- **(owner)** A failure stops the group: the cells of the failed step finish, the
  following steps do not start. No option to continue.
- **(owner)** A Flow Group lives in folders like a flow: Explore, global search,
  folder permissions, move, delete, audit.
- **(owner)** When the group reaches a flow or a refresh that is already running
  (its own schedule, someone else), it waits for that run and takes its outcome
  instead of starting a duplicate.
- **(owner)** No nested groups in this version: cells hold flows and datasource
  refreshes only.

## What it reuses

The orchestration machinery already does most of it (gateway/app/services/
orchestrator.py): the queue on `runs` claimed by an orchestrator process
(APP__ROLE), heartbeat and orphan handling, `_refresh_and_wait` (which already
waits on an ingest in progress), `orchestrate()` for a flow, `_finalize_orch_run`,
the scheduler's slot claiming, the one-live-run-per-flow index. What is new is
running the cells of a step concurrently.

## Data model (migration 0006)

`flow_groups`: `id`, `name`, `description`, `project_id` (folder), `owner_id`,
`definition` (JSON text), `run_schedule`, `run_scheduled_by`, `next_run_at`,
`created_at`, `updated_at`; unique `(project_id, name)`.

`definition`:

```json
{"steps": [[{"kind": "refresh", "id": 7}, {"kind": "refresh", "id": 9}, null],
           [{"kind": "flow", "id": 12}, {"kind": "flow", "id": 15}, null],
           [{"kind": "flow", "id": 20}, null, null]]}
```

`null` keeps an empty cell in its column, so the grid comes back as it was drawn.
Checked on save: kinds `flow` | `refresh`; the objects exist and the editor can see
them (VIEW); a refresh names a database datasource; an object appears at most once
in the same step (two runs of one flow at once would collide); up to 50 steps and
20 columns.

`runs`: a nullable `flow_group_id`. A group run is a `kind="orchestration"` row with
`flow_group_id` set and `flow_id` null, so the existing queue, claim, heartbeat and
orphan handling apply; a partial unique index keeps one live run per group.

## Running a group

1. The run is claimed by an orchestrator like a flow's orchestration.
2. Steps run in order. For each step, every cell starts at once, each with its own
   database session:
   - **refresh**: `_refresh_and_wait` with the group run as parent (it already
     reuses an ingest in progress).
   - **flow**: the cell takes the flow's live slot by inserting a child
     orchestration row (`flow_id`, `parent_run_id` = group run, claimed by the same
     process) and runs `orchestrate()` under it, in production mode like a scheduled
     run. If the flow already has a live run, the insert fails on the unique index
     and the cell waits for that run and takes its outcome. Taking the slot also
     keeps the flow's own schedule from starting a duplicate meanwhile.
3. When all cells of the step have finished: any failure stops the group, which ends
   FAILURE with a message naming the failed cells; otherwise the next step starts.
4. The child orchestration rows get the heartbeat like the group run, so a crash
   closes them as orphans too.

Per-cell state for the UI comes from the children: a flow cell is its child
orchestration row (and its output runs under it), a refresh cell is its ingest run.
Cells of steps never started are "skipped".

## Scheduling and permissions

| Action | Needs |
|---|---|
| see the group and its runs | VIEW on its folder |
| create, edit, delete, move | EDIT on the folder (both folders to move) |
| run now, set or clear the schedule | RUN on the folder |
| each cell, when it runs | RUN on the flow's folder; RUN on the datasource's folder and CONNECT on its connection's folder (as today) |

A scheduled run uses the authority of the user who set the schedule; moving the
group to another folder clears a schedule set by someone else (as datasources do).
A cell whose rights are missing at run time fails, and the group stops.

## API

- `GET /flow-groups`, `GET /projects/{id}/flow-groups`, `GET /flow-groups/{id}`
- `POST /projects/{id}/flow-groups`, `PATCH /flow-groups/{id}`, `DELETE /flow-groups/{id}`
- `PUT /flow-groups/{id}/schedule`, `POST /flow-groups/{id}/run-now` (409 if running)
- `GET /flow-groups/{id}/runs` (latest group runs), `GET /flow-groups/{id}/runs/{run_id}`
  (the run with the state of every cell)
- `/search` returns groups; audit actions `group.create|update|delete|move|run|schedule`;
  deleting a folder that holds groups is refused like for flows.

## UI

- Explore: a new row kind with its icon; "New flow group" next to "New flow"; move
  and delete like the other rows.
- Editor page:

```
┌ Nightly refresh ─────────────── [Save] [Schedule: 0 2 * * *] [Run now] ┐┌ Search… ──────────┐
│            Parallel 1        Parallel 2        Parallel 3    [+ col]   ││ (Flows) Datasources│
│ Step 1  [↻ orders     ✕]  [↻ customers  ✕]  [  drop here ]           ││ ⇄ Margin by cat.   │
│ Step 2  [⇄ Margins    ✕]  [⇄ Leaderboard✕]  [  drop here ]           ││   Sample › Flows   │
│ Step 3  [⇄ Report     ✕]  [  drop here ]    [  drop here ]           ││ ⇄ Agent leaderboard│
│ [+ row]                                                               ││   Sample › Flows   │
└───────────────────────────────────────────────────────────────────────┘└────────────────────┘
```

  Cells show the name with the folder path under it; a cell is dragged to another
  cell to move it; rows and columns can be removed. Every drag has a keyboard and
  click alternative (select a cell, then pick an item from the panel).
- While running and for past runs, each cell shows its state (waiting, running,
  succeeded, failed, skipped, waited on a run already in progress) with a link to the
  run; the latest runs are listed under the grid.
- Five locales.

## Out of scope for this version

Nested groups, retrying a cell, conditional steps, a cap on parallel cells, failure
emails for groups (the flows inside keep theirs),
webhooks for the group run itself (the runs inside keep theirs).

## Testing

- Gateway: definition validation; RBAC on every route; execution with the fake
  engine: cells of a step launched before any of them finishes, steps in order, stop
  at the first failed step, waiting on a flow or ingest already running, the flow's
  slot taken during the group, heartbeat and orphan closing of child rows, scheduler
  firing and authority, folder move clearing a foreign schedule.
- Browser: build a group by drag and drop and by keyboard, save, schedule, run now,
  see cell states live; Explore row, search, move, delete.
- Full regression: gateway and engine suites, typecheck, the live check, every page.

## Plan

1. Model, migration 0006, CRUD with RBAC, search, audit, folder-delete guard; tests.
2. Execution: parallel steps, stop on failure, waiting on runs in progress, child
   slots and heartbeat; tests.
3. Run now, schedule, run endpoints with per-cell state; tests.
4. Explore integration and the grid editor with the side panel, drag and drop and
   the keyboard alternative; save.
5. Live cell states, run history, schedule dialog; i18n.
6. Browser tests, documentation (README, CHANGELOG), full regression.
