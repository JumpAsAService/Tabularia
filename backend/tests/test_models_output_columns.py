"""What a model node hands to the next step before it has run (docs/capabilities/models.md,
«Trying a model»): the catalogue's `OUTPUT_TYPES` and `output_columns`, checked against what the
runners really write (`fixtures/models/output_columns.json`, made by make_output_columns.py with the
groups' images). The editor (frontend/tests/model-catalog.test.mjs) and the flow assistant
(gateway/tests/test_ai_flow_agent.py) read the same cases.
"""
from __future__ import annotations

import fnmatch
import json
from pathlib import Path

import pytest

from app.engine.models import catalog
from app.engine.stats.output import OUTPUT_COLUMNS

CASES = json.loads((Path(__file__).parent / "fixtures" / "models" / "output_columns.json").read_text(encoding="utf-8"))["cases"]
ENGINE_TYPES = {"Float64", "Int32", "Int64", "String", "Boolean"}


def _variant_of(case: dict) -> str:
    spec = catalog.MODELS[case["model"]]
    full = {**spec["defaults"], **case["params"]}
    return catalog.variant(case["model"], full)


def test_every_column_an_output_declares_has_a_type():
    assert list(catalog.OUTPUT_TYPES["results"].items()) == [(n, t) for n, t, _ in OUTPUT_COLUMNS]
    for model, spec in catalog.MODELS.items():
        time_keys = {k for k, kind in {**spec["needs"], **spec["takes"]}.items() if kind == "time_column"}
        for v in spec["variants"]:
            for output, names in v["outputs"].items():
                for name in names:
                    kind = catalog.OUTPUT_TYPES[output].get(name)
                    assert kind is not None, f"{model} ({v['id']}): {output} writes {name}, of no declared type"
                    # «@key», «=key»: the type of the input's column the key names
                    assert kind in ENGINE_TYPES or kind[1:] in time_keys or (kind[0] == "=" and kind[1:] in {**spec["needs"], **spec["takes"]}), \
                        f"{model}: {name} is {kind}"
    for output, types in catalog.OUTPUT_TYPES.items():
        declared = {n for spec in catalog.MODELS.values() for v in spec["variants"] for n in v["outputs"].get(output, [])}
        assert output == "results" or set(types) == declared, f"{output}: types of columns no model writes"
    assert set(catalog.ROWS_INPUT_TYPES) == {g["language"] for g in catalog.GROUPS.values()}


# No model is waiting for its first run now: lot E5's planning node has its three cases in make_output_columns.py.
NOT_RUN_YET: set[str] = set()


def test_every_variant_that_hands_on_columns_was_run():
    run = {(c["model"], _variant_of(c)) for c in CASES if {**catalog.MODELS[c["model"]]["defaults"], **c["params"]}.get("output") != "results"}
    for model, spec in catalog.MODELS.items():
        for v in spec["variants"]:
            if v["outputs"] and model not in NOT_RUN_YET:
                assert (model, v["id"]) in run, f"{model} ({v['id']}): no case of output_columns.json runs it"
    assert any({**catalog.MODELS[c["model"]]["defaults"], **c["params"]}.get("output") == "results" for c in CASES)


@pytest.mark.parametrize("case", CASES, ids=[c["what"] for c in CASES])
def test_the_columns_before_a_run_are_the_runner_s(case):
    """The columns known before the run are the runner's, in its order and of its types; the ones the
    data name (a probability per class) are left for the run, and are of their pattern's type."""
    cols, unknown = catalog.output_columns(case["model"], case["params"], [tuple(c) for c in case["input"]])
    written = [tuple(c) for c in case["columns"]]
    later = [(n, t) for n, t in written if any(fnmatch.fnmatchcase(n, p) for p in unknown)]
    assert cols == [c for c in written if c not in later]
    output = {**catalog.MODELS[case["model"]]["defaults"], **case["params"]}["output"]
    for name, kind in later:
        assert catalog.output_type(output, name) == kind, name


