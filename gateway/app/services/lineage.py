"""Lineage cross-flow: costruisce il grafo di provenienza/impatto tra flussi,
datasource, connessioni e destinazioni esterne (tabelle DB, oggetti S3).

Il grafo è DERIVATO on-demand, senza tabella dedicata: metà degli archi è già
persistita in `datasources` (flow_id = prodotta-da, connection_id = ingerita-da),
l'altra metà si legge dalle `definition` dei flussi (source node → datasource
letta; output node → destinazione scritta; runflow → orchestrazione). È lo
stesso attraversamento dello scheduler, raccolto come grafo invece che eseguito.

Tutto è filtrato per RBAC: si considerano solo gli oggetti nei progetti LEGGIBILI
dall'utente. Un riferimento verso un oggetto non leggibile/rimosso diventa un
nodo `restricted` (l'arco non resta pendente, ma non si rivela il contenuto).
"""
from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.models import Connection, Datasource, Flow, Run, User
from app.services import permissions as perm_service

logger = logging.getLogger(__name__)

# tipi di arco (direzione = verso di flusso del dato/controllo)
EDGE_READ = "read"          # datasource → flusso (consumo)
EDGE_PUBLISH = "publish"    # flusso → datasource (produzione)
EDGE_INGEST = "ingest"      # connessione → datasource (snapshot DB)
EDGE_WRITE = "write"        # flusso → sink esterno (tabella DB / S3)
EDGE_REFRESH = "refresh"    # flusso → datasource (rinfresco, controllo)
EDGE_ORCHESTRATE = "orchestrate"  # flusso → flusso (nodo "Esegui flusso")


def _fid(i: int) -> str:
    return f"flow:{i}"


def _dsid(i: int) -> str:
    return f"ds:{i}"


def _cid(i: int) -> str:
    return f"conn:{i}"


class _Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._edge_seen: set[tuple[str, str, str]] = set()

    def add_node(self, node_id: str, **attrs) -> None:
        # non sovrascrivere un nodo "reale" con un placeholder restricted
        if node_id in self.nodes:
            if not attrs.get("restricted"):
                self.nodes[node_id].update({k: v for k, v in attrs.items() if v is not None})
            return
        self.nodes[node_id] = {"id": node_id, **attrs}

    def add_edge(self, source: str, target: str, kind: str) -> None:
        key = (source, target, kind)
        if key in self._edge_seen:
            return
        self._edge_seen.add(key)
        self.edges.append({"source": source, "target": target, "kind": kind})


def _restricted_ds(g: _Graph, ds_id: int, unknown: set[int]) -> str:
    """Datasource riferita ma fuori dai progetti leggibili (o rimossa). Il motivo
    (rimossa vs non accessibile) è risolto a fine costruzione con una query."""
    unknown.add(ds_id)
    nid = _dsid(ds_id)
    g.add_node(nid, type="datasource", label=f"Datasource #{ds_id}", restricted=True, kind=None,
               meta={"reason": "unknown"})
    return nid


def _restricted_flow(g: _Graph, flow_id: int, unknown: set[int]) -> str:
    unknown.add(flow_id)
    nid = _fid(flow_id)
    g.add_node(nid, type="flow", label=f"Flusso #{flow_id}", restricted=True, meta={"reason": "unknown"})
    return nid


