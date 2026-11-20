"""CatBoost (lot X3 of docs/specs/statistical-models.md, «Group `boosting` — Python»):
fit_catboost(task) and predict_catboost(task, card).

The fit: CatBoostRegressor or CatBoostClassifier on the positional frame boosting_shared
prepared, with native categories (`cat_features`: a category's value is its label, a missing one
the sentinel «␟missing», which a column may not hold), numbers with nan_mode = Min, the training
rows in time order with has_time = true for a validation part in time (the ordered target
statistics then never read later rows), early stopping on the validation part with
use_best_model (the model keeps the trees up to the best round), the boosting started from the
best constant (boost_from_average for RMSE and Logloss: the mean, the logit of the event's share;
for MultiClass, which CatBoost does not start so, the log of each class's share in the training
part as the pools' baseline, then the model's bias: a model file predicts from there without a
baseline), a callback that stops at 60 % of
the step's time, allow_writing_files = False and train_dir under /tmp (the code folder and the
root are read-only: CatBoost writes nothing). The importance is PredictionValuesChange (it sums
to 100); the SHAP values are CatBoost's ShapValues (exact TreeSHAP; n × (p + 1), or n × k × (p + 1)
for a multiclass model, the last column the expected value). The model file is CatBoost's binary
format, catboost.cbm, data the library parses, never code; its random model_guid and its
train_finish_time are taken out of the metadata before it is written, so that the same fit writes
the same bytes.

Predict: the model file's sha256 checked against the card's, the file copied to /tmp under the
card's name and read by CatBoost().load_model(path, format = "cbm"), the card's frame (a level the
fit did not see takes the path of a missing value, the sentinel), the raw scores,
boosting_shared.predict.

The module's own tests (registered in the self-test): SHAP's additivity, the model file read back
bit for bit, determinism (rows, predictions, SHAP and the file's bytes), the time budget, the
sentinel, has_time, the stop metrics and the training AUC, nothing written outside /tmp.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from typing import Any

import numpy as np
import pandas as pd

import boosting_shared as B
from tab_protocol import ENV_MODEL_FILE_IN, Refusal, read_bytes

PACKAGES = ["catboost", "numpy", "pandas"]
LOSS = {"regression": "RMSE", "binary": "Logloss", "multiclass": "MultiClass"}
MODEL_NAME = "catboost.cbm"
MODEL_FORMAT = "catboost-cbm"
SENTINEL = "␟missing"  # U+241F then «missing»: a categorical value must be a string
TRAIN_DIR = "/tmp/catboost"
NOT_IN_FILE = ("model_guid", "train_finish_time")  # metadata that changes on every fit

# the stop metric: (CatBoost's eval_metric, the key of its curves in get_evals_result)
STOP = {("regression", "loss"): ("RMSE", "RMSE"), ("regression", "mae"): ("MAE", "MAE"),
        ("binary", "loss"): ("Logloss", "Logloss"), ("binary", "auc"): ("AUC:hints=skip_train~false", "AUC"),
        ("binary", "error"): ("ZeroOneLoss", "ZeroOneLoss"), ("multiclass", "loss"): ("MultiClass", "MultiClass"),
        ("multiclass", "error"): ("ZeroOneLoss", "ZeroOneLoss")}
STOP_REFUSED = {
    "mae": ("mae_needs_regression", "the mean absolute error is a regression's metric: use stop_metric = loss, auc or error"),
    "auc": ("auc_needs_binary", "the AUC is a binary task's metric: use stop_metric = loss (or mae for a regression, error for "
                                "a multiclass task)"),
    "error": ("error_needs_classes", "the error rate is a classification's metric: use stop_metric = loss or mae"),
}


def _cb():
    import catboost

    return catboost


def stop_metric(what: str, choice: str) -> tuple[str, str]:
    """CatBoost's eval_metric and the curves' key of the metric early stopping watches."""
    got = STOP.get((what, choice or "loss"))
    if got is None:
        code, message = STOP_REFUSED.get(choice, ("stop_metric", f"unknown stop_metric «{choice}»"))
        raise Refusal(code, message)
    return got


def cat_frame(X: pd.DataFrame) -> pd.DataFrame:
    """The frame CatBoost reads, from a positional frame: numbers as float64 (NaN missing), a
    category as its labels (object), the sentinel where it is missing or a level the card does
    not hold."""
    out: dict[str, Any] = {}
    for name in X.columns:
        x = X[name]
        if isinstance(x.dtype, pd.CategoricalDtype):
            levels = np.asarray(list(x.cat.categories) + [SENTINEL], dtype=object)
            codes = x.cat.codes.to_numpy()
            out[name] = levels[np.where(codes < 0, len(levels) - 1, codes)]
        else:
            out[name] = x.to_numpy(dtype=float)
    return pd.DataFrame(out, index=pd.RangeIndex(len(X)))


def _cat_features(X: pd.DataFrame) -> list[int]:
    return [j for j, c in enumerate(X.columns) if isinstance(X[c].dtype, pd.CategoricalDtype)]


def pool(X: pd.DataFrame, label: Any = None, weight: Any = None, baseline: np.ndarray | None = None):
    """A Pool of a positional frame (its categories as cat_features); `baseline`, one raw score per
    class, the same for every row."""
    rows = None if baseline is None else np.tile(np.asarray(baseline, dtype=np.float64), (len(X), 1))
    return _cb().Pool(cat_frame(X), label=label, weight=weight, cat_features=_cat_features(X), baseline=rows)


def class_start(P: B.Prepared) -> np.ndarray | None:
    """A multiclass fit's start: the log of each class's share of the fit's weights in the training
    part (the best constant of the multiclass loss; every class has training rows). None for the
    other tasks, which boost_from_average starts."""
    if P.task != "multiclass":
        return None
    tr = ~P.validation
    per = np.array([float(P.fit_w[tr & (P.y == c)].sum()) for c in range(P.k)])
    return np.log(per / per.sum())


def raw_scores(model: Any, X: pd.DataFrame, threads: int) -> np.ndarray:
    """The raw scores (RawFormulaVal: n, or n × k for a multiclass model) as float64."""
    return np.asarray(model.predict(pool(X), prediction_type="RawFormulaVal", thread_count=threads), dtype=np.float64)


def model_params(P: B.Prepared) -> dict[str, Any]:
    """The parameters of the CatBoost model, every one written (the node's, then the spec's fixed ones)."""
    p = P.params
    loss = LOSS[P.task]
    eval_metric, key = stop_metric(P.task, p.get("stop_metric", "loss"))
    early = bool(p.get("early_stopping", True))
    params = {"iterations": int(p["max_trees"]), "learning_rate": float(p["learning_rate"]), "depth": int(p["max_depth"]),
              "l2_leaf_reg": float(p["l2_leaf_reg"]), "rsm": float(p["rsm"]), "random_strength": float(p["random_strength"]),
              "bootstrap_type": "Bernoulli", "subsample": float(p["subsample"]), "loss_function": loss,
              "eval_metric": eval_metric, "nan_mode": "Min", "random_seed": int(p["seed"]), "thread_count": int(P.threads),
              "use_best_model": early, "has_time": P.split == "time", "allow_writing_files": False, "train_dir": TRAIN_DIR,
              "verbose": False}
    if P.task != "multiclass":  # CatBoost refuses it for MultiClass: class_start
        params["boost_from_average"] = True
    if key != loss:
        params["custom_metric"] = [loss]
    if early:
        params["early_stopping_rounds"] = int(p["early_stopping_rounds"])
    return params


def tree_bytes(depth: int, k: int) -> int:
    """At most the bytes one tree adds to catboost.cbm: a tree of CatBoost is symmetric, 2^depth
    leaves of k values (one per class of a multiclass model, else 1) and a weight, as doubles, its
    splits and a little more; the self-test checks it against the files of fitted models."""
    return (2 ** depth) * (k + 1) * 8 + 64 * depth + 512


def size_trees(P: B.Prepared, task: dict[str, Any]) -> int:
    """The most trees whose file stays within MODEL_SIZE_SHARE of max_model_file_bytes (the rest
    for the borders and the categories' statistics): a depth of 10 with 20 classes adds about
    170 KB a tree, so 64 MiB hold about 380 of them."""
    most = B.MODEL_SIZE_SHARE * B.limit(task, "max_model_file_bytes")
    return max(1, int(most // tree_bytes(int(P.params["max_depth"]), P.k)))


def model_bytes(model: Any) -> bytes:
    """catboost.cbm's bytes: written under /tmp and read back, without the metadata that changes
    on every fit."""
    meta = model.get_metadata()
    for k in NOT_IN_FILE:
        if k in meta:
            del meta[k]
    with tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None) as tmp:
        path = os.path.join(tmp, MODEL_NAME)
        model.save_model(path, format="cbm")
        with open(path, "rb") as f:
            return f.read()


class TimeBudget:
    """CatBoost's callback: after_iteration returns False (stop) once the deadline has passed."""

    def __init__(self, until: float | None):
        self.until = until
        self.stopped = False

    def after_iteration(self, info: Any) -> bool:
        if self.until is not None and time.monotonic() > self.until:
            self.stopped = True
        return not self.stopped


def fit_catboost(task: dict[str, Any]) -> dict[str, Any]:
    cb = _cb()
    P = B.prepare(task)
    p = P.params
    for name, lv in zip(P.predictors, P.levels):
        if lv and SENTINEL in lv:
            raise Refusal("missing_sentinel", f"the column «{name}» holds the value «{SENTINEL}», which the CatBoost node keeps "
                                              "for a missing value: replace it before the node", {"column": name})
    params = model_params(P)
    by_size = size_trees(P, task)
    capped = by_size < params["iterations"]
    params["iterations"] = min(params["iterations"], by_size)
    loss = LOSS[P.task]
    _, stop = stop_metric(P.task, p.get("stop_metric", "loss"))
    early = bool(params["use_best_model"])
    weighted = P.weighted or p.get("class_weight") == "balanced"
    tr_rows, va = P.train_order, P.valid
    start = class_start(P)
    train_pool = pool(P.X.iloc[tr_rows], P.y[tr_rows], P.fit_w[tr_rows] if weighted else None, start)
    # the validation part weighted as the fit (class_weight = balanced too): early stopping and the diagnostics read one loss
    valid_pool = pool(P.X.iloc[va], P.y[va], P.fit_w[va] if weighted else None, start)
    model = (cb.CatBoostRegressor if P.task == "regression" else cb.CatBoostClassifier)(**params)
    budget = TimeBudget(B.deadline(task))
    model.fit(train_pool, eval_set=valid_pool, callbacks=[budget] if budget.until is not None else None)
    if start is not None:  # the pools' baseline becomes the model's bias: the file predicts from the start too
        model.set_scale_and_bias(1.0, [float(b) for b in start])

    log = model.get_evals_result()
    curves = {m: {"train": [float(v) for v in log.get("learn", {}).get(m, [])], "validation": [float(v) for v in vals]}
              for m, vals in log.get("validation", {}).items() if m in (loss, stop)}
    rounds = len(curves[stop]["validation"])
    trees = int(model.tree_count_)
    best = int(model.get_best_iteration()) if early else trees - 1
    stopped_by_time = budget.stopped
    stopped_by_size = capped and not stopped_by_time and rounds >= int(params["iterations"])
    stopped_early = early and not stopped_by_time and not stopped_by_size and rounds < int(params["iterations"])
    threads = P.threads
    p_count = len(P.predictors)
    pvc = np.asarray(model.get_feature_importance(type="PredictionValuesChange", thread_count=threads), dtype=np.float64)
    gain = {j: float(v) for j, v in enumerate(pvc)}

    def shap(rows: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        sv = np.asarray(model.get_feature_importance(data=pool(P.X.iloc[rows]), type="ShapValues", thread_count=threads),
                        dtype=np.float64)
        return sv[..., :p_count], sv[..., p_count]

    fitted = B.Fitted(
        library="catboost", library_version=cb.__version__, packages=PACKAGES, objective=loss, loss_metric_name=loss,
        stop_metric_name=stop, importance_type="PredictionValuesChange",
        raw_train=raw_scores(model, P.X.iloc[P.train], threads), raw_validation=raw_scores(model, P.X.iloc[va], threads),
        curves=curves, best_iteration=best, trees=trees, stopped_early=bool(stopped_early), stopped_by_time=bool(stopped_by_time),
        gain=gain, shap=shap, predict_raw=lambda X: raw_scores(model, X, threads), model_bytes=model_bytes(model),
        model_name=MODEL_NAME, model_format=MODEL_FORMAT,
        missing_level=SENTINEL if any(t == "category" for t in P.types) else None, lower_is_better={"AUC": False},
        stopped_by_size=bool(stopped_by_size))
    return B.finish(task, P, fitted)


def load_model_file(card: dict[str, Any], data: bytes):
    """A CatBoost model from the model file's bytes, once their sha256 matched the card's: written
    to /tmp under the card's name, then read by CatBoost's own reader, in its cbm format."""
    entry = (card.get("boosting") or {}).get("model_file") or {}
    if entry.get("name") != MODEL_NAME:
        raise Refusal("model_file", "the model card names no CatBoost model file: it was not made by a CatBoost node")
    if hashlib.sha256(data).hexdigest() != entry.get("sha256") or len(data) != entry.get("bytes"):
        raise Refusal("model_file_mismatch", f"the model file of {card.get('model') or 'the model'} does not match the "
                                             "fingerprint its card keeps: it is not applied")
    with tempfile.TemporaryDirectory(dir="/tmp" if os.path.isdir("/tmp") else None) as tmp:
        path = os.path.join(tmp, MODEL_NAME)
        with open(path, "wb") as f:
            f.write(data)
        model = _cb().CatBoost()
        model.load_model(path, format="cbm")
        return model


def predict_catboost(task: dict[str, Any], card: dict[str, Any]) -> dict[str, Any]:
    where = os.environ.get(ENV_MODEL_FILE_IN, "")
    if not where:
        raise Refusal("no_model_file", f"{card.get('model') or 'the model'} has no model file among its files")
    model = load_model_file(card, read_bytes(where))
    threads = B.run_threads(task)
    return B.predict(task, card, lambda X: raw_scores(model, X, threads))


# ── the module's own tests ──────────────────────────────────────────────────────
from tab_selftest import register  # noqa: E402


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

    return {"model": "catboost", "variant": what, "group": "boosting", "group_version": "selftest", "mode": "fit",
            "output": "rows", "_frame": df, "limits": dict(catalog()["limits"]),
            "params": {"task": what, "outcome": "y", "predictors": ["a", "b", "c", "d"], "event_value": "yes",
                       "max_trees": 60, "max_depth": 3, "learning_rate": 0.2, "early_stopping_rounds": 10, **params}}


def _expect(ok: Any, message: str) -> None:
    if not ok:
        raise AssertionError(message)


def _at(rows: Any) -> dict[str, int]:
    return {k: i for i, k in enumerate(rows.key)}


@register("catboost: SHAP adds up to the raw score on every row and class; the importance sums to 100")
def _test_shap(code: Any) -> None:
    for what in ("regression", "binary", "multiclass"):
        df = synthetic(300, what, seed=2)
        out = fit_catboost(_task(what, df))
        model = load_model_file(out["card"], out["model_file"]["data"])
        X, _ = B.card_frame(out["card"], df)
        sv = np.asarray(model.get_feature_importance(data=pool(X), type="ShapValues", thread_count=2), dtype=np.float64)
        raw = raw_scores(model, X, 2)
        _expect(sv.shape == ((300, 5) if what != "multiclass" else (300, 3, 5)), f"{what}: ShapValues' shape {sv.shape}")
        gap = np.abs(sv.sum(axis=-1) - raw).max()
        _expect(gap <= 1e-6, f"{what}: Σφ + φ0 − the raw score up to {gap:.3g}")
        rows = out["rows"]
        at = _at(rows)
        g = [rows.value[at[f"importance|{c}||gain"]] for c in "abcd"]
        _expect(abs(sum(g) - 100) <= 1e-9, f"{what}: PredictionValuesChange sums to {sum(g)}")
        _expect(rows.text[at["model|||importance_type"]] == "PredictionValuesChange" and rows.text[at["model|||library"]] == "catboost",
                f"{what}: the texts")
        _expect(rows.value[at["model|||n_categorical"]] == 2, f"{what}: the category and the boolean are native categories")


@register("catboost: the model file read back predicts bit for bit; Predict checks its fingerprint")
def _test_model_file(code: Any) -> None:
    import json
    import warnings

    for what in ("regression", "binary", "multiclass"):
        df = synthetic(250, what, seed=3)
        out = fit_catboost(_task(what, df))
        o = out["output"]
        card = json.loads(json.dumps(out["card"]))
        b = card["boosting"]
        _expect(b["library"] == "catboost" and b["model_file"]["name"] == MODEL_NAME and b["missing_level"] == SENTINEL
                and b["trees"] == b["best_iteration"] + 1, f"{what}: the card's boosting object {b}")
        old = os.environ.get(ENV_MODEL_FILE_IN)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "model_file")  # the Job's name: no extension
                with open(path, "wb") as f:
                    f.write(out["model_file"]["data"])
                os.environ[ENV_MODEL_FILE_IN] = path
                new = synthetic(250, what, seed=3, unseen=True)
                with warnings.catch_warnings(record=True) as said:
                    warnings.simplefilter("always")
                    pred = predict_catboost({"_frame": df, "params": {}, "group_version": "selftest"}, card)["output"]
                    unseen = predict_catboost({"_frame": new, "params": {}, "group_version": "selftest"}, card)["output"]
                col = {"regression": "pred_value", "binary": "pred_prob", "multiclass": "pred_prob"}[what]
                mine = "prediction" if what != "multiclass" else "prediction_probability"
                _expect(np.array_equal(pred[mine].to_numpy(dtype=float), o[col].to_numpy(dtype=float)),
                        f"{what}: Predict from the file differs from the fit")
                if what == "multiclass":
                    _expect(list(pred["prediction"]) == list(o["pred_class"]), f"{what}: the classes differ")
                _expect(any("did not see" in str(w.message) and "«c»" in str(w.message) for w in said), f"{what}: no warning")
                missing = new.copy()
                missing.loc[:9, "c"] = None
                again = predict_catboost({"_frame": missing, "params": {}, "group_version": "selftest"}, card)["output"]
                _expect(np.array_equal(unseen[mine].to_numpy(dtype=float)[:10], again[mine].to_numpy(dtype=float)[:10]),
                        f"{what}: an unseen level does not take the missing value's path")
                bad = dict(card, boosting=dict(b, model_file=dict(b["model_file"], sha256="0" * 64)))
                for wrong, code_ in ((bad, "model_file_mismatch"),
                                     (dict(card, boosting=dict(b, model_file=dict(b["model_file"], name="../x.cbm"))), "model_file")):
                    try:
                        predict_catboost({"_frame": df, "params": {}}, wrong)
                        raise AssertionError(f"{code_}: applied")
                    except Refusal as e:
                        _expect(e.code == code_, e.code)
                infinite = df.copy()
                infinite.loc[3, "b"] = -np.inf  # CatBoost would take it silently: refused, as XGBoost's Predict
                try:
                    predict_catboost({"_frame": infinite, "params": {}}, card)
                    raise AssertionError("an infinite value was predicted")
                except Refusal as e:
                    _expect(e.code == "infinite" and e.params == {"column": "b"}, f"{e.code}: {e.message}")
        finally:
            if old is None:
                os.environ.pop(ENV_MODEL_FILE_IN, None)
            else:
                os.environ[ENV_MODEL_FILE_IN] = old


@register("catboost: two fits of one seed are the same bit for bit, the model file too; 1 and 4 threads give the same numbers")
def _test_determinism(code: Any) -> None:
    for what in ("regression", "binary", "multiclass"):
        df = synthetic(300, what, seed=4)
        one, two = fit_catboost(_task(what, df)), fit_catboost(_task(what, df))
        r1, r2 = one["rows"], two["rows"]
        _expect(r1.key == r2.key and r1.text == r2.text, f"{what}: the rows differ")
        for col in ("value", "x0", "x1"):
            _expect(np.array_equal(np.asarray(getattr(r1, col), dtype=float), np.asarray(getattr(r2, col), dtype=float),
                                   equal_nan=True), f"{what}: the numbers differ ({col})")
        _expect(one["model_file"]["data"] == two["model_file"]["data"], f"{what}: the model files differ")
        _expect(one["output"].equals(two["output"]), f"{what}: the rows output differs")
    df = synthetic(300, "binary", seed=4)
    t1, t4 = _task("binary", df), _task("binary", df)
    t1["group_version"] = t4["group_version"] = 1
    t1["mode"] = t4["mode"] = "fit"
    old = os.environ.get("OMP_NUM_THREADS")
    try:
        os.environ["OMP_NUM_THREADS"] = "1"
        a = fit_catboost(t1)
        os.environ["OMP_NUM_THREADS"] = "4"
        b = fit_catboost(t4)
    finally:
        if old is None:
            os.environ.pop("OMP_NUM_THREADS", None)
        else:
            os.environ["OMP_NUM_THREADS"] = old
    at = _at(a["rows"])
    _expect(a["rows"].value[at["model|||threads"]] == 1 and _at(b["rows"]) == at and b["rows"].value[at["model|||threads"]] == 4,
            "the threads")
    keep = [i for i, k in enumerate(a["rows"].key) if k != "model|||threads"]
    va, vb = np.asarray(a["rows"].value, dtype=float)[keep], np.asarray(b["rows"].value, dtype=float)[keep]
    _expect(np.array_equal(va, vb, equal_nan=True), "1 and 4 threads: the numbers differ")


@register("catboost: categories native with the missing sentinel, has_time in time order, the stop metrics, the time budget, "
          "the start at the class shares")
def _test_options(code: Any) -> None:
    df = synthetic(300, "binary", seed=6)
    bad = df.copy()
    bad.loc[5, "c"] = SENTINEL
    try:
        fit_catboost(_task("binary", bad))
        raise AssertionError("a column holding the sentinel was not refused")
    except Refusal as e:
        _expect(e.code == "missing_sentinel" and "«c»" in e.message, e.code)
    try:  # CatBoost stopped on «All features are either constant or ignored»: a refusal with a sentence instead
        fit_catboost(_task("binary", df.assign(a=1.0, b=0.5, c="n", d=True)))
        raise AssertionError("predictors that never vary were not refused")
    except Refusal as e:
        _expect(e.code == "constant_predictors", f"{e.code}: {e.message}")
    P = B.prepare(_task("binary", df))
    _expect(list(cat_frame(P.X)["v3"][df["c"].isna().to_numpy()]) == [SENTINEL] * int(df["c"].isna().sum()), "the sentinel")
    _expect(model_params(P)["has_time"] is False and model_params(P)["nan_mode"] == "Min", "has_time without time")
    timed = df.assign(t=np.arange(300)[::-1] * 1.0)
    T = _task("binary", timed, validation="time", order_by="t")
    PT = B.prepare(T)
    _expect(model_params(PT)["has_time"] is True and np.all(np.diff(timed["t"].to_numpy()[PT.train_order]) > 0),
            "has_time: the training rows in time order")
    rows = fit_catboost(T)["rows"]
    at = _at(rows)
    _expect(rows.text[at["model|||validation"]] == "time" and "model|||note_time_split" in at, "the split in time")
    for what, metric, key in (("binary", "auc", "AUC"), ("binary", "error", "ZeroOneLoss"), ("regression", "mae", "MAE"),
                              ("multiclass", "error", "ZeroOneLoss")):
        rows = fit_catboost(_task(what, synthetic(300, what, seed=7), stop_metric=metric))["rows"]
        at = _at(rows)
        _expect(rows.text[at["model|||stop_metric"]] == key, f"{what} {metric}: {rows.text[at['model|||stop_metric']]}")
        for part in ("train", "validation"):
            _expect(f"curve|learning_stop|{part}|y|0" in at and f"curve|learning_loss|{part}|y|0" in at,
                    f"{what} {metric}: no {part} curve")
    for what, metric in (("binary", "mae"), ("regression", "auc"), ("regression", "error"), ("multiclass", "auc")):
        try:
            fit_catboost(_task(what, synthetic(300, what, seed=7), stop_metric=metric))
            raise AssertionError(f"{what} with stop_metric {metric} was not refused")
        except Refusal:
            pass
    task = _task("regression", synthetic(300, "regression", seed=5), max_trees=5000, learning_rate=0.001,
                 early_stopping_rounds=1000)
    task["limits"]["time_budget_seconds"] = 0.5
    rows = fit_catboost(task)["rows"]
    at = _at(rows)
    _expect(rows.value[at["model|||stopped_by_time"]] == 1 and "model|||note_time_budget" in at, "the time budget")
    _expect(rows.text[at["assumption|trees_limit||verdict"]] == "doubtful", "trees_limit after the time budget")
    _expect(rows.value[at["model|||trees"]] < 5000, "the time budget kept every tree")
    for what in ("binary", "multiclass"):  # the start: the logit of the event's share, the log of the class shares
        t = _task(what, synthetic(300, what, seed=9))
        out = fit_catboost(t)
        P = B.prepare(t)
        bias = np.atleast_1d(np.asarray(load_model_file(out["card"], out["model_file"]["data"]).get_scale_and_bias()[1], dtype=float))
        counts = np.bincount(P.y[~P.validation], minlength=P.k if what == "multiclass" else 2).astype(float)
        want = np.log(counts / counts.sum()) if what == "multiclass" else np.log(counts[1:] / counts[:1])
        _expect(np.allclose(bias, want, rtol=0, atol=1e-6), f"{what}: the model starts at {bias}, not at {want}")
    off = fit_catboost(_task("regression", synthetic(300, "regression", seed=5), early_stopping=False, max_trees=40))["rows"]
    at = _at(off)
    _expect(off.value[at["model|||trees"]] == 40 and off.value[at["model|||best_iteration"]] == 39
            and off.value[at["model|||stopped_early"]] == 0, "without early stopping every tree is kept")


@register("catboost: a fit writes nothing outside /tmp, and nothing left in it")
def _test_no_files(code: Any) -> None:
    tmp = tempfile.gettempdir()
    before = set(os.listdir(tmp))
    here = set(os.listdir("."))
    fit_catboost(_task("multiclass", synthetic(200, "multiclass", seed=8)))
    _expect(set(os.listdir(tmp)) == before, f"left in {tmp}: {sorted(set(os.listdir(tmp)) - before)}")
    _expect(set(os.listdir(".")) == here, f"written in the working folder: {sorted(set(os.listdir('.')) - here)}")
    _expect(not os.path.exists(TRAIN_DIR), f"{TRAIN_DIR} was made")


@register("catboost: the trees are capped so that the model file stays within its limit; a tree's bytes are bounded")
def _test_model_size(code: Any) -> None:
    for what, depth in (("regression", 3), ("binary", 6), ("multiclass", 6)):
        df = synthetic(300, what, seed=6)
        sizes = []
        for trees in (10, 30):
            out = fit_catboost(_task(what, df, max_trees=trees, max_depth=depth, early_stopping=False))
            sizes.append(len(out["model_file"]["data"]))
        k = 3 if what == "multiclass" else 1
        per = (sizes[1] - sizes[0]) / 20
        _expect(per <= tree_bytes(depth, k), f"{what} depth {depth}: {per:.0f} bytes a tree, bounded by {tree_bytes(depth, k)}")
    task = _task("multiclass", synthetic(300, "multiclass", seed=6), max_trees=400, max_depth=6, early_stopping=False)
    task["limits"]["max_model_file_bytes"] = 100_000
    out = fit_catboost(task)
    rows, data = out["rows"], out["model_file"]["data"]
    at = _at(rows)
    want = int(B.MODEL_SIZE_SHARE * 100_000 // tree_bytes(6, 3))
    _expect(len(data) <= 100_000 and rows.value[at["model|||trees"]] == want and "model|||note_model_size" in at,
            f"a model file of {len(data)} bytes, {rows.value[at['model|||trees']]} trees ({want} expected)")
    _expect(rows.text[at["assumption|trees_limit||advice_id"]] == "trees_size", "trees_limit")


@register("catboost: with class_weight = balanced early stopping and beats_baseline read one loss, weighted as the fit")
def _test_balanced(code: Any) -> None:
    task = _task("binary", synthetic(400, "binary", seed=8), class_weight="balanced")
    out = fit_catboost(task)
    P = B.prepare(task)
    rows = out["rows"]
    at = {k: i for i, k in enumerate(rows.key)}
    best = rows.value[at["model|||best_iteration"]] + 1
    [curve] = [rows.value[i] for i, k in enumerate(rows.key) if k.startswith("curve|learning_loss|validation|y|") and rows.x0[i] == best]
    va = P.valid
    prob = out["output"]["pred_prob"].to_numpy(dtype=float)[np.flatnonzero(P.keep)][va]
    mine = B.logloss(P.y[va].astype(float), prob, P.fit_w[va])
    _expect(abs(curve - mine) <= 1e-5 * mine, f"the validation curve {curve} is not the loss weighted as the fit {mine}")
    even = B.logloss(P.y[va].astype(float), np.full(len(va), 0.5), P.fit_w[va])  # the best constant of balanced weights
    u = rows.value[at["assumption|beats_baseline||value"]]
    _expect(abs(u - (1 - mine / even)) <= 1e-9, f"beats_baseline {u}, by hand {1 - mine / even}")
    _expect(rows.text[at["assumption|beats_baseline||h0"]] == B.H0["beats_baseline_balanced"], "the h0 of balanced weights")
