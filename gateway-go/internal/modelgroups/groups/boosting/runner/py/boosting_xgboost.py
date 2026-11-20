"""XGBoost (lot X2 of docs/specs/statistical-models.md, «Group `boosting` — Python»):
fit_xgboost(task) and predict_xgboost(task, card).

The fit: xgb.train on a QuantileDMatrix (256 bins) of the positional frame boosting_shared
prepared, native categories (a pandas category whose categories are the card's levels, so that
XGBoost's own re-coding sees the same categories at fit and at predict, and a level never seen
is missing before XGBoost reads it), the validation part watched by early stopping (XGBoost's
EarlyStopping on the stop metric, then the booster sliced to the best round: the saved model
needs no iteration_range), a callback that stops at 60 % of the step's time. The contributions
are XGBoost's TreeSHAP (pred_contribs, exact on trees, which handles the categorical splits); the
gain is get_score(total_gain); the model file is the booster's own JSON (xgboost.json), data the
library parses, never code.

Predict: the model file's sha256 checked against the card's, the file copied to /tmp and read by
xgboost.Booster(model_file = …), the card's frame, the margins, boosting_shared.predict.

The module's own tests (registered in the self-test): a tree walker in numpy over xgboost.json
against XGBoost's margins, the gain from the trees' loss_changes, SHAP's additivity, the model
file read back bit for bit, determinism, the time budget, a level unseen at Predict.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import time
from typing import Any

import numpy as np
import pandas as pd

import boosting_shared as B
from tab_protocol import ENV_MODEL_FILE_IN, Refusal, read_bytes

PACKAGES = ["xgboost-cpu", "numpy", "pandas"]
OBJECTIVES = {"regression": "reg:squarederror", "binary": "binary:logistic", "multiclass": "multi:softprob"}
LOSS_METRICS = {"regression": "rmse", "binary": "logloss", "multiclass": "mlogloss"}
MAX_BIN = 256
MODEL_NAME = "xgboost.json"
MODEL_FORMAT = "xgboost-json"


def _xgb():
    import xgboost

    return xgboost


def stop_metric(what: str, choice: str) -> str:
    """XGBoost's name of the metric early stopping watches."""
    if choice == "mae":
        return "mae"
    if choice == "auc":
        return "auc"
    if choice == "error":
        return "merror" if what == "multiclass" else "error"
    return LOSS_METRICS[what]


def booster_params(P: B.Prepared) -> dict[str, Any]:
    """The parameters of xgb.train, every one written (the node's, then the spec's fixed ones)."""
    p = P.params
    loss = LOSS_METRICS[P.task]
    stop = stop_metric(P.task, p.get("stop_metric", "loss"))
    params = {"objective": OBJECTIVES[P.task], "eval_metric": [loss] + ([stop] if stop != loss else []),
              "tree_method": "hist", "max_bin": MAX_BIN, "eta": float(p["learning_rate"]), "max_depth": int(p["max_depth"]),
              "subsample": float(p["subsample"]), "colsample_bytree": float(p["colsample_bytree"]),
              "min_child_weight": float(p["min_child_weight"]), "lambda": float(p["reg_lambda"]), "alpha": float(p["reg_alpha"]),
              "gamma": float(p["gamma"]), "max_cat_to_onehot": 4, "max_cat_threshold": 64, "seed": int(p["seed"]),
              "nthread": int(P.threads), "validate_parameters": True}
    if P.task == "multiclass":
        params["num_class"] = P.k
    return params


def _dmatrix(X: pd.DataFrame, threads: int):
    return _xgb().DMatrix(X, enable_categorical=True, nthread=threads)


def margins(booster: Any, X: pd.DataFrame, threads: int) -> np.ndarray:
    """The raw scores (n, or n × k for a multiclass model) as float64."""
    return np.asarray(booster.predict(_dmatrix(X, threads), output_margin=True), dtype=np.float64)


def best_round(values: list[float], maximize: bool) -> int:
    """The first round of the best value: XGBoost's EarlyStopping keeps a new best only when it is
    strictly better."""
    best, at = None, 0
    for i, v in enumerate(values):
        if best is None or (v > best if maximize else v < best):
            best, at = v, i
    return at


