"""What the two boosting models share (lot X1 of docs/specs/statistical-models.md, «Group `boosting`
— Python» and «The boosting group's lots», whose interface is fixed there).

    prepare(task) -> Prepared        the input read, the rows left out, the predictors' kinds and the
                                     categories' levels, the positional frame (v1, v2…), y, the
                                     weights, the validation part, the classes, the card's inputs
    a library wrapper -> Fitted      what one library gave (boosting_xgboost, boosting_catboost)
    finish(task, prepared, fitted)   every row of «The output» from those numbers, the rows output,
       -> {rows, output, card, model_file}   the card and the model file
    predict(task, card, raw_of)      Predict: the card's frame, the library's margins, the columns

Everything here is numpy and pandas: the metrics are written by hand (scikit-learn is a witness
outside the image, reference/make_witness.py), the diagnostics are rules of thumb with their
thresholds below, the points of the charts are numbers the report engine draws.

Randomness: one generator per run (tab_protocol.rng, the node's seed), consumed in a fixed order:
the split, then the SHAP sample, then the beeswarm's rows, then the capped points.
"""
from __future__ import annotations

import math
import time
import warnings
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import pandas as pd

from tab_card import card as base_card, model_file_entry, software
from tab_charts import cap, extreme_labels, hist, thin
from tab_columns import complete_rows, is_missing, kind, label, sort_labels
from tab_main import model_input
from tab_protocol import Refusal, limit, model_spec, rng as run_rng, threads as run_threads, variant_spec
from tab_results import Rows, add, note, number, points, rows_start, text

TASKS = ("regression", "binary", "multiclass")
EPS = float(np.finfo(np.float64).eps)  # the log-loss's clipping (scikit-learn's rule since 1.5)
MIN_TRAIN_ROWS = 20
MIN_VALIDATION_ROWS = 10
TIME_BUDGET_SHARE = 0.6
SHAP_TIME_SHARE = 0.85  # the SHAP values end by 85 % of the step's time: the charts and the files keep the rest
SHAP_PILOT_ROWS = 64    # the rows whose SHAP values are timed to size the sample on the time left
MODEL_SIZE_SHARE = 0.95  # the boosting stops before its model file passes 95 % of max_model_file_bytes
BEESWARM_BINS = 50
BEESWARM_HALF_WIDTH = 0.4
CALIBRATION_BINS = 10
CALIBRATION_MIN_CLASS = 10  # the calibration is not read with fewer validation rows of a class
Z95 = 1.6448536269514722    # the normal's 95 % quantile, one-sided
Z975 = 1.959963984540054    # two-sided 95 %
PROBABILITY_BINS = 20
ROC_POINTS = 1000
ROC_OVR_POINTS = 200
COLOUR_BINS = ("low", "mid_low", "mid", "mid_high", "high")

NOTES = {
    "validation_chose_trees": "The validation part also chose the number of trees (early stopping): its numbers are slightly "
                              "optimistic.",
    "time_split": "The validation part is the latest rows by the time column; rows of the same time are all on the same side.",
    "grouped_split": "The validation part holds whole values of the grouping column: the rows of one value are never on both "
                     "sides.",
    "time_budget": "The boosting stopped at 60 % of the step's time: the trees kept are the best so far; raise the step's "
                   "timeout, or lower max_trees.",
    "shap_sampled": "The SHAP values are computed on a sample of the rows, drawn with the node's seed.",
    "class_absent_validation": "A class has no row in the validation part: its numbers there are empty.",
    "zero_weights": "Rows whose weight is 0 are left out of the fit and of every number.",
    "model_size": "The boosting stopped before its model file would be larger than a model file can be (64 MiB): the trees kept "
                  "are the best so far; lower max_depth (each level can double a tree), or the classes.",
}


def half_up(x: float) -> int:
    """round(x) as the spec writes it: halves go up (Python's round goes to the even)."""
    return int(math.floor(x + 0.5))


# ── the node's parameters ───────────────────────────────────────────────────────
def params_of(task: dict[str, Any]) -> dict[str, Any]:
    """The node's parameters with the catalogue's defaults for the keys the task does not hold."""
    spec = model_spec(task.get("model") or "") or {"defaults": {}}
    p = {**spec["defaults"], **{k: v for k, v in (task.get("params") or {}).items() if v is not None}}
    if p.get("task") not in TASKS:
        raise Refusal("task", f"unknown task «{p.get('task') or ''}»: regression, binary or multiclass")
    return p


# ── the prepared rows ───────────────────────────────────────────────────────────
@dataclass
class Prepared:
    """What both libraries are handed, and what finish reads back. Indices are positions in the
    rows the model uses (0…n − 1); `rows` maps them to the input's row numbers (from 1)."""
    params: dict[str, Any]
    task: str                       # regression | binary | multiclass
    outcome: str
    predictors: list[str]           # the input's names
    names: list[str]                # v1, v2… in the same order
    types: list[str]                # "number" | "category" per predictor
    levels: list[list[str] | None]  # a category's levels in text order (the codes 0…), None for a number
    X: pd.DataFrame                 # the positional frame: float64, or a pandas category whose categories are the levels
    y: np.ndarray                   # float64 (regression) or int64 class index
    w: np.ndarray                   # the weights of every number (1 without `weights`)
    fit_w: np.ndarray               # the weights of the fit: w, times n / (k · n_c) with class_weight = balanced
    weighted: bool
    validation: np.ndarray          # bool, True in the validation part
    train_order: np.ndarray         # the training rows in the order the library sees them (time order for a time split)
    classes: list[str]              # classification: index → label (binary: [the other, event_value]); [] for regression
    event_value: str | None
    inputs: list[dict[str, Any]]    # the card's inputs, in the order of the positional names
    rows: np.ndarray                # the input's row numbers (from 1) of the rows used
    keep: np.ndarray                # bool over the input's rows
    df: pd.DataFrame                # the input
    n: int
    n_missing: int
    split: str                      # random | grouped | time
    missing_share: np.ndarray       # per predictor, the share of the rows used where it is missing
    gen: np.random.Generator        # the run's generator, after the split
    threads: int
    notes: list[str] = field(default_factory=list)
    has_time_column: bool = False

    @property
    def k(self) -> int:
        return len(self.classes) if self.task == "multiclass" else 1

    @property
    def train(self) -> np.ndarray:
        """The training rows, in increasing order (the order of Fitted.raw_train)."""
        return np.flatnonzero(~self.validation)

    @property
    def valid(self) -> np.ndarray:
        """The validation rows, in increasing order (the order of Fitted.raw_validation)."""
        return np.flatnonzero(self.validation)


@dataclass
class Fitted:
    """What a library wrapper returns. Margins are on the raw scale (the outcome's units, log-odds,
    or one column per class before the softmax), float64, n or n × k."""
    library: str                    # xgboost | catboost
    library_version: str
    packages: list[str]             # the distributions named in `software`: ["xgboost-cpu", "numpy", "pandas"]
    objective: str                  # the library's name of the loss: reg:squarederror, Logloss…
    loss_metric_name: str           # the curves' key of the loss: rmse, logloss, mlogloss, RMSE…
    stop_metric_name: str           # the curves' key of the metric early stopping watched
    importance_type: str            # total_gain, PredictionValuesChange
    raw_train: np.ndarray           # margins of Prepared.train (increasing order)
    raw_validation: np.ndarray      # margins of Prepared.valid
    curves: dict[str, dict[str, list[float]]]  # {metric: {"train": [...], "validation": [...]}}, one value per round
    best_iteration: int             # from 0
    trees: int                      # the rounds kept (best_iteration + 1 with early stopping)
    stopped_early: bool
    stopped_by_time: bool
    gain: dict[int, float]          # predictor position (0…p − 1) → the native importance; a position left out is 0
    shap: Callable[[np.ndarray], tuple[np.ndarray, Any]]  # rows (positions) → (phi n × p or n × k × p, base)
    predict_raw: Callable[[pd.DataFrame], np.ndarray]     # a positional frame → margins
    model_bytes: bytes
    model_name: str                 # xgboost.json | catboost.cbm
    model_format: str               # xgboost-json | catboost-cbm
    missing_level: str | None = None
    lower_is_better: dict[str, bool] = field(default_factory=dict)  # per curve metric; a metric left out: lower is better
    stopped_by_size: bool = False   # stopped before the model file would pass MODEL_SIZE_SHARE of its limit


def _refuse_missing_columns(df: pd.DataFrame, columns: list[str]) -> None:
    for c in columns:
        if c not in df.columns:
            raise Refusal("missing_column", f"the rows reaching the node have no column «{c}»", {"column": c})


def _sortable(df: pd.DataFrame, name: str) -> np.ndarray:
    """A time column (number, integer, date, datetime) as float64 numbers in its order."""
    k = kind(df, name)
    x = df[name]
    if k in ("number", "integer"):
        return x.to_numpy(dtype=float, na_value=np.nan)
    if k in ("date", "datetime"):
        t = pd.to_datetime(pd.Series([None if is_missing(v) else v for v in x], dtype=object), utc=True)
        out = t.to_numpy(dtype="datetime64[ns]").astype("int64").astype(float)
        out[t.isna().to_numpy()] = np.nan
        return out
    raise Refusal("order_by_type", f"the column «{name}» cannot order the rows in time: a number, a date or a date and "
                                   "time can", {"column": name})


def _distinct(values: Any) -> tuple[np.ndarray, list[str | None]]:
    """The values as codes of their distinct values (pandas' factorize: −1 for a missing one) and
    the label of each distinct value, so that label() runs once per distinct value, not per row."""
    arr = values.to_numpy(dtype=object) if hasattr(values, "to_numpy") else np.asarray(list(values), dtype=object)
    codes, uniques = pd.factorize(arr, use_na_sentinel=True)
    return np.asarray(codes, dtype=np.int64), [label(u) for u in uniques]


def factor_labels(values: Any) -> tuple[np.ndarray, list[str]]:
    """A column's labels in text order (sort_labels) and each value's code among them, −1 for a
    missing value: what `[label(v) for v in values]` then sort_labels give, without a loop per row."""
    codes, labs = _distinct(values)
    levels = sort_labels(labs)
    at = {lv: i for i, lv in enumerate(levels)}
    to = np.array([at[lv] if lv is not None else -1 for lv in labs] + [-1], dtype=np.int64)
    return to[codes], levels


def category_codes(values: Any, levels: list[str]) -> tuple[np.ndarray, int]:
    """The codes of a column's values among `levels` (labels, catalog.label_of), −1 for a missing
    value or a level not among them; and how many values were unseen (present, not a level).
    label() runs once per distinct value (factorize), not once per row."""
    codes, labs = _distinct(values)
    at = {lv: i for i, lv in enumerate(levels)}
    to = np.array([at.get(lv, -1) if lv is not None else -1 for lv in labs] + [-1], dtype=np.int64)
    new = np.array([lv is not None and lv not in at for lv in labs] + [False], dtype=bool)
    return to[codes], int(new[codes].sum())


def _refuse_infinite(x: np.ndarray, column: str) -> None:
    """XGBoost stops on an infinite value, CatBoost takes it silently: both refuse it, the same way
    in the fit, the rows output and Predict."""
    if np.isinf(x).any():
        raise Refusal("infinite", f"the column «{column}» holds an infinite value: filter it out or replace it before the node",
                      {"column": column})


def positional_frame(df: pd.DataFrame, columns: list[str], types: list[str], levels: list[list[str] | None],
                     names: list[str] | None = None) -> tuple[pd.DataFrame, dict[str, int]]:
    """The frame a library reads: positional names, numbers as float64 (NaN missing; an infinite
    value is refused), categories as a pandas category whose categories are the card's levels in
    their order (a missing or unseen value is missing). The unseen values are counted per input
    column."""
    names = names or [f"v{i}" for i in range(1, len(columns) + 1)]
    out: dict[str, Any] = {}
    unseen: dict[str, int] = {}
    for col, name, t, lv in zip(columns, names, types, levels):
        if t == "number":
            out[name] = pd.Series(df[col]).to_numpy(dtype=float, na_value=np.nan)
            _refuse_infinite(out[name], col)
        else:
            codes, u = category_codes(df[col], lv or [])
            out[name] = pd.Categorical.from_codes(codes, categories=list(lv or []))
            if u:
                unseen[col] = u
    return pd.DataFrame(out, index=pd.RangeIndex(len(df))), unseen


