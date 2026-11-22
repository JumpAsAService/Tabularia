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
  columns of the rows, table or forecast output); optionally a `max_rows` of its own, below
  its model's and its group's (the risk simulation's ARIMA variant reads 20,000 rows).

`OUTPUT_TYPES` gives those columns their types, and `output_columns` what a node hands on
before it has run (the editor's and the assistants' columns of the steps after a model).
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from app.engine.exceptions import EngineError
from app.engine.stats.output import OUTPUT_COLUMNS

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
    "max_quantiles": 9,          # quantiles of a parametric survival node's time (besides the median)
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
    # the customer lifetime group's (docs/specs/customer-lifetime-models.md, «Performance and limits»)
    "min_customers": 20,         # customers a lifetime model needs
    "min_repeaters": 5,          # customers who bought again
    "max_opportunities": 1000,   # BG/BB's opportunities n, and its holdout's length
    "max_clv_steps": 520,        # periods summed one by one in a CLV; beyond it coarser steps
    "max_top_customers": 20,     # rows of the top customers' table
    "palive_grid_frequency": 10, # frequencies of the P(alive) map
    # the experiments group's (docs/specs/ab-testing.md, «Limits»)
    "max_variants": 20,          # values of the variant column, the control included
    "max_looks": 1000,           # days of the sequential looks; beyond it, equal periods of days
    "max_secondary_metrics": 10, # secondary metrics of an A/B node
    # a key of a map a person types ({value: number}, {column: choice}: a value of the variant column, a column's
    # name), in characters: as long as a file's name may be (255, the placeholders' MAX_NAME_BYTES), so that the
    # gateway and the engine bound what they read before the runner meets the data
    "max_key_chars": 255,
    "posterior_points": 200,     # points of a posterior density
    "power_points": 50,          # points of a power curve
    # the decomposition node's (docs/specs/ts-decomposition.md, «Performance and limits»)
    "max_period": 10_000,        # a seasonal period, in steps (hourly data with a yearly cycle is 8,766)
    "max_periods": 4,            # seasonal periods of one MSTL decomposition
    "max_imfs": 16,              # IMFs of an EMD (log2 of 200,000 rows is about 17.6)
    "max_lb_lags": 1000,         # lag of the Ljung–Box test, and lags of the remainder's ACF chart
    # a smoother's span (STL's seasonal and trend windows): stl's own periodic window for the group's longest series
    # (10 · 200,000 + 1); wider is no other smoother, and a larger number would not fit the Fortran's integers
    "max_window": 2_000_001,
    # the foundation forecast node's (docs/specs/foundation-forecast.md, «Performance and limits»). `max_horizon` is ARIMA's and
    # the lifetime nodes' 1,000 steps; Timestral's forecast is one forward pass of 32 patches of 32 steps
    "max_series": 10_000,            # series of one forecast (the values of the series column)
    "max_foundation_horizon": 1024,  # steps of a forecast, and of a holdout, of a pre-trained model
    "max_covariates": 16,            # covariate columns of a forecast, past and known together
    "max_series_charts": 12,         # series drawn in the report, the rest are in the table
    # the volatility (GARCH) and risk simulation nodes' (docs/specs/garch-risk.md, «Limits»). `seed_block` is part of the method,
    # like the seed: the paths are cut into blocks of this many, each drawn from its own stream; changing it changes the draws
    "seed_block": 1000,              # paths of a seed block
    "risk_bins": 1024,               # bins of the pass-1 histograms: only memory and the fan's declared error, never an exact measure
    "max_sample_paths": 20,          # paths 1…20 drawn in the fan
}

# ── groups: one image each, built when an administrator enables it ─────────
GROUPS: dict[str, dict[str, Any]] = {
    "regression": {"language": "r", "label": "models.groups.regression", "models": ["linear", "logistic"]},
    "survival": {"language": "r", "label": "models.groups.survival", "models": ["kaplan_meier", "cox", "parametric_survival"]},
    "timeseries": {"language": "r", "label": "models.groups.timeseries", "models": ["arima", "decomposition", "garch", "risk_simulation"]},
    "boosting": {"language": "python", "label": "models.groups.boosting", "models": ["xgboost", "catboost"]},
    "customer_lifetime": {"language": "r", "label": "models.groups.customer_lifetime", "models": ["clv", "bg_bb"]},
    "experiments": {"language": "r", "label": "models.groups.experiments", "models": ["ab_test", "ab_plan"]},
    "foundation_forecast": {"language": "python", "label": "models.groups.foundation_forecast", "models": ["timestral"]},
}

# ── controls and kinds ──────────────────────────────────────────────────────
# What a form draws, and how a value is read (the Go gateway reads the same):
#   column            one column; `numeric`: numbers only; `types`: the column kinds allowed
#   columns           distinct columns; `min`, `max` of them; `numeric`; `types`; `predictor`: numbers,
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
# `whole` (numbers, number_or_choice): each number is a whole one (a period, a window, a lag counted in steps): the
# engine, the gateway and the editor read it here, and refuse 7.5
#   level_numbers     a number for each value of the column the key `of` names, as {value: number},
#                     each within the number's bounds (the form lists the column's values with a
#                     number field each; the values themselves are the data's, known at the run)
#   column_choices    {column of the key `of`: one of `choices`}; a column it does not name keeps the
#                     node's own setting (the A/B test's direction of each secondary metric)
# `max_count` and `max_chars` (level_numbers, column_choices: both required): at most `max_count` keys, each at most
# `max_chars` characters long, read before any value (a limit of LIMITS each: the keys a map can usefully hold)
CONTROLS = ("column", "columns", "level", "reference", "pairs", "choice", "number", "integer",
            "boolean", "numbers", "number_or_choice", "level_numbers", "column_choices")

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
    "predictors_optional": {"control": "columns", "min": 0, "predictor": True},  # none or more
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
    "quantiles": {"control": "numbers", "above": 0, "below": 1, "max_count": LIMITS["max_quantiles"]},
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
    "distribution": _choice("weibull", "exponential", "lognormal", "loglogistic", "gengamma", "gompertz", "auto"),
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
    # the customer lifetime group's
    "time_unit": _choice("day", "week", "month", "quarter", "year"),
    "purchase_model": _choice("auto", "pareto_nbd", "bg_nbd", "mbg_nbd"),
    "horizon_periods": _int(1, LIMITS["max_horizon"]),
    "discount_rate": {"control": "number", "min": 0, "max": 1},
    "aic_margin": {"control": "number", "min": 0, "max": 10},  # parametric survival `auto`: AIC within this, the simpler one
    "holdout_opportunities": _int(1, LIMITS["max_opportunities"]),
    # the experiments group's (docs/specs/ab-testing.md, «The form»)
    "variant_column": {"control": "column", "types": ["text", "integer", "boolean", "date"]},
    "control_level": {"control": "level", "of": "variant"},
    "metric_type": _choice("binary", "continuous", "ratio"),
    "input_shape": _choice("units", "aggregated"),
    "metric_column": {"control": "column", "types": ["boolean", "integer", "number"]},
    "metric_columns": {"control": "columns", "min": 0, "max": LIMITS["max_secondary_metrics"],
                       "types": ["boolean", "integer", "number"]},
    "date_column": {"control": "column", "types": ["date", "datetime"]},
    "alternative": _choice("two_sided", "greater", "less"),
    "correction_ab": _choice("holm", "benjamini_hochberg", "bonferroni", "none"),
    "covariate_missing": _choice("fill", "exclude"),
    "cap_percentile": {"control": "number", "above": 0.5, "below": 1},
    "variant_shares": {"control": "level_numbers", "of": "variant", "above": 0, "max_count": LIMITS["max_variants"],
                       "max_chars": LIMITS["max_key_chars"]},
    "metric_directions": {"control": "column_choices", "of": "secondary_metrics", "choices": ["higher", "lower"],
                          "max_count": LIMITS["max_secondary_metrics"], "max_chars": LIMITS["max_key_chars"]},
    "relative": {"control": "number", "min": 0},
    "tau": {"control": "number", "above": 0},
    "share_open": {"control": "number", "above": 0, "max": 1},
    "solve_for": _choice("sample_size", "mde_for_units", "mde_for_days"),
    "arms": _int(2, LIMITS["max_variants"]),
    "days": _int(1, 3650),
    "units_per_variant": _int(2, 1_000_000_000),
    "output_variants": _choice("results", "variants"),
    "output_plan": _choice("results", "plan"),
    # the decomposition node's (docs/specs/ts-decomposition.md, «The form»)
    "decomposition_method": _choice("stl", "classical", "mstl", "emd"),
    "decomposition_type": _choice("additive", "multiplicative"),
    "period": _int(2, LIMITS["max_period"]),
    "periods": {"control": "numbers", "min": 2, "max": LIMITS["max_period"], "max_count": LIMITS["max_periods"], "whole": True},
    "s_window": {"control": "number_or_choice", "choices": ["periodic"], "min": 7, "max": LIMITS["max_window"], "whole": True},
    "s_windows": {"control": "numbers", "min": 7, "max": LIMITS["max_window"], "max_count": LIMITS["max_periods"], "whole": True},
    "t_window": {"control": "number_or_choice", "choices": ["auto"], "min": 3, "max": LIMITS["max_window"], "whole": True},
    "mstl_iterate": _int(1, 5),
    "max_imf": _int(1, LIMITS["max_imfs"]),
    "s_number": _int(0, 20),
    "max_siftings": _int(1, 1000),
    "noise_imfs": _int(0, LIMITS["max_imfs"]),
    "missing_values": _choice("refuse", "linear", "seasonal"),
    "seasonality_test": _choice("friedman", "kruskal_wallis", "none"),
    "lb_lag": {"control": "number_or_choice", "choices": ["auto"], "min": 1, "max": LIMITS["max_lb_lags"], "whole": True},
    "output_components": _choice("components", "results"),
    # the foundation forecast node's (docs/specs/foundation-forecast.md, «The form»)
    "series_column": {"control": "column", "types": ["text", "integer", "boolean"]},
    "season_steps": _int(1, LIMITS["max_period"]),
    "foundation_size": _choice("5m", "28m", "123m", "209m"),
    "foundation_horizon": _int(1, LIMITS["max_foundation_horizon"]),
    "foundation_holdout": _int(0, LIMITS["max_foundation_horizon"]),
    # the 21 levels the model forecasts, the median (its own column) left out: 20 at most. That a level is one of them is
    # the runner's refusal (`quantile_level`): a list of numbers has no set of values to choose from
    "foundation_quantiles": {"control": "numbers", "above": 0, "below": 1, "max_count": 20},
    # the covariate columns of both lists: at most 16 each here; 16 together is the runner's and the gateway's check
    "covariate_columns": {"control": "columns", "min": 0, "max": LIMITS["max_covariates"],
                          "types": ["number", "integer", "boolean", "text"]},
    "missing_foundation": _choice("refuse", "linear", "model"),
    "output_quantile_forecast": _choice("quantile_forecast", "results"),
    # the volatility (GARCH) and risk simulation nodes' (docs/specs/garch-risk.md, «The form»)
    "input_kind": _choice("returns", "prices"),
    "returns_type": _choice("log", "simple"),
    "variance_model": _choice("arch", "garch", "gjr", "egarch"),
    "arch_order": _int(1, 3),        # lags of the shocks: rugarch's garchOrder[1], q
    "garch_order": _int(0, 3),       # lags of the variance: rugarch's garchOrder[2], p
    "arma_order": _int(0, 5),        # AR and MA lags of the mean
    "garch_distribution": _choice("norm", "std", "sstd"),
    "se_garch": _choice("robust", "classical"),
    "risk_level": {"control": "number", "above": 0.5, "below": 1},
    "output_volatility": _choice("volatility", "results"),
    "risk_model": _choice("garch", "arima"),
    "aggregate": _choice("last", "sum", "relative_change"),
    "paths": _int(100, 10_000_000),
    "innovations": _choice("parametric", "bootstrap"),
    "tail": _choice("lower", "upper"),
    # a level in the fan's unit and an amount invested: finite, within ±1e15 less one (a bound of the kind, never an overflow; the
    # last whole number the sentences write without an exponent)
    "risk_threshold": {"control": "number", "min": -999_999_999_999_999, "max": 999_999_999_999_999},
    "risk_position": {"control": "number", "above": 0, "max": 999_999_999_999_999},
    "output_risk": _choice("risk_summary", "paths", "results"),
}

