"""Server-side resolution of a flow's definition into run requests.

It is the Python twin of the frontend's `resolveChain`/`executeOutputRuns`: the
SCHEDULER needs it, which runs without a browser. For every Output node it makes a
run's body (bucket, input_key, operations, publish|destination), walking up the
chain and injecting the right branches of join/union and the driver/body of the
foreach.

The crucial difference from the frontend (the scheduling's Option A): datasource
sources do NOT use the key frozen in the definition, they are RESOLVED AGAIN to the
current snapshot through `resolve_ds` (a refresh deletes the old blob: a frozen key
would give 404). So the scheduled run always uses the current data and follows the
changes to the flow.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Iterable, Optional

# (bucket, key) of a datasource's current snapshot, or None when missing
DsResolver = Callable[[int], Optional[tuple[str, str]]]
# the nodes that run outside Tabularia's engine and hand their output to the nodes after them
STEP_NODE_TYPES = ("script", "container")


class FlowResolveError(ValueError):
    """The definition cannot run (output disconnected, source missing…).
    `code` and `params` (optional) let a caller say it in the reader's language:
    "no_outputs", "output_no_source" {output}, "flow_loop" {node}."""

    def __init__(self, message: str, code: str | None = None, **params):
        super().__init__(message)
        self.code = code
        self.params = params


# ── UPSTREAM filters on the sources ──────────────────────────────────────────
# A source node may carry `data.filters` = [{column, operator, value}, …]: conditions
# in AND applied RIGHT AFTER reading the source, BEFORE any other operation
# (development sample included). Unlike the sample they are part of the flow: they
# hold in development AND in production, and reach the dbt export too. They become
# normal IR `filter` operations (no marker: `strip_dev_sample_ops` leaves them).
SOURCE_FILTER_OPERATORS = frozenset({
    "eq", "ne", "gt", "ge", "lt", "le", "in", "not_in",
    "between", "contains", "starts_with", "ends_with", "is_null", "is_not_null",
})
_NO_VALUE_OPERATORS = frozenset({"is_null", "is_not_null"})


def source_filter_operations(data: dict) -> list[dict]:
    """The IR `filter` operations of a source node's upstream conditions. The incomplete
    conditions (without a column, an unknown operator, a value missing where needed) are
    IGNORED, as the editor does: a row just added must not break the run."""
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


# ── DEVELOPMENT sample on the sources ────────────────────────────────────────
# A source node may carry `data.sample` = {mode: "first"|"random", rows|percent}: in
# "development" mode (editor, trial run-now) it becomes an operation injected RIGHT
# AFTER the source; in "production" (scheduler, run-now ?mode=production) it is never
# injected: the production flows run on ALL the records. The operation carries the
# `_dev_sample` marker so `strip_dev_sample_ops` can remove it for defence in depth.
#
# AUTOMATIC sample (PREVIEW__DEFAULT_SAMPLE_ROWS > 0): a node WITHOUT `data.sample` is
# sampled to the first N rows as if it had asked; `{mode: "off"}` is the user's
# EXPLICIT turning off and means «all rows» even with the default on. The mirror of
# `devSampleOperation` in frontend/composables/useFlowModel.ts.
DEV_SAMPLE_MARK = "_dev_sample"
MAX_SAMPLE_ROWS = 100_000_000


def default_sample_rows() -> int:
    """The configured automatic sample (0 = off), read at every call because the tests
    change it with monkeypatch on the settings."""
    from app.core.config import get_settings

    return min(int(get_settings().preview.default_sample_rows or 0), MAX_SAMPLE_ROWS)


def dev_sample_operation(data: dict, default_rows: int = 0) -> Optional[dict]:
    """The IR operation of a source node's sample, or None when not sampled.
    `default_rows` > 0 = automatic sample for the nodes without `sample`."""
    sample = (data or {}).get("sample")
    if not isinstance(sample, dict):
        if default_rows > 0:
            return {"type": "limit", "params": {"n": min(int(default_rows), MAX_SAMPLE_ROWS), DEV_SAMPLE_MARK: True}}
        return None
    mode = sample.get("mode")
    if mode == "off":  # turned off by the user: wins over the default
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
    """Removes EVERYWHERE (nested right/driver/body too) the operations marked
    `_dev_sample`. Returns (clean operations, number removed)."""
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


# handles of the SEQUENCE edges (orchestration), NOT to be confused with the data
SEQ_TARGET_HANDLE = "seq-in"
SEQ_SOURCE_HANDLE = "seq-out"


def _is_seq_edge(e: dict) -> bool:
    return e.get("targetHandle") == SEQ_TARGET_HANDLE or e.get("sourceHandle") == SEQ_SOURCE_HANDLE


def _incoming(edges: list[dict]) -> dict[str, dict[str, str]]:
    """target -> {left, right} by the targetHandle of the DATA edges (the sequence edges,
    vertical, are not part of the data pipeline)."""
    m: dict[str, dict[str, str]] = {}
    for e in edges:
        if _is_seq_edge(e):
            continue
        handle = e.get("targetHandle") or "left"
        entry = m.setdefault(e.get("target"), {})
        entry["right" if handle == "right" else "left"] = e.get("source")
    return m


def _resolve_source(node: dict, resolve_ds: DsResolver) -> Optional[tuple[str, str]]:
    """Source node → (bucket, key). A catalog datasource → the CURRENT snapshot (resolved
    again by id); upload → the file's key (permanent)."""
    data = node.get("data") or {}
    ds_id = data.get("datasourceId")
    if ds_id is not None:
        resolved = resolve_ds(ds_id)
        if resolved and resolved[1]:
            return resolved
        return None  # datasource gone or without a snapshot → the source cannot be resolved
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
        # ONLY "development" injects the samples; any other value = production
        self.dev_sampling = engine_mode == "development"
        # the automatic sample is read only when needed: in production it does not matter
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
        """The chain ending in target_id: (resolved source, IR operations)."""
        source, ops, _ids = self.chain_with_ids(target_id)
        return source, ops

    def chain_with_ids(self, target_id: str) -> tuple[Optional[tuple[str, str]], list[dict], list[str]]:
        """As `chain`, plus the node id of EVERY operation, aligned: whoever must recognize the
        steps shared among several outputs needs it (the dbt export). The operations the
        source node injects (upstream filters, sample) carry the source node's id with the
        `#head` suffix."""
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
        head_ops: list[dict] = []  # the source node's operations: upstream filters, then the sample
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
                # upstream filters: ALWAYS (development and production)
                head_ops = source_filter_operations(data)
                # sample: ONLY in development, and on the data filtered already
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
        # an entry child: without a left input INSIDE the container
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
    name = (d.get("table") if dt == "database" else d.get("s3Key") if dt == "s3"
            else d.get("sftpFileName") if dt == "sftp" else d.get("name"))
    return str(name or "").strip() or str(node.get("id") or "")