def _check_predictor(df: pd.DataFrame, p: str, max_levels: int) -> tuple[str, list[str] | None]:
    k = kind(df, p)
    if k in ("date", "datetime"):
        raise Refusal("date_predictor", f"the column «{p}» is a {k} and cannot be a predictor: compute a number from the date "
                                        "first: the month, the days since a start", {"column": p})
    if k in ("number", "integer"):
        x = df[p].to_numpy(dtype=float, na_value=np.nan)
        _refuse_infinite(x, p)
        if np.isnan(x).all():
            raise Refusal("empty_predictor", f"the column «{p}» is empty in the rows the model uses: leave it out",
                          {"column": p})
        return "number", None
    if k not in ("text", "boolean"):
        raise Refusal("predictor_type", f"the column «{p}» cannot be a predictor: numbers, texts and true or false can",
                      {"column": p})
    _, levels = factor_labels(df[p])
    if not levels:
        raise Refusal("empty_predictor", f"the column «{p}» is empty in the rows the model uses: leave it out", {"column": p})
    if len(levels) > max_levels:
        raise Refusal("too_many_levels", f"the column «{p}» has {len(levels):,} values: is it an identifier?",
                      {"column": p, "levels": len(levels)})
    return "category", levels


def prepare(task: dict[str, Any]) -> Prepared:
    """The rows of a fit, from the task (docs: «Rows and types», «The validation part»)."""
    p = params_of(task)
    what = p["task"]
    outcome = p.get("outcome")
    predictors = list(p.get("predictors") or [])
    if not outcome or not predictors:
        raise Refusal("no_columns", "the node needs an outcome and at least one predictor")
    weights = p.get("weights")
    split = "time" if p.get("validation") == "time" else "random"
    order_by = p.get("order_by") if split == "time" else None
    split_by = p.get("split_by") if split == "random" else None
    if split == "time" and not order_by:
        raise Refusal("time_needs_order", "a validation part in time needs the column of the time ('order_by')")
    if split_by:
        split = "grouped"
    if len(predictors) > int(limit(task, "max_predictors")):
        raise Refusal("too_many_predictors", f"the node has {len(predictors)} predictors (at most "
                                             f"{int(limit(task, 'max_predictors'))}): choose fewer",
                      {"predictors": len(predictors)})
    df = model_input(task)
    needed = list(dict.fromkeys([outcome, *predictors, *(c for c in (weights, order_by, split_by) if c)]))
    _refuse_missing_columns(df, needed)
    has_time = any(kind(df, c) in ("date", "datetime") for c in df.columns)

    # the outcome's kind
    ok = kind(df, outcome)
    if what == "regression" and ok not in ("number", "integer"):
        raise Refusal("outcome_not_number", f"the outcome «{outcome}» is not a number: a regression predicts a number; "
                                            "choose task = binary or multiclass for classes", {"column": outcome})
    if what != "regression" and ok not in ("text", "boolean", "integer", "number"):
        raise Refusal("outcome_type", f"the outcome «{outcome}» cannot be a class: texts, true or false and whole numbers can",
                      {"column": outcome})
    if weights and kind(df, weights) not in ("number", "integer"):
        raise Refusal("weights_not_number", f"the weights «{weights}» are not numbers", {"column": weights})

    # rows left out: the outcome, the weight, the time or the group missing (predictors may be)
    kept = complete_rows(df, [c for c in (outcome, weights, order_by, split_by) if c])
    keep = kept["keep"]
    n_missing = kept["n_missing"]
    notes: list[str] = []
    w_all = df[weights].to_numpy(dtype=float, na_value=np.nan) if weights else np.ones(len(df))
    if weights:
        if (w_all[keep] < 0).any():
            raise Refusal("weights_negative", f"the weights «{weights}» hold a negative value: a weight is 0 or more",
                          {"column": weights})
        zero = keep & (w_all == 0)
        if zero.any():
            keep = keep & ~zero
            notes.append("zero_weights")
    used = df.loc[keep].reset_index(drop=True)
    used.attrs = dict(df.attrs)
    rows = np.flatnonzero(keep) + 1
    n = len(used)
    w = w_all[keep].astype(float)

    # the predictors: numbers, or categories with their levels in text order
    max_levels = int(limit(task, "max_levels_trees"))
    types: list[str] = []
    levels: list[list[str] | None] = []
    for c in predictors:
        t, lv = _check_predictor(used, c, max_levels)
        types.append(t)
        levels.append(lv)
        if t == "number" and (task.get("output") or p.get("output")) == "rows":
            # the rows output predicts every input row, those the fit leaves out too: refused before the fit
            _refuse_infinite(df[c].to_numpy(dtype=float, na_value=np.nan), c)
    names = [f"v{i}" for i in range(1, len(predictors) + 1)]
    X, _ = positional_frame(used, predictors, types, levels, names)
    missing_share = np.array([float(X[nm].isna().mean()) if n else math.nan for nm in names])
    inputs = [{"name": c, "type": "number"} if t == "number" else {"name": c, "type": "category", "levels": list(lv or [])}
              for c, t, lv in zip(predictors, types, levels)]

    # the outcome's values
    classes: list[str] = []
    event_value = None
    if what == "regression":
        y = used[outcome].to_numpy(dtype=float)
    else:
        codes, found = factor_labels(used[outcome])
        if len(found) < 2:
            raise Refusal("one_outcome_value", f"the outcome «{outcome}» holds one value only in the rows the model uses: there "
                                               "is nothing to predict", {"column": outcome})
        if what == "binary":
            if len(found) > 2:
                raise Refusal("outcome_not_binary", f"the outcome «{outcome}» has {len(found)} values: a binary task needs "
                                                    "exactly two; choose task = multiclass", {"column": outcome})
            ev = p.get("event_value")
            if ev is None:
                raise Refusal("binary_needs_event", "a binary task needs the outcome's value whose probability is predicted "
                                                    "('event_value')")
            event_value = ev if isinstance(ev, str) else label(ev)
            if event_value not in found:
                raise Refusal("event_value", f"the outcome «{outcome}» has no value «{event_value}» to be the event: its "
                                             f"values are «{found[0]}» and «{found[1]}»",
                              {"column": outcome, "value": event_value})
            classes = [found[0] if found[1] == event_value else found[1], event_value]
        else:
            most = int(limit(task, "max_classes"))
            if len(found) < 3:
                raise Refusal("multiclass_two", f"the outcome «{outcome}» has 2 values: a multiclass task needs 3 to {most}; "
                                                "choose task = binary", {"column": outcome})
            if len(found) > most:
                raise Refusal("too_many_classes", f"the outcome «{outcome}» has {len(found)} values (at most {most} classes): "
                                                  "is it the right column?", {"column": outcome, "classes": len(found)})
            classes = found
        at = {c: i for i, c in enumerate(classes)}
        y = np.array([at[v] for v in found], dtype=np.int64)[codes]  # the outcome is never missing here

    gen = run_rng(task)
    share = float(p.get("validation_share", 0.2))
    if split == "time":
        validation, train_order = split_time(_sortable(used, order_by), share)
        notes.append("time_split")
    elif split == "grouped":
        g_codes, g_levels = factor_labels(used[split_by])
        validation = split_grouped(np.asarray(g_levels, dtype=object)[g_codes].tolist(), share, gen)
        train_order = np.flatnonzero(~validation)
        notes.append("grouped_split")
    else:
        strata = y if (what != "regression" and bool(p.get("stratify", True))) else None
        validation = split_random(n, share, gen, strata, len(classes))
        train_order = np.flatnonzero(~validation)

    check_parts(what, outcome, y, validation, classes, notes)
    refuse_constant(X, validation)

    fit_w = w.copy()
    if what != "regression" and p.get("class_weight") == "balanced":
        fit_w = w * balanced_factors(y, w, ~validation, len(classes))[y]

    return Prepared(params=p, task=what, outcome=outcome, predictors=predictors, names=names, types=types, levels=levels,
                    X=X, y=y, w=w, fit_w=fit_w, weighted=bool(weights), validation=validation, train_order=train_order,
                    classes=classes, event_value=event_value, inputs=inputs, rows=rows, keep=keep, df=df, n=n,
                    n_missing=n_missing, split=split, missing_share=missing_share, gen=gen, threads=run_threads(task),
                    notes=notes, has_time_column=has_time)


# ── the validation part ─────────────────────────────────────────────────────────
def split_random(n: int, share: float, gen: np.random.Generator, strata: np.ndarray | None = None, k: int = 0) -> np.ndarray:
    """Random, with the generator. Stratified (strata = the class index): per class in label order
    its rows shuffled, round(share × n_c) to validation, at least one when the class has two rows
    or more and never all of them; otherwise the first round(share × n) of the shuffled rows."""
    validation = np.zeros(n, dtype=bool)
    if strata is None:
        order = gen.permutation(n)
        validation[order[:half_up(share * n)]] = True
        return validation
    for c in range(k):
        idx = np.flatnonzero(strata == c)
        if not len(idx):
            continue
        order = gen.permutation(idx)
        m = half_up(share * len(idx))
        if len(idx) >= 2:
            m = min(max(m, 1), len(idx) - 1)
        else:
            m = 0
        validation[order[:m]] = True
    return validation


def split_grouped(groups: list[str | None], share: float, gen: np.random.Generator) -> np.ndarray:
    """By groups: the values (labels in text order) shuffled; whole values go to validation, in
    that order, until it holds at least share × n rows."""
    n = len(groups)
    values = sort_labels(groups)
    at = {v: i for i, v in enumerate(values)}
    g = np.array([at[v] for v in groups], dtype=np.int64)
    counts = np.bincount(g, minlength=len(values))
    validation = np.zeros(n, dtype=bool)
    total = 0
    for j in gen.permutation(len(values)):
        if total >= share * n:
            break
        validation[g == j] = True
        total += int(counts[j])
    return validation


def split_time(t: np.ndarray, share: float) -> tuple[np.ndarray, np.ndarray]:
    """By time: rows sorted by time (stable: ties keep the input's order); the last round(share × n)
    are validation, extended back to every row of the same time as the first of them. Returns the
    validation mask and the training rows in time order."""
    n = len(t)
    order = np.argsort(t, kind="stable")
    m = max(1, half_up(share * n))
    first = t[order[n - m]] if m <= n else -math.inf
    validation = t >= first
    train_order = order[~validation[order]]
    return validation, train_order


def check_parts(what: str, outcome: str, y: np.ndarray, validation: np.ndarray, classes: list[str], notes: list[str]) -> None:
    """The refusals of «The validation part», 4."""
    n_train, n_valid = int((~validation).sum()), int(validation.sum())
    if n_train < MIN_TRAIN_ROWS or n_valid < MIN_VALIDATION_ROWS:
        raise Refusal("too_few_rows", f"the training part has {n_train} rows and the validation part {n_valid}: they need at "
                                      f"least {MIN_TRAIN_ROWS} and {MIN_VALIDATION_ROWS}; add rows, or raise validation_share "
                                      "if the validation part is the small one",
                      {"train": n_train, "validation": n_valid})
    if what == "regression":
        return
    k = len(classes)
    in_train = np.bincount(y[~validation], minlength=k)
    in_valid = np.bincount(y[validation], minlength=k)
    for c in range(k):
        if in_train[c] == 0 or (what == "binary" and in_valid[c] == 0):
            part = "training" if in_train[c] == 0 else "validation"
            raise Refusal("class_absent", f"the {part} part has no row of the class «{classes[c]}» of «{outcome}»: raise "
                                          "validation_share, or check the outcome",
                          {"class": classes[c], "part": part})
    if (in_valid == 0).any():
        notes.append("class_absent_validation")


def refuse_constant(X: pd.DataFrame, validation: np.ndarray) -> None:
    """Refused when no predictor varies in the training part (a missing value counts as a value of
    its own, as both libraries split on it): CatBoost stops on it, XGBoost would grow trees
    without a split. Both refuse it with one sentence."""
    tr = ~validation
    for name in X.columns:
        x = X[name]
        values = x.cat.codes.to_numpy()[tr] if isinstance(x.dtype, pd.CategoricalDtype) else x.to_numpy(dtype=float)[tr]
        present = values[values >= 0] if isinstance(x.dtype, pd.CategoricalDtype) else values[~np.isnan(values)]
        if len(np.unique(present)) + int(len(present) < len(values)) > 1:
            return
    raise Refusal("constant_predictors", "no predictor varies in the training part: every one holds a single value there, so the "
                                         "trees have nothing to split on; choose predictors that vary")


def balanced_factors(y: np.ndarray, w: np.ndarray, train: np.ndarray, k: int) -> np.ndarray:
    """class_weight = balanced: n / (k · n_c) per class, weighted counts of the training part."""
    total = float(w[train].sum())
    per = np.array([float(w[train & (y == c)].sum()) for c in range(k)])
    with np.errstate(divide="ignore"):
        return np.where(per > 0, total / (k * per), 0.0)