# The seasonal period a frequency implies when `s` is not given.
SEASON_OF_FREQUENCY = {"hour": 24, "day": 7, "week": 52, "month": 12, "quarter": 4, "year": 1, "index": 1}

# ── what the sections of the ten columns hold (docs: «The output table») ────
# Per section: whether `level` and `level_2` are null, required or optional, and whether its
# rows are points of a chart (their key ends with an index; `unindexed` stats excepted).
SECTION_SHAPES: dict[str, dict[str, Any]] = {
    "model": {"level": "null", "level_2": "null", "indexed": False},
    "coef": {"level": "required", "level_2": "optional", "indexed": False},
    # a distribution's own parameter (`shape`, `sdlog`…), per stratum when there are strata
    "parameter": {"level": "required", "level_2": "optional", "indexed": False},
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
    # the customer lifetime group's: a customer of the top table, its id as the label
    "customer": {"level": "required", "level_2": "null", "indexed": False},
    # the experiments group's: a variant; a treatment against the control; a variant's or a treatment's
    # analysis; a metric against a treatment; a block of the report's explanation (and a variant); a
    # lift of the planning grid
    "variant": {"level": "required", "level_2": "null", "indexed": False},
    "comparison": {"level": "required", "level_2": "required", "indexed": False},
    "bayes": {"level": "required", "level_2": "null", "indexed": False},
    "sequential": {"level": "required", "level_2": "null", "indexed": False},
    "secondary": {"level": "required", "level_2": "required", "indexed": False},
    "plan": {"level": "required", "level_2": "null", "indexed": False},
    "reading": {"level": "required", "level_2": "optional", "indexed": False},
    # the decomposition node's: a seasonal period; an intrinsic mode function (or the residue)
    "season": {"level": "required", "level_2": "null", "indexed": False},
    "imf": {"level": "required", "level_2": "null", "indexed": False},
    # the foundation forecast's: a series (its measures); a line of a series' chart: the series, then the line
    "series": {"level": "required", "level_2": "null", "indexed": False},
    "series_curve": {"level": "required", "level_2": "required", "indexed": True},
    # the volatility and risk nodes': a backtest's test or count (a test per level); a risk measure, at the horizon (no level_2)
    # or at a step (level_2 the step)
    "backtest": {"level": "required", "level_2": "null", "indexed": False},
    "risk": {"level": "required", "level_2": "optional", "indexed": False},
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
        # its own limit of input rows, below the group's (200,000): auto.arima's cost, not the series' (decided 2026-11-21);
        # the gateway checks it before the Job with the group's sentence, the smaller of the two winning
        "max_rows": 20_000,
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
    # `advice_id`: ADF and KPSS at an end of tseries' table (the p-value is not known beyond it) say so with an id
    "assumption": {**_assumptions("adf", "kpss", "stationarity", "roots", "ljung_box_10", "ljung_box_2s", "arch_lm",
                                  "variance_ratio", "jarque_bera"), "texts": [*_ASSUMPTION["texts"], "advice_id"]},
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

# ── the decomposition node (docs/specs/ts-decomposition.md, «The node `decomposition`») ──────────────────────
# One node, four variants keyed on `method`, because what the runner writes differs: STL, the classical moving
# average and MSTL give a trend, a seasonal column per period and a remainder; EMD gives intrinsic mode functions
# and a residue. It makes no model: `card: false`, so nothing is registered or predicted, and MLflow logs the run.
_DEC_METHODS = ["stl", "classical", "mstl", "emd"]
_DEC_CHECKS = ["seasonality", "ljung_box", "adf", "kpss", "trend_strength", "seasonal_strength", "largest_acf"]
_DEC_EMD_CHECKS = ["imf_condition", "residue_monotonic", "orthogonality"]
_DEC_ASSUMPTION = {"levels": None, "numbers": [*_ASSUMPTION["numbers"], "lag"], "texts": [*_ASSUMPTION["texts"], "advice_id"]}
_DEC_MODEL_TEXTS = [*_MODEL_TEXTS, "method", "frequency", "transform", "time_kind", "missing", "seasonality_test", "periods"]
_DEC_MODEL_NUMBERS = ["n", "n_missing", "n_filled", "first", "last", "alpha", "box_cox_lambda", "lb_lag_used"]
_DEC_READING_BLOCKS = ["summary", "strength", "seasonality", "season", "remainder", "stationarity", "imfs", "method", "formulas"]
# the columns the next step receives (output `components`): the time keeps the input's own type
_DEC_SEASONAL_OUTPUT = ["time", "observed", "transformed", "filled", "trend", "seasonal_*", "remainder", "deseasonalized"]
_DEC_EMD_OUTPUT = ["time", "observed", "transformed", "filled", "imf_*", "residue"]
_DEC_ROWS_WHEN = {"transformed": {"transform": ["log", "box_cox"]}}


def _dec_variant(method: str) -> dict[str, Any]:
    """`stl`, `classical`, `mstl` or `emd` of the decomposition node: what the runner may write."""
    emd = method == "emd"
    own_texts = {"classical": ["type"], "emd": ["boundary"]}.get(method, [])
    own_numbers = {"stl": ["t_window_used"], "mstl": ["t_window_used", "iterate"],
                   "emd": ["max_imf", "n_imfs", "s_number", "max_siftings", "noise_imfs"]}.get(method, [])
    checks = [*_DEC_CHECKS, *(_DEC_EMD_CHECKS if emd else []), *(["robust_weights"] if method in ("stl", "mstl") else [])]
    sections: dict[str, Any] = {
        "model": {"levels": None, "texts": [*_DEC_MODEL_TEXTS, *own_texts], "numbers": [*_DEC_MODEL_NUMBERS, *own_numbers]},
        # the seasonal period's shape and, with the calendar's names, its peak and trough
        "season": {"levels": None, "texts": ["position_kind", "peak_label", "trough_label"],
                   "numbers": ["s_window_used", "cycles", "amplitude", "peak_position", "peak_value", "trough_position",
                               "trough_value"]},
        **({"imf": {"levels": None, "texts": [],
                    "numbers": ["mean_period", "mean_period_time", "variance_share", "energy_share", "extrema",
                                "zero_crossings"]}} if emd else {}),
        "assumption": {**_DEC_ASSUMPTION, "levels": checks},
        "curve": _chart(*(["imfs"] if emd else ["components", "adjusted"]), "subseries", stats=["y", "mean", "low", "high"]),
        "point": _chart("remainder", stats=["y"]),
        "lag": _chart("resid_acf", stats=["acf", "bound"]),
        "hist": _chart("remainder", stats=["count"]),
        "reading": {"levels": [b for b in _DEC_READING_BLOCKS if emd or b != "imfs"], "numbers": [], "texts": ["line_*"]},
    }
    charts = [*([{"id": "imfs", "under": None}] if emd else [{"id": "components", "under": None}, {"id": "adjusted", "under": None}]),
              {"id": "subseries", "under": "seasonality"}, {"id": "resid_acf", "under": "ljung_box"},
              {"id": "remainder", "under": "adf"}]
    return {"id": method, "when": {"method": [method]}, "sections": sections, "charts": charts,
            "outputs": {"components": list(_DEC_EMD_OUTPUT if emd else _DEC_SEASONAL_OUTPUT)}, "rows_when": dict(_DEC_ROWS_WHEN)}


MODELS["decomposition"] = {
    "group": "timeseries",
    "family": "timeseries",
    "card": False,
    "needs": {"time": "time_column", "value": "number_column", "frequency": "frequency"},
    "takes": {
        "method": "decomposition_method", "period": "period", "periods": "periods", "type": "decomposition_type",
        "robust": "boolean", "s_window": "s_window", "s_windows": "s_windows", "t_window": "t_window",
        "iterate": "mstl_iterate", "max_imf": "max_imf", "s_number": "s_number", "max_siftings": "max_siftings",
        "noise_imfs": "noise_imfs", "transform": "transform", "box_cox_lambda": "box_cox_lambda",
        "missing": "missing_values", "seasonality_test": "seasonality_test", "lb_lag": "lb_lag", "alpha": "alpha",
        "output": "output_components",
    },
    "defaults": {
        "method": "stl", "period": None, "periods": [], "type": "additive", "robust": False, "s_window": 11.0,
        "s_windows": [], "t_window": "auto", "iterate": 2, "max_imf": 10, "s_number": 4, "max_siftings": 50,
        "noise_imfs": 0, "transform": "none", "box_cox_lambda": "auto", "missing": "refuse",
        "seasonality_test": "friedman", "lb_lag": "auto", "alpha": 0.05, "output": "components",
    },
    "applies": {
        "period": {"method": ["stl", "classical", "emd"]},
        "periods": {"method": ["mstl"]},
        "type": {"method": ["classical"]},
        "robust": {"method": ["stl", "mstl"]},
        "s_window": {"method": ["stl"]},
        "s_windows": {"method": ["mstl"]},
        "t_window": {"method": ["stl", "mstl"]},
        "iterate": {"method": ["mstl"]},
        **{k: {"method": ["emd"]} for k in ("max_imf", "s_number", "max_siftings", "noise_imfs")},
        "box_cox_lambda": {"transform": ["box_cox"]},
    },
    "requires": [
        {"key": "period", "when": {"frequency": ["year", "index"], "method": ["stl", "classical"]}, "reason": "period_needed",
         "message": "a yearly or index series has no season of its own: give the period, in steps ('period')"},
        {"key": "periods", "when": {"method": "mstl"}, "reason": "periods_needed",
         "message": "an MSTL decomposition needs its seasonal periods, in steps ('periods')"},
    ],
    "forbids": [
        # a multiplicative decomposition already works in ratios
        *({"key": "transform", "value": t, "when": {"method": "classical", "type": "multiplicative"},
           "reason": "multiplicative_transform",
           "message": "a multiplicative decomposition already works in ratios: choose no transform, or an additive decomposition of the log"}
          for t in ("log", "box_cox")),
    ],
    "disjoint": ["time", "value"],
    "variants": [_dec_variant(m) for m in _DEC_METHODS],
}

# ── the volatility (GARCH) node and the risk simulation node (docs/specs/garch-risk.md) ──────────────────────────────
# Both are in the group `timeseries`. GARCH makes a model (a card, so it registers and predicts: lot G6); the risk simulation
# fits its own ARIMA or GARCH with the same options and the same runner code, simulates paths and makes no model:
# `card: false`, MLflow logs the run. The GARCH node draws nothing; the risk node's simulation is its product.
_GARCH_KEYS = {
    "input_kind": "input_kind", "returns_type": "returns_type", "variance_model": "variance_model", "arch_order": "arch_order",
    "garch_order": "garch_order", "ar_order": "arma_order", "ma_order": "arma_order", "distribution": "garch_distribution",
    "se_type": "se_garch",
}
_GARCH_DEFAULTS = {"input_kind": "returns", "returns_type": "log", "variance_model": "garch", "arch_order": 1, "garch_order": 1,
                   "ar_order": 0, "ma_order": 0, "distribution": "std", "se_type": "robust"}
# `returns_type` matters with prices (the node computes the returns); `garch_order` is 0 for an ARCH, so it is not asked
_GARCH_APPLIES = {"returns_type": {"input_kind": ["prices"]}, "garch_order": {"variance_model": ["garch", "gjr", "egarch"]}}
_GARCH_CHECKS = ["ljung_box_z", "ljung_box_z2", "arch_lm", "nyblom", "sign_bias", "at_bound", "variance_stationary"]
_GARCH_MODEL_TEXTS = [*_MODEL_TEXTS, "model_name", "variance_model", "distribution", "input_kind", "returns_type", "se_type",
                      "frequency", "time_kind", "solver"]
_GARCH_MODEL_NUMBERS = ["n", "n_missing", "first", "last", "alpha", "scale", "holdout", "horizon", "var_level", "log_lik", "aic",
                        "bic", "persistence", "half_life", "half_life_time", "uncond_sigma", "sigma_last", "sigma_next",
                        "lb_lag_used", "arch_lags_used", "kappa"]
# Nyblom's individual rows are one per parameter (`level_2`), Holm-adjusted; `lag` is the portmanteau tests' m
_GARCH_ASSUMPTION = {"levels": _GARCH_CHECKS, "numbers": [*_ASSUMPTION["numbers"], "lag", "p_adjusted"],
                     "texts": [*_ASSUMPTION["texts"], "advice_id"]}
_BACKTEST = {"levels": ["kupiec", "christoffersen_ind", "christoffersen_cc", "counts"],
             "numbers": ["statistic", "df", "p_value", "violations", "expected_violations", "violation_rate", "n00", "n01", "n10",
                         "n11", "longest_run"],
             "texts": ["significance", "verdict", "h0"]}
_GARCH_COEF = {"levels": None, "numbers": ["estimate", "std_error", "statistic", "p_value", "ci_low", "ci_high"],
               "texts": ["significance"]}
_GARCH_READING_BLOCKS = ["summary", "persistence", "asymmetry", "distribution", "residuals", "stability", "backtest", "method",
                         "formulas"]
_GARCH_SECTIONS: dict[str, Any] = {
    "model": {"levels": None, "texts": _GARCH_MODEL_TEXTS, "numbers": _GARCH_MODEL_NUMBERS},
    "coef": _GARCH_COEF,
    "assumption": _GARCH_ASSUMPTION,
    "backtest": _BACKTEST,
    # a chart per level; a line of it per series (`level_2`), its value `y`, the time (or ε, for the news impact) in x0
    "curve": _chart("returns", "sigma", "news_impact", "backtest", "density", stats=["y"]),
    "point": _chart("std_residual", "violation", "qq", stats=["y"]),
    "lag": _chart("z_acf", "z2_acf", stats=["acf", "bound"]),
    "hist": _chart("std_residual", stats=["count"]),
    "reading": {"levels": _GARCH_READING_BLOCKS, "numbers": [], "texts": ["line_*"]},
}
_GARCH_CHARTS = [
    {"id": "returns", "under": None}, {"id": "sigma", "under": None}, {"id": "std_residual", "under": None},
    {"id": "news_impact", "under": None}, {"id": "z_acf", "under": "ljung_box_z"}, {"id": "z2_acf", "under": "ljung_box_z2"},
    {"id": "std_residual_hist", "under": "arch_lm"}, {"id": "qq", "under": "arch_lm"}, {"id": "backtest", "under": None},
]
# the columns the next step receives (output `volatility`): the time keeps the input's own type; `return` only with prices
_GARCH_OUTPUT = ["time", "observed", "return", "mean", "sigma", "std_residual", "var", "violation", "is_holdout", "is_forecast"]

MODELS["garch"] = {
    "group": "timeseries",
    "family": "timeseries",
    "needs": {"time": "time_column", "value": "number_column", "frequency": "frequency"},
    "takes": {
        **{k: _GARCH_KEYS[k] for k in ("input_kind", "returns_type", "variance_model", "arch_order", "garch_order", "ar_order", "ma_order")},
        "mean": "boolean", "distribution": _GARCH_KEYS["distribution"], "se_type": _GARCH_KEYS["se_type"],
        "horizon": "horizon", "holdout": "holdout", "var_level": "risk_level", "lb_lag": "lb_lag", "arch_lags": "lb_lag",
        "alpha": "alpha", "seed": "seed", "output": "output_volatility",
    },
    "defaults": {**_GARCH_DEFAULTS, "mean": True, "horizon": 10, "holdout": 0, "var_level": 0.99, "lb_lag": "auto",
                 "arch_lags": "auto", "alpha": 0.05, "seed": 1, "output": "volatility"},
    "applies": dict(_GARCH_APPLIES),
    "requires": [],
    "forbids": [],
    "disjoint": ["time", "value"],
    "variants": [
        {
            "id": "default",
            "when": {},
            "sections": _GARCH_SECTIONS,
            "charts": _GARCH_CHARTS,
            "outputs": {"volatility": list(_GARCH_OUTPUT)},
            "rows_when": {"return": {"input_kind": ["prices"]}},
        },
    ],
}

# The risk node's model keys are the two nodes' own: ARIMA's (but for `exog`: a future exogenous value cannot be simulated,
# and `horizon`, which the risk node has of its own) shown when `model = arima`, GARCH's when `model = garch`; `mean` is one key.
_RISK_ARIMA_KEYS = ("s", "orders", "p", "d", "q", "P", "D", "Q", "criterion", "stepwise", "max_p", "max_q", "max_P", "max_Q",
                    "max_d", "max_D", "unit_root_test", "seasonal_test", "drift", "transform", "box_cox_lambda")
_RISK_ARIMA = MODELS["arima"]
_RISK_RESULT_TEXTS = ["risk_variable", "tail", "innovations", "aggregate"]
_RISK_RESULT_NUMBERS = ["paths", "horizon", "seed", "seed_block", "batches", "batch_paths", "bins", "var_level", "es_level",
                        "threshold", "position", "last_value", "simulated_values", "paths_bytes", "seconds_simulation",
                        "seconds_pass_2"]
_RISK_READING_BLOCKS = ["summary", "var", "es", "threshold", "drawdown", "shape", "fan", "backtest", "model", "method", "formulas"]
# the risk measures, at the horizon (no level_2) or at a step (level_2 = the step); the moments and the drawdown's are of H_h
_RISK = {"levels": ["var", "es", "prob_beyond", "prob_crossed", "drawdown", "moments"],
         "numbers": ["value", "low", "high", "se", "count", "mean", "sd", "skewness", "kurtosis", "median"], "texts": []}
# the fan (nested bands, the median, the history, the sample paths `path_<i>`), the measures per step, the backtest's VaR;
# `band_error`: the declared error of the fan's band edges at that step
_RISK_CURVE = _chart("fan", "term", "backtest", stats=["y", "band_error"])
_RISK_HIST = _chart("horizon", "drawdown", stats=["count"])
_RISK_CHARTS = [{"id": "fan", "under": None}, {"id": "horizon", "under": None}, {"id": "drawdown", "under": None},
                {"id": "term", "under": None}, {"id": "backtest", "under": None}]
_RISK_SUMMARY_OUTPUT = ["step", "time", "mean", "sd", "quantile_*", "price_quantile_*", "price_mean", "var", "var_low", "var_high",
                        "es", "es_low", "es_high", "prob_beyond", "prob_crossed", "band_error"]
_RISK_PATHS_OUTPUT = ["path", "step", "time", "value", "cumulative", "price", "sigma"]
# columns written only with their rule: the prices' columns for a GARCH of prices, the threshold's two with a threshold
_RISK_ROWS_WHEN = {
    "price_quantile_*": {"model": ["garch"], "input_kind": ["prices"]}, "price_mean": {"model": ["garch"], "input_kind": ["prices"]},
    "price": {"model": ["garch"], "input_kind": ["prices"]}, "sigma": {"model": ["garch"]},
    "prob_beyond": {"threshold": True}, "prob_crossed": {"threshold": True},
}


def _risk_sections(model: str) -> dict[str, Any]:
    """The sections of the risk node's `garch` or `arima` variant: the fitted model's own (compact) and the risk's."""
    if model == "garch":
        fit = {"model": {"levels": None, "texts": [*_GARCH_MODEL_TEXTS, *_RISK_RESULT_TEXTS],
                         "numbers": [*_GARCH_MODEL_NUMBERS, *(n for n in _RISK_RESULT_NUMBERS if n not in _GARCH_MODEL_NUMBERS)]},
               "coef": _GARCH_COEF, "assumption": _GARCH_ASSUMPTION}
    else:
        arima = _ARIMA_AUTO_SECTIONS
        fit = {"model": {"levels": None, "texts": [*arima["model"]["texts"], *_RISK_RESULT_TEXTS],
                         "numbers": [*arima["model"]["numbers"], *(n for n in _RISK_RESULT_NUMBERS if n not in arima["model"]["numbers"])]},
               "coef": arima["coef"], "assumption": arima["assumption"], "candidate": arima["candidate"]}
    return {**fit, "risk": _RISK, "backtest": _BACKTEST, "curve": _RISK_CURVE, "hist": _RISK_HIST,
            "reading": {"levels": _RISK_READING_BLOCKS, "numbers": [], "texts": ["line_*"]}}


def _risk_variant(model: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": model, "when": {"model": [model]}, "sections": _risk_sections(model), "charts": list(_RISK_CHARTS),
        "outputs": {"risk_summary": list(_RISK_SUMMARY_OUTPUT), "paths": list(_RISK_PATHS_OUTPUT)},
        "rows_when": {k: dict(v) for k, v in _RISK_ROWS_WHEN.items()},
    }
    if model == "arima":
        # ARIMA's own limit of rows, as its cost (decided 2026-11-21): the node of the GARCH variant has the group's alone
        out["max_rows"] = _RISK_ARIMA["max_rows"]
    return out


MODELS["risk_simulation"] = {
    "group": "timeseries",
    "family": "timeseries",
    "card": False,
    "needs": {"time": "time_column", "value": "number_column", "frequency": "frequency"},
    "takes": {
        "model": "risk_model",
        **_GARCH_KEYS,
        **{k: _RISK_ARIMA["takes"][k] for k in _RISK_ARIMA_KEYS},
        "mean": "boolean", "aggregate": "aggregate", "paths": "paths", "horizon": "horizon", "innovations": "innovations",
        "var_level": "risk_level", "es_level": "risk_level", "tail": "tail", "quantiles": "quantiles",
        "threshold": "risk_threshold", "position": "risk_position", "drawdown": "boolean", "holdout": "holdout",
        "alpha": "alpha", "seed": "seed", "lb_lag": "lb_lag", "arch_lags": "lb_lag", "output": "output_risk",
    },
    "defaults": {
        "model": "garch", **_GARCH_DEFAULTS, **{k: _RISK_ARIMA["defaults"][k] for k in _RISK_ARIMA_KEYS},
        "mean": True, "aggregate": "last", "paths": 10_000, "horizon": 10, "innovations": "parametric", "var_level": 0.99,
        "es_level": 0.975, "tail": "lower", "quantiles": [0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99], "threshold": None,
        "position": None, "drawdown": True, "holdout": 0, "alpha": 0.05, "seed": 1, "lb_lag": "auto", "arch_lags": "auto",
        "output": "risk_summary",
    },
    "applies": {
        **{k: {"model": ["garch"], **_GARCH_APPLIES.get(k, {})} for k in _GARCH_KEYS},
        **{k: {"model": ["arima"], **_RISK_ARIMA["applies"].get(k, {})} for k in _RISK_ARIMA_KEYS},
        "aggregate": {"model": ["arima"]},
        # `position` and `drawdown` matter when the risk variable is a relative change: a GARCH's always, an ARIMA's with
        # `aggregate = relative_change`. `applies` holds only when every rule does, so it cannot say «or»: they apply
        # always, and the runner says, with a sentence, that a position needs a relative change (G3)
    },
    "requires": [],
    "forbids": [],
    "disjoint": ["time", "value"],
    "variants": [_risk_variant("garch"), _risk_variant("arima")],
}


def garch_name(params: Mapping[str, Any]) -> str:
    """What a GARCH node's options make of it, as its title says: «GARCH(1,1), t», «GJR-GARCH(1,1), skewed t»,
    «AR(1)-EGARCH(1,1), normal», «ARCH(2), t». The orders are Bollerslev's, GARCH(p, q): p lags of the variance
    (`garch_order`), q of the shocks (`arch_order`); an ARCH has q alone."""
    full = {**MODELS["garch"]["defaults"], **{k: v for k, v in params.items() if v is not None}}
    q, p = int(full["arch_order"]), int(full["garch_order"])
    m, n = int(full["ar_order"]), int(full["ma_order"])
    kind = full["variance_model"]
    core = f"ARCH({q})" if kind == "arch" else {"garch": "GARCH", "gjr": "GJR-GARCH", "egarch": "EGARCH"}[kind] + f"({p},{q})"
    mean = "" if not (m or n) else f"AR({m})" if not n else f"MA({n})" if not m else f"ARMA({m},{n})"
    law = {"norm": "normal", "std": "t", "sstd": "skewed t"}[full["distribution"]]
    return f"{mean + '-' if mean else ''}{core}, {law}"


# ── parametric survival (docs: «The node «Parametric survival»») ────────────
# One node, four variants keyed on `distribution`, because what the runner writes differs: the
# Weibull and the exponential have time ratios and hazard ratios, the other AFT distributions time
# ratios only, Gompertz hazard ratios only, `auto` the union (the chosen one decides at run time).
# A variant declares what the runner MAY write: a check that does not apply to the distribution
# fitted is not written at all.
_PS_CHECKS_COMMON = ["convergence", "epv", "separation", "km_agreement", "distribution_vs_gengamma", "dfbeta", "vif",
                     "extrapolation"]
_PS_PH_CHECKS = ["ph_cox", "ph_cox_global"]
_PS_MODEL_TEXTS = [*_MODEL_TEXTS, "event_value", "distribution", "selection", "coefficient_scale", "profile",
                   "lr_significance"]
_PS_MODEL_NUMBERS = ["n", "events", "alpha", "censored", "n_missing", "n_zero_time", "n_delayed", "n_strata", "n_parameters", "log_lik", "aic",
                     "bic", "lr_statistic", "lr_df", "lr_p_value", "concordance", "concordance_se", "last_time",
                     "last_event_time"]
_PS_SHAPE_LR = ["exponential_lr_statistic", "exponential_lr_df", "exponential_lr_p_value"]  # the shape against a constant hazard
_PS_COEF_BASE = ["estimate", "std_error", "statistic", "p_value", "vif"]
_PS_TIME_RATIO = ["time_ratio", "tr_ci_low", "tr_ci_high"]
_PS_HAZARD_RATIO = ["hazard_ratio", "hr_ci_low", "hr_ci_high", "cox_hazard_ratio", "cox_hr_ci_low", "cox_hr_ci_high"]
_PS_CHARTS = [
    {"id": "fit_km", "under": "km_agreement"}, {"id": "cox_snell", "under": "km_agreement"},
    {"id": "profile_survival", "under": None}, {"id": "hazard", "under": None},
    {"id": "deviance_linear", "under": "dfbeta"},
]


def _ps_assumptions(*checks: str) -> dict[str, Any]:
    """The checks' rows: an assumption's, `advice_id` (the report picks the sentence by it), and for a
    measure, which has a number and no verdict, `description` and `at` (the time the number is read at)."""
    shape = _assumptions(*checks)
    return {**shape, "numbers": [*shape["numbers"], "at"], "texts": [*shape["texts"], "advice_id", "description"]}


def _ps_variant(id_: str, distributions: list[str], model_numbers: list[str], model_texts: list[str],
                coef: list[str], checks: list[str], charts: list[dict[str, Any]], candidates: bool = False) -> dict[str, Any]:
    sections: dict[str, Any] = {
        "model": {"levels": None, "texts": [*_PS_MODEL_TEXTS, *model_texts], "numbers": [*_PS_MODEL_NUMBERS, *model_numbers]},
        "coef": {"levels": None, "numbers": coef, "texts": ["significance"]},
        "parameter": {"levels": None, "numbers": ["estimate", "std_error", "ci_low", "ci_high"], "texts": []},
        "stratum": {"levels": None, "numbers": ["n", "events", "last_time", "median", "median_ci_low", "median_ci_high",
                                                "quantile_*", "surv_at_*", "km_median"], "texts": []},
    }
    if candidates:
        sections["candidate"] = {"levels": None, "texts": ["status"],
                                 "numbers": ["n_parameters", "log_lik", "aic", "bic", "delta_aic", "akaike_weight",
                                             "converged", "admissible", "chosen", "parsimony"]}
    sections["assumption"] = _ps_assumptions(*checks)
    loglog = any(c["id"] == "loglog" for c in charts)  # the Weibull's and the exponential's chart
    curve = ["fit_km", *(["loglog"] if loglog else []), "profile_survival", "hazard", *(["candidates"] if candidates else [])]
    sections["point"] = _chart("cox_snell", *(["loglog"] if loglog else []), "deviance_linear", stats=["y"])
    sections["curve"] = _chart(*curve, stats=["surv", "lower", "upper", "fitted", "hazard", "y"])
    return {
        "id": id_,
        "when": {"distribution": distributions},
        "sections": sections,
        "charts": charts,
        # the intervals of the median, of each quantile and of each time are columns of their own (`_low`, `_high`)
        "outputs": {"rows": ["pred_linear", "pred_median", "pred_median_low", "pred_median_high", "pred_quantile_*",
                             "pred_quantile_*_low", "pred_quantile_*_high", "pred_surv_at_*", "pred_surv_at_*_low",
                             "pred_surv_at_*_high", "pred_cox_snell", "pred_martingale", "pred_deviance"]},
    }


MODELS["parametric_survival"] = {
    "group": "survival",
    "family": "survival",
    "needs": {"time": "number_column", "event": "column", "event_value": "event_level"},
    "takes": {"predictors": "predictors_optional", "reference": "reference", "interactions": "interactions",
              "strata": "column", "weights": "number_column", "start": "number_column", "distribution": "distribution",
              "quantiles": "quantiles", "times": "times", "alive_at": "number_column", "aic_margin": "aic_margin", "alpha": "alpha",
              "seed": "seed", "output": "output_rows"},
    "defaults": {"predictors": [], "reference": {}, "interactions": [], "strata": None, "weights": None, "start": None,
                 "distribution": "weibull", "quantiles": [0.25, 0.75], "times": [], "alpha": 0.05, "seed": 1,
                 "output": "results", "alive_at": None, "aic_margin": 2.0},
    "applies": {"reference": {"predictors": True}, "interactions": {"predictors": True},
                "aic_margin": {"distribution": ["auto"]}},
    "requires": [],
    "forbids": [],
    "disjoint": ["time", "event", "start", "predictors", "strata", "weights"],
    "variants": [
        _ps_variant("ph_aft", ["weibull", "exponential"], [*_PS_SHAPE_LR], ["exponential_lr_significance"],
                    [*_PS_COEF_BASE, *_PS_TIME_RATIO, *_PS_HAZARD_RATIO],
                    [*_PS_CHECKS_COMMON, "constant_hazard", *_PS_PH_CHECKS],
                    [{"id": "time_ratio_interval", "under": None}, {"id": "hazard_ratio_interval", "under": None},
                     *_PS_CHARTS, {"id": "loglog", "under": "km_agreement"}]),
        _ps_variant("aft", ["lognormal", "loglogistic", "gengamma"], [], [],
                    [*_PS_COEF_BASE, *_PS_TIME_RATIO], _PS_CHECKS_COMMON,
                    [{"id": "time_ratio_interval", "under": None}, *_PS_CHARTS]),
        _ps_variant("ph", ["gompertz"], [*_PS_SHAPE_LR], ["exponential_lr_significance"],
                    [*_PS_COEF_BASE, *_PS_HAZARD_RATIO],
                    [*[c for c in _PS_CHECKS_COMMON if c != "distribution_vs_gengamma"], *_PS_PH_CHECKS],
                    [{"id": "hazard_ratio_interval", "under": None}, *_PS_CHARTS]),
        _ps_variant("auto", ["auto"], [*_PS_SHAPE_LR, "aic_margin"], ["exponential_lr_significance"],
                    [*_PS_COEF_BASE, *_PS_TIME_RATIO, *_PS_HAZARD_RATIO],
                    [*_PS_CHECKS_COMMON, "constant_hazard", *_PS_PH_CHECKS],
                    [{"id": "time_ratio_interval", "under": None}, {"id": "hazard_ratio_interval", "under": None},
                     *_PS_CHARTS, {"id": "loglog", "under": "km_agreement"}, {"id": "candidates", "under": None}],
                    candidates=True),
    ],
}

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


def _boosting_assumptions(*checks: str) -> dict[str, Any]:
    """The diagnostics' rows: an assumption's, and `advice_id`, the id of the advice the report
    says in its own words (its sentence may change, the id does not)."""
    shape = _assumptions(*checks)
    return {**shape, "texts": [*shape["texts"], "advice_id"]}
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
            "assumption": _boosting_assumptions(*_BOOSTING_CHECKS),
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
            "assumption": _boosting_assumptions(*_BOOSTING_CHECKS, "class_imbalance", "calibration", "calibration_slope"),
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
            "assumption": _boosting_assumptions(*_BOOSTING_CHECKS, "class_imbalance"),
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
    {"l2_leaf_reg": "positive", "rsm": "fraction", "random_strength": "strength"},
    {"l2_leaf_reg": 3.0, "rsm": 1.0, "random_strength": 1.0},
    "depth_catboost")

