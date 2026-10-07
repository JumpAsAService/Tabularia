"""OpenLineage: il lineage di Tabularia raccontato a un catalogo esterno.

Il lineage interno (`services.lineage`) resta com'è: derivato al volo, filtrato
per RBAC, mostrato nella pagina Lineage. Questo modulo è in più e OPZIONALE: se
`OPENLINEAGE__URL` (o `__FILE`) non è impostato non fa niente. Quando è acceso,
ogni run che si chiude diventa un `RunEvent` dello standard OpenLineage, con la
libreria ufficiale, verso Marquez o qualunque altro collector compatibile:

- l'esecuzione di un flusso = un job (nome = percorso della cartella + nome del
  flusso), i run dei suoi nodi Output = job figli con `ParentRunFacet`;
- il refresh di una datasource da database o SharePoint = un job a sé;
- input: la datasource letta (parquet nel bucket), la tabella del database, la
  query SQL (tabelle estratte con sqlglot, testo nel facet `sql`), il file
  SharePoint; output: la datasource pubblicata, la tabella scritta, l'oggetto
  S3/GCS, la copia-specchio. Un'email non è un dataset e non compare.

I nomi dei dataset seguono le convenzioni di naming di OpenLineage (namespace
`postgres://host:porta`, nome `db.schema.tabella`; `s3://bucket` + percorso), così
un catalogo che riceve lineage anche da altri strumenti incrocia gli stessi nodi.

Gli eventi partono DOPO il commit che chiude il run, da un thread di servizio:
un collector giù o lento non ferma mai un'esecuzione, e non tiene aperta la
transazione. Un evento perso (il processo muore prima di mandarlo) non si
ritenta: il lineage è una cronaca, non un contratto; l'export statico
(`job_events_for_flow`) ricostruisce comunque le dipendenze dalla definizione.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import Connection, Datasource, Flow, Run
from app.services import permissions as perm_service

logger = logging.getLogger(__name__)

PRODUCER = "https://github.com/JumpAsAService/Tabularia"
_LOCK = threading.Lock()
_client: Any = None            # OpenLineageClient, costruito una volta (o iniettato dai test)
_worker: ThreadPoolExecutor | None = None


# ── accensione ───────────────────────────────────────────────────────────────
def enabled() -> bool:
    s = get_settings().openlineage
    return bool(s.url or s.file or os.environ.get("OPENLINEAGE_URL") or os.environ.get("OPENLINEAGE_CONFIG"))


def set_client(client: Any) -> None:
    """Per i test: un client con un trasporto che cattura gli eventi."""
    global _client
    with _LOCK:
        _client = client


def _get_client() -> Any:
    global _client
    with _LOCK:
        if _client is not None:
            return _client
        from openlineage.client import OpenLineageClient
        from openlineage.client.transport import get_default_factory

        s = get_settings().openlineage
        configurazioni = []
        if s.url:
            http = {"type": "http", "url": s.url, "endpoint": s.endpoint, "timeout": s.timeout_seconds}
            if s.api_key:
                http["auth"] = {"type": "api_key", "apiKey": s.api_key}
            configurazioni.append(http)
        if s.file:
            configurazioni.append({"type": "file", "log_file_path": s.file, "append": True})
        if len(configurazioni) == 1:
            _client = OpenLineageClient(transport=get_default_factory().create(configurazioni[0]))
        elif configurazioni:
            _client = OpenLineageClient(transport=get_default_factory().create(
                {"type": "composite", "transports": configurazioni, "continue_on_failure": True}))
        else:
            _client = OpenLineageClient()  # OPENLINEAGE_URL / OPENLINEAGE_CONFIG dell'ambiente, come vuole lo standard
        return _client


def _pool() -> ThreadPoolExecutor:
    global _worker
    with _LOCK:
        if _worker is None:
            _worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="openlineage")
        return _worker


# ── nomi ─────────────────────────────────────────────────────────────────────
def namespace() -> str:
    return get_settings().openlineage.namespace


def run_uuid(run_id: int) -> str:
    """Un UUID stabile per run: lo stesso run, lo stesso id, da qualunque processo."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tabularia://{namespace()}/runs/{run_id}"))


