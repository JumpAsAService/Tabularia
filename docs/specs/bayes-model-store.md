# Spec: the saved Bayesian models (`model-store`)

Status: **written and approved by the owner on 2026-11-23.** It specifies the module `model-store` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 3 of its order of construction, after the approved
`bayes-model-graph.md` (**the graph spec**), `bayes-stan-codegen.md` (**the codegen spec**) and `bayes-contract.md` (**the
contract**). Nothing of it is built. Decisions marked **(owner)** are the owner's (the map's «The owner's decisions» and
«Interview answers», numbered as there: **the answers**, and the step-2 answers); the rest are this spec's, to confirm;
those marked «spec decision, 2026-11-23, owner may revisit» are small choices the owner may change without reopening the
module. The owner answered this spec's questions on 2026-11-23 («Decisions of 2026-11-23»), the schema migration
included (approved); no question is open.

Words: **verified** = read in this repository's code on 2026-11-23; **assumed** = not run here, checked by the lot named
beside it.

## Objective

A Bayesian model drawn in the editor becomes **an object of a folder**, as a saved script is:

- **`bayes_model`**: a name, a description, a folder, an owner, the version that is current, and the template it was
  opened from;
- **its versions**: each holds the graph as saved (canonical, layout included), its semantic hash, the findings of the
  validator (warnings only: a graph with an error is not saved), and **the generated program**, its sha256, the mapping
  table, the two manifests of the contract and the generator's version (`CodegenVersion`), all written once and never
  again: **every saved version has a program**;
- **restore**, **duplicate**, **move**, **rename**, **delete** (refused while a flow uses it);
- **who may do what**: VIEW on the folder and the use of the group `bayes` to see it, EDIT to change it, RUN to run it
  (the map);
- **where it appears**: Explore, the search, the recent objects, the folder's delete check, the audit;
- **«used by N flows»**: the flows whose node **Bayesian model** pins one of its versions (answer 7);
- **the compile in the background when a version is saved** (answer 8), through an interface that `stan-compile`
  implements;
- **the program saved runs, always** (owner, step 2: «Always the stored program»): a version is never generated again.

It draws nothing (`graph-editor`), compiles nothing itself (`stan-compile`), runs nothing (`flow-node`, `bayes-runner`).

## Decisions

