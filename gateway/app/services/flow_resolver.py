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

from contextlib import contextmanager
from typing import Any, Callable, Iterable, Optional

# (bucket, key) dello snapshot corrente di una datasource, o None se assente
DsResolver = Callable[[int], Optional[tuple[str, str]]]
# the nodes that run outside Tabularia's engine and hand their output to the nodes after them
STEP_NODE_TYPES = ("script", "container")


class FlowResolveError(ValueError):
    """La definizione non è eseguibile (output scollegato, sorgente mancante…).
    `code` and `params` (optional) let a caller say it in the reader's language:
    "no_outputs", "output_no_source" {output}, "flow_loop" {node}."""

    def __init__(self, message: str, code: str | None = None, **params):
        super().__init__(message)
        self.code = code
        self.params = params


# ── Filtri A MONTE sulle sorgenti ────────────────────────────────────────────
# Un nodo sorgente può portare `data.filters` = [{column, operator, value}, …]:
# condizioni in AND applicate SUBITO DOPO la lettura della sorgente, PRIMA di
# ogni altra operazione (campione di sviluppo compreso). A differenza del
# campione sono parte del flusso: valgono in sviluppo E in produzione, e
# arrivano anche nell'export dbt. Diventano normali operazioni `filter` IR
# (nessun marcatore: `strip_dev_sample_ops` non le tocca).
SOURCE_FILTER_OPERATORS = frozenset({
    "eq", "ne", "gt", "ge", "lt", "le", "in", "not_in",
    "between", "contains", "starts_with", "ends_with", "is_null", "is_not_null",
})
_NO_VALUE_OPERATORS = frozenset({"is_null", "is_not_null"})


def source_filter_operations(data: dict) -> list[dict]:
    """Operazioni IR `filter` delle condizioni a monte di un nodo sorgente.
    Le condizioni incomplete (senza colonna, operatore ignoto, valore mancante
    dove serve) vengono IGNORATE, come fa l'editor: una riga appena aggiunta
    non deve rompere il run."""
    filters = (data or {}).get("filters")
    if not isinstance(filters, list):
        return []
    ops: list[dict] = []
    for cond in filters:
        if not isinstance(cond, dict):
            continue
        column = cond.get("column")
        operator = cond.get("operator")
        if not isinstance(column, str) or not column or operator not in SOURCE_FILTER_OPERATORS:
            continue
        params: dict[str, Any] = {"column": column, "operator": operator}
        if operator not in _NO_VALUE_OPERATORS:
            value = cond.get("value")
            if value is None:
                continue
            if operator in ("in", "not_in") and (not isinstance(value, list) or not value):
                continue
            if operator == "between" and (not isinstance(value, list) or len(value) != 2):
                continue
            params["value"] = value
        ops.append({"type": "filter", "params": params})
    return ops


# ── Campione di SVILUPPO sulle sorgenti ──────────────────────────────────────
# Un nodo sorgente può portare `data.sample` = {mode: "first"|"random", rows|percent}:
# in modalità "development" (editor, run-now di prova) diventa un'operazione
# iniettata SUBITO DOPO la sorgente; in "production" (scheduler, run-now
# ?mode=production) NON viene mai iniettata: i flussi di produzione girano su
# TUTTI i record. L'operazione porta il marcatore `_dev_sample` così
# `strip_dev_sample_ops` può rimuoverla per difesa in profondità.
#
# Campione AUTOMATICO (PREVIEW__DEFAULT_SAMPLE_ROWS > 0): un nodo SENZA
# `data.sample` viene campionato alle prime N righe come se l'avesse chiesto;
# `{mode: "off"}` e' lo spegnimento ESPLICITO da parte dell'utente e vale
# «tutte le righe» anche con il default acceso. Speculare a `devSampleOperation`
# in frontend/composables/useFlowModel.ts.
DEV_SAMPLE_MARK = "_dev_sample"
MAX_SAMPLE_ROWS = 100_000_000