def _project_path(session: Session, project_id: int | None) -> str:
    if project_id is None:
        return ""
    progetti = perm_service._all_projects(session)
    ids = perm_service.ancestor_ids(progetti, project_id)
    return "/".join(progetti[i].name for i in reversed(ids) if i in progetti) if ids else ""


def job_name_for_flow(session: Session, flow: Flow) -> str:
    percorso = _project_path(session, flow.project_id)
    return f"{percorso}/{flow.name}" if percorso else flow.name


def job_name_for_refresh(session: Session, ds: Datasource) -> str:
    percorso = _project_path(session, ds.project_id)
    return f"{percorso}/{ds.name} (refresh)" if percorso else f"{ds.name} (refresh)"


_PORTE = {"postgresql": 5432, "mysql": 3306, "mariadb": 3306, "clickhouse": 8443, "trino": 8080}
_SCHEMI = {"postgresql": "postgres", "mysql": "mysql", "mariadb": "mysql", "clickhouse": "clickhouse", "trino": "trino"}


def _host_port(conn: Connection) -> str:
    host = (conn.host or "").split("://")[-1].rstrip("/")
    if ":" in host:
        return host
    return f"{host}:{conn.port or _PORTE.get(conn.db_type, 0)}"


def _storage_namespace(bucket: str) -> str:
    # il gateway non ha una configurazione dello storage: legge l'endpoint
    # dell'engine dall'ambiente condiviso, se c'è (GCS via API S3 → `gs://`)
    endpoint = (os.environ.get("STORAGE__ENDPOINT") or "").lower()
    return f"{'gs' if 'googleapis' in endpoint else 's3'}://{bucket}"


def table_dataset_name(conn: Connection, table: str) -> str:
    """`db.schema.tabella` per Postgres e Trino, `db.tabella` per MySQL e ClickHouse,
    come vuole la convenzione di naming di OpenLineage. Un nome già qualificato
    dall'utente (`schema.tabella`) si rispetta."""
    parti = [p.strip('"`[]') for p in table.strip().split(".") if p.strip()]
    tipo = conn.db_type
    if tipo in ("postgresql", "trino"):
        if len(parti) == 1:
            parti = [conn.db_schema or "public", *parti] if tipo == "postgresql" else [conn.db_schema or "default", *parti]
        if len(parti) == 2:
            parti = [conn.database, *parti]
    else:
        if len(parti) == 1:
            parti = [conn.database, *parti]
    return ".".join(p for p in parti if p)


def _tables_in_sql(conn: Connection, sql: str) -> list[str]:
    """Le tabelle lette da una query, con sqlglot nel dialetto della connessione.
    Best effort: una query che sqlglot non capisce dà una lista vuota (il testo
    della query resta nel facet `sql`)."""
    try:
        import sqlglot
        from sqlglot import exp

        albero = sqlglot.parse_one(sql, read=_SCHEMI.get(conn.db_type))
        cte = {c.alias_or_name for c in albero.find_all(exp.CTE)}
        nomi = []
        for t in albero.find_all(exp.Table):
            if t.name in cte or not t.name:
                continue
            qualificato = ".".join(p for p in (t.catalog, t.db, t.name) if p)
            nomi.append(table_dataset_name(conn, qualificato))
        return sorted(set(nomi))
    except Exception:  # noqa: BLE001 — una query strana non deve rompere il lineage
        return []


# ── dataset ──────────────────────────────────────────────────────────────────
def _schema_facet(columns_json: str | None):
    from openlineage.client.facet_v2 import schema_dataset

    try:
        cols = json.loads(columns_json or "[]")
    except json.JSONDecodeError:
        cols = []
    campi = [schema_dataset.SchemaDatasetFacetFields(name=str(c.get("name")), type=str(c.get("dtype") or ""))
             for c in cols if isinstance(c, dict) and c.get("name")]
    return schema_dataset.SchemaDatasetFacet(fields=campi) if campi else None