# ── customer lifetime (docs/specs/customer-lifetime-models.md, «The two nodes», «The output») ──
# Two nodes, `clv` (continuous time) and `bg_bb` (discrete opportunities), each with two variants
# keyed on whether a spend column is given: `purchases` (no Gamma–Gamma, no CLV) and `value`. The
# purchase model (Pareto/NBD, BG/NBD, MBG/NBD, auto) is a text of the `model` section, not a variant.
_CLV_PURCHASE_MODELS = ["pareto_nbd", "bg_nbd", "mbg_nbd"]
_CLV_CHECKS = ["convergence", "boundary", "sample_size", "frequency_fit", "holdout", "horizon_length"]
_CLV_VALUE_CHECKS = ["spend_independence", "spend_tail"]
_CLV_MODEL_TEXTS = [*_MODEL_TEXTS, "purchase_model", "purchase_model_asked", "time_unit", "optimizer"]
_CLV_MODEL_NUMBERS = [
    "n", "n_missing", "n_repeaters", "share_repeaters", "mean_frequency", "max_age", "median_age", "horizon", "log_lik",
    "aic", "bic", "n_params", "converged", "param_*", "mean_rate", "mean_dropout", "expected_transactions",
    "mean_p_alive", "holdout_length", "holdout_observed", "holdout_expected", "holdout_rel_error", "holdout_mae",
    "holdout_rmse", "holdout_correlation",
]
_CLV_VALUE_NUMBERS = [
    # the Gamma–Gamma's p, q and gamma are `spend_param_*`: BG/BB's own gamma is `param_gamma`
    "spend_n", "spend_log_lik", "spend_param_*", "mean_spend_population", "discount_rate", "discount_per_period", "margin", "clv_total",
    "clv_mean", "clv_median", "clv_top10_share", "clv_top20_share", "discounted_transactions",
]
_CLV_CUSTOMER = ["rank", "frequency", "recency", "age", "p_alive", "expected_transactions"]
_CLV_VALUE_CUSTOMER = ["expected_spend", "clv"]


