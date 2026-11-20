"""The catalogue of the statistical models: the groups (one image each), the models with
their parameters and variants, the Predict node, and what each variant writes in the ten
columns. Every other component reads its snapshot (docs/specs/statistical-models.md,
«The catalogue»):

    python -m app.engine.models.catalog          # the snapshot, catalog.json beside this file
    python3 .github/scripts/model_catalog.py     # its copies (from the repository's root)

The copies: frontend/assets/model-catalog.json (palette, form), gateway-go/internal/
modelcatalog/catalog.json (embedded: the parameters' check, the variant, the Job's
ConfigMap) and gateway/app/services/model_catalog.json (the assistants).

Everything here is plain data, and the check of a node's parameters is driven by the data:
a parameter's kind names a control (`KINDS`), and the control says how its value is read.
The Go gateway reads the same controls (internal/modelcatalog) and refuses with the same
sentences; `backend/tests/fixtures/models/params_cases.json` holds the cases both run.

A model's variants are values of its own parameters (`estimation`, `penalty`, `orders`),
never other models. Per model:

- `needs`, `takes`: key → kind, in the order the form shows them; `defaults` for `takes`
  (None: not given);
- `applies`: {key: {other: rule}} — the key matters only while every rule holds (the
  defaults count): a rule is a list of values, `True` (the other key is given) or `False`
  (it is not). The form shows the key only then; a key given while it does not apply is
  refused;
- `requires`: [{key, when, reason, message}] — `key` must be given while `when` holds;
- `forbids`: [{key, value, when, reason, message}] — `key` may not be `value` while
  `when` holds (a value of `when` that is a list: one of them);
- `disjoint`: keys naming columns that may not name the same column twice;
- `variants`: in order, the first whose `when` holds is the variant (`variant()`); each
  says which `sections` the runner writes and which rows (`results.py` refuses any other),
  the `assumptions` it checks, the `charts` of its report, and its `outputs` (the model's
  columns of the rows, table or forecast output).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from app.engine.exceptions import EngineError

NODE_MODEL = "model"
NODE_PREDICT = "model_predict"

# ── limits (docs/specs/statistical-models.md, «Limits») ─────────────────────
LIMITS: dict[str, int] = {
    "max_terms": 200,            # columns of the design after encoding
    "max_levels": 50,            # values of a categorical predictor
    "max_strata": 20,            # Kaplan–Meier and Cox strata
    "max_interactions": 20,      # pairs of predictors
    "max_times": 10,             # survival reported at these times
    "max_season": 60,            # the seasonal period s
    "max_horizon": 1000,         # steps of a forecast
    "max_chart_points": 2000,    # points per chart (MODELS__MAX_CHART_POINTS)
    "extreme_points": 10,        # the most extreme points always kept, labelled
    "qq_points": 200,            # quantiles of a normal Q–Q chart
    "max_lags": 40,              # lags of a correlogram
    "max_result_rows": 100_000,  # rows of results.parquet
    "max_card_bytes": 1 << 20,   # model.json
    "max_vcov_terms": 200,       # terms whose covariance the card keeps
    "max_baseline_times": 5000,  # times of Cox's baseline hazard in the card
    "max_paths_terms": 20,       # coefficient paths drawn, the rest summed up as «others»
    # the boosting group's (docs: «Group `boosting` — Python», «Performance and limits»); the R
    # groups read none of them
    "max_predictors": 200,       # predictors of a boosting node
    "max_levels_trees": 1000,    # values of a categorical predictor of a boosting node
    "max_classes": 20,           # classes of a multiclass outcome
    "max_cells": 50_000_000,     # rows × predictors, checked by the gateway before the Job
    "max_shap_rows": 5000,       # rows of the SHAP sample at most
    "shap_cost_budget": 20_000_000_000,  # T · L · D² · rows of TreeSHAP: fewer rows for large models
    "max_model_file_bytes": 64 << 20,    # xgboost.json, catboost.cbm
    "max_importance_bars": 20,   # bars of an importance chart, the rest summed up as «(others)»
    "max_beeswarm_predictors": 10,  # predictors of the SHAP summary
    "max_roc_classes": 10,       # one-against-rest ROC curves of a multiclass model
}

# ── groups: one image each, built when an administrator enables it ─────────
GROUPS: dict[str, dict[str, Any]] = {
    "regression": {"language": "r", "label": "models.groups.regression", "models": ["linear", "logistic"]},
    "survival": {"language": "r", "label": "models.groups.survival", "models": ["kaplan_meier", "cox"]},
    "timeseries": {"language": "r", "label": "models.groups.timeseries", "models": ["arima"]},
    "boosting": {"language": "python", "label": "models.groups.boosting", "models": ["xgboost", "catboost"]},
}

# ── controls and kinds ──────────────────────────────────────────────────────
# What a form draws, and how a value is read (the Go gateway reads the same):
#   column            one column; `numeric`: numbers only; `types`: the column kinds allowed
#   columns           distinct columns; `min`, `max` of them; `numeric`; `predictor`: numbers,
#                     texts and booleans (a date is refused with its own sentence)
#   level             one value of the column the key `of` names (a text, number or boolean)
#   reference         {column of the key `of`: its reference level}
#   pairs             pairs of distinct columns of the key `of`, at most `max`
#   choice            one of `choices`
#   number            a finite number; `above`, `below` excluded, `min`, `max` included
#   integer           a whole number between `min` and `max`, both included
#   boolean           true or false
#   numbers           distinct numbers, at most `max_count`, each within the number's bounds
#   number_or_choice  one of `choices`, or a number within the bounds
CONTROLS = ("column", "columns", "level", "reference", "pairs", "choice", "number", "integer",
            "boolean", "numbers", "number_or_choice")

# The kinds of a column, as the engine's dtypes map to them (`column_kind`).
COLUMN_KINDS = ("integer", "number", "text", "boolean", "date", "datetime", "other")


def _choice(*choices: str) -> dict[str, Any]:
    return {"control": "choice", "choices": list(choices)}


def _int(lo: int, hi: int) -> dict[str, Any]:
    return {"control": "integer", "min": lo, "max": hi}


KINDS: dict[str, dict[str, Any]] = {
    "column": {"control": "column"},
    "number_column": {"control": "column", "numeric": True},
    "time_column": {"control": "column", "types": ["date", "datetime", "integer"]},
    "predictors": {"control": "columns", "min": 1, "predictor": True},
    "number_columns": {"control": "columns", "numeric": True},  # optional (exog): an empty list is none
    "outcome_level": {"control": "level", "of": "outcome"},
    "event_level": {"control": "level", "of": "event"},
    "reference": {"control": "reference", "of": "predictors"},
    "interactions": {"control": "pairs", "of": "predictors", "max": LIMITS["max_interactions"]},
    "boolean": {"control": "boolean"},
    "alpha": {"control": "number", "above": 0, "below": 1},
    "proportion": {"control": "number", "above": 0, "below": 1},
    "positive": {"control": "number", "above": 0},
    "seed": _int(0, 2_147_483_647),
    "folds": _int(3, 20),
    "season": _int(1, LIMITS["max_season"]),
    "order_pq": _int(0, 5),
    "order_d": _int(0, 2),
    "order_seasonal": _int(0, 2),
    "order_seasonal_d": _int(0, 1),
    "horizon": _int(1, LIMITS["max_horizon"]),
    "holdout": _int(0, 1000),
    "times": {"control": "numbers", "min": 0, "max_count": LIMITS["max_times"]},
    "mixing": {"control": "number_or_choice", "choices": ["cv"], "above": 0, "below": 1},
    "lambda": {"control": "number_or_choice", "choices": ["cv_1se", "cv_min"], "above": 0},
    "box_cox_lambda": {"control": "number_or_choice", "choices": ["auto"], "min": -5, "max": 5},
    "estimation_linear": _choice("ols", "wls"),
    "estimation_logistic": _choice("ml", "firth"),
    "penalty": _choice("none", "ridge", "lasso", "elastic_net"),
    "se_linear": _choice("classical", "hc0", "hc1", "hc2", "hc3", "hac", "cluster"),
    "se_logistic": _choice("classical", "hc0", "hc1", "hc2", "hc3", "cluster"),
    "se_cox": _choice("classical", "robust"),
    "measure_linear": _choice("mse", "mae"),
    "measure_logistic": _choice("deviance", "auc", "class"),
    "link": _choice("logit", "probit"),
    "ci": _choice("wald", "profile"),
    "conf_type": _choice("log", "log-log", "plain"),
    "km_test": _choice("logrank", "peto"),
    "ties": _choice("efron", "breslow", "exact"),
    "frequency": _choice("hour", "day", "week", "month", "quarter", "year", "index"),
    "orders": _choice("manual", "auto"),
    "criterion": _choice("aicc", "aic", "bic"),
    "unit_root_test": _choice("kpss", "adf", "pp"),
    "seasonal_test": _choice("seas", "ocsb"),
    "transform": _choice("none", "log", "box_cox"),
    "output_rows": _choice("results", "rows"),
    "output_table": _choice("results", "table"),
    "output_forecast": _choice("results", "forecast"),
    # the boosting group's
    "task_boosting": _choice("regression", "binary", "multiclass"),
    "class_weight": _choice("none", "balanced"),
    "validation": _choice("random", "time"),
    "validation_share": {"control": "number", "min": 0.05, "max": 0.5},
    "order_column": {"control": "column", "types": ["number", "integer", "date", "datetime"]},
    "stop_metric": _choice("loss", "mae", "auc", "error"),
    "max_trees": _int(10, 5000),
    "rounds": _int(5, 1000),
    "learning_rate": {"control": "number", "above": 0, "max": 1},
    "depth_xgboost": _int(1, 12),
    "depth_catboost": _int(1, 10),
    "nonnegative": {"control": "number", "min": 0, "max": 1000},
    "fraction": {"control": "number", "above": 0, "max": 1},
    "strength": {"control": "number", "min": 0, "max": 100},
}

# The seasonal period a frequency implies when `s` is not given.
SEASON_OF_FREQUENCY = {"hour": 24, "day": 7, "week": 52, "month": 12, "quarter": 4, "year": 1, "index": 1}

# ── what the sections of the ten columns hold (docs: «The output table») ────
# Per section: whether `level` and `level_2` are null, required or optional, and whether its
# rows are points of a chart (their key ends with an index; `unindexed` stats excepted).
SECTION_SHAPES: dict[str, dict[str, Any]] = {
    "model": {"level": "null", "level_2": "null", "indexed": False},
    "coef": {"level": "required", "level_2": "optional", "indexed": False},
    "cv": {"level": "optional", "level_2": "null", "indexed": True},
    "candidate": {"level": "required", "level_2": "null", "indexed": False},
    "assumption": {"level": "required", "level_2": "optional", "indexed": False},
    "stratum": {"level": "required", "level_2": "null", "indexed": False},
    "pair": {"level": "required", "level_2": "required", "indexed": False},
    "cell": {"level": "required", "level_2": "required", "indexed": False},
    "point": {"level": "required", "level_2": "optional", "indexed": True},
    "curve": {"level": "required", "level_2": "optional", "indexed": True},
    "lag": {"level": "required", "level_2": "null", "indexed": True, "unindexed": ["bound"]},
    "hist": {"level": "required", "level_2": "optional", "indexed": True},
    # the boosting group's: a predictor (and, multiclass, a class); a class of the outcome
    "importance": {"level": "required", "level_2": "optional", "indexed": False},
    "class": {"level": "required", "level_2": "null", "indexed": False},
}

VERDICTS = ("ok", "doubtful", "failed", "not_computed")

# What every model writes in its `model` section, besides its own.
_MODEL_TEXTS = ["model", "variant", "formula", "software", "group_version", "rows_from", "note_*"]

# The rows of an assumption: what it checks and what came out.
_ASSUMPTION = {
    "numbers": ["statistic", "df", "df_2", "p_value", "threshold", "value", "count"],
    "texts": ["h0", "verdict", "advice", "significance", "rows"],
}


def _assumptions(*checks: str) -> dict[str, Any]:
    return {"levels": list(checks), **_ASSUMPTION}


def _chart(*levels: str, stats: list[str]) -> dict[str, Any]:
    return {"levels": list(levels), "numbers": stats, "texts": []}


_REGRESSION_SHARED = {
    "reference": "reference", "interactions": "interactions", "intercept": "boolean",
    "weights": "number_column",
}
_PENALTY_KEYS = {"mixing": "mixing", "lambda": "lambda", "folds": "folds", "fold_by": "column"}
_PENALIZED = ["ridge", "lasso", "elastic_net"]
_PENALTY_APPLIES = {
    "estimation": {"penalty": ["none"]},
    "se": {"penalty": ["none"]},
    "cluster": {"se": ["cluster"], "penalty": ["none"]},
    "mixing": {"penalty": ["elastic_net"]},
    "lambda": {"penalty": _PENALIZED},
    "folds": {"penalty": _PENALIZED},
    "fold_by": {"penalty": _PENALIZED},
    "measure": {"penalty": _PENALIZED},
    "standardize": {"penalty": _PENALIZED},
}
_CLUSTER_REQUIRED = {"key": "cluster", "when": {"se": "cluster", "penalty": "none"}, "reason": "cluster_needs_column",
                     "message": "clustered standard errors need the column of the clusters ('cluster')"}
_CV_SECTION = {"levels": None, "numbers": ["cv_mean", "cv_low", "cv_high", "n_selected"], "texts": []}
_PENALTY_FIT = ["lambda", "log_lambda", "lambda_min", "lambda_1se", "log_lambda_min", "log_lambda_1se", "mixing", "cv_value",
                "cv_se", "n_selected"]

MODELS: dict[str, dict[str, Any]] = {
    # ── regression ──────────────────────────────────────────────────────────
    "linear": {
        "group": "regression",
        "family": "regression",
        "needs": {"outcome": "number_column", "predictors": "predictors"},
        "takes": {
            "estimation": "estimation_linear", "penalty": "penalty", **_REGRESSION_SHARED,
            "se": "se_linear", "cluster": "column", "order_by": "column",
            **_PENALTY_KEYS, "measure": "measure_linear", "standardize": "boolean",
            "alpha": "alpha", "seed": "seed", "output": "output_rows",
        },
        "defaults": {
            "estimation": "ols", "penalty": "none", "reference": {}, "interactions": [], "intercept": True,
            "weights": None, "se": "classical", "cluster": None, "order_by": None,
            "mixing": 0.5, "lambda": "cv_1se", "folds": 10, "fold_by": None, "measure": "mse", "standardize": True,
            "alpha": 0.05, "seed": 1, "output": "results",
        },
        "applies": {**_PENALTY_APPLIES, "order_by": {"penalty": ["none"]}},
        "requires": [
            {"key": "weights", "when": {"estimation": "wls", "penalty": "none"}, "reason": "wls_needs_weights",
             "message": "weighted least squares need the column of the weights ('weights')"},
            _CLUSTER_REQUIRED,
        ],
        "forbids": [],
        "disjoint": ["outcome", "predictors", "weights"],
        "variants": [
            {
                "id": "unpenalized",
                "when": {"penalty": ["none"]},
                "sections": {
                    "model": {"levels": None, "texts": [*_MODEL_TEXTS, "family", "link", "se_type", "f_significance"],
                              "numbers": ["n", "n_missing", "df_model", "df_residual", "r_squared", "adj_r_squared",
                                          "sigma", "f_statistic", "f_df_1", "f_df_2", "f_p_value", "log_lik", "aic",
                                          "bic", "rmse", "mae", "condition_number"]},
                    "coef": {"levels": None, "numbers": ["estimate", "std_error", "statistic", "df", "p_value", "ci_low",
                                                         "ci_high", "vif"], "texts": ["significance"]},
                    "assumption": _assumptions("reset", "breusch_pagan", "white", "shapiro_wilk", "anderson_darling",
                                               "jarque_bera", "durbin_watson", "breusch_godfrey_1", "breusch_godfrey_4",
                                               "vif", "condition_number", "cooks_distance", "leverage", "outlier_test"),
                    "point": _chart("resid_fitted", "qq", "scale_location", "resid_leverage", "observed_fitted", stats=["y"]),
                    "curve": _chart("resid_fitted", "qq", "scale_location", stats=["y"]),
                    "hist": _chart("residual", stats=["count"]),
                },
                "charts": [
                    {"id": "resid_fitted", "under": "breusch_pagan"}, {"id": "qq", "under": "shapiro_wilk"},
                    {"id": "scale_location", "under": "white"}, {"id": "resid_leverage", "under": "cooks_distance"},
                    {"id": "residual", "under": "jarque_bera"}, {"id": "observed_fitted", "under": None},
                    {"id": "coef_interval", "under": None},
                ],
                "outputs": {"rows": ["pred_fitted", "pred_residual", "pred_std_residual", "pred_leverage",
                                     "pred_cooks_distance", "pred_low", "pred_high"]},
            },
            {
                "id": "penalized",
                "when": {"penalty": _PENALIZED},
                "sections": {
                    "model": {"levels": None, "texts": [*_MODEL_TEXTS, "family", "penalty", "cv_measure"],
                              "numbers": ["n", "n_missing", *_PENALTY_FIT, "rmse", "mae", "r_squared"]},
                    "coef": {"levels": None, "numbers": ["estimate", "selected"], "texts": []},
                    "cv": _CV_SECTION,
                    "assumption": _assumptions("lambda_choice"),
                    "point": _chart("resid_fitted", "qq", stats=["y"]),
                    "curve": _chart("paths", "resid_fitted", "qq", stats=["y"]),
                    "hist": _chart("residual", stats=["count"]),
                },
                "charts": [
                    {"id": "cv", "under": "lambda_choice"}, {"id": "paths", "under": "lambda_choice"},
                    {"id": "coef_selected", "under": None}, {"id": "resid_fitted", "under": None},
                    {"id": "qq", "under": None},
                ],
                "outputs": {"rows": ["pred_fitted", "pred_residual"]},
            },
        ],
    },
    "logistic": {
        "group": "regression",
        "family": "regression",
        "needs": {"outcome": "column", "event_value": "outcome_level", "predictors": "predictors"},
        "takes": {
            "estimation": "estimation_logistic", "penalty": "penalty", "link": "link", **_REGRESSION_SHARED,
            "se": "se_logistic", "cluster": "column", "ci": "ci", "threshold": "proportion",
            **_PENALTY_KEYS, "measure": "measure_logistic", "standardize": "boolean",
            "alpha": "alpha", "seed": "seed", "output": "output_rows",
        },
        "defaults": {
            "estimation": "ml", "penalty": "none", "link": "logit", "reference": {}, "interactions": [],
            "intercept": True, "weights": None, "se": "classical", "cluster": None, "ci": "wald", "threshold": 0.5,
            "mixing": 0.5, "lambda": "cv_1se", "folds": 10, "fold_by": None, "measure": "deviance",
            "standardize": True, "alpha": 0.05, "seed": 1, "output": "results",
        },
        "applies": {**_PENALTY_APPLIES, "link": {"penalty": ["none"]}, "ci": {"penalty": ["none"]}},
        "requires": [_CLUSTER_REQUIRED],
        "forbids": [
            {"key": "estimation", "value": "firth", "when": {"link": "probit"}, "reason": "firth_needs_logit",
             "message": "Firth's penalized likelihood is for the logit link: use link = logit, or estimation = ml"},
            {"key": "ci", "value": "profile", "when": {"se": ["hc0", "hc1", "hc2", "hc3", "cluster"]},
             "reason": "profile_needs_classical_se",
             "message": "profile likelihood intervals go with the classical standard errors: use ci = wald with robust ones"},
        ],
        "disjoint": ["outcome", "predictors", "weights"],
        "variants": [
            {
                "id": "unpenalized",
                "when": {"penalty": ["none"]},
                "sections": {
                    "model": {"levels": None, "texts": [*_MODEL_TEXTS, "family", "link", "estimation", "se_type",
                                                        "event_value", "lr_significance"],
                              "numbers": ["n", "events", "n_missing", "auc", "auc_ci_low", "auc_ci_high", "brier",
                                          "threshold", "accuracy", "sensitivity", "specificity", "precision",
                                          "deviance", "null_deviance", "lr_statistic", "lr_df", "lr_p_value",
                                          "log_lik", "aic", "bic", "mcfadden_r2", "nagelkerke_r2", "tjur_r2"]},
                    "coef": {"levels": None, "numbers": ["estimate", "std_error", "statistic", "p_value", "ci_low",
                                                         "ci_high", "odds_ratio", "or_ci_low", "or_ci_high", "vif"],
                             "texts": ["significance"]},
                    "assumption": _assumptions("separation", "hosmer_lemeshow", "box_tidwell", "vif", "epv",
                                               "cooks_distance", "leverage", "deviance_residuals"),
                    "point": _chart("calibration", "resid_linear", stats=["y"]),
                    "curve": _chart("roc", stats=["y"]),
                    "hist": _chart("prob", stats=["count"]),
                    "cell": {"levels": None, "numbers": ["observed"], "texts": []},
                },
                "charts": [
                    {"id": "roc", "under": None}, {"id": "calibration", "under": "hosmer_lemeshow"},
                    {"id": "prob", "under": None}, {"id": "confusion", "under": None},
                    {"id": "resid_linear", "under": "deviance_residuals"}, {"id": "odds_ratio_interval", "under": None},
                ],
                "outputs": {"rows": ["pred_prob", "pred_class", "pred_linear", "pred_deviance_residual",
                                     "pred_pearson_residual", "pred_leverage", "pred_cooks_distance"]},
            },
            {
                "id": "penalized",
                "when": {"penalty": _PENALIZED},
                "sections": {
                    "model": {"levels": None, "texts": [*_MODEL_TEXTS, "family", "link", "penalty", "cv_measure",
                                                        "event_value"],
                              # the AUC is the mean of the folds' (their fits' intercepts differ: a ranking
                              # cannot mix them), with its standard error between folds, as cv.glmnet's cvm
                              "numbers": ["n", "events", "n_missing", "auc", "auc_se", "auc_folds", "brier", "brier_se",
                                          "threshold", "accuracy", "sensitivity", "specificity", "precision",
                                          "deviance", *_PENALTY_FIT]},
                    "coef": {"levels": None, "numbers": ["estimate", "odds_ratio", "selected"], "texts": []},
                    "cv": _CV_SECTION,
                    "assumption": _assumptions("separation", "hosmer_lemeshow", "epv", "lambda_choice"),
                    "point": _chart("calibration", stats=["y"]),
                    "curve": _chart("roc", "paths", stats=["y"]),
                    "hist": _chart("prob", stats=["count"]),
                    "cell": {"levels": None, "numbers": ["observed"], "texts": []},
                },
                "charts": [
                    {"id": "cv", "under": "lambda_choice"}, {"id": "paths", "under": "lambda_choice"},
                    {"id": "roc", "under": None}, {"id": "calibration", "under": "hosmer_lemeshow"},
                    {"id": "prob", "under": None}, {"id": "confusion", "under": None},
                ],
                "outputs": {"rows": ["pred_prob", "pred_class", "pred_linear"]},
            },
        ],
    },
    # ── survival ────────────────────────────────────────────────────────────
    "kaplan_meier": {
        "group": "survival",
        "family": "survival",
        "needs": {"time": "number_column", "event": "column", "event_value": "event_level"},
        "takes": {"strata": "column", "conf_type": "conf_type", "test": "km_test", "pairwise": "boolean",
                  "times": "times", "rmst_tau": "positive", "alpha": "alpha", "seed": "seed", "output": "output_table"},
        "defaults": {"strata": None, "conf_type": "log", "test": "logrank", "pairwise": False, "times": [],
                     "rmst_tau": None, "alpha": 0.05, "seed": 1, "output": "results"},
        "applies": {"test": {"strata": True}, "pairwise": {"strata": True}},
        "requires": [],
        "forbids": [],
        "disjoint": ["time", "event", "strata"],
        "variants": [
            {
                "id": "default",
                "when": {},
                "sections": {
                    "model": {"levels": None, "texts": [*_MODEL_TEXTS, "event_value", "conf_type", "test",
                                                        "test_significance"],
                              "numbers": ["n", "events", "censored", "n_missing", "n_strata", "rmst_tau",
                                          "test_statistic", "test_df", "test_p_value"]},
                    "stratum": {"levels": None, "numbers": ["n", "events", "censored", "median", "median_ci_low",
                                                            "median_ci_high", "q25", "q25_ci_low", "q25_ci_high", "q75",
                                                            "q75_ci_low", "q75_ci_high", "surv_at_*", "rmst", "rmst_se",
                                                            "observed", "expected"], "texts": []},
                    "pair": {"levels": None, "numbers": ["statistic", "p_value", "p_adjusted"], "texts": ["significance"]},
                    "assumption": _assumptions("censoring_informative", "proportional_hazards", "censoring_by_stratum",
                                               "follow_up_by_stratum", "few_at_risk"),
                    "curve": _chart("km", "cumhaz", "loglog", stats=["surv", "lower", "upper", "cumhaz", "y"]),
                    "point": _chart("censored", "at_risk", stats=["surv", "n_risk"]),
                },
                "charts": [
                    {"id": "km", "under": None}, {"id": "at_risk", "under": None}, {"id": "cumhaz", "under": None},
                    {"id": "loglog", "under": "proportional_hazards"},
                ],
                "outputs": {"table": ["stratum", "time", "n_risk", "n_event", "n_censor", "surv", "lower", "upper"]},
            },
        ],
    },
    "cox": {
        "group": "survival",
        "family": "survival",
        "needs": {"time": "number_column", "event": "column", "event_value": "event_level", "predictors": "predictors"},
        "takes": {"start": "number_column", "reference": "reference", "interactions": "interactions",
                  "strata": "column", "ties": "ties", "se": "se_cox", "cluster": "column", "alpha": "alpha",
                  "seed": "seed", "output": "output_rows"},
        "defaults": {"start": None, "reference": {}, "interactions": [], "strata": None, "ties": "efron",
                     "se": "classical", "cluster": None, "alpha": 0.05, "seed": 1, "output": "results"},
        "applies": {"cluster": {"se": ["robust"]}},
        "requires": [],
        "forbids": [],
        "disjoint": ["time", "event", "start", "predictors", "strata"],
        "variants": [
            {
                "id": "default",
                "when": {},
                "sections": {
                    "model": {"levels": None, "texts": [*_MODEL_TEXTS, "event_value", "ties", "se_type",
                                                        "lr_significance", "wald_significance", "score_significance"],
                              "numbers": ["n", "events", "n_missing", "lr_statistic", "lr_df", "lr_p_value",
                                          "wald_statistic", "wald_df", "wald_p_value", "score_statistic", "score_df",
                                          "score_p_value", "concordance", "concordance_se", "log_lik", "aic"]},
                    "coef": {"levels": None, "numbers": ["estimate", "hazard_ratio", "hr_ci_low", "hr_ci_high",
                                                         "std_error", "robust_se", "statistic", "p_value", "vif"],
                             "texts": ["significance"]},
                    "assumption": _assumptions("schoenfeld", "schoenfeld_global", "martingale_form", "dfbeta", "vif",
                                               "epv"),
                    "point": _chart("schoenfeld", "martingale", "deviance_linear", stats=["y"]),
                    "curve": _chart("schoenfeld", "martingale", "adjusted_survival",
                                    stats=["smooth", "lower", "upper", "lowess", "surv"]),
                },
                "charts": [
                    {"id": "hazard_ratio_interval", "under": None}, {"id": "schoenfeld", "under": "schoenfeld"},
                    {"id": "martingale", "under": "martingale_form"}, {"id": "deviance_linear", "under": "dfbeta"},
                    {"id": "adjusted_survival", "under": None},
                ],
                "outputs": {"rows": ["pred_linear", "pred_risk", "pred_martingale", "pred_deviance"]},
            },
        ],
    },
    # ── time series ─────────────────────────────────────────────────────────
    "arima": {
        "group": "timeseries",
        "family": "timeseries",
        "needs": {"time": "time_column", "value": "number_column", "frequency": "frequency"},
        "takes": {
            "s": "season", "orders": "orders",
            "p": "order_pq", "d": "order_d", "q": "order_pq", "P": "order_seasonal", "D": "order_seasonal_d",
            "Q": "order_seasonal",
            "criterion": "criterion", "stepwise": "boolean", "max_p": "order_pq", "max_q": "order_pq",
            "max_P": "order_seasonal", "max_Q": "order_seasonal", "max_d": "order_d", "max_D": "order_seasonal_d",
            "unit_root_test": "unit_root_test", "seasonal_test": "seasonal_test",
            "drift": "boolean", "mean": "boolean", "exog": "number_columns",
            "transform": "transform", "box_cox_lambda": "box_cox_lambda",
            "horizon": "horizon", "holdout": "holdout", "alpha": "alpha", "seed": "seed", "output": "output_forecast",
        },
        "defaults": {
            "s": None, "orders": "manual", "p": 1, "d": 0, "q": 0, "P": 0, "D": 0, "Q": 0,
            "criterion": "aicc", "stepwise": True, "max_p": 5, "max_q": 5, "max_P": 2, "max_Q": 2, "max_d": 2,
            "max_D": 1, "unit_root_test": "kpss", "seasonal_test": "seas", "drift": False, "mean": True,
            "exog": [], "transform": "none", "box_cox_lambda": "auto", "horizon": 12, "holdout": 0,
            "alpha": 0.05, "seed": 1, "output": "results",
        },
        "applies": {
            **{k: {"orders": ["manual"]} for k in ("p", "d", "q", "P", "D", "Q")},
            **{k: {"orders": ["auto"]} for k in ("criterion", "stepwise", "max_p", "max_q", "max_P", "max_Q",
                                                  "max_d", "max_D", "unit_root_test", "seasonal_test")},
            "box_cox_lambda": {"transform": ["box_cox"]},
            "horizon": {"exog": False},
        },
        "requires": [],
        "forbids": [],
        "disjoint": ["time", "value", "exog"],
        "presets": {
            "ar": {"orders": "manual", "p": 1, "d": 0, "q": 0, "P": 0, "D": 0, "Q": 0},
            "ma": {"orders": "manual", "p": 0, "d": 0, "q": 1, "P": 0, "D": 0, "Q": 0},
            "arma": {"orders": "manual", "p": 1, "d": 0, "q": 1, "P": 0, "D": 0, "Q": 0},
            "arima": {"orders": "manual", "p": 1, "d": 1, "q": 1, "P": 0, "D": 0, "Q": 0},
            "sarima": {"orders": "manual", "p": 1, "d": 1, "q": 1, "P": 0, "D": 1, "Q": 1},
            "sarimax": {"orders": "manual", "p": 1, "d": 1, "q": 1, "P": 0, "D": 1, "Q": 1},
        },
        "variants": [
            {
                "id": "manual",
                "when": {"orders": ["manual"]},
                "sections": "_arima",
                "charts": "_arima",
                "outputs": "_arima",
            },
            {
                "id": "auto",
                "when": {"orders": ["auto"]},
                "sections": "_arima_auto",
                "charts": "_arima_auto",
                "outputs": "_arima",
            },
        ],
    },
}

_ARIMA_SECTIONS: dict[str, Any] = {
    "model": {"levels": None, "texts": [*_MODEL_TEXTS, "model_name", "frequency", "transform", "time_kind"],
              "numbers": ["n", "n_missing", "p", "d", "q", "P", "D", "Q", "s", "log_lik", "aic", "aicc", "bic",
                          "sigma2", "rmse", "mae", "mape", "mase", "holdout", "holdout_rmse", "holdout_mae",
                          "holdout_mape", "holdout_mase", "box_cox_lambda", "horizon", "ar_root_modulus_*",
                          "ma_root_modulus_*"]},
    "coef": {"levels": None, "numbers": ["estimate", "std_error", "statistic", "p_value", "ci_low", "ci_high"],
             "texts": ["significance"]},
    "assumption": _assumptions("adf", "kpss", "stationarity", "roots", "ljung_box_10", "ljung_box_2s", "arch_lm",
                               "variance_ratio", "jarque_bera"),
    "curve": _chart("forecast", "residuals", "qq",
                    stats=["observed", "fitted", "forecast", "lower_80", "upper_80", "lower", "upper", "y"]),
    "point": _chart("qq", stats=["y"]),
    "lag": _chart("acf", "pacf", "resid_acf", "resid_pacf", stats=["acf", "pacf", "bound"]),
    "hist": _chart("residual", stats=["count"]),
}
_ARIMA_CHARTS = [
    {"id": "forecast", "under": None}, {"id": "acf", "under": "stationarity"}, {"id": "pacf", "under": "stationarity"},
    {"id": "resid_acf", "under": "ljung_box_10"}, {"id": "resid_pacf", "under": "ljung_box_10"},
    {"id": "residuals", "under": "arch_lm"}, {"id": "residual", "under": "jarque_bera"}, {"id": "qq", "under": "jarque_bera"},
]
_ARIMA_AUTO_SECTIONS = {
    **_ARIMA_SECTIONS,
    "model": {**_ARIMA_SECTIONS["model"],
              "texts": [*_ARIMA_SECTIONS["model"]["texts"], "criterion", "orders_reason_*"],
              "numbers": [*_ARIMA_SECTIONS["model"]["numbers"], "candidates", "order_test_*"]},
    "candidate": {"levels": None, "numbers": ["aicc", "aic", "bic", "converged", "chosen"], "texts": []},
}
_ARIMA_OUTPUTS = {"forecast": ["time", "observed", "fitted", "forecast", "lower_80", "upper_80", "lower", "upper",
                               "is_forecast"]}
_SHARED_DECLARATIONS = {
    ("sections", "_arima"): _ARIMA_SECTIONS, ("sections", "_arima_auto"): _ARIMA_AUTO_SECTIONS,
    ("charts", "_arima"): _ARIMA_CHARTS,
    ("charts", "_arima_auto"): [*_ARIMA_CHARTS, {"id": "candidates", "under": None}],
    ("outputs", "_arima"): _ARIMA_OUTPUTS,
}
for _variant in MODELS["arima"]["variants"]:
    for _field in ("sections", "charts", "outputs"):
        _variant[_field] = _SHARED_DECLARATIONS[(_field, _variant[_field])]

# ── boosting (docs: «Group `boosting` — Python») ────────────────────────────
# Both nodes take the same keys but for their own hyperparameters; `task` is the variant.
_CLASSIFICATION = ["binary", "multiclass"]
_BOOSTING_NEEDS = {"task": "task_boosting", "outcome": "column", "predictors": "predictors"}
_BOOSTING_TAKES = {
    "event_value": "outcome_level", "weights": "number_column", "class_weight": "class_weight",
    "threshold": "proportion", "validation": "validation", "validation_share": "validation_share",
    "order_by": "order_column", "split_by": "column", "stratify": "boolean", "early_stopping": "boolean",
    "early_stopping_rounds": "rounds", "stop_metric": "stop_metric", "max_trees": "max_trees",
    "learning_rate": "learning_rate", "max_depth": "depth_xgboost", "subsample": "fraction",
}
_BOOSTING_DEFAULTS = {
    "event_value": None, "weights": None, "class_weight": "none", "threshold": 0.5, "validation": "random",
    "validation_share": 0.2, "order_by": None, "split_by": None, "stratify": True, "early_stopping": True,
    "early_stopping_rounds": 50, "stop_metric": "loss", "max_trees": 1000, "learning_rate": 0.05, "max_depth": 6,
    "subsample": 0.8,
}
_BOOSTING_TAIL = {"seed": "seed", "output": "output_rows"}  # last in the form
_BOOSTING_APPLIES = {
    "event_value": {"task": ["binary"]},
    "class_weight": {"task": _CLASSIFICATION},
    "threshold": {"task": ["binary"]},
    "order_by": {"validation": ["time"]},
    "split_by": {"validation": ["random"]},
    "stratify": {"validation": ["random"], "task": _CLASSIFICATION, "split_by": False},
    "early_stopping_rounds": {"early_stopping": [True]},
}
_BOOSTING_REQUIRES = [
    {"key": "event_value", "when": {"task": "binary"}, "reason": "binary_needs_event",
     "message": "a binary task needs the outcome's value whose probability is predicted ('event_value')"},
    {"key": "order_by", "when": {"validation": "time"}, "reason": "time_needs_order",
     "message": "a validation part in time needs the column of the time ('order_by')"},
]
_BOOSTING_FORBIDS = [
    {"key": "stop_metric", "value": "mae", "when": {"task": _CLASSIFICATION}, "reason": "mae_needs_regression",
     "message": "the mean absolute error is a regression's metric: use stop_metric = loss, auc or error"},
    {"key": "stop_metric", "value": "auc", "when": {"task": ["regression", "multiclass"]}, "reason": "auc_needs_binary",
     "message": "the AUC is a binary task's metric: use stop_metric = loss"
                + " (or mae for a regression, error for a multiclass task)"},
    {"key": "stop_metric", "value": "error", "when": {"task": ["regression"]}, "reason": "error_needs_classes",
     "message": "the error rate is a classification's metric: use stop_metric = loss or mae"},
]
_BOOSTING_MODEL_TEXTS = [*_MODEL_TEXTS, "task", "library", "objective", "stop_metric", "validation", "importance_type"]
_BOOSTING_MODEL_NUMBERS = ["n", "n_missing", "n_train", "n_validation", "n_predictors", "n_categorical", "trees",
                           "best_iteration", "max_trees", "stopped_early", "stopped_by_time", "threads", "shap_rows",
                           "seed"]
_BOOSTING_IMPORTANCE = {"levels": None, "numbers": ["gain", "gain_share", "mean_abs_shap", "mean_abs_shap_share", "rank",
                                                    "mean_abs_shap_train", "mean_abs_shap_validation", "missing_share"],
                        "texts": []}
_BOOSTING_CHECKS = ["validation_size", "beats_baseline", "overfitting", "trees_limit", "importance_stability",
                    "zero_importance", "leakage"]
_BEESWARM_BINS = ["low", "mid_low", "mid", "mid_high", "high", "category", "missing"]
_BOOSTING_SHARED_CHARTS = [
    {"id": "importance_gain", "under": None}, {"id": "importance_shap", "under": "importance_stability"},
]
_LEARNING_CHARTS = [{"id": "learning_loss", "under": "overfitting"}, {"id": "learning_stop", "under": "trees_limit"}]
_BOOSTING_VARIANTS: list[dict[str, Any]] = [
    {
        "id": "regression",
        "when": {"task": ["regression"]},
        "sections": {
            "model": {"levels": None, "texts": _BOOSTING_MODEL_TEXTS,
                      "numbers": [*_BOOSTING_MODEL_NUMBERS, "train_rmse", "train_mae", "train_r_squared",
                                  "validation_rmse", "validation_mae", "validation_r_squared", "baseline_rmse",
                                  "validation_mean"]},
            "importance": _BOOSTING_IMPORTANCE,
            "assumption": _assumptions(*_BOOSTING_CHECKS),
            "curve": _chart("learning_loss", "learning_stop", stats=["y"]),
            "point": {"levels": ["shap_beeswarm", "observed_predicted", "resid_predicted"],
                      "numbers": [*_BEESWARM_BINS, "y"], "texts": []},
            "hist": _chart("residual", stats=["count"]),
        },
        "charts": [*_BOOSTING_SHARED_CHARTS, {"id": "shap_beeswarm", "under": None}, *_LEARNING_CHARTS,
                   {"id": "observed_predicted", "under": None}, {"id": "resid_predicted", "under": None},
                   {"id": "residual", "under": None}],
        "outputs": {"rows": ["pred_value", "pred_residual", "pred_part"]},
    },
    {
        "id": "binary",
        "when": {"task": ["binary"]},
        "sections": {
            "model": {"levels": None, "texts": [*_BOOSTING_MODEL_TEXTS, "event_value"],
                      "numbers": [*_BOOSTING_MODEL_NUMBERS, "events", "validation_events", "train_auc",
                                  "validation_auc", "train_logloss", "validation_logloss", "baseline_logloss",
                                  "train_brier", "validation_brier", "threshold", "validation_accuracy",
                                  "validation_sensitivity", "validation_specificity", "validation_precision",
                                  "validation_f1"]},
            "importance": _BOOSTING_IMPORTANCE,
            "assumption": _assumptions(*_BOOSTING_CHECKS, "class_imbalance", "calibration", "calibration_slope"),
            "curve": _chart("learning_loss", "learning_stop", "roc", stats=["y"]),
            "point": {"levels": ["shap_beeswarm", "calibration"], "numbers": [*_BEESWARM_BINS, "y"], "texts": []},
            "hist": _chart("probability", stats=["count"]),
            "cell": {"levels": None, "numbers": ["observed"], "texts": []},
        },
        "charts": [*_BOOSTING_SHARED_CHARTS, {"id": "shap_beeswarm", "under": None}, *_LEARNING_CHARTS,
                   {"id": "roc", "under": None}, {"id": "calibration", "under": "calibration"},
                   {"id": "probability", "under": None}, {"id": "confusion", "under": None}],
        "outputs": {"rows": ["pred_prob", "pred_class", "pred_part"]},
    },
    {
        "id": "multiclass",
        "when": {"task": ["multiclass"]},
        "sections": {
            "model": {"levels": None, "texts": _BOOSTING_MODEL_TEXTS,
                      "numbers": [*_BOOSTING_MODEL_NUMBERS, "n_classes", "train_mlogloss", "validation_mlogloss",
                                  "baseline_mlogloss", "train_accuracy", "validation_accuracy",
                                  "validation_balanced_accuracy", "validation_macro_auc", "validation_macro_f1"]},
            "importance": _BOOSTING_IMPORTANCE,
            "class": {"levels": None, "numbers": ["support", "share_train", "share_validation", "precision", "recall",
                                                  "f1", "auc_ovr"], "texts": []},
            "assumption": _assumptions(*_BOOSTING_CHECKS, "class_imbalance"),
            "curve": _chart("learning_loss", "learning_stop", "roc_ovr", stats=["y"]),
            "cell": {"levels": None, "numbers": ["observed"], "texts": []},
        },
        "charts": [*_BOOSTING_SHARED_CHARTS, *_LEARNING_CHARTS, {"id": "confusion", "under": None},
                   {"id": "roc_ovr", "under": None}],
        "outputs": {"rows": ["pred_class", "pred_prob", "pred_prob_*", "pred_part"]},
    },
]


def _boosting(own: dict[str, str], defaults: dict[str, Any], depth: str) -> dict[str, Any]:
    """A boosting node: the shared keys, then the library's own hyperparameters, `seed` and
    `output` last. Each variant is a copy: the snapshot holds plain data."""
    takes = {**_BOOSTING_TAKES, "max_depth": depth, **own, **_BOOSTING_TAIL}
    return {
        "group": "boosting",
        "family": "boosting",
        "needs": dict(_BOOSTING_NEEDS),
        "takes": takes,
        "defaults": {**_BOOSTING_DEFAULTS, **defaults, "seed": 1, "output": "results"},
        "applies": json.loads(json.dumps(_BOOSTING_APPLIES)),
        "requires": json.loads(json.dumps(_BOOSTING_REQUIRES)),
        "forbids": json.loads(json.dumps(_BOOSTING_FORBIDS)),
        "disjoint": ["outcome", "predictors", "weights", "order_by", "split_by"],
        "variants": json.loads(json.dumps(_BOOSTING_VARIANTS)),
    }


MODELS["xgboost"] = _boosting(
    {"min_child_weight": "nonnegative", "colsample_bytree": "fraction", "reg_lambda": "nonnegative",
     "reg_alpha": "nonnegative", "gamma": "nonnegative"},
    {"min_child_weight": 1.0, "colsample_bytree": 0.8, "reg_lambda": 1.0, "reg_alpha": 0.0, "gamma": 0.0},
    "depth_xgboost")
MODELS["catboost"] = _boosting(
    {"l2_leaf_reg": "nonnegative", "rsm": "fraction", "random_strength": "strength"},
    {"l2_leaf_reg": 3.0, "rsm": 1.0, "random_strength": 1.0},
    "depth_catboost")

# ── Predict: a registered model applied to new rows ─────────────────────────
PREDICT: dict[str, Any] = {
    "takes": {"interval": "boolean", "threshold": "proportion", "horizon": "horizon", "at_times": "times"},
    # None: the card's own (its threshold, its horizon)
    "defaults": {"interval": False, "threshold": None, "horizon": None, "at_times": []},
    # which models each key means something for; given for another model it is refused. An
    # entry is a model, or `model:variant` for one variant of it (the card's variant)
    "applies_to": {"interval": ["linear", "kaplan_meier", "arima"],
                   "threshold": ["logistic", "xgboost:binary", "catboost:binary"],
                   "horizon": ["arima"], "at_times": ["cox"]},
    # the columns Predict adds, per model of the card (or `model:variant`, which comes first):
    # always, and with each key given. `prediction_prob_*`: one column per class of the card
    "columns": {
        "linear": {"always": ["prediction"], "interval": ["prediction_low", "prediction_high"]},
        "logistic": {"always": ["prediction"], "threshold": ["prediction_class"]},
        "kaplan_meier": {"always": ["prediction"], "interval": ["prediction_low", "prediction_high"]},
        "cox": {"always": ["prediction"], "at_times": ["survival_at_*"]},
        "arima": {"always": ["prediction"], "interval": ["lower_80", "upper_80", "lower", "upper"]},
        **{f"{m}:{v}": cols for m in ("xgboost", "catboost") for v, cols in (
            ("regression", {"always": ["prediction"]}),
            ("binary", {"always": ["prediction", "prediction_class"]}),
            ("multiclass", {"always": ["prediction", "prediction_probability", "prediction_prob_*"]}))},
    },
}

# The keys of a node's `data` outside `params`, as the gateway reads them (not checked here).
NODES: dict[str, dict[str, Any]] = {
    NODE_MODEL: {"data": ["model", "params", "mlflow", "timeoutSeconds", "retries"],
                 "mlflow": ["connectionId", "experiment", "registerAs", "continueWithout"]},
    NODE_PREDICT: {"data": ["connectionId", "experiment", "registeredModel", "version", "params",
                            "timeoutSeconds", "retries"]},
}


# ── reading a node's parameters ─────────────────────────────────────────────
def refusal(model: str, message: str) -> EngineError:
    """A model's refusal, led by the model's id."""
    return EngineError(f"{model}: {message}")


def column_kind(dtype: Any) -> str:
    """The kind of a column (`COLUMN_KINDS`) from its type as the engine names it («Int64»,
    «Float64», «String», «Boolean», «Date», «Datetime(time_unit='us', time_zone=None)»)."""
    name = str(getattr(dtype, "dtype", dtype))
    if name.startswith(("Int", "UInt")):
        return "integer"
    if name.startswith(("Float", "Decimal")):
        return "number"
    if name in ("String", "Utf8", "Categorical") or name.startswith("Enum"):
        return "text"
    if name == "Boolean":
        return "boolean"
    if name == "Date":
        return "date"
    if name.startswith("Datetime"):
        return "datetime"
    return "other"


def _is_number(v: Any) -> bool:
    return not isinstance(v, bool) and isinstance(v, (int, float)) and math.isfinite(v)


def _bounds_text(how: Mapping[str, Any]) -> str:
    parts = []
    if "above" in how:
        parts.append(f"above {_num(how['above'])}")
    if "min" in how:
        parts.append(f"at least {_num(how['min'])}")
    if "below" in how:
        parts.append(f"below {_num(how['below'])}")
    if "max" in how:
        parts.append(f"at most {_num(how['max'])}")
    return " and ".join(parts)


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def _within(how: Mapping[str, Any], v: float) -> bool:
    return (("above" not in how or v > how["above"]) and ("below" not in how or v < how["below"])
            and ("min" not in how or v >= how["min"]) and ("max" not in how or v <= how["max"]))


def _read_number(model: str, key: str, how: Mapping[str, Any], v: Any) -> float:
    if not _is_number(v) or not _within(how, float(v)):
        bounds = _bounds_text(how)
        raise refusal(model, f"'{key}' must be a number" + (f" {bounds}" if bounds else ""))
    return float(v)


def _read_level(model: str, key: str, v: Any) -> Any:
    if isinstance(v, str):
        if not v.strip():
            raise refusal(model, f"'{key}' must be a value of the column")
        return v
    if isinstance(v, bool) or _is_number(v):
        return v
    raise refusal(model, f"'{key}' must be a value of the column: a text, a number, true or false")


def read_value(model: str, key: str, kind: str, v: Any) -> Any:
    """A parameter's value as its kind's control reads it, without the columns: the
    gateway's check, word for word. A refusal is an EngineError with the sentence."""
    how = KINDS[kind]
    control = how["control"]
    if control == "column":
        if not isinstance(v, str) or not v:
            raise refusal(model, f"'{key}' must be the name of a column")
        return v
    if control == "columns":
        if not isinstance(v, list) or not all(isinstance(c, str) and c for c in v):
            raise refusal(model, f"'{key}' must be a list of column names")
        if len(set(v)) != len(v):
            raise refusal(model, f"'{key}' names a column twice")
        if len(v) < how.get("min", 0):
            raise refusal(model, f"'{key}' must name at least {how['min']} column{'' if how['min'] == 1 else 's'}")
        if "max" in how and len(v) > how["max"]:
            raise refusal(model, f"'{key}' may name at most {how['max']} columns")
        return list(v)
    if control == "level":
        return _read_level(model, key, v)
    if control == "reference":
        if not isinstance(v, dict):
            raise refusal(model, f"'{key}' must give a column its reference level, as {{column: level}}")
        return {str(c): _read_level(model, f"{key}.{c}", v[c]) for c in sorted(v)}
    if control == "pairs":
        if not isinstance(v, list):
            raise refusal(model, f"'{key}' must be a list of pairs of columns")
        seen: set[frozenset[str]] = set()
        out = []
        for pair in v:
            if (not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(c, str) and c for c in pair)
                    or pair[0] == pair[1]):
                raise refusal(model, f"'{key}' must be a list of pairs of two different columns")
            if frozenset(pair) in seen:
                raise refusal(model, f"'{key}' names the pair «{pair[0]}», «{pair[1]}» twice")
            seen.add(frozenset(pair))
            out.append(list(pair))
        if len(out) > how["max"]:
            raise refusal(model, f"'{key}' may hold at most {how['max']} pairs")
        return out
    if control == "choice":
        if not isinstance(v, str) or v not in how["choices"]:
            raise refusal(model, f"'{key}' must be one of {', '.join(how['choices'])}")
        return v
    if control == "number":
        return _read_number(model, key, how, v)
    if control == "integer":
        if not _is_number(v) or float(v) != math.floor(v) or not _within(how, float(v)):
            raise refusal(model, f"'{key}' must be a whole number from {how['min']} to {how['max']}")
        return int(v)
    if control == "boolean":
        if not isinstance(v, bool):
            raise refusal(model, f"'{key}' must be true or false")
        return v
    if control == "numbers":
        if not isinstance(v, list) or not all(_is_number(x) and _within(how, float(x)) for x in v):
            raise refusal(model, f"'{key}' must be a list of numbers, each {_bounds_text(how)}")
        if len(set(float(x) for x in v)) != len(v):
            raise refusal(model, f"'{key}' holds a number twice")
        if len(v) > how["max_count"]:
            raise refusal(model, f"'{key}' may hold at most {how['max_count']} numbers")
        return sorted(float(x) for x in v)
    if control == "number_or_choice":
        if isinstance(v, str) and v in how["choices"]:
            return v
        if _is_number(v) and _within(how, float(v)):
            return float(v)
        bounds = _bounds_text(how)
        raise refusal(model, f"'{key}' must be one of {', '.join(how['choices'])}, or a number"
                             + (f" {bounds}" if bounds else ""))
    raise AssertionError(f"unknown control {control}")