def datasource_dataset(ds: Datasource, output: bool = False, rows: int | None = None):
    """La datasource come dataset: nome STABILE (`/datasets/<id>`), lo snapshot
    corrente nel facet `datasetVersion`."""
    from openlineage.client.event_v2 import InputDataset, OutputDataset
    from openlineage.client.facet_v2 import (
        dataset_version_dataset, datasource_dataset, documentation_dataset, output_statistics_output_dataset, storage_dataset,
    )

    ns = _storage_namespace(ds.bucket)
    facets: dict[str, Any] = {
        "dataSource": datasource_dataset.DatasourceDatasetFacet(name=ds.name, uri=f"{ns}/{ds.key}"),
        "storage": storage_dataset.StorageDatasetFacet(storageLayer="object-storage", fileFormat="parquet"),
    }
    if ds.key:
        facets["datasetVersion"] = dataset_version_dataset.DatasetVersionDatasetFacet(datasetVersion=ds.key)
    schema = _schema_facet(ds.columns)
    if schema:
        facets["schema"] = schema
    if ds.description:
        facets["documentation"] = documentation_dataset.DocumentationDatasetFacet(description=ds.description)
    nome = f"/datasets/{ds.id}"
    if output:
        out_facets = {}
        if rows is not None:
            out_facets["outputStatistics"] = output_statistics_output_dataset.OutputStatisticsOutputDatasetFacet(rowCount=rows)
        return OutputDataset(namespace=ns, name=nome, facets=facets, outputFacets=out_facets)
    return InputDataset(namespace=ns, name=nome, facets=facets)


def table_dataset(conn: Connection, table: str, output: bool = False, rows: int | None = None, mode: str | None = None):
    from openlineage.client.event_v2 import InputDataset, OutputDataset
    from openlineage.client.facet_v2 import datasource_dataset, lifecycle_state_change_dataset, output_statistics_output_dataset

    ns = f"{_SCHEMI.get(conn.db_type, conn.db_type)}://{_host_port(conn)}"
    nome = table_dataset_name(conn, table)
    facets: dict[str, Any] = {"dataSource": datasource_dataset.DatasourceDatasetFacet(name=conn.name, uri=ns)}
    if output:
        if mode:
            stato = lifecycle_state_change_dataset.LifecycleStateChange.OVERWRITE if mode == "replace" else lifecycle_state_change_dataset.LifecycleStateChange.ALTER
            facets["lifecycleStateChange"] = lifecycle_state_change_dataset.LifecycleStateChangeDatasetFacet(lifecycleStateChange=stato)
        out_facets = {}
        if rows is not None:
            out_facets["outputStatistics"] = output_statistics_output_dataset.OutputStatisticsOutputDatasetFacet(rowCount=rows)
        return OutputDataset(namespace=ns, name=nome, facets=facets, outputFacets=out_facets)
    return InputDataset(namespace=ns, name=nome, facets=facets)


def object_dataset(endpoint: str, bucket: str, key: str, fmt: str | None = None, output: bool = False, rows: int | None = None):
    """Un oggetto su S3 o GCS (copia-specchio, destinazione file, file caricato)."""
    from openlineage.client.event_v2 import InputDataset, OutputDataset
    from openlineage.client.facet_v2 import output_statistics_output_dataset, storage_dataset

    ns = f"{'gs' if 'googleapis' in (endpoint or '').lower() else 's3'}://{bucket}"
    nome = "/" + key.strip("/")
    facets: dict[str, Any] = {}
    if fmt:
        facets["storage"] = storage_dataset.StorageDatasetFacet(storageLayer="object-storage", fileFormat=fmt)
    if output:
        out_facets = {}
        if rows is not None:
            out_facets["outputStatistics"] = output_statistics_output_dataset.OutputStatisticsOutputDatasetFacet(rowCount=rows)
        return OutputDataset(namespace=ns, name=nome, facets=facets, outputFacets=out_facets)
    return InputDataset(namespace=ns, name=nome, facets=facets)