def fit_xgboost(task: dict[str, Any]) -> dict[str, Any]:
    xgb = _xgb()
    P = B.prepare(task)
    p = P.params
    params = booster_params(P)
    stop = stop_metric(P.task, p.get("stop_metric", "loss"))
    maximize = stop == "auc"
    tr_rows = P.train_order
    weighted = P.weighted or p.get("class_weight") == "balanced"
    dtrain = xgb.QuantileDMatrix(P.X.iloc[tr_rows], label=P.y[tr_rows], weight=P.fit_w[tr_rows] if weighted else None,
                                 enable_categorical=True, max_bin=MAX_BIN, nthread=P.threads)
    va = P.valid
    dvalid = xgb.QuantileDMatrix(P.X.iloc[va], label=P.y[va], weight=P.w[va] if P.weighted else None, ref=dtrain,
                                 enable_categorical=True, max_bin=MAX_BIN, nthread=P.threads)

    class TimeBudget(xgb.callback.TrainingCallback):
        def __init__(self, until: float | None):
            super().__init__()
            self.until = until
            self.stopped = False

        def after_iteration(self, model: Any, epoch: int, evals_log: Any) -> bool:
            if self.until is not None and time.monotonic() > self.until:
                self.stopped = True
            return self.stopped

    budget = TimeBudget(B.deadline(task))
    callbacks: list[Any] = []
    early = bool(p.get("early_stopping", True))
    if early:
        callbacks.append(xgb.callback.EarlyStopping(rounds=int(p["early_stopping_rounds"]), metric_name=stop,
                                                    data_name="validation", maximize=maximize, save_best=False))
    callbacks.append(budget)
    log: dict[str, dict[str, list[float]]] = {}
    max_trees = int(p["max_trees"])
    booster = xgb.train(params, dtrain, num_boost_round=max_trees, evals=[(dtrain, "train"), (dvalid, "validation")],
                        evals_result=log, verbose_eval=False, callbacks=callbacks)
    rounds = booster.num_boosted_rounds()
    curves = {m: {"train": [float(v) for v in log["train"][m]], "validation": [float(v) for v in log["validation"][m]]}
              for m in log.get("validation", {})}
    if early:
        best = best_round(curves[stop]["validation"], maximize)
        kept = booster[: best + 1]
    else:
        best = rounds - 1
        kept = booster
    stopped_by_time = budget.stopped
    stopped_early = early and not stopped_by_time and rounds < max_trees
    threads = P.threads
    model_bytes = bytes(kept.save_raw("json"))
    p_count = len(P.predictors)
    gain = {int(k[1:]) - 1: float(v) for k, v in kept.get_score(importance_type="total_gain").items()}

    def shap(rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        contribs = np.asarray(kept.predict(_dmatrix(P.X.iloc[rows], threads), pred_contribs=True), dtype=np.float64)
        return contribs[..., :p_count], contribs[..., p_count]

    fitted = B.Fitted(
        library="xgboost", library_version=xgb.__version__, packages=PACKAGES, objective=OBJECTIVES[P.task],
        loss_metric_name=LOSS_METRICS[P.task], stop_metric_name=stop, importance_type="total_gain",
        raw_train=margins(kept, P.X.iloc[P.train], threads), raw_validation=margins(kept, P.X.iloc[va], threads),
        curves=curves, best_iteration=int(best), trees=int(best + 1), stopped_early=bool(stopped_early),
        stopped_by_time=bool(stopped_by_time), gain=gain, shap=shap, predict_raw=lambda X: margins(kept, X, threads),
        model_bytes=model_bytes, model_name=MODEL_NAME, model_format=MODEL_FORMAT, lower_is_better={"auc": False})
    return B.finish(task, P, fitted)


def load_model_file(card: dict[str, Any], data: bytes):
    """A Booster from the model file's bytes, once their sha256 matched the card's: written to /tmp
    under the name the library reads by its extension, then read by XGBoost's own reader."""
    entry = (card.get("boosting") or {}).get("model_file") or {}
    if entry.get("name") != MODEL_NAME:
        raise Refusal("model_file", "the model card names no XGBoost model file: it was not made by an XGBoost node")
    if hashlib.sha256(data).hexdigest() != entry.get("sha256") or len(data) != entry.get("bytes"):
        raise Refusal("model_file_mismatch", f"the model file of {card.get('model') or 'the model'} does not match the "
                                             "fingerprint its card keeps: it is not applied")
    with tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None) as tmp:
        path = os.path.join(tmp, MODEL_NAME)
        with open(path, "wb") as f:
            f.write(data)
        return _xgb().Booster(model_file=path)


