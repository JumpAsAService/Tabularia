# Spec: the compile of a saved model's program and the cache of binaries (`stan-compile`)

Status: **written and approved by the owner on 2026-11-24.** It specifies the module `stan-compile` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 4 of its order of construction, after the approved
`bayes-image.md` (**the image spec**), `bayes-stan-codegen.md` (**the codegen spec**), `bayes-contract.md` (**the
contract**) and `bayes-model-store.md` (**the store spec**), and beside `bayes-predictive-checks.md` (draft of the same
day), whose extension is part of every program this module compiles. Nothing of it is built. Decisions marked
**(owner)** are the owner's (the map's «Interview answers», numbered as there: **the answers**, and the corrections the
owner made since); the rest are this spec's, to confirm; those marked «spec decision, 2026-11-24, owner may revisit» are
small choices the owner may change without reopening the module. The owner answered this spec's two questions on
2026-11-24 («Decisions of 2026-11-24»), the schema migration included (approved); no question is open.

Words: **verified** = read in this repository's code on 2026-11-24; **measured** = run by an earlier lot and recorded in
the spec named beside it (this spec measured nothing new); **assumed** = from documentation, not run here, checked by the
lot named beside it.

## Objective

A saved version's program (`bayes_model_versions.program`, never regenerated: the owner's step-2 answer) becomes an
executable that a sampling Job may run, and only that executable:

