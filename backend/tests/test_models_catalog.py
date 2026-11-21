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
    assert [g["id"] for g in snap["groups"]] == ["regression", "survival", "timeseries", "boosting", "customer_lifetime", "experiments"]
    assert [m["id"] for m in snap["models"]] == ["linear", "logistic", "kaplan_meier", "cox", "parametric_survival", "arima", "decomposition", "xgboost", "catboost", "clv", "bg_bb", "ab_test", "ab_plan"]
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
        # a model that makes no card (`card: false`) has nothing to predict with
        assert spec.get("card", True) is False or model in keyed or all(f"{model}:{v['id']}" in keyed for v in spec["variants"]), model
        assert spec.get("card", True) is not False or not (model in keyed or any(f"{model}:{v['id']}" in keyed for v in spec["variants"])), model
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


def test_the_parametric_survival_node_and_its_four_variants():
    spec = catalog.MODELS["parametric_survival"]
    assert catalog.GROUPS["survival"]["models"] == ["kaplan_meier", "cox", "parametric_survival"]
    assert list(spec["needs"]) == ["time", "event", "event_value"] and list(spec["takes"])[-2:] == ["seed", "output"]
    assert spec["takes"]["predictors"] == "predictors_optional" and catalog.KINDS["predictors_optional"]["min"] == 0
    assert catalog.LIMITS["max_quantiles"] == 9 and catalog.KINDS["quantiles"]["max_count"] == 9
    by = {d: catalog.variant("parametric_survival", {"distribution": d}) for d in catalog.KINDS["distribution"]["choices"]}
    assert by == {"weibull": "ph_aft", "exponential": "ph_aft", "lognormal": "aft", "loglogistic": "aft", "gengamma": "aft",
                  "gompertz": "ph", "auto": "auto"}
    sections = {v["id"]: v["sections"] for v in spec["variants"]}
    coef = {v: set(s["coef"]["numbers"]) for v, s in sections.items()}
    assert {"time_ratio", "hazard_ratio", "cox_hazard_ratio"} <= coef["ph_aft"] <= coef["auto"]
    assert "hazard_ratio" not in coef["aft"] and "time_ratio" not in coef["ph"]  # Gompertz has no time ratio
    checks = {v: set(s["assumption"]["levels"]) for v, s in sections.items()}
    assert "ph_cox" not in checks["aft"] and "constant_hazard" not in checks["ph"] and "distribution_vs_gengamma" not in checks["ph"]
    assert checks["auto"] == checks["ph_aft"] | checks["aft"] | checks["ph"]
    assert [v for v, s in sections.items() if "candidate" in s] == ["auto"]
    assert all("parameter" in s and "advice_id" in s["assumption"]["texts"] for s in sections.values())
    # a parametric model is predicted beyond the data: the median, the quantiles, S(t), the interval
    assert set(catalog.PREDICT["columns"]["parametric_survival"]) == {"always", "quantiles", "at_times", "interval", "quantiles+interval",
                                                                         "at_times+interval"}
    assert all("parametric_survival" in catalog.PREDICT["applies_to"][k] for k in ("quantiles", "at_times", "interval"))
    base = {"time": "days", "event": "status", "event_value": "failed"}
    assert catalog.check_params("parametric_survival", base)["quantiles"] == [0.25, 0.75]
    assert "reference" not in catalog.check_params("parametric_survival", base)  # no predictor, no reference level
    assert catalog.check_params("parametric_survival", {**base, "predictors": ["x"]})["reference"] == {}