def test_the_columns_before_a_run_need_no_complete_parameters():
    """A node just dropped, nothing chosen: its columns all the same, and «Unknown» for a type read
    from a column not chosen yet."""
    for model in catalog.MODELS:
        cols, _ = catalog.output_columns(model, {}, None)
        assert cols and all(isinstance(n, str) and isinstance(t, str) for n, t in cols), model
    cols, _ = catalog.output_columns("arima", {"output": "forecast"}, [("month", "Date")])
    assert cols[0] == ("time", "Unknown")
    cols, _ = catalog.output_columns("arima", {"time": "month", "output": "forecast"}, [("month", "Date")])
    assert cols[0] == ("time", "Date")
    # the rows: the input first, as the runner writes it back (R: a whole number as Int32)
    cols, _ = catalog.output_columns("linear", {"output": "rows"}, [("n", "Int64"), ("x", "Float64")])
    assert cols[:3] == [("n", "Int32"), ("x", "Float64"), ("pred_fitted", "Float64")]
    cols, _ = catalog.output_columns("xgboost", {"task": "regression", "output": "rows"}, [("n", "Int64")])
    assert cols[:2] == [("n", "Int64"), ("pred_value", "Float64")]
    # the quantiles and the times as the check hands them to the runner: in order, each once
    names = [n for n, _ in catalog.output_columns("parametric_survival", {"output": "rows", "quantiles": [0.75, 0.25, 0.75],
                                                                          "times": [30, 7.5]}, [])[0]]
    assert [n for n in names if n.startswith(("pred_quantile_", "pred_surv_at_")) and not n.endswith(("_low", "_high"))] == [
        "pred_quantile_0.25", "pred_quantile_0.75", "pred_surv_at_7.5", "pred_surv_at_30"]
    # a holdout's column only with a holdout; the CLV's only with a spend
    names = [n for n, _ in catalog.output_columns("clv", {}, [])[0]]
    assert names == ["pred_p_alive", "pred_transactions"]
    names = [n for n, _ in catalog.output_columns("bg_bb", {"spend": "s", "holdout_transactions": "h"}, [])[0]]
    assert names == ["pred_p_alive", "pred_transactions", "pred_holdout_transactions", "pred_spend",
                     "pred_discounted_transactions", "pred_clv"]


def test_the_experiments_outputs_before_a_run():
    """`variants`: the variant column keeps the input's own type (`=variant`), whatever it is; `plan` has no input column.
    Always every column: a null where an analysis is off."""
    names = [n for n, _ in catalog.output_columns("ab_test", {"output": "variants"}, [])[0]]
    assert names == ["metric", "role", "variant", "is_control", "units", "share", "expected_share", "mean", "std_error", "ci_low",
                     "ci_high", "difference", "difference_ci_low", "difference_ci_high", "lift", "lift_ci_low", "lift_ci_high",
                     "p_value", "p_adjusted", "decision", "prob_beat_control", "prob_best", "expected_loss", "sequential_p_value",
                     "sequential_p_adjusted", "sequential_lift_low", "sequential_lift_high"]
    for own in ("Int64", "Int32", "Boolean", "String", "Date", "Datetime(time_unit='us', time_zone=None)"):
        cols, unknown = catalog.output_columns("ab_test", {"variant": "arm", "output": "variants"}, [("arm", own), ("x", "Int64")])
        assert dict(cols)["variant"] == own and unknown == [] and "x" not in dict(cols), own  # no input column but the variant
    assert dict(catalog.output_columns("ab_test", {"output": "variants"}, [("arm", "Int64")])[0])["variant"] == "Unknown"
    assert dict(catalog.output_columns("ab_test", {"variant": "gone", "output": "variants"}, [("arm", "Int64")])[0])["variant"] == "Unknown"
    # the rows output's Int32 rule is for `rows` and `@key` alone
    assert dict(catalog.output_columns("ab_test", {"variant": "arm", "output": "variants"}, [("arm", "Int64")])[0])["units"] == "Int64"
    plan, unknown = catalog.output_columns("ab_plan", {"output": "plan"}, [("arm", "Int64")])
    assert [n for n, _ in plan] == ["lift", "mde_absolute", "n_per_variant", "n_control", "n_total", "days", "weeks",
                                     "n_per_variant_cuped", "days_cuped", "asked"] and unknown == []
    assert dict(plan)["asked"] == "Boolean" and all(t == "Float64" for n, t in plan if n != "asked")
    for node in ("ab_test", "ab_plan"):
        assert catalog.output_columns(node, {}, [("arm", "Int64")])[0] == list(catalog.OUTPUT_TYPES["results"].items())


def test_output_type_reads_a_name_through_its_pattern():
    assert catalog.output_type("rows", "pred_quantile_0.25_low") == "Float64"
    assert catalog.output_type("rows", "pred_prob_gold") == "Float64"
    assert catalog.output_type("rows", "pred_class") == "String"
    assert catalog.output_type("rows", "pred_quantile_") is None
    assert catalog.output_type("table", "pred_fitted") is None
