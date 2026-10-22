# Spec: Script node and scripting environments

Status: v1 built, 2026-10-21 (environments, the node in runs, the editor); see docs/scripting.md. Decisions marked **(owner)** were taken by the project
owner; the rest are proposals to confirm or correct.

## Objective

Users ask to run their own Python and R code inside a flow. Tabularia must not run that
code itself: its workers hold the encryption key, the credentials of every connection
and full access to the storage, so code running there would see all of it and step
around the permissions.

The **Script node** runs the user's code as a **Kubernetes Job** outside Tabularia.
The Job receives one reference, the step that comes before the node, and reads it by
itself (`pl.scan_parquet("s3://…")` in Python, `arrow::open_dataset("s3://…")` in R).
It may write one table back, which the rest of the flow then uses like any other step.

The code runs in a **scripting environment** that an administrator defines in the
admin panel: language and version, packages, environment variables. Tabularia builds
it once into an image and the Jobs run that image.

Users: whoever builds flows and needs a step the visual operations cannot express
(a model, a statistical test, a library call); administrators, who decide which
environments exist, what they contain and who can use them.

Success looks like this:
- an administrator creates «Python 3.12 + scikit-learn» and sees it built, with the
  versions that were installed;
- an analyst in an allowed group adds a Script node after a join and writes twenty
  lines that score each row;
- the next node filters on the score;
- the scheduled run shows the Script step with its log, its duration and the version
  of the environment it ran in;
- the Job held a key valid only on the scripts bucket, never one for Tabularia's own
  storage.

## Decisions

- **(owner)** The code never runs in Tabularia's workers. It runs as a Kubernetes Job.
- **(owner)** The data is not pushed to the Job. The Job gets the reference of the
  last step before the node and reads it with `scan_parquet` from S3. Writing an
  output is optional.
- **(owner)** That step is written by the run to a **place of its own** and deleted
  when the run ends. The step cache (`cache/{hash}.parquet`) is not used: it expires,
  it is best-effort when Valkey is down, and runs do not write intermediate steps
  to it.
- **(owner)** That place is a **bucket the administrators create for the scripts**,
  outside Tabularia's own storage. In the admin panel they give its endpoint, its
  name and an **access key and secret key scoped to that bucket only**. The engine
  writes the inputs there with that key and the Jobs use the same key. No temporary
  credentials (STS): the key comes from the administrators, so any S3-compatible
  storage works, GCS included through its HMAC keys.
- **(owner)** The admin panel gets a section **«Scripting environments»**. There,
  administrators define for each environment:
  - the language and its version;
  - the packages to install;
  - its environment variables.
- **(owner)** The variables of an environment are **separate from Tabularia's**.
  Handing a Tabularia value to an environment is an exception an administrator
  makes explicitly, marked as such.
- **(owner)** An environment is **built once into an image**. The image is never
  rebuilt at every run.
- **(owner)** **Kubernetes only.** The image is built by a Job in the cluster and the
  scripts run as Jobs. With the Compose stack the section is visible but off
  («requires Kubernetes»). Compose would need the Docker socket, which is control
  of the host.
- **(owner)** The code editor of the node shows the variables of the chosen
  environment. A secret variable appears by name only; its value never reaches the
  browser.
- **(owner)** Community indexes only, no company's service: PyPI and CRAN (or mirrors
  of them), searched from the admin panel; rocker's default repository (a company's
  package manager) is replaced by CRAN, so R packages compile during the build.
- **(owner)** A node holds several files: modules next to the entrypoint.
- **(owner)** The editor offers the packages while typing: the image's check lists the
  public names of every module (`completions`), the editor reads the aliases in the
  code. No language server.
- **(owner)** The people who write scripts are trusted, and egress is open; one check
  stays: the code may not name the scripts bucket (services/script_nodes.py).
- **(owner)** Input and output are optional: a node with neither runs something
  outside Tabularia as a step of the flow.