def test_the_customer_lifetime_nodes_and_their_two_variants():
    assert catalog.GROUPS["customer_lifetime"] == {"language": "r", "label": "models.groups.customer_lifetime",
                                                   "models": ["clv", "bg_bb"]}
    assert catalog.SECTION_SHAPES["customer"] == {"level": "required", "level_2": "null", "indexed": False}
    for key, value in {"min_customers": 20, "min_repeaters": 5, "max_opportunities": 1000, "max_clv_steps": 520,
                       "max_top_customers": 20, "palive_grid_frequency": 10}.items():
        assert catalog.LIMITS[key] == value, key
    clv, bgbb = catalog.MODELS["clv"], catalog.MODELS["bg_bb"]
    assert list(clv["needs"]) == ["customer", "frequency", "recency", "age", "time_unit"]
    assert list(bgbb["needs"]) == ["customer", "frequency", "recency", "opportunities", "period_unit"]
    for spec in (clv, bgbb):
        assert list(spec["takes"])[-3:] == ["alpha", "seed", "output"]
        assert spec["defaults"]["output"] == "rows" and spec["defaults"]["discount_rate"] == 0.1 and spec["defaults"]["margin"] == 1.0
        assert spec["defaults"]["horizon"] is None and spec["defaults"]["spend"] is None
        assert [v["id"] for v in spec["variants"]] == ["value", "purchases"]
    assert list(clv["takes"])[:3] == ["spend", "purchase_model", "compare"] and clv["defaults"]["purchase_model"] == "auto"
    assert "purchase_model" not in bgbb["takes"] and "compare" not in bgbb["takes"]
    assert catalog.KINDS["holdout_opportunities"] == {"control": "integer", "min": 1, "max": 1000}
    base = {"customer": "id", "frequency": "x", "recency": "t_x", "age": "T", "time_unit": "week"}
    assert catalog.variant("clv", base) == "purchases" and catalog.variant("clv", {**base, "spend": "m"}) == "value"
    assert catalog.variant("bg_bb", {"spend": "m"}) == "value" and catalog.variant("bg_bb", {}) == "purchases"
    # the spend part's keys apply with a spend column only; `compare` only with a chosen purchase model
    assert "discount_rate" not in catalog.check_params("clv", base) and catalog.check_params("clv", {**base, "spend": "m"})["margin"] == 1.0
    with pytest.raises(EngineError, match="'margin' only applies when 'spend' is given"):
        catalog.check_params("clv", {**base, "margin": 0.5})
    with pytest.raises(EngineError, match="'compare' only applies when purchase_model = pareto_nbd or bg_nbd or mbg_nbd"):
        catalog.check_params("clv", {**base, "compare": True})
    assert catalog.check_params("clv", {**base, "purchase_model": "bg_nbd", "compare": True})["compare"] is True
    # the holdout is a pair
    with pytest.raises(EngineError, match="the holdout's purchases need the holdout's length"):
        catalog.check_params("clv", {**base, "holdout_transactions": "h"})
    with pytest.raises(EngineError, match="'holdout_length' only applies when 'holdout_transactions' is given"):
        catalog.check_params("clv", {**base, "holdout_length": 26})
    assert catalog.check_params("clv", {**base, "holdout_transactions": "h", "holdout_length": 26.5})["holdout_length"] == 26.5
    with pytest.raises(EngineError, match="whole number from 1 to 1000"):
        catalog.check_params("bg_bb", {"customer": "id", "frequency": "x", "recency": "t_x", "opportunities": "n",
                                       "period_unit": "year", "holdout_transactions": "h", "holdout_length": 26.5})
    with pytest.raises(EngineError, match="both 'frequency' and 'recency'"):
        catalog.check_params("clv", {**base, "recency": "x"})
    # what Predict says of them
    assert catalog.PREDICT["applies_to"]["horizon"] == ["arima", "clv", "bg_bb"]
    for model in ("clv", "bg_bb"):
        assert catalog.PREDICT["columns"][f"{model}:purchases"] == {"always": ["prediction", "prediction_p_alive"]}
        assert catalog.PREDICT["columns"][f"{model}:value"]["always"][-2:] == ["prediction_spend", "prediction_clv"]
    # a variant declares what the runner may write: the comparison of purchase models is `clv`'s alone
    sections = {(m, v["id"]): v["sections"] for m in ("clv", "bg_bb") for v in catalog.MODELS[m]["variants"]}
    assert {k for k, s in sections.items() if "candidate" in s} == {("clv", "value"), ("clv", "purchases")}
    assert all("point" in s for k, s in sections.items() if k[1] == "value") and not any("point" in s for k, s in sections.items() if k[1] == "purchases")
    assert all("spend_independence" in s["assumption"]["levels"] for k, s in sections.items() if k[1] == "value")
    assert not any("spend_independence" in s["assumption"]["levels"] for k, s in sections.items() if k[1] == "purchases")