def _clv_variant(id_: str, node: str, value: bool) -> dict[str, Any]:
    """`purchases` or `value` of a customer lifetime node. A variant declares what the runner MAY
    write: the comparison of purchase models (`candidate`) only with `clv`; in `purchases` the
    charts of the CLV are drawn on the expected transactions, under the same ids."""
    checks = [*_CLV_CHECKS, *(_CLV_VALUE_CHECKS if value else [])]
    hist = ["frequency", "holdout", "p_alive", "clv", *(["spend"] if value else [])]
    curve = ["p_alive_recency", "concentration", *(["spend_frequency"] if value else [])]
    assumption = _assumptions(*checks)
    assumption = {**assumption, "texts": [*assumption["texts"], "advice_id"]}
    sections: dict[str, Any] = {
        "model": {"levels": None, "texts": _CLV_MODEL_TEXTS,
                  "numbers": [*_CLV_MODEL_NUMBERS, *(_CLV_VALUE_NUMBERS if value else [])]},
        "coef": {"levels": None, "numbers": ["estimate", "std_error", "ci_low", "ci_high"], "texts": []},
    }
    if node == "clv":
        sections["candidate"] = {"levels": list(_CLV_PURCHASE_MODELS), "texts": [],
                                 "numbers": ["log_lik", "aic", "bic", "n_params", "chi_square", "chi_square_p_value",
                                             "holdout_rel_error", "holdout_mae", "converged", "chosen", "param_*"]}
    sections.update({
        "assumption": assumption,
        "hist": _chart(*hist, stats=["count", "mean"]),
        "cell": {"levels": None, "numbers": ["p_alive"], "texts": []},
        "curve": _chart(*curve, stats=["y"]),
    })
    if value:
        sections["point"] = _chart("spend_frequency", stats=["y"])
    sections["customer"] = {"levels": None, "numbers": [*_CLV_CUSTOMER, *(_CLV_VALUE_CUSTOMER if value else [])], "texts": []}
    charts = [{"id": "frequency", "under": "frequency_fit"}, {"id": "holdout", "under": "holdout"},
              {"id": "p_alive_cell", "under": None}, {"id": "p_alive_recency", "under": None},
              {"id": "p_alive", "under": None}]
    if value:
        charts += [{"id": "spend", "under": "spend_tail"}, {"id": "spend_frequency", "under": "spend_independence"}]
    charts += [{"id": "clv", "under": None}, {"id": "concentration", "under": None}]
    rows = ["pred_p_alive", "pred_transactions", "pred_holdout_transactions"]
    if value:
        rows += ["pred_spend", "pred_discounted_transactions", "pred_clv"]
    # `rows_when`: a column of the rows output the runner writes only when its rule holds (as a `when`)
    return {"id": id_, "when": {"spend": value}, "sections": sections, "charts": charts, "outputs": {"rows": rows},
            "rows_when": {"pred_holdout_transactions": {"holdout_transactions": True}}}