def default_sample_rows() -> int:
    """Il campione automatico configurato (0 = spento), letto a ogni chiamata
    perche' i test lo cambiano con monkeypatch sulle settings."""
    from app.core.config import get_settings

    return min(int(get_settings().preview.default_sample_rows or 0), MAX_SAMPLE_ROWS)


def dev_sample_operation(data: dict, default_rows: int = 0) -> Optional[dict]:
    """Operazione IR del campione di un nodo sorgente, o None se non campionato.
    `default_rows` > 0 = campione automatico per i nodi senza `sample`."""
    sample = (data or {}).get("sample")
    if not isinstance(sample, dict):
        if default_rows > 0:
            return {"type": "limit", "params": {"n": min(int(default_rows), MAX_SAMPLE_ROWS), DEV_SAMPLE_MARK: True}}
        return None
    mode = sample.get("mode")
    if mode == "off":  # spento dall'utente: vince sul default
        return None
    if mode == "first":
        try:
            n = int(sample.get("rows") or 0)
        except (TypeError, ValueError):
            return None
        if n <= 0:
            return None
        return {"type": "limit", "params": {"n": min(n, MAX_SAMPLE_ROWS), DEV_SAMPLE_MARK: True}}
    if mode == "random":
        try:
            pct = float(sample.get("percent") or 0)
        except (TypeError, ValueError):
            return None
        if not 0 < pct < 100:
            return None
        return {"type": "sample", "params": {"fraction": pct / 100, "seed": 42, DEV_SAMPLE_MARK: True}}
    return None