def sharepoint_dataset(conn: Connection, ref: dict):
    from openlineage.client.event_v2 import InputDataset
    from openlineage.client.facet_v2 import datasource_dataset

    sito = (conn.host or "").split("://")[-1].rstrip("/")
    percorso = "/" + str(ref.get("path") or "").strip("/")
    if ref.get("sheet"):
        percorso += f"#{ref['sheet']}"
    return InputDataset(namespace=f"sharepoint://{sito}", name=percorso,
                        facets={"dataSource": datasource_dataset.DatasourceDatasetFacet(name=conn.name, uri=f"https://{sito}")})


def origin_inputs(session: Session, ds: Datasource) -> tuple[list, dict]:
    """Da dove una datasource DATABASE prende i dati: gli input del suo refresh e
    i facet del job (la query, se è una query)."""
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is None:
        return [], {}
    if conn.db_type == "sharepoint":
        try:
            ref = json.loads(ds.source_ref or "{}")
        except json.JSONDecodeError:
            ref = {}
        return [sharepoint_dataset(conn, ref)], {}
    if ds.source_type == "sql":
        from openlineage.client.facet_v2 import sql_job

        sql = ds.source_ref or ""
        inputs = [table_dataset(conn, t) for t in _tables_in_sql(conn, sql)]
        return inputs, {"sql": sql_job.SQLJobFacet(query=sql, dialect=_SCHEMI.get(conn.db_type))}
    return [table_dataset(conn, ds.source_ref or "")], {}


# ── flussi: input e output dalla definizione ─────────────────────────────────
def _definition(flow: Flow) -> dict:
    try:
        d = json.loads(flow.definition or "{}")
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def flow_inputs(session: Session, flow: Flow) -> list:
    """Ciò che il flusso legge: le datasource dei nodi sorgente, o il file
    caricato se la sorgente non viene dal catalogo. Senza doppioni."""
    visti: set[tuple[str, str]] = set()
    inputs = []
    for n in _definition(flow).get("nodes") or []:
        if n.get("type") != "source":
            continue
        d = n.get("data") or {}
        ds = session.get(Datasource, d["datasourceId"]) if d.get("datasourceId") is not None else None
        if ds is not None:
            dataset = datasource_dataset(ds)
        elif d.get("parquetKey"):
            dataset = object_dataset("", d.get("bucket") or get_settings().engine.bucket, d["parquetKey"], "parquet")
        else:
            continue
        chiave = (dataset.namespace, dataset.name)
        if chiave not in visti:
            visti.add(chiave)
            inputs.append(dataset)
    return inputs


def _find_datasource(session: Session, project_id: int | None, name: str) -> Datasource | None:
    if project_id is None or not name:
        return None
    return session.exec(select(Datasource).where(Datasource.project_id == project_id, Datasource.name == name)).first()