def _clv_node(needs: dict[str, str], holdout_length: str, extra_takes: dict[str, str],
              extra_defaults: dict[str, Any], extra_applies: dict[str, Any], node: str) -> dict[str, Any]:
    takes = {"spend": "number_column", **extra_takes, "horizon": "horizon_periods", "discount_rate": "discount_rate",
             "margin": "fraction", "holdout_transactions": "number_column", "holdout_length": holdout_length,
             "alpha": "alpha", "seed": "seed", "output": "output_rows"}
    return {
        "group": "customer_lifetime",
        "family": "customer_lifetime",
        "needs": needs,
        "takes": takes,
        "defaults": {"spend": None, **extra_defaults, "horizon": None, "discount_rate": 0.1, "margin": 1.0,
                     "holdout_transactions": None, "holdout_length": None, "alpha": 0.05, "seed": 1, "output": "rows"},
        "applies": {**extra_applies, "discount_rate": {"spend": True}, "margin": {"spend": True},
                    "holdout_length": {"holdout_transactions": True}},
        "requires": [{"key": "holdout_length", "when": {"holdout_transactions": True}, "reason": "holdout_needs_length",
                      "message": "the holdout's purchases need the holdout's length ('holdout_length')"}],
        "forbids": [],
        "disjoint": ["customer", "frequency", "recency", *([k for k in needs if k in ("age", "opportunities")]), "spend",
                     "holdout_transactions"],
        "variants": [_clv_variant("value", node, True), _clv_variant("purchases", node, False)],
    }


MODELS["clv"] = _clv_node(
    {"customer": "column", "frequency": "number_column", "recency": "number_column", "age": "number_column",
     "time_unit": "time_unit"},
    "positive", {"purchase_model": "purchase_model", "compare": "boolean"},
    {"purchase_model": "auto", "compare": False}, {"compare": {"purchase_model": _CLV_PURCHASE_MODELS}}, "clv")
MODELS["bg_bb"] = _clv_node(
    {"customer": "column", "frequency": "number_column", "recency": "number_column", "opportunities": "number_column",
     "period_unit": "time_unit"},
    "holdout_opportunities", {}, {}, {}, "bg_bb")

# ── experiments (docs/specs/ab-testing.md, «The node ab_test», «The node ab_plan», «The output») ──
# Two nodes, `ab_test` and `ab_plan`, each with three variants, the metric's type: `binary`, `continuous`,
# `ratio` (they decide which numbers the sections hold; the analyses a variant MAY write — Bayes, sequential,
# CUPED, the cap, secondary metrics — are declared in each). Neither makes a model: `card: false`, so no
# model card, no registration as a model, no Predict; MLflow logs the run.
_AB_METRIC_TYPES = ["binary", "continuous", "ratio"]
_AB_CHECKS = ["srm", "covariate_balance", "covariate_missing_balance", "crossover", "variance_zero"]
_AB_MODEL_TEXTS = [*_MODEL_TEXTS, "metric_type", "input_shape", "control", "alternative", "correction", "covariate_missing",
                   "decision", "decision_variants"]
_AB_MODEL_NUMBERS = ["alpha", "srm_alpha", "n", "n_assigned", "n_missing", "n_variants", "n_crossover", "n_covariate_filled",
                     "n_covariate_excluded", "theta", "covariate_correlation", "min_lift", "power", "loss_threshold",
                     "sequential_tau", "n_looks", "first_day", "last_day"]
_AB_VARIANT_NUMBERS = ["is_control", "n", "n_assigned", "n_missing", "share", "expected_share", "mean", "std_dev", "std_error",
                       "ci_low", "ci_high", "mean_unadjusted", "std_error_unadjusted", "covariate_mean", "variance_reduction",
                       "n_covariate_filled", "n_covariate_excluded", "covariate_fill_value"]
_AB_COMPARISON_NUMBERS = ["difference", "difference_std_error", "difference_ci_low", "difference_ci_high", "lift",
                          "lift_std_error", "lift_ci_low", "lift_ci_high", "statistic", "p_value", "p_adjusted", "mde",
                          "power_at_min_lift"]
_AB_COMPARISON_TEXTS = ["significance", "p_adjusted_significance", "decision", "min_lift_position"]
_AB_BAYES_NUMBERS = ["posterior_mean", "posterior_sd", "cred_low", "cred_high", "prob_best", "expected_loss_best",
                     "expected_loss_best_relative", "prob_beat_control", "expected_loss", "expected_loss_relative",
                     "expected_loss_control", "expected_loss_control_relative"]
_AB_SEQUENTIAL_NUMBERS = ["p_value", "p_adjusted", "log_lambda", "cs_low", "cs_high", "first_crossing"]
_AB_READING_BLOCKS = ["decision", "srm", "comparison", "cuped", "cap", "bayes", "sequential", "power", "secondary", "method",
                      "formulas"]
_AB_VARIANTS_OUTPUT = ["metric", "role", "variant", "is_control", "units", "share", "expected_share", "mean", "std_error",
                       "ci_low", "ci_high", "difference", "difference_ci_low", "difference_ci_high", "lift", "lift_ci_low",
                       "lift_ci_high", "p_value", "p_adjusted", "decision", "prob_beat_control", "prob_best", "expected_loss",
                       "sequential_p_value", "sequential_p_adjusted", "sequential_lift_low", "sequential_lift_high"]
_AB_PLAN_OUTPUT = ["lift", "mde_absolute", "n_per_variant", "n_control", "n_total", "days", "weeks", "n_per_variant_cuped",
                   "days_cuped", "asked"]
_AB_PLAN_READING_BLOCKS = ["plan", "baseline", "cuped", "correction", "method", "formulas"]


def _ab_test_variant(metric_type: str) -> dict[str, Any]:
    """`binary`, `continuous` or `ratio` of the A/B test node: what the runner may write. The cap and the
    histogram of the metric are a continuous metric's; the Beta prior a binary metric's; Welch's degrees of
    freedom a continuous metric's (z has none); a ratio metric has its numerator's and denominator's sums."""
    binary, continuous, ratio = (metric_type == t for t in _AB_METRIC_TYPES)
    model_numbers = [*_AB_MODEL_NUMBERS, *(["prior_alpha", "prior_beta"] if binary else []),
                     *(["cap_value", "n_capped"] if continuous else [])]
    variant_numbers = [*_AB_VARIANT_NUMBERS, *(["skewness", "n_capped"] if continuous else []),
                       *(["conversions"] if binary else []), *(["numerator_sum", "denominator_sum"] if ratio else [])]
    comparison = [*_AB_COMPARISON_NUMBERS, *(["df"] if continuous else [])]
    assumption = {**_assumptions(*_AB_CHECKS), "texts": [*_ASSUMPTION["texts"], "advice_id"]}
    sections: dict[str, Any] = {
        "model": {"levels": None, "texts": _AB_MODEL_TEXTS, "numbers": model_numbers},
        "variant": {"levels": None, "numbers": variant_numbers, "texts": []},
        "comparison": {"levels": None, "numbers": comparison, "texts": _AB_COMPARISON_TEXTS},
        "bayes": {"levels": None, "numbers": _AB_BAYES_NUMBERS, "texts": ["loss_position"]},
        "sequential": {"levels": None, "numbers": _AB_SEQUENTIAL_NUMBERS, "texts": ["significance", "decision"]},
        "secondary": {"levels": None, "numbers": [*comparison, "mean_treatment", "mean_control"],
                      "texts": [*_AB_COMPARISON_TEXTS, "metric_type", "direction"]},
        "reading": {"levels": [b for b in _AB_READING_BLOCKS if continuous or b != "cap"], "numbers": [], "texts": ["line_*"]},
        "assumption": assumption,
        "curve": _chart("srm_share", "daily_units", "sequential_lift", "sequential_p", "posterior", "power",
                        stats=["y", "low", "high"]),
    }
    if continuous:
        sections["hist"] = _chart("metric", stats=["count"])
    charts = [{"id": "lift_interval", "under": None}, {"id": "srm_units", "under": "srm"}, {"id": "srm_share", "under": "srm"},
              {"id": "daily_units", "under": None}, *([{"id": "histogram", "under": None}] if continuous else []),
              {"id": "power", "under": None}, {"id": "posterior", "under": None}, {"id": "sequential_lift", "under": None},
              {"id": "sequential_p", "under": None}]
    return {"id": metric_type, "when": {"metric_type": [metric_type]}, "sections": sections, "charts": charts,
            "outputs": {"variants": list(_AB_VARIANTS_OUTPUT)}}