| Question | Decision | By |
|---|---|---|
| The object | A folder object with versions, as a saved script | Owner (map: «Where it lives») |
| Visibility | **VIEW on the folder and the use of the group `bayes`**; whoever lacks either does not see the model at all (404, absent from lists and search), as a saved script without its environment; administrators see every model | The map (`model-store`), the saved scripts' rule (verified) |
| Changes | **EDIT** on the folder (and on the destination of a move) | The map |
| Running | **RUN** on the folder, checked with the run's authority at every run by `flow-node` (`CanRun`), and for «Compile now» | The map; where it is checked: spec |
| Which version a flow runs | A **pinned** version; the notice «a newer version exists» | Owner (answer 7) |
| A new version and scheduled flows | **Nothing switched off**: a flow pins its version, so a save changes no scheduled run (the saved scripts' «schedule switched off» exists because Script nodes run the current version) | Spec (follows from answer 7) |
| The compile | Started **in the background after a version with a program commits**; a failure never fails the save; the run is the fallback | Owner (answer 8) |
| The program | **Stored with the version, never regenerated**; a newer `CodegenVersion` reaches a model only through a new version | Owner (step-2 answer) |
| The manifests | Stored with the version beside the program: a later generator could write other manifests for the same graph, and the runner checks the CSV header against the manifest of **the program that runs** | Spec (follows from the owner's step-2 answer and the contract's header check) |
| A graph with errors | **The save is refused**: the gateway answers 422 with the findings and creates no version; **warnings never block a save**, only errors do; so every saved version has a program. The editor warns before leaving a page with unsaved changes (`graph-editor`) | Owner (2026-11-23) |
| The program of a restored version | **Copied from the restored version**, with its `codegen_version`, mapping table and manifests: a saved version always runs with its saved program, restored or not | Owner (2026-11-23) |
| The schema | **Revision 0046 with two tables** (`bayes_models`, `bayes_model_versions`) and their SQLModel classes, schema only; `stan-compile`'s table of binaries comes with that module's own revision | Owner (2026-11-23: approved; the Python gateway is frozen except for migrations) |
| The routes | **Go only** (`gateway-go/internal/api/bayes_models.go`, logic in `gateway-go/internal/bayesmodels`); no route, service or code path in the Python gateway | The repository's rule (gateway Python frozen since 2026-11-06) |
| Two people saving at once | A save of the graph carries `base_version`; when it is not the current one the save is refused (409: «version 6 was saved by someone else meanwhile: reload to see it») | Spec decision, 2026-11-23, owner may revisit (saved scripts take the last write; a graph merged by nobody is safer refused) |
| Layout-only saves | A version like any other (the graph spec: a version is never rewritten in place); its semantic hash and program are the previous one's, so the compile cache answers at once | Spec |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| Versions of a folder object | `gateway-go/internal/savedscripts/savedscripts.go`: `AddVersion(ctx, tx, table, versions, fk, id, content, note, userID)` (the object's row `FOR UPDATE`, the next number, the insert), `VersionRow` (version 0 is the current one), `Using`, `References`; `gateway-go/internal/versioned/versioned.go`: `Kind{Table, Versions, FK, …}`, `GetVisible` (404 for whoever cannot see), `Visible`, `AddVersion`, `Delete` | The same shape: the row locked, the next number, the version inserted and the current one set in one transaction. `versioned.Kind` is **not** used as it is: it reads `views_total`, `last_viewed_at` and a views table a model has no use for, and a single `content` column; `bayesmodels` writes the same three statements with its own columns |
| Visibility through a group's use | `api/models_groups_image.go`: `ModelGroupUsable(ctx, q, c, userID, group)` — the group enabled and the person an administrator, or the group for everyone, or the person in a user group of its environment (`scriptnodes.CanUse`) | `CanSee` = admin, or VIEW on the folder and `ModelGroupUsable(…, "bayes")` |
| Capabilities | `internal/perm`: `View`, `Run`, `Edit`, `Manage`, `Satisfies` (VIEW < RUN < EDIT < MANAGE), `Checker.Has`, `Readable` | The three checks |
| The saved scripts' routes | `api/scripts.go`: list, list by folder, get, create, update (name, folder, content), duplicate (`copynames.Free`), delete (refused with the flows that use it, `pg_advisory_xact_lock`), versions, version, restore; `scObjectName` (a name free in its folder), `scVersionsOf` (`api/sql_scripts.go`), `audit.Record` with `script.create`, `script.update`, `script.restore`, `script.delete` | The same routes and answers for `/bayes-models` |
| «Used by» | `savedscripts.Using`: flows whose definition holds `"scriptId"`, then each Script node read; flow groups through `flowgroups.Containing` | `bayesmodels.Using` on `"bayesModelId"`; flow groups have no cell for a model (it needs a binding, which only a flow node has) |
| Explore, search, recent | `frontend/components/ProjectBrowser.vue` (kinds `script`, `sql`…: rename, move, duplicate, delete, «New»); `api/search.go` (`visible(kind, table, withEnv)`); `api/explore_recent.go` (`recentSQL`: one `UNION ALL` per kind) | A kind `bayes` in each |
| A folder that cannot be deleted while it holds objects | `api/projects.go`, `folderContents` | `{"bayes_models", "The folder contains Bayesian models: move or delete them first"}` |
| The audit page | `frontend/pages/audit.vue`: an icon, a colour and a label per action (`script.restore`…) | `bayes_model.*` |
| A bounded body | `http.MaxBytesReader` (`api/models_licence.go`, `api/data_apis.go`) | `max_graph_bytes` (256 KiB) plus 16 KiB for the other fields |
| Strict decoding, the hash, the findings | `bayesgraph.Decode` (upgrades an older `format_version` on read), `CheckStructure`, `Validate`, `Canonical`, `SemanticHash` | Every graph received |
| The program | `bayescodegen.Generate` (the codegen spec: the program, the mapping table, the two manifests, `CodegenVersion`; not built) | Called at every save of a graph without error |
| A table only Go reads, with its SQLModel class | `gateway/app/models/model_group.py`: «Only the Go gateway reads and writes this table; the model is here so that the migrated database and the models stay one and the same»; revision `0044_models.py` | The same for the two new tables |
| Migrations | `docs/database-migrations.md`: «The SQLModel models are the single source of truth: a revision is generated from them»; forward only; `python -m app.db.migra controlla`; the last revision is `0045_model_recheck.py` | Revision `0046` |
| Go tests on a database | `TEST_DATABASE_URL`, `gateway-go/scripts/test.sh` (`api/explore_recent_test.go` skips without it) | The routes' tests |

