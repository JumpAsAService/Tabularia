"""Every report a test builds, built both ways (app/reports/frames.py): the interpreter's
rows and DuckDB's columns must give the same model — every text, every value with its type
(an int is not a float: it prints differently), every chart's option, every warning — and
the same files, or the same error. Imported by the report tests, whose fixture `both_ways`
is then used by each of their tests:

    from tests.reports_both_ways import both_ways  # noqa: F401 — every report, both ways
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.reports import frames, render
from app.reports import model as rmodel

_inside: list[dict] = []  # a render built both ways: the models its two builds made


def _value(v: Any) -> Any:
    if isinstance(v, float):
        return ("float", repr(v))
    if v is None or isinstance(v, (bool, int, str)):
        return (type(v).__name__, v)
    return (type(v).__name__, repr(v))


def _block(kind: str, what: Any) -> Any:
    if kind == "table":
        return what.item.id
    if kind == "kpi":
        return json.dumps(what, sort_keys=True, ensure_ascii=False)
    item, other = what
    return (item.id, other)


def dump(m: rmodel.Model) -> dict:
    """Everything a model holds, comparable."""
    return {
        "doc": json.dumps(m.doc, sort_keys=True, ensure_ascii=False, default=repr),
        "tables": [
            (t.item.id, t.title, t.levels, json.dumps(t.head, sort_keys=True, ensure_ascii=False),
             [(c.header, c.width, c.align, c.fmt, json.dumps(c.style, sort_keys=True), sorted(c.kinds), c.subtotal)
              for c in t.cols],
             [(r.kind, r.level, r.label, r.span, json.dumps(r.key), r.closed, r.glue, r.zebra, r.repeat, r.continued,
               r.first, r.formula, [(c.text, _value(c.value), json.dumps(c.style, sort_keys=True)) for c in r.cells])
              for r in t.rows])
            for t in m.tables],
        "charts": [(c.name, c.width, c.height, json.dumps(c.option, sort_keys=True, ensure_ascii=False, default=repr),
                    c.alt) for c in m.charts],
        "files": {k: hashlib.sha256(v).hexdigest() for k, v in m.files.items()},
        "blocks": [(kind, _block(kind, what)) for kind, what in m.blocks],
        "params": m.params,
        "warnings": m.warnings,
        "title": m.title,
    }


def same_models(rows: rmodel.Model, columns: rmodel.Model) -> None:
    a, b = dump(rows), dump(columns)
    for key in a:
        if a[key] != b[key]:
            if isinstance(a[key], list) and isinstance(b[key], list) and len(a[key]) == len(b[key]):
                for i, (x, y) in enumerate(zip(a[key], b[key])):
                    assert x == y, f"the model's {key}[{i}] differs between the interpreter and DuckDB"
            assert a[key] == b[key], f"the model's {key} differs between the interpreter and DuckDB"


@contextlib.contextmanager
def _columns(on: bool):
    before = frames.COLUMNS
    frames.COLUMNS = None if on else False
    try:
        yield
    finally:
        frames.COLUMNS = before


def _outcome(fn: Any) -> tuple[Any, BaseException | None]:
    try:
        return fn(), None
    except Exception as e:  # noqa: BLE001 — compared, then raised again
        return None, e


def _same_errors(a: BaseException | None, b: BaseException | None) -> None:
    assert type(a) is type(b) and str(a) == str(b), \
        f"the interpreter and DuckDB end differently: {type(a).__name__}: {a} / {type(b).__name__}: {b}"


@pytest.fixture(autouse=True)
def both_ways(monkeypatch):
    real_build, real_render = rmodel.build, render.render

    def build(report, data, params, ctx, *args, **kw):
        if _inside:  # one of a render's two builds: the render compares them
            m = real_build(report, data, params, ctx, *args, **kw)
            _inside[-1].setdefault("models", []).append(m)
            return m
        if kw.get("store") is not None or frames.COLUMNS is False:
            return real_build(report, data, params, ctx, *args, **kw)
        with _columns(False):
            py, py_error = _outcome(lambda: real_build(report, data, params, ctx.at(memo={}), *args, **kw))
        with _columns(True):
            sq, sq_error = _outcome(lambda: real_build(report, data, params, ctx.at(memo={}), *args, **kw))
        _same_errors(py_error, sq_error)
        if sq_error is not None:
            raise sq_error
        same_models(py, sq)
        return sq

    def render_both(definition, datasets, params, fmt, workdir, **kw):
        workdir = Path(workdir)
        aside = workdir.parent / (workdir.name + "-interpreter")
        _inside.append({})
        try:
            with _columns(False):
                py, py_error = _outcome(lambda: real_render(definition, datasets, params, fmt, aside, **kw))
            py_models = _inside[-1].pop("models", [])
            with _columns(True):
                sq, sq_error = _outcome(lambda: real_render(definition, datasets, params, fmt, workdir, **kw))
            sq_models = _inside[-1].pop("models", [])
        finally:
            _inside.pop()
        _same_errors(py_error, sq_error)
        if sq_error is not None:
            shutil.rmtree(aside, ignore_errors=True)
            raise sq_error
        assert len(py_models) == len(sq_models)
        for a, b in zip(py_models, sq_models):
            same_models(a, b)
        if fmt in ("pdf", "svg"):  # laid out from the same model: the same bytes
            assert py.path.read_bytes() == sq.path.read_bytes(), "the files differ between the interpreter and DuckDB"
        assert (py.pages, py.warnings) == (sq.pages, sq.warnings)
        shutil.rmtree(aside, ignore_errors=True)
        return sq

    monkeypatch.setattr(rmodel, "build", build)
    monkeypatch.setattr(render, "render", render_both)
    yield