- **(owner)** **Run** in the node panel runs the node of the SAVED flow (unsaved
  changes are saved first): the chain before it on the development sample, the nodes
  after it not run (`POST /scripting/flows/{id}/nodes/{node}/run`, a `kind="script"`
  run with `trigger_type="trial"`). The nodes after a Script node preview its latest
  output, a trial's or a flow run's (`GET /scripting/flows/{id}/outputs`), with
  `stale` when the saved code changed since.
- **(owner)** Trials run in a namespace of their own (`SCRIPTS__TRIAL_NAMESPACE`,
  chart `<namespace>-scripts-dev`) with a ResourceQuota (`scripts.trialQuota`): they
  never take resources from the flows' runs. A Job the quota does not admit (a
  `FailedCreate` event «exceeded quota») fails the attempt at once, saying so.

## What it reuses

| need | already there |
|---|---|
| a node the orchestrator executes, not the engine | `refresh` and `runflow` are control nodes: `_ACTION_TYPES` in `gateway/app/services/flow_resolver.py:508`, executed in `orchestrate()` (`gateway/app/services/orchestrator.py:227-279`), with a bounded wait like `_refresh_and_wait` (:126) |
| a run made of steps, with children | `Run(kind="orchestration")` and child runs via `parent_run_id` (`gateway/app/models/run.py:103`), the queue claimed by the orchestrators, heartbeat and orphan handling (`chiudi_orfane`, :570) |
| writing a chain to a key of our choice | `POST /tasks/transform-data` with `{bucket, input_key, output_key, operations, …}`, so non-datasource outputs already go to `out/{uuid}.parquet` (`gateway/app/routes/runs.py:497-624`) |
| reading a parquet as the start of a chain | the same payload: `input_key` is any parquet in the bucket |
| talking to the Kubernetes API | `gateway/app/services/kube_scaling.py`: httpx to `https://kubernetes.default.svc`, the ServiceAccount token read at every call, no client library |
| secrets at rest | `encrypt_secret`/`decrypt_secret` (`gateway/app/core/crypto.py`) |
| a feature turned on in the chart | `adminScaling.enabled`: a template under a flag, a Role scoped to its resources, `KUBERNETES__*` env from a helper |

| writing to and reading from a bucket that is not Tabularia's, with a static key | the S3 Output (`backend/app/ingest/s3_destination.py`: endpoint, static key encrypted with Fernet, empty endpoint = AWS) |

New:
- the environment tables and their builds;
- the scripts bucket settings;
- the Job runner;
- the node and its editor.

## How a run executes a Script node

A flow with a Script node is an orchestration, like one with `refresh` or `runflow`.
On the frontend, `hasControlNodes` (`frontend/components/FlowEditor.vue:1622`)
includes `script`, so «Run» goes through `POST /flows/{id}/run-now`.

Each Script node of a run gets a **step id**, a random UUID (122 random bits).
**(owner)** The scripts bucket has one prefix per environment, `env-<environment_id>/`
(the id, not the name, which can change), and the files of a step live under it:
- `env-<id>/<step_id>/input.parquet`, the step before the node;
- `env-<id>/<step_id>/output.parquet`, what the script writes, if it writes anything.

The prefix keeps the bucket readable by environment, lets a lifecycle rule differ by
environment, and is emptied when its environment is deleted. With one key for the
installation it does not separate teams. A later version can give an environment a
key of its own, limited to its prefix by a bucket policy, without changing the
layout.

The resolver treats a Script node as a **boundary**:
- the chain that feeds it ends at its input;
- the chain of every node after it starts from its output.

The orchestrator orders the work by these dependencies: a Script runs before the
Outputs that read from it, and after the Scripts that feed it.

For each Script node the orchestrator:

1. **Writes the input.** It submits the chain that ends at the node to
   `transform-data`, with a destination of a new kind, `script_input`. The engine
   writes the parquet to `env-<id>/<step_id>/input.parquet` in the scripts bucket, with the
   key from the panel (handed over encrypted, like an S3 Output's). The
   orchestrator waits like for any child run (`_wait_run`), and the child run is
   recorded as today.
2. **Creates the Job.** It creates three objects in the scripts namespace, labelled
   with the run, the node and the step id:
   - a ConfigMap holding the code;
   - a Secret holding the bucket key and the environment's secret variables;
   - a Job using the environment's image by digest.

   The ConfigMap and the Secret have the Job as owner, so they go away with it.
   Details in [The Job](#the-job).
3. **Waits.** It polls the Job every few seconds, within the node's timeout. The
   orchestration's heartbeat keeps running while it waits.

   **(owner)** **Retries are a setting of the node**: `retries` (0 by default, at
   most `SCRIPTS__MAX_RETRIES`) and `retryDelaySeconds` (60 by default). When the
   Job fails or times out, the orchestrator waits that delay and creates a **new
   Job** on the same input:
   - each attempt has its own log, and the run page shows «attempt 2 of 3»;
   - a half-written output of the failed attempt is deleted first;
   - the input is not written again.

   A missing output after a successful Job is not retried: the script ran and did
   not write it, and running it again would do the same.
4. **Collects the result.**
   - **Success:** the Job succeeded. If the node has nodes after it,
     `output.parquet` must exist; if it does not, the node fails with «the script
     did not write its output (TABULARIA_OUTPUT)». With no nodes after it, any
     output is ignored.
   - **Failure:** the last attempt failed or timed out. The node fails with the exit
     code and the last lines of the log.
   - **Log:** in both cases the log of the pod is read and stored with the run.
5. **Brings the output in.** The chains after the node read `output.parquet` from
   the scripts bucket: their `transform-data` gets a source of a new kind,
   `script_output`, which the engine reads with the panel's key. It reads it the way
   the S3 folder datasource reads an external bucket. Nothing is copied into
   Tabularia's storage except what an Output writes.
6. **Cleans up.** When the orchestration ends, in any state, the engine deletes
   the input and the output files of every Script node of
   the run, by exact key, so the key needs no listing right. The administrators set
   a **lifecycle rule** on the bucket (objects older than a day expire), which
   removes what a crash left behind; the guide says how.

A failing Script node follows the rule of today's control nodes: the steps that depend
on it do not run, and the orchestration ends in FAILURE.

### Cancelling and orphans

- **Stop.** An administrator stopping the run from the queue page also deletes the
  Job (`propagationPolicy: Background`).
- **Orphans.** When `chiudi_orfane` closes an orchestration with a stopped
  heartbeat, it deletes the Jobs labelled with that run.
- **Clean-up of finished Jobs.** `ttlSecondsAfterFinished` (one hour by default)
  removes finished Jobs the orchestrator did not get to.

## The Job

```yaml
spec:
  backoffLimit: 0                  # retries are new Jobs, created by the orchestrator
  activeDeadlineSeconds: <node timeout>
  ttlSecondsAfterFinished: 3600
  template:
    spec:
      restartPolicy: Never
      # pod labels: tabularia.io/environment=<env_id>, tabularia.io/run=<run_id>, tabularia.io/step=<step_id>
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 10001, seccompProfile: {type: RuntimeDefault}}
      containers:
        - image: <registry>/tabularia-env-<env_id>@sha256:…
          command: ["python", "/tabularia/script.py"]     # R: ["Rscript", "/tabularia/script.R"]
          securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: [ALL]}}
          resources: <the environment's requests and limits>
          env: <the environment's plain variables>
          envFrom: [{secretRef: <the per-run Secret>}]
          volumeMounts: [/tabularia (the ConfigMap, read-only), /tmp (emptyDir)]
```

The variables Tabularia sets have **reserved names**: an administrator cannot define a
variable starting with `TABULARIA_` or `AWS_`.

| variable | value |
|---|---|
| `TABULARIA_INPUT` | `s3://<scripts bucket>/env-<id>/<step_id>/input.parquet` |
| `TABULARIA_OUTPUT` | `s3://<scripts bucket>/env-<id>/<step_id>/output.parquet` (empty when the node has nothing after it) |
| `TABULARIA_STEP_ID`, `TABULARIA_RUN_ID`, `TABULARIA_FLOW_ID`, `TABULARIA_NODE_ID` | identifiers, for the script's own logging |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | the key of the scripts bucket, from the panel |
| `AWS_ENDPOINT_URL`, `AWS_REGION` | the endpoint and region of the scripts bucket (empty endpoint = AWS) |
| `AWS_ALLOW_HTTP` | `true` when the endpoint is `http://`: Polars' `object_store` refuses plain HTTP otherwise (checked 2026-10-20 against MinIO) |

Polars (`object_store`), `pyarrow`, `s3fs` and R `arrow` read these `AWS_*` variables
by themselves, so the minimal script needs no configuration. Checked on 2026-10-20 against
MinIO with a key holding only `GetObject`, `PutObject` and `DeleteObject`: Polars 1.41
scans the input and sinks the output with it, and a glob over the bucket is refused.

```python
import os, polars as pl
df = pl.scan_parquet(os.environ["TABULARIA_INPUT"])
df.with_columns(score=pl.col("amount") * 0.1).sink_parquet(os.environ["TABULARIA_OUTPUT"])
```

```r
library(arrow); library(dplyr)
ds <- open_dataset(Sys.getenv("TABULARIA_INPUT"))
ds |> mutate(score = amount * 0.1) |> write_parquet(Sys.getenv("TABULARIA_OUTPUT"))
```

The code travels in a ConfigMap, which is limited to 1 MiB. The node caps the code at
`SCRIPTS__MAX_CODE_BYTES` (256 KiB by default).

**The log.** When the Job ends, the orchestrator reads the pod log
(`pods/{name}/log`). It keeps the last `SCRIPTS__LOG_MAX_BYTES` (256 KiB) in the
database with the node's result, because the gateway has no storage client. The run
page shows it. While the Job runs, the run page can tail the log through the gateway
(phase 2).

**Network.** **(owner)** Scripts may call external services: egress is open. It is
part of the product to leave that responsibility to the users and the administrators
who define the environments. The chart keeps one fixed NetworkPolicy, on by default
(`scripts.blockTabularia`, templates/user-code-networkpolicy.yaml — written after
the audit of 2026-10-22 found it missing), that denies the scripts namespace any
traffic to the pods of Tabularia's own namespace, to the cloud metadata addresses and
to the private ranges outside the cluster not listed in
`networkPolicy.userCodePrivateCIDRs`. Tabularia's database, Valkey and storage are out
of a script's reach when they are in the release's namespace or on an unlisted private
address; in another namespace of the cluster they need a policy of their own (the
guides' `infrastructure/minikube/dependencies.yaml` has one). The policy needs a
network plugin that enforces egress rules (Calico, Cilium; kind's kindnet does not).

## The scripts bucket

**(owner)** The administrators create a bucket for the scripts and a key that works
on that bucket only. They enter them at the top of the «Scripting environments»
section:
- endpoint (empty = AWS), region, bucket name;
- access key and secret key.

Path-style addressing with an endpoint, as for the S3 Outputs.

The secret key is encrypted with Fernet and, once saved, cannot be read back. The
engine uses the key to write the inputs, read the outputs and delete both; every Job
gets it as `AWS_*`.

**What this protects.** Tabularia's own storage (every datasource, every snapshot,
the step cache) never appears in a Job: its keys stay in the engine. A script can
reach only the scripts bucket, which holds nothing but the inputs and outputs of
runs in progress, deleted when each run ends.

**What it does not.** All scripts share the same key, so a script could reach the
files of another run in progress if it knew their path. Two measures make that path
impossible to find:
- the step id is a random UUID, never shown to other users;
- the guide asks for a key **without the listing right**: `GetObject`, `PutObject`
  and `DeleteObject` on the bucket's objects, not `ListBucket`.

Polars and Arrow read and write a single named file without listing, so scripts need
nothing more. The panel's **«Test»** button:
- writes, reads and deletes a probe object;
- **warns** if the key can list the bucket.

The guide also says what a well-scoped key looks like on MinIO, AWS, GCS (HMAC
keys) and Scaleway (a bucket policy on an IAM application).

## Scripting environments (admin panel)

The panel gets a new section, «Scripting environments». It is added the way the
existing ones are: the `Sezione` type, the `sezioni` list, a `v-else-if` branch, and
a component in `frontend/components/admin/` (`frontend/components/AdminPanel.vue:55`,
`:99-115`, `:739-757`).

An environment has:

- **Name**, **description**, **enabled**.
- **Language** (Python or R) and **version**, chosen from the list the chart offers
  (`scripts.baseImages`: e.g. Python 3.11/3.12/3.13 → `python:3.x-slim@sha256:…`,
  R 4.4/4.5 → `rocker/r-ver:4.x@sha256:…`). Pinned by digest, so a rebuild starts
  from the same base.
- **Packages**:
  - Python: one requirement per line, `name==version` recommended;
  - R: `name` or `name@version`.

  The packages that let a script read its input are always installed and cannot be
  removed: Python `polars` and `pyarrow`; R `arrow` with S3 support.
- **Variables**: name and value, each either plain or **secret**.
  - **Secret** values are encrypted with Fernet. After saving they show only as `••••`
    and an administrator can replace them but not read them back.
  - **Names** match `^[A-Z_][A-Z0-9_]*$`, outside the reserved prefixes.
  - **A value taken from Tabularia** is the marked exception: the panel shows a
    warning and the audit records who made it.
- **Resources**: CPU and memory requests and limits, and the **maximum timeout**
  a node may ask for.
- **Groups**: who can use the environment. **(owner)** A user in one of those groups
  who also has EDIT on the flow's folder can write a Script node with it.
  Administrators can use them all.

Variables and resources apply to the next run and need no rebuild. The language, the
version and the packages define the image, and changing them creates a new
**version**.

### Versions and builds

Saving a change to the version or the packages creates version *n+1* in state
**queued**. The orchestrators move it forward, one round every few seconds, one
process at a time (an advisory lock), in `services/script_builds.py`:

1. **Dockerfile.** It generates the Dockerfile:
   - `FROM <base>` (the chart may pin the bases by digest), with the user 10001;
   - the package list in a heredoc, after a check that every line is a package name
     and a version and nothing a shell would run;
   - Python: `pip install -r requirements.txt`, from the index the chart configures;
   - R: `install.packages` for the plain names and `remotes::install_version` for
     `name@version`, from CRAN or the mirror the chart configures. **(owner)** Open
     source sources only: no company's package manager, so rocker's default repository
     is replaced, in the build and in the image. CRAN publishes sources, so the
     packages compile, with the system libraries of the usual packages installed
     first; a package that did not install fails the build.
2. **Build** (`building`). A Job in the build namespace runs **rootless BuildKit**.
   Kaniko was the first idea, but Google archived it in June 2025 and the maintained
   fork publishes no free images. The Dockerfile travels in an environment variable,
   so no ConfigMap. The Job pushes `<registry>/tabularia-env-<env_id>:v<n>` and prints
   the digest as the last line of its log.
3. **Check** (`checking`). A Job in the scripts namespace runs the new image by
   digest, as a script would: user 10001, read-only root filesystem, no capability.
   It loads polars and pyarrow, or arrow with S3 support, and prints every installed
   package. This proves the image starts, and its output is the **lock** of the
   version.
4. **Outcome.**
   - **Ready:** the version stores the image reference by digest, the lock and the
     build log, and becomes current unless a newer version already is.
   - **Failed:** it keeps the log; the previous version stays current.

Every Script run records which **environment version** it ran on.

A flow references the environment, not a version: it runs on the current one. An
older version can be made current again from the panel, and its image is still in the
registry.

Deleting an environment that flows use is refused, with the list of those flows.
Disabling it makes their Script nodes fail with «the environment … is disabled».

## The node in the editor

- **Palette.** The node is called «Script». It has one data input on the left, one
  optional data output on the right, and sequence handles like the other nodes.
- **Settings.** The panel holds:
  - the environment: only the enabled ones the user may use, with their language;
  - the timeout, up to the environment's maximum;
  - the retries and the delay between them;
  - the code.
- **Code editor.** **(owner)** A small VS Code-like editor: **Monaco**, the editor
  of VS Code itself (MIT), through the Vue wrapper `@guolao/vue-monaco-editor` (MIT).
  - Python and R highlighting are built in.
  - The variables of the environment come in through a completion provider.
  - It is loaded only when a Script node is opened, because Monaco weighs a few MB.
  - Its files are served by the frontend itself, not by a CDN (the wrapper's
    default), so it works on a network with no internet access.
- **Variables.** Next to the code, the list of the variables the script will see:
  - the environment's plain variables with their value;
  - its secret ones by name only;
  - the `TABULARIA_*` ones, labelled as set by Tabularia.

  A click inserts the reference in the right language (`os.environ["NAME"]`,
  `Sys.getenv("NAME")`), and the names are offered while typing.
- **Read-only.** The node is read-only for someone who cannot use its environment.
  The gateway enforces this when the flow is saved: the code and the environment of
  a Script node change only if the user may use that environment. The flow save is
  audited with the node's code hash.
- **Preview after the node.** The nodes after a Script node preview its latest output
  (see **Run** in the decisions above).

## Data model (one migration)

- `script_storage`, a single row:
  - `endpoint`, `region`, `bucket`, `path_style`, `verify_tls`;
  - `access_key`, `secret_key_encrypted`;
  - `updated_by`, `updated_at`.
- `script_environments`:
  - `id`, `name` (unique), `description`, `language` (`python`|`r`);
  - `enabled`, `current_version_id`;
  - `cpu_request`, `cpu_limit`, `memory_request`, `memory_limit`, `max_timeout_seconds`;
  - `created_by`, `created_at`, `updated_at`.
- `script_environment_versions`:
  - `id`, `environment_id`, `number`, `language_version`, `base_image`, `packages`
    (text);
  - `status` (`building`|`ready`|`failed`), `image_ref` (by digest), `lock` (text),
    `build_log` (text, capped);
  - `claimed_by`, `started_at`, `finished_at`, `created_by`, `created_at`.
- `script_environment_variables`:
  - `id`, `environment_id`, `name`, `value`, `value_encrypted`, `secret`;
  - `from_tabularia` (the marked exception);
  - `updated_by`, `updated_at`;
  - unique `(environment_id, name)`.
- `script_environment_groups`: `environment_id`, `group_id`.
- **(owner)** The result of a Script node, one row per node and run:
  - a `Run` with `kind="script"` under the orchestration (`parent_run_id`), so the
    run page and the queue show it like the other steps;
  - its error in `error`;
  - new nullable columns `environment_version_id`, `exit_code`, `attempts`, `log`,
    `variables`.
    The log holds every attempt, each under an «attempt n» heading.

The node in the flow definition:

```json
{"type": "script", "data": {"environmentId": 3, "code": "…", "timeoutSeconds": 900,
                            "retries": 2, "retryDelaySeconds": 60}}
```

Audit actions:
- `scripting.storage_update` (never the secret key), `scripting.storage_test`;
- `scripting.environment_create`, `_update` (a new version is in its detail),
  `_delete`;
- `scripting.version_current`;
- `scripting.variable_set`, `scripting.variable_delete`: name only, never the
  value.

## Configuration

Gateway and orchestrator (`SCRIPTS__*`; off unless the chart turns them on):

| variable | default | |
|---|---|---|
| `SCRIPTS__ENABLED` | `false` | the section and the node exist only when true |
| `SCRIPTS__NAMESPACE` | — | where the scripts run |
| `SCRIPTS__BUILD_NAMESPACE` | — | where the builds run: rootless BuildKit needs seccomp and AppArmor `Unconfined`, which only the `privileged` Pod Security level admits; the scripts namespace is `restricted` |
| `SCRIPTS__REGISTRY` | — | `registry.example.com/tabularia` |
| `SCRIPTS__BASE_IMAGES` | Python 3.11–3.14, R 4.5–4.6 | JSON from the chart: language → version → image |
| `SCRIPTS__REGISTRY_INSECURE`, `_PUSH_SECRET`, `_PULL_SECRET` | | the registry's TLS and credentials |
| `SCRIPTS__BUILDER_IMAGE` | `moby/buildkit:v0.33.1-rootless` | |
| `SCRIPTS__PYPI_INDEX_URL`, `SCRIPTS__CRAN_URL` | PyPI, the base image's | mirrors for clusters without internet |
| `SCRIPTS__BUILD_TIMEOUT_SECONDS`, `SCRIPTS__CHECK_TIMEOUT_SECONDS` | 3600, 300 | |
| `SCRIPTS__MAX_CODE_BYTES` | 262144 | |
| `SCRIPTS__LOG_MAX_BYTES` | 262144 | |
| `SCRIPTS__POLL_SECONDS` | 5 | |
| `SCRIPTS__MAX_RETRIES` | 5 | the most retries a node may ask for |
| `SCRIPTS__JOB_TTL_SECONDS` | 3600 | |

The engine needs no new variable: the scripts bucket and its key come from the panel
with each task, like an S3 Output's.

Chart: a `scripts` block (`enabled`, namespaces, registry with its push and pull
Secrets, builder image, base images, package mirrors, `blockTabularia`) and these
templates:
- the two namespaces, with their Pod Security labels (`createNamespaces`);
- a Role and RoleBinding in each namespace for the ServiceAccount of the gateway and
  the orchestrator (the one of Admin → Workers, created when either feature is on):
  Jobs create/get/list/delete/deletecollection, Pods get/list, `pods/log` get. Phase
  2 adds Secrets and ConfigMaps create/delete, for the code and the variables;
- the NetworkPolicy that keeps the scripts out of Tabularia's namespace
  (`scripts.blockTabularia`, default `true`).

The API-only gateways need no Kubernetes rights: they write rows, and the
orchestrators act on them.

`docs/configuration.md` is regenerated, and a guide `docs/scripting.md` covers:
- turning it on;
- the registry;
- an air-gapped cluster;
- what a script sees.

## Phases

1. **Environments.** The admin section, the scripts bucket with its test, the
   tables, builds and versions, and the variables. Done when an administrator builds
   a Python and an R environment and sees their locks.
2. **The node in runs.** The resolver boundary, the input step, the Job, the
   output read back, the log and the cleanup. Done when:
   - a scheduled flow with a Script between a join and an Output runs on MinIO and on
     AWS;
   - the Job's environment holds no key of Tabularia's storage;
   - both files of the step are gone after the run.
3. **The editor.** The node, CodeMirror and the variables list (phase 2 can ship
   with a plain text area).
4. **Later.**
   - Live log tail.
   - **Optional: a size for `/tmp`.** Today the emptyDir has no `sizeLimit` and the
     container no `ephemeral-storage` limit: a script that writes many GB fills the
     node's disk, and the kubelet may evict other Pods too. A per-environment
     «Temporary space» (default 2 GiB) would set both, and a Pod over it would fail
     its run alone, saying so. Optional because not every managed cluster enforces
     `ephemeral-storage` (to check on Scaleway Kapsule first); an empty value keeps
     today's behaviour.
   - GPU.
   - Ray as a second runner (same environments, same contract).

## Variables seen by a run

**(owner)** A Script run keeps a copy of the variables it was given, in its
`variables` column: the plain ones with their value, the secret ones by name only,
with the time their value was last changed. The run page shows them, and marks a
secret one «changed after this run» when its value is newer. This answers the
question «why was Monday's result different?» when an administrator changed a
value in between.