def _ab_plan_variant(metric_type: str) -> dict[str, Any]:
    """`binary`, `continuous` or `ratio` of the planning node. No check (nothing here is a test)."""
    continuous = metric_type == "continuous"
    sections: dict[str, Any] = {
        "model": {"levels": None,
                  "texts": [*_MODEL_TEXTS, "metric_type", "input_shape", "solve_for", "alternative", "correction",
                            "covariate_missing", "history_short"],
                  "numbers": ["alpha", "n", "n_missing", "min_lift", "arms", "control_share", "traffic_share", "baseline_mean",
                              "baseline_sd", "n_history", "history_days", "daily_units", "alpha_per_comparison", "n_per_variant",
                              "n_control", "n_total", "days", "weeks", "mde", "mde_absolute", "power", "covariate_correlation",
                              "variance_reduction", "n_covariate_filled", "n_covariate_excluded", "n_per_variant_cuped",
                              "n_total_cuped", "days_cuped", "mde_cuped", *(["cap_value", "n_capped"] if continuous else [])]},
        "plan": {"levels": None, "numbers": list(_AB_PLAN_OUTPUT), "texts": []},
        "reading": {"levels": list(_AB_PLAN_READING_BLOCKS), "numbers": [], "texts": ["line_*"]},
        "curve": _chart("power_lift", "mde_days", "n_lift", stats=["y"]),
        "hist": _chart("metric", stats=["count"]),
    }
    charts = [{"id": "power_lift", "under": None}, {"id": "mde_days", "under": None}, {"id": "history", "under": None}]
    return {"id": metric_type, "when": {"metric_type": [metric_type]}, "sections": sections, "charts": charts,
            "outputs": {"plan": list(_AB_PLAN_OUTPUT)}}


# the keys both nodes read their metric from, and the rules around them
_AB_INPUT_TAKES = {"input_shape": "input_shape", "unit": "column", "metric": "metric_column", "numerator": "number_column",
                   "denominator": "number_column", "units_count": "number_column", "metric_sum": "number_column",
                   "metric_sum_squares": "number_column"}
_AB_INPUT_DEFAULTS = {"input_shape": "units", "unit": None, "metric": None, "numerator": None, "denominator": None,
                      "units_count": None, "metric_sum": None, "metric_sum_squares": None}
_AB_INPUT_APPLIES = {
    "unit": {"input_shape": ["units"]},
    "metric": {"input_shape": ["units"], "metric_type": ["binary", "continuous"]},
    "numerator": {"input_shape": ["units"], "metric_type": ["ratio"]},
    "denominator": {"input_shape": ["units"], "metric_type": ["ratio"]},
    "units_count": {"input_shape": ["aggregated"]},
    "metric_sum": {"input_shape": ["aggregated"]},
    "metric_sum_squares": {"input_shape": ["aggregated"], "metric_type": ["continuous"]},
    "covariate": {"input_shape": ["units"]},
    "covariate_missing": {"covariate": True},
    "cap_percentile": {"input_shape": ["units"], "metric_type": ["continuous"]},
}
_AB_INPUT_REQUIRES = [
    {"key": "metric", "when": {"input_shape": "units", "metric_type": ["binary", "continuous"]}, "reason": "metric_needed",
     "message": "the primary metric needs its column ('metric')"},
    {"key": "numerator", "when": {"input_shape": "units", "metric_type": "ratio"}, "reason": "numerator_needed",
     "message": "a ratio metric needs the column of its numerator ('numerator')"},
    {"key": "denominator", "when": {"input_shape": "units", "metric_type": "ratio"}, "reason": "denominator_needed",
     "message": "a ratio metric needs the column of its denominator ('denominator')"},
    {"key": "units_count", "when": {"input_shape": "aggregated"}, "reason": "units_count_needed",
     "message": "aggregated rows need the column of their units ('units_count')"},
    {"key": "metric_sum", "when": {"input_shape": "aggregated"}, "reason": "metric_sum_needed",
     "message": "aggregated rows need the column of their metric's sum ('metric_sum')"},
    {"key": "metric_sum_squares", "when": {"input_shape": "aggregated", "metric_type": "continuous"},
     "reason": "sum_squares_needed",
     "message": "the variance of a continuous metric needs the sum of its squares ('metric_sum_squares')"},
]
_AB_RATIO_FORBID = {"key": "input_shape", "value": "aggregated", "when": {"metric_type": "ratio"}, "reason": "ratio_needs_units",
                    "message": "a ratio metric needs one row per unit: the delta method uses each unit's numerator and denominator"}
_AB_DISJOINT = ["variant", "unit", "metric", "numerator", "denominator", "covariate", "date", "units_count", "metric_sum",
                "metric_sum_squares", "secondary_metrics"]

MODELS["ab_test"] = {
    "group": "experiments",
    "family": "experiments",
    "card": False,
    "needs": {"variant": "variant_column", "control": "control_level", "metric_type": "metric_type"},
    "takes": {
        **_AB_INPUT_TAKES, "higher_is_better": "boolean", "alternative": "alternative", "alpha": "alpha",
        "correction": "correction_ab", "covariate": "number_column", "covariate_missing": "covariate_missing",
        "cap_percentile": "cap_percentile", "srm_alpha": "proportion", "expected_shares": "variant_shares",
        "min_lift": "relative", "power": "proportion", "bayes": "boolean", "prior_alpha": "positive", "prior_beta": "positive",
        "loss_threshold": "relative", "date": "date_column", "sequential": "boolean", "sequential_tau": "tau",
        "secondary_metrics": "metric_columns", "secondary_directions": "metric_directions", "output": "output_variants",
    },
    "defaults": {
        **_AB_INPUT_DEFAULTS, "higher_is_better": True, "alternative": "two_sided", "alpha": 0.05, "correction": "holm",
        "covariate": None, "covariate_missing": "fill", "cap_percentile": None, "srm_alpha": 0.001, "expected_shares": {},
        "min_lift": 0.02, "power": 0.8, "bayes": True, "prior_alpha": 1.0, "prior_beta": 1.0, "loss_threshold": 0.001,
        "date": None, "sequential": False, "sequential_tau": 0.05, "secondary_metrics": [], "secondary_directions": {},
        "output": "results",
    },
    "applies": {
        **_AB_INPUT_APPLIES,
        "secondary_metrics": {"input_shape": ["units"]},
        # each secondary metric's own direction, «higher» or «lower» is better; one not named reads the primary's
        "secondary_directions": {"secondary_metrics": True},
        "prior_alpha": {"bayes": [True], "metric_type": ["binary"]},
        "prior_beta": {"bayes": [True], "metric_type": ["binary"]},
        "loss_threshold": {"bayes": [True]},
        "sequential_tau": {"sequential": [True]},
    },
    "requires": [*_AB_INPUT_REQUIRES],
    "forbids": [
        _AB_RATIO_FORBID,
        # the always-valid p-value of this node is two-sided: one rule for each one-sided alternative
        *({"key": "alternative", "value": side, "when": {"sequential": [True]}, "reason": "sequential_two_sided",
           "message": "the always-valid p-value of this node is two-sided"} for side in ("greater", "less")),
    ],
    "disjoint": list(_AB_DISJOINT),
    "variants": [_ab_test_variant(t) for t in _AB_METRIC_TYPES],
}

MODELS["ab_plan"] = {
    "group": "experiments",
    "family": "experiments",
    "card": False,
    "needs": {"metric_type": "metric_type"},
    "takes": {
        **_AB_INPUT_TAKES, "cap_percentile": "cap_percentile", "covariate": "number_column",
        "covariate_missing": "covariate_missing", "date": "date_column", "solve_for": "solve_for", "min_lift": "positive",
        "units_per_variant": "units_per_variant", "days": "days", "arms": "arms", "control_share": "proportion",
        "traffic_share": "share_open", "alpha": "alpha", "power": "proportion", "alternative": "alternative",
        "correction": "correction_ab", "output": "output_plan",
    },
    "defaults": {
        **_AB_INPUT_DEFAULTS, "cap_percentile": None, "covariate": None, "covariate_missing": "fill", "date": None,
        "solve_for": "sample_size", "min_lift": 0.02, "units_per_variant": None, "days": None, "arms": 2,
        "control_share": None, "traffic_share": 1.0, "alpha": 0.05, "power": 0.8, "alternative": "two_sided",
        "correction": "holm", "output": "results",
    },
    "applies": {
        **_AB_INPUT_APPLIES,
        "min_lift": {"solve_for": ["sample_size"]},
        "units_per_variant": {"solve_for": ["mde_for_units"]},
        "days": {"solve_for": ["mde_for_days"]},
        "traffic_share": {"date": True},
    },
    "requires": [
        *_AB_INPUT_REQUIRES,
        {"key": "units_per_variant", "when": {"solve_for": "mde_for_units"}, "reason": "units_per_variant_needed",
         "message": "the lift detectable with a number of units needs the units per variant ('units_per_variant')"},
        {"key": "days", "when": {"solve_for": "mde_for_days"}, "reason": "days_needed",
         "message": "the lift detectable in a number of days needs the days ('days')"},
        {"key": "date", "when": {"solve_for": "mde_for_days"}, "reason": "days_need_date",
         "message": "the lift detectable in a number of days needs the column of the day each unit first appeared ('date')"},
    ],
    "forbids": [_AB_RATIO_FORBID],
    "disjoint": [k for k in _AB_DISJOINT if k not in ("variant", "secondary_metrics")],
    "variants": [_ab_plan_variant(t) for t in _AB_METRIC_TYPES],
}

# ── the foundation forecast node (docs/specs/foundation-forecast.md, «The node `timestral`») ────────────────────────────
# Forecast with a pre-trained model: nothing is fitted, so `card: false` (nothing registered, no Predict) and MLflow logs the
# run. One variant: what the runner may write is the same whatever the options; the sections that depend on the holdout and on
# the series' count are declared here and written when the run has them (a variant declares what the runner MAY write).
_FF_READING_BLOCKS = ["summary", "forecast", "holdout", "coverage", "comparison", "limits", "licence", "method", "formulas"]
_FF_MEASURES = ["coverage_80", "coverage_90", "relative_mase"]  # assumption rows that are measures, with no verdict
_FF_MODEL_TEXTS = [*_MODEL_TEXTS, "frequency", "time_kind", "missing", "size", "weights", "weights_sha256", "licence",
                   "licence_sha256", "cpu_capability"]
_FF_MODEL_NUMBERS = ["n", "n_series", "n_points", "n_missing", "n_filled", "n_model_missing", "n_future_rows", "horizon", "holdout",
                     "season", "alpha", "threads", "batches", "seconds_load", "seconds_forecast", "n_quantiles", "mase",
                     "baseline_mase", "relative_mase_geomean", "smape", "baseline_smape", "wql", "coverage_80", "coverage_90",
                     "n_measured",
                     # what the means leave out, counted (lot F9): sMAPE's steps where the value and the forecast are both 0
                     # (Timestral's, the seasonal naïve's), the series whose relative MASE the geometric mean cannot take
                     "n_smape_zero", "n_baseline_smape_zero", "n_relative_left_out"]
_FF_SERIES_NUMBERS = ["n", "n_used", "n_missing", "n_filled", "first", "last", "horizon", "scale", "mase", "baseline_mase",
                      "relative_mase", "smape", "baseline_smape", "pinball", "wql", "coverage_80", "coverage_90", "total"]
_FF_OUTPUT = ["series", "time", "observed", "forecast", "quantile_*", "baseline", "filled", "is_holdout", "is_forecast"]