## The schema (revision 0046, approved by the owner on 2026-11-23)

Only additions: two empty tables; no existing table or column changes, so the previous version's code neither meets nor
misses them (expand/contract, `docs/database-migrations.md`).

`bayes_models`:

| Column | Type | |
|---|---|---|
| `id` | integer, primary key | |
| `name` | text, not null | unique in its folder (`uq_bayes_model_project_name`) |
| `description` | text, not null, default `''` | |
| `project_id` | integer, not null, → `projects.id`, indexed | the folder |
| `owner_id` | integer, → `users.id` | |
| `current_version` | integer, not null | the newest version (what the editor opens) |
| `template_id` | text(40) | the template it was opened from, or null (the free model) |
| `template_version` | integer | that template's `version` |
| `created_at`, `updated_at` | timestamp, not null | naive UTC, as every table of the gateway |

`bayes_model_versions`:

| Column | Type | |
|---|---|---|
| `id` | integer, primary key | |
| `model_id` | integer, not null, → `bayes_models.id`, indexed | |
| `version` | integer, not null | 1, 2, 3… (`uq_bayes_model_version`) |
| `graph` | text, not null | `Canonical(g)` with `layout`, as decoded (an older format upgraded on read: the text is never rewritten) |
| `format_version` | integer, not null | the graph's |
| `semantic_hash` | char(64), not null | sha256 of the canonical form without `layout` |
| `findings` | text, not null | the validator's findings at save (JSON: warnings only, a graph with an error is never saved) |
| `program` | text, not null | the generated program |
| `program_sha256` | char(64), not null, indexed | sha256 of `program`'s bytes (what `stan-compile` keys its cache with) |
| `mapping` | text, not null | the mapping table (JSON) |
| `data_manifest`, `quantity_manifest` | text, not null | the contract's two manifests (JSON) |
| `codegen_version` | integer, not null | `bayescodegen.CodegenVersion` of the program |
| `family` | text(12), not null | `hierarchical`, `series`, `glm` or `free`, **derived from the saved graph** at save («The family», below; Correction of 2026-11-24 from the mockups: revision 0046 is not built yet, so the column joins it) |
| `catalog_sha256` | char(64), not null | sha256 of the catalogue snapshot the gateway held (`bayescatalog.Raw()`) |
| `note` | text, not null | `created`, `edited`, `layout`, `restored from v3`, `duplicated from «…» v2` |
| `created_at` | timestamp, not null | |
| `created_by` | integer, → `users.id` | |

### The family (owner, 2026-11-24; Correction of 2026-11-24 from the mockups)

The list shows a model's family **derived from its saved graph**, never from the template it was opened from (a template,
once opened, is a graph like any other): `bayesmodels.Family(model)` on the validated model, at save, written in the
version's `family`, first rule that holds:

1. a group plate holds a node → `hierarchical`;
2. otherwise the rows plate is a series (`series` set) → `series`;
3. otherwise every likelihood is one of the catalogue's GLM likelihoods (`normal`, `bernoulli_logit`, `binomial_logit`,
   `poisson_log`, `neg_binomial_2_log`) whose location argument is a linear predictor node → `glm`;
4. otherwise → `free`.

A restored version copies the restored one's `family` with its program. The words are `graph-editor`'s (GLM,
hierarchical, series, free); the template stays visible on its own line («opened from «…»»).

Sizes: a graph is at most 256 KiB (`max_graph_bytes`); the program, the mapping and the manifests are bounded by the
graph's limits (`max_nodes` 200: the contract says the program is «well under the 1 MiB of a ConfigMap»). Measured by
lot D0 on the largest graph of the corpus and recorded.

**The Python side** (approved by the owner, 2026-11-23): `gateway/app/models/bayes_model.py` with the two SQLModel classes, the docstring
of `model_group.py`'s kind («only the Go gateway reads and writes these tables»), imported in
`gateway/app/models/__init__.py`; `gateway/app/db/migrations/versions/0046_bayes_models.py` generated from them
(`python -m app.db.migra nuova "bayes models"`), read by a person, forward only. **No other Python file changes.**

## Who may do what

