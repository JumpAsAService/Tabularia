"""Cross-flow lineage: builds the provenance/impact graph among flows, datasources,
connections and external destinations (DB tables, S3 objects), and the other objects
that read or write: saved scripts, SQL scripts, notebooks (the datasources they have
read, recorded by services/notebook_reads.py), flow groups (what they run, the images
of their Container cells, the emails of their Pushmail cells).

The graph is DERIVED on demand, without a dedicated table: half of the edges are
persisted in `datasources` already (flow_id = produced-by, connection_id =
ingested-by), the other half is read from the flows' `definition` (source node →
datasource read; output node → destination written; runflow → orchestration). It is
the same walk as the scheduler's, collected as a graph instead of run.

Everything is filtered by RBAC: only the objects in the projects the user can READ are
considered. A reference towards an object not readable/removed becomes a
`restricted` node (the edge does not dangle, but the content is not revealed).
"""
from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime
from typing import Callable, Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import (
    Connection, Datasource, Flow, Run, SavedScript, SavedScriptVersion, SqlScript, SqlScriptVersion, User,
)
from app.services import run_history
from app.services import permissions as perm_service

logger = logging.getLogger(__name__)

# edge kinds (direction = the way the data/control flows)
EDGE_READ = "read"          # datasource → flow, script, SQL script, notebook, email (consumption)
EDGE_PUBLISH = "publish"    # flow, script, SQL script → datasource (production)
EDGE_INGEST = "ingest"      # connection → datasource (DB snapshot)
EDGE_WRITE = "write"        # flow → external sink (DB table / S3); flow group → email
EDGE_REFRESH = "refresh"    # flow, flow group → datasource (refresh, control)
EDGE_ORCHESTRATE = "orchestrate"  # flow → flow ("Run flow" node); flow group → what it runs; → image


def _fid(i: int) -> str:
    return f"flow:{i}"


def _dsid(i: int) -> str:
    return f"ds:{i}"


def _sid(i: int) -> str:
    return f"script:{i}"


def _cid(i: int) -> str:
    return f"conn:{i}"


def _qid(i: int) -> str:
    return f"sql:{i}"


def _nid(i: int) -> str:
    return f"notebook:{i}"


def _gid(i: int) -> str:
    return f"group:{i}"


class _Graph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self._edge_seen: set[tuple[str, str, str]] = set()

    def add_node(self, node_id: str, **attrs) -> None:
        # do not overwrite a "real" node with a restricted placeholder
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
    """A datasource referenced but outside the readable projects (or removed). The reason
    (removed vs not accessible) is resolved at the end of the build with one query."""
    unknown.add(ds_id)
    nid = _dsid(ds_id)
    g.add_node(nid, type="datasource", label=f"Datasource #{ds_id}", restricted=True, kind=None,
               meta={"reason": "unknown"})
    return nid


def _restricted_flow(g: _Graph, flow_id: int, unknown: set[int]) -> str:
    unknown.add(flow_id)
    nid = _fid(flow_id)
    g.add_node(nid, type="flow", label=f"Flow #{flow_id}", restricted=True, meta={"reason": "unknown"})
    return nid


def _add_image(g: _Graph, data: dict, runner: str) -> None:
    """The image a Container node or cell runs (registry + repository, whatever the
    tag), and the edge from what runs it."""
    repository = str(data.get("repository") or "").strip()
    if not repository:
        return
    node = f"image:{data.get('registryId')}|{repository}"
    g.add_node(node, type="container", label=repository,
               meta={"registry_id": data.get("registryId"), "tag": str(data.get("tag") or "").strip() or "latest"})
    g.add_edge(runner, node, EDGE_ORCHESTRATE)


def current_contents(session: Session, objects: list, model, version_model,
                      empty: Callable[[], dict]) -> dict[int, dict]:
    """id → the content of the current version of saved scripts or SQL scripts, in ONE
    query (`content_of` asks one per object, and the graph reads every script at every
    look). As `content_of`: none or unreadable → `empty()`."""
    out = {o.id: empty() for o in objects}
    if not out:
        return out
    rows = session.exec(
        select(version_model.script_id, version_model.content)
        .join(model, (model.id == version_model.script_id) & (model.current_version == version_model.version))
        .where(model.id.in_(list(out)))  # type: ignore[attr-defined]
    ).all()
    for script_id, content in rows:
        try:
            out[script_id] = {**empty(), **json.loads(content)}
        except ValueError:
            pass
    return out