def strip_dev_sample_ops(operations: list[dict]) -> tuple[list[dict], int]:
    """Rimuove OVUNQUE (anche in right/driver/body annidati) le operazioni
    marcate `_dev_sample`. Torna (operazioni pulite, numero rimosse)."""
    removed = 0

    def clean_ops(ops: Any) -> list[dict]:
        nonlocal removed
        out: list[dict] = []
        for op in ops or []:
            if not isinstance(op, dict):
                out.append(op)
                continue
            params = op.get("params") or {}
            if isinstance(params, dict) and params.get(DEV_SAMPLE_MARK):
                removed += 1
                continue
            out.append({**op, "params": clean_params(params)})
        return out

    def clean_params(params: Any) -> Any:
        if not isinstance(params, dict):
            return params
        new = dict(params)
        for key in ("right", "driver"):
            ref = new.get(key)
            if isinstance(ref, dict) and "operations" in ref:
                new[key] = {**ref, "operations": clean_ops(ref.get("operations"))}
        if isinstance(new.get("body"), list):
            new["body"] = clean_ops(new["body"])
        return new

    return clean_ops(operations), removed


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
    def __init__(self, nodes: list[dict], edges: list[dict], resolve_ds: DsResolver, engine_mode: str = "production",
                 script_outputs: Optional[dict[str, tuple[str, str]]] = None):
        self.by_id = {n["id"]: n for n in nodes}
        # a Script or Container node is a boundary: the nodes after it start from what
        # it wrote, known once it has run in this orchestration (services/script_runs.py,
        # services/container_runs.py)
        self.script_outputs = script_outputs or {}
        self.edges = edges
        self.inc = _incoming(edges)
        self.resolve_ds = resolve_ds
        # SOLO "development" inietta i campioni; qualsiasi altro valore = produzione
        self.dev_sampling = engine_mode == "development"
        # il campione automatico si legge solo se serve: in produzione e' irrilevante
        self.default_sample_rows = default_sample_rows() if self.dev_sampling else 0
        # the calls in progress: the resolver is deterministic, so meeting the same call
        # again inside itself means the edges close a loop (e.g. a join's right input fed
        # by the join's own output) and the recursion would never end
        self._in_progress: set[tuple] = set()

    @contextmanager
    def _once(self, call: tuple, node_id: str):
        if call in self._in_progress:
            node = self.by_id.get(node_id) or {}
            label = str((node.get("data") or {}).get("label") or node_id)
            raise FlowResolveError(f"the flow has a loop through the node «{label}»: remove the edge that closes it",
                                   "flow_loop", node=label)
        self._in_progress.add(call)
        try:
            yield
        finally:
            self._in_progress.discard(call)

    def chain(self, target_id: str) -> tuple[Optional[tuple[str, str]], list[dict]]:
        """Catena che termina in target_id: (sorgente risolta, operazioni IR)."""
        source, ops, _ids = self.chain_with_ids(target_id)
        return source, ops

    def chain_with_ids(self, target_id: str) -> tuple[Optional[tuple[str, str]], list[dict], list[str]]:
        """Come `chain`, più l'id del nodo di OGNI operazione, allineato: serve a chi
        deve riconoscere i passi condivisi fra più uscite (l'export dbt). Le
        operazioni iniettate dal nodo sorgente (filtri a monte, campione) portano
        l'id del nodo sorgente con il suffisso `#head`."""
        with self._once(("chain", target_id), target_id):
            return self._chain_with_ids(target_id)

    def _chain_with_ids(self, target_id: str) -> tuple[Optional[tuple[str, str]], list[dict], list[str]]:
        target = self.by_id.get(target_id)
        if target and target.get("parentNode"):
            upstream_id = self.inc.get(target["parentNode"], {}).get("left")
            up_src, up_ops, up_ids = self.chain_with_ids(upstream_id) if upstream_id else (None, [], [])
            op = self._operation_for(target["parentNode"], until=target_id)
            return up_src, [*up_ops, op], [*up_ids, target["parentNode"]]

        op_ids: list[str] = []
        seen: set[str] = set()
        source: Optional[tuple[str, str]] = None
        source_id: Optional[str] = None
        head_ops: list[dict] = []  # operazioni del nodo sorgente: filtri a monte, poi campione
        cur: Optional[str] = target_id
        while cur and cur not in seen:
            seen.add(cur)
            node = self.by_id.get(cur)
            if not node:
                break
            if node.get("type") in STEP_NODE_TYPES:
                source = self.script_outputs.get(cur)
                source_id = cur
                break
            if node.get("type") == "source":
                source = _resolve_source(node, self.resolve_ds)
                source_id = cur
                data = node.get("data") or {}
                # filtri a monte: SEMPRE (sviluppo e produzione)
                head_ops = source_filter_operations(data)
                # campione: SOLO in sviluppo, e sui dati già filtrati
                if self.dev_sampling:
                    sample_op = dev_sample_operation(data, self.default_sample_rows)
                    if sample_op is not None:
                        head_ops.append(sample_op)
                break
            if node.get("type") in ("operation", "foreach"):
                op_ids.append(cur)
            cur = self.inc.get(cur, {}).get("left")

        op_ids.reverse()
        head_ids = [f"{source_id}#head"] * len(head_ops)
        return source, [*head_ops, *(self._operation_for(i) for i in op_ids)], [*head_ids, *op_ids]

    def _operation_for(self, node_id: str, until: Optional[str] = None) -> dict:
        with self._once(("operation", node_id, until), node_id):
            return self._build_operation(node_id, until)

    def _build_operation(self, node_id: str, until: Optional[str]) -> dict:
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
        return f"table {d.get('table') or '…'}"
    if dt == "s3":
        return f"S3 {d.get('s3Key') or '…'}"
    if dt == "sftp":
        return f"SFTP {d.get('sftpFileName') or '…'}"
    return f"datasource “{d.get('name') or '…'}”"


def _output_name(node: dict) -> str:
    """What the user calls an Output: the datasource, table or file it writes."""
    d = node.get("data") or {}
    dt = d.get("destType") or "datasource"
    nome = (d.get("table") if dt == "database" else d.get("s3Key") if dt == "s3"
            else d.get("sftpFileName") if dt == "sftp" else d.get("name"))
    return str(nome or "").strip() or str(node.get("id") or "")


