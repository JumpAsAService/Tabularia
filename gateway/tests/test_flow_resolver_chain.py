"""The flows' resolver is on the path of EVERY run. `chain` now goes through
`chain_with_ids` (which the dbt export needs): these tests pin that the chain produced
is IDENTICAL to the one of before, on a battery of graphs — linear, join and union with
right branches, upstream filters, development sample, foreach container with driver
and body, an output inside a container, a missing source, a cycle — and that the node
ids stay aligned with the operations."""
from typing import Optional

import pytest

from app.services import flow_resolver as fr
from app.services.flow_resolver import FlowResolveError, _Resolver, build_output_run_requests, resolve_output_chains

BUCKET = "data-prep"


def _old_chain(self, target_id: str) -> tuple[Optional[tuple[str, str]], list[dict]]:
    """`_Resolver.chain` as it was before 2026-10-08, copied word for word: it is the
    equivalence's oracle."""
    target = self.by_id.get(target_id)
    if target and target.get("parentNode"):
        upstream_id = self.inc.get(target["parentNode"], {}).get("left")
        up_src, up_ops = _old_chain(self, upstream_id) if upstream_id else (None, [])
        op = self._operation_for(target["parentNode"], until=target_id)
        return up_src, [*up_ops, op]

    op_ids: list[str] = []
    seen: set[str] = set()
    source: Optional[tuple[str, str]] = None
    head_ops: list[dict] = []
    cur: Optional[str] = target_id
    while cur and cur not in seen:
        seen.add(cur)
        node = self.by_id.get(cur)
        if not node:
            break
        if node.get("type") == "source":
            source = fr._resolve_source(node, self.resolve_ds)
            data = node.get("data") or {}
            head_ops = fr.source_filter_operations(data)
            if self.dev_sampling:
                sample_op = fr.dev_sample_operation(data, self.default_sample_rows)
                if sample_op is not None:
                    head_ops.append(sample_op)
            break
        if node.get("type") in ("operation", "foreach"):
            op_ids.append(cur)
        cur = self.inc.get(cur, {}).get("left")

    op_ids.reverse()
    return source, [*head_ops, *(self._operation_for(i) for i in op_ids)]


def _src(nid, key, **data):
    return {"id": nid, "type": "source", "data": {"bucket": BUCKET, "parquetKey": key, **data}}


def _op(nid, op_type, params=None, **extra):
    return {"id": nid, "type": "operation", "data": {"opType": op_type, "params": params or {}}, **extra}


def _out(nid, **extra):
    return {"id": nid, "type": "output", "data": {"destType": "datasource", "name": nid, "projectId": 1}, **extra}


def _e(a, b, handle=None):
    e = {"id": f"{a}-{b}", "source": a, "target": b}
    if handle:
        e["targetHandle"] = handle
    return e


def _resolve_ds(ds_id):
    return {7: (BUCKET, "datasets/7/v1.parquet")}.get(ds_id)


# ── the graphs ───────────────────────────────────────────────────────────────
LINEAR = {"nodes": [_src("s", "datasets/a.parquet"), _op("f", "filter", {"column": "x", "operator": "gt", "value": 1}),
                     _op("g", "group_by", {"by": ["k"], "aggregations": [{"column": "x", "func": "sum"}]}), _out("o")],
           "edges": [_e("s", "f"), _e("f", "g"), _e("g", "o")]}

JOIN_UNION = {"nodes": [_src("s", "datasets/a.parquet", filters=[{"column": "x", "operator": "gt", "value": 0}]),
                        _src("r", "datasets/b.parquet", filters=[{"column": "y", "operator": "eq", "value": "z"}]),
                        _src("u", "datasets/c.parquet"), _op("rs", "select", {"columns": ["k", "y"]}),
                        _op("j", "join", {"on": ["k"], "how": "left"}), _op("un", "union", {}), _out("o")],
              "edges": [_e("s", "j"), _e("r", "rs"), _e("rs", "j", "right"), _e("j", "un"), _e("u", "un", "right"), _e("un", "o")]}

CATALOG = {"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 7, "sample": {"mode": "first", "rows": 50}}},
                      _op("l", "limit", {"n": 5}), _out("o")],
            "edges": [_e("s", "l"), _e("l", "o")]}

FOREACH = {"nodes": [_src("s", "datasets/a.parquet"), _src("d", "datasets/driver.parquet"),
                     _op("pre", "select", {"columns": ["k", "x"]}),
                     {"id": "fe", "type": "foreach", "data": {"opType": "foreach", "params": {"column": "k"}}},
                     _op("b1", "filter", {"column": "x", "operator": "gt", "value": 0}, parentNode="fe"),
                     _op("b2", "sort", {"by": "x"}, parentNode="fe"),
                     _op("post", "limit", {"n": 10}), _out("fuori")],
           "edges": [_e("s", "pre"), _e("pre", "fe"), _e("d", "fe", "right"), _e("b1", "b2"),
                     _e("fe", "post"), _e("post", "fuori")]}