| Action | Needs | Otherwise |
|---|---|---|
| See (list, search, Explore, open, versions) | administrator, or VIEW on the folder **and** `ModelGroupUsable(…, "bayes")` | 404 «Bayesian model not found», absent from every list |
| Check a graph (`POST /bayes-models/check`) | the group's use | 403 «the Bayesian models are not available to you» |
| Create in a folder | the group's use and EDIT on the folder | 403 |
| Save, rename, describe, restore, delete | see it, and EDIT on its folder | 403 |
| Move | EDIT on both folders | 403; 404 for a destination that does not exist |
| Duplicate | see it, and EDIT on its folder (the copy is made there) | 403 |
| Compile now (`POST …/versions/{v}/compile`) | see it, and RUN on its folder | 403 |
| Run (in a flow) | `CanRun`: see it **with the run's authority** (the person who ran the flow, or the schedule's author) and RUN on its folder; checked by `flow-node` at every run, before any Job | the step fails, naming the model and the missing right |

When the group is disabled, `ModelGroupUsable` is false for every person but administrators: the models disappear from
everyone else's lists, as scripts do when their environment goes; nothing is deleted, and enabling the group again shows
them as they were.

## The routes (Go)

All under the gateway's authentication (`server.Authed`), registered with `register("bayes_models", …)` as
`api/scripts.go` is; bodies read through `http.MaxBytesReader` and decoded strictly (unknown keys refused).

| Route | Does |
|---|---|
| `GET /bayes-models` | the models this person sees, by name: id, name, folder, current version, template, the current version's `family`, updated, `editable`, `runnable`, **and (Correction of 2026-11-24 from D3: the approved list shows them in columns) `compile` (`state`, `detail`, `at` of the current version's program), `uses` (the flows that pin the model, as in the detail) and `saved_by` (the email of who saved the current version)** — never a graph or a program. The compile states come from one `Compiler.States` call for the distinct programs (a `BatchCompiler` answers in one read; a plain `Compiler` is asked once per distinct program), the uses from one read of the flows that hold the key, the author from the same join as the family: no query per model |
| `GET /projects/{project_id}/bayes-models` | the same for one folder |
| `GET /bayes-models/{model_id}` | the model, and its current version: graph, findings, program, mapping, compile state, `uses` (the count) |
| `POST /projects/{project_id}/bayes-models` | `{name, description, graph, template_id?, template_version?}` → version 1, note `created` |
| `PATCH /bayes-models/{model_id}` | `{name?, description?, project_id?, graph?, base_version?}`: a new version when `graph` differs from the current one's canonical bytes; `base_version` required with `graph` |
| `POST /bayes-models/{model_id}/duplicate` | a copy in the same folder (`copynames.Free`), its version 1 the current version's graph **and stored program** (the same bytes: the cache answers) |
| `DELETE /bayes-models/{model_id}` | refused (409) while a flow uses it, naming the flows (the first ten, as scripts); otherwise the model and its versions, in one transaction under `pg_advisory_xact_lock` |
| `GET /bayes-models/{model_id}/versions` | version, note, created, by, `semantic_hash`, `program_sha256`, `codegen_version`, current — no graph |
| `GET /bayes-models/{model_id}/versions/{version}` | one version in full |
| `POST /bayes-models/{model_id}/versions/{version}/restore` | a new version holding that one's graph, program, `program_sha256`, `codegen_version`, mapping and manifests **copied as stored** (owner, 2026-11-23), note `restored from v3` |
| `GET /bayes-models/{model_id}/uses` | the flows that use it: for each, the node id and the pinned version; a flow this person cannot open is counted, not named («and 2 flows you cannot open») |
| `POST /bayes-models/check` | `{graph}` → findings, and when there is no error the program, the mapping and the manifests; nothing saved (the editor's code panel and its check before saving) |
| `POST /bayes-models/{model_id}/versions/{version}/compile` | asks `stan-compile` for that version's program now (RUN); 409 while one is queued or running |

### Saving a graph

1. The body read within its bound; `graph` decoded by `bayesgraph.Decode` (an older `format_version` upgraded; a newer
   one refused with `graph.format`).
2. `Validate`: the findings, sorted (the graph spec). The gateway's findings are the authority: the editor replaces its
   mirror's with them.
3. **An error among the findings: the save is refused** (422, the findings in the answer, nothing stored, no version;
   owner, 2026-11-23). Warnings do not block. Then `Generate` → program, mapping, manifests, `CodegenVersion`; sha256 of
   the program. `Generate` is called with `predictive-checks`' extension (`bayeschecks.Extension{}`), so every stored
   program can run both predictive checks (Correction of 2026-11-24 from the step-4 specs). A `Generate` error is
   a bug of the generator: the save fails with 500 and the log says which rule, nothing is stored (the codegen spec:
   «`Generate` returns an error, never a program»).
4. `Canonical(g)` and `SemanticHash(g)`. When the canonical bytes equal the current version's, nothing is saved (200 with
   the model as it is).