def deadline(task: dict[str, Any], share: float = TIME_BUDGET_SHARE) -> float | None:
    """When `share` of the step's time is spent (time.monotonic()), counted from the runner's
    start (tab_main writes it in the task as `_started`: the input read and the rows prepared
    are part of the step's time), from now when the task does not say it; None without a time
    budget. The boosting stops at 60 %; the SHAP sample is sized to end by SHAP_TIME_SHARE."""
    s = (task.get("limits") or {}).get("time_budget_seconds")
    if not s:
        return None
    started = task.get("_started")
    return (time.monotonic() if started is None else float(started)) + share * float(s)


# ── the metrics, by hand ────────────────────────────────────────────────────────
def wmean(x: np.ndarray, w: np.ndarray) -> float:
    sw = float(np.sum(w))
    return float(np.sum(w * x) / sw) if sw > 0 else math.nan


def rmse(y: np.ndarray, pred: np.ndarray, w: np.ndarray) -> float:
    return math.sqrt(wmean((y - pred) ** 2, w))


def mae(y: np.ndarray, pred: np.ndarray, w: np.ndarray) -> float:
    return wmean(np.abs(y - pred), w)


def r_squared(y: np.ndarray, pred: np.ndarray, w: np.ndarray) -> float:
    ss = float(np.sum(w * (y - wmean(y, w)) ** 2))
    return 1.0 - float(np.sum(w * (y - pred) ** 2)) / ss if ss > 0 else math.nan


def logloss(y: np.ndarray, p: np.ndarray, w: np.ndarray) -> float:
    q = np.clip(p, EPS, 1 - EPS)
    return wmean(-(y * np.log(q) + (1 - y) * np.log(1 - q)), w)


def mlogloss(y: np.ndarray, P: np.ndarray, w: np.ndarray) -> float:
    q = np.clip(P[np.arange(len(y)), y], EPS, 1 - EPS)
    return wmean(-np.log(q), w)


def brier(y: np.ndarray, p: np.ndarray, w: np.ndarray) -> float:
    return wmean((p - y) ** 2, w)


def auc(score: np.ndarray, y: np.ndarray, w: np.ndarray) -> float:
    """Σ_pos Σ_neg w_i w_j ([s_i > s_j] + ½ [s_i = s_j]) / (W_pos W_neg), in one sort: per distinct
    score, its positive weight times (the negative weight below it + half its own)."""
    score = np.asarray(score, dtype=float)
    pos = (y == 1)
    wp, wn = float(w[pos].sum()), float(w[~pos].sum())
    if wp <= 0 or wn <= 0:
        return math.nan
    order = np.argsort(score, kind="stable")
    s = score[order]
    p_w = np.where(pos[order], w[order], 0.0)
    n_w = np.where(pos[order], 0.0, w[order])
    starts = np.flatnonzero(np.r_[True, s[1:] != s[:-1]])
    P = np.add.reduceat(p_w, starts)
    N = np.add.reduceat(n_w, starts)
    below = np.cumsum(N) - N
    return float(np.sum(P * (below + N / 2)) / (wp * wn))


