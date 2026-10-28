"""OpenLineage: Tabularia's lineage told to an external catalog.

The internal lineage (`services.lineage`) stays as it is: derived on the fly,
filtered by RBAC, shown on the Lineage page. This module is extra and OPTIONAL: when
`OPENLINEAGE__URL` (or `__FILE`) is not set it does nothing. When on, every run that
closes becomes a `RunEvent` of the OpenLineage standard, with the official library,
towards Marquez or any other compatible collector:

- a flow's run = a job (name = the folder's path + the flow's name), the runs of its
  Output nodes = child jobs with `ParentRunFacet`;
- the refresh of a datasource from a database or SharePoint = a job of its own;
- a SQL script's write, a saved script's run, a notebook's session (its reads), a
  flow group's run (with its Container and Pushmail cells as child jobs) and the
  Script and Container nodes of a flow = jobs too (see `run_events` and
  `notebook_session`);
- inputs: the datasource read (with the name it has in Tabularia, under the folder's
  path; the parquet in the bucket as a symlink, the id in the `tabularia` facet),
  the database table, the SQL query (tables extracted with sqlglot, text in the
  `sql` facet), the SharePoint file; outputs: the published datasource, the table
  written, the S3/GCS object, the mirror copy, the email of an Output node or of a
  Pushmail cell (the SMTP server, never the recipients).

External datasets follow OpenLineage's naming conventions (namespace
`postgres://host:port`, name `db.schema.table`; `s3://bucket` + path), so a catalog
that receives lineage from other tools too matches the same nodes. Tabularia's
datasources instead are named as in Tabularia's catalog (`tabularia://<namespace>` +
`/Folder/Subfolder/name`): the name whoever looks at the catalog recognizes; the
parquet's physical path is in the `symlinks` facet (and in `dataSource.uri`), the id
and the folder in the `tabularia` facet.

Renaming or moving a flow, a datasource or a folder changes these names: the
catalog gets the objects under the new name (datasources with the standard
`lifecycleStateChange` RENAME facet, naming the one they were) and, when the
collector is Marquez, the old name is deleted through its API.

The events go out AFTER the commit that closes the run, from a service thread with a
BOUNDED queue: a collector down or slow never stops a run, does not hold the
transaction open and does not fill the memory (with the queue full the event is
dropped, with a warning in the log). A lost event is not retried: lineage is a
chronicle, not a contract; the static export (`job_events_for_flow`) rebuilds the
dependencies from the definition anyway.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Iterable

from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import Connection, Datasource, Flow, FlowGroup, Notebook, Run, SavedScript, SqlScript
from app.services import permissions as perm_service

logger = logging.getLogger(__name__)

PRODUCER = "https://github.com/JumpAsAService/Tabularia"
QUEUE_MAX = 500                # closed runs waiting to be sent, per process: past this, dropped
                               # (measured: 57 KB per run with a 150-column datasource → at most ~28 MB)
_LOCK = threading.Lock()
_client: Any = None            # OpenLineageClient, built once (or injected by the tests)
_client_timeout: Any = None    # the timeout it was built with: changed from the panel → rebuilt
_queue: "queue.Queue[tuple[int, list] | None] | None" = None
_worker: threading.Thread | None = None
_dropped = 0                  # events dropped with the queue full (for the log and the tests)
_last_warning = 0.0


# ── switching on ─────────────────────────────────────────────────────────────
def enabled() -> bool:
    s = get_settings().openlineage
    return bool(s.url or s.file or os.environ.get("OPENLINEAGE_URL") or os.environ.get("OPENLINEAGE_CONFIG"))


def describe() -> str:
    """One line for the start log: where the events go, or that they do not."""
    s = get_settings().openlineage
    if not enabled():
        return "OpenLineage off (no OPENLINEAGE__URL nor OPENLINEAGE__FILE)"
    where = [x for x in (f"HTTP {s.url} ({s.endpoint})" if s.url else "", f"file {s.file}" if s.file else "",
                        "the library's configuration (OPENLINEAGE_URL/OPENLINEAGE_CONFIG)" if not (s.url or s.file) else "") if x]
    return f"OpenLineage on: events to {' + '.join(where)}, namespace «{s.namespace}»"


def set_client(client: Any) -> None:
    """For the tests: a client with a transport that captures the events."""
    global _client, _client_timeout
    with _LOCK:
        _client, _client_timeout = client, None


def _get_client() -> Any:
    global _client, _client_timeout
    with _LOCK:
        s = get_settings().openlineage
        if _client is not None and (_client_timeout is None or _client_timeout == s.timeout_seconds):
            return _client
        from openlineage.client import OpenLineageClient
        from openlineage.client.transport import get_default_factory

        _client_timeout = s.timeout_seconds
        configurations = []
        if s.url:
            # few attempts: a dead collector must not hold the thread for minutes on every event
            # (the queue is bounded, but meanwhile it piles up)
            http = {"type": "http", "url": s.url, "endpoint": s.endpoint, "timeout": s.timeout_seconds,
                    "retry": {"total": 2, "connect": 2, "read": 2, "backoff_factor": 0.3, "status_forcelist": [500, 502, 503, 504], "allowed_methods": ["HEAD", "POST"]}}
            if s.api_key:
                http["auth"] = {"type": "api_key", "apiKey": s.api_key}
            configurations.append(http)
        if s.file:
            configurations.append({"type": "file", "log_file_path": s.file, "append": True})
        if len(configurations) == 1:
            _client = OpenLineageClient(transport=get_default_factory().create(configurations[0]))
        elif configurations:
            _client = OpenLineageClient(transport=get_default_factory().create(
                {"type": "composite", "transports": configurations, "continue_on_failure": True}))
        else:
            _client = OpenLineageClient()  # the environment's OPENLINEAGE_URL / OPENLINEAGE_CONFIG, as the standard wants
        return _client


def _sender_loop(queue_: "queue.Queue") -> None:
    """The sending thread: takes from ITS queue and sends, until it receives None."""
    while True:
        item = queue_.get()
        if item is None:
            queue_.task_done()
            return
        run_id, job = item
        try:
            # a rename's events are built here, with a session of their own
            events, old_ones = job() if callable(job) else (job, [])
            _emit(events)
            if isinstance(run_id, int):
                logger.info("openlineage: %d events for run %s", len(events), run_id)
            elif old_ones:
                logger.info("openlineage: %d events for a %s, %d old names removed", len(events), run_id, _remove_old_names(old_ones))
            else:
                logger.info("openlineage: %d events for %s", len(events), run_id)
        except Exception as e:  # noqa: BLE001 — the run is closed already: lineage must weigh on nothing
            logger.warning("openlineage: events of run %s not sent: %s", run_id, str(e)[:300])
        finally:
            queue_.task_done()


def _enqueue(run_id: int | str, events: "list | Callable[[], tuple[list, list]]") -> bool:
    """Queues the events without ever waiting. With the queue full it drops them.
    `events` can also be a function the service thread calls to build them, which
    returns the events and the old names to remove."""
    global _queue, _worker, _dropped, _last_warning
    with _LOCK:
        if _queue is None:
            _queue = queue.Queue(maxsize=QUEUE_MAX)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_sender_loop, args=(_queue,), name="openlineage", daemon=True)
            _worker.start()
    try:
        _queue.put_nowait((run_id, events))
        return True
    except queue.Full:
        _dropped += 1
        if time.monotonic() - _last_warning > 60:
            _last_warning = time.monotonic()
            logger.warning("openlineage: queue full (%d runs waiting), dropping the events of run %s (%d dropped so far): the collector is not answering",
                           QUEUE_MAX, run_id, _dropped)
        return False


# ── names ────────────────────────────────────────────────────────────────────
def namespace() -> str:
    return get_settings().openlineage.namespace


def run_uuid(run_id: int) -> str:
    """A stable UUID per run: the same run, the same id, from any process."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tabularia://{namespace()}/runs/{run_id}"))


def _project_path(session: Session, project_id: int | None) -> str:
    if project_id is None:
        return ""
    projects = perm_service._all_projects(session)
    ids = perm_service.ancestor_ids({i: p.parent_id for i, p in projects.items()}, project_id)
    return "/".join(projects[i].name for i in reversed(ids) if i in projects) if ids else ""


def job_name_for_flow(session: Session, flow: Flow) -> str:
    path = _project_path(session, flow.project_id)
    return f"{path}/{flow.name}" if path else flow.name


def job_name_for_refresh(session: Session, ds: Datasource) -> str:
    path = _project_path(session, ds.project_id)
    return f"{path}/{ds.name} (refresh)" if path else f"{ds.name} (refresh)"


# the other objects that run: «<folder path>/<name> (<kind>)». The kind in the name keeps
# a SQL script, a saved script, a notebook and a flow group apart from a flow of the
# same name in the same folder (each kind has names unique per folder, not across kinds)
OBJECT_KINDS = {"sql_script": (SqlScript, "SQL script"), "saved_script": (SavedScript, "script"),
                "notebook": (Notebook, "notebook"), "flow_group": (FlowGroup, "flow group")}