def predict_xgboost(task: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    where = os.environ.get(ENV_MODEL_FILE_IN, "")
    if not where:
        raise Refusal("no_model_file", f"{card.get('model') or 'the model'} has no model file among its files")
    booster = load_model_file(card, read_bytes(where))
    threads = B.run_threads(task)
    return B.predict(task, card, lambda X: margins(booster, X, threads))


# ── the module's own tests ──────────────────────────────────────────────────────
from tab_selftest import register  # noqa: E402


def walk_margins(model: dict[str, Any], X: pd.DataFrame) -> np.ndarray:
    """The margins of xgboost.json by a tree walker in numpy: at a numerical split the value goes
    left when value < condition (in float32); at a categorical split the categories listed at the
    node go right; a missing value follows default_left; a leaf's value is its split_condition.
    The trees' sums (float64) plus base_score, as a margin (logit for binary:logistic)."""
    learner = model["learner"]
    gb = learner["gradient_booster"]["model"]
    trees = gb["trees"]
    info = gb["tree_info"]
    k = max(1, int(learner["learner_model_param"].get("num_class", "0") or 0))
    base = np.asarray(json.loads(learner["learner_model_param"]["base_score"]) if learner["learner_model_param"]["base_score"]
                      .startswith("[") else [float(learner["learner_model_param"]["base_score"])], dtype=float)
    objective = learner["objective"]["name"]
    if objective == "binary:logistic":
        base = np.log(base / (1 - base))
    cols = []
    for c in X.columns:
        x = X[c]
        cols.append(x.cat.codes.to_numpy().astype(float) if isinstance(x.dtype, pd.CategoricalDtype) else x.to_numpy(dtype=float))
    for i in range(len(cols)):
        if isinstance(X[X.columns[i]].dtype, pd.CategoricalDtype):
            cols[i][cols[i] < 0] = np.nan
    V = np.column_stack(cols)
    n = len(X)
    out = np.zeros((n, k)) + (base if len(base) == k else np.full(k, base[0]))
    for t, cls in zip(trees, info):
        left, right = np.asarray(t["left_children"]), np.asarray(t["right_children"])
        feat, cond = np.asarray(t["split_indices"]), np.asarray(t["split_conditions"], dtype=np.float32)
        dleft, stype = np.asarray(t["default_left"]).astype(bool), np.asarray(t["split_type"])
        cats = {node: set(t["categories"][s:s + z]) for node, s, z in zip(t["categories_nodes"], t["categories_segments"],
                                                                          t["categories_sizes"])}
        node = np.zeros(n, dtype=np.int64)
        while True:
            inner = left[node] != -1
            if not inner.any():
                break
            for r in np.flatnonzero(inner):
                j = node[r]
                v = V[r, feat[j]]
                if math.isnan(v):
                    go_left = dleft[j]
                elif stype[j] == 1:
                    go_left = int(v) not in cats.get(int(j), set())
                else:
                    go_left = np.float32(v) < cond[j]
                node[r] = left[j] if go_left else right[j]
        out[:, cls] += cond[node].astype(np.float64)
    return out[:, 0] if k == 1 else out


def gain_from_trees(model: dict[str, Any], p: int) -> np.ndarray:
    """total_gain by hand: the sum of the loss_changes of the splits on each predictor."""
    g = np.zeros(p)
    for t in model["learner"]["gradient_booster"]["model"]["trees"]:
        for j, (lc, f) in enumerate(zip(t["loss_changes"], t["split_indices"])):
            if t["left_children"][j] != -1:
                g[f] += lc
    return g


def synthetic(n: int, what: str, seed: int = 1, unseen: bool = False) -> pd.DataFrame:
    """A small table of the tests: two numbers (one with missing values), a category of five
    levels (with missing values), a boolean, and an outcome of each kind."""
    gen = np.random.Generator(np.random.PCG64(seed))
    a = gen.normal(size=n)
    b = gen.uniform(size=n)
    c = gen.choice(["n", "s", "e", "w", "c"], size=n)
    d = gen.uniform(size=n) < 0.4
    effect = {"n": 1.0, "s": -1.0, "e": 0.5, "w": 0.0, "c": 2.0}
    score = 1.5 * a + np.array([effect[x] for x in c]) + 0.8 * d - b + gen.normal(scale=0.7, size=n)
    df = pd.DataFrame({"a": a, "b": b, "c": pd.Series(c, dtype=object), "d": pd.Series(d.tolist(), dtype=object)})
    df.loc[gen.uniform(size=n) < 0.08, "a"] = np.nan
    df.loc[gen.uniform(size=n) < 0.05, "c"] = None
    if what == "regression":
        df["y"] = score
    elif what == "binary":
        df["y"] = pd.Series(np.where(score > np.quantile(score, 0.7), "yes", "no"), dtype=object)
    else:
        df["y"] = pd.Series(np.where(score > 1, "gold", np.where(score > -0.5, "silver", "bronze")), dtype=object)
    if unseen:
        df.loc[:9, "c"] = "z"
    return df


def _task(what: str, df: pd.DataFrame, **params: Any) -> dict[str, Any]:
    from tab_protocol import catalog

    return {"model": "xgboost", "variant": what, "group": "boosting", "group_version": "selftest", "mode": "fit",
            "output": "rows", "_frame": df, "limits": dict(catalog()["limits"]),
            "params": {"task": what, "outcome": "y", "predictors": ["a", "b", "c", "d"], "event_value": "yes",
                       "max_trees": 60, "max_depth": 3, "learning_rate": 0.2, "early_stopping_rounds": 10, **params}}


def _expect(ok: Any, message: str) -> None:
    if not ok:
        raise AssertionError(message)


@register("xgboost: a tree walker in numpy over xgboost.json gives XGBoost's margins, categories and missing values included")
def _test_walker(code: Any) -> None:
    for what in ("regression", "binary", "multiclass"):
        df = synthetic(300, what)
        task = _task(what, df)
        P = B.prepare(task)
        out = fit_xgboost(task)
        model = json.loads(out["model_file"]["data"])
        booster = load_model_file(out["card"], out["model_file"]["data"])
        X, _ = B.card_frame(out["card"], df)
        got = margins(booster, X, 2)
        walked = walk_margins(model, X)
        gap = np.abs(walked - got)
        bound = 1e-6 * np.maximum(1, np.abs(got))
        _expect(np.all(gap <= bound), f"{what}: the walker differs by {gap.max():.3g}")
        split_types = {s for t in model["learner"]["gradient_booster"]["model"]["trees"] for s in t["split_type"]}
        _expect(1 in split_types, f"{what}: no categorical split to walk")
        _expect(P.n == 300, "rows")


@register("xgboost: the gain is the sum of the trees' loss_changes, SHAP adds up to the margin")
def _test_gain_shap(code: Any) -> None:
    for what in ("regression", "binary", "multiclass"):
        df = synthetic(300, what, seed=2)
        task = _task(what, df)
        out = fit_xgboost(task)
        model = json.loads(out["model_file"]["data"])
        rows = out["rows"]
        at = {k: i for i, k in enumerate(rows.key)}
        g = gain_from_trees(model, 4)
        for j, name in enumerate(["a", "b", "c", "d"]):
            got = rows.value[at[f"importance|{name}||gain"]]
            _expect(abs(got - g[j]) <= 1e-6 * max(1.0, abs(g[j])), f"{what}: the gain of {name} {got}, by the trees {g[j]}")
        booster = load_model_file(out["card"], out["model_file"]["data"])
        X, _ = B.card_frame(out["card"], df)
        contribs = np.asarray(booster.predict(_dmatrix(X, 2), pred_contribs=True), dtype=np.float64)
        m = margins(booster, X, 2)
        total = contribs.sum(axis=-1)
        bound = 5 * 2.0 ** -23 * np.maximum(1, np.abs(contribs).sum(axis=-1))
        _expect(np.all(np.abs(total - m) <= bound), f"{what}: Σφ + φ0 − margin up to {np.abs(total - m).max():.3g}")


@register("xgboost: the model file read back predicts bit for bit; Predict checks its fingerprint")
def _test_model_file(code: Any) -> None:
    import warnings

    for what in ("regression", "binary", "multiclass"):
        df = synthetic(250, what, seed=3)
        out = fit_xgboost(_task(what, df))
        o = out["output"]
        old = os.environ.get(ENV_MODEL_FILE_IN)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "model_file")
                with open(path, "wb") as f:
                    f.write(out["model_file"]["data"])
                os.environ[ENV_MODEL_FILE_IN] = path
                card = json.loads(json.dumps(out["card"]))
                new = synthetic(250, what, seed=3, unseen=True)
                with warnings.catch_warnings(record=True) as said:
                    warnings.simplefilter("always")
                    pred = predict_xgboost({"_frame": df, "params": {}, "group_version": "selftest"}, card)["output"]
                    unseen = predict_xgboost({"_frame": new, "params": {}, "group_version": "selftest"}, card)["output"]
                col = {"regression": "pred_value", "binary": "pred_prob", "multiclass": "pred_prob"}[what]
                mine = "prediction" if what != "multiclass" else "prediction_probability"
                _expect(np.array_equal(pred[mine].to_numpy(dtype=float), o[col].to_numpy(dtype=float)),
                        f"{what}: Predict from the file differs from the fit")
                _expect(any("did not see" in str(w.message) and "«c»" in str(w.message) for w in said), f"{what}: no warning")
                missing = new.copy()
                missing.loc[:9, "c"] = None
                again = predict_xgboost({"_frame": missing, "params": {}, "group_version": "selftest"}, card)["output"]
                _expect(np.array_equal(unseen[mine].to_numpy(dtype=float)[:10], again[mine].to_numpy(dtype=float)[:10]),
                        f"{what}: an unseen level does not take the missing value's path")
                bad = dict(card, boosting=dict(card["boosting"], model_file=dict(card["boosting"]["model_file"], sha256="0" * 64)))
                try:
                    predict_xgboost({"_frame": df, "params": {}}, bad)
                    raise AssertionError("a wrong fingerprint was applied")
                except Refusal as e:
                    _expect(e.code == "model_file_mismatch", e.code)
                infinite = df.copy()
                infinite.loc[3, "b"] = np.inf  # XGBoost would stop on it: refused with the column, as CatBoost's Predict
                try:
                    predict_xgboost({"_frame": infinite, "params": {}}, card)
                    raise AssertionError("an infinite value was predicted")
                except Refusal as e:
                    _expect(e.code == "infinite" and e.params == {"column": "b"}, f"{e.code}: {e.message}")
        finally:
            if old is None:
                os.environ.pop(ENV_MODEL_FILE_IN, None)
            else:
                os.environ[ENV_MODEL_FILE_IN] = old