def roc(score: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The ROC's points (false-positive rate, true-positive rate) at every distinct threshold, from
    (0, 0) to (1, 1), in one sort; a row of weight 0 makes no threshold."""
    on = np.asarray(w) > 0
    score, y, w = np.asarray(score, dtype=float)[on], np.asarray(y)[on], np.asarray(w, dtype=float)[on]
    pos = (y == 1)
    wp, wn = float(w[pos].sum()), float(w[~pos].sum())
    order = np.argsort(-score, kind="stable")
    s = score[order]
    ends = np.flatnonzero(np.r_[s[1:] != s[:-1], True])
    tp = np.cumsum(np.where(pos[order], w[order], 0.0))[ends]
    fp = np.cumsum(np.where(pos[order], 0.0, w[order]))[ends]
    tpr = np.r_[0.0, tp / wp] if wp > 0 else np.r_[0.0, np.full(len(ends), math.nan)]
    fpr = np.r_[0.0, fp / wn] if wn > 0 else np.r_[0.0, np.full(len(ends), math.nan)]
    return fpr, tpr


def confusion(y: np.ndarray, pred: np.ndarray, w: np.ndarray, k: int) -> np.ndarray:
    """C[observed, predicted], weighted."""
    C = np.zeros((k, k))
    np.add.at(C, (y, pred), w)
    return C


def binary_at(C: np.ndarray) -> dict[str, float]:
    """Accuracy, sensitivity, specificity, precision and F1 from a 2 × 2 weighted matrix (1 = event)."""
    tn, fp, fn, tp = C[0, 0], C[0, 1], C[1, 0], C[1, 1]

    def ratio(a: float, b: float) -> float:
        return float(a / b) if b > 0 else math.nan
    return {"accuracy": ratio(tp + tn, C.sum()), "sensitivity": ratio(tp, tp + fn), "specificity": ratio(tn, tn + fp),
            "precision": ratio(tp, tp + fp), "f1": ratio(2 * tp, 2 * tp + fp + fn)}


def per_class(C: np.ndarray) -> dict[str, np.ndarray]:
    """Precision, recall and F1 per class from a weighted matrix (NULL where undefined)."""
    tp = np.diag(C)
    rows, cols = C.sum(axis=1), C.sum(axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        precision = np.where(cols > 0, tp / np.where(cols > 0, cols, 1), np.nan)
        recall = np.where(rows > 0, tp / np.where(rows > 0, rows, 1), np.nan)
        f1 = np.where(rows + cols > 0, 2 * tp / np.where(rows + cols > 0, rows + cols, 1), np.nan)
    return {"precision": precision, "recall": recall, "f1": f1, "support": rows}


def sigmoid(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=float)
    return np.where(m >= 0, 1 / (1 + np.exp(-np.abs(m))), np.exp(-np.abs(m)) / (1 + np.exp(-np.abs(m))))


def softmax(M: np.ndarray) -> np.ndarray:
    M = np.asarray(M, dtype=float)
    e = np.exp(M - M.max(axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


def _calibration_groups(p: np.ndarray, w: np.ndarray, bins: int = CALIBRATION_BINS) -> list[np.ndarray]:
    """The rows of each bin of equal weight of the predicted probability (rows sorted by p, stable;
    a row's bin is ⌊bins × (the weight before it) / W⌋), the bins with weight only."""
    order = np.argsort(p, kind="stable")
    ws = w[order]
    W = float(ws.sum())
    before = np.cumsum(ws) - ws
    b = np.minimum(bins - 1, np.floor(bins * before / W).astype(np.int64))
    groups = [order[b == j] for j in range(bins)]
    return [i for i in groups if len(i) and float(w[i].sum()) > 0]


def calibration_bins(p: np.ndarray, y: np.ndarray, w: np.ndarray, bins: int = CALIBRATION_BINS) -> dict[str, np.ndarray]:
    """Bins of equal weight of the predicted probability (_calibration_groups). Per bin with
    weight: the mean p, the observed rate (both weighted), the weight and the rows."""
    out = {"p": [], "y": [], "weight": [], "rows": []}
    for i in _calibration_groups(p, w, bins):
        wj = float(w[i].sum())
        out["p"].append(float(np.sum(w[i] * p[i]) / wj))
        out["y"].append(float(np.sum(w[i] * y[i]) / wj))
        out["weight"].append(wj)
        out["rows"].append(float(len(i)))
    return {k: np.asarray(v, dtype=float) for k, v in out.items()}


def ece(p: np.ndarray, y: np.ndarray, w: np.ndarray) -> float:
    """The expected calibration error: Σ_b (W_b / W) |ȳ_b − p̄_b| over 10 bins of equal weight."""
    b = calibration_bins(p, y, w)
    return float(np.sum(b["weight"] * np.abs(b["y"] - b["p"])) / np.sum(b["weight"]))


def ece_noise(p: np.ndarray, w: np.ndarray) -> float:
    """The expected calibration error that perfectly calibrated probabilities p reach 95 % of the
    time with these rows and weights, by a formula: in a bin the observed rate minus the mean p is
    about normal, of variance σ_b² = Σ w_i² p_i (1 − p_i) / W_b²; the ECE is then a sum of
    half-normals Σ ω_b |N(0, σ_b²)|, ω_b = W_b / W, of mean Σ ω_b σ_b √(2/π) and variance
    Σ ω_b² σ_b² (1 − 2/π), taken as normal: the mean + 1.645 standard deviations. With 80
    validation rows it is about 0.16, with 400 about 0.07, with 4,000 about 0.02."""
    W = float(w.sum())
    mean = var = 0.0
    for i in _calibration_groups(p, w):
        wb = float(w[i].sum())
        sd = math.sqrt(float(np.sum(w[i] ** 2 * p[i] * (1 - p[i])))) / wb
        omega = wb / W
        mean += omega * sd * math.sqrt(2 / math.pi)
        var += (omega * sd) ** 2 * (1 - 2 / math.pi)
    return mean + Z95 * math.sqrt(var)


def calibration_slope(p: np.ndarray, y: np.ndarray, w: np.ndarray) -> tuple[float, float]:
    """The slope of a weighted logistic regression of y on logit(p), with an intercept, by Newton's
    method, and its standard error (the inverse of the information at the optimum, the weights
    scaled to sum to the rows: they say how much a row counts, not how many rows it stands for);
    (NaN, NaN) when it does not converge (a separation)."""
    q = np.clip(p, EPS, 1 - EPS)
    x = np.log(q / (1 - q))
    w = w * (len(w) / float(w.sum()))
    A = np.column_stack([np.ones_like(x), x])
    beta = np.array([0.0, 1.0])
    for _ in range(100):
        mu = sigmoid(A @ beta)
        g = A.T @ (w * (y - mu))
        H = (A * (w * mu * (1 - mu))[:, None]).T @ A
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            return math.nan, math.nan
        beta = beta + step
        if not np.all(np.isfinite(beta)) or abs(beta[1]) > 1e6:
            return math.nan, math.nan
        if np.max(np.abs(step)) < 1e-12:
            mu = sigmoid(A @ beta)
            H = (A * (w * mu * (1 - mu))[:, None]).T @ A
            try:
                var = float(np.linalg.inv(H)[1, 1])
            except np.linalg.LinAlgError:
                return math.nan, math.nan
            return float(beta[1]), math.sqrt(var) if var > 0 else math.nan
    return math.nan, math.nan


def spearman(a: np.ndarray, b: np.ndarray) -> float:
    """Spearman's ρ: Pearson's correlation of the average ranks."""
    ra = pd.Series(a).rank(method="average").to_numpy()
    rb = pd.Series(b).rank(method="average").to_numpy()
    ra, rb = ra - ra.mean(), rb - rb.mean()
    d = math.sqrt(float(np.sum(ra * ra) * np.sum(rb * rb)))
    return float(np.sum(ra * rb) / d) if d > 0 else math.nan


# ── the diagnostics: rules of thumb, not tests ──────────────────────────────────
# Each advice has an id; the runner writes it next to the sentence (the row `advice_id`), and the
# report says the advice of that id in its own words (report_texts/boosting.json): the sentence
# may change, the id does not.
ADVICE = {
    "validation_size": "the validation numbers rest on few rows: raise validation_share, or add rows",
    "beats_baseline": "the model predicts the validation part no better than the average (the class shares): check the "
                      "predictors and the outcome",
    "beats_baseline_constant": "the baseline's loss is 0: the outcome does not vary in the validation part",
    "beats_baseline_balanced": "with class_weight = balanced both losses weigh the classes as the fit does (each class the same): "
                               "the model separates the classes no better than one probability for every row; check the "
                               "predictors and the outcome",
    "overfitting": "the validation loss of the tree kept is worse than at the best tree before it: the trees after it learn "
                   "the training rows, not the pattern; turn early stopping on with stop_metric = loss, or lower max_trees, "
                   "max_depth or learning_rate",
    "overfitting_no_gain": "the validation loss never goes below the baseline's, so there is no gain to give back",
    "trees_more": "the validation loss was still falling at the last tree: raise max_trees or learning_rate",
    "trees_few": "stopped after very few trees: lower learning_rate",
    "trees_no_stop": "the validation loss at the last tree is more than 1 % worse than at the best: turn early stopping on",
    "trees_size": "the boosting stopped so that the model file stays within 64 MiB: lower max_depth (each level can double a "
                  "tree), or raise learning_rate so that fewer trees are needed",
    "class_imbalance": "read the AUC and the log-loss, not the accuracy; choose a threshold; class_weight = balanced trades "
                       "calibrated probabilities for balance",
    "calibration": "the predicted probabilities do not match the observed rates: do not read them as probabilities, or "
                   "recalibrate",
    "calibration_balanced": "expected: balanced weights move the probabilities; turn it off if they are used as probabilities",
    "calibration_few_rows": f"the validation part has fewer than {CALIBRATION_MIN_CLASS} rows of a class: the calibration "
                            "cannot be read; raise validation_share, or add rows",
    "calibration_slope": "below 1: the probabilities are too extreme (overfitting); above 1: too timid",
    "slope_separated": "the logistic regression on logit(p) does not converge: the validation part is separated",
    "importance_stability": "the model leans on different predictors in the two parts: the ranking is not reliable; more "
                            "rows, or fewer correlated predictors",
    "importance_few_predictors": "fewer than 3 predictors: there is no ranking to compare",
    "importance_few_rows": "fewer than 50 validation rows in the SHAP sample: add rows, or raise validation_share",
    "importance_flat": "the importance does not vary between the predictors",
    "zero_importance": "the model never uses it: drop it, or check that it varies",
    "leakage": "one predictor explains nearly everything, or the fit is nearly perfect: check that no predictor is computed "
               "from the outcome or recorded after it (an id, a status, a later date)",
    "leakage_time": "one predictor explains nearly everything, or the fit is nearly perfect: check that no predictor is "
                    "computed from the outcome or recorded after it (an id, a status, a later date); try validation = time",
}

H0 = {
    "validation_size_regression": "the rows of the validation part",
    "validation_size": "the smallest number of rows of a class in the validation part",
    "beats_baseline": "1 − validation loss / the baseline's",
    "beats_baseline_balanced": "1 − validation loss / the baseline's, both weighted as the fit (class_weight = balanced: each "
                               "class the same)",
    "overfitting": "the share of the validation gain over the baseline given back between the best tree and the tree kept",
    "trees_limit": "whether early stopping stopped, and the trees kept",
    "trees_limit_no_stop": "how much worse the validation loss is at the last tree than at the best",
    "class_imbalance": "the largest class share over the smallest",
    "calibration": "the expected calibration error, against the one well calibrated probabilities reach with these rows",
    "calibration_slope": "the slope of a logistic regression on logit(p), read with its 95 % interval",
    "importance_stability": "Spearman's ρ of the importance in the two parts",
    "zero_importance": "the predictors no split uses",
    "leakage": "the first predictor's share of |SHAP| (with one predictor: the validation fit), and how nearly perfect the "
               "validation fit is",
}

# the thresholds of the diagnostics («Open questions for the owner», 20), tuned on clean and on
# spoiled tables (docs/specs/statistical-models.md, «The diagnostics»)
OVERFITTING_OK, OVERFITTING_FAILED = 0.05, 0.25
CALIBRATION_OK, CALIBRATION_FAILED = 0.05, 0.10
SLOPE_OK, SLOPE_DOUBTFUL = (0.8, 1.25), (0.5, 2.0)
LEAKAGE_SHARE, LEAKAGE_PERFECT = 0.8, 0.98
LEAKAGE_SOME_SHARE, LEAKAGE_HIGH = 0.6, 0.95
TREES_FEW_RATE = 0.05  # the default learning rate: «few trees» is read above it


def _check(check: str, verdict: str, value: float = math.nan, threshold: float = math.nan, advice: str | None = None,
           level_2: str | None = None, h0: str | None = None) -> dict[str, Any]:
    """A diagnostic's row; `advice` is the id of its advice (ADVICE), said only when it is not ok."""
    said = None if verdict == "ok" or advice is None else advice
    return {"check": check, "level_2": level_2, "h0": h0 or H0[check], "verdict": verdict, "value": value,
            "threshold": threshold, "advice_id": said, "advice": ADVICE[said] if said else None}


def check_validation_size(what: str, n_valid: int, smallest_class: int | None) -> dict[str, Any]:
    if what == "regression":
        v, ok, doubtful = n_valid, 100, 30
        h0 = H0["validation_size_regression"]
    else:
        v, ok, doubtful = int(smallest_class or 0), 30, 10
        h0 = H0["validation_size"]
    verdict = "ok" if v >= ok else "doubtful" if v >= doubtful else "failed"
    return _check("validation_size", verdict, v, ok, "validation_size", h0=h0)


def check_beats_baseline(loss_valid: float, base_valid: float, balanced: bool = False) -> dict[str, Any]:
    """u = 1 − L / B on the validation part. With class_weight = balanced both losses are weighted
    as the fit (the baseline then the best constant of balanced weights, one share per class), as
    early stopping reads them: a balanced fit moves its probabilities away from the class shares,
    and an unweighted loss against the prevalence would call a model that ranks well «failed»."""
    h0 = H0["beats_baseline_balanced"] if balanced else None
    if not (base_valid > 0) or not math.isfinite(loss_valid):
        return _check("beats_baseline", "not_computed", advice="beats_baseline_constant", h0=h0)
    u = 1 - loss_valid / base_valid
    verdict = "ok" if u >= 0.05 else "doubtful" if u > 0 else "failed"
    return _check("beats_baseline", verdict, u, 0.05, "beats_baseline_balanced" if balanced else "beats_baseline", h0=h0)


def check_overfitting(curve: list[float] | np.ndarray, kept: int, base_valid: float) -> dict[str, Any]:
    """r = (L_kept − L_best) / (B − L_best) on the validation loss curve, L_best its lowest value up
    to the tree kept, B the baseline's loss: the share of the gain over the baseline that the trees
    after the best give back. 0 when the tree kept is the best (early stopping on the loss); it
    grows when the trees go on learning the training rows while the validation gets worse. The
    training loss is not read: deep trees bring it near 0 by construction."""
    c = np.asarray(curve, dtype=float)[: kept + 1]
    if not len(c) or not np.isfinite(c).all() or not math.isfinite(base_valid):
        return _check("overfitting", "not_computed", advice="overfitting_no_gain")
    best = float(c.min())
    gain = base_valid - best
    if not gain > 0:
        return _check("overfitting", "not_computed", advice="overfitting_no_gain")
    r = max(0.0, (float(c[-1]) - best) / gain)
    verdict = "ok" if r <= OVERFITTING_OK else "doubtful" if r <= OVERFITTING_FAILED else "failed"
    return _check("overfitting", verdict, r, OVERFITTING_OK, "overfitting")


def check_trees_limit(early_stopping: bool, stopped_early: bool, stopped_by_time: bool, kept: int,
                      last_loss: float, best_loss: float, learning_rate: float = TREES_FEW_RATE,
                      stopped_by_size: bool = False) -> dict[str, Any]:
    """With early stopping: doubtful when it did not stop (max_trees reached, the time budget, or
    the model file's size), or when it stopped before 20 trees with a learning rate above 0.05
    (each tree a big step: a lower rate helps). With the default rate or a lower one, an early
    stop says that the pattern is weak, which beats_baseline reads: lowering the rate again would
    not help (the threshold of 20 is then not written). Without early stopping: the last tree's
    validation loss against the best."""
    if early_stopping:
        few = 20 if learning_rate > TREES_FEW_RATE else math.nan
        if stopped_by_size:
            return _check("trees_limit", "doubtful", kept, few, "trees_size")
        if stopped_by_time or not stopped_early:
            return _check("trees_limit", "doubtful", kept, few, "trees_more")
        if kept < 20 and learning_rate > TREES_FEW_RATE:
            return _check("trees_limit", "doubtful", kept, few, "trees_few")
        return _check("trees_limit", "ok", kept, few)
    h0 = H0["trees_limit_no_stop"]
    excess = (last_loss - best_loss) / abs(best_loss) if best_loss else (0.0 if last_loss == best_loss else math.inf)
    if stopped_by_size:
        return _check("trees_limit", "doubtful", excess, 0.01, "trees_size", h0=h0)
    if stopped_by_time:
        return _check("trees_limit", "doubtful", excess, 0.01, "trees_more", h0=h0)
    verdict = "doubtful" if excess > 0.01 else "ok"
    return _check("trees_limit", verdict, excess, 0.01, "trees_no_stop", h0=h0)


def check_class_imbalance(shares: np.ndarray) -> dict[str, Any]:
    present = shares[shares > 0]
    r = float(present.max() / present.min())
    verdict = "ok" if r < 4 else "doubtful" if r < 20 else "failed"
    return _check("class_imbalance", verdict, r, 4, "class_imbalance")


def check_calibration(e: float, noise: float, balanced: bool, smallest_class: int) -> dict[str, Any]:
    """The ECE against max(0.05, the noise): `noise` is the ECE that well calibrated probabilities
    reach 95 % of the time with these rows (ece_noise; about 0.16 with 80 validation rows), so that
    a small validation part does not fail by chance alone. Failed above max(0.10, noise + 0.05).
    Not computed with fewer than 10 validation rows of a class."""
    if smallest_class < CALIBRATION_MIN_CLASS or not math.isfinite(e):
        return _check("calibration", "not_computed", advice="calibration_few_rows")
    ok_at = max(CALIBRATION_OK, noise)
    failed_at = max(CALIBRATION_FAILED, noise + CALIBRATION_OK)
    verdict = "ok" if e <= ok_at else "doubtful" if e <= failed_at else "failed"
    return _check("calibration", verdict, e, ok_at, "calibration_balanced" if balanced else "calibration")


def check_calibration_slope(slope: float, se: float, smallest_class: int) -> dict[str, Any]:
    """The slope read with its 95 % interval: ok when the interval meets 0.8–1.25, doubtful when
    it misses it but meets 0.5–2, failed when it misses both. A slope from a few rows has a wide
    interval and is not called wrong for its noise alone."""
    if smallest_class < CALIBRATION_MIN_CLASS:
        return _check("calibration_slope", "not_computed", advice="calibration_few_rows")
    if not math.isfinite(slope) or not math.isfinite(se):
        return _check("calibration_slope", "not_computed", advice="slope_separated")
    lo, hi = slope - Z975 * se, slope + Z975 * se

    def meets(band: tuple[float, float]) -> bool:
        return hi >= band[0] and lo <= band[1]
    verdict = "ok" if meets(SLOPE_OK) else "doubtful" if meets(SLOPE_DOUBTFUL) else "failed"
    return _check("calibration_slope", verdict, slope, advice="calibration_slope")


def check_importance_stability(mas_train: np.ndarray, mas_valid: np.ndarray, valid_rows_in_sample: int) -> dict[str, Any]:
    if len(mas_train) < 3:
        return _check("importance_stability", "not_computed", advice="importance_few_predictors")
    if valid_rows_in_sample < 50:
        return _check("importance_stability", "not_computed", advice="importance_few_rows")
    top = 10
    union = sorted(set(np.lexsort((np.arange(len(mas_train)), -mas_train))[:top].tolist())
                   | set(np.lexsort((np.arange(len(mas_valid)), -mas_valid))[:top].tolist()))
    rho = spearman(mas_train[union], mas_valid[union])
    if not math.isfinite(rho):
        return _check("importance_stability", "not_computed", advice="importance_flat")
    verdict = "ok" if rho >= 0.8 else "doubtful" if rho >= 0.5 else "failed"
    return _check("importance_stability", verdict, rho, 0.8, "importance_stability")


def check_zero_importance(gain: np.ndarray, predictors: list[str]) -> list[dict[str, Any]]:
    zero = [j for j in range(len(predictors)) if gain[j] == 0]
    if not zero:
        return [_check("zero_importance", "ok", 0.0)]
    return [_check("zero_importance", "doubtful", 0.0, advice="zero_importance", level_2=predictors[j]) for j in zero]


def check_leakage(first: str | None, share: float, n_predictors: int, metric: float, time_advice: bool) -> dict[str, Any]:
    """Two signs: the first predictor's share of |SHAP| (with two predictors or more: alone, a
    predictor's share is 1 by construction), and a validation fit near perfect (R², AUC or macro
    AUC). Failed when one predictor holds ≥ 0.8 of |SHAP| and the fit is ≥ 0.98; doubtful with a
    fit ≥ 0.98 alone, or with a share ≥ 0.6 and a fit ≥ 0.95. A dominant predictor in a fit far
    from perfect is ok: real data often have one. With one predictor the value is the fit, its
    threshold 0.98."""
    perfect = math.isfinite(metric) and metric >= LEAKAGE_PERFECT
    high = math.isfinite(metric) and metric >= LEAKAGE_HIGH
    advice = "leakage_time" if time_advice else "leakage"
    if n_predictors < 2:
        return _check("leakage", "doubtful" if perfect else "ok", metric, LEAKAGE_PERFECT, advice, level_2=first)
    big = math.isfinite(share) and share >= LEAKAGE_SHARE
    some = math.isfinite(share) and share >= LEAKAGE_SOME_SHARE
    verdict = "failed" if big and perfect else "doubtful" if perfect or (some and high) else "ok"
    return _check("leakage", verdict, share, LEAKAGE_SHARE, advice, level_2=first)


def write_checks(rows: Rows, checks: list[dict[str, Any]]) -> None:
    for c in checks:
        lv, lv2 = c["check"], c["level_2"]
        text(rows, "assumption", "h0", c["h0"], level=lv, level_2=lv2)
        if math.isfinite(c["value"]):
            number(rows, "assumption", "value", c["value"], level=lv, level_2=lv2)
        if math.isfinite(c["threshold"]):
            number(rows, "assumption", "threshold", c["threshold"], level=lv, level_2=lv2)
        text(rows, "assumption", "verdict", c["verdict"], level=lv, level_2=lv2)
        if c["advice"]:
            text(rows, "assumption", "advice", c["advice"], level=lv, level_2=lv2)
            text(rows, "assumption", "advice_id", c["advice_id"], level=lv, level_2=lv2)


# ── SHAP: the sample, the importance, the beeswarm ──────────────────────────────
def shap_sample_size(task: dict[str, Any], trees_total: int, depth: int) -> int:
    """clamp(shap_cost_budget / (T · L · D²), 200, max_shap_rows): TreeSHAP's cost per row is
    T · L · D² (T the trees, L = 2^D the leaves, D the depth)."""
    d = max(1, int(depth))
    cost = max(1, int(trees_total)) * (2 ** d) * d * d
    most = int(limit(task, "max_shap_rows"))
    return int(min(most, max(200, math.floor(limit(task, "shap_cost_budget") / cost))))


def shap_size_in_time(task: dict[str, Any], shap: Callable[[np.ndarray], Any], n: int, size: int) -> int:
    """The SHAP sample's size on the time left: the SHAP values of the first SHAP_PILOT_ROWS rows
    timed, the size cut to what ends by SHAP_TIME_SHARE of the step's time (deadline), never
    below the pilot's rows. With time enough, or without a time budget, `size` as it is: the
    results then do not depend on the machine's speed."""
    end = deadline(task, SHAP_TIME_SHARE)
    size = min(size, n)
    if end is None or size <= SHAP_PILOT_ROWS:
        return size
    pilot = np.arange(SHAP_PILOT_ROWS)
    t0 = time.monotonic()
    shap(pilot)
    per_row = (time.monotonic() - t0) / len(pilot)
    left = end - time.monotonic()
    if left >= per_row * size:
        return size
    return int(max(SHAP_PILOT_ROWS, math.floor(left / per_row))) if per_row > 0 else size


def shap_sample(n: int, validation: np.ndarray, size: int, gen: np.random.Generator) -> np.ndarray:
    """The rows (positions, increasing) of the SHAP sample: all when n ≤ size; else drawn with the
    generator in proportion to the parts, at least min(500, the validation rows) from validation."""
    if n <= size:
        return np.arange(n)
    train, valid = np.flatnonzero(~validation), np.flatnonzero(validation)
    m_val = min(len(valid), max(half_up(size * len(valid) / n), min(500, len(valid))))
    m_tr = min(len(train), size - m_val)
    picked_tr = gen.choice(train, size=m_tr, replace=False)
    picked_val = gen.choice(valid, size=m_val, replace=False)
    return np.sort(np.concatenate([picked_tr, picked_val]))


def colour_bins(x: np.ndarray, is_category: bool) -> list[str]:
    """The beeswarm's colour of each point: the quintile of the predictor's value among the rows
    drawn (numbers: cut at the 20, 40, 60 and 80 % quantiles, a value equal to a cut in the lower
    bin), «category» for a category, «missing» for a missing value."""
    if is_category:
        return ["missing" if is_missing(v) else "category" for v in x]
    v = np.asarray(x, dtype=float)
    present = v[~np.isnan(v)]
    out = ["missing"] * len(v)
    if not len(present):
        return out
    cuts = np.quantile(present, [0.2, 0.4, 0.6, 0.8])
    for i, value in enumerate(v):
        if not math.isnan(value):
            out[i] = COLOUR_BINS[int(np.searchsorted(cuts, value, side="left"))]
    return out


def beeswarm_offsets(x: np.ndarray, bins: int = BEESWARM_BINS, half_width: float = BEESWARM_HALF_WIDTH) -> np.ndarray:
    """The vertical offsets of one predictor's points: sorted by SHAP value they fall in `bins`
    equal bins of the x range; the j-th point of a bin moves by (0, +1, −1, +2, −2, …)_j × h,
    h = half_width / max(1, ⌈c / 2⌉), c the largest bin count. Deterministic, within ±half_width."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if not n:
        return np.zeros(0)
    lo, hi = float(x.min()), float(x.max())
    b = np.zeros(n, dtype=np.int64) if hi == lo else np.minimum(bins - 1, np.floor(bins * (x - lo) / (hi - lo)).astype(np.int64))
    order = np.lexsort((np.arange(n), x))
    c = int(np.bincount(b, minlength=bins).max())
    h = half_width / max(1, math.ceil(c / 2))
    seen = np.zeros(bins, dtype=np.int64)
    out = np.zeros(n)
    for i in order:
        j = int(seen[b[i]])
        seen[b[i]] += 1
        step = (j + 1) // 2
        out[i] = (step if j % 2 == 1 else -step) * h
    return out


def beeswarm(rows: Rows, task: dict[str, Any], prepared: Prepared, sample: np.ndarray, phi: np.ndarray, mas: np.ndarray) -> None:
    """point|shap_beeswarm|<predictor>|<colour>|<i>: x0 the SHAP value, value the y position (the
    predictor of rank r at k − r + 1, plus its offset), x1 the predictor's value (NULL for a
    category or a missing value). The same m = ⌊max_chart_points / k⌋ rows for every predictor."""
    ranked = [j for j in np.lexsort((np.arange(len(mas)), -mas)) if mas[j] > 0]
    k = min(int(limit(task, "max_beeswarm_predictors")), len(ranked))
    if not k:
        return
    m = int(limit(task, "max_chart_points")) // k
    chosen = np.arange(len(sample)) if len(sample) <= m else np.sort(prepared.gen.choice(len(sample), size=m, replace=False))
    for r, j in enumerate(ranked[:k], start=1):
        x = phi[chosen, j]
        is_cat = prepared.types[j] == "category"
        raw = prepared.X[prepared.names[j]].to_numpy()[sample[chosen]]
        values = raw if is_cat else raw.astype(float)
        colours = colour_bins(values, is_cat)
        y = (k - r + 1) + beeswarm_offsets(x)
        x1 = [math.nan] * len(x) if is_cat else values
        add(rows, "point", colours, value=y, level="shap_beeswarm", level_2=prepared.predictors[j], x0=x, x1=x1,
            index=list(range(len(x))))


# ── finish: every row of «The output» ───────────────────────────────────────────
def _margins(prepared: Prepared, fitted: Fitted) -> np.ndarray:
    """The margins of every row used, in their order."""
    shape = (prepared.n,) if prepared.task != "multiclass" else (prepared.n, prepared.k)
    raw = np.zeros(shape)
    raw[prepared.train] = np.asarray(fitted.raw_train, dtype=float)
    raw[prepared.valid] = np.asarray(fitted.raw_validation, dtype=float)
    return raw


def _loss_curve(fitted: Fitted) -> list[float]:
    return list((fitted.curves.get(fitted.loss_metric_name) or {}).get("validation") or [])


def finish(task: dict[str, Any], prepared: Prepared, fitted: Fitted) -> dict[str, Any]:
    """The results, the rows output, the card and the model file of a fit."""
    P, what = prepared, prepared.task
    p = P.params
    rows = rows_start(task)
    tr, va = P.train, P.valid
    y, w = P.y, P.w
    raw = _margins(P, fitted)

    # ── the model section: texts, counts ──
    text(rows, "model", "formula", f"{P.outcome} ~ {' + '.join(P.predictors)}")
    text(rows, "model", "software", software(fitted.packages))
    text(rows, "model", "task", what)
    text(rows, "model", "library", fitted.library)
    text(rows, "model", "objective", fitted.objective)
    text(rows, "model", "stop_metric", fitted.stop_metric_name)
    text(rows, "model", "validation", P.split)
    text(rows, "model", "importance_type", fitted.importance_type)
    if what == "binary":
        text(rows, "model", "event_value", P.event_value)
    trees_total = fitted.trees * P.k
    size = shap_size_in_time(task, fitted.shap, P.n, shap_sample_size(task, trees_total, int(p.get("max_depth", 6))))
    sample = shap_sample(P.n, P.validation, size, P.gen)
    counts = {"n": P.n, "n_missing": P.n_missing, "n_train": len(tr), "n_validation": len(va), "n_predictors": len(P.predictors),
              "n_categorical": sum(t == "category" for t in P.types), "trees": fitted.trees, "best_iteration": fitted.best_iteration,
              "max_trees": int(p.get("max_trees", 1000)), "stopped_early": int(fitted.stopped_early),
              "stopped_by_time": int(fitted.stopped_by_time), "threads": P.threads, "shap_rows": len(sample),
              "seed": int(p.get("seed", 1))}
    for k_, v in counts.items():
        number(rows, "model", k_, v)

    # ── the metrics by task, and the losses the diagnostics read ──
    checks: list[dict[str, Any]] = []
    if what == "regression":
        pred = raw
        base = wmean(y[tr], w[tr])
        m = {"train_rmse": rmse(y[tr], pred[tr], w[tr]), "train_mae": mae(y[tr], pred[tr], w[tr]),
             "train_r_squared": r_squared(y[tr], pred[tr], w[tr]), "validation_rmse": rmse(y[va], pred[va], w[va]),
             "validation_mae": mae(y[va], pred[va], w[va]), "validation_r_squared": r_squared(y[va], pred[va], w[va]),
             "baseline_rmse": rmse(y[va], np.full(len(va), base), w[va]), "validation_mean": wmean(y[va], w[va])}
        loss = (m["validation_rmse"], m["baseline_rmse"])
        fit_metric = m["validation_r_squared"]
        checks.append(check_validation_size(what, len(va), None))
    elif what == "binary":
        prob = sigmoid(raw)
        prev = wmean(y[tr].astype(float), w[tr])
        thr = float(p.get("threshold", 0.5))
        C = confusion(y[va], (prob[va] >= thr).astype(np.int64), w[va], 2)
        at = binary_at(C)
        m = {"events": float(w[y == 1].sum()), "validation_events": float(w[va][y[va] == 1].sum()),
             "train_auc": auc(prob[tr], y[tr], w[tr]), "validation_auc": auc(prob[va], y[va], w[va]),
             "train_logloss": logloss(y[tr], prob[tr], w[tr]), "validation_logloss": logloss(y[va], prob[va], w[va]),
             "baseline_logloss": logloss(y[va], np.full(len(va), prev), w[va]), "train_brier": brier(y[tr], prob[tr], w[tr]),
             "validation_brier": brier(y[va], prob[va], w[va]), "threshold": thr,
             **{f"validation_{k_}": v for k_, v in at.items()}}
        loss = (m["validation_logloss"], m["baseline_logloss"])
        fit_metric = m["validation_auc"]
        checks.append(check_validation_size(what, len(va), int(np.bincount(y[va], minlength=2).min())))
    else:
        Pr = softmax(raw)
        k = P.k
        share_tr = np.array([float(w[tr][y[tr] == c].sum()) for c in range(k)]) / float(w[tr].sum())
        share_va = np.array([float(w[va][y[va] == c].sum()) for c in range(k)]) / float(w[va].sum())
        pred_cls = np.argmax(Pr, axis=1)
        C = confusion(y[va], pred_cls[va], w[va], k)
        pc = per_class(C)
        present = pc["support"] > 0
        aucs = np.array([auc(Pr[va, c], (y[va] == c).astype(np.int64), w[va]) if present[c] else math.nan for c in range(k)])
        m = {"n_classes": k, "train_mlogloss": mlogloss(y[tr], Pr[tr], w[tr]),
             "validation_mlogloss": mlogloss(y[va], Pr[va], w[va]),
             "baseline_mlogloss": mlogloss(y[va], np.tile(share_tr, (len(va), 1)), w[va]),
             "train_accuracy": wmean((pred_cls[tr] == y[tr]).astype(float), w[tr]),
             "validation_accuracy": float(np.trace(C) / C.sum()),
             "validation_balanced_accuracy": float(np.mean(pc["recall"][present])),
             "validation_macro_auc": float(np.nanmean(aucs[present])) if np.isfinite(aucs[present]).any() else math.nan,
             "validation_macro_f1": float(np.mean(pc["f1"][present]))}
        loss = (m["validation_mlogloss"], m["baseline_mlogloss"])
        fit_metric = m["validation_macro_auc"]
        counts_va = np.bincount(y[va], minlength=k)
        checks.append(check_validation_size(what, len(va), int(counts_va[counts_va > 0].min())))
    for k_, v in m.items():
        number(rows, "model", k_, v)

    # with class_weight = balanced the diagnostics read the losses weighted as the fit (and as early
    # stopping reads them): the best constant of those weights, one share per class, is the baseline
    balanced = what != "regression" and p.get("class_weight") == "balanced"
    if balanced:
        fw = P.fit_w
        if what == "binary":
            even = wmean(y[tr].astype(float), fw[tr])
            loss = (logloss(y[va], prob[va], fw[va]), logloss(y[va], np.full(len(va), even), fw[va]))
        else:
            even_k = np.array([float(fw[tr][y[tr] == c].sum()) for c in range(P.k)]) / float(fw[tr].sum())
            loss = (mlogloss(y[va], Pr[va], fw[va]), mlogloss(y[va], np.tile(even_k, (len(va), 1)), fw[va]))

    # ── notes ──
    notes = list(P.notes)
    if bool(p.get("early_stopping", True)):
        notes.insert(0, "validation_chose_trees")
    if fitted.stopped_by_time:
        notes.append("time_budget")
    if fitted.stopped_by_size:
        notes.append("model_size")
    if len(sample) < P.n:
        notes.append("shap_sampled")
    for nid in dict.fromkeys(notes):
        note(rows, nid, NOTES[nid])

    # ── importance: gain, mean |SHAP| ──
    phi, _base = fitted.shap(sample)
    phi = np.asarray(phi, dtype=float)
    absphi = np.abs(phi)
    in_tr = ~P.validation[sample]
    if what == "multiclass":
        mas_class = absphi.mean(axis=0)  # k × p
        mas = mas_class.sum(axis=0)
        mas_t = absphi[in_tr].mean(axis=0).sum(axis=0) if in_tr.any() else np.full(len(P.predictors), math.nan)
        mas_v = absphi[~in_tr].mean(axis=0).sum(axis=0) if (~in_tr).any() else np.full(len(P.predictors), math.nan)
    else:
        mas_class = None
        mas = absphi.mean(axis=0)
        mas_t = absphi[in_tr].mean(axis=0) if in_tr.any() else np.full(len(P.predictors), math.nan)
        mas_v = absphi[~in_tr].mean(axis=0) if (~in_tr).any() else np.full(len(P.predictors), math.nan)
    gain = np.array([float(fitted.gain.get(j, 0.0)) for j in range(len(P.predictors))])
    g_total, s_total = float(gain.sum()), float(mas.sum())
    order = np.lexsort((np.arange(len(mas)), -mas))
    for r, j in enumerate(order, start=1):
        name = P.predictors[j]
        number(rows, "importance", ["gain", "gain_share", "mean_abs_shap", "mean_abs_shap_share", "rank", "mean_abs_shap_train",
                                    "mean_abs_shap_validation", "missing_share"],
               [gain[j], gain[j] / g_total if g_total > 0 else math.nan, mas[j], mas[j] / s_total if s_total > 0 else math.nan,
                r, mas_t[j], mas_v[j], P.missing_share[j]], level=name)
        if mas_class is not None:
            number(rows, "importance", "mean_abs_shap", mas_class[:, j], level=name, level_2=P.classes)

    # ── the diagnostics ──
    L_va, B_va = loss
    checks.append(check_beats_baseline(L_va, B_va, balanced))
    curve = _loss_curve(fitted)
    checks.append(check_overfitting(curve, fitted.best_iteration, B_va))
    last, best = (curve[-1], min(curve)) if curve else (math.nan, math.nan)
    checks.append(check_trees_limit(bool(p.get("early_stopping", True)), fitted.stopped_early, fitted.stopped_by_time,
                                    fitted.best_iteration + 1, last, best, float(p.get("learning_rate", TREES_FEW_RATE)),
                                    fitted.stopped_by_size))
    if what != "regression":
        shares = np.array([float(w[tr][y[tr] == c].sum()) for c in range(len(P.classes))]) / float(w[tr].sum())
        checks.append(check_class_imbalance(shares))
    if what == "binary":
        smallest = int(np.bincount(y[va], minlength=2).min())
        pv, yv = prob[va], y[va].astype(float)
        checks.append(check_calibration(ece(pv, yv, w[va]), ece_noise(pv, w[va]), balanced, smallest))
        checks.append(check_calibration_slope(*calibration_slope(pv, yv, w[va]), smallest))
    checks.append(check_importance_stability(mas_t, mas_v, int((~in_tr).sum())))
    checks.extend(check_zero_importance(gain, P.predictors))
    first = int(order[0]) if len(order) and s_total > 0 else None
    checks.append(check_leakage(P.predictors[first] if first is not None else None,
                                mas[first] / s_total if first is not None else math.nan, len(P.predictors), fit_metric,
                                P.split == "random" and P.has_time_column))
    write_checks(rows, checks)

    # ── the classes and the confusion matrix ──
    if what == "multiclass":
        for c, lv in enumerate(P.classes):
            number(rows, "class", ["support", "share_train", "share_validation", "precision", "recall", "f1", "auc_ovr"],
                   [pc["support"][c], share_tr[c], share_va[c], pc["precision"][c], pc["recall"][c], pc["f1"][c], aucs[c]],
                   level=lv)
    if what != "regression":
        for a, la in enumerate(P.classes):
            number(rows, "cell", "observed", C[a, :], level=la, level_2=P.classes)

    # ── the curves ──
    most = int(limit(task, "max_chart_points"))
    curve_names = [("learning_loss", fitted.loss_metric_name)]
    if fitted.stop_metric_name != fitted.loss_metric_name:
        curve_names.append(("learning_stop", fitted.stop_metric_name))
    for level, metric in curve_names:
        for part in ("train", "validation"):
            values = np.asarray((fitted.curves.get(metric) or {}).get(part) or [], dtype=float)
            if not len(values):
                continue
            i = thin(len(values), most, keep=[fitted.best_iteration])
            points(rows, "curve", level, "y", x=i + 1.0, y=values[i], level_2=part)
    if what == "binary":
        fpr, tpr = roc(prob[va], y[va], w[va])
        i = thin(len(fpr), ROC_POINTS)
        points(rows, "curve", "roc", "y", x=fpr[i], y=tpr[i], level_2="validation")
    if what == "multiclass":
        support = pc["support"]
        top = [c for c in np.lexsort((np.arange(P.k), -support)) if support[c] > 0][: int(limit(task, "max_roc_classes"))]
        for c in sorted(top):
            fpr, tpr = roc(Pr[va, c], (y[va] == c).astype(np.int64), w[va])
            i = thin(len(fpr), ROC_OVR_POINTS)
            points(rows, "curve", "roc_ovr", "y", x=fpr[i], y=tpr[i], level_2=P.classes[c])

    # ── the points: the beeswarm first (the generator's order), then the capped charts ──
    if what != "multiclass":
        beeswarm(rows, task, P, sample, phi, mas)
    if what == "regression":
        resid = y[va] - pred[va]
        i = cap(len(va), P.gen, most, score=resid, keep=int(limit(task, "extreme_points")))
        labels = np.asarray(extreme_labels(resid, P.rows[va], keep=int(limit(task, "extreme_points"))), dtype=object)[i]
        points(rows, "point", "observed_predicted", "y", x=pred[va][i], y=y[va][i], labels=labels)
        points(rows, "point", "resid_predicted", "y", x=pred[va][i], y=resid[i], labels=labels)
        h = hist(resid)
        points(rows, "hist", "residual", "count", x=h["x0"], y=h["count"], x1=h["x1"])
    if what == "binary":
        b = calibration_bins(prob[va], y[va].astype(float), w[va])
        points(rows, "point", "calibration", "y", x=b["p"], y=b["y"], x1=b["rows"])
        for c, lv in enumerate(P.classes):
            h = hist(prob[va][y[va] == c], bins=PROBABILITY_BINS, lo=0.0, hi=1.0)
            points(rows, "hist", "probability", "count", x=h["x0"], y=h["count"], x1=h["x1"], level_2=lv)

    # ── the rows output, the card, the model file ──
    output = None
    if (task.get("output") or p.get("output")) == "rows":
        output = rows_output(P, fitted, raw)
    entry = model_file_entry(fitted.model_name, fitted.model_format, fitted.model_bytes)
    c = base_card(task, P.inputs, fitted.packages)
    c["boosting"] = {"library": fitted.library, "library_version": fitted.library_version, "task": what,
                     "objective": fitted.objective, "classes": list(P.classes), "event_value": P.event_value,
                     "threshold": float(p.get("threshold", 0.5)) if what == "binary" else None, "trees": int(fitted.trees),
                     "best_iteration": int(fitted.best_iteration), "threads": int(P.threads),
                     "missing_level": fitted.missing_level, "model_file": entry}
    return {"rows": rows, "output": output, "card": c, "model_file": {"name": fitted.model_name, "data": fitted.model_bytes}}


# ── the rows output and Predict ─────────────────────────────────────────────────
def _refuse_clash(df: pd.DataFrame, made: list[str]) -> None:
    clash = [c for c in made if c in df.columns]
    if clash:
        raise Refusal("output_column_exists", f"the rows already have a column «{clash[0]}»: rename it before the node",
                      {"column": clash[0]})


def rows_output(P: Prepared, fitted: Fitted, raw_used: np.ndarray) -> pd.DataFrame:
    """The input's rows with the model's columns (docs: «outputs.rows»): every row gets its
    prediction (a missing predictor or a level the fit did not see takes the missing-value path);
    the rows the fit used their part, and for a regression their residual."""
    df = P.df
    made = {"regression": ["pred_value", "pred_residual", "pred_part"], "binary": ["pred_prob", "pred_class", "pred_part"],
            "multiclass": ["pred_class", "pred_prob", *(f"pred_prob_{c}" for c in P.classes), "pred_part"]}[P.task]
    _refuse_clash(df, made)
    X, _ = positional_frame(df, P.predictors, P.types, P.levels, P.names)
    raw = np.asarray(fitted.predict_raw(X), dtype=float)
    used = np.flatnonzero(P.keep)
    raw[used] = raw_used
    part = np.full(len(df), None, dtype=object)
    part[used] = np.where(P.validation, "validation", "train")
    out = df.copy()
    if P.task == "regression":
        resid = np.full(len(df), math.nan)
        resid[used] = P.y - raw_used
        out["pred_value"] = raw
        out["pred_residual"] = resid
    elif P.task == "binary":
        prob = sigmoid(raw)
        out["pred_prob"] = prob
        out["pred_class"] = np.where(prob >= float(P.params.get("threshold", 0.5)), P.classes[1], P.classes[0]).astype(object)
    else:
        Pr = softmax(raw)
        best = np.argmax(Pr, axis=1)
        out["pred_class"] = np.asarray(P.classes, dtype=object)[best]
        out["pred_prob"] = Pr[np.arange(len(df)), best]
        for c, lv in enumerate(P.classes):
            out[f"pred_prob_{lv}"] = Pr[:, c]
    out["pred_part"] = part
    return out.reset_index(drop=True)


def card_frame(card: dict[str, Any], df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, int]]:
    """The positional frame of new rows from a card's inputs: the columns must be there, a number
    must still be a number; a level the fit did not see becomes missing (counted)."""
    inputs = card.get("inputs") or []
    _refuse_missing_columns(df, [i["name"] for i in inputs])
    for i in inputs:
        if i["type"] == "number" and kind(df, i["name"]) not in ("number", "integer"):
            raise Refusal("column_kind", f"the column «{i['name']}» must hold numbers, as when the model was fitted",
                          {"column": i["name"]})
    return positional_frame(df, [i["name"] for i in inputs], [i["type"] for i in inputs],
                            [i.get("levels") if i["type"] == "category" else None for i in inputs])


def predict(task: dict[str, Any], card: dict[str, Any], raw_of: Callable[[pd.DataFrame], np.ndarray]) -> dict[str, Any]:
    """Predict (mode predict): the card's frame of the input, the library's margins (raw_of, from
    the model file), the columns of PREDICT.columns for the card's variant."""
    df = model_input(task)
    b = card.get("boosting") or {}
    what = b.get("task")
    classes = list(b.get("classes") or [])
    made = {"regression": ["prediction"], "binary": ["prediction", "prediction_class"],
            "multiclass": ["prediction", "prediction_probability", *(f"prediction_prob_{c}" for c in classes)]}.get(what)
    if made is None:
        raise Refusal("card", "the model card has no boosting task: it was not made by a boosting node")
    _refuse_clash(df, made)
    X, unseen = card_frame(card, df)
    for column, count in unseen.items():
        warnings.warn(f"{count} rows of the column «{column}» hold a value the model did not see when fitted: they take the "
                      "model's path for a missing value")
    raw = np.asarray(raw_of(X), dtype=float)
    out = df.copy()
    if what == "regression":
        out["prediction"] = raw
    elif what == "binary":
        prob = sigmoid(raw)
        thr = (task.get("params") or {}).get("threshold")
        thr = float(b.get("threshold") or 0.5) if thr is None else float(thr)
        out["prediction"] = prob
        out["prediction_class"] = np.where(prob >= thr, classes[1], classes[0]).astype(object)
    else:
        Pr = softmax(raw)
        best = np.argmax(Pr, axis=1)
        out["prediction"] = np.asarray(classes, dtype=object)[best]
        out["prediction_probability"] = Pr[np.arange(len(df)), best]
        for c, lv in enumerate(classes):
            out[f"prediction_prob_{lv}"] = Pr[:, c]
    return {"output": out.reset_index(drop=True)}


# ── the module's own tests, run by the group's self-test with the common library's ──
from tab_selftest import register  # noqa: E402 — after the module's functions, which the tests call


def _expect(ok: Any, message: str) -> None:
    if not ok:
        raise AssertionError(message)


def _gen(seed: int = 1) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(seed))


