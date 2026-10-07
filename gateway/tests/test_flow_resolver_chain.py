"""Il risolutore dei flussi è sul percorso di OGNI run. `chain` ora passa da
`chain_with_ids` (che serve all'export dbt): questi test fissano che la catena
prodotta è IDENTICA a quella di prima, su una batteria di grafi — lineare, join e
union con rami destri, filtri a monte, campione di sviluppo, contenitore foreach
con driver e corpo, uscita dentro un contenitore, sorgente mancante, ciclo — e che
gli id dei nodi restano allineati alle operazioni."""
from typing import Optional

import pytest

from app.services import flow_resolver as fr
from app.services.flow_resolver import FlowResolveError, _Resolver, build_output_run_requests, resolve_output_chains

BUCKET = "data-prep"


def _vecchia_chain(self, target_id: str) -> tuple[Optional[tuple[str, str]], list[dict]]:
    """`_Resolver.chain` com'era prima del 2026-10-08, copiata parola per parola:
    è l'oracolo dell'equivalenza."""
    target = self.by_id.get(target_id)
    if target and target.get("parentNode"):
        upstream_id = self.inc.get(target["parentNode"], {}).get("left")
        up_src, up_ops = _vecchia_chain(self, upstream_id) if upstream_id else (None, [])
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


# ── i grafi ──────────────────────────────────────────────────────────────────
LINEARE = {"nodes": [_src("s", "datasets/a.parquet"), _op("f", "filter", {"column": "x", "operator": "gt", "value": 1}),
                     _op("g", "group_by", {"by": ["k"], "aggregations": [{"column": "x", "func": "sum"}]}), _out("o")],
           "edges": [_e("s", "f"), _e("f", "g"), _e("g", "o")]}

JOIN_UNION = {"nodes": [_src("s", "datasets/a.parquet", filters=[{"column": "x", "operator": "gt", "value": 0}]),
                        _src("r", "datasets/b.parquet", filters=[{"column": "y", "operator": "eq", "value": "z"}]),
                        _src("u", "datasets/c.parquet"), _op("rs", "select", {"columns": ["k", "y"]}),
                        _op("j", "join", {"on": ["k"], "how": "left"}), _op("un", "union", {}), _out("o")],
              "edges": [_e("s", "j"), _e("r", "rs"), _e("rs", "j", "right"), _e("j", "un"), _e("u", "un", "right"), _e("un", "o")]}