@register("xgboost: two fits of one seed and threads are the same bit for bit; the time budget stops the boosting")
def _test_determinism(code: Any) -> None:
    for what in ("regression", "binary", "multiclass"):
        df = synthetic(300, what, seed=4)
        one, two = fit_xgboost(_task(what, df)), fit_xgboost(_task(what, df))
        r1, r2 = one["rows"], two["rows"]
        _expect(r1.key == r2.key and r1.text == r2.text, f"{what}: the rows differ")
        v1, v2 = np.asarray(r1.value), np.asarray(r2.value)
        _expect(np.array_equal(v1, v2, equal_nan=True) and np.array_equal(np.asarray(r1.x0), np.asarray(r2.x0), equal_nan=True),
                f"{what}: the numbers differ")
        _expect(one["model_file"]["data"] == two["model_file"]["data"], f"{what}: the model files differ")
    task = _task("regression", synthetic(300, "regression", seed=5), max_trees=5000, learning_rate=0.001,
                 early_stopping_rounds=1000)
    task["limits"]["time_budget_seconds"] = 0.5
    rows = fit_xgboost(task)["rows"]
    at = {k: i for i, k in enumerate(rows.key)}
    _expect(rows.value[at["model|||stopped_by_time"]] == 1 and "model|||note_time_budget" in at, "the time budget")
    _expect(rows.text[at["assumption|trees_limit||verdict"]] == "doubtful", "trees_limit after the time budget")
    _expect(rows.value[at["model|||trees"]] < 5000, "the time budget kept every tree")


