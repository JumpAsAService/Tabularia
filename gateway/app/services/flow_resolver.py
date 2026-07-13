"""Risoluzione lato server della definizione di un flusso in richieste di run.

È il gemello Python di `resolveChain`/`executeOutputRuns` del frontend: serve
allo SCHEDULER, che gira senza browser. Per ogni nodo Output produce il corpo
di un run (bucket, input_key, operations, publish|destination), risalendo la
catena e iniettando i rami destri di join/union e il driver/corpo dei foreach.

Differenza cruciale con il frontend (Opzione A dello scheduling): le sorgenti
datasource NON usano la chiave congelata nella definizione, ma vengono
RI-RISOLTE allo snapshot corrente via `resolve_ds` (un refresh cancella il blob
vecchio: una chiave congelata darebbe 404). Così il run schedulato usa sempre i
dati correnti e segue le modifiche al flusso.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

# (bucket, key) dello snapshot corrente di una datasource, o None se assente
DsResolver = Callable[[int], Optional[tuple[str, str]]]


class FlowResolveError(ValueError):
    """La definizione non è eseguibile (output scollegato, sorgente mancante…)."""


# handle degli archi di SEQUENZA (orchestrazione), da NON confondere con i dati
SEQ_TARGET_HANDLE = "seq-in"
SEQ_SOURCE_HANDLE = "seq-out"


def _is_seq_edge(e: dict) -> bool:
    return e.get("targetHandle") == SEQ_TARGET_HANDLE or e.get("sourceHandle") == SEQ_SOURCE_HANDLE


def _incoming(edges: list[dict]) -> dict[str, dict[str, str]]:
    """target -> {left, right} in base al targetHandle degli archi DATI (gli archi
    di sequenza, verticali, non fanno parte della pipeline dati)."""
    m: dict[str, dict[str, str]] = {}
    for e in edges:
        if _is_seq_edge(e):
            continue
        handle = e.get("targetHandle") or "left"
        entry = m.setdefault(e.get("target"), {})
        entry["right" if handle == "right" else "left"] = e.get("source")
    return m


def _resolve_source(node: dict, resolve_ds: DsResolver) -> Optional[tuple[str, str]]:
    """Nodo sorgente → (bucket, key). Datasource del catalogo → snapshot CORRENTE
    (ri-risolto per id); upload → chiave del file (permanente)."""
    data = node.get("data") or {}
    ds_id = data.get("datasourceId")
    if ds_id is not None:
        resolved = resolve_ds(ds_id)
        if resolved and resolved[1]:
            return resolved
        return None  # datasource sparita o senza snapshot → sorgente non risolvibile
    key = data.get("parquetKey")
    if key:
        return (data.get("bucket"), key)
    return None


class _Resolver:
    def __init__(self, nodes: list[dict], edges: list[dict], resolve_ds: DsResolver):
        self.by_id = {n["id"]: n for n in nodes}
        self.edges = edges
        self.inc = _incoming(edges)
        self.resolve_ds = resolve_ds

    def chain(self, target_id: str) -> tuple[Optional[tuple[str, str]], list[dict]]:
        """Catena che termina in target_id: (sorgente risolta, operazioni IR)."""
        target = self.by_id.get(target_id)
        if target and target.get("parentNode"):
            upstream_id = self.inc.get(target["parentNode"], {}).get("left")
            up_src, up_ops = self.chain(upstream_id) if upstream_id else (None, [])
            op = self._operation_for(target["parentNode"], until=target_id)
            return up_src, [*up_ops, op]

        op_ids: list[str] = []
        seen: set[str] = set()
        source: Optional[tuple[str, str]] = None
        cur: Optional[str] = target_id
        while cur and cur not in seen:
            seen.add(cur)
            node = self.by_id.get(cur)
            if not node:
                break
            if node.get("type") == "source":
                source = _resolve_source(node, self.resolve_ds)
                break
            if node.get("type") in ("operation", "foreach"):
                op_ids.append(cur)
            cur = self.inc.get(cur, {}).get("left")

        op_ids.reverse()
        operations = [self._operation_for(i) for i in op_ids]
        return source, operations

    def _operation_for(self, node_id: str, until: Optional[str] = None) -> dict:
        node = self.by_id[node_id]
        data = node.get("data") or {}
        params: dict[str, Any] = dict(data.get("params") or {})
        op_type = data.get("opType")

        if op_type in ("join", "union"):
            right_id = self.inc.get(node_id, {}).get("right")
            if right_id:
                r_src, r_ops = self.chain(right_id)
                if r_src:
                    params["right"] = {
                        "source": {"bucket": r_src[0], "key": r_src[1]},
                        "operations": r_ops,
                    }

        if node.get("type") == "foreach":
            driver_id = self.inc.get(node_id, {}).get("right")
            if driver_id:
                d_src, d_ops = self.chain(driver_id)
                if d_src:
                    params["driver"] = {
                        "source": {"bucket": d_src[0], "key": d_src[1]},
                        "operations": d_ops,
                    }
            params["body"] = self._container_body(node_id, until)

        return {"type": op_type, "params": params}

    def _container_body(self, container_id: str, until: Optional[str]) -> list[dict]:
        children = [n for n in self.by_id.values() if n.get("parentNode") == container_id]
        if not children:
            return []
        child_ids = {c["id"] for c in children}
        nxt: dict[str, str] = {}
        for e in self.edges:
            if (
                e.get("source") in child_ids
                and e.get("target") in child_ids
                and (e.get("targetHandle") or "left") == "left"
            ):
                nxt[e["source"]] = e["target"]
        # figlio d'ingresso: senza un input sinistro INTERNO al container
        cur = next(
            (c["id"] for c in children if self.inc.get(c["id"], {}).get("left") not in child_ids),
            None,
        )
        ops: list[dict] = []
        seen: set[str] = set()
        while cur and cur not in seen:
            seen.add(cur)
            ops.append(self._operation_for(cur))
            if cur == until:
                break
            cur = nxt.get(cur)
        return ops


def _output_label(node: dict) -> str:
    d = node.get("data") or {}
    dt = d.get("destType") or "datasource"
    if dt == "database":
        return f"tabella {d.get('table') or '…'}"
    if dt == "s3":
        return f"S3 {d.get('s3Key') or '…'}"
    return f"datasource “{d.get('name') or '…'}”"


def _output_body(node: dict, source: tuple[str, str], operations: list[dict], default_bucket: str) -> dict:
    """Corpo-run (shape di RunCreate) di UN nodo Output già risolto."""
    bucket, key = source
    body: dict[str, Any] = {
        "bucket": bucket or default_bucket,
        "input_key": key,
        "operations": operations,
    }
    d = node.get("data") or {}
    dest_type = d.get("destType") or "datasource"
    if dest_type == "database":
        body["destination"] = {
            "type": "database",
            "connection_id": d.get("connectionId"),
            "table": (d.get("table") or "").strip(),
            "mode": d.get("mode") or "append",
            "post_sql": d.get("postSql") or "",
        }
    elif dest_type == "s3":
        body["destination"] = {
            "type": "s3",
            "connection_id": d.get("connectionId"),
            "bucket": (d.get("s3Bucket") or "").strip(),
            "key": (d.get("s3Key") or "").strip(),
            "format": d.get("s3Format") or "parquet",
            "partition_by": d.get("partitionBy") or [],
        }
    else:
        body["publish"] = {
            "name": (d.get("name") or "").strip(),
            "project_id": d.get("projectId"),
            "description": d.get("description") or "",
        }
    return body


def resolve_output_request(definition: dict, node: dict, resolve_ds: DsResolver, default_bucket: str) -> dict:
    """Risolve UN nodo Output → corpo-run. Solleva FlowResolveError se la sua
    catena dati non ha una sorgente risolvibile."""
    resolver = _Resolver(definition.get("nodes") or [], definition.get("edges") or [], resolve_ds)
    source, operations = resolver.chain(node["id"])
    if source is None:
        raise FlowResolveError(
            f"output {_output_label(node)}: nessuna sorgente con dati a monte "
            "(o la datasource sorgente non ha uno snapshot)"
        )
    return _output_body(node, source, operations, default_bucket)


def build_output_run_requests(
    definition: dict, resolve_ds: DsResolver, default_bucket: str
) -> list[dict]:
    """Un corpo-run per ogni nodo Output del flusso (senza ordine di sequenza)."""
    nodes = definition.get("nodes") or []
    outputs = [n for n in nodes if n.get("type") == "output"]
    if not outputs:
        raise FlowResolveError("il flusso non ha nodi Output: non c'è nulla da eseguire")
    return [resolve_output_request(definition, n, resolve_ds, default_bucket) for n in outputs]


# ordine di esecuzione dei nodi non collegati in sequenza (default sensato)
_ACTION_TYPES = ("refresh", "output", "runflow")
_DEFAULT_PRIORITY = {"refresh": 0, "output": 1, "runflow": 2}


def sequence_order(definition: dict) -> list[dict]:
    """Ordina gli 'action node' (refresh/output/runflow) secondo gli archi di
    SEQUENZA (topological sort, Kahn). I nodi non collegati escono nell'ordine
    di default (refresh → output → runflow, poi ordine di dichiarazione), così i
    flussi senza archi di sequenza mantengono il comportamento sensato di prima.
    Cicli: i nodi coinvolti finiscono in coda nell'ordine di default (best effort).
    """
    nodes = definition.get("nodes") or []
    edges = definition.get("edges") or []
    action = [n for n in nodes if n.get("type") in _ACTION_TYPES]
    by_id = {n["id"]: n for n in action}
    idx = {n["id"]: i for i, n in enumerate(action)}

    succ: dict[str, list[str]] = {n["id"]: [] for n in action}
    indeg: dict[str, int] = {n["id"]: 0 for n in action}
    for e in edges:
        if e.get("targetHandle") != SEQ_TARGET_HANDLE:
            continue
        s, t = e.get("source"), e.get("target")
        if s in by_id and t in by_id:
            succ[s].append(t)
            indeg[t] += 1

    def rank(nid: str) -> tuple[int, int]:
        return (_DEFAULT_PRIORITY.get(by_id[nid].get("type"), 9), idx[nid])

    ready = sorted([nid for nid in by_id if indeg[nid] == 0], key=rank)
    out: list[dict] = []
    seen: set[str] = set()
    while ready:
        nid = ready.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        out.append(by_id[nid])
        newly = []
        for m in succ[nid]:
            indeg[m] -= 1
            if indeg[m] == 0:
                newly.append(m)
        if newly:
            ready = sorted(ready + newly, key=rank)
    # eventuali nodi in ciclo: appesi in coda, ordine di default
    for nid in sorted((nid for nid in by_id if nid not in seen), key=rank):
        out.append(by_id[nid])
    return out