def test_the_experiments_group_and_its_two_nodes():
    assert catalog.GROUPS["experiments"] == {"language": "r", "label": "models.groups.experiments", "models": ["ab_test", "ab_plan"]}
    for key, value in {"max_variants": 20, "max_looks": 1000, "max_secondary_metrics": 10, "posterior_points": 200,
                       "power_points": 50}.items():
        assert catalog.LIMITS[key] == value, key
    test, plan = catalog.MODELS["ab_test"], catalog.MODELS["ab_plan"]
    # neither makes a model: no card, so no registration and no Predict
    assert test["card"] is False and plan["card"] is False
    assert all(spec.get("card", True) for m, spec in catalog.MODELS.items() if m not in ("ab_test", "ab_plan", "decomposition"))
    snap = {m["id"]: m for m in catalog.snapshot()["models"]}
    assert [snap[m]["card"] for m in ("linear", "clv", "ab_test", "ab_plan")] == [True, True, False, False]
    assert not any(k.startswith(("ab_test", "ab_plan")) for k in catalog.PREDICT["columns"])
    assert list(test["needs"]) == ["variant", "control", "metric_type"] and list(plan["needs"]) == ["metric_type"]
    assert test["takes"]["expected_shares"] == "variant_shares" and catalog.KINDS["variant_shares"] == {
        "control": "level_numbers", "of": "variant", "above": 0}
    assert catalog.KINDS["control_level"] == {"control": "level", "of": "variant"}
    # one primary metric and no random number: no seed
    assert "seed" not in test["takes"] and "seed" not in plan["takes"]
    # the defaults of the margins are the spec's, all visible in the form
    d = test["defaults"]
    assert (d["alpha"], d["srm_alpha"], d["min_lift"], d["power"], d["loss_threshold"], d["sequential_tau"], d["prior_alpha"],
            d["prior_beta"], d["correction"], d["covariate_missing"]) == (0.05, 0.001, 0.02, 0.8, 0.001, 0.05, 1.0, 1.0, "holm", "fill")
    for node in ("ab_test", "ab_plan"):
        assert [v["id"] for v in catalog.MODELS[node]["variants"]] == ["binary", "continuous", "ratio"]
        for t in ("binary", "continuous", "ratio"):
            assert catalog.variant(node, {"metric_type": t}) == t
    # the sections a variant may write: the report's explanation in every one, Welch's df and the cap in the continuous one
    for v in test["variants"]:
        s = v["sections"]
        assert {"model", "variant", "comparison", "bayes", "sequential", "secondary", "reading", "assumption", "curve"} <= set(s)
        assert s["reading"]["texts"] == ["line_*"] and "decision" in s["reading"]["levels"] and "formulas" in s["reading"]["levels"]
        assert ("df" in s["comparison"]["numbers"]) == (v["id"] == "continuous") and ("hist" in s) == (v["id"] == "continuous")
        assert ("cap" in s["reading"]["levels"]) == (v["id"] == "continuous")
        assert "covariate_fill_value" in s["variant"]["numbers"]  # the fill value is per variant (decision 14)
        assert set(s["assumption"]["levels"]) == {"srm", "covariate_balance", "covariate_missing_balance", "crossover", "variance_zero"}
        assert list(v["outputs"]) == ["variants"]
    assert all("assumption" not in v["sections"] for v in plan["variants"])  # nothing in a plan is a test
    assert all(list(v["outputs"]) == ["plan"] for v in plan["variants"])
    for name in ("variant", "comparison", "bayes", "sequential", "secondary", "plan", "reading"):
        assert name in catalog.SECTION_SHAPES
    assert catalog.SECTION_SHAPES["comparison"] == {"level": "required", "level_2": "required", "indexed": False}
    assert catalog.SECTION_SHAPES["reading"] == {"level": "required", "level_2": "optional", "indexed": False}