def job_name_for(session: Session, kind: str, obj: Any) -> str:
    """The catalog's job of a flow, a SQL script, a saved script, a notebook or a flow group."""
    if kind == "flow":
        return job_name_for_flow(session, obj)
    path = _project_path(session, obj.project_id)
    name = f"{path}/{obj.name}" if path else obj.name
    return f"{name} ({OBJECT_KINDS[kind][1]})"


_PORTS = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306, "clickhouse": 8443, "trino": 8080, "oracle": 1521, "sqlserver": 1433}
_SCHEMES = {"postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql", "clickhouse": "clickhouse", "trino": "trino", "oracle": "oracle", "sqlserver": "tsql"}
# the namespace URI's scheme, when it is not the sqlglot dialect's name (OpenLineage's
# convention: `mssql://host:port`)
_NAMESPACE = {"sqlserver": "mssql"}


def _host_port(conn: Connection) -> str:
    host = (conn.host or "").split("://")[-1].rstrip("/")
    if ":" in host:
        return host
    return f"{host}:{conn.port or _PORTS.get(conn.db_type, 0)}"


def _storage_namespace(bucket: str) -> str:
    # the gateway has no storage configuration: it reads the engine's endpoint from the
    # shared environment, when there is one (GCS through the S3 API → `gs://`)
    endpoint = (os.environ.get("STORAGE__ENDPOINT") or "").lower()
    return f"{'gs' if 'googleapis' in endpoint else 's3'}://{bucket}"


def table_dataset_name(conn: Connection, table: str) -> str:
    """`db.schema.table` for Postgres and Trino, `db.table` for MySQL and ClickHouse, as
    OpenLineage's naming convention wants. A name the user qualified already
    (`schema.table`) is respected."""
    if conn.db_type == "oracle":
        # `service.SCHEMA.TABLE`, with Oracle's rule (as the import): without quotes upper
        # case, in quotes as it is; the default schema is the connection's, or the user
        def _oracle(part: str) -> str:
            part = part.strip()
            return part[1:-1] if len(part) > 1 and part[0] == part[-1] == '"' else part.upper()

        names = [_oracle(p) for p in table.strip().split(".") if p.strip()]
        if len(names) == 1:
            names = [_oracle(conn.db_schema) if conn.db_schema else (conn.username or "").upper(), *names]
        return ".".join(p for p in [conn.database, *names] if p)
    parts = [p.strip('"`[]') for p in table.strip().split(".") if p.strip()]
    kind = conn.db_type
    if kind == "sqlserver":
        # `database.schema.table`; the default schema is the connection's, or dbo
        if len(parts) == 1:
            parts = [conn.db_schema or "dbo", *parts]
        if len(parts) == 2:
            parts = [conn.database, *parts]
        return ".".join(p for p in parts if p)
    if kind in ("postgresql", "trino"):
        if len(parts) == 1:
            parts = [conn.db_schema or "public", *parts] if kind == "postgresql" else [conn.db_schema or "default", *parts]
        if len(parts) == 2:
            parts = [conn.database, *parts]
    else:
        if len(parts) == 1:
            parts = [conn.database, *parts]
    return ".".join(p for p in parts if p)


@functools.lru_cache(maxsize=512)
def _qualified_tables(db_type: str | None, sql: str) -> tuple[str, ...]:
    """The qualified names of the tables a query reads (a sqlglot parse, a few
    milliseconds): cached by text and dialect, because the same datasource closes run
    after run. Best effort: a query sqlglot does not understand gives nothing."""
    try:
        import sqlglot
        from sqlglot import exp

        tree = sqlglot.parse_one(sql, read=_SCHEMES.get(db_type))
        cte = {c.alias_or_name for c in tree.find_all(exp.CTE)}
        return tuple(sorted({".".join(p for p in (t.catalog, t.db, t.name) if p) for t in tree.find_all(exp.Table) if t.name and t.name not in cte}))
    except Exception:  # noqa: BLE001 — an odd query must not break the lineage
        return ()


def _tables_in_sql(conn: Connection, sql: str) -> list[str]:
    """The tables a query reads, with the connection's standard names (the query's text
    stays in the `sql` facet)."""
    return sorted({table_dataset_name(conn, q) for q in _qualified_tables(conn.db_type, sql)})


# ── dataset ──────────────────────────────────────────────────────────────────
def _schema_facet(columns_json: str | None):
    from openlineage.client.facet_v2 import schema_dataset

    try:
        cols = json.loads(columns_json or "[]")
    except json.JSONDecodeError:
        cols = []
    fields = [schema_dataset.SchemaDatasetFacetFields(name=str(c.get("name")), type=str(c.get("dtype") or ""))
             for c in cols if isinstance(c, dict) and c.get("name")]
    return schema_dataset.SchemaDatasetFacet(fields=fields) if fields else None


@functools.cache
def _tabularia_facet():
    """The `tabularia` facet: the datasource's id, the folder and the kind — what is needed
    to go back from the catalog to the Tabularia object (through the API:
    /datasources/<id>). The class is defined ONCE: building it with attrs costs
    milliseconds."""
    import attr
    from openlineage.client.facet_v2 import DatasetFacet

    @attr.define
    class TabulariaDatasetFacet(DatasetFacet):
        datasourceId: int = attr.field()  # noqa: N815
        projectId: int = attr.field()  # noqa: N815
        kind: str = attr.field()
        folder: str = attr.field()

        @staticmethod
        def _get_schema() -> str:
            return f"{PRODUCER}/blob/main/docs/openlineage/TabulariaDatasetFacet.json"

    return TabulariaDatasetFacet


def datasource_name(session: Session, ds: Datasource) -> str:
    path = _project_path(session, ds.project_id)
    return f"/{path}/{ds.name}" if path else f"/{ds.name}"


def datasource_dataset(session: Session, ds: Datasource, output: bool = False, rows: int | None = None):
    """The datasource as a dataset: the NAME it has in Tabularia under the folder's path;
    the current snapshot's parquet as a symlink and in the `datasetVersion` facet; the id
    in the `tabularia` facet."""
    from openlineage.client.event_v2 import InputDataset, OutputDataset
    from openlineage.client.facet_v2 import (
        dataset_version_dataset, datasource_dataset, documentation_dataset, output_statistics_output_dataset, storage_dataset,
        symlinks_dataset,
    )

    ns = f"tabularia://{namespace()}"
    physical = _storage_namespace(ds.bucket)
    facets: dict[str, Any] = {
        "dataSource": datasource_dataset.DatasourceDatasetFacet(name=ds.name, uri=f"{physical}/{ds.key}"),
        "storage": storage_dataset.StorageDatasetFacet(storageLayer="object-storage", fileFormat="parquet"),
        "tabularia": _tabularia_facet()(datasourceId=ds.id, projectId=ds.project_id, kind=ds.kind, folder=_project_path(session, ds.project_id)),
    }
    if ds.key:
        facets["datasetVersion"] = dataset_version_dataset.DatasetVersionDatasetFacet(datasetVersion=ds.key)
    # the symlink is the datasource's FOLDER in the bucket, not the snapshot's file:
    # Marquez records every symlink as an alias of the dataset, and a new alias at every
    # run (a new parquet per snapshot) would grow the catalog at every run
    facets["symlinks"] = symlinks_dataset.SymlinksDatasetFacet(identifiers=[symlinks_dataset.Identifier(namespace=physical, name=f"/datasets/{ds.id}", type="LOCATION")])
    schema = _schema_facet(ds.columns)
    if schema:
        facets["schema"] = schema
    if ds.description:
        facets["documentation"] = documentation_dataset.DocumentationDatasetFacet(description=ds.description)
    name = datasource_name(session, ds)
    if output:
        out_facets = {}
        if rows is not None:
            out_facets["outputStatistics"] = output_statistics_output_dataset.OutputStatisticsOutputDatasetFacet(rowCount=rows)
        return OutputDataset(namespace=ns, name=name, facets=facets, outputFacets=out_facets)
    return InputDataset(namespace=ns, name=name, facets=facets)


def table_dataset(conn: Connection, table: str, output: bool = False, rows: int | None = None, mode: str | None = None):
    from openlineage.client.event_v2 import InputDataset, OutputDataset
    from openlineage.client.facet_v2 import datasource_dataset, lifecycle_state_change_dataset, output_statistics_output_dataset

    ns = f"{_NAMESPACE.get(conn.db_type) or _SCHEMES.get(conn.db_type, conn.db_type)}://{_host_port(conn)}"
    name = table_dataset_name(conn, table)
    facets: dict[str, Any] = {"dataSource": datasource_dataset.DatasourceDatasetFacet(name=conn.name, uri=ns)}
    if output:
        if mode:
            state = lifecycle_state_change_dataset.LifecycleStateChange.OVERWRITE if mode == "replace" else lifecycle_state_change_dataset.LifecycleStateChange.ALTER
            facets["lifecycleStateChange"] = lifecycle_state_change_dataset.LifecycleStateChangeDatasetFacet(lifecycleStateChange=state)
        out_facets = {}
        if rows is not None:
            out_facets["outputStatistics"] = output_statistics_output_dataset.OutputStatisticsOutputDatasetFacet(rowCount=rows)
        return OutputDataset(namespace=ns, name=name, facets=facets, outputFacets=out_facets)
    return InputDataset(namespace=ns, name=name, facets=facets)