@register("boosting: the validation part, random, stratified, by groups and in time")
def _test_splits(code: Any) -> None:
    v = split_random(103, 0.2, _gen())
    _expect(v.sum() == 21 and np.array_equal(v, split_random(103, 0.2, _gen())), f"random: {v.sum()} rows")
    _expect(not np.array_equal(v, split_random(103, 0.2, _gen(2))), "random: two seeds, one split")
    strata = np.repeat([0, 1, 2, 3], [50, 7, 1, 2])
    s = split_random(len(strata), 0.2, _gen(), strata, 4)
    got = [int(s[strata == c].sum()) for c in range(4)]
    _expect(got == [10, 1, 0, 1], f"stratified: {got} rows of each class in validation")
    _expect(split_random(10, 0.25, _gen()).sum() == 3, "a half goes up: round(2.5) is 3")
    groups = [f"g{i % 13}" for i in range(200)]
    g = split_grouped(groups, 0.2, _gen())
    names = np.array(groups)
    _expect(all(len(set(g[names == x])) == 1 for x in set(groups)), "by groups: a group on both sides")
    _expect(g.sum() >= 40, f"by groups: {g.sum()} rows in validation, at least 40 asked")
    values, total, want = sort_labels(groups), 0, np.zeros(200, dtype=bool)
    for j in _gen().permutation(len(values)):  # the definition, again: whole values until 40 rows
        if total >= 40:
            break
        want |= names == values[j]
        total += int((names == values[j]).sum())
    _expect(np.array_equal(g, want), "by groups: not the values drawn, in their order, until 40 rows")
    _expect(np.array_equal(g, split_grouped(groups, 0.2, _gen())), "by groups: the same seed, another split")
    t = np.array([4, 1, 3, 2, 4, 3, 1, 2, 2, 4, 3, 5, 5, 0, 3, 3, 2, 1, 0, 4], dtype=float)
    for share, want in ((0.1, 2), (0.2, 6), (0.3, 6), (0.5, 11)):
        val, order = split_time(t, share)
        _expect(val.sum() == want, f"in time, share {share}: {val.sum()} rows, expected {want}")
        _expect(t[~val].max() < t[val].min(), f"in time, share {share}: a time on both sides")
        _expect(np.array_equal(order, np.flatnonzero(~val)[np.argsort(t[~val], kind="stable")]), "the training rows in time order")