5. One transaction: `base_version` compared with `current_version` under the row's lock (409 when they differ); the next
   number; the version inserted; `current_version`, `updated_at` set.
6. `audit.Record`: `bayes_model.update` with `{version, warnings, semantic_changed, program_sha256}` (never the graph, never a
   label: they are the person's text and the audit's detail is shown to observers).
7. **After the commit**, when the version's program has no binary yet: `Compiler.Enqueue` (below). Its
   error is logged and shown on the page as the compile state; the save has already answered 200.

### Explore, search, recent, the folder

- **Explore** (`ProjectBrowser.vue`): a kind `bayes` with its icon, the current version and its compile state, rename, move, duplicate, delete, and «New → Bayesian model» for whoever may use the
  group (the editor's page with the folder: `graph-editor`).
- **Search** (`api/search.go`): kind `bayes_model`, the models whose name matches, with the same visibility (the group's
  use read once per request).
- **Recent** (`api/explore_recent.go`): `bayes_models` by owner and `bayes_model_versions` by author in `recentSQL`, a row
  kept only when the person still sees it.
- **The folder**: `folderContents` gains the models.

### «Used by N flows» and the pinned version (answer 7)

The interface with `flow-node`, fixed here: a flow node of type `bayes_model` writes in its `data`
`"bayesModelId": <id>, "bayesModelVersion": <version>` (`flow-node`'s spec confirms it). `bayesmodels.Using(ctx, q, id)`
reads the flows whose definition holds `"bayesModelId"` (the `LIKE` prefilter of `savedscripts.Using`), parses each node
and returns `[]Use{FlowID, FlowName, NodeID, Version}`.

For the notice «a newer version exists», `bayesmodels.Newer(ctx, q, id, pinned)` returns the current version, whether
its semantic hash differs from the pinned one's (a layout-only version is newer but not different: the notice says
nothing then). Every version has a program, so a newer one can always be pinned. The words are `flow-node`'s.

### The compile in the background (answer 8)

```go
// Compiler is what stan-compile gives the store. Enqueue is called after a version's transaction has
// committed; it never blocks the save. State answers for one program, whatever the version that holds it.
type Compiler interface {
	Enqueue(ctx context.Context, r CompileRequest) error
	State(ctx context.Context, programSHA256 string) (CompileState, error)
}

type CompileRequest struct {
	ModelID, Version int64
	ProgramSHA256    string
	By               int64 // the person who saved: the compile Job's authority and the audit's actor
}

type CompileState struct {
	State  string    // "none", "queued", "running", "ready", "failed", "timeout" (Correction of 2026-11-24 from the step-4 specs)
	Detail string    // a sentence for the page (stan-compile's words)
	At     time.Time // when the state was reached
}
```

Until `stan-compile` is built the gateway holds a `Compiler` that enqueues nothing and answers `none`; the page says
«compiled the first time it runs». `stan-compile` reads the program from `bayes_model_versions` by its sha256 (any version
holding it), never from the request: the program the binary comes from is the stored one.

### Restoring a version

A restore writes a new version whose graph, program, `program_sha256`, `codegen_version`, mapping table and manifests are
**the restored version's, copied byte for byte** (owner, 2026-11-23): nothing is validated or generated again, so the
restored version runs exactly what the old one ran, coherent with «a saved version always runs with its saved program».
The compile cache answers at once when the binary is still there; when it was evicted, `Compiler.Enqueue` after the
commit, as for any save. Saving the restored graph again from the editor is what writes it with the current generator.

### The program that runs

A run reads **the pinned version's stored** `program`, `program_sha256`, manifests and mapping; nothing is generated
again (owner). A version whose `codegen_version` is older than the gateway's runs as stored; its page says so as a fact
(«written by the generator of version 1; saving again writes it with version 2»). A version whose graph has an older
`format_version` opens upgraded in the editor and keeps its stored program; saving it writes a new version in the
current format.

## Memory

Graphs, programs and manifests are metadata bounded by the graph's limits; one version is read at a time. The lists
never select `graph`, `program`, `mapping` or the manifests. Nothing here grows with the rows of a flow; the rule of
2026-11-22 concerns data and draws, which this module never touches.

## Tech stack

- **Go** 1.27: `pgx/v5` (already a dependency), `internal/perm`, `internal/audit`, `internal/copynames`,
  `internal/bayesgraph`, `internal/bayescodegen`, `internal/bayescatalog`; no new module.
- **Python** (only the migration): SQLModel and Alembic as they are in `gateway/`; no new package.
- **TypeScript**: `frontend/composables/useBayesModels.ts` (the routes' client, as `useSavedScripts.ts`), the Explore kind,
  the audit labels; no new package.

## Commands

```bash
cd gateway && uv run python -m app.db.migra nuova "bayes models"     # generates 0046 from the models (then read by hand)
cd gateway && uv run python -m app.db.migra controlla                # the database equals the models
cd gateway-go && go test ./internal/bayesmodels/...                  # pure parts
cd gateway-go && scripts/test.sh ./internal/api/ -run 'BayesModel'   # the routes, on a throw-away database (TEST_DATABASE_URL)
cd frontend && node --experimental-strip-types --no-warnings=ExperimentalWarning --test tests/bayes-models.test.mjs
cd frontend && npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
```

## Project structure

```text
gateway/app/models/bayes_model.py                         BayesModel, BayesModelVersion (schema only)
gateway/app/models/__init__.py                            + the import
gateway/app/db/migrations/versions/0046_bayes_models.py   two tables, forward only
gateway-go/internal/bayesmodels/
  bayesmodels.go      Model, Version, Get, CanSee, GetVisible, Visible, CanRun
  save.go             Save (decode, validate, generate, canonical, the transaction), AddVersion
  uses.go             Using, References, Newer
  compile.go          Compiler, CompileRequest, CompileState, the no-op compiler
  *_test.go
gateway-go/internal/api/bayes_models.go                   the routes
gateway-go/internal/api/bayes_models_test.go              on TEST_DATABASE_URL
gateway-go/internal/api/{search.go, explore_recent.go, projects.go}   + the kind
frontend/composables/useBayesModels.ts
frontend/components/ProjectBrowser.vue, frontend/pages/audit.vue       + the kind, the actions
frontend/i18n/locales/*.gen.ts                            bayesModels.*, audit.bayesModel*Action
```

## Code style

A version added in the object's lock, the number taken in the same transaction (verified,
`gateway-go/internal/savedscripts/savedscripts.go`):

```go
func AddVersion(ctx context.Context, tx pgx.Tx, table, versions, fk string, id int64, content pyjson.Object, note string, userID int64) (int64, error) {
	if _, err := tx.Exec(ctx, fmt.Sprintf(`SELECT id FROM %s WHERE id = $1 FOR UPDATE`, table), id); err != nil {
		return 0, err
	}
	var last int64
	err := tx.QueryRow(ctx, fmt.Sprintf(`SELECT version FROM %s WHERE %s = $1 ORDER BY version DESC`, versions, fk), id).Scan(&last)
```

This module's in the same voice (a sketch; lot D1 writes it): the base version checked under the same lock, the stored
columns written once.

```go
// addVersion writes a version of a model. The row is locked first, so that two saves take the next
// numbers in turn and a save made on an older version is refused rather than merged by nobody.
func addVersion(ctx context.Context, tx pgx.Tx, m *Model, base int64, v *Version, userID int64) (int64, error) {
	var current int64
	if err := tx.QueryRow(ctx, `SELECT current_version FROM bayes_models WHERE id = $1 FOR UPDATE`, m.ID).Scan(&current); err != nil {
		return 0, err
	}
	if base != current {
		return 0, &Stale{Current: current}
	}
	next := current + 1
	_, err := tx.Exec(ctx, `INSERT INTO bayes_model_versions (model_id, version, graph, format_version, semantic_hash,
		findings, program, program_sha256, mapping, data_manifest, quantity_manifest, codegen_version,
		catalog_sha256, note, created_at, created_by) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)`,
		m.ID, next, v.Graph, v.FormatVersion, v.SemanticHash, v.Findings, v.Program, v.ProgramSHA256,
		v.Mapping, v.DataManifest, v.QuantityManifest, v.CodegenVersion, v.CatalogSHA256, v.Note, time.Now().UTC(), userID)
```