@register("xgboost: the time budget counts from the runner's start; the SHAP sample shrinks to the time left")
def _test_time_from_start(code: Any) -> None:
    df = synthetic(300, "regression", seed=5)
    # the step's time spent before the fit (reading, preparing): 60 % of it is already gone, one tree is grown
    late = _task("regression", df, max_trees=200, early_stopping=False)
    late["limits"]["time_budget_seconds"] = 100.0
    late["_started"] = time.monotonic() - 70.0
    rows = fit_xgboost(late)["rows"]
    at = {k: i for i, k in enumerate(rows.key)}
    _expect(rows.value[at["model|||trees"]] == 1 and rows.value[at["model|||stopped_by_time"]] == 1,
            f"the budget did not count from the start: {rows.value[at['model|||trees']]} trees")
    # 85 % of it gone too: the SHAP values of the pilot's rows only, the note says the sample
    later = dict(late, _started=time.monotonic() - 90.0)
    rows = fit_xgboost(later)["rows"]
    at = {k: i for i, k in enumerate(rows.key)}
    _expect(rows.value[at["model|||shap_rows"]] == B.SHAP_PILOT_ROWS and "model|||note_shap_sampled" in at,
            f"the SHAP sample on no time left: {rows.value[at['model|||shap_rows']]} rows")
    # time enough: every row, as without a budget (the numbers do not follow the machine's speed)
    ample = dict(late, _started=time.monotonic())
    ample["limits"] = dict(late["limits"], time_budget_seconds=1e6)
    rows = fit_xgboost(ample)["rows"]
    at = {k: i for i, k in enumerate(rows.key)}
    _expect(rows.value[at["model|||shap_rows"]] == 300 and rows.value[at["model|||trees"]] == 200, "time enough")
