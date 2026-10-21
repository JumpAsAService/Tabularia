# Python and R in flows

For whoever runs Tabularia on Kubernetes, and whoever writes the scripts.

- **Administration → Scripting environments** defines the Python and R environments
  user code runs in, outside Tabularia. Each is a language version and a list of
  packages, built once by the cluster into an image that is checked before use. Its
  variables are what the code finds in its environment.
- **The Script node** of the flow editor runs a step's code, one file or several, as a
  Kubernetes Job in one of those environments ([the design](specs/script-node.md)).

The packages come from community indexes only: PyPI for Python, CRAN for R, or your
mirrors of them.

## What runs where

| | where | what it may do |
|---|---|---|
| the builds | the **build namespace** | rootless BuildKit builds the image and pushes it to your registry |
| the checks and the scripts | the **scripts namespace** | the image runs as user 10001, with a read-only root filesystem, no privilege and no capability |
| the editor's trials (**Run**) | the **trials' namespace** | the same as a script, within a ResourceQuota of its own: trials never take resources from the flows' runs |
| Tabularia | its own namespace | the gateway and the orchestrator create those Jobs, read their Pods and logs, and delete them, in the two namespaces above only |

The scripts never run in Tabularia's namespace and never see its keys. They reach data
only through the **scripts bucket** below, with a key the administrators give for it.

## Turning it on

In the chart's values:

```yaml
scripts:
  enabled: true
  registry:
    repository: registry.example.com/tabularia   # the images: <repository>/tabularia-env-<id>:v<n>
    pushSecret: registry-push    # dockerconfigjson Secret in the build namespace
    pullSecret: registry-pull    # dockerconfigjson Secret in the scripts namespace
```

`helm upgrade` creates three namespaces next to the release's:
- `<release namespace>-scripts`, at the `restricted` Pod Security level;
- `<release namespace>-scripts-dev`, `restricted` too, for the editor's trials, with a
  ResourceQuota (`scripts.trialQuota`: 4 CPUs and 8 GiB requested, 8 CPUs and 16 GiB at
  most, 10 pods, all trials together). A trial that does not fit fails at once and
  says the quota is full;
- `<release namespace>-script-builds`, at the `privileged` level.

Rootless BuildKit needs a user namespace of its own, which needs seccomp and AppArmor
`Unconfined`, and only `privileged` admits that. Its pods still run as a user and are
not privileged containers.

To manage the namespaces yourself, set `scripts.createNamespaces: false` and name them
with `scripts.namespace`, `scripts.trialNamespace` and `scripts.buildNamespace`.

The Secrets hold the registry's credentials: the push one in the builds' namespace, the
pull one in the scripts' and in the trials' namespaces:

```bash
kubectl -n <release>-script-builds create secret docker-registry registry-push \
  --docker-server=registry.example.com --docker-username=… --docker-password=…
kubectl -n <release>-scripts create secret docker-registry registry-pull \
  --docker-server=registry.example.com --docker-username=… --docker-password=…
kubectl -n <release>-scripts-dev create secret docker-registry registry-pull \
  --docker-server=registry.example.com --docker-username=… --docker-password=…
```

Give the push account write access to the repository path, and the pull account read
access only. A registry that takes anonymous pulls needs no pull secret.

### A registry with your company's certificate

Two programs talk to the registry, and both must trust its certificate:
- **the builds** push the images. When the certificate is signed by your company's
  authority, give the chart that authority's certificate:

  ```yaml
  scripts:
    registry:
      caCert: |
        -----BEGIN CERTIFICATE-----
        …
        -----END CERTIFICATE-----
  ```

  `scripts.registry.insecure: true` instead skips the verification, or allows plain
  HTTP: for a test registry only.
- **the nodes** pull the images, as for any pod. Their container runtime must trust
  the authority too: install it on the nodes the way your platform does it (a node
  image, a DaemonSet that writes it to containerd's `certs.d`). A registry in plain HTTP
  must likewise be allowed on the nodes. Tabularia cannot do this from a pod.

When either side refuses the registry, the environment's version fails at once and
says which side and why. So does a run whose image a node cannot download: the
credentials are refused, the nodes do not trust the certificate, the image is no
longer there. It does not wait for its timeout.

### Network policies

With `networkPolicy.egressCIDRs` set, the engine and the workers reach only the
addresses listed. The engine also writes each Script node's input to the scripts
bucket: include its address. A bucket, broker or ClickHouse that is a Service of the
cluster, in another namespace (`minio.storage.svc.cluster.local`), is allowed from its
host name; for other namespaces, list them in `networkPolicy.egressNamespaces`.

### Nodes of more than one architecture

A build runs on one node and the image is built for that node's architecture (amd64,
arm64). Tabularia records it, and sends the check of the image and every run of its
scripts to nodes of the same architecture. On a cluster with amd64 and arm64 nodes,
the environment works on the nodes of the architecture its build landed on.

### Policies on pods