def build_full_graph(session: Session, user: User) -> _Graph:
    """The whole graph over the objects in the projects the user can read."""
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

    # ── base nodes ───────────────────────────────────────────────────────────
    for c in connections:
        g.add_node(_cid(c.id), type="connection", label=c.name, project_id=c.project_id,
                   meta={"db_type": c.db_type, "database": c.database})
    for d in datasources:
        g.add_node(_dsid(d.id), type="datasource", label=d.name, project_id=d.project_id,
                   kind=d.kind, meta={"rows": d.rows,
                                      "refreshed_at": d.refreshed_at.isoformat() if d.refreshed_at else None})
    # the last run per flow (for health/impact): the newest of all + the last SUCCESS
    # (the basis for staleness). Two aggregate queries on the index
    # (flow_id, started_at): loading every run as a row took seconds and hundreds
    # of megabytes once a year of history had piled up.
    last_run = run_history.latest_flow_runs(session, flow_ids)
    last_success_at: dict[int, datetime] = {}
    if flow_ids:
        flow_runs = (Run.flow_id.in_(flow_ids), Run.kind == "flow")
        for flow_id, at in session.exec(
            select(Run.flow_id, func.max(func.coalesce(Run.finished_at, Run.started_at)))
            .where(*flow_runs, Run.status == "SUCCESS").group_by(Run.flow_id)
        ).all():
            last_success_at[flow_id] = at

    for f in flows:
        lr = last_run.get(f.id)
        g.add_node(_fid(f.id), type="flow", label=f.name, project_id=f.project_id,
                   meta={"engine": f.engine, "scheduled": bool(f.run_schedule),
                         "last_run_at": (lr[1].isoformat() if lr else None),
                         "last_run_status": (lr[0] if lr else None),
                         "never_run": lr is None})

    # ── edges persisted from the datasources table ───────────────────────────
    for d in datasources:
        if d.flow_id and d.flow_id in flow_ids:
            g.add_edge(_fid(d.flow_id), _dsid(d.id), EDGE_PUBLISH)   # flow → produces datasource
        if d.connection_id and d.connection_id in conn_by_id:
            g.add_edge(_cid(d.connection_id), _dsid(d.id), EDGE_INGEST)  # connection → snapshot

    # ── edges deduced from the flows' definitions ────────────────────────────
    unknown_ds: set[int] = set()    # datasources referenced but not readable/removed
    unknown_flow: set[int] = set()  # flows referenced but not readable/removed
    for f in flows:
        try:
            definition = json.loads(f.definition or "{}")
        except json.JSONDecodeError:
            logger.warning("lineage: invalid definition for flow %s", f.id)
            continue
        for node in definition.get("nodes") or []:
            t = node.get("type")
            d = node.get("data") or {}

            if t == "source":
                ds_id = d.get("datasourceId")
                if ds_id is None:
                    continue
                target = _dsid(ds_id) if ds_id in ds_by_id else _restricted_ds(g, ds_id, unknown_ds)
                g.add_edge(target, _fid(f.id), EDGE_READ)            # datasource → flow

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
                    table = (d.get("table") or "").strip() or "(table?)"
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
                    key = (d.get("s3Key") or "").strip() or "(key?)"
                    sink = f"s3sink:{bucket}|{key}"
                    g.add_node(sink, type="s3_sink", label=key or bucket,
                               meta={"bucket": bucket, "key": key,
                                     "format": d.get("s3Format") or "parquet",
                                     "partitions": len(d.get("partitionBy") or [])})
                    g.add_edge(_fid(f.id), sink, EDGE_WRITE)
                elif dest == "sftp":
                    conn_id = d.get("connectionId")
                    folder = (d.get("sftpFolder") or "").strip()
                    name = (d.get("sftpFileName") or "").strip() or "(file?)"
                    sink = f"sftpsink:{conn_id}|{folder}|{name}"
                    conn = conn_by_id.get(conn_id)
                    g.add_node(sink, type="sftp_sink", label=name, restricted=conn is None,
                               meta={"connection_id": conn_id,
                                     "connection_name": conn.name if conn else None,
                                     "host": conn.host if conn else None,
                                     "folder": folder,
                                     "format": d.get("sftpFormat") or "csv"})
                    g.add_edge(_fid(f.id), sink, EDGE_WRITE)
                # dest == "datasource": the production is covered already by the persisted PUBLISH
                # edge (datasources.flow_id) when the flow has run

    # ── saved scripts (docs/specs/saved-scripts.md): the ones this person sees, the
    # datasources they declare as inputs, the one they publish, the flows that run
    # them by reference ─────────────────────────────────────────────────────────
    from app.services import saved_scripts
    from app.services.script_builds import disabled_reason

    # the datasource with a name in a folder (what a script publishes or writes): one lookup
    ds_at: dict[tuple[int, str], Datasource] = {}
    for d in datasources:
        ds_at.setdefault((d.project_id, d.name), d)
    stmt = saved_scripts.visible_query(session, user) if disabled_reason() is None else None
    scripts = list(session.exec(stmt).all()) if stmt is not None else []
    script_ids = {x.id for x in scripts}
    contents = current_contents(session, scripts, SavedScript, SavedScriptVersion, saved_scripts.empty_content)
    for x in scripts:
        g.add_node(_sid(x.id), type="script", label=x.name, project_id=x.project_id,
                   meta={"scheduled": bool(x.run_schedule), "version": x.current_version})
        content = contents[x.id]
        for item in content.get("inputs") or []:
            ds_id = item.get("datasource_id")
            if not isinstance(ds_id, int):
                continue
            source = _dsid(ds_id) if ds_id in ds_by_id else _restricted_ds(g, ds_id, unknown_ds)
            g.add_edge(source, _sid(x.id), EDGE_READ)                # datasource → script
        out = content.get("output") or {}
        if out.get("publish"):
            published = ds_at.get((out.get("project_id"), out.get("name")))
            if published is not None:
                g.add_edge(_sid(x.id), _dsid(published.id), EDGE_PUBLISH)  # script → datasource
    if script_ids:
        from app.services.script_nodes import saved_script_id, script_nodes

        for f in flows:
            if '"scriptId"' not in (f.definition or ""):
                continue
            for node in script_nodes(f.definition):
                sid = saved_script_id(node)
                if sid in script_ids:
                    g.add_edge(_fid(f.id), _sid(sid), EDGE_ORCHESTRATE)  # flow → script it runs

    # ── SQL scripts (docs/specs/sql-scripts.md): the datasources their SQL names (or
    # the connection they run on), the datasource they write ──────────────────────
    from app.services import sql_scripts

    stmt = sql_scripts.visible_query(session, user)
    queries = list(session.exec(stmt).all()) if stmt is not None else []
    sql_ids = {x.id for x in queries}
    contents = current_contents(session, queries, SqlScript, SqlScriptVersion, sql_scripts.empty_content)
    # what the SQL names is looked up among the readable datasources, by name and folder:
    # both built once, not once per script
    by_name = sql_scripts.by_name(datasources) if queries else {}
    folders = sql_scripts.folder_names(session) if queries else {}
    for x in queries:
        content = contents[x.id]
        g.add_node(_qid(x.id), type="sql_script", label=x.name, project_id=x.project_id,
                   meta={"scheduled": bool(x.run_schedule), "version": x.current_version,
                         "engine": content.get("engine")})
        if content.get("connection_id") is not None:
            if content["connection_id"] in conn_by_id:
                g.add_edge(_cid(content["connection_id"]), _qid(x.id), EDGE_READ)  # connection → script
        else:
            for ds in sql_scripts.named_datasources(session, content.get("sql") or "", content.get("engine"), by_name, folders):
                g.add_edge(_dsid(ds.id), _qid(x.id), EDGE_READ)                     # datasource → script
        out = content.get("output")
        if isinstance(out, dict):
            written = ds_at.get((out.get("project_id"), out.get("name")))
            if written is not None:
                g.add_edge(_qid(x.id), _dsid(written.id), EDGE_PUBLISH)          # script → datasource

    # ── notebooks (docs/specs/notebooks.md): the datasources they have read ──────
    from app.services import notebook_reads, notebooks

    stmt = notebooks.visible_query(session, user)
    visible_notebooks = list(session.exec(stmt).all()) if stmt is not None else []
    reads = notebook_reads.read_by(session, [n.id for n in visible_notebooks])
    for nb in visible_notebooks:
        g.add_node(_nid(nb.id), type="notebook", label=nb.name, project_id=nb.project_id,
                   meta={"version": nb.current_version})
        for ds_id in reads.get(nb.id, []):
            source = _dsid(ds_id) if ds_id in ds_by_id else _restricted_ds(g, ds_id, unknown_ds)
            g.add_edge(source, _nid(nb.id), EDGE_READ)                               # datasource → notebook

    # ── flow groups: what each runs (flows, refreshes, scripts, SQL scripts), the
    # images of their Container cells, the emails of their Pushmail cells ──────────
    from app.models import FlowGroup
    from app.services import container_nodes
    from app.services.flow_groups import definition_of

    for grp in session.exec(select(FlowGroup).where(FlowGroup.project_id.in_(readable))).all():
        cells = [c for row in definition_of(grp).steps for c in row if c is not None]
        g.add_node(_gid(grp.id), type="flow_group", label=grp.name, project_id=grp.project_id,
                   meta={"scheduled": bool(grp.run_schedule), "cells": len(cells)})
        for c in cells:
            if c.kind == "flow":
                target = _fid(c.id) if c.id in flow_ids else _restricted_flow(g, c.id, unknown_flow)
                g.add_edge(_gid(grp.id), target, EDGE_ORCHESTRATE)
            elif c.kind == "refresh":
                target = _dsid(c.id) if c.id in ds_by_id else _restricted_ds(g, c.id, unknown_ds)
                g.add_edge(_gid(grp.id), target, EDGE_REFRESH)
            elif c.kind == "script" and c.id in script_ids:
                g.add_edge(_gid(grp.id), _sid(c.id), EDGE_ORCHESTRATE)
            elif c.kind == "sql" and c.id in sql_ids:
                g.add_edge(_gid(grp.id), _qid(c.id), EDGE_ORCHESTRATE)
            elif c.kind == "container":
                _add_image(g, c.container or {}, _gid(grp.id))
            elif c.kind == "pushmail" and c.pushmail is not None:
                mail = f"mail:{grp.id}|{c.id}"
                conn = conn_by_id.get(c.pushmail.connectionId)
                g.add_node(mail, type="email_sink", label=c.pushmail.label.strip() or "Pushmail",
                           meta={"connection_id": c.pushmail.connectionId,
                                 "connection_name": conn.name if conn else None,
                                 "recipients": len([a for a in c.pushmail.to + c.pushmail.cc if a.strip()]),
                                 "attachments": len(c.pushmail.attachments)})
                g.add_edge(_gid(grp.id), mail, EDGE_WRITE)                            # group → email
                for a in c.pushmail.attachments:
                    if a.datasourceId is None:
                        continue
                    source = _dsid(a.datasourceId) if a.datasourceId in ds_by_id else _restricted_ds(g, a.datasourceId, unknown_ds)
                    g.add_edge(source, mail, EDGE_READ)                                # datasource → email

    # the images the flows' Container nodes run
    for f in flows:
        if '"container"' not in (f.definition or ""):
            continue
        for node in container_nodes.container_nodes(f.definition):
            _add_image(g, node.get("data") or {}, _fid(f.id))

    # ── the reason of the unresolved references: removed vs not accessible ─────────
    # one query per kind checks the EXISTENCE (not the content: nothing of an object in a
    # project that is not readable is revealed beyond "it exists").
    if unknown_ds:
        existing = set(session.exec(select(Datasource.id).where(Datasource.id.in_(unknown_ds))).all())
        for i in unknown_ds:
            g.nodes[_dsid(i)]["meta"]["reason"] = "inaccessible" if i in existing else "removed"
    if unknown_flow:
        existing = set(session.exec(select(Flow.id).where(Flow.id.in_(unknown_flow))).all())
        for i in unknown_flow:
            g.nodes[_fid(i)]["meta"]["reason"] = "inaccessible" if i in existing else "removed"

    # ── DIRECT staleness: a flow reads a DB datasource refreshed AFTER its last
    # successful run (its outputs are on overtaken data) ─────────────────────────
    for e in g.edges:
        if e["kind"] != EDGE_READ or not e["source"].startswith("ds:") or not e["target"].startswith("flow:"):
            continue
        ds = ds_by_id.get(int(e["source"].split(":")[1]))
        if not ds or not ds.refreshed_at:
            continue
        succ = last_success_at.get(int(e["target"].split(":")[1]))
        if succ is not None and ds.refreshed_at > succ:
            node = g.nodes.get(e["target"])
            if node:
                node["meta"]["stale"] = True
                node["meta"]["stale_reason"] = "source"  # direct cause

    # ── CASCADING staleness: whoever is downstream of a stale node (along the flow
    # of the DATA: read/publish/write) inherits the suspicion ─────────────────────
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
                node["meta"]["stale_reason"] = "upstream"  # inherited
            if nxt not in visited:
                visited.add(nxt)
                queue.append(nxt)

    return g


def subgraph(g: _Graph, center: str, direction: str = "both", depth: int = 3) -> _Graph:
    """Extracts the subgraph reachable from the `center` node within `depth` hops,
    following the edges upstream, downstream or both."""
    out = _Graph()
    if center not in g.nodes:
        return out

    # adjacencies by direction
    downstream: dict[str, list[tuple[str, dict]]] = {}
    upstream: dict[str, list[tuple[str, dict]]] = {}
    for e in g.edges:
        downstream.setdefault(e["source"], []).append((e["target"], e))
        upstream.setdefault(e["target"], []).append((e["source"], e))

    keep_nodes: set[str] = {center}
    keep_edges: list[dict] = []
    # BFS: each item is (node, hops_left)
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
