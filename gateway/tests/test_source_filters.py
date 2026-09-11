"""Filtri A MONTE sui nodi sorgente (`data.filters`).

Condizioni in AND applicate subito dopo la lettura della sorgente, PRIMA del
campione di sviluppo e di ogni altra operazione. A differenza del campione
fanno parte del flusso: iniettate in sviluppo E in produzione, senza marcatore,
quindi `strip_dev_sample_ops` non le rimuove. Condizioni incomplete = ignorate.
"""
from app.services.flow_resolver import (
    DEV_SAMPLE_MARK,
    build_output_run_requests,
    resolve_output_request,
    source_filter_operations,
    strip_dev_sample_ops,
)

BUCKET = "data-prep"


def _definition(filters=None, right_filters=None, sample=None):
    src = {"id": "s", "type": "source", "data": {"bucket": BUCKET, "parquetKey": "datasets/big.parquet", "filters": filters, "sample": sample}}
    right = {"id": "r", "type": "source", "data": {"bucket": BUCKET, "parquetKey": "datasets/dim.parquet", "filters": right_filters}}
    sel = {"id": "f", "type": "operation", "data": {"opType": "select", "params": {"columns": ["k", "x"]}}}
    join = {"id": "j", "type": "operation", "data": {"opType": "join", "params": {"on": ["k"], "how": "left"}}}
    out = {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "res", "projectId": 1}}
    edges = [
        {"id": "e1", "source": "s", "target": "f"},
        {"id": "e2", "source": "f", "target": "j"},
        {"id": "e3", "source": "r", "target": "j", "targetHandle": "right"},
        {"id": "e4", "source": "j", "target": "o"},
    ]
    return {"nodes": [src, right, sel, join, out], "edges": edges}, out


def test_source_filter_operations_shapes():
    assert source_filter_operations({}) == []
    assert source_filter_operations({"filters": None}) == []
    assert source_filter_operations({"filters": "x"}) == []
    ops = source_filter_operations({"filters": [
        {"id": "a", "column": "x", "operator": "gt", "value": 1},
        {"column": "s", "operator": "is_null", "value": "ignorato"},
        {"column": "k", "operator": "in", "value": ["a", "b"]},
        {"column": "d", "operator": "between", "value": ["2024-01-01", "2024-12-31"]},
        # incomplete → ignorate
        {"column": "", "operator": "eq", "value": 1},
        {"column": "x", "operator": "boh", "value": 1},
        {"column": "x", "operator": "eq"},
        {"column": "x", "operator": "in", "value": []},
        {"column": "x", "operator": "between", "value": [1]},
        "non-dict",
        {"operator": "eq", "value": 1},
    ]})
    assert ops == [
        {"type": "filter", "params": {"column": "x", "operator": "gt", "value": 1}},
        {"type": "filter", "params": {"column": "s", "operator": "is_null"}},
        {"type": "filter", "params": {"column": "k", "operator": "in", "value": ["a", "b"]}},
        {"type": "filter", "params": {"column": "d", "operator": "between", "value": ["2024-01-01", "2024-12-31"]}},
    ]
    # l'id di riga dell'editor non finisce nell'IR
    assert all("id" not in op["params"] for op in ops)


def test_filters_injected_in_every_mode_before_everything():
    filters = [{"column": "x", "operator": "gt", "value": 1}, {"column": "s", "operator": "is_not_null"}]
    definition, out = _definition(filters=filters, right_filters=[{"column": "k", "operator": "ne", "value": "z"}],
                                  sample={"mode": "first", "rows": 10})
    for mode in ("production", "development", None):
        kw = {"engine_mode": mode} if mode else {}
        req = resolve_output_request(definition, out, lambda i: None, BUCKET, **kw)
        ops = req["operations"]
        assert [o["type"] for o in ops[:2]] == ["filter", "filter"]
        assert ops[0]["params"] == {"column": "x", "operator": "gt", "value": 1}
        assert ops[1]["params"] == {"column": "s", "operator": "is_not_null"}
        # anche la sorgente del ramo destro del join è filtrata a monte
        right_ops = [o for o in ops if o["type"] == "join"][0]["params"]["right"]["operations"]
        assert right_ops[0] == {"type": "filter", "params": {"column": "k", "operator": "ne", "value": "z"}}
        if mode == "development":
            # campione DOPO i filtri (campiona i dati già filtrati), poi la catena
            assert ops[2]["type"] == "limit" and ops[2]["params"][DEV_SAMPLE_MARK]
            assert [o["type"] for o in ops[3:]] == ["select", "join"]
        else:
            assert [o["type"] for o in ops[2:]] == ["select", "join"]
    reqs = build_output_run_requests(definition, lambda i: None, BUCKET)
    assert reqs[0]["operations"][0]["type"] == "filter"


def test_filters_survive_strip_of_dev_samples():
    definition, out = _definition(filters=[{"column": "x", "operator": "gt", "value": 1}], sample={"mode": "random", "percent": 5})
    req = resolve_output_request(definition, out, lambda i: None, BUCKET, engine_mode="development")
    clean, removed = strip_dev_sample_ops(req["operations"])
    assert removed == 1
    assert [o["type"] for o in clean] == ["filter", "select", "join"]


def test_no_filters_is_untouched():
    definition, out = _definition()
    req = resolve_output_request(definition, out, lambda i: None, BUCKET, engine_mode="development")
    assert [o["type"] for o in req["operations"]] == ["select", "join"]
