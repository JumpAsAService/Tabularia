# Upgrading Tabularia on Kubernetes and minikube

How to move an installation made with the Helm chart to a newer release: what an upgrade
changes and what it keeps, how the database schema is migrated, how the new pods replace
the old ones, and what to do when something goes wrong. The first part applies to every
installation; then come the steps [on minikube](#on-minikube) and
[on a Kubernetes cluster](#on-a-kubernetes-cluster).

In short:

1. read the upgrade notes of every release between yours and the new one;
2. back up PostgreSQL;
3. run `helm upgrade` with **the same values file** you installed with;
4. check that every pod is ready and the database is at the new revision.

## What an upgrade changes, and what it keeps

The chart runs no database and keeps nothing on a volume: its pods can be replaced at
any moment. What Tabularia knows lives outside them.

| What | Where | During an upgrade |
|---|---|---|
| Users, folders, permissions, flows and their versions, connections, schedules, runs, the audit log | PostgreSQL | **kept**; its schema is migrated forward. Back it up first |
| The data: uploaded files, datasource snapshots, flow outputs | the object storage bucket | kept; an upgrade does not rewrite it |
| The automatic backups of PostgreSQL | the bucket, under `backups/postgres/` (or a bucket of their own) | kept |
| The task queue and the step cache's index | Valkey | kept if Valkey keeps running; losing it only drops queued tasks and starts the cache cold |
| Scripting environments' images | your container registry | kept |
| The secrets: Fernet key, JWT secret, passwords | your values file (or your own Secret) | whatever the values say: **pass the same ones** |
| Temporary files of a pod | `/tmp`, an `emptyDir` | gone with the pod, by design |

The **Fernet key** encrypts the passwords saved in connections: if an upgrade runs with a
different one, those passwords can no longer be read and must be typed again. The JWT
secret signs the sessions: a new one signs everyone out.

## What happens during `helm upgrade`

`helm upgrade` changes the Deployments; Kubernetes then replaces their pods, all
Deployments at the same time.

- **The gateway and the orchestrator** roll one pod at a time, a new pod ready before an
  old one stops (`maxSurge: 1`, `maxUnavailable: 0`): the API keeps answering.
- **Each new gateway and orchestrator pod migrates the database first**, in an init
  container called `migrate` that runs `python -m app.db.migra` before the application
  starts. The first one does the work, in one transaction (all or nothing); the others
  wait on a PostgreSQL lock and find it done. The application itself never migrates: it
  checks that the database is not behind its code and refuses to start if it is.
- **The old pods keep working on the migrated database.** Every revision is written so
  that the previous release still runs on it (columns are added, never renamed or
  dropped in the same release; see [database migrations](../database-migrations.md)).
  That is what lets the old and the new pods serve side by side for a minute.
- **The workers** stop taking tasks and finish the one in progress before stopping, up to
  `worker.terminationGracePeriodSeconds` (one hour) and
  `previewWorker.terminationGracePeriodSeconds` (two minutes). A run longer than that is
  cut and marked failed.
- **Beat** is a single pod: the old one stops before the new one starts, a pause of a few
  seconds in the periodic housekeeping.
- **Flow executions running on an old orchestrator pod are interrupted** when that pod
  stops: they are closed as failed, with a message that says so, and are not resumed.
  The Jobs of Script and Container nodes they had started are stopped. Scheduled flows
  are not lost: the new orchestrator takes the next slots.
- **The interface** rolls like the gateway. A browser tab left open keeps the old version
  until it is reloaded.

A change to the values alone (a setting, a password) rolls the pods the same way: the
pods carry a checksum of the chart's ConfigMap and Secret.

## Before you start

- **Read the upgrade notes** of every release between yours and the new one: the
  "Upgrade note" entries in [CHANGELOG.md](../../CHANGELOG.md) and the
  [chart README → Upgrades](../../infrastructure/helm/tabularia/README.md#upgrades).
  A few releases ask for a step of their own (a value to set first, two upgrades in a
  row). Skipping releases is fine for the database (the migration applies every
  revision in order), but the notes of each one still apply.
- **Back up PostgreSQL.** The simplest way: **Administration → Backups → Back up now**,
  and wait for it to say *Last backup … ago*. Tabularia makes one every night anyway
  (03:00 by default), into the object storage; the steps below make one by hand too. A
  revision goes forward only: going back to an older release after a broken upgrade
  means restoring a backup.
- **Use the values file you installed with.** Never run `helm upgrade` without
  `--values`: the chart's defaults would replace your settings and empty the secrets.
- **Pick a quiet moment**: no long flow running. **Runs** shows what is running;
  **Administration → Cluster** shows the pods, and the scripts and containers at work.

## On minikube

The guide [Try Tabularia on your computer with minikube](minikube.md) puts PostgreSQL and
Valkey in the namespace `tabularia-deps`, and PostgreSQL's data on a volume (a
PersistentVolumeClaim called `postgres`) on minikube's disk. That volume survives
`minikube stop` and `minikube start`, and every upgrade.

### 1. Back up the database

In Tabularia, **Administration → Backups → Back up now**: a few seconds later the backup
is in your bucket, and **Download** saves a copy on your computer. Or by hand, without
Tabularia: the backup is written inside the PostgreSQL pod, then copied to your computer.

```bash
kubectl exec --namespace tabularia-deps deploy/postgres -- pg_dump --username tabularia --format custom --file /tmp/tabularia.dump tabularia
```

Then the name of the pod, and the copy (the second command is the same in every shell).

macOS and Linux:

```bash
POD=$(kubectl get pod --namespace tabularia-deps -l app=postgres -o jsonpath='{.items[0].metadata.name}')
```

Windows (PowerShell):

```powershell
$POD = kubectl get pod --namespace tabularia-deps -l app=postgres -o jsonpath='{.items[0].metadata.name}'
```

```bash
kubectl cp "tabularia-deps/${POD}:/tmp/tabularia.dump" tabularia-backup.dump
```

`tabularia-backup.dump` is now in the folder your terminal is in. Keep it until you have
used the new release for a while.

> Copy the file out with `kubectl cp`, not with `kubectl exec … > file`: on Windows
> PowerShell the `>` rewrites binary output, and the backup would not restore.

### 2. Get the new release

Download and unzip the new release as in step 3 of the minikube guide, copy your
`my-values.yaml` into its folder, and open the terminal there. The images follow the
release: `my-values.yaml` does not name a version.

If the upgrade notes say that `infrastructure/minikube/dependencies.yaml` changed,
apply it again. This is safe: the volume and its data stay.

```bash
kubectl apply -f infrastructure/minikube/dependencies.yaml
```

> **Never delete the namespace `tabularia-deps`** (nor run `minikube delete`) to "start
> clean": the volume goes with it, and Tabularia's records with the volume.

### 3. Upgrade

```bash
helm upgrade tabularia infrastructure/helm/tabularia --namespace tabularia --values my-values.yaml --wait --timeout 20m
```

`--wait` returns when every new pod is ready, or fails after twenty minutes; the first
time a release runs, minikube downloads its images, which takes a few minutes.

### 4. Check

```bash
kubectl get pods --namespace tabularia
kubectl exec --namespace tabularia deploy/tabularia-tabularia-gateway -c gateway -- python -m app.db.migra stato
```

Every pod is `Running` and `1/1`, and the second command prints the same revision for the
database and the code, for example `database: 0020 · code: 0020`. Reload the page in the
browser with **Ctrl+Shift+R** (macOS: **Cmd+Shift+R**).

## On a Kubernetes cluster

PostgreSQL is usually a managed service, or runs in the cluster under your team's care:
back it up with the tools that come with it (the provider's snapshot or point-in-time
restore, or `pg_dump --format custom`). Then:

```bash
helm upgrade <release> infrastructure/helm/tabularia --namespace <namespace> \
  --values values-production.yaml --wait --timeout 30m
```

Follow it with `kubectl rollout status`, one Deployment at a time, or on
**Administration → Cluster**:

```bash
kubectl rollout status deployment/<release>-tabularia-gateway --namespace <namespace>
kubectl rollout status deployment/<release>-tabularia-orchestrator --namespace <namespace>
kubectl rollout status deployment/<release>-tabularia-worker --namespace <namespace> --timeout 70m
```

The workers can take up to their grace period (one hour) to finish the runs in progress:
give their `rollout status` a longer timeout, or `helm upgrade` a `--timeout` longer than
the longest run.

What the migration did is in the log of the `migrate` init container of a new pod, and the
revision can be read at any moment:

```bash
kubectl logs deployment/<release>-tabularia-gateway -c migrate --namespace <namespace>
kubectl exec deployment/<release>-tabularia-gateway -c gateway --namespace <namespace> -- python -m app.db.migra stato
```

The log ends with `database at revision 0021 (was 0020)` when a migration ran, and with
`database at revision 0021` when another pod had already done it.

**GitOps** (Argo CD, Flux): nothing to change. The init container is idempotent, and a sync
that rolls the pods twice migrates once.

### Migrations in your own pipeline

To migrate in a step of your own (a pipeline job, a change window), set
`migrations.enabled: false` and run the same command with the new image before
`helm upgrade`. The pods then only check the revision. A Job that does it, reading the
release's ConfigMap and Secret:

```yaml
apiVersion: batch/v1
kind: Job
metadata: { name: tabularia-migrate-1-2-2, namespace: <namespace> }
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: <registry>/tabularia-gateway:1.2.2
          command: ["python", "-m", "app.db.migra"]
          envFrom:
            - configMapRef: { name: <release>-tabularia-config }
            - secretRef: { name: <release>-tabularia-secrets }
```

It needs only the database's address and credentials, which the release already holds.
Wait for it (`kubectl wait --for=condition=complete job/tabularia-migrate-1-2-2`), then
upgrade.

## When something goes wrong

**A new gateway or orchestrator pod stays in `Init:Error` or `Init:CrashLoopBackOff`.**
The migration failed; read why:

```bash
kubectl logs <the new pod> -c migrate --namespace <namespace>
```

Nothing was half done: the migration is one transaction, and a failure rolls it back.
The old pods keep serving (`maxUnavailable: 0`), so Tabularia still works on the previous
release. Common causes: the database is unreachable, or its user may not change the
schema (the migration needs the rights of the tables' owner). Fix the cause; the pod
retries by itself. To give up instead, roll back (below).

**A new pod stays `0/1` or restarts.** `kubectl describe pod` and `kubectl logs` say why.
A gateway that says *the database is at revision … but this version of the gateway
needs …* found the database behind its code: the migration did not run
(`migrations.enabled: false` without your own step).

**Roll back to the previous release.**

```bash
helm history tabularia --namespace tabularia
helm rollback tabularia <the revision before> --namespace tabularia --wait --timeout 20m
```

The pods go back to the previous images and values; the database stays migrated, which
the previous release accepts: its `migrate` step finds a revision it does not know, does
nothing, and the gateway starts with a warning that the database is newer than its code.
This holds for **one release back**, and **from 1.2.2 on**: 1.2.1 and earlier stop on a
database migrated by a newer release (their new pods stay in `Init:CrashLoopBackOff`, and
the release is left half rolled back), so going back to them means restoring the backup
taken before the upgrade, below. Further back, or when the data itself is in question,
restore the backup too.

**Restore the backup.** First the file: the one of step 1, or one of the automatic
backups — **Download** in Administration → Backups while Tabularia still works, otherwise
straight from the bucket, in your provider's console or with its tools (the files are
under `backups/postgres/`, named after the time they were made, in UTC). Tabularia makes
them with the `pg_dump` of the server's own version, so the server's own `pg_restore`
reads them; a newer `pg_restore` does too, an older one does not. Then on minikube, with
the file in the current folder as `tabularia-backup.dump`:

1. Stop everything that uses the database (the HorizontalPodAutoscalers and KEDA, if you
   turned them on, would start the pods again: roll back first, see point 4):

   ```bash
   kubectl scale deployment --namespace tabularia --replicas 0 -l app.kubernetes.io/instance=tabularia
   kubectl wait --for=delete pod --namespace tabularia -l app.kubernetes.io/instance=tabularia --timeout 70m
   ```

   The second command returns when the last pod is gone: a worker finishing a run can
   take up to its grace period.

2. Copy the backup into the PostgreSQL pod (with `POD` set as in step 1):

   ```bash
   kubectl cp tabularia-backup.dump "tabularia-deps/${POD}:/tmp/restore.dump"
   ```

3. Recreate the database empty and load the backup. Recreating it, rather than restoring
   over it, also removes the tables the newer release had added:

   ```bash
   kubectl exec --namespace tabularia-deps deploy/postgres -- dropdb --username tabularia --force tabularia
   kubectl exec --namespace tabularia-deps deploy/postgres -- createdb --username tabularia tabularia
   kubectl exec --namespace tabularia-deps deploy/postgres -- pg_restore --username tabularia --dbname tabularia \
     --no-owner --no-privileges --single-transaction --exit-on-error /tmp/restore.dump
   ```

   All or nothing: the first error stops it and leaves the database empty. If it prints
   an error, or ends with *errors ignored on restore*, or its exit status is not 0, do not
   start the release: an empty database would be set up again as a new installation.

4. Start the release the backup belongs to: `helm rollback` to its revision (which also
   sets the number of pods back), or `helm upgrade` from that release's folder.

On a cluster, the same steps with your database's own restore in point 3.

## PostgreSQL in the cluster

When PostgreSQL runs in the cluster, as on minikube and kind, its volume is what holds
Tabularia's records:

- **One pod at a time.** Its Deployment uses `strategy: Recreate` and a `ReadWriteOnce`
  volume: the old pod stops before the new one starts. Two PostgreSQL servers on the
  same data directory would corrupt it.
- **The volume outlives the pod, not the namespace.** Deleting the PersistentVolumeClaim,
  or the namespace that holds it, deletes the data when the storage class says
  `reclaimPolicy: Delete` (the default of most, minikube's and kind's included). See it
  with `kubectl get pv`.
- **A new minor PostgreSQL version** (16.4 → 16.8) needs nothing: the same image tag
  (`postgres:16-alpine`) restarted picks it up, on the same files.
- **A new major version** (16 → 17) does not read the old files: back up, delete the
  Deployment and the PersistentVolumeClaim, apply the new image with an empty volume,
  and restore the backup into it as above.

Valkey has no volume in these setups, on purpose: what it holds can be lost (queued
tasks, the cache index), and the workers rebuild it.