@register("boosting: prepare keeps missing predictors, leaves out rows and refuses what it must")
def _test_prepare(code: Any) -> None:
    gen = _gen(3)
    n = 60
    df = pd.DataFrame({"y": gen.normal(size=n), "x": gen.normal(size=n),
                       "c": pd.Series(gen.choice(["b", "a", "c"], size=n), dtype=object),
                       "k2": pd.Series(gen.choice(["p", "q"], size=n), dtype=object),
                       "w": np.where(np.arange(n) % 10 == 3, 0.0, 1.0)})
    df.loc[[2, 5], "x"] = np.nan
    df.loc[7, "c"] = None
    df.loc[9, "y"] = np.nan
    task = {"model": "xgboost", "group_version": "selftest", "mode": "fit", "_frame": df, "limits": {},
            "params": {"task": "regression", "outcome": "y", "predictors": ["x", "c"], "weights": "w"}}
    from tab_protocol import catalog
    task["limits"] = dict(catalog()["limits"])
    P = prepare(task)
    _expect(P.n == 53 and P.n_missing == 1 and "zero_weights" in P.notes, f"rows: {P.n} used, {P.n_missing} missing")
    _expect(P.types == ["number", "category"] and P.levels[1] == ["a", "b", "c"], f"kinds {P.types} levels {P.levels}")
    _expect(abs(P.missing_share[0] - 2 / 53) < 1e-15 and abs(P.missing_share[1] - 1 / 53) < 1e-15, "missing shares")
    _expect(list(P.X.columns) == ["v1", "v2"] and list(P.X["v2"].cat.categories) == ["a", "b", "c"], "the positional frame")
    _expect(P.validation.sum() == half_up(0.2 * 53) and P.threads == 2, "the split and the threads")
    bad = df.assign(d=[None] * n)
    bad["d"] = pd.Series([pd.Timestamp("2026-01-01").date()] * n, dtype=object)
    bad.attrs["kinds"] = {"d": "date"}
    for params, code_ in (({"predictors": ["x", "d"]}, "date_predictor"),
                          ({"task": "binary", "outcome": "c", "event_value": "a"}, "outcome_not_binary"),
                          ({"task": "multiclass", "outcome": "k2", "predictors": ["x"]}, "multiclass_two"),
                          ({"task": "binary", "outcome": "k2", "event_value": "r"}, "event_value"),
                          ({"outcome": "c", "predictors": ["x"]}, "outcome_not_number")):
        t = {**task, "_frame": bad, "params": {**task["params"], **params}}
        try:
            prepare(t)
        except Refusal as e:
            _expect(e.code == code_, f"refused with {e.code}, expected {code_}: {e.message}")
            continue
        raise AssertionError(f"not refused: {code_}")
    few = df.iloc[:25]
    try:
        prepare({**task, "_frame": few})
        raise AssertionError("21 rows were not refused")
    except Refusal as e:
        _expect(e.code == "too_few_rows", e.code)
    # an infinite value: in a row the fit uses, refused; in a row it leaves out (the outcome missing), refused only
    # when the rows output must predict that row too, as Predict refuses it (XGBoost would stop on it)
    inf_used, inf_left = df.copy(), df.copy()
    inf_used.loc[11, "x"] = np.inf
    inf_left.loc[9, "x"] = -np.inf  # row 9's outcome is missing
    for frame, output, refused in ((inf_used, "results", True), (inf_left, "results", False), (inf_left, "rows", True)):
        try:
            prepare({**task, "_frame": frame, "output": output})
            _expect(not refused, f"an infinite value was not refused ({output})")
        except Refusal as e:
            _expect(refused and e.code == "infinite" and e.params == {"column": "x"} and "«x»" in e.message,
                    f"{output}: {e.code} {e.message}")
    # the labels once per distinct value give what label() of each value gives
    mixed = pd.Series([True, None, False, True, np.nan, "a", 2, 2.5, "a"], dtype=object)
    codes, levels = factor_labels(mixed)
    by_hand = [label(v) for v in mixed]
    _expect(levels == sort_labels(by_hand) and [levels[c] if c >= 0 else None for c in codes] == by_hand,
            f"factor_labels: {levels} {codes}")
    codes, unseen = category_codes(mixed, ["2", "a", "true"])
    _expect(codes.tolist() == [2, -1, -1, 2, -1, 1, 0, -1, 1] and unseen == 2, f"category_codes: {codes} {unseen}")
    # no predictor that varies: refused with a sentence (CatBoost stopped on it); one that varies, or a value with missing
    # ones beside it (both libraries split on a missing value), is enough
    flat = df.assign(x=2.0, c="a")
    for frame, refused in ((flat, True), (flat.assign(x=np.where(np.arange(n) % 3 == 0, np.nan, 2.0)), False),
                           (flat.assign(c=np.where(np.arange(n) % 2 == 0, "a", "b")), False)):
        try:
            prepare({**task, "_frame": frame})
            _expect(not refused, "predictors that never vary were not refused")
        except Refusal as e:
            _expect(refused and e.code == "constant_predictors", f"{e.code}: {e.message}")