The builds need the `privileged` Pod Security level for their namespace and
seccomp and AppArmor `Unconfined` (see above). A cluster whose admission policies
(Kyverno, Gatekeeper) forbid that for every namespace cannot build environments.

### A cluster without internet access

The builds download the base image, BuildKit and the packages. Point all four at your
mirrors:
- `scripts.builderImage`;
- `scripts.baseImages`: the language versions offered and their images, which can
  also be pinned by digest here;
- `scripts.pypiIndexUrl`;
- `scripts.cranUrl`.

Every variable is in the [configuration reference](configuration.md#scripting-environments-kubernetes).

## The scripts bucket

Create a bucket for the scripts only, and a key that works on that bucket and nothing
else. Tabularia writes the input of each Script node there and deletes it when the run
ends; the scripts read it, and write their output, with the same key. Tabularia's own
storage never reaches a script.

Give the key the right to **read, write and delete objects**, and **not** the right to
list the bucket. The scripts do not need to list it: they read and write files whose
names they are given. Without that right, a script cannot find the files of another
run in progress either.

AWS S3, an IAM user or role with:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
    "Resource": "arn:aws:s3:::tabularia-scripts/*"
  }]
}
```

MinIO, the same policy:

```bash
mc mb local/tabularia-scripts
mc admin policy create local tabularia-scripts policy.json   # the JSON above
mc admin user add local tabularia-scripts '<secret key>'
mc admin policy attach local tabularia-scripts --user tabularia-scripts
```

On **Scaleway**, use an IAM application with a bucket policy that grants it those three
actions on `tabularia-scripts/*`. On **Google Cloud Storage**, use an HMAC key of a
service account whose role on the bucket holds only `storage.objects.get`,
`storage.objects.create` and `storage.objects.delete`.

Add a **lifecycle rule** that expires the bucket's objects after a day. It removes what
a run that crashed left behind:

```bash
mc ilm rule add --expire-days 1 local/tabularia-scripts
```

In **Administration → Scripting environments**, fill in:
- the endpoint (empty for AWS);
- the bucket and the region;
- the access key and the secret key.

**Test** writes, reads and deletes a probe object from the engine, as the runs will.
It also warns when the key can list the bucket. The secret key is encrypted and cannot
be read back.

## Environments

**New environment** asks for a name, a language and its version, and the packages.
Packages go one per line:

- **Python**, as in `requirements.txt`: `scikit-learn==1.5.2`. `polars` and `pyarrow` are
  always installed.
- **R**: `dplyr`, or `data.table@1.16.0` for a fixed version. `arrow`, with S3 support,
  is always installed. CRAN publishes sources, so R packages compile during the build:
  the images carry the system libraries the usual packages need, and a build with
  `arrow` takes several minutes. It happens once per version.

A build gets its own CPU and memory, `SCRIPTS__BUILD_CPU` and `SCRIPTS__BUILD_MEMORY`
(2 cores and 4 GiB by default). The memory is requested whole, so a build lands only on
a node that has it. It runs one compiler per 1.5 GiB, at most one per core: a C++
compiler of `arrow` takes over 1 GB. A build that runs out of memory fails saying so;
on large nodes, raising both makes R builds faster.

**Search PyPI** / **Search CRAN** above the list finds a package by name in the index
the builds use and lists its versions. For Python it marks the versions that have no
wheel for the environment's Python: pip would compile those during the build, which
often fails. **Add to the list** writes the line.

Pin the versions: a line without one takes whatever is newest when the image is
built. Tabularia refuses a line that is not a package name and version, and says which
line it is.

Saving builds **version 1**. It goes through these states:
1. **Waiting to build**.
2. **Building**: a Job in the build namespace builds the image and pushes it.
3. **Checking**: a Job in the scripts namespace runs the new image, by digest, as a
   script would, loads the packages a script needs to read its input, and lists every
   installed package.
4. **Ready**: the version is in use.

The list of installed packages, the end of the build log and the image reference stay
with the version. A build that fails says why and keeps the version in use as it was.

Changing the version or the packages builds a new version. Changing anything else
applies to the next run without a build:
- the description;
- the resources (CPU and memory requested and limited, the longest run);
- who can use the environment;
- the variables.

**Build again** makes a new version from the same packages: after a failure, or to
start from a newer base image. **Use this version** puts an older version back; its
image is still in the registry.

### Who can use an environment

People in the environment's groups who can edit a flow can add Script nodes with it.
Administrators can use every environment.

Someone outside those groups **cannot open a flow with a Script node of the
environment**: its code is not theirs to read. The flow is still listed in Explore, in
Flows and in the search, greyed out with a lock and the reason on hover; it is not a
link and has no actions. Reached by its address, the editor says the same in place of
the canvas. Its versions, a copy, a run, a schedule and adding it to a group of flows
are refused the same way. An environment that no longer exists blocks nobody.

### Variables

The variables are what the scripts find in their environment, and they are separate
from Tabularia's. A **secret** variable is encrypted, never shown again, and given to
the scripts like the others.

Names starting with `TABULARIA_` or `AWS_` are reserved for what Tabularia sets in
every Job: where the input and the output are, and the bucket's key.

**From Tabularia** is the exception. It hands the scripts the value of one of
Tabularia's own variables, named in the field and read when each script starts.
Everyone who writes scripts with that environment can read it. The panel says so, and
the audit records who set it.

## The Script node

In the flow editor, **Script** is in the Control group of the palette for whoever may
use at least one environment. Choose its environment in the node panel, then **Edit
the code**.

### Input and output come from the edges

| the node has | the script gets |
|---|---|
| a data edge coming in | `TABULARIA_INPUT`: the step before it, one parquet file in the scripts bucket |
| a data edge going out | `TABULARIA_OUTPUT`: where it must write one parquet file; the nodes after it read that |
| neither | nothing to read or write: it runs as a step of the flow, for instance to start something outside Tabularia |

Polars, pyarrow and R's arrow read and write those `s3://` addresses by themselves:

```python
import os
import polars as pl

df = pl.scan_parquet(os.environ["TABULARIA_INPUT"])
df.with_columns(score=pl.col("amount") * 2).sink_parquet(os.environ["TABULARIA_OUTPUT"])
```

```r
library(arrow)
df <- read_parquet(Sys.getenv("TABULARIA_INPUT"))
write_parquet(df, Sys.getenv("TABULARIA_OUTPUT"))
```

A script that has nodes after it and writes nothing fails.

### Run, and the preview of the nodes after it

**Run** in the node panel runs the node on its own, as the flow is saved (with unsaved
changes the button is **Save and run**):
- the chain before it runs on the sources' development sample;
- the nodes after it do not run, and nothing is written to an Output;
- it runs in the trials' namespace, within its quota, never among the flows' runs.

It takes a few seconds once the environment's image is on the cluster's nodes. Its
log and its outcome show in the panel, and the run page lists it as a trial.

The nodes after a Script node preview what it wrote last: its latest trial or the
latest run of the flow, whichever is newer. The preview says which, and when; if the
saved code has changed since, it says so too. Before any of them, those nodes have no
preview.

### The code

The editor is Monaco, the editor of VS Code. On the left, the node's files: the one
marked ▶ is the one that runs, the others sit next to it in the same folder.
- In Python, `lib/scoring.py` is imported as `from lib import scoring`.
- In R, `source("R/helpers.R")` reads `R/helpers.R`.

On the right, what the script finds in its environment: Tabularia's variables, the
environment's variables (secret ones by name only), the installed packages. A click
inserts one at the cursor.

While typing, the editor offers:
- the variables, inside `os.environ["…"]`, `os.getenv("…")` and `Sys.getenv("…")`;
- the installed modules after `import` and `library(`;
- the public names of a module after its alias (`pl.` after `import polars as pl`)
  or after `pkg::`.

**Problems, before running.** Python code is checked by [Ruff](https://github.com/astral-sh/ruff)
in the gateway when typing pauses. It underlines names used and never defined or
imported, syntax errors, and imports or variables left unused; the list of files
counts each file's problems. An `import` of a module that is neither installed in
the environment (the list its build's check wrote), nor part of Python, nor one of
the node's files is underlined too. `SCRIPTS__LINT=false` turns the check off. R code
is not checked.

**The last run.** Under the code is the log of the node's last run. When it failed,
the editor marks the line it stopped at, in the file where it stopped, until that
file changes, and **Go to** opens it. For R, only a file read with `source()` that
does not parse gives a line.

| variable | |
|---|---|
| `TABULARIA_INPUT`, `TABULARIA_OUTPUT` | the step's files, or empty |
| `TABULARIA_STEP_ID` | the step's random id |
| `TABULARIA_RUN_ID` | the flow's run |
| `TABULARIA_FLOW_ID`, `TABULARIA_FLOW_NAME`, `TABULARIA_NODE_ID` | where the script is |
| `TABULARIA_ATTEMPT` | 1, then 2… when the node retries |
| `AWS_*` | the scripts bucket's key and endpoint, for the libraries |

**The path check.** The people who write scripts are trusted, and the scripts may
call any service. One thing is checked when the flow is saved: the code may not name
the scripts bucket, where the files of the other runs are. Read and write through
`TABULARIA_INPUT` and `TABULARIA_OUTPUT`.

### Running

A flow with a Script node runs as an orchestration, like one with a Refresh node.
- **Order.** The node runs after the steps it reads from and before the ones that
  read from it, with or without a sequence edge.
- **Limits.** **Timeout** stops it; **Retries** runs it again, as a new Job, after the
  delay.
- **Failure.** A failure stops the flow: the steps after it would read nothing.

The run page lists the node as a run of its own. It shows the environment and its
version, the attempts, the exit code, the variables it was given (secret ones by
name only) and the end of its log.

Whoever runs the flow, or the author of its schedule, must be allowed to use the
environment. A schedule set by someone else is switched off when its code changes,
as for any change that makes a flow reach further.

## Audit

Every change is in the audit log: the bucket, the environments, a version put in use,
every variable set or removed. A variable's value is never recorded.
