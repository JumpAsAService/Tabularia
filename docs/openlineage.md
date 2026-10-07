# OpenLineage: Tabularia in your data catalog

Tabularia derives the lineage of its own workspace for the Lineage page. Optionally it
also *tells* that lineage to an external catalog in the
[OpenLineage](https://openlineage.io) standard, with the official Python client, so
that Marquez, DataHub, Astro, OpenMetadata or any other collector sees what every run
read and wrote next to the lineage it already receives from other tools.

Nothing changes in Tabularia when it is on: events leave *after* the commit that
closes a run, from a background thread, and a collector that is down or slow never
fails a run.

## Turn it on

| Variable | Meaning |
|---|---|
| `OPENLINEAGE__URL` | The collector's base URL, e.g. `http://marquez:5000`. Empty = off. |
| `OPENLINEAGE__ENDPOINT` | Path of the lineage endpoint, default `api/v1/lineage` (Marquez, Astro). DataHub: `openapi/openlineage/api/v1/lineage`. |
| `OPENLINEAGE__API_KEY` | Bearer token, if the collector wants one. |
| `OPENLINEAGE__FILE` | Write the same events as JSON Lines to this path, instead of or besides the URL (air-gapped setups, custom pipelines). |
| `OPENLINEAGE__NAMESPACE` | The namespace of this installation's jobs, default `tabularia`. One per installation (`tabularia-prod`, `tabularia-staging`…). |
| `OPENLINEAGE__TIMEOUT_SECONDS` | HTTP timeout per attempt, default 10; an event is tried three times at most. |

The library's own configuration works too: `OPENLINEAGE_URL`, `OPENLINEAGE_API_KEY`,
or `OPENLINEAGE_CONFIG=/path/openlineage.yml` for composite or custom transports —
see the
[client documentation](https://openlineage.io/docs/client/python). The Kafka
transport needs the `confluent-kafka` package, which the image does not ship. Set the variables
on every gateway and orchestrator process (they are the ones that close runs); the
Helm chart takes them from `openlineage.*` and `secrets.openlineageApiKey`. The
gateway logs one line at start-up saying where events go.

Quick local collector for a test:

```sh
docker run -d --name marquez-db -e POSTGRES_USER=marquez -e POSTGRES_PASSWORD=marquez -e POSTGRES_DB=marquez postgres:16-alpine
docker run -d --name marquez --link marquez-db -p 5000:5000 -e POSTGRES_HOST=marquez-db -e POSTGRES_USER=marquez -e POSTGRES_PASSWORD=marquez -e POSTGRES_DB=marquez marquezproject/marquez
docker run -d --name marquez-web --link marquez -p 3005:3000 -e MARQUEZ_HOST=marquez -e MARQUEZ_PORT=5000 marquezproject/marquez-web
```

then `OPENLINEAGE__URL=http://marquez:5000` on a network the gateway shares with it.

## What is reported

Every run that reaches a terminal state sends a `START` and a `COMPLETE` (or `FAIL`)
`RunEvent`, with the real start and end times, so the catalog computes durations.

| Tabularia | OpenLineage job | Inputs | Outputs |
|---|---|---|---|
| A flow run (manual, scheduled, or from the editor) | `<folder path>/<flow name>` | every datasource the flow's source nodes read; a file uploaded in the editor | the union of what its output nodes wrote |
| An output node of that run | `<folder path>/<flow name>.<output>`, child of the flow run (`ParentRunFacet`) — `datasource <name>`, `table <name>`, `file <key>`, `email` | the flow's inputs | the datasource published, the table written, the S3/GCS object, the mirror copy |
| A nested flow (`Run flow` node) | its outputs stay under the nested flow's own job name | | |
| A datasource refresh (database or SharePoint), by hand, on schedule or inside a flow | `<folder path>/<datasource> (refresh)` | the table; the tables a SQL query reads; the SharePoint file | the datasource |

An email output is not a dataset: its job has no outputs.

**One job per node, whatever started it.** Marquez nests a job under its parent's
name when a run carries a `ParentRunFacet` (`<parent>.<child>`). So the parent facet
is sent only for the output nodes of the flow itself, which is where that hierarchy
is wanted; a refresh triggered by a flow and the outputs of a nested flow keep the
same job they have when they run on their own, and point at the orchestration that
contained them through the `tabularia` run facet (`parentRunId`). The same node run
from the editor and inside a schedule always lands in the same job.

Every run carries a `tabularia` run facet
([schema](openlineage/TabulariaRunFacet.json)): `runId` (`GET /runs/{runId}`),
`kind`, `trigger` (`manual`/`schedule`), `engine`, `parentRunId`.

### Dataset names

| Dataset | `namespace` | `name` | Facets |
|---|---|---|---|
| A Tabularia datasource | `tabularia://<namespace>` | `/<folder path>/<name>` — the name you see in Tabularia | `schema`, `documentation`, `datasetVersion` (the snapshot key), `symlinks` → its folder in the bucket (`s3://bucket` + `/datasets/<id>`, stable across snapshots), `dataSource` (uri of the parquet it serves now), `tabularia` (see below) |
| A database table | `postgres://host:5432`, `mysql://host:3306`, `clickhouse://host:8443`, `trino://host:8080` | `db.schema.table` (Postgres, Trino) · `db.table` (MySQL, ClickHouse) | `dataSource`; on outputs `lifecycleStateChange` (`OVERWRITE` for replace, `ALTER` for append) and `outputStatistics` |
| An object in a bucket | `s3://bucket` or `gs://bucket` | `/path/key` | `storage`, `outputStatistics` |
| A SharePoint file | `sharepoint://site host/sites/…` | `/path/file.xlsx#Sheet` | `dataSource` |
| A file uploaded in the editor | `s3://bucket` | `/uploads/…parquet` | `storage` |

Tables and objects follow the
[OpenLineage naming conventions](https://openlineage.io/docs/spec/naming), so a
catalog that also receives lineage from dbt, Airflow or Spark matches the same
nodes. Renaming or moving a datasource in Tabularia creates a new dataset in the
catalog; the id in the `tabularia` facet ties the two.

The `tabularia` facet on datasources — schema in
[`docs/openlineage/TabulariaDatasetFacet.json`](openlineage/TabulariaDatasetFacet.json)
— carries `datasourceId`, `projectId`, `kind` and `folder`, so a catalog can get
back to the object: `GET /datasources/{datasourceId}`.

Jobs carry `jobType` (`FLOW`, `OUTPUT`, `REFRESH`), `documentation` (the flow's
description) and, for a SQL-query datasource, `sql` (the query, with its dialect).
Failed runs carry `errorMessage`.

## Static export: dependencies without running

`GET /flows/{id}/openlineage` returns a `JobEvent` built from the flow's
*definition*: the job, what it reads, what it writes — without executing anything.
It is also in the Flows page, under *Export*. A datasource the flow will create at
its first run already has the name it will have.

`POST /flows/{id}/openlineage` sends that event to the configured collector: use it
to populate a catalog with the flows that exist before any of them runs, or after
changing a flow's definition. Both need VIEW on the flow's folder.

## What leaves the installation

Only metadata: folder and flow names, flow descriptions, datasource names and column
names with their types and descriptions, table names, the text of the SQL query of a
query-based datasource, bucket paths, row counts, run times and the error message of
a failed run. No rows of data, no user names or e-mail addresses, no credentials
(connection passwords never reach the gateway's events). Mind that a query text or
an error message can quote a literal value: if that matters, keep the collector inside
the same trust boundary as Tabularia.

## Guarantees and limits

- **Never in the way.** Events are built in the transaction that closes the run,
  from the data already written, and handed to a worker thread through a bounded
  queue (500 closed runs per process, at most ~30 MB): closing a run never waits for
  the collector. The
  thread tries each event three times at most, `OPENLINEAGE__TIMEOUT_SECONDS` each;
  if the collector refuses or does not answer, the run is still closed and a warning
  with the run's id is in the log of the process that closed it. When the queue is full — a collector down for long — new
  events are dropped and the log says so once a minute. A process that stops (a
  rolling update) waits up to 5 seconds for its queue to drain, no more. An event is
  not retried later: lineage is a chronicle, not a contract; the static export can
  rebuild the dependencies at any time.
- **Cost.** Building a run's events in the closing transaction takes 5–15 ms of
  Python and 6–17 reads (a datasource of 150 columns, a flow with three sources);
  serialising an event takes 3 ms in the worker thread; a full queue of 500 wide runs
  holds about 28 MB. Under a storm of runs (eleven flows in a loop, about two runs
  closing per second) the API processes serve the same requests per second with
  lineage on or off, whether the collector is fast or answers in 2 seconds, and the
  processes that emit spend about 5 points of a core more. A collector running on
  the same machine is a different matter: Marquez takes about one core while it
  ingests, and that is what slows everything else down.
- **Replicas.** Each gateway and orchestrator process sends its own events; the run
  id is a UUID derived from the run's id in Tabularia, the same from any process.
- **Dataset level.** Column-level lineage is not emitted yet.
- `Foreach` loops report the datasets of the flow, not each iteration.