def object_dataset(endpoint: str, bucket: str, key: str, fmt: str | None = None, output: bool = False, rows: int | None = None,
                   written: str | None = None):
    """An object on S3 or GCS (mirror copy, file destination, uploaded file), or
    the files of an «S3 folder» datasource (`key` = folder/glob). A key with
    placeholders stays ONE dataset named after its template: the key this run
    wrote is the dataset's version (`written`)."""
    from openlineage.client.event_v2 import InputDataset, OutputDataset
    from openlineage.client.facet_v2 import dataset_version_dataset, output_statistics_output_dataset, storage_dataset

    ns = f"{'gs' if 'googleapis' in (endpoint or '').lower() else 's3'}://{bucket}"
    name = "/" + key.strip("/")
    facets: dict[str, Any] = {}
    if fmt:
        facets["storage"] = storage_dataset.StorageDatasetFacet(storageLayer="object-storage", fileFormat=fmt)
    if written:
        facets["datasetVersion"] = dataset_version_dataset.DatasetVersionDatasetFacet(datasetVersion=written)
    if output:
        out_facets = {}
        if rows is not None:
            out_facets["outputStatistics"] = output_statistics_output_dataset.OutputStatisticsOutputDatasetFacet(rowCount=rows)
        return OutputDataset(namespace=ns, name=name, facets=facets, outputFacets=out_facets)
    return InputDataset(namespace=ns, name=name, facets=facets)


def sftp_dataset(address: str, path: str, fmt: str | None = None, output: bool = False, rows: int | None = None,
                 written: str | None = None):
    """Files on an SFTP server: namespace sftp://host:port. An Output is ONE dataset
    named after its folder and file-name template, whatever file each run writes:
    the file this run wrote is the dataset's version (`written`)."""
    from openlineage.client.event_v2 import InputDataset, OutputDataset
    from openlineage.client.facet_v2 import dataset_version_dataset, output_statistics_output_dataset, storage_dataset

    ns = f"sftp://{address}"
    name = "/" + (path or "").strip("/")
    facets: dict[str, Any] = {}
    if fmt:
        facets["storage"] = storage_dataset.StorageDatasetFacet(storageLayer="file", fileFormat=fmt)
    if written:
        facets["datasetVersion"] = dataset_version_dataset.DatasetVersionDatasetFacet(datasetVersion=written)
    if output:
        out_facets = {}
        if rows is not None:
            out_facets["outputStatistics"] = output_statistics_output_dataset.OutputStatisticsOutputDatasetFacet(rowCount=rows)
        return OutputDataset(namespace=ns, name=name, facets=facets, outputFacets=out_facets)
    return InputDataset(namespace=ns, name=name, facets=facets)


def _sftp_address(conn: Connection) -> str:
    return f"{conn.host}:{conn.port or 22}"


def _sftp_path(folder: str, name: str) -> str:
    return "/".join(p for p in ((folder or "").strip("/"), (name or "").strip("/")) if p)


def api_dataset(conn: Connection, ref: dict):
    """An HTTP API: namespace = the origin of the base URL (https://host[:port]), name =
    the base path + the call's path AS WRITTEN (placeholders included, so the dataset
    is the same at every refresh). Never the query string: it can hold a key."""
    from urllib.parse import urlsplit

    from openlineage.client.event_v2 import InputDataset

    base = urlsplit((conn.host or "").strip())
    host = (base.hostname or "").lower()
    default = 443 if base.scheme == "https" else 80
    port = base.port if base.port and base.port != default else None
    namespace = f"{base.scheme or 'https'}://{host}" + (f":{port}" if port else "")
    path = "/".join(p.strip("/") for p in (base.path, str(ref.get("path") or "").split("?")[0]) if p.strip("/"))
    return InputDataset(namespace=namespace, name="/" + path, facets={})


def sharepoint_dataset(conn: Connection, ref: dict):
    from openlineage.client.event_v2 import InputDataset
    from openlineage.client.facet_v2 import datasource_dataset

    site = (conn.host or "").split("://")[-1].rstrip("/")
    path = "/" + str(ref.get("path") or "").strip("/")
    if ref.get("sheet"):
        path += f"#{ref['sheet']}"
    return InputDataset(namespace=f"sharepoint://{site}", name=path,
                        facets={"dataSource": datasource_dataset.DatasourceDatasetFacet(name=conn.name, uri=f"https://{site}")})


def origin_inputs(session: Session, ds: Datasource) -> tuple[list, dict]:
    """Where a DATABASE datasource takes its data from: the inputs of its refresh and the
    job's facets (the query, when it is a query)."""
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is None:
        return [], {}
    if conn.db_type == "sharepoint":
        try:
            ref = json.loads(ds.source_ref or "{}")
        except json.JSONDecodeError:
            ref = {}
        return [sharepoint_dataset(conn, ref)], {}
    if conn.db_type == "api":
        try:
            ref = json.loads(ds.source_ref or "{}")
        except json.JSONDecodeError:
            ref = {}
        return [api_dataset(conn, ref if isinstance(ref, dict) else {})], {}
    if conn.db_type == "sftp":
        try:
            ref = json.loads(ds.source_ref or "{}")
        except json.JSONDecodeError:
            ref = {}
        return [sftp_dataset(_sftp_address(conn), _sftp_path(ref.get("folder", ""), ref.get("pattern", "")))], {}
    if conn.db_type == "s3":
        # the «S3 folder» datasource: s3://bucket/folder/glob (the templates as written)
        try:
            ref = json.loads(ds.source_ref or "{}")
        except json.JSONDecodeError:
            ref = {}
        bucket = (str(ref.get("bucket") or "").strip() or (conn.database or "").strip())
        return [object_dataset(conn.host or "", bucket, _sftp_path(ref.get("folder", ""), ref.get("pattern", "")))], {}
    if ds.source_type == "sql":
        from openlineage.client.facet_v2 import sql_job

        sql = ds.source_ref or ""
        inputs = [table_dataset(conn, t) for t in _tables_in_sql(conn, sql)]
        return inputs, {"sql": sql_job.SQLJobFacet(query=sql, dialect=_SCHEMES.get(conn.db_type))}
    return [table_dataset(conn, ds.source_ref or "")], {}


# ── flows: inputs and outputs from the definition ────────────────────────────
def _definition(flow: Flow) -> dict:
    try:
        d = json.loads(flow.definition or "{}")
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def flow_inputs(session: Session, flow: Flow) -> list:
    """What the flow reads: the datasources of the source nodes, or the uploaded file when
    the source does not come from the catalog. Without duplicates."""
    return _source_datasets(session, [n for n in _definition(flow).get("nodes") or [] if n.get("type") == "source"])


def _source_datasets(session: Session, nodes: list[dict]) -> list:
    """The datasets of these source nodes, without duplicates."""
    inputs = []
    for n in nodes:
        d = n.get("data") or {}
        ds = session.get(Datasource, d["datasourceId"]) if d.get("datasourceId") is not None else None
        if ds is not None:
            inputs.append(datasource_dataset(session, ds))
        elif d.get("parquetKey"):
            inputs.append(object_dataset("", d.get("bucket") or get_settings().engine.bucket, d["parquetKey"], "parquet"))
    return _unique(inputs)


def _unique(datasets: Iterable) -> list:
    seen_ones: set[tuple[str, str]] = set()
    out = []
    for dataset in datasets:
        key = (dataset.namespace, dataset.name)
        if key not in seen_ones:
            seen_ones.add(key)
            out.append(dataset)
    return out


def upstream_inputs(session: Session, flow: Flow, node_id: str | None) -> list:
    """What reaches a node of the flow (a Script or Container node's input): the source
    nodes upstream of it along the data edges."""
    from app.services.flow_resolver import _is_seq_edge

    definition = _definition(flow)
    nodes = {n.get("id"): n for n in definition.get("nodes") or [] if isinstance(n, dict)}
    into: dict[str, list[str]] = {}
    for e in definition.get("edges") or []:
        if isinstance(e, dict) and not _is_seq_edge(e):
            into.setdefault(e.get("target"), []).append(e.get("source"))
    sources, seen, todo = [], {node_id}, list(into.get(node_id, []))
    while todo:
        nid = todo.pop()
        if nid in seen or nid not in nodes:
            continue
        seen.add(nid)
        if nodes[nid].get("type") == "source":
            sources.append(nodes[nid])
        todo.extend(into.get(nid, []))
    order = {n.get("id"): i for i, n in enumerate(definition.get("nodes") or []) if isinstance(n, dict)}
    return _source_datasets(session, sorted(sources, key=lambda n: order.get(n.get("id"), 0)))