def test_the_experiments_nodes_rules():
    base = {"variant": "arm", "control": "A", "metric_type": "binary", "metric": "converted"}
    assert catalog.check_params("ab_test", base)["srm_alpha"] == 0.001
    for params, reason in (
            ({"variant": "arm", "control": "A", "metric_type": "ratio", "input_shape": "aggregated", "units_count": "u", "metric_sum": "s"},
             "one row per unit"),
            ({**base, "sequential": True, "alternative": "less"}, "always-valid p-value of this node is two-sided"),
            ({"variant": "arm", "control": "A", "metric_type": "continuous", "input_shape": "aggregated", "units_count": "u",
              "metric_sum": "s"}, "needs the sum of its squares")):
        with pytest.raises(EngineError, match=reason):
            catalog.check_params("ab_test", params)
    # the shares are one number for each value, the column's values are the data's: read at the run
    assert catalog.check_params("ab_test", {**base, "expected_shares": {"B": 1, "A": 3}})["expected_shares"] == {"A": 3.0, "B": 1.0}
    assert "expected_shares" in catalog.check_params("ab_test", {**base, "expected_shares": {}})
    with pytest.raises(EngineError, match="'expected_shares.B' must be a number above 0"):
        catalog.check_params("ab_test", {**base, "expected_shares": {"A": 1, "B": -1}})
    # the planning node
    plan = {"metric_type": "continuous", "metric": "m", "date": "d"}
    assert catalog.check_params("ab_plan", plan)["arms"] == 2 and "traffic_share" in catalog.check_params("ab_plan", plan)
    assert "traffic_share" not in catalog.check_params("ab_plan", {"metric_type": "binary", "metric": "m"})
    with pytest.raises(EngineError, match="needs the column of the day each unit first appeared"):
        catalog.check_params("ab_plan", {"metric_type": "binary", "metric": "m", "solve_for": "mde_for_days", "days": 7})
    # the columns' types: the variant a text, a number, a boolean or a date; the metric a boolean or a number
    columns = {"arm": "String", "converted": "Boolean", "d": "Date", "dt": "Datetime(time_unit='us', time_zone=None)", "label": "String"}
    assert catalog.check_params("ab_test", {**base, "date": "d"}, columns)["date"] == "d"
    assert catalog.check_params("ab_test", {**base, "date": "dt"}, columns)["date"] == "dt"
    with pytest.raises(EngineError, match="«label» \\('metric'\\) must be a boolean or a integer or a number"):
        catalog.check_params("ab_test", {**base, "metric": "label"}, columns)
    with pytest.raises(EngineError, match="«label» \\('date'\\) must be a date or a datetime"):
        catalog.check_params("ab_test", {**base, "date": "label"}, columns)
    with pytest.raises(EngineError, match="«converted» \\('variant'\\) must be a text or a integer or a boolean or a date"):
        catalog.check_params("ab_test", {**base, "variant": "converted", "metric": "d", "metric_type": "continuous"},
                             {**columns, "d": "Float64", "converted": "Float64"})
    with pytest.raises(EngineError, match="«label» \\('secondary_metrics'\\) must be a boolean or a integer or a number"):
        catalog.check_params("ab_test", {**base, "secondary_metrics": ["label"]}, columns)
    # the output «variants» is the same list of columns whichever metric: the variant of the metric type is not needed to know it
    assert catalog.output_columns("ab_test", {"output": "variants"}, [])[0] == catalog.output_columns(
        "ab_test", {"metric_type": "ratio", "output": "variants"}, [])[0]


