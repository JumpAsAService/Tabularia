"""DEVELOPMENT sample on the source nodes — a PRODUCTION invariant.

`data.sample` on a source node becomes an operation (limit/sample marked
`_dev_sample`) ONLY in engine_mode="development". In production (the resolver's
default, scheduler, run-now ?mode=production) it is never injected, and
`strip_dev_sample_ops` removes it everywhere anyway, nested too.
"""
from app.services.flow_resolver import (
    DEV_SAMPLE_MARK,
    build_output_run_requests,
    dev_sample_operation,
    resolve_output_request,
    strip_dev_sample_ops,
)

BUCKET = "data-prep"


def _definition(sample=None, right_sample=None):
    src = {"id": "s", "type": "source", "data": {"bucket": BUCKET, "parquetKey": "datasets/big.parquet", "sample": sample}}
    right = {"id": "r", "type": "source", "data": {"bucket": BUCKET, "parquetKey": "datasets/dim.parquet", "sample": right_sample}}
    filt = {"id": "f", "type": "operation", "data": {"opType": "filter", "params": {"column": "x", "operator": "gt", "value": 1}}}
    join = {"id": "j", "type": "operation", "data": {"opType": "join", "params": {"on": ["k"], "how": "left"}}}
    out = {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "res", "projectId": 1}}
    edges = [
        {"id": "e1", "source": "s", "target": "f"},
        {"id": "e2", "source": "f", "target": "j"},
        {"id": "e3", "source": "r", "target": "j", "targetHandle": "right"},
        {"id": "e4", "source": "j", "target": "o"},
    ]
    return {"nodes": [src, right, filt, join, out], "edges": edges}, out


def _all_ops(ops):
    """All the operations, those nested in right/driver/body too."""
    for op in ops:
        yield op
        p = op.get("params") or {}
        for key in ("right", "driver"):
            if isinstance(p.get(key), dict):
                yield from _all_ops(p[key].get("operations") or [])
        if isinstance(p.get("body"), list):
            yield from _all_ops(p["body"])


def _has_sample(ops) -> bool:
    return any((op.get("params") or {}).get(DEV_SAMPLE_MARK) for op in _all_ops(ops))


def test_dev_sample_operation_shapes():
    assert dev_sample_operation({}) is None
    assert dev_sample_operation({"sample": None}) is None
    assert dev_sample_operation({"sample": {"mode": "first", "rows": 0}}) is None
    assert dev_sample_operation({"sample": {"mode": "random", "percent": 100}}) is None
    assert dev_sample_operation({"sample": {"mode": "first", "rows": 1000}}) == {
        "type": "limit", "params": {"n": 1000, DEV_SAMPLE_MARK: True},
    }
    rnd = dev_sample_operation({"sample": {"mode": "random", "percent": 10}})
    assert rnd["type"] == "sample" and rnd["params"]["fraction"] == 0.1 and rnd["params"][DEV_SAMPLE_MARK]


def test_production_never_samples():
    definition, out = _definition(sample={"mode": "first", "rows": 1000}, right_sample={"mode": "random", "percent": 5})
    # the resolver's default = production
    req = resolve_output_request(definition, out, lambda i: None, BUCKET)
    assert not _has_sample(req["operations"])
    assert req["operations"][0]["type"] == "filter"
    # explicit
    req = resolve_output_request(definition, out, lambda i: None, BUCKET, engine_mode="production")
    assert not _has_sample(req["operations"])
    # any value other than "development" is production
    req = resolve_output_request(definition, out, lambda i: None, BUCKET, engine_mode="prod")
    assert not _has_sample(req["operations"])
    reqs = build_output_run_requests(definition, lambda i: None, BUCKET)
    assert not any(_has_sample(r["operations"]) for r in reqs)


def test_development_injects_sample_on_every_source():
    definition, out = _definition(sample={"mode": "first", "rows": 1000}, right_sample={"mode": "random", "percent": 5})
    req = resolve_output_request(definition, out, lambda i: None, BUCKET, engine_mode="development")
    ops = req["operations"]
    # the main chain's first operation = the source's sample
    assert ops[0] == {"type": "limit", "params": {"n": 1000, DEV_SAMPLE_MARK: True}}
    assert ops[1]["type"] == "filter"
    # the join's right side is sampled too
    right_ops = ops[2]["params"]["right"]["operations"]
    assert right_ops[0]["type"] == "sample" and right_ops[0]["params"][DEV_SAMPLE_MARK]


def test_development_without_sample_is_untouched():
    definition, out = _definition()
    req = resolve_output_request(definition, out, lambda i: None, BUCKET, engine_mode="development")
    assert not _has_sample(req["operations"])
    assert [o["type"] for o in req["operations"]] == ["filter", "join"]