def _given(v: Any) -> bool:
    return v is not None and v != [] and v != {}


def _holds(rule: Any, value: Any) -> bool:
    """A rule of `applies` (a list of values, True: given, False: not given) or a value of a
    `when` (a value, or a list: one of them) against a parameter's value."""
    if rule is True:
        return _given(value)
    if rule is False:
        return not _given(value)
    if isinstance(rule, list):
        return value in rule
    return value == rule


def applies(model: str, key: str, checked: Mapping[str, Any]) -> bool:
    """Whether `key` matters with the other parameters as they are (the defaults count)."""
    rule = MODELS[model]["applies"].get(key)
    return rule is None or all(_holds(r, checked.get(k)) for k, r in rule.items())


def _when_text(when: Mapping[str, Any]) -> str:
    def one(k: str, r: Any) -> str:
        if r is True:
            return f"'{k}' is given"
        if r is False:
            return f"'{k}' is not given"
        if isinstance(r, list):
            return f"{k} = {' or '.join(_word(x) for x in r)}"
        return f"{k} = {_word(r)}"

    return " and ".join(one(k, r) for k, r in when.items())


def _word(v: Any) -> str:
    """A value of a rule as the sentences write it: JSON's true and false, as the gateway's."""
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


def _column_names(spec: Mapping[str, Any], key: str, value: Any) -> list[str]:
    control = KINDS[{**spec["needs"], **spec["takes"]}[key]]["control"]
    if control == "column":
        return [value] if isinstance(value, str) else []
    if control == "columns":
        return list(value or [])
    return []


