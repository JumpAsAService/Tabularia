# Spec: the Container node and container registries

Status: done (pieces 1, 2 and 3). Decided with the owner on 2026-10-21. The user's
guide: [docs/container-node.md](../container-node.md).

## Objective

A step of a flow that runs **an image of the customer's**, as a Kubernetes Job, the way
Airflow's KubernetesPodOperator does: a Spark or dbt job the data team already packaged,
a model in an image, a company tool that has run in a container for years. Tabularia
orchestrates it (order, schedule, retries, log, outcome); the code and the image are
not Tabularia's.

It is not the Script node: there Tabularia builds the image from an environment an
administrator defines, and that stays internal to Tabularia. Here the image comes
ready from a registry.

## Decisions

- **Registries are administrators' connections**, in the admin panel only
  (Administration → Container registries). Static credentials first (Docker Hub, GHCR,
  GitLab, Harbor, Scaleway); registries with short-lived tokens (ECR) and the cluster's
  own identity later.
- **Access in the admin panel only, by group or user** (as the scripting environments,
  not tied to folders), each with all the registry's repositories or some of them. In
  the editor only what one may use is shown; **a flow that uses a registry or an image
  one may not use does not open** (nor its versions, copies, runs, schedules, groups of
  flows). `services/registry_access.py`.
- **An allowlist of repositories per registry**, decided by the administrator. A public
  registry also goes through a connection (with no credentials): no image runs that an
  administrator did not allow.
- **Insecure registries are supported**, because many companies have them: HTTP for an
  internal registry, a company authority's certificate (recommended), or a certificate
  not checked (recorded in the audit). The gateway's calls follow those settings; the
  nodes pull the images, so they must trust the registry too: Tabularia says so and
  cannot change it from a pod.
- **The containers' namespace is at the Pod Security `baseline` level**: root is allowed
  inside the container (most existing images run as root), privileges and host access
  are not. The scripts stay `restricted`.
- **Variables: administrator and node.** Secrets and shared variables on the registry
  (encrypted); plain variables also on the node.
- **Every run records the image's digest**: the tag is resolved when the run starts.
- **From the Script node**: the Job, the log, the exit code, the retries, the trial in a
  namespace with a quota, the optional input and output through the scripts bucket, the
  run as a `kind` of its own, the pull failures detected at once and explained.

## Pieces

1. **The registries** (done): `services/container_registry.py` speaks the Docker
   Registry HTTP API v2 (ping, Basic or Bearer token, tags page by page, tag → digest;
   public authorities, company authority or none; HTTP; never the cloud metadata
   addresses). `routes/registries.py`, the tables of migration 0018
   (`container_registries`, `_variables`, `_groups`), the admin section, the audit
   (`registry.*`), `CONTAINERS__ENABLED` and `containers.enabled`.
   [docs/container-registries.md](../container-registries.md).
2. **The cluster** (done): the containers' namespace (`baseline`) and its trials' one
   with a quota (chart `templates/containers.yaml`, `CONTAINERS__*`), a pull Secret per
   registry kept in step by Tabularia (written right before a Pod needs it, removed
   with the registry), and **Try in the cluster**
   (`POST /admin/registries/{id}/cluster-test`, `services/container_cluster.py`): a Pod
   with the image by digest and a command that does not exist, so the node downloads it
   and runs nothing; its pull error is translated (`kube_jobs.pull_failure`).
3. **The Container node** (done): registry, repository and tag (with the tags read
   from the registry), command and arguments, variables, CPU and memory, timeout,
   retries; the digest resolved and recorded; who may use it: the registry's access
   plus EDIT on the flow's folder. `services/container_nodes.py` (what a node holds,
   the checks at save), `services/container_runs.py` (a run, reusing the Script node's
   Job loop), `kind="container"` runs, the editor's **Run** through the same route as
   the Script node's, the schedules (`registryId` and the node's fingerprint widen
   them), the dbt export (`container_not_exportable`). Limits: `CONTAINERS__MAX_*`.