def test_the_decomposition_node_and_its_four_variants():
    """docs/specs/ts-decomposition.md, «The node `decomposition`»: one node of the group `timeseries`, four variants keyed on
    `method`, no card, the limits of the form, the rules between the options."""
    assert catalog.GROUPS["timeseries"]["models"] == ["arima", "decomposition"]
    for key, value in {"max_period": 10_000, "max_periods": 4, "max_imfs": 16, "max_lb_lags": 1000}.items():
        assert catalog.LIMITS[key] == value, key
    spec = catalog.MODELS["decomposition"]
    assert spec["card"] is False and spec["group"] == "timeseries" and spec["family"] == "timeseries"
    assert list(spec["needs"]) == ["time", "value", "frequency"] and "seed" not in spec["takes"]  # nothing is random
    assert not any(k.startswith("decomposition") for k in catalog.PREDICT["columns"])
    assert [v["id"] for v in spec["variants"]] == ["stl", "classical", "mstl", "emd"]
    for m in ("stl", "classical", "mstl", "emd"):
        assert catalog.variant("decomposition", {"method": m}) == m
    d = spec["defaults"]
    assert (d["method"], d["s_window"], d["t_window"], d["iterate"], d["max_imf"], d["s_number"], d["max_siftings"], d["noise_imfs"],
            d["missing"], d["seasonality_test"], d["lb_lag"], d["alpha"], d["output"]) == (
        "stl", 11.0, "auto", 2, 10, 4, 50, 0, "refuse", "friedman", "auto", 0.05, "components")
    # the ARIMA node keeps a limit of its own below the group's
    assert catalog.MODELS["arima"]["max_rows"] == 20_000
    assert all("max_rows" not in s for m, s in catalog.MODELS.items() if m != "arima")
    snap = {m["id"]: m for m in catalog.snapshot()["models"]}
    assert snap["arima"]["max_rows"] == 20_000 and snap["decomposition"]["max_rows"] is None and snap["decomposition"]["card"] is False
    # the sections: a season and the checks in every variant, the IMFs in EMD alone, its facts and the robust weights
    for v in spec["variants"]:
        s = v["sections"]
        emd = v["id"] == "emd"
        assert {"model", "season", "assumption", "curve", "point", "lag", "hist", "reading"} <= set(s)
        assert ("imf" in s) == emd and ("imfs" in s["reading"]["levels"]) == emd and ("imfs" in s["curve"]["levels"]) == emd
        assert {"seasonality", "ljung_box", "adf", "kpss", "trend_strength", "seasonal_strength", "largest_acf"} <= set(s["assumption"]["levels"])
        assert ({"imf_condition", "residue_monotonic", "orthogonality"} <= set(s["assumption"]["levels"])) == emd
        assert ("robust_weights" in s["assumption"]["levels"]) == (v["id"] in ("stl", "mstl"))
        assert s["reading"]["texts"] == ["line_*"] and {"summary", "method", "formulas"} <= set(s["reading"]["levels"])
        assert ("t_window_used" in s["model"]["numbers"]) == (v["id"] in ("stl", "mstl"))
        assert ("type" in s["model"]["texts"]) == (v["id"] == "classical") and ("boundary" in s["model"]["texts"]) == emd
        assert list(v["outputs"]) == ["components"] and v["rows_when"] == {"transformed": {"transform": ["log", "box_cox"]}}
    for name in ("season", "imf"):
        assert catalog.SECTION_SHAPES[name] == {"level": "required", "level_2": "null", "indexed": False}
    # the rules between the options
    base = {"time": "t", "value": "v", "frequency": "day"}
    for params, reason in (
            ({**base, "frequency": "year"}, "a yearly or index series has no season of its own"),
            ({**base, "frequency": "index", "method": "classical"}, "give the period, in steps"),
            ({**base, "method": "mstl"}, "needs its seasonal periods"),
            ({**base, "method": "classical", "type": "multiplicative", "transform": "log"}, "already works in ratios"),
            ({**base, "method": "classical", "type": "multiplicative", "transform": "box_cox"}, "already works in ratios"),
            ({**base, "method": "mstl", "periods": [7], "period": 7}, "'period' only applies when method = stl or classical or emd"),
            ({**base, "max_imf": 3}, "'max_imf' only applies when method = emd"),
            ({**base, "period": 1}, "whole number from 2 to 10000"),
            ({**base, "method": "mstl", "periods": [7, 30, 90, 365, 730]}, "at most 4 numbers")):
        with pytest.raises(EngineError, match=reason):
            catalog.check_params("decomposition", params)
    assert catalog.check_params("decomposition", {**base, "frequency": "year", "method": "emd"})["max_imf"] == 10
    assert catalog.check_params("decomposition", {**base, "method": "mstl", "periods": [365, 7]})["periods"] == [7.0, 365.0]
    assert "period" not in catalog.check_params("decomposition", {**base, "method": "mstl", "periods": [7]})
    # a multiplicative decomposition with no transform is fine
    assert catalog.check_params("decomposition", {**base, "method": "classical", "type": "multiplicative"})["transform"] == "none"