def flow_outputs(session: Session, flow: Flow) -> list:
    """Ciò che il flusso scrive, letto dai nodi Output della definizione (senza
    eseguirlo). Una datasource che il flusso creerà al primo run non ha ancora un
    id: il suo dataset porta il nome di catalogo (`tabularia://…`) finché non esiste."""
    from openlineage.client.event_v2 import OutputDataset

    outputs = []
    for n in _definition(flow).get("nodes") or []:
        if n.get("type") != "output":
            continue
        d = n.get("data") or {}
        tipo = d.get("destType") or "datasource"
        if tipo == "datasource":
            ds = _find_datasource(session, d.get("projectId"), (d.get("name") or "").strip())
            if ds is not None:
                outputs.append(datasource_dataset(ds, output=True))
            elif d.get("name"):
                percorso = _project_path(session, d.get("projectId"))
                outputs.append(OutputDataset(namespace=f"tabularia://{namespace()}", name=f"/{percorso}/{d['name'].strip()}".replace("//", "/")))
            if d.get("mirrorEnabled") and d.get("mirrorConnectionId"):
                mconn = session.get(Connection, d["mirrorConnectionId"])
                if mconn is not None:
                    outputs.append(object_dataset(mconn.host, (d.get("mirrorBucket") or mconn.database or "").strip(), d.get("mirrorKey") or "", "parquet", output=True))
        elif tipo == "database":
            conn = session.get(Connection, d.get("connectionId")) if d.get("connectionId") else None
            if conn is not None and (d.get("table") or "").strip():
                outputs.append(table_dataset(conn, d["table"].strip(), output=True, mode=d.get("mode") or "append"))
        elif tipo == "s3":
            conn = session.get(Connection, d.get("connectionId")) if d.get("connectionId") else None
            if conn is not None and (d.get("s3Key") or "").strip():
                outputs.append(object_dataset(conn.host, (d.get("s3Bucket") or conn.database or "").strip(), d["s3Key"].strip(), d.get("s3Format") or "parquet", output=True))
        # email: non è un dataset
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
    """Il lineage STATICO del flusso: un `JobEvent` con ciò che legge e scrive,
    ricostruito dalla definizione. Per popolare un catalogo coi flussi esistenti,
    o scaricare le dipendenze come file."""
    from openlineage.client.event_v2 import JobEvent

    return [JobEvent(eventTime=_now(), producer=PRODUCER, job=_job(session, flow),
                     inputs=flow_inputs(session, flow), outputs=flow_outputs(session, flow))]


# ── run: gli eventi di un'esecuzione chiusa ──────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso(t: datetime | None) -> str:
    if t is None:
        return _now()
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _run_outputs(session: Session, run: Run) -> list:
    """Gli output di UN run di nodo Output: dal run stesso, che sa cos'ha scritto."""
    outputs = []
    if run.datasource_id is not None and run.publish_name:
        ds = session.get(Datasource, run.datasource_id)
        if ds is not None:
            outputs.append(datasource_dataset(ds, output=True, rows=run.rows_written))
    if run.destination:
        try:
            dest = json.loads(run.destination)
        except json.JSONDecodeError:
            dest = {}
        conn = session.get(Connection, dest.get("connection_id")) if dest.get("connection_id") else None
        if dest.get("type") == "database" and conn is not None and dest.get("table"):
            outputs.append(table_dataset(conn, dest["table"], output=True, rows=run.rows_written, mode=dest.get("mode")))
        elif dest.get("type") == "s3" and dest.get("bucket") and dest.get("key"):
            outputs.append(object_dataset(dest.get("endpoint") or "", dest["bucket"], dest["key"], dest.get("format"), output=True, rows=run.rows_written))
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
            return f"table {d.get('table')}" if d.get("type") == "database" else f"file {d.get('key')}"
        except json.JSONDecodeError:
            pass
    if run.email:
        return "email"
    return f"output {run.id}"


