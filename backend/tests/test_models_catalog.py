"""The catalogue of the statistical models (docs/specs/statistical-models.md, «The
catalogue»): the snapshot every component reads is what `catalog.py` says; every kind has a
control a form can draw and the gateway can read; every default is accepted by the check
it feeds; the variants cover every value of the keys that make them; the parameters'
check answers the shared cases (`fixtures/models/params_cases.json`, which the Go gateway
answers too) word for word; the columns are checked when the input's types are known.

The copies of the snapshot are checked by `.github/scripts/model_catalog.py --check`.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest

from app.engine.exceptions import EngineError
from app.engine.models import catalog
from app.engine.models.catalog import check_params

CASES = Path(__file__).parent / "fixtures" / "models" / "params_cases.json"
COLUMNS = {"quantity": "Float64", "price": "Float64", "channel": "String", "day": "Date", "shop": "String",
           "w": "Float64", "pred_fitted": "Float64", "flag": "Boolean", "n": "Int64", "t": "Datetime(time_unit='us', time_zone=None)",
           "blob": "Binary"}


def test_the_snapshot_is_what_the_catalogue_says():
    written = catalog.SNAPSHOT.read_text(encoding="utf-8")
    assert written == catalog.snapshot_text(), (
        "app/engine/models/catalog.json is out of date: run `python -m app.engine.models.catalog`, "
        "then `python3 .github/scripts/model_catalog.py` for its copies"
    )


def test_groups_and_models_point_at_each_other():
    snap = catalog.snapshot()
    assert [g["id"] for g in snap["groups"]] == ["regression", "survival", "timeseries", "boosting"]
    assert [m["id"] for m in snap["models"]] == ["linear", "logistic", "kaplan_meier", "cox", "arima", "xgboost", "catboost"]
    listed = [m for g in catalog.GROUPS.values() for m in g["models"]]
    assert sorted(listed) == sorted(catalog.MODELS) and len(listed) == len(set(listed))
    for model, spec in catalog.MODELS.items():
        assert model in catalog.GROUPS[spec["group"]]["models"], model
        assert catalog.GROUPS[spec["group"]]["language"] == ("python" if spec["group"] == "boosting" else "r")
    # Predict's columns and options name a model, or `model:variant` (the card's variant)
    variants = {f"{m}:{v['id']}" for m, spec in catalog.MODELS.items() for v in spec["variants"]}
    keyed = set(catalog.PREDICT["columns"])
    assert keyed <= set(catalog.MODELS) | variants
    for model, spec in catalog.MODELS.items():
        assert model in keyed or all(f"{model}:{v['id']}" in keyed for v in spec["variants"]), model
    for key, models in catalog.PREDICT["applies_to"].items():
        assert key in catalog.PREDICT["takes"] and set(models) <= set(catalog.MODELS) | variants, key


def test_the_boosting_nodes_take_the_same_keys_but_for_their_own_hyperparameters():
    xgb, cat = catalog.MODELS["xgboost"], catalog.MODELS["catboost"]
    own = {"xgboost": ["min_child_weight", "colsample_bytree", "reg_lambda", "reg_alpha", "gamma"],
           "catboost": ["l2_leaf_reg", "rsm", "random_strength"]}
    shared = [k for k in xgb["takes"] if k not in own["xgboost"]]
    assert shared == [k for k in cat["takes"] if k not in own["catboost"]]
    assert list(xgb["needs"]) == ["task", "outcome", "predictors"] and list(xgb["takes"])[-2:] == ["seed", "output"]
    assert xgb["takes"]["max_depth"] == "depth_xgboost" and cat["takes"]["max_depth"] == "depth_catboost"
    for spec in (xgb, cat):
        assert [v["id"] for v in spec["variants"]] == ["regression", "binary", "multiclass"]
        for v in spec["variants"]:
            assert {"importance", "assumption"} <= set(v["sections"])
            assert ("cell" in v["sections"]) == (v["id"] != "regression") and ("class" in v["sections"]) == (v["id"] == "multiclass")
    base = {"outcome": "y", "predictors": ["x"]}
    assert catalog.check_params("xgboost", {**base, "task": "regression"})["max_depth"] == 6
    binary = catalog.check_params("catboost", {**base, "task": "binary", "event_value": "yes"})
    assert binary["threshold"] == 0.5 and binary["stratify"] is True and "order_by" not in binary
    assert "early_stopping_rounds" not in catalog.check_params("xgboost", {**base, "task": "regression", "early_stopping": False})
    for limit in ("max_predictors", "max_levels_trees", "max_classes", "max_cells", "max_shap_rows", "shap_cost_budget",
                  "max_model_file_bytes", "max_importance_bars", "max_beeswarm_predictors", "max_roc_classes"):
        assert catalog.LIMITS[limit] > 0, limit


def test_every_kind_has_a_control_and_only_the_control_s_options():
    options = {
        "column": {"numeric", "types"}, "columns": {"min", "max", "numeric", "predictor"}, "level": {"of"},
        "reference": {"of"}, "pairs": {"of", "max"}, "choice": {"choices"},
        "number": {"above", "below", "min", "max"}, "integer": {"min", "max"}, "boolean": set(),
        "numbers": {"above", "below", "min", "max", "max_count"},
        "number_or_choice": {"choices", "above", "below", "min", "max"},
    }
    assert set(options) == set(catalog.CONTROLS)
    for kind, how in catalog.KINDS.items():
        assert how["control"] in catalog.CONTROLS, kind
        assert set(how) - {"control"} <= options[how["control"]], kind
        for t in how.get("types", ()):
            assert t in catalog.COLUMN_KINDS, kind
    used = {how["control"] for how in catalog.KINDS.values()}
    assert used == set(catalog.CONTROLS)


def test_every_model_s_keys_defaults_and_rules_are_consistent():
    for model, spec in catalog.MODELS.items():
        kinds = {**spec["needs"], **spec["takes"]}
        assert not set(spec["needs"]) & set(spec["takes"]), model
        assert set(spec["defaults"]) == set(spec["takes"]), model
        for key, kind in kinds.items():
            assert kind in catalog.KINDS, (model, key)
            of = catalog.KINDS[kind].get("of")
            if of:
                assert of in kinds, (model, key, of)
        for key, rule in spec["applies"].items():
            assert key in spec["takes"] and set(rule) <= set(kinds) and key not in rule, (model, key)
        for rule in spec["requires"] + spec["forbids"]:
            assert rule["key"] in kinds and set(rule["when"]) <= set(kinds), (model, rule)
            assert rule["reason"].isidentifier() and rule["message"], (model, rule)
        for key in spec["disjoint"]:
            assert catalog.KINDS[kinds[key]]["control"] in ("column", "columns"), (model, key)
        # every default is a value the check accepts
        for key, value in spec["defaults"].items():
            if value is not None and value != [] and value != {}:
                assert catalog.read_value(model, key, kinds[key], value) == value, (model, key)
        assert set(spec.get("presets", {})) <= {"ar", "ma", "arma", "arima", "sarima", "sarimax"}


def test_every_combination_of_the_keys_that_make_variants_has_one():
    for model, spec in catalog.MODELS.items():
        keys = sorted({k for v in spec["variants"] for k in v["when"]})
        choices = [catalog.KINDS[{**spec["needs"], **spec["takes"]}[k]]["choices"] for k in keys]
        ids = [v["id"] for v in spec["variants"]]
        assert len(ids) == len(set(ids)), model
        for combo in itertools.product(*choices):
            assert catalog.variant(model, dict(zip(keys, combo))) in ids, (model, combo)


def test_every_variant_declares_known_sections_and_the_required_rows():
    for model, spec in catalog.MODELS.items():
        for v in spec["variants"]:
            assert set(v["sections"]) <= set(catalog.SECTION_SHAPES), (model, v["id"])
            assert {"model", "n"} <= {"model", *v["sections"]["model"]["numbers"]}, (model, v["id"])
            assert {"model", "variant"} <= set(v["sections"]["model"]["texts"]), (model, v["id"])
            for section, decl in v["sections"].items():
                assert set(decl) == {"levels", "numbers", "texts"}, (model, v["id"], section)
                assert not set(decl["numbers"]) & set(decl["texts"]), (model, v["id"], section)
            checks = set(v["sections"].get("assumption", {}).get("levels") or [])
            for chart in v["charts"]:
                assert chart["under"] is None or chart["under"] in checks, (model, v["id"], chart)
            assert v["outputs"], (model, v["id"])


def test_penalized_variants_write_no_inference():
    for model in ("linear", "logistic"):
        pen = catalog.variant_spec(model, {"penalty": "lasso"})["sections"]
        assert not {"std_error", "p_value", "ci_low", "ci_high", "statistic"} & set(pen["coef"]["numbers"])
        assert "cv" in pen
        unpen = catalog.variant_spec(model, {"penalty": "none"})["sections"]
        assert "cv" not in unpen and "p_value" in unpen["coef"]["numbers"]


@pytest.mark.parametrize("case", json.loads(CASES.read_text(encoding="utf-8"))["cases"], ids=lambda c: c["what"])
def test_the_shared_cases(case):
    if "error" in case:
        with pytest.raises(EngineError) as e:
            check_params(case["model"], case["params"])
        assert str(e.value) == case["error"]
    else:
        assert check_params(case["model"], case["params"]) == case["checked"]


def _twice(what, model, params):
    """check(check(x)) == check(x), in the shape and in the values (an ARIMA was once refused for
    its own default). A combination the check refuses has nothing to repeat."""
    try:
        first = check_params(model, params)
    except EngineError:
        return
    second = check_params(model, first)
    assert json.dumps(second, sort_keys=True) == json.dumps(first, sort_keys=True), (what, model)


def _sample(model):
    """The parameters a model needs, a column or a level each."""
    spec = catalog.MODELS[model]
    out = {}
    for i, (key, kind) in enumerate(spec["needs"].items()):
        control = catalog.KINDS[kind]["control"]
        out[key] = (f"col{i}" if control == "column" else [f"col{i}"] if control == "columns"
                    else "yes" if control == "level" else catalog.KINDS[kind]["choices"][0])
    return out


def test_checking_twice_is_checking_once():
    for model, spec in catalog.MODELS.items():
        base = _sample(model)
        _twice("the defaults", model, base)
        picks = []
        for key, kind in spec["takes"].items():
            control = catalog.KINDS[kind]["control"]
            if control == "choice":
                picks += [(key, c) for c in catalog.KINDS[kind]["choices"]]
            elif control == "boolean":
                picks += [(key, True), (key, False)]
        for i, a in enumerate(picks):
            _twice(a, model, {**base, a[0]: a[1]})
            for b in picks[i + 1:]:
                if a[0] != b[0]:
                    _twice((a, b), model, {**base, a[0]: a[1], b[0]: b[1]})


@pytest.mark.parametrize("case", [c for c in json.loads(CASES.read_text(encoding="utf-8"))["cases"] if "checked" in c],
                         ids=lambda c: c["what"])
def test_every_shared_case_that_passes_is_its_own_answer_too(case):
    _twice(case["what"], case["model"], case["params"])
    assert check_params(case["model"], case["checked"]) == case["checked"]
    assert json.dumps(check_params(case["model"], case["checked"]), sort_keys=True) == json.dumps(case["checked"], sort_keys=True)


def test_an_empty_list_of_columns_stays_an_empty_list():
    out = check_params("arima", {"time": "t", "value": "v", "frequency": "month", "exog": []})
    assert out["exog"] == [] and out["exog"] is not None


def test_the_shared_cases_cover_every_control_and_model():
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    assert {c["model"] for c in cases} >= set(catalog.MODELS)
    refused = " ".join(c.get("error", "") for c in cases)
    for sentence in ("must be one of", "must be true or false", "whole number", "must be a number", "only applies",
                     "is required", "there is no parameter", "names a column twice", "is both"):
        assert sentence in refused, sentence


def test_the_columns_are_checked_when_their_types_are_known():
    base = {"outcome": "quantity", "predictors": ["price", "channel"]}
    assert check_params("linear", base, COLUMNS)["outcome"] == "quantity"
    with pytest.raises(EngineError, match="«nope» \\('outcome'\\) is not in the step's input"):
        check_params("linear", {**base, "outcome": "nope"}, COLUMNS)
    with pytest.raises(EngineError, match="«channel» \\('outcome'\\) is not a number"):
        check_params("linear", {"outcome": "channel", "predictors": ["price"]}, COLUMNS)
    with pytest.raises(EngineError, match="is a date and cannot be a predictor: compute a number from the date first"):
        check_params("linear", {**base, "predictors": ["price", "day"]}, COLUMNS)
    with pytest.raises(EngineError, match="cannot be a predictor: numbers, texts and true or false can"):
        check_params("linear", {**base, "predictors": ["blob"]}, COLUMNS)
    assert check_params("linear", {**base, "predictors": ["flag", "n"]}, COLUMNS)
    with pytest.raises(EngineError, match="the input already has a column «pred_fitted»"):
        check_params("linear", {**base, "output": "rows"}, COLUMNS)
    arima = {"value": "quantity", "frequency": "day"}
    for ok in ("day", "t", "n"):
        assert check_params("arima", {**arima, "time": ok}, COLUMNS)["time"] == ok
    with pytest.raises(EngineError, match="must be a date or a datetime or a integer"):
        check_params("arima", {**arima, "time": "price"}, COLUMNS)
    # a key that does not apply names no column that must exist
    assert check_params("linear", {**base, "penalty": "lasso"}, COLUMNS)["penalty"] == "lasso"


def test_column_kinds_from_the_engine_s_types():
    for dtype, kind in (("Int64", "integer"), ("UInt8", "integer"), ("Float32", "number"), ("Decimal(10, 2)", "number"),
                        ("String", "text"), ("Utf8", "text"), ("Boolean", "boolean"), ("Date", "date"),
                        ("Datetime(time_unit='ns', time_zone='UTC')", "datetime"), ("Binary", "other")):
        assert catalog.column_kind(dtype) == kind, dtype


def test_the_name_an_arima_node_s_orders_make():
    name = catalog.arima_name
    assert name({"p": 2, "d": 0, "q": 0}) == "AR(2)"
    assert name({"p": 0, "d": 0, "q": 1}) == "MA(1)"
    assert name({"p": 1, "d": 0, "q": 1}) == "ARMA(1,1)"
    assert name({"p": 1, "d": 1, "q": 1}) == "ARIMA(1,1,1)"
    assert name({"frequency": "month", "p": 1, "d": 1, "q": 1, "D": 1, "Q": 1}) == "SARIMA(1,1,1)(0,1,1)[12]"
    assert name({"s": 4, "p": 1, "d": 1, "q": 1, "D": 1, "exog": ["promo"]}) == "SARIMAX(1,1,1)(0,1,0)[4]"
    assert name({"p": 1, "d": 1, "q": 0, "exog": ["promo"]}) == "ARIMAX(1,1,0)"
    # a seasonal order on a series without a season is no season
    assert name({"frequency": "year", "p": 1, "P": 1}) == "AR(1)"
    assert name({"orders": "auto"}) == "ARIMA (automatic orders)"
    for preset, params in catalog.MODELS["arima"]["presets"].items():
        made = name({**params, "frequency": "month", "exog": ["x"] if preset == "sarimax" else []})
        assert made.lower().startswith(preset + "(") or (preset == "arima" and made.startswith("ARIMA(")), (preset, made)