def _find_datasource(session: Session, project_id: int | None, name: str) -> Datasource | None:
    if project_id is None or not name:
        return None
    return session.exec(select(Datasource).where(Datasource.project_id == project_id, Datasource.name == name)).first()


def flow_outputs(session: Session, flow: Flow) -> list:
    """What the flow writes, read from the definition's Output nodes (without running it).
    A datasource the flow will create at the first run does not exist yet: its dataset
    has the name it will have already (folder + name), without the facets."""
    from openlineage.client.event_v2 import OutputDataset

    outputs = []
    for n in _definition(flow).get("nodes") or []:
        if n.get("type") != "output":
            continue
        d = n.get("data") or {}
        kind = d.get("destType") or "datasource"
        if kind == "datasource":
            ds = _find_datasource(session, d.get("projectId"), (d.get("name") or "").strip())
            if ds is not None:
                outputs.append(datasource_dataset(session, ds, output=True))
            elif d.get("name"):
                path = _project_path(session, d.get("projectId"))
                outputs.append(OutputDataset(namespace=f"tabularia://{namespace()}", name=f"/{path}/{d['name'].strip()}".replace("//", "/")))
            if d.get("mirrorEnabled") and d.get("mirrorConnectionId"):
                mconn = session.get(Connection, d["mirrorConnectionId"])
                if mconn is not None:
                    outputs.append(object_dataset(mconn.host, (d.get("mirrorBucket") or mconn.database or "").strip(), d.get("mirrorKey") or "", "parquet", output=True))
        elif kind == "database":
            conn = session.get(Connection, d.get("connectionId")) if d.get("connectionId") else None
            if conn is not None and (d.get("table") or "").strip():
                outputs.append(table_dataset(conn, d["table"].strip(), output=True, mode=d.get("mode") or "append"))
        elif kind == "s3":
            conn = session.get(Connection, d.get("connectionId")) if d.get("connectionId") else None
            if conn is not None and (d.get("s3Key") or "").strip():
                outputs.append(object_dataset(conn.host, (d.get("s3Bucket") or conn.database or "").strip(), d["s3Key"].strip(), d.get("s3Format") or "parquet", output=True))
        elif kind == "sftp":
            conn = session.get(Connection, d.get("connectionId")) if d.get("connectionId") else None
            if conn is not None and (d.get("sftpFileName") or "").strip():
                outputs.append(sftp_dataset(_sftp_address(conn), _sftp_path(d.get("sftpFolder") or "", d["sftpFileName"].strip()),
                                            d.get("sftpFormat") or "csv", output=True))
        elif kind == "email":
            # as a Pushmail cell's: the SMTP server, and the flow's path with the node's
            # name; never the recipients
            conn = session.get(Connection, d.get("connectionId")) if d.get("connectionId") else None
            if conn is not None:
                outputs.append(email_dataset(conn.host, conn.port or 587, flow_email_name(session, flow), conn.name))
    return outputs


def _job(session: Session, flow: Flow, extra_facets: dict | None = None):
    from openlineage.client.event_v2 import Job
    from openlineage.client.facet_v2 import documentation_job, job_type_job

    facets: dict[str, Any] = {"jobType": job_type_job.JobTypeJobFacet(processingType="BATCH", integration="TABULARIA", jobType="FLOW")}
    if flow.description:
        facets["documentation"] = documentation_job.DocumentationJobFacet(description=flow.description)
    facets.update(extra_facets or {})
    return Job(namespace=namespace(), name=job_name_for_flow(session, flow), facets=facets)


def job_events_for_flow(session: Session, flow: Flow) -> list:
    """The flow's STATIC lineage: a `JobEvent` with what it reads and writes, rebuilt from
    the definition. To fill a catalog with the existing flows, or download the
    dependencies as a file."""
    from openlineage.client.event_v2 import JobEvent

    return [JobEvent(eventTime=_now(), producer=PRODUCER, job=_job(session, flow),
                     inputs=flow_inputs(session, flow), outputs=flow_outputs(session, flow))]


# ── the other objects: SQL scripts, saved scripts, notebooks, flow groups ────
def _json(text: str | None) -> dict:
    try:
        d = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _datasource_inputs(session: Session, ids: Iterable) -> list:
    """The datasources with these ids, as inputs; a removed one is left out."""
    out = []
    for i in ids:
        ds = session.get(Datasource, i) if isinstance(i, int) and not isinstance(i, bool) else None
        if ds is not None:
            out.append(datasource_dataset(session, ds))
    return _unique(out)


def _intended_output(session: Session, project_id: int, name: str):
    """The datasource an object writes under `name` in its folder: as it is now, or with
    the name it will have when it does not exist yet (as `flow_outputs` does)."""
    from openlineage.client.event_v2 import OutputDataset

    ds = _find_datasource(session, project_id, (name or "").strip())
    if ds is not None:
        return datasource_dataset(session, ds, output=True)
    path = _project_path(session, project_id)
    return OutputDataset(namespace=f"tabularia://{namespace()}", name=f"/{path}/{name.strip()}".replace("//", "/"))


def _job_type(job_type: str) -> dict:
    from openlineage.client.facet_v2 import job_type_job

    return {"jobType": job_type_job.JobTypeJobFacet(processingType="BATCH", integration="TABULARIA", jobType=job_type)}


def _object_job(session: Session, kind: str, obj: Any, job_type: str, extra_facets: dict | None = None):
    from openlineage.client.event_v2 import Job
    from openlineage.client.facet_v2 import documentation_job

    facets: dict[str, Any] = _job_type(job_type)
    if getattr(obj, "description", ""):
        facets["documentation"] = documentation_job.DocumentationJobFacet(description=obj.description)
    facets.update(extra_facets or {})
    return Job(namespace=namespace(), name=job_name_for(session, kind, obj), facets=facets)


def sql_script_lineage(session: Session, script: SqlScript, run: Run | None = None) -> tuple[list, list, dict]:
    """(inputs, outputs, job facets) of a SQL script. For a write, from what the run
    recorded when it started (the version, the datasources the SQL named or the
    connection it ran on); without a run, from the current version. On a connection the
    inputs are the tables the SQL reads, found with sqlglot as for a SQL datasource's
    refresh — a query sqlglot does not understand gives none, its text stays in the
    `sql` facet."""
    from openlineage.client.facet_v2 import sql_job

    from app.services import sql_scripts

    info = _json(run.script) if run is not None else {}
    content = sql_scripts.content_of(session, script, info.get("version"))
    sql = content.get("sql") or ""
    connection_id = info["connection_id"] if "connection_id" in info else content.get("connection_id")
    if connection_id is not None:
        conn = session.get(Connection, connection_id)
        inputs = [table_dataset(conn, t) for t in _tables_in_sql(conn, sql)] if conn is not None else []
        dialect = _SCHEMES.get(conn.db_type) if conn is not None else None
    else:
        if "datasources" in info:
            inputs = _datasource_inputs(session, info["datasources"])
        else:
            inputs = _unique(datasource_dataset(session, d)
                             for d in sql_scripts.named_datasources(session, sql, content.get("engine")))
        engine = (run.engine if run is not None and run.engine else None) or content.get("engine") or "polars"
        dialect = sql_scripts.DIALECTS.get(engine, "postgres")
    facets = {"sql": sql_job.SQLJobFacet(query=sql, dialect=dialect)} if sql.strip() else {}
    outputs = []
    if run is not None and run.datasource_id is not None:
        ds = session.get(Datasource, run.datasource_id)
        if ds is not None:
            outputs.append(datasource_dataset(session, ds, output=True, rows=run.rows_written))
    elif run is not None:
        if run.publish_name and run.publish_project_id is not None:
            outputs.append(_intended_output(session, run.publish_project_id, run.publish_name))
    else:
        out = content.get("output")
        if isinstance(out, dict) and out.get("name") and isinstance(out.get("project_id"), int):
            outputs.append(_intended_output(session, out["project_id"], out["name"]))
    return inputs, outputs, facets


def saved_script_lineage(session: Session, script: SavedScript, child: Run | None = None) -> tuple[list, list]:
    """(inputs, outputs) of a saved script: its declared inputs (those the run copied,
    when there is a run) and the datasource it publishes."""
    from app.services import saved_scripts

    info = _json(child.script) if child is not None else {}
    content: dict | None = None
    if isinstance(info.get("inputs"), list):
        declared = info["inputs"]
    else:
        content = saved_scripts.content_of(session, script)
        declared = content.get("inputs") or []
    inputs = _datasource_inputs(session, [i.get("datasource_id") for i in declared if isinstance(i, dict)])
    outputs = []
    if child is not None and child.datasource_id is not None:
        ds = session.get(Datasource, child.datasource_id)
        if ds is not None:
            outputs.append(datasource_dataset(session, ds, output=True, rows=child.rows_written))
    elif child is None or child.status != "SUCCESS":
        # not run, or failed: what it publishes when it succeeds (a success that wrote
        # nothing published nothing)
        publish = (content if content is not None else saved_scripts.content_of(session, script)).get("output") or {}
        if publish.get("publish") and publish.get("name") and isinstance(publish.get("project_id"), int):
            outputs.append(_intended_output(session, publish["project_id"], str(publish["name"])))
    return inputs, outputs