def check_params(model: str, params: Mapping[str, Any] | None,
                 columns: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The parameters of a model node, checked against the catalogue, with the defaults of
    the keys that apply filled in (a key that does not apply is not in the answer). A key the
    model does not take is refused, as one it needs and lacks, one given while it does not
    apply, what `requires` and `forbids` say. With `columns` ({name: dtype}) the columns are
    checked too: that they are in the step's input and of a kind the key accepts."""
    if model not in MODELS:
        raise EngineError(f"there is no model '{model}' (the models: {', '.join(MODELS)})")
    spec = MODELS[model]
    kinds = {**spec["needs"], **spec["takes"]}
    given = {k: v for k, v in (params or {}).items() if v is not None}
    # in the catalogue's order (an unknown key: the first in text order), so that the
    # gateway, reading a JSON object without order, names the same key
    for key in sorted(given):
        if key not in kinds:
            raise refusal(model, f"there is no parameter '{key}' (it takes: {', '.join(kinds)})")
    for key in spec["needs"]:
        if key not in given:
            raise refusal(model, f"'{key}' is required")
    read = {key: read_value(model, key, kinds[key], given[key]) for key in kinds if key in given}
    full = {key: read.get(key, spec["defaults"].get(key)) for key in kinds}
    for key in read:
        if not applies(model, key, full):
            raise refusal(model, f"'{key}' only applies when {_when_text(spec['applies'][key])}")
    for rule in spec["requires"]:
        if all(_holds(v, full.get(k)) for k, v in rule["when"].items()) and not _given(full.get(rule["key"])):
            raise refusal(model, rule["message"])
    for rule in spec["forbids"]:
        if full.get(rule["key"]) == rule["value"] and all(_holds(v, full.get(k)) for k, v in rule["when"].items()):
            raise refusal(model, rule["message"])
    # what one key says of another: a reference level or a pair of predictors
    for key, kind in kinds.items():
        how = KINDS[kind]
        if how["control"] in ("reference", "pairs") and key in read:
            allowed = set(full.get(how["of"]) or [])
            named = list(read[key]) if how["control"] == "reference" else [c for pair in read[key] for c in pair]
            for c in named:
                if c not in allowed:
                    raise refusal(model, f"'{key}' names «{c}», which is not one of the '{how['of']}'")
    owner: dict[str, str] = {}
    for key in spec["disjoint"]:
        for c in _column_names(spec, key, full.get(key)):
            if c in owner:
                raise refusal(model, f"the column «{c}» is both '{owner[c]}' and '{key}': choose another")
            owner[c] = key
    if columns is not None:
        _check_columns(model, spec, kinds, full, columns)
    return {key: full[key] for key in kinds if applies(model, key, full)}


def _check_columns(model: str, spec: Mapping[str, Any], kinds: Mapping[str, str], full: Mapping[str, Any],
                   columns: Mapping[str, Any]) -> None:
    for key, kind in kinds.items():
        how = KINDS[kind]
        if how["control"] not in ("column", "columns") or not applies(model, key, full):
            continue
        for name in _column_names(spec, key, full.get(key)):
            if name not in columns:
                raise refusal(model, f"the column «{name}» ('{key}') is not in the step's input")
            ck = column_kind(columns[name])
            if how.get("numeric") and ck not in ("integer", "number"):
                raise refusal(model, f"the column «{name}» ('{key}') is not a number ({columns[name]}): "
                                     "convert it with a cast step, or choose another column")
            if "types" in how and ck not in how["types"]:
                raise refusal(model, f"the column «{name}» ('{key}') must be a {' or a '.join(how['types'])}")
            if how.get("predictor") and ck in ("date", "datetime"):
                raise refusal(model, f"the column «{name}» is a {ck} and cannot be a predictor: compute a number "
                                     "from the date first: the month, the days since a start")
            if how.get("predictor") and ck == "other":
                raise refusal(model, f"the column «{name}» ({columns[name]}) cannot be a predictor: "
                                     "numbers, texts and true or false can")
    output = full.get("output")
    variant_outputs = variant_spec(model, full)["outputs"]
    if output == "rows":
        clash = [c for c in variant_outputs["rows"] if c in columns]
        if clash:
            raise refusal(model, f"the input already has a column «{clash[0]}», which the model's rows add: rename it first")


def variant_spec(model: str, checked: Mapping[str, Any]) -> dict[str, Any]:
    """The variant of `model` its parameters make: the first whose `when` holds."""
    for v in MODELS[model]["variants"]:
        if all(checked.get(k, MODELS[model]["defaults"].get(k)) in values for k, values in v["when"].items()):
            return v
    raise AssertionError(f"{model}: no variant for {dict(checked)}")


def variant(model: str, checked: Mapping[str, Any]) -> str:
    """The id of the variant (`unpenalized`, `penalized`, `manual`, `auto`, `default`)."""
    return variant_spec(model, checked)["id"]


def arima_name(params: Mapping[str, Any]) -> str:
    """What an ARIMA node's orders make of it, as its title says: «AR(1)», «MA(2)»,
    «ARMA(1,1)», «ARIMA(1,1,1)», «SARIMA(1,1,1)(0,1,1)[12]», «SARIMAX(1,1,1)(0,1,1)[12]»
    (with exogenous columns and no season: «ARIMAX(1,1,1)»); «ARIMA (automatic orders)»."""
    full = {**MODELS["arima"]["defaults"], **{k: v for k, v in params.items() if v is not None}}
    if full["orders"] == "auto":
        return "ARIMA (automatic orders)"
    p, d, q, P, D, Q = (int(full[k]) for k in ("p", "d", "q", "P", "D", "Q"))
    s = full["s"] if full["s"] is not None else SEASON_OF_FREQUENCY.get(full.get("frequency"), 1)
    exog = bool(full["exog"])
    seasonal = s > 1 and (P or D or Q)
    if seasonal:
        return f"{'SARIMAX' if exog else 'SARIMA'}({p},{d},{q})({P},{D},{Q})[{s}]"
    if exog:
        return f"ARIMAX({p},{d},{q})"
    if d == 0 and q == 0:
        return f"AR({p})"
    if d == 0 and p == 0:
        return f"MA({q})"
    if d == 0:
        return f"ARMA({p},{q})"
    return f"ARIMA({p},{d},{q})"


# ── the snapshot: the catalogue as the other languages read it ──────────────
SNAPSHOT = Path(__file__).with_name("catalog.json")
FORMAT_VERSION = 1


def snapshot() -> dict[str, Any]:
    """The catalogue as plain data, in its order."""
    return {
        "format_version": FORMAT_VERSION,
        "nodes": NODES,
        "limits": LIMITS,
        "controls": list(CONTROLS),
        "column_kinds": list(COLUMN_KINDS),
        "kinds": KINDS,
        "season_of_frequency": SEASON_OF_FREQUENCY,
        "section_shapes": SECTION_SHAPES,
        "verdicts": list(VERDICTS),
        "groups": [{"id": g, **spec} for g, spec in GROUPS.items()],
        "models": [
            {
                "id": m,
                "group": spec["group"],
                "family": spec["family"],
                "label": f"models.{m}.name",
                "needs": spec["needs"],
                "takes": spec["takes"],
                "defaults": spec["defaults"],
                "applies": spec["applies"],
                "requires": spec["requires"],
                "forbids": spec["forbids"],
                "disjoint": spec["disjoint"],
                "presets": spec.get("presets", {}),
                "variants": spec["variants"],
            }
            for m, spec in MODELS.items()
        ],
        "predict": PREDICT,
    }


def snapshot_text() -> str:
    """The snapshot as it is written: `catalog.json` and its copies hold exactly this."""
    return json.dumps(snapshot(), indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    SNAPSHOT.write_text(snapshot_text(), encoding="utf-8")
    print(f"{SNAPSHOT.name}: {len(GROUPS)} groups, {len(MODELS)} models written")