def _no_source(node: dict) -> FlowResolveError:
    return FlowResolveError(
        f"output {_output_label(node)}: no source with data upstream "
        "(or the source datasource has no snapshot)",
        "output_no_source", output=_output_name(node),
    )


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
    if dest_type == "email":
        # I destinatari sono testo libero nella definizione del flusso: il
        # gateway li valida contro i domini ammessi della connessione prima di
        # costruire il payload (vedi routes/runs.py). Qui si trasportano soltanto.
        body["email"] = {
            "connection_id": d.get("connectionId"),
            "to": d.get("emailTo") or [],
            "cc": d.get("emailCc") or [],
            "subject": d.get("emailSubject") or "",
            "body": d.get("emailBody") or "",
            "body_is_html": bool(d.get("emailHtml")),
            "attachment_name": (d.get("attachmentName") or "").strip(),
            "attachment_format": d.get("attachmentFormat") or "xlsx",
            # scelta dell'utente: un invio fallito interrompe la sequenza invece
            # di essere solo raccolto fra gli errori (vedi services/orchestrator)
            "stop_on_failure": d.get("stopOnFailure") is not False,
        }
    elif dest_type == "database":
        body["destination"] = {
            "type": "database",
            "connection_id": d.get("connectionId"),
            "table": (d.get("table") or "").strip(),
            "mode": d.get("mode") or "append",
            "post_sql": d.get("postSql") or "",
        }
    elif dest_type == "sftp":
        # the run's number and the flow's name for the file-name template are
        # added by the gateway at launch (routes/runs.py): not part of the flow
        body["destination"] = {
            "type": "sftp",
            "connection_id": d.get("connectionId"),
            "folder": (d.get("sftpFolder") or "").strip(),
            "file_name": (d.get("sftpFileName") or "").strip(),
            "format": d.get("sftpFormat") or "csv",
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
            "overwrite": bool(d.get("overwrite")),
            "sort_keys": d.get("sortKeys") or [],
        }
        # copia su S3 esterno IN AGGIUNTA alla datasource: `destination` resta
        # libero, così il suo fallimento non è vincolante (vedi RunMirrorSpec)
        if d.get("mirrorEnabled") and d.get("mirrorConnectionId"):
            body["mirror"] = {
                "connection_id": d.get("mirrorConnectionId"),
                "bucket": (d.get("mirrorBucket") or "").strip(),
                "key": (d.get("mirrorKey") or "").strip(),
            }
    # the zone of the dates in the node's templates (file name, folder, key, subject):
    # only when set, so a node without one resolves exactly as before (UTC)
    tz = str(d.get("timezone") or "").strip()
    if tz:
        for part in ("destination", "email", "mirror"):
            if part in body:
                body[part]["timezone"] = tz
    return body


def resolve_output_request(
    definition: dict, node: dict, resolve_ds: DsResolver, default_bucket: str, engine_mode: str = "production",
    script_outputs: Optional[dict[str, tuple[str, str]]] = None,
) -> dict:
    """Risolve UN nodo Output → corpo-run. Solleva FlowResolveError se la sua
    catena dati non ha una sorgente risolvibile.

    `engine_mode`: default "production" (nessun campione, tutti i record); solo
    "development" inietta i campioni di sviluppo dei nodi sorgente. I filtri a
    monte (`data.filters`) delle sorgenti sono iniettati in OGNI modalità."""
    resolver = _Resolver(definition.get("nodes") or [], definition.get("edges") or [], resolve_ds, engine_mode,
                         script_outputs)
    source, operations = resolver.chain(node["id"])
    if source is None:
        raise _no_source(node)
    return _output_body(node, source, operations, default_bucket)


def script_input_edge(definition: dict, node_id: str) -> Optional[str]:
    """The node feeding a Script (or Container) node's input, None when it has no input."""
    return _incoming(definition.get("edges") or []).get(node_id, {}).get("left")


def script_has_output(definition: dict, node_id: str) -> bool:
    """A data edge leaves the Script (or Container) node: the nodes after it read what it writes."""
    return any(e.get("source") == node_id and not _is_seq_edge(e) for e in definition.get("edges") or [])