def notebook_inputs(session: Session, notebook_id: int, session_key: str | None = None) -> list:
    """The datasources a notebook has read: in one session (its Job's name), or ever."""
    from app.services import notebook_reads

    ids = (notebook_reads.read_in_session(session, notebook_id, session_key) if session_key
           else notebook_reads.read_by(session, [notebook_id]).get(notebook_id, []))
    return _datasource_inputs(session, ids)


def job_events_for(session: Session, kind: str, obj: Any) -> list:
    """The STATIC lineage of a flow, a SQL script, a saved script, a notebook or a flow
    group: a `JobEvent` with what it reads and writes, without running it."""
    from openlineage.client.event_v2 import JobEvent

    if kind == "flow":
        return job_events_for_flow(session, obj)
    if kind == "sql_script":
        inputs, outputs, facets = sql_script_lineage(session, obj)
        job = _object_job(session, kind, obj, "SQL_SCRIPT", facets)
    elif kind == "saved_script":
        inputs, outputs = saved_script_lineage(session, obj)
        job = _object_job(session, kind, obj, "SCRIPT")
    elif kind == "notebook":
        inputs, outputs = notebook_inputs(session, obj.id), []
        job = _object_job(session, kind, obj, "NOTEBOOK")
    else:  # a flow group: what it reads and writes is told by its runs
        inputs, outputs = [], []
        job = _object_job(session, kind, obj, "FLOW_GROUP")
    return [JobEvent(eventTime=_now(), producer=PRODUCER, job=job, inputs=inputs, outputs=outputs)]


def _group_cell_jobs(group: FlowGroup) -> list[str]:
    """The child jobs of a flow group's cells that exist only inside it (Container and
    Pushmail cells): `<group's job>.<these>`."""
    from app.services import container_nodes
    from app.services.flow_groups import definition_of

    labels = []
    for row in definition_of(group).steps:
        for cell in row:
            if cell is not None and cell.kind == "container":
                labels.append("container " + container_nodes.label({"id": f"container-cell-{cell.id}", "data": cell.container or {}}))
            elif cell is not None and cell.kind == "pushmail" and cell.pushmail is not None:
                labels.append("pushmail " + (cell.pushmail.label.strip() or "Pushmail"))
    return list(dict.fromkeys(labels))


def _step_labels(flow: Flow) -> list[str]:
    """The jobs of a flow's Script and Container nodes: `<flow's job>.<these>`."""
    from app.services import container_nodes, script_nodes

    definition = _definition(flow)
    return list(dict.fromkeys([f"script {script_nodes.label(n)}" for n in script_nodes.script_nodes(definition)]
                              + [f"container {container_nodes.label(n)}" for n in container_nodes.container_nodes(definition)]))


@functools.cache
def _container_facet_class():
    import attr
    from openlineage.client.facet_v2 import JobFacet

    @attr.define
    class TabulariaContainerJobFacet(JobFacet):
        registry: str = attr.field()
        repository: str = attr.field()
        registryHost: str | None = attr.field(default=None)  # noqa: N815
        tag: str | None = attr.field(default=None)
        digest: str | None = attr.field(default=None)
        image: str | None = attr.field(default=None)

        @staticmethod
        def _get_schema() -> str:
            return f"{PRODUCER}/blob/main/docs/openlineage/TabulariaContainerJobFacet.json"

    return TabulariaContainerJobFacet


def _container_facet(session: Session, info: dict):
    """The image a Container node or cell ran: the registry, the repository, the tag it
    asked for and the digest it was resolved to when the run started."""
    from app.models import ContainerRegistry

    registry = session.get(ContainerRegistry, info["registry_id"]) if isinstance(info.get("registry_id"), int) else None
    return _container_facet_class()(registry=str(info.get("registry") or ""), repository=str(info.get("repository") or ""),
                                    registryHost=registry.host if registry is not None else None,
                                    tag=info.get("reference"), digest=info.get("digest"), image=info.get("image"))


def email_dataset(host: str, port: int, name: str, connection_name: str | None = None):
    """The email a Pushmail cell sends, as a dataset: namespace = the SMTP server
    (`smtp://host:port`), name = `/<folder path>/<flow group>/<cell>`. Never the
    recipients: their addresses do not leave the installation."""
    from openlineage.client.event_v2 import OutputDataset
    from openlineage.client.facet_v2 import datasource_dataset

    ns = f"smtp://{(host or '').split('://')[-1].rstrip('/')}:{port}"
    return OutputDataset(namespace=ns, name=name, outputFacets={},
                         facets={"dataSource": datasource_dataset.DatasourceDatasetFacet(name=connection_name or ns, uri=ns)})


def flow_email_name(session: Session, flow: Flow) -> str:
    """The email an Output node of the flow sends, as a dataset name: the flow's path and
    `email` (a run does not know which Output node it was, so a flow's emails through
    one SMTP server are one dataset)."""
    return "/" + job_name_for_flow(session, flow).strip("/") + "/email"


def _pushmail_dataset_name(group_path: str, group_name: str, cell: str) -> str:
    return "/" + "/".join(p for p in (group_path, group_name, cell) if p)


def _group_of(session: Session, run: Run) -> FlowGroup | None:
    """The flow group whose run `run` is a cell of."""
    padre = session.get(Run, run.parent_run_id) if run.parent_run_id is not None else None
    return session.get(FlowGroup, padre.flow_group_id) if padre is not None and padre.flow_group_id else None


# ── runs: the events of a closed run ─────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso(t: datetime | None) -> str:
    if t is None:
        return _now()
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _run_outputs(session: Session, run: Run) -> list:
    """The outputs of ONE Output node run: from the run itself, which knows what it wrote."""
    outputs = []
    if run.datasource_id is not None and run.publish_name:
        ds = session.get(Datasource, run.datasource_id)
        if ds is not None:
            outputs.append(datasource_dataset(session, ds, output=True, rows=run.rows_written))
    if run.destination:
        try:
            dest = json.loads(run.destination)
        except json.JSONDecodeError:
            dest = {}
        conn = session.get(Connection, dest.get("connection_id")) if dest.get("connection_id") else None
        if dest.get("type") == "database" and conn is not None and dest.get("table"):
            outputs.append(table_dataset(conn, dest["table"], output=True, rows=run.rows_written, mode=dest.get("mode")))
        elif dest.get("type") == "s3" and dest.get("bucket") and dest.get("key"):
            outputs.append(object_dataset(dest.get("endpoint") or "", dest["bucket"], dest["key"], dest.get("format"), output=True,
                                          rows=run.rows_written, written=dest.get("written_key")))
        elif dest.get("type") == "sftp" and dest.get("host") and (dest.get("path") or dest.get("file_name")):
            outputs.append(sftp_dataset(dest["host"], _sftp_path(dest.get("folder") or "", dest.get("file_name") or ""),
                                        dest.get("format"), output=True, rows=run.rows_written, written=dest.get("path")))
    if run.email and run.kind == "flow" and run.flow_id is not None:
        e = _json(run.email)
        conn = session.get(Connection, e["connection_id"]) if isinstance(e.get("connection_id"), int) else None
        flow = session.get(Flow, run.flow_id)
        if flow is not None and (conn is not None or e.get("host")):
            outputs.append(email_dataset(conn.host if conn is not None else e["host"],
                                         (conn.port if conn is not None else None) or 587,
                                         flow_email_name(session, flow), conn.name if conn is not None else None))
    if run.mirror:
        try:
            m = json.loads(run.mirror)
        except json.JSONDecodeError:
            m = {}
        if m.get("bucket") and m.get("key") and m.get("ok") is not False:
            outputs.append(object_dataset(m.get("endpoint") or "", m["bucket"], m["key"], "parquet", output=True, rows=run.rows_written))
    return outputs


def _output_label(run: Run) -> str:
    if run.publish_name:
        return f"datasource {run.publish_name}"
    if run.destination:
        try:
            d = json.loads(run.destination)
            if d.get("type") == "sftp":
                # the template, not the rendered name: the job stays the same from run to run
                return f"file /{_sftp_path(d.get('folder') or '', d.get('file_name') or '')}"
            return f"table {d.get('table')}" if d.get("type") == "database" else f"file {d.get('key')}"
        except json.JSONDecodeError:
            pass
    if run.email:
        return "email"
    return f"output {run.id}"


