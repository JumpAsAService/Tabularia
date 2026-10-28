# Spec: saved scripts and the Scripting tab

Status: done in 1.2.2 (all six pieces). Decided with the owner on 2026-10-22.
Kubernetes only, as the Script node ([script-node.md](script-node.md)); on Docker
Compose none of it shows. One choice changed while building it: a group cell of a script
the reader cannot see is shown as a hidden cell, as a flow in an unreadable folder is,
instead of making the whole group impossible to open.

## Objective

A Python or R script becomes **an object of a folder**, like a flow or a saved view:
written once, kept with its versions, run on its own or on a schedule, put in a flow
group, and used by Script nodes of any flow. It reads the datasources its author may
read, and may leave a table behind: shown at the end of the run, and published as a
datasource when asked.

A **Scripting** tab gathers what scripting offers a person: the environments and the
container registries they may use, and their scripts.

## Decisions

- **Who sees a script: VIEW on its folder and the use of its environment.** Whoever
  lacks either does not see it at all: not in Explore, not in the search, not in the
  tab, not in the pickers of flows and groups. It is not greyed out like a locked flow:
  it is absent. RUN on the folder runs and schedules it; EDIT changes, moves, renames,
  deletes it. Administrators see every script.
- **Inputs are declared** in the script: datasources chosen from those the person may
  read, each with an **alias**. A datasource name may hold spaces and any character, and
  is unique only in its folder: two folders can each have a «Vendite Nord 2024». So an
  input is **its datasource's id**, never its name, and the code reaches it by the
  alias. The alias is proposed from the name (`Vendite Nord 2024` → `VENDITE_NORD_2024`)
  and can be changed: capitals, digits and `_`, unique in the script. Two inputs with
  the same name get different aliases at once (the folder's name added,
  `VENDITE_NORD_2024_MARKETING`, then a number if still needed); an alias used twice
  cannot be saved. The picker shows each datasource's folder path under its name, and
  searches by folder too. Renaming or moving a datasource changes nothing for the
  script.
- **The output is optional.** A script that writes `TABULARIA_OUTPUT` has it shown in
  preview at the end of the run; when the script says so, it is also **published as a
  datasource** (a name and a folder, overwrite or not), as an Output node does. A script
  that writes nothing (it sends emails with attachments, it loads another system) only
  has its outcome and its log.
- **A saved script runs on its own** («Run» on its page) **and on a schedule**, with the
  authority of whoever set it, as flows and groups do.
- **In a flow group, a saved script is a cell**, a step of its own: it runs with its
  declared inputs, and its failure stops the group as a flow's does.
- **In a flow, the Script node uses a saved script by reference**: fixed once, every
  flow runs the new version. The node keeps its edges: an edge in is still
  `TABULARIA_INPUT`, an edge out is still `TABULARIA_OUTPUT` for the nodes after it.
  The script's declared inputs come too.
- **Container images are not objects.** They live in their registry; the Container node,
  and a new **Container cell** of flow groups, choose and configure them in a guided way
  (piece 6).

## The object

`saved_scripts` (migration 0019):

| Column | |
|---|---|
| `id`, `name`, `description`, `project_id`, `owner_id` | as a flow; the name is unique in its folder |
| `environment_id` | the scripting environment |
| `current_version` | the version that runs |
| `run_schedule`, `run_scheduled_by`, `next_run_at` | as a flow group |
| `created_at`, `updated_at` | |

`saved_script_versions`: `script_id`, `version` (1, 2, 3…), `content` (JSON below),
`note` (`created`, `edited`, `restored from v2`), `created_at`, `created_by`. Every save
that changes the content makes a version, as for flows; an older one can be restored
(it becomes the newest).

`content`:

```json
{
  "files": [{"path": "main.py", "content": "…"}],
  "entrypoint": "main.py",
  "inputs": [{"datasource_id": 12, "alias": "VENDITE_NORD_2024"}],
  "output": {"publish": true, "name": "Forecast", "project_id": 4, "overwrite": true},
  "timeout_seconds": 900, "retries": 0, "retry_delay_seconds": 60
}
```

The checks of the Script node's code apply as they are (`script_nodes.check_files`:
names, sizes, the scripts bucket never named in the code).

## Inputs

At every run, for each declared input:

1. the reader's right is checked again, with the run's authority (the person who ran
   it, or the schedule's author): a datasource they can no longer read fails the run,
   saying which, before anything starts;
2. its current snapshot is copied to the scripts bucket (`script-<id>/<step>/in-<alias>.parquet`),
   the same way a Script node's input is prepared today. «Current» is read at that
   moment: the script keeps the datasource's **id**, which a refresh never changes,
   while the parquet file (`datasets/<id>/<time>-<uuid>.parquet`) is a new one at every
   refresh and the old one is deleted, so a key is never stored. The copy also shields
   the run: a refresh that lands while the script works does not touch its input;
3. the container finds:

| Variable | |
|---|---|
| `TABULARIA_INPUT_<ALIAS>` | the `s3://` address of that datasource's parquet file |
| `TABULARIA_INPUTS` | JSON keyed by **alias** (unique in the script): `{"VENDITE_NORD_2024": {"datasource_id": 12, "name": "Vendite Nord 2024", "folder": "Sales / North", "uri": "s3://…", "columns": [...]}}`, to find an input by its real name and folder, homonyms told apart |
| `TABULARIA_INPUT` | the step before, inside a flow only (as today) |

A deleted datasource makes the script fail with its name, and the script's page shows
the missing input.

## Output

- `TABULARIA_OUTPUT` is always given; writing there is optional.
- After the run, the run's page and the script's page preview it (the first rows and the
  columns), from Tabularia's storage, as the editor previews a Script node today.
- With `output.publish`: the file becomes a datasource in `output.project_id`, by the
  same path an Output node takes (EDIT on that folder, checked with the run's authority;
  overwrite keeps the datasource's id). Over a datasource that is there, as every
  writer's data: its data contract goes with the output to `POST /scripts/output/import`,
  which has a worker evaluate it on the copy (the runs' queue), and a broken blocking
  rule leaves the datasource as it was, fails the run and is recorded in the contract's
  history; the description someone wrote on it stays. Inside a flow the publish setting
  is ignored: there the nodes after the Script node decide where the data goes.

## Runs

A run of a saved script is `kind="script"` with `saved_script_id` and the version it ran
recorded in its `script` JSON, so the run page says «Forecast v3». Standalone and
scheduled runs go to the scripts' namespace (`SCRIPTS__NAMESPACE`); there is no
sample: a saved script runs on the whole data. Retries, timeout, log, exit code and the
pull failures behave as for a Script node.

## Schedules

As for flows and groups: a cron, the author's authority, the checks of
`schedule_authority` before every run (the environment, every declared input, the
publish folder). A change by someone else that widens what the script reaches (other
code, other inputs, another publish folder, another environment) switches the schedule
off, and the audit says why.

## Flow groups

A cell `{"kind": "script", "id": <saved script id>}`. The group editor offers only the
scripts the person sees; a cell of a script one may not see makes the group impossible
to open, as a flow with a script one may not use (`flow_access`). The cell runs the
script's current version.

## The Script node, by reference

`data.scriptId` in place of `data.files` / `data.entrypoint` / `data.environmentId`. The
node shows the script's name and version and opens its code read-only, with a button to
open the script itself. Whoever cannot see the script cannot open the flow. A script
referenced by a flow or a group cannot be deleted: the page lists who uses it.

## Explore, search, Scripting tab

- **Explore**: a fifth kind of object (icon, filter, move, rename, duplicate, delete),
  shown only under the visibility rule above.
- **Search**: `SearchKind` gains `script`.
- **Scripting tab**, visible to whoever may use an environment or a registry:
  - *My environments*: language and version, packages and their versions, variables
    (secret ones by name), build state — read-only;
  - *My registries*: repositories and variables — read-only;
  - *My scripts*: the scripts one sees, with environment, last run, schedule, and «New
    script».
- **The script's page**: the code editor (the one of the Script node, full page), the
  inputs (a datasource picker with the alias), the output settings, the versions, the
  runs with their log and the preview of the last output, Run, Schedule.

## Elsewhere

- **Audit**: `script.create`, `script.update`, `script.delete`, `script.run`,
  `script.schedule`, `script.restore`.
- **dbt export**: a flow whose output reads what a Script node writes is refused as
  today, whatever the code's origin.
- **API tokens**: read tokens list and read scripts they may see; write tokens run them.
- **Lineage**: a saved script reads its inputs and, when it publishes, writes its
  datasource: the lineage page draws those edges (piece 5).

## Pieces

1. **The object**: tables and migration 0019, versions, visibility, CRUD, Explore, search,
   the script's page with the editor, the audit.
2. **Inputs and output**: declared inputs with aliases and `TABULARIA_INPUTS`, «Run» on
   the page, the preview at the end, the publish as a datasource.
3. **Schedules and flow groups**: the cron and its authority, the script cell.
4. **The Script node by reference**: `data.scriptId`, the read-only view, the references
   that block a deletion, `flow_access`.
5. **The Scripting tab** and the lineage edges.
6. **Container in flow groups and guided images**: the Container cell; the image's own
   configuration read from the registry (entrypoint, command, declared variables, labels,
   architectures) to fill in the node and the cell.