def run_events(session: Session, run: Run) -> list:
    """START e COMPLETE/FAIL del run, costruiti quando il run si è CHIUSO: i due
    eventi arrivano insieme, con i tempi veri, e Marquez ne ricava la durata."""
    from openlineage.client.event_v2 import Job, Run as OlRun, RunEvent, RunState
    from openlineage.client.facet_v2 import error_message_run, job_type_job, parent_run

    run_facets: dict[str, Any] = {}
    if run.kind == "orchestration":
        flow = session.get(Flow, run.flow_id) if run.flow_id else None
        if flow is None:
            return []
        job = _job(session, flow)
        inputs = flow_inputs(session, flow)
        figli = session.exec(select(Run).where(Run.parent_run_id == run.id, Run.kind == "flow")).all()
        outputs = [o for f in figli for o in _run_outputs(session, f)]
    elif run.kind == "flow":
        flow = session.get(Flow, run.flow_id) if run.flow_id else None
        if flow is None:
            return []
        extra: dict[str, Any] = {"jobType": job_type_job.JobTypeJobFacet(processingType="BATCH", integration="TABULARIA", jobType="OUTPUT")}
        job = Job(namespace=namespace(), name=f"{job_name_for_flow(session, flow)} › {_output_label(run)}", facets=extra)
        if run.parent_run_id is not None:
            run_facets["parent"] = parent_run.ParentRunFacet(
                run=parent_run.Run(runId=run_uuid(run.parent_run_id)),
                job=parent_run.Job(namespace=namespace(), name=job_name_for_flow(session, flow)),
            )
        inputs = flow_inputs(session, flow)
        outputs = _run_outputs(session, run)
    elif run.kind == "ingest":
        ds = session.get(Datasource, run.datasource_id) if run.datasource_id else None
        if ds is None:
            return []
        inputs, job_facets = origin_inputs(session, ds)
        job_facets["jobType"] = job_type_job.JobTypeJobFacet(processingType="BATCH", integration="TABULARIA", jobType="REFRESH")
        job = Job(namespace=namespace(), name=job_name_for_refresh(session, ds), facets=job_facets)
        outputs = [datasource_dataset(ds, output=True, rows=run.rows_written)] if run.status == "SUCCESS" else [datasource_dataset(ds, output=True)]
        if run.parent_run_id is not None:
            padre = session.get(Run, run.parent_run_id)
            padre_flow = session.get(Flow, padre.flow_id) if padre is not None and padre.flow_id else None
            if padre_flow is not None:
                run_facets["parent"] = parent_run.ParentRunFacet(
                    run=parent_run.Run(runId=run_uuid(run.parent_run_id)),
                    job=parent_run.Job(namespace=namespace(), name=job_name_for_flow(session, padre_flow)),
                )
    else:
        return []

    ol_run = OlRun(runId=run_uuid(run.id), facets=run_facets)
    start = RunEvent(eventTime=_iso(run.started_at), producer=PRODUCER, run=ol_run, job=job, eventType=RunState.START, inputs=inputs, outputs=[])
    fine_facets = dict(run_facets)
    if run.status != "SUCCESS" and run.error:
        fine_facets["errorMessage"] = error_message_run.ErrorMessageRunFacet(message=run.error[:2000], programmingLanguage="python")
    fine = RunEvent(
        eventTime=_iso(run.finished_at), producer=PRODUCER, run=OlRun(runId=run_uuid(run.id), facets=fine_facets), job=job,
        eventType=RunState.COMPLETE if run.status == "SUCCESS" else RunState.FAIL, inputs=inputs, outputs=outputs,
    )
    return [start, fine]


def to_json(events: Iterable) -> list[dict]:
    from openlineage.client.serde import Serde

    return [json.loads(Serde.to_json(e)) for e in events]


# ── emissione ────────────────────────────────────────────────────────────────
def _emit(events: list) -> int:
    client = _get_client()
    n = 0
    for e in events:
        client.emit(e)
        n += 1
    return n


def _emit_in_background(run_id: int, eventi: list) -> None:
    try:
        _emit(eventi)
        logger.info("openlineage: %d eventi per il run %s", len(eventi), run_id)
    except Exception:  # noqa: BLE001 — il run è già chiuso: il lineage non deve pesare su niente
        logger.exception("openlineage: eventi del run %s non inviati", run_id)


def run_closed(session: Session, run: Run) -> None:
    """Da chiamare DOPO il commit che chiude un run. Gli eventi si costruiscono
    qui, con la sessione di chi chiama (qualche lettura); la spedizione va in un
    thread di servizio. Non blocca, non solleva."""
    if not enabled():
        return
    try:
        eventi = run_events(session, run)
        if eventi:
            _pool().submit(_emit_in_background, run.id, eventi)
    except Exception:  # noqa: BLE001
        logger.exception("openlineage: eventi del run %s non costruiti", run.id)


def emit_flow(session: Session, flow: Flow) -> int:
    """Manda il lineage statico del flusso al collector, subito. Solleva se il
    collector rifiuta: chi lo chiede a mano vuole saperlo."""
    return _emit(job_events_for_flow(session, flow))


def wait_idle(timeout: float = 10.0) -> None:
    """Per i test e lo spegnimento: aspetta che la coda si svuoti."""
    global _worker
    with _LOCK:
        w, _worker = _worker, None
    if w is not None:
        w.shutdown(wait=True)