def _without_statistics(outputs: list) -> list:
    from openlineage.client.event_v2 import OutputDataset

    return [OutputDataset(namespace=o.namespace, name=o.name, facets=o.facets, outputFacets={}) for o in outputs]


def _parent_facet(session: Session, run: Run):
    """The `ParentRunFacet` ONLY for the steps that exist only inside their parent: a
    flow's own Output, Script and Container nodes (under the flow's job, «Flow.output»),
    and a flow group's Container and Pushmail cells (under the group's job).

    Marquez renames every child «<parent>.<child>»: a datasource refresh, a sub-flow's
    output (`Run flow`), and every object a flow group runs that has a life of its own
    (a flow, a refresh, a SQL script, a saved script) would end up in a job other than
    the one they have when they run alone. For them the link to the orchestration or
    the group that contained them is in the run's `tabularia` facet (`parentRunId`), not
    in the jobs' hierarchy."""
    from openlineage.client.facet_v2 import parent_run

    if run.parent_run_id is None:
        return None
    if run.kind in ("flow", "script", "container") and run.flow_id is not None:
        padre = session.get(Run, run.parent_run_id)
        if padre is None or padre.flow_id != run.flow_id:
            return None
        flow = session.get(Flow, padre.flow_id)
        if flow is None:
            return None
        parent_job = job_name_for_flow(session, flow)
    elif run.kind in ("container", "pushmail") and run.flow_id is None:
        group = _group_of(session, run)
        if group is None:
            return None
        parent_job = job_name_for(session, "flow_group", group)
    else:
        return None
    return parent_run.ParentRunFacet(
        run=parent_run.Run(runId=run_uuid(run.parent_run_id)),
        job=parent_run.Job(namespace=namespace(), name=parent_job),
    )


@functools.cache
def _tabularia_run_facet_class():
    import attr
    from openlineage.client.facet_v2 import RunFacet

    @attr.define
    class TabulariaRunFacet(RunFacet):
        kind: str = attr.field()
        trigger: str = attr.field()
        runId: int | None = attr.field(default=None)  # noqa: N815 — none for a notebook's session
        engine: str | None = attr.field(default=None)
        parentRunId: str | None = attr.field(default=None)  # noqa: N815
        version: int | None = attr.field(default=None)  # the SQL script's, saved script's or notebook's
        mode: str | None = attr.field(default=None)  # a notebook's session: app | edit

        @staticmethod
        def _get_schema() -> str:
            return f"{PRODUCER}/blob/main/docs/openlineage/TabulariaRunFacet.json"

    return TabulariaRunFacet


def _tabularia_run_facet(run: Run, engine: str | None = None, version: int | None = None):
    """The run's `tabularia` facet: the run's id in Tabularia, who launched it
    (manual/scheduled), the engine and the orchestration or flow group that contains
    it — even when the job is NOT a child in the catalog's hierarchy."""
    return _tabularia_run_facet_class()(runId=run.id, kind=run.kind, trigger=run.trigger_type or "manual",
                                        engine=engine or run.engine or None, version=version,
                                        parentRunId=run_uuid(run.parent_run_id) if run.parent_run_id is not None else None)


def _describe(session: Session, run: Run, top: bool = True):
    """(job, inputs, outputs, run facets) of a closed run, or None when the run is not
    told (a step's preparation, an editor's trial, an object gone). A flow group's run
    carries what its cells read and wrote, as a flow's orchestration carries its
    Outputs'."""
    from openlineage.client.event_v2 import Job

    run_facets: dict[str, Any] = {"tabularia": _tabularia_run_facet(run)}
    if run.kind == "orchestration" and run.flow_group_id is not None:
        group = session.get(FlowGroup, run.flow_group_id)
        if group is None:
            return None
        inputs, outputs = [], []
        if top:
            for child in session.exec(select(Run).where(Run.parent_run_id == run.id).order_by(Run.id)).all():
                described = _describe(session, child, top=False)
                if described is not None:
                    inputs += described[1]
                    outputs += described[2]
        return _object_job(session, "flow_group", group, "FLOW_GROUP"), _unique(inputs), _unique(outputs), run_facets
    if run.kind == "orchestration" and run.saved_script_id is not None:
        script = session.get(SavedScript, run.saved_script_id)
        if script is None:
            return None
        # the script's run proper is the kind="script" row under this one: its inputs,
        # its output, the environment it ran in
        child = session.exec(select(Run).where(Run.parent_run_id == run.id, Run.kind == "script")
                             .order_by(Run.id.desc())).first()
        inputs, outputs = saved_script_lineage(session, script, child)
        run_facets["tabularia"] = _tabularia_run_facet(run, engine=child.engine if child is not None else None,
                                                       version=_json(child.script).get("script_version") if child is not None else None)
        return _object_job(session, "saved_script", script, "SCRIPT"), inputs, outputs, run_facets
    if run.kind == "orchestration":
        flow = session.get(Flow, run.flow_id) if run.flow_id else None
        if flow is None:
            return None
        children = session.exec(select(Run).where(Run.parent_run_id == run.id, Run.kind == "flow")).all()
        return _job(session, flow), flow_inputs(session, flow), [o for f in children for o in _run_outputs(session, f)], run_facets
    if run.kind == "flow":
        flow = session.get(Flow, run.flow_id) if run.flow_id else None
        if flow is None or _json(run.destination).get("type") in ("script_input", "container_input"):
            return None  # the input given to a Script or Container node: a step inside the flow, not an Output
        # «<flow>.<output>» ALWAYS, with or without an orchestration: Marquez prefixes a child
        # with the parent's name by itself, and a run launched from the editor (without a
        # parent) would otherwise end up in a job other than the same Output orchestrated
        job = Job(namespace=namespace(), name=f"{job_name_for_flow(session, flow)}.{_output_label(run)}", facets=_job_type("OUTPUT"))
        return job, flow_inputs(session, flow), _run_outputs(session, run), run_facets
    if run.kind == "ingest":
        ds = session.get(Datasource, run.datasource_id) if run.datasource_id else None
        if ds is None:
            return None
        inputs, job_facets = origin_inputs(session, ds)
        job_facets.update(_job_type("REFRESH"))
        job = Job(namespace=namespace(), name=job_name_for_refresh(session, ds), facets=job_facets)
        return job, inputs, [datasource_dataset(session, ds, output=True, rows=run.rows_written if run.status == "SUCCESS" else None)], run_facets
    if run.kind == "sql":
        script = session.get(SqlScript, run.sql_script_id) if run.sql_script_id else None
        if script is None:
            return None
        inputs, outputs, facets = sql_script_lineage(session, script, run)
        run_facets["tabularia"] = _tabularia_run_facet(run, version=_json(run.script).get("version"))
        return _object_job(session, "sql_script", script, "SQL_SCRIPT", facets), inputs, outputs, run_facets
    if run.kind in ("script", "container"):
        info = _json(run.script)
        if info.get("trial"):
            return None  # the editor's trial: not a run of the flow
        facets = _job_type("SCRIPT" if run.kind == "script" else "CONTAINER")
        if run.kind == "container":
            facets["tabularia_container"] = _container_facet(session, info)
        if run.flow_id is not None:
            # a flow's Script or Container node: what reaches its input from the flow's
            # sources, and the datasources a saved script it runs declares; what it hands
            # to the nodes after it is not a dataset (they write the Outputs)
            flow = session.get(Flow, run.flow_id)
            if flow is None:
                return None
            parent_job = job_name_for_flow(session, flow)
            inputs = upstream_inputs(session, flow, info.get("node_id")) if info.get("input") else []
            inputs = _unique(inputs + _datasource_inputs(session, [i.get("datasource_id") for i in info.get("inputs") or []
                                                                   if isinstance(i, dict)]))
        elif run.kind == "container":
            group = _group_of(session, run)  # a flow group's Container cell: no data in, none out
            if group is None:
                return None
            parent_job, inputs = job_name_for(session, "flow_group", group), []
        else:
            return None  # a saved script's own run: told by its orchestration
        label = str(info.get("label") or info.get("node_id") or run.kind)
        return Job(namespace=namespace(), name=f"{parent_job}.{run.kind} {label}", facets=facets), inputs, [], run_facets
    if run.kind == "pushmail":
        group = _group_of(session, run)
        if group is None:
            return None
        email = _json(run.email)
        cell = str(email.get("cell") or "Pushmail")
        conn = session.get(Connection, email["connection_id"]) if isinstance(email.get("connection_id"), int) else None
        host = conn.host if conn is not None else email.get("host")
        outputs = [email_dataset(host, (conn.port if conn is not None else None) or 587,
                                 _pushmail_dataset_name(_project_path(session, group.project_id), group.name, cell),
                                 conn.name if conn is not None else None)] if host else []
        job = Job(namespace=namespace(), name=f"{job_name_for(session, 'flow_group', group)}.pushmail {cell}", facets=_job_type("PUSHMAIL"))
        return job, _datasource_inputs(session, email.get("datasource_ids") or []), outputs, run_facets
    return None