MODELS["timestral"] = {
    "group": "foundation_forecast",
    "family": "timeseries",
    "card": False,
    "needs": {"time": "time_column", "value": "number_column", "frequency": "frequency"},
    "takes": {
        "series": "series_column", "season": "season_steps", "size": "foundation_size", "horizon": "foundation_horizon",
        "quantiles": "foundation_quantiles", "past_covariates": "covariate_columns", "known_covariates": "covariate_columns",
        "holdout": "foundation_holdout", "missing": "missing_foundation", "symmetric_averaging": "boolean",
        "clip_nonnegative": "boolean", "history": "boolean", "alpha": "alpha", "output": "output_quantile_forecast",
    },
    # `season`: None is the frequency's own; `holdout`: None is the horizon (with known covariates, the future rows' count)
    "defaults": {
        "series": None, "season": None, "size": "28m", "horizon": 28, "quantiles": [0.05, 0.1, 0.9, 0.95], "past_covariates": [],
        "known_covariates": [], "holdout": None, "missing": "refuse", "symmetric_averaging": True, "clip_nonnegative": True,
        "history": True, "alpha": 0.05, "output": "quantile_forecast",
    },
    "applies": {
        # with known covariates the horizon is the future rows the input holds
        "horizon": {"known_covariates": False},
        "history": {"output": ["quantile_forecast"]},
    },
    "requires": [],
    "forbids": [],
    "disjoint": ["time", "value", "series", "past_covariates", "known_covariates"],
    "variants": [{
        "id": "default",
        "when": {},
        "sections": {
            "model": {"levels": None, "texts": _FF_MODEL_TEXTS, "numbers": _FF_MODEL_NUMBERS},
            "series": {"levels": None, "numbers": _FF_SERIES_NUMBERS, "texts": ["not_measured"]},
            # rank_biserial: (W+ − W−) / (W+ + W−) of the comparison, whose sign is the side the test leans to (lot F9)
            "assumption": {**_assumptions("baseline_comparison", *_FF_MEASURES), "texts": [*_ASSUMPTION["texts"], "advice_id"],
                           "numbers": [*_ASSUMPTION["numbers"], "rank_biserial"]},
            "series_curve": {"levels": None, "numbers": ["y"], "texts": []},
            "reading": {"levels": list(_FF_READING_BLOCKS), "numbers": [], "texts": ["line_*"]},
        },
        "charts": [{"id": "forecast", "under": None}, {"id": "holdout", "under": None},
                   {"id": "relative_mase", "under": "baseline_comparison"}],
        "outputs": {"quantile_forecast": list(_FF_OUTPUT)},
        "rows_when": {"series": {"series": True}},
    }],
}

# the models in the order of their groups: the palette, the sentences and the snapshot list them so
MODELS = {m: MODELS[m] for g in GROUPS.values() for m in g["models"]}

# ── Predict: a registered model applied to new rows ─────────────────────────
PREDICT: dict[str, Any] = {
    "takes": {"interval": "boolean", "threshold": "proportion", "horizon": "horizon", "at_times": "times",
              "quantiles": "quantiles", "alive_at": "number_column"},
    # None: the card's own (its threshold, its horizon); an empty list: the card's times, quantiles
    "defaults": {"interval": False, "threshold": None, "horizon": None, "at_times": [], "quantiles": [], "alive_at": None},
    # which models each key means something for; given for another model it is refused. An
    # entry is a model, or `model:variant` for one variant of it (the card's variant)
    "applies_to": {"interval": ["linear", "kaplan_meier", "arima", "parametric_survival"],
                   "threshold": ["logistic", "xgboost:binary", "catboost:binary"],
                   "horizon": ["arima", "garch", "clv", "bg_bb"], "at_times": ["cox", "parametric_survival"],
                   "quantiles": ["parametric_survival"], "alive_at": ["parametric_survival"]},
    # the columns Predict adds, per model of the card (or `model:variant`, which comes first):
    # always, and with each key given. `prediction_prob_*`: one column per class of the card
    "columns": {
        "linear": {"always": ["prediction"], "interval": ["prediction_low", "prediction_high"]},
        "logistic": {"always": ["prediction"], "threshold": ["prediction_class"]},
        "kaplan_meier": {"always": ["prediction"], "interval": ["prediction_low", "prediction_high"]},
        "cox": {"always": ["prediction"], "at_times": ["survival_at_*"]},
        # the median time; a time past the fit's last one is predicted (the distribution's shape gives it)
        # a key `a+b` is given when both options are: its `*` is one per entry of `a` (the interval of each quantile, of each time)
        "parametric_survival": {"always": ["prediction"], "quantiles": ["quantile_*"], "at_times": ["survival_at_*"],
                                "interval": ["prediction_low", "prediction_high"],
                                "quantiles+interval": ["quantile_*_low", "quantile_*_high"],
                                "at_times+interval": ["survival_at_*_low", "survival_at_*_high"]},
        "arima": {"always": ["prediction"], "interval": ["lower_80", "upper_80", "lower", "upper"]},
        # the volatility of each new row given the rows before it, and the 1-step VaR at the card's level; with `horizon`, forecast
        # rows after the last (docs/specs/garch-risk.md, «Card, MLflow and Predict»: the runner is lot G6's, the contract G0's)
        "garch": {"always": ["prediction", "prediction_var"]},
        # the expected transactions over the horizon, P(alive); with a spend part the expected spend and the CLV
        **{f"{m}:{v}": cols for m in ("clv", "bg_bb") for v, cols in (
            ("purchases", {"always": ["prediction", "prediction_p_alive"]}),
            ("value", {"always": ["prediction", "prediction_p_alive", "prediction_spend", "prediction_clv"]}))},
        **{f"{m}:{v}": cols for m in ("xgboost", "catboost") for v, cols in (
            ("regression", {"always": ["prediction"]}),
            ("binary", {"always": ["prediction", "prediction_class"]}),
            ("multiclass", {"always": ["prediction", "prediction_probability", "prediction_prob_*"]}))},
    },
}

# ── the types of what a model node hands on (docs: «The output») ──────────────
# Per output, the type of each column the model writes, as the engine names it when it reads
# the runner's Parquet file (`column_kind` reads these names): the editor and the assistants
# show a node after a model with these columns before the model has run. A name with a `*`
# stands for every name it makes (`pred_quantile_*`: one per quantile asked; `pred_prob_*`: one
# per class of the data, known only after a run). «@<key>»: the type of the input's column the
# key names, a whole number written as Int32 (no output uses it since lot D7: ARIMA's time kept its type).
# «=<key>»: the exact type of the input's column the key names, whatever it is (the experiments'
# variant: an Int64 stays Int64, a date a date; the time of ARIMA's forecast and of the decomposition).
# `results` is the statistics' ten columns. What the runners really write is in
# backend/tests/fixtures/models/output_columns.json, made by running them, and the engine's
# test checks these types against it.
OUTPUT_TYPES: dict[str, dict[str, str]] = {
    "results": {name: kind for name, kind, _ in OUTPUT_COLUMNS},
    "rows": {
        **{name: "Float64" for name in (
            "pred_fitted", "pred_residual", "pred_std_residual", "pred_leverage", "pred_cooks_distance", "pred_low",
            "pred_high", "pred_prob", "pred_linear", "pred_deviance_residual", "pred_pearson_residual", "pred_risk",
            "pred_martingale", "pred_deviance", "pred_median", "pred_median_low", "pred_median_high", "pred_quantile_*",
            "pred_quantile_*_low", "pred_quantile_*_high", "pred_surv_at_*", "pred_surv_at_*_low", "pred_surv_at_*_high",
            "pred_cox_snell", "pred_value", "pred_prob_*", "pred_p_alive", "pred_transactions", "pred_holdout_transactions",
            "pred_spend", "pred_discounted_transactions", "pred_clv")},
        "pred_class": "String",  # the outcome's value as a text, whatever the outcome's type
        "pred_part": "String",   # train or validation, empty for a row the fit did not use
    },
    "table": {"stratum": "String", "time": "Float64", "n_risk": "Int32", "n_event": "Int32", "n_censor": "Int32",
              "surv": "Float64", "lower": "Float64", "upper": "Float64"},
    "forecast": {"time": "=time", **{name: "Float64" for name in (
        "observed", "fitted", "forecast", "lower_80", "upper_80", "lower", "upper")}, "is_forecast": "Boolean"},
    # the experiments group's: a row per variant and metric (the variant keeps the input column's own type), and the
    # planning grid; every column is always there, null where an analysis is off (docs/specs/ab-testing.md, «The output»)
    "variants": {"metric": "String", "role": "String", "variant": "=variant", "is_control": "Boolean", "units": "Int64",
                 **{name: "Float64" for name in (
                     "share", "expected_share", "mean", "std_error", "ci_low", "ci_high", "difference", "difference_ci_low",
                     "difference_ci_high", "lift", "lift_ci_low", "lift_ci_high", "p_value", "p_adjusted")},
                 "decision": "String",
                 **{name: "Float64" for name in (
                     "prob_beat_control", "prob_best", "expected_loss", "sequential_p_value", "sequential_p_adjusted",
                     "sequential_lift_low", "sequential_lift_high")}},
    "plan": {**{name: "Float64" for name in (
        "lift", "mde_absolute", "n_per_variant", "n_control", "n_total", "days", "weeks", "n_per_variant_cuped", "days_cuped")},
        "asked": "Boolean"},
    # the decomposition's: one row per instant of the series (docs/specs/ts-decomposition.md, «The output»). The time keeps the
    # input column's own type; `seasonal_*` is one column per seasonal period, `imf_*` one per IMF up to `max_imf`
    "components": {"time": "=time", **{name: "Float64" for name in (
        "observed", "transformed", "trend", "seasonal_*", "remainder", "deseasonalized", "imf_*", "residue")},
        "filled": "Boolean"},
    # the foundation forecast's: one row per instant and series (docs/specs/foundation-forecast.md, «The output»). The time and the
    # series keep the input columns' own types (`=time`, `=series`); `quantile_*` is one column per level asked, 0.5 left out
    "quantile_forecast": {"series": "=series", "time": "=time", **{name: "Float64" for name in (
        "observed", "forecast", "quantile_*", "baseline")}, "filled": "Boolean", "is_holdout": "Boolean", "is_forecast": "Boolean"},
    # the volatility node's: one row per instant of the series, then the forecast's (docs/specs/garch-risk.md, «The output»); the time
    # keeps the input column's own type
    "volatility": {"time": "=time", **{name: "Float64" for name in ("observed", "return", "mean", "sigma", "std_residual", "var")},
                   **{name: "Boolean" for name in ("violation", "is_holdout", "is_forecast")}},
    # the risk node's summary: one row per step of the risk variable's distribution; `quantile_*` one per level asked (the median
    # kept: it has no column of its own here), `price_quantile_*` the same on the price, `prob_*` with a threshold
    "risk_summary": {"step": "Int32", "time": "=time", **{name: "Float64" for name in (
        "mean", "sd", "quantile_*", "price_quantile_*", "price_mean", "var", "var_low", "var_high", "es", "es_low", "es_high",
        "prob_beyond", "prob_crossed", "band_error")}},
    # the simulated paths, rows sorted by path then step (docs/specs/garch-risk.md, «The Parquet file of the paths»)
    "paths": {"path": "Int32", "step": "Int32", "time": "=time", **{name: "Float64" for name in (
        "value", "cumulative", "price", "sigma")}},
}

# How a runner writes the input's own columns back in the rows output, by their kind, where the type
# changes: R reads a whole number as its integer, and writes it as Int32 (a value past 2^31 stays Int64).
ROWS_INPUT_TYPES: dict[str, dict[str, str]] = {"r": {"integer": "Int32"}, "python": {}}

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
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    try:  # JSON's integers have no bound: one of 400 digits is no float, and math.isfinite would raise
        return math.isfinite(v)
    except OverflowError:
        return False