CYCLE = {"nodes": [_src("s", "datasets/a.parquet"), _op("a", "limit", {"n": 1}), _op("b", "limit", {"n": 2}), _out("o")],
         "edges": [_e("a", "b"), _e("b", "a"), _e("b", "o")]}

WITHOUT_SOURCE = {"nodes": [_op("a", "limit", {"n": 1}), _out("o")], "edges": [_e("a", "o")]}

GRAPHS = {"lineare": LINEAR, "join_union": JOIN_UNION, "catalogo": CATALOG, "foreach": FOREACH, "ciclo": CYCLE, "senza_sorgente": WITHOUT_SOURCE}


@pytest.mark.parametrize("mode", ["production", "development"])
@pytest.mark.parametrize("name", list(GRAPHS))
def test_the_chain_is_identical_to_the_one_of_before(name, mode, monkeypatch):
    monkeypatch.setattr(fr, "default_sample_rows", lambda: 1000)
    g = GRAPHS[name]
    for target in [n["id"] for n in g["nodes"]]:
        new_one = _Resolver(g["nodes"], g["edges"], _resolve_ds, mode).chain(target)
        old_one = _old_chain(_Resolver(g["nodes"], g["edges"], _resolve_ds, mode), target)
        assert new_one == old_one, (name, mode, target)


@pytest.mark.parametrize("mode", ["production", "development"])
@pytest.mark.parametrize("name", list(GRAPHS))
def test_the_ids_are_aligned_with_the_operations(name, mode, monkeypatch):
    monkeypatch.setattr(fr, "default_sample_rows", lambda: 1000)
    g = GRAPHS[name]
    by_id = {n["id"]: n for n in g["nodes"]}
    for target in [n["id"] for n in g["nodes"]]:
        src, ops, ids = _Resolver(g["nodes"], g["edges"], _resolve_ds, mode).chain_with_ids(target)
        assert len(ops) == len(ids), (name, target)
        for op, nid in zip(ops, ids):
            if nid.endswith("#head"):
                assert by_id[nid[:-5]]["type"] == "source" and op["type"] in ("filter", "limit", "sample")
            else:
                assert op["type"] == by_id[nid]["data"]["opType"]


def test_the_development_sample_and_upstream_filters_carry_the_sources_id(monkeypatch):
    monkeypatch.setattr(fr, "default_sample_rows", lambda: 1000)
    src, ops, ids = _Resolver(CATALOG["nodes"], CATALOG["edges"], _resolve_ds, "development").chain_with_ids("o")
    assert src == (BUCKET, "datasets/7/v1.parquet")
    assert ids == ["s#head", "l"] and ops[0]["type"] == "limit"
    src, ops, ids = _Resolver(JOIN_UNION["nodes"], JOIN_UNION["edges"], _resolve_ds, "production").chain_with_ids("o")
    assert ids == ["s#head", "j", "un"] and [o["type"] for o in ops] == ["filter", "join", "union"]
    right = ops[1]["params"]["right"]
    assert right["source"]["key"] == "datasets/b.parquet" and [o["type"] for o in right["operations"]] == ["filter", "select"]


def test_a_node_inside_a_container_carries_the_containers_id():
    # the preview of a node of the body: the container up to that node
    src, ops, ids = _Resolver(FOREACH["nodes"], FOREACH["edges"], _resolve_ds, "production").chain_with_ids("b2")
    assert ids == ["pre", "fe"] and [o["type"] for o in ops] == ["select", "foreach"]
    body = ops[1]["params"]["body"]
    assert [o["type"] for o in body] == ["filter", "sort"] and ops[1]["params"]["driver"]["source"]["key"] == "datasets/driver.parquet"
    src, ops, ids = _Resolver(FOREACH["nodes"], FOREACH["edges"], _resolve_ds, "production").chain_with_ids("b1")
    assert ids == ["pre", "fe"] and [o["type"] for o in ops[1]["params"]["body"]] == ["filter"]
    src, ops, ids = _Resolver(FOREACH["nodes"], FOREACH["edges"], _resolve_ds, "production").chain_with_ids("fuori")
    assert ids == ["pre", "fe", "post"]


def test_resolve_output_chains_gives_the_same_bodies_as_production_runs():
    for name in ("lineare", "join_union", "catalogo", "foreach"):
        g = GRAPHS[name]
        chains = resolve_output_chains(g, _resolve_ds, BUCKET)
        bodies = build_output_run_requests(g, _resolve_ds, BUCKET, "production")
        assert [c["body"] for c in chains] == bodies, name
        assert all(len(c["op_ids"]) == len(c["body"]["operations"]) for c in chains)


def test_resolve_output_chains_refuses_as_before():
    with pytest.raises(FlowResolveError, match="no source with data upstream"):
        resolve_output_chains(WITHOUT_SOURCE, _resolve_ds, BUCKET)
    with pytest.raises(FlowResolveError, match="no source with data upstream"):
        build_output_run_requests(WITHOUT_SOURCE, _resolve_ds, BUCKET)
    with pytest.raises(FlowResolveError, match="has no Output nodes"):
        resolve_output_chains({"nodes": [_src("s", "k")], "edges": []}, _resolve_ds, BUCKET)