def run_events(session: Session, run: Run) -> list:
    """The run's START and COMPLETE/FAIL, built when the run has CLOSED: the two events
    arrive together, with the true times, and Marquez works out the duration."""
    from openlineage.client.event_v2 import Run as OlRun, RunEvent, RunState
    from openlineage.client.facet_v2 import error_message_run

    described = _describe(session, run)
    if described is None:
        return []
    job, inputs, outputs, run_facets = described
    padre = _parent_facet(session, run)
    if padre is not None:
        run_facets["parent"] = padre
    ol_run = OlRun(runId=run_uuid(run.id), facets=run_facets)
    # the outputs are declared at START already (without statistics): with an empty START
    # and a full COMPLETE the catalog would see two versions of the job at every run
    start = RunEvent(eventTime=_iso(run.started_at), producer=PRODUCER, run=ol_run, job=job, eventType=RunState.START, inputs=inputs, outputs=_without_statistics(outputs))
    end_facets = dict(run_facets)
    if run.status != "SUCCESS" and run.error:
        end_facets["errorMessage"] = error_message_run.ErrorMessageRunFacet(message=run.error[:2000], programmingLanguage="python")
    end = RunEvent(
        eventTime=_iso(run.finished_at), producer=PRODUCER, run=OlRun(runId=run_uuid(run.id), facets=end_facets), job=job,
        eventType=RunState.COMPLETE if run.status == "SUCCESS" else RunState.FAIL, inputs=inputs, outputs=outputs,
    )
    return [start, end]