- **compiled in the background** after the version's transaction commits (answer 8), in a **compile Job** of its own
  (the contract's mode `compile`), with the limits the image spec measured and the owner approved: **1 CPU, 3 GiB, 300 s,
  `/tmp` 512 MiB**; the first run that needs it is the **fallback** (answer 8);
- **one compile per person at a time**, and **a program whose sha256 passed the compile deadline is not compiled again**
  under the same limits (the owner's correction of 2026-11-24 from E4, in the map's `stan-compile`);
- kept in a **cache keyed by the program's sha256, CmdStan's version, the flags, the architecture and the image's
  digest**, evicted least recently used under a **limit the administrator sees and sets** (default **2 GiB**: answer 8,
  the image spec's card);
- **stored in Tabularia's own storage**, never trusted for where it sits: its sha256 comes **only from the compile Job's
  termination message**, is recorded in Postgres, is checked when the binary is kept, when it is copied for a run, and
  by the runner before `exec` (the map, «Integrity»; the contract, `binary_hash`);
- with a **state per program** — `none`, `queued`, `running`, `ready`, `failed`, `timeout` — that the list and the
  detail of the saved models show (`model-store`'s lots D2 and D3), through a real implementation of
  `bayesmodels.Compiler`;
- with the **administration card's compile and cache fields** that lot B0 left out (the image spec, «As built»).

Who it serves: the person who saves a model (the binary is ready when they run it), `flow-node` (asks for the binary
before a sampling Job, and compiles as the fallback), the administrator (limits, cache, «Empty the cache»).

It compiles only what `stan-codegen` wrote and `model-store` stored; it never receives a program from a request.

## Decisions

| Question | Decision | By |
|---|---|---|
| When a program is compiled | **In the background after the version commits** (`bayesmodels.afterCommit` calls `Enqueue`, verified); «Compile now» (RUN) and the first run that needs it are the other two ways in | Owner (answer 8); the store spec |
| The Job | **A compile Job of its own** (mode `compile` of the contract), never inside a sampling Job | The map (provisional decision «A compile Job separate from the sampling Job»), to confirm |
| Its limits | Defaults **1 CPU, 3 GiB, 300 s, `/tmp` 512 MiB**, settings of the card with their defaults visible, applied to the next compile without a build | Owner (approved with `bayes-image.md`) |
| One compile per person | **A person has at most one compile running**; another of theirs waits its turn in the queue, it never starts a second Job | Owner (correction of 2026-11-24 from E4) |
| A program that passed the deadline | **Not compiled again with the same limits**: its state stays `timeout` with the deadline and the date; a new program (a new version whose program differs), or limits the administrator raised, compile anew | Owner (correction of 2026-11-24 from E4) |
| A program that passed the memory limit (killed for memory) | **Treated as the deadline**: state `timeout`, reason «memory», not compiled again until the memory limit is raised | Owner, 2026-11-24 (the rule of the deadline widened to the memory limit) |
| The cache key | sha256 of the program's sha256, CmdStan's version, stanc's arguments, the C++ flags, the architecture and the image's digest («The cache key») | The map; the C++ flags as a constant of the image: the image spec («Open questions» of that spec, for this module) |
| Where a binary lives | **Tabularia's storage** (the engine's bucket, the same storage the flows' outputs are copied into), under the prefix `bayes-binaries/`, **outside the managed prefixes** that a client may name; never the scripts bucket beyond a Job's own step | The map («Integrity»); the prefix: spec decision, 2026-11-24, owner may revisit |
| Which hash is trusted | **Only the termination message** of the compile Job's container; the binary is hashed again by the engine when it is kept and when it is copied for a run, and by the runner before `exec` | The map; the contract |
| The cache's limit | **2 GiB** by default (about 700 binaries of the measured 2.5–3.1 MB), a setting of the card; least recently used evicted when a new binary would pass it | Owner (answer 8); the default: the image spec |
| The order of eviction | Binaries of an image that is no longer the group's current one first, then least recently used | Spec decision, 2026-11-24, owner may revisit |
| Compiles at once in the installation | A setting **«Compiles at once»**, default **2** (2 CPU and 6 GiB at the default limits), beside the per-person rule | Spec decision, 2026-11-24, owner may revisit (the precedent: `MODELS__MAX_CONCURRENT_BUILDS` for the images' builds, verified in `models_build.go`) |
| A queued compile overtaken by a newer save | When a person saves a newer version of the same model while the older version's compile is still **queued** (not running), the older one leaves the queue (its state goes back to `none`: the run compiles it if a flow ever pins it) | Spec decision, 2026-11-24, owner may revisit |
| After a rebuild of the group (a new image digest) | Every binary of the old image is unusable (the image spec: «a binary is valid only with the image that compiled it»); the versions **pinned by flows that have a schedule** are queued in the background, by their schedule's author; every other program compiles at its next save, «Compile now» or run | Spec decision, 2026-11-24, owner may revisit |
| A failure of the cluster (Pod evicted, node lost, image not pulled, the cluster busy) | Not a failure of the program: back to `queued`, at most **3 attempts**, then `failed` with the cluster's reason; «Compile now» queues it again | Spec decision, 2026-11-24, owner may revisit |
| A failure of the compiler (`compile_failed`, `program_hash`, a `CrossCheck` mismatch) | `failed`, «a fault of Tabularia, not of the model» (the contract), **not retried by itself** (the same program on the same image fails the same way); «Compile now» may queue it again | Spec |
| The routes | **Go only**; the engine (Python, `backend/`) gains three internal routes for the binary's bytes, beside the existing `/scripts/objects/upload`, because only the engine holds the buckets' keys | The repository's rule (gateway Python frozen); the engine's routes: spec |
| The schema | **Revision 0047**: `bayes_binaries` and `bayes_settings` («The schema») | Owner, 2026-11-24 (approved) |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| The interface the store calls | `gateway-go/internal/bayesmodels/compile.go`: `Compiler{Enqueue, State}`, `BatchCompiler{States}`, `CompileRequest{ModelID, Version, ProgramSHA256, By}`, `CompileState{State, Detail, At}`, the states `none`, `queued`, `running`, `ready`, `failed`, `NoCompiler`, `afterCommit` (no `Enqueue` when the state is `ready`, `queued` or `running`; an error logged, never failing the save), `States` | Implemented by `bayescompile.Compiler`; a state `CompileTimeout` (`"timeout"`) added; `afterCommit` also skips `timeout` (the owner's rule) |
| Where the gateway takes it | `internal/api/api.go`: `Deps.BayesCompiler bayesmodels.Compiler` («until it is built, one that enqueues nothing»); `internal/api/bayes_models.go`: `bayesCompileOut{State, Detail, At}` in the list and the detail, `compileBayesModelVersion` (`POST …/versions/{v}/compile`: RUN, 409 while queued or running, audit `bayes_model.compile`) | Set at start-up when the group's machinery is on; the route answers 409 for `timeout` too, with the deadline |
| The client and the editor's words | `frontend/composables/useBayesModels.ts`: `BayesCompileState = 'none' \| 'queued' \| 'running' \| 'ready' \| 'failed'`; the list's words in `bayes-graph-editor.md` («compiled, compiling, to compile, failed with its detail») | Gains `'timeout'` and its words («Correction proposed») |
| The task of a compile | `internal/bayescontract/task.go`: `ModeCompile`, `Task.Check(ModeCompile, …)` reads the model alone (no sampler, no manifest, no binary sha256); `testdata/tasks/compile.json`; `modelgroups.Task.Bayes` | Written by this module for every compile Job |
| The runner's compile mode | The contract, «The program and the binary (`compile`)»: `program.stan` from the ConfigMap checked against `program_sha256`, `env -i PATH=… make -C /opt/cmdstan /tmp/c/model PRECOMPILED_HEADERS=false`, `stanc --info`, the binary hashed and uploaded in 8 MiB chunks to `TABULARIA_BINARY_OUT`, `artifacts/compile.json`, the termination message; refusals `program_hash`, `compile_failed` (lot K3 of the contract) | Used as it is: this module starts the Job and reads what it leaves |
| What the image guarantees | The image spec, «The contract of the image»: empty environment, no variable on make's command line but `PRECOMPILED_HEADERS=false`, the flags the image's alone, byte-identical binaries for the same program on the same image, «a binary is valid only with the image that compiled it» | The cache key; no flag ever passed by this module |
| The measured costs | The image spec: a compile 10–28 s with the header, peaks 1.3–1.9 GiB, binaries 2.5–3.1 MB, `/tmp` 2.5–3.0 MB after a compile; the codegen spec, lot S5: the 1,497-node budget 130.5 s and 1.63 GB with 1 CPU and 3 GiB | The defaults' margins; the cache's capacity |
| The defaults' home | `internal/modelgroups/groups.go`: `Recipe.Compile *CompileLimits{CPU, Memory, Seconds, Tmp}`, `Recipe.Cache *CacheLimits{MaxBytes}`, checked at load; `groups/bayes/recipe.json` holds `"compile": {"cpu": "1", "memory": "3Gi", "seconds": 300, "tmp": "512Mi"}`, `"cache": {"max_bytes": 2147483648}` | The card shows them as the defaults; the administrator's values live in `bayes_settings` |
| A model Job | `internal/api/jobs_models.go`: `modelJobManifest` (image by digest, uid 10001, read-only root, no capability, no ServiceAccount token, `/tmp` an `emptyDir` with a `sizeLimit`, the node selector of the image's architecture, the offline label), `jobManifestOf` (`backoffLimit` 0, `activeDeadlineSeconds`), `ReadyModelGroupImage` and `ModelGroupImage{Image, Digest, Arch, RecipeHash, RunnerHash}` (`models_groups_image.go`) | The compile Job's manifest, with the compile limits instead of the sampling ones |
| A background loop with Jobs and a state in Postgres | `internal/api/jobs_script_builds.go`: `RegisterJob("script_builds_loop", …)`, one round every `SCRIPTS__POLL_SECONDS` under the transaction's advisory lock, «a Job's name is fixed by its environment and version, so a process that dies halfway is replaced by another that picks up the same Job»; `mgBuildSlots` counts the running builds in the round's transaction | The compile loop, the same shape: its own lock, the Job's name fixed by the compile's id |
| A Job's end | `internal/kube/jobs.go`: `JobResult` (the log's tail, the exit code, the runtime's reason, `StoppedByCluster`: `Evicted`, `DisruptionTarget`); `JobPods` | Reused; a helper `TerminationMessage(pod)` added (the container's `state.terminated.message`, which `JobResult` does not read today) |
| Bytes between the buckets, never through the gateway | `backend/app/api/routes/scripts.py`: `/scripts/output/import` (scripts bucket → a temporary file → Tabularia's storage with `get_storage_service().upload_file`), `/scripts/input/export` (Tabularia's storage → the scripts bucket), `/scripts/objects/upload` (bytes hashed while received, written only when size and sha256 are the expected ones), `/scripts/objects/delete` | Three routes in the same file and voice («The binary's way») |
| What a client may name | `internal/objects/objects.go`: `ManagedPrefixes = {"datasets/", "out/", "raw/", "cache/"}`, «everything else cannot be addressed by the clients» | `bayes-binaries/` is not added to the list |
| The program's second check | `bayescodegen.CrossCheck(info, p *Program) []Mismatch` (lot S3: stanc's `--info` against the manifests and the catalogue's allow-list) | Run by the gateway on the `info` of `compile.json` before the binary is kept |
| Settings with defaults on the card | `internal/api/models_admin.go`: `PUT /admin/models/{group_id}/settings` (`cpu_limit`, `memory_limit`, `max_timeout_seconds`, `max_rows`), audited `models.group_settings`, observers read; `frontend/components/admin/ModelsSection.vue` | A second settings route for the group `bayes` only, its fields on the same card |

## The cache key

```text
bayes-binary/1
program=<program_sha256>
cmdstan=2.40.0
stanc=
cxx=image
arch=amd64
image=sha256:<the group version's image digest>
```

`cache_key` = the sha256 of these seven lines (each ending with `\n`), written by one Go function. `stanc=` is empty
because stanc runs with make's own arguments and no flag (the image spec, «The contract of the image»); `cxx=image`
because the C++ flags are the image's alone (`STAN_THREADS` from `make/local`, `-O3`, the precompiled header built with
them) and the image's digest already names them; `arch` is `ModelGroupImage.Arch` (`amd64` when empty: the image spec
claims arm64 only after its self-test). A change of any line — a new CmdStan, a rebuild, the other architecture —
is a new key, so a binary is never run with an image other than its own (the image spec, «Never»).

The first line is the key's format: a later change of what the key holds changes it, and every old key simply stops
matching (their binaries age out of the cache).

## The states of a program's compile

`Compiler.State(programSHA256)` answers for the **group's current image** (its key): the row of `bayes_binaries` with
that key, or `none`.

| State | When | `Detail` (the page's sentence, five languages) |
|---|---|---|
| `none` | No row: never asked, evicted, a new image, or a queued compile overtaken | «Compiled the first time it runs.» (the store spec's words) |
| `queued` | Waiting for a slot, or for this person's running compile | «Waiting to compile: another compile of yours is running.» / «Waiting for a free place to compile (2 at once).» |
| `running` | Its Job exists | «Compiling since 14:02 (usually 10–30 s).» |
| `ready` | Kept, hashed, recorded | «Compiled on 23 Nov at 14:02 in 12 s.» |
| `failed` | The compiler refused it, the cross-check found a mismatch, the binary did not reach Tabularia's storage, or the cluster failed it three times | «The generated program did not compile: this is a fault of Tabularia, not of the model. The administrator's log says where.» / the cluster's reason |
| `timeout` | Its Job passed the compile deadline or was killed for memory (the owner, 2026-11-24) | «The program did not compile within the limit of 300 s (23 Nov): it is not compiled again with this limit. A simpler model, or a longer limit set by an administrator, compiles anew.» |

`At` is when the state was reached. The words of the list and the editor are `graph-editor`'s («Correction proposed»
adds `timeout`: «too long to compile»).

**Transitions**: `none → queued` (`Enqueue`); `queued → running` (the loop starts its Job); `running → ready | failed |
timeout`; `running → queued` (the cluster failed it, attempts left); `ready → none` (evicted, emptied); `failed → queued`
(«Compile now»; a run or a new save only when the reason was the cluster's); `timeout → queued` **only** when the current
limits differ from the ones the row records (`deadline_seconds`, `memory_bytes`) by a larger deadline (or memory, under
the owner, 2026-11-24): the owner's rule. `queued → none`: overtaken (below).

## The queue

`Enqueue(r)` writes or updates the row of the key in one transaction (the row `FOR UPDATE`):

1. `ready`, `queued`, `running`: nothing (the cache answers or a compile is on its way).
2. `timeout` with the same or lower limits: nothing; the caller reads the state and says the sentence. `failed` for a
   reason of the compiler: nothing, unless the request is «Compile now» (`CompileRequest` gains `Again bool`, set only by
   the route).
3. Otherwise the row becomes `queued`, `requested_by` = `r.By`, `model_id`/`version` = the request's, `queued_at` = now.
4. **Overtaken**: any other row `queued` (not `running`) whose `requested_by` is the same person and whose `model_id` is the
   same model, with a lower `version`, goes back to `none` (its row is deleted). A different model of the same person
   stays in the queue: «one compile per person at a time» orders them, it does not drop them.

**The loop** (`bayes_compile_loop`, `RegisterJob`, one round every `SCRIPTS__POLL_SECONDS`, the transaction's advisory
lock of its own, asked once):

- for each `running` row: follow its Job («Finishing a compile»);
- free places = «Compiles at once» − rows `running`;
- take the `queued` rows in `queued_at` order, **skipping a person who has a row `running`** (the owner's rule), until the
  places are used; each: the group still usable (enabled and ready: `ReadyModelGroupImage`), the image still the
  key's (a rebuild meanwhile makes the row `none`), the program read **from `bayes_model_versions` by its sha256** (the
  store spec: never from the request), the Job created, the row `running` with `job_name` and `started_at`.

A Job's name is fixed by the row's id (`bayes-compile-<id>-<attempt>`), so a gateway that dies halfway is replaced by
another that finds the same Job, as the build loop does.

**The run's fallback** (answer 8): `flow-node` calls `Enqueue` with the run's authority as `By` when the state is `none`
or `failed` for the cluster's reason (`cluster`), then waits for the row while the step's timeout lasts, saying «compiling the model first (usually 10–30 s)»
on the run page; `timeout`, and `failed` for a reason of the compiler (`compile_failed`, `program_hash`, `crosscheck`,
`too_large`), fail the step at once with the state's sentence (the same program on the same image would fail the same
way). The step's waiting counts in its own
timeout; the compile's limits are the compile's.

## The compile Job

`modelJobManifest`'s shape with the compile limits:

| Field | Value |
|---|---|
| Image | the group version's image **by digest** (`ModelGroupImage.Image`) |
| Command | the runner (`python -I -B /tabularia/code/runner/run.py`), mode `compile` |
| ConfigMap | the runner's files, `task.json` (`modelgroups.Task` with `Bayes` = `bayescontract.Task{Model: {ID, Version, SemanticHash, ProgramSHA256, CodegenVersion, CmdStan}}`, checked by `Task.Check(ModeCompile, "")`), and `program.stan` — the stored program's exact bytes |
| Resources | CPU limit = «Compile CPU» (1), request the same (a compile is one process: the image spec); memory limit = «Compile memory» (3 GiB), request the same: a compile that starts must not be the first to be evicted |
| `activeDeadlineSeconds` | «Longest compile» (300) |
| `/tmp` | `emptyDir` with `sizeLimit` «Compile /tmp» (512 MiB), executable (the image spec, «What fails», 3) |
| Isolation | uid 10001, read-only root, no capability, no ServiceAccount token, `seccompProfile: RuntimeDefault`, the offline label (no network), as every model Job |
| Environment | `TABULARIA_BINARY_OUT` = `bayes-compile/<row id>-<attempt>/model` in the scripts bucket, the bucket's key (the model Jobs' secret: the runner writes there) |
| `ids` of `task.json` | **empty**: a compile is not a step of a flow («Corrections proposed» to the contract's example) |
| `limits` of `task.json` | `time_budget_seconds` = the deadline, `memory_bytes` = the compile memory, `tmp_bytes` = the compile `/tmp`, `started_unix` |
| Container | `terminationMessagePolicy: File` (Kubernetes' default, written explicitly), never `FallbackToLogsOnError`: the message is the file the runner writes, never a log line a program could print |

## Finishing a compile

When the Job has ended (`JobResult`, then the Pod's container status):

| What the Job left | Outcome |
|---|---|
| Exit 0, a termination message `{"program_sha256", "binary_sha256", "info_sha256"}` (64 hex digits each, at most 4,096 bytes) whose `program_sha256` is the row's | **Keep** (below) |
| Exit 3 with `compile_failed` or `program_hash` (`artifacts/error.json`) | `failed`, reason the code; the last 40 lines of `make.log` from the log in the gateway's log (never on the page: they name our program's internals) |
| The Job's condition `DeadlineExceeded` | `timeout`, reason `deadline`, the row's `deadline_seconds` and `memory_bytes` recorded |
| The container `OOMKilled` (exit 137) | `timeout`, reason `memory`, the row's limits recorded (the owner, 2026-11-24) |
| `Evicted`, `DisruptionTarget`, a Pod never scheduled or never pulled, the cluster busy | back to `queued` (attempt + 1), `failed` after the third with the cluster's reason |
| Exit 0 without a valid message, any other exit | `failed`, reason `job` (a fault of Tabularia) |

**Keep**:

1. `artifacts/compile.json` is read from the step's prefix through the engine (`/scripts/objects/read`); its sha256 must
   equal `info_sha256` of the message — the file sits in the shared bucket, so its content counts only once the message
   vouches for it; its `info` goes through `bayescodegen.CrossCheck(info, program)` with the stored manifests; a mismatch
   is `failed` (reason `crosscheck`, the mismatches in the gateway's log).
2. The engine's `POST /scripts/binary/keep` copies `TABULARIA_BINARY_OUT` into Tabularia's storage at
   `bayes-binaries/<binary_sha256>`, hashing while it reads; it writes nothing unless the size is at most
   `binary_max_bytes` (64 MiB, a constant: 20 times the largest measured binary) and the sha256 is the message's.
3. The cache makes room («The cache») and the row becomes `ready` with `binary_sha256`, `binary_bytes`, `info_sha256`,
   `warnings` (stanc's, at most 50 lines, shown to the administrator only), `seconds`, `finished_at`, `last_used_at`.
4. The step's files leave the scripts bucket by exact key (`/scripts/objects/delete`), whatever the outcome.

The steps are idempotent by the row's id: a gateway that dies between 2 and 3 finds the object already kept (the engine
answers that the key exists with that sha256) and goes on.

## The binary's way, and its integrity

```text
compile Job ── writes ──▶ scripts bucket, bayes-compile/<id>-<n>/model        (anyone with the bucket's key could write here)
            └─ termination message: binary_sha256  ──▶ gateway ──▶ Postgres (bayes_binaries.binary_sha256)
engine /scripts/binary/keep:  scripts bucket ──hash──▶ Tabularia's storage bayes-binaries/<sha256>   (only if equal)
engine /scripts/binary/place: Tabularia's storage ──hash──▶ scripts bucket, <step prefix>/model        (only if equal)
runner (sampling Job): download in chunks, hash, compare with task.json's binary_sha256, then chmod 0700 and exec
```

- **The trusted hash** is the termination message's: the Kubernetes API holds it, and only the compile container writes it
  (the contract, «The termination message»; that it is writable with a read-only root is assumed there and checked by
  its lot K3, and again by lot C5 here on kind).
- **Tabularia's storage** is the engine's bucket. Its key is the engine's alone (no Job holds it: verified, the model Jobs
  receive the scripts bucket's key only), and `bayes-binaries/` is outside `objects.ManagedPrefixes`, so no request of a
  client can name it.
- **Three checks of the same hash**: when the engine keeps the binary, when it places it for a run, and in the runner
  before `exec` (`binary_hash`). A binary changed in Tabularia's storage (a fault of the storage) is refused at the second
  check: the row is deleted, the object too, and the run compiles again (state `none` → `queued`).
- The gateway never holds a binary's bytes: the engine streams them through a temporary file of its own (3 MB).

### The engine's three routes (internal, the gateway only)

| Route | Body | Does |
|---|---|---|
| `POST /scripts/binary/keep` | `{connection, key, sha256, max_bytes, bucket}` | scripts bucket `key` → temporary file, hashed; above `max_bytes` or another sha256: 409, nothing written; equal: uploaded to `bucket` (Tabularia's) at `bayes-binaries/<sha256>`; an existing object of that key is read and hashed first, and kept when equal (idempotent) |
| `POST /scripts/binary/place` | `{connection, sha256, bucket, target_key}` | Tabularia's `bayes-binaries/<sha256>` → temporary file, hashed; another sha256: 409 (`{"altered": true}`), nothing placed; equal: uploaded to the scripts bucket at `target_key` |
| `POST /scripts/binary/delete` | `{bucket, sha256s}` | the objects removed from Tabularia's storage (eviction, «Empty the cache») |

They live in `backend/app/api/routes/scripts.py` beside `/scripts/objects/upload`, in its voice; they read and write
only the prefix `bayes-binaries/` and a key of the form `bayes-compile/…` or a step's prefix, refusing any other.

## A run (what `flow-node` calls)

```go
// Binaries is what a sampling step asks before its Job: the binary of a stored program for the
// group's current image, and its copy into the step's prefix.
type Binaries interface {
	// Ready is the binary when the state is ready, nil and the state otherwise.
	Ready(ctx context.Context, programSHA256 string) (*Binary, bayesmodels.CompileState, error)
	// Place copies the binary into the scripts bucket at key, the engine hashing it on the way, and
	// records the use (least recently used). ErrAltered: the stored object was not the binary.
	Place(ctx context.Context, b *Binary, key string) error
}

type Binary struct {
	CacheKey, BinarySHA256 string
	Bytes                  int64
}
```

`flow-node` writes `BinarySHA256` into `task.json` (`bayes.model.binary_sha256`) and `TABULARIA_BINARY_IN` = the key;
nothing else of this module reaches the sampling Job.

## The cache

- **The limit**: «Cache of binaries» (default 2 GiB, `recipe.json`'s `cache.max_bytes`), counted on `binary_bytes` of
  the `ready` rows.
- **Least recently used**: `last_used_at` is set when a binary is kept and at every `Place`. When a binary is kept and
  the sum would pass the limit, `ready` rows are evicted — those of an image that is no longer the group's current one
  first, then by `last_used_at` — until it fits; each eviction deletes the row and, when no other row holds the same
  `binary_sha256`, the object (`/scripts/binary/delete`). The row of a binary being placed is locked `FOR SHARE` during
  `Place`, so it is never evicted under a run that is copying it; a run that finds the row gone asks again.
- **A binary larger than the whole limit** is not kept: `failed`, «the cache's limit (2 GiB) is smaller than this model's
  binary (…): an administrator can raise it».
- **Lowering the limit** evicts at the loop's next round. **«Empty the cache»** (with a confirmation) evicts every `ready`
  row; `timeout` and `failed` rows stay (they hold no binary and keep the owner's rule).
- `timeout` and `failed` rows hold no bytes and are never evicted; they go when their image is no longer current and no
  version holds their program (the loop's cleaning, once a day).

## The state in the list and the detail (lots D2 and D3 of the store)

- `bayescompile.Compiler` implements `Compiler` and `BatchCompiler`: `States(programs)` is **one** `SELECT … WHERE
  cache_key = ANY($1)` over the keys of the current image, so the list asks once (the store's `States`, verified).
- `Deps.BayesCompiler` is set when the scripting machinery is on (Kubernetes); otherwise `NoCompiler` stays, and the page
  says «compiled the first time it runs» as today.
- `POST …/versions/{v}/compile` answers 409 for `timeout` too, with the state's sentence (the deadline and the date).
- `bayesmodels.afterCommit` skips `timeout` as it skips `ready`, `queued` and `running`.
- The client's `BayesCompileState` gains `'timeout'`; the list and the editor show it in `danger` with the sentence
  (`graph-editor`'s words, «Corrections proposed»).

## The administration card (the fields B0 left out)

On the group `bayes`'s card in **Administration → Statistical models** (the image spec, «The administration card»):

- **Compile limits**: CPU (default **1**), memory (default **3 GiB**), longest compile (default **300 s**), `/tmp`
  (default **512 MiB**), «Compiles at once» (default **2**); each with its default written beside it and «Reset to
  default»; applied to the next compile, no build. A memory below 2 GiB or a deadline below 60 s is refused with the
  measured peaks and times quoted (the image spec: 1.3–1.9 GiB, 11–45 s; the 1,497-node budget 130.5 s).
- **Cache of binaries**: the limit (default **2 GiB**), the space used and the number of binaries, the date of the oldest
  use, the binaries of an older image if any; **Empty the cache** (confirmation: «the next run of each model compiles
  again»).
- **The sampling Job's `/tmp`** (default **2 GiB**, the image spec and the contract): stored in the same settings row,
  read by `flow-node` into `task.json`'s `tmp_bytes`.
- **Compiles**: the last 20 rows (model and version when the reader may see them, state, seconds, when), and the
  `timeout` rows with their limits — what an administrator needs before raising a limit.

Routes (Go, `models_admin.go`'s guard: administrators act, observers read, never an API token):
`GET /admin/models/bayes/compile` (settings with defaults, cache use, last compiles), `PUT /admin/models/bayes/compile`
(the settings; audit `models.bayes_compile_settings` with before and after), `POST /admin/models/bayes/cache/empty`
(audit `models.bayes_cache_empty` with the count and bytes). Texts in the five languages.

## The schema (revision 0047, approved by the owner on 2026-11-24)

Only additions (expand/contract, `docs/database-migrations.md`): two new tables, no existing table or column changes.

`bayes_binaries`:

| Column | Type | |
|---|---|---|
| `id` | integer, primary key | |
| `cache_key` | char(64), not null, unique | «The cache key» |
| `program_sha256` | char(64), not null, indexed | |
| `cmdstan` | text(16), not null | `2.40.0` |
| `image_digest` | text(71), not null, indexed | `sha256:…` |
| `arch` | text(16), not null | |
| `group_version` | integer, not null | the group's version whose image compiled it |
| `state` | text(8), not null | `queued`, `running`, `ready`, `failed`, `timeout` |
| `reason` | text(20) | `compile_failed`, `program_hash`, `crosscheck`, `deadline`, `memory`, `cluster`, `job`, `too_large` |
| `detail` | text | the sentence (English; the page translates by `reason`) |
| `requested_by` | integer, → `users.id` | the person whose turn it counts in (the saver, the runner, the schedule's author) |
| `model_id`, `version` | integer | the request that queued it (for the card and the overtaking rule; a program may be held by several versions) |
| `attempt` | integer, not null, default 0 | |
| `job_name` | text(63) | |
| `deadline_seconds`, `memory_bytes` | integer, bigint | the limits it ran under (the owner's rule on `timeout`) |
| `binary_sha256` | char(64) | from the termination message |
| `binary_bytes` | bigint | |
| `info_sha256` | char(64) | |
| `warnings` | text | stanc's lines (JSON, at most 50) |
| `seconds` | double | the compile's time |
| `queued_at`, `started_at`, `finished_at`, `last_used_at` | timestamp | naive UTC, as every table of the gateway |

`bayes_settings` (one row, `id = 1`; a null column is the recipe's default): `compile_cpu` text(8), `compile_memory`
text(16), `compile_seconds` integer, `compile_tmp` text(16), `compile_slots` integer, `cache_max_bytes` bigint,
`sampling_tmp` text(16), `updated_by` text(320), `updated_at` timestamp.

**The Python side**: `gateway/app/models/bayes_binary.py` with the two SQLModel classes («only the Go gateway reads and
writes these tables», as `model_group.py` says), its import, and `0047_bayes_binaries.py` generated from them
(`python -m app.db.migra nuova "bayes binaries"`), read by a person, forward only. No other Python file of the gateway
changes.

## Security

| Threat | Answer |
|---|---|
| A poisoned binary in the shared scripts bucket (the map) | Its hash comes from the termination message only; kept only when equal; placed only when equal; the runner checks again before `exec` |
| A poisoned `compile.json` (its `info` read by the cross-check) | Its sha256 must equal the message's `info_sha256` before it is read as JSON |
| A program that is not the stored one | The loop reads it from `bayes_model_versions` by sha256; the runner checks `program.stan` against `program_sha256` (`program_hash`) |
| A compile that eats the node | The Job's CPU, memory, deadline and `/tmp`; the graph's limits bound the program (`max_program_vector_nodes` 1,500 compiles in 130.5 s, the codegen spec) |
| Compiles that eat the cluster | One per person, «Compiles at once», `timeout` never retried under the same limits |
| A person learning another's model through the card or a state | The card names a model only to a reader who sees it (`bayesmodels.CanSee`); a state answers for a program's sha256, which only a reader of a version holding it knows |
| A binary run in another image | The image's digest is in the key; the runner's image is the key's (the image spec) |
| Flags, include paths, a person's text in make | None: the contract's fixed command, `env -i`, no variable but `PRECOMPILED_HEADERS=false` |

## Memory

A compile's memory is the Job's (3 GiB by default); the gateway holds rows, never bytes; the engine streams a binary
through a temporary file (3 MB measured, at most `binary_max_bytes`). The rule of 2026-11-22 (never everything in RAM)
concerns data and draws, which this module never touches.

## Tech stack

- **Go** 1.27, standard library and `pgx/v5` (already dependencies): `internal/bayescompile` (new), `internal/api`
  (the loop, the routes), `internal/kube` (one helper); no new module.
- **Python** (the engine, `backend/`): three routes in `app/api/routes/scripts.py` with `boto3` and `hashlib` as the file
  already uses them; no new package. The gateway's Python: the migration only.
- **TypeScript**: `useBayesModels.ts` (the state `timeout`), `ModelsSection.vue` (the fields), locales; no new package.

## Commands

```bash
cd gateway-go && go test ./internal/bayescompile/... ./internal/bayesmodels/... ./internal/kube/...
cd gateway-go && scripts/test.sh ./internal/api/ -run 'BayesCompile|BayesModel|ModelsAdmin'   # on TEST_DATABASE_URL
cd gateway && uv run python -m app.db.migra nuova "bayes binaries" && uv run python -m app.db.migra controlla
cd backend && pytest tests/test_scripts_binary.py -q                                            # no -n on this machine
cd frontend && node --experimental-strip-types --no-warnings=ExperimentalWarning --test tests/bayes-models-client.test.mjs
cd frontend && npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
```

## Project structure

```text
gateway/app/models/bayes_binary.py                          BayesBinary, BayesSettings (schema only)
gateway/app/db/migrations/versions/0047_bayes_binaries.py   two tables, forward only
gateway-go/internal/bayescompile/
  key.go          CacheKey(program, image): the seven lines and their sha256
  state.go        the states, the transitions, the sentences' reasons; Overtaken, MayRetry (the owner's rule)
  queue.go        Enqueue, the next rows to start (one per person, the places)
  cache.go        the room to make (eviction order), the limit
  compiler.go     Compiler, BatchCompiler and Binaries over the database
  *_test.go
gateway-go/internal/api/jobs_bayes_compile.go               the loop: start, follow, finish, keep, clean
gateway-go/internal/api/models_admin_bayes.go               the card's routes
gateway-go/internal/kube/jobs.go                            + TerminationMessage
backend/app/api/routes/scripts.py                           + /scripts/binary/keep, place, delete
backend/tests/test_scripts_binary.py
frontend/composables/useBayesModels.ts, frontend/components/admin/ModelsSection.vue, frontend/i18n/locales/*.gen.ts
```

## Code style

The store's side, as it is (verified, `internal/bayesmodels/compile.go`): a compile is asked after the commit, and only
when nothing is on its way.

```go
func afterCommit(ctx context.Context, c Compiler, log *slog.Logger, r CompileRequest) bool {
	if c == nil {
		return false
	}
	s, err := c.State(ctx, r.ProgramSHA256)
	if err != nil {
		logError(log, "bayes models: the compile state", err, r)
	} else {
		switch s.State {
		case CompileReady, CompileQueued, CompileRunning:
			return false
		}
	}
```

This module's in the same voice (a sketch; lot C1 writes it): the owner's rule is one function, tested alone.

```go
// MayRetry says whether a program that passed a limit may be compiled again: only when the limit it
// passed is larger now. A deadline passed at 300 s is not tried again at 300 s, nor at 200 s.
func MayRetry(row Row, now Limits) bool {
	switch row.Reason {
	case ReasonDeadline:
		return now.DeadlineSeconds > row.DeadlineSeconds
	case ReasonMemory:
		return now.MemoryBytes > row.MemoryBytes
	}
	return true
}
```

## Testing strategy

Small by construction: programs of the codegen's golden corpus, a fake cluster, a fake engine, a throw-away database.

- **Pure** (`bayescompile`): the key's seven lines and sha256 for a fixed input (golden), a change of each line changes it;
  every transition of the states' table, and none other; `MayRetry` for deadline and memory, raised, equal, lowered;
  the queue's order with three people and two places (a person never has two running; the order is `queued_at`);
  overtaking (an older queued version of the same model leaves, a running one does not, another model of the same person
  stays); the eviction order (old image first, then least recently used; a locked row is skipped; a binary larger than
  the limit is refused).
- **The loop** (`jobs_bayes_compile_test.go`, on `TEST_DATABASE_URL`, the cluster played as `modelAttemptJob` is in the
  model tests): every row of «Finishing a compile» — a message with another `program_sha256`, a message longer than
  4,096 bytes or not hex, `compile.json` whose sha256 differs from `info_sha256`, a `CrossCheck` mismatch, the engine
  refusing the keep (another sha256), `DeadlineExceeded`, `OOMKilled`, `Evicted` three times; a gateway that dies after
  the keep and before the row (the second round finishes it once); the step's files deleted in every case; the Job's
  manifest (limits, deadline, `/tmp`, isolation, `terminationMessagePolicy: File`, empty `ids`, the program's bytes in the
  ConfigMap equal to the stored ones).
- **The store's interface**: a save calls `Enqueue` once after the commit (the store's test, with the real compiler on the
  database); a `timeout` row makes `afterCommit` skip and the route answer 409 with the sentence; the list asks `States`
  once for twenty models.
- **The engine** (`backend/tests/test_scripts_binary.py`, a fake S3): keep refuses another sha256 and an oversized file and
  writes nothing; place refuses an altered stored object; delete removes only `bayes-binaries/` keys; a key outside the
  allowed forms is refused.
- **The card**: the routes' rights (administrator acts, observer reads, a token refused), the refusals below the measured
  floors, the audit's before and after; `ModelsSection.vue`'s fields in five languages, checked by hand on Chrome and
  Firefox.
- **On kind, by hand, one heavy job at a time** (lot C5): enable the group; save a template; the compile Job in a real Pod
  (the termination message read with a read-only root, `/tmp` executable); the binary in Tabularia's storage; a byte
  changed by hand in the scripts bucket after the compile is never kept; a byte changed in Tabularia's storage is refused
  at the place; two saves of one person queue, a third person's runs beside; a program made slow on purpose (the
  1,497-node budget with the deadline lowered to 60 s) ends `timeout` and is not compiled again until the deadline is
  raised.
- **Regression**: the store's suites, the model Jobs' tests, `models_admin` tests, every other group's bytes unchanged.

## Boundaries

- **Always**: compile only a stored program, read by its sha256; the image by digest; the contract's fixed command; the
  hash from the termination message only; three checks of it; the key with the image's digest; one compile per person;
  `timeout` never retried under the same limits; every limit on the card with its default visible; the step's files
  deleted; Go for every route of the gateway.
- **Ask first**: any change of the two tables beyond revision 0047; a flag of the compiler (the image spec's «Ask
  first»); another place for the binaries; retrying a deterministic failure by itself; compiling in a sampling Job.
- **Never**: a program from a request; a binary trusted for where it sits; a binary run with an image other than its key's;
  `FallbackToLogsOnError`; a model's name or a person's text in the Job's command, environment or make's arguments; a
  route of the gateway in Python; the binary's bytes held by the gateway.

## Success criteria

1. Saving a template queues one compile after the commit; on kind its Job ends `ready` within the measured times, the
   binary is in Tabularia's storage under its sha256, and the list shows «compiled».
2. A binary whose bytes differ from the termination message's sha256 is never kept; a stored binary altered afterwards is
   never placed; a placed binary altered in the scripts bucket is never executed (the contract's `binary_hash`).
3. A person never has two compile Jobs at once; «Compiles at once» is never passed, with two gateways running.
4. A program that passed the deadline stays `timeout` across saves, «Compile now» and runs until the deadline is raised;
   then it compiles once more.
5. The cache never holds more than its limit after a round; the oldest use is evicted first, an older image's binaries
   before any; «Empty the cache» leaves no binary and no `ready` row.
6. The card shows the compile limits, «Compiles at once», the cache's limit, use, count and oldest use, each default
   visible; every change is audited.
7. `migra controlla` passes at 0047; no Python file of the gateway beyond the model, its import and the revision changes;
   no new module in `go.mod`, no new package in `package.json` or the engine's requirements.

## Lots

| Lot | Objective | Files | Accepted when | Depends on | Agent |
|---|---|---|---|---|---|
| **C0** | The SQLModel classes and revision 0047 (approved by the owner on 2026-11-24) | `bayes_binary.py`, `0047_bayes_binaries.py` | `migra controlla` green on a fresh database and on one at 0046 | approved by the owner (2026-11-24) | sonnet |
| **C1** | `bayescompile`: the key, the states, the queue, the cache, the owner's rule | `internal/bayescompile/*` | pure tests green | — | **opus** (the owner's rules and the integrity) |
| **C2** | The loop: the Job, its end, the keep, the cleaning; `kube.TerminationMessage`; the engine's three routes | `jobs_bayes_compile.go`, `kube/jobs.go`, `scripts.py`, tests | loop and engine tests green | C0, C1; the contract's K3 (the compile mode) | **opus** (integrity, concurrency of gateways) |
| **C3** | The real `Compiler` and `Binaries`; `Deps.BayesCompiler`; the route's 409 for `timeout`; `afterCommit`; the client's state | `compiler.go`, `bayes_models.go`, `compile.go`, `useBayesModels.ts`, locales | route and client tests green | C2 | sonnet |
| **C4** | The card's fields and routes | `models_admin_bayes.go`, `ModelsSection.vue`, locales | route tests; Chrome and Firefox by hand | C0 | sonnet |
| **C5** | The trial on kind, by hand, alone | «As built» | success criteria 1–5 on kind | C3, C4; `predictive-checks`' lot P1 (the programs it compiles hold the extension) | sonnet (by hand) |

C1 can start at once; C0 waits for the owner. C5 is the only heavy lot.

## Corrections made to the map and to the approved specs (2026-11-24)

Applied on the owner's approval of 2026-11-24, each marked «Correction of 2026-11-24 from the step-4 specs» where it
lands:

- **`bayes-model-store.md`**, «The compile in the background»: `CompileState.State` gains `timeout`; `afterCommit` skips
  it; the route `POST …/compile` answers 409 for `timeout` too. And «The routes»: the detail's compile state is the
  state **for the group's current image**; `CompileRequest` gains `Again` («Compile now» asks again for a program the
  compiler refused).
- **`bayes-graph-editor.md`**, «The list» and the mockups' list: a fifth word for `timeout` («too long to compile», in
  `danger`, with the sentence).
- **`bayes-contract.md`**, `testdata/tasks/compile.json` and «The program and the binary (`compile`)»: a compile's `ids`
  are empty (a compile is not a step), and its `limits` are the compile's (`time_budget_seconds` the deadline,
  `memory_bytes` and `tmp_bytes` the compile's), not the sampling ones of the example.
- **`bayes-image.md`**, «The administration card»: «Compiles at once» (default 2) joins the compile limits; the cache's
  card lists an older image's binaries.
- **The map**, `stan-compile`'s «Choices»: the binaries' storage is the engine's bucket under `bayes-binaries/`, outside
  the managed prefixes; the engine (Python, `backend/`) gains three internal routes for the bytes.

## Decisions of 2026-11-24 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| The schema migration | **Approved**: revision 0047 with two new tables, `bayes_binaries` and `bayes_settings`, as «The schema» says; no existing table changes | Owner, 2026-11-24 |
| A compile killed for memory | **Treated as the deadline**: state `timeout` with the reason `memory`, not compiled again until the memory limit is raised (the owner's rule on the deadline, widened to the other limit a compile can pass) | Owner, 2026-11-24 |

## Open questions

None.

## Correction of 2026-11-24 from C1

The owner's answers to lot C1's two questions, on 2026-11-24. Where this section and the text above differ, this
section holds.

1. **A binary larger than the cache is treated as the deadline.** A row `failed` with the reason `too_large` keeps the
   binary's size (`binary_bytes`) and is queued again **by itself** — by any request, a save, a run or «Compile now»,
   as a `timeout` is when its limit was raised — once the cache's limit holds that size (`binary_bytes` ≤ «Cache of
   binaries»), and never before, «Compile now» included: the same program on the same image gives the same binary.
   Until then a run fails at once with the row's sentence. Nothing queues it in the background: as for a `timeout`,
   the next request does. In `bayescompile`: `MayRetry` reads the cache's limit beside the deadline and the memory,
   and `PlanEnqueue` and `Refused` follow it (the «Transitions» gain nothing: `failed → queued` was there).
2. **A newer save always takes the older queued version out.** When a person saves a newer version of a model, their
   older version of that model still `queued` (never one `running`) leaves the queue, whatever the newer version's own
   program is (`ready`, `queued`, `running`, `timeout`, `failed` or `none`). So the store asks `Enqueue` after **every**
   save — `afterCommit` no longer skips `ready`, `queued` and `running` (lot C3; it still asks `State` for nothing
   else) — and `Enqueue` deletes `Plan.Overtaken` even when `Plan.Queue` is false. `PlanEnqueue` plans the overtaking
   for every state of the key's row since lot C1; lot C2 tests it for each.

## As built — lot C2 (2026-11-24)

- **The loop**: `gateway-go/internal/api/jobs_bayes_compile.go`, `bayes_compile_loop` (order 14), one round every
  `SCRIPTS__POLL_SECONDS` under `bayescompile.LoopLock` (7,402,160), which «Empty the cache» (lot C4) takes too. A round
  locks the `queued` and `running` rows `FOR UPDATE`, follows each running row's Job, deletes the queued rows of an
  older image, brings the cache under a lowered limit, starts what `NextToStart` gives, and once a day (per gateway)
  cleans; the Jobs whose end it read and their step's files (`model`, `artifacts/compile.json`, `error.json`,
  `run.json`) are deleted after its commit, so a round that fails finds them again.
- **The table's reads and writes** for C2 and C3: `bayescompile/rows.go` (`Columns`, `Scan`, `Insert`, `Write`,
  `Delete`); the key's CmdStan line from the recipe: `bayescompile.CmdStanOf`.
- **The Job**: as «The compile Job» says, the container named `model`, `terminationMessagePath: /dev/termination-log`
  and `terminationMessagePolicy: File` written; the ConfigMap holds the runner, `task.json` and **`program.stan` beside
  `task.json`** (`/tabularia/code/program.stan`); `TABULARIA_BINARY_OUT` and `TABULARIA_ARTIFACTS` are `s3://` addresses
  of the scripts bucket, as the other model Jobs' (`…/bayes-compile/<id>-<attempt>/model` and `…/artifacts/`);
  `task.json`'s `limits` hold the four compile values only (the runner's defaults for the rest); `recipe_hash` and
  `runner_hash` are the shipped group's. The program's sha256 is checked by the gateway too before the Job
  (`program_hash` otherwise).
- **The ends, as read** (`kube.TerminationMessage(pod, container)` added): the exit code 3's reason is read from the
  log's `TABULARIA_ERROR` line, as the model steps read it (`compile_failed` and `program_hash`; any other code is
  `job`), not from `artifacts/error.json`. The cluster's failures, each one attempt: the Job gone; no Pod after 10 s
  with the cluster's refusal (`FailedCreate`, a full quota); an image not pulled for 30 s; a Pod not scheduled after
  `min(60 s, a quarter of the deadline)` (deleted and queued again, rather than left to pass the deadline it would be
  blamed for); `DeadlineExceeded` with a Pod whose container never started; a failure without an exit code and no
  container that ran; a Job not ended 180 s after its deadline. `DeadlineExceeded` with the Pod already gone is a
  `timeout` (the container is taken to have run).
- **What the message vouches for**: `info_sha256` is the sha256 of the **file** `artifacts/compile.json` as uploaded
  («Keep», 1); the gateway reads its `program_sha256` and `binary_sha256` (equal to the message's or `job`),
  `binary_bytes`, `info`, `seconds` and `warnings` (at most 50 kept). **For the contract (lot K3)**: compile.json holds
  no `info_sha256` of its own — a file cannot hold its own hash; the contract's field list loses it.
- **The keep**: a `binary_bytes` above the cache's limit is `too_large` before the engine copies anything; the size
  recorded is the engine's measure. The cache's room is made only when the ready rows' bytes pass the limit, then with
  `FOR UPDATE SKIP LOCKED` over the ready rows (the rows a run is placing are left out); the evicted objects are deleted
  through the engine **before** their rows, and an engine that does not answer leaves every row in place for the next
  round (a row whose object went anyway is refused at its next place, forgotten and compiled again).
- **The engine's routes** (`backend/app/api/routes/scripts.py`, `backend/tests/test_scripts_binary.py`): `keep` takes
  only `bayes-compile/<id>-<attempt>/model`, answers 404 `{"missing": true}` and 409 `{"altered": true}` or
  `{"too_long": true}`, and `{bytes, existed}`; `place` takes only a step's key `env-<…>/<step>/model` (the form
  `flow-node` uses), answers 404 `{"missing": true}` and 409 `{"altered": true}`; `delete` takes sha256s only, so it can
  name nothing outside `bayes-binaries/`. The engine's own ceiling is `BINARY_MAX_BYTES` (64 MiB).
- **Measured locally** (2026-11-24, the image `tabularia-bayes-b0` under the Job's conditions with `docker run
  --read-only --tmpfs /tmp:rw,exec,size=512m --network none --user 10001:10001 --memory 3g --cpus 1 --cap-drop ALL`, a
  stand-in for the runner's compile mode, which is lot K3's): the golden `hier_slopes` compiled in 23.8 s (24.4 s
  wall), a binary of 2,788,592 bytes, 2.7 MB of `/tmp`; the termination message (written to a file in place of
  `/dev/termination-log`) is 254 bytes, `ParseMessage` reads it, its `info_sha256` is compile.json's, and
  `CrossCheck` finds no mismatch with the stored manifests. The message read through Kubernetes with a read-only root
  stays lot C5's (and K3's) to check on kind.

## As built — lot C3 (2026-11-24)

- **The compiler**: `gateway-go/internal/bayescompile/compiler.go`, `Compiler` (`bayesmodels.Compiler`,
  `BatchCompiler` and `Binaries`) over `bayes_binaries`, given the group's current image and the limits in force by a
  function (`Current`: nil while the group cannot run a Job, and then every state is `none` and a request queues
  nothing) and the binaries' bytes by the engine (`Storage`). The gateway builds it in
  `gateway-go/internal/api/bayes_compiler.go` (`BayesCompiler(deps)`, reading the image with `ReadyModelGroupImage` and
  the limits with `bayes_settings` and `Resolve`, as a round of the loop does) and sets `Deps.BayesCompiler` at
  start-up where the statistical models can run (`modelsOff` empty and the group shipped); elsewhere `NoCompiler` stays.
- **`Enqueue`** is one transaction: two advisory locks of the two-key space (which never meets `LoopLock`'s), first the
  person's model and then the key, so that two requests of one key insert its row once (the unique constraint is never
  the answer) and a newer version always sees the older one's queued row; then one `SELECT … FOR UPDATE` in id order of
  the key's row and the overtaking's candidates, `PlanEnqueue`, `Insert` or `Write`, and the overtaken rows deleted
  whatever the key's row is.
- **`afterCommit`** asks `Enqueue` after every save and reads no state (the correction from C1); `Saved.Enqueued` says
  that `Enqueue` answered.
- **The states**: `States` is one `SELECT … WHERE cache_key = ANY($1)` that also says whether each row's person has
  another compile running (the queued sentence); `State` is `States` of one program. A sha256 not of its form is `none`.
- **«Compile now»** (`POST …/versions/{v}/compile`): 409 while queued or running (as before); otherwise `Enqueue` with
  `Again`, then the state: `timeout`, or `failed` (after `Again` only a binary larger than the cache stays failed),
  answers 409 with the state's sentence and is not audited. A raised deadline, memory or cache makes the same request
  queue it (202).
- **A run** (`Binaries`, for `flow-node`): `Ready`; `Need` (the run's fallback: the binary when ready, `ErrRefused` with
  the state when `Refused`, otherwise `Enqueue` with the run's authority, never `Again`, and the state to wait on);
  `Place(b, key)`, the key only of the form `env-<…>/<step>/model`, the row `FOR SHARE` during the engine's copy and
  the use recorded after it in a short transaction of its own (`FOR UPDATE SKIP LOCKED`: two runs of one binary never
  wait on each other). A 409 `altered` or 404 `missing` of the engine is `ErrAltered`: under `LoopLock` the row is
  deleted and the object too when no other ready row holds its bytes, and the run's next `Need` queues it again;
  `ErrGone` is a row evicted between `Ready` and `Place`.
- **The client**: `BayesCompileState` gains `'timeout'`; the list's cell (`bayesCompileCell`) shows «too long to compile»
  (`bayesModels.compile_timeout`, five languages) in danger with the gateway's sentence; Explore's row names it as the
  other states. The editor's header shows no compile state today, so nothing changed there.

## Correction of 2026-11-24 from the step-5 specs

From `bayes-flow-node.md` and `bayes-runner.md`, approved by the owner on 2026-11-24. Where this section and the text above
differ, this section holds.

1. **«A run»**: `Binaries` is `Ready`, `Need` and `Place` (as built by lot C3). `flow-node` calls `Need` when the step is
   planned (it queues the compile with the run's authority before the input is prepared), then waits on the state every
   2 seconds within the step's timeout, then `Place`s the binary at `<step prefix>/model`; `ErrAltered` and `ErrGone` ask
   `Need` again.
2. **The wait is the step's**: the Job's deadline and `time_budget_seconds` are the step's timeout minus the time it waited
   for the compile.
3. The runner fetches the binary from `TABULARIA_BINARY_IN` **before it reads the data** and compares its sha256 before
   `chmod` and exec (`bayes-runner.md`, «The binary»).

## As built — lot C4 (2026-11-24)

- **The routes**: `gateway-go/internal/api/models_admin_bayes.go`, the three of the card, declared Go-only
  (`server/go_only_routes.go`) and guarded as the models' others (`mgGuarded`: administrators act, observers read, an
  API token never changes anything). `GET /admin/models/bayes/compile` answers `limits` (in force), `defaults` (the
  recipe's, «Compiles at once» at `bayescompile.DefaultSlots`), `cache` (`used_bytes`, `binaries`, `oldest_use`,
  `older_image` {`binaries`, `bytes`}, the older image being any ready row whose digest is not the group's current
  one), the last 20 `compiles` and `editable`. `PUT` takes the seven settings (`compile_cpu`, `compile_memory`,
  `compile_seconds`, `compile_tmp`, `compile_slots`, `cache_max`, `sampling_tmp`), a field left out or null being the
  default; a value equal to the default is stored as NULL, so it keeps following the recipe; the audit
  `models.bayes_compile_settings` holds the values in force before and after and is written only when they differ.
  `POST …/cache/empty` answers the card with `emptied` {`binaries`, `bytes`, `kept`} and audits `models.bayes_cache_empty`.
- **The ranges** (422 with the sentence): CPU a whole number of cores 1–16; compile memory 2 GiB–64 GiB (the sentence
  quotes the measured 1.3–1.9 GiB); the longest compile 60–3,600 s (11–45 s, the 1,497-node budget 130.5 s); compile
  `/tmp` 64 MiB–16 GiB; «Compiles at once» 1–16; the cache's limit 64 MiB–1 TiB; the sampling `/tmp` 256 MiB–256 GiB.
  Quantities are whole Mi or Gi (`bayescompile.QuantityBytes`); the cache's limit is stored in bytes. The loop and the
  Compiler read the same row with `bcSettings` and `Resolve`; the sampling `/tmp` is stored for `flow-node` and read by
  nothing yet. A change of limits touches no row (a timeout row queues again by itself, `MayRetry`).
- **«Empty the cache»**: in one transaction under `pg_advisory_xact_lock(LoopLock)` (it waits for a round, never skips
  it): every ready row, `MakeRoom` with a limit of 0 and the rows a run is placing (`FOR UPDATE SKIP LOCKED`) left out;
  the objects are deleted through the engine before the rows, and an engine that fails answers 502 with nothing deleted
  and nothing audited. Queued, running, timeout and failed rows are not touched.
- **The compiles' list** names a model and its version only to a reader who sees the model (`bayesmodels.CanSee`);
  an observer with no grant on the folder reads the state, the seconds and the limits the compile ran under, not the
  name. A `timeout` row carries `deadline_seconds` and `memory_bytes`.
- **The card**: `frontend/components/admin/BayesCardFields.vue`, drawn inside the group `bayes`'s «Resources, access and
  versions» disclosure once it is opened (so the page asks the gateway only then); the fields show their default and a
  «Reset to default» when they differ, one «Save» for all, «Empty the cache» asks first. Words under
  `modelsAdmin.bayes.*` in the five languages, the group's own name and description added (`modelsAdmin.group.bayes`),
  the two audit actions labelled. Tried by hand in Chrome (Chromium) and Firefox against `nuxt dev` with the routes
  answered by a fake (Playwright interception) in the Italian interface, dark and light at 1440 px: the defaults, a
  change, a refusal (the gateway's sentence in the toast), the save, the emptying with its confirmation, an observer's
  card (every fieldset disabled, no action), and the keyboard (Tab from the cache's field reaches its «Reset to
  default», Enter resets it). Not tried against a real gateway: the routes are tested on a real database.
- **For lot C5**: nothing on kind needs the card, but to try a timeout without a slow program lower
  `compile_seconds` to 60 from the card (the deadline the Job gets is the one in force when it starts) and raise it
  again to see «Compile now» queue the program; the card's list shows each row's limits.