def build_full_graph(session: Session, user: User) -> _Graph:
    """Grafo completo sugli oggetti nei progetti leggibili dall'utente."""
    g = _Graph()
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return g

    flows = session.exec(select(Flow).where(Flow.project_id.in_(readable))).all()
    datasources = session.exec(select(Datasource).where(Datasource.project_id.in_(readable))).all()
    connections = session.exec(select(Connection).where(Connection.project_id.in_(readable))).all()

    flow_ids = {f.id for f in flows}
    ds_by_id = {d.id: d for d in datasources}
    conn_by_id = {c.id: c for c in connections}

    # ── nodi base ────────────────────────────────────────────────────────────
    for c in connections:
        g.add_node(_cid(c.id), type="connection", label=c.name, project_id=c.project_id,
                   meta={"db_type": c.db_type, "database": c.database})
    for d in datasources:
        g.add_node(_dsid(d.id), type="datasource", label=d.name, project_id=d.project_id,
                   kind=d.kind, meta={"rows": d.rows,
                                      "refreshed_at": d.refreshed_at.isoformat() if d.refreshed_at else None})
    # ultimo run per flusso (per salute/impatto): più recente in assoluto +
    # ultimo SUCCESS (base per la staleness). Una sola query sui run dei flussi.
    last_run: dict[int, Run] = {}
    last_success_at: dict[int, datetime] = {}
    if flow_ids:
        runs = session.exec(
            select(Run).where(Run.flow_id.in_(flow_ids), Run.kind == "flow").order_by(Run.started_at)
        ).all()
        for r in runs:  # ordinati crescenti → l'ultimo visto vince
            last_run[r.flow_id] = r
            if r.status == "SUCCESS":
                last_success_at[r.flow_id] = r.finished_at or r.started_at

    for f in flows:
        lr = last_run.get(f.id)
        g.add_node(_fid(f.id), type="flow", label=f.name, project_id=f.project_id,
                   meta={"engine": f.engine, "scheduled": bool(f.run_schedule),
                         "last_run_at": (lr.started_at.isoformat() if lr else None),
                         "last_run_status": (lr.status if lr else None),
                         "never_run": lr is None})

    # ── archi persistiti dalla tabella datasources ───────────────────────────
    for d in datasources:
        if d.flow_id and d.flow_id in flow_ids:
            g.add_edge(_fid(d.flow_id), _dsid(d.id), EDGE_PUBLISH)   # flusso → produce datasource
        if d.connection_id and d.connection_id in conn_by_id:
            g.add_edge(_cid(d.connection_id), _dsid(d.id), EDGE_INGEST)  # connessione → snapshot

    # ── archi dedotti dalle definition dei flussi ────────────────────────────
    unknown_ds: set[int] = set()    # datasource riferite ma non leggibili/rimosse
    unknown_flow: set[int] = set()  # flussi riferiti ma non leggibili/rimossi
    for f in flows:
        try:
            definition = json.loads(f.definition or "{}")
        except json.JSONDecodeError:
            logger.warning("lineage: definition non valida per flusso %s", f.id)
            continue
        for node in definition.get("nodes") or []:
            t = node.get("type")
            d = node.get("data") or {}

            if t == "source":
                ds_id = d.get("datasourceId")
                if ds_id is None:
                    continue
                target = _dsid(ds_id) if ds_id in ds_by_id else _restricted_ds(g, ds_id, unknown_ds)
                g.add_edge(target, _fid(f.id), EDGE_READ)            # datasource → flusso

            elif t == "refresh":
                ds_id = d.get("datasourceId")
                if ds_id is None:
                    continue
                target = _dsid(ds_id) if ds_id in ds_by_id else _restricted_ds(g, ds_id, unknown_ds)
                g.add_edge(_fid(f.id), target, EDGE_REFRESH)

            elif t == "runflow":
                sub_id = d.get("flowId")
                if sub_id is None:
                    continue
                target = _fid(sub_id) if sub_id in flow_ids else _restricted_flow(g, sub_id, unknown_flow)
                g.add_edge(_fid(f.id), target, EDGE_ORCHESTRATE)

            elif t == "output":
                dest = d.get("destType") or "datasource"
                if dest == "database":
                    conn_id = d.get("connectionId")
                    table = (d.get("table") or "").strip() or "(tabella?)"
                    sink = f"dbsink:{conn_id}|{table}"
                    conn = conn_by_id.get(conn_id)
                    g.add_node(sink, type="db_sink", label=table, restricted=conn is None,
                               meta={"connection_id": conn_id,
                                     "connection_name": conn.name if conn else None,
                                     "db_type": conn.db_type if conn else None,
                                     "mode": d.get("mode") or "append"})
                    g.add_edge(_fid(f.id), sink, EDGE_WRITE)
                elif dest == "s3":
                    bucket = (d.get("s3Bucket") or "").strip()
                    key = (d.get("s3Key") or "").strip() or "(chiave?)"
                    sink = f"s3sink:{bucket}|{key}"
                    g.add_node(sink, type="s3_sink", label=key or bucket,
                               meta={"bucket": bucket, "key": key,
                                     "format": d.get("s3Format") or "parquet",
                                     "partitions": len(d.get("partitionBy") or [])})
                    g.add_edge(_fid(f.id), sink, EDGE_WRITE)
                # dest == "datasource": la produzione è già coperta dall'arco
                # PUBLISH persistito (datasources.flow_id) quando il flusso è girato

    # ── motivo dei riferimenti non risolti: rimosso vs non accessibile ─────────
    # una sola query per tipo controlla l'ESISTENZA (non il contenuto: non si
    # rivela nulla di un oggetto in un progetto non leggibile oltre "esiste").
    if unknown_ds:
        existing = {i for (i,) in session.exec(select(Datasource.id).where(Datasource.id.in_(unknown_ds)))}
        for i in unknown_ds:
            g.nodes[_dsid(i)]["meta"]["reason"] = "inaccessible" if i in existing else "removed"
    if unknown_flow:
        existing = {i for (i,) in session.exec(select(Flow.id).where(Flow.id.in_(unknown_flow)))}
        for i in unknown_flow:
            g.nodes[_fid(i)]["meta"]["reason"] = "inaccessible" if i in existing else "removed"

    # ── staleness DIRETTA: un flusso legge una datasource DB rinfrescata DOPO il
    # suo ultimo run riuscito (i suoi output sono su dati superati) ─────────────
    for e in g.edges:
        if e["kind"] != EDGE_READ or not e["source"].startswith("ds:"):
            continue
        ds = ds_by_id.get(int(e["source"].split(":")[1]))
        if not ds or not ds.refreshed_at:
            continue
        succ = last_success_at.get(int(e["target"].split(":")[1]))
        if succ is not None and ds.refreshed_at > succ:
            node = g.nodes.get(e["target"])
            if node:
                node["meta"]["stale"] = True
                node["meta"]["stale_reason"] = "source"  # causa diretta

    # ── staleness a CASCATA: chi sta a valle di un nodo stale (lungo il flusso
    # dei DATI: read/publish/write) eredita il sospetto ────────────────────────
    down: dict[str, list[str]] = {}
    for e in g.edges:
        if e["kind"] in (EDGE_READ, EDGE_PUBLISH, EDGE_WRITE):
            down.setdefault(e["source"], []).append(e["target"])
    seeds = [nid for nid, n in g.nodes.items() if n.get("meta", {}).get("stale")]
    queue = deque(seeds)
    visited: set[str] = set(seeds)
    while queue:
        cur = queue.popleft()
        for nxt in down.get(cur, []):
            node = g.nodes.get(nxt)
            if node is None:
                continue
            if not node["meta"].get("stale"):
                node["meta"]["stale"] = True
                node["meta"]["stale_reason"] = "upstream"  # ereditata
            if nxt not in visited:
                visited.add(nxt)
                queue.append(nxt)

    return g