@register("boosting: each diagnostic on both sides of its thresholds")
def _test_checks(code: Any) -> None:
    def v(c: dict[str, Any]) -> str:
        return c["verdict"]
    _expect([v(check_validation_size("regression", n, None)) for n in (100, 99, 30, 29)] == ["ok", "doubtful", "doubtful", "failed"],
            "validation_size, regression")
    _expect([v(check_validation_size("binary", 0, n)) for n in (30, 29, 10, 9)] == ["ok", "doubtful", "doubtful", "failed"],
            "validation_size, classes")
    _expect([v(check_beats_baseline(L, 1.0)) for L in (0.95, 0.96, 0.999, 1.0, 1.2)] == ["ok", "doubtful", "doubtful", "failed",
                                                                                         "failed"], "beats_baseline")
    _expect(check_beats_baseline(0.99, 1.0, True)["advice_id"] == "beats_baseline_balanced"
            and check_beats_baseline(0.9, 1.0, True)["h0"] == H0["beats_baseline_balanced"], "beats_baseline with balanced weights")
    up = lambda r: [1.0, 0.6, 0.5, 0.5 + 0.5 * r]  # noqa: E731 — a curve from 1 down to 0.5, back up by r of the gain
    _expect([v(check_overfitting(up(r), 3, 1.0)) for r in (0.0, 0.049, 0.051, 0.249, 0.251, 3.0)]
            == ["ok", "ok", "doubtful", "doubtful", "failed", "failed"], "overfitting")
    _expect(abs(check_overfitting(up(0.2), 3, 1.0)["value"] - 0.2) < 1e-12, "overfitting's share given back")
    _expect(v(check_overfitting([1.2, 0.5, 0.9], 1, 1.0)) == "ok", "overfitting reads the curve up to the tree kept")
    _expect(v(check_overfitting([1.2, 1.1, 1.3], 2, 1.0)) == "not_computed", "overfitting without a validation gain")
    trees = [v(check_trees_limit(True, True, False, 20, 0, 0, 0.1)), v(check_trees_limit(True, True, False, 19, 0, 0, 0.1)),
             v(check_trees_limit(True, False, False, 500, 0, 0)), v(check_trees_limit(True, True, True, 500, 0, 0)),
             v(check_trees_limit(False, False, False, 1000, 1.005, 1.0)), v(check_trees_limit(False, False, False, 1000, 1.0101, 1.0))]
    _expect(trees == ["ok", "doubtful", "doubtful", "doubtful", "ok", "doubtful"], f"trees_limit: {trees}")
    _expect(check_trees_limit(True, True, False, 19, 0, 0, 0.1)["advice"] == ADVICE["trees_few"]
            and check_trees_limit(True, True, False, 19, 0, 0, 0.1)["advice_id"] == "trees_few", "the advice of few trees")
    _expect(v(check_trees_limit(True, True, False, 3, 0, 0, 0.05)) == "ok", "few trees at the default rate: a weak pattern")
    _expect(check_trees_limit(True, True, False, 20, 0, 0)["advice_id"] is None, "an ok check has no advice")
    _expect([v(check_class_imbalance(np.array([r, 1.0, 2.0]))) for r in (3.99, 4.0, 19.99, 20.0)]
            == ["ok", "doubtful", "doubtful", "failed"], "class_imbalance")
    _expect([v(check_calibration(e, 0.03, False, 10)) for e in (0.05, 0.0501, 0.10, 0.1001)] == ["ok", "doubtful", "doubtful",
                                                                                                "failed"], "calibration")
    _expect([v(check_calibration(e, 0.16, False, 10)) for e in (0.16, 0.1601, 0.21, 0.2101)] == ["ok", "doubtful", "doubtful",
                                                                                                "failed"], "calibration, noisy")
    _expect(check_calibration(0.1, 0.16, False, 10)["threshold"] == 0.16 and v(check_calibration(0.3, 0.1, False, 9))
            == "not_computed", "calibration's threshold, and its rows")
    _expect(check_calibration(0.2, 0.03, True, 10)["advice"] == ADVICE["calibration_balanced"], "calibration with balanced weights")
    slopes = [v(check_calibration_slope(s, se, 10)) for s, se in ((1.0, 0.01), (0.7, 0.052), (0.7, 0.05), (2.3, 0.2),
                                                                   (2.5, 0.2), (0.4, 0.05), (math.nan, math.nan), (1.0, 0.1))]
    _expect(slopes == ["ok", "ok", "doubtful", "doubtful", "failed", "failed", "not_computed", "ok"], f"calibration_slope {slopes}")
    _expect(v(check_calibration_slope(1.0, 0.1, 9)) == "not_computed" and math.isnan(check_calibration_slope(0.5, 0.1, 10)
                                                                                         ["threshold"]), "the slope's rows")
    a = np.array([5.0, 4, 3, 2, 1])
    _expect([v(check_importance_stability(a, b, 50)) for b in (a, np.array([5.0, 4, 3, 1, 2]), np.array([3.0, 5, 4, 1, 2]),
                                                               np.array([1.0, 2, 3, 4, 5]))]
            == ["ok", "ok", "doubtful", "failed"], "importance_stability")
    _expect(v(check_importance_stability(a, a, 49)) == "not_computed" and v(check_importance_stability(a[:2], a[:2], 99))
            == "not_computed", "importance_stability below its rows or predictors")
    z = check_zero_importance(np.array([1.0, 0.0, 2.0, 0.0]), ["a", "b", "c", "d"])
    _expect([(c["level_2"], c["verdict"]) for c in z] == [("b", "doubtful"), ("d", "doubtful")], "zero_importance")
    _expect([(c["level_2"], c["verdict"]) for c in check_zero_importance(np.array([1.0]), ["a"])] == [(None, "ok")],
            "zero_importance, none")
    leak = [v(check_leakage("a", s, 3, m, False)) for s, m in ((0.79, 0.949), (0.9, 0.5), (0.59, 0.97), (0.6, 0.95), (0.5, 0.98),
                                                               (0.79, 0.99), (0.8, 0.98))]
    _expect(leak == ["ok", "ok", "ok", "doubtful", "doubtful", "doubtful", "failed"], f"leakage: {leak}")
    one = [check_leakage("a", 1.0, 1, m, False) for m in (0.97, 0.98)]
    _expect([c["verdict"] for c in one] == ["ok", "doubtful"] and one[0]["value"] == 0.97, f"leakage, one predictor: {one}")
    _expect(check_leakage("a", 0.9, 3, 0.99, True)["advice"].endswith("try validation = time")
            and check_leakage("a", 0.9, 3, 0.99, True)["advice_id"] == "leakage_time", "leakage's advice in time")
    for c in ADVICE:
        _expect(_check("x", "doubtful", advice=c, h0="x")["advice"] == ADVICE[c], c)