def _no_source(node: dict) -> FlowResolveError:
    return FlowResolveError(
        f"output {_output_label(node)}: no source with data upstream "
        "(or the source datasource has no snapshot)",
        "output_no_source", output=_output_name(node),
    )


def _output_body(node: dict, source: tuple[str, str], operations: list[dict], default_bucket: str) -> dict:
    """The run body (RunCreate's shape) of ONE Output node resolved already."""
    bucket, key = source
    body: dict[str, Any] = {
        "bucket": bucket or default_bucket,
        "input_key": key,
        "operations": operations,
    }
    d = node.get("data") or {}
    dest_type = d.get("destType") or "datasource"
    if dest_type == "email":
        # The recipients are free text in the flow's definition: the gateway validates them
        # against the connection's allowed domains before building the payload (see
        # routes/runs.py). Here they are only carried.
        body["email"] = {
            "connection_id": d.get("connectionId"),
            "to": d.get("emailTo") or [],
            "cc": d.get("emailCc") or [],
            "subject": d.get("emailSubject") or "",
            "body": d.get("emailBody") or "",
            "body_is_html": bool(d.get("emailHtml")),
            "attachment_name": (d.get("attachmentName") or "").strip(),
            "attachment_format": d.get("attachmentFormat") or "xlsx",
            # the user's choice: a failed send stops the sequence instead of being only collected
            # among the errors (see services/orchestrator)
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
        # a copy to an external S3 AS WELL AS the datasource: `destination` stays free, so its
        # failure does not bind (see RunMirrorSpec)
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
    """Resolves ONE Output node → run body. Raises FlowResolveError when its data chain has
    no resolvable source.

    `engine_mode`: default "production" (no sample, every record); only "development"
    injects the source nodes' development samples. The sources' upstream filters
    (`data.filters`) are injected in EVERY mode."""
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
    """For EVERY Output node (production mode, without samples): the run body, the node
    itself and the node ids of its operations — what the dbt export needs to recognize
    the shared steps. An Output without a resolvable source raises FlowResolveError, as
    `resolve_output_request` does. With `node_ids`, only
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
    """One run body per Output node of the flow (without sequence order)."""
    nodes = definition.get("nodes") or []
    outputs = [n for n in nodes if n.get("type") == "output"]
    if not outputs:
        raise FlowResolveError("the flow has no Output nodes: there is nothing to run", "no_outputs")
    return [resolve_output_request(definition, n, resolve_ds, default_bucket, engine_mode) for n in outputs]


# the run order of the nodes not connected in sequence (a sensible default)
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
    """Sorts the 'action nodes' (refresh/output/runflow) by the SEQUENCE edges (topological
    sort, Kahn). The unconnected nodes come out in the default order (refresh → output →
    runflow, then declaration order), so flows without sequence edges keep the sensible
    behaviour of before. Cycles: the nodes involved end up at the back in the default
    order (best effort).
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
    # any nodes in a cycle: appended at the back, default order
    for nid in sorted((nid for nid in by_id if nid not in seen), key=rank):
        out.append(by_id[nid])
    return out