def test_strip_removes_marked_ops_everywhere():
    ops = [
        {"type": "limit", "params": {"n": 10, DEV_SAMPLE_MARK: True}},
        {"type": "limit", "params": {"n": 5}},  # a limit the user WANTED: it stays
        {"type": "join", "params": {"on": ["k"], "right": {"source": {"bucket": BUCKET, "key": "datasets/d.parquet"}, "operations": [
            {"type": "sample", "params": {"fraction": 0.1, DEV_SAMPLE_MARK: True}},
            {"type": "select", "params": {"columns": ["k"]}},
        ]}}},
        {"type": "foreach", "params": {
            "driver": {"source": {"bucket": BUCKET, "key": "datasets/drv.parquet"}, "operations": [{"type": "limit", "params": {"n": 3, DEV_SAMPLE_MARK: True}}]},
            "body": [{"type": "sample", "params": {"fraction": 0.5, DEV_SAMPLE_MARK: True}}, {"type": "filter", "params": {"column": "x", "operator": "eq", "value": 1}}],
        }},
    ]
    clean, removed = strip_dev_sample_ops(ops)
    assert removed == 4
    assert not _has_sample(clean)
    assert [o["type"] for o in clean] == ["limit", "join", "foreach"]
    assert clean[0]["params"] == {"n": 5}
    assert [o["type"] for o in clean[1]["params"]["right"]["operations"]] == ["select"]
    assert clean[2]["params"]["driver"]["operations"] == []
    assert [o["type"] for o in clean[2]["params"]["body"]] == ["filter"]
    # idempotent and without effects on clean chains
    again, n = strip_dev_sample_ops(clean)
    assert n == 0 and again == clean


# ── AUTOMATIC sample (PREVIEW__DEFAULT_SAMPLE_ROWS) ─────────────────────────
def _with_default(monkeypatch, rows: int):
    import app.core.config as config_mod

    settings = config_mod.Settings(preview={"default_sample_rows": rows})
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)
    return settings


def test_default_sample_shapes():
    from app.services.flow_resolver import MAX_SAMPLE_ROWS

    # without a sample on the node: the default samples it
    assert dev_sample_operation({}, default_rows=100_000) == {"type": "limit", "params": {"n": 100_000, DEV_SAMPLE_MARK: True}}
    assert dev_sample_operation({"sample": None}, default_rows=100_000)["params"]["n"] == 100_000
    # a safety cap as for the manual sample
    assert dev_sample_operation({}, default_rows=10**12)["params"]["n"] == MAX_SAMPLE_ROWS
    # turned off EXPLICITLY by the user: all the rows, even with the default on
    assert dev_sample_operation({"sample": {"mode": "off"}}, default_rows=100_000) is None
    assert dev_sample_operation({"sample": {"mode": "off"}}) is None
    # a sample the user chose wins over the default
    assert dev_sample_operation({"sample": {"mode": "first", "rows": 10}}, default_rows=100_000)["params"]["n"] == 10
    assert dev_sample_operation({"sample": {"mode": "random", "percent": 5}}, default_rows=100_000)["type"] == "sample"
    # a malformed manual sample does NOT fall back on the default: it was a choice, not an absence
    assert dev_sample_operation({"sample": {"mode": "first", "rows": 0}}, default_rows=100_000) is None
    # default off = the behaviour of before
    assert dev_sample_operation({}, default_rows=0) is None


def test_default_sample_applies_to_every_unsampled_source_in_development(monkeypatch):
    _with_default(monkeypatch, 100_000)
    definition, out = _definition()  # no sample on the nodes
    req = resolve_output_request(definition, out, lambda i: None, BUCKET, engine_mode="development")
    ops = req["operations"]
    assert ops[0] == {"type": "limit", "params": {"n": 100_000, DEV_SAMPLE_MARK: True}}
    assert ops[1]["type"] == "filter"
    right_ops = ops[2]["params"]["right"]["operations"]
    assert right_ops[0] == {"type": "limit", "params": {"n": 100_000, DEV_SAMPLE_MARK: True}}


def test_default_sample_respects_explicit_off_and_manual_sample(monkeypatch):
    _with_default(monkeypatch, 100_000)
    definition, out = _definition(sample={"mode": "off"}, right_sample={"mode": "random", "percent": 5})
    req = resolve_output_request(definition, out, lambda i: None, BUCKET, engine_mode="development")
    ops = req["operations"]
    assert [o["type"] for o in ops] == ["filter", "join"]  # main source: all the rows
    right_ops = ops[1]["params"]["right"]["operations"]
    assert right_ops[0]["type"] == "sample" and right_ops[0]["params"]["fraction"] == 0.05


def test_default_sample_never_reaches_production(monkeypatch):
    _with_default(monkeypatch, 100_000)
    definition, out = _definition()
    for mode in (None, "production", "prod"):
        kw = {"engine_mode": mode} if mode else {}
        req = resolve_output_request(definition, out, lambda i: None, BUCKET, **kw)
        assert not _has_sample(req["operations"])
    reqs = build_output_run_requests(definition, lambda i: None, BUCKET)
    assert not any(_has_sample(r["operations"]) for r in reqs)


def test_default_sample_off_by_default(monkeypatch):
    settings = _with_default(monkeypatch, 0)
    assert settings.preview.default_sample_rows == 0
    definition, out = _definition()
    req = resolve_output_request(definition, out, lambda i: None, BUCKET, engine_mode="development")
    assert not _has_sample(req["operations"])


def test_system_info_exposes_default_sample(monkeypatch):
    import app.routes.system as system_mod

    settings = _with_default(monkeypatch, 100_000)
    monkeypatch.setattr(system_mod, "get_settings", lambda: settings)
    info = system_mod.app_info(user=None)
    assert info.preview_default_sample_rows == 100_000
    assert info.version == settings.app.version


def test_preview_settings_reject_negative():
    import pytest
    import app.core.config as config_mod

    with pytest.raises(Exception):
        config_mod.Settings(preview={"default_sample_rows": -1})