def test_the_decomposition_s_periods_windows_and_lag_are_whole_numbers_declared_by_the_catalogue():
    """Lot D9: the keys counted in steps say so in the catalogue (`whole`), which the engine, the Go gateway and the editor
    read: a 7.5 is refused before the Job, and a window has an upper bound that fits the runner's integers."""
    whole = {k for k, how in catalog.KINDS.items() if how.get("whole")}
    assert whole == {"periods", "s_windows", "s_window", "t_window", "lb_lag"}
    assert all(catalog.KINDS[k]["control"] in ("numbers", "number_or_choice") for k in whole)
    for k in ("s_window", "s_windows", "t_window"):
        assert catalog.KINDS[k]["max"] == catalog.LIMITS["max_window"] < 2 ** 31
    base = {"time": "m", "value": "v", "frequency": "month"}
    for params in ({**base, "s_window": 7.5}, {**base, "t_window": 21.5}, {**base, "lb_lag": 7.5}, {**base, "s_window": 10 ** 400},
                   {**base, "method": "mstl", "periods": [7, 12.5]}, {**base, "method": "mstl", "periods": [7], "s_windows": [9.5]}):
        with pytest.raises(EngineError, match="whole number"):
            catalog.check_params("decomposition", params)
    assert catalog.check_params("decomposition", {**base, "s_window": 13.0, "lb_lag": 24})["s_window"] == 13.0
    # a list too long is refused for its length, before any of its numbers is read
    with pytest.raises(EngineError, match="at most 4 numbers"):
        catalog.check_params("decomposition", {**base, "method": "mstl", "periods": [object()] * 1_000_000})


def test_an_integer_of_any_length_is_refused_as_a_number_never_an_overflow():
    """JSON's integers have no bound: a parameter of 400 digits is a refusal with its sentence, not an OverflowError."""
    for params in ({"outcome": "y", "predictors": ["x"], "alpha": 10 ** 400}, {"outcome": "y", "predictors": ["x"], "folds": -10 ** 400}):
        with pytest.raises(EngineError, match="must be"):
            catalog.check_params("linear", params)