def subgraph(g: _Graph, center: str, direction: str = "both", depth: int = 3) -> _Graph:
    """Estrae il sottografo raggiungibile dal nodo `center` entro `depth` salti,
    seguendo gli archi a monte (upstream), a valle (downstream) o entrambi."""
    out = _Graph()
    if center not in g.nodes:
        return out

    # adiacenze per direzione
    downstream: dict[str, list[tuple[str, dict]]] = {}
    upstream: dict[str, list[tuple[str, dict]]] = {}
    for e in g.edges:
        downstream.setdefault(e["source"], []).append((e["target"], e))
        upstream.setdefault(e["target"], []).append((e["source"], e))

    keep_nodes: set[str] = {center}
    keep_edges: list[dict] = []
    # BFS: ogni voce è (nodo, salti_rimasti)
    q: deque[tuple[str, int]] = deque([(center, depth)])
    visited: set[tuple[str, int]] = set()
    go_down = direction in ("both", "downstream")
    go_up = direction in ("both", "upstream")

    while q:
        nid, budget = q.popleft()
        if budget <= 0 or (nid, budget) in visited:
            continue
        visited.add((nid, budget))
        if go_down:
            for nxt, e in downstream.get(nid, []):
                keep_nodes.add(nxt)
                keep_edges.append(e)
                q.append((nxt, budget - 1))
        if go_up:
            for prv, e in upstream.get(nid, []):
                keep_nodes.add(prv)
                keep_edges.append(e)
                q.append((prv, budget - 1))

    out.nodes = {nid: g.nodes[nid] for nid in keep_nodes if nid in g.nodes}
    seen: set[tuple[str, str, str]] = set()
    for e in keep_edges:
        key = (e["source"], e["target"], e["kind"])
        if key in seen or e["source"] not in out.nodes or e["target"] not in out.nodes:
            continue
        seen.add(key)
        out.edges.append(e)
    return out
