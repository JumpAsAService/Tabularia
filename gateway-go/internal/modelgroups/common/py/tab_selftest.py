"""The self-test of a group's image (the twin of common/R/selftest.R): `python -I -B
runner/run.py --selftest` (or a task whose mode is selftest). The check Job of a model group runs
it; the version is ready only when it exits 0. It runs:

  1. COMMON_TESTS: the library's own tests, on common/selftest/table.csv (20 rows whose column
     names look like code: a`b, x); system("id"), ü ñ), then the tests a model module registered
     itself (`@register("…")`, as TAB_COMMON_TESTS in R);
  2. the group's reference cases, reference/expected.json (below), each fitted in this process
     from its CSV and compared with the values computed outside Tabularia.

It prints one line per check, «TABULARIA_SELFTEST ok|failed|skipped <name>: <detail>», then
«TABULARIA_SELFTEST_REPORT {"ok": n, "failed": n, "skipped": n, "checks": […]}» last, and
exits 0 when nothing failed, 4 otherwise.

reference/expected.json:
  {"format_version": 1, "generated": {"by": "make_reference.py", "python": "3.14.0", "packages": {…}},
   "cases": [{"id": "xgboost_regression", "model": "xgboost", "data": "data/regression.csv",
              "types": {"boxes": "number", "channel": "text"}, "params": {…},
              "expect": [{"key": "model|||validation_rmse", "value": 1.234, "rel": 1e-9},
                         {"key": "model|||variant", "text": "regression"}],
              "refuses": null}]}
A case may instead expect a refusal: "refuses": {"code": "too_few_rows", "message": "…"}.
`rel` (relative, default 1e-9) and `abs` (absolute, default 1e-12) bound a number; a number
expected null is written null.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from typing import Any, Callable

import numpy as np

from tab_card import CARD_FORMAT, NOTE_CARD_TOO_LARGE, NOTE_MODEL_FILE_TOO_LARGE, card, card_fits, mlmodel, model_file_entry, versions
from tab_charts import cap, extreme_labels, hist, thin
from tab_columns import complete_rows, kind, label, read_csv, read_input, read_ref_text, ref_path, rename, sort_labels
from tab_protocol import (EXIT_OK, EXIT_SELFTEST_FAILED, PROTOCOL, Refusal, catalog, read_text, rng, to_json, variant_of,
                          write_text)
from tab_results import COLUMNS, Rows, add, check_declared, frame, number, p_value, points, rows_start, table, text

Test = Callable[[Path], Any]

# name → test(code): the library's, then those a model module registers
COMMON_TESTS: dict[str, Test] = {}


def register(name: str) -> Callable[[Test], Test]:
    """A model module's own test, run with the library's (a test returns "skipped" to skip)."""
    def wrap(f: Test) -> Test:
        COMMON_TESTS[name] = f
        return f
    return wrap


def selftest(group: str, routes: dict[str, Any], code: Path) -> int:
    checks: list[dict[str, str]] = []

    def record(name: str, status: str, detail: str = "") -> None:
        checks.append({"name": name, "status": status, "detail": detail})
        print(f"TABULARIA_SELFTEST {status} {name}" + (f": {detail}" if detail else ""), flush=True)

    def run(name: str, f: Callable[[], Any]) -> None:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                outcome = f()
            record(name, "skipped", "not run here") if outcome == "skipped" else record(name, "ok")
        except Exception as e:  # noqa: BLE001 — every failure is a line of the report
            record(name, "failed", f"{type(e).__name__}: {e}" if not isinstance(e, AssertionError) else str(e))

    for name, test in list(COMMON_TESTS.items()):
        run(name, lambda test=test: test(code))
    expected = code / "reference" / "expected.json"  # expected.json.gz in the check Job's ConfigMap (ref_path)
    if os.path.exists(ref_path(expected)):
        ref = json.loads(read_ref_text(expected))
        for case in ref.get("cases", []):
            run(f"reference {case['id']}", lambda case=case: reference_case(case, routes, code, group))
        if not ref.get("cases"):
            record("reference cases", "ok", "none yet")
    else:
        record("reference cases", "failed", "reference/expected.json is missing")
    counts = {s: sum(c["status"] == s for c in checks) for s in ("ok", "failed", "skipped")}
    report = {"group": group, **counts, "software": versions(_installed()), "checks": checks}
    print("TABULARIA_SELFTEST_REPORT " + to_json(report), flush=True)
    return EXIT_SELFTEST_FAILED if counts["failed"] else EXIT_OK


def _installed() -> list[str]:
    from importlib import metadata

    names = {d.metadata["Name"] for d in metadata.distributions() if d.metadata["Name"]}
    return sorted((n for n in names if n.lower() in {"numpy", "pandas", "pyarrow", "scipy", "xgboost-cpu", "xgboost", "catboost"}),
                  key=str.lower)


def reference_case(case: dict[str, Any], routes: dict[str, Any], code: Path, group: str) -> None:
    """One reference case: fitted in this process on its CSV, compared value by value."""
    df = read_csv(str(code / "reference" / case["data"]), case.get("types") or {})
    params = case.get("params") or {}
    task = {"protocol": PROTOCOL, "mode": "fit", "group": group, "group_version": "selftest", "model": case["model"],
            "variant": case.get("variant") or variant_of(case["model"], params), "params": params, "output": "results",
            "write_output": False, "rows_from": "run", "limits": dict(catalog()["limits"]), "_frame": df}
    route = routes.get(case["model"])
    if route is None:
        raise AssertionError(f"the group has no route for {case['model']}")
    try:
        outcome = route["fit"](task)
    except Refusal as e:
        outcome = e
    refuses = case.get("refuses")
    if refuses is not None:
        _expect(isinstance(outcome, Refusal), "it was not refused")
        _expect(outcome.code == refuses["code"], f"refused with {outcome.code}, not {refuses['code']}")
        if refuses.get("message") is not None:
            _expect(outcome.message == refuses["message"], f"refused with «{outcome.message}»")
        return
    if isinstance(outcome, Refusal):
        raise AssertionError(f"refused: {outcome.message}")
    rows = outcome["rows"]
    problems = check_declared(rows, case["model"], task["variant"])
    _expect(not problems, problems[0] if problems else "")
    at = {k: i for i, k in enumerate(rows.key)}
    for e in case.get("expect", []):
        i = at.get(e["key"])
        _expect(i is not None, f"no row {e['key']}")
        if "text" in e:
            _expect(rows.text[i] == e["text"], f"{e['key']}: «{rows.text[i]}», expected «{e['text']}»")
            continue
        got, want = rows.value[i], e.get("value")
        if want is None or math.isnan(got):
            _expect(want is None and math.isnan(got), f"{e['key']}: {got}, expected {want}")
            continue
        tol = max(float(e.get("abs", 1e-12)), float(e.get("rel", 1e-9)) * abs(float(want)))
        _expect(abs(got - float(want)) <= tol, f"{e['key']}: {got!r}, expected {float(want)!r} (tolerance {tol:.3g})")


# ── the common library's own tests ─────────────────────────────────────────────
def _expect(ok: Any, message: str) -> None:
    if not ok:
        raise AssertionError(message)


def _refused_with(code: str, f: Callable[[], Any]) -> str:
    try:
        f()
    except Refusal as e:
        _expect(e.code == code, f"refused with {e.code}, expected {code}")
        return e.message
    raise AssertionError(f"not refused (expected {code})")


A = "a`b"
X = 'x); system("id")'
U = "ü ñ"


def _table(code: Path):
    return read_csv(str(code / "common" / "selftest" / "table.csv"),
                    {"y": "number", A: "number", X: "text", U: "boolean", "when": "date", "w": "number"})


@register("column names that look like code come back as labels")
def _names(code: Path) -> None:
    df = _table(code)
    to, back = rename(df.columns)
    positional = df.rename(columns=to)
    _expect(list(positional.columns) == [f"v{i}" for i in range(1, 7)], f"positional names: {list(positional.columns)}")
    _expect([back[c] for c in positional.columns] == ["y", A, X, U, "when", "w"], "the way back")
    rows = Rows()
    number(rows, "importance", "gain", [1.0, 2.0, 3.0], level=[A, X, U])
    with tempfile.TemporaryDirectory() as tmp:
        import pyarrow.parquet as pq

        from tab_results import write

        write(rows, os.path.join(tmp, "r.parquet"))
        got = pq.read_table(os.path.join(tmp, "r.parquet")).column("level").to_pylist()
    _expect(got == [A, X, U], f"levels: {got}")
    _expect(rows.key[1] == f'importance|{X}||gain', f"key: {rows.key[1]}")
    _expect(sort_labels(df[X]) == ["east", "north", "south"], f"levels of {X}: {sort_labels(df[X])}")
    _expect(sort_labels(label(v) for v in df[U]) == ["false", "true"], "a boolean's labels")
    _expect(kind(df, X) == "text" and kind(df, U) == "boolean" and kind(df, "when") == "date" and kind(df, A) == "number", "kinds")


@register("rows with a NULL are left out and counted; an infinite value is refused")
def _missing(code: Path) -> None:
    df = _table(code)
    r = complete_rows(df, [A, "w"])
    _expect(r["n"] == 18 and r["n_missing"] == 2 and list(r["rows"]) == [i for i in range(1, 21) if i not in (4, 17)],
            f"n {r['n']}, missing {r['n_missing']}")
    _expect(complete_rows(df, [X, U])["n"] == 20, "a text and a boolean without NULL")
    bad = df.copy()
    bad.loc[2, A] = math.inf
    m = _refused_with("infinite", lambda: complete_rows(bad, [A]))
    _expect(A in m, m)


@register("the ten columns, their types and keys")
def _columns(code: Path) -> None:
    rows = Rows()
    text(rows, "model", "model", "linear")
    number(rows, "model", "n", 20)
    p_value(rows, "coef", [0.0004, 0.2], level=["price", "channel"], level_2=[None, "web"])
    number(rows, "model", "x", -0.0)
    number(rows, "model", "nan", math.nan)
    number(rows, "model", "tiny", 1e-310)
    points(rows, "point", "qq", "y", x=[-1, 0, 1], y=[0.5, math.inf, 2], labels=["17", None, None])
    df = frame(rows)
    _expect(list(df.columns) == COLUMNS, f"columns: {list(df.columns)}")
    _expect([str(t) for t in df.dtypes] == ["object"] * 4 + ["float64", "object", "float64", "float64", "int64", "object"],
            f"types: {[str(t) for t in df.dtypes]}")
    _expect(list(df["ord"]) == list(range(len(df))), "ord")
    want = ["model|||model", "model|||n", "coef|price||p_value", "coef|price||significance", "coef|channel|web|p_value",
            "coef|channel|web|significance", "model|||x", "model|||nan", "model|||tiny", "point|qq||y|0", "point|qq||y|1",
            "point|qq||y|2"]
    _expect(list(df["key"]) == want, f"keys: {' '.join(df['key'])}")
    by = dict(zip(df["key"], range(len(df))))
    _expect(df["text"][by["coef|price||significance"]] == "***", "R's stars")
    _expect(df["text"][by["coef|channel|web|significance"]] == "", "a p-value above 0.1 has an empty code")
    x = df["value"][by["model|||x"]]
    _expect(x == 0 and math.copysign(1, x) == 1, "−0 is not 0")
    _expect(math.isnan(df["value"][by["model|||nan"]]) and df["value"][by["model|||tiny"]] == 0, "NaN or subnormal")
    _expect(math.isnan(df["value"][by["point|qq||y|1"]]) and df["text"][by["point|qq||y|0"]] == "17", "points")
    t = table(rows)
    _expect(t.column("value").null_count == int(np.isnan(df["value"]).sum()) == 5 and t.column("x0").null_count == len(df) - 3,
            "a number that is not finite, or none, is NULL in the table")


@register("the results' Parquet file has the exact types")
def _parquet(code: Path) -> None:
    import pyarrow.parquet as pq

    from tab_results import write

    rows = Rows()
    text(rows, "model", "model", "linear")
    number(rows, "model", "n", 20)
    points(rows, "hist", "residual", "count", x=[0, 1], y=[3, 4], x1=[1, 2])
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "r.parquet")
        write(rows, path)
        back = pq.read_table(path)
    types = [str(f.type) for f in back.schema]
    _expect(types == ["string"] * 4 + ["double", "string", "double", "double", "int64", "string"], f"types: {types}")
    _expect(back.schema.names == COLUMNS, "names")
    _expect(back.column("ord").to_pylist() == [0, 1, 2, 3] and back.column("x1").to_pylist()[2:] == [1.0, 2.0], "values")


@register("the catalogue's declaration is checked")
def _declared(code: Path) -> None:
    task = {"model": "linear", "variant": "unpenalized", "rows_from": "trial", "group_version": 1}
    rows = rows_start(task)
    number(rows, "model", "n", 20)
    p_value(rows, "coef", 0.01, level="price")
    text(rows, "assumption", "verdict", "ok", level="breusch_pagan")
    problems = check_declared(rows, "linear", "unpenalized")
    _expect(not problems, "; ".join(problems))
    _expect(any("declares no p_value in coef" in p for p in check_declared(rows, "linear", "penalized")),
            "a p-value of a penalized fit passed")
    text(rows, "assumption", "verdict", "fine", level="white")
    _expect(any("is not one of" in p for p in check_declared(rows, "linear", "unpenalized")), "a verdict «fine» passed")
    boost = rows_start({"model": "xgboost", "variant": "binary", "rows_from": "run", "group_version": 3})
    number(boost, "model", "n", 400)
    number(boost, "importance", "gain", 1.5, level=A)
    _expect(not check_declared(boost, "xgboost", "binary"), "; ".join(check_declared(boost, "xgboost", "binary")))
    add(boost, "class", "support", 3, level="gold")
    _expect(any("has no section class" in p for p in check_declared(boost, "xgboost", "binary")), "a binary model's class rows passed")
    _expect(variant_of("linear", {"penalty": "lasso"}) == "penalized" and variant_of("arima", {}) == "manual"
            and variant_of("catboost", {"task": "multiclass"}) == "multiclass", "variants")


@register("charts are capped and keep their extremes")
def _charts(code: Path) -> None:
    from numpy.random import PCG64, Generator

    score = Generator(PCG64(1)).normal(size=5000)
    i = cap(5000, Generator(PCG64(1)), 2000, score, 10)
    top = np.argsort(-np.abs(score), kind="stable")[:10]
    _expect(len(i) == 2000 and np.all(np.diff(i) > 0) and set(top) <= set(i.tolist()), "the cap")
    _expect(np.array_equal(i, cap(5000, Generator(PCG64(1)), 2000, score, 10)), "the same seed, the same points")
    _expect(np.array_equal(cap(15, Generator(PCG64(1))), np.arange(15)), "below the cap")
    h = hist(score)
    _expect(h["count"].sum() == 5000 and 5 <= len(h["count"]) <= 30, "histogram")
    p = hist([0.0, 0.05, 0.5, 1.0], bins=20, lo=0.0, hi=1.0)
    _expect(p["count"].sum() == 4 and p["count"][0] == 2 and p["count"][19] == 1, f"bins of probabilities: {p['count']}")
    t = thin(3000, 100, keep=[1234])
    _expect(len(t) <= 100 and t[0] == 0 and t[-1] == 2999 and 1234 in t and np.all(np.diff(t) > 0), f"a thinned curve: {len(t)}")
    _expect(np.array_equal(thin(50, 100), np.arange(50)), "a short curve")
    _expect(extreme_labels([0.1, -5, 0.2], rows=[11, 12, 13], keep=1) == [None, "12", None], "labels")


@register("labels of values are the engine's")
def _labels(code: Path) -> None:
    got = [label(True), label(dt.date(2026, 11, 19)), label(2.0), label(0.1), label(3), label(1e-05), label(-0.0),
           label(np.float64(1.5e20)), label(np.int64(7)), label(np.bool_(False)), label(dt.datetime(2026, 1, 2, 3, 4, 5)),
           label(None), label(math.nan)]
    want = ["true", "2026-11-19", "2.0", "0.1", "3", "1e-05", "0.0", "1.5e+20", "7", "false", "2026-01-02 03:04:05", None, None]
    _expect(got == want, " ".join(map(str, got)))
    _expect(sort_labels(["b", "B", "à", "a", None]) == ["B", "a", "b", "à"], "text order")


@register("the input is read from Parquet with its columns' kinds and names")
def _input(code: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    t = pa.table({A: pa.array([1, None, 3], pa.int64()), X: pa.array(["b", None, "a"]), U: pa.array([True, None, False]),
                  "when": pa.array([dt.date(2026, 1, 1), None, dt.date(2026, 1, 3)]),
                  "at": pa.array([dt.datetime(2026, 1, 1, 12), None, None], pa.timestamp("us")), "y": pa.array([0.5, 1.5, None])})
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "in.parquet")
        pq.write_table(t, path)
        df = read_input("file://" + path)
        some = read_input(path, columns=[X, "y"])
    _expect(list(df.columns) == [A, X, U, "when", "at", "y"], f"names: {list(df.columns)}")
    kinds = [kind(df, c) for c in df.columns]
    _expect(kinds == ["integer", "text", "boolean", "date", "datetime", "number"], f"kinds: {kinds}")
    _expect(complete_rows(df, [A, X, U, "when"])["n"] == 2 and label(df["when"][0]) == "2026-01-01" and label(df[U][2]) == "false",
            "values")
    _expect(list(some.columns) == [X, "y"], "the columns asked for")
    _refused_with("no_input", lambda: read_input(""))


@register("a text is written to an address and read back whole")
def _address(code: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        uri = "file://" + os.path.join(tmp, "a.json")
        body = '{"a": "' + "à" * 3000 + '"}'
        write_text(uri, body)
        back = read_text(uri)
        _expect(back == body, f"read back {len(back)} characters, wrote {len(body)}")
        write_text(uri, "")
        _expect(read_text(uri) == "", "an empty file")


@register("the run's generator: the same seed, the same draws")
def _generator(code: Path) -> None:
    a, b = rng({"params": {"seed": 7}}), rng({"params": {"seed": 7}})
    _expect(np.array_equal(a.permutation(100), b.permutation(100)), "two generators of one seed differ")
    _expect(not np.array_equal(rng({"params": {"seed": 7}}).permutation(100), rng({"params": {"seed": 8}}).permutation(100)),
            "two seeds give the same draws")
    _expect(np.array_equal(rng({"params": {}}).permutation(10), rng({"params": {"seed": 1}}).permutation(10)), "the seed 1 by default")


@register("the model card")
def _card(code: Path) -> None:
    task = {"model": "xgboost", "variant": "binary", "group": "boosting", "group_version": 1, "params": {"task": "binary"},
            "limits": dict(catalog()["limits"])}
    c = card(task, [{"name": A, "type": "number"}, {"name": X, "type": "category", "levels": ["east", "north", "south"]}],
             packages=["numpy"])
    data = b'{"learner": {}}'
    c["boosting"] = {"library": "xgboost", "library_version": "3.4.1", "task": "binary", "objective": "binary:logistic",
                     "classes": ["no", "yes"], "event_value": "yes", "threshold": 0.5, "trees": 3, "best_iteration": 2, "threads": 2,
                     "missing_level": None, "model_file": model_file_entry("xgboost.json", "xgboost-json", data)}
    numbers = [0.1, 1 / 3, math.pi * 1e10, -1.2345678901234567e-5, 5e-324, 1.7976931348623157e308]
    c["numbers"] = numbers
    back = json.loads(to_json(c))
    schema = json.loads((code / "card.schema.json").read_text(encoding="utf-8"))
    missing = [k for k in schema["required"] if k not in back]
    _expect(not missing, f"missing: {', '.join(missing)}")
    lacking = [k for k in schema["properties"]["boosting"]["required"] if k not in back["boosting"]]
    _expect(not lacking, f"the boosting object lacks: {', '.join(lacking)}")
    _expect(back["format"] == CARD_FORMAT and isinstance(back["params"], dict) and "Python" in back["software"], "the common keys")
    _expect(back["numbers"] == numbers, "a number read back from the card is not the same double")
    _expect(back["inputs"][1]["levels"] == ["east", "north", "south"], "levels")
    _expect(back["boosting"]["model_file"]["bytes"] == len(data) and len(back["boosting"]["model_file"]["sha256"]) == 64, "the model file")
    _expect(to_json({"x": math.nan, "y": [math.inf, 1]}) == '{"x":null,"y":[null,1]}', "a number that is not finite")
    m = mlmodel(c)
    _expect("tabularia:\n    card: model.json" in m and "model_file: xgboost.json" in m and "'2" in m, m)
    small = {**task, "limits": {**task["limits"], "max_card_bytes": 100}}
    rows = Rows()
    with warnings.catch_warnings(record=True) as said:
        warnings.simplefilter("always")
        _expect(card_fits(small, c, rows) is None, "a card too large was kept")
    _expect(rows.text == [NOTE_CARD_TOO_LARGE] and rows.key == ["model|||note_card_too_large"] and len(said) == 1, "the note")


@register("a card or a model file too large does not stop the fit: results and a note, no model files")
def _finish(code: Path) -> None:
    import pyarrow.parquet as pq

    from tab_main import finish_fit

    data = b"0123456789" * 10
    names = ("model.json", "MLmodel", "xgboost.json")
    old = {k: os.environ.get(k) for k in ("TABULARIA_RESULTS", "TABULARIA_ARTIFACTS", "TABULARIA_OUTPUT")}
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["TABULARIA_RESULTS"] = os.path.join(tmp, "results.parquet")
            os.environ["TABULARIA_ARTIFACTS"] = os.path.join(tmp, "artifacts")
            os.environ["TABULARIA_OUTPUT"] = os.path.join(tmp, "output.parquet")
            for limits, note in (({"max_card_bytes": 100}, "note_card_too_large"),
                                 ({"max_model_file_bytes": 50}, "note_model_file_too_large"), ({}, None)):
                for name in names:
                    Path(tmp, "artifacts", name).unlink(missing_ok=True)
                task = {"model": "xgboost", "variant": "regression", "group": "boosting", "group_version": 1, "params": {},
                        "mlflow": True, "write_output": True, "limits": {**catalog()["limits"], **limits}}
                rows = rows_start(task)
                number(rows, "model", "n", 20)
                c = card(task, [{"name": A, "type": "number"}])
                c["boosting"] = {"model_file": model_file_entry("xgboost.json", "xgboost-json", data)}
                import pandas as pd

                with warnings.catch_warnings(record=True) as said:
                    warnings.simplefilter("always")
                    finish_fit(task, {"rows": rows, "output": pd.DataFrame({A: [1.0], "pred_value": [2.0]}), "card": c,
                                      "model_file": {"name": "xgboost.json", "data": data}})
                back = pq.read_table(os.environ["TABULARIA_RESULTS"]).to_pydict()
                there = [Path(tmp, "artifacts", n).exists() for n in names]
                if note:
                    _expect(f"model|||{note}" in back["key"], f"no {note} in the results")
                    _expect(back["text"][back["key"].index(f"model|||{note}")] in (NOTE_CARD_TOO_LARGE, NOTE_MODEL_FILE_TOO_LARGE), "the sentence")
                    _expect(not any(there), f"{note}: a model file was written")
                    _expect(len(said) == 1 and "is not written" in str(said[0].message), "the warning for run.json")
                else:
                    _expect(not any(k.startswith("model|||note_") for k in back["key"]), "a note under the limits")
                    _expect(all(there) and Path(tmp, "artifacts", "xgboost.json").read_bytes() == data, "the model files are missing")
                _expect(pq.read_table(os.environ["TABULARIA_OUTPUT"]).column_names == [A, "pred_value"], "the rows")
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


_FAKE_RUNNER = '''
import os, sys, warnings
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parent.parent / "common" / "py")]
import tab_main, tab_selftest
from tab_protocol import Refusal


def fit(task):
    warnings.warn("a warning of a library")
    if task["params"].get("seed") == 2:
        raise ValueError("a bug")
    raise Refusal("selftest_refusal", "a refusal à la carte", {"column": "a`b"})


if os.environ.get("TABULARIA_SELFTEST_FAIL") == "1":
    tab_selftest.COMMON_TESTS.clear()

    @tab_selftest.register("a check made to fail")
    def _fails(code):
        raise AssertionError("as asked")

tab_main.main("selftest", {"xgboost": {"fit": fit}})
'''


@register("a refusal writes error.json and exits 3; a crash exits 1; a failed self-test exits 4")
def _exits(code: Path) -> None:
    import shutil

    for route, status in (("refuse", 3), ("crash", 1), ("protocol", 3), ("selftest", 4)):
        with tempfile.TemporaryDirectory() as tmp:
            here = Path(tmp)
            (here / "runner").mkdir()
            shutil.copytree(code / "common" / "py", here / "common" / "py")
            shutil.copy(code / "catalog.json", here / "catalog.json")
            (here / "runner" / "run.py").write_text(_FAKE_RUNNER, encoding="utf-8")
            task = {"protocol": 99 if route == "protocol" else PROTOCOL, "mode": "fit", "group": "selftest", "model": "xgboost",
                    "params": {"seed": 2 if route == "crash" else 1}}
            (here / "task.json").write_text(to_json(task), encoding="utf-8")
            env = {**os.environ, "TABULARIA_ARTIFACTS": str(here / "artifacts")}
            args = [sys.executable, "-I", "-B", str(here / "runner" / "run.py")]
            if route == "selftest":
                env["TABULARIA_SELFTEST_FAIL"] = "1"
                args.append("--selftest")
            out = subprocess.run(args, cwd=here, env=env, capture_output=True, text=True, timeout=120)
            log = out.stdout + out.stderr
            _expect(out.returncode == status, f"{route}: exit {out.returncode}\n{log[-2000:]}")
            if route == "refuse":
                err = json.loads((here / "artifacts" / "error.json").read_text(encoding="utf-8"))
                _expect(err == {"code": "selftest_refusal", "message": "a refusal à la carte", "params": {"column": "a`b"}},
                        f"error.json: {err}")
                _expect(any(line.startswith("TABULARIA_ERROR {") for line in out.stdout.splitlines()), "no TABULARIA_ERROR line")
                run = json.loads((here / "artifacts" / "run.json").read_text(encoding="utf-8"))
                _expect(run["exit"] == 3 and "a warning of a library" in run["warnings"], f"run.json: {run}")
            if route == "protocol":
                err = json.loads((here / "artifacts" / "error.json").read_text(encoding="utf-8"))
                _expect(err["code"] == "protocol", f"protocol: {err['code']}")
            if route == "crash":
                _expect("the runner failed: a bug" in out.stderr and not (here / "artifacts" / "error.json").exists(), out.stderr[-500:])
            if route == "selftest":
                last = [line for line in out.stdout.splitlines() if line.startswith("TABULARIA_SELFTEST_REPORT ")]
                report = json.loads(last[-1].removeprefix("TABULARIA_SELFTEST_REPORT ")) if last else {}
                _expect(report.get("failed", 0) >= 1 and report.get("group") == "selftest", f"the report: {report}")


# the series' own tests (tab_series.py) register themselves; imported last, once register and _expect exist
import tab_series_tests  # noqa: E402,F401