def resolve_script_input(
    definition: dict, node: dict, resolve_ds: DsResolver, default_bucket: str, engine_mode: str = "production",
    script_outputs: Optional[dict[str, tuple[str, str]]] = None,
) -> Optional[dict]:
    """The chain that ends at a Script (or Container) node's input, as the body of a run (bucket,
    input_key, operations); None when the node has no input."""
    upstream = script_input_edge(definition, node["id"])
    if not upstream:
        return None
    resolver = _Resolver(definition.get("nodes") or [], definition.get("edges") or [], resolve_ds, engine_mode,
                         script_outputs)
    source, operations = resolver.chain(upstream)
    if source is None:
        label = str((node.get("data") or {}).get("label") or node.get("id"))
        raise FlowResolveError(f"script {label}: no source with data upstream "
                               "(or the source datasource has no snapshot)", "script_no_source", script=label)
    bucket, key = source
    return {"bucket": bucket or default_bucket, "input_key": key, "operations": operations}


def resolve_output_chains(definition: dict, resolve_ds: DsResolver, default_bucket: str,
                          node_ids: Optional[Iterable[str]] = None) -> list[dict]:
    """Per OGNI nodo Output (modalità produzione, senza campioni): il corpo-run, il
    nodo stesso e gli id dei nodi delle sue operazioni — ciò che serve all'export
    dbt per riconoscere i passi condivisi. Un Output senza sorgente risolvibile
    solleva FlowResolveError, come `resolve_output_request`. With `node_ids`, only
    those Outputs: a broken draft elsewhere in the flow does not matter."""
    nodes = definition.get("nodes") or []
    outputs = [n for n in nodes if n.get("type") == "output"]
    if not outputs:
        raise FlowResolveError("the flow has no Output nodes: there is nothing to export", "no_outputs")
    if node_ids is not None:
        wanted = set(node_ids)
        outputs = [n for n in outputs if n.get("id") in wanted]
    resolver = _Resolver(nodes, definition.get("edges") or [], resolve_ds, "production")
    out = []
    for node in outputs:
        source, operations, ids = resolver.chain_with_ids(node["id"])
        if source is None:
            raise _no_source(node)
        out.append({"node": node, "body": _output_body(node, source, operations, default_bucket), "op_ids": ids})
    return out


def build_output_run_requests(
    definition: dict, resolve_ds: DsResolver, default_bucket: str, engine_mode: str = "production"
) -> list[dict]:
    """Un corpo-run per ogni nodo Output del flusso (senza ordine di sequenza)."""
    nodes = definition.get("nodes") or []
    outputs = [n for n in nodes if n.get("type") == "output"]
    if not outputs:
        raise FlowResolveError("the flow has no Output nodes: there is nothing to run", "no_outputs")
    return [resolve_output_request(definition, n, resolve_ds, default_bucket, engine_mode) for n in outputs]


# ordine di esecuzione dei nodi non collegati in sequenza (default sensato)
_ACTION_TYPES = ("refresh", "script", "container", "output", "runflow")
_DEFAULT_PRIORITY = {"refresh": 0, "script": 1, "container": 1, "output": 1, "runflow": 2}


def _scripts_upstream(node_id: str, nodes: dict[str, dict], edges: list[dict]) -> set[str]:
    """The Script and Container nodes a node reads data from, however far up: it runs
    after them."""
    into: dict[str, list[str]] = {}
    for e in edges:
        if not _is_seq_edge(e):
            into.setdefault(e.get("target"), []).append(e.get("source"))
    found: set[str] = set()
    stack, seen = [node_id], set()
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        parent = (nodes.get(cur) or {}).get("parentNode")
        for up in [*into.get(cur, []), *([parent] if parent else [])]:
            if (nodes.get(up) or {}).get("type") in STEP_NODE_TYPES:
                found.add(up)
            else:
                stack.append(up)
    return found


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
    pairs = [(e.get("source"), e.get("target")) for e in edges if e.get("targetHandle") == SEQ_TARGET_HANDLE]
    # a node that reads what a Script or a Container writes runs after it, edge or no edge
    all_nodes = {n["id"]: n for n in nodes if isinstance(n, dict) and "id" in n}
    for n in action:
        pairs += [(s, n["id"]) for s in _scripts_upstream(n["id"], all_nodes, edges)]
    for s, t in dict.fromkeys(pairs):
        if s in by_id and t in by_id and s != t:
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