## Testing strategy

Small by construction: graphs of the corpus and the templates; a throw-away database.

- **Pure** (`internal/bayesmodels`): a save of every template graph gives a version with a program and the golden
  program's sha256; a graph with an error is refused with its findings and no version is written, one with warnings only is saved; canonical-equal bodies
  save nothing; a layout-only change gives a new version with the same semantic hash and program; the stored program is
  read back byte for byte; `Newer` on (same hash, layout only, different); a restore copies the program, its `codegen_version`, mapping and manifests byte for byte.
- **Routes** (`bayes_models_test.go`, on `TEST_DATABASE_URL`): every row of «Who may do what» with a person who has the
  right and one who lacks exactly it (the folder's VIEW, the group's use — not in its user group, the group disabled —,
  EDIT, RUN), the 404 being the same body for «does not exist» and «not visible»; two saves on the same `base_version`
  (one 200, one 409); a body over the bound refused before decoding (413); unknown keys refused (422); delete refused
  while a flow's definition pins it, accepted after the node is removed; restore; duplicate keeps the program's sha256;
  move needs EDIT on both folders; the search, Explore's list and the recent objects show the model to whoever sees it
  and to nobody else; a folder holding a model cannot be deleted; every action is in the audit with no label or graph in
  its detail.
- **The compiler's interface**: a fake `Compiler` records `Enqueue` after the commit, never before; its error does not
  change the save's answer; no `Enqueue` for a version whose program has a binary already; no `Enqueue` for a refused save.