# ── a notebook's session: a run that lasts as long as its pod ────────────────
def notebook_run_uuid(session_key: str) -> str:
    """A session's run id, from its Job's name (unique per pod): the same from every
    replica and from the reaper that stops it."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tabularia://{namespace()}/notebook-sessions/{session_key}"))


NOTEBOOK_STATES = ("START", "RUNNING", "COMPLETE", "FAIL", "ABORT")


def notebook_session_events(session: Session, row: Any, state: str, error: str | None = None,
                            datasource_id: int | None = None) -> list:
    """One event of a notebook's session (`row`: a NotebookSession, or anything with its
    notebook_id, mode, version, job_name and started_at). The job is the notebook, the
    run is the session: START when its pod starts, RUNNING with the datasource it reads
    the first time it reads it (OpenLineage accumulates a run's datasets across its
    events), COMPLETE when it stops — with every datasource it read, for the collectors
    that look at the closing event only —, FAIL when the pod could not start, ABORT when
    it was stopped before it started."""
    from openlineage.client.event_v2 import Run as OlRun, RunEvent, RunState
    from openlineage.client.facet_v2 import error_message_run

    notebook = session.get(Notebook, row.notebook_id)
    if notebook is None or state not in NOTEBOOK_STATES:
        return []
    facets: dict[str, Any] = {"tabularia": _tabularia_run_facet_class()(kind="notebook", trigger="manual",
                                                                        version=row.version, mode=row.mode)}
    if state == "FAIL" and error:
        facets["errorMessage"] = error_message_run.ErrorMessageRunFacet(message=error[:2000], programmingLanguage="python")
    if state == "START":
        inputs, when = [], _iso(row.started_at)
    elif state == "RUNNING":
        ds = session.get(Datasource, datasource_id) if datasource_id is not None else None
        if ds is None:
            return []
        inputs, when = [datasource_dataset(session, ds)], _now()
    else:
        inputs, when = notebook_inputs(session, row.notebook_id, row.job_name), _now()
    return [RunEvent(eventTime=when, producer=PRODUCER, run=OlRun(runId=notebook_run_uuid(row.job_name), facets=facets),
                     job=_object_job(session, "notebook", notebook, "NOTEBOOK"), eventType=getattr(RunState, state),
                     inputs=inputs, outputs=[])]


def notebook_session(session: Session, row: Any, state: str, error: str | None = None,
                     datasource_id: int | None = None) -> None:
    """Tells the catalog a moment of a notebook's session (see `notebook_session_events`).
    It does not block, it does not raise."""
    if not enabled():
        return
    try:
        events = notebook_session_events(session, row, state, error, datasource_id)
        if events:
            _enqueue(f"notebook {row.notebook_id} ({state.lower()})", events)
    except Exception:  # noqa: BLE001
        logger.exception("openlineage: event of a session of notebook %s not built", getattr(row, "notebook_id", None))


def to_json(events: Iterable) -> list[dict]:
    from openlineage.client.serde import Serde

    return [json.loads(Serde.to_json(e)) for e in events]


# ── emitting ─────────────────────────────────────────────────────────────────
def _emit(events: list) -> int:
    client = _get_client()
    n = 0
    for e in events:
        client.emit(e)
        n += 1
    return n


def run_closed(session: Session, run: Run) -> None:
    """To call AFTER the commit that closes a run. The events are built here, with the
    caller's session (a few reads); the sending goes to the service thread. It does not
    block, it does not raise."""
    if not enabled():
        return
    try:
        events = run_events(session, run)
        if events:
            _enqueue(run.id, events)
    except Exception:  # noqa: BLE001
        logger.exception("openlineage: events of run %s not built", run.id)


def emit_flow(session: Session, flow: Flow) -> int:
    """Sends the flow's static lineage to the collector, at once. Raises when the collector
    refuses: whoever asks for it by hand wants to know."""
    return _emit(job_events_for_flow(session, flow))


def wait_idle(timeout: float = 10.0) -> None:
    """For the tests and the shutdown: waits for the queue to empty (at most `timeout`
    seconds), then dismisses the thread."""
    global _queue, _worker
    with _LOCK:
        queue_, worker = _queue, _worker
        _queue, _worker = None, None
    if queue_ is None:
        return
    end = time.monotonic() + timeout
    while queue_.unfinished_tasks and time.monotonic() < end:
        time.sleep(0.02)
    try:
        queue_.put(None, timeout=max(0.0, end - time.monotonic()))   # dismissal: the thread exits when it gets there
    except queue.Full:
        pass                                                        # stays daemon: dies with the process
    if worker is not None:
        worker.join(timeout=1.0)


# ── renames and moves: the new name in the catalog, the old one removed ──────
def catalog_names(session: Session, flows: Iterable[Flow] = (), datasources: Iterable[Datasource] = (),
                  objects: Iterable[tuple[str, Any]] = ()) -> dict:
    """The names flows, datasources and the other objects that run (`objects`: pairs
    (kind, object), kinds of OBJECT_KINDS) have in the catalog NOW. Taken before a
    rename or a move (the folder path is part of every name), and handed to
    `renamed` after the commit. Empty when OpenLineage is off."""
    if not enabled():
        return {}
    names: dict[tuple[str, int], str] = {}
    for flow in flows:
        names[("flow", flow.id)] = job_name_for_flow(session, flow)
    for ds in datasources:
        names[("datasource", ds.id)] = datasource_name(session, ds)
    for kind, obj in objects:
        names[(kind, obj.id)] = job_name_for(session, kind, obj)
    return names


def folder_names(session: Session, project_id: int) -> dict:
    """`catalog_names` of everything in a folder and its subfolders: renaming or
    moving the folder renames all of it."""
    if not enabled():
        return {}
    ids = perm_service.descendant_ids(perm_service.folder_parents(session), {project_id})
    flows = session.exec(select(Flow).where(Flow.project_id.in_(ids))).all()
    datasources = session.exec(select(Datasource).where(Datasource.project_id.in_(ids))).all()
    objects = [(kind, obj) for kind, (model, _) in OBJECT_KINDS.items()
               for obj in session.exec(select(model).where(model.project_id.in_(ids))).all()]
    return catalog_names(session, flows, datasources, objects)


def _current_name(session: Session, kind: str, obj: Any) -> str:
    return datasource_name(session, obj) if kind == "datasource" else job_name_for(session, kind, obj)


def renamed(session: Session, before: dict) -> None:
    """Call after the commit of a rename or a move, with `catalog_names` taken
    before it. For whatever changed name, the catalog gets the object under the
    new name and loses the old one. The events are built and sent by the service
    thread, with a session of its own: a folder can hold many flows. Never raises."""
    if not before or not enabled():
        return
    try:
        models = {"flow": Flow, "datasource": Datasource, **{k: m for k, (m, _) in OBJECT_KINDS.items()}}
        found: dict[str, dict] = {}
        for kind in {k for k, _ in before}:
            model = models[kind]
            found[kind] = {o.id: o for o in session.exec(select(model).where(model.id.in_([i for k, i in before if k == kind]))).all()}
        changed = {}
        for (kind, oid), old_name in before.items():
            obj = found.get(kind, {}).get(oid)
            if obj is not None and _current_name(session, kind, obj) != old_name:
                changed[(kind, oid)] = old_name
        if changed:
            _enqueue("rename", functools.partial(_rename_events, session.get_bind(), changed))
    except Exception:  # noqa: BLE001 — the rename is done: the lineage must not weigh on it
        logger.exception("openlineage: rename events not built")


def _rename_events(bind: Any, changed: dict) -> tuple[list, list[tuple[str, str, str]]]:
    """The events that tell the catalog the new names, and the old names to remove
    as (jobs | datasets, namespace, name). A flow comes back as its static job and
    loses its old job and its Output, Script and Container jobs (they return with its
    next run); a SQL script, a saved script, a notebook and a flow group come back as
    their static job and lose the old one (a group, its cells' jobs and its Pushmail
    emails too); a datasource comes back as a dataset that names the one it was, with
    its refresh job; the flows that read or write a renamed datasource, and the saved
    scripts and notebooks that read it, come back pointing at the new name."""
    from openlineage.client.event_v2 import DatasetEvent, StaticDataset
    from openlineage.client.facet_v2 import lifecycle_state_change_dataset as lifecycle

    events: list = []
    old: list[tuple[str, str, str]] = []
    ds_namespace = f"tabularia://{namespace()}"
    sent: set[tuple[str, int]] = set()
    renamed_ds: list[Datasource] = []
    with Session(bind) as s:
        for (kind, oid), old_name in changed.items():
            if kind == "flow":
                flow = s.get(Flow, oid)
                if flow is None:
                    continue
                events += job_events_for_flow(s, flow)
                sent.add(("flow", flow.id))
                old.append(("jobs", namespace(), old_name))
                old += [("jobs", namespace(), f"{old_name}.{label}") for label in _output_labels(s, flow.id) + _step_labels(flow)]
                continue
            if kind in OBJECT_KINDS:
                obj = s.get(OBJECT_KINDS[kind][0], oid)
                if obj is None:
                    continue
                events += job_events_for(s, kind, obj)
                sent.add((kind, obj.id))
                old.append(("jobs", namespace(), old_name))
                if kind == "flow_group":
                    old += [("jobs", namespace(), f"{old_name}.{label}") for label in _group_cell_jobs(obj)]
                    old += _old_pushmail_datasets(s, obj, old_name)
                continue
            ds = s.get(Datasource, oid)
            if ds is None:
                continue
            dataset = datasource_dataset(s, ds)
            facets = dict(dataset.facets or {})
            facets["lifecycleStateChange"] = lifecycle.LifecycleStateChangeDatasetFacet(
                lifecycleStateChange=lifecycle.LifecycleStateChange.RENAME,
                previousIdentifier=lifecycle.PreviousIdentifier(name=old_name, namespace=ds_namespace),
            )
            events.append(DatasetEvent(eventTime=_now(), producer=PRODUCER,
                                       dataset=StaticDataset(namespace=ds_namespace, name=dataset.name, facets=facets)))
            old.append(("datasets", ds_namespace, old_name))
            if ds.kind == "database":
                events.append(_refresh_job_event(s, ds))
                old.append(("jobs", namespace(), f"{old_name[1:]} (refresh)"))  # «/a/b» was the job «a/b (refresh)»
            renamed_ds.append(ds)
        for flow in _flows_touching(s, renamed_ds):
            if ("flow", flow.id) not in sent:
                events += job_events_for_flow(s, flow)
        for kind, obj in _readers_touching(s, renamed_ds):
            if (kind, obj.id) not in sent:
                events += job_events_for(s, kind, obj)
    return events, old


def _old_pushmail_datasets(session: Session, group: FlowGroup, old_name: str) -> list[tuple[str, str, str]]:
    """The emails of a renamed flow group's Pushmail cells, under the group's old name
    (`/<folder path>/<group>/<cell>`, from the old job «<folder path>/<group> (flow group)»)."""
    from app.services.flow_groups import definition_of

    suffix = f" ({OBJECT_KINDS['flow_group'][1]})"
    old_group = old_name[:-len(suffix)] if old_name.endswith(suffix) else old_name
    out = []
    for row in definition_of(group).steps:
        for cell in row:
            if cell is None or cell.kind != "pushmail" or cell.pushmail is None or cell.pushmail.connectionId is None:
                continue
            conn = session.get(Connection, cell.pushmail.connectionId)
            if conn is None:
                continue
            ns = email_dataset(conn.host, conn.port or 587, "").namespace
            out.append(("datasets", ns, _pushmail_dataset_name("", old_group, cell.pushmail.label.strip() or "Pushmail")))
    return out


def _readers_touching(session: Session, datasources: list[Datasource]) -> list[tuple[str, Any]]:
    """The saved scripts that declare one of these datasources as an input and the
    notebooks that have read one (by id)."""
    if not datasources:
        return []
    from app.services import notebook_reads, saved_scripts

    ids = {d.id for d in datasources}
    out: list[tuple[str, Any]] = []
    for script in session.exec(select(SavedScript)).all():
        if any(isinstance(i, dict) and i.get("datasource_id") in ids for i in saved_scripts.content_of(session, script).get("inputs") or []):
            out.append(("saved_script", script))
    for notebook_id in notebook_reads.readers_of(session, ids):
        notebook = session.get(Notebook, notebook_id)
        if notebook is not None:
            out.append(("notebook", notebook))
    return out


def _output_labels(session: Session, flow_id: int) -> list[str]:
    """The names of a flow's Output jobs in the catalog, one per destination its
    runs wrote to (see `_output_label`)."""
    rows = session.exec(
        select(Run.publish_name, Run.destination, Run.email).where(Run.flow_id == flow_id, Run.kind == "flow").distinct().limit(200)
    ).all()
    labels: list[str] = []
    for publish_name, destination, email in rows:
        if publish_name or destination or email:
            label = _output_label(SimpleNamespace(publish_name=publish_name, destination=destination, email=email, id=None))
            if label not in labels:
                labels.append(label)
    return labels


def _refresh_job_event(session: Session, ds: Datasource):
    """The refresh of a database datasource as a static job, as its runs describe it."""
    from openlineage.client.event_v2 import Job, JobEvent
    from openlineage.client.facet_v2 import job_type_job

    inputs, job_facets = origin_inputs(session, ds)
    job_facets["jobType"] = job_type_job.JobTypeJobFacet(processingType="BATCH", integration="TABULARIA", jobType="REFRESH")
    return JobEvent(eventTime=_now(), producer=PRODUCER, job=Job(namespace=namespace(), name=job_name_for_refresh(session, ds), facets=job_facets),
                    inputs=inputs, outputs=[datasource_dataset(session, ds, output=True)])


def _flows_touching(session: Session, datasources: list[Datasource]) -> list[Flow]:
    """The flows whose sources read one of these datasources (by id) or whose
    Output nodes write one of them (by folder and current name)."""
    if not datasources:
        return []
    ids = {d.id for d in datasources}
    written = {(d.project_id, d.name) for d in datasources}
    out = []
    for flow in session.exec(select(Flow)).all():
        for n in _definition(flow).get("nodes") or []:
            d = n.get("data") or {}
            if n.get("type") == "source" and d.get("datasourceId") in ids:
                out.append(flow)
                break
            if (n.get("type") == "output" and (d.get("destType") or "datasource") == "datasource"
                    and (d.get("projectId"), (d.get("name") or "").strip()) in written):
                out.append(flow)
                break
    return out


_http_transport: Any = None  # for the tests: an httpx transport standing in for Marquez


def _marquez_api() -> str | None:
    """Marquez's REST API, when the collector is Marquez (its lineage endpoint under
    OPENLINEAGE__URL): the place where old names can be deleted. Other collectors
    get the standard RENAME facet and drop the old name their own way."""
    s = get_settings().openlineage
    if not s.url or s.endpoint.strip("/") != "api/v1/lineage":
        return None
    return f"{s.url.rstrip('/')}/api/v1"


def _remove_old_names(old: list[tuple[str, str, str]]) -> int:
    """Deletes old jobs and datasets from Marquez (a soft delete: an event for the
    same name brings it back). A name already gone counts as removed."""
    api = _marquez_api()
    if not old or api is None:
        return 0
    import httpx
    from urllib.parse import quote

    s = get_settings().openlineage
    headers = {"Authorization": f"Bearer {s.api_key}"} if s.api_key else {}
    removed = 0
    with httpx.Client(timeout=s.timeout_seconds, headers=headers, transport=_http_transport) as client:
        for kind, ns, name in old:
            try:
                r = client.delete(f"{api}/namespaces/{quote(ns, safe='')}/{kind}/{quote(name, safe='')}")
            except httpx.HTTPError as e:
                logger.warning("openlineage: old %s «%s» not removed: %s", kind[:-1], name, e)
                continue
            if r.status_code < 300 or r.status_code == 404:
                removed += 1
            else:
                logger.warning("openlineage: old %s «%s» not removed: HTTP %s", kind[:-1], name, r.status_code)
    return removed


def pending() -> int:
    """Events waiting to be sent in this process (for the tests and diagnosis)."""
    return _queue.qsize() if _queue is not None else 0