@register("boosting: the beeswarm's positions stay within ±0.4 and are the same on every run")
def _test_beeswarm(code: Any) -> None:
    x = np.round(_gen(5).normal(size=400), 2)
    a = beeswarm_offsets(x)
    _expect(np.all(np.abs(a) <= BEESWARM_HALF_WIDTH + 1e-12), f"an offset of {np.abs(a).max()}")
    _expect(np.array_equal(a, beeswarm_offsets(x.copy())), "two runs, two pictures")
    _expect(np.array_equal(beeswarm_offsets(np.array([1.0, 2.0, 3.0])), np.zeros(3)), "one point per bin sits on its line")
    _expect(np.allclose(sorted(beeswarm_offsets(np.zeros(5))), [-0.4 * 2 / 3, -0.4 / 3, 0, 0.4 / 3, 0.4 * 2 / 3]),
            f"five equal values: {sorted(beeswarm_offsets(np.zeros(5)))}")
    cols = colour_bins(np.array([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10, math.nan]), False)
    _expect(cols == ["low", "low", "mid_low", "mid_low", "mid", "mid", "mid_high", "mid_high", "high", "high", "missing"],
            f"colours: {cols}")
    _expect(colour_bins(np.array(["a", math.nan], dtype=object), True) == ["category", "missing"], "a category's colours")


@register("boosting: the metrics by hand against their definitions")
def _test_metrics(code: Any) -> None:
    gen = _gen(7)
    n = 300
    s = np.round(gen.uniform(size=n), 1)  # ties
    yb = (gen.uniform(size=n) < s).astype(np.int64)
    w = gen.integers(0, 4, size=n).astype(float)
    num = sum(w[i] * w[j] * ((s[i] > s[j]) + 0.5 * (s[i] == s[j])) for i in range(n) if yb[i] for j in range(n) if not yb[j])
    brute = num / (w[yb == 1].sum() * w[yb == 0].sum())
    _expect(abs(auc(s, yb, w) - brute) < 1e-12, f"the AUC in one sort {auc(s, yb, w)}, by pairs {brute}")
    fpr, tpr = roc(s, yb, w)
    area = float(np.sum(np.diff(fpr) * (tpr[1:] + tpr[:-1]) / 2))
    _expect(abs(area - brute) < 1e-12 and fpr[0] == tpr[0] == 0 and abs(fpr[-1] - 1) < 1e-15, "the ROC's area is the AUC")
    _expect(logloss(np.array([1.0, 0.0]), np.array([1.0, 0.0]), np.ones(2)) == -math.log(1 - EPS), "the log-loss's clipping")
    _expect(abs(rmse(np.array([1.0, 3]), np.array([0.0, 0]), np.array([1.0, 3])) - math.sqrt(7)) < 1e-15, "the weighted RMSE")
    _expect(abs(r_squared(np.array([1.0, 2, 3]), np.array([1.0, 2, 3]), np.ones(3)) - 1) < 1e-15, "R² of a perfect fit")
    p = np.linspace(0.05, 0.95, 20)
    _expect(abs(ece(p, p, np.ones(20))) < 1e-15, "a calibration error of 0")
    _expect(abs(ece(p, np.zeros(20), np.ones(20)) - p.mean()) < 1e-12, "the calibration error of all zeros")
    b = calibration_bins(p, p, np.ones(20))
    _expect(list(b["rows"]) == [2.0] * 10, f"ten bins of equal weight: {b['rows']}")
    z = gen.normal(size=20000)
    y2 = (gen.uniform(size=len(z)) < sigmoid(z)).astype(float)
    slope, se = calibration_slope(sigmoid(2 * z), y2, np.ones(len(z)))
    _expect(abs(slope - 0.5) < 0.05 and 0.005 < se < 0.02, f"the calibration slope of over-confident probabilities {slope} ± {se}")
    _expect(calibration_slope(sigmoid(2 * z), y2, np.full(len(z), 7.0)) == (slope, se), "the slope does not read the weights' scale")
    q = 1 / (1 + np.exp(-gen.normal(-1, 1.2, size=400)))
    noise = ece_noise(q, np.ones(400))
    draws = [ece(q, (gen.uniform(size=400) < q).astype(float), np.ones(400)) for _ in range(400)]
    _expect(0.06 < noise < 0.08 and 0.9 <= np.mean(np.array(draws) <= noise) <= 0.99,
            f"the noise of the calibration error {noise}: {np.mean(np.array(draws) <= noise)} of the draws under it")
    C = confusion(np.array([0, 0, 1, 1, 2]), np.array([0, 1, 1, 1, 0]), np.array([1.0, 2, 1, 1, 1]), 3)
    pc = per_class(C)
    _expect(C.tolist() == [[1, 2, 0], [0, 2, 0], [1, 0, 0]] and np.isnan(pc["precision"][2]) and pc["f1"][2] == 0,
            f"a confusion matrix: {C.tolist()}")
    _expect(abs(spearman(np.array([1.0, 2, 3, 4]), np.array([1.0, 3, 2, 4])) - 0.8) < 1e-15, "Spearman's ρ")
    _expect(np.allclose(softmax(np.array([[0.0, 0, math.log(2)]])), [[0.25, 0.25, 0.5]]) and sigmoid(np.array([-800.0]))[0] == 0,
            "softmax and sigmoid")


def _fake_fitted(P: Prepared) -> Fitted:
    """A model without a library: the margin is a sum of one term per predictor (numbers times a
    coefficient, a category's code times another), so its SHAP values are those terms centred."""
    p = len(P.predictors)
    k = P.k

    def terms(X: pd.DataFrame) -> np.ndarray:
        cols = []
        for j, nm in enumerate(P.names):
            if P.types[j] == "number":
                x = np.nan_to_num(X[nm].to_numpy(dtype=float), nan=0.0)
            else:
                x = X[nm].cat.codes.to_numpy().astype(float).clip(0)
            cols.append(x * (1.0 if j == 0 else 0.5 if j == 1 else 0.0))
        T = np.column_stack(cols)
        return T if k == 1 else np.stack([T * (c - (k - 1) / 2) for c in range(k)], axis=1)

    centre = terms(P.X).mean(axis=0)

    def raw_of(X: pd.DataFrame) -> np.ndarray:
        return terms(X).sum(axis=-1)

    def shap(rows: np.ndarray) -> tuple[np.ndarray, Any]:
        T = terms(P.X.iloc[rows])
        return T - centre, centre.sum(axis=-1)

    raw = raw_of(P.X)
    rounds = 30
    curves = {"loss": {"train": list(np.linspace(1, 0.2, rounds)), "validation": list(np.r_[np.linspace(1, 0.5, 20), [0.52] * 10])}}
    if P.task == "binary":
        curves["auc"] = {"train": list(np.linspace(0.6, 0.99, rounds)), "validation": list(np.linspace(0.6, 0.8, rounds))}
    return Fitted(library="xgboost", library_version="0", packages=["numpy"], objective="test", loss_metric_name="loss",
                  stop_metric_name="auc" if P.task == "binary" else "loss", importance_type="total_gain",
                  raw_train=raw[P.train], raw_validation=raw[P.valid], curves=curves, best_iteration=19, trees=20,
                  stopped_early=True, stopped_by_time=False, gain={0: 3.0, 1: 1.0} if p > 1 else {0: 1.0}, shap=shap,
                  predict_raw=raw_of, model_bytes=b'{"learner": {}}', model_name="xgboost.json", model_format="xgboost-json")


@register("boosting: finish writes every declared row, the rows output and the card, from any fitted model")
def _test_finish(code: Any) -> None:
    import json

    from tab_protocol import catalog
    from tab_results import check_declared

    gen = _gen(11)
    n = 240
    x = gen.normal(size=n)
    df = pd.DataFrame({"x": x, "c": pd.Series(gen.choice(["a", "b", "c"], size=n), dtype=object), "z": gen.normal(size=n),
                       "num": x * 2 + gen.normal(size=n)})
    df["two"] = pd.Series(np.where(df["num"] > 0.5, "yes", "no"), dtype=object)
    df["three"] = pd.Series(np.where(df["num"] > 1, "hi", np.where(df["num"] > -1, "mid", "lo")), dtype=object)
    df.loc[[3, 4], "x"] = np.nan
    schema = json.loads((code / "card.schema.json").read_text(encoding="utf-8"))
    for what, outcome in (("regression", "num"), ("binary", "two"), ("multiclass", "three")):
        task = {"model": "xgboost", "variant": what, "group": "boosting", "group_version": "selftest", "mode": "fit",
                "output": "rows", "_frame": df, "limits": dict(catalog()["limits"]),
                "params": {"task": what, "outcome": outcome, "predictors": ["x", "c", "z"], "event_value": "yes"}}
        P = prepare(task)
        out = finish(task, P, _fake_fitted(P))
        problems = check_declared(out["rows"], "xgboost", what)
        _expect(not problems, f"{what}: {problems[:3]}")
        keys = set(out["rows"].key)
        declared = variant_spec("xgboost", what)["sections"]["model"]["numbers"]
        lacking = [s for s in declared if f"model|||{s}" not in keys]
        _expect(not lacking, f"{what}: no row {lacking}")
        _expect("model|||note_validation_chose_trees" in keys and "importance|x||rank" in keys, f"{what}: notes and importance")
        _expect(any(k.startswith("curve|learning_loss|validation|y|") for k in keys), f"{what}: the learning curve")
        if what != "multiclass":
            _expect(any(k.startswith("point|shap_beeswarm|x|") for k in keys), f"{what}: the beeswarm")
        o = out["output"]
        _expect(len(o) == n and o["pred_part"].notna().sum() == P.n and list(o.columns[:6]) == list(df.columns), f"{what}: rows")
        c = json.loads(json.dumps(out["card"]))
        _expect(all(k in c for k in schema["required"]) and all(k in c["boosting"] for k in schema["properties"]["boosting"]["required"]),
                f"{what}: the card")
        again = predict({"_frame": df, "params": {}}, c, _fake_fitted(P).predict_raw)["output"]
        col = {"regression": "pred_value", "binary": "pred_prob", "multiclass": "pred_class"}[what]
        _expect(list(again["prediction"]) == list(o[col]), f"{what}: Predict from the card differs from the rows output")