- **Migration**: on a database at 0045, `migra` brings it to 0046 and `migra controlla` passes; the previous version's
  gateway starts on it (the tables are only added).
- **Frontend**: `useBayesModels.ts` against a fake fetch (shapes, the 409 of a stale save); `vue-tsc` green; the Explore
  kind and the audit labels in five languages.
- **Regression**: the saved scripts', search's, Explore's and folders' suites, unchanged.

## Boundaries

- **Always**: the gateway decodes, validates and generates again whatever the browser sends; a version written once and
  never changed; the program run is the stored one; the same 404 for «absent» and «not yours»; audit for every change;
  Go only for the routes.
- **Ask first**: any change of the two tables beyond revision 0046 (approved on 2026-11-23); another visibility rule;
  regenerating stored programs; a flow-group cell for a model.
- **Never**: a program received from the browser; a version rewritten in place; a label or a graph in the audit's
  detail; a Python route, service or code path; a save that waits for a compile.

## Success criteria

1. A template saved, restored and duplicated keeps its program's bytes (and sha256) where the decisions say so.
2. Every row of «Who may do what» passes with the right and fails without it; a model is absent, not greyed, for whoever
   cannot see it.
3. A stale save is refused with the current version named; a canonical-equal save makes no version; a graph with an
   error is refused with its findings and writes no version, one with warnings only is saved; every stored version has a
   program.
4. A model pinned by a flow cannot be deleted; «used by» names the flows the reader may open and counts the others.
5. Saving a version with a new program calls `Compiler.Enqueue` once, after the commit.
6. `migra controlla` passes at 0046; no Python file beyond the model, its import and the revision changes.
7. No new module in `go.mod`, no new package in `package.json`.

## Lots

| Lot | Objective | Files | Accepted when | Depends on | Agent |
|---|---|---|---|---|---|
| **D0** | The SQLModel classes and revision 0046, after the owner's OK | `gateway/app/models/bayes_model.py`, `__init__.py`, `0046_bayes_models.py` | `migra controlla` green on a fresh database and on one at 0045 | approved by the owner (2026-11-23) | sonnet |
| **D1** | `bayesmodels`: visibility, save, versions, restore, duplicate, `Using`, `Newer`, the `Compiler` interface and its no-op | `internal/bayesmodels/` | pure tests green | D0; `model-graph` M2; `stan-codegen` S0 (a no-error graph needs a program) | **opus** (permissions and the stored program) |
| **D2** | The routes, the audit, the folder check, search and recent | `api/bayes_models.go`, `api/search.go`, `api/explore_recent.go`, `api/projects.go`, tests | route tests green | D1 | sonnet |
| **D3** | The client, the Explore kind, the audit page's labels, five languages | `useBayesModels.ts`, `ProjectBrowser.vue`, `audit.vue`, locales | Node tests and `vue-tsc` green; Explore checked by hand on Chrome and Firefox | D2 | sonnet |