def _whole(how: Mapping[str, Any], v: Any) -> bool:
    """A number of a `whole` kind is a whole one (a period, a window, a lag): any number otherwise."""
    return not how.get("whole") or float(v) == math.floor(float(v))


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
        # the count first: a list of a million numbers is refused before one of them is read
        if isinstance(v, list) and len(v) > how["max_count"]:
            raise refusal(model, f"'{key}' may hold at most {how['max_count']} numbers")
        if not isinstance(v, list) or not all(_is_number(x) and _within(how, float(x)) and _whole(how, x) for x in v):
            what = "whole numbers" if how.get("whole") else "numbers"
            raise refusal(model, f"'{key}' must be a list of {what}, each {_bounds_text(how)}")
        if len(set(float(x) for x in v)) != len(v):
            raise refusal(model, f"'{key}' holds a number twice")
        return sorted(float(x) for x in v)
    if control == "level_numbers":
        # the count first, then the keys' length: an object of a million keys, or one key of a million characters, is
        # refused before one value is read (and before a key is put in a sentence)
        if isinstance(v, dict) and len(v) > how["max_count"]:
            raise refusal(model, f"'{key}' may give a number to at most {how['max_count']} values")
        if not isinstance(v, dict) or not all(isinstance(c, str) and c for c in v):
            raise refusal(model, f"'{key}' must give a number to each value of the column, as {{value: number}}")
        if any(len(c) > how["max_chars"] for c in v):
            raise refusal(model, f"'{key}' names a value longer than {how['max_chars']} characters")
        return {c: _read_number(model, f"{key}.{c}", how, v[c]) for c in sorted(v)}
    if control == "column_choices":
        if isinstance(v, dict) and len(v) > how["max_count"]:
            raise refusal(model, f"'{key}' may name at most {how['max_count']} columns")
        if not isinstance(v, dict) or not all(isinstance(c, str) and c for c in v):
            raise refusal(model, f"'{key}' must give a column one of {', '.join(how['choices'])}, as {{column: choice}}")
        if any(len(c) > how["max_chars"] for c in v):
            raise refusal(model, f"'{key}' names a column longer than {how['max_chars']} characters")
        for c in sorted(v):
            if not isinstance(v[c], str) or v[c] not in how["choices"]:
                raise refusal(model, f"'{key}.{c}' must be one of {', '.join(how['choices'])}")
        return {c: v[c] for c in sorted(v)}
    if control == "number_or_choice":
        if isinstance(v, str) and v in how["choices"]:
            return v
        if _is_number(v) and _within(how, float(v)) and _whole(how, v):
            return float(v)
        bounds = _bounds_text(how)
        raise refusal(model, f"'{key}' must be one of {', '.join(how['choices'])}, or a "
                             + ("whole number" if how.get("whole") else "number") + (f" {bounds}" if bounds else ""))
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
    # what one key says of another: a reference level, a pair of predictors, a choice for some columns
    for key, kind in kinds.items():
        how = KINDS[kind]
        if how["control"] in ("reference", "pairs", "column_choices") and key in read:
            allowed = set(full.get(how["of"]) or [])
            named = [c for pair in read[key] for c in pair] if how["control"] == "pairs" else list(read[key])
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
    made = variant_spec(model, full)
    if output == "rows":
        clash = [c for c in _rows_columns(made["outputs"]["rows"], full, made.get("rows_when")) if c in columns]
        if clash:
            raise refusal(model, f"the input already has a column «{clash[0]}», which the model's rows add: rename it first")


# The names with a `*` that the node's own options fill, as the runner writes them: the key is the prefix, the value
# a function of the node's parameters that gives the labels (a name with a `*` the data fill, `pred_prob_*`, has none
# here: it stays as it is). `pred_quantile_`, `pred_surv_at_`: one per number of a list (`km_time_label`: a whole number
# without decimals, else its shortest form), increasing, each once; `seasonal_`: one per seasonal period; `imf_`: one
# per IMF the node extracts at most.
def _number_label(t: float) -> str:
    return str(int(t)) if float(t).is_integer() and abs(t) < 1e15 else repr(float(t))


def _labels_of(key: str):
    def labels(full: Mapping[str, Any]) -> list[str]:
        # a quantile of 0.5 is the median, `pred_median`: the runner writes no pred_quantile_0.5 (ps_quantiles)
        return [_number_label(v) for v in sorted({float(x) for x in full.get(key) or [] if _is_number(x)})
                if not (key == "quantiles" and v == 0.5)]

    return labels


def _risk_quantile_labels(full: Mapping[str, Any]) -> list[str]:
    """`quantile_<q>` of a risk summary: the levels of `quantiles`, increasing, each once, the 0.5 too (no other column says the median)."""
    return [_number_label(v) for v in sorted({float(x) for x in full.get("quantiles") or [] if _is_number(x)})]


def _seasonal_labels(full: Mapping[str, Any]) -> list[str]:
    """The seasonal periods of a decomposition, as the runner names its columns (`seasonal_7`): the periods of an MSTL, else
    the node's period, else the frequency's own; whole numbers, increasing, each once."""
    if full.get("method") == "mstl":
        given = full.get("periods") or []
    elif full.get("period") is not None:
        given = [full["period"]]
    else:
        given = [SEASON_OF_FREQUENCY.get(full.get("frequency"), 1)]
    return [str(int(v)) for v in sorted({float(x) for x in given if _is_number(x) and float(x) == int(x) and float(x) > 1})]


def _imf_labels(full: Mapping[str, Any]) -> list[str]:
    """`imf_1` … `imf_<max_imf>`: the names are fixed by the option, not by the data (docs/specs/ts-decomposition.md)."""
    n = full.get("max_imf")
    n = int(n) if _is_number(n) and float(n) == int(n) else MODELS["decomposition"]["defaults"]["max_imf"]
    return [str(i) for i in range(1, min(max(n, 0), LIMITS["max_imfs"]) + 1)]


_ROWS_WILDCARDS = {"pred_quantile_": _labels_of("quantiles"), "pred_surv_at_": _labels_of("times"),
                   "seasonal_": _seasonal_labels, "imf_": _imf_labels,
                   # the foundation forecast's quantile columns: the same function as `pred_quantile_`, 0.5 left out (it is `forecast`);
                   # the risk simulation's summary has no median column besides its quantiles, so it keeps the 0.5 (the node is the one
                   # with an `es_level`): the same prefix, the rule of the node whose columns they are (docs/specs/garch-risk.md, G0)
                   "quantile_": lambda full: _risk_quantile_labels(full) if "es_level" in full else _labels_of("quantiles")(full),
                   "price_quantile_": lambda full: _risk_quantile_labels(full)}


def _rows_columns(names: list[str], full: Mapping[str, Any], when: Mapping[str, Mapping[str, Any]] | None = None) -> list[str]:
    """The columns the rows output adds, the `*` of a quantile's or a time's name made one per number. `when`
    (a variant's `rows_when`): a column the runner adds only when its rule holds, as a variant's `when`
    (`pred_holdout_transactions` only with a holdout)."""
    out: list[str] = []
    for name in names:
        rule = (when or {}).get(name)
        if rule and not all(_holds(r, full.get(k)) for k, r in rule.items()):
            continue
        pre, star, post = name.partition("*")
        labels = _ROWS_WILDCARDS.get(pre)
        if not star or labels is None:
            out.append(name)
        else:
            out.extend(f"{pre}{label}{post}" for label in labels(full))
    return out


def variant_spec(model: str, checked: Mapping[str, Any]) -> dict[str, Any]:
    """The variant of `model` its parameters make: the first whose `when` holds."""
    for v in MODELS[model]["variants"]:
        if all(_holds(rule, checked.get(k, MODELS[model]["defaults"].get(k))) for k, rule in v["when"].items()):
            return v
    raise AssertionError(f"{model}: no variant for {dict(checked)}")


def variant(model: str, checked: Mapping[str, Any]) -> str:
    """The id of the variant (`unpenalized`, `penalized`, `manual`, `auto`, `default`)."""
    return variant_spec(model, checked)["id"]


def output_type(output: str, name: str) -> str | None:
    """The declared type of a column an output writes (`OUTPUT_TYPES`), by its name or by the name
    with a `*` that makes it; None when the output declares no such column."""
    types = OUTPUT_TYPES.get(output, {})
    if name in types:
        return types[name]
    for pattern, kind in types.items():
        pre, star, post = pattern.partition("*")
        if star and name.startswith(pre) and name.endswith(post) and len(name) > len(pre) + len(post):
            return kind
    return None


def output_columns(model: str, params: Mapping[str, Any] | None,
                   input_columns: list[tuple[str, str]] | None = None) -> tuple[list[tuple[str, str]], list[str]]:
    """What a model node hands to the next step before it has run, from its parameters alone:
    ([(name, type)], [the names with a `*` the data fill]). `input_columns` [(name, type)] are the
    input's: the rows output starts with them, as the runner writes them back (`ROWS_INPUT_TYPES`),
    and «@<key>» types are read from them. The names a
    quantile or a time makes are in the runner's order, each with its interval («pred_quantile_0.25»,
    «…_low», «…_high», then the next quantile); a name the data fill («pred_prob_*», one per class)
    is not a column yet, and is returned apart. The parameters need not be complete: what is not
    given takes its default."""
    spec = MODELS[model]
    given = {k: v for k, v in (params or {}).items() if v is not None}
    full = {k: given.get(k, spec["defaults"].get(k)) for k in {**spec["needs"], **spec["takes"]}}
    output = full.get("output") or "results"
    types = OUTPUT_TYPES[output]
    if output == "results":
        return list(types.items()), []
    inputs = dict(input_columns or [])
    try:
        made = variant_spec(model, full)
    except AssertionError:
        # the key that makes the variant is not chosen yet (a node just dropped): the output's columns are the same
        # in every variant that has the output (the experiments' metric type), and the first stands for them
        made = next(v for v in spec["variants"] if output in v["outputs"])
    names = [n for n in made["outputs"][output]
             if not (rule := (made.get("rows_when") or {}).get(n)) or all(_holds(r, full.get(k)) for k, r in rule.items())]
    cols: list[tuple[str, str]] = []
    unknown: list[str] = []
    i = 0
    while i < len(names):
        pre, star, _ = names[i].partition("*")
        if not star:
            cols.append((names[i], names[i]))
            i += 1
            continue
        # the names with the same start go together, one number at a time: as the runner writes them
        group = [names[i]]
        while i + len(group) < len(names) and names[i + len(group)].partition("*")[0] == pre \
                and names[i + len(group)].partition("*")[1]:
            group.append(names[i + len(group)])
        i += len(group)
        labels = _ROWS_WILDCARDS.get(pre)
        if labels is None:
            unknown.extend(group)
            continue
        # in increasing order, each once: as the check hands them to the runner
        for label in labels(full):
            cols.extend((pattern.replace("*", label, 1), pattern) for pattern in group)
    out: list[tuple[str, str]] = []
    for name, pattern in cols:
        kind = types[pattern]
        if kind.startswith("@"):
            source = inputs.get(full.get(kind[1:]) or "")
            # the input's column not known (not chosen yet, no input): neither is the type
            kind = "Unknown" if source is None else "Int32" if column_kind(source) == "integer" else str(source)
        elif kind.startswith("="):
            source = inputs.get(full.get(kind[1:]) or "")
            kind = "Unknown" if source is None else str(source)  # its own type, as the runner writes it
        out.append((name, kind))
    if output == "rows":
        kept = ROWS_INPUT_TYPES[GROUPS[spec["group"]]["language"]]
        out = [(n, kept.get(column_kind(t), str(t))) for n, t in (input_columns or [])] + out
    return out, unknown


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
        "output_types": OUTPUT_TYPES,
        "rows_input_types": ROWS_INPUT_TYPES,
        "section_shapes": SECTION_SHAPES,
        "verdicts": list(VERDICTS),
        "groups": [{"id": g, **spec} for g, spec in GROUPS.items()],
        "models": [
            {
                "id": m,
                "group": spec["group"],
                "family": spec["family"],
                "label": f"models.{m}.name",
                "card": spec.get("card", True),
                "max_rows": spec.get("max_rows"),
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