CATALOGO = {"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 7, "sample": {"mode": "first", "rows": 50}}},
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

CICLO = {"nodes": [_src("s", "datasets/a.parquet"), _op("a", "limit", {"n": 1}), _op("b", "limit", {"n": 2}), _out("o")],
         "edges": [_e("a", "b"), _e("b", "a"), _e("b", "o")]}

SENZA_SORGENTE = {"nodes": [_op("a", "limit", {"n": 1}), _out("o")], "edges": [_e("a", "o")]}

GRAFI = {"lineare": LINEARE, "join_union": JOIN_UNION, "catalogo": CATALOGO, "foreach": FOREACH, "ciclo": CICLO, "senza_sorgente": SENZA_SORGENTE}


@pytest.mark.parametrize("modo", ["production", "development"])
@pytest.mark.parametrize("nome", list(GRAFI))
def test_la_catena_e_identica_a_quella_di_prima(nome, modo, monkeypatch):
    monkeypatch.setattr(fr, "default_sample_rows", lambda: 1000)
    g = GRAFI[nome]
    for target in [n["id"] for n in g["nodes"]]:
        nuova = _Resolver(g["nodes"], g["edges"], _resolve_ds, modo).chain(target)
        vecchia = _vecchia_chain(_Resolver(g["nodes"], g["edges"], _resolve_ds, modo), target)
        assert nuova == vecchia, (nome, modo, target)


@pytest.mark.parametrize("modo", ["production", "development"])
@pytest.mark.parametrize("nome", list(GRAFI))
def test_gli_id_sono_allineati_alle_operazioni(nome, modo, monkeypatch):
    monkeypatch.setattr(fr, "default_sample_rows", lambda: 1000)
    g = GRAFI[nome]
    by_id = {n["id"]: n for n in g["nodes"]}
    for target in [n["id"] for n in g["nodes"]]:
        src, ops, ids = _Resolver(g["nodes"], g["edges"], _resolve_ds, modo).chain_with_ids(target)
        assert len(ops) == len(ids), (nome, target)
        for op, nid in zip(ops, ids):
            if nid.endswith("#head"):
                assert by_id[nid[:-5]]["type"] == "source" and op["type"] in ("filter", "limit", "sample")
            else:
                assert op["type"] == by_id[nid]["data"]["opType"]


def test_il_campione_di_sviluppo_e_i_filtri_a_monte_portano_l_id_della_sorgente(monkeypatch):
    monkeypatch.setattr(fr, "default_sample_rows", lambda: 1000)
    src, ops, ids = _Resolver(CATALOGO["nodes"], CATALOGO["edges"], _resolve_ds, "development").chain_with_ids("o")
    assert src == (BUCKET, "datasets/7/v1.parquet")
    assert ids == ["s#head", "l"] and ops[0]["type"] == "limit"
    src, ops, ids = _Resolver(JOIN_UNION["nodes"], JOIN_UNION["edges"], _resolve_ds, "production").chain_with_ids("o")
    assert ids == ["s#head", "j", "un"] and [o["type"] for o in ops] == ["filter", "join", "union"]
    destra = ops[1]["params"]["right"]
    assert destra["source"]["key"] == "datasets/b.parquet" and [o["type"] for o in destra["operations"]] == ["filter", "select"]


def test_un_nodo_dentro_un_contenitore_porta_l_id_del_contenitore():
    # la preview di un nodo del corpo: il contenitore fino a quel nodo
    src, ops, ids = _Resolver(FOREACH["nodes"], FOREACH["edges"], _resolve_ds, "production").chain_with_ids("b2")
    assert ids == ["pre", "fe"] and [o["type"] for o in ops] == ["select", "foreach"]
    corpo = ops[1]["params"]["body"]
    assert [o["type"] for o in corpo] == ["filter", "sort"] and ops[1]["params"]["driver"]["source"]["key"] == "datasets/driver.parquet"
    src, ops, ids = _Resolver(FOREACH["nodes"], FOREACH["edges"], _resolve_ds, "production").chain_with_ids("b1")
    assert ids == ["pre", "fe"] and [o["type"] for o in ops[1]["params"]["body"]] == ["filter"]
    src, ops, ids = _Resolver(FOREACH["nodes"], FOREACH["edges"], _resolve_ds, "production").chain_with_ids("fuori")
    assert ids == ["pre", "fe", "post"]


def test_resolve_output_chains_da_gli_stessi_corpi_dei_run_di_produzione():
    for nome in ("lineare", "join_union", "catalogo", "foreach"):
        g = GRAFI[nome]
        catene = resolve_output_chains(g, _resolve_ds, BUCKET)
        corpi = build_output_run_requests(g, _resolve_ds, BUCKET, "production")
        assert [c["body"] for c in catene] == corpi, nome
        assert all(len(c["op_ids"]) == len(c["body"]["operations"]) for c in catene)


def test_resolve_output_chains_rifiuta_come_prima():
    with pytest.raises(FlowResolveError, match="nessuna sorgente"):
        resolve_output_chains(SENZA_SORGENTE, _resolve_ds, BUCKET)
    with pytest.raises(FlowResolveError, match="nessuna sorgente"):
        build_output_run_requests(SENZA_SORGENTE, _resolve_ds, BUCKET)
    with pytest.raises(FlowResolveError, match="non ha nodi Output"):
        resolve_output_chains({"nodes": [_src("s", "k")], "edges": []}, _resolve_ds, BUCKET)