D0 is light and approved. D1 can be written against the codegen's interface before S0 is complete (the refusal of graphs
with errors first), and finished with it. `graph-editor`'s first screen does not wait for any of these (it uses local data).

## Corrections made to the map (2026-11-23)

Applied by the owner's decision of 2026-11-23, marked in the map «Correction of 2026-11-23 from the step-3/4 specs»:

- `model-store`'s «Choices»: the version also holds **the two manifests** and the catalogue's sha256 beside the program.
- `model-store`'s «Choices»: the compile starts in the background **only after the version's transaction commits**.
- «What exists and is reused»: the `versioned` **pattern** is reused, not `versioned.Kind` itself.

## Correction of 2026-11-24 from the step-4 specs

From `bayes-stan-compile.md`, `bayes-diagnostics.md` and `bayes-predictive-checks.md`, approved by the owner on
2026-11-24. Where this section and the text above differ, this section holds.

1. **`timeout`.** `CompileState.State` gains `timeout` (a program that passed the compile deadline, or was killed for
   memory, is not compiled again under the same limits: the owner, 2026-11-24); `afterCommit` skips it as it skips
   `ready`, `queued` and `running`, and skips a `failed` the compiler caused; the route `POST …/versions/{v}/compile`
   answers 409 for `timeout` too, with the state's sentence.
2. **The state of the current image.** The list's and the detail's compile state is the state of the program **for the
   group's current image** (`stan-compile`'s cache key holds the image's digest).
3. **`CompileRequest.Again`**: set only by «Compile now», which asks again for a program the compiler refused.
4. **The extension in every stored program.** `Generate(m, bayeschecks.Extension{})` at every save; `CodegenVersion`
   becomes 2; the golden sha256 of the templates' saved programs change once (their tests' fixtures written again and
   reviewed). A version stored by generator 1 runs as stored, without predictive checks.

## Decisions of 2026-11-23 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| A graph with errors | **The save is refused** (against the recommendation): the gateway answers 422 with the findings and creates no version; warnings never block; every saved version has a program; the editor warns before leaving a page with unsaved changes (`graph-editor`, as the flow editor and the script page do) | Owner, 2026-11-23 |
| The program of a restored version | **Copied from the restored version** (against the recommendation), with its generator's version, mapping table and manifests: a saved version always runs with its saved program | Owner, 2026-11-23 |
| The schema migration | **Approved**: revision 0046 with the two tables as specified (`bayes_models`, `bayes_model_versions`) and their SQLModel classes; the table of binaries comes with `stan-compile` | Owner, 2026-11-23 |

## Decisions of 2026-11-24 (mockups)

| Question | Decision | By |
|---|---|---|
| The family in the list | **Derived from the saved graph** (group plates → hierarchical, a series → series, a linear predictor with a GLM likelihood → GLM, otherwise free), stored with the version (`family`, in revision 0046, not built yet); the column added to the approved schema was approved by the owner on 2026-11-24 | Owner, 2026-11-24 |
| A «has errors» state in the list | None: a graph with errors is never saved | Technical, 2026-11-24 |

## Open questions

None.

## Correction of 2026-11-24 from the step-5 specs

From `bayes-flow-node.md`, approved by the owner on 2026-11-24. Where this section and the text above differ, this section
holds.

1. **The lock at a flow's save.** Every write of `flows.definition` — `saveFlow`, `insertFlow` and the promotion of a flow's
   version, each in a transaction — takes `pg_advisory_xact_lock(Lock::integer, id::integer)` for every model the new
   definition pins, **in increasing id order** (two saves never deadlock), checks under it that each pinned version exists,
   and refuses a new or changed pin of a model the saver cannot see (the same sentence for absent and not seen). A `Delete`
   then waits for the commit and finds the flow among the references.
2. **The node's type** is `bayes_model` (`NodeType`); its runs' kind is `model`.
3. **The notice** «a newer version exists» follows `Newer`'s rule (the semantic hash differs); moving the pin keeps the
   bindings that still fit, by data node id.