def test_every_kind_has_a_control_and_only_the_control_s_options():
    options = {
        "column": {"numeric", "types"}, "columns": {"min", "max", "numeric", "types", "predictor"}, "level": {"of"},
        "reference": {"of"}, "pairs": {"of", "max"}, "choice": {"choices"},
        "number": {"above", "below", "min", "max"}, "integer": {"min", "max"}, "boolean": set(),
        "numbers": {"above", "below", "min", "max", "max_count", "whole"},
        "number_or_choice": {"choices", "above", "below", "min", "max", "whole"},
        "level_numbers": {"of", "above", "below", "min", "max"}, "column_choices": {"of", "choices"},
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
        # a key that makes a variant by being given or not (a column) has two cases
        choices = [catalog.KINDS[{**spec["needs"], **spec["takes"]}[k]].get("choices", [None, "x"]) for k in keys]
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


def test_a_rows_column_the_runner_writes_only_with_a_holdout_clashes_only_with_one():
    """`pred_holdout_transactions` is written only with a holdout (the variant's `rows_when`): without one an
    input column of that name is no clash."""
    columns = {"id": "String", "x": "Int64", "tx": "Float64", "T": "Float64", "later": "Int64",
               "pred_holdout_transactions": "Float64"}
    base = {"customer": "id", "frequency": "x", "recency": "tx", "age": "T", "time_unit": "week"}
    assert check_params("clv", base, columns)["output"] == "rows"
    with pytest.raises(EngineError, match="already has a column «pred_holdout_transactions»"):
        check_params("clv", {**base, "holdout_transactions": "later", "holdout_length": 26}, columns)
    with pytest.raises(EngineError, match="already has a column «pred_p_alive»"):
        check_params("clv", base, {**columns, "pred_p_alive": "Float64"})
    for v in catalog.MODELS["clv"]["variants"] + catalog.MODELS["bg_bb"]["variants"]:
        assert set(v["rows_when"]) <= set(v["outputs"]["rows"]), v["id"]


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


def test_conditional_survival_is_a_column_of_the_parametric_node_and_of_its_predict():
    # alive_at: the time each unit has already survived, a number column; the answers are about the time left
    spec = catalog.MODELS["parametric_survival"]
    assert spec["takes"]["alive_at"] == "number_column" and spec["defaults"]["alive_at"] is None
    assert catalog.PREDICT["takes"]["alive_at"] == "number_column"
    assert catalog.PREDICT["applies_to"]["alive_at"] == ["parametric_survival"]  # Kaplan–Meier's and Cox's curves stop
    base = {"time": "days", "event": "status", "event_value": "failed"}
    assert catalog.check_params("parametric_survival", base)["alive_at"] is None
    assert catalog.check_params("parametric_survival", {**base, "alive_at": "age_days"})["alive_at"] == "age_days"
    # the columns Predict adds are the same ones: they now mean the time left
    assert "alive_at" not in catalog.PREDICT["columns"]["parametric_survival"]


def test_the_intervals_of_the_quantiles_and_the_times_are_columns_of_predict_and_of_the_rows_output():
    # a key `a+b` is given when both options are; its `*` is one per entry of `a`
    columns = catalog.PREDICT["columns"]["parametric_survival"]
    assert columns["quantiles+interval"] == ["quantile_*_low", "quantile_*_high"]
    assert columns["at_times+interval"] == ["survival_at_*_low", "survival_at_*_high"]
    assert all(set(key.split("+")) <= set(catalog.PREDICT["takes"]) for key in columns if key != "always")
    rows = catalog.variant_spec("parametric_survival", {})["outputs"]["rows"]
    assert {"pred_median_low", "pred_median_high", "pred_quantile_*_low", "pred_surv_at_*_high"} <= set(rows)
    # the rows output's names are the node's own numbers: a column the input already has is refused, intervals included
    base = {"time": "t", "event": "e", "event_value": 1, "output": "rows", "quantiles": [0.1, 0.125], "times": [30]}
    columns_in = {"t": "Float64", "e": "Int64"}
    for taken in ("pred_median_low", "pred_quantile_0.1_high", "pred_quantile_0.125_low", "pred_surv_at_30_low", "pred_surv_at_30"):
        with pytest.raises(Exception, match=taken):
            catalog.check_params("parametric_survival", base, {**columns_in, taken: "Float64"})
    catalog.check_params("parametric_survival", base, {**columns_in, "pred_surv_at_31_low": "Float64"})  # not one of the times
    # a quantile of 0.5 is the median (pred_median): the runner writes no pred_quantile_0.5, so a column of that name is no clash
    catalog.check_params("parametric_survival", {**base, "quantiles": [0.25, 0.5]}, {**columns_in, "pred_quantile_0.5": "Float64"})
    # the checks' measures: a number and a sentence, no verdict; `at` is the time it is read at
    assertion = catalog.variant_spec("parametric_survival", {})["sections"]["assumption"]
    assert "at" in assertion["numbers"] and "description" in assertion["texts"]
    assert "alpha" in catalog.variant_spec("parametric_survival", {})["sections"]["model"]["numbers"]


def test_delayed_entry_is_a_column_of_the_parametric_node_as_of_cox():
    # start: Cox's key and kind; the time of each row is then the end of the interval (start, time]
    spec = catalog.MODELS["parametric_survival"]
    assert spec["takes"]["start"] == catalog.MODELS["cox"]["takes"]["start"] == "number_column"
    assert spec["defaults"]["start"] is None and "start" in spec["disjoint"]
    base = {"time": "days", "event": "status", "event_value": "failed"}
    assert catalog.check_params("parametric_survival", base)["start"] is None
    assert catalog.check_params("parametric_survival", {**base, "start": "entered"})["start"] == "entered"
    # the node writes how many rows enter after 0, in every variant
    for variant in spec["variants"]:
        assert "n_delayed" in variant["sections"]["model"]["numbers"], variant["id"]
    # Predict has no start: the card holds the parameters, and the time already survived of a unit is alive_at
    assert "start" not in catalog.PREDICT["takes"]
