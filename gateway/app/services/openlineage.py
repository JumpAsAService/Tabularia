"""OpenLineage: il lineage di Tabularia raccontato a un catalogo esterno.

Il lineage interno (`services.lineage`) resta com'è: derivato al volo, filtrato
per RBAC, mostrato nella pagina Lineage. Questo modulo è in più e OPZIONALE: se
`OPENLINEAGE__URL` (o `__FILE`) non è impostato non fa niente. Quando è acceso,
ogni run che si chiude diventa un `RunEvent` dello standard OpenLineage, con la
libreria ufficiale, verso Marquez o qualunque altro collector compatibile:

- l'esecuzione di un flusso = un job (nome = percorso della cartella + nome del
  flusso), i run dei suoi nodi Output = job figli con `ParentRunFacet`;
- il refresh di una datasource da database o SharePoint = un job a sé;
- input: la datasource letta (col nome che ha in Tabularia, sotto il percorso
  della cartella; il parquet nel bucket come symlink, l'id nel facet
  `tabularia`), la tabella del database, la
  query SQL (tabelle estratte con sqlglot, testo nel facet `sql`), il file
  SharePoint; output: la datasource pubblicata, la tabella scritta, l'oggetto
  S3/GCS, la copia-specchio. Un'email non è un dataset e non compare.

I dataset esterni seguono le convenzioni di naming di OpenLineage (namespace
`postgres://host:porta`, nome `db.schema.tabella`; `s3://bucket` + percorso), così
un catalogo che riceve lineage anche da altri strumenti incrocia gli stessi nodi.
Le datasource di Tabularia invece si chiamano come nel catalogo di Tabularia
(`tabularia://<namespace>` + `/Cartella/Sottocartella/nome`): è il nome che chi
guarda il catalogo riconosce; il percorso fisico del parquet sta nel facet
`symlinks` (e in `dataSource.uri`), l'id e la cartella nel facet `tabularia`.

Gli eventi partono DOPO il commit che chiude il run, da un thread di servizio
con una coda LIMITATA: un collector giù o lento non ferma mai un'esecuzione, non
tiene aperta la transazione e non riempie la memoria (a coda piena l'evento si
scarta, con un avviso nel log). Un evento perso non si ritenta: il lineage è
una cronaca, non un contratto; l'export statico (`job_events_for_flow`)
ricostruisce comunque le dipendenze dalla definizione.
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
from typing import Any, Iterable

from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import Connection, Datasource, Flow, Run
from app.services import permissions as perm_service

logger = logging.getLogger(__name__)

PRODUCER = "https://github.com/JumpAsAService/Tabularia"
QUEUE_MAX = 500                # run chiusi in attesa di spedizione, per processo: oltre, si scarta
                               # (al banco: 57 KB a run con datasource da 150 colonne → al più ~28 MB)
_LOCK = threading.Lock()
_client: Any = None            # OpenLineageClient, costruito una volta (o iniettato dai test)
_coda: "queue.Queue[tuple[int, list] | None] | None" = None
_worker: threading.Thread | None = None
_scartati = 0                  # eventi scartati a coda piena (per il log e i test)
_ultimo_avviso = 0.0


# ── accensione ───────────────────────────────────────────────────────────────
def enabled() -> bool:
    s = get_settings().openlineage
    return bool(s.url or s.file or os.environ.get("OPENLINEAGE_URL") or os.environ.get("OPENLINEAGE_CONFIG"))


def describe() -> str:
    """Una riga per il log di avvio: dove vanno gli eventi, o che non vanno."""
    s = get_settings().openlineage
    if not enabled():
        return "OpenLineage spento (nessun OPENLINEAGE__URL né OPENLINEAGE__FILE)"
    dove = [x for x in (f"HTTP {s.url} ({s.endpoint})" if s.url else "", f"file {s.file}" if s.file else "",
                        "configurazione della libreria (OPENLINEAGE_URL/OPENLINEAGE_CONFIG)" if not (s.url or s.file) else "") if x]
    return f"OpenLineage acceso: eventi verso {' + '.join(dove)}, namespace «{s.namespace}»"


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
            # pochi tentativi: un collector morto non deve tenere il thread per minuti
            # su ogni evento (la coda è limitata, ma intanto si accumula)
            http = {"type": "http", "url": s.url, "endpoint": s.endpoint, "timeout": s.timeout_seconds,
                    "retry": {"total": 2, "connect": 2, "read": 2, "backoff_factor": 0.3, "status_forcelist": [500, 502, 503, 504], "allowed_methods": ["HEAD", "POST"]}}
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


def _lavoro(coda: "queue.Queue") -> None:
    """Il thread di spedizione: prende dalla SUA coda e manda, finché non riceve None."""
    while True:
        voce = coda.get()
        if voce is None:
            coda.task_done()
            return
        run_id, eventi = voce
        try:
            _emit(eventi)
            logger.info("openlineage: %d eventi per il run %s", len(eventi), run_id)
        except Exception as e:  # noqa: BLE001 — il run è già chiuso: il lineage non deve pesare su niente
            logger.warning("openlineage: eventi del run %s non inviati: %s", run_id, str(e)[:300])
        finally:
            coda.task_done()


def _accoda(run_id: int, eventi: list) -> bool:
    """Mette gli eventi in coda senza mai aspettare. A coda piena li scarta."""
    global _coda, _worker, _scartati, _ultimo_avviso
    with _LOCK:
        if _coda is None:
            _coda = queue.Queue(maxsize=QUEUE_MAX)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_lavoro, args=(_coda,), name="openlineage", daemon=True)
            _worker.start()
    try:
        _coda.put_nowait((run_id, eventi))
        return True
    except queue.Full:
        _scartati += 1
        if time.monotonic() - _ultimo_avviso > 60:
            _ultimo_avviso = time.monotonic()
            logger.warning("openlineage: coda piena (%d run in attesa), scarto gli eventi del run %s (%d scartati finora): il collector non sta rispondendo",
                           QUEUE_MAX, run_id, _scartati)
        return False


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


@functools.lru_cache(maxsize=512)
def _qualified_tables(db_type: str | None, sql: str) -> tuple[str, ...]:
    """I nomi qualificati delle tabelle lette da una query (parse sqlglot, qualche
    millisecondo): in cache per testo e dialetto, perché la stessa datasource si
    chiude run dopo run. Best effort: una query che sqlglot non capisce dà niente."""
    try:
        import sqlglot
        from sqlglot import exp

        albero = sqlglot.parse_one(sql, read=_SCHEMI.get(db_type))
        cte = {c.alias_or_name for c in albero.find_all(exp.CTE)}
        return tuple(sorted({".".join(p for p in (t.catalog, t.db, t.name) if p) for t in albero.find_all(exp.Table) if t.name and t.name not in cte}))
    except Exception:  # noqa: BLE001 — una query strana non deve rompere il lineage
        return ()


def _tables_in_sql(conn: Connection, sql: str) -> list[str]:
    """Le tabelle lette da una query, coi nomi standard della connessione (il testo
    della query resta nel facet `sql`)."""
    return sorted({table_dataset_name(conn, q) for q in _qualified_tables(conn.db_type, sql)})


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


@functools.cache
def _tabularia_facet():
    """Il facet `tabularia`: l'id della datasource, la cartella e il tipo — ciò che
    serve per risalire dal catalogo all'oggetto di Tabularia (via API: /datasources/<id>).
    La classe si definisce UNA volta: costruirla con attrs costa millisecondi."""
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
    percorso = _project_path(session, ds.project_id)
    return f"/{percorso}/{ds.name}" if percorso else f"/{ds.name}"


def datasource_dataset(session: Session, ds: Datasource, output: bool = False, rows: int | None = None):
    """La datasource come dataset: il NOME che ha in Tabularia sotto il percorso
    della cartella; il parquet dello snapshot corrente come symlink e nel facet
    `datasetVersion`; l'id nel facet `tabularia`."""
    from openlineage.client.event_v2 import InputDataset, OutputDataset
    from openlineage.client.facet_v2 import (
        dataset_version_dataset, datasource_dataset, documentation_dataset, output_statistics_output_dataset, storage_dataset,
        symlinks_dataset,
    )

    ns = f"tabularia://{namespace()}"
    fisico = _storage_namespace(ds.bucket)
    facets: dict[str, Any] = {
        "dataSource": datasource_dataset.DatasourceDatasetFacet(name=ds.name, uri=f"{fisico}/{ds.key}"),
        "storage": storage_dataset.StorageDatasetFacet(storageLayer="object-storage", fileFormat="parquet"),
        "tabularia": _tabularia_facet()(datasourceId=ds.id, projectId=ds.project_id, kind=ds.kind, folder=_project_path(session, ds.project_id)),
    }
    if ds.key:
        facets["datasetVersion"] = dataset_version_dataset.DatasetVersionDatasetFacet(datasetVersion=ds.key)
    # il symlink è la CARTELLA della datasource nel bucket, non il file dello snapshot:
    # Marquez registra ogni symlink come un alias del dataset, e un alias nuovo a ogni
    # run (un parquet nuovo per snapshot) farebbe crescere il catalogo a ogni esecuzione
    facets["symlinks"] = symlinks_dataset.SymlinksDatasetFacet(identifiers=[symlinks_dataset.Identifier(namespace=fisico, name=f"/datasets/{ds.id}", type="LOCATION")])
    schema = _schema_facet(ds.columns)
    if schema:
        facets["schema"] = schema
    if ds.description:
        facets["documentation"] = documentation_dataset.DocumentationDatasetFacet(description=ds.description)
    nome = datasource_name(session, ds)
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
            dataset = datasource_dataset(session, ds)
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
    eseguirlo). Una datasource che il flusso creerà al primo run non esiste ancora:
    il suo dataset ha già il nome che avrà (cartella + nome), senza i facet."""
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
                outputs.append(datasource_dataset(session, ds, output=True))
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


def _senza_statistiche(outputs: list) -> list:
    from openlineage.client.event_v2 import OutputDataset

    return [OutputDataset(namespace=o.namespace, name=o.name, facets=o.facets, outputFacets={}) for o in outputs]


def _parent_facet(session: Session, run: Run):
    """Il `ParentRunFacet` SOLO per i nodi Output del flusso stesso, che il
    catalogo mette sotto il job del flusso («Flusso.uscita»).

    Marquez rinomina ogni figlio in «<padre>.<figlio>»: un refresh di datasource
    o l'uscita di un sotto-flusso (`Run flow`), che portano il parent_run_id della
    radice, finirebbero in un job diverso da quello che hanno quando girano da
    soli. Per loro il legame con l'orchestrazione sta nel facet `tabularia` del
    run (`parentRunId`), non nella gerarchia dei job."""
    from openlineage.client.facet_v2 import parent_run

    if run.parent_run_id is None or run.kind != "flow":
        return None
    padre = session.get(Run, run.parent_run_id)
    if padre is None or padre.flow_id != run.flow_id:
        return None
    flow = session.get(Flow, padre.flow_id)
    return parent_run.ParentRunFacet(
        run=parent_run.Run(runId=run_uuid(run.parent_run_id)),
        job=parent_run.Job(namespace=namespace(), name=job_name_for_flow(session, flow)),
    )


@functools.cache
def _tabularia_run_facet_class():
    import attr
    from openlineage.client.facet_v2 import RunFacet

    @attr.define
    class TabulariaRunFacet(RunFacet):
        runId: int = attr.field()  # noqa: N815
        kind: str = attr.field()
        trigger: str = attr.field()
        engine: str | None = attr.field(default=None)
        parentRunId: str | None = attr.field(default=None)  # noqa: N815

        @staticmethod
        def _get_schema() -> str:
            return f"{PRODUCER}/blob/main/docs/openlineage/TabulariaRunFacet.json"

    return TabulariaRunFacet


def _tabularia_run_facet(run: Run):
    """Il facet `tabularia` del run: l'id del run in Tabularia, chi l'ha lanciato
    (manuale/schedulato), il motore e l'orchestrazione che lo contiene — anche
    quando il job NON è un figlio nella gerarchia del catalogo."""
    return _tabularia_run_facet_class()(runId=run.id, kind=run.kind, trigger=run.trigger_type or "manual", engine=run.engine or None,
                                        parentRunId=run_uuid(run.parent_run_id) if run.parent_run_id is not None else None)


def run_events(session: Session, run: Run) -> list:
    """START e COMPLETE/FAIL del run, costruiti quando il run si è CHIUSO: i due
    eventi arrivano insieme, con i tempi veri, e Marquez ne ricava la durata."""
    from openlineage.client.event_v2 import Job, Run as OlRun, RunEvent, RunState
    from openlineage.client.facet_v2 import error_message_run, job_type_job, parent_run

    run_facets: dict[str, Any] = {"tabularia": _tabularia_run_facet(run)}
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
        # «<flusso>.<output>» SEMPRE, con o senza orchestrazione: Marquez prefissa da sé
        # un figlio col nome del padre, e un run lanciato dall'editor (senza padre)
        # finirebbe altrimenti in un job diverso dallo stesso Output orchestrato
        job = Job(namespace=namespace(), name=f"{job_name_for_flow(session, flow)}.{_output_label(run)}", facets=extra)
        padre = _parent_facet(session, run)
        if padre is not None:
            run_facets["parent"] = padre
        inputs = flow_inputs(session, flow)
        outputs = _run_outputs(session, run)
    elif run.kind == "ingest":
        ds = session.get(Datasource, run.datasource_id) if run.datasource_id else None
        if ds is None:
            return []
        inputs, job_facets = origin_inputs(session, ds)
        job_facets["jobType"] = job_type_job.JobTypeJobFacet(processingType="BATCH", integration="TABULARIA", jobType="REFRESH")
        job = Job(namespace=namespace(), name=job_name_for_refresh(session, ds), facets=job_facets)
        outputs = [datasource_dataset(session, ds, output=True, rows=run.rows_written if run.status == "SUCCESS" else None)]
        padre = _parent_facet(session, run)
        if padre is not None:
            run_facets["parent"] = padre
    else:
        return []

    ol_run = OlRun(runId=run_uuid(run.id), facets=run_facets)
    # gli output si dichiarano già allo START (senza statistiche): con START vuoto e
    # COMPLETE pieno il catalogo vedrebbe due versioni del job a ogni esecuzione
    start = RunEvent(eventTime=_iso(run.started_at), producer=PRODUCER, run=ol_run, job=job, eventType=RunState.START, inputs=inputs, outputs=_senza_statistiche(outputs))
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


def run_closed(session: Session, run: Run) -> None:
    """Da chiamare DOPO il commit che chiude un run. Gli eventi si costruiscono
    qui, con la sessione di chi chiama (qualche lettura); la spedizione va al
    thread di servizio. Non blocca, non solleva."""
    if not enabled():
        return
    try:
        eventi = run_events(session, run)
        if eventi:
            _accoda(run.id, eventi)
    except Exception:  # noqa: BLE001
        logger.exception("openlineage: eventi del run %s non costruiti", run.id)


def emit_flow(session: Session, flow: Flow) -> int:
    """Manda il lineage statico del flusso al collector, subito. Solleva se il
    collector rifiuta: chi lo chiede a mano vuole saperlo."""
    return _emit(job_events_for_flow(session, flow))


def wait_idle(timeout: float = 10.0) -> None:
    """Per i test e lo spegnimento: aspetta che la coda si svuoti (al più
    `timeout` secondi), poi congeda il thread."""
    global _coda, _worker
    with _LOCK:
        coda, worker = _coda, _worker
        _coda, _worker = None, None
    if coda is None:
        return
    fine = time.monotonic() + timeout
    while coda.unfinished_tasks and time.monotonic() < fine:
        time.sleep(0.02)
    try:
        coda.put(None, timeout=max(0.0, fine - time.monotonic()))   # congedo: il thread esce quando ci arriva
    except queue.Full:
        pass                                                        # resta daemon: muore col processo
    if worker is not None:
        worker.join(timeout=1.0)


def pending() -> int:
    """Eventi in attesa di spedizione in questo processo (per i test e la diagnosi)."""
    return _coda.qsize() if _coda is not None else 0


